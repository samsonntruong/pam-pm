"""Next-sprint proposal (ADDONS item 4) on both backends.

Runs as a step after the sprint review, or on demand from the console.
Carryover keeps its owner, retro actions go first as first-class items,
and backlog fills remaining room in order without pulling an item
before its predecessor. Owner load uses real throughput from closed
items plus roster capacity; with no history it falls back to capacity
and estimates and says so.
"""

from __future__ import annotations

from typing import Any

from pam_pm.adapters.pm.base import is_closed

RETRO_TAG = "retro-action"
DEFAULT_ESTIMATE = 4.0
BOARD_LIMIT = 500

_OWNER_KEYS = ("assigned_to", "owner", "System.AssignedTo", "assignee")
_ESTIMATE_KEYS = (
    "remaining",
    "estimate",
    "Microsoft.VSTS.Scheduling.RemainingWork",
    "Microsoft.VSTS.Scheduling.OriginalEstimate",
)
_ITERATION_KEYS = ("iteration_id", "System.IterationPath")


def _item_id(item: Any) -> str:
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


def owner_of(item: Any) -> str:
    """Return the owner of a board item across both backends."""
    fields = _fields_of(item)
    for key in _OWNER_KEYS:
        value = fields.get(key)
        if isinstance(value, dict):
            for name in ("displayName", "uniqueName", "name", "id"):
                if value.get(name):
                    return str(value[name]).strip()
            continue
        if value and str(value).strip():
            return str(value).strip()
    return ""


def estimate_of(item: Any) -> float:
    """Return the hours for an item, defaulting to 4 when missing."""
    fields = _fields_of(item)
    for key in _ESTIMATE_KEYS:
        value = fields.get(key)
        if value is None or value == "":
            continue
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return DEFAULT_ESTIMATE


