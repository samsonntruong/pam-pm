"""RAID log: Risks, Assumptions, Issues, Dependencies (ADDONS item 2).

Pam stores RAID entries in SQLite keyed by (backend, external_id) so the
Pam board and Azure DevOps each keep their own log. Pam proposes entries
and the lead accepts them; manual add and edit always work. Every
proposal is logged through WriteService with its evidence quote, so dry
run and Undo apply the same way as board writes.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from pam_pm.core.config import normalize_backend
from pam_pm.core.store import RAID_TABLE_DDL, now_iso

DbLike = str | Path | sqlite3.Connection

KINDS = ("risk", "assumption", "issue", "dependency")
KIND_LABELS = {
    "risk": "Risk",
    "assumption": "Assumption",
    "issue": "Issue",
    "dependency": "Dependency",
}
KIND_TITLES = {
    "risk": "Risks",
    "assumption": "Assumptions",
    "issue": "Issues",
    "dependency": "Dependencies",
}
STATUSES = ("proposed", "open", "closed")
SEVERITIES = ("low", "medium", "high", "critical")
NUDGE_SEVERITIES = ("high", "critical")

LEAD_ONLY_QUOTE = "Lead only"

RISK_PATTERNS = (
    "risk",
    "risky",
    "might slip",
    "could slip",
    "may slip",
    "might miss",
    "could miss",
    "may miss",
    "likely to miss",
    "worried",
    "worry",
    "concerned",
    "concern",
    "at risk",
    "in danger",
    "danger",
    "uncertain",
    "not sure we",
    "unsure",
)

ASSUMPTION_PATTERNS = (
    "assum",
    "suppos",
    "given that",
    "taking for granted",
    "we believe",
    "presume",
    "presuppos",
)


def _connect(db: DbLike) -> sqlite3.Connection:
    if isinstance(db, sqlite3.Connection):
        return db
    return sqlite3.connect(str(db))


def _close(db: DbLike, conn: sqlite3.Connection) -> None:
    if not isinstance(db, sqlite3.Connection):
        conn.close()


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(RAID_TABLE_DDL)
    conn.commit()


def new_external_id(kind: str) -> str:
    """Return a fresh external id for a RAID kind."""
    slug = (kind or "item").strip().lower() or "item"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def normalize_kind(kind: object) -> str:
    """Return a valid RAID kind, defaulting to risk."""
    text = str(kind or "").strip().lower()
    if text.endswith("s") and text[:-1] in KINDS:
        text = text[:-1]
    return text if text in KINDS else "risk"


def normalize_status(status: object) -> str:
    """Return a valid RAID status, defaulting to proposed."""
    text = str(status or "").strip().lower()
    return text if text in STATUSES else "proposed"


def normalize_severity(severity: object) -> str:
    """Return a valid severity, defaulting to medium."""
    text = str(severity or "").strip().lower()
    return text if text in SEVERITIES else "medium"


def _linked_json(linked: Any) -> str:
    if isinstance(linked, str):
        try:
            parsed = json.loads(linked)
        except ValueError:
            parsed = [linked]
        linked = parsed
    ids = [str(entry).strip() for entry in (linked or []) if str(entry).strip()]
    return json.dumps(ids)


def _linked_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(entry) for entry in raw]
    try:
        parsed = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return [str(entry) for entry in parsed] if isinstance(parsed, list) else []


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "backend": row[0],
        "external_id": row[1],
        "project": row[2],
        "kind": row[3],
        "title": row[4],
        "owner": row[5],
        "severity": row[6],
        "status": row[7],
        "due_date": row[8],
        "linked_ids": _linked_list(row[9]),
        "evidence_quote": row[10],
        "evidence_date": row[11],
        "write_id": int(row[12] or 0),
        "created_at": row[13],
        "updated_at": row[14],
    }


_COLUMNS = (
    "backend, external_id, project, kind, title, owner, severity, status,"
    " due_date, linked_ids, evidence_quote, evidence_date, write_id,"
    " created_at, updated_at"
)


def save_item(
    db: DbLike,
    backend: str,
    external_id: str,
    project: str = "default",
    kind: str = "risk",
    title: str = "",
    owner: str = "",
    severity: str = "medium",
    status: str = "proposed",
    due_date: str = "",
    linked_ids: Any = (),
    evidence_quote: str = "",
    evidence_date: str = "",
    write_id: int = 0,
) -> dict[str, Any]:
    """Insert or replace one RAID entry keyed by (backend, external_id)."""
    conn = _connect(db)
    try:
        _ensure(conn)
        conn.execute(
            "INSERT INTO raid_items"
            " (backend, external_id, project, kind, title, owner, severity,"
            " status, due_date, linked_ids, evidence_quote, evidence_date,"
            " write_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " datetime('now'), datetime('now'))"
            " ON CONFLICT(backend, external_id) DO UPDATE SET"
            " project = excluded.project, kind = excluded.kind,"
            " title = excluded.title, owner = excluded.owner,"
            " severity = excluded.severity, status = excluded.status,"
            " due_date = excluded.due_date, linked_ids = excluded.linked_ids,"
            " evidence_quote = excluded.evidence_quote,"
            " evidence_date = excluded.evidence_date,"
            " write_id = excluded.write_id, updated_at = datetime('now')",
            (
                normalize_backend(backend),
                str(external_id),
                str(project or "default"),
                normalize_kind(kind),
                str(title or ""),
                str(owner or ""),
                normalize_severity(severity),
                normalize_status(status),
                str(due_date or ""),
                _linked_json(linked_ids),
                str(evidence_quote or ""),
                str(evidence_date or ""),
                int(write_id or 0),
            ),
        )
        conn.commit()
    finally:
        _close(db, conn)
    item = get_item(db, backend, external_id)
    if item is None:
        raise ValueError("RAID save failed")
    return item


def get_item(db: DbLike, backend: str, external_id: str) -> dict[str, Any] | None:
    """Fetch one RAID entry by (backend, external_id)."""
    conn = _connect(db)
    try:
        _ensure(conn)
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM raid_items"
            " WHERE backend = ? AND external_id = ?",
            (normalize_backend(backend), str(external_id)),
        ).fetchone()
    finally:
        _close(db, conn)
    return _row_to_dict(row) if row else None


def list_items(
    db: DbLike,
    backend: str,
    project: str | None = None,
    kind: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List RAID entries for a backend, newest last, with filters."""
    query = f"SELECT {_COLUMNS} FROM raid_items WHERE backend = ?"
    params: list[object] = [normalize_backend(backend)]
    if project:
        query += " AND project = ?"
        params.append(str(project))
    if kind:
        query += " AND kind = ?"
        params.append(normalize_kind(kind))
    if status:
        query += " AND status = ?"
        params.append(normalize_status(status))
    query += " ORDER BY rowid ASC"
    conn = _connect(db)
    try:
        _ensure(conn)
        rows = conn.execute(query, params).fetchall()
    finally:
        _close(db, conn)
    return [_row_to_dict(row) for row in rows]


