"""SQLite state store: writes log, calls log, and interviews (F5, F9)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pam_pm.core.cost import CALLS_TABLE_DDL, CallUsage, log_call
from pam_pm.core.schemas import WriteRecord

DbLike = str | Path | sqlite3.Connection

WRITES_TABLE_DDL = """CREATE TABLE IF NOT EXISTS writes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation TEXT NOT NULL,
  item_id TEXT NOT NULL,
  evidence_quote TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'proposed',
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

INTERVIEWS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS interviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person TEXT NOT NULL DEFAULT '',
  item_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""


@contextmanager
def _connect(db: DbLike) -> Iterator[sqlite3.Connection]:
    """Yield a connection without closing one owned by the caller."""
    if isinstance(db, sqlite3.Connection):
        yield db
    else:
        conn = sqlite3.connect(str(db))
        try:
            yield conn
        finally:
            conn.close()


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(WRITES_TABLE_DDL)
    conn.execute(CALLS_TABLE_DDL)
    conn.execute(INTERVIEWS_TABLE_DDL)
    conn.commit()


def init_db(path: str | Path) -> Path:
    """Create the database and all tables, returning the path."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        _ensure_tables(conn)
    return db_path


def insert_write(db: DbLike, record: WriteRecord | dict) -> int:
    """Log a write record, returning its row ID."""
    item = WriteRecord.model_validate(record)
    with _connect(db) as conn:
        _ensure_tables(conn)
        cur = conn.execute(
            "INSERT INTO writes (operation, item_id, evidence_quote, status, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (item.operation, item.item_id, item.evidence_quote, item.status, item.detail),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def list_writes(
    db: DbLike, *, status: str | None = None, limit: int | None = None
) -> list[WriteRecord]:
    """List logged writes ordered by ID, optionally filtered by status."""
    query = (
        "SELECT id, operation, item_id, evidence_quote, status, detail"
        " FROM writes"
    )
    params: list[object] = []
    if status is not None:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    with _connect(db) as conn:
        _ensure_tables(conn)
        rows = conn.execute(query, params).fetchall()
    return [
        WriteRecord(
            id=row[0],
            operation=row[1],
            item_id=row[2],
            evidence_quote=row[3],
            status=row[4],
            detail=row[5],
        )
        for row in rows
    ]


def set_write_status(db: DbLike, write_id: int, status: str) -> bool:
    """Set a write status, returning True when a row was updated."""
    with _connect(db) as conn:
        _ensure_tables(conn)
        cur = conn.execute(
            "UPDATE writes SET status = ? WHERE id = ?", (status, write_id)
        )
        conn.commit()
        return cur.rowcount > 0


def undo_write(db: DbLike, write_id: int) -> bool:
    """Mark a write as undone, returning True when a row was updated."""
    return set_write_status(db, write_id, "undone")


def insert_call(db: DbLike, usage: CallUsage | dict) -> int:
    """Log one model call cost row, returning its row ID."""
    item = CallUsage(**usage) if isinstance(usage, dict) else usage
    with _connect(db) as conn:
        _ensure_tables(conn)
        return log_call(conn, item)
