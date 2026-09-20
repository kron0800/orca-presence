# SPIKE: Reading token totals from the Orca CLI (read-only recon)

- Date: 2026-09-20
- Host: Windows (win32), worktree `presence-dev`
- Orca CLI version: **1.4.205** (`orca --version` prints exactly `1.4.205`; also
  `status.result.runtime.appVersion == "1.4.205"`)
- Runtime: local Orca app running (`status.result.app.running == true`,
  `runtime.state == "ready"`)

## Guardrails honored

- All commands were read-only with respect to Orca runtime state.
- No agent hooks were enabled/toggled (`agent hooks status` was only queried).
- No Python source files were created or modified.
- Raw CLI JSON payloads were captured under `tests/fixtures/`.

## 1. Captured fixtures

| File | Command | Exit code |
|------|---------|-----------|
| `tests/fixtures/status.json` | `orca status --json` | 0 |
| `tests/fixtures/worktree_ps.json` | `orca worktree ps --json` | 0 |
| `tests/fixtures/agent_hooks_status.json` | `orca agent hooks status --json` | 0 |
| `tests/fixtures/terminal_list_active.json` | `orca terminal list --worktree active --json` | 0 |
| `tests/fixtures/terminal_show_worker.json` | `orca terminal show --terminal term_... --json` (extra probe) | 0 |
| `tests/fixtures/agent_context.json` | `orca agent-context --json` (full command schema) | 0 |
| `tests/fixtures/orca_help.txt` | `orca --help` | 0 |
| `tests/fixtures/orca_version.txt` | `orca --version` | 0 |
| `tests/fixtures/orca_agent_help.txt` | `orca agent --help` (hidden subtree) | 0 |
| `tests/fixtures/orca_usage_help.txt` | `orca usage --help` | 1 (unknown command) |
| `tests/fixtures/orca_tokens_help.txt` | `orca tokens --help` | 1 (unknown command) |
| `tests/fixtures/opencode_db_session_schema.sql` | `sqlite3 .schema session` on a copy of `opencode.db` | 0 |

Each JSON capture has a sibling `*.stderr.txt` (all empty; all commands exited 0).

## 2. Is there a CLI command for tokens/cost?

**No. Confirmed for CLI 1.4.205.**

- `orca --help` lists no `usage`, `tokens`, or `cost` command; the only
  `usage` occurrences in help text are the literal `Usage: orca ...` lines.
- `orca usage --help` -> exit 1, `Unknown command: usage`.
- `orca tokens --help` -> exit 1, `Unknown command: tokens`.
- The machine-readable command schema (`orca agent-context --json`) contains
  **234 `"command"` entries**; none reference token/cost/usage data. Every
  `"usage"` key there is a per-command syntax string (e.g.
  `"usage": "orca status [--json]"`), not a command.
- `orca agent hooks ...` is a hidden subtree (absent from `orca --help`, present
  in `orca agent --help`) with `prepare-codex`, `status`, `off`, `on`; none
  expose token or cost data.

## 3. Exact JSON schemas observed

### 3.1 `orca status --json` (`status.json`)

- Top level: `id`, `ok`, `result`, `_meta`
- `result.target`: `kind`
- `result.app`: `running`, `pid`, `desktopWindowStatus`
- `result.runtime`: `state`, `reachable`, `connectionState`, `runtimeId`,
  `appVersion`, `remoteUpdateSupport` (`installMode`, `automatic`, `reason`),
  `capabilities` (array of strings)
- `result.graph`: `state`
- `_meta`: `runtimeId`
- **No token/cost fields.** Zero case-insensitive matches for `token`/`cost`.

### 3.2 `orca agent hooks status --json` (`agent_hooks_status.json`)

- Top level: `id`, `ok`, `result`, `_meta`
- `result`: `enabled`, `settingsPath`, `appliedBy`, `statuses`
- `result.statuses[]`: `agent`, `state` (`installed` | `not_installed`),
  `configPath`, `managedHooksPresent`, `detail`
- `_meta`: `runtimeId`
- Observed values (not modified): `enabled: true`, `appliedBy: "offline"`,
  `settingsPath: C:\Users\kron\AppData\Roaming\orca\profiles\local-default\orca-data.json`.
- **No token/cost fields.** Zero matches.

### 3.3 `orca worktree ps --json` (`worktree_ps.json`)

- Top level: `id`, `ok`, `result`, `_meta`
- `result`: `worktrees`, `hostScope` (`hostIds`, `omittedHostIds`),
  `totalCount`, `truncated`
