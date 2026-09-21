"""Pure construction of the Discord Rich Presence activity payload.

This module maps an :class:`orca_presence.models.AgentSnapshot` to a frozen
:class:`DiscordActivity` describing what the Orca ADE runtime is doing. It is a
pure transformation: no network, filesystem or Discord IPC access happens here,
and the only environment interaction is reading the optional
``ORCA_PRESENCE_LARGE_IMAGE`` asset override. Callers own the actual transport
and can render the wire body with :meth:`DiscordActivity.to_payload`.

Discord semantics
-----------------
* ``details`` reports the agent summary: ``N active, M blocked``, or
  ``No agents running`` when no agent is active or blocked (``waiting`` /
  ``done`` agents alone do not count as activity).
* ``state`` reports the OpenCode usage only, prefixed with ``Usage:`` and
  optionally carrying the estimated cost (``Usage: 1.78M tok · USD 0.59``).
  Without a usage snapshot the field is omitted from the payload entirely, so
  the presence degrades to the details line.
* ``party.size`` carries ``[active, total]`` so Discord can render
  ``current of max``; it is omitted when the snapshot has no agents at all.
* ``timestamps.start`` is the caller-provided ``start_ts`` and
  ``assets.large_image`` comes from the environment or the placeholder asset.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from orca_presence.models import AgentSnapshot
from orca_presence.usage_client import UsageSnapshot

__all__ = [
    "DEFAULT_LARGE_IMAGE",
    "IDLE_STATE",
    "LARGE_IMAGE_ENV",
    "LARGE_TEXT",
    "TOKENS_SUFFIX",
    "USAGE_PREFIX",
    "USAGE_SEPARATOR",
    "DiscordActivity",
    "build_activity",
    "compose_state",
    "format_activity",
    "format_tokens",
    "format_usage",
]

LARGE_TEXT = "Orca Dev ADE"
IDLE_STATE = "No agents running"
DEFAULT_LARGE_IMAGE = "orca_ade"
LARGE_IMAGE_ENV = "ORCA_PRESENCE_LARGE_IMAGE"

# The usage line is prefixed with ``Usage:`` and joined with a middle dot; the
# token suffix matches the documented state format.
USAGE_PREFIX = "Usage: "
USAGE_SEPARATOR = " · "
TOKENS_SUFFIX = " tok"


@dataclass(frozen=True, slots=True)
class DiscordActivity:
    """Immutable Discord Rich Presence activity.

    ``details`` carries the agent summary and ``state`` the optional usage
    line; a ``None`` state means the field is omitted from the wire payload.
    ``party_size`` is the ``(current, max)`` pair behind Discord's
    ``party.size`` field and is ``None`` when there are no agents to show.
    """

    details: str
    state: str | None
    start_ts: int
    large_image: str
    large_text: str
    party_size: tuple[int, int] | None = None

    def to_payload(self) -> dict[str, object]:
        """Render the JSON-ready activity body for a Discord SET_ACTIVITY call."""

        payload: dict[str, object] = {
            "details": self.details,
            "timestamps": {"start": self.start_ts},
            "assets": {
                "large_image": self.large_image,
                "large_text": self.large_text,
            },
        }
        if self.state is not None:
            payload["state"] = self.state
        if self.party_size is not None:
            payload["party"] = {"size": [self.party_size[0], self.party_size[1]]}
        return payload


def format_tokens(tokens: int) -> str:
    """Format a token count with human units: ``25000`` -> ``25.0K``."""

    if tokens >= 1_000_000_000:
        return f"{tokens / 1_000_000_000:.2f}B"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.2f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}K"
    return str(tokens)


def format_activity(snapshot: AgentSnapshot) -> str:
    """Format the details line: the live counts or the idle summary."""

    if snapshot.active + snapshot.blocked == 0:
        return IDLE_STATE
    return f"{snapshot.active} active, {snapshot.blocked} blocked"


def format_usage(usage: UsageSnapshot, *, show_cost: bool = False) -> str:
    """Format the state line, e.g. ``Usage: 1.78M tok · USD 0.59``."""

    parts = [f"{format_tokens(usage.tokens)}{TOKENS_SUFFIX}"]
    if show_cost:
        parts.append(f"USD {usage.cost_usd:.2f}")
    return f"{USAGE_PREFIX}{USAGE_SEPARATOR.join(parts)}"


def compose_state(
    usage: UsageSnapshot | None = None,
    *,
    show_cost: bool = False,
) -> str | None:
    """Compose the optional state line from a usage snapshot.

    Returns ``None`` when no snapshot is available or the counting window is
    empty, so the details line is published on its own.
    """

    if usage is None or (usage.tokens <= 0 and usage.cost_usd <= 0):
        return None
    return format_usage(usage, show_cost=show_cost)


def build_activity(
    snapshot: AgentSnapshot,
    start_ts: int,
    usage: UsageSnapshot | None = None,
    *,
    show_cost: bool = False,
) -> DiscordActivity:
    """Build the Discord activity for ``snapshot``.

    Pure function: for the same snapshot, usage and environment it always
    returns an equal activity, never mutating its inputs or the process
    environment. ``details`` carries the agent summary and ``usage`` (only
    passed while usage tracking is enabled) fills the optional state line.
    """

    party_size = (snapshot.active, snapshot.total) if snapshot.total > 0 else None

    return DiscordActivity(
        details=format_activity(snapshot),
        state=compose_state(usage, show_cost=show_cost),
        start_ts=start_ts,
        large_image=os.environ.get(LARGE_IMAGE_ENV) or DEFAULT_LARGE_IMAGE,
        large_text=LARGE_TEXT,
        party_size=party_size,
    )
