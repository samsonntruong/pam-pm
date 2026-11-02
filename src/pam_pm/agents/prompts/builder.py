"""Single place where model request messages are assembled.

Fixed order: system prompt, tool definitions, project context (stable for
the sprint), then the conversation in order, then the volatile block LAST
in the final user message only. No timestamps in the prefix.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pam_pm.agents.prompts.system import (
    BLOCKER_PLAN_SYSTEM,
    BLOCKER_SYSTEM,
    INTERVIEWER_SYSTEM,
)
from pam_pm.agents.prompts.tools import FINISH_INTERVIEW

SYSTEM_PROMPTS = {
    "blocker": BLOCKER_SYSTEM,
    "blocker_plan": BLOCKER_PLAN_SYSTEM,
    "interviewer": INTERVIEWER_SYSTEM,
}

# Shared default prompt. All parallel interviewers use the exact same system
# prompt and tools; per-person detail lives in the messages, never here.
SYSTEM_PROMPT = INTERVIEWER_SYSTEM

# Built once at import with sorted keys and reused byte for byte.
TOOLS: list[dict[str, Any]] = [json.loads(json.dumps(FINISH_INTERVIEW, sort_keys=True))]

VOLATILE_HEADER = "Live context (refreshes every call, never cached):"


def _render_volatile(volatile: Mapping[str, Any] | str) -> str:
    if isinstance(volatile, str):
        return volatile
    lines = [f"{key}: {volatile[key]}" for key in sorted(volatile)]
    if not lines:
        return VOLATILE_HEADER
    return VOLATILE_HEADER + "\n" + "\n".join(lines)


def _normalize_turn(turn: Mapping[str, Any]) -> dict[str, Any]:
    """Copy one turn, shaping assistant text for the Responses API.

    The Meta Responses API rejects input_text on assistant messages,
    so assistant text is emitted as output_text parts. All other
    roles keep their content untouched (plain strings stay strings
    so prefix stability and chat fallback are unchanged).
    """
    item = dict(turn)
    if str(item.get("role", "")) != "assistant":
        content = item.get("content")
        if isinstance(content, list):
            item["content"] = [dict(p) if isinstance(p, dict) else p for p in content]
        return item
    content = item.get("content", "")
    if isinstance(content, str):
        item["content"] = [{"type": "output_text", "text": content}]
        return item
    if isinstance(content, list):
        parts: list[Any] = []
        for part in content:
            if not isinstance(part, dict):
                parts.append(part)
                continue
            one = dict(part)
            if one.get("type") in ("text", "input_text"):
                one["type"] = "output_text"
            parts.append(one)
        item["content"] = parts
        return item
    return item


def build_messages(
    role: str,
    project_context: str,
    conversation: Sequence[Mapping[str, Any]],
    volatile: Mapping[str, Any] | str | None,
) -> dict[str, Any]:
    """Assemble the request payload with a stable prefix.

    Unknown roles fall back to the shared SYSTEM_PROMPT. Conversation
    entries are copied in order and never reordered or compacted.
    Assistant turns are reshaped to output_text parts for the Meta
    Responses API; other roles pass through untouched.
    """
    system = SYSTEM_PROMPTS.get(role, SYSTEM_PROMPT)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": project_context},
    ]
    messages.extend(_normalize_turn(turn) for turn in conversation)
    if volatile is not None:
        messages.append({"role": "user", "content": _render_volatile(volatile)})
    return {"messages": messages, "tools": TOOLS}
