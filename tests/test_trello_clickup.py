"""Trello and ClickUp adapters on fake transports, plus wizard wiring.

Covers CRUD, states, links, comments, poll watermarks, setup save
plus Test endpoints, board read-only honoring, and that secrets
never reach logs, pages, or progress files.
"""

from __future__ import annotations

import json
import logging
import re
import stat
import sys
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pam_pm.adapters.pm.base import is_closed
from pam_pm.adapters.pm.clickup import ClickUpAdapter, ClickUpError
from pam_pm.adapters.pm.clickup import redact as clickup_redact
from pam_pm.adapters.pm.trello import TrelloAdapter, TrelloError
from pam_pm.adapters.pm.trello import redact as trello_redact

TRELLO_BOARD = "Test Board"
TRELLO_KEY = "trello-test-key-1"
TRELLO_TOKEN = "trello-test-token-1"

CLICKUP_TEAM = "Acme"
CLICKUP_LIST = "Sprint list"
CLICKUP_TOKEN = "clickup-test-token-1"


class _FakeTrello:
    """In-memory Trello API v1 surface."""

    def __init__(self) -> None:
        self._cards: dict[str, dict[str, Any]] = {}
        self._attachments: dict[str, list[dict[str, Any]]] = {}
        self._comments: dict[str, list[dict[str, Any]]] = {}
        self._actions: list[dict[str, Any]] = []
        self._lists = [
            {"id": "list1", "name": "To Do", "pos": 1, "closed": False},
            {"id": "list2", "name": "Doing", "pos": 2, "closed": False},
            {"id": "list3", "name": "Done", "pos": 3, "closed": False},
        ]
        self._members = [{"id": "m1", "fullName": "sam", "username": "sam"}]
        self._labels = [
            {"id": "l1", "name": "sprint-1", "color": "green"},
            {"id": "l2", "name": "bug", "color": "red"},
        ]
        self._next_num = 1
        self._next_action = 1
        self._next_attach = 1
        self._clock = 0

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-02-01T00:00:00.{self._clock:06d}Z"

    def _log(self, card_id: str, action_type: str) -> None:
        self._actions.append(
            {
                "id": f"a{self._next_action}",
                "type": action_type,
                "date": self._stamp(),
                "data": {"card": {"id": card_id}},
            }
        )
        self._next_action += 1

    def _payload(self, card_id: str) -> dict[str, Any]:
        entry = self._cards[card_id]
        by_id = {label["id"]: label["name"] for label in self._labels}
        return {
            "id": card_id,
            "name": entry["name"],
            "desc": entry["desc"],
            "due": entry["due"],
            "idList": entry["idList"],
            "idMembers": list(entry["idMembers"]),
            "idLabels": list(entry["idLabels"]),
            "labels": [{"name": by_id[i]} for i in entry["idLabels"] if i in by_id],
            "dateLastActivity": entry["updated"],
            "shortLink": entry["short"],
            "shortUrl": f"https://trello.com/c/{card_id}/slug-{entry['num']}",
            "url": f"https://trello.com/c/{card_id}/slug-{entry['num']}",
            "badges": {
                "checklists": 1,
                "attachments": len(self._attachments.get(card_id, [])),
                "comments": len(self._comments.get(card_id, [])),
            },
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("key")
        assert request.url.params.get("token")
        path = request.url.path
        method = request.method
        params = dict(request.url.params)
        if path.endswith("/members/me/boards") and method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "b1",
                        "name": TRELLO_BOARD,
                        "shortLink": "boardshort",
                        "shortUrl": "https://trello.com/b/boardshort",
                        "url": "https://trello.com/b/boardshort/test-board",
                    }
                ],
            )
        if re.search(r"/boards/[^/]+/lists$", path) and method == "GET":
            return httpx.Response(200, json=list(self._lists))
        if re.search(r"/boards/[^/]+/members$", path) and method == "GET":
            return httpx.Response(200, json=list(self._members))
        if re.search(r"/boards/[^/]+/labels$", path) and method == "GET":
            return httpx.Response(200, json=list(self._labels))
        if re.search(r"/boards/[^/]+/cards$", path) and method == "GET":
            return httpx.Response(
                200, json=[self._payload(k) for k in self._cards]
            )
        if re.search(r"/boards/[^/]+/actions$", path) and method == "GET":
            return httpx.Response(200, json=list(self._actions))
        if path.endswith("/cards") and method == "POST":
            return self._create(params)
        match = re.search(r"/cards/([^/]+)/attachments$", path)
        if match:
            return self._attach_route(match.group(1), method, params)
        if path.endswith("/actions/comments") and method == "POST":
            card_id = path.rsplit("/cards/", 1)[-1].split("/")[0]
            return self._comment_create(card_id, params)
        match = re.search(r"/cards/([^/]+)/actions$", path)
        if match and method == "GET":
            return httpx.Response(
                200, json=list(self._comments.get(match.group(1), []))
            )
        match = re.search(r"/cards/([^/]+)$", path)
        if match:
            return self._card_route(match.group(1), method, params)
        return httpx.Response(404, json={"message": "unknown fake route"})

    def _split_ids(self, value: str) -> list[str]:
        return [p.strip() for p in value.split(",") if p.strip()]

    def _create(self, params: dict[str, str]) -> httpx.Response:
        card_id = f"card{self._next_num}"
        num = self._next_num
        self._next_num += 1
        due = params.get("due", "")
        if due.strip().lower() in ("", "null"):
            due = None
        self._cards[card_id] = {
            "num": num,
            "name": params.get("name", ""),
            "desc": params.get("desc", ""),
            "due": due,
            "idList": params.get("idList", "list1"),
            "idMembers": self._split_ids(params.get("idMembers", "")),
            "idLabels": self._split_ids(params.get("idLabels", "")),
            "short": f"sl{num}",
            "updated": self._stamp(),
        }
        self._log(card_id, "createCard")
        return httpx.Response(200, json={"id": card_id, "shortLink": f"sl{num}"})

    def _card_route(
        self, card_id: str, method: str, params: dict[str, str]
    ) -> httpx.Response:
        if card_id not in self._cards:
            return httpx.Response(404, json={"message": "not found"})
        if method == "GET":
            return httpx.Response(200, json=self._payload(card_id))
        if method == "PUT":
            entry = self._cards[card_id]
            if "name" in params:
                entry["name"] = params["name"]
            if "desc" in params:
                entry["desc"] = params["desc"]
            if "due" in params:
                due = params["due"]
                entry["due"] = None if due.strip().lower() in ("", "null") else due
            if params.get("idList"):
                entry["idList"] = params["idList"]
            if "idMembers" in params:
                entry["idMembers"] = self._split_ids(params["idMembers"])
            if "idLabels" in params:
                entry["idLabels"] = self._split_ids(params["idLabels"])
            entry["updated"] = self._stamp()
            self._log(card_id, "updateCard")
            return httpx.Response(200, json={})
        if method == "DELETE":
            del self._cards[card_id]
            self._attachments.pop(card_id, None)
            self._comments.pop(card_id, None)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "unknown fake route"})

    def _attach_route(
        self, card_id: str, method: str, params: dict[str, str]
    ) -> httpx.Response:
        if card_id not in self._cards:
            return httpx.Response(404, json={"message": "not found"})
        if method == "GET":
            return httpx.Response(
                200, json=list(self._attachments.get(card_id, []))
            )
        entry = {
            "id": f"att{self._next_attach}",
            "url": params.get("url", ""),
            "name": params.get("name", ""),
        }
        self._next_attach += 1
        self._attachments.setdefault(card_id, []).append(entry)
        return httpx.Response(200, json=entry)

    def _comment_create(
        self, card_id: str, params: dict[str, str]
    ) -> httpx.Response:
        if card_id not in self._cards:
            return httpx.Response(404, json={"message": "not found"})
        entry = {
            "id": f"c{self._next_action}",
            "type": "commentCard",
            "date": self._stamp(),
            "data": {"text": params.get("text", ""), "card": {"id": card_id}},
            "memberCreator": {"fullName": "Trello User", "username": "trellouser"},
        }
        self._next_action += 1
        self._comments.setdefault(card_id, []).append(entry)
        self._actions.append(entry)
        return httpx.Response(200, json=entry)


