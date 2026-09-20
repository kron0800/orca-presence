# orca-presence

Discord Rich Presence bridge for the Orca ADE runtime.

`orca-presence` is a small daemon that polls the local Orca CLI, counts the live
agents it finds and mirrors them into your Discord profile as a Rich Presence
activity:

```
Playing Orca ADE
2 active, 1 blocked
(2 of 5)
```

It is read-only with respect to Orca: the only commands it runs are
`orca worktree ps --json` (the live agent source), `orca agent hooks status
--json` (a gate) and a best-effort `orca agent hooks on` at startup when the
Orca-managed hooks look disabled. It never spawns an agent, never writes to the
Orca database and never parses terminal text.

---

## How it works

| Piece | Behaviour |
|-------|-----------|
| Poll loop | Runs `orca worktree ps --json` every `poll_interval_seconds` (default 15s) and normalizes `agents[].state` into `active` / `blocked` / `total` counts. |
| Discord loop | Keeps a `pypresence` IPC connection to the Discord desktop client alive, reconnecting with exponential backoff (1s → 30s) and republishing the latest activity after every reconnect. |
| Throttle | De-duplicates unchanged activities but re-sends them once per interval, so a presence lost by Discord is always restored. |
| Orca closed | The runtime-unavailable error (`runtime_unavailable`) switches the presence to `Idle` and polling keeps retrying; when Orca comes back the counts resume automatically. |
| Discord closed | The daemon logs a warning, backs off and reconnects when the Discord desktop client is started again. |
| CLI missing | A missing `orca` executable is logged with a remediation hint and the poller keeps running instead of crashing. |
| Invalid client id | A handshake rejection is fatal (logged at `CRITICAL`) because no retry can fix it: fix `ORCA_DISCORD_CLIENT_ID` and restart. |

Agent counting follows Orca's own `agents[].state` enum: `working` → active,
`blocked` → blocked, `waiting` / `done` → total only. See `SPIKE.md` section 6
for the evidence captured against Orca CLI 1.4.205.

---

## Requirements

- Windows 10/11 (primary target; the code itself is cross-platform).
- Python 3.11 or newer.
- The Orca ADE runtime with its CLI (`orca`) installed and on `PATH`.
- The **Discord desktop client**, running and logged in on the same user
  session. Rich Presence uses Discord's local IPC pipe; the web client does not
  expose it.
- Your own Discord Application (see below) — an Application ID, **not** a bot
  token.

---

## Install

