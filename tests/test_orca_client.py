"""Tests for the typed async wrapper around the ``orca`` CLI.

All stdout/stderr fed to :class:`OrcaClient` comes from the payloads captured by
the CLI spike and committed under ``tests/fixtures`` (see ``SPIKE.md``), so the
tests exercise the real response shapes. A recording process factory stands in
for :func:`asyncio.create_subprocess_exec`, which keeps the suite hermetic: no
``orca`` process is ever spawned, and every test can inspect the exact argv and
pipe configuration the client used.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest

from orca_presence.orca_client import (
    AGENT_HOOKS_ON_ARGS,
    AGENT_HOOKS_STATUS_ARGS,
    DEFAULT_EXECUTABLE,
    DEFAULT_TIMEOUT_SECONDS,
    NOT_RUNNING_ERROR_CODE,
    STATUS_ARGS,
    WORKTREE_PS_ARGS,
    OrcaClient,
    OrcaCliNotFoundError,
    OrcaError,
    OrcaNotRunningError,
    OrcaTimeoutError,
    SubprocessExec,
)

FIXTURES = Path(__file__).parent / "fixtures"
EXECUTABLE = "orca-test"
TIMEOUT = 5.0

NOT_RUNNING_ENVELOPE = json.dumps(
    {
        "id": "local-status",
        "ok": False,
        "error": {
            "code": NOT_RUNNING_ERROR_CODE,
            "message": "The Orca runtime is not running",
        },
        "_meta": {"runtimeId": "local"},
    }
).encode("utf-8")


def _fixture_bytes(name: str) -> bytes:
    """Read a captured CLI payload as raw bytes, exactly as stdout delivered it."""

    return (FIXTURES / name).read_bytes()


class FakeProcess:
    """Minimal stand-in for :class:`asyncio.subprocess.Process`.

    ``hang=True`` makes :meth:`communicate` block until :meth:`kill` is called,
    which is how the timeout and cancellation paths are exercised.
    """

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        *,
        hang: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None if hang else returncode
        self.killed = False
        self.waited = False
        self._released = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.returncode is None:
            await self._released.wait()
        return (self.stdout, self.stderr)

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
            self._released.set()

    async def wait(self) -> int | None:
        self.waited = True
        return self.returncode


class RecordingExec:
    """Subprocess factory that records its arguments and returns queued processes."""

    def __init__(self, *processes: FakeProcess, error: OSError | None = None) -> None:
        self._processes = list(processes)
        self._error = error
        self.calls: list[tuple[str, ...]] = []
        self.kwargs: list[dict[str, int | None]] = []

    async def __call__(
        self,
        program: str,
        *args: str,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> FakeProcess:
        self.calls.append((program, *args))
        self.kwargs.append({"stdin": stdin, "stdout": stdout, "stderr": stderr})
        if self._error is not None:
            raise self._error
        assert self._processes, "no scripted process left for this invocation"
        return self._processes.pop(0)


def _client(
    exec_: RecordingExec,
    *,
    executable: str = EXECUTABLE,
    timeout: float = TIMEOUT,
) -> OrcaClient:
    return OrcaClient(
        executable,
        timeout=timeout,
        subprocess_exec=cast(SubprocessExec, exec_),
    )


def test_documented_defaults() -> None:
    assert DEFAULT_EXECUTABLE == "orca"
    assert DEFAULT_TIMEOUT_SECONDS == 5.0

    client = OrcaClient()

    assert client.executable == DEFAULT_EXECUTABLE
    assert client.timeout == DEFAULT_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_status_runs_the_documented_command_and_parses() -> None:
    exec_ = RecordingExec(FakeProcess(stdout=_fixture_bytes("status.json")))

    payload = await _client(exec_).status()

    assert exec_.calls == [(EXECUTABLE, *STATUS_ARGS)]
    assert payload is not None
    assert payload.id == "local-status"
    assert payload.ok is True
    assert payload.meta.runtimeId == "8d3c9764-a0c8-4089-a7a9-6b9c00f91ea2"
    assert payload.result.target.kind == "local"
    assert payload.result.app.running is True
    assert payload.result.app.pid == 16572
    assert payload.result.runtime.state == "ready"
    assert payload.result.runtime.reachable is True
    assert payload.result.runtime.appVersion == "1.4.205"
    assert payload.result.runtime.remoteUpdateSupport.automatic is True
    assert payload.result.graph.state == "ready"
    assert "runtime.status.compat.v1" in payload.result.runtime.capabilities


@pytest.mark.asyncio
async def test_worktree_ps_runs_the_documented_command_and_parses() -> None:
    exec_ = RecordingExec(FakeProcess(stdout=_fixture_bytes("worktree_ps.json")))

    payload = await _client(exec_).worktree_ps()

    assert exec_.calls == [(EXECUTABLE, *WORKTREE_PS_ARGS)]
    assert payload is not None
    assert payload.result.totalCount == 10
    assert payload.result.truncated is False
    assert len(payload.result.worktrees) == 10
    assert payload.result.hostScope.hostIds == ["local"]
    assert payload.result.hostScope.omittedHostIds == []

    presence_dev = payload.result.worktrees[0]
    assert presence_dev.repo == "orca-presence"
    assert presence_dev.branch == "refs/heads/presence-dev"
    assert presence_dev.displayName == "presence-dev"
    assert presence_dev.status == "working"
    assert presence_dev.isArchived is False
    assert len(presence_dev.agents) == 1
    assert presence_dev.agents[0].state == "working"
    assert presence_dev.agents[0].agentType == "opencode"

    states = [
        agent.state
        for worktree in payload.result.worktrees
        for agent in worktree.agents
    ]
    assert states.count("working") == 1
    assert states.count("done") == 7
    assert "blocked" not in states


@pytest.mark.asyncio
async def test_agent_hooks_status_runs_the_documented_command_and_parses() -> None:
    exec_ = RecordingExec(FakeProcess(stdout=_fixture_bytes("agent_hooks_status.json")))

    payload = await _client(exec_).agent_hooks_status()

    assert exec_.calls == [(EXECUTABLE, *AGENT_HOOKS_STATUS_ARGS)]
    assert payload is not None
    assert payload.ok is True
    assert payload.result.enabled is True
    assert payload.result.appliedBy == "offline"
    assert len(payload.result.statuses) == 14

    states = {status.agent: status.state for status in payload.result.statuses}
    assert states["codex"] == "installed"
    assert states["antigravity"] == "installed"
    assert states["copilot"] == "installed"
    assert states["claude"] == "not_installed"

    devin = next(
        status for status in payload.result.statuses if status.agent == "devin"
    )
    assert devin.detail is not None
    assert "Devin" in devin.detail


@pytest.mark.asyncio
async def test_agent_hooks_on_runs_the_documented_command() -> None:
    exec_ = RecordingExec(FakeProcess(stdout=b"not json, and not parsed"))

    await _client(exec_).agent_hooks_on()

    assert exec_.calls == [(EXECUTABLE, *AGENT_HOOKS_ON_ARGS)]


@pytest.mark.asyncio
async def test_cli_is_spawned_with_piped_stdio_and_stdin_closed() -> None:
    exec_ = RecordingExec(FakeProcess(stdout=_fixture_bytes("status.json")))

    await _client(exec_).status()

    assert exec_.kwargs == [
        {
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
    ]


@pytest.mark.asyncio
async def test_missing_executable_raises_cli_not_found() -> None:
    exec_ = RecordingExec(error=FileNotFoundError(EXECUTABLE))

    with pytest.raises(OrcaCliNotFoundError) as excinfo:
        await _client(exec_).status()

    assert isinstance(excinfo.value.__cause__, OSError)
    assert EXECUTABLE in str(excinfo.value)


@pytest.mark.asyncio
async def test_runtime_unavailable_envelope_raises_not_running() -> None:
    exec_ = RecordingExec(FakeProcess(stdout=NOT_RUNNING_ENVELOPE, returncode=1))

    with pytest.raises(OrcaNotRunningError) as excinfo:
        await _client(exec_).worktree_ps()

    assert "exited with code 1" in str(excinfo.value)
    assert NOT_RUNNING_ERROR_CODE in str(excinfo.value)
    assert "The Orca runtime is not running" in str(excinfo.value)


@pytest.mark.asyncio
async def test_not_running_stderr_marker_raises_not_running() -> None:
    exec_ = RecordingExec(
        FakeProcess(stderr=b"Error: orca is not running on this host", returncode=1)
    )

    with pytest.raises(OrcaNotRunningError, match="not running"):
        await _client(exec_).status()


@pytest.mark.asyncio
async def test_other_nonzero_exit_raises_the_base_orca_error() -> None:
    exec_ = RecordingExec(
        FakeProcess(stdout=b'{"ok": false}', stderr=b"unknown flag", returncode=2)
    )

    with pytest.raises(OrcaError) as excinfo:
        await _client(exec_).status()

    assert excinfo.value.__class__ is OrcaError
    assert "exited with code 2" in str(excinfo.value)
    assert "unknown flag" in str(excinfo.value)


@pytest.mark.asyncio
async def test_agent_hooks_on_raises_the_base_orca_error_on_failure() -> None:
    exec_ = RecordingExec(FakeProcess(stderr=b"hooks are locked down", returncode=1))

    with pytest.raises(OrcaError) as excinfo:
        await _client(exec_).agent_hooks_on()

    assert excinfo.value.__class__ is OrcaError
    assert "hooks are locked down" in str(excinfo.value)


def test_typed_errors_share_the_orca_error_base() -> None:
    assert issubclass(OrcaCliNotFoundError, OrcaError)
    assert issubclass(OrcaNotRunningError, OrcaError)
    assert issubclass(OrcaTimeoutError, OrcaError)


@pytest.mark.asyncio
async def test_timeout_kills_the_cli_process() -> None:
    process = FakeProcess(hang=True)
    exec_ = RecordingExec(process)

    with pytest.raises(OrcaTimeoutError, match=r"0\.05s"):
        await _client(exec_, timeout=0.05).worktree_ps()

    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_cancellation_kills_the_cli_process() -> None:
    process = FakeProcess(hang=True)
    exec_ = RecordingExec(process)
    client = _client(exec_)

    task = asyncio.create_task(client.status())
    while not exec_.calls:
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_non_json_output_is_ignored() -> None:
    exec_ = RecordingExec(FakeProcess(stdout=b"<html>not json</html>"))

    assert await _client(exec_).status() is None


@pytest.mark.asyncio
async def test_invalid_utf8_output_is_ignored() -> None:
    exec_ = RecordingExec(FakeProcess(stdout=b"\xff\xfe\x00\x01"))

    assert await _client(exec_).agent_hooks_status() is None


@pytest.mark.asyncio
async def test_schema_mismatch_is_ignored() -> None:
    exec_ = RecordingExec(FakeProcess(stdout=b'{"id": "x", "ok": true}'))

    assert await _client(exec_).worktree_ps() is None


@pytest.mark.asyncio
async def test_ok_false_envelope_is_not_usable() -> None:
    raw = json.loads(_fixture_bytes("worktree_ps.json"))
    raw["ok"] = False
    exec_ = RecordingExec(FakeProcess(stdout=json.dumps(raw).encode("utf-8")))

    assert await _client(exec_).worktree_ps() is None


@pytest.mark.asyncio
async def test_unknown_fields_are_tolerated() -> None:
    raw = json.loads(_fixture_bytes("status.json"))
    raw["futureEnvelopeField"] = {"unexpected": True}
    raw["result"]["runtime"]["futureField"] = "ignored"
    exec_ = RecordingExec(FakeProcess(stdout=json.dumps(raw).encode("utf-8")))

    payload = await _client(exec_).status()

    assert payload is not None
    assert payload.result.runtime.appVersion == "1.4.205"


@pytest.mark.parametrize("executable", ["", "   "])
def test_blank_executable_is_rejected(executable: str) -> None:
    with pytest.raises(ValueError, match="executable"):
        OrcaClient(executable)


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        OrcaClient(EXECUTABLE, timeout=timeout)


def test_configuration_is_exposed_read_only() -> None:
    client = OrcaClient(EXECUTABLE, timeout=2.5)

    assert client.executable == EXECUTABLE
    assert client.timeout == 2.5
