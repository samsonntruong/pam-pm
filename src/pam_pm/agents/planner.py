"""Sprint planner from sticky-note readings."""

from __future__ import annotations

from typing import Any

DEFAULT_ITEM_TYPE = "Task"
DEFAULT_STATE = "To Do"


def _note_title(note: Any, index: int) -> str:
    if isinstance(note, str):
        text = note.strip()
        return text if text else f"Note {index + 1}"
    if isinstance(note, dict):
        for key in ("title", "text", "reading"):
            value = note.get(key)
            if value and str(value).strip():
                return str(value).strip()
        return f"Note {index + 1}"
    for attr in ("title", "text", "reading"):
        value = getattr(note, attr, None)
        if value and str(value).strip():
            return str(value).strip()
    text = str(note).strip()
    return text if text else f"Note {index + 1}"


def _note_field(note: Any, names: tuple[str, ...]) -> Any:
    if isinstance(note, dict):
        for name in names:
            if note.get(name) not in (None, ""):
                return note.get(name)
        return None
    for name in names:
        value = getattr(note, name, None)
        if value not in (None, ""):
            return value
    return None


def _normalize_notes(notes: Any) -> list[Any]:
    if notes is None:
        return []
    if isinstance(notes, str):
        lines = [line.strip() for line in notes.splitlines()]
        return [line for line in lines if line]
    if isinstance(notes, dict):
        return [notes]
    if isinstance(notes, (list, tuple)):
        out: list[Any] = []
        for entry in notes:
            if isinstance(entry, str) and "\n" in entry:
                out.extend([ln.strip() for ln in entry.splitlines() if ln.strip()])
            else:
                out.append(entry)
        return out
    return [notes]


def _coerce_estimate(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def propose_sprint(notes: Any) -> list[dict[str, Any]]:
    """Propose sprint item drafts from note readings or crops.

    Accepts notes text (one item per line), a list of crops or readings,
    or note dicts with title, type, owner, and estimate keys. Each note
    becomes one draft with title, type, owner, estimate, state, and
    description. The caller must approve before creating board items.
    Returns an empty list when notes are empty.
    """
    raw = _normalize_notes(notes)
    proposal: list[dict[str, Any]] = []
    for index, note in enumerate(raw):
        if isinstance(note, str) and not note.strip():
            continue
        title = _note_title(note, index)
        item_type = _note_field(note, ("type", "item_type", "work_item_type"))
        owner = _note_field(note, ("owner", "assigned_to", "assignee"))
        estimate = _coerce_estimate(_note_field(note, ("estimate", "effort", "remaining")))
        proposal.append(
            {
                "title": title,
                "type": str(item_type).strip() if item_type else DEFAULT_ITEM_TYPE,
                "owner": str(owner).strip() if owner else None,
                "estimate": estimate,
                "state": DEFAULT_STATE,
                "description": title,
            }
        )
    return proposal


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
    return DEFAULT_STATE


def _map_state(board: Any, state: str) -> str:
    states = _board_states(board)
    if states and state not in states:
        return states[0]
    return state or _open_state(board)


def _item_id(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id", item.get("item_id", "")) or "")
    return str(getattr(item, "id", getattr(item, "item_id", "")) or "")


def _draft_iteration(draft: Any) -> str:
    if not isinstance(draft, dict):
        return ""
    for key in ("iteration_id", "iteration", "sprint"):
        value = draft.get(key)
        if value:
            return str(value)
    return ""


def _resolve_writes(writes: Any, db: Any, dry_run: bool) -> Any:
    if writes is not None:
        return writes
    if db is None:
        return None
    from pam_pm.core.writes import WriteService

    return WriteService(db, dry_run=dry_run)


def approve_proposal(
    proposal: Any,
    board: Any,
    *,
    writes: Any = None,
    db: Any = None,
    dry_run: bool = True,
    evidence_quote: str = "",
) -> list[Any]:
    """Create board items for an approved proposal via a PMAdapter.

    Each draft becomes one item through board.create_item with ADO style
    fields (type, assigned_to, remaining, description). The draft state
    is mapped to the board open state when the board does not list it,
    so the same proposal works on the native board (New) and on ADO
    (New on Agile, To Do on Scrum). Drafts carrying iteration_id,
    iteration, or sprint are moved to that sprint when the board
    supports iterations. Each creation is logged through writes with
    its evidence quote when a write service is given. Returns the
    created items in proposal order. An empty proposal creates nothing.
    """
    if board is None:
        raise ValueError("board is required to approve a proposal")
    service = _resolve_writes(writes, db, dry_run)
    drafts = list(proposal) if isinstance(proposal, (list, tuple)) else [proposal]
    created: list[Any] = []
    for draft in drafts:
        if isinstance(draft, dict):
            title = str(draft.get("title", "") or "").strip() or "Untitled"
            raw_state = str(draft.get("state", DEFAULT_STATE) or DEFAULT_STATE)
            fields: dict[str, Any] = {
                "type": str(draft.get("type", DEFAULT_ITEM_TYPE) or DEFAULT_ITEM_TYPE),
                "state": _map_state(board, raw_state),
                "description": str(draft.get("description", title) or title),
            }
            owner = draft.get("owner")
            if owner:
                fields["assigned_to"] = str(owner)
            estimate = _coerce_estimate(draft.get("estimate"))
            if estimate is not None:
                fields["remaining"] = estimate
            tags = draft.get("tags")
            if tags:
                fields["tags"] = tags
        else:
            title = _note_title(draft, len(created))
            fields = {
                "type": DEFAULT_ITEM_TYPE,
                "state": _open_state(board),
                "description": title,
            }
        item = board.create_item(title, fields)
        iteration = _draft_iteration(draft)
        if iteration and getattr(board, "supports_iterations", True):
            setter = getattr(board, "set_item_iteration", None)
            if callable(setter):
                try:
                    item = setter(_item_id(item), iteration)
                except Exception:  # noqa: BLE001, S110
                    pass
        created.append(item)
        if service is not None:
            quote = (evidence_quote or title).strip()[:500] or title[:500]
            service.propose(
                {
                    "operation": "create_item",
                    "item_id": _item_id(item),
                    "evidence_quote": quote,
                    "status": "proposed",
                    "detail": f"approved from sticky notes: {title[:200]}",
                }
            )
    return created


class Planner:
    """Planner helper that delegates to propose_sprint."""

    def propose_sprint(self, notes: Any) -> list[dict[str, Any]]:
        """Propose sprint items from note readings."""
        return propose_sprint(notes)

    def approve(self, proposal: Any, board: Any, **kwargs: Any) -> list[Any]:
        """Create board items for an approved proposal."""
        return approve_proposal(proposal, board, **kwargs)
