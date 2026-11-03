"""Listen auto pipeline: propose tasks with owners and estimates."""

from __future__ import annotations

import re
from typing import Any

_WORDS = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _WORDS.findall(str(text or "").lower()) if len(t) > 2}


def _person_search_terms(person: dict[str, Any]) -> tuple[set[str], set[str]]:
    role = str(person.get("role", "") or "")
    role_tokens = _tokens(role)
    raw_skills = person.get("skillsets", person.get("skills", []))
    skill_tokens: set[str] = set()
    if isinstance(raw_skills, str):
        skill_tokens = _tokens(raw_skills)
    elif isinstance(raw_skills, (list, tuple)):
        for entry in raw_skills:
            skill_tokens |= _tokens(str(entry))
    return role_tokens, skill_tokens


def suggest_owner(
    title: str,
    people: list[dict[str, Any]] | None,
    board_owners: list[str] | None,
) -> str | None:
    """Pick an owner from Team memory, else board owners, else None.

    Team memory entries carry a role plus skillsets. The best match
    wins on shared words with the title, skills weighted over role.
    With no shared words the first teammate is the default so every
    proposal still carries a suggestion. Board owners apply only when
    Team memory is empty.
    """
    candidates = [p for p in (people or []) if str(p.get("name", "")).strip()]
    if candidates:
        words = _tokens(title)
        for person in candidates:
            name = str(person["name"]).strip()
            if not name:
                continue
            given = name.lower().replace(".", " ").split()[0]
            if given and given in words:
                return name
        best = ""
        best_score = -1
        for person in candidates:
            role_tokens, skill_tokens = _person_search_terms(person)
            score = len(words & skill_tokens) * 2 + len(words & role_tokens)
            if score > best_score:
                best_score = score
                best = str(person["name"]).strip()
        if best_score > 0:
            return best
        return str(candidates[0]["name"]).strip()
    for owner in board_owners or []:
        if str(owner or "").strip():
            return str(owner).strip()
    return None


def estimate_hours(title: str) -> float:
    """Default estimate from title length, editable before approval."""
    size = len(str(title or "").strip())
    if size <= 60:
        return 1.0
    if size <= 120:
        return 2.0
    return 4.0


def _short_title(title: str, limit: int = 80) -> str:
    """Condense a long evidence sentence to a short card title.

    Short titles pass through. Longer ones use the notes condenser
    so cards read like "Rate limiting on login endpoint" instead of
    the full transcript sentence.
    """
    text = " ".join(str(title or "").split()).strip()
    if len(text) <= limit:
        return text
    try:
        from pam_pm.agents.notes import concise_title

        return concise_title(text, limit)[:200]
    except Exception:  # noqa: BLE001
        cut = text[:limit].rsplit(" ", 1)[0].strip()
        return cut or text[:limit].strip()


def _known_names(
    people: list[dict[str, Any]] | None, board_owners: list[str] | None
) -> set[str]:
    names = {str(p.get("name", "")).strip().lower() for p in (people or [])}
    names |= {str(o).strip().lower() for o in (board_owners or [])}
    return {n for n in names if n}


def propose_listen_tasks(
    transcript: str,
    notes_result: dict[str, Any] | None,
    people: list[dict[str, Any]] | None,
    board_owners: list[str] | None,
    session_id: str,
) -> list[dict[str, Any]]:
    """Build task proposals from notes, one per action item.

    Nothing is posted here. Each proposal keeps the meeting id and
    an evidence quote so approved writes link back to the source.
    """
    notes = notes_result if isinstance(notes_result, dict) else {}
    actions = notes.get("action_items", [])
    if not isinstance(actions, list):
        actions = []
    known = _known_names(people, board_owners)
    proposals: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        title = str(action.get("title", "") or "").strip()[:200]
        if not title:
            continue
        raw_owner = str(action.get("owner", "") or "").strip()
        if raw_owner and raw_owner.lower() in known:
            owner: str | None = raw_owner
        else:
            owner = suggest_owner(title, people, board_owners)
        quote = str(action.get("evidence_quote", "") or "")[:300] or title[:300]
        title = _short_title(title)
        proposals.append(
            {
                "title": title,
                "owner": owner,
                "estimate": estimate_hours(title),
                "evidence_quote": quote,
                "due_date": str(action.get("due_date", "") or "")[:120],
                "meeting_id": str(session_id),
            }
        )
    if not proposals:
        text = " ".join(str(transcript or "").split())
        if text:
            summary = str(notes.get("summary", "") or "").strip() or text
            title = _short_title(summary[:200])
            proposals.append(
                {
                    "title": title,
                    "owner": suggest_owner(title, people, board_owners),
                    "estimate": estimate_hours(title),
                    "evidence_quote": text[:300],
                    "due_date": "",
                    "meeting_id": str(session_id),
                }
            )
    return proposals


def _parse_estimate(value: Any, default: float = 2.0) -> float:
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return default
    if hours < 0:
        return 0.0
    if hours > 80:
        return 80.0
    return hours


def _open_state(board: Any) -> str:
    try:
        states = [str(s) for s in board.list_states() if str(s)]
    except Exception:  # noqa: BLE001
        states = []
    return states[0] if states else "To Do"


def _item_id(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id", item.get("item_id", "")) or "")
    return str(getattr(item, "id", getattr(item, "item_id", "")) or "")


def approve_listen_proposals(
    board: Any,
    proposals: list[dict[str, Any]],
    selected: list[int],
    session_id: str,
    transcript: str,
    *,
    writes: Any = None,
    db: Any = None,
    dry_run: bool = True,
    meeting_title: str = "",
) -> list[Any]:
    """Create board items for selected proposals only.

    Each write carries the meeting id plus a transcript quote so the
    board item links back to the meeting it came from.
    """
    if board is None:
        raise ValueError("board is required to approve proposals")
    service = writes
    if service is None and db is not None:
        from pam_pm.core.writes import WriteService

        service = WriteService(db, dry_run=dry_run)
    state = _open_state(board)
    excerpt = " ".join(str(transcript or "").split())[:300]
    label = f"meeting {session_id}"
    if str(meeting_title or "").strip():
        label += f" ({str(meeting_title).strip()[:80]})"
    created: list[Any] = []
    for index in selected:
        if index < 0 or index >= len(proposals):
            continue
        draft = proposals[index]
        if not isinstance(draft, dict):
            continue
        title = str(draft.get("title", "") or "").strip() or "Meeting action"
        owner = str(draft.get("owner", "") or "").strip()
        quote = str(draft.get("evidence_quote", "") or "").strip()[:500]
        quote = quote or excerpt or title[:500]
        hours = _parse_estimate(draft.get("estimate", 2.0))
        due = str(draft.get("due_date", "") or "").strip()
        description = f"From {label}. Evidence: \"{quote}\"."
        if excerpt and excerpt != quote:
            description += f" Transcript: \"{excerpt[:200]}\"."
        description += f" Estimate: {hours:g}h."
        if due:
            description += f" Due: {due}."
        fields: dict[str, Any] = {
            "state": state,
            "description": description,
            "estimate": hours,
        }
        if owner:
            fields["assigned_to"] = owner
        if due:
            fields["due_date"] = due
        item = board.create_item(title[:200], fields)
        created.append(item)
        if service is not None:
            service.propose(
                {
                    "operation": "create_item",
                    "item_id": _item_id(item),
                    "evidence_quote": quote,
                    "status": "proposed",
                    "detail": f"listen proposal from {label}: {title[:200]}",
                }
            )
    return created
