"""Interviewer agent with pushback on vague answers."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from pam_pm.agents.prompts.builder import build_messages
from pam_pm.core.schemas import InterviewResult

logger = logging.getLogger(__name__)

MAX_TURNS = 4

VAGUE_PHRASES = ("almost done", "soon", "working on it")

PLACEHOLDER_EVIDENCE_MARKERS = ("placeholder", "lorem", "todo")

_SYNTHETIC_PREFIXES = ("Reminder:", "Extract ")


def _has_user_turn(conversation: list[dict[str, Any]]) -> bool:
    """Return True when conversation holds one real non-empty user turn."""
    for turn in conversation or []:
        if not isinstance(turn, dict):
            continue
        if turn.get("role") != "user":
            continue
        content = turn.get("content", "")
        text = content if isinstance(content, str) else str(content or "")
        if not text.strip():
            continue
        if text.startswith(_SYNTHETIC_PREFIXES):
            continue
        return True
    return False


def _is_bad_evidence(quote: Any) -> bool:
    """Return True for empty or obvious placeholder evidence quotes."""
    if quote is None:
        return True
    text = quote if isinstance(quote, str) else str(quote)
    stripped = text.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    return any(marker in lowered for marker in PLACEHOLDER_EVIDENCE_MARKERS)


def _finish_args_evidence_is_bad(args: Any) -> bool:
    """Check raw finish args evidence before coercion backfill."""
    if isinstance(args, str):
        if not args.strip():
            return True
        try:
            data = json.loads(args)
        except ValueError:
            return True
        if not isinstance(data, dict):
            return True
        return _is_bad_evidence(data.get("evidence_quote", ""))
    if isinstance(args, dict):
        return _is_bad_evidence(args.get("evidence_quote", ""))
    return True

_REMAINING_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", re.IGNORECASE)
_ETA_DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}")
_ETA_DAY_RE = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow)\b",
    re.IGNORECASE,
)
_ETA_PREFIX_RE = re.compile(r"\beta?eta\b\s*[:\-]?\s*([^\n.,;]{1,40})", re.IGNORECASE)


def is_vague(text: str) -> bool:
    """Return True when the answer uses a known vague phrase."""
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in VAGUE_PHRASES)


def _person_id(person: Any) -> str:
    if isinstance(person, str):
        return person
    if isinstance(person, dict):
        for key in ("user_id", "id", "slack_id", "name"):
            value = person.get(key)
            if value:
                return str(value)
        return "user"
    for attr in ("user_id", "id", "slack_id", "name"):
        value = getattr(person, attr, None)
        if value:
            return str(value)
    return str(person)


def _normalize_items(items: Any) -> list[dict[str, str]]:
    if items is None:
        return []
    if isinstance(items, (str, dict)):
        raw_list: list[Any] = [items]
    elif isinstance(items, (list, tuple)):
        raw_list = list(items)
    else:
        raw_list = [items]
    norm: list[dict[str, str]] = []
    for entry in raw_list:
        if isinstance(entry, str):
            norm.append({"id": entry, "title": entry, "state": ""})
        elif isinstance(entry, dict):
            item_id = str(entry.get("id", entry.get("item_id", "")) or "")
            title = str(entry.get("title", item_id or "item") or "")
            state = str(entry.get("state", "") or "")
            if not item_id:
                item_id = title or f"item-{len(norm) + 1}"
            norm.append({"id": item_id, "title": title, "state": state})
        else:
            item_id = str(getattr(entry, "id", getattr(entry, "item_id", "")) or "")
            title = str(getattr(entry, "title", item_id or "item") or "")
            state = str(getattr(entry, "state", "") or "")
            if not item_id:
                item_id = title or f"item-{len(norm) + 1}"
            norm.append({"id": item_id, "title": title, "state": state})
    return norm


def _project_context(norm_items: list[dict[str, str]]) -> str:
    if not norm_items:
        return "Project standup. No active items."
    lines = ["Project standup. Items:"]
    for item in norm_items:
        lines.append(f"- {item['id']}: {item['title']} [{item['state']}]")
    return "\n".join(lines)


_PUNCT_GLUE_RE = re.compile(r"(?<=[.!?])(?=[^\s])")
_LOWER_UPPER_GLUE_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")

def normalize_followup_spacing(text: str) -> str:
    """Ensure one separator where sentences or ack plus question join."""
    out = (text or "").strip().replace("\n", " ")
    out = re.sub(r"\s+", " ", out).strip()
    out = _PUNCT_GLUE_RE.sub(" ", out)
    out = _LOWER_UPPER_GLUE_RE.sub(" ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def join_ack_question(ack: str, question: str, sep: str = " ") -> str:
    """Join ack and question with exactly one space or newline."""
    left = (ack or "").strip()
    right = (question or "").strip()
    if not left:
        return normalize_followup_spacing(right)
    if not right:
        return normalize_followup_spacing(left)
    if left[-1] not in ".!?":
        left += "."
    joiner = "\n" if sep == "\n" else " "
    return normalize_followup_spacing(left.rstrip() + joiner + right.lstrip())


def _one_question(text: str) -> str:
    cleaned = normalize_followup_spacing(text)
    if not cleaned:
        return "What is the current status?"
    if "?" not in cleaned:
        base = cleaned.rstrip(".! ").strip()
        if not base:
            return "What remains and what is your ETA?"
        return join_ack_question(base, "What remains and what is your ETA?")
    first = cleaned.split("?", 1)[0].strip()
    if not first:
        return "What remains and what is your ETA?"
    return normalize_followup_spacing(first + "?")


def _has_remaining(text: str) -> bool:
    return "remain" in (text or "").lower()


def _has_eta(text: str) -> bool:
    return "eta" in (text or "").lower()


def _clean_quote(text: str, limit: int = 200) -> str:
    cleaned = (text or "").strip().replace("\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.replace('"', "'").replace("?", ".")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip()
    return cleaned


def _last_user_text(conversation: list[dict[str, Any]]) -> str | None:
    for turn in reversed(conversation):
        if turn.get("role") == "user":
            content = turn.get("content", "")
            if isinstance(content, str) and content.strip():
                if content.startswith(("Reminder:", "Extract ")):
                    continue
                return content
    return None


def _default_question(item: dict[str, str]) -> str:
    return f"What is the status of {item['title']}?"


def _coerce_ts(value: Any) -> float | None:
    """Coerce a timestamp-like value to float, else None.

    Plain reply text and non-numeric ids return None so callers
    keep the no-timestamp accept behavior.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _reply_ts(reply: Any) -> float | None:
    """Extract a numeric timestamp from a reply, else None."""
    if isinstance(reply, str):
        return None
    if isinstance(reply, dict):
        for key in ("ts", "timestamp", "created", "created_at"):
            if reply.get(key) is not None:
                coerced = _coerce_ts(reply.get(key))
                if coerced is not None:
                    return coerced
        return None
    if isinstance(reply, (tuple, list)) and len(reply) == 2:
        return _coerce_ts(reply[0])
    for attr in ("ts", "timestamp", "created", "created_at"):
        coerced = _coerce_ts(getattr(reply, attr, None))
        if coerced is not None:
            return coerced
    return None


def _reply_text(reply: Any) -> str:
    if isinstance(reply, str):
        return reply
    if isinstance(reply, dict):
        return str(reply.get("text", ""))
    if isinstance(reply, (tuple, list)) and len(reply) == 2:
        return str(reply[1])
    return str(getattr(reply, "text", reply) or "")


def _is_stale(ts: float | None, after_ts: float | None) -> bool:
    return after_ts is not None and ts is not None and ts <= after_ts


def _next_answer(
    chat: Any,
    person_id: str,
    buffer: list[Any],
    after_ts: float | None = None,
) -> str | None:
    while buffer:
        entry = buffer.pop(0)
        if _is_stale(_reply_ts(entry), after_ts):
            continue
        return _reply_text(entry)
    try:
        replies = chat.read_replies(person_id)
    except TypeError:
        try:
            replies = chat.read_replies(person_id, None)
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None
    if not replies:
        return None
    fresh: list[tuple[float | None, str]] = []
    for reply in replies:
        ts = _reply_ts(reply)
        if _is_stale(ts, after_ts):
            continue
        text = _reply_text(reply)
        if text is None:
            continue
        fresh.append((ts, text))
    if not fresh:
        return None
    if len(fresh) > 1:
        for ts, text in fresh[1:]:
            buffer.append((ts, text))
    return fresh[0][1]


