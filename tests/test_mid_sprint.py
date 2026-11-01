"""Mid-sprint adjustment tests on native and ADO backends (fakes only)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.sqlite_board import NativeBoard
from pam_pm.agents import mid_sprint as ms_mod
from pam_pm.agents import next_sprint as ns_mod
from tests.test_both_adapters import _make_board

TODAY = "2026-03-10"
OLD_CHANGED = "2026-01-20T00:00:00Z"


@pytest.fixture(params=["native", "ado-agile"], ids=["native", "ado"])
def board_case(request: Any) -> Any:
    board, _done, _states = _make_board(str(request.param))
    try:
        yield board
    finally:
        board.close()


def _roster() -> list[dict[str, Any]]:
    return [
        {"name": "Maya", "capacity": 10.0},
        {"name": "Sam", "capacity": 10.0},
    ]


def _sprint(board: Any, name: str = "Sprint 1") -> Any:
    return board.create_iteration(name)


def _add_to_sprint(board: Any, sprint: Any, title: str, fields: dict) -> Any:
    item = board.create_item(title, dict(fields))
    board.set_item_iteration(item.id, sprint.id)
    return item


def _make_old(board: Any, item: Any) -> None:
    conn = getattr(board, "_conn", None)
    if conn is not None:
        conn.execute(
            "UPDATE items SET changed = ? WHERE id = ?", (OLD_CHANGED, item.id)
        )
        conn.commit()


def test_stalled_blocker_trigger(board_case: Any) -> None:
    board = board_case
    sprint = _sprint(board)
    item = _add_to_sprint(
        board, sprint, "Migrate billing data",
        {"state": "Blocked", "assigned_to": "Maya", "estimate": 6},
    )
    _make_old(board, item)
    proposal = ms_mod.propose_adjustment(board, _roster(), sprint.id, today=TODAY)
    kinds = [entry["kind"] for entry in proposal["triggers"]]
    assert ms_mod.TRIGGER_STALLED in kinds
    moves = [c for c in proposal["changes"] if c["action"] == "move_out"]
    assert len(moves) == 1
    assert moves[0]["item_id"] == str(item.id)
    assert moves[0]["goal_effect"]
    assert moves[0]["evidence"]


def test_falling_behind_trigger(board_case: Any) -> None:
    board = board_case
    sprint = _sprint(board)
    for num in range(3):
        _add_to_sprint(
            board, sprint, f"Backend slice {num + 1}",
            {"state": "Active", "assigned_to": "Maya", "estimate": 4},
        )
    proposal = ms_mod.propose_adjustment(
        board, _roster(), sprint.id, today=TODAY, overload_threshold=2
    )
    kinds = [entry["kind"] for entry in proposal["triggers"]]
    assert ms_mod.TRIGGER_BEHIND in kinds
    moves = [c for c in proposal["changes"] if c["action"] == "reassign"]
    assert len(moves) == 1
    assert moves[0]["owner"] == "Maya"
    assert moves[0]["target_owner"] == "Sam"
    assert "goal" in moves[0]["goal_effect"].lower()


def test_urgent_scope_trigger(board_case: Any) -> None:
    board = board_case
    sprint = _sprint(board)
    _add_to_sprint(
        board, sprint, "Polish settings page",
        {"state": "Active", "assigned_to": "Maya", "estimate": 3},
    )
    urgent = _add_to_sprint(
        board, sprint, "Fix login outage now",
        {"state": "Active", "assigned_to": "Sam", "estimate": 2, "tags": "urgent"},
    )
    roster = [{"name": "Maya"}, {"name": "Sam"}]
    proposal = ms_mod.propose_adjustment(board, roster, sprint.id, today=TODAY)
    kinds = [entry["kind"] for entry in proposal["triggers"]]
    assert ms_mod.TRIGGER_URGENT in kinds
    swaps = [c for c in proposal["changes"] if c["action"] == "swap"]
    assert len(swaps) == 1
    assert swaps[0]["in_item_id"] == str(urgent.id)
    assert swaps[0]["goal_effect"]


def test_surplus_capacity_trigger(board_case: Any) -> None:
    board = board_case
    sprint = _sprint(board)
    _add_to_sprint(
        board, sprint, "Small docs fix",
        {"state": "Active", "assigned_to": "Maya", "estimate": 2},
    )
    board.create_item("Backlog epilogue work", {"state": "New", "estimate": 3})
    roster = [
        {"name": "Maya", "capacity": 20.0},
        {"name": "Sam", "capacity": 20.0},
    ]
    proposal = ms_mod.propose_adjustment(board, roster, sprint.id, today=TODAY)
    kinds = [entry["kind"] for entry in proposal["triggers"]]
    assert ms_mod.TRIGGER_SURPLUS in kinds
    pulls = [c for c in proposal["changes"] if c["action"] == "pull"]
    assert len(pulls) == 1
    assert pulls[0]["title"] == "Backlog epilogue work"
    assert pulls[0]["goal_effect"]


def test_goal_effect_present(board_case: Any) -> None:
    board = board_case
    sprint = _sprint(board)
    item = _add_to_sprint(
        board, sprint, "Stuck deploy pipeline",
        {"state": "Blocked", "assigned_to": "Maya", "estimate": 5},
    )
    _make_old(board, item)
    proposal = ms_mod.propose_adjustment(
        board, _roster(), sprint.id, goal="Ship checkout.", today=TODAY
    )
    assert proposal["goal"] == "Ship checkout."
    assert proposal["changes"]
    for change in proposal["changes"]:
        assert change["goal_effect"]
        assert change["reason"]
    text = ms_mod.format_mid_sprint_message(proposal)
    assert "Ship checkout." in text
    assert "Goal effect:" in text


def test_approve_applies(board_case: Any) -> None:
    board = board_case
    sprint = _sprint(board)
    for num in range(3):
        _add_to_sprint(
            board, sprint, f"Backend slice {num + 1}",
            {"state": "Active", "assigned_to": "Maya", "estimate": 4},
        )
    proposal = ms_mod.propose_adjustment(
        board, _roster(), sprint.id, today=TODAY, overload_threshold=2
    )
    change = next(c for c in proposal["changes"] if c["action"] == "reassign")
    dry = ms_mod.apply_change(board, change, sprint.id, dry_run=True)
    assert dry["applied"] is False
    assert ns_mod.owner_of(board.get_item(change["item_id"])) == "Maya"
    live = ms_mod.apply_change(board, change, sprint.id, dry_run=False)
    assert live["applied"] is True
    assert ns_mod.owner_of(board.get_item(change["item_id"])) == "Sam"


def test_approve_pull_and_move_out(board_case: Any) -> None:
    board = board_case
    sprint = _sprint(board)
    waiting = board.create_item("Backlog epilogue work", {"state": "New"})
    pull = {
        "action": "pull", "item_id": str(waiting.id), "title": waiting.title,
        "owner": "", "target_owner": "Maya", "estimate": 3.0,
        "reason": "Use free capacity.", "evidence": "Backlog waits.",
        "goal_effect": "Advances the goal.",
    }
    out = ms_mod.apply_change(board, pull, sprint.id, dry_run=False)
    assert out["applied"] is True
    assert ns_mod.iteration_of(board.get_item(waiting.id)) in (
        sprint.id,
        sprint.path,
        sprint.name,
    )
    move = dict(pull, action="move_out", target_owner="")
    out2 = ms_mod.apply_change(board, move, sprint.id, dry_run=False)
    assert out2["applied"] is True
    assert ns_mod.iteration_of(board.get_item(waiting.id)) == ""


def test_never_auto_applies(board_case: Any) -> None:
    board = board_case
    sprint = _sprint(board)
    item = _add_to_sprint(
        board, sprint, "Migrate billing data",
        {"state": "Blocked", "assigned_to": "Maya", "estimate": 6},
    )
    _make_old(board, item)

    class FakeWrites:
        def __init__(self) -> None:
            self.proposed: list[dict] = []
            self._next = 1

        def propose(self, entry: dict) -> int:
            self.proposed.append(dict(entry))
            ident = self._next
            self._next += 1
            return ident

    writes = FakeWrites()
    result = ms_mod.run_mid_sprint(
        board, _roster(), sprint.id, writes=writes, today=TODAY
    )
    assert result["count"] >= 1
    assert len(result["write_ids"]) == result["count"]
    assert ns_mod.iteration_of(board.get_item(item.id)) in (
        sprint.id,
        sprint.path,
        sprint.name,
    )
    assert ns_mod.owner_of(board.get_item(item.id)) == "Maya"


def test_slack_post_has_approve_button(board_case: Any) -> None:
    board = board_case
    sprint = _sprint(board)
    item = _add_to_sprint(
        board, sprint, "Migrate billing data",
        {"state": "Blocked", "assigned_to": "Maya", "estimate": 6},
    )
    _make_old(board, item)
    proposal = ms_mod.propose_adjustment(board, _roster(), sprint.id, today=TODAY)
    chat = InMemoryChat()
    assert ms_mod.post_mid_sprint(chat, "U1", proposal) is True
    assert len(chat.dms) == 1
    assert "Mid-sprint" in chat.dms[0]["text"]
    assert chat.dms[0]["buttons"][0]["label"] == "Approve"


def test_rules_engine_registers_both_triggers() -> None:
    from pam_pm.rules.engine import RuleEngine

    engine = RuleEngine()
    seen: list[str] = []
    ms_mod.register_mid_sprint(engine, lambda event: seen.append("ran"))
    assert engine.handler_count("schedule") == 1
    assert engine.handler_count("ado_change") == 1


def test_console_card_renders(tmp_path: Any, monkeypatch: Any) -> None:
    from pam_pm.web import app as app_mod

    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    board = NativeBoard()
    try:
        sprint = board.create_iteration("Sprint 1")
        for title, fields in (
            ("Polish settings page", {"state": "Active", "assigned_to": "Maya"}),
            ("Fix login outage now", {"state": "Active", "tags": "urgent"}),
        ):
            item = board.create_item(title, fields)
            board.set_item_iteration(item.id, sprint.id)
        app_mod._BOARD_OVERRIDES["default"] = board
        client = TestClient(app_mod.app)
        assert client.get("/mid-sprint").status_code == 200
        assert 'href="/mid-sprint"' in client.get("/setup").text
        resp = client.post("/api/mid-sprint/run?project=default")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1
        card = client.post(
            "/api/mid-sprint/run?project=default", headers={"hx-request": "true"}
        )
        assert "Mid-sprint" in card.text
        assert "Approve" in card.text
        assert "Goal effect" in card.text
    finally:
        app_mod._BOARD_OVERRIDES.pop("default", None)
        board.close()
