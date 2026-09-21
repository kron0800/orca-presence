"""Read-only client for the local OpenCode usage database.

The Orca CLI exposes no token or cost command (see ``SPIKE.md``), so the only
per-session source is the local OpenCode database at
``~/.local/share/opencode/opencode.db``: the ``session`` table carries the
``tokens_input``, ``tokens_output``, ``tokens_reasoning`` and ``cost``
columns, and ``time_updated`` (a Unix timestamp in milliseconds) bounds the
counting window.

:func:`read_usage` aggregates that window for a caller-provided start
timestamp: it opens the database read-only through a ``file:`` URI and, when a
concurrent OpenCode writer holds a lock, copies the database (plus its WAL
sidecars) to a temporary directory and reads the copy instead, so OpenCode is
never disturbed. It never uses a blanket ``except Exception``: every failure
is classified as a :class:`UsageError` subclass and :func:`read_usage_or_none`
turns that into a logged ``None`` for the daemon's usage loop, which must keep
running even when the database is missing.

The module is fully synchronous: reading a local SQLite file takes
microseconds, so the poller calls it directly from its event loop.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "USAGE_QUERY",
    "UsageDbLockedError",
    "UsageDbNotFoundError",
    "UsageError",
    "UsageReadError",
    "UsageSnapshot",
    "read_usage",
    "read_usage_or_none",
]

logger = logging.getLogger(__name__)

# The advertised token total sums input, output and reasoning tokens; cache
# reads and cache writes are deliberately excluded. ``time_updated`` is a Unix
# timestamp in milliseconds, matching the caller-provided bound.
USAGE_QUERY = (
    "SELECT COUNT(*), "
    "COALESCE(SUM(tokens_input + tokens_output + tokens_reasoning), 0), "
    "COALESCE(SUM(cost), 0.0) "
    "FROM session WHERE time_updated >= ?"
)

_READ_ONLY_TIMEOUT_SECONDS = 1.0


class UsageError(RuntimeError):
    """Base class for OpenCode usage database failures."""


class UsageDbNotFoundError(UsageError):
    """The OpenCode usage database does not exist at the configured path."""


class UsageDbLockedError(UsageError):
    """The OpenCode usage database is locked and could not be copied."""


class UsageReadError(UsageError):
    """The OpenCode usage database exists but could not be read."""


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """Aggregated token and cost usage over a time window.

    ``tokens`` sums the input, output and reasoning tokens of every counted
    session, ``cost_usd`` sums the real reported cost and ``count`` is the
    number of sessions inside the window.
    """

    tokens: int
    cost_usd: float
    count: int


def read_usage(db_path: str | Path, start_ts_ms: int) -> UsageSnapshot:
    """Aggregate usage for sessions updated at or after ``start_ts_ms``.

    ``start_ts_ms`` is a Unix timestamp in milliseconds (the unit of the
    OpenCode ``session.time_updated`` column). The database is read in
    read-only mode; when a writer locks it, the file is copied to a temporary
    directory and the copy is queried instead.

    Raises a :class:`UsageError` subclass when the database is missing, stays
    locked after the copy fallback or cannot be read at all.
    """

    path = Path(db_path).expanduser()
    if not path.is_file():
        raise UsageDbNotFoundError(f"usage database not found: {path}")
    try:
        return _query(path, start_ts_ms, read_only=True)
    except UsageDbLockedError:
        logger.debug("Usage database %s is locked; reading a copy instead", path)
    return _read_copied(path, start_ts_ms)


def read_usage_or_none(db_path: str | Path, start_ts_ms: int) -> UsageSnapshot | None:
    """Read usage, logging and returning ``None`` instead of raising.

    This is the daemon-facing wrapper: presence must degrade gracefully when
    the OpenCode database is missing, locked or unreadable.
    """

    try:
        return read_usage(db_path, start_ts_ms)
    except UsageError as exc:
        logger.warning("OpenCode usage is unavailable: %s", exc)
        return None


def _query(path: Path, start_ts_ms: int, *, read_only: bool) -> UsageSnapshot:
    """Run the aggregate query on ``path`` and normalize the result row."""

    try:
        with closing(_connect(path, read_only=read_only)) as connection:
            row = connection.execute(USAGE_QUERY, (start_ts_ms,)).fetchone()
    except sqlite3.OperationalError as exc:
        if _is_locked_error(exc):
            raise UsageDbLockedError(f"usage database is locked: {path}") from exc
        raise UsageReadError(
            f"usage database could not be queried: {path}: {exc}"
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise UsageReadError(
            f"usage database could not be queried: {path}: {exc}"
        ) from exc

    if row is None:
        return UsageSnapshot(tokens=0, cost_usd=0.0, count=0)
    count, tokens, cost = row
    return UsageSnapshot(tokens=int(tokens), cost_usd=float(cost), count=int(count))


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    """Open ``path``, optionally through a read-only ``file:`` URI."""

    if read_only:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=_READ_ONLY_TIMEOUT_SECONDS)
    return sqlite3.connect(path, timeout=_READ_ONLY_TIMEOUT_SECONDS)


def _read_copied(path: Path, start_ts_ms: int) -> UsageSnapshot:
    """Copy the database and its WAL sidecars, then query the copy.

    Copying is the fallback for a database locked by a running OpenCode
    process: the copy can be read even while the original is being written.
    """

    try:
        with tempfile.TemporaryDirectory(prefix="orca-presence-usage-") as directory:
            copy = Path(directory) / path.name
            shutil.copy2(path, copy)
            for suffix in ("-wal", "-shm"):
                sidecar = path.with_name(f"{path.name}{suffix}")
                if sidecar.is_file():
                    shutil.copy2(sidecar, copy.with_name(f"{copy.name}{suffix}"))
            return _query(copy, start_ts_ms, read_only=False)
    except OSError as exc:
        raise UsageDbLockedError(
            f"usage database is locked and could not be copied: {path}: {exc}"
        ) from exc


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    """Return True when an operational error means the database is locked."""

    message = str(exc).lower()
    return "locked" in message or "busy" in message
