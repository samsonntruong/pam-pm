"""Board triage rule (ADDONS item 3) with RAID hooks (ADDONS item 2).

Flags RAID entries with no owner or past due, plus board findings:
stale items, unassigned items, missing estimates, overloaded people,
items blocked by a closed or missing predecessor, and unclear titles.
The scan is plain code, no tokens. The model runs only when the board
changed since the last scan and a finding needs judgment.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from pam_pm.adapters.pm.base import is_closed


def format_triage_message(flags: dict[str, list[dict[str, Any]]]) -> str:
    """Render no-owner and past-due RAID flags as one chat message."""
    no_owner = flags.get("no_owner", [])
    past_due = flags.get("past_due", [])
    if not no_owner and not past_due:
        return "RAID triage: nothing needs attention."
    lines = ["RAID triage: items need attention.", ""]
    if no_owner:
        lines.append("No owner:")
        for item in no_owner:
            lines.append(f"- {item.get('title', '')} [{item.get('kind', '')}]")
        lines.append("")
    if past_due:
        lines.append("Past due:")
        for item in past_due:
            lines.append(
                f"- {item.get('title', '')} (due {item.get('due_date', '')})"
            )
    return "\n".join(lines).rstrip() + "\n"


def triage_raid(
    db: Any,
    backend: str = "azure_devops",
    project: str = "default",
    chat: Any = None,
    lead: str = "",
    today: str = "",
) -> dict[str, Any]:
    """Flag ownerless and overdue RAID items, DMing the lead when asked.

    Returns the flags plus the message text. A DM goes out only when a
    chat adapter and lead id are given and at least one item is flagged.
    """
    from pam_pm.core import raid as raid_mod

    flags = raid_mod.triage_flags(
        db,
        str(backend or "azure_devops"),
        str(project or "default"),
        today=today,
    )
    message = format_triage_message(flags)
    sent = False
    if chat is not None and lead and (flags["no_owner"] or flags["past_due"]):
        try:
            chat.send_dm(str(lead), message)
            sent = True
        except Exception:  # noqa: BLE001
            sent = False
    return {
        "no_owner": flags["no_owner"],
        "past_due": flags["past_due"],
        "message": message,
        "sent": sent,
    }


TRIAGE_SCHEDULE_DEFAULT = "daily"
TRIAGE_SCHEDULE_TIME = "09:00"
DEFAULT_STALE_DAYS = 3
DEFAULT_OVERLOAD_THRESHOLD = 5
DEFAULT_ESTIMATE = 4.0
BOARD_LIMIT = 500
MAX_SLACK_BUTTONS = 10

FORWARD_REL = "System.LinkTypes.Dependency-Forward"
REVERSE_REL = "System.LinkTypes.Dependency-Reverse"

TRIAGE_STATE_DDL = """CREATE TABLE IF NOT EXISTS triage_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
)"""

_OPEN_STATE_NAMES = {"new", "to do", "todo", "open", "proposed"}
_EXTRA_CLOSED = {"removed", "resolved"}

_OWNER_KEYS = (
    "assigned_to",
    "owner",
    "System.AssignedTo",
    "user_id",
    "assignee",
)

_ESTIMATE_KEYS = (
    "estimate",
    "Microsoft.VSTS.Scheduling.OriginalEstimate",
    "original_estimate",
)

_GENERIC_TITLES = {
    "todo",
    "task",
    "fix",
    "bug",
    "work",
    "item",
    "update",
    "follow up",
    "followup",
    "tbd",
    "stuff",
    "misc",
}

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _item_id(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("id", item.get("item_id", "")) or "")
    return str(getattr(item, "id", getattr(item, "item_id", "")) or "")


def _item_title(item: Any) -> str:
    if isinstance(item, dict):
        fields = item.get("fields", {})
        if isinstance(fields, dict) and fields.get("System.Title"):
            return str(fields["System.Title"])
        return str(item.get("title", "") or _item_id(item))
    title = str(getattr(item, "title", "") or "")
    if title:
        return title
    fields = getattr(item, "fields", None)
    if isinstance(fields, dict) and fields.get("System.Title"):
        return str(fields["System.Title"])
    return _item_id(item)


def _item_state(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("state", "") or "")
    return str(getattr(item, "state", "") or "")


def _item_changed(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("changed", "ChangedDate", "changed_date"):
            value = item.get(key)
            if value:
                return str(value)
        fields = item.get("fields", {})
        if isinstance(fields, dict):
            cand = fields.get("System.ChangedDate", "")
            if cand:
                return str(cand)
        return ""
    value = getattr(item, "changed", "")
    if value:
        return str(value)
    fields = getattr(item, "fields", None)
    if isinstance(fields, dict):
        return str(fields.get("System.ChangedDate", "") or "")
    return ""


def _fields_of(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        raw = item.get("fields", {})
        base = dict(raw) if isinstance(raw, dict) else {}
        for key, val in item.items():
            if key != "fields":
                base.setdefault(key, val)
        return base
    raw = getattr(item, "fields", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _owner_text(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, dict):
        for key in ("displayName", "uniqueName", "name", "id"):
            cand = value.get(key)
            if cand:
                return str(cand)
        return ""
    return str(value).strip()


def owner_of(item: Any) -> str:
    """Return the owner of a board item across both backends."""
    fields = _fields_of(item)
    for key in _OWNER_KEYS:
        text = _owner_text(fields.get(key))
        if text:
            return text
    return ""


def _has_estimate(item: Any) -> bool:
    fields = _fields_of(item)
    for key in _ESTIMATE_KEYS:
        value = fields.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return True
    return False


def _is_closed_state(state: str) -> bool:
    if is_closed(state):
        return True
    return state.strip().lower() in _EXTRA_CLOSED


def _is_in_progress(state: str) -> bool:
    text = state.strip().lower()
    if not text or _is_closed_state(state):
        return False
    return text not in _OPEN_STATE_NAMES


def _parse_day(text: str) -> date | None:
    match = _DATE_RE.match((text or "").strip())
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _today(text: str) -> date:
    return _parse_day(text) or date.today()


def age_days(changed: str, today: str = "") -> int | None:
    """Return whole days since a ChangedDate stamp, or None if unknown."""
    day = _parse_day(changed)
    if day is None:
        return None
    return max(0, (_today(today) - day).days)


def board_fingerprint(items: list[Any]) -> str:
    """Hash board state so repeat scans can tell if anything changed."""
    parts = []
    for item in items:
        fields = _fields_of(item)
        est = ""
        for key in _ESTIMATE_KEYS:
            if fields.get(key) not in (None, ""):
                est = str(fields[key])
                break
        parts.append(
            "|".join(
                (
                    _item_id(item),
                    _item_title(item),
                    _item_state(item),
                    _item_changed(item),
                    owner_of(item),
                    est,
                )
            )
        )
    blob = "\n".join(sorted(parts))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _state_key(backend: str, project: str) -> str:
    return f"triage:{backend}:{project}"


def _connect_state(db: Any) -> sqlite3.Connection:
    if isinstance(db, sqlite3.Connection):
        return db
    return sqlite3.connect(str(db))


def get_last_fingerprint(db: Any, backend: str, project: str) -> str | None:
    """Return the board fingerprint from the last triage scan, if any."""
    key = _state_key(str(backend or "azure_devops"), str(project or "default"))
    conn = _connect_state(db)
    try:
        conn.execute(TRIAGE_STATE_DDL)
        row = conn.execute(
            "SELECT value FROM triage_state WHERE key = ?", (key,)
        ).fetchone()
    finally:
        if not isinstance(db, sqlite3.Connection):
            conn.close()
    if row is None or not row[0]:
        return None
    return str(row[0])


def save_last_fingerprint(
    db: Any, backend: str, project: str, fingerprint: str
) -> None:
    """Store the board fingerprint from the latest triage scan."""
    key = _state_key(str(backend or "azure_devops"), str(project or "default"))
    conn = _connect_state(db)
    try:
        conn.execute(TRIAGE_STATE_DDL)
        conn.execute(
            "INSERT INTO triage_state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(fingerprint)),
        )
        conn.commit()
    finally:
        if not isinstance(db, sqlite3.Connection):
            conn.close()


def _least_loaded(
    active_by_owner: dict[str, list[Any]], names: list[str]
) -> str:
    if not names:
        return ""
    return min(names, key=lambda label: len(active_by_owner.get(label, [])))


def _is_unclear(title: str) -> bool:
    text = re.sub(r"\s+", " ", str(title or "").strip())
    if not text:
        return True
    if text.lower() in _GENERIC_TITLES:
        return True
    if len(text.split(" ")) < 3:
        return True
    return len(text) < 12


def _predecessor_map(board: Any, items: list[Any]) -> dict[str, list[str]]:
    """Map each item id to its predecessor ids across both backends.

    Links are read from every item because ADO stores a Forward link
    on the source item only, while the native board answers both ends.
    """
    found: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for item in items:
        try:
            links = list(board.list_links(_item_id(item)) or [])
        except Exception:  # noqa: BLE001, S112
            continue
        for link in links:
            if isinstance(link, dict):
                src = str(link.get("source_id", ""))
                tgt = str(link.get("target_id", ""))
                rel = str(link.get("rel", ""))
            else:
                src = str(getattr(link, "source_id", ""))
                tgt = str(getattr(link, "target_id", ""))
                rel = str(getattr(link, "rel", ""))
            pred = ""
            target = ""
            if rel.endswith("Dependency-Forward") and src and tgt:
                pred, target = src, tgt
            elif rel.endswith("Dependency-Reverse") and src and tgt:
                pred, target = tgt, src
            if not pred or not target or (pred, target) in seen:
                continue
            seen.add((pred, target))
            found.setdefault(target, []).append(pred)
    return found


def scan_board(
    board: Any,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    overload_threshold: int = DEFAULT_OVERLOAD_THRESHOLD,
    roster: list[str] | None = None,
    today: str = "",
    limit: int = BOARD_LIMIT,
) -> list[dict[str, Any]]:
    """Scan a board with plain code only. No model calls happen here."""
    names = [str(entry).strip() for entry in (roster or []) if str(entry).strip()]
    items = list(board.list_items(limit=limit))
    try:
        states = [str(entry) for entry in board.list_states()]
    except Exception:  # noqa: BLE001
        states = []
    open_state = states[0] if states else "New"
    by_id = {_item_id(item): item for item in items}
    active_by_owner: dict[str, list[Any]] = {}
    for item in items:
        if _is_closed_state(_item_state(item)):
            continue
        owner = owner_of(item)
        if owner:
            active_by_owner.setdefault(owner, []).append(item)
    predecessors = _predecessor_map(board, items)
    findings: list[dict[str, Any]] = []
    for item in items:
        item_id = _item_id(item)
        title = _item_title(item)
        state = _item_state(item)
        if _is_closed_state(state):
            continue
        owner = owner_of(item)
        if _is_in_progress(state):
            age = age_days(_item_changed(item), today)
            if age is not None and age >= max(1, int(stale_days)):
                findings.append(
                    {
                        "kind": "stale",
                        "item_id": item_id,
                        "title": title,
                        "detail": f"No change in {age} days while {state}.",
                        "fix_label": "Post a nudge comment asking for a status update.",
                        "fix": {
                            "action": "comment",
                            "item_id": item_id,
                            "text": (
                                f"Triage nudge: no change in {age} days. "
                                "Please post a status update."
                            ),
                        },
                        "needs_judgment": False,
                    }
                )
        if not owner:
            suggested = _least_loaded(active_by_owner, names)
            label = f"Assign to {suggested}." if suggested else "Assign an owner."
            findings.append(
                {
                    "kind": "unassigned",
                    "item_id": item_id,
                    "title": title,
                    "detail": "No owner while open.",
                    "fix_label": label,
                    "fix": {
                        "action": "assign",
                        "item_id": item_id,
                        "owner": suggested,
                    },
                    "needs_judgment": False,
                }
            )
        if not _has_estimate(item):
            findings.append(
                {
                    "kind": "missing_estimate",
                    "item_id": item_id,
                    "title": title,
                    "detail": "No estimate while open.",
                    "fix_label": f"Set estimate to {DEFAULT_ESTIMATE:g}h as a start.",
                    "fix": {
                        "action": "set_estimate",
                        "item_id": item_id,
                        "estimate": DEFAULT_ESTIMATE,
                    },
                    "needs_judgment": False,
                }
            )
        for pred_id in predecessors.get(item_id, []):
            target = by_id.get(pred_id)
            if target is None:
                try:
                    target = board.get_item(pred_id)
                except Exception:  # noqa: BLE001
                    target = None
            if target is None:
                findings.append(
                    {
                        "kind": "blocked_predecessor",
                        "item_id": item_id,
                        "title": title,
                        "detail": f"Blocked by {pred_id}, which is missing.",
                        "fix_label": "Post a comment asking the owner to relink it.",
                        "fix": {
                            "action": "comment",
                            "item_id": item_id,
                            "text": (
                                f"Triage: predecessor {pred_id} is missing. "
                                "Please relink or confirm this item is unblocked."
                            ),
                        },
                        "needs_judgment": False,
                    }
                )
            elif _is_closed_state(_item_state(target)):
                findings.append(
                    {
                        "kind": "blocked_predecessor",
                        "item_id": item_id,
                        "title": title,
                        "detail": f"Blocked by {pred_id}, which is closed.",
                        "fix_label": f"Move back to {open_state}; the blocker cleared.",
                        "fix": {
                            "action": "reopen",
                            "item_id": item_id,
                            "state": open_state,
                        },
                        "needs_judgment": False,
                    }
                )
        if _is_unclear(title):
            draft = f"{title} (say what and where)" if title else "Untitled item"
            findings.append(
                {
                    "kind": "unclear_title",
                    "item_id": item_id,
                    "title": title,
                    "detail": "Title is too short to act on.",
                    "fix_label": f"Rename to {draft}.",
                    "fix": {"action": "rename", "item_id": item_id, "title": draft},
                    "needs_judgment": True,
                }
            )
    for person, owned in sorted(active_by_owner.items()):
        if len(owned) <= max(0, int(overload_threshold)):
            continue
        oldest = min(owned, key=lambda entry: _item_changed(entry) or "")
        oldest_id = _item_id(oldest)
        others = [label for label in names if label != person]
        suggested = _least_loaded(active_by_owner, others)
        if suggested:
            label = f"Reassign {oldest_id} to {suggested}."
        else:
            label = "Reassign one item to someone with room."
        findings.append(
            {
                "kind": "overloaded",
                "item_id": oldest_id,
                "title": _item_title(oldest),
                "detail": f"{person} holds {len(owned)} open items.",
                "fix_label": label,
                "fix": {
                    "action": "reassign",
                    "item_id": oldest_id,
                    "owner": suggested,
                    "from": person,
                },
                "needs_judgment": False,
            }
        )
    return findings


def scan_raid(
    db: Any,
    backend: str = "azure_devops",
    project: str = "default",
    today: str = "",
    lead: str = "",
) -> list[dict[str, Any]]:
    """Flag RAID items with no owner or past due via the existing hook."""
    from pam_pm.core import raid as raid_mod

    backend_name = str(backend or "azure_devops")
    project_name = str(project or "default")
    flags = raid_mod.triage_flags(db, backend_name, project_name, today=today)
    findings: list[dict[str, Any]] = []
    for item in flags.get("no_owner", []):
        external_id = str(item.get("external_id", ""))
        label = f"Assign to {lead}." if lead else "Assign an owner."
        findings.append(
            {
                "kind": "raid_no_owner",
                "item_id": external_id,
                "title": str(item.get("title", "")),
                "detail": f"{item.get('kind', 'entry')} has no owner.",
                "fix_label": label,
                "fix": {
                    "action": "raid_set_owner",
                    "external_id": external_id,
                    "owner": str(lead or ""),
                },
                "needs_judgment": False,
            }
        )
    new_due = (_today(today) + timedelta(days=7)).isoformat()
    for item in flags.get("past_due", []):
        external_id = str(item.get("external_id", ""))
        findings.append(
            {
                "kind": "raid_past_due",
                "item_id": external_id,
                "title": str(item.get("title", "")),
                "detail": f"Due {item.get('due_date', '')}, which has passed.",
                "fix_label": f"Move the due date to {new_due} or close it.",
                "fix": {
                    "action": "raid_extend_due",
                    "external_id": external_id,
                    "due_date": new_due,
                },
                "needs_judgment": False,
            }
        )
    return findings


def refine_with_model(model: Any, findings: list[dict[str, Any]]) -> bool:
    """Ask the model for clearer titles. Returns True when it was called."""
    unclear = [entry for entry in findings if entry.get("kind") == "unclear_title"]
    if not unclear:
        return False
    hook = None
    for name in ("review_triage", "judge"):
        cand = getattr(model, name, None)
        if callable(cand):
            hook = cand
            break
    if hook is None and callable(model):
        hook = model
    if hook is None:
        chat = getattr(model, "chat", None)
        if callable(chat):
            hook = chat
    if hook is None:
        return False
    try:
        result = hook(unclear)
    except TypeError:
        try:
            result = hook(
                [
                    {"item_id": entry["item_id"], "title": entry["title"]}
                    for entry in unclear
                ]
            )
        except Exception:  # noqa: BLE001
            return True
    except Exception:  # noqa: BLE001
        return True
    suggestions: dict[str, str] = {}
    if isinstance(result, dict):
        for key, val in result.items():
            text = str(val or "").strip()
            if text:
                suggestions[str(key)] = text[:200]
    elif isinstance(result, list):
        for entry, finding in zip(result, unclear):
            text = str(entry or "").strip()
            if text:
                suggestions[str(finding["item_id"])] = text[:200]
    for finding in unclear:
        better = suggestions.get(str(finding["item_id"]), "")
        if better:
            finding["fix"] = {
                "action": "rename",
                "item_id": finding["item_id"],
                "title": better,
            }
            finding["fix_label"] = f"Rename to {better}."
    return True


def format_board_triage_message(findings: list[dict[str, Any]]) -> str:
    """Render board plus RAID findings as one chat message."""
    if not findings:
        return "Triage: nothing needs attention.\n"
    count = len(findings)
    word = "item" if count == 1 else "items"
    lines = [f"{count} {word} need attention.", ""]
    for finding in findings:
        lines.append(
            f"- {finding.get('title', '')} [{finding.get('kind', '')}]: "
            f"{finding.get('detail', '')} Fix: {finding.get('fix_label', '')}"
        )
    return "\n".join(lines).rstrip() + "\n"


def _finding_write(findings: Any, writes: Any) -> list[int]:
    entries = findings if isinstance(findings, list) else [findings]
    ids: list[int] = []
    if writes is None:
        return ids
    for finding in entries:
        try:
            wid = writes.propose(
                {
                    "operation": f"triage_{finding.get('kind', 'fix')}",
                    "item_id": str(finding.get("item_id", "")),
                    "evidence_quote": (
                        str(finding.get("detail", ""))
                        or str(finding.get("title", ""))
                    )[:500],
                    "status": "proposed",
                    "detail": str(finding.get("fix_label", ""))[:500],
                }
            )
        except Exception:  # noqa: BLE001, S112
            continue
        ids.append(int(wid))
    return ids


def propose_finding_writes(
    findings: list[dict[str, Any]], writes: Any
) -> list[int]:
    """Log one proposed write per finding, returning the write ids."""
    return _finding_write(findings, writes)


def apply_finding(
    board: Any,
    finding: dict[str, Any],
    *,
    db: Any = None,
    backend: str = "azure_devops",
    project: str = "default",
    writes: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Apply one triage fix. Dry run proposes the write without changing data."""
    write_id = 0
    if writes is not None:
        ids = _finding_write(finding, writes)
        write_id = ids[0] if ids else 0
    if dry_run:
        return {
            "applied": False,
            "write_id": write_id,
            "detail": "Dry run is on. The fix was proposed but not applied.",
        }
    fix = dict(finding.get("fix") or {})
    action = str(fix.get("action", ""))
    item_id = str(finding.get("item_id", ""))
    try:
        if action == "comment":
            board.add_comment(
                item_id,
                str(fix.get("text", "")) or str(finding.get("fix_label", "")),
            )
        elif action in ("assign", "reassign"):
            owner = str(fix.get("owner", "")).strip()
            if not owner:
                return {
                    "applied": False,
                    "write_id": write_id,
                    "detail": "No suggested owner. Pick an owner and try again.",
                }
            board.update_item(item_id, {"assigned_to": owner, "owner": owner})
        elif action == "set_estimate":
            try:
                value = float(fix.get("estimate", DEFAULT_ESTIMATE))
            except (TypeError, ValueError):
                value = DEFAULT_ESTIMATE
            board.update_item(item_id, {"estimate": value})
        elif action == "reopen":
            board.update_item(item_id, {"state": str(fix.get("state", "New"))})
        elif action == "rename":
            title = str(fix.get("title", "")).strip()
            if not title:
                return {
                    "applied": False,
                    "write_id": write_id,
                    "detail": "No suggested title. Edit the title and try again.",
                }
            board.update_item(item_id, {"title": title})
        elif action == "raid_set_owner":
            if db is None:
                return {
                    "applied": False,
                    "write_id": write_id,
                    "detail": "The RAID store is missing.",
                }
            from pam_pm.core import raid as raid_mod

            owner = str(fix.get("owner", "")).strip()
            if not owner:
                return {
                    "applied": False,
                    "write_id": write_id,
                    "detail": "No suggested owner. Pick an owner and try again.",
                }
            raid_mod.update_item(
                db, backend, str(fix.get("external_id", item_id)), {"owner": owner}
            )
        elif action == "raid_extend_due":
            if db is None:
                return {
                    "applied": False,
                    "write_id": write_id,
                    "detail": "The RAID store is missing.",
                }
            from pam_pm.core import raid as raid_mod

            raid_mod.update_item(
                db,
                backend,
                str(fix.get("external_id", item_id)),
                {"due_date": str(fix.get("due_date", ""))},
            )
        else:
            return {
                "applied": False,
                "write_id": write_id,
                "detail": "Unknown fix. Nothing was applied.",
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "applied": False,
            "write_id": write_id,
            "detail": f"The fix could not be applied: {exc}",
        }
    if writes is not None and write_id:
        try:
            writes.apply(write_id)
        except Exception:  # noqa: BLE001, S110
            pass
    return {"applied": True, "write_id": write_id, "detail": "Applied."}


