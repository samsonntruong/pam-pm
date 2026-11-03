"""Exact short title beats longer title containing it."""

from __future__ import annotations

from pam_pm.agents import interviewer as iv

SHORT_TITLE = "PamTest: Session timeout"
LONG_TITLE = "Fix Token service outage for 'PamTest: Session timeout' followup"
REPORT = "Blocked on PamTest Session timeout, cannot proceed with checkin"


class _FakeItem:
    def __init__(self, item_id, title, state, display_name):
        self.id = item_id
        self.title = title
        self.state = state
        self.fields = {"System.AssignedTo": {"displayName": display_name}}


class _FakeBoard:
    def __init__(self, items):
        self._items = items

    def list_items(self, limit=200):
        return list(self._items)[:limit]


def test_blocked_matcher_prefers_exact_short_title() -> None:
    items = [
        _FakeItem("long-1", LONG_TITLE, "Active", "Leo"),
        _FakeItem("short-1", SHORT_TITLE, "Active", "Leo"),
    ]
    board = _FakeBoard(items)
    matched = iv._match_blocked_item(REPORT, ["Leo"], board, None)
    assert matched is not None
    assert matched.id == "short-1"


def test_blocking_matcher_prefers_exact_short_title() -> None:
    items = [
        _FakeItem("blocked-1", "Leo current work", "Active", "Leo"),
        _FakeItem("long-2", LONG_TITLE, "Active", "Maya"),
        _FakeItem("short-2", SHORT_TITLE, "Active", "Maya"),
    ]
    board = _FakeBoard(items)
    matched = iv._match_blocking_item(REPORT, board, "blocked-1", ["Leo"])
    assert matched is not None
    assert matched.id == "short-2"
