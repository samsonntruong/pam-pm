"""Shared PMAdapter behavior suite on NativeBoard and AzureDevOps.

Same tests run against the native SQLite board and against ADO backed
by an in-memory fake transport, once per process template (Agile and
Scrum) to pin the M2b state map.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx
import pytest

from pam_pm.adapters.pm.azure_devops import ADOAdapter, ADOError
from pam_pm.adapters.pm.base import PMAdapter, is_closed
from pam_pm.adapters.pm.sqlite_board import NativeBoard, SQLiteBoard


def _val(fields: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in fields:
            return fields[key]
    return None


class _FakeADO:
    """In-memory ADO REST surface used by the shared suite."""

    def __init__(self, process: str) -> None:
        self._process = process
        self._items: dict[int, dict[str, Any]] = {}
        self._relations: dict[int, list[dict[str, str]]] = {}
        self._comments: dict[int, list[dict[str, Any]]] = {}
        self._nodes: dict[str, dict[str, Any]] = {}
        self._team_iterations: list[dict[str, Any]] = []
        self._next_id = 1
        self._next_comment = 1
        self._next_iter = 1
        self._clock = 1

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-02-01T00:00:00.{self._clock:06d}Z"

    def _item_payload(self, item_id: int) -> dict[str, Any]:
        return {
            "id": item_id,
            "fields": dict(self._items[item_id]),
            "relations": list(self._relations.get(item_id, [])),
        }

    def _apply_ops(self, item_id: int, ops: Any) -> None:
        fields = self._items[item_id]
        for op in ops if isinstance(ops, list) else []:
            if not isinstance(op, dict):
                continue
            path = str(op.get("path", ""))
            if path == "/relations/-":
                value = op.get("value", {})
                if isinstance(value, dict):
                    self._relations.setdefault(item_id, []).append(
                        {
                            "rel": str(value.get("rel", "")),
                            "url": str(value.get("url", "")),
                        }
                    )
            elif path.startswith("/fields/"):
                fields[path[len("/fields/"):]] = op.get("value")
        fields["System.ChangedDate"] = self._stamp()

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else None
        except ValueError:
            body = None
        if path.endswith("/_apis/wit/wiql") and method == "POST":
            return self._wiql(body)
        if path.endswith("/comments") and "/_apis/wit/workitems/" in path:
            return self._comments_route(path, method, body)
        if "/_apis/wit/workitems" in path and method == "POST" and "/$" in path:
            return self._create(body)
        match = re.search(r"/_apis/wit/workitems/([^/]+)$", path)
        if match and match.group(1) != "workitems":
            return self._item_route(match.group(1), method, body)
        if path.endswith("/_apis/wit/workitems") and method == "GET":
            return self._batch(request)
        if "classificationnodes/iterations" in path and method == "POST":
            return self._create_node(body)
        if "teamsettings/iterations" in path:
            return self._team_iterations_route(method, body)
        if "/_apis/projects/" in path and method == "GET":
            return httpx.Response(
                200,
                json={
                    "capabilities": {
                        "processTemplate": {"templateName": self._process}
                    }
                },
            )
        if "workitemtypes/Task/states" in path and method == "GET":
            names = (
                ["New", "Active", "Closed"]
                if self._process == "Agile"
                else ["To Do", "In Progress", "Done"]
            )
            return httpx.Response(
                200, json={"value": [{"name": name} for name in names]}
            )
        return httpx.Response(404, json={"message": "unknown fake route"})

    def _wiql(self, body: Any) -> httpx.Response:
        query = str(body.get("query", "")) if isinstance(body, dict) else ""
        ids = sorted(
            self._items,
            key=lambda i: self._items[i].get("System.ChangedDate", ""),
        )
        tick = re.search(r">\s*'([^']*)'", query)
        if tick:
            ids = [
                i
                for i in ids
                if str(self._items[i].get("System.ChangedDate", "")) > tick.group(1)
            ]
        elif "DESC" in query:
            ids = list(reversed(ids))
        return httpx.Response(200, json={"workItems": [{"id": i} for i in ids]})

    def _create(self, body: Any) -> httpx.Response:
        item_id = self._next_id
        self._next_id += 1
        self._items[item_id] = {
            "System.Title": "",
            "System.State": "",
            "System.ChangedDate": self._stamp(),
        }
        self._relations[item_id] = []
        self._apply_ops(item_id, body)
        return httpx.Response(200, json=self._item_payload(item_id))

    def _batch(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.params.get("ids", "")
        out = [
            self._item_payload(int(part))
            for part in raw.split(",")
            if part.strip().isdigit() and int(part) in self._items
        ]
        return httpx.Response(200, json={"value": out})

    def _item_route(
        self, item_id: str, method: str, body: Any
    ) -> httpx.Response:
        if not item_id.isdigit() or int(item_id) not in self._items:
            return httpx.Response(404, json={"message": "not found"})
        num = int(item_id)
        if method == "GET":
            return httpx.Response(200, json=self._item_payload(num))
        if method == "PATCH":
            self._apply_ops(num, body)
            return httpx.Response(200, json=self._item_payload(num))
        if method == "DELETE":
            del self._items[num]
            self._relations.pop(num, None)
            self._comments.pop(num, None)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "unknown fake route"})

    def _comments_route(
        self, path: str, method: str, body: Any
    ) -> httpx.Response:
        match = re.search(r"/workitems/([^/]+)/comments$", path)
        item_id = match.group(1) if match else ""
        if not item_id.isdigit() or int(item_id) not in self._items:
            return httpx.Response(404, json={"message": "not found"})
        num = int(item_id)
        if method == "GET":
            return httpx.Response(
                200, json={"value": list(self._comments.get(num, []))}
            )
        text = str(body.get("text", "")) if isinstance(body, dict) else ""
        comment = {
            "id": self._next_comment,
            "text": text,
            "createdBy": {"displayName": "ADO User"},
            "createdDate": self._stamp(),
        }
        self._next_comment += 1
        self._comments.setdefault(num, []).append(comment)
        return httpx.Response(200, json=comment)

    def _create_node(self, body: Any) -> httpx.Response:
        guid = f"guid-{self._next_iter}"
        self._next_iter += 1
        name = str(body.get("name", "")) if isinstance(body, dict) else ""
        attrs = body.get("attributes", {}) if isinstance(body, dict) else {}
        self._nodes[guid] = {"name": name, "attrs": dict(attrs)}
        return httpx.Response(
            200, json={"identifier": guid, "id": guid, "name": name}
        )

    def _team_iterations_route(self, method: str, body: Any) -> httpx.Response:
        if method == "GET":
            return httpx.Response(200, json={"value": self._team_iterations})
        guid = str(body.get("id", "")) if isinstance(body, dict) else ""
        node = self._nodes.get(guid, {"name": "", "attrs": {}})
        entry = {
            "id": guid,
            "name": node["name"],
            "path": f"proj\\{node['name']}",
            "attributes": {
                "startDate": node["attrs"].get("startDate", ""),
                "finishDate": node["attrs"].get("finishDate", ""),
            },
        }
        self._team_iterations.append(entry)
        return httpx.Response(200, json=entry)


def _make_board(kind: str) -> tuple[PMAdapter, str, list[str]]:
    if kind == "native":
        return NativeBoard(), "Done", ["New", "Active", "Blocked", "Done"]
    process = "Agile" if kind == "ado-agile" else "Scrum"
    os.environ["ADO_ORG_URL"] = "https://dev.azure.com/fake"
    os.environ["ADO_PROJECT"] = "proj"
    os.environ["ADO_TEAM"] = "team"
    os.environ["ADO_PAT"] = "test-pat-value"
    fake = _FakeADO(process)
    adapter = ADOAdapter(
        transport=httpx.MockTransport(fake.handle), process=process
    )
    if process == "Agile":
        return adapter, "Closed", ["New", "Active", "Closed"]
    return adapter, "Done", ["To Do", "In Progress", "Done"]


@pytest.fixture(
    params=["native", "ado-agile", "ado-scrum"],
    ids=["native", "ado-agile", "ado-scrum"],
)
def board_case(request: Any) -> Any:
    board, done, states = _make_board(str(request.param))
    try:
        yield (board, done, states)
    finally:
        board.close()


def test_native_alias_still_works() -> None:
    assert SQLiteBoard is NativeBoard
    board = SQLiteBoard()
    try:
        item = board.create_item("Alias check", {"state": "New"})
        assert board.get_item(item.id).title == "Alias check"
    finally:
        board.close()


def test_capability_flags(board_case: Any) -> None:
    board, _, _ = board_case
    assert board.supports_dependency_links is True
    assert board.supports_iterations is True
    assert board.supports_comments is True


def test_list_states(board_case: Any) -> None:
    board, _, states = board_case
    assert board.list_states() == states


def test_create_get_update(board_case: Any) -> None:
    board, _, _ = board_case
    created = board.create_item(
        "Ship the thing",
        {
            "description": "details here",
            "tags": "sprint-1",
            "due_date": "2026-03-01",
            "owner": "sam",
            "estimate": 8,
            "remaining": 5,
        },
    )
    assert created.id
    assert created.title == "Ship the thing"
    assert created.changed
    fetched = board.get_item(created.id)
    assert fetched.title == "Ship the thing"
    assert _val(fetched.fields, "description", "System.Description") == "details here"
    assert _val(fetched.fields, "tags", "System.Tags") == "sprint-1"
    assert (
        _val(
            fetched.fields,
            "due_date",
            "Microsoft.VSTS.Scheduling.DueDate",
        )
        == "2026-03-01"
    )
    assert (
        _val(fetched.fields, "owner", "assigned_to", "System.AssignedTo")
        == "sam"
    )
    assert (
        _val(
            fetched.fields,
            "estimate",
            "Microsoft.VSTS.Scheduling.OriginalEstimate",
        )
        == 8
    )
    assert (
        _val(
            fetched.fields,
            "remaining",
            "Microsoft.VSTS.Scheduling.RemainingWork",
        )
        == 5
    )
    updated = board.update_item(
        created.id, {"title": "Ship it", "remaining": 2}
    )
    assert updated.title == "Ship it"
    assert (
        _val(
            updated.fields,
            "remaining",
            "Microsoft.VSTS.Scheduling.RemainingWork",
        )
        == 2
    )
    assert len(board.list_items()) == 1


def test_close_item(board_case: Any) -> None:
    board, done, _ = board_case
    created = board.create_item("Close me", {"state": "Active"})
    assert not is_closed(created.state)
    closed = board.close_item(created.id)
    assert closed.state == done
    assert is_closed(closed.state)
    assert is_closed(board.get_item(created.id).state)


def test_links(board_case: Any) -> None:
    board, _, _ = board_case
    first = board.create_item("First", {"state": "New"})
    second = board.create_item("Second", {"state": "New"})
    link = board.add_link(
        first.id, second.id, "System.LinkTypes.Dependency-Forward"
    )
    assert link.source_id == first.id
    assert link.target_id == second.id
    assert link.rel.endswith("Dependency-Forward")
    short = board.add_link(first.id, second.id, "Dependency-Reverse")
    assert short.rel.endswith("Dependency-Reverse")
    rels = [entry.rel for entry in board.list_links(first.id)]
    assert any(rel.endswith("Dependency-Forward") for rel in rels)
    assert any(rel.endswith("Dependency-Reverse") for rel in rels)
    with pytest.raises((ValueError, ADOError)):
        board.add_link(first.id, second.id, "Not-A-Rel")


def test_iterations(board_case: Any) -> None:
    board, _, _ = board_case
    sprint = board.create_iteration(
        "Sprint 1", start_date="2026-01-05", end_date="2026-01-18"
    )
    assert sprint.id
    assert sprint.name == "Sprint 1"
    assert sprint.start_date == "2026-01-05"
    assert sprint.end_date == "2026-01-18"
    names = [entry.name for entry in board.list_iterations()]
    assert "Sprint 1" in names
    item = board.create_item("Sprint work", {"state": "New"})
    moved = board.set_item_iteration(item.id, sprint.id)
    assert moved.fields.get("iteration_id") == sprint.id


def test_comments(board_case: Any) -> None:
    board, _, _ = board_case
    item = board.create_item("Discuss", {"state": "New"})
    assert board.list_comments(item.id) == []
    comment = board.add_comment(item.id, "Looks good", author="sam")
    assert comment.id
    assert comment.item_id == item.id
    assert comment.text == "Looks good"
    assert comment.author
    listed = board.list_comments(item.id)
    assert [entry.text for entry in listed] == ["Looks good"]
    with pytest.raises((ValueError, ADOError)):
        board.add_comment(item.id, "  ")


def test_poll_changes(board_case: Any) -> None:
    board, _, _ = board_case
    first = board.create_item("One", {"state": "New"})
    time.sleep(0.01)
    second = board.create_item("Two", {"state": "New"})
    assert [item.id for item in board.poll_changes()] == [first.id, second.id]
    assert [item.id for item in board.poll_changes(first.changed)] == [second.id]
    time.sleep(0.01)
    board.update_item(first.id, {"remaining": 1})
    assert [item.id for item in board.poll_changes(second.changed)] == [first.id]


def test_delete_item(board_case: Any) -> None:
    board, _, _ = board_case
    item = board.create_item("Gone", {"state": "New"})
    board.delete_item(item.id)
    assert board.list_items() == []
    with pytest.raises((ValueError, ADOError)):
        board.get_item(item.id)


def test_unknown_item_raises(board_case: Any) -> None:
    board, _, _ = board_case
    with pytest.raises((ValueError, ADOError)):
        board.get_item("no-such-item")
