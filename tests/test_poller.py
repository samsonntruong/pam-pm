"""ADOPoller watermark behavior with fake board."""

from __future__ import annotations

import time

from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.rules.poller import POLL_INTERVAL, ADOPoller, build_changed_wiql


def test_watermark_advances(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        first = board.create_item("One", {"state": "Doing"})
        time.sleep(0.02)
        board.create_item("Two", {"state": "Doing"})
        db_path = tmp_path / "poll.db"
        poller = ADOPoller(board, db_path, project="p1")
        assert poller.interval == POLL_INTERVAL == 60
        assert "ORDER BY [System.ChangedDate] ASC" in poller.build_wiql()
        assert "ORDER BY [System.ChangedDate] ASC" in build_changed_wiql(first.changed)
        got = poller.poll_once()
        assert len(got) == 2
        mark = poller.get_watermark()
        assert mark
        time.sleep(0.02)
        third = board.create_item("Three", {"state": "Doing"})
        again = poller.poll_once()
        assert [item.id for item in again] == [third.id]
        assert poller.get_watermark() >= mark
    finally:
        board.close()


def test_no_duplicates_on_second_poll(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        board.create_item("One", {"state": "Doing"})
        db_path = tmp_path / "poll.db"
        poller = ADOPoller(board, db_path, project="p1")
        assert len(poller.poll_once()) == 1
        assert poller.poll_once() == []
        other = ADOPoller(board, db_path, project="p1")
        assert other.get_watermark() == poller.get_watermark()
        assert other.poll_once() == []
    finally:
        board.close()
