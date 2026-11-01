"""Jira and Linear adapters on fake transports, plus wizard wiring.

Covers CRUD, states, links, comments, iterations, poll watermarks,
setup save plus Test endpoints, board read-only honoring, and that
secrets never reach logs, pages, or progress files.
"""

from __future__ import annotations

import json
import logging
import re
import stat
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pam_pm.adapters.pm.base import is_closed
from pam_pm.adapters.pm.jira import JiraAdapter, JiraError
from pam_pm.adapters.pm.jira import redact as jira_redact
from pam_pm.adapters.pm.linear import (
    LinearAdapter,
    LinearError,
)
from pam_pm.adapters.pm.linear import (
    redact as linear_redact,
)

JIRA_BASE = "https://fake.atlassian.net"
JIRA_EMAIL = "tester@example.com"
JIRA_PROJECT = "PROJ"
JIRA_TOKEN = "jira-test-token-1"

LINEAR_TEAM = "ENG"
LINEAR_KEY = "lin_api_testkey1"


class _FakeJira:
    """In-memory Jira Cloud plus Agile API surface."""

    def __init__(self) -> None:
        self._issues: dict[str, dict[str, Any]] = {}
        self._comments: dict[str, list[dict[str, Any]]] = {}
        self._sprints: list[dict[str, Any]] = []
        self._next_num = 1
        self._next_comment = 1
        self._next_sprint = 1
        self._clock = 0

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-02-01T00:00:00.{self._clock:06d}Z"

    def _payload(self, key: str) -> dict[str, Any]:
        entry = self._issues[key]
        return {
            "id": entry["num"],
            "key": key,
            "fields": {
                "summary": entry["summary"],
                "status": {"name": entry["status"]},
                "description": entry["description"],
                "labels": list(entry["labels"]),
                "duedate": entry["duedate"],
                "assignee": dict(entry["assignee"]) if entry["assignee"] else None,
                "timeoriginalestimate": entry["estimate"],
                "timeestimate": entry["remaining"],
                "priority": {"name": entry["priority"]} if entry["priority"] else None,
                "updated": entry["updated"],
                "issuelinks": list(entry["links"]),
                "customfield_10020": list(entry["sprints"]),
            },
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "").startswith("Basic ")
        path = request.url.path
        method = request.method
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else None
        except ValueError:
            body = None
        if path.endswith("/search/jql") and method == "POST":
            return self._search(body)
        if path.endswith("/issueLink") and method == "POST":
            return self._link(body)
        if "/rest/agile/1.0/" in path:
            return self._agile(path, method, body)
        if path.endswith("/statuses") and method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "1",
                        "name": "Task",
                        "statuses": [
                            {"name": "To Do", "statusCategory": {"key": "new"}},
                            {
                                "name": "In Progress",
                                "statusCategory": {"key": "indeterminate"},
                            },
                            {"name": "Done", "statusCategory": {"key": "done"}},
                        ],
                    }
                ],
            )
        if path.endswith("/comment"):
            return self._comments_route(path, method, body)
        if path.endswith("/transitions"):
            return self._transitions_route(path, method, body)
        if path.endswith("/rest/api/3/issue") and method == "POST":
            return self._create(body)
        match = re.search(r"/rest/api/3/issue/([^/]+)$", path)
        if match:
            return self._issue_route(match.group(1), method, body)
        return httpx.Response(404, json={"message": "unknown fake route"})

    def _search(self, body: Any) -> httpx.Response:
        jql = str(body.get("jql", "")) if isinstance(body, dict) else ""
        limit = int(body.get("maxResults", 50) or 50) if isinstance(body, dict) else 50
        keys = sorted(self._issues, key=lambda k: self._issues[k]["updated"])
        if "DESC" in jql:
            keys = list(reversed(keys))
        return httpx.Response(
            200, json={"issues": [self._payload(k) for k in keys[:limit]]}
        )

    def _create(self, body: Any) -> httpx.Response:
        fields = body.get("fields", {}) if isinstance(body, dict) else {}
        key = f"{JIRA_PROJECT}-{self._next_num}"
        self._next_num += 1
        self._issues[key] = {
            "num": str(self._next_num),
            "summary": str(fields.get("summary", "")),
            "status": "To Do",
            "description": fields.get("description"),
            "labels": list(fields.get("labels", []) or []),
            "duedate": str(fields.get("duedate", "") or ""),
            "assignee": self._assignee(fields.get("assignee")),
            "estimate": fields.get("timeoriginalestimate"),
            "remaining": fields.get("timeestimate"),
            "priority": self._priority(fields.get("priority")),
            "updated": self._stamp(),
            "links": [],
            "sprints": [],
        }
        return httpx.Response(200, json={"id": key, "key": key})

    @staticmethod
    def _assignee(node: Any) -> dict[str, str] | None:
        if not isinstance(node, dict):
            return None
        account = str(node.get("accountId", "") or "")
        if not account:
            return None
        return {"accountId": account, "displayName": account}

    @staticmethod
    def _priority(node: Any) -> str:
        if isinstance(node, dict):
            return str(node.get("name", "") or "")
        return str(node or "")

    def _apply(self, key: str, fields: Any) -> None:
        entry = self._issues[key]
        for name, val in fields.items() if isinstance(fields, dict) else []:
            if name == "summary":
                entry["summary"] = str(val)
            elif name == "description":
                entry["description"] = val
            elif name == "labels":
                entry["labels"] = list(val or [])
            elif name == "duedate":
                entry["duedate"] = str(val or "")
            elif name == "assignee":
                entry["assignee"] = self._assignee(val)
            elif name == "timeoriginalestimate":
                entry["estimate"] = val
            elif name == "timeestimate":
                entry["remaining"] = val
            elif name == "priority":
                entry["priority"] = self._priority(val)
        entry["updated"] = self._stamp()

    def _issue_route(
        self, key: str, method: str, body: Any
    ) -> httpx.Response:
        if key not in self._issues:
            return httpx.Response(404, json={"message": "not found"})
        if method == "GET":
            return httpx.Response(200, json=self._payload(key))
        if method == "PUT":
            fields = body.get("fields", {}) if isinstance(body, dict) else {}
            self._apply(key, fields)
            return httpx.Response(204)
        if method == "DELETE":
            del self._issues[key]
            self._comments.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "unknown fake route"})

    def _transitions_route(
        self, path: str, method: str, body: Any
    ) -> httpx.Response:
        key = path.rsplit("/issue/", 1)[-1].split("/")[0]
        if key not in self._issues:
            return httpx.Response(404, json={"message": "not found"})
        options = [
            {"id": "11", "to": {"name": "To Do"}},
            {"id": "21", "to": {"name": "In Progress"}},
            {"id": "31", "to": {"name": "Done"}},
        ]
        if method == "GET":
            return httpx.Response(200, json={"transitions": options})
        want = str(body.get("transition", {}).get("id", "")) if isinstance(body, dict) else ""
        names = {"11": "To Do", "21": "In Progress", "31": "Done"}
        if want not in names:
            return httpx.Response(400, json={"message": "bad transition"})
        self._issues[key]["status"] = names[want]
        self._issues[key]["updated"] = self._stamp()
        return httpx.Response(204)

    def _link(self, body: Any) -> httpx.Response:
        if not isinstance(body, dict):
            return httpx.Response(400, json={"message": "bad link"})
        outward = str(body.get("outwardIssue", {}).get("key", ""))
        inward = str(body.get("inwardIssue", {}).get("key", ""))
        if outward not in self._issues or inward not in self._issues:
            return httpx.Response(404, json={"message": "not found"})
        self._issues[outward]["links"].append(
            {"type": {"name": "Blocks"}, "outwardIssue": {"key": inward}}
        )
        self._issues[inward]["links"].append(
            {"type": {"name": "Blocks"}, "inwardIssue": {"key": outward}}
        )
        return httpx.Response(201, json={})

    def _comments_route(
        self, path: str, method: str, body: Any
    ) -> httpx.Response:
        key = path.rsplit("/issue/", 1)[-1].split("/")[0]
        if key not in self._issues:
            return httpx.Response(404, json={"message": "not found"})
        if method == "GET":
            return httpx.Response(
                200, json={"comments": list(self._comments.get(key, []))}
            )
        entry = {
            "id": str(self._next_comment),
            "body": body.get("body") if isinstance(body, dict) else None,
            "author": {"displayName": "Jira User"},
            "created": self._stamp(),
        }
        self._next_comment += 1
        self._comments.setdefault(key, []).append(entry)
        return httpx.Response(201, json=entry)

    def _agile(self, path: str, method: str, body: Any) -> httpx.Response:
        if path.endswith("/board") and method == "GET":
            return httpx.Response(200, json={"values": [{"id": 7, "name": "Board"}]})
        if path.endswith("/backlog/issue") and method == "POST":
            keys = body.get("issues", []) if isinstance(body, dict) else []
            for key in keys:
                if key in self._issues:
                    self._issues[key]["sprints"] = []
            return httpx.Response(204)
        move = re.search(r"/sprint/([^/]+)/issue$", path)
        if move and method == "POST":
            sprint = next(
                (s for s in self._sprints if str(s["id"]) == move.group(1)), None
            )
            if sprint is None:
                return httpx.Response(404, json={"message": "not found"})
            keys = body.get("issues", []) if isinstance(body, dict) else []
            for key in keys:
                if key in self._issues:
                    self._issues[key]["sprints"] = [
                        {"id": sprint["id"], "name": sprint["name"]}
                    ]
            return httpx.Response(204)
        if path.endswith("/sprint") and method == "POST":
            sprint_id = self._next_sprint
            self._next_sprint += 1
            entry = {
                "id": sprint_id,
                "name": str(body.get("name", "")) if isinstance(body, dict) else "",
                "startDate": str(body.get("startDate", "") or "")
                if isinstance(body, dict)
                else "",
                "endDate": str(body.get("endDate", "") or "")
                if isinstance(body, dict)
                else "",
            }
            self._sprints.append(entry)
            return httpx.Response(201, json=entry)
        if "/sprint" in path and method == "GET":
            return httpx.Response(200, json={"values": list(self._sprints)})
        return httpx.Response(404, json={"message": "unknown fake route"})