- `result.worktrees[]`: `workspaceKind`, `worktreeId`, `repoId`, `hostId`,
  `terminalPlatform`, `repo`, `path`, `branch`, `isArchived`, `isMainWorktree`,
  `hasHostSidebarActivity`, `worktreeInstanceId`,
  `lineageWorktreeInstanceId` (conditional), `parentWorktreeInstanceId`
  (conditional), `parentWorktreeId`, `childWorktreeIds`, `displayName`,
  `workspaceStatus`, `sortOrder`, `lastActivityAt`, `createdAt`,
  `creatorProvenance` (`kind`), `linkedIssue`, `linkedPR`
  (`number`, `state`) | null, `linkedLinearIssue`, `linkedGitLabMR`,
  `linkedGitLabIssue`, `comment`, `isPinned`, `isActive`, `unread`,
  `liveTerminalCount`, `hasAttachedPty`, `lastOutputAt`, `preview`, `status`,
  `agents`
- `result.worktrees[].agents[]`: `paneKey`, `parentPaneKey`, `state`,
  `agentType`, `prompt`, `taskTitle`, `displayName`, `lastAssistantMessage`,
  `toolName`, `toolInput`, `interrupted`, `stateStartedAt`, `updatedAt`
- **No structured token/cost fields.** The single regex hit was incidental
  free text (a `lastAssistantMessage` that mentioned the word "tokens").
- `preview` is arbitrary terminal output; it contained an OpenCode status line
  like `35.1K (4%) · $0.01` (free text, not a field).

### 3.4 `orca terminal list --worktree active --json` (`terminal_list_active.json`)

- Top level: `id`, `ok`, `result`, `_meta`
- `result`: `terminals`, `hostScope` (`hostIds`, `omittedHostIds`),
  `topologyRevisions` (map of worktreeId -> integer), `totalCount`, `truncated`
- `result.terminals[]`: `handle`, `ptyId`, `incarnationId`, `orphaned`,
  `worktreeId`, `worktreePath`, `branch`, `tabId`, `leafId`, `title`,
  `connected`, `writable`, `lastOutputAt`, `preview`, `executionHostId`,
  `agentIdentity` (present only on agent-bound terminals)
- `orca terminal show --json` returns the same object plus `paneRuntimeId`,
  `rendererGraphEpoch`, `agentWait` (null when idle).
- **No structured token/cost fields.** The single regex hit was a terminal
  title that contained the word "tokens".
- OpenCode's status line (`48.7K (5%) · $0.01`) appears only inside the
  free-text `preview` string.

## 4. Where token totals actually live (local, not CLI)

### 4.1 Authoritative per-session totals: OpenCode sqlite DB

Path: `%USERPROFILE%\.local\share\opencode\opencode.db` (WAL mode).

Table `session` (exact columns, see `tests/fixtures/opencode_db_session_schema.sql`):

- `id`, `project_id`, `workspace_id`, `parent_id`, `slug`, `directory`, `path`,
  `title`, `version`, `share_url`, `summary_additions`, `summary_deletions`,
  `summary_files`, `summary_diffs`, `metadata`
- Token/cost columns (exact names):
  - `cost` REAL DEFAULT 0 NOT NULL
  - `tokens_input` INTEGER DEFAULT 0 NOT NULL
  - `tokens_output` INTEGER DEFAULT 0 NOT NULL
  - `tokens_reasoning` INTEGER DEFAULT 0 NOT NULL
  - `tokens_cache_read` INTEGER DEFAULT 0 NOT NULL
  - `tokens_cache_write` INTEGER DEFAULT 0 NOT NULL
- Plus `revert`, `permission`, `agent`, `model`, `time_created`, `time_updated`,
  `time_compacting`, `time_archived`.
- Opened via a temp copy; the live DB was never written (use `-readonly` or a
  copy because of WAL).

### 4.2 Orca's derived opencode aggregate

Path: `%APPDATA%\orca\orca-opencode-usage.json` (`schemaVersion: 2`).

- Top level: `schemaVersion`, `worktreeFingerprint`, `processedDatabases`,
  `dailyAggregates`, `scanState`
- `processedDatabases[]`: `path`, `mtimeMs`, `size`, `sessions[]`
- `sessions[]`: `sessionId`, `firstTimestamp`, `lastTimestamp`, `primaryModel`,
  `hasMixedModels`, `primaryProjectLabel`, `hasMixedLocations`,
  `primaryWorktreeId`, `primaryRepoId`, `eventCount`,
  `totalInputTokens`, `totalCachedInputTokens`, `totalOutputTokens`,
  `totalReasoningOutputTokens`, `totalTokens`, `estimatedCostUsd`,
  `locationBreakdown[]`, `modelBreakdown[]`, `locationModelBreakdown[]`
