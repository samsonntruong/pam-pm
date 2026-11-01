"""Board triage rule tests on native and ADO backends (fakes only)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.sqlite_board import NativeBoard
from pam_pm.core import raid as raid_mod
from pam_pm.rules.templates import triage as triage_mod
from tests.test_both_adapters import _make_board

TODAY = "2026-02-10"
OLD_CHANGED = "2026-01-20T00:00:00Z"


@pytest.fixture(params=["native", "ado-agile"], ids=["native", "ado"])
def board_case(request: Any) -> Any:
    board, _done, _states = _make_board(str(request.param))
    try:
        yield (board, str(request.param))
    finally:
        board.close()


def _make_old(board: Any, item: Any) -> None:
    conn = getattr(board, "_conn", None)
    if conn is not None:
        conn.execute(
            "UPDATE items SET changed = ? WHERE id = ?", (OLD_CHANGED, item.id)
        )
        conn.commit()


def _find(
    findings: list[dict[str, Any]], kind: str, item_id: str | None = None
) -> list[dict[str, Any]]:
    return [
        entry
        for entry in findings
        if entry["kind"] == kind
        and (item_id is None or entry["item_id"] == str(item_id))
    ]


class FakeWrites:
    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run
        self.proposed: list[dict[str, Any]] = []
        self.applied: list[int] = []
        self._next = 1

    def propose(self, entry: dict[str, Any]) -> int:
        self.proposed.append(dict(entry))
        ident = self._next
        self._next += 1
        return ident

    def apply(self, write_id: int) -> bool:
        self.applied.append(int(write_id))
        return True


class CountingModel:
    def __init__(self, titles: dict[str, str] | None = None) -> None:
        self.calls = 0
        self.titles = dict(titles or {})

    def review_triage(self, unclear: list[dict[str, Any]]) -> dict[str, str]:
        self.calls += 1
        out: dict[str, str] = {}
        for entry in unclear:
            better = self.titles.get(str(entry["item_id"]), "")
            if better:
                out[str(entry["item_id"])] = better
        return out


def test_stale_detector(board_case: Any) -> None:
    board, _ = board_case
    item = board.create_item(
        "Finish quarterly report draft",
        {"state": "Active", "assigned_to": "Sam", "estimate": 5},
    )
    _make_old(board, item)
    findings = triage_mod.scan_board(board, today=TODAY)
    stale = _find(findings, "stale", item.id)
    assert len(stale) == 1
    assert "no change" in stale[0]["detail"].lower()
    assert stale[0]["fix"]["action"] == "comment"


def test_unassigned_detector(board_case: Any) -> None:
    board, _ = board_case
    item = board.create_item(
        "Write release notes draft", {"state": "New", "estimate": 2}
    )
    findings = triage_mod.scan_board(board, roster=["Sam"], today=TODAY)
    found = _find(findings, "unassigned", item.id)
    assert len(found) == 1
    assert found[0]["fix"]["action"] == "assign"
    assert found[0]["fix"]["owner"] == "Sam"


def test_missing_estimate_detector(board_case: Any) -> None:
    board, _ = board_case
    item = board.create_item(
        "Build export page flow", {"state": "New", "assigned_to": "Sam"}
    )
    findings = triage_mod.scan_board(board, today=TODAY)
    found = _find(findings, "missing_estimate", item.id)
    assert len(found) == 1
    assert found[0]["fix"]["action"] == "set_estimate"
    assert float(found[0]["fix"]["estimate"]) > 0


def test_overloaded_detector(board_case: Any) -> None:
    board, _ = board_case
    titles = ["Ship login page flow", "Ship billing page flow", "Ship search page"]
    for title in titles:
        board.create_item(
            title, {"state": "Active", "assigned_to": "Sam", "estimate": 3}
        )
    findings = triage_mod.scan_board(board, overload_threshold=2, today=TODAY)
    found = _find(findings, "overloaded")
    assert len(found) == 1
    assert "Sam" in found[0]["detail"]
    assert "3 open items" in found[0]["detail"]


def test_blocked_by_closed_predecessor(board_case: Any) -> None:
    board, _ = board_case
    pred = board.create_item(
        "Provide API keys file",
        {"state": "New", "assigned_to": "Sam", "estimate": 1},
    )
    succ = board.create_item(
        "Connect billing service",
        {"state": "New", "assigned_to": "Jo", "estimate": 2},
    )
    board.add_link(pred.id, succ.id, "System.LinkTypes.Dependency-Forward")
    board.close_item(pred.id)
    findings = triage_mod.scan_board(board, today=TODAY)
    found = _find(findings, "blocked_predecessor", succ.id)
    assert len(found) == 1
    assert "closed" in found[0]["detail"]
    assert found[0]["fix"]["action"] == "reopen"


def test_blocked_by_missing_predecessor(board_case: Any) -> None:
    board, _ = board_case
    succ = board.create_item(
        "Connect search service",
        {"state": "New", "assigned_to": "Jo", "estimate": 2},
    )
    conn = getattr(board, "_conn", None)
    if conn is not None:
        conn.execute(
            "INSERT INTO links (source_id, target_id, rel) VALUES (?, ?, ?)",
            ("9999", succ.id, "System.LinkTypes.Dependency-Forward"),
        )
        conn.commit()
    else:
        board.add_link(succ.id, "9999", "System.LinkTypes.Dependency-Reverse")
    findings = triage_mod.scan_board(board, today=TODAY)
    found = _find(findings, "blocked_predecessor", succ.id)
    assert len(found) == 1
    assert "missing" in found[0]["detail"]


def test_unclear_title_detector(board_case: Any) -> None:
    board, _ = board_case
    item = board.create_item(
        "fix", {"state": "New", "assigned_to": "Sam", "estimate": 3}
    )
    findings = triage_mod.scan_board(board, today=TODAY)
    found = _find(findings, "unclear_title", item.id)
    assert len(found) == 1
    assert found[0]["needs_judgment"] is True
    assert found[0]["fix"]["action"] == "rename"


def test_raid_flags_via_hooks(tmp_path: Any) -> None:
    db = tmp_path / "raid.db"
    raid_mod.save_item(
        db, "pam_board", "risk-1", project="atlas", kind="risk",
        title="Keys may slip", status="open",
    )
    raid_mod.save_item(
        db, "pam_board", "issue-1", project="atlas", kind="issue",
        title="Deploy blocked", owner="Sam", status="open", due_date="2026-01-01",
    )
    findings = triage_mod.scan_raid(db, "pam_board", "atlas", today=TODAY, lead="Ana")
    owners = _find(findings, "raid_no_owner", "risk-1")
    dues = _find(findings, "raid_past_due", "issue-1")
    assert len(owners) == 1
    assert owners[0]["fix"]["owner"] == "Ana"
    assert len(dues) == 1
    assert dues[0]["fix"]["due_date"] > TODAY


def test_model_called_only_when_needed(board_case: Any) -> None:
    board, kind = board_case
    item = board.create_item(
        "fix", {"state": "New", "assigned_to": "Sam", "estimate": 3}
    )
    model = CountingModel({str(item.id): "Fix login timeout on retry"})
    first = triage_mod.run_triage(board, model=model, roster=["Sam"], today=TODAY)
    assert first["changed"] is True
    assert first["model_called"] is True
    assert model.calls == 1
    unclear = _find(first["findings"], "unclear_title", item.id)
    assert unclear and unclear[0]["fix"]["title"] == "Fix login timeout on retry"
    second = triage_mod.run_triage(
        board, model=model, roster=["Sam"], today=TODAY,
        last_fingerprint=first["fingerprint"],
    )
    assert second["changed"] is False
    assert second["model_called"] is False
    assert model.calls == 1
    plain_board, _, _ = _make_board(kind)
    try:
        plain_board.create_item(
            "Build export page flow", {"state": "New", "assigned_to": "Sam"}
        )
        third = triage_mod.run_triage(
            plain_board, model=model, roster=["Sam"], today=TODAY
        )
    finally:
        plain_board.close()
    assert third["findings"]
    assert all(not entry["needs_judgment"] for entry in third["findings"])
    assert third["model_called"] is False
    assert model.calls == 1


def test_approve_applies_fix(board_case: Any) -> None:
    board, _ = board_case
    item = board.create_item(
        "Write release notes draft", {"state": "New", "estimate": 2}
    )
    findings = triage_mod.scan_board(board, roster=["Sam", "Jo"], today=TODAY)
    unassigned = _find(findings, "unassigned", item.id)[0]
    dry = triage_mod.apply_finding(board, unassigned, writes=FakeWrites(), dry_run=True)
    assert dry["applied"] is False
    assert triage_mod.owner_of(board.get_item(item.id)) == ""
    live = triage_mod.apply_finding(
        board, unassigned, writes=FakeWrites(), dry_run=False
    )
    assert live["applied"] is True
    assert triage_mod.owner_of(board.get_item(item.id)) == "Sam"
    item2 = board.create_item(
        "Build export page flow", {"state": "New", "assigned_to": "Jo"}
    )
    findings2 = triage_mod.scan_board(board, today=TODAY)
    missing = _find(findings2, "missing_estimate", item2.id)[0]
    out = triage_mod.apply_finding(board, missing, writes=FakeWrites(), dry_run=False)
    assert out["applied"] is True


def test_slack_post_and_message(board_case: Any) -> None:
    board, _ = board_case
    board.create_item("fix", {"state": "New", "assigned_to": "Sam", "estimate": 3})
    chat = InMemoryChat()
    result = triage_mod.run_triage(
        board, chat=chat, lead="U1", roster=["Sam"], today=TODAY
    )
    assert result["count"] >= 1
    assert result["sent"] is True
    assert len(chat.dms) == 1
    assert "need attention" in chat.dms[0]["text"]
    assert chat.dms[0]["kind"] == "buttons"
    assert chat.dms[0]["buttons"]


def test_console_card_renders(tmp_path: Any, monkeypatch: Any) -> None:
    from pam_pm.web import app as app_mod

    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    board = NativeBoard()
    try:
        board.create_item("fix", {"state": "New"})
        app_mod._BOARD_OVERRIDES["default"] = board
        client = TestClient(app_mod.app)
        assert client.get("/triage").status_code == 200
        assert 'href="/triage"' in client.get("/setup").text
        resp = client.post("/api/triage/run?project=default")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1
        card = client.post(
            "/api/triage/run?project=default", headers={"hx-request": "true"}
        )
        assert "need attention" in card.text
        assert "Approve" in card.text
    finally:
        app_mod._BOARD_OVERRIDES.pop("default", None)
        board.close()
