"""Live ADO fixes on fakes: lazy state map plus assignee resolution."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from pam_pm.adapters.pm.azure_devops import ADOAdapter


def _env() -> None:
    os.environ["ADO_ORG_URL"] = "https://dev.azure.com/fake"
    os.environ["ADO_PROJECT"] = "proj"
    os.environ["ADO_TEAM"] = "team"
    os.environ["ADO_PAT"] = "test-pat-value"


def _make_adapter(handler: Any, **kwargs: Any) -> ADOAdapter:
    _env()
    return ADOAdapter(transport=httpx.MockTransport(handler), **kwargs)


def test_no_process_maps_open_to_new_on_agile() -> None:
    seen: dict[str, Any] = {"project_calls": 0, "bodies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/_apis/projects/proj") and request.method == "GET":
            seen["project_calls"] += 1
            return httpx.Response(
                200,
                json={
                    "capabilities": {
                        "processTemplate": {"templateName": "Agile"}
                    }
                },
            )
        body = request.content.decode("utf-8") if request.content else ""
        seen["bodies"].append(body)
        ops = json.loads(body) if body else []
        state = ""
        for op in ops if isinstance(ops, list) else []:
            if op.get("path") == "/fields/System.State":
                state = str(op.get("value", ""))
        return httpx.Response(
            200,
            json={
                "id": 7,
                "fields": {
                    "System.Title": "t",
                    "System.State": state,
                    "System.ChangedDate": "2026-01-01T00:00:00Z",
                },
            },
        )

    adapter = _make_adapter(handler)
    try:
        assert adapter._process is None
        item = adapter.create_item("t", {"state": "open"})
        assert item.state == "New"
        assert seen["project_calls"] == 1
        assert adapter._process == "Agile"
        item2 = adapter.update_item("7", {"state": "done"})
        assert item2.state == "Closed"
        assert seen["project_calls"] == 1
        assert '"New"' in seen["bodies"][0]
    finally:
        adapter.close()


def test_assigned_to_resolved_descriptor_used() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/_apis/identities") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "displayName": "Maya Rao",
                            "descriptor": "aad.MAYA-DESCRIPTOR",
                        }
                    ]
                },
            )
        body = request.content.decode("utf-8") if request.content else ""
        seen["body"] = body
        ops = json.loads(body) if body else []
        assigned = ""
        for op in ops if isinstance(ops, list) else []:
            if op.get("path") == "/fields/System.AssignedTo":
                assigned = str(op.get("value", ""))
        return httpx.Response(
            200,
            json={
                "id": 11,
                "fields": {
                    "System.Title": "t",
                    "System.State": "New",
                    "System.AssignedTo": assigned,
                    "System.ChangedDate": "2026-01-01T00:00:00Z",
                },
            },
        )

    adapter = _make_adapter(handler, process="Agile")
    try:
        item = adapter.create_item("t", {"assigned_to": "Maya Rao"})
        assert "aad.MAYA-DESCRIPTOR" in seen.get("body", "")
        assert "warning" not in item.fields
    finally:
        adapter.close()


def test_assigned_to_fallback_raw_plus_warning(
    caplog: Any,
) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/_apis/identities") and request.method == "GET":
            return httpx.Response(403, json={"message": "denied"})
        body = request.content.decode("utf-8") if request.content else ""
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "id": 12,
                "fields": {
                    "System.Title": "t",
                    "System.State": "New",
                    "System.ChangedDate": "2026-01-01T00:00:00Z",
                },
            },
        )

    adapter = _make_adapter(handler, process="Agile")
    try:
        with caplog.at_level(logging.WARNING, logger="pam_pm.adapters.pm.azure_devops"):
            item = adapter.create_item("t", {"assigned_to": "Maya Rao"})
        assert "Maya Rao" in seen.get("body", "")
        assert "may be unassigned" in item.fields.get("warning", "")
        assert any(
            "may be unassigned" in rec.message for rec in caplog.records
        )
    finally:
        adapter.close()
