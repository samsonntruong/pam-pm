"""Blocker intake template: ask, link, request ETA, notify on clear."""

from __future__ import annotations

from typing import Any

from pam_pm.adapters.pm.base import is_closed
from pam_pm.core.schemas import BlockerInfo
from pam_pm.core.writes import WriteService

WHAT_Q = "What is blocked?"
WHO_Q = "Who or what blocks it?"
IMPACT_Q = "What is the impact?"

FORWARD_REL = "System.LinkTypes.Dependency-Forward"


def close_via_adapter(board: Any, item_id: str) -> Any:
    """Close one item through the adapter mapped state when available."""
    close_item = getattr(board, "close_item", None)
    if callable(close_item):
        return close_item(item_id)
    return board.update_item(item_id, {"state": "Closed"})


def _text_of(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("text", "") or "")
    return str(getattr(entry, "text", entry) or "")


def _item_id(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("id", item.get("item_id", "")) or "")
    return str(getattr(item, "id", getattr(item, "item_id", "")) or "")


def _item_title(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("title", "") or _item_id(item))
    return str(getattr(item, "title", "") or _item_id(item))


def _item_state(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("state", "") or "")
    return str(getattr(item, "state", "") or "")


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
    if isinstance(item, dict):
        fields = item.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}
        for key in OWNER_KEYS:
            value = _owner_text(fields.get(key)) or _owner_text(item.get(key))
            if value:
                return value
        return ""
    fields = getattr(item, "fields", None)
    if not isinstance(fields, dict):
        fields = {}
    for key in OWNER_KEYS:
        value = _owner_text(fields.get(key))
        if value:
            return value
    return ""


def _board_states(board: Any) -> list[str]:
    try:
        states = list(board.list_states())
    except Exception:  # noqa: BLE001
        return []
    return [str(entry) for entry in states if str(entry)]


def _open_state(board: Any) -> str:
    states = _board_states(board)
    if states:
        return states[0]
    return "To Do"


def _active_state(board: Any) -> str:
    states = _board_states(board)
    if len(states) >= 2:
        return states[1]
    if states:
        return states[0]
    return "Active"


def _iteration_of(item: Any) -> str:
    fields: dict[str, Any] = {}
    if isinstance(item, dict):
        raw = item.get("fields", {})
        if isinstance(raw, dict):
            fields = raw
        for key in (
            "iteration_id",
            "iteration",
            "System.IterationId",
            "System.IterationPath",
        ):
            value = fields.get(key) or item.get(key)
            if value:
                return str(value)
        return ""
    raw = getattr(item, "fields", None)
    if isinstance(raw, dict):
        fields = raw
    for key in (
        "iteration_id",
        "iteration",
        "System.IterationId",
        "System.IterationPath",
    ):
        value = fields.get(key)
        if value:
            return str(value)
    return ""


def _maybe_copy_iteration(board: Any, blocked: Any, blocking_id: str) -> None:
    if not getattr(board, "supports_iterations", True):
        return
    iteration = _iteration_of(blocked)
    if not iteration:
        return
    setter = getattr(board, "set_item_iteration", None)
    if not callable(setter):
        return
    try:
        setter(blocking_id, iteration)
    except Exception:  # noqa: BLE001
        return


def _collect_answers(chat: Any, reporter: str) -> list[str]:
    try:
        replies = chat.read_replies(reporter, None)
    except TypeError:
        try:
            replies = chat.read_replies(reporter)
        except Exception:  # noqa: BLE001
            return []
    except Exception:  # noqa: BLE001
        return []
    out = [_text_of(entry).strip() for entry in (replies or [])]
    return [text for text in out if text]


