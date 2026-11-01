"""Asana and Monday adapters on fake transports, plus wizard wiring.

Covers CRUD, states, links, comments, poll watermarks, setup save
plus Test endpoints, board read-only honoring, and that secrets
never reach logs, pages, or progress files.
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

from pam_pm.adapters.pm.asana import AsanaAdapter, AsanaError
from pam_pm.adapters.pm.asana import redact as asana_redact
from pam_pm.adapters.pm.base import is_closed
from pam_pm.adapters.pm.monday import MondayAdapter, MondayError
from pam_pm.adapters.pm.monday import redact as monday_redact

ASANA_PROJECT = "Website launch"
ASANA_TOKEN = "asana-test-token-1"

MONDAY_BOARD = "Sprint board"
MONDAY_TOKEN = "monday-test-token-1"


class _FakeAsana:
    """In-memory Asana API 1.0 surface."""

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._stories: dict[str, list[dict[str, Any]]] = {}
        self._deps: dict[str, set[str]] = {}
        self._sections = [
            {"gid": "s1", "name": "To Do"},
            {"gid": "s2", "name": "Doing"},
            {"gid": "s3", "name": "Done"},
        ]
        self._users = [{"gid": "u1", "name": "sam"}]
        self._tags = [
            {"gid": "t1", "name": "sprint-1"},
            {"gid": "t2", "name": "bug"},
        ]
        self._next_num = 1
        self._next_story = 1
        self._clock = 0

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-02-01T00:00:00.{self._clock:06d}Z"

    def _section_name(self, gid: str) -> str:
        for entry in self._sections:
            if entry["gid"] == gid:
                return entry["name"]
        return ""

    def _payload(self, gid: str) -> dict[str, Any]:
        entry = self._tasks[gid]
        assignee = {"gid": "u1", "name": entry["assignee"]} if entry["assignee"] else None
        return {
            "gid": gid,
            "name": entry["name"],
            "notes": entry["notes"],
            "memberships": [
                {
                    "project": {"gid": "p1"},
                    "section": {"gid": entry["section"], "name": self._section_name(entry["section"])},
                }
            ],
            "tags": [{"gid": "tx", "name": n} for n in entry["tags"]],
            "due_on": entry["due_on"],
            "assignee": assignee,
            "modified_at": entry["modified"],
            "permalink_url": f"https://app.asana.com/0/p1/{gid}",
            "projects": [{"gid": "p1", "name": ASANA_PROJECT}],
        }

    def _body(self, request: httpx.Request) -> dict[str, Any]:
        try:
            raw = json.loads(request.content.decode("utf-8")) if request.content else {}
        except ValueError:
            return {}
        data = raw.get("data", {}) if isinstance(raw, dict) else {}
        return dict(data) if isinstance(data, dict) else {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "").startswith("Bearer ")
        path = request.url.path
        method = request.method
        if re.search(r"/projects$", path) and method == "GET":
            return httpx.Response(
                200,
                json={"data": [{"gid": "p1", "name": ASANA_PROJECT, "workspace": {"gid": "w1", "name": "WS"}}]},
            )
        match = re.search(r"/projects/([^/]+)/sections$", path)
        if match and method == "GET":
            if match.group(1) != "p1":
                return httpx.Response(404, json={"errors": [{"message": "not found"}]})
            return httpx.Response(200, json={"data": list(self._sections)})
        match = re.search(r"/projects/([^/]+)$", path)
        if match and method == "GET":
            if match.group(1) != "p1":
                return httpx.Response(404, json={"errors": [{"message": "not found"}]})
            return httpx.Response(
                200,
                json={"data": {"gid": "p1", "name": ASANA_PROJECT, "workspace": {"gid": "w1", "name": "WS"}}},
            )
        if path.endswith("/users") and method == "GET":
            return httpx.Response(200, json={"data": list(self._users)})
        if path.endswith("/tags") and method == "GET":
            return httpx.Response(200, json={"data": list(self._tags)})
        if path.endswith("/tasks") and method == "GET":
            return httpx.Response(200, json={"data": [self._payload(k) for k in self._tasks]})
        if path.endswith("/tasks") and method == "POST":
            return self._create(self._body(request))
        match = re.search(r"/sections/([^/]+)/addTask$", path)
        if match and method == "POST":
            return self._move(match.group(1), self._body(request))
        match = re.search(r"/tasks/([^/]+)/addDependencies$", path)
        if match and method == "POST":
            return self._add_deps(match.group(1), self._body(request))
        match = re.search(r"/tasks/([^/]+)/dependencies$", path)
        if match and method == "GET":
            return self._list_deps(match.group(1), forward=False)
        match = re.search(r"/tasks/([^/]+)/dependents$", path)
        if match and method == "GET":
            return self._list_deps(match.group(1), forward=True)
        match = re.search(r"/tasks/([^/]+)/stories$", path)
        if match:
            return self._story_route(match.group(1), method, self._body(request))
        match = re.search(r"/tasks/([^/]+)$", path)
        if match:
            return self._task_route(match.group(1), method, self._body(request))
        return httpx.Response(404, json={"errors": [{"message": "unknown fake route"}]})

    def _resolve_tags(self, gids: Any) -> list[str]:
        by_id = {t["gid"]: t["name"] for t in self._tags}
        if isinstance(gids, list):
            return [by_id[g] for g in gids if str(g) in by_id]
        return []

    def _resolve_user(self, gid: Any) -> str:
        for entry in self._users:
            if entry["gid"] == str(gid):
                return entry["name"]
        return ""

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        gid = f"a{self._next_num}"
        self._next_num += 1
        assignee = body.get("assignee")
        self._tasks[gid] = {
            "name": str(body.get("name", "")),
            "notes": str(body.get("notes", "") or ""),
            "section": "s1",
            "tags": self._resolve_tags(body.get("tags", [])),
            "due_on": str(body.get("due_on", "") or "") if body.get("due_on") else None,
            "assignee": self._resolve_user(assignee) if assignee else "",
            "modified": self._stamp(),
        }
        self._deps.setdefault(gid, set())
        return httpx.Response(200, json={"data": {"gid": gid}})

    def _task_route(self, gid: str, method: str, body: dict[str, Any]) -> httpx.Response:
        if gid not in self._tasks:
            return httpx.Response(404, json={"errors": [{"message": "not found"}]})
        if method == "GET":
            return httpx.Response(200, json={"data": self._payload(gid)})
        if method == "PUT":
            entry = self._tasks[gid]
            if "name" in body:
                entry["name"] = str(body["name"])
            if "notes" in body:
                entry["notes"] = str(body.get("notes", "") or "")
            if "due_on" in body:
                entry["due_on"] = str(body["due_on"]) if body["due_on"] else None
            if "assignee" in body:
                entry["assignee"] = self._resolve_user(body["assignee"]) if body["assignee"] else ""
            if "tags" in body:
                entry["tags"] = self._resolve_tags(body.get("tags", []))
            entry["modified"] = self._stamp()
            return httpx.Response(200, json={"data": self._payload(gid)})
        if method == "DELETE":
            del self._tasks[gid]
            self._stories.pop(gid, None)
            self._deps.pop(gid, None)
            for others in self._deps.values():
                others.discard(gid)
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(404, json={"errors": [{"message": "unknown fake route"}]})

    def _move(self, section_gid: str, body: dict[str, Any]) -> httpx.Response:
        gid = str(body.get("task", ""))
        if gid not in self._tasks:
            return httpx.Response(404, json={"errors": [{"message": "not found"}]})
        if section_gid not in {s["gid"] for s in self._sections}:
            return httpx.Response(404, json={"errors": [{"message": "not found"}]})
        self._tasks[gid]["section"] = section_gid
        self._tasks[gid]["modified"] = self._stamp()
        return httpx.Response(200, json={"data": {}})

    def _add_deps(self, gid: str, body: dict[str, Any]) -> httpx.Response:
        if gid not in self._tasks:
            return httpx.Response(404, json={"errors": [{"message": "not found"}]})
        for other in body.get("dependencies", []) or []:
            if str(other) in self._tasks:
                self._deps.setdefault(gid, set()).add(str(other))
        return httpx.Response(200, json={"data": {}})

    def _list_deps(self, gid: str, forward: bool) -> httpx.Response:
        if gid not in self._tasks:
            return httpx.Response(404, json={"errors": [{"message": "not found"}]})
        if forward:
            found = [k for k, v in self._deps.items() if gid in v]
        else:
            found = sorted(self._deps.get(gid, set()))
        return httpx.Response(200, json={"data": [{"gid": g} for g in found]})

    def _story_route(self, gid: str, method: str, body: dict[str, Any]) -> httpx.Response:
        if gid not in self._tasks:
            return httpx.Response(404, json={"errors": [{"message": "not found"}]})
        if method == "GET":
            return httpx.Response(200, json={"data": list(self._stories.get(gid, []))})
        entry = {
            "gid": f"st{self._next_story}",
            "type": "comment",
            "resource_subtype": "comment_added",
            "text": str(body.get("text", "")),
            "created_by": {"name": "Asana User"},
            "created_at": self._stamp(),
        }
        self._next_story += 1
        self._stories.setdefault(gid, []).append(entry)
        return httpx.Response(200, json={"data": entry})


class _FakeMonday:
    """In-memory Monday GraphQL surface."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._updates: dict[str, list[dict[str, Any]]] = {}
        self._groups = [
            {"id": "g1", "title": "To Do"},
            {"id": "g2", "title": "Doing"},
            {"id": "g3", "title": "Done"},
        ]
        self._columns = [
            {"id": "desc", "title": "Description", "type": "long-text"},
            {"id": "due", "title": "Due date", "type": "date"},
            {"id": "owner", "title": "Owner", "type": "person"},
            {"id": "tags", "title": "Tags", "type": "tags"},
            {"id": "dependency", "title": "Depends on", "type": "dependency"},
        ]
        self._users = [{"id": "mu1", "name": "sam"}]
        self._next_num = 1
        self._next_update = 1
        self._clock = 0

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-03-01T00:00:00.{self._clock:06d}Z"

    def _group_title(self, gid: str) -> str:
        for entry in self._groups:
            if entry["id"] == gid:
                return entry["title"]
        return ""

    def _payload(self, item_id: str) -> dict[str, Any]:
        entry = self._items[item_id]
        col_by_id = {c["id"]: c for c in self._columns}
        owner_name = entry["owner"]
        owner_id = ""
        for user in self._users:
            if user["name"] == owner_name:
                owner_id = user["id"]
        persons = {"personsAndTeams": [{"id": owner_id, "kind": "person"}]} if owner_id else {"personsAndTeams": []}
        values = [
            {"id": "desc", "text": entry["desc"], "value": json.dumps(entry["desc"]), "column": col_by_id["desc"]},
            {"id": "due", "text": entry["due"], "value": json.dumps({"date": entry["due"] or None}), "column": col_by_id["due"]},
            {"id": "owner", "text": owner_name, "value": json.dumps(persons), "column": col_by_id["owner"]},
            {"id": "tags", "text": ", ".join(entry["tags"]), "value": json.dumps({"labels": entry["tags"]}), "column": col_by_id["tags"]},
            {"id": "dependency", "text": "", "value": json.dumps({"linked_item_ids": sorted(entry["deps"])}), "column": col_by_id["dependency"]},
        ]
        return {
            "id": item_id,
            "name": entry["name"],
            "group": {"id": entry["group"], "title": self._group_title(entry["group"])},
            "column_values": values,
            "updated_at": entry["updated"],
        }

    def _apply_values(self, item_id: str, values: Any) -> None:
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except ValueError:
                return
        if not isinstance(values, dict):
            return
        entry = self._items[item_id]
        if "desc" in values:
            entry["desc"] = str(values["desc"] or "")
        if "due" in values:
            raw = values["due"]
            entry["due"] = str(raw.get("date", "") or "") if isinstance(raw, dict) else ""
        if "owner" in values:
            raw = values["owner"]
            people = raw.get("personsAndTeams", []) if isinstance(raw, dict) else []
            name = ""
            if isinstance(people, list) and people and isinstance(people[0], dict):
                for user in self._users:
                    if user["id"] == str(people[0].get("id", "")):
                        name = user["name"]
            entry["owner"] = name
        if "tags" in values:
            raw = values["tags"]
            labels = raw.get("labels", []) if isinstance(raw, dict) else []
            entry["tags"] = [str(v) for v in labels] if isinstance(labels, list) else []
        if "dependency" in values:
            raw = values["dependency"]
            ids = raw.get("linked_item_ids", []) if isinstance(raw, dict) else []
            entry["deps"] = {str(v) for v in ids if str(v) in self._items} if isinstance(ids, list) else set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "")
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
        except ValueError:
            body = {}
        query = str(body.get("query", "") or "")
        variables = body.get("variables", {}) if isinstance(body, dict) else {}
        if not isinstance(variables, dict):
            variables = {}
        if "BoardLookup" in query:
            return httpx.Response(200, json={"data": {"boards": [{"id": "M1", "name": MONDAY_BOARD, "groups": self._groups, "columns": self._columns}]}})
        if "BoardGroups" in query:
            return httpx.Response(200, json={"data": {"boards": [{"groups": list(self._groups)}]}})
        if "BoardColumns" in query:
            return httpx.Response(200, json={"data": {"boards": [{"columns": list(self._columns)}]}})
        if "BoardItems" in query:
            return httpx.Response(200, json={"data": {"boards": [{"items_page": {"items": [self._payload(k) for k in self._items]}}]}})
        if "ItemGet" in query:
            item_id = str(variables.get("itemId", ""))
            if item_id in self._items:
                return httpx.Response(200, json={"data": {"items": [self._payload(item_id)]}})
            return httpx.Response(200, json={"data": {"items": []}})
        if "ItemCreate" in query:
            return self._create(variables)
        if "ItemRename" in query:
            item_id = str(variables.get("itemId", ""))
            if item_id not in self._items:
                return httpx.Response(200, json={"errors": [{"message": "not found"}]})
            self._items[item_id]["name"] = str(variables.get("name", ""))
            self._items[item_id]["updated"] = self._stamp()
            return httpx.Response(200, json={"data": {"change_item_name": {"id": item_id}}})
        if "ItemMove" in query:
            item_id = str(variables.get("itemId", ""))
            group_id = str(variables.get("groupId", ""))
            if item_id not in self._items:
                return httpx.Response(200, json={"errors": [{"message": "not found"}]})
            self._items[item_id]["group"] = group_id
            self._items[item_id]["updated"] = self._stamp()
            return httpx.Response(200, json={"data": {"move_item_to_group": {"id": item_id}}})
        if "ItemColumns" in query:
            item_id = str(variables.get("itemId", ""))
            if item_id not in self._items:
                return httpx.Response(200, json={"errors": [{"message": "not found"}]})
            self._apply_values(item_id, variables.get("values", {}))
            self._items[item_id]["updated"] = self._stamp()
            return httpx.Response(200, json={"data": {"change_multiple_column_values": {"id": item_id}}})
        if "ItemDelete" in query:
            item_id = str(variables.get("itemId", ""))
            self._items.pop(item_id, None)
            self._updates.pop(item_id, None)
            for entry in self._items.values():
                entry["deps"].discard(item_id)
            return httpx.Response(200, json={"data": {"delete_item": {"id": item_id}}})
        if "ItemUpdates" in query:
            item_id = str(variables.get("itemId", ""))
            return httpx.Response(200, json={"data": {"items": [{"updates": list(self._updates.get(item_id, []))}]}})
        if "UpdateCreate" in query:
            item_id = str(variables.get("itemId", ""))
            if item_id not in self._items:
                return httpx.Response(200, json={"errors": [{"message": "not found"}]})
            entry = {
                "id": str(self._next_update),
                "body": str(variables.get("body", "")),
                "creator": {"name": "Monday User"},
                "created_at": self._stamp(),
            }
            self._next_update += 1
            self._updates.setdefault(item_id, []).append(entry)
            return httpx.Response(200, json={"data": {"create_update": entry}})
        if "UserLookup" in query:
            return httpx.Response(200, json={"data": {"users": list(self._users)}})
        return httpx.Response(400, json={"errors": [{"message": "unknown fake query"}]})

    def _create(self, variables: dict[str, Any]) -> httpx.Response:
        item_id = str(self._next_num)
        self._next_num += 1
        self._items[item_id] = {
            "name": str(variables.get("name", "")),
            "group": str(variables.get("groupId", "g1")),
            "desc": "",
            "due": "",
            "owner": "",
            "tags": [],
            "deps": set(),
            "updated": self._stamp(),
        }
        self._apply_values(item_id, variables.get("values", {}))
        self._items[item_id]["updated"] = self._stamp()
        return httpx.Response(200, json={"data": {"create_item": {"id": item_id}}})