- `locationBreakdown[]`: `locationKey`, `projectLabel`, `repoId`, `worktreeId`,
  `eventCount`, `inputTokens`, `cachedInputTokens`, `outputTokens`,
  `reasoningOutputTokens`, `totalTokens`, `estimatedCostUsd`
- `modelBreakdown[]`: `modelKey`, `modelLabel`, `eventCount`, `inputTokens`,
  `cachedInputTokens`, `outputTokens`, `reasoningOutputTokens`, `totalTokens`,
  `estimatedCostUsd`
- `locationModelBreakdown[]`: `locationKey`, `modelKey`, `modelLabel`, `repoId`,
  `worktreeId` + the same numeric fields
- `dailyAggregates[]`: `day`, `model`, `projectKey`, `projectLabel`, `repoId`,
  `worktreeId`, `eventCount`, `inputTokens`, `cachedInputTokens`,
  `outputTokens`, `reasoningOutputTokens`, `totalTokens`, `estimatedCostUsd`
- `scanState`: `enabled`, `lastScanStartedAt`, `lastScanCompletedAt`,
  `lastScanError`
- This file is Orca-written derived state, not exposed by any CLI command.
  In the observed copy `lastScanCompletedAt` was `1789549859925`
  (2026-09-16), so it can be stale relative to the live session DB.
- A copy of this file was intentionally NOT committed to fixtures (contains
  local paths and session identifiers); only its exact field names are
  documented here.

### 4.3 Other local stores checked

- `%APPDATA%\orca\orchestration.db` (sqlite): tables such as
  `dispatch_contexts`, `worker_dispatches`, `tasks`, `messages`, etc. Its
  `launch_token_hash` / `capability_hash` columns are dispatch-auth hashes,
  NOT usage tokens. No `cost`/`usage`/token-counter columns exist.
- `%APPDATA%\orca\orca-stats.json` (`schemaVersion: 1`): lifecycle events only
  (`agent_start` / `agent_stop` with `durationMs`); no token/cost counters.

## 5. Conclusion

- **Via the Orca CLI: not possible** in 1.4.205. There is no public or hidden
  command that returns token totals, and none of the structured JSON payloads
  (`status`, `agent hooks status`, `worktree ps`, `terminal list`, `terminal
  show`) embed token/cost fields. Tokens appear only as incidental free text in
  terminal `preview` strings.
- **Via local sqlite: possible** for OpenCode sessions. The authoritative
  per-session source is `opencode.db` table `session`
  (`tokens_input`, `tokens_output`, `tokens_reasoning`, `tokens_cache_read`,
  `tokens_cache_write`, `cost`). A pre-aggregated, worktree/model/day-rolled-up
  view already exists at `%APPDATA%\orca\orca-opencode-usage.json`, but it is
  derived Orca state and may lag the live DB.
- **Not possible** from `orchestration.db` or `orca-stats.json`; no
  claude/codex token source was found in this recon.
- Recommended read path for a future tool: query `opencode.db` read-only
  (session rows filtered by `directory`/`project_id`, summing the six
  token/cost columns), optionally cross-checking the derived
  `orca-opencode-usage.json` aggregates. Do not parse terminal `preview` text.

## 6. Agent activity semantics for presence counts

Decision recorded while implementing `orca_presence/models.py` (Kron,
2026-09-20): the Discord presence derives live agent counts **only** from
`orca worktree ps --json`.

- `result.worktrees[].agents[]` is the single counting source, using the
  `state` enum the Orca runtime itself tracks:
  `working | blocked | waiting | done`.
- Normalization into `AgentSnapshot`:
  - `working` -> `active += 1`
  - `blocked` -> `blocked += 1`
  - `waiting` / `done` -> counted only in `total`
  - `total` -> every agent across every worktree in the payload.
- `orca agent hooks status --json` is **not** a counting source: its
  `statuses[]` only report hook installation per agent type
  (`installed | not_installed | partial | error`, plus
  `managedHooksPresent`), never live activity. It acts as a gate (are hooks
  available?) and `AgentSnapshot.from_hooks_status()` deliberately returns an
  empty snapshot `(0, 0, 0)`.
- `result.worktrees[].status` (`working | active | inactive`) is descriptive
  worktree-level activity and does not contribute to the counts.
- Implemented API: `StatusPayload`, `AgentHooksStatusPayload`,
  `WorktreePsPayload` (pydantic models) and frozen dataclass `AgentSnapshot`
  with `from_worktree_ps()` / `from_hooks_status()` in
  `orca_presence/models.py`.
