"""Blocker technical action plan: owner interview to child tasks plus ETA.

When Pam contacts the blocking owner she acts like a technical PM for at
most 6 owner turns:

1. Ask one question about what is causing the problem.
2. Propose 2 to 4 concrete steps built only from the owner's answer plus
   the blocking item (fix, regression test, deploy). The owner may
   change, add, or drop steps in plain language.
3. Ask for a rough estimate per step.
4. Compute an ETA from the estimates plus the owner's working hours
   (scheduler work_start to work_end, weekdays only) and confirm it.
5. On confirmation, create the steps as child tasks under the blocking
   item with estimates and the owner assigned, and update remaining
   work. Owner confirmation is the approval. Evidence, Undo, and the
   write layer apply as for every board write. Works on the Pam board
   and on ADO through Hierarchy child links.
6. Tell the waiting person the ETA plus a one-line plan summary.

Model calls go through build_messages with role blocker_plan so the
system prompt and tool list stay byte stable and only the volatile
block (time, person, turn) changes per call. Steps never invent
technical facts: they quote owner words plus the item title, and
uncertainty is phrased as a question.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pam_pm.agents.prompts.builder import build_messages
from pam_pm.core.writes import WriteService

MAX_TURNS = 6

CHILD_FORWARD = "System.LinkTypes.Hierarchy-Forward"
CHILD_REVERSE = "System.LinkTypes.Hierarchy-Reverse"

_HOURS_EXPLICIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h)\b", re.IGNORECASE
)
_STEP_EST_RE = re.compile(
    r"step\s*(\d+)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(hours?|hrs?|h)?",
    re.IGNORECASE,
)
_DROP_RE = re.compile(r"(drop|remove|delete|cancel)\s+(step\s*)?(\d+)", re.IGNORECASE)
_CHANGE_RE = re.compile(
    r"(change|replace|edit|update)\s+(step\s*)?(\d+)\s*"
    r"(to|with|:|->|as)?\s*(.+)",
    re.IGNORECASE,
)
_STEP_SHOULD_RE = re.compile(
    r"step\s*(\d+)\s*(should be|is|:|=|->)\s*(.+)", re.IGNORECASE
)
_ADD_RE = re.compile(r"^(add|append|also add|plus)\b[\s:]*(\d+\s*[:.\)]\s*)?(.+)", re.IGNORECASE)
_AND_SPLIT_RE = re.compile(
    r"\s+and\s+(?=(drop|remove|delete|cancel|add|append|change|replace|edit|update)\b)",
    re.IGNORECASE,
)

_AFFIRM_PHRASES = (
    "yes",
    "yeah",
    "yep",
    "confirm",
    "confirmed",
    "ok",
    "okay",
    "approved",
    "approve",
    "looks good",
    "sounds good",
    "lgtm",
    "go ahead",
    "do it",
    "correct",
    "right",
)
_HEDGE_WORDS = ("but", "however", "instead", "change", "edit", "not yet", "wait")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


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


def _item_description(item: Any) -> str:
    fields: dict[str, Any] = {}
    if isinstance(item, dict):
        raw = item.get("fields", {})
        if isinstance(raw, dict):
            fields = raw
        for key in ("description", "System.Description"):
            value = fields.get(key) or item.get(key)
            if value:
                return str(value)
        return ""
    raw = getattr(item, "fields", None)
    if isinstance(raw, dict):
        fields = raw
    for key in ("description", "System.Description"):
        value = fields.get(key)
        if value:
            return str(value)
    return ""


def _open_state(board: Any) -> str:
    try:
        states = [str(s) for s in board.list_states() if str(s)]
    except Exception:  # noqa: BLE001
        states = []
    if states:
        return states[0]
    return "To Do"


def _one_question(text: str, fallback: str) -> str:
    cleaned = (text or "").strip().replace("\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return fallback
    if "?" not in cleaned:
        base = cleaned.rstrip(".! ").strip()
        if not base:
            return fallback
        return base + " " + fallback if len(base) < 20 else fallback
    first = cleaned.split("?", 1)[0].strip()
    if not first:
        return fallback
    return first + "?"


def _model_text(raw: Any) -> str:
    if hasattr(raw, "text"):
        return str(getattr(raw, "text", "") or "")
    if isinstance(raw, dict):
        return str(raw.get("text", "") or "")
    if isinstance(raw, str):
        return raw
    return str(raw or "")


def _next_answer(chat: Any, user_id: str, buffer: list[str]) -> str | None:
    if buffer:
        return buffer.pop(0)
    try:
        replies = chat.read_replies(user_id)
    except TypeError:
        try:
            replies = chat.read_replies(user_id, None)
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None
    if not replies:
        return None
    texts = [_text_of(entry) for entry in replies]
    texts = [t for t in texts if t is not None]
    if not texts:
        return None
    if len(texts) > 1:
        buffer.extend(texts[1:])
    return texts[0]


def plan_project_context(blocking_item: Any) -> str:
    """Return the stable project context for one blocking item.

    Stable per item for prefix caching: no owner names, no waiter
    names, and no timestamps. Volatile detail travels in the last
    user message only.
    """
    title = _item_title(blocking_item) or "blocking item"
    item_id = _item_id(blocking_item) or "item"
    return f"Blocker action plan for item {item_id}: {title}."


def build_plan_messages(
    blocking_item: Any,
    conversation: list[dict[str, Any]],
    volatile: dict[str, Any],
) -> dict[str, Any]:
    """Build one blocker plan model request with a stable prefix."""
    return build_messages(
        "blocker_plan", plan_project_context(blocking_item), conversation, volatile
    )


def _cause_phrase(cause: str, limit: int = 80) -> str:
    cleaned = re.sub(r"\s+", " ", (cause or "").strip()).strip("\"' .")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip()
    return cleaned or "the reported problem"


def propose_steps(
    cause: str, item_title: str, item_description: str = ""
) -> list[str]:
    """Propose 2 to 4 grounded steps from owner words plus the item.

    Steps quote the owner's cause phrase and the item title. Nothing
    else is invented: no file names, no services, no root causes the
    owner did not name.
    """
    phrase = _cause_phrase(cause)
    title = (item_title or "blocking item").strip() or "blocking item"
    _ = item_description
    return [
        f"Fix {phrase} for '{title}'",
        f"Add regression test covering {phrase}",
        f"Deploy and verify '{title}' with the waiter",
    ]


def plan_summary(steps: list[str]) -> str:
    """Return a one-line summary of the plan steps."""
    short: list[str] = []
    for step in steps:
        text = re.sub(r"\s+", " ", (step or "").strip())
        if len(text) > 60:
            text = text[:60].rstrip()
        short.append(text)
    return "; ".join(s for s in short if s) or "No steps yet"


def apply_step_edits(
    steps: list[str], reply: str
) -> tuple[list[str], bool]:
    """Apply plain language step edits, returning the new list."""
    current = list(steps)
    text = (reply or "").strip()
    if not text:
        return current, False
    lowered = text.lower()
    if re.fullmatch(r"(keep|keep all|no changes?|looks good|ok|okay|yes|lgtm)[.! ]*",
                    lowered):
        return current, False
    clauses: list[str] = []
    for chunk in re.split(r"[;\n]+", text):
        clauses.extend(_AND_SPLIT_RE.split(chunk))
    changed = False
    for clause in clauses:
        part = clause.strip().strip(".")
        if not part:
            continue
        low = part.lower()
        if re.fullmatch(r"(keep|keep all|no changes?|looks good|ok|okay|yes|lgtm)[.! ]*",
                        low):
            continue
        match = _DROP_RE.search(part)
        if match:
            idx = int(match.group(3)) - 1
            if 0 <= idx < len(current):
                current.pop(idx)
                changed = True
            continue
        match = _CHANGE_RE.search(part)
        if match:
            idx = int(match.group(3)) - 1
            new_text = (match.group(5) or "").strip().strip("\"'")
            if 0 <= idx < len(current) and new_text:
                current[idx] = new_text
                changed = True
            continue
        match = _STEP_SHOULD_RE.search(part)
        if match:
            idx = int(match.group(1)) - 1
            new_text = (match.group(3) or "").strip().strip("\"'")
            if 0 <= idx < len(current) and new_text:
                current[idx] = new_text
                changed = True
            continue
        match = _ADD_RE.search(part)
        if match:
            new_text = (match.group(3) or "").strip().strip("\"'")
            if new_text and len(new_text) > 2:
                current.append(new_text)
                changed = True
            continue
    if not current:
        return list(steps), False
    return current, changed


def parse_estimates(reply: str, n_steps: int) -> list[float]:
    """Parse rough hour estimates for each step, defaulting to 1.0."""
    count = max(1, int(n_steps))
    text = (reply or "").strip()
    if not text:
        return [1.0] * count
    by_step: dict[int, float] = {}
    for match in _STEP_EST_RE.finditer(text):
        try:
            idx = int(match.group(1)) - 1
            val = float(match.group(2))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < count and idx not in by_step:
            by_step[idx] = val
    if len(by_step) == count:
        return [by_step[i] for i in range(count)]
    if by_step and len(by_step) < count and len(_HOURS_EXPLICIT_RE.findall(text)) <= 1:
        out = [by_step.get(i, 1.0) for i in range(count)]
        return [float(v) for v in out]
    explicit = [float(m.group(1)) for m in _HOURS_EXPLICIT_RE.finditer(text)]
    lowered = text.lower()
    if explicit and len(explicit) == count:
        return explicit
    if explicit and len(explicit) == 1 and count > 1:
        only = explicit[0]
        if "each" in lowered or "per step" in lowered or "apiece" in lowered:
            return [only] * count
        if "total" in lowered:
            each = round(only / count, 2)
            return [each] * count
    bare_ok = bool(re.fullmatch(r"[\d\s,.;hrours/]+", lowered)) and len(text) <= 60
    if bare_ok:
        nums = [float(m.group(0)) for m in re.finditer(r"\d+(?:\.\d+)?", text)]
        if len(nums) == count:
            return nums
        if len(nums) == 1 and count > 1 and "each" in lowered:
            return [nums[0]] * count
    if explicit:
        out = list(explicit[:count])
        while len(out) < count:
            out.append(1.0)
        return out
    return [1.0] * count


def compute_eta(
    total_hours: float,
    now: datetime,
    scheduler: Any | None = None,
    tz: str = "UTC",
) -> tuple[str, datetime]:
    """Compute an ETA date from estimates plus scheduler working hours.

    Hours spread over weekday working days only, from work_start
    (inclusive) to work_end (exclusive). Times outside the window
    roll forward to the next working start. Unknown hours default
    to 9 to 5.
    """
    try:
        work_start = int(getattr(scheduler, "work_start", 9))
    except (TypeError, ValueError):
        work_start = 9
    try:
        work_end = int(getattr(scheduler, "work_end", 17))
    except (TypeError, ValueError):
        work_end = 17
    work_start = min(max(work_start, 0), 23)
    work_end = min(max(work_end, 1), 24)
    if work_end <= work_start:
        work_start, work_end = 9, 17
    zone = _zone(tz)
    cur = now
    if cur.tzinfo is None:
        cur = cur.replace(tzinfo=zone)
    else:
        cur = cur.astimezone(zone)
    remaining = max(0.0, float(total_hours))

    def at_start(day: datetime) -> datetime:
        return day.replace(hour=work_start, minute=0, second=0, microsecond=0)

    def next_start(day: datetime) -> datetime:
        nxt = (day + timedelta(days=1)).replace(
            hour=work_start, minute=0, second=0, microsecond=0
        )
        while nxt.weekday() >= 5:
            nxt = (nxt + timedelta(days=1)).replace(
                hour=work_start, minute=0, second=0, microsecond=0
            )
        return nxt

    while cur.weekday() >= 5:
        cur = next_start(cur)
    day_start = at_start(cur)
    day_end = cur.replace(hour=work_end, minute=0, second=0, microsecond=0)
    if cur < day_start:
        cur = day_start
    elif cur >= day_end:
        cur = next_start(cur)
    if remaining <= 0:
        return cur.date().isoformat(), cur
    while True:
        day_end = cur.replace(hour=work_end, minute=0, second=0, microsecond=0)
        avail = (day_end - cur).total_seconds() / 3600.0
        if remaining <= avail + 1e-9:
            finish = cur + timedelta(hours=remaining)
            return finish.date().isoformat(), finish
        remaining -= max(0.0, avail)
        cur = next_start(cur)


def is_confirmed(reply: str) -> bool:
    """Return True when the owner reply confirms the plan."""
    lowered = (reply or "").lower()
    if not lowered.strip():
        return False
    if not any(phrase in lowered for phrase in _AFFIRM_PHRASES):
        return False
    return not any(word in lowered for word in _HEDGE_WORDS)


def _is_uncertain(cause: str) -> bool:
    words = (cause or "").strip().split()
    if len(words) < 3:
        return True
    lowered = (cause or "").lower()
    return any(
        token in lowered
        for token in ("not sure", "maybe", "unknown", "dunno", "no idea", "?")
    )


OWNER_PLAN_KIND = "blocker"

STEP_AWAITING_CAUSE = "awaiting_cause"
STEP_AWAITING_EDIT = "awaiting_edit"
STEP_AWAITING_ESTIMATES = "awaiting_estimates"
STEP_AWAITING_CONFIRM = "awaiting_confirm"


def owner_plan_thread_key(owner: str, blocking_id: str) -> str:
    """Return the durable thread key for one owner's plan."""
    from pam_pm.core import recovery as recovery_mod

    what = f"plan:{blocking_id}" if str(blocking_id or "") else "plan"
    return recovery_mod.thread_key(str(owner), what, kind=OWNER_PLAN_KIND)


