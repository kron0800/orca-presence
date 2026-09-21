"""Tests for the async poller that bridges Orca activity into Discord.

Every test injects fake Orca and Discord clients, so no CLI process is spawned
and no Discord IPC socket is opened. Waits are event-based rather than
time-based, keeping the suite deterministic and fast.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from pypresence import AioClient

from orca_presence.activity_builder import DiscordActivity
from orca_presence.config import ORCA_EXECUTABLE_ENV, Config
from orca_presence.discord_client import DiscordAuthError, DiscordPresenceClient
from orca_presence.models import AgentHooksStatusPayload, WorktreePsPayload
from orca_presence.orca_client import (
    OrcaClient,
    OrcaCliNotFoundError,
    OrcaError,
    OrcaNotRunningError,
)
from orca_presence.poller import Poller, should_enable_agent_hooks

FIXTURES = Path(__file__).parent / "fixtures"
CLIENT_ID = "123456789012345678"
POLL_INTERVAL = 0.05


def _config() -> Config:
    return Config(
        client_id=CLIENT_ID,
        poll_interval_seconds=POLL_INTERVAL,
        orca_executable="fake-orca",
        orca_timeout_seconds=0.5,
    )


def _worktree_payload() -> WorktreePsPayload:
    raw = json.loads((FIXTURES / "worktree_ps.json").read_text(encoding="utf-8"))
    return WorktreePsPayload.model_validate(raw)


def _hooks_payload(*, enabled: bool, states: list[str]) -> AgentHooksStatusPayload:
    return AgentHooksStatusPayload.model_validate(
        {
            "id": "local",
            "ok": True,
            "result": {
                "enabled": enabled,
                "settingsPath": "C:/orca-data.json",
                "statuses": [
                    {
                        "agent": f"agent{index}",
                        "state": state,
                        "configPath": "C:/settings.json",
                        "managedHooksPresent": state == "installed",
                    }
                    for index, state in enumerate(states)
                ],
            },
            "_meta": {"runtimeId": "local"},
        }
    )


async def _never_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
    raise AssertionError("the fake Orca client must not spawn a process")


def _unused_factory(_client_id: str) -> AioClient:
    raise AssertionError("the fake Discord client must not create an IPC client")


class FakeOrcaClient(OrcaClient):
    """OrcaClient whose answers are scripted by the test."""

    def __init__(self) -> None:
        super().__init__("fake-orca", timeout=0.5, subprocess_exec=_never_spawn)
        self.worktree_payload: WorktreePsPayload | None = None
        self.worktree_error: Exception | None = None
        self.worktree_calls = 0
        self.worktree_polled = asyncio.Event()
        self.hooks_status: AgentHooksStatusPayload | None = None
        self.hooks_status_error: Exception | None = None
        self.hooks_on_calls = 0
        self.hooks_on_error: Exception | None = None
        self.hooks_on_event = asyncio.Event()

    async def worktree_ps(self) -> WorktreePsPayload | None:
        self.worktree_calls += 1
        self.worktree_polled.set()
        if self.worktree_error is not None:
            raise self.worktree_error
        return self.worktree_payload

    async def agent_hooks_status(self) -> AgentHooksStatusPayload | None:
        if self.hooks_status_error is not None:
            raise self.hooks_status_error
        return self.hooks_status

    async def agent_hooks_on(self) -> None:
        self.hooks_on_calls += 1
        self.hooks_on_event.set()
        if self.hooks_on_error is not None:
            raise self.hooks_on_error


class FakeDiscordClient(DiscordPresenceClient):
    """DiscordPresenceClient that records calls instead of touching Discord."""

    def __init__(self) -> None:
        super().__init__(CLIENT_ID, client_factory=_unused_factory)
        self.published: list[DiscordActivity] = []
        self.publish_calls = 0
        self.maintain_calls = 0
        self.close_calls = 0
        self.cancel_calls = 0
        self.published_event = asyncio.Event()
        self.maintain_error: Exception | None = None
        self._release = asyncio.Event()
        self._closed = False

    async def maintain(self) -> None:
        self.maintain_calls += 1
        if self.maintain_error is not None:
            raise self.maintain_error
        await self._release.wait()

    async def publish(self, activity: DiscordActivity) -> bool:
        self.publish_calls += 1
        self.published.append(activity)
        self.published_event.set()
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_calls += 1
        self._release.set()
        await super().close()

    def cancel(self) -> None:
        self.cancel_calls += 1
        super().cancel()


def _poller(orca: FakeOrcaClient, discord: FakeDiscordClient) -> Poller:
    return Poller(_config(), orca_client=orca, discord_client=discord)


async def _start(poller: Poller) -> asyncio.Task[None]:
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0)
    return task


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in _leaves(child)]
    return [error]


@pytest.mark.parametrize(
    ("enabled", "states", "expected"),
    [
        (False, [], True),
        (False, ["installed"], True),
        (True, [], True),
        (True, ["not_installed", "not_installed"], True),
        (True, ["installed", "not_installed"], False),
        (True, ["partial"], True),
        (True, ["error"], True),
    ],
)
def test_should_enable_agent_hooks(
    enabled: bool, states: list[str], expected: bool
) -> None:
    payload = _hooks_payload(enabled=enabled, states=states)

    assert should_enable_agent_hooks(payload) is expected


@pytest.mark.asyncio
async def test_run_enables_disabled_agent_hooks() -> None:
    orca = FakeOrcaClient()
    orca.hooks_status = _hooks_payload(enabled=False, states=["not_installed"])
    discord = FakeDiscordClient()
    poller = _poller(orca, discord)

    task = await _start(poller)
    await asyncio.wait_for(orca.hooks_on_event.wait(), timeout=1.0)

    assert orca.hooks_on_calls == 1
    await _cancel(task)


@pytest.mark.asyncio
async def test_run_skips_enabling_installed_agent_hooks() -> None:
    orca = FakeOrcaClient()
    orca.hooks_status = _hooks_payload(enabled=True, states=["installed"])
    discord = FakeDiscordClient()
    poller = _poller(orca, discord)

    task = await _start(poller)
    await asyncio.wait_for(orca.worktree_polled.wait(), timeout=1.0)

    assert orca.hooks_on_calls == 0
    await _cancel(task)


@pytest.mark.asyncio
async def test_run_tolerates_a_failed_hooks_enable_attempt() -> None:
    orca = FakeOrcaClient()
    orca.hooks_status = _hooks_payload(enabled=False, states=[])
    orca.hooks_on_error = OrcaError("hooks could not be enabled")
    discord = FakeDiscordClient()
    poller = _poller(orca, discord)

    task = await _start(poller)
    await asyncio.wait_for(orca.hooks_on_event.wait(), timeout=1.0)
    await asyncio.wait_for(orca.worktree_polled.wait(), timeout=1.0)

    assert orca.hooks_on_calls == 1
    assert task.done() is False
    await _cancel(task)


@pytest.mark.asyncio
async def test_run_skips_hook_repair_when_the_status_is_unavailable() -> None:
    orca = FakeOrcaClient()
    orca.hooks_status_error = OrcaError("orca is not running")
    discord = FakeDiscordClient()
    poller = _poller(orca, discord)

    task = await _start(poller)
    await asyncio.wait_for(orca.worktree_polled.wait(), timeout=1.0)

    assert orca.hooks_on_calls == 0
    await _cancel(task)


@pytest.mark.asyncio
async def test_poll_loop_publishes_the_normalized_activity() -> None:
    orca = FakeOrcaClient()
    orca.worktree_payload = _worktree_payload()
    discord = FakeDiscordClient()
    poller = _poller(orca, discord)

    task = await _start(poller)
    await asyncio.wait_for(discord.published_event.wait(), timeout=1.0)

    activity = discord.published[0]
    assert activity.state == "1 active, 0 blocked"
    assert activity.party_size == (1, 8)
    assert discord.maintain_calls == 1
    await _cancel(task)


@pytest.mark.asyncio
async def test_poll_loop_keeps_running_while_orca_is_not_running() -> None:
    orca = FakeOrcaClient()
    orca.worktree_error = OrcaNotRunningError("runtime_unavailable")
    discord = FakeDiscordClient()
    poller = _poller(orca, discord)

    task = await _start(poller)
    await asyncio.wait_for(discord.published_event.wait(), timeout=1.0)
    assert discord.published[0].state == "No agents running"
    assert discord.published[0].party_size is None

    orca.worktree_error = None
    orca.worktree_payload = _worktree_payload()
    orca.worktree_polled.clear()
    discord.published.clear()
    discord.published_event.clear()
    await asyncio.wait_for(orca.worktree_polled.wait(), timeout=1.0)
    await asyncio.wait_for(discord.published_event.wait(), timeout=1.0)

    assert task.done() is False
    assert discord.published[0].state == "1 active, 0 blocked"
    await _cancel(task)


@pytest.mark.asyncio
async def test_poll_loop_reports_a_missing_cli_with_a_remediation_hint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    orca = FakeOrcaClient()
    orca.worktree_error = OrcaCliNotFoundError("orca could not be located")
    discord = FakeDiscordClient()
    poller = _poller(orca, discord)
    caplog.set_level(logging.WARNING)

    task = await _start(poller)
    await asyncio.wait_for(orca.worktree_polled.wait(), timeout=1.0)
    await asyncio.sleep(0)

    assert task.done() is False
    assert "was not found or could not be started" in caplog.text
    assert "fake-orca" in caplog.text
    assert ORCA_EXECUTABLE_ENV in caplog.text
    await _cancel(task)


@pytest.mark.asyncio
async def test_poll_loop_logs_orca_going_offline_and_back_online(
    caplog: pytest.LogCaptureFixture,
) -> None:
    orca = FakeOrcaClient()
    orca.worktree_error = OrcaNotRunningError("runtime_unavailable")
    discord = FakeDiscordClient()
    poller = _poller(orca, discord)
    caplog.set_level(logging.INFO)

    task = await _start(poller)
    await asyncio.wait_for(discord.published_event.wait(), timeout=1.0)
    assert "Orca runtime is unreachable" in caplog.text

    orca.worktree_error = None
    orca.worktree_payload = _worktree_payload()
    orca.worktree_polled.clear()
    discord.published_event.clear()
    await asyncio.wait_for(orca.worktree_polled.wait(), timeout=1.0)
    await asyncio.wait_for(discord.published_event.wait(), timeout=1.0)

    assert "Connected to the Orca runtime" in caplog.text
    await _cancel(task)


@pytest.mark.asyncio
async def test_cancelling_run_closes_the_discord_client() -> None:
    orca = FakeOrcaClient()
    orca.worktree_payload = _worktree_payload()
    discord = FakeDiscordClient()
    poller = _poller(orca, discord)

    task = await _start(poller)
    await asyncio.wait_for(discord.published_event.wait(), timeout=1.0)
    await _cancel(task)

    assert discord.close_calls == 1
    assert discord.cancel_calls == 0


@pytest.mark.asyncio
async def test_unrecoverable_discord_errors_propagate_and_close_the_client() -> None:
    orca = FakeOrcaClient()
    discord = FakeDiscordClient()
    discord.maintain_error = DiscordAuthError("invalid client id")
    poller = _poller(orca, discord)

    task = asyncio.create_task(poller.run())

    with pytest.raises(ExceptionGroup) as excinfo:
        await asyncio.wait_for(task, timeout=1.0)

    assert any(isinstance(leaf, DiscordAuthError) for leaf in _leaves(excinfo.value))
    assert discord.close_calls == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_the_throttle_and_closes_the_client_once() -> None:
    orca = FakeOrcaClient()
    discord = FakeDiscordClient()
    poller = _poller(orca, discord)

    await poller.shutdown()
    await poller.shutdown()

    assert discord.cancel_calls == 2
    assert discord.close_calls == 1