@pytest.fixture()
def asana_env(monkeypatch):
    monkeypatch.setenv("ASANA_PROJECT", ASANA_PROJECT)
    monkeypatch.setenv("ASANA_API_TOKEN", ASANA_TOKEN)


@pytest.fixture()
def monday_env(monkeypatch):
    monkeypatch.setenv("MONDAY_BOARD", MONDAY_BOARD)
    monkeypatch.setenv("MONDAY_API_TOKEN", MONDAY_TOKEN)


def _asana() -> tuple[AsanaAdapter, _FakeAsana]:
    fake = _FakeAsana()
    adapter = AsanaAdapter(transport=httpx.MockTransport(fake.handle))
    return adapter, fake


def _monday() -> tuple[MondayAdapter, _FakeMonday]:
    fake = _FakeMonday()
    adapter = MondayAdapter(transport=httpx.MockTransport(fake.handle))
    return adapter, fake


def test_capability_flags(asana_env, monday_env) -> None:
    asana, _ = _asana()
    monday, _ = _monday()
    try:
        assert asana.supports_dependency_links is True
        assert asana.supports_iterations is False
        assert asana.supports_comments is True
        assert monday.supports_dependency_links is True
        assert monday.supports_iterations is False
        assert monday.supports_comments is True
    finally:
        asana.close()
        monday.close()