class _FakeClickUp:
    """In-memory ClickUp API v2 surface."""

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._comments: dict[str, list[dict[str, Any]]] = {}
        self._statuses = [
            {"status": "To Do", "type": "open", "orderindex": 0},
            {"status": "In Progress", "type": "custom", "orderindex": 1},
            {"status": "Done", "type": "done", "orderindex": 2},
        ]
        self._members = [{"user": {"id": "u1", "username": "sam", "email": "samx"}}]
        self._members[0]["user"]["email"] = "sam@example.com"
        self._next_num = 1
        self._next_comment = 1
        self._clock = 0
        self._base_ms = 1769900000000

    def _ms(self) -> str:
        self._clock += 1
        return str(self._base_ms + self._clock * 1000)

    def _payload(self, task_id: str) -> dict[str, Any]:
        entry = self._tasks[task_id]
        return {
            "id": task_id,
            "name": entry["name"],
            "text_content": entry["description"],
            "description": entry["description"],
            "status": dict(entry["status"]),
            "tags": [{"name": n} for n in entry["tags"]],
            "due_date": entry["due_date"],
            "assignees": [dict(a) for a in entry["assignees"]],
            "priority": dict(entry["priority"]) if entry["priority"] else None,
            "date_created": entry["created"],
            "date_updated": entry["updated"],
            "date_closed": None,
            "list": {"id": "L1", "name": CLICKUP_LIST},
            "url": f"https://app.clickup.com/t/{task_id}",
            "dependencies": [{"id": i} for i in entry["dependencies"]],
            "dependents": [{"id": i} for i in entry["dependents"]],
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "")
        path = request.url.path
        method = request.method
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else None
        except ValueError:
            body = None
        if path.endswith("/team") and method == "GET":
            return httpx.Response(
                200, json={"teams": [{"id": "T1", "name": CLICKUP_TEAM}]}
            )
        if re.search(r"/team/[^/]+/space$", path) and method == "GET":
            return httpx.Response(200, json={"spaces": [{"id": "S1", "name": "Space"}]})
        if re.search(r"/space/[^/]+/list$", path) and method == "GET":
            return httpx.Response(
                200,
                json={
                    "lists": [
                        {"id": "L1", "name": CLICKUP_LIST, "statuses": self._statuses}
                    ]
                },
            )
        if re.search(r"/space/[^/]+/folder$", path) and method == "GET":
            return httpx.Response(200, json={"folders": []})
        if path.endswith("/list/L1") and method == "GET":
            return httpx.Response(
                200,
                json={"id": "L1", "name": CLICKUP_LIST, "statuses": self._statuses},
            )
        if path.endswith("/list/L1/member") and method == "GET":
            return httpx.Response(200, json={"members": list(self._members)})
        if path.endswith("/list/L1/task") and method == "GET":
            return self._list_tasks(request)
        if path.endswith("/list/L1/task") and method == "POST":
            return self._create(body)
        link = re.search(r"/task/([^/]+)/link/([^/]+)$", path)
        if link and method == "POST":
            return self._link(link.group(1), link.group(2))
        comment = re.search(r"/task/([^/]+)/comment$", path)
        if comment:
            return self._comment_route(comment.group(1), method, body)
        match = re.search(r"/task/([^/]+)$", path)
        if match:
            return self._task_route(match.group(1), method, body)
        return httpx.Response(404, json={"message": "unknown fake route"})

    def _list_tasks(self, request: httpx.Request) -> httpx.Response:
        floor = request.url.params.get("date_updated_gt", "")
        rows = [self._payload(k) for k in self._tasks]
        if floor.strip():
            try:
                limit = int(floor)
            except ValueError:
                limit = 0
            rows = [r for r in rows if int(str(r["date_updated"])) > limit]
        return httpx.Response(200, json={"tasks": rows})

    def _status_for(self, name: str) -> dict[str, str]:
        for entry in self._statuses:
            if entry["status"].lower() == name.strip().lower():
                return dict(entry)
        return dict(self._statuses[0])

    def _users_for(self, ids: Any) -> list[dict[str, str]]:
        out = []
        for want in ids or []:
            for member in self._members:
                user = member["user"]
                if user["id"] == str(want):
                    out.append(dict(user))
        return out

    def _priority_for(self, value: Any) -> dict[str, str] | None:
        names = {1: "urgent", 2: "high", 3: "normal", 4: "low"}
        if value is None or value == "":
            return None
        try:
            num = int(str(value))
        except (TypeError, ValueError):
            return None
        if num not in names:
            return None
        return {"id": str(num), "priority": names[num]}

    def _create(self, body: Any) -> httpx.Response:
        payload = body if isinstance(body, dict) else {}
        task_id = str(self._next_num)
        self._next_num += 1
        status = self._status_for(str(payload.get("status", "") or "To Do"))
        tags = payload.get("tags", [])
        due = payload.get("due_date")
        entry = {
            "name": str(payload.get("name", "")),
            "description": str(payload.get("description", "") or ""),
            "status": status,
            "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
            "due_date": str(due) if due not in (None, "") else None,
            "assignees": self._users_for(payload.get("assignees", [])),
            "priority": self._priority_for(payload.get("priority")),
            "created": self._ms(),
            "updated": self._ms(),
            "estimate": payload.get("time_estimate"),
            "dependencies": [],
            "dependents": [],
        }
        self._tasks[task_id] = entry
        return httpx.Response(200, json={"id": task_id})

    def _task_route(
        self, task_id: str, method: str, body: Any
    ) -> httpx.Response:
        if task_id not in self._tasks:
            return httpx.Response(404, json={"message": "not found"})
        if method == "GET":
            data = self._payload(task_id)
            data["time_estimate"] = self._tasks[task_id]["estimate"]
            return httpx.Response(200, json=data)
        if method == "PUT":
            payload = body if isinstance(body, dict) else {}
            entry = self._tasks[task_id]
            if "name" in payload:
                entry["name"] = str(payload["name"])
            if "description" in payload:
                entry["description"] = str(payload.get("description", "") or "")
            if payload.get("status"):
                entry["status"] = self._status_for(str(payload["status"]))
            if "tags" in payload:
                tags = payload.get("tags", [])
                entry["tags"] = [str(t) for t in tags] if isinstance(tags, list) else []
            if "due_date" in payload:
                due = payload.get("due_date")
                entry["due_date"] = str(due) if due not in (None, "") else None
            if "assignees" in payload:
                entry["assignees"] = self._users_for(payload.get("assignees", []))
            if "time_estimate" in payload:
                entry["estimate"] = payload.get("time_estimate")
            if "priority" in payload:
                entry["priority"] = self._priority_for(payload.get("priority"))
            entry["updated"] = self._ms()
            return httpx.Response(200, json={})
        if method == "DELETE":
            del self._tasks[task_id]
            self._comments.pop(task_id, None)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "unknown fake route"})

    def _link(self, blocked: str, blocker: str) -> httpx.Response:
        if blocked not in self._tasks or blocker not in self._tasks:
            return httpx.Response(404, json={"message": "not found"})
        if blocker not in self._tasks[blocked]["dependencies"]:
            self._tasks[blocked]["dependencies"].append(blocker)
        if blocked not in self._tasks[blocker]["dependents"]:
            self._tasks[blocker]["dependents"].append(blocked)
        return httpx.Response(200, json={})

    def _comment_route(
        self, task_id: str, method: str, body: Any
    ) -> httpx.Response:
        if task_id not in self._tasks:
            return httpx.Response(404, json={"message": "not found"})
        if method == "GET":
            return httpx.Response(
                200, json={"comments": list(self._comments.get(task_id, []))}
            )
        text = str(body.get("comment_text", "")) if isinstance(body, dict) else ""
        entry = {
            "id": str(self._next_comment),
            "comment_text": text,
            "user": {"username": "ClickUp User"},
            "date": self._ms(),
        }
        self._next_comment += 1
        self._comments.setdefault(task_id, []).append(entry)
        return httpx.Response(200, json=entry)


