"""Interviewer agent with pushback on vague answers."""

from __future__ import annotations

import json
import re
from typing import Any

from pam_pm.agents.prompts.builder import build_messages
from pam_pm.core.schemas import InterviewResult

MAX_TURNS = 6

VAGUE_PHRASES = ("almost done", "soon", "working on it")

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
            item_id = str(
                getattr(entry, "id", getattr(entry, "item_id", "")) or ""
            )
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


def _one_question(text: str) -> str:
    cleaned = (text or "").strip().replace("\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "What is the current status?"
    if "?" not in cleaned:
        base = cleaned.rstrip(".! ").strip()
        if not base:
            return "What remains and what is your ETA?"
        return base + " What remains and what is your ETA?"
    first = cleaned.split("?", 1)[0].strip()
    if not first:
        return "What remains and what is your ETA?"
    return first + "?"


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


def _next_answer(
    chat: Any, person_id: str, buffer: list[str]
) -> str | None:
    if buffer:
        return buffer.pop(0)
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
    texts: list[str] = []
    for reply in replies:
        if isinstance(reply, str):
            texts.append(reply)
        elif isinstance(reply, dict):
            texts.append(str(reply.get("text", "")))
        else:
            texts.append(str(getattr(reply, "text", reply) or ""))
    texts = [text for text in texts if text is not None]
    if not texts:
        return None
    if len(texts) > 1:
        buffer.extend(texts[1:])
    return texts[0]


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
    if not result.evidence_quote.strip():
        last = _last_user_text(conversation)
        if last:
            update["evidence_quote"] = _clean_quote(last, 500)
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
    if not result.evidence_quote.strip():
        last = _last_user_text(conversation)
        if last:
            update["evidence_quote"] = _clean_quote(last, 500)
    if update:
        result = result.model_copy(update=update)
    return result


def _build_fallback_result(
    conversation: list[dict[str, Any]]
) -> InterviewResult:
    last = _last_user_text(conversation) or ""
    quote = _clean_quote(last, 500)
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


def _safe_board_update(board: Any, item_id: str, result: InterviewResult) -> None:
    if board is None:
        return
    try:
        board.update_item(
            item_id,
            {
                "state": result.status[:80] if result.status else "",
                "remaining": result.remaining,
                "eta": result.eta,
                "evidence_quote": result.evidence_quote,
            },
        )
    except Exception:  # noqa: BLE001
        return


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
    ) -> InterviewResult | dict[str, InterviewResult]:
        """Interview one person about items, returning structured results."""
        person_id = _person_id(person)
        norm_items = _normalize_items(items)
        if not norm_items:
            return {}
        project_context = _project_context(norm_items)
        conversation: list[dict[str, Any]] = []
        reply_buffer: list[str] = []
        turns = 0
        model_calls = 0

        def call_model(conv: list[dict[str, Any]], volatile: dict[str, Any]) -> Any:
            nonlocal model_calls
            payload = build_messages("interviewer", project_context, conv, volatile)
            model_calls += 1
            try:
                return model_client.chat(
                    payload["messages"], tools=payload["tools"]
                )
            except TypeError:
                return model_client.chat(payload["messages"], payload["tools"])

        results: dict[str, InterviewResult] = {}
        for item in norm_items:
            item_id = item["id"]
            while turns < self.max_turns and item_id not in results:
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
                    try:
                        candidate = _coerce_result(finish_args, conversation)
                    except Exception:  # noqa: BLE001
                        candidate = None
                    if candidate is not None:
                        last_user = _last_user_text(conversation)
                        needs_pushback = (
                            last_user is not None
                            and is_vague(last_user)
                            and (
                                candidate.remaining is None
                                or candidate.eta is None
                            )
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
                if last_user is not None and is_vague(last_user) and not (
                    _has_remaining(question) and _has_eta(question)
                ):
                    quote = _clean_quote(last_user)
                    question = _one_question(
                            f'You said "{quote}". '
                            "What work remains and what is your ETA?"
                        )
                try:
                    chat.send_dm(person_id, question)
                except Exception:  # noqa: BLE001
                    break
                turns += 1
                conversation.append({"role": "assistant", "content": question})
                answer = _next_answer(chat, person_id, reply_buffer)
                if answer is None or not str(answer).strip():
                    break
                conversation.append({"role": "user", "content": str(answer)})
            if item_id in results:
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
                        results[item_id] = candidate
                        _safe_board_update(board, item_id, candidate)
                        done = True
                    except Exception:  # noqa: BLE001
                        done = False
            except Exception:  # noqa: BLE001
                done = False
            if done:
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
                        results[item_id] = candidate
                        _safe_board_update(board, item_id, candidate)
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
        self.last_turns = turns
        self.last_model_calls = model_calls
        if len(norm_items) == 1:
            single_id = norm_items[0]["id"]
            if single_id in results:
                return results[single_id]
            fallback_single = _build_fallback_result(conversation)
            _safe_board_update(board, single_id, fallback_single)
            return fallback_single
        return results
