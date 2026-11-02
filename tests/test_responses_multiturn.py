"""Responses API multi-turn payload shape regression."""

from __future__ import annotations

import json
from typing import Any

import httpx

from pam_pm.agents.prompts.builder import build_messages
from pam_pm.models.client import ModelClient


def _client(handler: Any) -> ModelClient:
    c = ModelClient(model="m", base_url="https://meta.test", api_key="k")
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_builder_assistant_uses_output_text() -> None:
    conv = [
        {"role": "assistant", "content": "What is left?"},
        {"role": "user", "content": "Almost done."},
    ]
    payload = build_messages("interviewer", "ctx", conv, {"person": "U1"})
    assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert isinstance(assistant["content"], list)
    assert all(p["type"] == "output_text" for p in assistant["content"])
    assert "input_text" not in json.dumps(assistant)
    assert conv == [
        {"role": "assistant", "content": "What is left?"},
        {"role": "user", "content": "Almost done."},
    ]


def test_multiturn_responses_payload_per_role() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen["body"] = body
        for item in body["input"]:
            kinds = [
                p.get("type") for p in item["content"] if "text" in str(p.get("type"))
            ]
            if item["role"] == "assistant":
                assert kinds and all(k == "output_text" for k in kinds), item
            else:
                assert kinds and all(k == "input_text" for k in kinds), item
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    conv = [
        {"role": "assistant", "content": "What is the status?"},
        {"role": "user", "content": "Working on TASK-1."},
    ]
    payload = build_messages("interviewer", "Project ctx.", conv, {"person": "U1"})
    client = _client(handler)
    try:
        result = client.chat(payload["messages"], tools=payload["tools"])
    finally:
        client.close()
    assert result.text == "ok"
    roles = [i["role"] for i in seen["body"]["input"]]
    assert "assistant" in roles and "user" in roles and "system" in roles
