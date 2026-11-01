"""Prompt prefix stability: system and tools byte identical across calls."""

import json

from pam_pm.agents.prompts.builder import build_messages


def _payload(person: str, now: str) -> dict:
    project = "Project Atlas. Sprint 12. Roster in volatile block only."
    conversation = [
        {"role": "user", "content": "Yesterday I finished the login form."},
        {"role": "assistant", "content": "Nice. What is left on the item?"},
    ]
    volatile = {"board_diff": "none", "now": now, "person": person}
    return build_messages("interviewer", project, conversation, volatile)


def test_prefix_stable_across_people_and_times() -> None:
    first = _payload("Ana", "2026-11-01T09:00:00")
    second = _payload("Ben", "2026-11-01T15:30:00")

    assert json.dumps(first["tools"], sort_keys=True) == json.dumps(
        second["tools"], sort_keys=True
    )
    assert first["tools"] is second["tools"]

    head_first = json.dumps(first["messages"][:-1], sort_keys=True)
    head_second = json.dumps(second["messages"][:-1], sort_keys=True)
    assert head_first == head_second

    tail_first = json.dumps(first["messages"][-1], sort_keys=True)
    tail_second = json.dumps(second["messages"][-1], sort_keys=True)
    assert tail_first != tail_second

    # Volatile detail must not leak into the cached prefix.
    assert "Ana" not in head_first
    assert "Ben" not in head_second
    assert "2026-11-01" not in head_first
    assert "2026-11-01" not in head_second


def test_conversation_is_append_only() -> None:
    conversation = [{"role": "user", "content": "Working on TASK-1 today."}]
    payload = build_messages(
        "interviewer", "Project Atlas.", conversation, {"person": "Ana"}
    )
    assert conversation == [{"role": "user", "content": "Working on TASK-1 today."}]
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["content"] == "Project Atlas."
    assert payload["messages"][2] == conversation[0]
    assert payload["messages"][2] is not conversation[0]
