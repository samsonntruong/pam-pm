"""Morning check-in template: DM each person with active items."""

from __future__ import annotations

from typing import Any

from pam_pm.agents.interviewer import Interviewer
from pam_pm.core.writes import WriteService

DONE_STATES = {"done", "closed", "removed", "resolved"}


def _state_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("state", "") or "")
    return str(getattr(item, "state", "") or "")


def _id_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id", item.get("item_id", "")) or "")
    return str(getattr(item, "id", getattr(item, "item_id", "")) or "")


def _owner_of(item: Any) -> str:
    fields: dict[str, Any] = {}
    if isinstance(item, dict):
        raw = item.get("fields", {})
        if isinstance(raw, dict):
            fields = raw
        for key in ("assigned_to", "owner", "user_id", "assignee"):
            value = fields.get(key) or item.get(key)
            if value:
                return str(value)
        return ""
    raw = getattr(item, "fields", None)
    if isinstance(raw, dict):
        fields = raw
    for key in ("assigned_to", "owner", "user_id", "assignee"):
        value = fields.get(key)
        if value:
            return str(value)
    return ""


def _is_active(item: Any) -> bool:
    return _state_of(item).strip().lower() not in DONE_STATES


def _resolve_writes(
    project: Any, writes: WriteService | None, db: Any, dry_run: bool
) -> WriteService | None:
    if writes is not None:
        return writes
    if isinstance(project, dict):
        cand = project.get("writes")
        if isinstance(cand, WriteService):
            return cand
        if db is None:
            db = project.get("db")
            dry = project.get("dry_run")
            if isinstance(dry, bool):
                dry_run = dry
    if db is not None:
        return WriteService(db, dry_run=dry_run)
    return None


def _resolve_roster(project: Any) -> list[str] | None:
    if not isinstance(project, dict):
        return None
    for key in ("roster", "people", "members", "users"):
        raw = project.get(key)
        if isinstance(raw, (list, tuple)):
            return [str(entry) for entry in raw if str(entry)]
    return None


def morning_checkin(
    project: Any,
    chat: Any,
    board: Any,
    model: Any,
    *,
    writes: WriteService | None = None,
    db: Any = None,
    dry_run: bool = True,
    interviewer: Interviewer | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """DM each person with active items and log results as writes.

    People with nothing active are skipped. Uses Interviewer per person
    and proposes one write per result through WriteService.
    """
    service = _resolve_writes(project, writes, db, dry_run)
    items = list(board.list_items(limit=limit))
    active = [item for item in items if _is_active(item)]
    if not active:
        return {}
    roster = _resolve_roster(project)
    by_person: dict[str, list[Any]] = {}
    for item in active:
        owner = _owner_of(item)
        if not owner:
            continue
        if roster is not None and owner not in roster:
            continue
        by_person.setdefault(owner, []).append(item)
    if roster is not None and not by_person:
        return {}
    if roster is None and not by_person:
        return {}
    agent = interviewer or Interviewer()
    out: dict[str, Any] = {}
    for person, owned in by_person.items():
        raw = agent.run(person, owned, chat, board, model)
        if isinstance(raw, dict):
            results = dict(raw)
        else:
            first_id = _id_of(owned[0]) if owned else "item"
            results = {first_id: raw}
        out[person] = results
        if service is not None:
            for item_id, result in results.items():
                if isinstance(result, dict):
                    quote = str(result.get("evidence_quote", "") or "")
                    status = str(result.get("status", "") or "")
                else:
                    quote = str(getattr(result, "evidence_quote", "") or "")
                    status = str(getattr(result, "status", "") or "")
                service.propose(
                    {
                        "operation": "update_state",
                        "item_id": str(item_id),
                        "evidence_quote": quote,
                        "status": "proposed",
                        "detail": status[:500],
                    }
                )
    return out


def run_morning_checkin(
    project: Any = None,
    chat: Any = None,
    board: Any = None,
    model: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Alias for morning_checkin kept for older imports."""
    if project is None or chat is None or board is None or model is None:
        return {}
    return morning_checkin(project, chat, board, model, **kwargs)
