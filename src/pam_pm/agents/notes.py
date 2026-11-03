"""Meeting notes agent: summary, decisions, actions, RAID, matches."""

from __future__ import annotations

import json
import re
from typing import Any

NOTES_MAX_TOKENS = 4000

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_OWNER_PREFIX = re.compile(r"^\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*:\s*(.+)$")
_OWNER_VOCATIVE_RE = re.compile(
    r"^\s*[A-Z][a-z]+\s*,\s*(can you|could you|please|will you|need you to)\s+",
    re.IGNORECASE,
)
_NAME_WILL = re.compile(r"\b([A-Z][a-z]+)\s+will\b")
_DUE_RE = re.compile(
    r"\b(by|due)\s+([A-Za-z]+\s+\d{1,2}(?:,\s*\d{4})?|"
    r"\d{4}-\d{2}-\d{2}|Friday|Monday|Tuesday|Wednesday|Thursday|"
    r"tomorrow|next\s+week|end\s+of\s+week)\b",
    re.IGNORECASE,
)
_DECISION_RE = re.compile(r"\b(decided|decision|agreed|agreement|chose|choice)\b", re.IGNORECASE)
_ACTION_RE = re.compile(
    r"\b(will|shall|todo|to-do|action\s+item|follow\s*up|assign|owner|due)\b",
    re.IGNORECASE,
)
_RAID_RES = {
    "risk": re.compile(r"\b(risk|threat|might\s+fail|concern)\b", re.IGNORECASE),
    "issue": re.compile(r"\b(issue|bug|blocked|blocker|problem)\b", re.IGNORECASE),
    "dependency": re.compile(r"\b(depend|waiting\s+on|needs\s+from|blocked\s+by)\b", re.IGNORECASE),
    "assumption": re.compile(r"\b(assum|expect|suppose|presume)\b", re.IGNORECASE),
}


def _sentences(transcript: str) -> list[str]:
    text = " ".join(str(transcript or "").split())
    if not text:
        return []
    parts = _SENT_SPLIT.split(text)
    return [p.strip() for p in parts if p.strip()]


def _guess_owner(sentence: str) -> str:
    match = _OWNER_PREFIX.match(sentence)
    if match:
        return match.group(1).strip()
    found = _NAME_WILL.search(sentence)
    if found:
        return found.group(1).strip()
    return ""


def _guess_due(sentence: str) -> str:
    match = _DUE_RE.search(sentence)
    if not match:
        return ""
    return " ".join(match.group(0).split())

_OWNER_WILL_RE = re.compile(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+will\s+", re.IGNORECASE)
_TEAM_WILL_RE = re.compile(r"^(the\s+team|someone|we)\s+will\s+", re.IGNORECASE)
_NEEDS_TO_RE = re.compile(
    r"^(someone|anyone|we|the\s+team)\s+(needs?\s+to|has\s+to|should)\s+",
    re.IGNORECASE,
)
_LABEL_RE = re.compile(
    r"^(please\s+|action\s*item\s*:?\s*|todo\s*:?\s*)", re.IGNORECASE
)
_AGREE_TAIL_RE = re.compile(
    r"\s*\?\s*(yep|yes|sure|okay|ok|sounds good|will do|got it|i'll take it|i will)[,.!?\s]*$",
    re.IGNORECASE,
)


def concise_title(sentence: str, limit: int = 80) -> str:
    """Derive a short card title from one evidence sentence.

    Strips speaker prefixes, will/needs wrappers, and due phrases so
    the card reads like "Rate limiting on login endpoint" instead of
    the full transcript sentence. The full sentence stays in the
    evidence quote.
    """
    text = " ".join(str(sentence or "").split()).strip()
    owned = _OWNER_PREFIX.match(text)
    if owned:
        text = owned.group(2).strip()
    text = _OWNER_VOCATIVE_RE.sub("", text).strip()
    for pattern in (_OWNER_WILL_RE, _TEAM_WILL_RE, _NEEDS_TO_RE):
        text = pattern.sub("", text).strip()
    due = _DUE_RE.search(text)
    if due:
        text = (text[:due.start()] + text[due.end():]).strip()
    text = _LABEL_RE.sub("", text).strip()
    text = _AGREE_TAIL_RE.sub("", text).strip()
    text = text.rstrip(" .")
    if len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0].strip()
        text = cut or text[:limit].strip()
    if not text:
        return "Meeting action"
    return text[0].upper() + text[1:]



def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", str(text).lower()) if len(t) > 2}