def save_owner_plan(
    db: Any,
    owner: str,
    blocking_id: str,
    state: dict[str, Any],
    last_seen: str = "",
) -> str:
    """Persist one pending owner plan, returning its thread key."""
    from pam_pm.core import recovery as recovery_mod

    key = owner_plan_thread_key(owner, blocking_id)
    body = dict(state)
    body["pending_owner_plan"] = True
    body["owner"] = str(owner)
    body["blocking_id"] = str(blocking_id or "")
    recovery_mod.save_conversation(
        db,
        key,
        str(owner),
        str(blocking_id or ""),
        [{"role": "system", "content": json.dumps(body)}],
        str(last_seen or ""),
        "open",
    )
    return key


def _owner_plan_candidates(chat: Any, sender: str) -> list[str]:
    wants = [str(sender or "")]
    try:
        from pam_pm.agents.interviewer import resolve_display_name as _res

        got = _res(chat, str(sender or ""))
        if got and got not in wants:
            wants.append(got)
    except Exception:  # noqa: BLE001, S110
        pass
    return wants


def load_owner_plan(
    db: Any, sender: str, chat: Any = None
) -> dict[str, Any] | None:
    """Return the newest pending owner plan for a sender, or None."""
    if db is None or not str(sender or "").strip():
        return None
    try:
        from pam_pm.core import recovery as recovery_mod

        wants = _owner_plan_candidates(chat, sender)
        lowered = {str(entry).lower() for entry in wants}
        found: dict[str, Any] | None = None
        for conv in recovery_mod.list_open_conversations(db):
            if ":plan:" not in str(conv.get("thread_key", "") or ""):
                continue
            person = str(conv.get("person", "") or "")
            for turn in conv.get("turns", []) or []:
                if not isinstance(turn, dict):
                    continue
                content = str(turn.get("content", "") or "")
                if not content.strip().startswith("{"):
                    continue
                try:
                    data = json.loads(content)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                if not data.get("pending_owner_plan"):
                    continue
                who = str(data.get("owner", "") or person)
                if (
                    who in wants
                    or who.lower() in lowered
                    or person in wants
                    or person.lower() in lowered
                ):
                    data["_thread_key"] = str(conv.get("thread_key", ""))
                    data["_person"] = person or who
                    data["_last_seen"] = str(conv.get("last_seen", "") or "")
                    found = data
        return found
    except Exception:  # noqa: BLE001
        return None


