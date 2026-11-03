"""Monday close group plus dependency link path on fake transports."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pam_pm.adapters.pm.monday import MondayAdapter, MondayError

BOARD = "pam test"
TOKEN = "monday-test-token-m3"


class _FakeMonday:
    def __init__(
        self,
        groups: list[dict[str, str]],
        columns: list[dict[str, str]],
    ) -> None:
        self.groups = groups
        self.columns = columns
        self.items: dict[str, dict[str, Any]] = {
            "1": {"id": "1", "name": "one", "group": dict(groups[0]), "deps": []},
            "2": {"id": "2", "name": "two", "group": dict(groups[0]), "deps": []},
        }
        self.last_values: dict[str, Any] = {}

    def _payload(self, item_id: str) -> dict[str, Any]:
        entry = self.items[item_id]
        values = []
        for col in self.columns:
            if col["type"] in ("dependency", "board_relation", "link"):
                raw = json.dumps({"linked_item_ids": entry["deps"]})
            else:
                raw = json.dumps("")
            values.append(
                {
                    "id": col["id"],
                    "text": "",
                    "value": raw,
                    "column": {
                        "id": col["id"],
                        "title": col["title"],
                        "type": col["type"],
                    },
                }
            )
        return {
            "id": item_id,
            "name": entry["name"],
            "group": entry["group"],
            "column_values": values,
            "updated_at": "2026-01-01T00:00:00Z",
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        try:
            body = (
                json.loads(request.content.decode("utf-8")) if request.content else {}
            )
        except ValueError:
            body = {}
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
                                "id": "B1",
                                "name": BOARD,
                                "groups": self.groups,
                                "columns": self.columns,
                            }
                        ]
                    }
                },
            )
        if "BoardGroups" in query:
            return httpx.Response(
                200, json={"data": {"boards": [{"groups": self.groups}]}}
            )
        if "BoardColumns" in query:
            return httpx.Response(
                200, json={"data": {"boards": [{"columns": self.columns}]}}
            )
        if "ItemGet" in query:
            item_id = str(variables.get("itemId", ""))
            if item_id in self.items:
                return httpx.Response(
                    200, json={"data": {"items": [self._payload(item_id)]}}
                )
            return httpx.Response(200, json={"data": {"items": []}})
        if "ItemMove" in query:
            item_id = str(variables.get("itemId", ""))
            group_id = str(variables.get("groupId", ""))
            for group in self.groups:
                if group["id"] == group_id:
                    self.items[item_id]["group"] = dict(group)
            return httpx.Response(
                200, json={"data": {"move_item_to_group": {"id": item_id}}}
            )
        if "ItemColumns" in query:
            raw = variables.get("values", {})
            values = json.loads(raw) if isinstance(raw, str) else dict(raw)
            known = {c["id"] for c in self.columns}
            for key in values:
                if key not in known:
                    return httpx.Response(
                        200,
                        json={
                            "errors": [
                                {
                                    "message": "This column ID doesn't exist for the board"
                                }
                            ]
                        },
                    )
            self.last_values = values
            item_id = str(variables.get("itemId", ""))
            for key, val in values.items():
                if isinstance(val, dict) and isinstance(
                    val.get("linked_item_ids"), list
                ):
                    self.items[item_id]["deps"] = [
                        str(v) for v in val["linked_item_ids"]
                    ]
            return httpx.Response(
                200, json={"data": {"change_multiple_column_values": {"id": item_id}}}
            )
        return httpx.Response(400, json={"errors": [{"message": "unknown fake query"}]})


def _adapter(fake: _FakeMonday, monkeypatch: pytest.MonkeyPatch) -> MondayAdapter:
    monkeypatch.setenv("MONDAY_BOARD", BOARD)
    monkeypatch.setenv("MONDAY_API_TOKEN", TOKEN)
    return MondayAdapter(transport=httpx.MockTransport(fake.handle))


def test_close_moves_to_completed_when_no_done(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeMonday(
        groups=[{"id": "g1", "title": "To-Do"}, {"id": "g2", "title": "Completed"}],
        columns=[
            {"id": "name", "title": "Name", "type": "name"},
            {"id": "owner", "title": "Owner", "type": "people"},
        ],
    )
    adapter = _adapter(fake, monkeypatch)
    try:
        closed = adapter.close_item("1")
        assert closed.state == "Completed"
        assert fake.items["1"]["group"]["id"] == "g2"
    finally:
        adapter.close()


def test_add_link_uses_discovered_dependency_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeMonday(
        groups=[{"id": "g1", "title": "To-Do"}, {"id": "g2", "title": "Completed"}],
        columns=[
            {"id": "name", "title": "Name", "type": "name"},
            {"id": "dep_abc", "title": "Blocked by", "type": "dependency"},
        ],
    )
    adapter = _adapter(fake, monkeypatch)
    try:
        link = adapter.add_link("1", "2", "is blocked by")
        assert link.source_id == "1"
        assert "dep_abc" in fake.last_values
        assert "dependency" not in fake.last_values
    finally:
        adapter.close()


def test_add_link_missing_column_raises_helpful_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeMonday(
        groups=[{"id": "g1", "title": "To-Do"}, {"id": "g2", "title": "Completed"}],
        columns=[
            {"id": "name", "title": "Name", "type": "name"},
            {"id": "owner", "title": "Owner", "type": "people"},
            {"id": "status", "title": "Status", "type": "status"},
            {"id": "due", "title": "Due date", "type": "date"},
        ],
    )
    adapter = _adapter(fake, monkeypatch)
    try:
        with pytest.raises(
            MondayError, match="add a Dependency or Link column to the board"
        ):
            adapter.add_link("1", "2", "is blocked by")
    finally:
        adapter.close()