def _item_text(item: Any) -> tuple[str, str]:
    if isinstance(item, dict):
        item_id = str(item.get("id", item.get("item_id", "")) or "")
        title = str(item.get("title", "") or "")
        return item_id, title
    item_id = str(getattr(item, "id", getattr(item, "item_id", "")) or "")
    title = str(getattr(item, "title", "") or "")
    return item_id, title


def match_notes_to_items(
    actions: Any, existing_items: Any
) -> list[dict[str, str]]:
    """Match action titles to board items by word overlap, no invention."""
    if not isinstance(actions, list):
        actions = [actions] if actions else []
    if existing_items is None:
        return []
    if isinstance(existing_items, dict):
        items = [existing_items]
    elif isinstance(existing_items, (list, tuple)):
        items = list(existing_items)
    else:
        items = [existing_items]
    matches: list[dict[str, str]] = []
    for action in actions:
        if isinstance(action, dict):
            title = str(action.get("title", "") or "")
        else:
            title = str(getattr(action, "title", "") or "")
        words = _tokens(title)
        if not words:
            continue
        best_id = ""
        best_title = ""
        best_hit = 0
        for item in items:
            item_id, item_title = _item_text(item)
            if not item_id:
                continue
            hit = len(words & _tokens(item_title))
            if hit > best_hit:
                best_hit = hit
                best_id = item_id
                best_title = item_title
        if best_id and best_hit >= 1:
            matches.append(
                {
                    "note_title": title[:200],
                    "item_id": best_id,
                    "reason": f"Shares {best_hit} words with '{best_title[:120]}'.",
                }
            )
    return matches


def _local_extract(
    transcript: str, existing_items: Any = None
) -> dict[str, Any]:
    sentences = _sentences(transcript)
    summary = " ".join(sentences[:2])[:500] if sentences else "No transcript yet."
    decisions: list[dict[str, str]] = []
    actions: list[dict[str, Any]] = []
    raid: list[dict[str, str]] = []
    for sent in sentences:
        quote = sent[:300]
        if _DECISION_RE.search(sent):
            decisions.append({"text": sent[:300], "evidence_quote": quote})
        if _ACTION_RE.search(sent) or _OWNER_PREFIX.match(sent):
            owner = _guess_owner(sent)
            actions.append(
                {
                    "title": concise_title(sent)[:200],
                    "owner": owner or None,
                    "due_date": _guess_due(sent),
                    "evidence_quote": quote,
                    "confidence": 0.6 if owner else 0.4,
                }
            )
        for kind, pattern in _RAID_RES.items():
            if pattern.search(sent):
                raid.append(
                    {
                        "kind": kind,
                        "title": sent[:200],
                        "owner": _guess_owner(sent),
                        "severity": "medium",
                        "evidence_quote": quote,
                    }
                )
                break
    matches = match_notes_to_items(actions, existing_items)
    return {
        "summary": summary,
        "decisions": decisions,
        "action_items": actions,
        "proposed_raid": raid,
        "matches": matches,
    }


def _render_notes_markdown(result: dict[str, Any]) -> str:
    lines = ["# Meeting notes", ""]
    lines += ["## Summary", str(result.get("summary", "No transcript yet.")), ""]
    lines.append("## Decisions")
    decisions = result.get("decisions", [])
    if decisions:
        for item in decisions:
            text = item.get("text", "") if isinstance(item, dict) else str(item)
            quote = item.get("evidence_quote", "") if isinstance(item, dict) else ""
            lines.append(f'- {text} Evidence: "{quote}".')
    else:
        lines.append("No decisions recorded.")
    lines += ["", "## Action items"]
    actions = result.get("action_items", [])
    if actions:
        for item in actions:
            title = item.get("title", "") if isinstance(item, dict) else str(item)
            owner = item.get("owner", "") if isinstance(item, dict) else ""
            due = item.get("due_date", "") if isinstance(item, dict) else ""
            quote = item.get("evidence_quote", "") if isinstance(item, dict) else ""
            lines.append(f'- {title} Owner: {owner or "unassigned"}. Due: {due or "none"}. Evidence: "{quote}".')
    else:
        lines.append("No action items recorded.")
    lines += ["", "## Proposed RAID"]
    entries = result.get("proposed_raid", [])
    if entries:
        for item in entries:
            kind = item.get("kind", "risk") if isinstance(item, dict) else "risk"
            title = item.get("title", "") if isinstance(item, dict) else str(item)
            lines.append(f"- [{kind}] {title}")
    else:
        lines.append("No RAID entries proposed.")
    lines += ["", "## Matches"]
    matches = result.get("matches", [])
    if matches:
        for item in matches:
            lines.append(f'- {item.get("note_title", "")} matches {item.get("item_id", "")}. {item.get("reason", "")}')
    else:
        lines.append("No matches to existing items.")
    return "\n".join(lines) + "\n"