@pytest.fixture()
def trello_env(monkeypatch):
    monkeypatch.setenv("TRELLO_BOARD", TRELLO_BOARD)
    monkeypatch.setenv("TRELLO_API_KEY", TRELLO_KEY)
    monkeypatch.setenv("TRELLO_TOKEN", TRELLO_TOKEN)


@pytest.fixture()
def clickup_env(monkeypatch):
    monkeypatch.setenv("CLICKUP_TEAM", CLICKUP_TEAM)
    monkeypatch.setenv("CLICKUP_LIST", CLICKUP_LIST)
    monkeypatch.setenv("CLICKUP_API_TOKEN", CLICKUP_TOKEN)


def _trello() -> tuple[TrelloAdapter, _FakeTrello]:
    fake = _FakeTrello()
    adapter = TrelloAdapter(transport=httpx.MockTransport(fake.handle))
    return adapter, fake


def _clickup() -> tuple[ClickUpAdapter, _FakeClickUp]:
    fake = _FakeClickUp()
    adapter = ClickUpAdapter(transport=httpx.MockTransport(fake.handle))
    return adapter, fake


def test_capability_flags(trello_env, clickup_env) -> None:
    trello, _ = _trello()
    clickup, _ = _clickup()
    try:
        assert trello.supports_dependency_links is True
        assert trello.supports_iterations is False
        assert trello.supports_comments is True
        assert clickup.supports_dependency_links is True
        assert clickup.supports_iterations is False
        assert clickup.supports_comments is True
    finally:
        trello.close()
        clickup.close()