From the project root:

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e .
```

This installs the `orca-presence` console script into the virtual environment.
Verify the installation:

```bat
.venv\Scripts\orca-presence
```

Without configuration the daemon fails fast with a clear error and exit code
`2`:

```
... ERROR    orca_presence.main: Invalid configuration: client_id is not
configured; set the ORCA_DISCORD_CLIENT_ID environment variable or the
client_id key in orca-presence.toml
```

---

## Create your own Discord Application

Rich Presence is tied to a Discord Application you own, so every user needs to
create one in the [Discord Developer Portal](https://discord.com/developers/applications):

1. Open **Applications → New Application** and give it a name (for example
   `Orca Presence`).
2. On **General Information**, copy the **Application ID** (a 17–19 digit
   number). This is the value for `ORCA_DISCORD_CLIENT_ID`.
3. Optional: on **Rich Presence → Art Assets**, upload an image and set its key
   to `orca_ade` so the default large image resolves.
4. You do **not** need a bot, a bot token or any privileged intent. The
   Application ID is a public identifier; keep any bot token out of this
   project entirely.

### Caveats to know before you start

- **The image slot may not exist.** A fresh Discord Application has no art
  assets, so the default large image key `orca_ade` may not resolve and Discord
  will show the activity without the large image. Either upload an asset with
  that key, or point the daemon at an existing key with
  `ORCA_PRESENCE_LARGE_IMAGE`. The daemon never fails over a missing asset.
- **There is no token/cost slot.** Orca CLI 1.4.205 exposes no token or cost
  command (verified in `SPIKE.md`), so this project publishes no token totals
  and advertises no token field. Token counters live in the OpenCode sqlite
  database, not behind the Orca CLI, and are deliberately out of scope.
- **Application ID, not bot token.** If you paste a bot token into
  `ORCA_DISCORD_CLIENT_ID`, the handshake is rejected (code `4000`) and the
  daemon exits with a `CRITICAL` log line telling you so.

---

## Configure

Every setting is resolved from, in order of precedence:

1. environment variables,
2. a TOML file,
3. the documented defaults.

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `ORCA_DISCORD_CLIENT_ID` | *required* | Your Discord Application ID. The daemon refuses to start without it. |
| `ORCA_PRESENCE_POLL_INTERVAL_SECONDS` | `15` | Seconds between Orca polls; also the Discord publish throttle interval. |
| `ORCA_PRESENCE_ORCA_EXECUTABLE` | `orca` | Orca CLI command or full path. Use this when `orca` is not on `PATH`. A leading `~` is expanded. |
| `ORCA_PRESENCE_ORCA_TIMEOUT_SECONDS` | `5` | Maximum seconds per Orca CLI invocation; the child is killed on timeout. |
| `ORCA_PRESENCE_CONFIG_FILE` | `./orca-presence.toml` | Explicit TOML file; must exist when set. |
| `ORCA_PRESENCE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` or a numeric level. |
| `ORCA_PRESENCE_LARGE_IMAGE` | `orca_ade` | Large image asset key. Set it to an asset that exists in *your* Discord Application. |

Set the client id for the current user (new shells and logon sessions pick it
up):

```bat
setx ORCA_DISCORD_CLIENT_ID "123456789012345678"
```

### TOML file

`orca-presence.toml` in the working directory (or the file named by
`ORCA_PRESENCE_CONFIG_FILE`) may hold the same values:

```toml
client_id = "123456789012345678"
poll_interval_seconds = 15.0
orca_executable = "orca"
orca_timeout_seconds = 5.0
```

Unknown keys and invalid values are rejected at startup with a precise error
instead of failing later at runtime.

---

## Run manually

```bat
.venv\Scripts\orca-presence
```

or, equivalently:

```bat
.venv\Scripts\python -m orca_presence.main
```

Stop it with `Ctrl+C`; the shutdown is clean (the Discord presence is cleared
and the IPC socket is closed). Exit codes:

| Code | Meaning |
|------|---------|
| `0` | Clean shutdown. |
| `1` | Known unrecoverable runtime error (Orca/Discord/OS/event-loop failure). |
| `2` | Invalid or missing configuration. |
| `130` | Interrupted before the shutdown handler was installed. |

---

## Run as a background service on Windows

Two important constraints:

- The daemon **must run in the same user session as the Discord desktop
  client**, because Discord's IPC pipe is per user. Do **not** use Task
  Scheduler's "Run whether user is logged on or not" option.
- `orca` must be on `PATH` for that session, or `ORCA_PRESENCE_ORCA_EXECUTABLE`
  must point at the full path (for example
  `C:\Users\<you>\AppData\Local\Programs\orca\orca.exe`).

A ready-made launcher is provided at `scripts\orca-presence.bat`. It resolves
the project root from its own location, runs `.venv\Scripts\orca-presence.exe`
and exits with the daemon's exit code. Set `ORCA_PRESENCE_HOME` if you copy the
script elsewhere, and `ORCA_PRESENCE_LOG_FILE` to append the daemon output to a
file instead of the console.

### Option A — Task Scheduler (recommended)

1. Open **Task Scheduler → Create Task…**
2. **General**: name it `OrcaPresence`, select **Run only when user is logged
   on**, and tick **Hidden** to avoid a console window.
3. **Triggers**: New… → **At log on** → your user.
4. **Actions**: New… → Start a program →
   - Program/script: `C:\path\to\orca-presence\scripts\orca-presence.bat`
   - Start in: `C:\path\to\orca-presence`
   - Optionally set the `ORCA_PRESENCE_LOG_FILE` environment variable in the
     user environment so background runs are logged to a file.
5. **Settings**: tick **If the task fails, restart every** `1 minute`, up to a
   few times. (The daemon itself already retries Discord and Orca outages;
   this only covers unexpected crashes.)
6. Save, then right-click the task and **Run** to start it immediately.

### Option B — `schtasks` one-liners

From an elevated or normal `cmd.exe` (adjust the path):

```bat
schtasks /Create /TN "OrcaPresence" /SC ONLOGON /RL LIMITED /F ^
  /TR "\"C:\path\to\orca-presence\scripts\orca-presence.bat\""

