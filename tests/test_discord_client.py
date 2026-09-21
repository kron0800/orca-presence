"""Tests for the Discord Rich Presence transport wrapper.

Every test injects a fake pypresence client, clock and sleep so reconnection,
backoff and throttling are exercised without a running Discord IPC socket.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pytest
from pypresence.exceptions import (
    DiscordError,
    DiscordNotFound,
    InvalidID,
    InvalidPipe,
    ServerError,
)

import orca_presence.discord_client as discord_client_module
from orca_presence.activity_builder import DiscordActivity
from orca_presence.discord_client import (
    CLIENT_ID_ENV,
    INITIAL_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    DiscordAuthError,
    DiscordClientError,
    DiscordConfigError,
    DiscordNotRunningError,
    DiscordPresenceClient,
    next_backoff,
)
from orca_presence.throttle import DEFAULT_INTERVAL_SECONDS, ActivityThrottle

START_TS = 1_789_549_800_000


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWriter:
    """Minimal stand-in for an asyncio StreamWriter."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeAioClient:
    """Stand-in for ``pypresence.AioClient`` that never touches Discord."""

    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        send_error: Exception | None = None,
        clear_error: Exception | None = None,
        clear_delay_seconds: float | None = None,
    ) -> None:
        self.client_id = "fake"
        self.sock_writer: FakeWriter | None = None
        self.start_error = start_error
        self.send_error = send_error
        self.clear_error = clear_error
        self.clear_delay_seconds = clear_delay_seconds
        self.sent: list[dict[str, object]] = []
        self.clear_calls = 0
        self.close_frames = 0

    async def start(self) -> None:
        if self.start_error is not None:
            self.sock_writer = FakeWriter()
            raise self.start_error
        self.sock_writer = FakeWriter()

    async def set_activity(self, **kwargs: object) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(dict(kwargs))

    async def clear_activity(self) -> None:
        self.clear_calls += 1
        if self.clear_error is not None:
            raise self.clear_error
        if self.clear_delay_seconds is not None:
            # Simulates pypresence's read_output waiting for an IPC answer that
            # never arrives while Discord reports "server at capacity".
            await asyncio.sleep(self.clear_delay_seconds)

    def send_data(self, op: int, payload: object) -> None:
        self.close_frames += 1