def test_trello_crud(trello_env) -> None:
    adapter, _ = _trello()
    try:
        created = adapter.create_item(
            "Ship the thing",
            {
                "description": "details here",
                "tags": ["sprint-1"],
                "due_date": "2026-03-01",
                "owner": "sam",
            },
        )
        assert created.id == "card1"
        assert created.title == "Ship the thing"
        assert created.changed
        fetched = adapter.get_item(created.id)
        assert fetched.fields["description"] == "details here"
        assert fetched.fields["tags"] == ["sprint-1"]
        assert fetched.fields["due_date"] == "2026-03-01"
        assert fetched.fields["owner"] == "sam"
        assert fetched.fields["checklist_count"] == 1
        assert fetched.fields["attachments_count"] == 0
        updated = adapter.update_item(created.id, {"title": "Ship it"})
        assert updated.title == "Ship it"
        assert len(adapter.list_items()) == 1
        adapter.delete_item(created.id)
        assert adapter.list_items() == []
        with pytest.raises(TrelloError):
            adapter.get_item(created.id)
    finally:
        adapter.close()


def test_trello_states_and_close(trello_env) -> None:
    adapter, _ = _trello()
    try:
        assert adapter.list_states() == ["To Do", "Doing", "Done"]
        item = adapter.create_item("Close me", {})
        assert not is_closed(item.state)
        moved = adapter.update_item(item.id, {"state": "Doing"})
        assert moved.state == "Doing"
        closed = adapter.close_item(item.id)
        assert closed.state == "Done"
        assert is_closed(closed.state)
        assert closed.fields["is_closed"] is True
    finally:
        adapter.close()