schtasks /Run    /TN "OrcaPresence"
schtasks /Query  /TN "OrcaPresence" /V /FO LIST
schtasks /Delete /TN "OrcaPresence" /F
```

`schtasks` cannot set the "Hidden" flag or restart-on-failure; use Option A (or
edit the task's XML) if you need those. The task starts at logon from then on.

### Option C — Startup folder

Create a shortcut to `scripts\orca-presence.bat` in
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`. This is the simplest
option but has no restart-on-failure and always shows a console window unless
the shortcut is configured to run minimized.

### Service logs

- Interactive runs log to `stderr` with timestamps.
- Background runs: set `ORCA_PRESENCE_LOG_FILE` (for example
  `%LOCALAPPDATA%\orca-presence\orca-presence.log`) so the batch file appends
  the daemon output to that file.
- `ORCA_PRESENCE_LOG_LEVEL=DEBUG` adds per-poll and per-publish detail.

---

## Troubleshooting

| Symptom (log line) | Cause | Fix |
|--------------------|-------|-----|
| `Orca CLI executable 'orca' was not found or could not be started` | `orca` is not on `PATH` for this session. | Install the Orca CLI or set `ORCA_PRESENCE_ORCA_EXECUTABLE` to the full path. The daemon keeps polling and recovers automatically. |
| `Orca runtime is unreachable; waiting for it to come back` | Orca desktop is closed. | Nothing to do — the presence shows `Idle` and resumes when Orca is started. |
| `Discord is not running or unreachable (...); retrying in Ns` | Discord desktop client is closed or its IPC pipe is gone. | Start Discord; the daemon reconnects with backoff and republishes the latest activity. |
| `Discord presence disabled: the handshake rejected the client id ...` | `ORCA_DISCORD_CLIENT_ID` is wrong, blank or a bot token instead of an Application ID. | Fix the variable and restart. This error is fatal by design. |
| Presence shows no large image | The asset key does not exist in your Discord Application. | Upload an asset with key `orca_ade` or set `ORCA_PRESENCE_LARGE_IMAGE` to an existing key. |
| `Invalid configuration: ...` (exit `2`) | Missing/invalid client id or config file. | Read the message; it names the offending variable, key or file. |

---

## Development

```bat
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
.venv\Scripts\python -m mypy .
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
```

`mypy` runs in `strict` mode over `orca_presence` and `tests` (with
`ignore-without-code`, `redundant-expr` and `truthy-bool` enabled), and `ruff`
enforces the strict lint set configured in `pyproject.toml`. The test suite is
hermetic: it never spawns a real `orca` process and never opens a Discord IPC
socket — CLI fixtures live in `tests/fixtures/` and were captured in
`SPIKE.md`.

| Module | Responsibility |
|--------|----------------|
| `orca_presence/config.py` | Typed, validated configuration (env → TOML → defaults). |
| `orca_presence/orca_client.py` | Async, read-only wrapper around the `orca` CLI with typed errors. |
| `orca_presence/models.py` | Pydantic models for the captured CLI payloads + `AgentSnapshot`. |
| `orca_presence/activity_builder.py` | Pure mapping from `AgentSnapshot` to a Discord activity. |
| `orca_presence/throttle.py` | Value-based publish throttle with `cancel` / `reset`. |
| `orca_presence/discord_client.py` | Discord IPC transport with reconnect/backoff and typed errors. |
| `orca_presence/poller.py` | Wires the poll loop to the Discord loop. |
| `orca_presence/main.py` | Entry point, logging and signal-driven shutdown. |
