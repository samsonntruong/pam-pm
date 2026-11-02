"""Slack block_actions handling: approve, cancel, unknown ids on fakes."""

from __future__ import annotations

from typing import Any

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.chat.slack_socket import SocketModeInbound
from pam_pm.core import router as router_mod
from pam_pm.core import store as store_mod


class FakeBoard:
    """Minimal board fake supporting approve_proposal."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def list_states(self) -> list[str]:
        return ["To Do", "Doing", "Done"]

    def create_item(self, title: str, fields: dict[str, Any]) -> dict[str, Any]:
        item = {"id": str(len(self.items) + 1), "title": title, **fields}
        self.items.append(item)
        return item


def _proposal() -> list[dict[str, Any]]:
    return [
        {"title": "Login form", "type": "Task", "state": "To Do"},
        {"title": "API keys", "type": "Task", "state": "To Do"},
    ]


def _payload(
    action_id: str, value: str, trigger: str = "T1"
) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "trigger_id": trigger,
        "user": {"id": "U1"},
        "channel": {"id": "D1"},
        "actions": [{"action_id": action_id, "value": value}],
    }


def test_approve_creates_items_and_confirms(tmp_path: Any) -> None:
    db = str(tmp_path / "pam.db")
    pid = store_mod.save_plan_proposal(db, _proposal(), channel="D1")
    chat, board = InMemoryChat(), FakeBoard()
    result = router_mod.handle_slack_event(
        _payload("plan_approve", f"approve:{pid}"), chat=chat, board=board, db=db
    )
    assert result["kind"] == "block_actions"
    assert result["replied"] is True
    assert [i["title"] for i in board.items] == ["Login form", "API keys"]
    assert store_mod.get_plan_proposal(db, pid)["status"] == "approved"
    assert "Created 2 items" in chat.dms[-1]["text"]
    assert chat.dms[-1]["user_id"] == "U1"


def test_cancel_discards_and_confirms(tmp_path: Any) -> None:
    db = str(tmp_path / "pam.db")
    pid = store_mod.save_plan_proposal(db, _proposal(), channel="D1")
    chat, board = InMemoryChat(), FakeBoard()
    result = router_mod.handle_slack_event(
        _payload("plan_cancel", f"cancel:{pid}"), chat=chat, board=board, db=db
    )
    assert result["replied"] is True
    assert board.items == []
    assert store_mod.get_plan_proposal(db, pid)["status"] == "discarded"
    assert "Discarded" in chat.dms[-1]["text"]


def test_unknown_id_politely_declines(tmp_path: Any) -> None:
    db = str(tmp_path / "pam.db")
    chat, board = InMemoryChat(), FakeBoard()
    result = router_mod.handle_slack_event(
        _payload("plan_approve", "approve:9999"), chat=chat, board=board, db=db
    )
    assert result["replied"] is True
    assert board.items == []
    assert "could not find" in chat.dms[-1]["text"]


def test_socket_routes_interactive_envelopes() -> None:
    received: list[dict[str, Any]] = []
    acks: list[dict[str, Any]] = []

    class _Client:
        def send_socket_mode_response(self, payload: dict[str, Any]) -> None:
            acks.append(dict(payload))

    inbound = SocketModeInbound("xapp-test", received.append)
    payload = _payload("plan_approve", "approve:1")
    out = inbound.handle_request(
        _Client(), {"type": "interactive", "envelope_id": "env-i", "payload": payload}
    )
    assert out == payload
    assert received == [payload]
    assert acks == [{"envelope_id": "env-i"}]
