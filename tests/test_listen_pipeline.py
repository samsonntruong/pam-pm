"""Listen auto pipeline: finish to proposals, edit, approve, discard."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from pam_pm.core import memory as memory_mod
from pam_pm.core import store as store_mod

TWO_ACTIONS = (
    "Avery will draft the rollout checklist by Friday. "
    "Blake will confirm the staging window tomorrow."
)
CHECKOUT_LINE = "the team will rebuild the checkout API before launch."


class _FakeBoard:
    supports_dependency_links = False

    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self._existing = list(existing or [])
        self._next = 1

    def list_items(self, limit: int = 500) -> list[dict[str, Any]]:
        return self._existing[:limit]

    def list_states(self) -> list[str]:
        return ["New", "Active", "Done"]

    def create_item(
        self, title: str, fields: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        item = {"id": str(self._next), "title": title, **(fields or {})}
        self._next += 1
        self.created.append(item)
        return item


def _setup(monkeypatch: Any, tmp_path: Any) -> Any:
    import pam_pm.web.app as app_mod
    from pam_pm.core.config import Config

    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("PAM_DRY_RUN", "1")
    db = Config().db_path()
    store_mod.set_project_backend(db, "demo", "pam_board")
    return app_mod, db


def _seed(db: Any, session_id: str, *lines: str) -> None:
    store_mod.create_listen_session(db, session_id, "demo", "Standup")
    for line in lines:
        store_mod.insert_listen_segment(db, session_id, "demo", line)


def _seed_team(db: Any) -> None:
    memory_mod.upsert_person(
        db, "Avery", role="Backend Engineer",
        skillsets=["checkout", "api"], source="test",
    )
    memory_mod.upsert_person(
        db, "Carol", role="Designer",
        skillsets=["mockups"], source="test",
    )


def test_stop_to_proposals_suggests_memory_owner(tmp_path, monkeypatch) -> None:
    app_mod, db = _setup(monkeypatch, tmp_path)
    _seed(db, "pipe-1", CHECKOUT_LINE)
    _seed_team(db)
    board = _FakeBoard()
    app_mod._BOARD_OVERRIDES["demo"] = board
    try:
        client = TestClient(app_mod.app)
        resp = client.post("/api/meetings/pipe-1/finish?project=demo")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert len(body["proposals"]) >= 1
        assert body["proposals"][0]["owner"] == "Avery"
        assert body["proposals"][0]["estimate"] > 0
        assert board.created == [], "finish must not post to the board"
        saved = client.get("/api/meetings/pipe-1/proposals?project=demo")
        assert saved.status_code == 200
        assert saved.json()["proposals"][0]["meeting_id"] == "pipe-1"
    finally:
        app_mod._BOARD_OVERRIDES.pop("demo", None)


def test_board_owners_fallback_without_memory(tmp_path, monkeypatch) -> None:
    app_mod, db = _setup(monkeypatch, tmp_path)
    _seed(db, "pipe-fb", CHECKOUT_LINE)
    board = _FakeBoard([{"id": "9", "title": "Old", "assigned_to": "Sam"}])
    app_mod._BOARD_OVERRIDES["demo"] = board
    try:
        client = TestClient(app_mod.app)
        resp = client.post("/api/meetings/pipe-fb/finish?project=demo")
        assert resp.status_code == 200, resp.text
        assert resp.json()["proposals"][0]["owner"] == "Sam"
        assert board.created == []
    finally:
        app_mod._BOARD_OVERRIDES.pop("demo", None)


def test_edit_before_approve_applies_edits(tmp_path, monkeypatch) -> None:
    app_mod, db = _setup(monkeypatch, tmp_path)
    _seed(db, "pipe-2", TWO_ACTIONS)
    _seed_team(db)
    board = _FakeBoard()
    app_mod._BOARD_OVERRIDES["demo"] = board
    try:
        client = TestClient(app_mod.app)
        assert client.post("/api/meetings/pipe-2/finish?project=demo").status_code == 200
        edit = client.post(
            "/api/meetings/pipe-2/proposals/0?project=demo",
            json={"title": "Edited checklist", "owner": "Sam", "estimate": 6.5},
        )
        assert edit.status_code == 200, edit.text
        assert edit.json()["proposal"]["title"] == "Edited checklist"
        approve = client.post(
            "/api/meetings/pipe-2/proposals/approve?project=demo&all=1"
        )
        assert approve.status_code == 200, approve.text
        assert len(board.created) == 2
        first = board.created[0]
        assert first["title"] == "Edited checklist"
        assert first["assigned_to"] == "Sam"
        assert first["estimate"] == 6.5
        assert "pipe-2" in first["description"]
        assert "Evidence" in first["description"]
    finally:
        app_mod._BOARD_OVERRIDES.pop("demo", None)


def test_approve_selected_writes_only_selected(tmp_path, monkeypatch) -> None:
    app_mod, db = _setup(monkeypatch, tmp_path)
    _seed(db, "pipe-3", TWO_ACTIONS)
    _seed_team(db)
    board = _FakeBoard()
    app_mod._BOARD_OVERRIDES["demo"] = board
    try:
        client = TestClient(app_mod.app)
        assert client.post("/api/meetings/pipe-3/finish?project=demo").status_code == 200
        approve = client.post(
            "/api/meetings/pipe-3/proposals/approve?project=demo",
            json={"selected": [1]},
        )
        assert approve.status_code == 200, approve.text
        assert len(board.created) == 1
        assert "staging window" in board.created[0]["title"]
        assert "pipe-3" in board.created[0]["description"]
        assert "Transcript" in board.created[0]["description"]
    finally:
        app_mod._BOARD_OVERRIDES.pop("demo", None)


def test_discard_writes_nothing(tmp_path, monkeypatch) -> None:
    app_mod, db = _setup(monkeypatch, tmp_path)
    _seed(db, "pipe-4", TWO_ACTIONS)
    _seed_team(db)
    board = _FakeBoard()
    app_mod._BOARD_OVERRIDES["demo"] = board
    try:
        client = TestClient(app_mod.app)
        assert client.post("/api/meetings/pipe-4/finish?project=demo").status_code == 200
        discard = client.post(
            "/api/meetings/pipe-4/proposals/discard?project=demo"
        )
        assert discard.status_code == 200, discard.text
        assert board.created == []
        saved = store_mod.get_listen_proposals(db, "pipe-4")
        assert saved is not None and saved["status"] == "discarded"
        approve = client.post(
            "/api/meetings/pipe-4/proposals/approve?project=demo&all=1"
        )
        assert approve.status_code == 400
        assert board.created == []
    finally:
        app_mod._BOARD_OVERRIDES.pop("demo", None)


def test_no_auto_post_without_approval(tmp_path, monkeypatch) -> None:
    app_mod, db = _setup(monkeypatch, tmp_path)
    _seed(db, "pipe-5", TWO_ACTIONS)
    _seed_team(db)
    board = _FakeBoard()
    app_mod._BOARD_OVERRIDES["demo"] = board
    try:
        client = TestClient(app_mod.app)
        assert client.post("/api/meetings/pipe-5/finish?project=demo").status_code == 200
        assert client.post("/api/meetings/pipe-5/finish?project=demo").status_code == 200
        notes = client.post("/api/meetings/pipe-5/notes?project=demo")
        assert notes.status_code == 200
        assert board.created == [], "nothing posts without approval"
    finally:
        app_mod._BOARD_OVERRIDES.pop("demo", None)