def test_trello_links(trello_env) -> None:
    adapter, _ = _trello()
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
        with pytest.raises(TrelloError):
            adapter.add_link(first.id, second.id, "Not-A-Rel")
    finally:
        adapter.close()


def test_trello_comments(trello_env) -> None:
    adapter, _ = _trello()
    try:
        item = adapter.create_item("Discuss", {})
        assert adapter.list_comments(item.id) == []
        comment = adapter.add_comment(item.id, "Looks good")
        assert comment.id
        assert comment.item_id == item.id
        assert comment.text == "Looks good"
        assert comment.author == "Trello User"
        assert [c.text for c in adapter.list_comments(item.id)] == ["Looks good"]
        with pytest.raises(TrelloError):
            adapter.add_comment(item.id, "  ")
    finally:
        adapter.close()


def test_trello_no_iterations(trello_env) -> None:
    adapter, _ = _trello()
    try:
        assert adapter.list_iterations() == []
        with pytest.raises(TrelloError):
            adapter.create_iteration("Sprint 1")
        item = adapter.create_item("Work", {})
        cleared = adapter.set_item_iteration(item.id, "")
        assert cleared.id == item.id
        with pytest.raises(TrelloError):
            adapter.set_item_iteration(item.id, "sprint-1")
    finally:
        adapter.close()


