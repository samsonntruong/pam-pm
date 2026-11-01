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


def approve_proposal(proposal: Any, board: Any) -> list[Any]:
    """Create board items for an approved proposal via a PMAdapter.

    Each draft becomes one item through board.create_item with ADO style
    fields (type, assigned_to, remaining, description). Returns the
    created items in proposal order. An empty proposal creates nothing.
    """
    if board is None:
        raise ValueError("board is required to approve a proposal")
    drafts = list(proposal) if isinstance(proposal, (list, tuple)) else [proposal]
    created: list[Any] = []
    for draft in drafts:
        if isinstance(draft, dict):
            title = str(draft.get("title", "") or "").strip() or "Untitled"
            fields: dict[str, Any] = {
                "type": str(draft.get("type", DEFAULT_ITEM_TYPE) or DEFAULT_ITEM_TYPE),
                "state": str(draft.get("state", DEFAULT_STATE) or DEFAULT_STATE),
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
                "state": DEFAULT_STATE,
                "description": title,
            }
        created.append(board.create_item(title, fields))
    return created


class Planner:
    """Planner helper that delegates to propose_sprint."""

    def propose_sprint(self, notes: Any) -> list[dict[str, Any]]:
        """Propose sprint items from note readings."""
        return propose_sprint(notes)

    def approve(self, proposal: Any, board: Any) -> list[Any]:
        """Create board items for an approved proposal."""
        return approve_proposal(proposal, board)
