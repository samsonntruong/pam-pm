"""Phase 0 batch A1: test check-in, ADO writes, reporter-first, poll interval.

Each behavior runs on both backends (Pam board and ADO through the
shared fake transport) with fake chat and a scripted model.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.agents.interviewer import Interviewer, write_interview_result
from pam_pm.core import auth as auth_mod
from pam_pm.core import recovery as recovery_mod
from pam_pm.core.config import Config
from pam_pm.core.schemas import InterviewResult
from pam_pm.core.writes import WriteService
from pam_pm.models.client import ChatResult, ChatToolCall
from pam_pm.rules.poller import POLL_INTERVAL, ADOPoller, effective_poll_interval
from pam_pm.rules.templates import morning as morning_mod
from pam_pm.rules.templates.blocker import blocker_intake
from pam_pm.web import app as app_mod
from tests.test_both_adapters import _make_board


class _ScriptedModel:
    """Model returning one question then a finish_interview call."""

    def __init__(self, script: list[ChatResult]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        _ = (messages, tools, max_tokens)
        self.calls.append({"messages": messages})
        if len(self.calls) <= len(self._script):
            return self._script[len(self.calls) - 1]
        return self._script[-1]


def _finish_script(
    status: str = "Active",
    remaining: float = 4.0,
    eta: str = "2026-11-06",
    quote: str = "4 hours left, ETA 2026-11-06, finishing tests",
) -> list[ChatResult]:
    return [
        ChatResult(text="What is the status of the task?", tool_calls=[]),
        ChatResult(
            text="",
            tool_calls=[
                ChatToolCall(
                    name="finish_interview",
                    arguments=json.dumps(
                        {
                            "status": status,
                            "remaining": remaining,
                            "eta": eta,
                            "evidence_quote": quote,
                            "blocker": None,
                            "blocker_owner": None,
                            "confidence": 0.8,
                        }
                    ),
                )
            ],
        ),
    ]


@pytest.fixture(params=["native", "ado-agile"], ids=["native", "ado-agile"])
def board_pair(request: Any, tmp_path: Any) -> Any:
    _ = tmp_path
    board, _, _ = _make_board(str(request.param))
    try:
        yield (str(request.param), board)
    finally:
        board.close()


def _client(monkeypatch: Any, tmp_path: Any) -> TestClient:
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    for key in ("PAM_ALLOWED_HOST", "PAM_CONSOLE_PASSWORD", "PAM_SHARE_ON_LAN"):
        monkeypatch.delenv(key, raising=False)
    auth_mod.reset_sessions()
    auth_mod.reset_login_attempts()
    app_mod.reset_board_state()
    app_mod.reset_chat_state()
    return TestClient(app_mod.app)


def _teardown_client() -> None:
    app_mod.reset_board_state()
    app_mod.reset_chat_state()


def test_checkin_helper_sends_to_chosen_teammate(
    board_pair: Any, tmp_path: Any
) -> None:
    _, board = board_pair
    board.create_item("A1 morning", {"state": "Active", "assigned_to": "U1"})
    chat = InMemoryChat()
    chat.script_replies("U1", ["4 hours left, ETA 2026-11-06, finishing tests"])
    out = morning_mod.test_checkin(
        "U1",
        chat,
        board,
        _ScriptedModel(_finish_script()),
        project="a1",
        db=tmp_path / "a1.db",
    )
    assert "U1" in out
    assert [dm for dm in chat.dms if dm["user_id"] == "U1"]


def test_checkin_helper_ignores_blank_person(board_pair: Any) -> None:
    _, board = board_pair
    chat = InMemoryChat()
    assert morning_mod.test_checkin("  ", chat, board, _ScriptedModel([])) == {}
    assert chat.dms == []


@pytest.mark.parametrize("kind", ["native", "ado-agile"])
def test_console_test_checkin_endpoint(
    kind: str, tmp_path: Any, monkeypatch: Any
) -> None:
    client = _client(monkeypatch, tmp_path)
    board, _, _ = _make_board(kind)
    try:
        project = "a1-checkin"
        app_mod.set_board_adapter(project, board)
        chat = InMemoryChat()
        app_mod.set_chat_adapter(project, chat)
        board.create_item("A1 console", {"state": "Active", "assigned_to": "U1"})
        chat.script_replies("U1", ["4 hours left, ETA 2026-11-06, finishing"])
        app_mod.set_model_client(project, _ScriptedModel(_finish_script()))
        resp = client.post(
            f"/api/checkin/test?project={project}", json={"person": "U1"}
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["person"] == "U1"
        assert [dm for dm in chat.dms if dm["user_id"] == "U1"]
        page = client.get("/activity").text
        assert "Send test check-in" in page
        assert "—" not in page
    finally:
        board.close()
        _teardown_client()


def test_interview_result_writes_comment_and_remaining(
    board_pair: Any,
) -> None:
    kind, board = board_pair
    item = board.create_item("A1 write path", {"state": "Active"})
    result = InterviewResult(
        status="Active",
        remaining=4.0,
        eta="2026-11-06",
        evidence_quote="4 hours left",
        confidence=0.8,
    )
    out = write_interview_result(board, item.id, result)
    assert out["comment"] is not None
    fields = board.get_item(item.id).fields
    if kind == "native":
        assert fields.get("remaining") == 4.0
    else:
        assert fields.get("Microsoft.VSTS.Scheduling.RemainingWork") == 4.0
    comments = board.list_comments(item.id)
    assert len(comments) == 1
    assert "Active" in comments[0].text
    assert "4 hours left" in comments[0].text


def test_interviewer_run_posts_comment(board_pair: Any) -> None:
    kind, board = board_pair
    item = board.create_item(
        "A1 interview", {"state": "Active", "assigned_to": "U1"}
    )
    chat = InMemoryChat()
    chat.script_replies("U1", ["4 hours left, ETA 2026-11-06, finishing tests"])
    Interviewer().run("U1", [item], chat, board, _ScriptedModel(_finish_script()))
    fields = board.get_item(item.id).fields
    if kind == "native":
        assert fields.get("remaining") == 4.0
    else:
        assert fields.get("Microsoft.VSTS.Scheduling.RemainingWork") == 4.0
    comments = board.list_comments(item.id)
    assert len(comments) == 1
    assert "Remaining 4.0h" in comments[0].text


def test_blocker_dms_reporter_before_owner(
    board_pair: Any, tmp_path: Any
) -> None:
    _, board = board_pair
    chat = InMemoryChat()
    blocked = board.create_item(
        "Ship report", {"state": "Active", "assigned_to": "U1"}
    )
    chat.script_replies("U1", ["Report blocked", "U2", "Misses deadline"])
    writes = WriteService(tmp_path / "a1.db", dry_run=True)
    result = blocker_intake("U1", chat, board, writes, blocked_item_id=blocked.id)
    assert result["reporter_dm"]
    assert result["eta_dm"]
    texts = [(dm["user_id"], dm["text"]) for dm in chat.dms]
    reporter_idx = next(
        i
        for i, (user, text) in enumerate(texts)
        if user == "U1" and "about to ask" in text.lower()
    )
    owner_idx = next(
        i
        for i, (user, text) in enumerate(texts)
        if user == "U2" and "eta" in text.lower()
    )
    assert reporter_idx < owner_idx
    assert "U2" in texts[reporter_idx][1]
    assert "block" in texts[reporter_idx][1].lower()


def test_poll_interval_default_and_env(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.delenv("PAM_POLL_INTERVAL", raising=False)
    assert Config().poll_interval == 60
    assert POLL_INTERVAL == 60
    monkeypatch.setenv("PAM_POLL_INTERVAL", "7")
    assert Config().poll_interval == 7


def test_poller_honors_config_and_saved_value(
    board_pair: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    _, board = board_pair
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.delenv("PAM_POLL_INTERVAL", raising=False)
    db = tmp_path / "poll.db"
    assert ADOPoller(board, db, project="a1").interval == 60
    monkeypatch.setenv("PAM_POLL_INTERVAL", "9")
    assert ADOPoller(board, db, project="a1").interval == 9
    monkeypatch.delenv("PAM_POLL_INTERVAL", raising=False)
    recovery_mod.kv_set(db, "settings.poll_interval", "5")
    assert effective_poll_interval(db) == 5
    assert ADOPoller(board, db, project="a1").interval == 5
    assert ADOPoller(board, db, project="a1", interval=30).interval == 30


def test_poll_loop_refreshes_saved_value(
    board_pair: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    _, board = board_pair
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.delenv("PAM_POLL_INTERVAL", raising=False)
    db = tmp_path / "poll.db"
    poller = ADOPoller(board, db, project="a1")
    assert poller.interval == 60
    recovery_mod.kv_set(db, "settings.poll_interval", "4")
    rounds = list(poller.poll(max_rounds=2, sleep=False))
    assert len(rounds) == 2
    assert poller.interval == 4


def test_poll_interval_console_roundtrip(tmp_path: Any, monkeypatch: Any) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        resp = client.post("/api/settings/poll-interval", json={"poll_interval": 5})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "poll_interval": 5}
        assert effective_poll_interval(Config().db_path()) == 5
        text = client.get("/settings").text
        assert "Poll interval" in text
        assert 'value="5"' in text
        bad = client.post("/api/settings/poll-interval", json={"poll_interval": 0})
        assert bad.status_code == 400
    finally:
        _teardown_client()
