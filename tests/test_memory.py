"""Team memory tests: capture, recall, fallback, and Settings routes."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from pam_pm.adapters.pm.sqlite_board import NativeBoard
from pam_pm.agents import next_sprint as ns_mod
from pam_pm.agents.kickoff import propose_kickoff
from pam_pm.core import memory as memory_mod

TOKEN = "memory-test-token-1"


def _board() -> Any:
    return NativeBoard()


def test_kickoff_captures_roster_to_memory(tmp_path: Any) -> None:
    db = tmp_path / "memory.db"
    board = _board()
    try:
        roster = [
            {"name": "Maya", "skills": "backend api", "capacity": 10.0},
            {"name": "Sam", "skills": "design", "capacity": 8.0},
        ]
        propose_kickoff("Maya: build login api, 4h", roster, board, db=db)
        maya = memory_mod.get_person(db, "Maya")
        sam = memory_mod.get_person(db, "Sam")
        assert maya is not None and maya["source"] == "kickoff"
        assert "backend" in maya["skillsets"] and "api" in maya["skillsets"]
        assert maya["weekly_capacity"] == 10.0
        assert sam is not None and sam["weekly_capacity"] == 8.0
    finally:
        board.close()


def test_interview_text_captures_profile(tmp_path: Any) -> None:
    db = tmp_path / "memory.db"
    saved = memory_mod.capture_interview(
        db,
        "Sam",
        [
            "I am a frontend designer",
            "skills: design, ux",
            "I work 20 hours per week",
        ],
    )
    assert saved is not None and saved["source"] == "interview"
    assert "Designer" in saved["role"]
    assert "design" in saved["skillsets"] and "ux" in saved["skillsets"]
    assert saved["weekly_capacity"] == 20.0


def test_recall_holds_backlog_over_capacity() -> None:
    board = _board()
    try:
        memory = {
            "people": [{"name": "Maya", "weekly_capacity": 4.0, "skillsets": []}],
            "bias": 1.0,
        }
        proposal = ns_mod.propose_next_sprint(
            board,
            [{"name": "Maya"}],
            backlog=[{"title": "Big rewrite", "estimate": 8.0}],
            current_iteration_id="nope",
            memory=memory,
        )
        assert proposal["items"] == []
        assert "Held for capacity: Big rewrite." in proposal["risk_note"]
    finally:
        board.close()


def test_recall_prefers_skill_match() -> None:
    board = _board()
    try:
        memory = {
            "people": [
                {"name": "Maya", "weekly_capacity": 10.0, "skillsets": ["api"]},
                {"name": "Sam", "weekly_capacity": 10.0, "skillsets": ["design"]},
            ],
            "bias": 1.0,
        }
        proposal = ns_mod.propose_next_sprint(
            board,
            [{"name": "Maya"}, {"name": "Sam"}],
            backlog=[{"title": "Build login api", "estimate": 4.0}],
            current_iteration_id="nope",
            memory=memory,
        )
        assert len(proposal["items"]) == 1
        assert proposal["items"][0]["owner"] == "Maya"
        assert "Skill match (api)" in proposal["items"][0]["reason"]
    finally:
        board.close()


def test_recall_corrects_estimates_by_bias() -> None:
    board = _board()
    try:
        memory = {
            "people": [{"name": "Maya", "weekly_capacity": 10.0, "skillsets": []}],
            "bias": 1.5,
        }
        proposal = ns_mod.propose_next_sprint(
            board,
            [{"name": "Maya"}],
            backlog=[{"title": "Small task", "estimate": 4.0}],
            current_iteration_id="nope",
            memory=memory,
        )
        assert proposal["items"][0]["estimate"] == 6.0
        assert "corrected by past bias x1.5" in proposal["risk_note"]
    finally:
        board.close()


def test_empty_memory_matches_original_behavior(tmp_path: Any) -> None:
    db = tmp_path / "memory.db"
    snapshot = memory_mod.planning_context(db)
    assert snapshot == {"people": [], "bias": 1.0}
    assert memory_mod.memory_is_empty(snapshot) is True
    board = _board()
    try:
        item = board.create_item("Fresh work", {"state": "New"})
        roster = [{"name": "Maya", "capacity": 10.0}]
        with_memory = ns_mod.propose_next_sprint(
            board, roster, backlog=[item], current_iteration_id="nope",
            memory=snapshot,
        )
        without_memory = ns_mod.propose_next_sprint(
            board, roster, backlog=[item], current_iteration_id="nope",
        )
        assert with_memory == without_memory
    finally:
        board.close()


def _memory_client(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("PAM_SETUP_TOKEN", TOKEN)
    import pam_pm.web.app as app_mod

    return TestClient(app_mod.app)


def test_settings_person_edit_route(tmp_path: Any, monkeypatch: Any) -> None:
    client = _memory_client(tmp_path, monkeypatch)
    first = client.post(
        "/api/settings/memory/person",
        json={
            "token": TOKEN, "name": "Maya", "role": "Backend Engineer",
            "skills": "api, sql", "capacity": 8,
        },
    )
    assert first.status_code == 200
    assert first.json()["ok"] is True
    assert first.json()["people"][0]["role"] == "Backend Engineer"
    second = client.post(
        "/api/settings/memory/person",
        json={"token": TOKEN, "name": "Maya", "role": "Lead Engineer"},
    )
    assert second.json()["people"][0]["role"] == "Lead Engineer"
    view = client.get(f"/api/settings/memory?token={TOKEN}")
    assert view.json()["people"][0]["name"] == "Maya"
    assert "api" in view.json()["people"][0]["skillsets"]


def test_settings_person_delete_route(tmp_path: Any, monkeypatch: Any) -> None:
    client = _memory_client(tmp_path, monkeypatch)
    client.post(
        "/api/settings/memory/person",
        json={"token": TOKEN, "name": "Maya", "capacity": 8},
    )
    removed = client.post(
        "/api/settings/memory/person/delete",
        json={"token": TOKEN, "name": "Maya"},
    )
    assert removed.status_code == 200
    assert removed.json()["ok"] is True
    view = client.get(f"/api/settings/memory?token={TOKEN}")
    assert view.json()["people"] == []


def test_settings_clear_route(tmp_path: Any, monkeypatch: Any) -> None:
    client = _memory_client(tmp_path, monkeypatch)
    for name in ("Maya", "Sam"):
        client.post(
            "/api/settings/memory/person",
            json={"token": TOKEN, "name": name, "capacity": 8},
        )
    denied = client.post(
        "/api/settings/memory/clear", json={"token": TOKEN}
    )
    assert denied.status_code == 400
    cleared = client.post(
        "/api/settings/memory/clear", json={"token": TOKEN, "confirm": True}
    )
    assert cleared.status_code == 200
    assert cleared.json()["cleared"] == 2
    view = client.get(f"/api/settings/memory?token={TOKEN}")
    assert view.json()["people"] == []
