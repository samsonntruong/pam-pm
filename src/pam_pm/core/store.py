"""SQLite state store: writes log, calls log, interviews, and recovery (F5, F9)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
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

PROJECTS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS projects (
  name TEXT PRIMARY KEY,
  backend TEXT NOT NULL DEFAULT 'azure_devops'
)"""

CONVERSATIONS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS conversations (
  thread_key TEXT PRIMARY KEY,
  person TEXT NOT NULL DEFAULT '',
  item_id TEXT NOT NULL DEFAULT '',
  turns_json TEXT NOT NULL DEFAULT '[]',
  last_seen TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

PROPOSALS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL DEFAULT 'default',
  kind TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

SCHEDULED_JOBS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS scheduled_jobs (
  job_id TEXT PRIMARY KEY,
  project TEXT NOT NULL DEFAULT 'default',
  rule TEXT NOT NULL DEFAULT '',
  spec_json TEXT NOT NULL DEFAULT '{}',
  misfire_grace_hours REAL NOT NULL DEFAULT 3.0,
  last_run TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

SCHEDULED_RUNS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS scheduled_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  due_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

LIFECYCLE_TABLE_DDL = """CREATE TABLE IF NOT EXISTS lifecycle (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

KV_TABLE_DDL = """CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
)"""

KICKOFF_TABLE_DDL = """CREATE TABLE IF NOT EXISTS kickoff_proposals (
  backend TEXT NOT NULL,
  external_id TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT 'default',
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (backend, external_id)
)"""

RAID_TABLE_DDL = """CREATE TABLE IF NOT EXISTS raid_items (
  backend TEXT NOT NULL,
  external_id TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT 'default',
  kind TEXT NOT NULL DEFAULT 'risk',
  title TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL DEFAULT 'medium',
  status TEXT NOT NULL DEFAULT 'proposed',
  due_date TEXT NOT NULL DEFAULT '',
  linked_ids TEXT NOT NULL DEFAULT '[]',
  evidence_quote TEXT NOT NULL DEFAULT '',
  evidence_date TEXT NOT NULL DEFAULT '',
  write_id INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (backend, external_id)
)"""

CATCHUP_KEY = "catchup_last"
STARTED_AT_KEY = "run_started_at"


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


def _migrate_projects(conn: sqlite3.Connection) -> None:
    """Add the backend column to old projects tables and backfill it."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()]
    if "backend" not in cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN backend"
            " TEXT NOT NULL DEFAULT 'azure_devops'"
        )
    conn.execute(
        "UPDATE projects SET backend = 'azure_devops'"
        " WHERE backend IS NULL OR backend = ''"
    )
    conn.commit()


def _ensure_tables(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    conn.execute(WRITES_TABLE_DDL)
    conn.execute(CALLS_TABLE_DDL)
    conn.execute(INTERVIEWS_TABLE_DDL)
    conn.execute(PROJECTS_TABLE_DDL)
    conn.execute(CONVERSATIONS_TABLE_DDL)
    conn.execute(PROPOSALS_TABLE_DDL)
    conn.execute(SCHEDULED_JOBS_TABLE_DDL)
    conn.execute(SCHEDULED_RUNS_TABLE_DDL)
    conn.execute(LIFECYCLE_TABLE_DDL)
    conn.execute(KV_TABLE_DDL)
    conn.execute(KICKOFF_TABLE_DDL)
    conn.execute(RAID_TABLE_DDL)
    _migrate_projects(conn)
    conn.commit()


def now_iso() -> str:
    """Return the current UTC time as an ISO string with Z."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def journal_mode(db: DbLike) -> str:
    """Return the SQLite journal mode for a database."""
    with _connect(db) as conn:
        _ensure_tables(conn)
        row = conn.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).lower() if row else ""


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


def _norm_backend(value: object) -> str:
    from pam_pm.core.config import normalize_backend

    return normalize_backend(value)


def list_projects(db: DbLike) -> list[dict[str, str]]:
    """List projects with canonical backends, migrating old rows."""
    with _connect(db) as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT name, backend FROM projects ORDER BY name ASC"
        ).fetchall()
    return [
        {"name": str(name), "backend": _norm_backend(backend)}
        for name, backend in rows
    ]


def get_project_backend(db: DbLike, name: str) -> str:
    """Return a project's backend, defaulting to azure_devops."""
    want = (name or "").strip()
    with _connect(db) as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT backend FROM projects WHERE name = ?", (want,)
        ).fetchone()
    if row is None:
        return "azure_devops"
    return _norm_backend(row[0])


def set_project_backend(db: DbLike, name: str, backend: str) -> str:
    """Store a project's backend choice, returning the canonical value."""
    want = (name or "").strip() or "default"
    canonical = _norm_backend(backend)
    with _connect(db) as conn:
        _ensure_tables(conn)
        conn.execute(
            "INSERT INTO projects (name, backend) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET backend = excluded.backend",
            (want, canonical),
        )
        conn.commit()
    return canonical


def save_kickoff_proposal(
    db: DbLike,
    backend: str,
    external_id: str,
    project: str,
    payload: dict,
    status: str = "pending",
) -> None:
    """Store a kickoff proposal keyed by (backend, external_id)."""
    from pam_pm.core.config import normalize_backend

    with _connect(db) as conn:
        _ensure_tables(conn)
        conn.execute(
            "INSERT INTO kickoff_proposals"
            " (backend, external_id, project, payload_json, status,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))"
            " ON CONFLICT(backend, external_id) DO UPDATE SET"
            " project = excluded.project, payload_json = excluded.payload_json,"
            " status = excluded.status, updated_at = datetime('now')",
            (
                normalize_backend(backend),
                str(external_id),
                str(project or "default"),
                json.dumps(payload),
                str(status or "pending"),
            ),
        )
        conn.commit()


def get_kickoff_proposal(
    db: DbLike, backend: str, external_id: str
) -> dict | None:
    """Fetch one kickoff proposal by (backend, external_id)."""
    from pam_pm.core.config import normalize_backend

    with _connect(db) as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT project, payload_json, status FROM kickoff_proposals"
            " WHERE backend = ? AND external_id = ?",
            (normalize_backend(backend), str(external_id)),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[1])
    except ValueError:
        payload = {}
    return {"project": row[0], "payload": payload, "status": row[2]}
