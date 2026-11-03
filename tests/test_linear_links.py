"""Linear links by UUID with no self-loops, on a fake transport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pam_pm.adapters.pm.linear import LinearAdapter

TEAM = "SAM"
KEY = "lin_api_testkey1"

UUID_27 = "11111111-1111-1111-1111-111111111111"
UUID_31 = "22222222-2222-2222-2222-222222222222"


class _FakeLinks:
    def __init__(self) -> None:
        self.by_ident = {
            "SAM-27": UUID_27,
            "SAM-31": UUID_31,
        }
        self.by_uuid = {v: k for k, v in self.by_ident.items()}
        self.relations: list[tuple[str, str]] = [(UUID_31, UUID_27)]
        self.sent_inputs: list[dict[str, Any]] = []
        self.sent_ids: list[str] = []
        self.issue_get_calls = 0
        self.inject_self_loop = False

    def _node(self, ident: str) -> dict[str, Any]:
        return {
            "id": self.by_ident[ident],
            "identifier": ident,
            "title": ident,
            "description": "",
            "state": {"id": "S1", "name": "Todo", "type": "unstarted"},
            "labels": {"nodes": []},
            "dueDate": "",
            "assignee": None,
            "estimate": None,
            "priority": None,
            "updatedAt": "2026-02-01T00:00:00.000000Z",
            "cycle": None,
            "url": f"https://linear.app/fake/issue/{ident}",
        }

    def _resolve(self, key: str) -> str:
        if key in self.by_ident:
            return key
        return self.by_uuid.get(key, "")

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        query = str(body.get("query", ""))
        variables = body.get("variables", {})
        if "IssueGet" in query:
            self.issue_get_calls += 1
            ident = self._resolve(str(variables.get("id", "")))
            node = self._node(ident) if ident else None
            return httpx.Response(200, json={"data": {"issue": node}})
        if "RelationCreate" in query:
            payload = dict(variables.get("input", {}))
            self.sent_inputs.append(payload)
            self.relations.append(
                (str(payload.get("issueId", "")), str(payload.get("relatedIssueId", "")))
            )
            inner = {"success": True, "issueRelation": {"id": "R1"}}
            return httpx.Response(200, json={"data": {"issueRelationCreate": inner}})
        if "IssueRelations" in query:
            raw = str(variables.get("id", ""))
            self.sent_ids.append(raw)
            ident = self._resolve(raw)
            uuid = self.by_ident.get(ident, raw)
            rels = []
            inverse = []
            for blocker, blocked in self.relations:
                if blocker == uuid and blocked in self.by_uuid:
                    other = self.by_uuid[blocked]
                    rels.append(
                        {
                            "type": "blocks",
                            "relatedIssue": {"id": blocked, "identifier": other},
                        }
                    )
                if blocked == uuid and blocker in self.by_uuid:
                    other = self.by_uuid[blocker]
                    inverse.append(
                        {
                            "type": "blocks",
                            "relatedIssue": {"id": blocker, "identifier": other},
                        }
                    )
            if self.inject_self_loop and ident:
                inverse.append(
                    {
                        "type": "blocks",
                        "relatedIssue": {"id": uuid, "identifier": ident},
                    }
                )
            data = {
                "issue": {
                    "relations": {"nodes": rels},
                    "inverseRelations": {"nodes": inverse},
                }
            }
            return httpx.Response(200, json={"data": data})
        return httpx.Response(200, json={"errors": [{"message": "unknown op"}]})


@pytest.fixture()
def linear_env(monkeypatch):
    monkeypatch.setenv("LINEAR_TEAM", TEAM)
    monkeypatch.setenv("LINEAR_API_KEY", KEY)


def _adapter(fake: _FakeLinks) -> LinearAdapter:
    return LinearAdapter(transport=httpx.MockTransport(fake.handle))


def test_add_link_sends_uuids(linear_env) -> None:
    fake = _FakeLinks()
    fake.relations = []
    adapter = _adapter(fake)
    try:
        link = adapter.add_link("SAM-27", "SAM-31", "blocks")
        assert (link.source_id, link.target_id) == ("SAM-27", "SAM-31")
        assert fake.sent_inputs
        sent = fake.sent_inputs[-1]
        assert sent["issueId"] == UUID_27
        assert sent["relatedIssueId"] == UUID_31
    finally:
        adapter.close()


def test_list_links_both_directions(linear_env) -> None:
    fake = _FakeLinks()
    adapter = _adapter(fake)
    try:
        forward = adapter.list_links("SAM-31")
        assert [(e.target_id, e.rel) for e in forward] == [("SAM-27", "blocks")]
        back = adapter.list_links("SAM-27")
        assert [(e.target_id, e.rel) for e in back] == [("SAM-31", "is blocked by")]
        assert fake.sent_ids
        assert all(v in (UUID_27, UUID_31) for v in fake.sent_ids)
    finally:
        adapter.close()


def test_list_links_drops_self_loop(linear_env) -> None:
    fake = _FakeLinks()
    fake.relations = []
    fake.inject_self_loop = True
    adapter = _adapter(fake)
    try:
        assert adapter.list_links("SAM-27") == []
    finally:
        adapter.close()


def test_uuid_input_skips_fetch(linear_env) -> None:
    fake = _FakeLinks()
    adapter = _adapter(fake)
    try:
        assert adapter._uuid_for(UUID_27) == UUID_27
        assert fake.issue_get_calls == 0
        assert adapter._uuid_for("SAM-27") == UUID_27
        assert fake.issue_get_calls == 1
    finally:
        adapter.close()
