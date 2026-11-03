"""Jira estimate degrade: retry without time tracking on 400."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pam_pm.adapters.pm.jira import JiraAdapter, JiraError

BASE = "https://fake.atlassian.net"
EMAIL = "tester@example.com"
PROJECT = "TEST"
TOKEN = "fake-token-1"


def _issue_payload(key: str = "TEST-1") -> dict[str, Any]:
    return {
        "id": "10001",
        "key": key,
        "fields": {
            "summary": "Sample",
            "status": {"name": "To Do"},
            "description": None,
            "labels": [],
            "duedate": "",
            "assignee": None,
            "timeoriginalestimate": None,
            "timeestimate": None,
            "priority": None,
            "updated": "2026-02-01T00:00:00.000000Z",
            "issuelinks": [],
            "customfield_10020": [],
        },
    }


def _adapter(handler: Any) -> JiraAdapter:
    return JiraAdapter(
        base_url=BASE,
        email=EMAIL,
        project=PROJECT,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture()
def jira_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", BASE)
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    monkeypatch.setenv("JIRA_PROJECT", PROJECT)
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)


def test_create_retries_without_estimate(jira_env: None) -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode()) if request.content else {}
        if request.method == "POST" and path.endswith("/rest/api/3/issue"):
            fields = body.get("fields", {})
            seen.append(dict(fields))
            if "timeoriginalestimate" in fields:
                return httpx.Response(
                    400,
                    json={
                        "errors": {
                            "timeoriginalestimate": (
                                "Field 'timeoriginalestimate' cannot be set. "
                                "It is not on the appropriate screen, or unknown."
                            )
                        }
                    },
                )
            return httpx.Response(200, json={"id": "10001", "key": "TEST-1"})
        if request.method == "GET" and "/rest/api/3/issue/" in path:
            return httpx.Response(200, json=_issue_payload())
        return httpx.Response(404, json={"message": "nope"})

    adapter = _adapter(handle)
    try:
        item = adapter.create_item("Sample", {"estimate": 8})
    finally:
        adapter.close()
    assert len(seen) == 2
    assert "timeoriginalestimate" in seen[0]
    assert "timeoriginalestimate" not in seen[1]
    assert "timeestimate" not in seen[1]
    assert item.fields.get("estimate_dropped")
    reason = str(
        item.fields.get("estimate_drop_reason", "")
        or item.fields.get("estimate_dropped_reason", "")
    )
    assert reason
    assert "timeoriginalestimate" in reason.lower() or "400" in reason


def test_update_retries_without_estimate(jira_env: None) -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode()) if request.content else {}
        if request.method == "PUT" and "/rest/api/3/issue/" in path:
            fields = body.get("fields", {})
            seen.append(dict(fields))
            if "timeoriginalestimate" in fields:
                return httpx.Response(
                    400,
                    text="Field 'timeoriginalestimate' cannot be set. "
                    "It is not on the appropriate screen, or unknown.",
                )
            return httpx.Response(204)
        if request.method == "GET" and "/rest/api/3/issue/" in path:
            return httpx.Response(200, json=_issue_payload())
        return httpx.Response(404, json={"message": "nope"})

    adapter = _adapter(handle)
    try:
        item = adapter.update_item("TEST-1", {"estimate": 5, "title": "New"})
    finally:
        adapter.close()
    assert len(seen) == 2
    assert seen[1].get("summary") == "New"
    assert "timeoriginalestimate" not in seen[1]
    assert item.fields.get("estimate_dropped")


def test_other_400_still_raises(jira_env: None) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/rest/api/3/issue"):
            return httpx.Response(400, json={"errors": {"summary": "required"}})
        if request.method == "PUT" and "/rest/api/3/issue/" in path:
            return httpx.Response(400, json={"errors": {"summary": "required"}})
        if request.method == "GET":
            return httpx.Response(200, json=_issue_payload())
        return httpx.Response(404, json={"message": "nope"})

    adapter = _adapter(handle)
    try:
        with pytest.raises(JiraError):
            adapter.create_item("Sample", {"estimate": 8})
        with pytest.raises(JiraError):
            adapter.update_item("TEST-1", {"title": "x"})
    finally:
        adapter.close()
