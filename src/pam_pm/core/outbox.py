"""Outbox for exactly-once Slack messages and board writes (WP3b).

Every outgoing Slack message and every board write goes through the
outbox with an idempotency key. Entries are marked sent only after
success, and board retries are checked against the write log before
any retry, so a restart never sends the same DM twice or applies the
same write twice.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pam_pm.core import store as store_mod

DbLike = str | Path | sqlite3.Connection

OUTBOX_TABLE_DDL = """CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  sent_at TEXT NOT NULL DEFAULT ''
)"""

KIND_SLACK = "slack"
KIND_BOARD = "board"

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"


@contextmanager
def _connect(db: DbLike) -> Iterator[sqlite3.Connection]:
    if isinstance(db, sqlite3.Connection):
        yield db
    else:
        if str(db) != ":memory:":
            Path(str(db)).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        try:
            yield conn
        finally:
            conn.close()


def ensure_outbox(db: DbLike) -> None:
    """Create base tables plus the outbox table (WAL mode for files)."""
    with _connect(db) as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        conn.execute(store_mod.WRITES_TABLE_DDL)
        conn.execute(store_mod.KV_TABLE_DDL)
        conn.execute(OUTBOX_TABLE_DDL)
        conn.commit()


def make_key(*parts: object) -> str:
    """Return a stable idempotency key from parts."""
    return ":".join(str(p) for p in parts)


def enqueue(
    db: DbLike, *, kind: str, key: str, payload: dict[str, Any]
) -> int:
    """Queue one outbox entry, returning its id.

    When the key already exists the original id is returned and the
    payload is left untouched, so double enqueue is a no-op.
    """
    want_kind = str(kind or "").strip() or KIND_SLACK
    want_key = str(key or "").strip()
    if not want_key:
        raise ValueError("idempotency key is required")
    body = json.dumps(dict(payload or {}))
    with _connect(db) as conn:
        ensure_outbox(conn)
        row = conn.execute(
            "SELECT id FROM outbox WHERE key = ?", (want_key,)
        ).fetchone()
        if row is not None:
            return int(row[0])
        try:
            cur = conn.execute(
                "INSERT INTO outbox (key, kind, payload_json) VALUES (?, ?, ?)",
                (want_key, want_kind, body),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM outbox WHERE key = ?", (want_key,)
            ).fetchone()
            if row is not None:
                return int(row[0])
            raise
        conn.commit()
        return int(cur.lastrowid or 0)


def get_by_key(db: DbLike, key: str) -> dict[str, Any] | None:
    """Return one outbox entry by key, or None when missing."""
    with _connect(db) as conn:
        ensure_outbox(conn)
        row = conn.execute(
            "SELECT id, key, kind, payload_json, status, attempts,"
            " last_error, created_at, sent_at FROM outbox WHERE key = ?",
            (str(key),),
        ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_pending(db: DbLike, limit: int | None = None) -> list[dict[str, Any]]:
    """List pending outbox entries ordered by id."""
    query = (
        "SELECT id, key, kind, payload_json, status, attempts,"
        " last_error, created_at, sent_at FROM outbox"
        " WHERE status = 'pending' ORDER BY id ASC"
    )
    params: list[object] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    with _connect(db) as conn:
        ensure_outbox(conn)
        rows = conn.execute(query, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: tuple) -> dict[str, Any]:
    try:
        payload = json.loads(row[3] or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "id": int(row[0]),
        "key": str(row[1]),
        "kind": str(row[2]),
        "payload": payload,
        "status": str(row[4]),
        "attempts": int(row[5]),
        "last_error": str(row[6] or ""),
        "created_at": str(row[7] or ""),
        "sent_at": str(row[8] or ""),
    }


def mark_sent(db: DbLike, key: str) -> bool:
    """Mark one entry sent, returning True when a row was updated."""
    with _connect(db) as conn:
        ensure_outbox(conn)
        cur = conn.execute(
            "UPDATE outbox SET status = 'sent',"
            " sent_at = datetime('now'), updated_at = datetime('now')"
            " WHERE key = ?",
            (str(key),),
        )
        conn.commit()
        return cur.rowcount > 0


def mark_failed(db: DbLike, key: str, error: str) -> bool:
    """Record a failed attempt, keeping the entry pending for retry."""
    with _connect(db) as conn:
        ensure_outbox(conn)
        cur = conn.execute(
            "UPDATE outbox SET attempts = attempts + 1,"
            " last_error = ?, updated_at = datetime('now')"
            " WHERE key = ? AND status = 'pending'",
            (str(error or "")[:500], str(key)),
        )
        conn.commit()
        return cur.rowcount > 0


def _write_log_has_key(db: DbLike, key: str) -> bool:
    """Return True when the write log already applied this key."""
    try:
        writes = store_mod.list_writes(db)
    except Exception:  # noqa: BLE001
        return False
    for record in writes:
        if record.status != "applied":
            continue
        detail = record.detail or ""
        if not key or not detail:
            continue
        try:
            parsed = json.loads(detail)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            if parsed.get("idempotency_key") == key:
                return True
        elif detail.strip() == key:
            return True
    return False


def _send_slack(entry: dict[str, Any], chat: Any) -> None:
    payload = dict(entry.get("payload", {}))
    user_id = str(payload.get("user_id", "")).strip()
    text = str(payload.get("text", ""))
    if not user_id:
        raise ValueError("slack payload needs user_id")
    chat.send_dm(user_id, text)


def _apply_board(db: DbLike, entry: dict[str, Any], board: Any) -> None:
    payload = dict(entry.get("payload", {}))
    operation = str(payload.get("operation", "update")).strip() or "update"
    item_id = str(payload.get("item_id", "")).strip()
    if not item_id and operation != "create":
        raise ValueError("board payload needs item_id")
    fields = payload.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
    fields = dict(fields)
    if operation == "comment":
        text = str(payload.get("text", fields.pop("text", "")) or "")
        author = str(payload.get("author", "") or "")
        board.add_comment(item_id, text, author=author)
    elif operation == "create":
        title = str(payload.get("title", fields.pop("title", "Untitled")))
        board.create_item(title, fields)
    else:
        board.update_item(item_id, fields)
    key = str(entry.get("key", ""))
    detail = json.dumps({"idempotency_key": key, "operation": operation})
    try:
        store_mod.insert_write(
            db,
            {
                "operation": operation,
                "item_id": item_id,
                "evidence_quote": str(payload.get("evidence_quote", "")),
                "status": "applied",
                "detail": detail,
            },
        )
    except Exception:  # noqa: BLE001, S110
        pass


def flush(
    db: DbLike,
    *,
    chat: Any | None = None,
    board: Any | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    """Send pending entries exactly once, returning counts.

    Each entry is checked before sending: sent entries are skipped,
    and board entries already present in the write log are marked
    sent without reapplying. Entries are marked sent only after the
    underlying send succeeds.
    """
    pending = list_pending(db, limit=limit)
    sent = 0
    skipped = 0
    failed = 0
    for entry in pending:
        key = str(entry.get("key", ""))
        kind = str(entry.get("kind", ""))
        current = get_by_key(db, key)
        if current is None or current.get("status") == STATUS_SENT:
            skipped += 1
            continue
        try:
            if kind == KIND_BOARD:
                if board is None:
                    raise ValueError("board adapter is required for board entries")
                if _write_log_has_key(db, key):
                    mark_sent(db, key)
                    skipped += 1
                    continue
                _apply_board(db, entry, board)
            else:
                if chat is None:
                    raise ValueError("chat adapter is required for slack entries")
                _send_slack(entry, chat)
            mark_sent(db, key)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            mark_failed(db, key, str(exc))
            failed += 1
    return {"sent": sent, "skipped": skipped, "failed": failed}
