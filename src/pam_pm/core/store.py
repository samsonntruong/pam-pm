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

LISTEN_SESSIONS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS listen_sessions (
  session_id TEXT PRIMARY KEY,
  project TEXT NOT NULL DEFAULT 'default',
  backend TEXT NOT NULL DEFAULT 'azure_devops',
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

LISTEN_SEGMENTS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS listen_segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT 'default',
  idx INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL DEFAULT '',
  speaker TEXT NOT NULL DEFAULT '',
  start_ms INTEGER NOT NULL DEFAULT 0,
  end_ms INTEGER NOT NULL DEFAULT 0,
  turn_id TEXT NOT NULL DEFAULT '',
  is_final INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

LISTEN_NOTES_TABLE_DDL = """CREATE TABLE IF NOT EXISTS listen_notes (
  session_id TEXT PRIMARY KEY,
  project TEXT NOT NULL DEFAULT 'default',
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'proposed',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

LISTEN_KEEP_AUDIO_KEY = "listen.keep_audio"

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
    conn.execute(LISTEN_SESSIONS_TABLE_DDL)
    conn.execute(LISTEN_SEGMENTS_TABLE_DDL)
    conn.execute(LISTEN_NOTES_TABLE_DDL)
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


def create_listen_session(
    db: DbLike,
    session_id: str,
    project: str = "default",
    title: str = "",
    status: str = "open",
) -> dict[str, str]:
    """Create a listen session row keyed by project, returning its fields."""
    want_project = (project or "default").strip() or "default"
    want_status = (status or "open").strip() or "open"
    with _connect(db) as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT backend FROM projects WHERE name = ?", (want_project,)
        ).fetchone()
        backend = _norm_backend(row[0]) if row else "azure_devops"
        conn.execute(
            "INSERT INTO listen_sessions"
            " (session_id, project, backend, title, status,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))"
            " ON CONFLICT(session_id) DO UPDATE SET"
            " title = excluded.title, status = excluded.status,"
            " updated_at = datetime('now')",
            (
                str(session_id),
                want_project,
                backend,
                str(title or ""),
                want_status,
            ),
        )
        conn.commit()
    return {
        "session_id": str(session_id),
        "project": want_project,
        "backend": backend,
        "title": str(title or ""),
        "status": want_status,
    }


def get_listen_session(db: DbLike, session_id: str) -> dict[str, str] | None:
    """Fetch one listen session by id, or None when missing."""
    with _connect(db) as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT session_id, project, backend, title, status"
            " FROM listen_sessions WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "session_id": str(row[0]),
        "project": str(row[1]),
        "backend": _norm_backend(row[2]),
        "title": str(row[3] or ""),
        "status": str(row[4] or ""),
    }


def set_listen_session_status(
    db: DbLike, session_id: str, status: str
) -> bool:
    """Set a listen session status, returning True when a row changed."""
    with _connect(db) as conn:
        _ensure_tables(conn)
        cur = conn.execute(
            "UPDATE listen_sessions SET status = ?, updated_at = datetime('now')"
            " WHERE session_id = ?",
            (str(status), str(session_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def insert_listen_segment(
    db: DbLike,
    session_id: str,
    project: str,
    text: str,
    *,
    speaker: str = "",
    start_ms: int = 0,
    end_ms: int = 0,
    turn_id: str = "",
    idx: int | None = None,
) -> int:
    """Persist one final transcript segment, returning its row ID."""
    want_project = (project or "default").strip() or "default"
    with _connect(db) as conn:
        _ensure_tables(conn)
        if idx is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(idx), -1) FROM listen_segments"
                " WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
            idx = int(row[0]) + 1 if row else 0
        cur = conn.execute(
            "INSERT INTO listen_segments"
            " (session_id, project, idx, text, speaker,"
            " start_ms, end_ms, turn_id, is_final)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                str(session_id),
                want_project,
                int(idx),
                str(text or ""),
                str(speaker or ""),
                int(start_ms or 0),
                int(end_ms or 0),
                str(turn_id or ""),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def list_listen_segments(
    db: DbLike, session_id: str
) -> list[dict[str, object]]:
    """List final segments for a session ordered by idx then id."""
    with _connect(db) as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT id, session_id, project, idx, text, speaker,"
            " start_ms, end_ms, turn_id FROM listen_segments"
            " WHERE session_id = ? ORDER BY idx ASC, id ASC",
            (str(session_id),),
        ).fetchall()
    return [
        {
            "id": int(row[0]),
            "session_id": str(row[1]),
            "project": str(row[2]),
            "idx": int(row[3]),
            "text": str(row[4] or ""),
            "speaker": str(row[5] or ""),
            "start_ms": int(row[6] or 0),
            "end_ms": int(row[7] or 0),
            "turn_id": str(row[8] or ""),
        }
        for row in rows
    ]


def get_listen_transcript(db: DbLike, session_id: str) -> str:
    """Join saved final segments so a session can finish after restart."""
    parts = [
        str(seg["text"]).strip()
        for seg in list_listen_segments(db, session_id)
    ]
    return " ".join(part for part in parts if part)


def save_listen_notes(
    db: DbLike,
    session_id: str,
    project: str,
    payload: dict,
    status: str = "proposed",
) -> None:
    """Store notes for a listen session, replacing any prior draft."""
    want_project = (project or "default").strip() or "default"
    want_status = (status or "proposed").strip() or "proposed"
    body = dict(payload or {})
    with _connect(db) as conn:
        _ensure_tables(conn)
        conn.execute(
            "INSERT INTO listen_notes"
            " (session_id, project, payload_json, status,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))"
            " ON CONFLICT(session_id) DO UPDATE SET"
            " project = excluded.project,"
            " payload_json = excluded.payload_json,"
            " status = excluded.status, updated_at = datetime('now')",
            (str(session_id), want_project, json.dumps(body), want_status),
        )
        conn.commit()


def get_listen_notes(db: DbLike, session_id: str) -> dict | None:
    """Fetch one notes draft by session id, or None when missing."""
    with _connect(db) as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT session_id, project, payload_json, status"
            " FROM listen_notes WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[2] or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "session_id": str(row[0]),
        "project": str(row[1] or "default"),
        "payload": payload,
        "status": str(row[3] or "proposed"),
    }


def set_listen_notes_status(db: DbLike, session_id: str, status: str) -> bool:
    """Set a notes draft status, returning True when a row changed."""
    with _connect(db) as conn:
        _ensure_tables(conn)
        cur = conn.execute(
            "UPDATE listen_notes SET status = ?, updated_at = datetime('now')"
            " WHERE session_id = ?",
            (str(status), str(session_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def get_meeting_detail(db: DbLike, session_id: str) -> dict | None:
    """Return session plus segments plus notes for one meeting."""
    session = get_listen_session(db, str(session_id))
    if session is None:
        return None
    segments = list_listen_segments(db, str(session_id))
    notes = get_listen_notes(db, str(session_id))
    transcript = " ".join(
        str(seg.get("text", "")).strip() for seg in segments
    ).strip()
    transcript = " ".join(transcript.split())
    return {
        "session": session,
        "segments": segments,
        "notes": notes,
        "transcript": transcript,
        "segment_count": len(segments),
    }


def delete_meeting(db: DbLike, session_id: str) -> bool:
    """Delete a meeting and its segments plus notes, True when found."""
    want = str(session_id)
    with _connect(db) as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT session_id FROM listen_sessions WHERE session_id = ?",
            (want,),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "DELETE FROM listen_segments WHERE session_id = ?", (want,)
        )
        conn.execute(
            "DELETE FROM listen_notes WHERE session_id = ?", (want,)
        )
        conn.execute(
            "DELETE FROM listen_sessions WHERE session_id = ?", (want,)
        )
        conn.commit()
        return True


def delete_listen_session(db: DbLike, session_id: str) -> bool:
    """Alias for delete_meeting kept for transport symmetry."""
    return delete_meeting(db, session_id)


def get_listen_keep_audio(db: DbLike) -> bool:
    """Return the Keep audio setting, default off."""
    with _connect(db) as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT value FROM kv WHERE key = ?", (LISTEN_KEEP_AUDIO_KEY,)
        ).fetchone()
    if row is None:
        return False
    return str(row[0] or "").strip().lower() in ("1", "true", "on", "yes")


def set_listen_keep_audio(db: DbLike, keep: bool) -> bool:
    """Store the Keep audio setting, returning the stored value."""
    want = bool(keep)
    with _connect(db) as conn:
        _ensure_tables(conn)
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (LISTEN_KEEP_AUDIO_KEY, "on" if want else "off"),
        )
        conn.commit()
    return want


def list_meetings(
    db: DbLike, project: str | None = None
) -> list[dict[str, str]]:
    """List meetings as sessions with segment counts, newest first."""
    sessions = list_listen_sessions(db, project)
    if not sessions:
        return []
    with _connect(db) as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT session_id, COUNT(*) FROM listen_segments GROUP BY session_id"
        ).fetchall()
    counts = {str(row[0]): int(row[1] or 0) for row in rows}
    out: list[dict[str, str]] = []
    for entry in sessions:
        item = dict(entry)
        item["segment_count"] = str(counts.get(str(entry["session_id"]), 0))
        out.append(item)
    return out


def list_listen_sessions(
    db: DbLike, project: str | None = None
) -> list[dict[str, str]]:
    """List listen sessions, optionally for one project, newest first."""
    with _connect(db) as conn:
        _ensure_tables(conn)
        if project is None:
            rows = conn.execute(
                "SELECT session_id, project, backend, title, status"
                " FROM listen_sessions ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT session_id, project, backend, title, status"
                " FROM listen_sessions WHERE project = ?"
                " ORDER BY created_at DESC",
                (str(project),),
            ).fetchall()
    return [
        {
            "session_id": str(row[0]),
            "project": str(row[1]),
            "backend": _norm_backend(row[2]),
            "title": str(row[3] or ""),
            "status": str(row[4] or ""),
        }
        for row in rows
    ]
