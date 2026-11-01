"""Agent memory: team roles, skillsets, capacity, and project lifecycle.

Backed by the existing SQLite store. Table ``people`` holds one row per
teammate (name, role, skillsets JSON, timezone, weekly capacity hours,
notes, source). Table ``memory_lifecycle`` holds one row per project
(completed estimate vs actual samples, throughput per week, blocker
recurrence counts, and a style note such as the team planning rhythm).

The name ``memory_lifecycle`` is used because ``lifecycle`` is already
taken by the run event log in :mod:`pam_pm.core.store`.

Capture flows (kickoff, interview) save to memory automatically with a
source noted. Later edits update only the fields that carry new info
and log each change to the run event log, so nothing is silently
overwritten.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

DbLike = str | Path | sqlite3.Connection

PEOPLE_TABLE_DDL = """CREATE TABLE IF NOT EXISTS people (
  name TEXT PRIMARY KEY,
  role TEXT NOT NULL DEFAULT '',
  skillsets TEXT NOT NULL DEFAULT '[]',
  timezone TEXT NOT NULL DEFAULT '',
  weekly_capacity REAL NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

MEMORY_LIFECYCLE_TABLE_DDL = """CREATE TABLE IF NOT EXISTS memory_lifecycle (
  project TEXT PRIMARY KEY,
  estimates_json TEXT NOT NULL DEFAULT '[]',
  throughput_json TEXT NOT NULL DEFAULT '{}',
  blockers_json TEXT NOT NULL DEFAULT '{}',
  style_note TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

MAX_SAMPLES = 200

_ROLE_WORDS = (
    "engineer",
    "developer",
    "designer",
    "tester",
    "manager",
    "lead",
    "devops",
    "analyst",
    "researcher",
    "writer",
    "architect",
    "support",
)

_SKILL_WORDS = (
    "backend",
    "frontend",
    "api",
    "design",
    "testing",
    "qa",
    "devops",
    "data",
    "ml",
    "ai",
    "docs",
    "mobile",
    "web",
    "database",
    "sql",
    "security",
    "infra",
    "cloud",
    "ux",
    "ui",
)

_ROLE_RE = re.compile(
    r"(?:i am a(?:n)?|i work as a(?:n)?|role\s*[:\-]?)\s*"
    r"([a-z][a-z .+/-]{2,40}?(?:engineer|developer|designer|tester|"
    r"manager|lead|devops|analyst|researcher|writer|architect|support))"
    r"(?:\s|[.,;]|$)",
    re.IGNORECASE,
)
_SKILLS_PREFIX_RE = re.compile(
    r"skills?\s*[:\-]\s*([a-z0-9][a-z0-9 ,/+._-]{1,120})", re.IGNORECASE
)
_CAPACITY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?\s*(?:per|a|/)?\s*week", re.IGNORECASE
)
_TZ_OFFSET_RE = re.compile(r"\b(?:utc|gmt)\s*([+-]\s*\d{1,2}(?::?\d{2})?)\b", re.IGNORECASE)
_TZ_NAME_RE = re.compile(
    r"\b([A-Z][a-z]+/[A-Za-z_]+(?:/[A-Za-z_]+)?)\b"
)


def _conn(db: DbLike) -> sqlite3.Connection:
    if isinstance(db, sqlite3.Connection):
        return db
    conn = sqlite3.connect(str(db))
    return conn


def ensure_memory_tables(db: DbLike) -> None:
    """Create the memory tables when missing."""
    own = not isinstance(db, sqlite3.Connection)
    conn = _conn(db)
    try:
        conn.execute(PEOPLE_TABLE_DDL)
        conn.execute(MEMORY_LIFECYCLE_TABLE_DDL)
        conn.commit()
    finally:
        if own:
            conn.close()


def _fetch_people(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT name, role, skillsets, timezone, weekly_capacity,"
        " notes, source FROM people ORDER BY name ASC"
    ).fetchall()
    return [_person_row(row) for row in rows]


def _person_row(row: Any) -> dict[str, Any]:
    try:
        skills = json.loads(row[2] or "[]")
    except ValueError:
        skills = []
    if not isinstance(skills, list):
        skills = []
    try:
        capacity = float(row[4] or 0)
    except (TypeError, ValueError):
        capacity = 0.0
    return {
        "name": str(row[0]),
        "role": str(row[1] or ""),
        "skillsets": [str(entry) for entry in skills],
        "timezone": str(row[3] or ""),
        "weekly_capacity": max(0.0, capacity),
        "notes": str(row[5] or ""),
        "source": str(row[6] or ""),
    }


def _log_change(conn: sqlite3.Connection, detail: str) -> None:
    try:
        conn.execute(
            "INSERT INTO lifecycle (event, detail) VALUES (?, ?)",
            ("memory_update", detail[:500]),
        )
    except sqlite3.Error:
        pass


def _ensure_store_tables(conn: sqlite3.Connection) -> None:
    from pam_pm.core import store as store_mod

    store_mod._ensure_tables(conn)


def list_people(db: DbLike) -> list[dict[str, Any]]:
    """List remembered teammates ordered by name."""
    own = not isinstance(db, sqlite3.Connection)
    conn = _conn(db)
    try:
        _ensure_store_tables(conn)
        ensure_memory_tables(conn)
        return _fetch_people(conn)
    finally:
        if own:
            conn.close()


def get_person(db: DbLike, name: str) -> dict[str, Any] | None:
    """Fetch one remembered teammate by name, or None when missing."""
    want = (name or "").strip()
    if not want:
        return None
    own = not isinstance(db, sqlite3.Connection)
    conn = _conn(db)
    try:
        _ensure_store_tables(conn)
        ensure_memory_tables(conn)
        row = conn.execute(
            "SELECT name, role, skillsets, timezone, weekly_capacity,"
            " notes, source FROM people WHERE name = ?",
            (want,),
        ).fetchone()
    finally:
        if own:
            conn.close()
    return _person_row(row) if row else None


def _norm_skills(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.replace(",", " ").replace(";", " ").replace("/", " ")
        items = [part.strip().lower() for part in raw.split()]
    elif isinstance(value, (list, tuple)):
        items = [str(entry).strip().lower() for entry in value]
    else:
        return None
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def upsert_person(
    db: DbLike,
    name: str,
    *,
    role: str | None = None,
    skillsets: Any = None,
    timezone: str | None = None,
    weekly_capacity: float | None = None,
    notes: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Insert or update one teammate, touching only fields with new info.

    Empty or None values never overwrite stored values. Each changed
    field is logged to the run event log with the source noted.
    """
    want = (name or "").strip()
    if not want:
        raise ValueError("name is required")
    own = not isinstance(db, sqlite3.Connection)
    conn = _conn(db)
    try:
        _ensure_store_tables(conn)
        ensure_memory_tables(conn)
        row = conn.execute(
            "SELECT name, role, skillsets, timezone, weekly_capacity,"
            " notes, source FROM people WHERE name = ?",
            (want,),
        ).fetchone()
        current = _person_row(row) if row else None
        skills = _norm_skills(skillsets)
        if current is None:
            conn.execute(
                "INSERT INTO people (name, role, skillsets, timezone,"
                " weekly_capacity, notes, source, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                (
                    want,
                    (role or "").strip(),
                    json.dumps(skills or []),
                    (timezone or "").strip(),
                    max(0.0, float(weekly_capacity or 0)),
                    (notes or "").strip(),
                    (source or "").strip(),
                ),
            )
            _log_change(conn, f"{want}: added from {source or 'unknown'}.")
            conn.commit()
            fresh = conn.execute(
                "SELECT name, role, skillsets, timezone, weekly_capacity,"
                " notes, source FROM people WHERE name = ?",
                (want,),
            ).fetchone()
            return _person_row(fresh)
        updates: dict[str, Any] = {}
        changed: list[str] = []
        if role is not None and role.strip() and role.strip() != current["role"]:
            updates["role"] = role.strip()
            changed.append("role")
        if skills and skills != current["skillsets"]:
            merged = list(current["skillsets"])
            for item in skills:
                if item not in merged:
                    merged.append(item)
            updates["skillsets"] = json.dumps(merged)
            changed.append("skillsets")
        if (
            timezone is not None
            and timezone.strip()
            and timezone.strip() != current["timezone"]
        ):
            updates["timezone"] = timezone.strip()
            changed.append("timezone")
        if weekly_capacity is not None:
            try:
                hours = max(0.0, float(weekly_capacity))
            except (TypeError, ValueError):
                hours = 0.0
            if hours > 0 and hours != current["weekly_capacity"]:
                updates["weekly_capacity"] = hours
                changed.append("weekly_capacity")
        if notes is not None and notes.strip() and notes.strip() != current["notes"]:
            updates["notes"] = notes.strip()
            changed.append("notes")
        if (
            source is not None
            and source.strip()
            and source.strip() != current["source"]
        ):
            updates["source"] = source.strip()
        if updates:
            sets = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE people SET {sets},"
                " updated_at = datetime('now') WHERE name = ?",
                (*updates.values(), want),
            )
            if changed:
                _log_change(
                    conn,
                    f"{want}: updated {', '.join(changed)}"
                    f" from {source or current['source'] or 'unknown'}.",
                )
            conn.commit()
        fresh = conn.execute(
            "SELECT name, role, skillsets, timezone, weekly_capacity,"
            " notes, source FROM people WHERE name = ?",
            (want,),
        ).fetchone()
        return _person_row(fresh)
    finally:
        if own:
            conn.close()