def update_item(
    db: DbLike, backend: str, external_id: str, fields: dict[str, Any]
) -> dict[str, Any] | None:
    """Patch one RAID entry, returning the updated row or None."""
    current = get_item(db, backend, external_id)
    if current is None:
        return None
    merged = dict(current)
    for key in (
        "project",
        "kind",
        "title",
        "owner",
        "severity",
        "status",
        "due_date",
        "evidence_quote",
        "evidence_date",
    ):
        if key in fields and fields[key] is not None:
            merged[key] = fields[key]
    if "linked_ids" in fields and fields["linked_ids"] is not None:
        merged["linked_ids"] = fields["linked_ids"]
    if "write_id" in fields and fields["write_id"] is not None:
        merged["write_id"] = fields["write_id"]
    return save_item(
        db,
        merged["backend"],
        merged["external_id"],
        project=str(merged["project"]),
        kind=str(merged["kind"]),
        title=str(merged["title"]),
        owner=str(merged["owner"]),
        severity=str(merged["severity"]),
        status=str(merged["status"]),
        due_date=str(merged["due_date"]),
        linked_ids=merged["linked_ids"],
        evidence_quote=str(merged["evidence_quote"]),
        evidence_date=str(merged["evidence_date"]),
        write_id=int(merged["write_id"] or 0),
    )


