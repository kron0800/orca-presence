"""Tests for the daemon entry point and its logging configuration.

The poller itself is covered by ``test_poller.py``; here ``_serve`` is patched
so every exit-code path can be exercised without an event loop or a daemon.
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

import pytest

import orca_presence.main as main_module
from orca_presence.config import CLIENT_ID_ENV, CONFIG_FILE_ENV, Config
from orca_presence.discord_client import DiscordClientError
from orca_presence.main import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    LOG_LEVEL_ENV,
    configure_logging,
    run,
)
from orca_presence.orca_client import OrcaError

CLIENT_ID = "123456789012345678"


def test_configure_logging_defaults_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)

    configure_logging(stream=io.StringIO())

    assert logging.getLogger().level == logging.INFO


def test_configure_logging_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "debug")

    configure_logging(stream=io.StringIO())

    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_accepts_numeric_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "10")

    configure_logging(stream=io.StringIO())

    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_falls_back_on_an_unknown_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "verbose")
    stream = io.StringIO()

    configure_logging(stream=stream)

    assert logging.getLogger().level == logging.INFO
    assert "Invalid log level" in stream.getvalue()


def test_run_returns_the_config_error_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(CONFIG_FILE_ENV, raising=False)

    assert run() == EXIT_CONFIG_ERROR


def _patch_serve(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> io.StringIO:
    """Make ``run`` fail with ``error`` and capture the daemon log output."""

    stream = io.StringIO()
    original = main_module.configure_logging

    def configure() -> None:
        original(stream=stream)

    async def serve(_config: Config) -> int:
        raise error

    monkeypatch.setattr(main_module, "configure_logging", configure)
    monkeypatch.setattr(main_module, "_serve", serve)
    return stream


@pytest.mark.parametrize(
    "error",
    [
        OrcaError("orca status failed"),
        DiscordClientError("discord transport failed"),
        OSError("the event loop could not be created"),
        RuntimeError("the event loop is closed"),
        ExceptionGroup("poller failed", [RuntimeError("discord handshake rejected")]),
    ],
)
def test_run_returns_the_failure_code_for_known_runtime_errors(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    stream = _patch_serve(monkeypatch, error)

    assert run(Config(CLIENT_ID)) == EXIT_FAILURE

    assert "Unrecoverable presence error" in stream.getvalue()


def test_run_describes_the_leaf_of_an_exception_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _patch_serve(
        monkeypatch,
        ExceptionGroup("poller failed", [DiscordClientError("invalid client id")]),
    )

    assert run(Config(CLIENT_ID)) == EXIT_FAILURE

    assert "DiscordClientError: invalid client id" in stream.getvalue()


def test_run_returns_the_interrupted_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _patch_serve(monkeypatch, KeyboardInterrupt())

    assert run(Config(CLIENT_ID)) == EXIT_INTERRUPTED

    assert "Interrupted" in stream.getvalue()


def test_run_does_not_swallow_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_serve(monkeypatch, KeyError("bug"))

    with pytest.raises(KeyError, match="bug"):
        run(Config(CLIENT_ID))


