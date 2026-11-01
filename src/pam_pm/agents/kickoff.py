"""Kickoff from a document with owner suggestions (ADDONS item 1).

Parses a pasted brief or an uploaded file into epics, stories, and
tasks, suggests an owner for each, and reuses the sticky-note
planner proposal and approval code. Works on both backends.
"""

from __future__ import annotations

import re
from typing import Any

from pam_pm.adapters.pm.base import is_closed
from pam_pm.agents.planner import approve_proposal, propose_sprint

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

_ESTIMATE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*h(?:ours?)?\b", re.IGNORECASE)
_OWNER_PREFIX_RE = re.compile(r"^\s*([A-Za-z][A-Za-z .'-]{1,40})\s*[:\-]\s+(.+)$")


def decode_document_bytes(data: bytes, filename: str = "") -> str:
    """Decode an uploaded brief to text without crashing.

    Text and Markdown decode as UTF-8. PDF input tries pypdf when it
    is installed, else falls back to a plain decode with control
    characters stripped. Always returns a string.
    """
    raw = bytes(data or b"")
    if not raw:
        return ""
    name = (filename or "").lower()
    is_pdf = name.endswith(".pdf") or raw[:5] == b"%PDF-"
    if is_pdf:
        try:
            import io

            from pypdf import PdfReader  # type: ignore[import-not-found]

            reader = PdfReader(io.BytesIO(raw))
            parts = []
            for page in reader.pages:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001, S112
                    continue
            text = "\n".join(part for part in parts if part.strip())
            if text.strip():
                return text
        except Exception:  # noqa: BLE001, S110
            pass
        text = raw.decode("utf-8", errors="ignore")
        cleaned = re.sub(r"[^\x09\x0a\x0d\x20-\x7e\u00a0-\u024f]", " ", text)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()
    return raw.decode("utf-8", errors="ignore")