def test_asana_crud(asana_env) -> None:
    adapter, _ = _asana()
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
        assert created.id == "a1"
        assert created.title == "Ship the thing"
        assert created.changed
        fetched = adapter.get_item(created.id)
        assert fetched.fields["description"] == "details here"
        assert fetched.fields["tags"] == ["sprint-1"]
        assert fetched.fields["due_date"] == "2026-03-01"
        assert fetched.fields["owner"] == "sam"
        assert fetched.fields["url"].endswith("/0/p1/a1")
        updated = adapter.update_item(created.id, {"title": "Ship it"})
        assert updated.title == "Ship it"
        assert len(adapter.list_items()) == 1
        adapter.delete_item(created.id)
        assert adapter.list_items() == []
        with pytest.raises(AsanaError):
            adapter.get_item(created.id)
    finally:
        adapter.close()


def test_asana_states_and_close(asana_env) -> None:
    adapter, _ = _asana()
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


def test_asana_links(asana_env) -> None:
    adapter, _ = _asana()
    try:
        first = adapter.create_item("First", {})
        second = adapter.create_item("Second", {})
        link = adapter.add_link(first.id, second.id, "blocks")
        assert (link.source_id, link.target_id, link.rel) == (first.id, second.id, "blocks")
        assert adapter.add_link(first.id, second.id, "Dependency-Reverse").rel == "is blocked by"
        rels = [(e.target_id, e.rel) for e in adapter.list_links(first.id)]
        assert (second.id, "blocks") in rels
        assert (second.id, "is blocked by") in rels
        back = [(e.target_id, e.rel) for e in adapter.list_links(second.id)]
        assert (first.id, "is blocked by") in back
        with pytest.raises(AsanaError):
            adapter.add_link(first.id, second.id, "Not-A-Rel")
    finally:
        adapter.close()