def iteration_of(item: Any) -> str:
    """Return the sprint id for an item, or empty when in the backlog."""
    fields = _fields_of(item)
    for key in _ITERATION_KEYS:
        value = fields.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_roster(roster: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in roster or []:
        if isinstance(entry, str):
            name = entry.strip()
            if name:
                out.append({"name": name, "capacity": 0.0})
            continue
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        try:
            capacity = float(str(entry.get("capacity", 0) or 0))
        except (TypeError, ValueError):
            capacity = 0.0
        out.append({"name": name, "capacity": max(0.0, capacity)})
    return out


def throughput_by_owner(board: Any, limit: int = BOARD_LIMIT) -> dict[str, float]:
    """Sum closed hours per owner as real throughput from past sprints."""
    if board is None:
        return {}
    try:
        items = list(board.list_items(limit=limit))
    except Exception:  # noqa: BLE001
        return {}
    throughput: dict[str, float] = {}
    for item in items:
        if not is_closed(_item_state(item)):
            continue
        owner = owner_of(item)
        if not owner:
            continue
        throughput[owner] = throughput.get(owner, 0.0) + estimate_of(item)
    return throughput


def has_history(board: Any, limit: int = BOARD_LIMIT) -> bool:
    """Return True when the board holds closed items to learn from."""
    if board is None:
        return False
    try:
        items = list(board.list_items(limit=limit))
    except Exception:  # noqa: BLE001
        return False
    return any(is_closed(_item_state(item)) for item in items)


def slip_reason(board: Any, item: Any, state_fallback: bool = True) -> str:
    """Explain why an item slipped: latest comment, else its state."""
    item_id = _item_id(item)
    try:
        comments = list(board.list_comments(item_id) or [])
    except Exception:  # noqa: BLE001
        comments = []
    if comments:
        last = comments[-1]
        text = last.get("text", "") if isinstance(last, dict) else getattr(last, "text", "")
        text = str(text or "").strip()
        if text:
            return text[:200]
    if state_fallback:
        state = _item_state(item).strip()
        if state:
            return f"Still {state} at review."
        return "Still open at review."
    return ""


def carryover_items(
    board: Any,
    current_iteration_id: str | None = None,
    limit: int = BOARD_LIMIT,
) -> list[Any]:
    """List open items in the current sprint (carryover candidates)."""
    if board is None:
        return []
    try:
        items = list(board.list_items(limit=limit))
    except Exception:  # noqa: BLE001
        return []
    wanted = str(current_iteration_id or "").strip()
    aliases = {wanted} if wanted else set()
    if wanted and board is not None:
        try:
            for entry in board.list_iterations() or []:
                ident = str(getattr(entry, "id", ""))
                label = str(getattr(entry, "name", ""))
                path = str(getattr(entry, "path", ""))
                if wanted in (ident, label, path):
                    aliases.update(part for part in (ident, label, path) if part)
        except Exception:  # noqa: BLE001, S110
            pass
    out = []
    for item in items:
        if is_closed(_item_state(item)):
            continue
        sprint = iteration_of(item)
        if wanted:
            if sprint in aliases:
                out.append(item)
        elif sprint:
            out.append(item)
    return out


def ordered_backlog(
    board: Any,
    order: list[str] | None = None,
    limit: int = BOARD_LIMIT,
) -> list[Any]:
    """List open backlog items (no sprint) in the given or board order."""
    if board is None:
        return []
    try:
        items = list(board.list_items(limit=limit))
    except Exception:  # noqa: BLE001
        return []
    open_backlog = [
        item
        for item in items
        if not is_closed(_item_state(item)) and not iteration_of(item)
    ]
    if not order:
        return open_backlog
    rank = {str(entry): index for index, entry in enumerate(order)}
    return sorted(
        open_backlog,
        key=lambda item: (rank.get(_item_id(item), len(rank)), _item_id(item)),
    )


def predecessor_map(board: Any, items: list[Any]) -> dict[str, list[str]]:
    """Map each item id to predecessor ids across both backends."""
    found: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for item in items or []:
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
            pred = target = ""
            if rel.endswith("Dependency-Forward") and src and tgt:
                pred, target = src, tgt
            elif rel.endswith("Dependency-Reverse") and src and tgt:
                pred, target = tgt, src
            if not pred or not target or (pred, target) in seen:
                continue
            seen.add((pred, target))
            found.setdefault(target, []).append(pred)
    return found


def _retro_draft(action: Any) -> dict[str, Any]:
    if isinstance(action, dict):
        title = str(action.get("title", "") or "").strip() or "Retro action"
        owner = str(action.get("owner", "") or "").strip()
        description = str(action.get("description", "") or title)
        estimate = action.get("estimate", DEFAULT_ESTIMATE)
    else:
        title = str(getattr(action, "title", "") or "").strip() or "Retro action"
        owner = str(getattr(action, "owner", "") or "").strip()
        description = str(getattr(action, "description", "") or title)
        estimate = getattr(action, "estimate", DEFAULT_ESTIMATE)
    try:
        hours = max(0.0, float(estimate))
    except (TypeError, ValueError):
        hours = DEFAULT_ESTIMATE
    return {"title": title, "owner": owner, "description": description, "estimate": hours}


def _effective_room(capacity: float, pace: float, used: float) -> float:
    room = max(capacity, pace) if (capacity > 0 or pace > 0) else 0.0
    return room - used


def propose_next_sprint(
    board: Any,
    roster: Any = None,
    backlog: list[Any] | None = None,
    retro_actions: list[Any] | None = None,
    current_iteration_id: str | None = None,
    slip_reasons: dict[str, str] | None = None,
    limit: int = BOARD_LIMIT,
) -> dict[str, Any]:
    """Propose the next sprint: retro first, carryover next, backlog last.

    Retro actions are first-class items placed first. Carryover keeps its
    owner with a slip reason per item. Backlog fills remaining room in
    the given order, skipping any item whose predecessor is neither
    closed nor already selected. Owners balance against throughput plus
    capacity; with no history the proposal falls back to capacity and
    estimates and says so in has_history.
    """
    people = _normalize_roster(roster)
    names = [entry["name"] for entry in people]
    capacity = {entry["name"]: entry["capacity"] for entry in people}
    pace = throughput_by_owner(board, limit=limit)
    history = has_history(board, limit=limit)
    slips = {str(key): str(val) for key, val in (slip_reasons or {}).items()}

    try:
        all_items = list(board.list_items(limit=limit)) if board is not None else []
    except Exception:  # noqa: BLE001
        all_items = []
    by_id = {_item_id(item): item for item in all_items}
    closed_ids = {
        _item_id(item) for item in all_items if is_closed(_item_state(item))
    }
    preds = predecessor_map(board, all_items) if board is not None else {}

    loads: dict[str, float] = {name: 0.0 for name in names}
    selected: set[str] = set()
    items: list[dict[str, Any]] = []
    deferred: list[str] = []

    def _assign(preferred: str, hours: float) -> tuple[str, str]:
        if preferred and preferred in loads:
            loads[preferred] += hours
            return preferred, f"Keep with {preferred}."
        if names:
            ordered = sorted(names, key=lambda label: (loads.get(label, 0.0), pace.get(label, 0.0), label))
            pick = ordered[0]
            loads[pick] += hours
            cap = capacity.get(pick, 0.0)
            speed = pace.get(pick, 0.0) if history else 0.0
            if history and speed > 0:
                return pick, f"Load balance, {pick} paced {speed:g}h before."
            if cap > 0:
                free = max(0.0, cap - (loads[pick] - hours))
                return pick, f"Load balance, {pick} has {free:g}h free."
            return pick, f"Load balance, assigned to {pick}."
        return "", "Needs an owner."

    for action in retro_actions or []:
        draft = _retro_draft(action)
        owner, why = _assign(draft["owner"], draft["estimate"])
        items.append(
            {
                "item_id": "",
                "title": draft["title"],
                "owner": owner,
                "reason": f"Retro action. {why}",
                "estimate": draft["estimate"],
                "kind": "retro",
                "description": draft["description"],
            }
        )

    for item in carryover_items(board, current_iteration_id, limit=limit):
        item_id = _item_id(item)
        hours = estimate_of(item)
        current = owner_of(item)
        if current and current not in loads:
            loads[current] = 0.0
            names.append(current)
            capacity.setdefault(current, 0.0)
        if current:
            loads[current] = loads.get(current, 0.0) + hours
            owner, why = current, f"Carryover, keep with {current}."
        else:
            owner, why = _assign("", hours)
            why = f"Carryover. {why}"
        reason_text = slips.get(item_id, "")
        if not reason_text and board is not None:
            reason_text = slip_reason(board, item)
        selected.add(item_id)
        items.append(
            {
                "item_id": item_id,
                "title": _item_title(item),
                "owner": owner,
                "reason": why,
                "estimate": hours,
                "kind": "carryover",
                "slip_reason": reason_text,
            }
        )

    ordered = list(backlog) if backlog is not None else ordered_backlog(board, limit=limit)
    for entry in ordered:
        item = by_id.get(_item_id(entry), entry) if not isinstance(entry, dict) else entry
        if isinstance(item, dict) and "title" in item and "item_id" not in item and "id" not in item:
            title = str(item.get("title", "") or "").strip() or "Backlog item"
            hours = DEFAULT_ESTIMATE
            try:
                hours = max(0.0, float(item.get("estimate", DEFAULT_ESTIMATE)))
            except (TypeError, ValueError):
                hours = DEFAULT_ESTIMATE
            owner, why = _assign(str(item.get("owner", "") or ""), hours)
            items.append(
                {
                    "item_id": "",
                    "title": title,
                    "owner": owner,
                    "reason": f"Backlog. {why}",
                    "estimate": hours,
                    "kind": "backlog",
                }
            )
            continue
        item_id = _item_id(item)
        if not item_id or item_id in selected:
            continue
        blocked_by = [
            pred
            for pred in preds.get(item_id, [])
            if pred not in closed_ids and pred not in selected
        ]
        if blocked_by:
            deferred.append(_item_title(item))
            continue
        hours = estimate_of(item)
        current = owner_of(item)
        if current and current not in loads:
            loads[current] = 0.0
            names.append(current)
            capacity.setdefault(current, 0.0)
        room_ok = True
        if current and (capacity.get(current, 0.0) > 0 or pace.get(current, 0.0) > 0):
            room = _effective_room(
                capacity.get(current, 0.0),
                pace.get(current, 0.0) if history else 0.0,
                loads.get(current, 0.0),
            )
            room_ok = room >= hours or all(
                _effective_room(
                    capacity.get(label, 0.0),
                    pace.get(label, 0.0) if history else 0.0,
                    loads.get(label, 0.0),
                )
                < hours
                for label in names
            )
        if current and room_ok:
            loads[current] = loads.get(current, 0.0) + hours
            owner, why = current, f"Backlog, keep with {current}."
        else:
            owner, why = _assign("", hours)
            why = f"Backlog. {why}"
        selected.add(item_id)
        items.append(
            {
                "item_id": item_id,
                "title": _item_title(item),
                "owner": owner,
                "reason": why,
                "estimate": hours,
                "kind": "backlog",
            }
        )

    risks: list[str] = []
    if not history:
        risks.append("No history yet, plan uses capacity and estimates.")
    over = sorted(
        label
        for label in loads
        if capacity.get(label, 0.0) > 0 and loads[label] > capacity[label]
    )
    if over:
        risks.append(f"Over capacity: {', '.join(over)}.")
    if deferred:
        shown = ", ".join(deferred[:3])
        more = "" if len(deferred) <= 3 else f" plus {len(deferred) - 3} more"
        risks.append(f"Held for dependencies: {shown}{more}.")
    if not risks:
        risks.append("Load fits capacity and dependencies clear.")
    return {
        "items": items,
        "loads": dict(loads),
        "throughput": dict(pace),
        "has_history": history,
        "risk_note": " ".join(risks),
        "deferred": deferred,
    }


def format_next_sprint_message(proposal: dict[str, Any]) -> str:
    """Render a proposal as one Slack and console message."""
    items = list((proposal or {}).get("items", []))
    if not items:
        return "Next sprint: no items proposed.\n"
    lines = [f"Next sprint: {len(items)} items proposed.", ""]
    for entry in items:
        title = str(entry.get("title", ""))
        owner = str(entry.get("owner", "") or "Unassigned")
        reason = str(entry.get("reason", ""))
        slip = str(entry.get("slip_reason", "") or "").strip()
        estimate = entry.get("estimate", DEFAULT_ESTIMATE)
        try:
            hours = f"{float(estimate):g}h"
        except (TypeError, ValueError):
            hours = f"{DEFAULT_ESTIMATE:g}h"
        lines.append(f"- {title} [{hours}, {owner}]: {reason}".rstrip())
        if slip:
            lines.append(f"  Slipped because: {slip}")
    lines.append("")
    loads = dict((proposal or {}).get("loads", {}))
    if loads:
        parts = [f"{name} {hours:g}h" for name, hours in sorted(loads.items())]
        lines.append(f"Load: {', '.join(parts)}.")
    risk = str((proposal or {}).get("risk_note", "") or "").strip()
    if risk:
        lines.append(f"Risk: {risk}")
    if (proposal or {}).get("has_history") is False:
        lines.append("Note: no history yet, plan uses capacity and estimates.")
    return "\n".join(lines).rstrip() + "\n"


def post_next_sprint(chat: Any, lead: str, proposal: dict[str, Any]) -> bool:
    """Post a proposal to the lead with an Approve button."""
    if chat is None or not str(lead or "").strip():
        return False
    message = format_next_sprint_message(proposal)
    try:
        sender = getattr(chat, "send_buttons", None)
        if callable(sender):
            sender(
                str(lead),
                message,
                [{"id": "next-sprint", "label": "Approve", "value": "approve"}],
            )
        else:
            chat.send_dm(str(lead), message)
        return True
    except Exception:  # noqa: BLE001
        return False


def _resolve_writes(writes: Any, db: Any, dry_run: bool) -> Any:
    if writes is not None:
        return writes
    if db is None:
        return None
    from pam_pm.core.writes import WriteService

    return WriteService(db, dry_run=dry_run)


def _open_state(board: Any) -> str:
    try:
        states = [str(entry) for entry in board.list_states() if str(entry)]
    except Exception:  # noqa: BLE001
        states = []
    return states[0] if states else "New"


def approve_next_sprint(
    board: Any,
    proposal: Any,
    iteration_name: str,
    *,
    writes: Any = None,
    db: Any = None,
    dry_run: bool = True,
    evidence_quote: str = "",
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    """Create the next iteration and move proposal items into it.

    Dry run proposes the writes without creating anything. Live run
    creates the iteration, creates retro action items, and moves every
    listed item into the new sprint. Returns the iteration plus moved
    and created items.
    """
    if board is None:
        raise ValueError("board is required to approve a sprint")
    name = str(iteration_name or "").strip()
    if not name:
        raise ValueError("iteration name is required")
    entries = (
        list(proposal.get("items", []))
        if isinstance(proposal, dict)
        else list(proposal or [])
    )
    service = _resolve_writes(writes, db, dry_run)
    quote = (evidence_quote or f"Approved next sprint: {name}.").strip()[:500]
    if dry_run:
        if service is not None:
            for entry in entries:
                title = str(entry.get("title", "") or "") if isinstance(entry, dict) else ""
                service.propose(
                    {
                        "operation": "plan_next_sprint",
                        "item_id": str(entry.get("item_id", "") or title)[:64] or name[:64],
                        "evidence_quote": quote or name[:500],
                        "status": "proposed",
                        "detail": f"proposed for {name}: {(title or name)[:200]}",
                    }
                )
        return {"iteration": None, "moved": [], "created": [], "applied": False}
    sprint = board.create_iteration(name, start_date=start_date, end_date=end_date)
    sprint_id = str(getattr(sprint, "id", ""))
    moved: list[Any] = []
    created: list[Any] = []
    state = _open_state(board)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("item_id", "") or "").strip()
        title = str(entry.get("title", "") or "").strip() or "Sprint item"
        owner = str(entry.get("owner", "") or "").strip()
        description = str(entry.get("description", "") or title)
        if not item_id:
            fields: dict[str, Any] = {
                "state": state,
                "description": description,
                "tags": [RETRO_TAG] if entry.get("kind") == "retro" else [],
            }
            if owner:
                fields["assigned_to"] = owner
            estimate = entry.get("estimate")
            try:
                fields["remaining"] = max(0.0, float(estimate))
            except (TypeError, ValueError):
                pass
            item = board.create_item(title, fields)
            item_id = _item_id(item)
            created.append(item)
        if sprint_id and getattr(board, "supports_iterations", True):
            setter = getattr(board, "set_item_iteration", None)
            if callable(setter):
                try:
                    updated = setter(item_id, sprint_id)
                    moved.append(updated)
                except Exception:  # noqa: BLE001, S110
                    pass
        if service is not None:
            service.propose(
                {
                    "operation": "plan_next_sprint",
                    "item_id": item_id or sprint_id,
                    "evidence_quote": quote or title[:500],
                    "status": "proposed",
                    "detail": f"moved to {name}: {title[:200]}",
                }
            )
    return {"iteration": sprint, "moved": moved, "created": created, "applied": True}
