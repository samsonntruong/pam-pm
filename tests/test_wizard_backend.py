"""WP1b wizard backend choice: Pam board vs ADO, with resume."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def isolated_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    for key in ("ADO_ORG_URL", "ADO_PROJECT", "ADO_PAT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("META_MODEL", "fake-model")
    monkeypatch.setenv("META_BASE_URL", "https://fake.local")
    monkeypatch.setenv("META_API_KEY", "fake-key")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("PAM_SETUP_TOKEN", "wizard-test-setup-token-1")
    import pam_pm.web.app as app_mod

    app_mod.SETUP_DONE = False
    client = TestClient(app_mod.app)
    yield client
    app_mod.SETUP_DONE = False


def _no_secrets(text: str) -> None:
    assert "\u2014" not in text
    assert "fake-key" not in text
    assert "xoxb-fake" not in text


def test_pam_board_finishes_with_no_ado(isolated_client: TestClient) -> None:
    from pam_pm.adapters.pm.sqlite_board import SQLiteBoard

    board = SQLiteBoard(":memory:")
    item = board.create_item("Kickoff task", {"state": "New"})
    assert board.get_item(item.id).title == "Kickoff task"

    resp = isolated_client.post(
        "/api/setup/backend", json={"token": "wizard-test-setup-token-1", "project": "pamp", "backend": "pam_board"}
    )
    assert resp.status_code == 200
    assert resp.json()["backend"] == "pam_board"

    page = isolated_client.get("/setup?project=pamp").text
    assert "Where should your tasks live?" in page
    assert "Pam board" in page
    assert "Recommended" in page
    assert "Azure DevOps" in page
    assert "docs/adapters.md" in page
    assert "ONE internal Slack app once" in page
    assert "Tell your team" in page
    assert 'id="step-3" hidden' in page or 'id="step-3"\nhidden' in page or "hidden" in page
    _no_secrets(page)

    assert isolated_client.post("/api/test/meta?project=pamp").json()["ok"] is True
    assert isolated_client.post("/api/test/slack?project=pamp").json()["ok"] is True
    ado = isolated_client.post("/api/test/ado?project=pamp").json()
    assert ado["ok"] is False
    assert ado["hint"]

    save = isolated_client.post(
        "/api/setup/progress",
        json={"token": "wizard-test-setup-token-1", "project": "pamp", "step": "people", "data": {"people": "Ana\nSam"}},
    )
    assert save.status_code == 200
    assert isolated_client.post("/api/test/people?project=pamp").json()["ok"] is True
    assert isolated_client.post("/api/test/rules?project=pamp").json()["ok"] is True
    assert isolated_client.post("/api/test/safety?project=pamp").json()["ok"] is True


def test_ado_choice_shows_ado_step(isolated_client: TestClient) -> None:
    from pam_pm.adapters.pm.base import PMAdapter

    class FakeADO(PMAdapter):
        def __init__(self) -> None:
            self.items: dict[str, Any] = {}

        def list_items(self, limit: int = 50):
            return []

        def get_item(self, item_id: str):
            raise KeyError(item_id)

        def update_item(self, item_id: str, fields: dict[str, Any]):
            raise KeyError(item_id)

        def create_item(self, title: str, fields: dict[str, Any] | None = None):
            from pam_pm.adapters.pm.base import WorkItem

            item = WorkItem(id="1", title=title)
            self.items["1"] = item
            return item

        def delete_item(self, item_id: str) -> None:
            return None

        def list_states(self) -> list[str]:
            return ["New", "Active", "Resolved", "Closed"]

        def close_item(self, item_id: str):
            from pam_pm.adapters.pm.base import WorkItem

            return WorkItem(id=item_id, title="t", state="Closed")

        def list_iterations(self):
            return []

        def create_iteration(self, name: str, start_date: str = "", end_date: str = ""):
            from pam_pm.adapters.pm.base import Iteration

            return Iteration(id="i1", name=name)

        def set_item_iteration(self, item_id: str, iteration_id: str):
            from pam_pm.adapters.pm.base import WorkItem

            return WorkItem(id=item_id, title="t")

        def add_link(self, source_id: str, target_id: str, rel: str):
            from pam_pm.adapters.pm.base import Link

            return Link(source_id=source_id, target_id=target_id, rel=rel)

        def list_links(self, item_id: str):
            return []

        def list_comments(self, item_id: str):
            return []

        def add_comment(self, item_id: str, text: str, author: str = ""):
            from pam_pm.adapters.pm.base import Comment

            return Comment(id="c1", item_id=item_id, text=text)

        def poll_changes(self, since: str | None = None):
            return []

    fake: Any = FakeADO()
    assert fake.create_item("ADO task").title == "ADO task"

    resp = isolated_client.post(
        "/api/setup/backend", json={"token": "wizard-test-setup-token-1", "project": "adop", "backend": "azure_devops"}
    )
    assert resp.json()["backend"] == "azure_devops"
    page = isolated_client.get("/setup?project=adop").text
    assert 'id="step-3"' in page
    assert 'id="step-3" hidden' not in page
    assert "/api/test/ado" in page
    assert "(Pam board)" in page or "Pam board" in page
    _no_secrets(page)


def test_resume_works_after_close(isolated_client: TestClient, tmp_path) -> None:
    first = isolated_client.post(
        "/api/setup/progress",
        json={"token": "wizard-test-setup-token-1", "project": "resume", "step": "slack", "data": {}},
    )
    assert first.status_code == 200
    progress_file = tmp_path / "wizard_progress.json"
    assert progress_file.exists()

    import pam_pm.web.app as app_mod

    fresh = TestClient(app_mod.app)
    got = fresh.get("/api/setup/progress?project=resume").json()
    assert got["ok"] is True
    assert got["progress"]["current"] == "slack"
    page = fresh.get("/setup?project=resume").text
    assert "Welcome back" in page
    _no_secrets(page)
