"""M1b fake loop: seed board, script vague chat reply, propose write."""

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.core import store
from pam_pm.core.writes import WriteService


def test_fake_loop_proposes_write_with_evidence(tmp_path):
    board = SQLiteBoard()
    try:
        first = board.create_item("Fix morning sync query", {"state": "Doing"})
        second = board.create_item("Draft blocker follow-up", {"state": "To Do"})
        assert first.id != second.id
        assert len(board.list_items()) == 2

        chat = InMemoryChat()
        user_id = "U123"
        chat.send_dm(user_id, "Quick check: where is Fix morning sync query?")
        vague = "Still working on it, almost done I think, had a late start."
        chat.script_replies(user_id, [vague])
        replies = chat.read_replies(user_id)
        assert len(replies) == 1
        quote = replies[0].text.strip()
        assert quote == vague

        db_path = tmp_path / "state.db"
        writes = WriteService(db_path)
        write_id = writes.propose(
            {
                "operation": "update_state",
                "item_id": first.id,
                "evidence_quote": quote,
                "status": "proposed",
                "detail": "interview answer recorded from chat",
            }
        )
        assert write_id > 0
        rows = store.list_writes(db_path, status="proposed")
        assert len(rows) == 1
        assert rows[0].item_id == first.id
        assert rows[0].evidence_quote == vague
        assert "almost done" in rows[0].evidence_quote
        assert chat.dms and chat.dms[0]["user_id"] == user_id
    finally:
        board.close()
