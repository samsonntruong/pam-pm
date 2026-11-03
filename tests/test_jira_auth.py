"""Jira auth probe: fail loudly on bad credentials."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from pam_pm.adapters.pm.jira import AUTH_ERROR, JiraAdapter, JiraError

BASE = "https://fake.atlassian.net"
EMAIL = "tester@example.com"
PROJECT = "TEST"
TOKEN = "fake-token-1"
FIX = "https://id.atlassian.com/manage-profile/security/api-tokens"


@pytest.fixture()
def jira_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", BASE)
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    monkeypatch.setenv("JIRA_PROJECT", PROJECT)
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)


def _adapter(handler: Any) -> JiraAdapter:
    return JiraAdapter(
        base_url=BASE,
        email=EMAIL,
        project=PROJECT,
        transport=httpx.MockTransport(handler),
    )


def _issue(key: str = "TEST-1") -> dict[str, Any]:
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
            "updated": "2026-02-01T00:00:00.000+00:00",
            "issuelinks": [],
            "customfield_10020": [],
        },
    }


def test_myself_401_raises_and_skips_search(jira_env: None) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/rest/api/3/myself"):
            return httpx.Response(401, json={"message": "Unauthorized"})
        return httpx.Response(200, json={"issues": [_issue()]})

    adapter = _adapter(handle)
    try:
        with pytest.raises(JiraError) as excinfo:
            adapter.list_items(limit=5)
    finally:
        adapter.close()
    assert FIX in str(excinfo.value)
    assert "rejected the credentials" in str(excinfo.value)
    assert str(excinfo.value) == AUTH_ERROR
    assert not any("/search/jql" in c for c in calls)
    assert any("/myself" in c for c in calls)


def test_myself_200_proceeds(jira_env: None) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rest/api/3/myself"):
            return httpx.Response(200, json={"accountId": "abc"})
        if request.url.path.endswith("/rest/api/3/search/jql"):
            return httpx.Response(200, json={"issues": [_issue()]})
        return httpx.Response(404, json={})

    adapter = _adapter(handle)
    try:
        items = adapter.list_items(limit=5)
    finally:
        adapter.close()
    assert [i.id for i in items] == ["TEST-1"]


def test_auth_cached_across_list_items(jira_env: None) -> None:
    myself_calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal myself_calls
        if request.url.path.endswith("/rest/api/3/myself"):
            myself_calls += 1
            return httpx.Response(200, json={"accountId": "abc"})
        if request.url.path.endswith("/rest/api/3/search/jql"):
            return httpx.Response(200, json={"issues": []})
        return httpx.Response(404, json={})

    adapter = _adapter(handle)
    try:
        adapter.list_items(limit=5)
        adapter.list_items(limit=5)
        adapter.list_items(limit=5)
    finally:
        adapter.close()
    assert myself_calls == 1
