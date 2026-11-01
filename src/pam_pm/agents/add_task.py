"""Create one task from a Slack app mention (ADDONS item 5).

Parses messages like "@Pam add a task: fix login timeout, 3h, assign
Maya", shows a draft with owner and estimate, and creates the item on
Approve with evidence and Undo. Owner suggestions reuse the kickoff
selection order (names win, skill match, load vs capacity, Unassigned
flagged). Works on both backends.
"""

from __future__ import annotations

import itertools
import re
from typing import Any

from pam_pm.agents.kickoff import suggest_owners
from pam_pm.agents.planner import approve_proposal

_MENTION_RE = re.compile(r"^\s*(?:<@[^>]+>|@pam:?)\s*", re.IGNORECASE)
_TRIGGER_RE = re.compile(
    r"^add\s+(?:a\s+)?task\s*:?\s*(.*)$", re.IGNORECASE | re.DOTALL
)
_HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*h(?:ours?)?\b", re.IGNORECASE)
_MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?\b", re.IGNORECASE)
_OWNER_RE = re.compile(
    r"(?:,|\b|^)\s*(?:assign(?:ed)?(?:\s+to)?|assignee|owner)\s+"
    r"([A-Za-z][A-Za-z .'-]{1,40}?)\s*(?=,|$)",
    re.IGNORECASE,
)

_draft_seq = itertools.count(1)
PENDING: dict[str, dict[str, Any]] = {}


def _strip_mentions(text: str) -> str:
    """Remove leading app mention tokens such as @Pam or <@U123>."""
    out = str(text or "")
    while True:
        reduced = _MENTION_RE.sub("", out)
        if reduced == out:
            return out.strip()
        out = reduced


def parse_add_task(text: str) -> dict[str, Any] | None:
    """Parse an app mention into title, estimate, and owner hint.

    Accepts "add task" or "add a task" with an optional colon after
    stripping leading @Pam or <@U...> mentions. The estimate reads
    like "3h" or "30m" (minutes convert to fractional hours). The
    owner reads from "assign Maya", "assigned to Maya", "assignee
    Maya", or "owner Maya". Returns None when the text is not an
    add-task request or the title is empty.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _TRIGGER_RE.match(_strip_mentions(raw))
    if not match:
        return None
    rest = match.group(1).strip()
    owner_hint = ""
    owner_match = _OWNER_RE.search(rest)
    if owner_match:
        owner_hint = owner_match.group(1).strip()
        rest = _OWNER_RE.sub(" ", rest, count=1)
    estimate: float | None = None
    hours = _HOURS_RE.search(rest)
    if hours:
        try:
            estimate = float(hours.group(1))
        except ValueError:
            estimate = None
        rest = _HOURS_RE.sub(" ", rest, count=1)
    else:
        minutes = _MINUTES_RE.search(rest)
        if minutes:
            try:
                estimate = round(float(minutes.group(1)) / 60.0, 2)
            except ValueError:
                estimate = None
            rest = _MINUTES_RE.sub(" ", rest, count=1)
    title = re.sub(r",(\s*,)+", ",", rest)
    title = re.sub(r"\s+", " ", title).strip(" -,:\t")
    if not title:
        return None
    return {
        "title": title,
        "estimate": estimate,
        "owner_hint": owner_hint,
        "evidence_quote": raw[:500],
    }


def propose_add_task(
    text: str, roster: Any = None, board: Any = None
) -> dict[str, Any] | None:
    """Draft one owned task from an app mention, or None when no match.

    Owner suggestion reuses kickoff.suggest_owners, so the same order
    applies: explicit names win, then skill match, then load against
    capacity, else Unassigned flagged as needing an owner.
    """
    parsed = parse_add_task(text)
    if parsed is None:
        return None
    drafts = suggest_owners(
        [
            {
                "title": parsed["title"],
                "type": "Task",
                "estimate": parsed["estimate"],
                "description": parsed["title"],
                "owner_hint": parsed["owner_hint"],
            }
        ],
        roster or [],
        board,
    )
    draft = dict(drafts[0])
    draft["evidence_quote"] = parsed["evidence_quote"]
    return draft


def format_slack_draft(draft: dict[str, Any]) -> str:
    """Render a draft as Slack message text with owner and estimate."""
    title = str(draft.get("title", "") or "Untitled")
    owner = str(draft.get("owner") or "Unassigned")
    reason = str(draft.get("reason", "") or "")
    estimate = draft.get("estimate")
    if estimate is None:
        estimate_text = "Not set"
    else:
        try:
            estimate_text = f"{float(estimate):g}h"
        except (TypeError, ValueError):
            estimate_text = "Not set"
    lines = [
        f"New task draft: {title}",
        f"Owner: {owner} ({reason})" if reason else f"Owner: {owner}",
        f"Estimate: {estimate_text}",
    ]
    if draft.get("warning"):
        lines.append("Warning: this owner would be over capacity.")
    if draft.get("needs_owner"):
        lines.append("Needs an owner before creation.")
    quote = str(draft.get("evidence_quote", "") or "")
    if quote:
        lines.append(f'Evidence: "{quote}"')
    return "\n".join(lines)


def post_add_task_draft(
    chat: Any, channel: str, draft: dict[str, Any]
) -> dict[str, str]:
    """Post a draft with Approve and Cancel buttons, returning its ids."""
    draft_id = f"add-task-{next(_draft_seq)}"
    PENDING[draft_id] = dict(draft)
    message_id = chat.send_buttons(
        channel,
        format_slack_draft(draft),
        [
            {"id": "add_task_approve", "label": "Approve", "value": draft_id},
            {"id": "add_task_cancel", "label": "Cancel", "value": draft_id},
        ],
    )
    return {"draft_id": draft_id, "message_id": str(message_id)}


def handle_add_task_mention(
    text: str,
    roster: Any,
    board: Any,
    chat: Any,
    channel: str,
) -> dict[str, Any] | None:
    """Draft and post an add-task mention, or None when no match."""
    draft = propose_add_task(text, roster, board)
    if draft is None:
        return None
    posted = post_add_task_draft(chat, channel, draft)
    return {"draft": draft, **posted}


def approve_add_task(
    draft_id_or_draft: str | dict[str, Any],
    board: Any,
    *,
    writes: Any = None,
    db: Any = None,
    dry_run: bool = True,
    evidence_quote: str = "",
) -> list[Any]:
    """Create the board item for an approved draft.

    Accepts a pending draft id or a draft dict. Creation reuses
    approve_proposal, so evidence is logged and Undo works from the
    Activity screen like other writes. Unknown ids raise ValueError.
    """
    key: str | None = None
    if isinstance(draft_id_or_draft, str):
        key = draft_id_or_draft
        draft = PENDING.get(key)
        if draft is None:
            raise ValueError(f"unknown add-task draft: {key}")
    else:
        draft = dict(draft_id_or_draft)
    quote = (
        evidence_quote or str(draft.get("evidence_quote") or draft.get("title") or "")
    ).strip()[:500]
    created = approve_proposal(
        [draft], board, writes=writes, db=db, dry_run=dry_run,
        evidence_quote=quote,
    )
    if key is not None:
        PENDING.pop(key, None)
    return created
