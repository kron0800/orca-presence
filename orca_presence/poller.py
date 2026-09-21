"""Async poller that mirrors Orca agent activity into Discord Rich Presence.

:class:`Poller` owns the daemon's two long-lived coroutines and runs them in a
single :class:`asyncio.TaskGroup` (Python 3.11):

* the poll loop reads the live agents from ``orca worktree ps --json`` through
  :class:`~orca_presence.orca_client.OrcaClient` every
  ``config.poll_interval_seconds`` seconds, normalizes them into an
  :class:`~orca_presence.models.AgentSnapshot` and builds the matching
  :class:`~orca_presence.activity_builder.DiscordActivity`;
* the Discord loop keeps the IPC connection alive through
  :meth:`~orca_presence.discord_client.DiscordPresenceClient.maintain` and
  publishes the latest activity through
  :meth:`~orca_presence.discord_client.DiscordPresenceClient.publish`.

The two tasks never call each other directly. The poll loop stores every
activity in :class:`_ActivityState`, an ``asyncio.Lock``-protected slot with an
``asyncio.Event`` wake-up, and the Discord loop waits on that state: a slow or
disconnected Discord therefore never blocks the Orca polling cadence, and a
failed publish never loses the newest activity because
:meth:`~orca_presence.discord_client.DiscordPresenceClient.publish` remembers
it and republishes it after reconnecting.

Startup behaviour
-----------------
:meth:`Poller.run` first reads ``orca agent hooks status`` and, when the hooks
look disabled, tries ``orca agent hooks on`` once. That attempt is best effort:
any failure is logged at DEBUG and the poller keeps running. Live counts always
come from ``orca worktree ps`` regardless of the hook state, as decided in
``SPIKE.md`` section 6.

Failure behaviour
-----------------
The poll loop never dies from an Orca CLI failure. When the runtime is
explicitly unreachable (``runtime_unavailable``) the presence switches to
``No agents running``, because no agent can be running; a missing CLI
executable, a timeout
or any other :class:`~orca_presence.orca_client.OrcaError` is logged with its
remediation hint and the previous presence stays in place until the next cycle.
The Discord loop is the opposite:
an unrecoverable transport error (for example a client id rejected by the
handshake) propagates out of the :class:`asyncio.TaskGroup`, so the caller can
log it at ERROR and exit.

Every event is reported through :mod:`logging`; this module never prints.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from orca_presence.activity_builder import DiscordActivity, build_activity
from orca_presence.config import ORCA_EXECUTABLE_ENV, Config
from orca_presence.discord_client import DiscordPresenceClient
from orca_presence.models import AgentHooksStatusPayload, AgentSnapshot
from orca_presence.orca_client import (
    OrcaClient,
    OrcaCliNotFoundError,
    OrcaError,
    OrcaNotRunningError,
    OrcaTimeoutError,
)

__all__ = ["Poller", "should_enable_agent_hooks"]

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]

# Presence shown while the Orca runtime is unreachable: no agent can be running.
_IDLE_SNAPSHOT = AgentSnapshot(active=0, blocked=0, total=0)


def should_enable_agent_hooks(payload: AgentHooksStatusPayload) -> bool:
    """Return True when an ``agent hooks status`` payload shows hooks disabled.

    The hooks are considered disabled when the master ``enabled`` flag is false
    or when no agent type reports its hooks as ``installed``. A ``partial`` or
    ``error`` state with no installed agent also counts as disabled, so the
    startup repair attempt can fix an incomplete installation; a configuration
    where at least one agent is installed is left alone.
    """

    if not payload.result.enabled:
        return True
    return not any(status.state == "installed" for status in payload.result.statuses)


class _ActivityState:
    """Latest activity shared between the poll loop and the Discord loop.

    The slot is written by the poll loop and read by the Discord loop; both
    sides take :attr:`_lock`, and :attr:`_updated` wakes the Discord loop as
    soon as a new value is stored. Clearing the wake-up under the same lock
    means a value stored while the reader was waking up is never lost: the
    reader always observes the newest activity.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._activity: DiscordActivity | None = None
        self._updated = asyncio.Event()

    async def set(self, activity: DiscordActivity) -> None:
        """Store ``activity`` and wake the Discord loop."""

        async with self._lock:
            self._activity = activity
            self._updated.set()

    async def wait(self) -> DiscordActivity:
        """Block until an activity is stored, then return the latest one."""

        while True:
            await self._updated.wait()
            async with self._lock:
                self._updated.clear()
                activity = self._activity
            if activity is not None:
                return activity


