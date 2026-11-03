"""Role-aware blocker matching boost with fake boards only."""

from __future__ import annotations

from pam_pm.agents import interviewer as iv

REPORT = (
    "PamTest: Auth token refresh is failing, "
    "so I can't finish the PamTest: Session timeout work"
)


class _FakeItem:
    def __init__(self, item_id, title, state, owner):
        self.id = item_id
        self.title = title
        self.state = state
        self.assigned_to = owner


class _FakeBoard:
    def __init__(self, items):
        self._items = items

    def list_items(self, limit=200):
        return list(self._items)[:limit]


class _ADOItem:
    def __init__(self, item_id, title, state, display_name):
        self.id = item_id
        self.title = title
        self.state = state
        self.fields = {"System.AssignedTo": {"displayName": display_name}}


def _base_scores():
    tokens = iv._blocker_tokens(REPORT)
    auth = iv._title_score("PamTest: Auth token refresh", tokens)
    sess = iv._title_score("PamTest: Session timeout", tokens)
    return auth, sess


def test_single_owner_blocked_prefers_session_timeout() -> None:
    auth, sess = _base_scores()
    assert auth > sess
    board = _FakeBoard([
        _FakeItem("auth-1", "PamTest: Auth token refresh", "Active", "Leo"),
        _FakeItem("sess-1", "PamTest: Session timeout", "Active", "Leo"),
    ])
    matched = iv._match_blocked_item(REPORT, ["Leo"], board, None)
    assert matched is not None
    assert matched.id == "sess-1"


def test_single_owner_blocking_prefers_auth_refresh() -> None:
    board = _FakeBoard([
        _FakeItem("auth-1", "PamTest: Auth token refresh", "Active", "Leo"),
        _FakeItem("sess-1", "PamTest: Session timeout", "Active", "Leo"),
    ])
    blocked = iv._match_blocked_item(REPORT, ["Leo"], board, None)
    assert blocked is not None
    matched = iv._match_blocking_item(REPORT, board, blocked.id, ["Leo"])
    assert matched is not None
    assert matched.id == "auth-1"


def test_two_owner_ado_shaped_unchanged() -> None:
    report = (
        "I'm blocked. Refresh tokens are returning a 500, "
        "so I can't finish the session timeout work."
    )
    board = _FakeBoard([
        _ADOItem("5", "Refresh tokens", "Active", "Maya Pam"),
        _ADOItem("6", "Session timeout", "Active", "Leo Pam"),
    ])
    blocked = iv._match_blocked_item(report, ["Leo Pam"], board, None)
    assert blocked is not None
    assert blocked.id == "6"
    blocking = iv._match_blocking_item(report, board, "6", ["Leo Pam"])
    assert blocking is not None
    assert blocking.id == "5"
