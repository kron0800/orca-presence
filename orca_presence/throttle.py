"""Publish throttling for Discord Rich Presence activity updates.

The poller rebuilds a :class:`~orca_presence.activity_builder.DiscordActivity`
on every cycle, but Discord only needs an update when something changed. This
module keeps that decision out of the transport: :class:`ActivityThrottle`
compares activities by value (``DiscordActivity`` is a frozen dataclass, so
equality covers every published field) and allows a re-publish once the
configured interval elapsed, which doubles as a liveness heartbeat.

``cancel`` and ``reset`` are explicit state transitions: ``cancel`` stops all
publishes (shutdown path) and ``reset`` forgets the cached activity so the next
publish is sent even when the activity value is identical to the last one. The
latter is required after a Discord reconnect, where Discord may have lost the
presence while the throttle still believes it is current.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from orca_presence.activity_builder import DiscordActivity

__all__ = ["DEFAULT_INTERVAL_SECONDS", "ActivityThrottle"]

DEFAULT_INTERVAL_SECONDS: float = 15.0


class ActivityThrottle:
    """Decide when a Discord activity must be (re)published.

    A publish is allowed when the throttle never published, when the activity
    differs by value from the last published one, or when at least
    :attr:`interval` seconds elapsed since the last publish. The time-based
    re-publish is what keeps a reconnect detectable: an unchanged activity is
    still written to the socket once per interval.

    The throttle is not thread-safe; call it from one event loop task (the
    Discord client already serializes calls with its own lock).
    """

    def __init__(
        self,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError(
                f"interval must be a positive finite number, got {interval!r}"
            )
        self._interval = float(interval)
        self._clock: Callable[[], float] = time.monotonic if clock is None else clock
        self._last_activity: DiscordActivity | None = None
        self._last_published_at: float | None = None
        self._cancelled = False

    @property
    def interval(self) -> float:
        """Seconds after which an unchanged activity is published again."""

        return self._interval

    @property
    def cancelled(self) -> bool:
        """True when :meth:`cancel` was called and :meth:`reset` has not run."""

        return self._cancelled

    def should_publish(self, activity: DiscordActivity) -> bool:
        """Return True when ``activity`` must be sent to Discord now."""

        if self._cancelled:
            return False
        if self._last_activity is None or self._last_activity != activity:
            return True
        if self._last_published_at is None:
            return True
        return self._clock() - self._last_published_at >= self._interval

    def mark_published(self, activity: DiscordActivity) -> None:
        """Record that ``activity`` was successfully written to Discord.

        Call this only after a successful send: an activity that failed to go
        out stays pending and is retried on the next :meth:`should_publish`.
        """

        self._last_activity = activity
        self._last_published_at = self._clock()

    def reset(self) -> None:
        """Forget all state so the next activity is published immediately.

        Required after reconnecting to Discord: the presence may have been
        dropped while the throttle still holds the previous activity. A reset
        also re-enables publishing stopped by :meth:`cancel`.
        """

        self._last_activity = None
        self._last_published_at = None
        self._cancelled = False

    def cancel(self) -> None:
        """Stop publishing until :meth:`reset` is called (shutdown path)."""

        self._cancelled = True