def clear_owner_plan(
    db: Any,
    owner: str = "",
    blocking_id: str = "",
    thread_key: str = "",
) -> None:
    """Mark pending owner plan threads as finished."""
    if db is None:
        return
    try:
        from pam_pm.core import recovery as recovery_mod

        keys: list[str] = []
        if thread_key:
            keys.append(str(thread_key))
        elif owner and blocking_id:
            keys.append(owner_plan_thread_key(owner, blocking_id))
        elif owner:
            want = str(owner)
            for conv in recovery_mod.list_open_conversations(db):
                tkey = str(conv.get("thread_key", "") or "")
                if ":plan:" not in tkey:
                    continue
                person = str(conv.get("person", "") or "")
                if person == want or person.lower() == want.lower():
                    keys.append(tkey)
        for key in keys:
            conv = recovery_mod.load_conversation(db, key)
            if conv is None:
                continue
            recovery_mod.save_conversation(
                db,
                key,
                str(conv.get("person", "") or owner),
                str(conv.get("item_id", "") or blocking_id),
                list(conv.get("turns", []) or []),
                str(conv.get("last_seen", "") or ""),
                "finished",
            )
    except Exception:  # noqa: BLE001, S110
        pass


def resume_owner_plan(
    answer: str,
    chat: Any,
    board: Any,
    db: Any = None,
    pending: dict[str, Any] | None = None,
    *,
    sender: str = "",
    message_id: str = "",
    writes: WriteService | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any] | None:
    """Continue one persisted owner plan with a late answer.

    Each call sends exactly one DM: the next owner question, or the
    waiter ETA once the owner confirms. The thread is re-persisted
    after every question and cleared on confirmation, decline, or
    the turn cap. Returns None when no plan is pending.
    """
    state = dict(pending or {})
    if not state.get("pending_owner_plan"):
        loaded = load_owner_plan(db, sender, chat) if sender else None
        if loaded is None:
            return None
        state = loaded
    owner = str(state.get("owner", "") or sender)
    waiter = str(state.get("waiter", "") or "")
    blocking_id = str(state.get("blocking_id", "") or "")
    blocked_id = str(state.get("blocked_id", "") or "")
    step = str(state.get("step", "") or "")
    marker = str(message_id or "").strip()
    seen = str(state.get("_last_seen", "") or "")
    if marker and seen and marker == seen:
        return {
            "kind": "duplicate",
            "replied": False,
            "person": owner,
            "item_id": blocking_id,
        }
    text = str(answer or "").strip()
    if not text:
        return {"kind": "ignored", "replied": False, "person": owner}
    try:
        turns = int(state.get("turns", 0) or 0)
    except (TypeError, ValueError):
        turns = 0
    try:
        cap = int(state.get("max_turns", MAX_TURNS) or MAX_TURNS)
    except (TypeError, ValueError):
        cap = MAX_TURNS
    cap = max(1, cap)
    try:
        model_calls = int(state.get("model_calls", 0) or 0)
    except (TypeError, ValueError):
        model_calls = 0
    owner_dms = list(state.get("owner_dms", []) or [])
    conversation = [
        dict(entry)
        for entry in (state.get("conversation", []) or [])
        if isinstance(entry, dict)
    ]
    tz = str(state.get("tz", "") or "UTC")
    try:
        work_start = int(state.get("work_start", 9))
    except (TypeError, ValueError):
        work_start = 9
    try:
        work_end = int(state.get("work_end", 17))
    except (TypeError, ValueError):
        work_end = 17
    title = str(state.get("title", "") or "")
    description = str(state.get("description", "") or "")
    if board is not None and blocking_id:
        try:
            current = board.get_item(blocking_id)
        except Exception:  # noqa: BLE001
            current = None
        if current is not None:
            title = _item_title(current) or title
            description = _item_description(current) or description
    if not title:
        title = "blocking item"
    service = writes
    if service is None and db is not None:
        want_dry = state.get("dry_run", True) if dry_run is None else dry_run
        try:
            service = WriteService(db, dry_run=bool(want_dry))
        except Exception:  # noqa: BLE001
            service = None

    def _send(text_dm: str) -> str | None:
        nonlocal turns
        if turns >= cap:
            return None
        mid = str(chat.send_dm(owner, text_dm))
        owner_dms.append(mid)
        turns += 1
        conversation.append({"role": "assistant", "content": text_dm})
        return mid

    def _keep(step_name: str, extra: dict[str, Any]) -> None:
        if db is None:
            return
        body = dict(state)
        body.update(extra)
        body["waiter"] = waiter
        body["blocked_id"] = blocked_id
        body["step"] = step_name
        body["turns"] = turns
        body["model_calls"] = model_calls
        body["owner_dms"] = list(owner_dms)
        body["conversation"] = [dict(entry) for entry in conversation]
        body["title"] = title
        body["description"] = str(description or "")[:500]
        try:
            save_owner_plan(db, owner, blocking_id, body, marker)
        except Exception:  # noqa: BLE001, S110
            pass

    def _finish() -> None:
        try:
            clear_owner_plan(
                db,
                owner,
                blocking_id,
                thread_key=str(state.get("_thread_key", "") or ""),
            )
        except Exception:  # noqa: BLE001, S110
            pass

    def _capped() -> dict[str, Any]:
        _finish()
        return {
            "kind": "owner_plan_max_turns",
            "status": "max_turns",
            "replied": False,
            "person": owner,
            "item_id": blocking_id,
            "blocking_item_id": blocking_id,
            "blocked_item_id": blocked_id,
            "turns": turns,
        }

    if step == STEP_AWAITING_CAUSE:
        cause = text
        conversation.append({"role": "user", "content": cause})
        steps = propose_steps(cause, title, description)
        numbered = "\n".join(
            f"{idx + 1}. {entry}" for idx, entry in enumerate(steps)
        )
        proposal = (
            f"Thanks. Based on what you said plus '{title}', here is a draft plan:\n"
            f"{numbered}\n"
            "Reply in plain language to change, add, or drop steps "
            "(for example: drop 2, add verify on staging, "
            "change step 1 to restart the worker). "
            "Reply keep or ok if it looks right."
        )
        if _is_uncertain(cause):
            proposal += (
                f" Your note was short, so is the main cause '{_cause_phrase(cause)}' "
                "or something else? Tell me and I will adjust the steps."
            )
        mid = _send(proposal)
        if mid is None:
            return _capped()
        _keep(STEP_AWAITING_EDIT, {"cause": cause, "steps": list(steps)})
        return {
            "kind": "owner_plan_proposal",
            "replied": True,
            "person": owner,
            "item_id": blocking_id,
            "blocking_item_id": blocking_id,
            "blocked_item_id": blocked_id,
            "message_id": mid,
            "question": proposal,
            "cause": cause,
            "steps": steps,
            "turns": turns,
            "model_calls": model_calls,
        }
    if step == STEP_AWAITING_EDIT:
        edit_reply = text
        conversation.append({"role": "user", "content": edit_reply})
        steps = [str(entry) for entry in (state.get("steps", []) or [])]
        if not steps:
            steps = propose_steps(
                str(state.get("cause", "") or text), title, description
            )
        steps, _ = apply_step_edits(steps, edit_reply)
        est_numbered = "\n".join(
            f"{idx + 1}. {entry}" for idx, entry in enumerate(steps)
        )
        est_q = (
            "Rough estimate per step in hours?\n"
            f"{est_numbered}\n"
            "Reply like: 2h, 1h, 3h (one number per step)."
        )
        mid = _send(est_q)
        if mid is None:
            return _capped()
        _keep(
            STEP_AWAITING_ESTIMATES,
            {"steps": list(steps), "edit_reply": edit_reply},
        )
        return {
            "kind": "owner_plan_estimates",
            "replied": True,
            "person": owner,
            "item_id": blocking_id,
            "blocking_item_id": blocking_id,
            "blocked_item_id": blocked_id,
            "message_id": mid,
            "question": est_q,
            "steps": steps,
            "turns": turns,
            "model_calls": model_calls,
        }
    if step == STEP_AWAITING_ESTIMATES:
        est_reply = text
        conversation.append({"role": "user", "content": est_reply})
        steps = [str(entry) for entry in (state.get("steps", []) or [])]
        if not steps:
            steps = propose_steps(
                str(state.get("cause", "") or text), title, description
            )
        estimates = parse_estimates(est_reply, len(steps))
        total = round(float(sum(estimates)), 2)
        moment = datetime.now(UTC)
        from types import SimpleNamespace as _hours_ns

        sched = _hours_ns(work_start=work_start, work_end=work_end)
        eta_date, _ = compute_eta(total, moment, sched, tz)
        summary = plan_summary(steps)
        confirm_dm = (
            f"Plan ({total:g}h total, ETA {eta_date}): {summary}. "
            "Does this ETA work? Reply yes to confirm and I will create "
            "the steps as child tasks."
        )
        mid = _send(confirm_dm)
        if mid is None:
            return _capped()
        _keep(
            STEP_AWAITING_CONFIRM,
            {
                "est_reply": est_reply,
                "estimates": list(estimates),
                "total_hours": total,
                "eta": eta_date,
                "summary": summary,
            },
        )
        return {
            "kind": "owner_plan_confirm",
            "replied": True,
            "person": owner,
            "item_id": blocking_id,
            "blocking_item_id": blocking_id,
            "blocked_item_id": blocked_id,
            "message_id": mid,
            "question": confirm_dm,
            "steps": steps,
            "estimates": estimates,
            "total_hours": total,
            "eta": eta_date,
            "summary": summary,
            "turns": turns,
            "model_calls": model_calls,
        }
    if step == STEP_AWAITING_CONFIRM:
        confirm_reply = text
        conversation.append({"role": "user", "content": confirm_reply})
        steps = [str(entry) for entry in (state.get("steps", []) or [])]
        raw_est = list(state.get("estimates", []) or [])
        estimates = [float(entry) for entry in raw_est]
        if len(estimates) != len(steps):
            estimates = parse_estimates(
                str(state.get("est_reply", "") or ""), len(steps)
            )
        try:
            total = float(state.get("total_hours", round(sum(estimates), 2)))
        except (TypeError, ValueError):
            total = round(float(sum(estimates)), 2)
        eta_date = str(state.get("eta", "") or "")
        summary = str(state.get("summary", "") or plan_summary(steps))
        cause = str(state.get("cause", "") or "")
        edit_reply = str(state.get("edit_reply", "") or "")
        est_reply = str(state.get("est_reply", "") or "")
        if not is_confirmed(confirm_reply):
            _finish()
            return {
                "kind": "owner_plan_declined",
                "status": "declined",
                "replied": False,
                "person": owner,
                "item_id": blocking_id,
                "blocking_item_id": blocking_id,
                "blocked_item_id": blocked_id,
                "steps": steps,
                "estimates": estimates,
                "total_hours": total,
                "eta": eta_date,
                "summary": summary,
                "cause": cause,
                "confirm_reply": confirm_reply,
                "turns": turns,
                "model_calls": model_calls,
                "owner_dms": list(owner_dms),
            }
        if board is None:
            note = (
                "I could not create the plan as child tasks: "
                "no board access right now."
            )
            mid = _send(note)
            _finish()
            return {
                "kind": "owner_plan_write_failed",
                "replied": bool(mid),
                "person": owner,
                "item_id": blocking_id,
                "blocking_item_id": blocking_id,
                "blocked_item_id": blocked_id,
                "reason": "no board configured",
            }
        evidence = "; ".join(
            part
            for part in (
                cause.strip(),
                edit_reply.strip(),
                est_reply.strip(),
                confirm_reply.strip(),
            )
            if part
        )[:500] or f"owner confirmed plan for {title}"[:500]
        write_ids: list[int] = []

        def _log(operation: str, item_id: str, detail: str) -> None:
            if service is None:
                return
            try:
                wid = service.propose(
                    {
                        "operation": operation,
                        "item_id": str(item_id),
                        "evidence_quote": evidence,
                        "status": "proposed",
                        "detail": detail[:500],
                    }
                )
                write_ids.append(int(wid))
                if not service.dry_run:
                    service.apply(int(wid))
            except Exception:  # noqa: BLE001
                return

        children: list[Any] = []
        links: list[Any] = []
        open_state = _open_state(board)
        for step_text, est in zip(steps, estimates):
            child = board.create_item(
                step_text[:200],
                {
                    "state": open_state,
                    "assigned_to": owner,
                    "owner": owner,
                    "estimate": float(est),
                    "remaining": float(est),
                },
            )
            children.append(child)
            _log("create_item", _item_id(child), f"child step for {blocking_id}")
            try:
                link = board.add_link(
                    blocking_id, _item_id(child), CHILD_FORWARD
                )
            except Exception:  # noqa: BLE001
                try:
                    link = board.add_link(
                        _item_id(child), blocking_id, CHILD_REVERSE
                    )
                except Exception:  # noqa: BLE001
                    link = None
            if link is not None:
                links.append(link)
                _log(
                    "add_link",
                    blocking_id,
                    f"Child {_item_id(child)} of {blocking_id}",
                )
        try:
            board.update_item(
                blocking_id, {"remaining": total, "estimate": total}
            )
            _log("update_remaining", blocking_id, f"remaining {total:g}h")
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            board.add_comment(
                blocking_id,
                f"Action plan ({total:g}h, ETA {eta_date}): {summary}. "
                f"Evidence: \"{cause.strip()[:200]}\"",
                author="Pam",
            )
            _log("add_comment", blocking_id, f"plan ETA {eta_date}")
        except Exception:  # noqa: BLE001, S110
            pass
        waiter_dm: str | None = None
        if waiter:
            try:
                waiter_dm = str(
                    chat.send_dm(
                        waiter, f"ETA {eta_date} for '{title}': {summary}."
                    )
                )
            except Exception:  # noqa: BLE001
                waiter_dm = None
        _finish()
        return {
            "kind": "owner_plan_confirmed",
            "status": "confirmed",
            "replied": bool(waiter_dm),
            "person": owner,
            "item_id": blocking_id,
            "blocking_item_id": blocking_id,
            "blocked_item_id": blocked_id,
            "steps": steps,
            "estimates": estimates,
            "total_hours": total,
            "eta": eta_date,
            "summary": summary,
            "cause": cause,
            "children": children,
            "links": links,
            "write_ids": write_ids,
            "evidence_quote": evidence,
            "owner_dms": list(owner_dms),
            "waiter_dm": waiter_dm,
            "turns": turns,
            "model_calls": model_calls,
        }
    _finish()
    return None


