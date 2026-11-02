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
    roll forward to the next working start.
    """
    try:
        work_start = int(getattr(scheduler, "work_start", 8))
    except (TypeError, ValueError):
        work_start = 8
    try:
        work_end = int(getattr(scheduler, "work_end", 18))
    except (TypeError, ValueError):
        work_end = 18
    work_start = min(max(work_start, 0), 23)
    work_end = min(max(work_end, 1), 24)
    if work_end <= work_start:
        work_start, work_end = 8, 18
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
    _ = blocked_item_id
    service = writes
    if service is None and db is not None:
        service = WriteService(db, dry_run=dry_run)
    cap = max(1, int(max_turns))
    conversation: list[dict[str, Any]] = []
    reply_buffer: list[str] = []
    turns = 0
    model_calls = 0
    owner_dms: list[str] = []

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
