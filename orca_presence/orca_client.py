"""Typed async wrapper around the ``orca`` CLI used by orca-presence.

The wrapper is deliberately small and read-only. Every method spawns the CLI
through :func:`asyncio.create_subprocess_exec` with an explicit argument list
(never a shell), waits at most :data:`DEFAULT_TIMEOUT_SECONDS` seconds and
parses stdout with the pydantic models from :mod:`orca_presence.models` that
mirror the payloads captured under ``tests/fixtures`` and documented in
``SPIKE.md`` (Orca CLI 1.4.205):

* :meth:`OrcaClient.status` runs ``orca status --json``.
* :meth:`OrcaClient.worktree_ps` runs ``orca worktree ps --json``; this is the
  only live agent source (see ``SPIKE.md`` section 6).
* :meth:`OrcaClient.agent_hooks_status` runs ``orca agent hooks status --json``.
* :meth:`OrcaClient.agent_hooks_on` runs ``orca agent hooks on``; it is a
  best-effort startup repair used by the poller when the hooks are disabled.

Failure contract
----------------
* :class:`OrcaCliNotFoundError` -- the ``orca`` executable could not be located
  or started.
* :class:`OrcaNotRunningError` -- the CLI could not reach a running Orca
  runtime, reported by the CLI as the ``runtime_unavailable`` error code.
* :class:`OrcaTimeoutError` -- the invocation exceeded the timeout; the child
  process is killed before the error is raised.
* any other non-zero exit is raised as the base :class:`OrcaError`.
* output that is not JSON, or JSON that does not validate against the expected
  model, is not an error: the method logs a warning and returns ``None`` so a
  poller can fail over instead of crashing. Nothing here swallows failures with
  a blanket ``except Exception``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import NoReturn, TypeVar

from pydantic import ValidationError

from orca_presence.models import (
    AgentHooksStatusPayload,
    OrcaPayload,
    StatusPayload,
    WorktreePsPayload,
)

__all__ = [
    "AGENT_HOOKS_ON_ARGS",
    "AGENT_HOOKS_STATUS_ARGS",
    "DEFAULT_EXECUTABLE",
    "DEFAULT_TIMEOUT_SECONDS",
    "NOT_RUNNING_ERROR_CODE",
    "STATUS_ARGS",
    "WORKTREE_PS_ARGS",
    "OrcaCliNotFoundError",
    "OrcaClient",
    "OrcaError",
    "OrcaNotRunningError",
    "OrcaTimeoutError",
    "SubprocessExec",
]

logger = logging.getLogger(__name__)

DEFAULT_EXECUTABLE = "orca"
DEFAULT_TIMEOUT_SECONDS = 5.0

# Error code the CLI emits (JSON envelope) when it cannot reach the runtime.
NOT_RUNNING_ERROR_CODE = "runtime_unavailable"

STATUS_ARGS: tuple[str, ...] = ("status", "--json")
WORKTREE_PS_ARGS: tuple[str, ...] = ("worktree", "ps", "--json")
AGENT_HOOKS_STATUS_ARGS: tuple[str, ...] = ("agent", "hooks", "status", "--json")
AGENT_HOOKS_ON_ARGS: tuple[str, ...] = ("agent", "hooks", "on")

PayloadT = TypeVar("PayloadT", bound=OrcaPayload)
SubprocessExec = Callable[..., Awaitable[asyncio.subprocess.Process]]

# Human-readable fallbacks for CLI versions that do not report a stable code.
_NOT_RUNNING_MARKERS: tuple[str, ...] = (
    NOT_RUNNING_ERROR_CODE,
    "not running",
    "isn't running",
    "not reachable",
    "runtime metadata",
    "connect to the running orca",
    "closed the connection before responding",
    "restart orca",
)


class OrcaError(Exception):
    """Base class for Orca CLI failures."""


class OrcaCliNotFoundError(OrcaError):
    """The ``orca`` executable could not be located or started."""


class OrcaNotRunningError(OrcaError):
    """The CLI could not reach a running Orca runtime (``runtime_unavailable``)."""


class OrcaTimeoutError(OrcaError):
    """The ``orca`` invocation exceeded the configured timeout."""


async def _spawn_default(
    program: str,
    *args: str,
    stdin: int | None = None,
    stdout: int | None = None,
    stderr: int | None = None,
) -> asyncio.subprocess.Process:
    """Spawn the CLI with the default asyncio process factory."""

    return await asyncio.create_subprocess_exec(
        program, *args, stdin=stdin, stdout=stdout, stderr=stderr
    )


async def _kill_process(process: asyncio.subprocess.Process) -> None:
    """Kill a still-running CLI process and reap it."""

    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            logger.debug("Orca CLI process exited before it could be killed")
    await process.wait()


def _error_envelope(raw: bytes) -> tuple[str | None, str | None]:
    """Extract ``(code, message)`` from an ``ok: false`` CLI JSON envelope."""

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (None, None)
    if not isinstance(data, dict):
        return (None, None)
    error = data.get("error")
    if not isinstance(error, dict):
        return (None, None)
    code = error.get("code")
    message = error.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
    )


def _raise_for_exit(
    command: tuple[str, ...], returncode: int, stdout: bytes, stderr: bytes
) -> NoReturn:
    """Raise the typed error matching a non-zero ``orca`` exit."""

    code, message = _error_envelope(stdout)
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    parts = [part for part in (code, message) if part]
    if not parts and stderr_text:
        parts.append(stderr_text)
    detail = f": {': '.join(parts)}" if parts else ""
    haystack = " ".join((*parts, stderr_text)).lower()

    if code == NOT_RUNNING_ERROR_CODE or any(
        marker in haystack for marker in _NOT_RUNNING_MARKERS
    ):
        raise OrcaNotRunningError(
            f"{' '.join(command)} exited with code {returncode}{detail}"
        )
    raise OrcaError(f"{' '.join(command)} exited with code {returncode}{detail}")


def _parse_payload(
    payload_type: type[PayloadT], stdout: str, command: str
) -> PayloadT | None:
    """Validate ``stdout`` against ``payload_type``, returning ``None`` on mismatch.

    A missing required field, an unknown enum value, invalid JSON or an
    ``ok: false`` envelope all mean "this payload is not usable" and never
    propagate as an exception, so callers fail over instead of crashing.
    """

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning("%s: output is not valid JSON; ignoring payload", command)
        return None
    try:
        payload = payload_type.model_validate(data)
    except ValidationError:
        logger.warning(
            "%s: output does not match %s; ignoring payload",
            command,
            payload_type.__name__,
        )
        return None
    if not payload.ok:
        logger.warning("%s: payload reports ok=false; ignoring payload", command)
        return None
    return payload


class OrcaClient:
    """Async client for the ``orca`` CLI.

    :param executable: CLI to spawn; resolved through ``PATH`` when it is a
        bare command name.
    :param timeout: maximum seconds per invocation before
        :class:`OrcaTimeoutError` is raised and the child is killed.
    :param subprocess_exec: optional process factory override, used by tests to
        avoid spawning a real process.

    The client keeps no state beyond its configuration, so one instance can be
    reused across poll cycles and concurrent calls.
    """

    def __init__(
        self,
        executable: str = DEFAULT_EXECUTABLE,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        subprocess_exec: SubprocessExec | None = None,
    ) -> None:
        if not executable.strip():
            raise ValueError("executable must be a non-empty command name or path")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(
                f"timeout must be a positive finite number, got {timeout!r}"
            )
        self._executable = executable
        self._timeout = float(timeout)
        self._subprocess_exec: SubprocessExec = (
            _spawn_default if subprocess_exec is None else subprocess_exec
        )

    @property
    def executable(self) -> str:
        """Configured CLI command or path."""

        return self._executable

    @property
    def timeout(self) -> float:
        """Maximum seconds allowed per CLI invocation."""

        return self._timeout

    async def status(self) -> StatusPayload | None:
        """Run ``orca status --json`` and parse the runtime status payload.

        The CLI answers even when the runtime is down; such a payload carries
        ``runtime.reachable: false`` and ``runtime.runtimeId: null``, which does
        not match :class:`StatusPayload`, so ``None`` is returned instead.
        """

        return _parse_payload(
            StatusPayload, await self._run(STATUS_ARGS), "orca status"
        )

    async def worktree_ps(self) -> WorktreePsPayload | None:
        """Run ``orca worktree ps --json`` and parse the worktree listing.

        This is the live agent source for presence counts. Raises
        :class:`OrcaNotRunningError` when the runtime is unreachable.
        """

        return _parse_payload(
            WorktreePsPayload, await self._run(WORKTREE_PS_ARGS), "orca worktree ps"
        )

    async def agent_hooks_status(self) -> AgentHooksStatusPayload | None:
        """Run ``orca agent hooks status --json`` and parse the hook statuses.

        The payload is a gate (are hooks installed?), never a live agent
        count. Raises :class:`OrcaNotRunningError` when the runtime is
        unreachable.
        """

        return _parse_payload(
            AgentHooksStatusPayload,
            await self._run(AGENT_HOOKS_STATUS_ARGS),
            "orca agent hooks status",
        )

    async def agent_hooks_on(self) -> None:
        """Run ``orca agent hooks on`` to enable Orca-managed status hooks.

        Best effort by design: a non-zero exit raises :class:`OrcaError` like
        every other invocation, so the caller decides whether the failure is
        tolerable. Live agent counts never depend on this command; they always
        come from :meth:`worktree_ps`.
        """

        await self._run(AGENT_HOOKS_ON_ARGS)

    async def _run(self, args: Sequence[str]) -> str:
        """Spawn ``orca`` with ``args`` and return decoded stdout.

        Raises :class:`OrcaCliNotFoundError` when the executable cannot be
        started, :class:`OrcaTimeoutError` when it outlives the timeout, and
        :class:`OrcaNotRunningError` / :class:`OrcaError` on a non-zero exit.
        """

        command = (self._executable, *args)
        logger.debug("Running Orca CLI command: %s", " ".join(command))
        try:
            process = await self._subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise OrcaCliNotFoundError(
                f"Orca CLI executable {self._executable!r} could not be "
                f"located or started: {exc}"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout
            )
        except TimeoutError as exc:
            await _kill_process(process)
            raise OrcaTimeoutError(
                f"{' '.join(command)} did not finish within "
                f"{self._timeout:g}s and was killed"
            ) from exc
        except asyncio.CancelledError:
            await _kill_process(process)
            raise

        returncode = process.returncode
        if returncode is not None and returncode != 0:
            _raise_for_exit(command, returncode, stdout, stderr)
        return stdout.decode("utf-8", errors="replace")
