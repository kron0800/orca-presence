"""Async Discord Rich Presence transport with reconnect and throttling.

:class:`DiscordPresenceClient` wraps ``pypresence``'s :class:`AioClient` and
adds the behaviour the poller needs:

* :meth:`DiscordPresenceClient.maintain` keeps a live IPC connection alive,
  reconnecting with exponential backoff from 1s up to a 30s cap. Discord not
  running (no IPC socket, refused pipe, broken pipe) is retryable. A handshake
  rejection of the configured client id is not: it is logged at CRITICAL and
  raised as :class:`DiscordAuthError` so the caller stops instead of spinning.
* after every successful reconnect the throttle is reset so the next activity
  is published immediately. Without that reset, a presence Discord lost during
  the outage would never be restored while the activity value is unchanged;
  this is the same bug the Orca presence PR hit with ``throttle.reset``.
* :meth:`DiscordPresenceClient.publish` de-duplicates and rate-limits updates
  through an :class:`~orca_presence.throttle.ActivityThrottle`, so callers can
  push the current activity every poll cycle.

The Discord application id is read from the ``ORCA_DISCORD_CLIENT_ID``
environment variable and is never hardcoded.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from pypresence import AioClient
from pypresence.exceptions import (
    DiscordError,
    DiscordNotFound,
    InvalidID,
    InvalidPipe,
    PyPresenceException,
)

from orca_presence.activity_builder import DiscordActivity
from orca_presence.throttle import DEFAULT_INTERVAL_SECONDS, ActivityThrottle

__all__ = [
    "BACKOFF_FACTOR",
    "CLIENT_ID_ENV",
    "INITIAL_BACKOFF_SECONDS",
    "INVALID_CLIENT_ID_CODES",
    "MAX_BACKOFF_SECONDS",
    "ClientFactory",
    "DiscordAuthError",
    "DiscordClientError",
    "DiscordConfigError",
    "DiscordNotRunningError",
    "DiscordPresenceClient",
    "SleepFn",
    "next_backoff",
]

logger = logging.getLogger(__name__)

CLIENT_ID_ENV = "ORCA_DISCORD_CLIENT_ID"
INITIAL_BACKOFF_SECONDS: float = 1.0
MAX_BACKOFF_SECONDS: float = 30.0
BACKOFF_FACTOR: float = 2.0

# Discord IPC error code 4000 is returned by the handshake when the application
# id is unknown; no number of reconnects can fix a bad client id.
INVALID_CLIENT_ID_CODES: frozenset[int] = frozenset({4000})

ClientFactory = Callable[[str], AioClient]
SleepFn = Callable[[float], Awaitable[None]]


class DiscordClientError(Exception):
    """Base class for Discord presence failures."""


class DiscordConfigError(DiscordClientError):
    """The presence configuration is unusable; retrying cannot help."""


class DiscordAuthError(DiscordConfigError):
    """Discord rejected the configured client id during the handshake."""


class DiscordNotRunningError(DiscordClientError):
    """Discord is not running or its IPC socket is unavailable (retryable)."""


def next_backoff(current: float) -> float:
    """Return the next reconnect delay, capped at :data:`MAX_BACKOFF_SECONDS`."""

    return min(current * BACKOFF_FACTOR, MAX_BACKOFF_SECONDS)


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _set_activity_kwargs(activity: DiscordActivity) -> dict[str, object]:
    """Flatten an activity into pypresence's ``set_activity`` keyword names.

    :meth:`DiscordActivity.to_payload` is the nested Discord wire body; the
    pypresence call takes the same fields as flat keyword arguments.
    """

    kwargs: dict[str, object] = {
        "details": activity.details,
        "start": activity.start_ts,
        "large_image": activity.large_image,
        "large_text": activity.large_text,
    }
    if activity.state is not None:
        kwargs["state"] = activity.state
    if activity.party_size is not None:
        kwargs["party_size"] = [activity.party_size[0], activity.party_size[1]]
    return kwargs


class DiscordPresenceClient:
    """Maintain a throttled Discord Rich Presence connection.

    :meth:`maintain` is the long-running task. :meth:`publish` may be called
    from another task, including before the first connection is up: the
    activity is remembered and sent as soon as Discord is reachable.
    """

    def __init__(
        self,
        client_id: str | None = None,
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        throttle: ActivityThrottle | None = None,
        client_factory: ClientFactory = AioClient,
        sleep: SleepFn = _default_sleep,
    ) -> None:
        resolved_client_id = (
            client_id if client_id is not None else os.environ.get(CLIENT_ID_ENV)
        )
        if resolved_client_id is None or not resolved_client_id.strip():
            raise DiscordConfigError(
                "Discord client id is not configured; set the "
                f"{CLIENT_ID_ENV} environment variable"
            )
        self._client_id = resolved_client_id.strip()
        self._throttle = (
            throttle if throttle is not None else ActivityThrottle(interval)
        )
        self._client_factory = client_factory
        self._sleep = sleep

        self._client: AioClient | None = None
        self._desired_activity: DiscordActivity | None = None
        self._connected = False
        self._closing = False
        self._lock = asyncio.Lock()
        self._disconnect_event = asyncio.Event()

    @property
    def client_id(self) -> str:
        """Configured Discord application id."""

        return self._client_id

    @property
    def connected(self) -> bool:
        """True while an IPC socket is open and usable."""

        return self._connected

    @property
    def throttle(self) -> ActivityThrottle:
        """Throttle deciding when activity updates reach Discord."""

        return self._throttle

    def reset(self) -> None:
        """Force the next publish to be sent even if the activity is unchanged.

        Called automatically after every reconnect; also re-enables publishing
        stopped by :meth:`cancel`. It does not reopen a closed client.
        """

        self._throttle.reset()

    def cancel(self) -> None:
        """Keep the connection open but stop publishing until :meth:`reset`."""

        self._throttle.cancel()

    async def publish(self, activity: DiscordActivity) -> bool:
        """Send ``activity`` to Discord unless throttled or disconnected.

        The activity is remembered even when it cannot be sent, so the
        reconnect path republishes it as soon as Discord is reachable again.
        Returns True only when the activity was written to the IPC socket.
        """

        self._desired_activity = activity
        async with self._lock:
            client = self._client
            if client is None or not self._connected:
                logger.debug("Discord is not connected; deferring presence update")
                return False
            if not self._throttle.should_publish(activity):
                logger.debug(
                    "Presence update throttled (unchanged within %.1fs)",
                    self._throttle.interval,
                )
                return False
            try:
                await client.set_activity(**_set_activity_kwargs(activity))
            except (PyPresenceException, OSError) as exc:
                logger.warning("Discord presence update failed: %s", exc)
                self._mark_disconnected()
                return False
            self._throttle.mark_published(activity)
            logger.debug("Published presence: %s", activity.details)
            return True

    async def connect(self) -> None:
        """Open a single Discord IPC connection.

        Raises :class:`DiscordNotRunningError` when Discord is unavailable and
        :class:`DiscordAuthError` when the handshake rejects the client id.
        """

        if self._closing:
            raise DiscordClientError("Discord client is closing")

        try:
            client = self._client_factory(self._client_id)
        except DiscordNotFound as exc:
            raise DiscordNotRunningError(
                f"Discord IPC socket not found: {exc}"
            ) from exc
        except (PyPresenceException, OSError) as exc:
            raise DiscordNotRunningError(
                f"Discord IPC connection could not be created: {exc}"
            ) from exc

        try:
            await client.start()
        except asyncio.CancelledError:
            self._teardown_client(client)
            raise
        except DiscordError as exc:
            self._teardown_client(client)
            if exc.code in INVALID_CLIENT_ID_CODES:
                raise DiscordAuthError(
                    f"Discord rejected the client id from {CLIENT_ID_ENV} "
                    f"(code {exc.code}): {exc}"
                ) from exc
            raise DiscordNotRunningError(f"Discord handshake failed: {exc}") from exc
        except (InvalidID, InvalidPipe) as exc:
            # The IPC pipe disappeared while the handshake was in flight.
            self._teardown_client(client)
            raise DiscordNotRunningError(f"Discord connection lost: {exc}") from exc
        except (PyPresenceException, OSError) as exc:
            self._teardown_client(client)
            raise DiscordNotRunningError(f"Discord connection failed: {exc}") from exc

        async with self._lock:
            if self._closing:
                self._teardown_client(client)
                raise DiscordClientError("Discord client is closing")
            self._client = client
            self._connected = True
        logger.info("Connected to Discord IPC (client id %s)", self._client_id)

    async def maintain(self) -> None:
        """Keep the Discord connection alive until :meth:`close` or cancellation.

        Retryable failures back off 1s, 2s, 4s, ... capped at
        :data:`MAX_BACKOFF_SECONDS`. A rejected client id is logged at CRITICAL
        and re-raised as :class:`DiscordAuthError`: retrying cannot succeed, so
        the caller must fix ``ORCA_DISCORD_CLIENT_ID`` and restart.
        """

        backoff = INITIAL_BACKOFF_SECONDS
        while not self._closing:
            try:
                await self.connect()
            except DiscordAuthError:
                logger.critical(
                    "Discord presence disabled: the handshake rejected the client id "
                    "from %s; check that it is a valid Discord application id",
                    CLIENT_ID_ENV,
                )
                raise
            except DiscordNotRunningError as exc:
                logger.warning(
                    "Discord is not running or unreachable (%s); retrying in %.1fs",
                    exc,
                    backoff,
                )
                await self._sleep(backoff)
                backoff = next_backoff(backoff)
                continue
            except DiscordClientError:
                if not self._closing:
                    raise
                break

            backoff = INITIAL_BACKOFF_SECONDS
            self._disconnect_event.clear()
            if not self._throttle.cancelled:
                # Discord may have dropped the presence while disconnected:
                # forget the cached activity so the next publish goes out now.
                self._throttle.reset()
            desired = self._desired_activity
            if desired is not None:
                await self.publish(desired)

            await self._disconnect_event.wait()
            self._disconnect_event.clear()
            if self._closing:
                break
            logger.info("Discord IPC connection lost; reconnecting")
            await self._drop_connection()

    async def close(self) -> None:
        """Stop publishing, clear the presence and close the IPC socket.

        Idempotent. A running :meth:`maintain` task returns after this call.
        """

        if self._closing:
            return
        self._closing = True
        self._throttle.cancel()
        self._disconnect_event.set()
        await self._drop_connection(clear_activity=True)

    def _mark_disconnected(self) -> None:
        """Flag the connection as lost and wake :meth:`maintain` to reconnect."""

        self._connected = False
        self._disconnect_event.set()

    async def _drop_connection(self, *, clear_activity: bool = False) -> None:
        async with self._lock:
            client = self._client
            self._client = None
            self._connected = False
            if client is None:
                return
            if clear_activity:
                try:
                    await client.clear_activity()
                except (PyPresenceException, OSError):
                    logger.debug("Could not clear the Discord activity", exc_info=True)
            self._teardown_client(client)
        logger.debug("Discord IPC connection closed")

    def _teardown_client(self, client: AioClient) -> None:
        """Close an IPC socket, ignoring an already-broken pipe.

        ``AioClient.close`` is not used because in pypresence 4.2.1 it also
        closes the event loop, which would kill the caller's running loop;
        sending the close frame and closing the writer is equivalent.
        """

        writer = getattr(client, "sock_writer", None)
        if writer is None:
            return
        try:
            client.send_data(2, {"v": 1, "client_id": client.client_id})
        except (PyPresenceException, OSError):
            logger.debug("Discord IPC close frame could not be sent", exc_info=True)
        writer.close()