def test_asana_comments(asana_env) -> None:
    adapter, _ = _asana()
    try:
        item = adapter.create_item("Discuss", {})
        assert adapter.list_comments(item.id) == []
        comment = adapter.add_comment(item.id, "Looks good")
        assert comment.id
        assert comment.item_id == item.id
        assert comment.text == "Looks good"
        assert comment.author == "Asana User"
        assert [c.text for c in adapter.list_comments(item.id)] == ["Looks good"]
        with pytest.raises(AsanaError):
            adapter.add_comment(item.id, "  ")
    finally:
        adapter.close()


def test_asana_no_iterations(asana_env) -> None:
    adapter, _ = _asana()
    try:
        assert adapter.list_iterations() == []
        with pytest.raises(AsanaError):
            adapter.create_iteration("Sprint 1")
        item = adapter.create_item("Work", {})
        cleared = adapter.set_item_iteration(item.id, "")
        assert cleared.id == item.id
        with pytest.raises(AsanaError):
            adapter.set_item_iteration(item.id, "sprint-1")
    finally:
        adapter.close()


def test_asana_poll_watermark(asana_env) -> None:
    adapter, _ = _asana()
    try:
        first = adapter.create_item("One", {})
        second = adapter.create_item("Two", {})
        assert [i.id for i in adapter.poll_changes()] == [first.id, second.id]
        assert [i.id for i in adapter.poll_changes(first.changed)] == [second.id]
        adapter.update_item(first.id, {"title": "One more"})
        assert [i.id for i in adapter.poll_changes(second.changed)] == [first.id]
    finally:
        adapter.close()