def test_trello_poll_watermark(trello_env) -> None:
    adapter, _ = _trello()
    try:
        first = adapter.create_item("One", {})
        second = adapter.create_item("Two", {})
        assert [i.id for i in adapter.poll_changes()] == [first.id, second.id]
        assert [i.id for i in adapter.poll_changes(first.changed)] == [second.id]
        adapter.update_item(first.id, {"title": "One more"})
        assert [i.id for i in adapter.poll_changes(second.changed)] == [first.id]
    finally:
        adapter.close()


def test_clickup_crud(clickup_env) -> None:
    adapter, _ = _clickup()
    try:
        created = adapter.create_item(
            "Ship the thing",
            {
                "description": "details here",
                "tags": ["sprint-1"],
                "due_date": "2026-03-01",
                "owner": "sam",
                "estimate": 8,
                "priority": "high",
            },
        )
        assert created.id == "1"
        assert created.changed
        fetched = adapter.get_item(created.id)
        assert fetched.fields["description"] == "details here"
        assert fetched.fields["tags"] == ["sprint-1"]
        assert fetched.fields["due_date"] == "2026-03-01"
        assert fetched.fields["owner"] == "sam"
        assert fetched.fields["estimate"] == 8
        assert fetched.fields["priority"] == "high"
        assert fetched.fields["url"].endswith("/t/1")
        updated = adapter.update_item(created.id, {"title": "Ship it"})
        assert updated.title == "Ship it"
        assert len(adapter.list_items()) == 1
        adapter.delete_item(created.id)
        assert adapter.list_items() == []
        with pytest.raises(ClickUpError):
            adapter.get_item(created.id)
    finally:
        adapter.close()


def test_clickup_states_and_close(clickup_env) -> None:
    adapter, _ = _clickup()
    try:
        assert adapter.list_states() == ["To Do", "In Progress", "Done"]
        item = adapter.create_item("Close me", {"state": "To Do"})
        assert not is_closed(item.state)
        moved = adapter.update_item(item.id, {"state": "In Progress"})
        assert moved.state == "In Progress"
        closed = adapter.close_item(item.id)
        assert closed.state == "Done"
        assert is_closed(closed.state)
        assert closed.fields["is_closed"] is True
    finally:
        adapter.close()


def test_clickup_links(clickup_env) -> None:
    adapter, _ = _clickup()
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
        with pytest.raises(ClickUpError):
            adapter.add_link(first.id, second.id, "Not-A-Rel")
    finally:
        adapter.close()


def test_clickup_comments(clickup_env) -> None:
    adapter, _ = _clickup()
    try:
        item = adapter.create_item("Discuss", {})
        assert adapter.list_comments(item.id) == []
        comment = adapter.add_comment(item.id, "Looks good")
        assert comment.id
        assert comment.text == "Looks good"
        assert comment.author == "ClickUp User"
        assert [c.text for c in adapter.list_comments(item.id)] == ["Looks good"]
        with pytest.raises(ClickUpError):
            adapter.add_comment(item.id, "  ")
    finally:
        adapter.close()


def test_clickup_no_iterations(clickup_env) -> None:
    adapter, _ = _clickup()
    try:
        assert adapter.list_iterations() == []
        with pytest.raises(ClickUpError):
            adapter.create_iteration("Sprint 1")
        item = adapter.create_item("Work", {})
        cleared = adapter.set_item_iteration(item.id, "")
        assert cleared.id == item.id
        with pytest.raises(ClickUpError):
            adapter.set_item_iteration(item.id, "sprint-1")
    finally:
        adapter.close()


def test_clickup_poll_watermark(clickup_env) -> None:
    adapter, _ = _clickup()
    try:
        first = adapter.create_item("One", {})
        second = adapter.create_item("Two", {})
        assert [i.id for i in adapter.poll_changes()] == [first.id, second.id]
        assert [i.id for i in adapter.poll_changes(first.changed)] == [second.id]
        adapter.update_item(first.id, {"title": "One more"})
        assert [i.id for i in adapter.poll_changes(second.changed)] == [first.id]
    finally:
        adapter.close()


