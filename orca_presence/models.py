"""Typed models for the Orca CLI JSON payloads consumed by orca-presence.

Every model mirrors a payload captured under ``tests/fixtures`` and inventoried
in ``SPIKE.md`` (Orca CLI 1.4.205). Field names keep the exact camelCase keys
emitted by the CLI so the mapping back to the documented schemas stays obvious.
Unknown extra keys are ignored, but a missing required key or a value outside a
documented enum raises a pydantic ``ValidationError``: callers such as
``orca_presence.orca_client`` treat that as "the payload does not match the
expected schema" and fail over instead of crashing.

Agent activity semantics
------------------------
Live agent counts are derived exclusively from ``orca worktree ps`` through the
``agents[].state`` field (the enum Orca itself tracks: ``working``, ``blocked``,
``waiting`` and ``done``):

* ``working`` counts as one active agent.
* ``blocked`` counts as one blocked agent.
* ``waiting`` and ``done`` agents count only towards the total.

``orca agent hooks status`` does not expose live agent counts: it only reports
whether Orca-managed status hooks are installed per agent type. It therefore
acts as a gate (hooks available or not) rather than as a counting source, and
:meth:`AgentSnapshot.from_hooks_status` returns an empty snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AgentHookState",
    "AgentHookStatus",
    "AgentHooksStatusPayload",
    "AgentHooksStatusResult",
    "AgentSnapshot",
    "AgentState",
    "AppStatus",
    "CreatorProvenance",
    "GraphStatus",
    "HostScope",
    "LinkedPullRequest",
    "OrcaMeta",
    "OrcaPayload",
    "RemoteUpdateSupport",
    "RuntimeStatus",
    "StatusPayload",
    "StatusResult",
    "StatusTarget",
    "Worktree",
    "WorktreeAgent",
    "WorktreePsPayload",
    "WorktreePsResult",
]

AgentState = Literal["working", "blocked", "waiting", "done"]
AgentHookState = Literal["installed", "not_installed", "partial", "error"]


class OrcaMeta(BaseModel):
    """``_meta`` block shared by every CLI JSON response."""

    model_config = ConfigDict(extra="ignore")

    runtimeId: str


class OrcaPayload(BaseModel):
    """Common envelope of ``orca <command> --json`` responses."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    ok: bool
    meta: OrcaMeta = Field(alias="_meta")


class StatusTarget(BaseModel):
    """Target runtime the status command was resolved against."""

    model_config = ConfigDict(extra="ignore")

    kind: str


class AppStatus(BaseModel):
    """Desktop application process details."""

    model_config = ConfigDict(extra="ignore")

    running: bool
    pid: int
    desktopWindowStatus: str


class RemoteUpdateSupport(BaseModel):
    """How the running runtime supports remote updates."""

    model_config = ConfigDict(extra="ignore")

    installMode: str
    automatic: bool
    reason: str


class RuntimeStatus(BaseModel):
    """Runtime connection state and advertised capabilities."""

    model_config = ConfigDict(extra="ignore")

    state: str
    reachable: bool
    connectionState: str
    runtimeId: str
    appVersion: str
    remoteUpdateSupport: RemoteUpdateSupport
    capabilities: list[str]


class GraphStatus(BaseModel):
    """Workspace graph state."""

    model_config = ConfigDict(extra="ignore")

    state: str


class StatusResult(BaseModel):
    """``result`` block of ``orca status --json``."""

    model_config = ConfigDict(extra="ignore")

    target: StatusTarget
    app: AppStatus
    runtime: RuntimeStatus
    graph: GraphStatus


class StatusPayload(OrcaPayload):
    """Full ``orca status --json`` payload."""

    result: StatusResult


class AgentHookStatus(BaseModel):
    """Installation state of the Orca-managed hooks for one agent type."""

    model_config = ConfigDict(extra="ignore")

    agent: str
    state: AgentHookState
    configPath: str
    managedHooksPresent: bool
    detail: str | None = None