def _coerce_estimate(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _estimate_from_text(text: str) -> float | None:
    match = _ESTIMATE_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _strip_estimate(text: str) -> str:
    return _ESTIMATE_RE.sub("", text or "").strip(" -,\t")


def parse_brief(text: str) -> list[dict[str, Any]]:
    """Parse a brief into draft dicts with title, type, and estimate.

    Headings map to epics and stories: lines starting with '# ' are
    epics, '## ' are stories, '- ' or '* ' are tasks. Lines starting
    with 'Epic:' or 'Story:' map the same way. A leading 'Name: task'
    prefix is kept as an owner hint. Estimates like '3h' are parsed
    and removed from the title. Empty input returns an empty list.
    """
    lines = [line.strip() for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    drafts: list[dict[str, Any]] = []
    for line in lines:
        item_type = "Task"
        title = line
        if line.startswith("# "):
            item_type = "Epic"
            title = line[2:].strip()
        elif line.startswith("## "):
            item_type = "Story"
            title = line[3:].strip()
        elif line.startswith("### "):
            item_type = "Task"
            title = line[4:].strip()
        elif line.lower().startswith("epic:"):
            item_type = "Epic"
            title = line[5:].strip()
        elif line.lower().startswith("story:"):
            item_type = "Story"
            title = line[6:].strip()
        elif line.startswith(("- ", "* ")):
            item_type = "Task"
            title = line[2:].strip()
        owner_hint = ""
        prefix = _OWNER_PREFIX_RE.match(title)
        if prefix and item_type == "Task":
            maybe_name = prefix.group(1).strip()
            if len(maybe_name.split()) <= 3 and len(maybe_name) <= 30:
                owner_hint = maybe_name
                title = prefix.group(2).strip()
        estimate = _estimate_from_text(title)
        title = _strip_estimate(title).strip()
        if not title:
            continue
        drafts.append(
            {
                "title": title,
                "type": item_type,
                "estimate": estimate,
                "description": title,
                "owner_hint": owner_hint,
            }
        )
    return drafts


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def initials_for(name: str) -> set[str]:
    """Return the initial forms for a roster name."""
    parts = [part for part in re.split(r"\s+", str(name or "").strip()) if part]
    if not parts:
        return set()
    firsts = [part[0].upper() for part in parts if part[0].isalpha()]
    out = set()
    if firsts:
        out.add("".join(firsts))
        out.add(firsts[0])
    return {entry for entry in out if entry}


def _name_in_text(name: str, text: str) -> bool:
    want = str(name or "").strip().lower()
    if not want:
        return False
    hay = str(text or "").lower()
    if want in hay:
        return True
    first = want.split()[0]
    return len(first) >= 3 and re.search(r"\b" + re.escape(first) + r"\b", hay) is not None


def _initials_in_text(name: str, text: str) -> bool:
    hay = str(text or "")
    for form in initials_for(name):
        if len(form) >= 2 and re.search(r"\b" + re.escape(form) + r"\b", hay):
            return True
    return False


def _skill_tokens(skills: str) -> set[str]:
    raw = str(skills or "").replace(",", " ").replace(";", " ").replace("/", " ")
    return {tok for tok in _tokens(raw) if len(tok) >= 3}


def _owner_of_item(item: Any) -> str:
    fields = dict(getattr(item, "fields", {}) or {})
    for key in ("owner", "assigned_to", "System.AssignedTo"):
        value = fields.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _hours_of_item(item: Any) -> float:
    fields = dict(getattr(item, "fields", {}) or {})
    for key in (
        "remaining",
        "Microsoft.VSTS.Scheduling.RemainingWork",
        "estimate",
        "Microsoft.VSTS.Scheduling.OriginalEstimate",
    ):
        value = _coerce_estimate(fields.get(key))
        if value is not None:
            return max(0.0, value)
    return 0.0


def current_load(board: Any) -> dict[str, float]:
    """Sum open work hours per owner from a board adapter."""
    if board is None:
        return {}
    try:
        items = list(board.list_items(limit=500))
    except Exception:  # noqa: BLE001
        return {}
    load: dict[str, float] = {}
    for item in items:
        if is_closed(getattr(item, "state", "")):
            continue
        owner = _owner_of_item(item)
        if not owner:
            continue
        load[owner] = load.get(owner, 0.0) + _hours_of_item(item)
    return load


def owner_history(board: Any) -> dict[str, set[str]]:
    """Map each owner to tokens from past titles, tags, and paths."""
    if board is None:
        return {}
    try:
        items = list(board.list_items(limit=500))
    except Exception:  # noqa: BLE001
        return {}
    history: dict[str, set[str]] = {}
    for item in items:
        owner = _owner_of_item(item)
        if not owner:
            continue
        fields = dict(getattr(item, "fields", {}) or {})
        text = str(getattr(item, "title", ""))
        for key in ("System.Tags", "tags", "System.AreaPath", "System.IterationPath"):
            value = fields.get(key)
            if value:
                text += " " + str(value)
        bag = history.setdefault(owner, set())
        bag |= {tok for tok in _tokens(text) if len(tok) >= 3}
    return history


def _normalize_roster(roster: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in roster or []:
        if isinstance(entry, str):
            name = entry.strip()
            if name:
                out.append({"name": name, "skills": "", "capacity": 0.0})
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
        out.append(
            {
                "name": name,
                "skills": str(entry.get("skills", "") or ""),
                "capacity": max(0.0, capacity),
            }
        )
    return out


def _free_hours(capacity: float, load: float) -> float:
    if capacity <= 0:
        return 0.0
    return capacity - load


def suggest_owners(
    drafts: list[dict[str, Any]],
    roster: Any,
    board: Any = None,
    source_text: str = "",
) -> list[dict[str, Any]]:
    """Suggest an owner for each draft in owner selection order.

    Order: (1) names or initials stated in the document win; (2) skill
    match from roster fields plus past assignments, area paths, and
    tags; (3) current load against capacity with a visible warning
    when capacity is exceeded; (4) Unassigned flagged as needing an
    owner. Every draft keeps its title, type, estimate, and state and
    gains owner, reason, confidence, warning, and needs_owner. Nothing
    is assigned without Approve; this only proposes.
    """
    people = _normalize_roster(roster)
    by_name = {entry["name"].lower(): entry for entry in people}
    load = current_load(board)
    history = owner_history(board)
    provisional = dict(load)
    enriched: list[dict[str, Any]] = []
    for draft in drafts or []:
        base = dict(draft) if isinstance(draft, dict) else {"title": str(draft)}
        title = str(base.get("title", "") or "").strip() or "Untitled"
        item_text = f"{title} {base.get('description', title)}"
        if base.get("owner_hint"):
            item_text += f" {base.get('owner_hint')}"
        estimate = _coerce_estimate(base.get("estimate")) or 0.0
        owner = ""
        reason = ""
        confidence = CONFIDENCE_LOW
        warning = False
        needs_owner = False
        hint = str(base.get("owner_hint", "") or "").strip()
        if hint and hint.lower() in by_name:
            owner = by_name[hint.lower()]["name"]
            reason = f"named in brief, assigned to {owner}."
            confidence = CONFIDENCE_HIGH
        if not owner:
            for entry in people:
                if _name_in_text(entry["name"], item_text):
                    owner = entry["name"]
                    reason = f"named in brief, assigned to {owner}."
                    confidence = CONFIDENCE_HIGH
                    break
            if not owner:
                for entry in people:
                    if _initials_in_text(entry["name"], item_text):
                        owner = entry["name"]
                        reason = f"initials in brief, assigned to {owner}."
                        confidence = CONFIDENCE_HIGH
                        break
        skill_word = ""
        if not owner and people:
            item_toks = {tok for tok in _tokens(item_text) if len(tok) >= 3}
            best: list[tuple[int, dict[str, Any], str]] = []
            for entry in people:
                skills = _skill_tokens(entry["skills"])
                past = history.get(entry["name"], set())
                match = sorted(item_toks & skills)
                past_match = sorted(item_toks & past)
                score = len(match) * 2 + len(past_match)
                if score > 0:
                    word = match[0] if match else past_match[0]
                    best.append((score, entry, word))
            if best:
                best.sort(
                    key=lambda row: (
                        -row[0],
                        provisional.get(row[1]["name"], 0.0),
                        row[1]["name"],
                    )
                )
                _, entry, skill_word = best[0]
                owner = entry["name"]
                confidence = CONFIDENCE_MEDIUM
        if not owner and people:
            ranked = sorted(
                people,
                key=lambda entry: (
                    provisional.get(entry["name"], 0.0)
                    - (entry["capacity"] if entry["capacity"] > 0 else 1e9),
                    provisional.get(entry["name"], 0.0),
                    entry["name"],
                ),
            )
            pick = None
            for entry in ranked:
                cap = entry["capacity"]
                used = provisional.get(entry["name"], 0.0)
                if cap <= 0 or used + estimate <= cap:
                    pick = entry
                    break
            if pick is None:
                pick = ranked[0]
            owner = pick["name"]
            confidence = CONFIDENCE_LOW
        if owner:
            used = provisional.get(owner, 0.0)
            cap = next(
                (entry["capacity"] for entry in people if entry["name"] == owner),
                0.0,
            )
            if not reason:
                if skill_word:
                    if cap > 0:
                        free = _free_hours(cap, used)
                        reason = (
                            f"{skill_word} match, "
                            f"{owner} has {free:.0f}h free."
                        )
                    else:
                        reason = f"{skill_word} match, assigned to {owner}."
                else:
                    if cap > 0:
                        free = _free_hours(cap, used)
                        reason = f"load balance, {owner} has {free:.0f}h free."
                    else:
                        reason = f"load balance, assigned to {owner}."
            if cap > 0 and used + estimate > cap:
                warning = True
                over = used + estimate - cap
                reason = reason.rstrip(".") + f", over capacity by {over:.0f}h."
            provisional[owner] = used + estimate
        else:
            needs_owner = True
            reason = "needs an owner."
            confidence = CONFIDENCE_LOW
        enriched.append(
            {
                "title": title,
                "type": str(base.get("type", "Task") or "Task"),
                "owner": owner or None,
                "estimate": _coerce_estimate(base.get("estimate")),
                "state": str(base.get("state", "To Do") or "To Do"),
                "description": str(base.get("description", title) or title),
                "reason": reason,
                "confidence": confidence,
                "warning": warning,
                "needs_owner": needs_owner,
            }
        )
    return enriched


def has_board_history(board: Any) -> bool:
    """Return True when the board holds any items to learn from."""
    if board is None:
        return False
    try:
        return len(list(board.list_items(limit=1))) > 0
    except Exception:  # noqa: BLE001
        return False


def propose_kickoff(
    document_text: str,
    roster: Any,
    board: Any = None,
    *,
    db: Any = None,
) -> dict[str, Any]:
    """Propose owned drafts from a brief, reusing propose_sprint.

    Parses the brief, builds base drafts with propose_sprint, then
    overlays owner suggestions. Returns items plus a has_history flag
    so the card can say when it fell back to names plus load. When a
    database is given, the roster is also saved to agent memory with
    the kickoff source noted.
    """
    text = str(document_text or "")
    if db is not None:
        try:
            from pam_pm.core import memory as memory_mod

            memory_mod.capture_roster(db, roster, source="kickoff")
        except Exception:  # noqa: BLE001, S110
            pass
    parsed = parse_brief(text)
    base = propose_sprint(
        [
            {
                "title": entry["title"],
                "type": entry["type"],
                "estimate": entry["estimate"],
                "description": entry["description"],
            }
            for entry in parsed
        ]
    )
    for index, entry in enumerate(parsed):
        if index < len(base):
            base[index]["owner_hint"] = entry.get("owner_hint", "")
    items = suggest_owners(base, roster, board, source_text=text)
    history = has_board_history(board)
    return {"items": items, "has_history": history}


def intake_document_bytes(
    data: bytes,
    roster: Any,
    board: Any = None,
    filename: str = "",
    *,
    db: Any = None,
) -> dict[str, Any]:
    """Propose a kickoff from uploaded or DM file bytes."""
    text = decode_document_bytes(data, filename)
    result = propose_kickoff(text, roster, board, db=db)
    result["source_text"] = text
    return result


def intake_slack_file(
    chat: Any,
    file_id: str,
    roster: Any,
    board: Any = None,
    *,
    db: Any = None,
) -> dict[str, Any]:
    """Download a Slack DM file and propose a kickoff from it."""
    data = chat.download_file(file_id)
    return intake_document_bytes(
        bytes(data or b""), roster, board, filename=str(file_id), db=db
    )


def approve_kickoff(
    proposal: Any,
    board: Any,
    *,
    writes: Any = None,
    db: Any = None,
    dry_run: bool = True,
    evidence_quote: str = "",
) -> list[Any]:
    """Create board items for an approved kickoff proposal.

    Thin wrapper over approve_proposal so kickoff and the sticky-note
    planner share one approval path with evidence and Undo.
    """
    return approve_proposal(
        proposal, board, writes=writes, db=db, dry_run=dry_run,
        evidence_quote=evidence_quote,
    )


def notify_owners(
    chat: Any, created: list[Any], proposal: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """DM each new owner with Accept or Flag buttons.

    Skips items without an owner. Returns one record per DM sent.
    """
    sent: list[dict[str, Any]] = []
    drafts = list(proposal or [])
    for index, item in enumerate(created or []):
        title = getattr(item, "title", "")
        item_id = str(getattr(item, "id", ""))
        owner = ""
        if index < len(drafts) and isinstance(drafts[index], dict):
            owner = str(drafts[index].get("owner") or "")
        if not owner:
            fields = dict(getattr(item, "fields", {}) or {})
            for key in ("owner", "assigned_to", "System.AssignedTo"):
                if fields.get(key):
                    owner = str(fields[key])
                    break
        if not owner:
            continue
        text = f"You have been assigned {title or item_id}. Accept or flag."
        try:
            mid = chat.send_buttons(
                owner,
                text,
                [
                    {"id": "accept", "label": "Accept", "value": item_id},
                    {"id": "flag", "label": "Flag", "value": item_id},
                ],
            )
        except Exception:  # noqa: BLE001, S112
            continue
        sent.append({"owner": owner, "item_id": item_id, "message_id": str(mid)})
    return sent


def parse_owner_reply(text: str) -> str:
    """Classify an owner DM reply as accept, flag, or unknown."""
    lowered = str(text or "").lower()
    if re.search(r"\b(flag|concern|blocked|wrong|cannot|can't|no)\b", lowered):
        return "flag"
    if re.search(r"\b(accept|ok|okay|yes|sure|will do|on it)\b", lowered):
        return "accept"
    return "unknown"


def handle_owner_response(item_id: str, text: str) -> dict[str, str]:
    """Map an owner reply to accepted or flagged for lead review."""
    decision = parse_owner_reply(text)
    if decision == "flag":
        return {"item_id": str(item_id), "status": "flagged"}
    if decision == "accept":
        return {"item_id": str(item_id), "status": "accepted"}
    return {"item_id": str(item_id), "status": "pending"}
