"""Morning check-in template: DM each person with active items."""

from __future__ import annotations

from typing import Any

from pam_pm.adapters.pm.base import is_closed
from pam_pm.agents.interviewer import Interviewer
from pam_pm.core.writes import WriteService

OTHER_CLOSED = {"removed", "resolved"}


def close_via_adapter(board: Any, item_id: str) -> Any:
    """Close one item through the adapter mapped state when available."""
    close_item = getattr(board, "close_item", None)
    if callable(close_item):
        return close_item(item_id)
    return board.update_item(item_id, {"state": "Closed"})


def _state_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("state", "") or "")
    return str(getattr(item, "state", "") or "")


def _id_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id", item.get("item_id", "")) or "")
    return str(getattr(item, "id", getattr(item, "item_id", "")) or "")


OWNER_KEYS = (
    "assigned_to",
    "owner",
    "System.AssignedTo",
    "user_id",
    "assignee",
)


def _owner_text(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, dict):
        for key in ("displayName", "uniqueName", "name", "id"):
            cand = value.get(key)
            if cand:
                return str(cand)
        return ""
    return str(value)


def _owner_of(item: Any) -> str:
    fields: dict[str, Any] = {}
    if isinstance(item, dict):
        raw = item.get("fields", {})
        if isinstance(raw, dict):
            fields = raw
        for key in OWNER_KEYS:
            value = _owner_text(fields.get(key)) or _owner_text(item.get(key))
            if value:
                return value
        return ""
    raw = getattr(item, "fields", None)
    if isinstance(raw, dict):
        fields = raw
    for key in OWNER_KEYS:
        value = _owner_text(fields.get(key))
        if value:
            return value
    return ""


def _is_active(item: Any) -> bool:
    state = _state_of(item)
    if is_closed(state):
        return False
    return state.strip().lower() not in OTHER_CLOSED


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


def _raid_context(
    project: Any, db: Any, backend: str
) -> tuple[Any, str, str]:
    """Resolve the RAID store, backend label, and project name."""
    name = "default"
    store = db
    label = str(backend or "azure_devops")
    if isinstance(project, dict):
        name = str(
            project.get("project", project.get("name", "default")) or "default"
        )
        if store is None:
            store = project.get("db")
        label = str(project.get("backend", label) or label)
    elif isinstance(project, str) and project.strip():
        name = project.strip()
    return store, label, name


def _apply_raid_hooks(
    project: Any,
    chat: Any,
    service: WriteService | None,
    db: Any,
    backend: str,
    by_person: dict[str, list[Any]],
    out: dict[str, Any],
) -> None:
    """Propose Risks from check-in text and nudge owners about RAID."""
    store, label, name = _raid_context(project, db, backend)
    if store is None:
        return
    try:
        from pam_pm.core import raid as raid_mod
    except Exception:  # noqa: BLE001
        return
    for person, results in out.items():
        if not isinstance(results, dict):
            continue
        for result in results.values():
            if isinstance(result, dict):
                text = str(result.get("evidence_quote", "") or "") + " " + str(
                    result.get("status", "") or ""
                )
            else:
                text = str(getattr(result, "evidence_quote", "") or "") + " " + str(
                    getattr(result, "status", "") or ""
                )
            try:
                raid_mod.propose_risk_from_checkin(
                    store, label, name, text, person, writes=service
                )
            except Exception:  # noqa: BLE001, S110
                pass
    for person in by_person:
        try:
            lines = raid_mod.nudge_lines(store, label, name, person)
        except Exception:  # noqa: BLE001
            lines = []
        if not lines:
            continue
        body = "RAID for today:\n" + "\n".join(f"- {line}" for line in lines)
        try:
            chat.send_dm(person, body)
        except Exception:  # noqa: BLE001, S110
            pass


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
    backend: str = "azure_devops",
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
    run_db = db
    if run_db is None and isinstance(project, dict):
        run_db = project.get("db")
    out: dict[str, Any] = {}
    for person, owned in by_person.items():
        raw = agent.run(person, owned, chat, board, model, db=run_db)
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
    _apply_raid_hooks(project, chat, service, db, backend, by_person, out)
    return out


def test_checkin(
    person: str,
    chat: Any,
    board: Any,
    model: Any,
    *,
    project: Any = None,
    writes: WriteService | None = None,
    db: Any = None,
    dry_run: bool = True,
    interviewer: Interviewer | None = None,
    limit: int = 200,
    backend: str = "azure_devops",
) -> dict[str, Any]:
    """Run one morning check-in to a single chosen teammate.

    Same path as morning_checkin with the roster narrowed to one
    person, so the console test button exercises the real flow.
    """
    label = str(person or "").strip()
    if not label:
        return {}
    if isinstance(project, dict):
        scoped: Any = dict(project)
        scoped["roster"] = [label]
    elif isinstance(project, str) and project.strip():
        scoped = {"project": project.strip(), "roster": [label]}
    else:
        scoped = {"roster": [label]}
    return morning_checkin(
        scoped,
        chat,
        board,
        model,
        writes=writes,
        db=db,
        dry_run=dry_run,
        interviewer=interviewer,
        limit=limit,
        backend=backend,
    )


run_test_checkin = test_checkin


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
