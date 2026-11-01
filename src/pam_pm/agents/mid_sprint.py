"""Mid-sprint adjustment (ADDONS item 6) on both backends.

Triggered by the rules engine (a schedule check or a board change) when
a sprint is off track: slippage from a stalled blocker or a person
falling behind, urgent scope added, or surplus capacity. Pam proposes
changes (move out, swap, reassign, pull from backlog) with the effect
on the sprint goal. Approve buttons only; the sprint never changes on
its own. Proposal helpers and the approve pattern reuse the next-sprint
module; per-change Approve buttons reuse the triage card pattern.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pam_pm.adapters.pm.base import is_closed
from pam_pm.agents import next_sprint as ns_mod

STALLED_DAYS = 3
FALLING_BEHIND_COUNT = 4
SURPLUS_RATIO = 0.5
SURPLUS_FREE_HOURS = 4.0
BOARD_LIMIT = 500
MAX_SLACK_BUTTONS = 10
DEFAULT_GOAL = "Finish the planned sprint scope."

MID_SPRINT_SCHEDULE_DEFAULT = "daily"
MID_SPRINT_SCHEDULE_TIME = "09:30"

TRIGGER_STALLED = "stalled_blocker"
TRIGGER_BEHIND = "falling_behind"
TRIGGER_URGENT = "urgent_scope"
TRIGGER_SURPLUS = "surplus_capacity"

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

_PRIORITY_KEYS = (
    "priority",
    "Priority",
    "Microsoft.VSTS.Common.Priority",
)
_TAGS_KEYS = ("tags", "System.Tags")

_URGENT_PRIORITIES = {"1", "high", "urgent", "critical", "p0", "p1"}


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


def _parse_day(text: str) -> date | None:
    match = _DATE_RE.match((text or "").strip())
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def age_days(changed: str, today: str = "") -> int:
    """Return whole days since a stamp, or 0 when unknown."""
    day = _parse_day(changed)
    if day is None:
        return 0
    now = _parse_day(today) or date.today()
    return max(0, (now - day).days)


def _is_blocked(item: Any) -> bool:
    return _item_state(item).strip().lower() == "blocked"


def _is_urgent(item: Any) -> bool:
    fields = _fields_of(item)
    for key in _PRIORITY_KEYS:
        value = fields.get(key)
        if value is not None and str(value).strip().lower() in _URGENT_PRIORITIES:
            return True
    for key in _TAGS_KEYS:
        value = fields.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            tags = [str(entry).strip().lower() for entry in value]
        else:
            tags = [
                part.strip().lower()
                for part in str(value).replace(";", ",").split(",")
            ]
        if "urgent" in tags:
            return True
    return False


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


def _sprint_items(
    board: Any,
    current_iteration_id: str | None,
    limit: int = BOARD_LIMIT,
) -> list[Any]:
    """Return open items in the current sprint via next-sprint code."""
    try:
        return ns_mod.carryover_items(board, current_iteration_id, limit=limit)
    except Exception:  # noqa: BLE001
        return []


def _least_loaded(loads: dict[str, float], names: list[str]) -> str:
    if not names:
        return ""
    return min(names, key=lambda label: (loads.get(label, 0.0), label))


def detect_off_track(
    board: Any,
    roster: Any = None,
    current_iteration_id: str | None = None,
    *,
    today: str = "",
    stale_days: int = STALLED_DAYS,
    overload_threshold: int = FALLING_BEHIND_COUNT,
    limit: int = BOARD_LIMIT,
) -> list[dict[str, Any]]:
    """Detect off track signals with plain code. No model calls here."""
    people = _normalize_roster(roster)
    capacity = {entry["name"]: entry["capacity"] for entry in people}
    names = [entry["name"] for entry in people]
    sprint = _sprint_items(board, current_iteration_id, limit=limit)
    triggers: list[dict[str, Any]] = []

    try:
        all_items = list(board.list_items(limit=limit)) if board is not None else []
    except Exception:  # noqa: BLE001
        all_items = []
    preds = ns_mod.predecessor_map(board, all_items) if board is not None else {}
    closed_ids = {
        _item_id(item) for item in all_items if is_closed(_item_state(item))
    }

    for item in sprint:
        item_id = _item_id(item)
        title = _item_title(item)
        age = age_days(_item_changed(item), today)
        if _is_blocked(item) and age >= max(0, int(stale_days)):
            triggers.append(
                {
                    "kind": TRIGGER_STALLED,
                    "item_id": item_id,
                    "title": title,
                    "detail": f"Blocked with no change in {age} days.",
                    "evidence": f"{title} is Blocked, last change {age} days ago.",
                }
            )
            continue
        open_preds = [
            pred
            for pred in preds.get(item_id, [])
            if pred not in closed_ids
        ]
        if open_preds and age >= max(0, int(stale_days)):
            triggers.append(
                {
                    "kind": TRIGGER_STALLED,
                    "item_id": item_id,
                    "title": title,
                    "detail": (
                        f"Waiting on {len(open_preds)} open predecessor "
                        f"with no change in {age} days."
                    ),
                    "evidence": f"{title} waits on {open_preds[0]}.",
                }
            )

    load_hours: dict[str, float] = {}
    load_count: dict[str, int] = {}
    for item in sprint:
        owner = ns_mod.owner_of(item)
        if not owner:
            continue
        load_hours[owner] = load_hours.get(owner, 0.0) + ns_mod.estimate_of(item)
        load_count[owner] = load_count.get(owner, 0) + 1
    for person in sorted(load_hours):
        hours = load_hours[person]
        count = load_count.get(person, 0)
        cap = capacity.get(person, 0.0)
        over_hours = cap > 0 and hours > cap
        over_count = count > max(0, int(overload_threshold))
        if over_hours or over_count:
            if over_hours:
                detail = f"{person} holds {hours:g}h of sprint work over {cap:g}h capacity."
            else:
                detail = f"{person} holds {count} open sprint items."
            triggers.append(
                {
                    "kind": TRIGGER_BEHIND,
                    "item_id": person,
                    "title": person,
                    "detail": detail,
                    "evidence": detail,
                }
            )

    urgent = [item for item in sprint if _is_urgent(item)]
    for item in urgent:
        title = _item_title(item)
        triggers.append(
            {
                "kind": TRIGGER_URGENT,
                "item_id": _item_id(item),
                "title": title,
                "detail": f"Urgent scope in the sprint: {title}.",
                "evidence": f"{title} is marked urgent in the sprint.",
            }
        )

    backlog = ns_mod.ordered_backlog(board, limit=limit)
    open_backlog = [item for item in backlog if not _is_urgent(item)]
    total_load = sum(
        ns_mod.estimate_of(item) for item in sprint
    )
    total_cap = sum(capacity.get(name, 0.0) for name in names)
    free_room = any(
        capacity.get(name, 0.0) - load_hours.get(name, 0.0) >= SURPLUS_FREE_HOURS
        for name in names
        if capacity.get(name, 0.0) > 0
    )
    if open_backlog and (
        (total_cap > 0 and total_load < total_cap * SURPLUS_RATIO) or free_room
    ):
        triggers.append(
            {
                "kind": TRIGGER_SURPLUS,
                "item_id": _item_id(open_backlog[0]),
                "title": _item_title(open_backlog[0]),
                "detail": (
                    f"Sprint load is {total_load:g}h against {total_cap:g}h "
                    "capacity with backlog waiting."
                    if total_cap > 0
                    else "Someone has room with backlog waiting."
                ),
                "evidence": f"{len(open_backlog)} backlog items wait.",
            }
        )
    return triggers


def propose_adjustment(
    board: Any,
    roster: Any = None,
    current_iteration_id: str | None = None,
    *,
    goal: str = "",
    today: str = "",
    stale_days: int = STALLED_DAYS,
    overload_threshold: int = FALLING_BEHIND_COUNT,
    limit: int = BOARD_LIMIT,
) -> dict[str, Any]:
    """Propose mid-sprint changes, each with its goal effect.

    Move out frees a stalled item, reassign balances a person who is
    falling behind, swap makes room for urgent scope, and pull uses
    surplus capacity. Nothing is applied here; Approve applies.
    """
    want_goal = (goal or "").strip() or DEFAULT_GOAL
    triggers = detect_off_track(
        board,
        roster,
        current_iteration_id,
        today=today,
        stale_days=stale_days,
        overload_threshold=overload_threshold,
        limit=limit,
    )
    sprint = _sprint_items(board, current_iteration_id, limit=limit)
    by_id = {_item_id(item): item for item in sprint}
    backlog = ns_mod.ordered_backlog(board, limit=limit)
    people = _normalize_roster(roster)
    names = [entry["name"] for entry in people]
    loads: dict[str, float] = {name: 0.0 for name in names}
    for item in sprint:
        owner = ns_mod.owner_of(item)
        if owner:
            loads[owner] = loads.get(owner, 0.0) + ns_mod.estimate_of(item)

    changes: list[dict[str, Any]] = []
    used_out: set[str] = set()
    used_in: set[str] = set()

    def _move_out(item_id: str, reason: str, evidence: str) -> None:
        if item_id in used_out or item_id not in by_id:
            return
        used_out.add(item_id)
        hours = ns_mod.estimate_of(by_id[item_id])
        changes.append(
            {
                "action": "move_out",
                "item_id": item_id,
                "title": _item_title(by_id[item_id]),
                "owner": ns_mod.owner_of(by_id[item_id]),
                "target_owner": "",
                "estimate": hours,
                "reason": reason,
                "evidence": evidence,
                "goal_effect": (
                    f"Protects the goal by freeing {hours:g}h "
                    "while the blocker clears."
                ),
            }
        )

    for trig in triggers:
        if trig["kind"] == TRIGGER_STALLED:
            _move_out(
                str(trig["item_id"]),
                "Move out until the blocker clears, then pull back.",
                str(trig["evidence"]),
            )
        elif trig["kind"] == TRIGGER_BEHIND:
            person = str(trig["item_id"])
            owned = [
                item
                for item in sprint
                if ns_mod.owner_of(item) == person
                and _item_id(item) not in used_out
            ]
            if not owned:
                continue
            owned.sort(key=lambda entry: ns_mod.estimate_of(entry), reverse=True)
            candidate = owned[0]
            others = [label for label in names if label != person]
            if not others:
                others = [
                    label for label in loads if label != person
                ]
            target = _least_loaded(loads, others)
            hours = ns_mod.estimate_of(candidate)
            if target:
                loads[person] = loads.get(person, 0.0) - hours
                loads[target] = loads.get(target, 0.0) + hours
                changes.append(
                    {
                        "action": "reassign",
                        "item_id": _item_id(candidate),
                        "title": _item_title(candidate),
                        "owner": person,
                        "target_owner": target,
                        "estimate": hours,
                        "reason": f"Reassign to {target}, who has room.",
                        "evidence": str(trig["evidence"]),
                        "goal_effect": (
                            "Protects the goal by balancing load "
                            f"so {person} can finish the rest."
                        ),
                    }
                )
            else:
                _move_out(
                    _item_id(candidate),
                    "Move out the largest item; nobody has room to take it.",
                    str(trig["evidence"]),
                )
        elif trig["kind"] == TRIGGER_URGENT:
            urgent_id = str(trig["item_id"])
            plain = [
                item
                for item in sprint
                if _item_id(item) != urgent_id
                and _item_id(item) not in used_out
                and not _is_urgent(item)
            ]
            if plain:
                plain.sort(key=lambda entry: ns_mod.estimate_of(entry))
                victim = plain[0]
                used_out.add(_item_id(victim))
                hours = ns_mod.estimate_of(victim)
                changes.append(
                    {
                        "action": "swap",
                        "item_id": _item_id(victim),
                        "title": _item_title(victim),
                        "owner": ns_mod.owner_of(victim),
                        "target_owner": "",
                        "in_item_id": urgent_id,
                        "in_title": str(trig["title"]),
                        "estimate": hours,
                        "reason": (
                            f"Move out {_item_title(victim)} to make "
                            f"room for urgent {trig['title']}."
                        ),
                        "evidence": str(trig["evidence"]),
                        "goal_effect": (
                            "Keeps the goal intact: urgent scope lands "
                            "without overloading the sprint."
                        ),
                    }
                )
        elif trig["kind"] == TRIGGER_SURPLUS:
            for item in backlog:
                item_id = _item_id(item)
                if not item_id or item_id in used_in:
                    continue
                used_in.add(item_id)
                hours = ns_mod.estimate_of(item)
                target = _least_loaded(loads, names)
                if target:
                    loads[target] = loads.get(target, 0.0) + hours
                changes.append(
                    {
                        "action": "pull",
                        "item_id": item_id,
                        "title": _item_title(item),
                        "owner": ns_mod.owner_of(item),
                        "target_owner": target,
                        "estimate": hours,
                        "reason": (
                            "Pull from the backlog to use free capacity"
                            + (f" ({target} takes it)." if target else ".")
                        ),
                        "evidence": str(trig["evidence"]),
                        "goal_effect": (
                            "Advances the goal by using surplus capacity "
                            "instead of leaving it idle."
                        ),
                    }
                )
                break

    if changes:
        effect = (
            f"{len(changes)} change{'s' if len(changes) != 1 else ''} "
            f"proposed to protect the goal: {want_goal}"
        )
    else:
        effect = f"The sprint is on track for the goal: {want_goal}"
    return {
        "goal": want_goal,
        "triggers": triggers,
        "changes": changes,
        "goal_effect": effect,
    }


def format_mid_sprint_message(proposal: dict[str, Any]) -> str:
    """Render a proposal as one Slack and console message."""
    changes = list((proposal or {}).get("changes", []))
    goal = str((proposal or {}).get("goal", "") or DEFAULT_GOAL)
    if not changes:
        return f"Mid-sprint: the sprint is on track.\nGoal: {goal}\n"
    lines = [f"Mid-sprint: {len(changes)} changes proposed.", f"Goal: {goal}", ""]
    labels = {
        "move_out": "Move out",
        "swap": "Swap",
        "reassign": "Reassign",
        "pull": "Pull",
    }
    for entry in changes:
        action = labels.get(str(entry.get("action", "")), "Change")
        title = str(entry.get("title", ""))
        reason = str(entry.get("reason", ""))
        effect = str(entry.get("goal_effect", ""))
        try:
            hours = f"{float(entry.get('estimate', 0.0)):g}h"
        except (TypeError, ValueError):
            hours = ""
        head = f"- {action}: {title}"
        if hours:
            head += f" [{hours}]"
        lines.append(f"{head}: {reason}".rstrip())
        if effect:
            lines.append(f"  Goal effect: {effect}")
    lines.append("")
    lines.append(str((proposal or {}).get("goal_effect", "") or "").strip())
    return "\n".join(line for line in lines if line is not None).rstrip() + "\n"


def post_mid_sprint(chat: Any, lead: str, proposal: dict[str, Any]) -> bool:
    """Post a proposal to the lead with Approve buttons."""
    if chat is None or not str(lead or "").strip():
        return False
    changes = list((proposal or {}).get("changes", []))
    if not changes:
        return False
    message = format_mid_sprint_message(proposal)
    buttons = [
        {"id": f"mid-sprint-{index}", "label": "Approve", "value": f"approve:{index}"}
        for index in range(min(len(changes), MAX_SLACK_BUTTONS))
    ]
    try:
        sender = getattr(chat, "send_buttons", None)
        if callable(sender):
            sender(str(lead), message, buttons)
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


def _clear_iteration(board: Any, item_id: str) -> None:
    """Move one item back to the backlog on either backend."""
    try:
        board.set_item_iteration(item_id, "")
        return
    except Exception:  # noqa: BLE001, S110
        pass
    board.update_item(item_id, {"iteration_id": ""})


def apply_change(
    board: Any,
    change: dict[str, Any],
    current_iteration_id: str | None,
    *,
    writes: Any = None,
    db: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Apply one approved change. Dry run proposes only, never applies."""
    service = _resolve_writes(writes, db, dry_run)
    action = str((change or {}).get("action", ""))
    item_id = str((change or {}).get("item_id", ""))
    title = str((change or {}).get("title", "") or item_id)
    evidence = (
        str((change or {}).get("evidence", ""))
        or str((change or {}).get("reason", ""))
        or title
    )[:500]
    write_id = 0
    if service is not None:
        try:
            write_id = int(
                service.propose(
                    {
                        "operation": f"mid_sprint_{action or 'change'}",
                        "item_id": item_id or title[:64],
                        "evidence_quote": evidence,
                        "status": "proposed",
                        "detail": str((change or {}).get("reason", ""))[:500],
                    }
                )
            )
        except Exception:  # noqa: BLE001, S110
            pass
    if dry_run:
        return {
            "applied": False,
            "write_id": write_id,
            "detail": "Dry run is on. The change was proposed but not applied.",
        }
    if board is None:
        return {
            "applied": False,
            "write_id": write_id,
            "detail": "The board is missing.",
        }
    try:
        if action == "move_out":
            _clear_iteration(board, item_id)
        elif action == "pull":
            sprint = str(current_iteration_id or "").strip()
            if not sprint:
                return {
                    "applied": False,
                    "write_id": write_id,
                    "detail": "No current sprint. Pick a sprint and try again.",
                }
            target = str((change or {}).get("target_owner", "") or "").strip()
            if target:
                board.update_item(item_id, {"assigned_to": target, "owner": target})
            board.set_item_iteration(item_id, sprint)
        elif action == "reassign":
            target = str((change or {}).get("target_owner", "") or "").strip()
            if not target:
                return {
                    "applied": False,
                    "write_id": write_id,
                    "detail": "No suggested owner. Pick an owner and try again.",
                }
            board.update_item(item_id, {"assigned_to": target, "owner": target})
        elif action == "swap":
            in_id = str((change or {}).get("in_item_id", "") or "").strip()
            _clear_iteration(board, item_id)
            if in_id:
                sprint = str(current_iteration_id or "").strip()
                if sprint:
                    try:
                        board.set_item_iteration(in_id, sprint)
                    except Exception:  # noqa: BLE001, S110
                        pass
        else:
            return {
                "applied": False,
                "write_id": write_id,
                "detail": "Unknown change. Nothing was applied.",
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "applied": False,
            "write_id": write_id,
            "detail": f"The change could not be applied: {exc}",
        }
    if service is not None and write_id:
        try:
            service.apply(write_id)
        except Exception:  # noqa: BLE001, S110
            pass
    return {"applied": True, "write_id": write_id, "detail": "Applied."}