def delete_person(db: DbLike, name: str) -> bool:
    """Delete one teammate, returning True when a row was removed."""
    want = (name or "").strip()
    if not want:
        return False
    own = not isinstance(db, sqlite3.Connection)
    conn = _conn(db)
    try:
        _ensure_store_tables(conn)
        ensure_memory_tables(conn)
        cur = conn.execute("DELETE FROM people WHERE name = ?", (want,))
        if cur.rowcount > 0:
            _log_change(conn, f"{want}: removed from memory.")
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def clear_people(db: DbLike) -> int:
    """Delete all teammates, returning the removed row count."""
    own = not isinstance(db, sqlite3.Connection)
    conn = _conn(db)
    try:
        _ensure_store_tables(conn)
        ensure_memory_tables(conn)
        cur = conn.execute("DELETE FROM people")
        _log_change(conn, f"cleared {cur.rowcount} teammates from memory.")
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        if own:
            conn.close()


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def extract_profile_from_text(text: str) -> dict[str, Any]:
    """Extract role, skillsets, capacity, and timezone from free text.

    Returns only the fields that were found, so callers can pass the
    result straight into :func:`upsert_person` without overwriting
    stored values with blanks.
    """
    source = str(text or "")
    found: dict[str, Any] = {}
    role_match = _ROLE_RE.search(source)
    if role_match:
        role = re.sub(r"\s+", " ", role_match.group(1)).strip().title()
        if role:
            found["role"] = role
    skills: list[str] = []
    prefix = _SKILLS_PREFIX_RE.search(source)
    if prefix:
        for part in re.split(r"[,/]", prefix.group(1)):
            word = part.strip().lower()
            if word and word not in skills:
                skills.append(word)
    toks = _tokens(source)
    for word in _SKILL_WORDS:
        if word in toks and word not in skills:
            skills.append(word)
    if "qa" in toks and "qa" not in skills:
        skills.append("qa")
    if skills:
        found["skillsets"] = skills
    cap = _CAPACITY_RE.search(source)
    if cap:
        try:
            found["weekly_capacity"] = max(0.0, float(cap.group(1)))
        except ValueError:
            pass
    offset = _TZ_OFFSET_RE.search(source)
    if offset:
        zone = "UTC" + re.sub(r"\s+", "", offset.group(1))
        found["timezone"] = zone
    else:
        named = _TZ_NAME_RE.search(source)
        if named:
            found["timezone"] = named.group(1)
    return found


