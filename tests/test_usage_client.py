"""Tests for the read-only OpenCode usage database client.

Every test builds a synthetic ``session`` database in ``tmp_path`` from the
recorded schema fixture, so nothing depends on the machine's real OpenCode
database and no OpenCode process is required.
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

import pytest

from orca_presence.usage_client import (
    UsageDbLockedError,
    UsageDbNotFoundError,
    UsageError,
    UsageReadError,
    UsageSnapshot,
    read_usage,
    read_usage_or_none,
)

FIXTURES = Path(__file__).parent / "fixtures"
START_TS_MS = 1_789_549_800_000


def _create_db(path: Path, rows: Iterable[tuple[int, int, int, int, float]]) -> None:
    """Create a ``session`` database with the recorded schema and rows.

    Every row is ``(time_updated, tokens_input, tokens_output, tokens_reasoning,
    cost)``; the cache columns are filled with sentinels that must stay out of
    the advertised total.
    """

    schema = (FIXTURES / "opencode_db_session_schema.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(schema)
        connection.executemany(
            "INSERT INTO session ("
            "id, project_id, slug, directory, title, version, time_created, "
            "time_updated, tokens_input, tokens_output, tokens_reasoning, "
            "tokens_cache_read, tokens_cache_write, cost"
            ") VALUES (?, 'proj', ?, 'C:/work', ?, '1.0', 0, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"session-{index}",
                    f"session-{index}",
                    f"Session {index}",
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    999_999,
                    888_888,
                    row[4],
                )
                for index, row in enumerate(rows)
            ],
        )
        connection.commit()


def test_aggregates_only_sessions_inside_the_window(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _create_db(
        db,
        [
            (START_TS_MS - 1, 5_000, 5_000, 5_000, 9.99),
            (START_TS_MS, 1_000, 200, 50, 0.25),
            (START_TS_MS + 5_000, 0, 1_000_000, 2_000_000, 0.34),
        ],
    )

    snapshot = read_usage(db, START_TS_MS)

    assert snapshot.tokens == 3_001_250
    assert snapshot.cost_usd == pytest.approx(0.59)
    assert snapshot.count == 2


def test_empty_window_returns_zeroed_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _create_db(db, [(START_TS_MS - 10, 100, 100, 100, 1.5)])

    snapshot = read_usage(db, START_TS_MS)

    assert snapshot == UsageSnapshot(tokens=0, cost_usd=0.0, count=0)


def test_missing_database_raises_db_not_found(tmp_path: Path) -> None:
    with pytest.raises(UsageDbNotFoundError):
        read_usage(tmp_path / "missing.db", START_TS_MS)


def test_read_usage_or_none_logs_and_returns_none_for_a_missing_database(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    assert read_usage_or_none(tmp_path / "missing.db", START_TS_MS) is None
    assert "usage is unavailable" in caplog.text


def test_unreadable_database_raises_usage_read_error(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    db.write_text("this is not a database", encoding="utf-8")

    with pytest.raises(UsageReadError):
        read_usage(db, START_TS_MS)


def test_locked_database_is_read_through_a_copy(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _create_db(db, [(START_TS_MS, 1_000, 0, 0, 0.5)])

    writer = sqlite3.connect(db)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        snapshot = read_usage(db, START_TS_MS)
    finally:
        writer.close()

    assert snapshot.tokens == 1_000
    assert snapshot.cost_usd == pytest.approx(0.5)
    assert snapshot.count == 1


def test_snapshot_is_frozen() -> None:
    snapshot = UsageSnapshot(tokens=1, cost_usd=0.0, count=1)
    field_name = "tokens"

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(snapshot, field_name, 2)


def test_error_hierarchy() -> None:
    assert issubclass(UsageDbNotFoundError, UsageError)
    assert issubclass(UsageDbLockedError, UsageError)
    assert issubclass(UsageReadError, UsageError)
    assert issubclass(UsageError, RuntimeError)