def _parse_model_result(data: Any) -> tuple[str, list[tuple[str, Any]]]:
    text = ""
    raw_calls: list[Any] = []
    if hasattr(data, "text") or hasattr(data, "tool_calls"):
        text = str(getattr(data, "text", "") or "")
        raw_calls = list(getattr(data, "tool_calls", []) or [])
    elif isinstance(data, dict):
        text = str(data.get("text", "") or "")
        raw_calls = list(data.get("tool_calls", []) or [])
    elif isinstance(data, str):
        text = data
    else:
        text = str(data or "")
    calls: list[tuple[str, Any]] = []
    for call in raw_calls:
        if hasattr(call, "name"):
            calls.append((str(call.name), getattr(call, "arguments", "")))
        elif isinstance(call, dict):
            name = str(call.get("name", ""))
            args = call.get("arguments", "")
            if not name and isinstance(call.get("function"), dict):
                fn = call["function"]
                name = str(fn.get("name", ""))
                args = fn.get("arguments", "")
            calls.append((name, args))
    return text, calls


def _find_finish(calls: list[tuple[str, Any]]) -> Any | None:
    for name, args in calls:
        if name == "finish_interview":
            return args
    return None


def _coerce_result(args: Any, conversation: list[dict[str, Any]]) -> InterviewResult:
    if isinstance(args, str):
        data = json.loads(args) if args.strip() else {}
    elif isinstance(args, dict):
        data = dict(args)
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}
    result = InterviewResult.model_validate(data)
    update: dict[str, Any] = {}
    if _is_bad_evidence(result.evidence_quote):
        last = _last_user_text(conversation)
        if last and not _is_bad_evidence(last):
            update["evidence_quote"] = _clean_quote(last, 500)
        else:
            update["evidence_quote"] = ""
    if not result.status.strip():
        last = _last_user_text(conversation)
        update["status"] = _clean_quote(last or "unknown", 200)
    if update:
        result = result.model_copy(update=update)
    return result


def _extract_remaining(text: str) -> float | None:
    match = _REMAINING_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _extract_eta(text: str) -> str | None:
    source = text or ""
    match = _ETA_DATE_RE.search(source)
    if match:
        return match.group(0)
    match = _ETA_DAY_RE.search(source)
    if match:
        return match.group(0)
    match = _ETA_PREFIX_RE.search(source)
    if match:
        value = match.group(1).strip().rstrip(".")
        if value:
            return value[:40]
    return None