def test_saved_config_then_explicit(tmp_path, monkeypatch) -> None:
    for key in (
        "TRELLO_BOARD",
        "TRELLO_API_KEY",
        "TRELLO_TOKEN",
        "CLICKUP_TEAM",
        "CLICKUP_LIST",
        "CLICKUP_API_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    from pam_pm.core import secrets as secrets_mod

    secrets_mod.save_trello_config(
        tmp_path, board="saved-board", api_key=TRELLO_KEY, token=TRELLO_TOKEN
    )
    secrets_mod.save_clickup_config(
        tmp_path, team="saved-team", list_ref="saved-list", api_token=CLICKUP_TOKEN
    )
    trello = TrelloAdapter(
        transport=httpx.MockTransport(_FakeTrello().handle), data_dir=tmp_path
    )
    try:
        assert trello._board_ref == "saved-board"
    finally:
        trello.close()
    trello2 = TrelloAdapter(
        board="explicit-board",
        transport=httpx.MockTransport(_FakeTrello().handle),
        data_dir=tmp_path,
    )
    try:
        assert trello2._board_ref == "explicit-board"
    finally:
        trello2.close()
    clickup = ClickUpAdapter(
        transport=httpx.MockTransport(_FakeClickUp().handle), data_dir=tmp_path
    )
    try:
        assert clickup._list_ref == "saved-list"
    finally:
        clickup.close()


def test_secrets_never_in_logs_or_errors(
    trello_env, clickup_env, tmp_path, monkeypatch, caplog
) -> None:
    assert TRELLO_KEY not in trello_redact(f"boom {TRELLO_KEY} tail", TRELLO_KEY)
    assert TRELLO_TOKEN not in trello_redact(f"boom {TRELLO_TOKEN} tail", TRELLO_TOKEN)
    assert "[REDACTED]" in trello_redact(f"?key={TRELLO_KEY}&token={TRELLO_TOKEN}")
    assert CLICKUP_TOKEN not in clickup_redact(f"boom {CLICKUP_TOKEN} tail", CLICKUP_TOKEN)
    with caplog.at_level(logging.INFO):
        trello, _ = _trello()
        try:
            item = trello.create_item("Logged work", {})
            trello.add_comment(item.id, "note")
            trello.poll_changes()
            try:
                trello.get_item("NOPE-999")
            except TrelloError as exc:
                assert TRELLO_KEY not in str(exc)
                assert TRELLO_TOKEN not in str(exc)
        finally:
            trello.close()
        clickup, _ = _clickup()
        try:
            created = clickup.create_item("Logged work", {})
            clickup.add_comment(created.id, "note")
            clickup.poll_changes()
            try:
                clickup.get_item("9999")
            except ClickUpError as exc:
                assert CLICKUP_TOKEN not in str(exc)
        finally:
            clickup.close()
    assert TRELLO_KEY not in caplog.text
    assert TRELLO_TOKEN not in caplog.text
    assert CLICKUP_TOKEN not in caplog.text


@pytest.fixture()
def wiz_client(tmp_path, monkeypatch, caplog):
    for key in (
        "TRELLO_BOARD",
        "TRELLO_API_KEY",
        "TRELLO_TOKEN",
        "CLICKUP_TEAM",
        "CLICKUP_LIST",
        "CLICKUP_API_TOKEN",
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
        "/api/setup/trello",
        json={
            "token": "wizard-test-setup-token-1",
            "project": "tcwiz",
            "board": TRELLO_BOARD,
            "api_key": TRELLO_KEY,
            "trello_token": TRELLO_TOKEN,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    resp = client.post(
        "/api/setup/clickup",
        json={
            "token": "wizard-test-setup-token-1",
            "project": "tcwiz",
            "team": CLICKUP_TEAM,
            "list": CLICKUP_LIST,
            "api_token": CLICKUP_TOKEN,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    assert client.post("/api/test/trello?project=tcwiz").json()["ok"] is True
    assert client.post("/api/test/clickup?project=tcwiz").json()["ok"] is True

    blob = (home / "wizard_progress.json").read_text(encoding="utf-8")
    assert TRELLO_KEY not in blob
    assert TRELLO_TOKEN not in blob
    assert CLICKUP_TOKEN not in blob
    steps = json.loads(blob)["tcwiz"]["steps"]
    assert steps["trello"]["has_key"] is True
    assert steps["trello"]["has_token"] is True
    assert steps["trello"]["board"] == TRELLO_BOARD
    assert steps["clickup"]["has_token"] is True
    assert steps["clickup"]["list"] == CLICKUP_LIST

    if sys.platform != "win32":
        assert stat.S_IMODE((home / "trello_api_key.txt").stat().st_mode) == 0o600
    if sys.platform != "win32":
        assert stat.S_IMODE((home / "trello_token.txt").stat().st_mode) == 0o600
    if sys.platform != "win32":
        assert stat.S_IMODE((home / "clickup_api_token.txt").stat().st_mode) == 0o600

    page = client.get("/setup?project=tcwiz").text
    assert "Trello" in page
    assert "ClickUp" in page
    assert "/static/icons/trello.svg" in page
    assert "/static/icons/clickup.svg" in page
    assert "Asana" in page
    assert "Monday" in page
    assert "/static/icons/asana.svg" in page
    assert "/static/icons/monday.svg" in page
    assert "Coming soon" not in page
    assert "/api/test/trello" in page
    assert "/api/test/clickup" in page
    assert TRELLO_KEY not in page
    assert TRELLO_TOKEN not in page
    assert CLICKUP_TOKEN not in page
    assert "\u2014" not in page

    assert TRELLO_KEY not in caplog.text
    assert TRELLO_TOKEN not in caplog.text
    assert CLICKUP_TOKEN not in caplog.text

    assert client.get("/static/icons/trello.svg").status_code == 200
    assert client.get("/static/icons/clickup.svg").status_code == 200
    assert "<svg" in client.get("/static/icons/trello.svg").text


def test_board_read_only_with_capabilities(wiz_client, monkeypatch) -> None:
    client, _, app_mod = wiz_client
    monkeypatch.setenv("TRELLO_BOARD", TRELLO_BOARD)
    monkeypatch.setenv("TRELLO_API_KEY", TRELLO_KEY)
    monkeypatch.setenv("TRELLO_TOKEN", TRELLO_TOKEN)
    monkeypatch.setenv("CLICKUP_TEAM", CLICKUP_TEAM)
    monkeypatch.setenv("CLICKUP_LIST", CLICKUP_LIST)
    monkeypatch.setenv("CLICKUP_API_TOKEN", CLICKUP_TOKEN)
    trello = TrelloAdapter(transport=httpx.MockTransport(_FakeTrello().handle))
    clickup = ClickUpAdapter(transport=httpx.MockTransport(_FakeClickUp().handle))
    try:
        trello.create_item("Trello card", {})
        clickup.create_item("ClickUp card", {})
        app_mod.set_board_adapter("tproj", trello)
        app_mod.set_board_adapter("cproj", clickup)
        client.post(
            "/api/setup/backend",
            json={
                "token": "wizard-test-setup-token-1",
                "project": "tproj",
                "backend": "trello",
            },
        )
        client.post(
            "/api/setup/backend",
            json={
                "token": "wizard-test-setup-token-1",
                "project": "cproj",
                "backend": "clickup",
            },
        )
        for project in ("tproj", "cproj"):
            data = client.get(f"/api/board?project={project}").json()
            assert data["editable"] is False
            assert data["capabilities"]["dependency_links"] is True
            assert data["capabilities"]["iterations"] is False
            assert data["capabilities"]["comments"] is True
            assert len(data["items"]) == 1
            denied = client.post(
                f"/api/board/items?project={project}", json={"title": "Nope"}
            )
            assert denied.status_code == 403
        ttext = client.get("/board?project=tproj").text
        assert "Read only" in ttext
        assert "Open in Trello" in ttext
        assert "https://trello.com/c/card1" in ttext
        ctext = client.get("/board?project=cproj").text
        assert "Open in ClickUp" in ctext
        assert "https://app.clickup.com/t/1" in ctext
        assert TRELLO_KEY not in ttext
        assert TRELLO_TOKEN not in ttext
        assert CLICKUP_TOKEN not in ctext
    finally:
        trello.close()
        clickup.close()