class RecordingSleep:
    """Sleep replacement that records delays and yields to the scheduler."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)


class SpyThrottle(ActivityThrottle):
    """Throttle that counts how often ``reset`` ran."""

    def __init__(self) -> None:
        super().__init__()
        self.reset_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1
        super().reset()


def _activity(
    *,
    state: str | None = "1 active, 0 blocked",
    party_size: tuple[int, int] | None = (1, 1),
) -> DiscordActivity:
    return DiscordActivity(
        details="Playing Orca ADE",
        state=state,
        start_ts=START_TS,
        large_image="orca_ade",
        large_text="Orca Dev ADE",
        party_size=party_size,
    )


def _factory_for(*clients: FakeAioClient) -> Callable[[str], FakeAioClient]:
    queue = list(clients)

    def factory(client_id: str) -> FakeAioClient:
        return queue.pop(0)

    return factory


def _failing_factory(error: Exception) -> Callable[[str], FakeAioClient]:
    def factory(client_id: str) -> FakeAioClient:
        raise error

    return factory


def _scripted_factory(
    outcomes: list[Exception | None],
) -> Callable[[str], FakeAioClient]:
    queue = list(outcomes)

    def factory(client_id: str) -> FakeAioClient:
        return FakeAioClient(start_error=queue.pop(0))

    return factory


def _client(
    factory: Callable[[str], FakeAioClient],
    *,
    throttle: ActivityThrottle | None = None,
    sleep: RecordingSleep | None = None,
) -> DiscordPresenceClient:
    return DiscordPresenceClient(
        "123456789012345678",
        throttle=throttle,
        client_factory=factory,
        sleep=RecordingSleep() if sleep is None else sleep,
    )


async def _drain(times: int = 1) -> None:
    """Let pending tasks advance one scheduler slice per call."""

    for _ in range(times):
        await asyncio.sleep(0)


def test_next_backoff_doubles_up_to_the_cap() -> None:
    delays: list[float] = []
    delay = INITIAL_BACKOFF_SECONDS
    for _ in range(6):
        delays.append(delay)
        delay = next_backoff(delay)

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    assert next_backoff(INITIAL_BACKOFF_SECONDS) == 2.0
    assert next_backoff(MAX_BACKOFF_SECONDS) == MAX_BACKOFF_SECONDS
    assert MAX_BACKOFF_SECONDS == 30.0


def test_missing_client_id_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CLIENT_ID_ENV, raising=False)

    with pytest.raises(DiscordConfigError):
        DiscordPresenceClient()


def test_blank_client_id_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CLIENT_ID_ENV, "   ")

    with pytest.raises(DiscordConfigError):
        DiscordPresenceClient()


def test_client_id_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CLIENT_ID_ENV, " 123456789012345678 ")

    assert DiscordPresenceClient().client_id == "123456789012345678"


def test_explicit_client_id_overrides_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CLIENT_ID_ENV, "111111111111111111")

    assert DiscordPresenceClient("222222222222222222").client_id == "222222222222222222"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        DiscordNotFound(),
        InvalidPipe(),
        InvalidID(),
        ServerError("internal error"),
        OSError("pipe refused"),
    ],
)
async def test_connect_classifies_retryable_failures(error: Exception) -> None:
    client = _client(_failing_factory(error))

    with pytest.raises(DiscordNotRunningError) as excinfo:
        await client.connect()

    assert isinstance(excinfo.value.__cause__, type(error))


@pytest.mark.asyncio
async def test_connect_classifies_invalid_client_id_as_fatal() -> None:
    fake = FakeAioClient(start_error=DiscordError(4000, "Invalid Client ID"))
    client = _client(_factory_for(fake))

    with pytest.raises(DiscordAuthError) as excinfo:
        await client.connect()

    assert isinstance(excinfo.value, DiscordConfigError)
    assert CLIENT_ID_ENV in str(excinfo.value)
    assert "4000" in str(excinfo.value)
    assert fake.close_frames == 1
    assert fake.sock_writer is not None
    assert fake.sock_writer.closed is True


@pytest.mark.asyncio
async def test_connect_keeps_retrying_unknown_handshake_codes() -> None:
    fake = FakeAioClient(start_error=DiscordError(5000, "Internal error"))
    client = _client(_factory_for(fake))

    with pytest.raises(DiscordNotRunningError):
        await client.connect()

    assert fake.sock_writer is not None
    assert fake.sock_writer.closed is True


@pytest.mark.asyncio
async def test_connect_after_close_is_rejected() -> None:
    client = _client(_factory_for(FakeAioClient()))
    await client.close()

    with pytest.raises(DiscordClientError):
        await client.connect()


@pytest.mark.asyncio
async def test_maintain_retries_with_exponential_backoff_and_connects() -> None:
    sleep = RecordingSleep()
    factory = _scripted_factory([InvalidPipe(), InvalidPipe(), None])
    client = _client(factory, sleep=sleep)

    task = asyncio.create_task(client.maintain())
    await _drain(3)

    assert sleep.delays == [1.0, 2.0]
    assert client.connected is True

    await client.close()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_maintain_stops_on_rejected_client_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sleep = RecordingSleep()
    factory = _scripted_factory(
        [InvalidPipe(), DiscordError(4000, "Invalid Client ID")]
    )
    client = _client(factory, sleep=sleep)
    caplog.set_level(logging.CRITICAL)

    task = asyncio.create_task(client.maintain())

    with pytest.raises(DiscordAuthError):
        await asyncio.wait_for(task, timeout=1.0)

    assert sleep.delays == [1.0]
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)
    assert CLIENT_ID_ENV in caplog.text


@pytest.mark.asyncio
async def test_maintain_logs_a_clear_warning_while_discord_is_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sleep = RecordingSleep()
    client = _client(_failing_factory(DiscordNotFound()), sleep=sleep)
    caplog.set_level(logging.WARNING)

    task = asyncio.create_task(client.maintain())
    await _drain(2)

    assert "Discord is not running or unreachable" in caplog.text
    assert sleep.delays == [1.0, 2.0]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_maintain_resets_the_throttle_after_reconnecting() -> None:
    throttle = SpyThrottle()
    first = FakeAioClient()
    second = FakeAioClient()
    client = _client(_factory_for(first, second), throttle=throttle)

    task = asyncio.create_task(client.maintain())
    await _drain(1)
    assert client.connected is True
    assert throttle.reset_calls == 1

    first.send_error = InvalidID()
    assert await client.publish(_activity(state="2 active, 0 blocked")) is False

    await _drain(2)

    assert second.sent
    assert throttle.reset_calls == 2

    await client.close()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_maintain_republishes_the_last_activity_after_reconnecting() -> None:
    first = FakeAioClient()
    second = FakeAioClient()
    client = _client(_factory_for(first, second))

    task = asyncio.create_task(client.maintain())
    await _drain(1)
    assert await client.publish(_activity()) is True
    assert len(first.sent) == 1

    first.send_error = OSError("pipe closed")
    changed = _activity(state="3 active, 1 blocked")
    assert await client.publish(changed) is False
    assert client.connected is False

    await _drain(2)

    assert [payload["state"] for payload in second.sent] == [changed.state]
    assert client.connected is True

    await client.close()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_publish_before_connect_defers_then_sends() -> None:
    fake = FakeAioClient()
    client = _client(_factory_for(fake))
    activity = _activity()

    assert await client.publish(activity) is False
    assert fake.sent == []

    await client.connect()

    assert await client.publish(activity) is True
    assert len(fake.sent) == 1


@pytest.mark.asyncio
async def test_publish_flattens_the_activity_for_pypresence() -> None:
    fake = FakeAioClient()
    client = _client(_factory_for(fake))
    await client.connect()

    assert await client.publish(_activity()) is True

    assert fake.sent == [
        {
            "details": "Playing Orca ADE",
            "state": "1 active, 0 blocked",
            "start": START_TS,
            "large_image": "orca_ade",
            "large_text": "Orca Dev ADE",
            "party_size": [1, 1],
        }
    ]

    assert (
        await client.publish(_activity(state="2 active, 0 blocked", party_size=None))
        is True
    )
    assert "party_size" not in fake.sent[-1]


@pytest.mark.asyncio
async def test_publish_omits_a_missing_state() -> None:
    fake = FakeAioClient()
    client = _client(_factory_for(fake))
    await client.connect()

    assert await client.publish(_activity(state=None)) is True

    assert "state" not in fake.sent[-1]
    assert fake.sent[-1]["details"] == "Playing Orca ADE"


@pytest.mark.asyncio
async def test_publish_throttles_unchanged_activity_until_the_interval() -> None:
    clock = FakeClock()
    fake = FakeAioClient()
    client = _client(_factory_for(fake), throttle=ActivityThrottle(clock=clock))
    await client.connect()
    activity = _activity()

    assert await client.publish(activity) is True
    assert await client.publish(activity) is False
    clock.advance(DEFAULT_INTERVAL_SECONDS - 0.001)
    assert await client.publish(activity) is False
    clock.advance(0.001)
    assert await client.publish(activity) is True
    assert len(fake.sent) == 2


@pytest.mark.asyncio
async def test_publish_sends_changed_activity_immediately() -> None:
    clock = FakeClock()
    fake = FakeAioClient()
    client = _client(_factory_for(fake), throttle=ActivityThrottle(clock=clock))
    await client.connect()

    assert await client.publish(_activity(state="1 active, 0 blocked")) is True
    clock.advance(0.001)
    assert await client.publish(_activity(state="2 active, 0 blocked")) is True
    assert len(fake.sent) == 2


@pytest.mark.asyncio
async def test_publish_marks_disconnected_on_send_failure() -> None:
    fake = FakeAioClient()
    client = _client(_factory_for(fake))
    await client.connect()
    fake.send_error = OSError("pipe closed")

    assert await client.publish(_activity()) is False
    assert client.connected is False


@pytest.mark.asyncio
async def test_reset_forces_republish_of_identical_activity() -> None:
    clock = FakeClock()
    fake = FakeAioClient()
    client = _client(_factory_for(fake), throttle=ActivityThrottle(clock=clock))
    await client.connect()
    activity = _activity()

    assert await client.publish(activity) is True
    assert await client.publish(activity) is False

    client.reset()

    assert await client.publish(activity) is True
    assert len(fake.sent) == 2


@pytest.mark.asyncio
async def test_cancel_stops_publishing_until_reset() -> None:
    fake = FakeAioClient()
    client = _client(_factory_for(fake))
    await client.connect()
    client.cancel()

    assert await client.publish(_activity()) is False
    assert fake.sent == []

    client.reset()

    assert await client.publish(_activity()) is True
    assert len(fake.sent) == 1


@pytest.mark.asyncio
async def test_close_clears_presence_and_closes_the_socket_idempotently() -> None:
    fake = FakeAioClient()
    client = _client(_factory_for(fake))
    task = asyncio.create_task(client.maintain())
    await _drain(1)
    assert client.connected is True

    await client.close()

    assert fake.clear_calls == 1
    assert fake.close_frames == 1
    assert fake.sock_writer is not None
    assert fake.sock_writer.closed is True
    assert client.connected is False

    await asyncio.wait_for(task, timeout=1.0)
    await client.close()

    assert fake.clear_calls == 1