class _FakeLinear:
    """In-memory Linear GraphQL surface."""

    def __init__(self) -> None:
        self._issues: dict[str, dict[str, Any]] = {}
        self._comments: dict[str, list[dict[str, Any]]] = {}
        self._cycles: list[dict[str, Any]] = []
        self._relations: list[tuple[str, str]] = []
        self._labels = {"sprint-1": "L1", "bug": "L2"}
        self._users = {"U1": "sam"}
        self._states = [
            {"id": "S1", "name": "Backlog", "type": "backlog", "position": 0},
            {"id": "S2", "name": "Todo", "type": "unstarted", "position": 1},
            {"id": "S3", "name": "In Progress", "type": "started", "position": 2},
            {"id": "S4", "name": "Done", "type": "completed", "position": 3},
            {"id": "S5", "name": "Canceled", "type": "canceled", "position": 4},
        ]
        self._next_num = 1
        self._next_comment = 1
        self._next_cycle = 1
        self._clock = 0

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-02-01T00:00:00.{self._clock:06d}Z"

    def _node(self, ident: str) -> dict[str, Any]:
        entry = self._issues[ident]
        state = next(s for s in self._states if s["id"] == entry["state_id"])
        cycle = next(
            (c for c in self._cycles if c["id"] == entry["cycle_id"]), None
        )
        return {
            "id": entry["uid"],
            "identifier": ident,
            "title": entry["title"],
            "description": entry["description"],
            "state": {"id": state["id"], "name": state["name"], "type": state["type"]},
            "labels": {"nodes": [{"name": n} for n in entry["labels"]]},
            "dueDate": entry["due_date"],
            "assignee": {"displayName": entry["owner"]} if entry["owner"] else None,
            "estimate": entry["estimate"],
            "priority": entry["priority"],
            "updatedAt": entry["updated"],
            "cycle": {"id": cycle["id"], "name": cycle["name"]} if cycle else None,
            "url": f"https://linear.app/fake/issue/{ident}",
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "")
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
        except ValueError:
            body = {}
        query = str(body.get("query", "")) if isinstance(body, dict) else ""
        variables = body.get("variables", {}) if isinstance(body, dict) else {}
        if "TeamLookup" in query:
            nodes = [{"id": "T1", "key": LINEAR_TEAM, "name": "Eng"}]
            return httpx.Response(200, json={"data": {"teams": {"nodes": nodes}}})
        if "TeamStates" in query:
            return httpx.Response(
                200, json={"data": {"team": {"states": {"nodes": self._states}}}}
            )
        if "TeamLabels" in query:
            nodes = [{"id": lid, "name": n} for n, lid in self._labels.items()]
            return httpx.Response(
                200, json={"data": {"team": {"labels": {"nodes": nodes}}}}
            )
        if "UserLookup" in query:
            want = str(variables.get("name", "")).strip().lower()
            nodes = [
                {"id": uid, "displayName": name}
                for uid, name in self._users.items()
                if name.lower() == want
            ]
            return httpx.Response(200, json={"data": {"users": {"nodes": nodes}}})
        if "TeamIssues" in query:
            return httpx.Response(200, json={"data": self._issues_data(variables)})
        if "IssueGet" in query:
            found = self._find(str(variables.get("id", "")))
            return httpx.Response(
                200, json={"data": {"issue": self._node(found) if found else None}}
            )
        if "IssueCreate" in query:
            return httpx.Response(
                200, json={"data": {"issueCreate": self._create(variables)}}
            )
        if "IssueUpdate" in query:
            return httpx.Response(
                200, json={"data": {"issueUpdate": self._update(variables)}}
            )
        if "IssueDelete" in query:
            self._issues.pop(str(variables.get("id", "")), None)
            return httpx.Response(
                200, json={"data": {"issueDelete": {"success": True}}}
            )
        if "CommentCreate" in query:
            return httpx.Response(
                200, json={"data": {"commentCreate": self._comment(variables)}}
            )
        if "RelationCreate" in query:
            payload = variables.get("input", {})
            self._relations.append(
                (str(payload.get("issueId", "")), str(payload.get("relatedIssueId", "")))
            )
            inner: dict[str, Any] = {
                "success": True,
                "issueRelation": {"id": f"R{len(self._relations)}"},
            }
            return httpx.Response(200, json={"data": {"issueRelationCreate": inner}})
        if "TeamCycles" in query:
            return httpx.Response(
                200,
                json={"data": {"team": {"cycles": {"nodes": list(self._cycles)}}}},
            )
        if "CycleCreate" in query:
            payload = variables.get("input", {})
            entry = {
                "id": f"C{self._next_cycle}",
                "name": str(payload.get("name", "")),
                "startsAt": str(payload.get("startsAt", "") or ""),
                "endsAt": str(payload.get("endsAt", "") or ""),
            }
            self._next_cycle += 1
            self._cycles.append(entry)
            return httpx.Response(
                200,
                json={"data": {"cycleCreate": {"success": True, "cycle": entry}}},
            )
        if "IssueComments" in query:
            ident = str(variables.get("id", ""))
            nodes = list(self._comments.get(ident, []))
            return httpx.Response(
                200, json={"data": {"issue": {"comments": {"nodes": nodes}}}}
            )
        if "IssueRelations" in query:
            ident = str(variables.get("id", ""))
            return httpx.Response(
                200, json={"data": {"issue": self._relations_data(ident)}}
            )
        return httpx.Response(200, json={"errors": [{"message": "unknown op"}]})

    def _find(self, key: str) -> str:
        if key in self._issues:
            return key
        for ident, entry in self._issues.items():
            if entry["uid"] == key:
                return ident
        return ""

    def _issues_data(self, variables: Any) -> dict[str, Any]:
        filt = variables.get("filter", {}) if isinstance(variables, dict) else {}
        extra = filt.get("updatedAt", {}) if isinstance(filt, dict) else {}
        idents = sorted(self._issues, key=lambda k: self._issues[k]["updated"])
        if isinstance(extra, dict):
            floor = str(extra.get("gte", "") or extra.get("gt", "") or "")
            if floor:
                idents = [
                    k for k in idents if self._issues[k]["updated"] >= floor[:26]
                ]
        return {
            "issues": {
                "nodes": [self._node(k) for k in idents],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }

    def _create(self, variables: Any) -> dict[str, Any]:
        payload = variables.get("input", {}) if isinstance(variables, dict) else {}
        ident = f"{LINEAR_TEAM}-{self._next_num}"
        self._next_num += 1
        entry = {
            "uid": f"I{self._next_num}",
            "title": str(payload.get("title", "")),
            "description": str(payload.get("description", "") or ""),
            "state_id": str(payload.get("stateId", "") or "S1"),
            "labels": self._label_names(payload.get("labelIds", [])),
            "due_date": str(payload.get("dueDate", "") or ""),
            "owner": self._users.get(str(payload.get("assigneeId", "") or ""), ""),
            "estimate": payload.get("estimate"),
            "priority": payload.get("priority"),
            "cycle_id": str(payload.get("cycleId", "") or ""),
            "updated": self._stamp(),
        }
        self._issues[ident] = entry
        return {"success": True, "issue": self._node(ident)}

    def _update(self, variables: Any) -> dict[str, Any]:
        key = str(variables.get("id", "")) if isinstance(variables, dict) else ""
        ident = self._find(key)
        payload = variables.get("input", {}) if isinstance(variables, dict) else {}
        if not ident or not isinstance(payload, dict):
            return {"success": False, "issue": None}
        entry = self._issues[ident]
        if "title" in payload:
            entry["title"] = str(payload["title"])
        if "description" in payload:
            entry["description"] = str(payload.get("description", "") or "")
        if payload.get("stateId"):
            entry["state_id"] = str(payload["stateId"])
        if "labelIds" in payload:
            entry["labels"] = self._label_names(payload.get("labelIds", []))
        if "dueDate" in payload:
            entry["due_date"] = str(payload.get("dueDate", "") or "")
        if payload.get("assigneeId"):
            entry["owner"] = self._users.get(str(payload["assigneeId"]), "")
        if "estimate" in payload:
            entry["estimate"] = payload.get("estimate")
        if "priority" in payload:
            entry["priority"] = payload.get("priority")
        if "cycleId" in payload:
            entry["cycle_id"] = str(payload.get("cycleId", "") or "")
        entry["updated"] = self._stamp()
        return {"success": True, "issue": self._node(ident)}

    def _label_names(self, ids: Any) -> list[str]:
        by_id = {lid: name for name, lid in self._labels.items()}
        return [by_id[i] for i in (ids or []) if i in by_id]

    def _comment(self, variables: Any) -> dict[str, Any]:
        payload = variables.get("input", {}) if isinstance(variables, dict) else {}
        ident = str(payload.get("issueId", ""))
        entry = {
            "id": str(self._next_comment),
            "body": str(payload.get("body", "")),
            "user": {"displayName": "Linear User"},
            "createdAt": self._stamp(),
        }
        self._next_comment += 1
        self._comments.setdefault(ident, []).append(entry)
        return {"success": True, "comment": entry}

    def _relations_data(self, ident: str) -> dict[str, Any]:
        rels = []
        inverse = []
        for blocker, blocked in self._relations:
            if blocker == ident and blocked in self._issues:
                rels.append(
                    {
                        "type": "blocks",
                        "relatedIssue": {
                            "id": self._issues[blocked]["uid"],
                            "identifier": blocked,
                        },
                    }
                )
            if blocked == ident and blocker in self._issues:
                inverse.append(
                    {
                        "type": "blocks",
                        "relatedIssue": {
                            "id": self._issues[blocker]["uid"],
                            "identifier": blocker,
                        },
                    }
                )
        return {"relations": {"nodes": rels}, "inverseRelations": {"nodes": inverse}}


@pytest.fixture()
def jira_env(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", JIRA_BASE)
    monkeypatch.setenv("JIRA_EMAIL", JIRA_EMAIL)
    monkeypatch.setenv("JIRA_PROJECT", JIRA_PROJECT)
    monkeypatch.setenv("JIRA_API_TOKEN", JIRA_TOKEN)


@pytest.fixture()
def linear_env(monkeypatch):
    monkeypatch.setenv("LINEAR_TEAM", LINEAR_TEAM)
    monkeypatch.setenv("LINEAR_API_KEY", LINEAR_KEY)


def _jira() -> tuple[JiraAdapter, _FakeJira]:
    fake = _FakeJira()
    adapter = JiraAdapter(transport=httpx.MockTransport(fake.handle))
    return adapter, fake


def _linear() -> tuple[LinearAdapter, _FakeLinear]:
    fake = _FakeLinear()
    adapter = LinearAdapter(transport=httpx.MockTransport(fake.handle))
    return adapter, fake


def test_capability_flags(jira_env, linear_env) -> None:
    jira, _ = _jira()
    linear, _ = _linear()
    try:
        for adapter in (jira, linear):
            assert adapter.supports_dependency_links is True
            assert adapter.supports_iterations is True
            assert adapter.supports_comments is True
    finally:
        jira.close()
        linear.close()


def test_jira_crud(jira_env) -> None:
    adapter, _ = _jira()
    try:
        created = adapter.create_item(
            "Ship the thing",
            {
                "description": "details here",
                "tags": ["sprint-1"],
                "due_date": "2026-03-01",
                "owner": "owner-1",
                "estimate": 8,
                "remaining": 5,
            },
        )
        assert created.id == "PROJ-1"
        assert created.title == "Ship the thing"
        assert created.changed
        fetched = adapter.get_item(created.id)
        assert fetched.fields["description"] == "details here"
        assert fetched.fields["tags"] == ["sprint-1"]
        assert fetched.fields["due_date"] == "2026-03-01"
        assert fetched.fields["owner"] == "owner-1"
        assert fetched.fields["estimate"] == 8
        assert fetched.fields["remaining"] == 5
        updated = adapter.update_item(created.id, {"title": "Ship it", "remaining": 2})
        assert updated.title == "Ship it"
        assert updated.fields["remaining"] == 2
        assert len(adapter.list_items()) == 1
        adapter.delete_item(created.id)
        assert adapter.list_items() == []
        with pytest.raises(JiraError):
            adapter.get_item(created.id)
    finally:
        adapter.close()


def test_jira_states_and_close(jira_env) -> None:
    adapter, _ = _jira()
    try:
        assert adapter.list_states() == ["To Do", "In Progress", "Done"]
        item = adapter.create_item("Close me", {})
        assert not is_closed(item.state)
        moved = adapter.update_item(item.id, {"state": "In Progress"})
        assert moved.state == "In Progress"
        closed = adapter.close_item(item.id)
        assert closed.state == "Done"
        assert is_closed(closed.state)
        assert closed.fields["is_closed"] is True
    finally:
        adapter.close()


def test_jira_links(jira_env) -> None:
    adapter, _ = _jira()
    try:
        first = adapter.create_item("First", {})
        second = adapter.create_item("Second", {})
        link = adapter.add_link(first.id, second.id, "blocks")
        assert (link.source_id, link.target_id, link.rel) == (
            first.id,
            second.id,
            "blocks",
        )
        assert adapter.add_link(first.id, second.id, "Dependency-Reverse").rel == (
            "is blocked by"
        )
        rels = [(e.target_id, e.rel) for e in adapter.list_links(first.id)]
        assert (second.id, "blocks") in rels
        assert (second.id, "is blocked by") in rels
        back = [(e.target_id, e.rel) for e in adapter.list_links(second.id)]
        assert (first.id, "is blocked by") in back
        with pytest.raises(JiraError):
            adapter.add_link(first.id, second.id, "Not-A-Rel")
    finally:
        adapter.close()


def test_jira_comments(jira_env) -> None:
    adapter, _ = _jira()
    try:
        item = adapter.create_item("Discuss", {})
        assert adapter.list_comments(item.id) == []
        comment = adapter.add_comment(item.id, "Looks good")
        assert comment.id
        assert comment.item_id == item.id
        assert comment.text == "Looks good"
        assert comment.author == "Jira User"
        assert [c.text for c in adapter.list_comments(item.id)] == ["Looks good"]
        with pytest.raises(JiraError):
            adapter.add_comment(item.id, "  ")
    finally:
        adapter.close()


def test_jira_iterations(jira_env) -> None:
    adapter, _ = _jira()
    try:
        sprint = adapter.create_iteration(
            "Sprint 1", start_date="2026-01-05", end_date="2026-01-18"
        )
        assert sprint.id
        assert sprint.start_date == "2026-01-05"
        assert sprint.end_date == "2026-01-18"
        assert "Sprint 1" in [e.name for e in adapter.list_iterations()]
        item = adapter.create_item("Sprint work", {})
        moved = adapter.set_item_iteration(item.id, sprint.id)
        assert moved.fields["iteration_id"] == sprint.id
        assert adapter.get_item(item.id).fields["iteration_id"] == sprint.id
        cleared = adapter.set_item_iteration(item.id, "")
        assert cleared.fields["iteration_id"] == ""
    finally:
        adapter.close()


def test_jira_poll_watermark(jira_env) -> None:
    adapter, _ = _jira()
    try:
        first = adapter.create_item("One", {})
        second = adapter.create_item("Two", {})
        assert [i.id for i in adapter.poll_changes()] == [first.id, second.id]
        assert [i.id for i in adapter.poll_changes(first.changed)] == [second.id]
        adapter.update_item(first.id, {"remaining": 1})
        assert [i.id for i in adapter.poll_changes(second.changed)] == [first.id]
    finally:
        adapter.close()


def test_linear_crud(linear_env) -> None:
    adapter, _ = _linear()
    try:
        created = adapter.create_item(
            "Ship the thing",
            {
                "description": "details here",
                "tags": ["sprint-1"],
                "due_date": "2026-03-01",
                "owner": "sam",
                "estimate": 8,
            },
        )
        assert created.id == "ENG-1"
        assert created.changed
        fetched = adapter.get_item(created.id)
        assert fetched.fields["description"] == "details here"
        assert fetched.fields["tags"] == ["sprint-1"]
        assert fetched.fields["due_date"] == "2026-03-01"
        assert fetched.fields["owner"] == "sam"
        assert fetched.fields["estimate"] == 8
        assert fetched.fields["url"].endswith("/ENG-1")
        updated = adapter.update_item(created.id, {"title": "Ship it"})
        assert updated.title == "Ship it"
        assert len(adapter.list_items()) == 1
        adapter.delete_item(created.id)
        assert adapter.list_items() == []
        with pytest.raises(LinearError):
            adapter.get_item(created.id)
    finally:
        adapter.close()


def test_linear_states_and_close(linear_env) -> None:
    adapter, _ = _linear()
    try:
        assert adapter.list_states() == [
            "Backlog",
            "Todo",
            "In Progress",
            "Done",
            "Canceled",
        ]
        item = adapter.create_item("Close me", {"state": "Todo"})
        assert not is_closed(item.state)
        closed = adapter.close_item(item.id)
        assert closed.state == "Done"
        assert is_closed(closed.state)
        assert closed.fields["is_closed"] is True
    finally:
        adapter.close()


def test_linear_links(linear_env) -> None:
    adapter, _ = _linear()
    try:
        first = adapter.create_item("First", {})
        second = adapter.create_item("Second", {})
        link = adapter.add_link(first.id, second.id, "blocks")
        assert (link.source_id, link.target_id, link.rel) == (
            first.id,
            second.id,
            "blocks",
        )
        rels = [(e.target_id, e.rel) for e in adapter.list_links(first.id)]
        assert rels == [(second.id, "blocks")]
        back = [(e.target_id, e.rel) for e in adapter.list_links(second.id)]
        assert back == [(first.id, "is blocked by")]
        with pytest.raises(LinearError):
            adapter.add_link(first.id, second.id, "Not-A-Rel")
    finally:
        adapter.close()


def test_linear_comments(linear_env) -> None:
    adapter, _ = _linear()
    try:
        item = adapter.create_item("Discuss", {})
        assert adapter.list_comments(item.id) == []
        comment = adapter.add_comment(item.id, "Looks good")
        assert comment.id
        assert comment.text == "Looks good"
        assert comment.author == "Linear User"
        assert [c.text for c in adapter.list_comments(item.id)] == ["Looks good"]
        with pytest.raises(LinearError):
            adapter.add_comment(item.id, "  ")
    finally:
        adapter.close()


def test_linear_iterations(linear_env) -> None:
    adapter, _ = _linear()
    try:
        cycle = adapter.create_iteration(
            "Cycle 1", start_date="2026-01-05", end_date="2026-01-18"
        )
        assert cycle.id
        assert cycle.start_date == "2026-01-05"
        assert cycle.end_date == "2026-01-18"
        assert "Cycle 1" in [e.name for e in adapter.list_iterations()]
        item = adapter.create_item("Cycle work", {})
        moved = adapter.set_item_iteration(item.id, cycle.id)
        assert moved.fields["iteration_id"] == cycle.id
        cleared = adapter.set_item_iteration(item.id, "")
        assert cleared.fields["iteration_id"] == ""
    finally:
        adapter.close()


def test_linear_poll_watermark(linear_env) -> None:
    adapter, _ = _linear()
    try:
        first = adapter.create_item("One", {})
        second = adapter.create_item("Two", {})
        assert [i.id for i in adapter.poll_changes()] == [first.id, second.id]
        assert [i.id for i in adapter.poll_changes(first.changed)] == [second.id]
        adapter.update_item(first.id, {"estimate": 3})
        assert [i.id for i in adapter.poll_changes(second.changed)] == [first.id]
    finally:
        adapter.close()


def test_saved_config_then_explicit(tmp_path, monkeypatch) -> None:
    for key in (
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_PROJECT",
        "JIRA_API_TOKEN",
        "LINEAR_TEAM",
        "LINEAR_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    from pam_pm.core import secrets as secrets_mod

    secrets_mod.save_jira_config(
        tmp_path,
        base_url="https://saved.atlassian.net",
        email="saved@example.com",
        project="saved",
        api_token=JIRA_TOKEN,
    )
    secrets_mod.save_linear_config(tmp_path, team="saved", api_key=LINEAR_KEY)
    jira = JiraAdapter(
        transport=httpx.MockTransport(_FakeJira().handle), data_dir=tmp_path
    )
    try:
        assert jira._base == "https://saved.atlassian.net"
        assert jira._project == "SAVED"
    finally:
        jira.close()
    jira2 = JiraAdapter(
        base_url="https://explicit.atlassian.net",
        email="e@example.com",
        project="expl",
        transport=httpx.MockTransport(_FakeJira().handle),
        data_dir=tmp_path,
    )
    try:
        assert jira2._base == "https://explicit.atlassian.net"
    finally:
        jira2.close()
    linear = LinearAdapter(
        transport=httpx.MockTransport(_FakeLinear().handle), data_dir=tmp_path
    )
    try:
        assert linear._team_key == "saved"
    finally:
        linear.close()


def test_secrets_never_in_logs_or_errors(
    jira_env, linear_env, tmp_path, monkeypatch, caplog
) -> None:
    assert JIRA_TOKEN not in jira_redact(f"boom {JIRA_TOKEN} tail", JIRA_TOKEN)
    assert LINEAR_KEY not in linear_redact(f"boom {LINEAR_KEY} tail", LINEAR_KEY)
    assert "[REDACTED]" in jira_redact(f"Basic abcdefgh {JIRA_TOKEN}", JIRA_TOKEN)
    with caplog.at_level(logging.INFO):
        jira, _ = _jira()
        try:
            item = jira.create_item("Logged work", {})
            jira.add_comment(item.id, "note")
            jira.poll_changes()
            try:
                jira.get_item("NOPE-999")
            except JiraError as exc:
                assert JIRA_TOKEN not in str(exc)
        finally:
            jira.close()
        linear, _ = _linear()
        try:
            created = linear.create_item("Logged work", {})
            linear.add_comment(created.id, "note")
            linear.poll_changes()
            try:
                linear.get_item("NOPE-9")
            except LinearError as exc:
                assert LINEAR_KEY not in str(exc)
        finally:
            linear.close()
    assert JIRA_TOKEN not in caplog.text
    assert LINEAR_KEY not in caplog.text


@pytest.fixture()
def wiz_client(tmp_path, monkeypatch, caplog):
    for key in (
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_PROJECT",
        "JIRA_API_TOKEN",
        "LINEAR_TEAM",
        "LINEAR_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("PAM_SETUP_TOKEN", "wizard-test-setup-token-1")
    import pam_pm.web.app as app_mod

    app_mod.SETUP_DONE = False
    caplog.set_level(logging.INFO)
    client = TestClient(app_mod.app)
    yield client, tmp_path, app_mod
    app_mod.reset_board_state()
    app_mod.SETUP_DONE = False


def test_wizard_save_then_test_green_no_leaks(wiz_client, caplog) -> None:
    client, home, _ = wiz_client
    resp = client.post(
        "/api/setup/jira",
        json={
            "token": "wizard-test-setup-token-1",
            "project": "jlwiz",
            "base_url": JIRA_BASE,
            "email": JIRA_EMAIL,
            "project_key": JIRA_PROJECT,
            "api_token": JIRA_TOKEN,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    resp = client.post(
        "/api/setup/linear",
        json={
            "token": "wizard-test-setup-token-1",
            "project": "jlwiz",
            "team": LINEAR_TEAM,
            "api_key": LINEAR_KEY,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    assert client.post("/api/test/jira?project=jlwiz").json()["ok"] is True
    assert client.post("/api/test/linear?project=jlwiz").json()["ok"] is True

    blob = (home / "wizard_progress.json").read_text(encoding="utf-8")
    assert JIRA_TOKEN not in blob
    assert LINEAR_KEY not in blob
    steps = json.loads(blob)["jlwiz"]["steps"]
    assert steps["jira"]["has_token"] is True
    assert steps["jira"]["project"] == JIRA_PROJECT
    assert steps["linear"]["has_key"] is True
    assert steps["linear"]["team"] == LINEAR_TEAM

    assert stat.S_IMODE((home / "jira_api_token.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((home / "linear_api_key.txt").stat().st_mode) == 0o600

    page = client.get("/setup?project=jlwiz").text
    assert "Jira" in page
    assert "Linear" in page
    assert "/static/icons/jira.svg" in page
    assert "/static/icons/linear.svg" in page
    assert "Asana, Monday" in page
    assert "Coming soon" in page
    assert "Jira, Asana, Linear" not in page
    assert "/api/test/jira" in page
    assert "/api/test/linear" in page
    assert JIRA_TOKEN not in page
    assert LINEAR_KEY not in page
    assert "\u2014" not in page

    assert JIRA_TOKEN not in caplog.text
    assert LINEAR_KEY not in caplog.text

    assert client.get("/static/icons/jira.svg").status_code == 200
    assert client.get("/static/icons/linear.svg").status_code == 200
    assert "<svg" in client.get("/static/icons/jira.svg").text


def test_board_read_only_with_capabilities(wiz_client, monkeypatch) -> None:
    client, _, app_mod = wiz_client
    monkeypatch.setenv("JIRA_BASE_URL", JIRA_BASE)
    monkeypatch.setenv("JIRA_EMAIL", JIRA_EMAIL)
    monkeypatch.setenv("JIRA_PROJECT", JIRA_PROJECT)
    monkeypatch.setenv("JIRA_API_TOKEN", JIRA_TOKEN)
    monkeypatch.setenv("LINEAR_TEAM", LINEAR_TEAM)
    monkeypatch.setenv("LINEAR_API_KEY", LINEAR_KEY)
    jira = JiraAdapter(transport=httpx.MockTransport(_FakeJira().handle))
    linear = LinearAdapter(transport=httpx.MockTransport(_FakeLinear().handle))
    try:
        jira.create_item("Jira card", {})
        linear.create_item("Linear card", {})
        app_mod.set_board_adapter("jproj", jira)
        app_mod.set_board_adapter("lproj", linear)
        client.post(
            "/api/setup/backend",
            json={
                "token": "wizard-test-setup-token-1",
                "project": "jproj",
                "backend": "jira",
            },
        )
        client.post(
            "/api/setup/backend",
            json={
                "token": "wizard-test-setup-token-1",
                "project": "lproj",
                "backend": "linear",
            },
        )
        for project in ("jproj", "lproj"):
            data = client.get(f"/api/board?project={project}").json()
            assert data["editable"] is False
            assert data["capabilities"]["dependency_links"] is True
            assert data["capabilities"]["iterations"] is True
            assert data["capabilities"]["comments"] is True
            assert len(data["items"]) == 1
            denied = client.post(
                f"/api/board/items?project={project}", json={"title": "Nope"}
            )
            assert denied.status_code == 403
        jtext = client.get("/board?project=jproj").text
        assert "Read only" in jtext
        assert "Open in Jira" in jtext
        assert f"{JIRA_BASE}/browse/PROJ-1" in jtext
        ltext = client.get("/board?project=lproj").text
        assert "Open in Linear" in ltext
        assert "https://linear.app/fake/issue/ENG-1" in ltext
        assert JIRA_TOKEN not in jtext
        assert LINEAR_KEY not in ltext
    finally:
        jira.close()
        linear.close()
