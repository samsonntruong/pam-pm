"""No-history blocker: match owned item, ask first, orphan still creates."""

from __future__ import annotations

import json
from typing import Any

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.agents.interviewer import resume_interview

LEO_REPORT = (
    "I'm blocked. Refresh tokens are returning a 500, "
    "so I can't finish the session timeout work."
)

MAYA_MESSAGE = (
    "Blocked: waiting on security review for the token change, "
    "can't proceed without it"
)


def _fields_of(board: Any, item_id: str) -> dict[str, Any]:
    stored = board.get_item(item_id)
    if isinstance(stored, dict):
        fields = stored.get("fields", {})
        return dict(fields) if isinstance(fields, dict) else {}
    fields = getattr(stored, "fields", {})
    return dict(fields) if isinstance(fields, dict) else {}


def _flagged(board: Any, item_id: str) -> bool:
    blob = json.dumps(_fields_of(board, item_id), default=str).lower()
    if "block" in blob:
        return True
    for meth in ("list_comments", "comments", "get_comments"):
        fn = getattr(board, meth, None)
        if not callable(fn):
            continue
        try:
            comments = fn(item_id)
        except TypeError:
            try:
                comments = fn()
            except Exception:  # noqa: BLE001, S112
                continue
        except Exception:  # noqa: BLE001, S112
            continue
        if "block" in json.dumps(comments or [], default=str).lower():
            return True
    return False


def test_no_history_blocker_matches_assigned_item_and_asks_first(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        for i in range(5):
            board.create_item(f"Filler {i + 1}", {"state": "Done"})
        leo_item = board.create_item(
            "Session timeout", {"state": "Active", "assigned_to": "Leo"}
        )
        maya_item = board.create_item(
            "Refresh tokens", {"state": "Active", "assigned_to": "Maya"}
        )
        chat = InMemoryChat()
        db = str(tmp_path / "nohist.db")
        out = resume_interview("Leo", LEO_REPORT, chat, board, None, db=db)
        assert out["kind"] == "blocker_clarify"
        assert out["item_id"] == leo_item.id
        assert out["blocking_item_id"] == maya_item.id
        assert len(chat.dms) == 1
        q = chat.dms[0]["text"]
        assert "Session timeout" in q
        assert "Refresh tokens" in q
        assert list(board.list_links(leo_item.id) or []) == []
        assert len(board.list_items(limit=200)) == 7
    finally:
        board.close()


def test_no_history_blocker_links_and_plans_after_confirm(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        item = board.create_item(
            "Token change", {"state": "Doing", "assigned_to": "Maya"}
        )
        chat = InMemoryChat()
        db = str(tmp_path / "blocker.db")
        first = resume_interview("Maya", MAYA_MESSAGE, chat, board, None, db=db)
        assert first["kind"] == "blocker_clarify"
        assert first["item_id"] == item.id
        assert len(chat.dms) == 1
        assert "block" in chat.dms[0]["text"].lower()
        assert list(board.list_links(item.id) or []) == []
        second = resume_interview("Maya", "yes, that is right", chat, board, None, db=db)
        assert second["kind"] == "blocker_linked"
        assert second["item_id"] == item.id
        links = list(board.list_links(item.id) or [])
        assert links, "confirm must link the open item"
        titles = []
        for entry in board.list_items(limit=200):
            if isinstance(entry, dict):
                titles.append(str(entry.get("title", "")))
            else:
                titles.append(str(getattr(entry, "title", "")))
        assert any("blocking" in title.lower() for title in titles)
        assert _flagged(board, item.id), "open item must carry a blocker flag"
    finally:
        board.close()


def test_true_orphan_still_creates_after_confirm(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        other = board.create_item(
            "Other work", {"state": "Active", "assigned_to": "Sam"}
        )
        _ = other
        chat = InMemoryChat()
        db = str(tmp_path / "orphan.db")
        before = len(board.list_items(limit=200))
        first = resume_interview(
            "Zoe", "Blocked: waiting on VPN access, cannot proceed",
            chat, board, None, db=db,
        )
        assert first["kind"] == "blocker_clarify"
        assert first["item_id"] == ""
        assert len(chat.dms) == 1
        assert len(board.list_items(limit=200)) == before
        second = resume_interview("Zoe", "yes", chat, board, None, db=db)
        assert second["kind"] == "blocker_linked"
        assert second["item_id"]
        assert second["blocking_item_id"]
        assert len(board.list_items(limit=200)) == before + 2
    finally:
        board.close()


def test_plain_chatter_keeps_polite_reply(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        chat = InMemoryChat()
        db = str(tmp_path / "chatter.db")
        outcome = resume_interview(
            "U9", "lunch was great today", chat, board, None, db=db
        )
        assert outcome["kind"] == "no_open_state"
        assert outcome["replied"] is True
        assert len(chat.dms) == 1
        assert "no open check-in" in chat.dms[0]["text"]
    finally:
        board.close()
