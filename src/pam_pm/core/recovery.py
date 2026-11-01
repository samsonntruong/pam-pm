"""Recovery: lifecycle, durable conversations, misfire policy, catch-up (WP3b).

SQLite is the source of truth across restarts. This module adds small
helpers over the durable tables created by pam_pm.core.store: kv,
lifecycle, conversations, scheduled_jobs, and scheduled_runs. It never
changes existing DDL, it only reads and writes rows.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pam_pm.core import store as store_mod

DbLike = str | Path | sqlite3.Connection

STOPPED_AT_KEY = "run_stopped_at"
OFFLINE_BANNER_KEY = "offline_banner_last"

DEFAULT_MISFIRE_GRACE_HOURS = 3.0


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


def _ensure(db: DbLike) -> None:
    with _connect(db) as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        conn.execute(store_mod.KV_TABLE_DDL)
        conn.execute(store_mod.LIFECYCLE_TABLE_DDL)
        conn.execute(store_mod.CONVERSATIONS_TABLE_DDL)
        conn.execute(store_mod.SCHEDULED_JOBS_TABLE_DDL)
        conn.execute(store_mod.SCHEDULED_RUNS_TABLE_DDL)
        conn.commit()


def now_iso() -> str:
    return store_mod.now_iso()


def _parse_when(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def kv_get(db: DbLike, key: str) -> str:
    _ensure(db)
    with _connect(db) as conn:
        row = conn.execute(
            "SELECT value FROM kv WHERE key = ?", (str(key),)
        ).fetchone()
    return str(row[0]) if row else ""


def kv_set(db: DbLike, key: str, value: str) -> None:
    _ensure(db)
    with _connect(db) as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(key), str(value)),
        )
        conn.commit()


def log_lifecycle(db: DbLike, event: str, detail: str = "") -> int:
    _ensure(db)
    with _connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO lifecycle (event, detail) VALUES (?, ?)",
            (str(event), str(detail)),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def list_lifecycle(db: DbLike, limit: int = 50) -> list[dict[str, Any]]:
    _ensure(db)
    with _connect(db) as conn:
        rows = conn.execute(
            "SELECT id, event, detail, created_at FROM lifecycle"
            " ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [
        {
            "id": int(row[0]),
            "event": str(row[1]),
            "detail": str(row[2] or ""),
            "created_at": str(row[3] or ""),
        }
        for row in rows
    ]


def note_startup(db: DbLike, now: str | None = None) -> dict[str, Any]:
    """Log a start, detecting a crash when the last run never stopped."""
    _ensure(db)
    moment = (now or "").strip() or now_iso()
    started_key = store_mod.STARTED_AT_KEY
    prev_started = kv_get(db, started_key)
    prev_stopped = kv_get(db, STOPPED_AT_KEY)
    crashed = bool(prev_started) and (
        not prev_stopped or prev_stopped < prev_started
    )
    if crashed:
        log_lifecycle(
            db, "crash", f"previous run started at {prev_started} never stopped"
        )
    log_lifecycle(db, "start", f"started at {moment}")
    kv_set(db, started_key, moment)
    return {
        "crashed": crashed,
        "offline_from": prev_stopped or prev_started,
        "offline_to": moment,
    }


def note_stop(db: DbLike, detail: str = "", now: str | None = None) -> None:
    """Log a clean stop so the next startup is not a crash."""
    _ensure(db)
    moment = (now or "").strip() or now_iso()
    log_lifecycle(db, "stop", detail or f"stopped at {moment}")
    kv_set(db, STOPPED_AT_KEY, moment)


def thread_key(person: str, item_id: str, kind: str = "interview") -> str:
    who = (person or "").strip() or "user"
    what = (item_id or "").strip() or "item"
    label = (kind or "").strip() or "interview"
    return f"{label}:{who}:{what}"


def save_conversation(
    db: DbLike,
    key: str,
    person: str,
    item_id: str,
    turns: list[dict[str, Any]],
    last_seen: str = "",
    status: str = "open",
) -> None:
    _ensure(db)
    body = json.dumps([dict(t) for t in (turns or [])])
    with _connect(db) as conn:
        conn.execute(
            "INSERT INTO conversations"
            " (thread_key, person, item_id, turns_json, last_seen, status,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))"
            " ON CONFLICT(thread_key) DO UPDATE SET person = excluded.person,"
            " item_id = excluded.item_id, turns_json = excluded.turns_json,"
            " last_seen = excluded.last_seen, status = excluded.status,"
            " updated_at = datetime('now')",
            (
                str(key),
                str(person or ""),
                str(item_id or ""),
                body,
                str(last_seen or ""),
                str(status or "open"),
            ),
        )
        conn.commit()


def load_conversation(db: DbLike, key: str) -> dict[str, Any] | None:
    _ensure(db)
    with _connect(db) as conn:
        row = conn.execute(
            "SELECT thread_key, person, item_id, turns_json, last_seen, status,"
            " updated_at FROM conversations WHERE thread_key = ?",
            (str(key),),
        ).fetchone()
    if row is None:
        return None
    try:
        turns = json.loads(row[3] or "[]")
    except ValueError:
        turns = []
    if not isinstance(turns, list):
        turns = []
    return {
        "thread_key": str(row[0]),
        "person": str(row[1] or ""),
        "item_id": str(row[2] or ""),
        "turns": turns,
        "last_seen": str(row[4] or ""),
        "status": str(row[5] or ""),
        "updated_at": str(row[6] or ""),
    }


def list_open_conversations(db: DbLike) -> list[dict[str, Any]]:
    _ensure(db)
    with _connect(db) as conn:
        rows = conn.execute(
            "SELECT thread_key, person, item_id, turns_json, last_seen, status,"
            " updated_at FROM conversations WHERE status = 'open'"
            " ORDER BY updated_at ASC"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            turns = json.loads(row[3] or "[]")
        except ValueError:
            turns = []
        if not isinstance(turns, list):
            turns = []
        out.append(
            {
                "thread_key": str(row[0]),
                "person": str(row[1] or ""),
                "item_id": str(row[2] or ""),
                "turns": turns,
                "last_seen": str(row[4] or ""),
                "status": str(row[5] or ""),
                "updated_at": str(row[6] or ""),
            }
        )
    return out


def append_turn(
    db: DbLike,
    key: str,
    person: str,
    item_id: str,
    role: str,
    content: str,
    last_seen: str = "",
) -> int:
    """Append one turn to a durable conversation, returning turn count."""
    current = load_conversation(db, key)
    turns = list(current.get("turns", [])) if current else []
    turns.append({"role": str(role), "content": str(content)})
    seen = last_seen or (current.get("last_seen", "") if current else "")
    status = current.get("status", "open") if current else "open"
    save_conversation(db, key, person, item_id, turns, seen, status)
    return len(turns)


def set_last_seen(db: DbLike, key: str, last_seen: str) -> None:
    current = load_conversation(db, key)
    if current is None:
        return
    save_conversation(
        db,
        key,
        current.get("person", ""),
        current.get("item_id", ""),
        current.get("turns", []),
        last_seen,
        current.get("status", "open"),
    )


def upsert_job(
    db: DbLike,
    job_id: str,
    project: str = "default",
    rule: str = "",
    spec: dict[str, Any] | None = None,
    misfire_grace_hours: float = DEFAULT_MISFIRE_GRACE_HOURS,
) -> None:
    _ensure(db)
    body = json.dumps(dict(spec or {}))
    try:
        grace = float(misfire_grace_hours)
    except (TypeError, ValueError):
        grace = DEFAULT_MISFIRE_GRACE_HOURS
    with _connect(db) as conn:
        conn.execute(
            "INSERT INTO scheduled_jobs"
            " (job_id, project, rule, spec_json, misfire_grace_hours,"
            " updated_at) VALUES (?, ?, ?, ?, ?, datetime('now'))"
            " ON CONFLICT(job_id) DO UPDATE SET project = excluded.project,"
            " rule = excluded.rule, spec_json = excluded.spec_json,"
            " misfire_grace_hours = excluded.misfire_grace_hours,"
            " updated_at = datetime('now')",
            (str(job_id), str(project or "default"), str(rule or ""), body, grace),
        )
        conn.commit()


def get_job(db: DbLike, job_id: str) -> dict[str, Any] | None:
    _ensure(db)
    with _connect(db) as conn:
        row = conn.execute(
            "SELECT job_id, project, rule, spec_json, misfire_grace_hours,"
            " last_run, updated_at FROM scheduled_jobs WHERE job_id = ?",
            (str(job_id),),
        ).fetchone()
    if row is None:
        return None
    try:
        spec = json.loads(row[3] or "{}")
    except ValueError:
        spec = {}
    if not isinstance(spec, dict):
        spec = {}
    return {
        "job_id": str(row[0]),
        "project": str(row[1] or ""),
        "rule": str(row[2] or ""),
        "spec": spec,
        "misfire_grace_hours": float(row[4] or DEFAULT_MISFIRE_GRACE_HOURS),
        "last_run": str(row[5] or ""),
        "updated_at": str(row[6] or ""),
    }


def list_jobs(db: DbLike) -> list[dict[str, Any]]:
    _ensure(db)
    with _connect(db) as conn:
        rows = conn.execute(
            "SELECT job_id, project, rule, spec_json, misfire_grace_hours,"
            " last_run, updated_at FROM scheduled_jobs ORDER BY job_id ASC"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            spec = json.loads(row[3] or "{}")
        except ValueError:
            spec = {}
        if not isinstance(spec, dict):
            spec = {}
        out.append(
            {
                "job_id": str(row[0]),
                "project": str(row[1] or ""),
                "rule": str(row[2] or ""),
                "spec": spec,
                "misfire_grace_hours": float(
                    row[4] or DEFAULT_MISFIRE_GRACE_HOURS
                ),
                "last_run": str(row[5] or ""),
                "updated_at": str(row[6] or ""),
            }
        )
    return out


def set_job_last_run(db: DbLike, job_id: str, when: str) -> None:
    _ensure(db)
    with _connect(db) as conn:
        conn.execute(
            "UPDATE scheduled_jobs SET last_run = ?, updated_at = datetime('now')"
            " WHERE job_id = ?",
            (str(when), str(job_id)),
        )
        conn.commit()


def record_run(
    db: DbLike, job_id: str, due_at: str, status: str, detail: str = ""
) -> int:
    _ensure(db)
    with _connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO scheduled_runs (job_id, due_at, status, detail)"
            " VALUES (?, ?, ?, ?)",
            (str(job_id), str(due_at), str(status), str(detail)),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def list_runs(db: DbLike, job_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    _ensure(db)
    with _connect(db) as conn:
        if job_id:
            rows = conn.execute(
                "SELECT id, job_id, due_at, status, detail, created_at"
                " FROM scheduled_runs WHERE job_id = ? ORDER BY id DESC LIMIT ?",
                (str(job_id), int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, job_id, due_at, status, detail, created_at"
                " FROM scheduled_runs ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    return [
        {
            "id": int(row[0]),
            "job_id": str(row[1]),
            "due_at": str(row[2] or ""),
            "status": str(row[3] or ""),
            "detail": str(row[4] or ""),
            "created_at": str(row[5] or ""),
        }
        for row in rows
    ]


def decide_misfire(due_at: str, now: str, grace_hours: float) -> str:
    """Return run when a missed due time is still within grace, else skip."""
    try:
        grace = float(grace_hours)
    except (TypeError, ValueError):
        grace = DEFAULT_MISFIRE_GRACE_HOURS
    due = _parse_when(due_at)
    moment = _parse_when(now)
    if due is None or moment is None:
        return "skip"
    late_hours = (moment - due).total_seconds() / 3600.0
    if late_hours < 0:
        return "skip"
    if late_hours <= grace:
        return "run"
    return "skip"


def _grace_for(db: DbLike, job_id: str, fallback: float) -> float:
    job = get_job(db, job_id)
    if job is None:
        return fallback
    try:
        return float(job.get("misfire_grace_hours", fallback))
    except (TypeError, ValueError):
        return fallback


def apply_misfires(
    db: DbLike,
    missed: list[dict[str, Any] | tuple[str, str]],
    now: str | None = None,
    runner: Callable[[str, str], None] | None = None,
    default_grace_hours: float = DEFAULT_MISFIRE_GRACE_HOURS,
) -> dict[str, Any]:
    """Apply the misfire policy to missed runs, at most one run per job.

    Each job runs once when its latest missed due time is still within
    its grace window, otherwise all its missed runs are skipped and
    logged. This never sends a burst of stale check-ins.
    """
    _ensure(db)
    moment = (now or "").strip() or now_iso()
    grouped: dict[str, list[str]] = {}
    for entry in missed or []:
        if isinstance(entry, dict):
            job_id = str(entry.get("job_id", "")).strip()
            due_at = str(entry.get("due_at", "")).strip()
        else:
            job_id, due_at = str(entry[0]).strip(), str(entry[1]).strip()
        if not job_id or not due_at:
            continue
        grouped.setdefault(job_id, []).append(due_at)
    ran: list[str] = []
    skipped = 0
    for job_id in sorted(grouped):
        dues = sorted(set(grouped[job_id]))
        grace = _grace_for(db, job_id, default_grace_hours)
        latest = dues[-1]
        if decide_misfire(latest, moment, grace) == "run":
            try:
                if runner is not None:
                    runner(job_id, latest)
                record_run(db, job_id, latest, "ran", "misfire catch-up within grace")
                set_job_last_run(db, job_id, moment)
                ran.append(job_id)
            except Exception as exc:  # noqa: BLE001
                record_run(db, job_id, latest, "failed", str(exc)[:300])
                skipped += 1
            for older in dues[:-1]:
                record_run(db, job_id, older, "coalesced", "merged into one catch-up")
                skipped += 1
        else:
            for due_at in dues:
                record_run(db, job_id, due_at, "skipped", "misfire grace passed")
                skipped += 1
    log_lifecycle(
        db, "misfire", f"ran {len(ran)}, skipped {skipped} at {moment}"
    )
    return {"ran": ran, "skipped": skipped}


def format_offline_banner(
    offline_from: str, offline_to: str, replies: int, board_changes: int, skipped: int
) -> str:
    start = (offline_from or "").strip() or "unknown"
    end = (offline_to or "").strip() or "unknown"
    return (
        f"Pam was offline from {start} to {end}."
        f" Caught up: {int(replies)} replies, {int(board_changes)} board changes."
        f" Skipped: {int(skipped)} check-ins."
    )


def set_offline_banner(db: DbLike, text: str) -> None:
    kv_set(db, OFFLINE_BANNER_KEY, str(text or ""))


def get_offline_banner(db: DbLike) -> str:
    return kv_get(db, OFFLINE_BANNER_KEY)


def clear_offline_banner(db: DbLike) -> None:
    kv_set(db, OFFLINE_BANNER_KEY, "")


def _msg_text(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        return str(msg.get("text", "") or "")
    return str(getattr(msg, "text", "") or "")


def _msg_marker(msg: Any) -> str:
    if isinstance(msg, dict):
        for key in ("ts", "id"):
            value = msg.get(key)
            if value:
                return str(value)
        return ""
    for key in ("ts", "id"):
        value = getattr(msg, key, "")
        if value:
            return str(value)
    return ""


def catch_up(
    db: DbLike,
    *,
    poller: Any | None = None,
    chat: Any | None = None,
    process_reply: Callable[[dict[str, Any], Any], None] | None = None,
    missed_runs: list[dict[str, Any] | tuple[str, str]] | None = None,
    now: str | None = None,
    runner: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Run startup catch-up and store the offline banner.

    The poller resumes from its stored watermark, open interview and
    blocker threads read history since the last seen message, and
    missed runs follow the misfire policy.
    """
    _ensure(db)
    moment = (now or "").strip() or now_iso()
    offline_from = kv_get(db, STOPPED_AT_KEY) or kv_get(db, store_mod.CATCHUP_KEY)
    board_changes = 0
    if poller is not None:
        try:
            fresh = list(poller.poll_once())
        except Exception:  # noqa: BLE001
            fresh = []
        board_changes = len(fresh)
    replies = 0
    if chat is not None:
        for conv in list_open_conversations(db):
            user_id = str(conv.get("person", "")).strip()
            if not user_id:
                continue
            since = str(conv.get("last_seen", "") or "")
            try:
                found = list(chat.read_replies(user_id, since or None))
            except TypeError:
                try:
                    found = list(chat.read_replies(user_id))
                except Exception:  # noqa: BLE001, S112
                    continue
            except Exception:  # noqa: BLE001, S112
                continue
            for msg in found:
                marker = _msg_marker(msg)
                if since and marker and marker <= since:
                    continue
                text = _msg_text(msg)
                if process_reply is not None:
                    try:
                        process_reply(conv, msg)
                    except Exception:  # noqa: BLE001, S110
                        pass
                else:
                    append_turn(
                        db,
                        str(conv.get("thread_key", "")),
                        user_id,
                        str(conv.get("item_id", "")),
                        "user",
                        text,
                        marker or since,
                    )
                if marker:
                    set_last_seen(db, str(conv.get("thread_key", "")), marker)
                    since = marker
                replies += 1
    misfire = apply_misfires(db, list(missed_runs or []), moment, runner)
    skipped = int(misfire.get("skipped", 0))
    kv_set(db, store_mod.CATCHUP_KEY, moment)
    banner = ""
    if offline_from and (replies or board_changes or skipped or missed_runs):
        banner = format_offline_banner(
            offline_from, moment, replies, board_changes, skipped
        )
        set_offline_banner(db, banner)
        log_lifecycle(db, "catchup", banner)
    return {
        "replies": replies,
        "board_changes": board_changes,
        "skipped": skipped,
        "ran": list(misfire.get("ran", [])),
        "offline_from": offline_from,
        "offline_to": moment,
        "banner": banner,
    }
