"""Owner display names resolve to user ids.

Seeding assigns owner "Samson Truong" via update_item assigned_to.
Each backend resolves the name to its id field; unknown names skip
the field and set fields["assign_note"]. Fakes only, no network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

SAMSON = "Samson Truong"
UNKNOWN = "Nobody Ever"


# Jira fake


class _FakeJiraOwner:
    def __init__(self) -> None:
        self.users = [
            {"accountId": "acc-samson-1", "displayName": SAMSON},
            {"accountId": "acc-samson-2", "displayName": "Samson Other"},
        ]
        self.put_fields: list[dict[str, Any]] = []
        self.search_calls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/user/search") and method == "GET":
            self.search_calls += 1
            query = request.url.params.get("query", "").strip().lower()
            if query == SAMSON.lower():
                return httpx.Response(200, json=[self.users[0]])
            if query == "samson":
                return httpx.Response(200, json=list(self.users))
            return httpx.Response(200, json=[])
        if "/rest/api/3/issue/" in path and method == "PUT":
            body = json.loads(request.content.decode()) if request.content else {}
            self.put_fields.append(dict(body.get("fields", {})))
            return httpx.Response(204)
        if "/rest/api/3/issue/" in path and method == "GET":
            return httpx.Response(200, json=_jira_issue())
        return httpx.Response(404, json={"message": "nope"})


def _jira_issue() -> dict[str, Any]:
    return {
        "id": "10001",
        "key": "PROJ-1",
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


def _jira_adapter(fake: _FakeJiraOwner, tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://fake.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "tester@example.com")
    monkeypatch.setenv("JIRA_PROJECT", "PROJ")
    monkeypatch.setenv("JIRA_API_TOKEN", "fake-token-1")
    from pam_pm.adapters.pm.jira import JiraAdapter

    return JiraAdapter(
        transport=httpx.MockTransport(fake.handle), data_dir=tmp_path
    )


def test_jira_known_resolves(tmp_path, monkeypatch) -> None:
    fake = _FakeJiraOwner()
    adapter = _jira_adapter(fake, tmp_path, monkeypatch)
    try:
        item = adapter.update_item("PROJ-1", {"assigned_to": SAMSON})
    finally:
        adapter.close()
    assert fake.put_fields
    assert fake.put_fields[-1].get("assignee") == {"accountId": "acc-samson-1"}
    assert "assign_note" not in item.fields


def test_jira_prefix_resolves_first(tmp_path, monkeypatch) -> None:
    fake = _FakeJiraOwner()
    adapter = _jira_adapter(fake, tmp_path, monkeypatch)
    try:
        adapter.update_item("PROJ-1", {"assigned_to": "Samson"})
    finally:
        adapter.close()
    assert fake.put_fields[-1].get("assignee") == {"accountId": "acc-samson-1"}


def test_jira_unknown_skips_with_note(tmp_path, monkeypatch) -> None:
    fake = _FakeJiraOwner()
    adapter = _jira_adapter(fake, tmp_path, monkeypatch)
    try:
        item = adapter.update_item("PROJ-1", {"assigned_to": UNKNOWN})
    finally:
        adapter.close()
    assert all("assignee" not in fields for fields in fake.put_fields)
    assert UNKNOWN in str(item.fields.get("assign_note", ""))


def test_jira_cache_reuses_instance(tmp_path, monkeypatch) -> None:
    fake = _FakeJiraOwner()
    adapter = _jira_adapter(fake, tmp_path, monkeypatch)
    try:
        adapter.update_item("PROJ-1", {"assigned_to": SAMSON})
        adapter.update_item("PROJ-1", {"assigned_to": SAMSON})
    finally:
        adapter.close()
    assert fake.search_calls == 1


# Asana fake


class _FakeAsanaOwner:
    def __init__(self) -> None:
        self.users = [{"gid": "u-samson", "name": SAMSON}]
        self.put_bodies: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/projects/123") and method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "gid": "123",
                        "name": "Test",
                        "workspace": {"gid": "w1", "name": "WS"},
                    }
                },
            )
        if path.endswith("/users") and method == "GET":
            return httpx.Response(200, json={"data": list(self.users)})
        if path.endswith("/tasks/a1") and method == "PUT":
            raw = json.loads(request.content.decode()) if request.content else {}
            data = raw.get("data", {}) if isinstance(raw, dict) else {}
            self.put_bodies.append(dict(data))
            return httpx.Response(200, json={"data": _asana_task()})
        if path.endswith("/tasks/a1") and method == "GET":
            return httpx.Response(200, json={"data": _asana_task()})
        return httpx.Response(404, json={"errors": [{"message": "nope"}]})


def _asana_task() -> dict[str, Any]:
    return {
        "gid": "a1",
        "name": "Sample",
        "notes": "",
        "memberships": [
            {"project": {"gid": "123"}, "section": {"gid": "s1", "name": "To Do"}}
        ],
        "tags": [],
        "due_on": None,
        "assignee": None,
        "modified_at": "2026-02-01T00:00:00.000Z",
        "permalink_url": "https://app.asana.com/0/123/a1",
        "projects": [{"gid": "123", "name": "Test"}],
    }


def _asana_adapter(fake: _FakeAsanaOwner, tmp_path, monkeypatch):
    monkeypatch.setenv("ASANA_PROJECT", "123")
    monkeypatch.setenv("ASANA_API_TOKEN", "fake-token-1")
    from pam_pm.adapters.pm.asana import AsanaAdapter

    return AsanaAdapter(
        transport=httpx.MockTransport(fake.handle), data_dir=tmp_path
    )


def test_asana_known_resolves(tmp_path, monkeypatch) -> None:
    fake = _FakeAsanaOwner()
    adapter = _asana_adapter(fake, tmp_path, monkeypatch)
    try:
        item = adapter.update_item("a1", {"assigned_to": SAMSON})
    finally:
        adapter.close()
    assert fake.put_bodies
    assert fake.put_bodies[-1].get("assignee") == "u-samson"
    assert "assign_note" not in item.fields


def test_asana_unknown_skips_with_note(tmp_path, monkeypatch) -> None:
    fake = _FakeAsanaOwner()
    adapter = _asana_adapter(fake, tmp_path, monkeypatch)
    try:
        item = adapter.update_item("a1", {"assigned_to": UNKNOWN})
    finally:
        adapter.close()
    assert all("assignee" not in body for body in fake.put_bodies)
    assert UNKNOWN in str(item.fields.get("assign_note", ""))


# Monday fake


class _FakeMondayOwner:
    def __init__(self) -> None:
        self.users = [{"id": "mu-samson", "name": SAMSON}]
        self.sent_values: list[dict[str, Any]] = []
        self.columns = [
            {"id": "owner", "title": "Owner", "type": "person"},
        ]
        self.groups = [{"id": "g1", "title": "To Do"}]

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        query = str(body.get("query", "") or "")
        variables = body.get("variables", {})
        if not isinstance(variables, dict):
            variables = {}
        if "BoardLookup" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "boards": [
                            {
                                "id": "M1",
                                "name": "Board",
                                "groups": self.groups,
                                "columns": self.columns,
                            }
                        ]
                    }
                },
            )
        if "UserLookup" in query:
            return httpx.Response(200, json={"data": {"users": list(self.users)}})
        if "ItemColumns" in query:
            values = variables.get("values", {})
            if isinstance(values, str):
                try:
                    values = json.loads(values)
                except ValueError:
                    values = {}
            self.sent_values.append(dict(values) if isinstance(values, dict) else {})
            return httpx.Response(
                200, json={"data": {"change_multiple_column_values": {"id": "1"}}}
            )
        if "ItemGet" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "id": "1",
                                "name": "Sample",
                                "group": {"id": "g1", "title": "To Do"},
                                "column_values": [],
                                "updated_at": "2026-03-01T00:00:00.000000Z",
                            }
                        ]
                    }
                },
            )
        return httpx.Response(400, json={"errors": [{"message": "nope"}]})


def _monday_adapter(fake: _FakeMondayOwner, tmp_path, monkeypatch):
    monkeypatch.setenv("MONDAY_BOARD", "Board")
    monkeypatch.setenv("MONDAY_API_TOKEN", "fake-token-1")
    from pam_pm.adapters.pm.monday import MondayAdapter

    return MondayAdapter(
        transport=httpx.MockTransport(fake.handle), data_dir=tmp_path
    )


def test_monday_known_resolves(tmp_path, monkeypatch) -> None:
    fake = _FakeMondayOwner()
    adapter = _monday_adapter(fake, tmp_path, monkeypatch)
    try:
        item = adapter.update_item("1", {"assigned_to": SAMSON})
    finally:
        adapter.close()
    assert fake.sent_values
    owner_val = fake.sent_values[-1].get("owner", {})
    people = owner_val.get("personsAndTeams", [])
    assert people and people[0].get("id") == "mu-samson"
    assert "assign_note" not in item.fields


def test_monday_unknown_skips_with_note(tmp_path, monkeypatch) -> None:
    fake = _FakeMondayOwner()
    adapter = _monday_adapter(fake, tmp_path, monkeypatch)
    try:
        item = adapter.update_item("1", {"assigned_to": UNKNOWN})
    finally:
        adapter.close()
    assert fake.sent_values == []
    assert UNKNOWN in str(item.fields.get("assign_note", ""))


# ClickUp fake


class _FakeClickUpOwner:
    def __init__(self, list_members: list[dict[str, Any]] | None = None) -> None:
        if list_members is None:
            list_members = [
                {
                    "user": {
                        "id": "12345",
                        "username": SAMSON,
                        "email": "samson@example.com",
                    }
                }
            ]
        self._list_members = list_members
        self.put_bodies: list[dict[str, Any]] = []
        self.statuses = [{"status": "To Do", "type": "open", "orderindex": 0}]

    def handle(self, request: httpx.Request) -> httpx.Response:
        import re

        path = request.url.path
        method = request.method
        if path.endswith("/team") and method == "GET":
            return httpx.Response(
                200, json={"teams": [{"id": "T1", "name": "Acme", "members": []}]}
            )
        if re.search(r"/team/[^/]+/space$", path) and method == "GET":
            return httpx.Response(200, json={"spaces": [{"id": "S1", "name": "Sp"}]})
        if re.search(r"/space/[^/]+/list$", path) and method == "GET":
            return httpx.Response(
                200,
                json={
                    "lists": [
                        {"id": "L1", "name": "Li", "statuses": self.statuses}
                    ]
                },
            )
        if re.search(r"/space/[^/]+/folder$", path) and method == "GET":
            return httpx.Response(200, json={"folders": []})
        if path.endswith("/list/L1/member") and method == "GET":
            return httpx.Response(200, json={"members": list(self._list_members)})
        if path.endswith("/task/1") and method == "PUT":
            body = json.loads(request.content.decode()) if request.content else {}
            self.put_bodies.append(dict(body))
            return httpx.Response(200, json={})
        if path.endswith("/task/1") and method == "GET":
            return httpx.Response(200, json=_clickup_task())
        return httpx.Response(404, json={"message": "nope"})


def _clickup_task() -> dict[str, Any]:
    return {
        "id": "1",
        "name": "Sample",
        "text_content": "",
        "description": "",
        "status": {"status": "To Do", "type": "open"},
        "tags": [],
        "due_date": None,
        "assignees": [],
        "priority": None,
        "date_created": "1769900000000",
        "date_updated": "1769900001000",
        "list": {"id": "L1", "name": "Li"},
        "url": "https://app.clickup.com/t/1",
        "dependencies": [],
        "dependents": [],
    }


def _clickup_adapter(fake: _FakeClickUpOwner, tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKUP_TEAM", "Acme")
    monkeypatch.setenv("CLICKUP_LIST", "Li")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "fake-token-1")
    from pam_pm.adapters.pm.clickup import ClickUpAdapter

    return ClickUpAdapter(
        transport=httpx.MockTransport(fake.handle), data_dir=tmp_path
    )


def test_clickup_known_resolves(tmp_path, monkeypatch) -> None:
    fake = _FakeClickUpOwner()
    adapter = _clickup_adapter(fake, tmp_path, monkeypatch)
    try:
        item = adapter.update_item("1", {"assigned_to": SAMSON})
    finally:
        adapter.close()
    assert fake.put_bodies
    assignees = fake.put_bodies[-1].get("assignees", [])
    assert [str(v) for v in assignees] == ["12345"]
    assert "assign_note" not in item.fields


def test_clickup_unknown_skips_with_note(tmp_path, monkeypatch) -> None:
    fake = _FakeClickUpOwner()
    adapter = _clickup_adapter(fake, tmp_path, monkeypatch)
    try:
        item = adapter.update_item("1", {"assigned_to": UNKNOWN})
    finally:
        adapter.close()
    assert all("assignees" not in body for body in fake.put_bodies)
    assert UNKNOWN in str(item.fields.get("assign_note", ""))


def test_clickup_team_fallback_resolves(tmp_path, monkeypatch) -> None:
    fake = _FakeClickUpOwner(list_members=[])

    orig_handle = fake.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/team") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "teams": [
                        {
                            "id": "T1",
                            "name": "Acme",
                            "members": [
                                {
                                    "user": {
                                        "id": "67890",
                                        "username": SAMSON,
                                        "email": "samson@example.com",
                                    }
                                }
                            ],
                        }
                    ]
                },
            )
        return orig_handle(request)

    adapter = _clickup_adapter(fake, tmp_path, monkeypatch)
    adapter.close()
    from pam_pm.adapters.pm.clickup import ClickUpAdapter

    adapter2 = ClickUpAdapter(
        team="Acme",
        list_ref="Li",
        transport=httpx.MockTransport(handle),
        data_dir=tmp_path,
    )
    try:
        item = adapter2.update_item("1", {"assigned_to": SAMSON})
    finally:
        adapter2.close()
    assert fake.put_bodies
    assert [str(v) for v in fake.put_bodies[-1].get("assignees", [])] == ["67890"]
    assert "assign_note" not in item.fields
