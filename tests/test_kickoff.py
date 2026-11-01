"""Kickoff from a document tests on both backends (fakes only)."""

from __future__ import annotations

from typing import Any

import pytest

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.base import PMAdapter, WorkItem
from pam_pm.adapters.pm.sqlite_board import NativeBoard
from pam_pm.agents.kickoff import (
    approve_kickoff,
    handle_owner_response,
    intake_slack_file,
    notify_owners,
    parse_owner_reply,
    propose_kickoff,
)
from pam_pm.core import store as store_mod

ROSTER = [
    {"name": "Maya", "skills": "backend api", "capacity": 10.0},
    {"name": "Sam", "skills": "design", "capacity": 8.0},
]


class StubAdoBoard(PMAdapter):
    """Minimal ADO-shaped fake: Scrum states, in-memory items."""

    supports_dependency_links = True
    supports_iterations = True
    supports_comments = True

    def __init__(self) -> None:
        self._items: dict[str, WorkItem] = {}
        self._next = 1

    def close(self) -> None:
        return None

    def list_items(self, limit: int = 50) -> list[WorkItem]:
        return list(self._items.values())[:limit]

    def get_item(self, item_id: str) -> WorkItem:
        return self._items[item_id]

    def update_item(self, item_id: str, fields: dict[str, Any]) -> WorkItem:
        item = self._items[item_id]
        item.fields.update(fields)
        return item

    def create_item(self, title: str, fields: dict[str, Any] | None = None) -> WorkItem:
        item_id = str(self._next)
        self._next += 1
        item = WorkItem(
            id=item_id,
            title=title,
            state=str((fields or {}).get("state", "To Do")),
            fields=dict(fields or {}),
        )
        self._items[item_id] = item
        return item

    def delete_item(self, item_id: str) -> None:
        del self._items[item_id]

    def list_states(self) -> list[str]:
        return ["To Do", "In Progress", "Done"]

    def close_item(self, item_id: str) -> WorkItem:
        item = self._items[item_id]
        item.state = "Done"
        return item

    def list_iterations(self) -> list:
        return []

    def create_iteration(self, name: str, start_date: str = "", end_date: str = "") -> Any:
        raise NotImplementedError

    def set_item_iteration(self, item_id: str, iteration_id: str) -> WorkItem:
        return self._items[item_id]

    def add_link(self, source_id: str, target_id: str, rel: str) -> Any:
        raise NotImplementedError

    def list_links(self, item_id: str) -> list:
        return []

    def list_comments(self, item_id: str) -> list:
        return []

    def add_comment(self, item_id: str, text: str, author: str = "") -> Any:
        raise NotImplementedError

    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        return []


@pytest.fixture(params=["native", "ado"], ids=["native", "ado"])
def board(request: Any) -> Any:
    if request.param == "native":
        adapter: Any = NativeBoard()
    else:
        adapter = StubAdoBoard()
    try:
        yield adapter
    finally:
        adapter.close()


def test_names_win_and_skill_match_with_reasons(board: Any) -> None:
    text = "Maya: build login api, 4h\nDesign landing page, 3h"
    result = propose_kickoff(text, ROSTER, board)
    assert result["has_history"] is False
    items = result["items"]
    assert items[0]["owner"] == "Maya"
    assert items[0]["confidence"] == "high"
    assert "named in brief" in items[0]["reason"]
    assert items[1]["owner"] == "Sam"
    assert "design" in items[1]["reason"]


def test_unassigned_flagged_when_no_roster(board: Any) -> None:
    result = propose_kickoff("Fix login timeout, 3h", [], board)
    assert result["items"][0]["owner"] is None
    assert result["items"][0]["needs_owner"] is True
    assert "needs an owner" in result["items"][0]["reason"]


def test_over_capacity_warns(board: Any) -> None:
    roster = [{"name": "Maya", "skills": "", "capacity": 2.0}]
    result = propose_kickoff("Big task, 8h", roster, board)
    assert result["items"][0]["warning"] is True
    assert "over capacity" in result["items"][0]["reason"]


def test_approve_creates_items(board: Any) -> None:
    result = propose_kickoff("Login form, 2h\nAPI keys, 3h", ROSTER, board)
    created = approve_kickoff(result["items"], board)
    assert [item.title for item in created] == ["Login form", "API keys"]
    assert len(board.list_items()) == 2


def test_slack_file_intake_and_owner_notify(board: Any) -> None:
    chat = InMemoryChat()
    chat.download_file = lambda file_id: b"Maya: ship api, 2h"  # type: ignore[method-assign]
    result = intake_slack_file(chat, "file-1", ROSTER, board)
    assert result["items"][0]["owner"] == "Maya"
    created = approve_kickoff(result["items"], board)
    sent = notify_owners(chat, created, result["items"])
    assert sent and sent[0]["owner"] == "Maya"
    assert "Accept or flag" in chat.dms[-1]["text"]


def test_accept_and_flag_flow() -> None:
    assert parse_owner_reply("Yes, I accept") == "accept"
    assert parse_owner_reply("Flag: I am blocked") == "flag"
    assert handle_owner_response("1", "looks good, accept")["status"] == "accepted"
    assert handle_owner_response("2", "flag, wrong owner")["status"] == "flagged"


def test_proposals_keyed_by_backend_and_external_id(tmp_path: Any) -> None:
    db = tmp_path / "kickoff.db"
    store_mod.save_kickoff_proposal(
        db, "pam_board", "brief-1", "atlas", {"items": [{"title": "A"}]}
    )
    store_mod.save_kickoff_proposal(
        db, "azure_devops", "brief-1", "atlas", {"items": [{"title": "B"}]}
    )
    native = store_mod.get_kickoff_proposal(db, "pam_board", "brief-1")
    ado = store_mod.get_kickoff_proposal(db, "azure_devops", "brief-1")
    assert native and native["payload"]["items"][0]["title"] == "A"
    assert ado and ado["payload"]["items"][0]["title"] == "B"
