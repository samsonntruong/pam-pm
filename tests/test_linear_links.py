"""Linear links from forward team edges only, on a fake transport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pam_pm.adapters.pm.linear import LinearAdapter

TEAM = "SAM"
KEY = "lin_api_testkey1"
TEAM_ID = "team-uuid-sam"

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
        self.queries: list[str] = []
        self.issue_get_calls = 0
        self.team_lookup_calls = 0
        self.team_issues_calls = 0
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
        self.queries.append(query)
        if "TeamLookup" in query:
            self.team_lookup_calls += 1
            nodes = [{"id": TEAM_ID, "key": TEAM, "name": TEAM}]
            return httpx.Response(200, json={"data": {"teams": {"nodes": nodes}}})
        if "TeamIssueLinks" in query:
            self.team_issues_calls += 1
            assert variables.get("teamId") == TEAM_ID
            assert "issues(first: 25)" in query
            assert "relations(first: 10)" in query
            assert "inverseRelations" not in query
            nodes: list[dict[str, Any]] = []
            for ident, uuid in self.by_ident.items():
                rels: list[dict[str, Any]] = []
                for blocker, blocked in self.relations:
                    if blocker == uuid and blocked in self.by_uuid:
                        other = self.by_uuid[blocked]
                        rels.append(
                            {
                                "type": "blocks",
                                "relatedIssue": {"id": blocked, "identifier": other},
                            }
                        )
                if self.inject_self_loop and ident == "SAM-27":
                    rels.append(
                        {
                            "type": "blocks",
                            "relatedIssue": {"id": uuid, "identifier": ident},
                        }
                    )
                nodes.append(
                    {
                        "id": uuid,
                        "identifier": ident,
                        "relations": {"nodes": rels},
                    }
                )
            data = {"team": {"issues": {"nodes": nodes}}}
            return httpx.Response(200, json={"data": data})
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


def test_list_links_both_directions_from_forward_only(linear_env) -> None:
    fake = _FakeLinks()
    adapter = _adapter(fake)
    try:
        forward = adapter.list_links("SAM-31")
        assert [(e.source_id, e.target_id, e.rel) for e in forward] == [
            ("SAM-31", "SAM-27", "blocks")
        ]
        back = adapter.list_links("SAM-27")
        assert [(e.source_id, e.target_id, e.rel) for e in back] == [
            ("SAM-31", "SAM-27", "blocks")
        ]
        assert all("inverseRelations" not in q for q in fake.queries)
        assert any("TeamIssueLinks" in q for q in fake.queries)
        assert fake.team_lookup_calls == 1
        assert fake.team_issues_calls == 2
        assert fake.issue_get_calls == 0
    finally:
        adapter.close()


def test_list_links_uuid_resolves_without_fetch(linear_env) -> None:
    fake = _FakeLinks()
    adapter = _adapter(fake)
    try:
        by_uuid = adapter.list_links(UUID_27)
        assert [(e.source_id, e.target_id, e.rel) for e in by_uuid] == [
            ("SAM-31", "SAM-27", "blocks")
        ]
        assert fake.issue_get_calls == 0
        assert fake.team_lookup_calls == 1
        assert fake.team_issues_calls == 1
    finally:
        adapter.close()


def test_list_links_drops_self_loop(linear_env) -> None:
    fake = _FakeLinks()
    fake.relations = []
    fake.inject_self_loop = True
    adapter = _adapter(fake)
    try:
        assert adapter.list_links("SAM-27") == []
        assert adapter.list_links(UUID_27) == []
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
