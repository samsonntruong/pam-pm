"""ADO System.Tags list values serialize to a joined string."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from pam_pm.adapters.pm.azure_devops import ADOAdapter


def _env() -> None:
    os.environ["ADO_ORG_URL"] = "https://dev.azure.com/fake"
    os.environ["ADO_PROJECT"] = "proj"
    os.environ["ADO_TEAM"] = "team"
    os.environ["ADO_PAT"] = "test-pat-value"


def _make_adapter(captured: list[Any]) -> ADOAdapter:
    _env()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH" and "/_apis/wit/workitems/12" in request.url.path:
            ops = json.loads(request.content.decode("utf-8"))
            captured.extend(ops)
            fields = {}
            for op in ops:
                if isinstance(op, dict) and str(op.get("path", "")).startswith("/fields/"):
                    fields[str(op["path"]).removeprefix("/fields/")] = op.get("value")
            fields.setdefault("System.Title", "t")
            return httpx.Response(200, json={"id": 12, "fields": fields})
        if request.method == "POST" and "/_apis/wit/workitems/$" in request.url.path:
            ops = json.loads(request.content.decode("utf-8"))
            captured.extend(ops)
            fields = {}
            for op in ops:
                if isinstance(op, dict) and str(op.get("path", "")).startswith("/fields/"):
                    fields[str(op["path"]).removeprefix("/fields/")] = op.get("value")
            return httpx.Response(200, json={"id": 13, "fields": fields})
        return httpx.Response(404, json={"message": "unknown fake route"})

    return ADOAdapter(transport=httpx.MockTransport(handle), process="Agile")


def test_update_tags_list_joins_with_semicolon() -> None:
    captured: list[Any] = []
    adapter = _make_adapter(captured)
    try:
        item = adapter.update_item("12", {"System.Tags": ["blocked"]})
        assert item.fields.get("System.Tags") == "blocked"
        tag_ops = [op for op in captured if op.get("path") == "/fields/System.Tags"]
        assert len(tag_ops) == 1
        assert tag_ops[0]["value"] == "blocked"
        assert isinstance(tag_ops[0]["value"], str)
    finally:
        adapter.close()


def test_update_tags_alias_list_joins_multiple() -> None:
    captured: list[Any] = []
    adapter = _make_adapter(captured)
    try:
        adapter.update_item("12", {"tags": ["a", "b"]})
        tag_ops = [op for op in captured if op.get("path") == "/fields/System.Tags"]
        assert len(tag_ops) == 1
        assert tag_ops[0]["value"] == "a; b"
    finally:
        adapter.close()


def test_create_tags_list_joins() -> None:
    captured: list[Any] = []
    adapter = _make_adapter(captured)
    try:
        adapter.create_item("t", {"tags": ["blocked", "urgent"]})
        tag_ops = [op for op in captured if op.get("path") == "/fields/System.Tags"]
        assert len(tag_ops) == 1
        assert tag_ops[0]["value"] == "blocked; urgent"
    finally:
        adapter.close()