def delete_item(db: DbLike, backend: str, external_id: str) -> bool:
    """Delete one RAID entry, returning True when a row was removed."""
    conn = _connect(db)
    try:
        _ensure(conn)
        cur = conn.execute(
            "DELETE FROM raid_items WHERE backend = ? AND external_id = ?",
            (normalize_backend(backend), str(external_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        _close(db, conn)


def _quote_exists(
    db: DbLike, backend: str, project: str, kind: str, quote: str
) -> bool:
    want = " ".join(str(quote or "").split()).lower()
    if not want:
        return False
    for item in list_items(db, backend, project=project, kind=kind):
        if item["status"] == "closed":
            continue
        have = " ".join(str(item["evidence_quote"] or "").split()).lower()
        if have and have == want:
            return True
    return False


def propose_item(
    db: DbLike,
    backend: str,
    project: str,
    kind: str,
    title: str,
    owner: str = "",
    severity: str = "medium",
    due_date: str = "",
    linked_ids: Any = (),
    evidence_quote: str = "",
    evidence_date: str = "",
    writes: Any = None,
    external_id: str = "",
) -> dict[str, Any]:
    """Store a proposed RAID entry and log it through WriteService.

    The entry stays proposed until the lead accepts it. When a write
    service is given, the proposal is logged with its evidence quote so
    dry run and Undo apply; the write id is kept on the row.
    """
    ident = str(external_id or "").strip() or new_external_id(kind)
    item = save_item(
        db,
        backend,
        ident,
        project=project,
        kind=kind,
        title=title,
        owner=owner,
        severity=severity,
        status="proposed",
        due_date=due_date,
        linked_ids=linked_ids,
        evidence_quote=evidence_quote,
        evidence_date=evidence_date or now_iso()[:10],
    )
    if writes is not None:
        try:
            write_id = writes.propose(
                {
                    "operation": f"raid_propose_{normalize_kind(kind)}",
                    "item_id": ident,
                    "evidence_quote": str(evidence_quote or "")[:500],
                    "status": "proposed",
                    "detail": f"RAID {normalize_kind(kind)} proposed: {title[:200]}",
                }
            )
        except Exception:  # noqa: BLE001
            write_id = 0
        if write_id:
            updated = update_item(
                db, backend, ident, {"write_id": int(write_id)}
            )
            if updated is not None:
                item = updated
    return item


def accept_item(
    db: DbLike, backend: str, external_id: str, writes: Any = None
) -> dict[str, Any] | None:
    """Accept a proposed entry, moving it to open.

    Returns None when the entry is missing, not proposed, or blocked by
    dry run. With a write service in dry run mode the entry stays
    proposed, mirroring board writes.
    """
    item = get_item(db, backend, external_id)
    if item is None or item["status"] != "proposed":
        return None
    if writes is not None and bool(getattr(writes, "dry_run", False)):
        return None
    updated = update_item(db, backend, external_id, {"status": "open"})
    if writes is not None and item["write_id"]:
        try:
            writes.apply(int(item["write_id"]))
        except Exception:  # noqa: BLE001, S110
            pass
    return updated


def close_item(
    db: DbLike, backend: str, external_id: str, writes: Any = None
) -> dict[str, Any] | None:
    """Close an open or proposed entry, returning the updated row."""
    item = get_item(db, backend, external_id)
    if item is None or item["status"] == "closed":
        return None
    updated = update_item(db, backend, external_id, {"status": "closed"})
    if writes is not None:
        try:
            writes.propose(
                {
                    "operation": "raid_close",
                    "item_id": str(external_id),
                    "evidence_quote": str(item["evidence_quote"] or "")[:500],
                    "status": "proposed",
                    "detail": f"RAID {item['kind']} closed: {item['title'][:200]}",
                }
            )
        except Exception:  # noqa: BLE001, S110
            pass
    return updated


def on_write_undone(db: DbLike, write_id: int) -> dict[str, Any] | None:
    """Revert the RAID entry tied to an undone write.

    An accepted entry goes back to proposed; a still proposed entry is
    removed. Returns the reverted row, or None when nothing matched.
    """
    conn = _connect(db)
    try:
        _ensure(conn)
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM raid_items WHERE write_id = ?",
            (int(write_id),),
        ).fetchone()
    finally:
        _close(db, conn)
    if row is None:
        return None
    item = _row_to_dict(row)
    if item["status"] == "open":
        return update_item(
            db, item["backend"], item["external_id"], {"status": "proposed"}
        )
    if item["status"] == "proposed":
        delete_item(db, item["backend"], item["external_id"])
        return {**item, "status": "undone"}
    return None


def _blocker_text(info: Any, name: str) -> str:
    if isinstance(info, dict):
        return str(info.get(name, "") or "")
    return str(getattr(info, name, "") or "")


def propose_issue_from_blocker(
    db: DbLike,
    backend: str,
    project: str,
    blocker: Any,
    writes: Any = None,
) -> dict[str, Any]:
    """Propose an Issue from a blocker intake result."""
    description = _blocker_text(blocker, "description") or "Blocked work"
    owner = _blocker_text(blocker, "owner")
    quote = _blocker_text(blocker, "evidence_quote")
    blocked = _blocker_text(blocker, "blocked_item_id")
    blocking = _blocker_text(blocker, "blocking_item_id")
    linked = [ident for ident in (blocked, blocking) if ident]
    title = description.strip()
    if len(title) > 120:
        title = title[:117].rstrip() + "..."
    return propose_item(
        db,
        backend,
        project,
        "issue",
        f"Blocked: {title}",
        owner=owner,
        severity="high",
        linked_ids=linked,
        evidence_quote=quote or description,
        writes=writes,
    )


def propose_dependency_from_link(
    db: DbLike,
    backend: str,
    project: str,
    source_id: str,
    target_id: str,
    evidence_quote: str = "",
    owner: str = "",
    writes: Any = None,
) -> dict[str, Any]:
    """Propose a Dependency linked to both ends of a board link."""
    quote = str(evidence_quote or "").strip() or (
        f"Item {source_id} blocks item {target_id}."
    )
    return propose_item(
        db,
        backend,
        project,
        "dependency",
        f"{source_id} blocks {target_id}",
        owner=owner,
        severity="medium",
        linked_ids=[str(source_id), str(target_id)],
        evidence_quote=quote,
        writes=writes,
    )


def mentions_risk(text: str) -> str:
    """Return the first risk phrase found in text, or an empty string."""
    lowered = str(text or "").lower()
    for phrase in RISK_PATTERNS:
        if phrase in lowered:
            return phrase
    return ""


def propose_risk_from_checkin(
    db: DbLike,
    backend: str,
    project: str,
    text: str,
    person: str = "",
    writes: Any = None,
) -> dict[str, Any] | None:
    """Propose a Risk when check-in text mentions one.

    Returns None when no risk phrase is found or the same quote was
    already proposed, so repeated check-ins do not spam the log.
    """
    raw = str(text or "").strip()
    if not raw or not mentions_risk(raw):
        return None
    if _quote_exists(db, backend, project, "risk", raw):
        return None
    title = re.sub(r"\s+", " ", raw)
    if len(title) > 120:
        title = title[:117].rstrip() + "..."
    lowered = raw.lower()
    severity = "high" if ("will miss" in lowered or "at risk" in lowered) else "medium"
    return propose_item(
        db,
        backend,
        project,
        "risk",
        f"Risk: {title}",
        owner=str(person or ""),
        severity=severity,
        evidence_quote=raw[:500],
        writes=writes,
    )


def extract_assumptions(text: str) -> list[dict[str, str]]:
    """Pull assumption lines out of a kickoff document.

    Scans each line for assumption phrases and returns title plus quote
    pairs. Plain code, no model calls.
    """
    out: list[dict[str, str]] = []
    for line in str(text or "").splitlines():
        cleaned = re.sub(r"\s+", " ", line.strip()).strip("#*- \t")
        if len(cleaned) < 12:
            continue
        lowered = cleaned.lower()
        if not any(phrase in lowered for phrase in ASSUMPTION_PATTERNS):
            continue
        title = cleaned if len(cleaned) <= 120 else cleaned[:117].rstrip() + "..."
        out.append({"title": title, "evidence_quote": cleaned[:500]})
    return out


def propose_assumptions_from_text(
    db: DbLike,
    backend: str,
    project: str,
    text: str,
    writes: Any = None,
) -> list[dict[str, Any]]:
    """Propose one Assumption per assumption line in a document."""
    proposed: list[dict[str, Any]] = []
    for entry in extract_assumptions(text):
        if _quote_exists(
            db, backend, project, "assumption", entry["evidence_quote"]
        ):
            continue
        proposed.append(
            propose_item(
                db,
                backend,
                project,
                "assumption",
                f"Assumption: {entry['title']}",
                evidence_quote=entry["evidence_quote"],
                writes=writes,
            )
        )
    return proposed


def close_issues_for_board_item(
    db: DbLike, backend: str, project: str, board_item_id: str
) -> list[dict[str, Any]]:
    """Close open Issues linked to a board item that just closed."""
    want = str(board_item_id or "").strip()
    if not want:
        return []
    closed: list[dict[str, Any]] = []
    for item in list_items(db, backend, project=project, kind="issue"):
        if item["status"] != "open":
            continue
        if want in [str(ident) for ident in item["linked_ids"]]:
            updated = update_item(
                db, backend, item["external_id"], {"status": "closed"}
            )
            if updated is not None:
                closed.append(updated)
    return closed


def nudge_lines(
    db: DbLike, backend: str, project: str, person: str
) -> list[str]:
    """Return morning nudge lines for one owner.

    Covers their open Issues and their high or critical Risks. Empty
    when there is nothing to nudge about.
    """
    want = str(person or "").strip()
    if not want:
        return []
    lines: list[str] = []
    for item in list_items(db, backend, project=project):
        if item["status"] != "open":
            continue
        if str(item["owner"] or "").strip() != want:
            continue
        if item["kind"] == "issue":
            lines.append(f"Open issue: {item['title']}")
        elif item["kind"] == "risk" and item["severity"] in NUDGE_SEVERITIES:
            lines.append(f"High risk: {item['title']}")
    return lines


def review_section(
    db: DbLike, backend: str, project: str
) -> dict[str, Any]:
    """Summarize new and closed RAID items for the sprint review."""
    items = list_items(db, backend, project=project)
    new_items = [item for item in items if item["status"] in ("proposed", "open")]
    closed_items = [item for item in items if item["status"] == "closed"]
    lines = ["## RAID changes", ""]
    if new_items:
        lines.append("New or open:")
        for item in new_items:
            label = KIND_LABELS.get(item["kind"], item["kind"])
            owner = f" ({item['owner']})" if item["owner"] else " (no owner)"
            lines.append(f"- {label}: {item['title']}{owner} [{item['status']}]")
    else:
        lines.append("No new RAID items.")
    lines.append("")
    if closed_items:
        lines.append("Closed:")
        for item in closed_items:
            label = KIND_LABELS.get(item["kind"], item["kind"])
            lines.append(f"- {label}: {item['title']}")
    else:
        lines.append("No closed RAID items.")
    return {
        "markdown": "\n".join(lines) + "\n",
        "new": new_items,
        "closed": closed_items,
    }


def _is_past_due(due_date: str, today: str) -> bool:
    due = str(due_date or "").strip()[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        return False
    return due < str(today or "").strip()[:10]


def triage_flags(
    db: DbLike, backend: str, project: str, today: str = ""
) -> dict[str, list[dict[str, Any]]]:
    """Flag RAID items with no owner or past due for the triage rule.

    Only proposed and open items are flagged; closed items are done.
    """
    day = str(today or "").strip()[:10] or date.today().isoformat()
    no_owner: list[dict[str, Any]] = []
    past_due: list[dict[str, Any]] = []
    for item in list_items(db, backend, project=project):
        if item["status"] == "closed":
            continue
        if not str(item["owner"] or "").strip():
            no_owner.append(item)
        if _is_past_due(str(item["due_date"] or ""), day):
            past_due.append(item)
    return {"no_owner": no_owner, "past_due": past_due}


def _quote_for(item: dict[str, Any], for_lead: bool) -> str:
    if for_lead:
        return str(item.get("evidence_quote", "") or "")
    if str(item.get("evidence_quote", "") or ""):
        return LEAD_ONLY_QUOTE
    return ""


def export_csv(items: list[dict[str, Any]], for_lead: bool = True) -> str:
    """Render RAID entries as CSV with one row per entry."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "kind",
            "title",
            "owner",
            "severity",
            "status",
            "due_date",
            "linked_ids",
            "evidence_quote",
            "evidence_date",
        ]
    )
    for item in items or []:
        linked = ";".join(str(ident) for ident in (item.get("linked_ids") or []))
        writer.writerow(
            [
                str(item.get("kind", "")),
                str(item.get("title", "")),
                str(item.get("owner", "")),
                str(item.get("severity", "")),
                str(item.get("status", "")),
                str(item.get("due_date", "")),
                linked,
                _quote_for(item, for_lead),
                str(item.get("evidence_date", "")),
            ]
        )
    return buf.getvalue()


def export_markdown(items: list[dict[str, Any]], for_lead: bool = True) -> str:
    """Render RAID entries as Markdown grouped by kind."""
    lines = ["# RAID log", ""]
    grouped: dict[str, list[dict[str, Any]]] = {kind: [] for kind in KINDS}
    for item in items or []:
        grouped.setdefault(normalize_kind(item.get("kind")), []).append(item)
    for kind in KINDS:
        lines.append(f"## {KIND_TITLES[kind]}")
        lines.append("")
        rows = grouped.get(kind, [])
        if not rows:
            lines.append(f"No {kind}s.")
            lines.append("")
            continue
        lines.append(
            "| Title | Owner | Severity | Status | Due | Linked | Evidence | Date |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for item in rows:
            linked = ", ".join(str(ident) for ident in (item.get("linked_ids") or []))
            quote = _quote_for(item, for_lead).replace("|", "/").replace("\n", " ")
            cells = [
                str(item.get("title", "")).replace("|", "/").replace("\n", " "),
                str(item.get("owner", "")),
                str(item.get("severity", "")),
                str(item.get("status", "")),
                str(item.get("due_date", "")),
                linked,
                quote,
                str(item.get("evidence_date", "")),
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)
