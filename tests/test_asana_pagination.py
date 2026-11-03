"""Asana pagination fix: workspace param or direct gid fetch.

Fake transport returns the real workspace-required error for
GET /projects without a workspace. The adapter must send a
workspace or fetch the project directly by gid, then parse items.
"""

from __future__ import annotations

import re

import httpx

ASANA_PROJECT = "Website launch"
ASANA_GID = "987654"
ASANA_TOKEN = "asana-fake-token-1"


class _FakeWorkspaceRequired:
    def __init__(self) -> None:
        self.project_list_calls: list[dict[str, str]] = []
        self.direct_fetches: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "").startswith("Bearer ")
        path = request.url.path
        method = request.method
        params = dict(request.url.params)
        if path.endswith("/workspaces") and method == "GET":
            return httpx.Response(
                200, json={"data": [{"gid": "w1", "name": "WS"}]}
            )
        if re.search(r"/projects$", path) and method == "GET":
            self.project_list_calls.append(params)
            if not params.get("workspace"):
                return httpx.Response(
                    400,
                    json={
                        "errors": [
                            {"message": "Need to specify a workspace to paginate!"}
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "gid": "p1",
                            "name": ASANA_PROJECT,
                            "workspace": {"gid": "w1", "name": "WS"},
                        }
                    ]
                },
            )
        match = re.search(r"/projects/([^/]+)$", path)
        if match and method == "GET":
            gid = match.group(1)
            self.direct_fetches.append(gid)
            if gid == ASANA_GID:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "gid": ASANA_GID,
                            "name": ASANA_PROJECT,
                            "workspace": {"gid": "w1", "name": "WS"},
                        }
                    },
                )
            if gid == "p1":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "gid": "p1",
                            "name": ASANA_PROJECT,
                            "workspace": {"gid": "w1", "name": "WS"},
                        }
                    },
                )
            return httpx.Response(
                404, json={"errors": [{"message": "not found"}]}
            )
        if re.search(r"/projects/[^/]+/sections$", path) and method == "GET":
            return httpx.Response(
                200, json={"data": [{"gid": "s1", "name": "To Do"}]}
            )
        if path.endswith("/tasks") and method == "GET":
            assert params.get("project") in ("p1", ASANA_GID)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "gid": "t1",
                            "name": "Do thing",
                            "notes": "details",
                            "memberships": [
                                {
                                    "project": {"gid": params.get("project")},
                                    "section": {"gid": "s1", "name": "To Do"},
                                }
                            ],
                            "tags": [],
                            "due_on": None,
                            "assignee": None,
                            "modified_at": "2026-02-01T00:00:01Z",
                            "permalink_url": "https://app.asana.com/0/x/t1",
                            "projects": [
                                {"gid": params.get("project"), "name": ASANA_PROJECT}
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"errors": [{"message": "unknown"}]})


def test_asana_sends_workspace_and_parses_items(
    tmp_path, monkeypatch
) -> None:
    from pam_pm.adapters.pm.asana import AsanaAdapter

    monkeypatch.setenv("ASANA_PROJECT", ASANA_PROJECT)
    monkeypatch.setenv("ASANA_API_TOKEN", ASANA_TOKEN)
    fake = _FakeWorkspaceRequired()
    adapter = AsanaAdapter(
        transport=httpx.MockTransport(fake.handle), data_dir=tmp_path
    )
    try:
        items = adapter.list_items(limit=10)
    finally:
        adapter.close()
    assert len(items) == 1
    assert items[0].id == "t1"
    assert items[0].title == "Do thing"
    assert items[0].state == "To Do"
    assert fake.project_list_calls, "expected a projects list call"
    assert any(
        call.get("workspace") == "w1" for call in fake.project_list_calls
    ), "adapter must send workspace when paginating projects"


def test_asana_fetches_project_directly_by_gid(tmp_path, monkeypatch) -> None:
    from pam_pm.adapters.pm.asana import AsanaAdapter

    monkeypatch.setenv("ASANA_PROJECT", ASANA_GID)
    monkeypatch.setenv("ASANA_API_TOKEN", ASANA_TOKEN)
    fake = _FakeWorkspaceRequired()
    adapter = AsanaAdapter(
        transport=httpx.MockTransport(fake.handle), data_dir=tmp_path
    )
    try:
        items = adapter.list_items(limit=10)
    finally:
        adapter.close()
    assert len(items) == 1
    assert items[0].id == "t1"
    assert ASANA_GID in fake.direct_fetches
