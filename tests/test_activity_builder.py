"""Tests for the pure Discord activity builder.

Synthetic ``AgentSnapshot`` inputs keep the suite hermetic: no Orca runtime,
Discord IPC or filesystem is involved.
"""

from __future__ import annotations

import dataclasses

import pytest

from orca_presence.activity_builder import (
    DEFAULT_LARGE_IMAGE,
    DETAILS,
    IDLE_STATE,
    LARGE_IMAGE_ENV,
    LARGE_TEXT,
    DiscordActivity,
    build_activity,
)
from orca_presence.models import AgentSnapshot

START_TS = 1_789_549_800_000


def _snapshot(
    active: int = 0, blocked: int = 0, total: int | None = None
) -> AgentSnapshot:
    if total is None:
        total = active + blocked
    return AgentSnapshot(active=active, blocked=blocked, total=total)


def test_returns_a_frozen_dataclass() -> None:
    activity = build_activity(_snapshot(1, 0), START_TS)

    assert isinstance(activity, DiscordActivity)
    assert dataclasses.is_dataclass(activity)
    field_name = "state"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(activity, field_name, "mutated")


def test_details_are_static() -> None:
    assert DETAILS == "Playing Orca ADE"
    assert build_activity(_snapshot(0, 0, 0), START_TS).details == "Playing Orca ADE"


def test_large_text_is_static() -> None:
    assert LARGE_TEXT == "Orca Dev ADE"
    assert build_activity(_snapshot(0, 0, 0), START_TS).large_text == "Orca Dev ADE"


@pytest.mark.parametrize(
    ("active", "blocked", "total", "expected"),
    [
        (1, 0, 1, "1 active, 0 blocked"),
        (0, 1, 1, "0 active, 1 blocked"),
        (1, 1, 2, "1 active, 1 blocked"),
        (3, 2, 9, "3 active, 2 blocked"),
        (4, 0, 4, "4 active, 0 blocked"),
        (0, 3, 3, "0 active, 3 blocked"),
    ],
)
def test_state_counts_active_and_blocked_agents(
    active: int, blocked: int, total: int, expected: str
) -> None:
    activity = build_activity(_snapshot(active, blocked, total), START_TS)

    assert activity.state == expected


@pytest.mark.parametrize(
    ("active", "blocked", "total"),
    [
        (0, 0, 0),
        (0, 0, 1),
        (0, 0, 7),
    ],
)
def test_state_is_idle_when_nothing_is_active_or_blocked(
    active: int, blocked: int, total: int
) -> None:
    assert IDLE_STATE == "Idle"
    activity = build_activity(_snapshot(active, blocked, total), START_TS)

    assert activity.state == "Idle"
    assert activity.state != ""


@pytest.mark.parametrize(
    ("active", "blocked", "total"),
    [
        (0, 0, 0),
        (0, 0, 5),
        (1, 0, 1),
        (0, 1, 1),
        (10, 4, 42),
    ],
)
def test_state_is_never_empty(active: int, blocked: int, total: int) -> None:
    assert build_activity(_snapshot(active, blocked, total), START_TS).state


@pytest.mark.parametrize(
    ("active", "blocked", "total", "expected"),
    [
        (1, 0, 1, (1, 1)),
        (2, 0, 2, (2, 2)),
        (2, 1, 5, (2, 5)),
        (1, 1, 4, (1, 4)),
        (0, 2, 2, (0, 2)),
        (0, 0, 3, (0, 3)),
    ],
)
def test_party_size_is_active_of_total(
    active: int, blocked: int, total: int, expected: tuple[int, int]
) -> None:
    activity = build_activity(_snapshot(active, blocked, total), START_TS)

    assert activity.party_size == expected


def test_party_size_is_absent_for_an_empty_snapshot() -> None:
    assert build_activity(_snapshot(0, 0, 0), START_TS).party_size is None


@pytest.mark.parametrize("start_ts", [0, 1, START_TS, 2**63 - 1])
def test_start_timestamp_is_preserved(start_ts: int) -> None:
    activity = build_activity(_snapshot(1, 0), start_ts)

    assert activity.start_ts == start_ts
    assert activity.to_payload()["timestamps"] == {"start": start_ts}


def test_large_image_defaults_to_the_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LARGE_IMAGE_ENV, raising=False)

    activity = build_activity(_snapshot(1, 0), START_TS)

    assert LARGE_IMAGE_ENV == "ORCA_PRESENCE_LARGE_IMAGE"
    assert DEFAULT_LARGE_IMAGE == "orca_ade"
    assert activity.large_image == "orca_ade"


def test_large_image_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LARGE_IMAGE_ENV, "custom_orca_asset")

    activity = build_activity(_snapshot(1, 0), START_TS)

    assert activity.large_image == "custom_orca_asset"


def test_large_image_empty_environment_falls_back_to_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LARGE_IMAGE_ENV, "")

    activity = build_activity(_snapshot(1, 0), START_TS)

    assert activity.large_image == DEFAULT_LARGE_IMAGE


def test_build_activity_is_deterministic_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LARGE_IMAGE_ENV, "asset_x")
    snapshot = _snapshot(2, 1, 6)

    first = build_activity(snapshot, START_TS)
    second = build_activity(snapshot, START_TS)

    assert first == second
    assert snapshot == _snapshot(2, 1, 6)
    assert snapshot.active == 2
    assert snapshot.blocked == 1
    assert snapshot.total == 6


def test_to_payload_matches_the_discord_activity_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LARGE_IMAGE_ENV, "asset_x")

    payload = build_activity(_snapshot(2, 1, 5), START_TS).to_payload()

    assert payload == {
        "details": "Playing Orca ADE",
        "state": "2 active, 1 blocked",
        "timestamps": {"start": START_TS},
        "assets": {"large_image": "asset_x", "large_text": "Orca Dev ADE"},
        "party": {"size": [2, 5]},
    }


def test_to_payload_omits_party_when_there_are_no_agents() -> None:
    payload = build_activity(_snapshot(0, 0, 0), START_TS).to_payload()

    assert "party" not in payload
    assert set(payload) == {"details", "state", "timestamps", "assets"}


def test_payload_does_not_claim_token_totals() -> None:
    """The Orca CLI exposes no token data (SPIKE.md), so none is advertised."""

    payload = build_activity(_snapshot(3, 1, 8), START_TS).to_payload()

    assert not any("token" in key.lower() for key in payload)
