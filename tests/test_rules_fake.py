"""M2a rules templates with fake adapters only."""

from __future__ import annotations

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.core import store
from pam_pm.core.writes import WriteService
from pam_pm.rules.templates.blocker import blocker_intake, on_blocker_closed
from pam_pm.rules.templates.follow_up import on_item_done
from pam_pm.rules.templates.morning import morning_checkin


class _FakeModel:
    def chat(self, messages, tools=None):
        raise AssertionError("model should not run when nothing is active")


def test_morning_skips_empty(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        chat = InMemoryChat()
        writes = WriteService(tmp_path / "state.db", dry_run=True)
        out = morning_checkin({"db": tmp_path / "s.db"}, chat, board, _FakeModel())
        assert out == {}
        assert chat.dms == []
        done = board.create_item("Old task", {"state": "Done"})
        assert done.state == "Done"
        out = morning_checkin("proj", chat, board, _FakeModel(), writes=writes)
        assert out == {}
        assert chat.dms == []
    finally:
        board.close()


def test_follow_up_triggers_on_close() -> None:
    board = SQLiteBoard()
    try:
        chat = InMemoryChat()
        first = board.create_item("Step one", {"state": "Doing"})
        nxt = board.create_item(
            "Step two", {"state": "To Do", "assigned_to": "U9"}
        )
        board.add_link(first.id, nxt.id, "System.LinkTypes.Dependency-Forward")
        doing = board.get_item(first.id)
        assert on_item_done(doing, chat, board) == []
        assert chat.dms == []
        board.update_item(first.id, {"state": "Done"})
        closed = board.get_item(first.id)
        sent = on_item_done(closed, chat, board)
        assert len(sent) == 1
        assert chat.dms[0]["user_id"] == "U9"
        assert "status" in chat.dms[0]["text"].lower()
    finally:
        board.close()


def test_blocker_creates_link_and_notifies_on_clear(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        chat = InMemoryChat()
        blocked = board.create_item(
            "Ship report", {"state": "Doing", "assigned_to": "U1"}
        )
        chat.script_replies("U1", ["Report blocked", "U2", "Misses deadline"])
        db_path = tmp_path / "state.db"
        writes = WriteService(db_path, dry_run=True)
        result = blocker_intake(
            "U1", chat, board, writes, blocked_item_id=blocked.id
        )
        blocking_id = str(result["blocking_item"].id)
        assert blocking_id != blocked.id
        links = board.list_links(blocked.id)
        assert len(links) == 1
        assert links[0].source_id == blocking_id
        assert links[0].target_id == blocked.id
        assert links[0].rel == "System.LinkTypes.Dependency-Forward"
        eta = [dm for dm in chat.dms if dm["user_id"] == "U2"]
        assert len(eta) == 1
        assert "eta" in eta[0]["text"].lower()
        rows = store.list_writes(db_path, status="proposed")
        assert len(rows) >= 1
        assert all(row.evidence_quote for row in rows)
        board.update_item(blocking_id, {"state": "Done"})
        cleared = board.get_item(blocking_id)
        dm_id = on_blocker_closed(cleared, "U1", chat)
        assert dm_id
        last = chat.dms[-1]
        assert last["user_id"] == "U1"
        assert "clear" in last["text"].lower()
    finally:
        board.close()
