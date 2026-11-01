"""Next-sprint proposal tests on native and ADO backends (fakes only)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.sqlite_board import NativeBoard
from pam_pm.agents import next_sprint as ns_mod
from tests.test_both_adapters import _make_board


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


def test_carryover_keeps_owner_with_slip_reason(board_case: Any) -> None:
    board = board_case
    sprint = board.create_iteration("Sprint 1")
    item = board.create_item("Finish login", {"state": "Active", "assigned_to": "Maya"})
    board.set_item_iteration(item.id, sprint.id)
    board.add_comment(item.id, "Waiting on API keys.")
    proposal = ns_mod.propose_next_sprint(
        board, _roster(), current_iteration_id=sprint.id
    )
    carry = [entry for entry in proposal["items"] if entry["kind"] == "carryover"]
    assert len(carry) == 1
    assert carry[0]["owner"] == "Maya"
    assert "API keys" in carry[0]["slip_reason"]
    assert proposal["has_history"] is False
    assert "capacity and estimates" in proposal["risk_note"]


def test_dependency_order_holds_predecessor(board_case: Any) -> None:
    board = board_case
    pred = board.create_item("Provide keys", {"state": "New", "assigned_to": "Sam"})
    succ = board.create_item("Connect billing", {"state": "New", "assigned_to": "Maya"})
    board.add_link(pred.id, succ.id, "System.LinkTypes.Dependency-Forward")
    proposal = ns_mod.propose_next_sprint(
        board, _roster(), backlog=[succ], current_iteration_id="nope"
    )
    titles = [entry["title"] for entry in proposal["items"]]
    assert titles == []
    assert proposal["deferred"] == ["Connect billing"]
    full = ns_mod.propose_next_sprint(
        board, _roster(), backlog=[pred, succ], current_iteration_id="nope"
    )
    order = [entry["title"] for entry in full["items"]]
    assert order.index("Provide keys") < order.index("Connect billing")


def test_load_balance_uses_throughput_then_capacity(board_case: Any) -> None:
    board = board_case
    done = board.create_item("Old work", {"state": "New", "assigned_to": "Maya"})
    board.close_item(done.id)
    first = board.create_item("Task one", {"state": "New"})
    second = board.create_item("Task two", {"state": "New"})
    proposal = ns_mod.propose_next_sprint(
        board, _roster(), backlog=[first, second], current_iteration_id="nope"
    )
    assert proposal["has_history"] is True
    assert proposal["throughput"].get("Maya", 0.0) > 0
    owners = [entry["owner"] for entry in proposal["items"]]
    assert owners[0] == "Sam"
    assert proposal["loads"]["Sam"] > 0


def test_retro_actions_first(board_case: Any) -> None:
    board = board_case
    backlog_item = board.create_item("Backlog work", {"state": "New"})
    proposal = ns_mod.propose_next_sprint(
        board,
        _roster(),
        backlog=[backlog_item],
        retro_actions=[{"title": "Fix flaky test", "owner": "Maya"}],
        current_iteration_id="nope",
    )
    assert proposal["items"][0]["kind"] == "retro"
    assert proposal["items"][0]["title"] == "Fix flaky test"
    assert proposal["items"][0]["owner"] == "Maya"


def test_no_history_fallback_says_so(board_case: Any) -> None:
    board = board_case
    item = board.create_item("Fresh work", {"state": "New"})
    proposal = ns_mod.propose_next_sprint(
        board, _roster(), backlog=[item], current_iteration_id="nope"
    )
    assert proposal["has_history"] is False
    text = ns_mod.format_next_sprint_message(proposal)
    assert "no history yet" in text.lower()
    assert "Load:" in text
    assert "Risk:" in text


def test_approve_creates_iteration_and_moves(board_case: Any) -> None:
    board = board_case
    item = board.create_item("Ship it", {"state": "New", "assigned_to": "Maya"})
    proposal = ns_mod.propose_next_sprint(
        board, _roster(), backlog=[item], current_iteration_id="nope"
    )
    dry = ns_mod.approve_next_sprint(board, proposal, "Sprint 2", dry_run=True)
    assert dry["applied"] is False
    assert board.list_iterations() == []
    live = ns_mod.approve_next_sprint(board, proposal, "Sprint 2", dry_run=False)
    assert live["applied"] is True
    assert live["iteration"] is not None
    names = [entry.name for entry in board.list_iterations()]
    assert "Sprint 2" in names
    assert ns_mod.iteration_of(board.get_item(item.id)) in (
        live["iteration"].id,
        live["iteration"].path,
        live["iteration"].name,
    )


def test_slack_post_has_approve_button(board_case: Any) -> None:
    board = board_case
    item = board.create_item("Ship it", {"state": "New", "assigned_to": "Maya"})
    proposal = ns_mod.propose_next_sprint(
        board, _roster(), backlog=[item], current_iteration_id="nope"
    )
    chat = InMemoryChat()
    assert ns_mod.post_next_sprint(chat, "U1", proposal) is True
    assert len(chat.dms) == 1
    assert "Next sprint" in chat.dms[0]["text"]
    assert chat.dms[0]["buttons"][0]["label"] == "Approve"


def test_console_page_renders(tmp_path: Any, monkeypatch: Any) -> None:
    from pam_pm.web import app as app_mod

    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    board = NativeBoard()
    try:
        board.create_item("Ship it", {"state": "New", "assigned_to": "Maya"})
        app_mod._BOARD_OVERRIDES["default"] = board
        client = TestClient(app_mod.app)
        assert client.get("/next-sprint").status_code == 200
        assert 'href="/next-sprint"' in client.get("/setup").text
        resp = client.post("/api/next-sprint/run?project=default")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1
        card = client.post(
            "/api/next-sprint/run?project=default", headers={"hx-request": "true"}
        )
        assert "Next sprint" in card.text
        assert "Approve" in card.text
    finally:
        app_mod._BOARD_OVERRIDES.pop("default", None)
        board.close()