def blocker_intake(
    reporter: str,
    chat: Any,
    board: Any,
    writes: WriteService | None = None,
    *,
    db: Any = None,
    dry_run: bool = True,
    blocked_item_id: str | None = None,
    blocking_owner: str | None = None,
    blocking_title: str | None = None,
    model: Any = None,
    project: str = "default",
    backend: str = "azure_devops",
) -> dict[str, Any]:
    """Run blocker intake and return the linked items and evidence."""
    _ = model
    service = writes
    if service is None and db is not None:
        service = WriteService(db, dry_run=dry_run)
    for question in (WHAT_Q, WHO_Q, IMPACT_Q):
        try:
            chat.send_dm(reporter, question)
        except Exception:  # noqa: BLE001
            break
    answers = _collect_answers(chat, reporter)
    what = answers[0] if len(answers) > 0 else ""
    who = answers[1] if len(answers) > 1 else ""
    impact = answers[2] if len(answers) > 2 else ""
    evidence = "; ".join([a for a in (what, who, impact) if a]) or "blocker intake"
    blocked = None
    if blocked_item_id:
        blocked = board.get_item(blocked_item_id)
    else:
        for cand in board.list_items(limit=200):
            if _owner_of(cand) == reporter:
                blocked = cand
                break
        if blocked is None:
            blocked = board.create_item(
                f"Blocked work for {reporter}",
                {"state": _active_state(board), "assigned_to": reporter},
            )
            if service is not None:
                service.propose(
                    {
                        "operation": "create_item",
                        "item_id": _item_id(blocked),
                        "evidence_quote": evidence[:500],
                        "status": "proposed",
                        "detail": "blocked item created from intake",
                    }
                )
    blocked_id = _item_id(blocked)
    owner = (blocking_owner or "").strip()
    if not owner and who:
        token = who.strip().split()[0].strip(",.:")
        if token.startswith("U") and len(token) >= 2:
            owner = token
    title = (blocking_title or "").strip()
    if not title:
        if who:
            title = f"Blocking: {who[:60]}"
        elif what:
            title = f"Blocking: {what[:60]}"
        else:
            title = "Blocking item"
    blocking = None
    needle = who.lower() if who else title.lower()
    for cand in board.list_items(limit=200):
        cand_title = _item_title(cand).lower()
        if _item_id(cand) == blocked_id:
            continue
        if needle and (needle in cand_title or cand_title in needle):
            blocking = cand
            break
    if blocking is None:
        fields: dict[str, Any] = {"state": _open_state(board)}
        if owner:
            fields["assigned_to"] = owner
        blocking = board.create_item(title, fields)
        _maybe_copy_iteration(board, blocked, _item_id(blocking))
        if service is not None:
            service.propose(
                {
                    "operation": "create_item",
                    "item_id": _item_id(blocking),
                    "evidence_quote": evidence[:500],
                    "status": "proposed",
                    "detail": f"blocking item for {blocked_id}",
                }
            )
    blocking_id = _item_id(blocking)
    link = board.add_link(blocking_id, blocked_id, FORWARD_REL)
    if service is not None:
        service.propose(
            {
                "operation": "add_link",
                "item_id": blocked_id,
                "evidence_quote": evidence[:500],
                "status": "proposed",
                "detail": f"Predecessor {blocking_id} Successor {blocked_id}",
            }
        )
    blocking_owner_id = _owner_of(blocking) or owner
    eta_dm: str | None = None
    if blocking_owner_id:
        try:
            eta_dm = str(
                chat.send_dm(
                    blocking_owner_id,
                    f"You are blocking '{_item_title(blocked)}'. "
                    "What is your ETA?",
                )
            )
        except Exception:  # noqa: BLE001
            eta_dm = None
    info = BlockerInfo(
        description=what or _item_title(blocked),
        blocked_item_id=blocked_id,
        blocking_item_id=blocking_id,
        owner=blocking_owner_id or None,
        impact=impact or None,
        evidence_quote=evidence[:500],
    )
    raid_issue: dict[str, Any] | None = None
    if db is not None:
        try:
            from pam_pm.core import raid as raid_mod

            raid_issue = raid_mod.propose_issue_from_blocker(
                db,
                str(backend or "azure_devops"),
                str(project or "default"),
                info,
                writes=service,
            )
        except Exception:  # noqa: BLE001
            raid_issue = None
    return {
        "info": info,
        "blocked_item": blocked,
        "blocking_item": blocking,
        "link": link,
        "evidence_quote": evidence,
        "eta_dm": eta_dm,
        "raid_issue": raid_issue,
    }


def on_blocker_closed(
    blocking_item: Any,
    waiter: str,
    chat: Any,
    board: Any | None = None,
    *,
    db: Any = None,
    backend: str = "azure_devops",
    project: str = "default",
) -> str | None:
    """Notify the waiter when the blocking item is closed. Else None."""
    resolved = blocking_item
    if isinstance(blocking_item, str) and board is not None:
        try:
            resolved = board.get_item(blocking_item)
        except Exception:  # noqa: BLE001
            return None
    if not is_closed(_item_state(resolved)):
        return None
    if db is not None:
        try:
            from pam_pm.core import raid as raid_mod

            raid_mod.close_issues_for_board_item(
                db,
                str(backend or "azure_devops"),
                str(project or "default"),
                _item_id(resolved),
            )
        except Exception:  # noqa: BLE001, S110
            pass
    title = _item_title(resolved)
    try:
        return str(
            chat.send_dm(
                waiter, f"Your blocker '{title}' is cleared. What is your status?"
            )
        )
    except Exception:  # noqa: BLE001
        return None


def notify_waiter(
    blocking_item: Any,
    waiter: str,
    chat: Any,
    board: Any | None = None,
    **kwargs: Any,
) -> str | None:
    """Alias for on_blocker_closed."""
    return on_blocker_closed(blocking_item, waiter, chat, board, **kwargs)


def run_blocker_intake(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Alias for blocker_intake kept for older imports."""
    if len(args) >= 3:
        return blocker_intake(*args, **kwargs)  # type: ignore[arg-type]
    return {}
