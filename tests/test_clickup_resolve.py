"""ClickUp list resolve/create on a fake transport.

Covers find-existing by case-insensitive name, missing triggers
create-space-then-list in order, and permission errors become a
plain ClickUpError naming the ClickUp setting.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

CLICKUP_TEAM = "Acme"
CLICKUP_TOKEN = "pk_fake_clickup_token_1"


class _FakeResolve:
    def __init__(
        self,
        lists: list[dict[str, Any]] | None = None,
        permission_fail: str = "",
    ) -> None:
        self._lists = lists if lists is not None else []
        self._permission_fail = permission_fail
        self.calls: list[tuple[str, str]] = []

    def _denied(self) -> httpx.Response:
        return httpx.Response(403, json={"err": "Permission denied"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "")
        path = request.url.path
        method = request.method
        self.calls.append((method, path))
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
        except ValueError:
            body = {}
        if self._permission_fail == "team" and path.endswith("/team"):
            return self._denied()
        if path.endswith("/team") and method == "GET":
            return httpx.Response(
                200, json={"teams": [{"id": "T1", "name": CLICKUP_TEAM}]}
            )
        match = re.search(r"/team/([^/]+)/space$", path)
        if match and method == "GET":
            if self._permission_fail == "space":
                return self._denied()
            return httpx.Response(
                200, json={"spaces": [{"id": "S1", "name": "Eng"}]}
            )
        if match and method == "POST":
            if self._permission_fail == "create":
                return self._denied()
            name = str(body.get("name", "") or "")
            return httpx.Response(200, json={"id": "S2", "name": name})
        if re.search(r"/space/[^/]+/list$", path) and method == "GET":
            return httpx.Response(200, json={"lists": list(self._lists)})
        if re.search(r"/space/[^/]+/folder$", path) and method == "GET":
            return httpx.Response(200, json={"folders": []})
        match = re.search(r"/space/([^/]+)/list$", path)
        if match and method == "POST":
            if self._permission_fail == "create":
                return self._denied()
            name = str(body.get("name", "") or "")
            return httpx.Response(200, json={"id": "L2", "name": name})
        return httpx.Response(404, json={"err": "unknown"})


def _env(monkeypatch, list_val: str = "") -> None:
    monkeypatch.setenv("CLICKUP_TEAM", CLICKUP_TEAM)
    monkeypatch.setenv("CLICKUP_LIST", list_val)
    monkeypatch.delenv("CLICKUP_LIST_ID", raising=False)
    monkeypatch.setenv("CLICKUP_API_TOKEN", CLICKUP_TOKEN)


def test_find_existing_returns_id(tmp_path, monkeypatch) -> None:
    from pam_pm.adapters.pm.clickup import ClickUpAdapter

    _env(monkeypatch)
    fake = _FakeResolve(
        lists=[{"id": "L1", "name": "Website"}],
    )
    adapter = ClickUpAdapter(
        transport=httpx.MockTransport(fake.handle), data_dir=tmp_path
    )
    try:
        found = adapter.resolve_list("website")
    finally:
        adapter.close()
    assert found == "L1"
    assert all(method != "POST" for method, _ in fake.calls)


def test_missing_triggers_create_space_then_list(tmp_path, monkeypatch) -> None:
    from pam_pm.adapters.pm.clickup import ClickUpAdapter, resolve_list

    _env(monkeypatch)
    fake = _FakeResolve(lists=[])
    adapter = ClickUpAdapter(
        transport=httpx.MockTransport(fake.handle), data_dir=tmp_path
    )
    try:
        created = adapter.resolve_list("New Project")
    finally:
        adapter.close()
    assert created == "L2"
    posts = [path for method, path in fake.calls if method == "POST"]
    assert posts == ["/api/v2/team/T1/space", "/api/v2/space/S2/list"]

    fake2 = _FakeResolve(lists=[])
    got = resolve_list(
        "New Project",
        team=CLICKUP_TEAM,
        transport=httpx.MockTransport(fake2.handle),
        data_dir=tmp_path,
    )
    assert got == "L2"
    posts2 = [path for method, path in fake2.calls if method == "POST"]
    assert posts2 == ["/api/v2/team/T1/space", "/api/v2/space/S2/list"]


def test_permission_errors_become_plain(tmp_path, monkeypatch) -> None:
    from pam_pm.adapters.pm.clickup import ClickUpAdapter, ClickUpError

    _env(monkeypatch)
    for fail in ("team", "space", "create"):
        fake = _FakeResolve(lists=[], permission_fail=fail)
        adapter = ClickUpAdapter(
            transport=httpx.MockTransport(fake.handle), data_dir=tmp_path
        )
        try:
            with pytest.raises(ClickUpError) as excinfo:
                adapter.resolve_list("New Project")
        finally:
            adapter.close()
        msg = str(excinfo.value)
        assert "Settings" in msg
        assert "ClickUp" in msg
        assert CLICKUP_TOKEN not in msg
        assert "Traceback" not in msg


def test_dry_run_finds_without_creating(tmp_path, monkeypatch) -> None:
    from pam_pm.adapters.pm.clickup import ClickUpAdapter

    _env(monkeypatch)
    fake = _FakeResolve(lists=[{"id": "L9", "name": "Demo"}])
    adapter = ClickUpAdapter(
        transport=httpx.MockTransport(fake.handle), data_dir=tmp_path
    )
    try:
        found = adapter.resolve_list("DEMO", dry_run=True)
        assert found == "L9"
        missing = adapter.resolve_list("Absent", dry_run=True)
        assert missing == ""
    finally:
        adapter.close()
    assert all(method != "POST" for method, _ in fake.calls)