def _extract_json_block(text: str) -> dict[str, Any] | None:
    source = text or ""
    start = source.find("{")
    end = source.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(source[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _parse_text_json(
    text: str, conversation: list[dict[str, Any]]
) -> InterviewResult | None:
    data = _extract_json_block(text)
    if data is None:
        return None
    try:
        result = InterviewResult.model_validate(data)
    except Exception:  # noqa: BLE001
        return None
    update: dict[str, Any] = {}
    if _is_bad_evidence(result.evidence_quote):
        last = _last_user_text(conversation)
        if last and not _is_bad_evidence(last):
            update["evidence_quote"] = _clean_quote(last, 500)
        else:
            update["evidence_quote"] = ""
    if update:
        result = result.model_copy(update=update)
    return result


def _build_fallback_result(conversation: list[dict[str, Any]]) -> InterviewResult:
    last = _last_user_text(conversation) or ""
    quote = _clean_quote(last, 500)
    if quote and _is_bad_evidence(quote):
        quote = ""
    remaining: float | None = None
    eta: str | None = None
    for turn in reversed(conversation):
        if turn.get("role") != "user":
            continue
        content = str(turn.get("content", "") or "")
        if remaining is None:
            remaining = _extract_remaining(content)
        if eta is None:
            eta = _extract_eta(content)
        if remaining is not None and eta is not None:
            break
    status = _clean_quote(last, 200) if last.strip() else "unknown"
    blocker: str | None = None
    if "block" in last.lower():
        blocker = _clean_quote(last, 200)
    return InterviewResult(
        status=status,
        remaining=remaining,
        eta=eta,
        evidence_quote=quote,
        blocker=blocker,
        blocker_owner=None,
        confidence=0.3,
    )


SLOT_REMAINING = "remaining_work"
SLOT_ETA = "eta"
SLOT_BLOCKERS = "blockers"
CHECKIN_SLOTS = (SLOT_REMAINING, SLOT_ETA, SLOT_BLOCKERS)

SLOT_LABELS = {
    SLOT_REMAINING: "remaining work",
    SLOT_ETA: "ETA",
    SLOT_BLOCKERS: "blockers",
}

_REMAINING_WORD_RE = re.compile(
    r"\b(remain\w*|left|retry|retest|test\w*|fix\w*|finish\w*|implement\w*|"
    r"cod\w+|review\w*|merge|deploy\w*|build\w*|debug\w*|refactor\w*|"
    r"polish\w*|wrapping up|docs?|migration|design\w*)\b",
    re.IGNORECASE,
)
_ETA_WORD_RE = re.compile(
    r"(noon|midnight|morning|afternoon|evening|\beod\b|\beow\b|\bcob\b|"
    r"today|tomorrow|done by|finish by|ready by|will finish|ship\w*|"
    r"o'?clock|\d\s*(?:am|pm)|end of (?:day|week))",
    re.IGNORECASE,
)
_BLOCKER_SLOT_RE = re.compile(
    r"\bblock(?:er|ers|ed|ing)?\b|impediment|waiting on|\bstuck\b|dependen\w*",
    re.IGNORECASE,
)
_BLOCKER_NEG_RE = re.compile(
    r"\bno\s+block(?:er|ers)?\b|\bnot\s+blocked\b|nothing\s+blocking|"
    r"\bno\s+impediment|^\s*none\.?\s*$",
    re.IGNORECASE,
)


def _answer_slots(answer: Any) -> dict[str, str]:
    """Map one answer to the check-in slots it fills, with evidence."""
    text = answer if isinstance(answer, str) else str(answer or "")
    if not text.strip():
        return {}
    vague = is_vague(text)
    slots: dict[str, str] = {}
    evidence = text.strip()[:200]
    if _extract_remaining(text) is not None or (
        not vague and _REMAINING_WORD_RE.search(text)
    ):
        slots[SLOT_REMAINING] = evidence
    if _extract_eta(text) is not None or (not vague and _ETA_WORD_RE.search(text)):
        slots[SLOT_ETA] = evidence
    if _BLOCKER_SLOT_RE.search(text):
        slots[SLOT_BLOCKERS] = evidence
    if not slots and not vague and len(text.strip()) >= 2:
        slots[SLOT_REMAINING] = evidence
    return slots


def filled_slots(conversation: list[dict[str, Any]]) -> dict[str, str]:
    """Return check-in slots filled by user turns, with first evidence."""
    filled: dict[str, str] = {}
    for turn in conversation or []:
        if not isinstance(turn, dict) or turn.get("role") != "user":
            continue
        content = turn.get("content", "")
        text = content if isinstance(content, str) else str(content or "")
        if text.startswith(("Reminder:", "Extract ")):
            continue
        for slot, evidence in _answer_slots(text).items():
            filled.setdefault(slot, evidence)
    return filled


def missing_slots(conversation: list[dict[str, Any]]) -> list[str]:
    """Return check-in slots no user turn has filled yet."""
    filled = filled_slots(conversation)
    return [slot for slot in CHECKIN_SLOTS if slot not in filled]


def _build_slot_result(
    conversation: list[dict[str, Any]], filled: dict[str, str]
) -> InterviewResult:
    """Build a result from filled slots without asking the model."""
    remaining: float | None = None
    eta: str | None = None
    for turn in reversed(conversation):
        if not isinstance(turn, dict) or turn.get("role") != "user":
            continue
        content = str(turn.get("content", "") or "")
        if remaining is None:
            remaining = _extract_remaining(content)
        if eta is None:
            eta = _extract_eta(content)
    blocker: str | None = None
    blocker_evidence = filled.get(SLOT_BLOCKERS, "")
    if blocker_evidence and not _BLOCKER_NEG_RE.search(blocker_evidence):
        blocker = _clean_quote(blocker_evidence, 200)
    parts = [filled[slot] for slot in CHECKIN_SLOTS if filled.get(slot)]
    status = _clean_quote("; ".join(parts), 200) if parts else "unknown"
    last = _last_user_text(conversation) or ""
    quote = _clean_quote(last, 500)
    if _is_bad_evidence(quote):
        quote = ""
    gaps = [slot for slot in CHECKIN_SLOTS if slot not in filled]
    return InterviewResult(
        status=status,
        remaining=remaining,
        eta=eta,
        evidence_quote=quote,
        blocker=blocker,
        blocker_owner=None,
        confidence=0.7 if not gaps else 0.4,
    )


def _model_thanks(
    model_client: Any,
    project_context: str,
    conversation: list[dict[str, Any]],
    result: InterviewResult,
    item_id: str,
) -> str:
    """Ask the model to write the closing thanks message.

    Falls back to the standard recorded line when the model is
    unavailable or answers with another question.
    """
    fallback = f"Thanks. I recorded this for {item_id}: {result.status}"
    try:
        payload = build_messages(
            "interviewer",
            project_context,
            conversation
            + [
                {
                    "role": "user",
                    "content": (
                        "All check-in slots are filled. Write one short "
                        "thanks message confirming what was recorded. "
                        "No questions."
                    ),
                }
            ],
            {"item_id": item_id, "phase": "thanks"},
        )
        try:
            raw = model_client.chat(payload["messages"], tools=payload["tools"])
        except TypeError:
            raw = model_client.chat(payload["messages"], payload["tools"])
    except Exception:  # noqa: BLE001
        return fallback
    text, _ = _parse_model_result(raw)
    cleaned = normalize_followup_spacing(text)
    if not cleaned or "?" in cleaned:
        return fallback
    return cleaned[:500]


def _note_gaps_on_board(board: Any, item_id: str, note: str) -> None:
    """Post the honest gap note next to the capped check-in write."""
    if not note:
        return
    add_comment = getattr(board, "add_comment", None)
    if not callable(add_comment):
        return
    try:
        add_comment(item_id, note, author="Pam")
    except Exception:  # noqa: BLE001, S110
        pass


def format_checkin_comment(result: InterviewResult) -> str:
    """Render one interview result as a board comment line."""
    head = f"Check-in: {result.status}".strip()
    parts = [head] if head else []
    if result.remaining is not None:
        parts.append(f"Remaining {result.remaining}h")
    if result.eta:
        parts.append(f"ETA {result.eta}")
    if result.evidence_quote:
        parts.append(f'Evidence: "{result.evidence_quote[:200]}"')
    return ". ".join(parts) + "."


def write_interview_result(
    board: Any, item_id: str, result: InterviewResult
) -> dict[str, Any]:
    """Write one interview result to the board.

    Updates RemainingWork through the canonical remaining field and
    posts the outcome as a comment. Same call shapes on Pam board
    and ADO, so both backends behave the same way. The comment is
    attempted even when the field update fails, and the outcome
    reports which fields persisted so callers never claim a write
    that did not stick.
    """
    evidence = result.evidence_quote
    if _is_bad_evidence(evidence):
        evidence = ""
    payload = {
        "state": result.status[:80] if result.status else "",
        "remaining": result.remaining,
        "eta": result.eta,
        "evidence_quote": evidence,
    }
    sent = [key for key, val in payload.items() if val is not None]
    updated: Any = None
    update_error: str | None = None
    applied: list[str] = []
    rejected: dict[str, str] = {}
    try:
        updated = board.update_item(item_id, payload)
    except Exception as exc:  # noqa: BLE001
        update_error = str(exc) or type(exc).__name__
        applied = list(getattr(exc, "applied", []) or [])
        raw_rejected = getattr(exc, "rejected", None)
        if isinstance(raw_rejected, dict):
            rejected = {str(k): str(v) for k, v in raw_rejected.items()}
        if not rejected and not applied:
            rejected = {key: update_error for key in sent}
    else:
        fields = getattr(updated, "fields", None)
        if isinstance(fields, dict) and "applied_fields" in fields:
            applied = [str(k) for k in (fields.get("applied_fields") or [])]
            raw = fields.get("rejected_fields") or {}
            if isinstance(raw, dict):
                rejected = {str(k): str(v) for k, v in raw.items()}
        else:
            applied = list(sent)
    comment: Any = None
    comment_error: str | None = None
    add_comment = getattr(board, "add_comment", None)
    if callable(add_comment):
        try:
            comment = add_comment(
                item_id, format_checkin_comment(result), author="Pam"
            )
        except Exception as exc:  # noqa: BLE001
            comment_error = str(exc) or type(exc).__name__
    return {
        "updated": updated,
        "comment": comment,
        "applied_fields": applied,
        "rejected_fields": rejected,
        "update_error": update_error,
        "comment_error": comment_error,
    }


def write_persisted(outcome: Any) -> bool:
    """Return True when at least the core evidence reached the board."""
    if not isinstance(outcome, dict):
        return False
    if outcome.get("comment") is not None:
        return True
    return bool(outcome.get("applied_fields"))


def write_failure_reason(outcome: Any) -> str:
    """Summarize why a board write persisted nothing, without secrets."""
    if not isinstance(outcome, dict):
        return "board write failed"
    rejected = outcome.get("rejected_fields") or {}
    if isinstance(rejected, dict) and rejected:
        key = min(rejected)
        detail = str(rejected[key])[:200].strip()
        if detail:
            return f"{key}: {detail}"
        return str(key)
    for entry in (outcome.get("update_error"), outcome.get("comment_error")):
        if entry:
            return str(entry)[:200].strip()
    return "board write failed"


def _safe_board_update(
    board: Any, item_id: str, result: InterviewResult
) -> dict[str, Any]:
    if board is None:
        return {
            "updated": None,
            "comment": None,
            "applied_fields": [],
            "rejected_fields": {},
            "update_error": "no board configured",
            "comment_error": None,
        }
    try:
        return write_interview_result(board, item_id, result)
    except Exception as exc:  # noqa: BLE001
        reason = str(exc) or type(exc).__name__
        return {
            "updated": None,
            "comment": None,
            "applied_fields": [],
            "rejected_fields": {},
            "update_error": reason,
            "comment_error": None,
        }


FINISHED_STATUS = "finished"

NO_OPEN_STATE_REPLY = (
    "Thanks for the update. I have no open check-in for you right now, "
    "so I did not record anything. Your next check-in will pick this up."
)


def _save_interview_state(
    db: Any,
    person_id: str,
    item_id: str,
    turns: list[dict[str, Any]],
    last_seen: str = "",
    status: str = "open",
) -> None:
    """Persist interview turns through the durable conversations path."""
    if db is None:
        return
    try:
        from pam_pm.core import recovery as recovery_mod

        key = recovery_mod.thread_key(str(person_id), str(item_id))
        recovery_mod.save_conversation(
            db,
            key,
            str(person_id),
            str(item_id),
            [dict(turn) for turn in (turns or [])],
            str(last_seen or ""),
            str(status or "open"),
        )
    except Exception:  # noqa: BLE001
        return


_DISPLAY_NAME_CACHE: dict[str, str] = {}

_ID_LIKE_RE = re.compile(r"^[UW][A-Z0-9]{5,}$")


def _parse_slack_user_name(user: Any) -> str:
    """Return the best display name from a Slack users_info user object."""
    if not isinstance(user, dict):
        return ""
    profile = user.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    for key in ("display_name", "real_name"):
        text = str(profile.get(key, "") or "").strip()
        if text:
            return text
    for key in ("real_name", "name"):
        text = str(user.get(key, "") or "").strip()
        if text:
            return text
    return ""


def _resolve_display_name(chat: Any, user_id: str) -> str | None:
    """Map a Slack user ID to a roster display name, cached.

    Tries the chat adapter's resolve helpers first, then Slack
    users_info for ID-like values. Returns None when unresolvable.
    """
    want = str(user_id or "").strip()
    if not want:
        return None
    cached = _DISPLAY_NAME_CACHE.get(want)
    if cached:
        return cached
    if chat is None:
        return None
    try:
        for helper in (
            "display_name_for_user",
            "resolve_display_name",
            "user_display_name",
            "name_for_user",
        ):
            fn = getattr(chat, helper, None)
            if not callable(fn):
                continue
            try:
                name = str(fn(want) or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.info("display name helper %s failed for %s: %s", helper, want, exc)
                continue
            if name and name != want:
                _DISPLAY_NAME_CACHE[want] = name
                return name
        cache = getattr(chat, "_display_cache", None)
        if isinstance(cache, dict):
            name = str(cache.get(want, "") or "").strip()
            if name:
                _DISPLAY_NAME_CACHE[want] = name
                return name
        if not _ID_LIKE_RE.match(want.upper()):
            return None
        client = None
        direct = getattr(chat, "_client", None)
        if direct is not None and not callable(direct):
            client = direct
        if client is None:
            direct = getattr(chat, "client", None)
            if direct is not None and not callable(direct):
                client = direct
        if client is None:
            factory = getattr(chat, "_client_or_raise", None)
            if callable(factory):
                try:
                    client = factory()
                except Exception:  # noqa: BLE001
                    client = None
        users_info = getattr(client, "users_info", None) if client is not None else None
        if not callable(users_info):
            return None
        try:
            resp = users_info(user=want)
        except TypeError:
            try:
                resp = users_info(want)
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None
        data = getattr(resp, "data", resp)
        if isinstance(data, dict):
            name = _parse_slack_user_name(data.get("user", {}))
            if name:
                _DISPLAY_NAME_CACHE[want] = name
                return name
    except Exception:  # noqa: BLE001
        return None
    return None


def resolve_display_name(chat: Any, user_id: str) -> str | None:
    """Public alias for router and adapter use."""
    return _resolve_display_name(chat, user_id)


def _candidate_identities(chat: Any, person_id: str) -> list[str]:
    """Return lookup identities: raw value plus any resolved display name."""
    want = str(person_id or "")
    candidates = [want]
    if want:
        resolved = _resolve_display_name(chat, want)
        if resolved and resolved not in candidates:
            candidates.append(resolved)
    return candidates


def _load_open_state(
    db: Any, person_id: str, chat: Any = None
) -> dict[str, Any] | None:
    """Return the newest open interview state for a person, or None.

    Matches stored state on either the raw identity (Slack user ID
    or roster name) or the display name resolved from it, so inbound
    events carrying a user ID find state stored under the roster name.
    """
    if db is None:
        return None
    try:
        from pam_pm.core import recovery as recovery_mod

        wants = _candidate_identities(chat, person_id)
        lowered = {str(w).lower() for w in wants}
        matches = [
            conv
            for conv in recovery_mod.list_open_conversations(db)
            if not str(conv.get("thread_key", "") or "").startswith("blocker:")
            and (
                str(conv.get("person", "") or "") in wants
                or str(conv.get("person", "") or "").lower() in lowered
            )
        ]
    except Exception:  # noqa: BLE001
        return None
    if not matches:
        return None
    return matches[-1]


def _call_model_once(
    model_client: Any,
    project_context: str,
    conversation: list[dict[str, Any]],
    volatile: dict[str, Any],
) -> Any:
    payload = build_messages("interviewer", project_context, conversation, volatile)
    try:
        return model_client.chat(payload["messages"], tools=payload["tools"])
    except TypeError:
        return model_client.chat(payload["messages"], payload["tools"])


def _capture_resume_memory(
    db: Any, person_id: str, conversation: list[dict[str, Any]]
) -> None:
    if db is None:
        return
    try:
        from pam_pm.core import memory as memory_mod

        said = [
            str(turn.get("content", ""))
            for turn in conversation
            if turn.get("role") == "user"
            and str(turn.get("content", "")).strip()
            and not str(turn.get("content", "")).startswith(("Reminder:", "Extract "))
        ]
        memory_mod.capture_interview(db, person_id, said, source="interview")
    except Exception:  # noqa: BLE001
        return


_BLOCKER_EXTRA_MARKS = (
    "can't proceed",
    "cant proceed",
    "cannot proceed",
    "can not proceed",
    "dependencies",
    "depends on",
    "dependent on",
    "waiting for",
    "blocked by",
    "blocked",
    "blocker",
)

_BLOCKER_LINK_REL = "System.LinkTypes.Dependency-Forward"


def _looks_like_blocker(text: str) -> bool:
    """Return True when free text reads like a blocker report."""
    try:
        from pam_pm.rules.engine import is_blocker_message as _engine_match

        if _engine_match(text or ""):
            return True
    except Exception:  # noqa: BLE001, S110
        pass
    lowered = (text or "").lower().replace("’", "'").replace("‘", "'")
    squashed = lowered.replace("'", "")
    for mark in _BLOCKER_EXTRA_MARKS:
        if mark in lowered or mark.replace("'", "") in squashed:
            return True
    return False


def _blocker_item_id(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id", item.get("item_id", "")) or "")
    return str(getattr(item, "id", getattr(item, "item_id", "")) or "")


def _blocker_item_title(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("title", "") or _blocker_item_id(item))
    return str(getattr(item, "title", "") or _blocker_item_id(item))


def _blocker_item_state(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("state", "") or "")
    return str(getattr(item, "state", "") or "")


def _blocker_owner_name(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("displayName", "display_name", "name", "uniqueName"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return ""
    return str(value or "").strip()


def _blocker_item_owner(item: Any) -> str:
    if isinstance(item, dict):
        fields = item.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}
        for key in ("assigned_to", "owner", "assignee", "user_id"):
            value = item.get(key) or fields.get(key)
            if value:
                return _blocker_owner_name(value)
        return _blocker_owner_name(fields.get("System.AssignedTo", ""))
    for key in ("assigned_to", "owner", "assignee", "user_id"):
        value = getattr(item, key, None)
        if value:
            return _blocker_owner_name(value)
    fields = getattr(item, "fields", None)
    if isinstance(fields, dict):
        for key in ("assigned_to", "owner", "assignee", "user_id"):
            value = fields.get(key)
            if value:
                return _blocker_owner_name(value)
        return _blocker_owner_name(fields.get("System.AssignedTo", ""))
    return ""


def _find_open_item_for(board: Any, candidates: list[str]) -> Any | None:
    """Return the first open board item owned by one of the candidates."""
    if board is None:
        return None
    try:
        items = list(board.list_items(limit=200) or [])
    except Exception:  # noqa: BLE001
        return None
    try:
        from pam_pm.adapters.pm.base import is_closed as _is_closed
    except Exception:  # noqa: BLE001
        _is_closed = None
    wants = {str(entry) for entry in candidates if str(entry)}
    lowered = {str(entry).lower() for entry in candidates if str(entry)}
    for item in items:
        owner = _blocker_item_owner(item)
        if owner not in wants and owner.lower() not in lowered:
            continue
        state = _blocker_item_state(item)
        try:
            closed = bool(_is_closed(state)) if _is_closed is not None else False
        except Exception:  # noqa: BLE001
            closed = False
        if not closed:
            return item
    return None


_BLOCKER_STOPWORDS = frozenset(
    {
        "i", "im", "m", "blocked", "blocker", "blockers", "blocking",
        "cant", "cannot", "can", "not", "no", "the", "a", "an", "on",
        "in", "is", "are", "was", "were", "be", "to", "for", "of",
        "and", "or", "so", "it", "its", "this", "that", "with", "by",
        "from", "as", "at", "my", "me", "we", "our", "you", "your",
        "they", "their", "he", "she", "his", "her", "but", "if",
        "then", "than", "too", "very", "just", "still", "already",
        "finish", "finishing", "work", "working", "done", "get",
        "got", "need", "needs", "s", "t", "d",
    }
)

_BLOCKER_CONFIRM_PHRASES = (
    "yes", "yeah", "yep", "confirm", "confirmed", "correct",
    "right", "ok", "okay", "looks good", "sounds good",
    "go ahead", "do it", "approved", "approve", "lgtm",
)

_BLOCKER_HEDGE_WORDS = ("but", "however", "instead", "change", "not yet", "wait")


def _blocker_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _BLOCKER_STOPWORDS and len(w) > 1}


def _title_score(title: str, report_tokens: set[str]) -> int:
    if not report_tokens:
        return 0
    title_tokens = _blocker_tokens(title)
    if not title_tokens:
        return 0
    return len(title_tokens & report_tokens)


def _match_blocked_item(
    report: str, candidates: list[str], board: Any, db: Any = None
) -> Any | None:
    if board is None:
        return None
    try:
        items = list(board.list_items(limit=200) or [])
    except Exception:  # noqa: BLE001
        return None
    try:
        from pam_pm.adapters.pm.base import is_closed as _is_closed
    except Exception:  # noqa: BLE001
        _is_closed = None
    wants = {str(e) for e in candidates if str(e)}
    lowered = {str(e).lower() for e in candidates if str(e)}
    mine: list[Any] = []
    for item in items:
        owner = _blocker_item_owner(item)
        if owner not in wants and owner.lower() not in lowered:
            continue
        state = _blocker_item_state(item)
        try:
            closed = bool(_is_closed(state)) if _is_closed is not None else False
        except Exception:  # noqa: BLE001
            closed = False
        if not closed:
            mine.append(item)
    if not mine:
        return None
    tokens = _blocker_tokens(report)
    best: Any | None = None
    best_score = 0
    for item in mine:
        score = _title_score(_blocker_item_title(item), tokens)
        if score > best_score:
            best_score = score
            best = item
    if best is not None and best_score > 0:
        return best
    if db is not None:
        try:
            from pam_pm.core import recovery as recovery_mod

            for cand in candidates:
                latest = recovery_mod.load_latest_for_person(db, cand)
                if not latest:
                    continue
                want_id = str(latest.get("item_id", "") or "")
                for item in mine:
                    if want_id and _blocker_item_id(item) == want_id:
                        return item
        except Exception:  # noqa: BLE001, S110
            pass
    return mine[0]


def _match_blocking_item(
    report: str, board: Any, blocked_id: str, reporter_candidates: list[str]
) -> Any | None:
    if board is None:
        return None
    try:
        items = list(board.list_items(limit=200) or [])
    except Exception:  # noqa: BLE001
        return None
    try:
        from pam_pm.adapters.pm.base import is_closed as _is_closed
    except Exception:  # noqa: BLE001
        _is_closed = None
    wants = {str(e) for e in reporter_candidates if str(e)}
    lowered = {str(e).lower() for e in reporter_candidates if str(e)}
    tokens = _blocker_tokens(report)
    best_other: Any | None = None
    best_other_score = 0
    best_mine: Any | None = None
    best_mine_score = 0
    for item in items:
        item_id = _blocker_item_id(item)
        if blocked_id and item_id == blocked_id:
            continue
        state = _blocker_item_state(item)
        try:
            closed = bool(_is_closed(state)) if _is_closed is not None else False
        except Exception:  # noqa: BLE001
            closed = False
        if closed:
            continue
        score = _title_score(_blocker_item_title(item), tokens)
        if score <= 0:
            continue
        owner = _blocker_item_owner(item)
        is_mine = owner in wants or owner.lower() in lowered
        if is_mine:
            if score > best_mine_score:
                best_mine_score = score
                best_mine = item
        else:
            if score > best_other_score:
                best_other_score = score
                best_other = item
    if best_other is not None:
        return best_other
    return best_mine


def _has_checkin_history(db: Any, candidates: list[str]) -> bool:
    if db is None:
        return False
    try:
        from pam_pm.core import recovery as recovery_mod

        for cand in candidates:
            if recovery_mod.load_latest_for_person(db, cand) is not None:
                return True
        wants = {str(e) for e in candidates if str(e)}
        lowered = {str(e).lower() for e in candidates if str(e)}
        for conv in recovery_mod.list_open_conversations(db):
            person = str(conv.get("person", "") or "")
            if person in wants or person.lower() in lowered:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _pending_blocker_key(person_id: str) -> str:
    from pam_pm.core import recovery as recovery_mod

    return recovery_mod.thread_key(str(person_id), "pending", kind="blocker")


def _load_pending_blocker(
    db: Any, person_id: str, chat: Any = None
) -> dict[str, Any] | None:
    if db is None:
        return None
    try:
        from pam_pm.core import recovery as recovery_mod

        for cand in _candidate_identities(chat, person_id):
            key = recovery_mod.thread_key(str(cand), "pending", kind="blocker")
            conv = recovery_mod.load_conversation(db, key)
            if conv is None or str(conv.get("status", "")) != "open":
                continue
            turns = conv.get("turns", []) or []
            for turn in turns:
                if not isinstance(turn, dict):
                    continue
                content = str(turn.get("content", "") or "")
                if not content.strip().startswith("{"):
                    continue
                try:
                    data = json.loads(content)
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("pending_blocker"):
                    data["_thread_key"] = key
                    data["_person"] = str(conv.get("person", "") or cand)
                    return data
    except Exception:  # noqa: BLE001
        return None
    return None


def _save_pending_blocker(
    db: Any, person_id: str, pending: dict[str, Any]
) -> None:
    if db is None:
        return
    try:
        from pam_pm.core import recovery as recovery_mod

        key = _pending_blocker_key(person_id)
        body = dict(pending)
        body["pending_blocker"] = True
        recovery_mod.save_conversation(
            db, key, str(person_id), str(body.get("blocked_id", "") or "pending"),
            [{"role": "system", "content": json.dumps(body)}], "", "open",
        )
    except Exception:  # noqa: BLE001, S110
        pass


def _clear_pending_blocker(db: Any, person_id: str, chat: Any = None) -> None:
    if db is None:
        return
    try:
        from pam_pm.core import recovery as recovery_mod

        keys = {_pending_blocker_key(person_id)}
        for cand in _candidate_identities(chat, person_id):
            keys.add(recovery_mod.thread_key(str(cand), "pending", kind="blocker"))
        for key in keys:
            conv = recovery_mod.load_conversation(db, key)
            if conv is None:
                continue
            recovery_mod.save_conversation(
                db, key, str(conv.get("person", "") or person_id),
                str(conv.get("item_id", "") or "pending"),
                list(conv.get("turns", []) or []), str(conv.get("last_seen", "") or ""),
                "finished",
            )
    except Exception:  # noqa: BLE001, S110
        pass


def _is_blocker_confirmed(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered.strip():
        return False
    if not any(p in lowered for p in _BLOCKER_CONFIRM_PHRASES):
        return False
    if any(h in lowered for h in ("but", "however", "instead", "not yet", "wait")):
        return False
    return not (
        re.search(r"\bno\b|\bnot\b", lowered) and "not sure" not in lowered
    )


def _handle_blocker_clarify(
    person_id: str, answer: str, chat: Any, board: Any, db: Any,
    candidates: list[str],
) -> dict[str, Any]:
    report = answer.strip()[:500]
    blocked = _match_blocked_item(answer, candidates, board, db)
    blocked_id = _blocker_item_id(blocked) if blocked is not None else ""
    blocked_title = (
        _blocker_item_title(blocked) if blocked is not None
        else f"Blocked work for {person_id}"
    )
    blocking = _match_blocking_item(answer, board, blocked_id, candidates)
    blocking_id = _blocker_item_id(blocking) if blocking is not None else ""
    blocking_title = (
        _blocker_item_title(blocking) if blocking is not None
        else f"Blocking: {answer.strip()[:60]}" if answer.strip()
        else "Blocking item"
    )
    question = (
        f"Looks like '{blocked_title}' is blocked by '{blocking_title}'. "
        "Is that right?"
    )
    try:
        mid = str(chat.send_dm(person_id, normalize_followup_spacing(question)) or "")
    except Exception:  # noqa: BLE001
        mid = ""
    _save_pending_blocker(db, person_id, {
        "blocked_id": blocked_id, "blocked_title": blocked_title,
        "blocking_id": blocking_id, "blocking_title": blocking_title,
        "report": report,
    })
    return {
        "kind": "blocker_clarify", "replied": bool(mid), "person": person_id,
        "item_id": blocked_id, "blocking_item_id": blocking_id,
        "blocked_title": blocked_title, "blocking_title": blocking_title,
        "message_id": mid, "question": question,
    }


def _handle_blocker_confirmation(
    person_id: str, answer: str, chat: Any, board: Any, db: Any,
    pending: dict[str, Any],
) -> dict[str, Any]:
    if not _is_blocker_confirmed(answer):
        _clear_pending_blocker(db, pending.get("_person", person_id))
        try:
            mid = str(chat.send_dm(
                person_id, normalize_followup_spacing(
                    "Okay, I will not link this yet. "
                    "Tell me what is blocked and what blocks it."
                )) or "")
        except Exception:  # noqa: BLE001
            mid = ""
        return {
            "kind": "blocker_declined", "replied": bool(mid),
            "person": person_id, "message_id": mid,
        }
    report = str(pending.get("report", "") or answer.strip())[:500] or "blocker intake"
    blocked_id = str(pending.get("blocked_id", "") or "")
    blocking_id = str(pending.get("blocking_id", "") or "")
    blocked_title = str(pending.get("blocked_title", "") or "")
    blocking_title = str(pending.get("blocking_title", "") or "")
    if not blocked_id and board is not None:
        try:
            created = board.create_item(
                blocked_title or f"Blocked work for {person_id}",
                {"assigned_to": person_id},
            )
            blocked_id = _blocker_item_id(created)
            blocked_title = _blocker_item_title(created)
        except Exception:  # noqa: BLE001
            blocked_id = ""
    if not blocking_id and board is not None:
        try:
            raw_states = board.list_states() or []
            states = [str(e) for e in raw_states if str(e)]
            created = board.create_item(
                blocking_title or "Blocking item",
                {"state": states[0] if states else "To Do"},
            )
            blocking_id = _blocker_item_id(created)
            blocking_title = _blocker_item_title(created)
        except Exception:  # noqa: BLE001
            blocking_id = ""
    if board is not None and blocked_id:
        try:
            current = board.get_item(blocked_id)
            fields = current.get("fields", {}) if isinstance(current, dict) else getattr(current, "fields", {})
            tags = list((fields or {}).get("tags", []) or [])
            if "blocked" not in [str(t).lower() for t in tags]:
                tags.append("blocked")
            board.update_item(blocked_id, {"tags": tags})
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            add_comment = getattr(board, "add_comment", None)
            if callable(add_comment):
                add_comment(blocked_id, f"Blocker reported: {report}", author="Pam")
        except Exception:  # noqa: BLE001, S110
            pass
    link: Any = None
    if board is not None and blocked_id and blocking_id:
        try:
            link = board.add_link(blocking_id, blocked_id, _BLOCKER_LINK_REL)
        except Exception:  # noqa: BLE001
            link = None
    if db is not None and blocked_id:
        try:
            from pam_pm.core.writes import WriteService

            service = WriteService(db, dry_run=True)
            service.propose({
                "operation": "add_link", "item_id": blocked_id,
                "evidence_quote": report, "status": "proposed",
                "detail": f"Predecessor {blocking_id} Successor {blocked_id}",
            })
        except Exception:  # noqa: BLE001, S110
            pass
    try:
        link_mid = str(chat.send_dm(
            person_id, normalize_followup_spacing(
                f"Linked '{blocked_title}' as blocked by '{blocking_title}'."
            )) or "")
    except Exception:  # noqa: BLE001
        link_mid = ""
    plan_out: dict[str, Any] = {}
    if board is not None and blocking_id:
        try:
            blocking_item: Any = board.get_item(blocking_id)
        except Exception:  # noqa: BLE001
            blocking_item = None
        owner = _blocker_item_owner(blocking_item) if blocking_item is not None else ""
        if owner:
            try:
                from pam_pm.rules.templates.blocker_plan import blocker_action_plan

                plan_out = blocker_action_plan(
                    owner, person_id, chat, board, None, db=db,
                    blocking_item_id=blocking_id, blocked_item_id=blocked_id,
                )
            except Exception:  # noqa: BLE001
                plan_out = {}
    _clear_pending_blocker(db, pending.get("_person", person_id))
    return {
        "kind": "blocker_linked", "replied": True, "person": person_id,
        "item_id": blocked_id, "blocking_item_id": blocking_id,
        "blocked_title": blocked_title, "blocking_title": blocking_title,
        "message_id": link_mid, "link": link, "plan": plan_out,
        "eta": str(plan_out.get("eta", "") or "") if plan_out else "",
    }


def _handle_blocker_without_state(
    person_id: str,
    answer: str,
    chat: Any,
    board: Any,
    db: Any,
    candidates: list[str],
) -> dict[str, Any]:
    """Link a blocker reported with no open check-in to the open item.

    Finds the reporter's open item (creating one when the board has
    none), flags it, links a blocking item to it, proposes the writes
    with the report as evidence, and tells the reporter what happened.
    Never sends the generic no-open-state shrug for blocker language.
    """
    evidence = answer.strip()[:500]

    def _send(text: str) -> str:
        try:
            return str(chat.send_dm(person_id, normalize_followup_spacing(text)) or "")
        except Exception:  # noqa: BLE001
            return ""

    blocked = _find_open_item_for(board, candidates)
    if blocked is None and board is not None:
        try:
            blocked = board.create_item(
                f"Blocked work for {person_id}",
                {"assigned_to": person_id},
            )
        except Exception:  # noqa: BLE001
            blocked = None
    if blocked is None:
        mid = _send(
            "Got it, you are blocked. I have no board access right now, "
            f"so I could not link this yet: {evidence[:200]}"
        )
        return {
            "kind": "blocker_no_state",
            "replied": bool(mid),
            "person": person_id,
            "item_id": "",
            "message_id": mid,
        }
    blocked_id = _blocker_item_id(blocked)
    title = _blocker_item_title(blocked)
    try:
        if isinstance(blocked, dict):
            fields = blocked.get("fields", {})
        else:
            current = board.get_item(blocked_id)
            if isinstance(current, dict):
                fields = current.get("fields", {})
            else:
                fields = getattr(current, "fields", {})
        tags = list((fields or {}).get("tags", []) or [])
        if "blocked" not in [str(tag).lower() for tag in tags]:
            tags.append("blocked")
        board.update_item(blocked_id, {"tags": tags})
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        add_comment = getattr(board, "add_comment", None)
        if callable(add_comment):
            add_comment(blocked_id, f"Blocker reported: {evidence}", author="Pam")
    except Exception:  # noqa: BLE001, S110
        pass
    blocking_id = ""
    try:
        raw_states = board.list_states() or []
        states = [str(entry) for entry in raw_states if str(entry)]
        blocking = board.create_item(
            f"Blocking: {answer.strip()[:60]}",
            {"state": states[0] if states else "To Do"},
        )
        blocking_id = _blocker_item_id(blocking)
    except Exception:  # noqa: BLE001
        blocking_id = ""
    if blocking_id:
        try:
            board.add_link(blocking_id, blocked_id, _BLOCKER_LINK_REL)
        except Exception:  # noqa: BLE001, S110
            pass
    if db is not None:
        try:
            from pam_pm.core.writes import WriteService

            service = WriteService(db, dry_run=True)
            service.propose(
                {
                    "operation": "add_link",
                    "item_id": blocked_id,
                    "evidence_quote": evidence,
                    "status": "proposed",
                    "detail": f"Predecessor {blocking_id} Successor {blocked_id}",
                }
            )
            service.propose(
                {
                    "operation": "comment",
                    "item_id": blocked_id,
                    "evidence_quote": evidence,
                    "status": "proposed",
                    "detail": "blocker flag from no-open-state report",
                }
            )
        except Exception:  # noqa: BLE001, S110
            pass
    mid = _send(
        f"Got it, you are blocked. I linked this to '{title}' ({blocked_id}) "
        "and flagged it."
    )
    return {
        "kind": "blocker_no_state",
        "replied": bool(mid),
        "person": person_id,
        "item_id": blocked_id,
        "blocking_item_id": blocking_id,
        "message_id": mid,
    }


def resume_interview(
    person: Any,
    answer_text: Any,
    chat: Any,
    board: Any,
    model_client: Any,
    *,
    db: Any = None,
    message_id: str = "",
    max_turns: int = MAX_TURNS,
) -> dict[str, Any]:
    """Resume one open interview with a late answer, running one model step.

    Loads the newest open state for the person, appends the answer, and
    runs one model step over the full stored history. Pushback, vague
    detection, result coercion, and the board write reuse the same
    internals as the synchronous run. A follow-up question updates the
    stored state; a finish writes the board with evidence and closes
    the state. A repeated message id is a duplicate and stays quiet.
    With no open state, sends one polite fallback instead of crashing.
    """
    person_id = _person_id(person)
    answer = str(answer_text or "").strip()
    marker = str(message_id or "").strip()
    if answer:
        early_pending = _load_pending_blocker(db, person_id, chat)
        if early_pending is not None:
            return _handle_blocker_confirmation(
                person_id, answer, chat, board, db, early_pending
            )
        try:
            from pam_pm.rules.templates import blocker_plan as _owner_plan_mod
        except Exception:  # noqa: BLE001
            _owner_plan_mod = None
        if _owner_plan_mod is not None:
            try:
                pending_plan = _owner_plan_mod.load_owner_plan(
                    db, person_id, chat
                )
            except Exception:  # noqa: BLE001
                pending_plan = None
            if pending_plan is not None:
                try:
                    plan_out = _owner_plan_mod.resume_owner_plan(
                        answer,
                        chat,
                        board,
                        db,
                        pending_plan,
                        sender=person_id,
                        message_id=marker,
                    )
                except Exception:  # noqa: BLE001
                    plan_out = None
                if plan_out is not None:
                    return plan_out

    def _send(text: str) -> str:
        try:
            return str(chat.send_dm(person_id, normalize_followup_spacing(text)) or "")
        except Exception:  # noqa: BLE001
            return ""

    if not answer:
        return {"kind": "ignored", "replied": False, "person": person_id}
    state = _load_open_state(db, person_id, chat)
    if state is None:
        if marker and db is not None:
            try:
                from pam_pm.core import recovery as recovery_mod

                latest = None
                for candidate in _candidate_identities(chat, person_id):
                    latest = recovery_mod.load_latest_for_person(db, candidate)
                    if latest is not None:
                        break
            except Exception:  # noqa: BLE001
                latest = None
            seen = str((latest or {}).get("last_seen", "") or "")
            if seen and marker <= seen:
                return {
                    "kind": "duplicate",
                    "replied": False,
                    "person": person_id,
                    "item_id": str((latest or {}).get("item_id", "") or ""),
                }
        pending = _load_pending_blocker(db, person_id, chat)
        if pending is not None:
            return _handle_blocker_confirmation(
                person_id, answer, chat, board, db, pending
            )
        if _looks_like_blocker(answer):
            candidates = _candidate_identities(chat, person_id)
            return _handle_blocker_clarify(
                person_id, answer, chat, board, db, candidates
            )
        mid = _send(NO_OPEN_STATE_REPLY)
        return {
            "kind": "no_open_state",
            "replied": bool(mid),
            "person": person_id,
            "message_id": mid,
        }
    item_id = str(state.get("item_id", "") or "item")
    last_seen = str(state.get("last_seen", "") or "")
    if marker and last_seen and marker <= last_seen:
        return {
            "kind": "duplicate",
            "replied": False,
            "person": person_id,
            "item_id": item_id,
        }
    stored = state.get("turns", [])
    conversation = [dict(turn) for turn in stored if isinstance(turn, dict)]
    conversation.append({"role": "user", "content": answer})
    asked = sum(1 for turn in conversation if turn.get("role") == "assistant")
    project_context = f"Project standup. Items:\n- {item_id}: {item_id} []"
    volatile = {
        "item_id": item_id,
        "person": person_id,
        "turn": asked,
        "phase": "resume",
    }
    filled = filled_slots(conversation)
    gaps = [slot for slot in CHECKIN_SLOTS if slot not in filled]
    if not gaps:
        candidate = _build_slot_result(conversation, filled)
        outcome = _safe_board_update(board, item_id, candidate)
        if not write_persisted(outcome):
            reason = write_failure_reason(outcome)
            _save_interview_state(db, person_id, item_id, conversation, marker)
            _capture_resume_memory(db, person_id, conversation)
            mid = _send(f"I could not write this to the board: {reason}")
            return {
                "kind": "write_failed",
                "replied": bool(mid),
                "person": person_id,
                "item_id": item_id,
                "message_id": mid,
                "result": candidate,
                "reason": reason,
                "deterministic": True,
            }
        _save_interview_state(
            db, person_id, item_id, conversation, marker, status=FINISHED_STATUS
        )
        _capture_resume_memory(db, person_id, conversation)
        thanks = _model_thanks(
            model_client, project_context, conversation, candidate, item_id
        )
        mid = _send(thanks)
        return {
            "kind": "finished",
            "replied": bool(mid),
            "person": person_id,
            "item_id": item_id,
            "message_id": mid,
            "result": candidate,
            "deterministic": True,
        }
    if asked >= max_turns:
        capped = _build_slot_result(conversation, filled)
        note = "Not reported: " + ", ".join(SLOT_LABELS[slot] for slot in gaps) + "."
        outcome = _safe_board_update(board, item_id, capped)
        _note_gaps_on_board(board, item_id, note)
        if not write_persisted(outcome):
            reason = write_failure_reason(outcome)
            _save_interview_state(db, person_id, item_id, conversation, marker)
            _capture_resume_memory(db, person_id, conversation)
            mid = _send(f"I could not write this to the board: {reason}")
            return {
                "kind": "write_failed",
                "replied": bool(mid),
                "person": person_id,
                "item_id": item_id,
                "message_id": mid,
                "result": capped,
                "reason": reason,
                "turn_cap": True,
            }
        _save_interview_state(
            db, person_id, item_id, conversation, marker, status=FINISHED_STATUS
        )
        _capture_resume_memory(db, person_id, conversation)
        mid = _send(
            join_ack_question(
                f"Thanks. I recorded this for {item_id}: {capped.status}", note
            )
        )
        return {
            "kind": "finished",
            "replied": bool(mid),
            "person": person_id,
            "item_id": item_id,
            "message_id": mid,
            "result": capped,
            "turn_cap": True,
        }
    try:
        raw = _call_model_once(model_client, project_context, conversation, volatile)
    except Exception:  # noqa: BLE001
        _save_interview_state(db, person_id, item_id, conversation, marker)
        return {
            "kind": "model_error",
            "replied": False,
            "person": person_id,
            "item_id": item_id,
        }
    text, tool_calls = _parse_model_result(raw)
    finish_args = _find_finish(tool_calls)
    if finish_args is not None:
        try:
            candidate = _coerce_result(finish_args, conversation)
        except Exception:  # noqa: BLE001
            candidate = None
        if candidate is not None:
            last_user = _last_user_text(conversation)
            needs_pushback = (
                last_user is not None
                and is_vague(last_user)
                and (candidate.remaining is None or candidate.eta is None)
            )
            if not needs_pushback:
                outcome = _safe_board_update(board, item_id, candidate)
                if not write_persisted(outcome):
                    reason = write_failure_reason(outcome)
                    _save_interview_state(
                        db, person_id, item_id, conversation, marker
                    )
                    _capture_resume_memory(db, person_id, conversation)
                    mid = _send(
                        f"I could not write this to the board: {reason}"
                    )
                    return {
                        "kind": "write_failed",
                        "replied": bool(mid),
                        "person": person_id,
                        "item_id": item_id,
                        "message_id": mid,
                        "result": candidate,
                        "reason": reason,
                    }
                _save_interview_state(
                    db,
                    person_id,
                    item_id,
                    conversation,
                    marker,
                    status=FINISHED_STATUS,
                )
                _capture_resume_memory(db, person_id, conversation)
                status = str(candidate.status or "").strip().replace("\n", " ")
                mid = _send(f"Thanks. I recorded this for {item_id}: {status}")
                return {
                    "kind": "finished",
                    "replied": bool(mid),
                    "person": person_id,
                    "item_id": item_id,
                    "message_id": mid,
                    "result": candidate,
                }
            text = ""
    item = {"id": item_id, "title": item_id, "state": ""}
    question = _one_question(text or _default_question(item))
    last_user = _last_user_text(conversation)
    if (
        last_user is not None
        and is_vague(last_user)
        and not (_has_remaining(question) and _has_eta(question))
    ):
        quote = _clean_quote(last_user)
        question = _one_question(
            f'You said "{quote}". What work remains and what is your ETA?'
        )
    mid = _send(question)
    if not mid:
        _save_interview_state(db, person_id, item_id, conversation, marker)
        return {
            "kind": "send_failed",
            "replied": False,
            "person": person_id,
            "item_id": item_id,
        }
    conversation.append({"role": "assistant", "content": question})
    _save_interview_state(db, person_id, item_id, conversation, marker)
    return {
        "kind": "reply",
        "replied": True,
        "person": person_id,
        "item_id": item_id,
        "message_id": mid,
        "question": question,
        "turns": len(conversation),
    }


class Interviewer:
    """Short standup interview with pushback and structured result."""

    def __init__(self, max_turns: int = MAX_TURNS) -> None:
        self.max_turns = max_turns
        self.last_turns = 0
        self.last_model_calls = 0

    def run(
        self,
        person: Any,
        items: Any,
        chat: Any,
        board: Any,
        model_client: Any,
        *,
        db: Any = None,
    ) -> InterviewResult | dict[str, InterviewResult]:
        """Interview one person about items, returning structured results.

        When a database is given, role, skillsets, and capacity found
        in the person's answers are saved to agent memory with the
        interview source noted.
        """
        person_id = _person_id(person)
        norm_items = _normalize_items(items)
        if not norm_items:
            return {}
        project_context = _project_context(norm_items)
        conversation: list[dict[str, Any]] = []
        reply_buffer: list[Any] = []
        question_ts: float | None = None
        turns = 0
        model_calls = 0

        def call_model(conv: list[dict[str, Any]], volatile: dict[str, Any]) -> Any:
            nonlocal model_calls
            payload = build_messages("interviewer", project_context, conv, volatile)
            model_calls += 1
            try:
                return model_client.chat(payload["messages"], tools=payload["tools"])
            except TypeError:
                return model_client.chat(payload["messages"], payload["tools"])

        results: dict[str, InterviewResult] = {}
        for item in norm_items:
            item_id = item["id"]
            awaiting = False
            while turns < self.max_turns and item_id not in results:
                if _has_user_turn(conversation) and not missing_slots(conversation):
                    done_result = _build_slot_result(
                        conversation, filled_slots(conversation)
                    )
                    results[item_id] = done_result
                    _safe_board_update(board, item_id, done_result)
                    _save_interview_state(
                        db, person_id, item_id, conversation, status="finished"
                    )
                    break
                volatile = {
                    "item_id": item_id,
                    "person": person_id,
                    "turn": turns,
                }
                try:
                    raw = call_model(conversation, volatile)
                except Exception:  # noqa: BLE001
                    break
                text, tool_calls = _parse_model_result(raw)
                finish_args = _find_finish(tool_calls)
                if finish_args is not None:
                    if not _has_user_turn(conversation):
                        logger.info(
                            "ignoring finish with no user turns for %s", item_id
                        )
                    else:
                        try:
                            candidate = _coerce_result(finish_args, conversation)
                        except Exception:  # noqa: BLE001
                            candidate = None
                        if candidate is not None:
                            last_user = _last_user_text(conversation)
                            vague_pushback = (
                                last_user is not None
                                and is_vague(last_user)
                                and (
                                    candidate.remaining is None
                                    or candidate.eta is None
                                )
                            )
                            missing_evidence = _is_bad_evidence(
                                candidate.evidence_quote
                            )
                            original_bad = _finish_args_evidence_is_bad(
                                finish_args
                            )
                            needs_pushback = (
                                vague_pushback
                                or missing_evidence
                                or original_bad
                            )
                            if not needs_pushback:
                                results[item_id] = candidate
                                _safe_board_update(board, item_id, candidate)
                                break
                            text = ""
                if turns >= self.max_turns:
                    break
                question = _one_question(text or _default_question(item))
                last_user = _last_user_text(conversation)
                if (
                    last_user is not None
                    and is_vague(last_user)
                    and not (_has_remaining(question) and _has_eta(question))
                ):
                    quote = _clean_quote(last_user)
                    question = _one_question(
                        f'You said "{quote}". What work remains and what is your ETA?'
                    )
                try:
                    sent_ts: Any = chat.send_dm(person_id, question)
                except Exception:  # noqa: BLE001
                    break
                if isinstance(sent_ts, dict):
                    sent_ts = sent_ts.get("ts", sent_ts.get("message_id"))
                elif not isinstance(sent_ts, (str, int, float)):
                    sent_ts = getattr(
                        sent_ts, "ts", getattr(sent_ts, "message_id", None)
                    )
                question_ts = _coerce_ts(sent_ts)
                if question_ts is None:
                    question_ts = time.time()
                turns += 1
                conversation.append({"role": "assistant", "content": question})
                _save_interview_state(db, person_id, item_id, conversation)
                answer = _next_answer(chat, person_id, reply_buffer, question_ts)
                if answer is None or not str(answer).strip():
                    awaiting = True
                    break
                conversation.append({"role": "user", "content": str(answer)})
                _save_interview_state(db, person_id, item_id, conversation)
            if item_id in results:
                _save_interview_state(
                    db, person_id, item_id, conversation, status="finished"
                )
                continue
            if awaiting and db is not None:
                continue
            if not _has_user_turn(conversation):
                continue
            done = False
            try:
                reminder_conv = conversation + [
                    {
                        "role": "user",
                        "content": (
                            "Reminder: call finish_interview now with status, "
                            "remaining, eta, evidence_quote, blocker, "
                            "blocker_owner, confidence."
                        ),
                    }
                ]
                volatile_reminder = {
                    "item_id": item_id,
                    "person": person_id,
                    "phase": "reminder",
                    "turn": turns,
                }
                raw_retry = call_model(reminder_conv, volatile_reminder)
                _, retry_calls = _parse_model_result(raw_retry)
                retry_args = _find_finish(retry_calls)
                if retry_args is not None:
                    try:
                        candidate = _coerce_result(retry_args, conversation)
                        if _is_bad_evidence(
                            candidate.evidence_quote
                        ) or _finish_args_evidence_is_bad(retry_args):
                            done = False
                        else:
                            results[item_id] = candidate
                            _safe_board_update(board, item_id, candidate)
                            done = True
                    except Exception:  # noqa: BLE001
                        done = False
            except Exception:  # noqa: BLE001
                done = False
            if done:
                _save_interview_state(
                    db, person_id, item_id, conversation, status="finished"
                )
                continue
            try:
                fallback_conv = conversation + [
                    {
                        "role": "user",
                        "content": (
                            "Extract the interview result as JSON with keys "
                            "status, remaining, eta, evidence_quote, blocker, "
                            "blocker_owner, confidence. "
                            "Use only what the person said."
                        ),
                    }
                ]
                volatile_extract = {
                    "item_id": item_id,
                    "person": person_id,
                    "phase": "extract",
                    "turn": turns,
                }
                raw_fallback = call_model(fallback_conv, volatile_extract)
                text_fallback, fallback_calls = _parse_model_result(raw_fallback)
                fallback_args = _find_finish(fallback_calls)
                if fallback_args is not None:
                    try:
                        candidate = _coerce_result(fallback_args, conversation)
                        if _is_bad_evidence(
                            candidate.evidence_quote
                        ) or _finish_args_evidence_is_bad(fallback_args):
                            pass
                        else:
                            results[item_id] = candidate
                            _safe_board_update(board, item_id, candidate)
                            _save_interview_state(
                                db,
                                person_id,
                                item_id,
                                conversation,
                                status="finished",
                            )
                            continue
                    except Exception:  # noqa: BLE001, S110
                        pass
                parsed = _parse_text_json(text_fallback, conversation)
                if parsed is not None:
                    results[item_id] = parsed
                else:
                    results[item_id] = _build_fallback_result(conversation)
                _safe_board_update(board, item_id, results[item_id])
            except Exception:  # noqa: BLE001
                try:
                    fallback_result = _build_fallback_result(conversation)
                    results[item_id] = fallback_result
                    _safe_board_update(board, item_id, fallback_result)
                except Exception:  # noqa: BLE001, S112
                    continue
            if item_id in results:
                _save_interview_state(
                    db, person_id, item_id, conversation, status="finished"
                )
        self.last_turns = turns
        self.last_model_calls = model_calls
        if db is not None:
            try:
                from pam_pm.core import memory as memory_mod

                said = [
                    str(turn.get("content", ""))
                    for turn in conversation
                    if turn.get("role") == "user"
                    and str(turn.get("content", "")).strip()
                    and not str(turn.get("content", "")).startswith(
                        ("Reminder:", "Extract ")
                    )
                ]
                memory_mod.capture_interview(db, person_id, said, source="interview")
            except Exception:  # noqa: BLE001, S110
                pass
        if len(norm_items) == 1:
            single_id = norm_items[0]["id"]
            if single_id in results:
                return results[single_id]
            if db is not None:
                return {}
            if not _has_user_turn(conversation):
                return {}
            fallback_single = _build_fallback_result(conversation)
            _safe_board_update(board, single_id, fallback_single)
            return fallback_single
        return results
