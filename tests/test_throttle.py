"""Tests for the Discord activity publish throttle.

The clock is injected so every timing assertion is deterministic: no sleeping,
no Discord IPC and no Orca runtime are involved.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from orca_presence.activity_builder import DiscordActivity
from orca_presence.throttle import DEFAULT_INTERVAL_SECONDS, ActivityThrottle

START_TS = 1_789_549_800_000


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _activity(
    *,
    details: str = "Playing Orca ADE",
    state: str = "1 active, 0 blocked",
    start_ts: int = START_TS,
    large_image: str = "orca_ade",
    large_text: str = "Orca Dev ADE",
    party_size: tuple[int, int] | None = (1, 1),
) -> DiscordActivity:
    return DiscordActivity(
        details=details,
        state=state,
        start_ts=start_ts,
        large_image=large_image,
        large_text=large_text,
        party_size=party_size,
    )


BASE_ACTIVITY = _activity()

CHANGED_ACTIVITIES = [
    dataclasses.replace(BASE_ACTIVITY, details="Orca ADE is idle"),
    dataclasses.replace(BASE_ACTIVITY, state="2 active, 1 blocked"),
    dataclasses.replace(BASE_ACTIVITY, start_ts=START_TS + 1),
    dataclasses.replace(BASE_ACTIVITY, large_image="other_asset"),
    dataclasses.replace(BASE_ACTIVITY, large_text="Other text"),
    dataclasses.replace(BASE_ACTIVITY, party_size=None),
    dataclasses.replace(BASE_ACTIVITY, party_size=(1, 4)),
]


def _throttle(
    clock: FakeClock, interval: float = DEFAULT_INTERVAL_SECONDS
) -> ActivityThrottle:
    return ActivityThrottle(interval, clock=clock)


def test_default_interval_is_15_seconds() -> None:
    assert DEFAULT_INTERVAL_SECONDS == 15.0
    assert ActivityThrottle().interval == 15.0


def test_interval_is_configurable() -> None:
    assert _throttle(FakeClock(), interval=5.0).interval == 5.0


def test_activities_compare_by_value() -> None:
    first = _activity()
    second = _activity()

    assert first is not second
    assert first == second


def test_first_activity_is_always_published() -> None:
    assert _throttle(FakeClock()).should_publish(BASE_ACTIVITY) is True


def test_identical_activity_is_suppressed_within_the_interval() -> None:
    clock = FakeClock()
    throttle = _throttle(clock)
    throttle.mark_published(BASE_ACTIVITY)

    clock.advance(DEFAULT_INTERVAL_SECONDS - 0.001)

    assert throttle.should_publish(BASE_ACTIVITY) is False


def test_identical_activity_is_republished_at_the_interval_boundary() -> None:
    clock = FakeClock()
    throttle = _throttle(clock)
    throttle.mark_published(BASE_ACTIVITY)

    clock.advance(DEFAULT_INTERVAL_SECONDS)

    assert throttle.should_publish(BASE_ACTIVITY) is True


def test_identical_activity_is_republished_after_the_interval() -> None:
    clock = FakeClock()
    throttle = _throttle(clock)
    throttle.mark_published(BASE_ACTIVITY)

    clock.advance(DEFAULT_INTERVAL_SECONDS * 3)

    assert throttle.should_publish(BASE_ACTIVITY) is True


def test_equal_activity_values_are_suppressed_across_instances() -> None:
    clock = FakeClock()
    throttle = _throttle(clock)
    published = _activity(state="2 active, 1 blocked")
    equal_copy = _activity(state="2 active, 1 blocked")
    throttle.mark_published(published)

    clock.advance(0.001)

    assert published is not equal_copy
    assert throttle.should_publish(equal_copy) is False


@pytest.mark.parametrize("changed", CHANGED_ACTIVITIES)
def test_changed_field_is_published_immediately(changed: DiscordActivity) -> None:
    clock = FakeClock()
    throttle = _throttle(clock)
    throttle.mark_published(BASE_ACTIVITY)

    clock.advance(0.001)

    assert changed != BASE_ACTIVITY
    assert throttle.should_publish(changed) is True


def test_mark_published_restarts_the_interval() -> None:
    clock = FakeClock()
    throttle = _throttle(clock)
    first = _activity(state="1 active, 0 blocked")
    second = _activity(state="2 active, 0 blocked")

    throttle.mark_published(first)
    clock.advance(DEFAULT_INTERVAL_SECONDS)
    assert throttle.should_publish(second) is True
    throttle.mark_published(second)

    clock.advance(0.001)

    assert throttle.should_publish(second) is False


def test_unmarked_activity_stays_pending_after_a_failed_send() -> None:
    clock = FakeClock()
    throttle = _throttle(clock)

    assert throttle.should_publish(BASE_ACTIVITY) is True
    clock.advance(1.0)
    assert throttle.should_publish(BASE_ACTIVITY) is True
    throttle.mark_published(BASE_ACTIVITY)
    assert throttle.should_publish(BASE_ACTIVITY) is False


def test_reset_forces_immediate_republish() -> None:
    clock = FakeClock()
    throttle = _throttle(clock)
    throttle.mark_published(BASE_ACTIVITY)
    clock.advance(0.001)
    assert throttle.should_publish(BASE_ACTIVITY) is False

    throttle.reset()

    assert throttle.should_publish(BASE_ACTIVITY) is True


def test_reset_restarts_the_interval_for_the_next_mark() -> None:
    clock = FakeClock()
    throttle = _throttle(clock)
    throttle.mark_published(BASE_ACTIVITY)
    clock.advance(1.0)

    throttle.reset()
    throttle.mark_published(BASE_ACTIVITY)

    clock.advance(0.001)

    assert throttle.should_publish(BASE_ACTIVITY) is False


def test_cancel_stops_publishing() -> None:
    clock = FakeClock()
    throttle = _throttle(clock)

    throttle.cancel()

    assert throttle.cancelled is True
    assert throttle.should_publish(BASE_ACTIVITY) is False
    clock.advance(DEFAULT_INTERVAL_SECONDS * 10)
    assert throttle.should_publish(BASE_ACTIVITY) is False
    assert throttle.should_publish(_activity(state="9 active, 0 blocked")) is False


def test_cancel_stops_a_previously_published_activity() -> None:
    clock = FakeClock()
    throttle = _throttle(clock)
    throttle.mark_published(BASE_ACTIVITY)
    clock.advance(DEFAULT_INTERVAL_SECONDS)

    throttle.cancel()

    assert throttle.should_publish(BASE_ACTIVITY) is False


def test_reset_re_enables_publishing_after_cancel() -> None:
    throttle = _throttle(FakeClock())
    throttle.cancel()

    throttle.reset()

    assert throttle.cancelled is False
    assert throttle.should_publish(BASE_ACTIVITY) is True


def test_default_clock_suppresses_an_immediate_republish() -> None:
    throttle = ActivityThrottle()
    throttle.mark_published(BASE_ACTIVITY)

    assert throttle.should_publish(BASE_ACTIVITY) is False


@pytest.mark.parametrize("interval", [0.0, -0.0, -1.0, -0.001])
def test_non_positive_interval_is_rejected(interval: float) -> None:
    with pytest.raises(ValueError):
        ActivityThrottle(interval)


@pytest.mark.parametrize("interval", [math.nan, math.inf, -math.inf])
def test_non_finite_interval_is_rejected(interval: float) -> None:
    with pytest.raises(ValueError):
        ActivityThrottle(interval)
