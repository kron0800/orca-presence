"""Tests for the pure Discord activity builder.

Synthetic ``AgentSnapshot`` inputs keep the suite hermetic: no Orca runtime,
Discord IPC or filesystem is involved.
"""

from __future__ import annotations

import dataclasses

import pytest

from orca_presence.activity_builder import (
    DEFAULT_LARGE_IMAGE,
    IDLE_STATE,
    LARGE_IMAGE_ENV,
    LARGE_TEXT,
    TOKENS_SUFFIX,
    USAGE_PREFIX,
    USAGE_SEPARATOR,
    DiscordActivity,
    build_activity,
    compose_state,
    format_activity,
    format_tokens,
    format_usage,
)
from orca_presence.models import AgentSnapshot
from orca_presence.usage_client import UsageSnapshot

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


def test_details_report_the_agent_counts() -> None:
    activity = build_activity(_snapshot(2, 1, 3), START_TS)

    assert activity.details == "2 active, 1 blocked"
    assert activity.state is None


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
def test_details_count_active_and_blocked_agents(
    active: int, blocked: int, total: int, expected: str
) -> None:
    assert format_activity(_snapshot(active, blocked, total)) == expected
    assert (
        build_activity(_snapshot(active, blocked, total), START_TS).details == expected
    )


@pytest.mark.parametrize(
    ("active", "blocked", "total"),
    [
        (0, 0, 0),
        (0, 0, 1),
        (0, 0, 7),
    ],
)
def test_details_are_idle_when_nothing_is_active_or_blocked(
    active: int, blocked: int, total: int
) -> None:
    assert IDLE_STATE == "No agents running"
    activity = build_activity(_snapshot(active, blocked, total), START_TS)

    assert activity.details == "No agents running"
    assert activity.details != ""


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
def test_details_are_never_empty(active: int, blocked: int, total: int) -> None:
    assert build_activity(_snapshot(active, blocked, total), START_TS).details


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
        "details": "2 active, 1 blocked",
        "timestamps": {"start": START_TS},
        "assets": {"large_image": "asset_x", "large_text": "Orca Dev ADE"},
        "party": {"size": [2, 5]},
    }


def test_to_payload_omits_party_when_there_are_no_agents() -> None:
    payload = build_activity(_snapshot(0, 0, 0), START_TS).to_payload()

    assert "party" not in payload
    assert "state" not in payload
    assert set(payload) == {"details", "timestamps", "assets"}


def test_payload_does_not_advertise_usage_by_default() -> None:
    """Without a usage snapshot (the default), no token data is published."""

    payload = build_activity(_snapshot(3, 1, 8), START_TS).to_payload()

    assert "state" not in payload
    assert not any("token" in key.lower() for key in payload)


# --- usage formatting --------------------------------------------------------


def _usage(
    tokens: int = 1_780_899, cost_usd: float = 0.59, count: int = 3
) -> UsageSnapshot:
    return UsageSnapshot(tokens=tokens, cost_usd=cost_usd, count=count)


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (0, "0"),
        (7, "7"),
        (999, "999"),
        (1_000, "1.0K"),
        (25_000, "25.0K"),
        (999_500, "999.5K"),
        (1_000_000, "1.00M"),
        (1_780_899, "1.78M"),
        (12_345_678, "12.35M"),
        (2_500_000_000, "2.50B"),
    ],
)
def test_format_tokens_uses_human_units(tokens: int, expected: str) -> None:
    assert format_tokens(tokens) == expected


def test_format_usage_omits_cost_by_default() -> None:
    assert USAGE_PREFIX == "Usage: "
    assert USAGE_SEPARATOR == " · "
    assert TOKENS_SUFFIX == " tok"
    assert format_usage(_usage()) == "Usage: 1.78M tok"


def test_format_usage_appends_cost_when_enabled() -> None:
    assert format_usage(_usage(), show_cost=True) == "Usage: 1.78M tok · USD 0.59"


def test_compose_state_formats_the_usage_line() -> None:
    assert compose_state(_usage()) == "Usage: 1.78M tok"


def test_compose_state_includes_the_cost_when_enabled() -> None:
    assert compose_state(_usage(), show_cost=True) == "Usage: 1.78M tok · USD 0.59"


@pytest.mark.parametrize(
    "usage",
    [
        None,
        UsageSnapshot(tokens=0, cost_usd=0.0, count=0),
        UsageSnapshot(tokens=0, cost_usd=0.0, count=4),
    ],
)
def test_compose_state_without_usage_data_is_none(
    usage: UsageSnapshot | None,
) -> None:
    assert compose_state(usage) is None


def test_build_activity_includes_usage_only_when_passed() -> None:
    plain = build_activity(_snapshot(2, 0, 2), START_TS)
    enriched = build_activity(_snapshot(2, 0, 2), START_TS, _usage())

    assert plain.details == "2 active, 0 blocked"
    assert plain.state is None
    assert enriched.details == "2 active, 0 blocked"
    assert enriched.state == "Usage: 1.78M tok"


def test_build_activity_usage_payload_has_no_double_quotes() -> None:
    payload = build_activity(
        _snapshot(2, 0, 2), START_TS, _usage(), show_cost=True
    ).to_payload()

    assert '"' not in str(payload["state"])
    assert payload["state"] == "Usage: 1.78M tok · USD 0.59"


def test_build_activity_with_usage_is_deterministic() -> None:
    usage = _usage()

    first = build_activity(_snapshot(2, 0, 2), START_TS, usage, show_cost=True)
    second = build_activity(_snapshot(2, 0, 2), START_TS, usage, show_cost=True)

    assert first == second