def capture_roster(
    db: DbLike, roster: Any, source: str = "kickoff"
) -> list[dict[str, Any]]:
    """Save roster entries (name, skills, capacity) into memory."""
    saved: list[dict[str, Any]] = []
    for entry in roster or []:
        if isinstance(entry, str):
            label = entry.strip()
            if label:
                saved.append(upsert_person(db, label, source=source))
            continue
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("name", "")).strip()
        if not label:
            continue
        try:
            hours = float(str(entry.get("capacity", 0) or 0))
        except (TypeError, ValueError):
            hours = 0.0
        saved.append(
            upsert_person(
                db,
                label,
                skillsets=str(entry.get("skills", "") or "") or None,
                weekly_capacity=hours if hours > 0 else None,
                source=source,
            )
        )
    return saved


def capture_interview(
    db: DbLike, person: str, answers: Any, source: str = "interview"
) -> dict[str, Any] | None:
    """Extract a profile from interview answers and save it to memory.

    Returns the saved person, or None when no name was given. When the
    answers carry no profile info, the person row is still created so
    later edits have a place to land.
    """
    label = str(person or "").strip()
    if not label:
        return None
    if isinstance(answers, str):
        text = answers
    elif isinstance(answers, (list, tuple)):
        text = "\n".join(str(entry) for entry in answers)
    else:
        text = str(answers or "")
    profile = extract_profile_from_text(text)
    return upsert_person(db, label, source=source, **profile)


