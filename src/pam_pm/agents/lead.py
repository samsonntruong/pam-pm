"""Lead synthesis agent: facts versus interpretation."""

from __future__ import annotations

from typing import Any

from pam_pm.agents.prompts.builder import build_messages

REVIEW_ROLE = "lead"
REVIEW_MAX_TOKENS = 6000
LOW_CONFIDENCE_THRESHOLD = 0.5
RETRO_TAG = "retro-action"


def _confidence_of(entry: Any) -> float:
    if isinstance(entry, dict):
        try:
            return float(entry.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    value = getattr(entry, "confidence", 0.0)
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _entry_id(key: Any, entry: Any, index: int) -> str:
    if isinstance(key, str) and key:
        return key
    if isinstance(entry, dict):
        for name in ("item_id", "id"):
            value = entry.get(name)
            if value:
                return str(value)
    for attr in ("item_id", "id"):
        value = getattr(entry, attr, None)
        if value:
            return str(value)
    return f"item-{index + 1}"


def _entry_text(entry: Any, name: str, default: str = "") -> str:
    if isinstance(entry, dict):
        return str(entry.get(name, default) or default)
    return str(getattr(entry, name, default) or default)


def _pairs_of(interviews: Any) -> list[tuple[Any, Any]]:
    if isinstance(interviews, dict):
        return list(interviews.items())
    if isinstance(interviews, (list, tuple)):
        return [(None, entry) for entry in interviews]
    if interviews is None:
        return []
    return [(None, interviews)]


def synthesize_daily(items: Any, interviews: Any) -> dict[str, Any]:
    """Separate facts from interpretation and flag low confidence.

    High confidence entries (0.5 and above) become facts. Lower entries
    become interpretations and are listed in low_confidence flags.
    """
    pairs = _pairs_of(interviews)
    facts: list[dict[str, Any]] = []
    interpretations: list[dict[str, Any]] = []
    low_confidence: list[str] = []
    for index, (key, entry) in enumerate(pairs):
        item_id = _entry_id(key, entry, index)
        confidence = _confidence_of(entry)
        record = {
            "item_id": item_id,
            "status": _entry_text(entry, "status"),
            "evidence_quote": _entry_text(entry, "evidence_quote"),
            "confidence": confidence,
        }
        if confidence < LOW_CONFIDENCE_THRESHOLD:
            interpretations.append(record)
            low_confidence.append(item_id)
        else:
            facts.append(record)
    _ = items
    return {
        "facts": facts,
        "interpretations": interpretations,
        "low_confidence": low_confidence,
    }


def _items_context(items: Any) -> str:
    if items is None:
        return "Sprint review. No board items."
    if isinstance(items, (str, dict)):
        raw = [items]
    elif isinstance(items, (list, tuple)):
        raw = list(items)
    else:
        raw = [items]
    lines = ["Sprint review. Board items:"]
    for index, entry in enumerate(raw):
        item_id = _entry_id(None, entry, index)
        title = _entry_text(entry, "title", item_id)
        state = _entry_text(entry, "state")
        lines.append(f"- {item_id}: {title} [{state}]")
    return "\n".join(lines)


def _events_text(events: Any) -> list[str]:
    if events is None:
        return []
    if isinstance(events, dict):
        raw = [events]
    elif isinstance(events, (list, tuple)):
        raw = list(events)
    else:
        raw = [events]
    lines: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            text = entry.strip()
        elif isinstance(entry, dict):
            op = str(entry.get("operation", "") or "")
            item = str(entry.get("item_id", "") or "")
            detail = str(entry.get("detail", "") or "")
            text = f"{op} {item} {detail}".strip()
        else:
            text = str(entry).strip()
        if text:
            lines.append(text)
    return lines


def _retro_actions(
    summary: dict[str, Any], events: Any
) -> list[dict[str, Any]]:
    """Derive retro actions from low confidence items and blockers.

    Only interview evidence and event text feed the actions. Nothing is
    invented: each action quotes or names its source item or event.
    """
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in summary.get("interpretations", []):
        item_id = str(record.get("item_id", ""))
        status = str(record.get("status", "") or "unknown")
        quote = str(record.get("evidence_quote", "") or "")
        key = f"followup:{item_id}"
        if key in seen:
            continue
        seen.add(key)
        detail = f"Confidence is low on {item_id} ({status})."
        if quote:
            detail += f' Evidence: "{quote}".'
        actions.append(
            {
                "title": f"Follow up on {item_id}: clarify status",
                "owner": None,
                "description": detail,
                "tags": [RETRO_TAG],
            }
        )
    for record in summary.get("facts", []) + summary.get("interpretations", []):
        item_id = str(record.get("item_id", ""))
        combined = (
            str(record.get("status", ""))
            + " "
            + str(record.get("evidence_quote", ""))
        ).lower()
        key = f"blocker:{item_id}"
        if "block" not in combined or key in seen:
            continue
        seen.add(key)
        quote = str(record.get("evidence_quote", "") or "")
        detail = f"Unblock {item_id}."
        if quote:
            detail += f' Evidence: "{quote}".'
        actions.append(
            {
                "title": f"Unblock {item_id}",
                "owner": None,
                "description": detail,
                "tags": [RETRO_TAG],
            }
        )
    for line in _events_text(events):
        if "block" not in line.lower():
            continue
        key = f"event:{line[:60]}"
        if key in seen:
            continue
        seen.add(key)
        actions.append(
            {
                "title": "Clear blocker from event log",
                "owner": None,
                "description": f"Event note: {line[:200]}.",
                "tags": [RETRO_TAG],
            }
        )
    return actions


def _render_review_markdown(
    summary: dict[str, Any], events: Any, actions: list[dict[str, Any]]
) -> str:
    lines = ["# Sprint review", ""]
    lines.append("## Facts")
    facts = summary.get("facts", [])
    if facts:
        for record in facts:
            item_id = record.get("item_id", "")
            status = record.get("status", "") or "unknown"
            quote = record.get("evidence_quote", "")
            lines.append(f"- {item_id}: {status}. Evidence: \"{quote}\".")
    else:
        lines.append("No high confidence facts. No interview evidence above threshold.")
    lines += ["", "## Interpretations"]
    interps = summary.get("interpretations", [])
    if interps:
        for record in interps:
            item_id = record.get("item_id", "")
            status = record.get("status", "") or "unknown"
            quote = record.get("evidence_quote", "")
            conf = record.get("confidence", 0.0)
            lines.append(
                f"- {item_id}: {status} (low confidence {conf}). "
                f'Evidence: "{quote}".'
            )
    else:
        lines.append("No low confidence interpretations.")
    lines += ["", "## Low confidence flags"]
    low = summary.get("low_confidence", [])
    if low:
        for item_id in low:
            lines.append(f"- {item_id}: needs follow up, evidence is weak.")
    else:
        lines.append("None. All entries meet the confidence threshold.")
    event_lines = _events_text(events)
    lines += ["", "## Events"]
    if event_lines:
        for line in event_lines[:20]:
            lines.append(f"- {line[:200]}")
    else:
        lines.append("No events recorded.")
    lines += ["", "## Retro actions"]
    if actions:
        for action in actions:
            lines.append(f"- {action.get('title', '')} [retro-action]")
    else:
        lines.append("No retro actions. Nothing flagged.")
    return "\n".join(lines) + "\n"


def _model_text(data: Any) -> str:
    if hasattr(data, "text"):
        return str(getattr(data, "text", "") or "")
    if isinstance(data, dict):
        return str(data.get("text", "") or "")
    if isinstance(data, str):
        return data
    return str(data or "")


def synthesize_review(
    items: Any,
    interviews: Any,
    events: Any = None,
    model_client: Any = None,
    max_tokens: int = REVIEW_MAX_TOKENS,
) -> dict[str, Any]:
    """Build a sprint review: markdown plus tagged retro actions.

    Facts come from high confidence interviews, interpretations from low
    confidence ones, and low confidence ids are flagged. The prompt is
    assembled with build_messages and large max_tokens (6000 or more).
    When no model client is given, the markdown is rendered locally from
    interview evidence only, so no status is ever invented.
    """
    summary = synthesize_daily(items, interviews)
    actions = _retro_actions(summary, events)
    conversation: list[dict[str, Any]] = []
    for record in summary["facts"] + summary["interpretations"]:
        conversation.append(
            {
                "role": "user",
                "content": (
                    f"{record['item_id']}: {record['status'] or 'unknown'}. "
                    f"Evidence: \"{record['evidence_quote']}\". "
                    f"Confidence: {record['confidence']}."
                ),
            }
        )
    for line in _events_text(events)[:20]:
        conversation.append({"role": "user", "content": f"Event: {line[:200]}"})
    volatile = {"low_confidence": ",".join(summary["low_confidence"]) or "none"}
    payload = build_messages(REVIEW_ROLE, _items_context(items), conversation, volatile)
    tokens = max(int(max_tokens or 0), REVIEW_MAX_TOKENS)
    markdown = ""
    model_path = "local"
    if model_client is not None:
        try:
            raw = model_client.chat(
                payload["messages"], tools=payload["tools"], max_tokens=tokens
            )
        except TypeError:
            raw = model_client.chat(payload["messages"], payload["tools"])
        markdown = _model_text(raw).strip()
        model_path = "live"
    if not markdown:
        markdown = _render_review_markdown(summary, events, actions)
        model_path = "local"
    return {
        "review_markdown": markdown,
        "markdown": markdown,
        "retro_actions": actions,
        "facts": summary["facts"],
        "interpretations": summary["interpretations"],
        "low_confidence": summary["low_confidence"],
        "max_tokens": tokens,
        "model_path": model_path,
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


def _action_field(action: Any, name: str, default: Any = "") -> Any:
    if isinstance(action, dict):
        value = action.get(name, default)
        return default if value is None else value
    value = getattr(action, name, default)
    return default if value is None else value


def _resolve_writes(writes: Any, db: Any, dry_run: bool) -> Any:
    if writes is not None:
        return writes
    if db is None:
        return None
    from pam_pm.core.writes import WriteService

    return WriteService(db, dry_run=dry_run)


def create_retro_items(
    board: Any,
    actions: Any,
    *,
    writes: Any = None,
    db: Any = None,
    dry_run: bool = True,
    iteration_id: str | None = None,
    evidence_quote: str = "",
) -> list[Any]:
    """Create board items for retro actions via a PMAdapter.

    Each action becomes one item in the board open state (New on the
    native board, mapped open state on ADO) tagged retro-action, with
    owner and description carried over. Items move to iteration_id when
    given and the board supports iterations. Each creation is logged
    through writes with its evidence quote when a write service is
    given. Returns the created items in action order.
    """
    if board is None:
        raise ValueError("board is required to create retro items")
    service = _resolve_writes(writes, db, dry_run)
    state = _open_state(board)
    drafts = list(actions) if isinstance(actions, (list, tuple)) else [actions]
    created: list[Any] = []
    for draft in drafts:
        title = str(_action_field(draft, "title", "") or "").strip() or "Retro action"
        owner = _action_field(draft, "owner", "")
        description = str(_action_field(draft, "description", "") or "") or title
        tags = _action_field(draft, "tags", "")
        fields: dict[str, Any] = {
            "state": state,
            "description": description,
            "tags": tags or [RETRO_TAG],
        }
        if owner and str(owner).strip():
            fields["assigned_to"] = str(owner).strip()
        item = board.create_item(title, fields)
        if iteration_id and getattr(board, "supports_iterations", True):
            setter = getattr(board, "set_item_iteration", None)
            if callable(setter):
                try:
                    item = setter(_item_id(item), iteration_id)
                except Exception:  # noqa: BLE001, S110
                    pass
        created.append(item)
        if service is not None:
            quote = (evidence_quote or description or title).strip()[:500]
            service.propose(
                {
                    "operation": "create_item",
                    "item_id": _item_id(item),
                    "evidence_quote": quote or title[:500],
                    "status": "proposed",
                    "detail": f"retro action from review: {title[:200]}",
                }
            )
    return created


def approve_retro_actions(board: Any, actions: Any, **kwargs: Any) -> list[Any]:
    """Alias for create_retro_items kept for planner symmetry."""
    return create_retro_items(board, actions, **kwargs)


class LeadAgent:
    """Synthesis helper that delegates to synthesize_daily."""

    def synthesize_daily(self, items: Any, interviews: Any) -> dict[str, Any]:
        """Separate facts from interpretation with confidence flags."""
        return synthesize_daily(items, interviews)

    def synthesize_review(
        self,
        items: Any,
        interviews: Any,
        events: Any = None,
        model_client: Any = None,
        max_tokens: int = REVIEW_MAX_TOKENS,
    ) -> dict[str, Any]:
        """Build a sprint review with markdown plus retro actions."""
        return synthesize_review(items, interviews, events, model_client, max_tokens)

    def create_retro_items(
        self, board: Any, actions: Any, **kwargs: Any
    ) -> list[Any]:
        """Create board items for retro actions."""
        return create_retro_items(board, actions, **kwargs)
