"""Blocker reported with no open check-in: link and flag, never shrug."""

from __future__ import annotations

import json
from typing import Any

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.agents.interviewer import resume_interview

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


def test_blocker_without_open_state_links_and_flags(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        item = board.create_item(
            "Token change", {"state": "Doing", "assigned_to": "Maya"}
        )
        chat = InMemoryChat()
        db = str(tmp_path / "blocker.db")
        outcome = resume_interview("Maya", MAYA_MESSAGE, chat, board, None, db=db)
        assert outcome["kind"] == "blocker_no_state"
        assert outcome["replied"] is True
        assert outcome["item_id"] == item.id
        assert len(chat.dms) == 1
        reply = chat.dms[0]["text"]
        assert "no open check-in" not in reply.lower()
        assert "block" in reply.lower()
        links = list(board.list_links(item.id) or [])
        assert links, "blocker report must link the open item"
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