def test_monday_crud(monday_env) -> None:
    adapter, _ = _monday()
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
        assert created.id == "1"
        assert created.changed
        fetched = adapter.get_item(created.id)
        assert fetched.fields["description"] == "details here"
        assert fetched.fields["tags"] == ["sprint-1"]
        assert fetched.fields["due_date"] == "2026-03-01"
        assert fetched.fields["owner"] == "sam"
        assert fetched.fields["url"].endswith("/boards/M1/pulses/1")
        updated = adapter.update_item(created.id, {"title": "Ship it"})
        assert updated.title == "Ship it"
        assert len(adapter.list_items()) == 1
        adapter.delete_item(created.id)
        assert adapter.list_items() == []
        with pytest.raises(MondayError):
            adapter.get_item(created.id)
    finally:
        adapter.close()


def test_monday_states_and_close(monday_env) -> None:
    adapter, _ = _monday()
    try:
        assert adapter.list_states() == ["To Do", "Doing", "Done"]
        item = adapter.create_item("Close me", {"state": "To Do"})
        assert not is_closed(item.state)
        moved = adapter.update_item(item.id, {"state": "Doing"})
        assert moved.state == "Doing"
        closed = adapter.close_item(item.id)
        assert closed.state == "Done"
        assert is_closed(closed.state)
        assert closed.fields["is_closed"] is True
    finally:
        adapter.close()


