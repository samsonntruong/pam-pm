"""Conversational blocker intake: match real item, clarify, confirm, plan."""

from __future__ import annotations

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.agents.interviewer import resume_interview
from pam_pm.core import recovery as recovery_mod

LEO_REPORT = (
    "I'm blocked. Refresh tokens are returning a 500, "
    "so I can't finish the session timeout work."
)


def _seed_board(board):
    for i in range(5):
        board.create_item(f"Filler {i + 1}", {"state": "Done"})
    leo_item = board.create_item(
        "Session timeout", {"state": "Active", "assigned_to": "Leo"}
    )
    maya_item = board.create_item(
        "Refresh tokens", {"state": "Active", "assigned_to": "Maya"}
    )
    return leo_item, maya_item


def _seed_history(db, person, item_id):
    key = recovery_mod.thread_key(person, item_id)
    recovery_mod.save_conversation(
        db, key, person, item_id,
        [{"role": "user", "content": "working on it"}],
        "", "finished",
    )


def _setup(tmp_path):
    board = SQLiteBoard()
    leo_item, maya_item = _seed_board(board)
    db = str(tmp_path / "conv.db")
    _seed_history(db, "Leo", leo_item.id)
    chat = InMemoryChat()
    return board, chat, db, leo_item, maya_item


def test_blocker_matches_real_item(tmp_path) -> None:
    board, chat, db, leo_item, maya_item = _setup(tmp_path)
    try:
        out = resume_interview("Leo", LEO_REPORT, chat, board, None, db=db)
        assert out["kind"] == "blocker_clarify"
        assert out["item_id"] == leo_item.id
        assert out["blocking_item_id"] == maya_item.id
        assert len(board.list_items(limit=200)) == 7
    finally:
        board.close()


def test_clarifying_question_precedes_link(tmp_path) -> None:
    board, chat, db, leo_item, maya_item = _setup(tmp_path)
    try:
        first = resume_interview("Leo", LEO_REPORT, chat, board, None, db=db)
        assert first["kind"] == "blocker_clarify"
        assert len(chat.dms) == 1
        q = chat.dms[0]["text"]
        assert "Session timeout" in q
        assert "Refresh tokens" in q
        assert list(board.list_links(leo_item.id) or []) == []
        chat.script_replies("Maya", ["Token service crashes", "keep", "1h, 1h, 1h", "yes"])
        second = resume_interview("Leo", "yes, that is right", chat, board, None, db=db)
        assert second["kind"] == "blocker_linked"
        links = list(board.list_links(leo_item.id) or [])
        assert len(links) == 1
        assert links[0].source_id == maya_item.id
        assert links[0].target_id == leo_item.id
    finally:
        board.close()


def test_owner_conversation_starts_after_confirmation(tmp_path) -> None:
    board, chat, db, _leo_item, _ = _setup(tmp_path)
    try:
        resume_interview("Leo", LEO_REPORT, chat, board, None, db=db)
        assert [d for d in chat.dms if d["user_id"] == "Maya"] == []
        chat.script_replies(
            "Maya",
            ["Token service null on refresh", "keep", "2h, 1h, 1h", "yes"],
        )
        out = resume_interview("Leo", "yes", chat, board, None, db=db)
        assert out["kind"] == "blocker_linked"
        maya_dms = [d["text"] for d in chat.dms if d["user_id"] == "Maya"]
        assert len(maya_dms) == 4
        assert "?" in maya_dms[0]
        assert out["plan"]["status"] == "confirmed"
        assert len(out["plan"]["children"]) == 3
    finally:
        board.close()


def test_waiter_gets_eta_at_end(tmp_path) -> None:
    board, chat, db, _leo_item, _ = _setup(tmp_path)
    try:
        resume_interview("Leo", LEO_REPORT, chat, board, None, db=db)
        chat.script_replies("Maya", ["Expired cert", "keep", "1h, 1h, 1h", "yes"])
        out = resume_interview("Leo", "yes", chat, board, None, db=db)
        assert out["eta"]
        leo_texts = [d["text"] for d in chat.dms if d["user_id"] == "Leo"]
        assert len(leo_texts) >= 2
        last = leo_texts[-1]
        assert out["eta"] in last
        assert out["plan"]["summary"] in last
    finally:
        board.close()