def _lifecycle_row(row: Any) -> dict[str, Any]:
    try:
        estimates = json.loads(row[1] or "[]")
    except ValueError:
        estimates = []
    try:
        throughput = json.loads(row[2] or "{}")
    except ValueError:
        throughput = {}
    try:
        blockers = json.loads(row[3] or "{}")
    except ValueError:
        blockers = {}
    if not isinstance(estimates, list):
        estimates = []
    if not isinstance(throughput, dict):
        throughput = {}
    if not isinstance(blockers, dict):
        blockers = {}
    return {
        "project": str(row[0]),
        "estimates": estimates,
        "throughput": throughput,
        "blockers": blockers,
        "style_note": str(row[4] or ""),
    }


def _read_lifecycle(conn: sqlite3.Connection, project: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT project, estimates_json, throughput_json, blockers_json,"
        " style_note FROM memory_lifecycle WHERE project = ?",
        (project,),
    ).fetchone()
    if row is None:
        return {
            "project": project,
            "estimates": [],
            "throughput": {},
            "blockers": {},
            "style_note": "",
        }
    return _lifecycle_row(row)


def _write_lifecycle(conn: sqlite3.Connection, data: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO memory_lifecycle (project, estimates_json,"
        " throughput_json, blockers_json, style_note,"
        " updated_at) VALUES (?, ?, ?, ?, ?, datetime('now'))"
        " ON CONFLICT(project) DO UPDATE SET"
        " estimates_json = excluded.estimates_json,"
        " throughput_json = excluded.throughput_json,"
        " blockers_json = excluded.blockers_json,"
        " style_note = excluded.style_note,"
        " updated_at = datetime('now')",
        (
            str(data.get("project", "default")),
            json.dumps(list(data.get("estimates", []))[-MAX_SAMPLES:]),
            json.dumps(dict(data.get("throughput", {}))),
            json.dumps(dict(data.get("blockers", {}))),
            str(data.get("style_note", "") or ""),
        ),
    )
    conn.commit()


def get_lifecycle(db: DbLike, project: str = "default") -> dict[str, Any]:
    """Fetch the lifecycle row for a project, with empty defaults."""
    want = (project or "default").strip() or "default"
    own = not isinstance(db, sqlite3.Connection)
    conn = _conn(db)
    try:
        _ensure_store_tables(conn)
        ensure_memory_tables(conn)
        return _read_lifecycle(conn, want)
    finally:
        if own:
            conn.close()


def record_estimate(
    db: DbLike, project: str, estimate: float, actual: float
) -> dict[str, Any]:
    """Append one completed estimate vs actual sample for a project."""
    want = (project or "default").strip() or "default"
    try:
        est = max(0.0, float(estimate))
    except (TypeError, ValueError):
        est = 0.0
    try:
        done = max(0.0, float(actual))
    except (TypeError, ValueError):
        done = 0.0
    own = not isinstance(db, sqlite3.Connection)
    conn = _conn(db)
    try:
        _ensure_store_tables(conn)
        ensure_memory_tables(conn)
        data = _read_lifecycle(conn, want)
        samples = list(data["estimates"])
        samples.append({"estimate": est, "actual": done})
        data["estimates"] = samples[-MAX_SAMPLES:]
        _write_lifecycle(conn, data)
        return data
    finally:
        if own:
            conn.close()


def bias_factor(
    db: DbLike | None = None,
    project: str = "default",
    *,
    estimates: list[dict[str, Any]] | None = None,
) -> float:
    """Return actual over estimated hours, or 1.0 with no history.

    Accepts either a database plus project or an explicit sample list,
    so the planner and tests can use it without touching SQLite.
    """
    samples = estimates
    if samples is None:
        if db is None:
            return 1.0
        samples = get_lifecycle(db, project)["estimates"]
    est_total = 0.0
    done_total = 0.0
    for sample in samples or []:
        if not isinstance(sample, dict):
            continue
        try:
            est_total += max(0.0, float(sample.get("estimate", 0) or 0))
            done_total += max(0.0, float(sample.get("actual", 0) or 0))
        except (TypeError, ValueError):
            continue
    if est_total <= 0:
        return 1.0
    return round(done_total / est_total, 2)


def record_throughput(
    db: DbLike, project: str, week: str, hours: float
) -> dict[str, Any]:
    """Store completed hours for one week label such as 2026-W44."""
    want = (project or "default").strip() or "default"
    label = str(week or "").strip()
    try:
        done = max(0.0, float(hours))
    except (TypeError, ValueError):
        done = 0.0
    own = not isinstance(db, sqlite3.Connection)
    conn = _conn(db)
    try:
        _ensure_store_tables(conn)
        ensure_memory_tables(conn)
        data = _read_lifecycle(conn, want)
        board = dict(data["throughput"])
        if label:
            board[label] = done
        data["throughput"] = board
        _write_lifecycle(conn, data)
        return data
    finally:
        if own:
            conn.close()


def record_blocker(db: DbLike, project: str, key: str) -> dict[str, Any]:
    """Count one recurrence of a blocker key for a project."""
    want = (project or "default").strip() or "default"
    label = str(key or "").strip().lower() or "blocked"
    own = not isinstance(db, sqlite3.Connection)
    conn = _conn(db)
    try:
        _ensure_store_tables(conn)
        ensure_memory_tables(conn)
        data = _read_lifecycle(conn, want)
        counts = {str(k): int(v or 0) for k, v in data["blockers"].items()}
        counts[label] = counts.get(label, 0) + 1
        data["blockers"] = counts
        _write_lifecycle(conn, data)
        return data
    finally:
        if own:
            conn.close()


def save_style_note(db: DbLike, project: str, note: str) -> dict[str, Any]:
    """Save the lifecycle style note, such as the planning rhythm."""
    want = (project or "default").strip() or "default"
    own = not isinstance(db, sqlite3.Connection)
    conn = _conn(db)
    try:
        _ensure_store_tables(conn)
        ensure_memory_tables(conn)
        data = _read_lifecycle(conn, want)
        data["style_note"] = str(note or "").strip()
        _write_lifecycle(conn, data)
        return data
    finally:
        if own:
            conn.close()


def clear_lifecycle(db: DbLike, project: str | None = None) -> int:
    """Clear lifecycle rows, one project or all, returning the count."""
    own = not isinstance(db, sqlite3.Connection)
    conn = _conn(db)
    try:
        _ensure_store_tables(conn)
        ensure_memory_tables(conn)
        if project is None:
            cur = conn.execute("DELETE FROM memory_lifecycle")
        else:
            want = (project or "default").strip() or "default"
            cur = conn.execute(
                "DELETE FROM memory_lifecycle WHERE project = ?", (want,)
            )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        if own:
            conn.close()


def planning_context(db: DbLike, project: str = "default") -> dict[str, Any]:
    """Build the snapshot the next-sprint planner consumes.

    Returns people plus the bias factor. Empty memory yields no people
    and a bias of 1.0, which the planner treats exactly as no memory.
    """
    want = (project or "default").strip() or "default"
    return {
        "people": list_people(db),
        "bias": bias_factor(db, want),
    }


def memory_is_empty(snapshot: Any) -> bool:
    """Return True when a planning snapshot carries no memory."""
    if not isinstance(snapshot, dict):
        return True
    people = snapshot.get("people", [])
    bias = snapshot.get("bias", 1.0)
    try:
        neutral = abs(float(bias or 1.0) - 1.0) < 1e-9
    except (TypeError, ValueError):
        neutral = True
    return not people and neutral