def test_monday_links(monday_env) -> None:
    adapter, _ = _monday()
    try:
        first = adapter.create_item("First", {})
        second = adapter.create_item("Second", {})
        link = adapter.add_link(first.id, second.id, "blocks")
        assert (link.source_id, link.target_id, link.rel) == (first.id, second.id, "blocks")
        rels = [(e.target_id, e.rel) for e in adapter.list_links(first.id)]
        assert rels == [(second.id, "blocks")]
        back = [(e.target_id, e.rel) for e in adapter.list_links(second.id)]
        assert back == [(first.id, "is blocked by")]
        with pytest.raises(MondayError):
            adapter.add_link(first.id, second.id, "Not-A-Rel")
    finally:
        adapter.close()


def test_monday_comments(monday_env) -> None:
    adapter, _ = _monday()
    try:
        item = adapter.create_item("Discuss", {})
        assert adapter.list_comments(item.id) == []
        comment = adapter.add_comment(item.id, "Looks good")
        assert comment.id
        assert comment.text == "Looks good"
        assert comment.author == "Monday User"
        assert [c.text for c in adapter.list_comments(item.id)] == ["Looks good"]
        with pytest.raises(MondayError):
            adapter.add_comment(item.id, "  ")
    finally:
        adapter.close()


def test_monday_no_iterations(monday_env) -> None:
    adapter, _ = _monday()
    try:
        assert adapter.list_iterations() == []
        with pytest.raises(MondayError):
            adapter.create_iteration("Sprint 1")
        item = adapter.create_item("Work", {})
        cleared = adapter.set_item_iteration(item.id, "")
        assert cleared.id == item.id
        with pytest.raises(MondayError):
            adapter.set_item_iteration(item.id, "sprint-1")
    finally:
        adapter.close()