def run_triage(
    board: Any,
    db: Any = None,
    backend: str = "azure_devops",
    project: str = "default",
    chat: Any = None,
    model: Any = None,
    writes: Any = None,
    *,
    dry_run: bool = True,
    stale_days: int = DEFAULT_STALE_DAYS,
    overload_threshold: int = DEFAULT_OVERLOAD_THRESHOLD,
    roster: list[str] | None = None,
    lead: str = "",
    today: str = "",
    last_fingerprint: str | None = None,
    post: bool = True,
) -> dict[str, Any]:
    """Run one triage pass: plain scan, gated model call, one Slack post.

    The model runs only when the board changed since the last scan and a
    finding needs judgment. One Slack post goes out when a chat adapter
    and lead id are given and at least one item needs attention.
    """
    backend_name = str(backend or "azure_devops")
    project_name = str(project or "default")
    names = [str(entry).strip() for entry in (roster or []) if str(entry).strip()]
    findings = scan_board(
        board,
        stale_days=stale_days,
        overload_threshold=overload_threshold,
        roster=names,
        today=today,
    )
    if db is not None:
        findings.extend(
            scan_raid(db, backend_name, project_name, today=today, lead=str(lead))
        )
    fingerprint = board_fingerprint(list(board.list_items(limit=BOARD_LIMIT)))
    last = last_fingerprint
    if last is None and db is not None:
        try:
            last = get_last_fingerprint(db, backend_name, project_name)
        except Exception:  # noqa: BLE001
            last = None
    changed = last is None or last != fingerprint
    model_called = False
    if (
        model is not None
        and changed
        and any(entry.get("needs_judgment") for entry in findings)
    ):
        model_called = refine_with_model(model, findings)
    message = format_board_triage_message(findings)
    write_ids = propose_finding_writes(findings, writes)
    sent = False
    if chat is not None and post and findings and str(lead or ""):
        buttons = [
            {"id": f"triage-{index}", "label": "Approve", "value": f"approve:{index}"}
            for index in range(min(len(findings), MAX_SLACK_BUTTONS))
        ]
        try:
            sender = getattr(chat, "send_buttons", None)
            if callable(sender):
                sender(str(lead), message, buttons)
            else:
                chat.send_dm(str(lead), message)
            sent = True
        except Exception:  # noqa: BLE001
            sent = False
    if db is not None:
        try:
            save_last_fingerprint(db, backend_name, project_name, fingerprint)
        except Exception:  # noqa: BLE001, S110
            pass
    _ = dry_run
    return {
        "findings": findings,
        "message": message,
        "fingerprint": fingerprint,
        "changed": changed,
        "model_called": model_called,
        "sent": sent,
        "write_ids": write_ids,
        "count": len(findings),
    }