class AgentHooksStatusResult(BaseModel):
    """``result`` block of ``orca agent hooks status --json``."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool
    settingsPath: str
    appliedBy: str | None = None
    statuses: list[AgentHookStatus]


class AgentHooksStatusPayload(OrcaPayload):
    """Full ``orca agent hooks status --json`` payload.

    This payload describes hook instrumentation only; use
    :meth:`AgentSnapshot.from_worktree_ps` for live agent counts.
    """

    result: AgentHooksStatusResult


class HostScope(BaseModel):
    """Hosts included in (and omitted from) a worktree listing."""

    model_config = ConfigDict(extra="ignore")

    hostIds: list[str]
    omittedHostIds: list[str]


class CreatorProvenance(BaseModel):
    """Who created a worktree."""

    model_config = ConfigDict(extra="ignore")

    kind: str


class LinkedPullRequest(BaseModel):
    """Pull/merge request linked to a worktree."""

    model_config = ConfigDict(extra="ignore")

    number: int
    state: str


class WorktreeAgent(BaseModel):
    """Agent bound to a pane inside a worktree.

    ``state`` is the live state Orca tracks (hook-driven when hooks are
    installed, inferred otherwise) and is the only field the snapshot
    normalization depends on; every other field is descriptive.
    """

    model_config = ConfigDict(extra="ignore")

    paneKey: str
    state: AgentState
    parentPaneKey: str | None = None
    agentType: str | None = None
    prompt: str | None = None
    taskTitle: str | None = None
    displayName: str | None = None
    lastAssistantMessage: str | None = None
    toolName: str | None = None
    toolInput: str | None = None
    interrupted: bool = False
    stateStartedAt: int | None = None
    updatedAt: int | None = None


class Worktree(BaseModel):
    """One entry of ``result.worktrees[]`` in ``orca worktree ps --json``.

    ``status`` is the worktree-level activity flag observed as ``working``,
    ``active`` or ``inactive``; agent counts do not depend on it.
    """

    model_config = ConfigDict(extra="ignore")

    workspaceKind: str
    worktreeId: str
    repoId: str
    hostId: str | None = None
    terminalPlatform: str | None = None
    repo: str
    path: str
    branch: str
    isArchived: bool
    isMainWorktree: bool
    hasHostSidebarActivity: bool
    worktreeInstanceId: str | None = None
    lineageWorktreeInstanceId: str | None = None
    parentWorktreeInstanceId: str | None = None
    parentWorktreeId: str | None = None
    childWorktreeIds: list[str]
    displayName: str
    workspaceStatus: str
    sortOrder: int
    lastActivityAt: int | None
    createdAt: int | None = None
    creatorProvenance: CreatorProvenance | None = None
    linkedIssue: object | None = None
    linkedPR: LinkedPullRequest | None = None
    linkedLinearIssue: object | None = None
    linkedGitLabMR: object | None = None
    linkedGitLabIssue: object | None = None
    comment: str
    isPinned: bool
    isActive: bool
    unread: bool
    liveTerminalCount: int
    hasAttachedPty: bool
    lastOutputAt: int | None
    preview: str
    status: str
    agents: list[WorktreeAgent]


class WorktreePsResult(BaseModel):
    """``result`` block of ``orca worktree ps --json``."""

    model_config = ConfigDict(extra="ignore")

    worktrees: list[Worktree]
    hostScope: HostScope
    totalCount: int
    truncated: bool


class WorktreePsPayload(OrcaPayload):
    """Full ``orca worktree ps --json`` payload; the live agent source."""

    result: WorktreePsResult


@dataclass(frozen=True, slots=True)
class AgentSnapshot:
    """Immutable count of Orca agents at a point in time.

    ``active`` counts agents in state ``working``, ``blocked`` counts agents in
    state ``blocked`` and ``total`` counts every agent in the source payload
    (including ``waiting`` and ``done`` ones). Counts are only ever derived
    from ``orca worktree ps``; ``orca agent hooks status`` is a gate, not a
    counting source.
    """

    active: int
    blocked: int
    total: int

    def __post_init__(self) -> None:
        for name in ("active", "blocked", "total"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(
                    f"AgentSnapshot.{name} must be non-negative, got {value}"
                )

    @classmethod
    def from_worktree_ps(cls, payload: WorktreePsPayload) -> AgentSnapshot:
        """Normalize live agents out of an ``orca worktree ps --json`` payload."""

        agents = [
            agent for worktree in payload.result.worktrees for agent in worktree.agents
        ]
        active = sum(agent.state == "working" for agent in agents)
        blocked = sum(agent.state == "blocked" for agent in agents)
        return cls(active=active, blocked=blocked, total=len(agents))

    @classmethod
    def from_hooks_status(cls, payload: AgentHooksStatusPayload) -> AgentSnapshot:
        """Return an empty snapshot for an ``agent hooks status`` payload.

        Hook status describes instrumentation, not activity, so it yields no
        live counts; callers use it only to decide whether hooks are available
        and keep normalizing counts with :meth:`from_worktree_ps`.
        """

        return cls(active=0, blocked=0, total=0)