def test_monday_poll_watermark(monday_env) -> None:
    adapter, _ = _monday()
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
    for key in ("ASANA_PROJECT", "ASANA_API_TOKEN", "MONDAY_BOARD", "MONDAY_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    from pam_pm.core import secrets as secrets_mod

    secrets_mod.save_asana_config(tmp_path, project="saved-project", api_token=ASANA_TOKEN)
    secrets_mod.save_monday_config(tmp_path, board="saved-board", api_token=MONDAY_TOKEN)
    asana = AsanaAdapter(transport=httpx.MockTransport(_FakeAsana().handle), data_dir=tmp_path)
    try:
        assert asana._project_ref == "saved-project"
    finally:
        asana.close()
    asana2 = AsanaAdapter(project="explicit-project", transport=httpx.MockTransport(_FakeAsana().handle), data_dir=tmp_path)
    try:
        assert asana2._project_ref == "explicit-project"
    finally:
        asana2.close()
    monday = MondayAdapter(transport=httpx.MockTransport(_FakeMonday().handle), data_dir=tmp_path)
    try:
        assert monday._board_ref == "saved-board"
    finally:
        monday.close()


def test_secrets_never_in_logs_or_errors(asana_env, monday_env, tmp_path, monkeypatch, caplog) -> None:
    assert ASANA_TOKEN not in asana_redact(f"boom {ASANA_TOKEN} tail", ASANA_TOKEN)
    assert MONDAY_TOKEN not in monday_redact(f"boom {MONDAY_TOKEN} tail", MONDAY_TOKEN)
    with caplog.at_level(logging.INFO):
        asana, _ = _asana()
        try:
            item = asana.create_item("Logged work", {})
            asana.add_comment(item.id, "note")
            asana.poll_changes()
            try:
                asana.get_item("NOPE-999")
            except AsanaError as exc:
                assert ASANA_TOKEN not in str(exc)
        finally:
            asana.close()
        monday, _ = _monday()
        try:
            created = monday.create_item("Logged work", {})
            monday.add_comment(created.id, "note")
            monday.poll_changes()
            try:
                monday.get_item("9999")
            except MondayError as exc:
                assert MONDAY_TOKEN not in str(exc)
        finally:
            monday.close()
    assert ASANA_TOKEN not in caplog.text
    assert MONDAY_TOKEN not in caplog.text


@pytest.fixture()
def wiz_client(tmp_path, monkeypatch, caplog):
    for key in ("ASANA_PROJECT", "ASANA_API_TOKEN", "MONDAY_BOARD", "MONDAY_API_TOKEN"):
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
        "/api/setup/asana",
        json={"token": "wizard-test-setup-token-1", "project": "amwiz", "project_name": ASANA_PROJECT, "api_token": ASANA_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    resp = client.post(
        "/api/setup/monday",
        json={"token": "wizard-test-setup-token-1", "project": "amwiz", "board": MONDAY_BOARD, "api_token": MONDAY_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    assert client.post("/api/test/asana?project=amwiz").json()["ok"] is True
    assert client.post("/api/test/monday?project=amwiz").json()["ok"] is True

    blob = (home / "wizard_progress.json").read_text(encoding="utf-8")
    assert ASANA_TOKEN not in blob
    assert MONDAY_TOKEN not in blob
    steps = json.loads(blob)["amwiz"]["steps"]
    assert steps["asana"]["has_token"] is True
    assert steps["asana"]["project"] == ASANA_PROJECT
    assert steps["monday"]["has_token"] is True
    assert steps["monday"]["board"] == MONDAY_BOARD

    assert stat.S_IMODE((home / "asana_api_token.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((home / "monday_api_token.txt").stat().st_mode) == 0o600

    page = client.get("/setup?project=amwiz").text
    assert "Asana" in page
    assert "Monday" in page
    assert "/static/icons/asana.svg" in page
    assert "/static/icons/monday.svg" in page
    assert "/api/test/asana" in page
    assert "/api/test/monday" in page
    assert "Coming soon" not in page
    assert ASANA_TOKEN not in page
    assert MONDAY_TOKEN not in page
    assert "\u2014" not in page

    assert ASANA_TOKEN not in caplog.text
    assert MONDAY_TOKEN not in caplog.text

    assert client.get("/static/icons/asana.svg").status_code == 200
    assert client.get("/static/icons/monday.svg").status_code == 200
    assert "<svg" in client.get("/static/icons/asana.svg").text


def test_board_read_only_with_capabilities(wiz_client, monkeypatch) -> None:
    client, _, app_mod = wiz_client
    monkeypatch.setenv("ASANA_PROJECT", ASANA_PROJECT)
    monkeypatch.setenv("ASANA_API_TOKEN", ASANA_TOKEN)
    monkeypatch.setenv("MONDAY_BOARD", MONDAY_BOARD)
    monkeypatch.setenv("MONDAY_API_TOKEN", MONDAY_TOKEN)
    asana = AsanaAdapter(transport=httpx.MockTransport(_FakeAsana().handle))
    monday = MondayAdapter(transport=httpx.MockTransport(_FakeMonday().handle))
    try:
        asana.create_item("Asana card", {})
        monday.create_item("Monday card", {})
        app_mod.set_board_adapter("aproj", asana)
        app_mod.set_board_adapter("mproj", monday)
        client.post("/api/setup/backend", json={"token": "wizard-test-setup-token-1", "project": "aproj", "backend": "asana"})
        client.post("/api/setup/backend", json={"token": "wizard-test-setup-token-1", "project": "mproj", "backend": "monday"})
        for project in ("aproj", "mproj"):
            data = client.get(f"/api/board?project={project}").json()
            assert data["editable"] is False
            assert data["capabilities"]["dependency_links"] is True
            assert data["capabilities"]["iterations"] is False
            assert data["capabilities"]["comments"] is True
            assert len(data["items"]) == 1
            denied = client.post(f"/api/board/items?project={project}", json={"title": "Nope"})
            assert denied.status_code == 403
        atext = client.get("/board?project=aproj").text
        assert "Read only" in atext
        assert "Open in Asana" in atext
        assert "https://app.asana.com/0/p1/a1" in atext
        mtext = client.get("/board?project=mproj").text
        assert "Open in Monday" in mtext
        assert "https://monday.com/boards/M1/pulses/1" in mtext
        assert ASANA_TOKEN not in atext
        assert MONDAY_TOKEN not in mtext
    finally:
        asana.close()
        monday.close()