def run_mid_sprint(
    board: Any,
    roster: Any = None,
    current_iteration_id: str | None = None,
    *,
    goal: str = "",
    chat: Any = None,
    lead: str = "",
    writes: Any = None,
    db: Any = None,
    dry_run: bool = True,
    today: str = "",
    post: bool = True,
) -> dict[str, Any]:
    """Detect, propose, and optionally post. Never applies on its own."""
    proposal = propose_adjustment(
        board, roster, current_iteration_id, goal=goal, today=today
    )
    service = _resolve_writes(writes, db, dry_run)
    write_ids: list[int] = []
    if service is not None:
        for change in proposal["changes"]:
            try:
                write_ids.append(
                    int(
                        service.propose(
                            {
                                "operation": f"mid_sprint_{change.get('action', 'change')}",
                                "item_id": str(change.get("item_id", "")),
                                "evidence_quote": str(
                                    change.get("evidence", "")
                                    or change.get("reason", "")
                                )[:500],
                                "status": "proposed",
                                "detail": str(change.get("reason", ""))[:500],
                            }
                        )
                    )
                )
            except Exception:  # noqa: BLE001, S112
                continue
    message = format_mid_sprint_message(proposal)
    sent = False
    if chat is not None and post and proposal["changes"] and str(lead or ""):
        sent = post_mid_sprint(chat, str(lead), proposal)
    return {
        "proposal": proposal,
        "triggers": proposal["triggers"],
        "changes": proposal["changes"],
        "message": message,
        "sent": sent,
        "write_ids": write_ids,
        "count": len(proposal["changes"]),
    }


def register_mid_sprint(engine: Any, handler: Any) -> Any:
    """Register a mid-sprint handler for schedule and board change."""
    engine.register("schedule", handler)
    engine.register("ado_change", handler)
    return handler


def schedule_mid_sprint(
    scheduler: Any,
    project: str,
    time: str = MID_SPRINT_SCHEDULE_TIME,
    tz: str = "UTC",
    days: str = "mon-fri",
    func: Any | None = None,
    *,
    db: Any | None = None,
    misfire_grace_hours: float | None = None,
) -> str:
    """Schedule the mid-sprint check, persisting the job for catch-up."""
    name = (project or "default").strip() or "default"
    job_id = f"mid-sprint:{name}:{time}:{tz}:{days}"
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
                rule="mid_sprint",
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
        left, _, right = (time or MID_SPRINT_SCHEDULE_TIME).partition(":")
        hour = min(max(int(left.strip()), 0), 23)
        minute = min(max(int(right.strip()), 0), 59)
    except ValueError:
        hour, minute = 9, 30
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