def _model_text(data: Any) -> str:
    if hasattr(data, "text"):
        return str(getattr(data, "text", "") or "")
    if isinstance(data, dict):
        return str(data.get("text", "") or "")
    if isinstance(data, str):
        return data
    return str(data or "")


def _parse_model_json(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(raw[start : end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def _normalize_actions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    drafts = value if isinstance(value, list) else [value]
    out: list[dict[str, Any]] = []
    for draft in drafts:
        if isinstance(draft, str):
            out.append(
                {
                    "title": concise_title(draft)[:200],
                    "owner": None,
                    "due_date": _guess_due(draft),
                    "evidence_quote": draft[:300],
                    "confidence": 0.4,
                }
            )
        elif isinstance(draft, dict):
            title = str(draft.get("title", "") or "")[:200] or "Action item"
            owner = draft.get("owner") or None
            if isinstance(owner, str) and not owner.strip():
                owner = None
            out.append(
                {
                    "title": title,
                    "owner": str(owner).strip() if owner else None,
                    "due_date": str(draft.get("due_date", "") or "")[:120],
                    "evidence_quote": str(draft.get("evidence_quote", "") or "")[:300],
                    "confidence": float(draft.get("confidence", 0.5) or 0.5),
                }
            )
    return out


def _normalize_decisions(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    drafts = value if isinstance(value, list) else [value]
    out: list[dict[str, str]] = []
    for draft in drafts:
        if isinstance(draft, str):
            out.append({"text": draft[:300], "evidence_quote": draft[:300]})
        elif isinstance(draft, dict):
            text = str(draft.get("text", draft.get("title", "")) or "")[:300]
            out.append(
                {
                    "text": text,
                    "evidence_quote": str(draft.get("evidence_quote", "") or "")[:300],
                }
            )
    return [item for item in out if item["text"]]


def _normalize_raid(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    drafts = value if isinstance(value, list) else [value]
    out: list[dict[str, str]] = []
    for draft in drafts:
        if isinstance(draft, str):
            out.append(
                {
                    "kind": "risk",
                    "title": draft[:200],
                    "owner": "",
                    "severity": "medium",
                    "evidence_quote": draft[:300],
                }
            )
        elif isinstance(draft, dict):
            kind = str(draft.get("kind", "risk") or "risk").strip().lower()
            if kind not in ("risk", "assumption", "issue", "dependency"):
                kind = "risk"
            out.append(
                {
                    "kind": kind,
                    "title": str(draft.get("title", "") or "")[:200],
                    "owner": str(draft.get("owner", "") or "")[:120],
                    "severity": str(draft.get("severity", "medium") or "medium")[:20],
                    "evidence_quote": str(draft.get("evidence_quote", "") or "")[:300],
                }
            )
    return [item for item in out if item["title"]]


def build_project_context(items: Any, roster: Any = None) -> str:
    """Build stable project context listing board items and roster."""
    if items is None:
        raw: list[Any] = []
    elif isinstance(items, (list, tuple)):
        raw = list(items)
    else:
        raw = [items]
    lines = ["Meeting notes. Board items:"]
    if not raw:
        lines.append("No board items.")
    for entry in raw[:50]:
        item_id, title = _item_text(entry)
        state = ""
        if isinstance(entry, dict):
            state = str(entry.get("state", "") or "")
        else:
            state = str(getattr(entry, "state", "") or "")
        lines.append(f"- {item_id or 'item'}: {title or 'untitled'} [{state}]")
    if roster:
        names: list[str] = []
        for entry in (roster if isinstance(roster, list) else [roster]):
            if isinstance(entry, dict) and entry.get("name"):
                names.append(str(entry["name"]))
            elif isinstance(entry, str) and entry.strip():
                names.append(entry.strip())
        if names:
            lines.append("Team: " + ", ".join(names[:20]))
    return "\n".join(lines)


def extract_notes(
    transcript: str,
    *,
    project_context: str = "",
    existing_items: Any = None,
    roster: Any = None,
    model_client: Any = None,
    max_tokens: int = NOTES_MAX_TOKENS,
    volatile: Any = None,
) -> dict[str, Any]:
    """Extract notes from a transcript with a local fallback.

    The local path uses only transcript words with evidence quotes.
    The model path builds prefix stable messages and parses JSON,
    falling back to local output when the model text is unusable.
    """
    context = project_context or build_project_context(existing_items, roster)
    local = _local_extract(transcript, existing_items)
    tokens = max(int(max_tokens or 0), 1000)
    if model_client is None:
        markdown = _render_notes_markdown(local)
        return {
            **local,
            "markdown": markdown,
            "notes_markdown": markdown,
            "max_tokens": tokens,
            "model_path": "local",
        }
    from pam_pm.models import realtime as rt_mod

    payload = rt_mod.build_notes_messages(context, transcript or "", volatile)
    try:
        raw = model_client.chat(
            payload["messages"], tools=payload["tools"], max_tokens=tokens
        )
    except TypeError:
        raw = model_client.chat(payload["messages"], payload["tools"])
    text = _model_text(raw).strip()
    parsed = _parse_model_json(text)
    if not parsed:
        markdown = _render_notes_markdown(local)
        return {
            **local,
            "markdown": markdown,
            "notes_markdown": markdown,
            "max_tokens": tokens,
            "model_path": "local",
            "model_text": text[:2000],
        }
    actions = _normalize_actions(parsed.get("action_items", parsed.get("actions", [])))
    decisions = _normalize_decisions(parsed.get("decisions", []))
    raid = _normalize_raid(parsed.get("proposed_raid", parsed.get("raid", [])))
    matches = parsed.get("matches", [])
    if not isinstance(matches, list):
        matches = []
    clean_matches = [m for m in matches if isinstance(m, dict) and m.get("item_id")]
    if not clean_matches:
        clean_matches = match_notes_to_items(actions, existing_items)
    summary = str(parsed.get("summary", "") or "")[:2000] or local["summary"]
    result = {
        "summary": summary,
        "decisions": decisions or local["decisions"],
        "action_items": actions,
        "proposed_raid": raid,
        "matches": clean_matches,
    }
    markdown = str(parsed.get("markdown", "") or "").strip()
    if not markdown:
        markdown = _render_notes_markdown(result)
    return {
        **result,
        "markdown": markdown,
        "notes_markdown": markdown,
        "max_tokens": tokens,
        "model_path": "live",
    }


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


def _item_id(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id", item.get("item_id", "")) or "")
    return str(getattr(item, "id", getattr(item, "item_id", "")) or "")


def _resolve_writes(writes: Any, db: Any, dry_run: bool) -> Any:
    if writes is not None:
        return writes
    if db is None:
        return None
    from pam_pm.core.writes import WriteService

    return WriteService(db, dry_run=dry_run)


def create_action_items(
    board: Any,
    actions: Any,
    *,
    writes: Any = None,
    db: Any = None,
    dry_run: bool = True,
    evidence_quote: str = "",
) -> list[Any]:
    """Create board items for approved meeting actions via a PMAdapter."""
    if board is None:
        raise ValueError("board is required to create action items")
    service = _resolve_writes(writes, db, dry_run)
    state = _open_state(board)
    drafts = list(actions) if isinstance(actions, (list, tuple)) else [actions]
    created: list[Any] = []
    for draft in drafts:
        if isinstance(draft, dict):
            title = str(draft.get("title", "") or "").strip() or "Meeting action"
            owner = str(draft.get("owner", "") or "").strip()
            quote = str(
                draft.get("evidence_quote", "") or evidence_quote or title
            ).strip()[:500]
            due = str(draft.get("due_date", "") or "").strip()
        else:
            title = str(getattr(draft, "title", "") or "").strip() or "Meeting action"
            owner = str(getattr(draft, "owner", "") or "").strip()
            quote = (evidence_quote or title).strip()[:500]
            due = ""
        description = f"From meeting notes. Evidence: \"{quote}\"."
        if due:
            description += f" Due: {due}."
        fields: dict[str, Any] = {"state": state, "description": description}
        if owner:
            fields["assigned_to"] = owner
        item = board.create_item(title, fields)
        created.append(item)
        if service is not None:
            service.propose(
                {
                    "operation": "create_item",
                    "item_id": _item_id(item),
                    "evidence_quote": quote or title[:500],
                    "status": "proposed",
                    "detail": f"meeting action from notes: {title[:200]}",
                }
            )
    return created


def approve_notes(board: Any, actions: Any, **kwargs: Any) -> list[Any]:
    """Alias for create_action_items kept for kickoff symmetry."""
    return create_action_items(board, actions, **kwargs)


class NotesAgent:
    """Notes helper that delegates to extract_notes."""

    def extract_notes(self, transcript: str, **kwargs: Any) -> dict[str, Any]:
        """Extract notes from a transcript."""
        return extract_notes(transcript, **kwargs)

    def create_action_items(
        self, board: Any, actions: Any, **kwargs: Any
    ) -> list[Any]:
        """Create board items for approved meeting actions."""
        return create_action_items(board, actions, **kwargs)