class Poller:
    """Poll Orca and mirror the live agent counts into Discord Rich Presence.

    :param config: resolved settings; ``poll_interval_seconds`` drives both the
        Orca polling cadence and the Discord publish throttle.
    :param orca_client: optional client override; a default one is built from
        the configuration when omitted.
    :param discord_client: optional client override; a default one is built
        from the configuration when omitted.
    :param clock: wall-clock source used for the Discord ``start`` timestamp,
        injectable for tests.
    :param sleep: sleep function used by the poll loop, injectable for tests.

    The poller closes the Discord connection when :meth:`run` returns or is
    cancelled; :meth:`shutdown` does the same explicitly and is idempotent.
    """

    def __init__(
        self,
        config: Config,
        *,
        orca_client: OrcaClient | None = None,
        discord_client: DiscordPresenceClient | None = None,
        clock: Clock = time.time,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._config = config
        self._orca = (
            orca_client
            if orca_client is not None
            else OrcaClient(config.orca_executable, timeout=config.orca_timeout_seconds)
        )
        self._discord = (
            discord_client
            if discord_client is not None
            else DiscordPresenceClient(
                config.client_id, interval=config.poll_interval_seconds
            )
        )
        self._clock = clock
        self._sleep = sleep
        self._state = _ActivityState()
        self._start_ts = int(clock())
        self._orca_online: bool | None = None
        self._last_poll_error: str | None = None

    @property
    def orca_client(self) -> OrcaClient:
        """Client used to read the Orca runtime."""

        return self._orca

    @property
    def discord_client(self) -> DiscordPresenceClient:
        """Client used to publish the Discord presence."""

        return self._discord

    @property
    def start_ts(self) -> int:
        """Unix timestamp published as the Discord activity start time."""

        return self._start_ts

    async def run(self) -> None:
        """Poll Orca and keep Discord updated until cancelled.

        Raises whatever the child tasks raise: a
        :class:`~orca_presence.discord_client.DiscordAuthError` (or any other
        unexpected exception) is wrapped by the :class:`asyncio.TaskGroup` in
        an ``ExceptionGroup``. The Discord connection is closed on the way out,
        including when the task is cancelled.
        """

        self._start_ts = int(self._clock())
        logger.info(
            "Starting the Orca presence poller (poll interval %.1fs)",
            self._config.poll_interval_seconds,
        )
        await self._enable_agent_hooks()
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._poll_loop())
                tasks.create_task(self._discord_loop())
        finally:
            await self._discord.close()
            logger.info("Orca presence poller stopped")

    async def shutdown(self) -> None:
        """Cancel publishing and close the Discord IPC socket.

        Safe to call more than once and after :meth:`run` has already stopped.
        """

        self._discord.cancel()
        await self._discord.close()

    async def _enable_agent_hooks(self) -> None:
        """Best-effort enablement of the Orca-managed agent hooks at startup.

        When the hook status shows the hooks disabled, ``orca agent hooks on``
        is attempted once. Any failure is swallowed at DEBUG level: presence
        falls back to ``orca worktree ps``, which remains the live agent source
        either way.
        """

        try:
            status = await self._orca.agent_hooks_status()
        except OrcaError as exc:
            logger.debug("Could not read the Orca agent hook status: %s", exc)
            return
        if status is None:
            logger.debug("Orca agent hook status is not available")
            return
        if not should_enable_agent_hooks(status):
            logger.debug("Orca agent hooks are already installed")
            return

        logger.info("Orca agent hooks look disabled; trying to enable them")
        try:
            await self._orca.agent_hooks_on()
        except OrcaError as exc:
            logger.debug("Could not enable the Orca agent hooks: %s", exc)
            return
        logger.info("Enabled the Orca agent hooks")

    async def _poll_loop(self) -> None:
        """Read the Orca CLI every interval and store the latest activity."""

        while True:
            cycle_started = self._clock()
            activity = await self._poll_once()
            if activity is not None:
                await self._state.set(activity)
            elapsed = self._clock() - cycle_started
            await self._sleep(max(0.0, self._config.poll_interval_seconds - elapsed))

    async def _poll_once(self) -> DiscordActivity | None:
        """Read one worktree listing and build the matching activity.

        Returns a ``No agents running`` activity when the runtime is unreachable
        and ``None`` for a transient failure, in which case the previous
        presence is kept.
        """

        try:
            payload = await self._orca.worktree_ps()
        except OrcaNotRunningError as exc:
            self._set_orca_online(False)
            logger.debug("Orca runtime is not running: %s", exc)
            return build_activity(_IDLE_SNAPSHOT, self._start_ts)
        except OrcaCliNotFoundError as exc:
            self._report_poll_error(
                f"Orca CLI executable {self._orca.executable!r} was not found or "
                f"could not be started ({exc}); install the Orca CLI or set "
                f"{ORCA_EXECUTABLE_ENV} to its full path"
            )
            return None
        except OrcaTimeoutError as exc:
            self._report_poll_error(f"Orca CLI timed out: {exc}")
            return None
        except OrcaError as exc:
            self._report_poll_error(f"Orca CLI failed: {exc}")
            return None

        if payload is None:
            self._report_poll_error("Orca worktree listing was not usable")
            return None

        self._set_orca_online(True)
        snapshot = AgentSnapshot.from_worktree_ps(payload)
        logger.debug(
            "Orca agents: %d active, %d blocked, %d total",
            snapshot.active,
            snapshot.blocked,
            snapshot.total,
        )
        return build_activity(snapshot, self._start_ts)

    def _set_orca_online(self, online: bool) -> None:
        """Log an INFO transition when the Orca runtime comes or goes away."""

        self._last_poll_error = None
        if self._orca_online == online:
            return
        self._orca_online = online
        if online:
            logger.info("Connected to the Orca runtime")
        else:
            logger.info("Orca runtime is unreachable; waiting for it to come back")

    def _report_poll_error(self, message: str) -> None:
        """Log a poll failure once at WARNING, then only at DEBUG."""

        if message == self._last_poll_error:
            logger.debug("Orca poll still failing: %s", message)
            return
        self._last_poll_error = message
        logger.warning("Orca poll failed: %s", message)

    async def _discord_loop(self) -> None:
        """Keep Discord connected and publish every stored activity.

        The loop ends when either child ends: a fatal ``maintain`` error stops
        the publisher and propagates, and a connection closed from outside
        stops the publisher instead of leaving it waiting for an update that
        can no longer be sent.
        """

        async with asyncio.TaskGroup() as tasks:
            maintain_task = tasks.create_task(self._discord.maintain())
            tasks.create_task(self._publish_loop())
            await maintain_task

    async def _publish_loop(self) -> None:
        """Publish each activity the poll loop stores, newest wins."""

        while True:
            activity = await self._state.wait()
            await self._discord.publish(activity)