def blocker_action_plan(
    owner: str,
    waiter: str,
    chat: Any,
    board: Any,
    model: Any = None,
    *,
    writes: WriteService | None = None,
    db: Any = None,
    dry_run: bool = True,
    blocking_item_id: str | None = None,
    blocked_item_id: str | None = None,
    scheduler: Any | None = None,
    now: datetime | None = None,
    tz: str = "UTC",
    max_turns: int = MAX_TURNS,
) -> dict[str, Any]:
    """Run the 6-step owner flow and notify the waiter on confirmation."""
    if not blocking_item_id:
        raise ValueError("blocking_item_id is required")
    blocking = board.get_item(blocking_item_id)
    title = _item_title(blocking)
    description = _item_description(blocking)
    blocked_id = str(blocked_item_id or "")
    service = writes
    if service is None and db is not None:
        service = WriteService(db, dry_run=dry_run)
    cap = max(1, int(max_turns))
    conversation: list[dict[str, Any]] = []
    reply_buffer: list[str] = []
    turns = 0
    model_calls = 0
    owner_dms: list[str] = []
    _plan_db = db if db is not None else getattr(service, "_db", None)
    try:
        _plan_work_start = int(getattr(scheduler, "work_start", 9))
    except (TypeError, ValueError):
        _plan_work_start = 9
    try:
        _plan_work_end = int(getattr(scheduler, "work_end", 17))
    except (TypeError, ValueError):
        _plan_work_end = 17
    _plan_dry = service.dry_run if service is not None else bool(dry_run)

    def _persist_plan(step_name: str, extra: dict[str, Any]) -> None:
        if _plan_db is None:
            return
        try:
            body = dict(extra)
            body.update(
                {
                    "waiter": waiter,
                    "blocked_id": blocked_id,
                    "step": step_name,
                    "turns": turns,
                    "model_calls": model_calls,
                    "owner_dms": list(owner_dms),
                    "conversation": [dict(t) for t in conversation],
                    "title": title,
                    "description": str(description or "")[:500],
                    "tz": tz,
                    "work_start": _plan_work_start,
                    "work_end": _plan_work_end,
                    "dry_run": bool(_plan_dry),
                    "max_turns": cap,
                }
            )
            save_owner_plan(
                _plan_db, owner, str(blocking_item_id or ""), body
            )
        except Exception:  # noqa: BLE001, S110
            pass

    def _clear_plan() -> None:
        if _plan_db is None:
            return
        try:
            clear_owner_plan(_plan_db, owner, str(blocking_item_id or ""))
        except Exception:  # noqa: BLE001, S110
            pass

    def call_model(volatile: dict[str, Any]) -> Any:
        nonlocal model_calls
        payload = build_plan_messages(blocking, conversation, volatile)
        model_calls += 1
        try:
            return model.chat(payload["messages"], tools=payload["tools"])
        except TypeError:
            return model.chat(payload["messages"], payload["tools"])

    def send_owner(text: str) -> str | None:
        nonlocal turns
        if turns >= cap:
            _clear_plan()
            return None
        mid = str(chat.send_dm(owner, text))
        owner_dms.append(mid)
        turns += 1
        conversation.append({"role": "assistant", "content": text})
        return mid

    def hear_owner() -> str | None:
        answer = _next_answer(chat, owner, reply_buffer)
        if answer is None:
            return None
        conversation.append({"role": "user", "content": str(answer)})
        return str(answer)

    def now_iso() -> str:
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(_zone(tz)).isoformat()

    cause_fallback = f"What is causing the problem with '{title}'?"
    cause_q = cause_fallback
    if model is not None:
        try:
            raw = call_model({"now": now_iso(), "person": owner, "turn": turns})
            cause_q = _one_question(_model_text(raw), cause_fallback)
        except Exception:  # noqa: BLE001
            cause_q = cause_fallback
    if send_owner(cause_q) is None:
        return {
            "status": "max_turns",
            "turns": turns,
            "model_calls": model_calls,
            "steps": [],
            "estimates": [],
            "owner_dms": owner_dms,
        }
    _persist_plan(STEP_AWAITING_CAUSE, {})
    cause = hear_owner()
    if cause is None or not cause.strip():
        return {
            "status": "incomplete",
            "reason": "no cause answer",
            "turns": turns,
            "model_calls": model_calls,
            "steps": [],
            "estimates": [],
            "owner_dms": owner_dms,
        }
    steps = propose_steps(cause, title, description)
    numbered = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(steps))
    proposal = (
        f"Thanks. Based on what you said plus '{title}', here is a draft plan:\n"
        f"{numbered}\n"
        "Reply in plain language to change, add, or drop steps "
        "(for example: drop 2, add verify on staging, "
        "change step 1 to restart the worker). "
        "Reply keep or ok if it looks right."
    )
    if _is_uncertain(cause):
        proposal += (
            f" Your note was short, so is the main cause '{_cause_phrase(cause)}' "
            "or something else? Tell me and I will adjust the steps."
        )
    if send_owner(proposal) is None:
        return {
            "status": "max_turns",
            "turns": turns,
            "model_calls": model_calls,
            "steps": steps,
            "estimates": [],
            "cause": cause,
            "owner_dms": owner_dms,
        }
    _persist_plan(
        STEP_AWAITING_EDIT, {"cause": cause, "steps": list(steps)}
    )
    edit_reply = hear_owner()
    if edit_reply is None:
        return {
            "status": "incomplete",
            "reason": "no plan edit answer",
            "turns": turns,
            "model_calls": model_calls,
            "steps": steps,
            "estimates": [],
            "cause": cause,
            "owner_dms": owner_dms,
        }
    steps, _ = apply_step_edits(steps, edit_reply)
    est_numbered = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(steps))
    est_fallback = (
        "Rough estimate per step in hours?\n"
        f"{est_numbered}\n"
        "Reply like: 2h, 1h, 3h (one number per step)."
    )
    est_q = est_fallback
    if model is not None:
        try:
            raw = call_model(
                {
                    "now": now_iso(),
                    "person": owner,
                    "steps": len(steps),
                    "turn": turns,
                }
            )
            candidate = _model_text(raw)
            if "hour" in candidate.lower() or "estimat" in candidate.lower():
                est_q = candidate.strip()[:800] or est_fallback
            else:
                est_q = est_fallback
        except Exception:  # noqa: BLE001
            est_q = est_fallback
    if send_owner(est_q) is None:
        return {
            "status": "max_turns",
            "turns": turns,
            "model_calls": model_calls,
            "steps": steps,
            "estimates": [],
            "cause": cause,
            "owner_dms": owner_dms,
        }
    _persist_plan(
        STEP_AWAITING_ESTIMATES,
        {"cause": cause, "steps": list(steps), "edit_reply": edit_reply},
    )
    est_reply = hear_owner()
    if est_reply is None or not est_reply.strip():
        return {
            "status": "incomplete",
            "reason": "no estimate answer",
            "turns": turns,
            "model_calls": model_calls,
            "steps": steps,
            "estimates": [],
            "cause": cause,
            "owner_dms": owner_dms,
        }
    estimates = parse_estimates(est_reply, len(steps))
    total = round(float(sum(estimates)), 2)
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    eta_date, _ = compute_eta(total, moment, scheduler, tz)
    summary = plan_summary(steps)
    confirm_dm = (
        f"Plan ({total:g}h total, ETA {eta_date}): {summary}. "
        "Does this ETA work? Reply yes to confirm and I will create "
        "the steps as child tasks."
    )
    if send_owner(confirm_dm) is None:
        return {
            "status": "max_turns",
            "turns": turns,
            "model_calls": model_calls,
            "steps": steps,
            "estimates": estimates,
            "total_hours": total,
            "eta": eta_date,
            "summary": summary,
            "cause": cause,
            "owner_dms": owner_dms,
        }
    _persist_plan(
        STEP_AWAITING_CONFIRM,
        {
            "cause": cause,
            "steps": list(steps),
            "edit_reply": edit_reply,
            "est_reply": est_reply,
            "estimates": list(estimates),
            "total_hours": total,
            "eta": eta_date,
            "summary": summary,
        },
    )
    confirm_reply = hear_owner()
    if confirm_reply is None:
        return {
            "status": "incomplete",
            "reason": "no confirmation answer",
            "turns": turns,
            "model_calls": model_calls,
            "steps": steps,
            "estimates": estimates,
            "total_hours": total,
            "eta": eta_date,
            "summary": summary,
            "cause": cause,
            "owner_dms": owner_dms,
        }
    if not is_confirmed(confirm_reply):
        _clear_plan()
        return {
            "status": "declined",
            "turns": turns,
            "model_calls": model_calls,
            "steps": steps,
            "estimates": estimates,
            "total_hours": total,
            "eta": eta_date,
            "summary": summary,
            "cause": cause,
            "confirm_reply": confirm_reply,
            "owner_dms": owner_dms,
        }
    evidence = "; ".join(
        part
        for part in (cause.strip(), edit_reply.strip(), est_reply.strip(),
                     confirm_reply.strip())
        if part
    )[:500] or f"owner confirmed plan for {title}"[:500]
    write_ids: list[int] = []

    def log(operation: str, item_id: str, detail: str) -> None:
        if service is None:
            return
        try:
            wid = service.propose(
                {
                    "operation": operation,
                    "item_id": str(item_id),
                    "evidence_quote": evidence,
                    "status": "proposed",
                    "detail": detail[:500],
                }
            )
            write_ids.append(int(wid))
            if not service.dry_run:
                service.apply(int(wid))
        except Exception:  # noqa: BLE001
            return

    children: list[Any] = []
    links: list[Any] = []
    state = _open_state(board)
    for step, est in zip(steps, estimates):
        child = board.create_item(
            step[:200],
            {
                "state": state,
                "assigned_to": owner,
                "owner": owner,
                "estimate": float(est),
                "remaining": float(est),
            },
        )
        children.append(child)
        log("create_item", _item_id(child), f"child step for {blocking_item_id}")
        try:
            link = board.add_link(blocking_item_id, _item_id(child), CHILD_FORWARD)
        except Exception:  # noqa: BLE001
            try:
                link = board.add_link(
                    _item_id(child), blocking_item_id, CHILD_REVERSE
                )
            except Exception:  # noqa: BLE001
                link = None
        if link is not None:
            links.append(link)
            log(
                "add_link",
                blocking_item_id,
                f"Child {_item_id(child)} of {blocking_item_id}",
            )
    try:
        board.update_item(
            blocking_item_id, {"remaining": total, "estimate": total}
        )
        log("update_remaining", blocking_item_id, f"remaining {total:g}h")
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        board.add_comment(
            blocking_item_id,
            f"Action plan ({total:g}h, ETA {eta_date}): {summary}. "
            f"Evidence: \"{cause.strip()[:200]}\"",
            author="Pam",
        )
        log("add_comment", blocking_item_id, f"plan ETA {eta_date}")
    except Exception:  # noqa: BLE001, S110
        pass
    waiter_dm: str | None = None
    try:
        waiter_dm = str(
            chat.send_dm(
                waiter, f"ETA {eta_date} for '{title}': {summary}."
            )
        )
    except Exception:  # noqa: BLE001
        waiter_dm = None
    _clear_plan()
    return {
        "status": "confirmed",
        "turns": turns,
        "model_calls": model_calls,
        "steps": steps,
        "estimates": estimates,
        "total_hours": total,
        "eta": eta_date,
        "summary": summary,
        "cause": cause,
        "children": children,
        "links": links,
        "write_ids": write_ids,
        "evidence_quote": evidence,
        "owner_dms": owner_dms,
        "waiter_dm": waiter_dm,
    }


def run_blocker_action_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Alias for blocker_action_plan kept for older imports."""
    if len(args) >= 4:
        return blocker_action_plan(*args, **kwargs)  # type: ignore[arg-type]
    return {}
