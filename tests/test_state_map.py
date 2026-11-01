"""M2b ADO state mapping with fake transport only."""

from __future__ import annotations

import json
import os

import httpx

from pam_pm.adapters.pm.azure_devops import STATE_MAP, ADOAdapter
from pam_pm.adapters.pm.base import is_closed


def _env() -> dict[str, str]:
    os.environ["ADO_ORG_URL"] = "https://dev.azure.com/fake"
    os.environ["ADO_PROJECT"] = "proj"
    os.environ["ADO_PAT"] = "test-pat-value"
    return {}


def test_state_map_values() -> None:
    assert STATE_MAP["Agile"]["done"] == "Closed"
    assert STATE_MAP["Scrum"]["done"] == "Done"
    assert STATE_MAP["Agile"]["open"] == "New"
    assert STATE_MAP["Scrum"]["open"] == "To Do"


def test_is_closed_both() -> None:
    assert is_closed("Done")
    assert is_closed("Closed")
    assert is_closed(" done ")
    assert not is_closed("Active")
    assert not is_closed("New")


def _adapter_with_capture(process: str, seen: dict) -> ADOAdapter:
    _env()

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8", errors="replace")
        seen["url"] = str(request.url)
        seen["body"] = body
        try:
            ops = json.loads(body) if body else []
        except ValueError:
            ops = []
        state = ""
        for op in ops if isinstance(ops, list) else []:
            if isinstance(op, dict) and op.get("path") == "/fields/System.State":
                state = str(op.get("value", ""))
        payload = {
            "id": 42,
            "fields": {
                "System.Title": "t",
                "System.State": state,
                "System.ChangedDate": "2026-01-01T00:00:00Z",
            },
        }
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    return ADOAdapter(transport=transport, process=process)


def test_close_item_uses_mapped_agile() -> None:
    seen: dict[str, str] = {}
    adapter = _adapter_with_capture("Agile", seen)
    try:
        item = adapter.close_item("42")
        assert item.state == "Closed"
        assert "Closed" in seen.get("body", "")
    finally:
        adapter.close()


def test_close_item_uses_mapped_scrum() -> None:
    seen: dict[str, str] = {}
    adapter = _adapter_with_capture("Scrum", seen)
    try:
        item = adapter.close_item("42")
        assert item.state == "Done"
        assert "Done" in seen.get("body", "")
    finally:
        adapter.close()
