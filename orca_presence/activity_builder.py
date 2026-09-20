"""Pure construction of the Discord Rich Presence activity payload.

This module maps an :class:`orca_presence.models.AgentSnapshot` to a frozen
:class:`DiscordActivity` describing what the Orca ADE runtime is doing. It is a
pure transformation: no network, filesystem or Discord IPC access happens here,
and the only environment interaction is reading the optional
``ORCA_PRESENCE_LARGE_IMAGE`` asset override. Callers own the actual transport
and can render the wire body with :meth:`DiscordActivity.to_payload`.

Discord semantics
-----------------
* ``details`` always reads ``Playing Orca ADE``.
* ``state`` reports ``"<active> active, <blocked> blocked"``, or ``Idle`` when
  no agent is active or blocked (``waiting`` / ``done`` agents alone do not
  count as activity). The state is never an empty string.
* ``party.size`` carries ``[active, total]`` so Discord can render
  ``current of max``; it is omitted when the snapshot has no agents at all.
* ``timestamps.start`` is the caller-provided ``start_ts`` and
  ``assets.large_image`` comes from the environment or the placeholder asset.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from orca_presence.models import AgentSnapshot

__all__ = [
    "DEFAULT_LARGE_IMAGE",
    "DETAILS",
    "IDLE_STATE",
    "LARGE_IMAGE_ENV",
    "LARGE_TEXT",
    "DiscordActivity",
    "build_activity",
]

DETAILS = "Playing Orca ADE"
LARGE_TEXT = "Orca Dev ADE"
IDLE_STATE = "Idle"
DEFAULT_LARGE_IMAGE = "orca_ade"
LARGE_IMAGE_ENV = "ORCA_PRESENCE_LARGE_IMAGE"


@dataclass(frozen=True, slots=True)
class DiscordActivity:
    """Immutable Discord Rich Presence activity.

    ``party_size`` is the ``(current, max)`` pair behind Discord's
    ``party.size`` field and is ``None`` when there are no agents to show.
    """

    details: str
    state: str
    start_ts: int
    large_image: str
    large_text: str
    party_size: tuple[int, int] | None = None

    def to_payload(self) -> dict[str, object]:
        """Render the JSON-ready activity body for a Discord SET_ACTIVITY call."""

        payload: dict[str, object] = {
            "details": self.details,
            "state": self.state,
            "timestamps": {"start": self.start_ts},
            "assets": {
                "large_image": self.large_image,
                "large_text": self.large_text,
            },
        }
        if self.party_size is not None:
            payload["party"] = {"size": [self.party_size[0], self.party_size[1]]}
        return payload


def build_activity(snapshot: AgentSnapshot, start_ts: int) -> DiscordActivity:
    """Build the Discord activity for ``snapshot``.

    Pure function: for the same snapshot and environment it always returns an
    equal activity, never mutating its inputs or the process environment.
    """

    active = snapshot.active
    blocked = snapshot.blocked
    if active + blocked == 0:
        state = IDLE_STATE
    else:
        state = f"{active} active, {blocked} blocked"

    party_size = (active, snapshot.total) if snapshot.total > 0 else None

    return DiscordActivity(
        details=DETAILS,
        state=state,
        start_ts=start_ts,
        large_image=os.environ.get(LARGE_IMAGE_ENV) or DEFAULT_LARGE_IMAGE,
        large_text=LARGE_TEXT,
        party_size=party_size,
    )
