"""Sprint planner from sticky-note readings."""

from __future__ import annotations

from typing import Any


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


def propose_sprint(notes: Any) -> list[dict[str, Any]]:
    """Propose sprint items from note readings, awaiting approval.

    Each note becomes one To Do item. The caller must approve before
    creating board items. Returns an empty list when notes are empty.
    """
    if notes is None:
        return []
    if isinstance(notes, (str, dict)):
        raw = [notes]
    elif isinstance(notes, (list, tuple)):
        raw = list(notes)
    else:
        raw = [notes]
    proposal: list[dict[str, Any]] = []
    for index, note in enumerate(raw):
        title = _note_title(note, index)
        proposal.append(
            {"title": title, "state": "To Do", "description": title}
        )
    return proposal


class Planner:
    """Planner helper that delegates to propose_sprint."""

    def propose_sprint(self, notes: Any) -> list[dict[str, Any]]:
        """Propose sprint items from note readings."""
        return propose_sprint(notes)
