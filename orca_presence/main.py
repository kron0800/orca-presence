"""Executable entry point for the orca-presence daemon.

``python -m orca_presence.main`` (or the ``orca-presence`` console script,
which points at :func:`run`) resolves the configuration, configures logging and
runs the poller until SIGINT or SIGTERM arrives.

Shutdown is deliberate and ordered:

1. the signal sets an :class:`asyncio.Event`, never raising inside the handler;
2. the poller task is cancelled, which cancels the Discord throttle, clears the
   presence and closes the IPC socket (:meth:`Poller.shutdown` repeats the
   close, so it also runs when the poller already stopped);
3. :func:`logging.shutdown` flushes and closes every logging handler.

Log levels follow one rule: INFO for connections and lifecycle transitions,
DEBUG for per-publish detail (the Discord client logs every publish at DEBUG)
and ERROR for unrecoverable failures. The module never prints.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from types import FrameType, TracebackType
from typing import IO, Self

from orca_presence.config import Config, ConfigError, load_env_file
from orca_presence.discord_client import DiscordClientError
from orca_presence.orca_client import OrcaError
from orca_presence.poller import Poller

__all__ = ["LOG_LEVEL_ENV", "configure_logging", "main", "run"]

logger = logging.getLogger(__name__)

LOG_LEVEL_ENV = "ORCA_PRESENCE_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_INTERRUPTED = 130

SignalHandler = Callable[[int, FrameType | None], object]
RestorableHandler = SignalHandler | int | signal.Handlers | None


def configure_logging(
    level: int | str | None = None, *, stream: IO[str] | None = None
) -> None:
    """Configure root logging for the daemon.

    ``level`` defaults to the ``ORCA_PRESENCE_LOG_LEVEL`` environment variable
    and then to ``INFO``. Numeric strings such as ``"10"`` are accepted, and an
    unknown level name falls back to ``INFO`` with a warning instead of
    aborting the daemon. ``stream`` defaults to :data:`sys.stderr`; tests can
    pass an in-memory stream.
    """

    resolved = _resolve_log_level(
        level if level is not None else os.environ.get(LOG_LEVEL_ENV, DEFAULT_LOG_LEVEL)
    )
    try:
        _basic_config(resolved, stream)
    except ValueError:
        _basic_config(DEFAULT_LOG_LEVEL, stream)
        logger.warning(
            "Invalid log level %r; falling back to %s", resolved, DEFAULT_LOG_LEVEL
        )


def _resolve_log_level(raw: int | str) -> int | str:
    """Normalize a level setting, turning numeric strings into integers."""

    if isinstance(raw, str):
        text = raw.strip()
        if text.lstrip("+-").isdigit():
            return int(text)
        return text.upper()
    return raw


def _basic_config(level: int | str, stream: IO[str] | None) -> None:
    """Apply one :func:`logging.basicConfig` call for the daemon."""

    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        stream=stream,
        force=True,
    )


class _ShutdownSignals:
    """Turn SIGINT/SIGTERM into a single :class:`asyncio.Event` wake-up.

    The loop-level handlers are preferred because they run on the event loop
    thread; Windows event loops do not implement them, so the process-level
    :func:`signal.signal` handlers are used there. Signal handlers never raise:
    a repeated signal only logs a warning while the first shutdown is already
    in progress.
    """

    def __init__(self, stop: asyncio.Event) -> None:
        self._stop = stop
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_signals: list[signal.Signals] = []
        self._previous_handlers: list[tuple[signal.Signals, RestorableHandler]] = []

    def __enter__(self) -> Self:
        self._loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, self._request_stop, sig)
            except (NotImplementedError, RuntimeError, ValueError):
                self._install_fallback(sig)
            else:
                self._loop_signals.append(sig)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        loop = self._loop
        if loop is not None:
            for sig in self._loop_signals:
                loop.remove_signal_handler(sig)
        for sig, previous in self._previous_handlers:
            signal.signal(sig, previous)

    def _install_fallback(self, sig: signal.Signals) -> None:
        """Install a process-level handler when the loop cannot do it."""

        try:
            previous = signal.signal(sig, self._handle_signal)
        except (OSError, ValueError):
            logger.debug("Signal %s cannot be handled in this thread", sig.name)
        else:
            self._previous_handlers.append((sig, previous))

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        """Process-level signal handler; runs on the main thread."""

        self._request_stop(signum)

    def _request_stop(self, signum: int) -> None:
        """Request one clean shutdown; later signals are logged and ignored."""

        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        if self._stop.is_set():
            logger.warning("Received %s again; shutdown is already in progress", name)
            return
        logger.info("Received %s; shutting down", name)
        self._stop.set()


async def _serve(config: Config) -> int:
    """Run the poller until a shutdown signal arrives, then shut down cleanly."""

    stop = asyncio.Event()
    poller = Poller(config)
    with _ShutdownSignals(stop):
        run_task: asyncio.Task[None] = asyncio.create_task(poller.run())
        stop_task: asyncio.Task[bool] = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait(
                {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stop_task.cancel()
            run_task.cancel()
            await asyncio.gather(stop_task, run_task, return_exceptions=True)
            await poller.shutdown()

    if run_task.cancelled():
        return EXIT_OK
    error = run_task.exception()
    if error is None:
        return EXIT_OK
    logger.error("Unrecoverable presence error: %s", _describe(error))
    return EXIT_FAILURE


def _is_timeout_group(error: BaseException) -> bool:
    """True for a timeout or an exception group made only of timeouts."""

    if isinstance(error, TimeoutError):
        return True
    if isinstance(error, BaseExceptionGroup):
        return all(_is_timeout_group(child) for child in error.exceptions)
    return False


def _describe(error: BaseException) -> str:
    """Describe an exception, unwrapping the first leaf of an exception group."""

    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}"


def run(config: Config | None = None) -> int:
    """Entry point: load ``.env``, resolve configuration, run the daemon.

    Before configuration is resolved, :func:`load_env_file` loads the ``.env``
    file (if present) into the process environment so the same values are seen
    by :func:`configure_logging` and :meth:`Config.load`. Existing environment
    variables always win over the file.

    Returns the process exit code: :data:`EXIT_OK` on a clean shutdown,
    :data:`EXIT_FAILURE` on a known unrecoverable runtime error,
    :data:`EXIT_CONFIG_ERROR` on an invalid configuration and
    :data:`EXIT_INTERRUPTED` when the process is interrupted before the
    shutdown handler is installed. Logging handlers are flushed on the way out.

    Only the daemon's documented failure modes (Orca CLI errors, Discord
    transport errors, operating-system errors, event-loop errors and the
    :class:`ExceptionGroup` raised by the poller's task groups) are translated
    into :data:`EXIT_FAILURE`; anything else propagates with its traceback so a
    bug is never hidden behind an exit code.
    """

    load_env_file()
    configure_logging()
    try:
        resolved = Config.load() if config is None else config
    except ConfigError as exc:
        logger.error("Invalid configuration: %s", exc)
        return EXIT_CONFIG_ERROR

    try:
        return asyncio.run(_serve(resolved))
    except KeyboardInterrupt:
        logger.info("Interrupted before the shutdown handler was installed")
        return EXIT_INTERRUPTED
    except (
        OrcaError,
        DiscordClientError,
        OSError,
        RuntimeError,
        ExceptionGroup,
    ) as exc:
        logger.error("Unrecoverable presence error: %s", _describe(exc))
        return EXIT_FAILURE
    finally:
        logging.shutdown()


def main() -> None:
    """Run the daemon and exit with its status code."""

    raise SystemExit(run())


if __name__ == "__main__":
    main()