def schedule_triage(
    scheduler: Any,
    project: str,
    time: str = TRIAGE_SCHEDULE_TIME,
    tz: str = "UTC",
    days: str = "mon-fri",
    func: Any | None = None,
    *,
    db: Any | None = None,
    misfire_grace_hours: float | None = None,
) -> str:
    """Schedule the daily triage scan, persisting the job for catch-up."""
    name = (project or "default").strip() or "default"
    job_id = f"triage:{name}:{time}:{tz}:{days}"
    spec = {"project": name, "time": time, "tz": tz, "days": days}
    try:
        specs = getattr(scheduler, "_specs", None)
        if isinstance(specs, dict):
            specs[job_id] = dict(spec)
    except Exception:  # noqa: BLE001, S110
        pass
    target = db if db is not None else getattr(scheduler, "_db", None)
    if target is not None:
        try:
            from pam_pm.core.recovery import upsert_job

            grace = getattr(scheduler, "misfire_grace_hours", 3.0)
            if misfire_grace_hours is not None:
                grace = float(misfire_grace_hours)
            upsert_job(
                target,
                job_id,
                project=name,
                rule="triage",
                spec=spec,
                misfire_grace_hours=grace,
            )
        except Exception:  # noqa: BLE001, S110
            pass
    if func is None:
        return job_id
    try:
        from zoneinfo import ZoneInfo

        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        return job_id
    try:
        left, _, right = (time or TRIAGE_SCHEDULE_TIME).partition(":")
        hour = min(max(int(left.strip()), 0), 23)
        minute = min(max(int(right.strip()), 0), 59)
    except ValueError:
        hour, minute = 9, 0
    try:
        zone = ZoneInfo(tz or "UTC")
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    try:
        backend = scheduler._backend()
        trigger = CronTrigger(
            hour=hour, minute=minute, day_of_week=days, timezone=zone
        )
        existing = backend.get_job(job_id)
        if existing is not None:
            backend.remove_job(job_id)
        backend.add_job(func, trigger, id=job_id, args=[name])
    except Exception:  # noqa: BLE001, S110
        pass
    return job_id


def register_triage(engine: Any, handler: Any) -> Any:
    """Register a triage handler for schedule and board change triggers."""
    engine.register("schedule", handler)
    engine.register("ado_change", handler)
    return handler
