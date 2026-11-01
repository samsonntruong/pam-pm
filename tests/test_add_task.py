"""@Pam add task tests on both backends (fakes only)."""

from __future__ import annotations

from typing import Any

import pytest

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.agents.add_task import (
    approve_add_task,
    handle_add_task_mention,
    parse_add_task,
    post_add_task_draft,
    propose_add_task,
)
from pam_pm.core import store as store_mod
from pam_pm.core.writes import WriteService
from tests.test_both_adapters import _make_board

ROSTER = [
    {"name": "Maya", "skills": "backend api", "capacity": 10.0},
    {"name": "Sam", "skills": "design", "capacity": 8.0},
]


@pytest.fixture(params=["native", "ado-agile"], ids=["native", "ado"])
def board_case(request: Any) -> Any:
    board, _done, _states = _make_board(str(request.param))
    try:
        yield board
    finally:
        board.close()


def test_parse_full_example() -> None:
    parsed = parse_add_task("@Pam add a task: fix login timeout, 3h, assign Maya")
    assert parsed is not None
    assert parsed["title"] == "fix login timeout"
    assert parsed["estimate"] == 3.0
    assert parsed["owner_hint"] == "Maya"


def test_parse_variants() -> None:
    angle = parse_add_task("<@U123> add task fix flaky test, assign Sam")
    assert angle is not None
    assert angle["title"] == "fix flaky test"
    assert angle["estimate"] is None
    assert angle["owner_hint"] == "Sam"
    no_owner = parse_add_task("@pam add a task: write release notes, 2h")
    assert no_owner is not None
    assert no_owner["title"] == "write release notes"
    assert no_owner["estimate"] == 2.0
    assert no_owner["owner_hint"] == ""
    minutes = parse_add_task("add task: quick triage, 30m")
    assert minutes is not None
    assert minutes["estimate"] == 0.5
    assert parse_add_task("@Pam how is the sprint going?") is None
    assert parse_add_task("@Pam add a task: , 3h") is None


def test_owner_names_win(board_case: Any) -> None:
    draft = propose_add_task(
        "@Pam add a task: fix login timeout, 3h, assign Maya", ROSTER, board_case
    )
    assert draft is not None
    assert draft["owner"] == "Maya"
    assert draft["confidence"] == "high"
    assert "named in brief" in draft["reason"]


def test_owner_skill_match_and_load(board_case: Any) -> None:
    skill = propose_add_task(
        "@Pam add a task: design landing page, 3h", ROSTER, board_case
    )
    assert skill is not None
    assert skill["owner"] == "Sam"
    assert "design" in skill["reason"]
    plain = propose_add_task("@Pam add a task: tidy backlog, 1h", ROSTER, board_case)
    assert plain is not None
    assert plain["owner"] in ("Maya", "Sam")
    assert "load balance" in plain["reason"]


def test_missing_owner_flagged(board_case: Any) -> None:
    draft = propose_add_task("@Pam add a task: fix login timeout, 3h", [], board_case)
    assert draft is not None
    assert draft["owner"] is None
    assert draft["needs_owner"] is True
    assert "needs an owner" in draft["reason"]


def test_slack_draft_posts_approve_button(board_case: Any) -> None:
    chat = InMemoryChat()
    result = handle_add_task_mention(
        "@Pam add a task: fix login timeout, 3h, assign Maya",
        ROSTER,
        board_case,
        chat,
        "lead-channel",
    )
    assert result is not None
    assert result["draft"]["owner"] == "Maya"
    posted = chat.dms[-1]
    assert posted["kind"] == "buttons"
    assert "fix login timeout" in posted["text"]
    assert "Maya" in posted["text"]
    assert "3h" in posted["text"]
    labels = [button["label"] for button in posted["buttons"]]
    assert labels == ["Approve", "Cancel"]
    assert posted["buttons"][0]["value"] == result["draft_id"]


def test_unknown_text_posts_nothing(board_case: Any) -> None:
    chat = InMemoryChat()
    assert (
        handle_add_task_mention(
            "@Pam how is the sprint?", ROSTER, board_case, chat, "lead-channel"
        )
        is None
    )
    assert chat.dms == []


def test_approve_creates_on_both_backends(board_case: Any, tmp_path: Any) -> None:
    chat = InMemoryChat()
    result = handle_add_task_mention(
        "@Pam add a task: fix login timeout, 3h, assign Maya",
        ROSTER,
        board_case,
        chat,
        "lead-channel",
    )
    assert result is not None
    db = tmp_path / "add_task.db"
    writes = WriteService(db, dry_run=False)
    created = approve_add_task(
        result["draft_id"], board_case, writes=writes, dry_run=False
    )
    assert len(created) == 1
    assert created[0].title == "fix login timeout"
    assert len(board_case.list_items()) == 1


def test_evidence_and_undo(board_case: Any, tmp_path: Any) -> None:
    draft = propose_add_task(
        "@Pam add a task: fix login timeout, 3h, assign Maya", ROSTER, board_case
    )
    assert draft is not None
    db = tmp_path / "add_task_evidence.db"
    writes = WriteService(db, dry_run=False)
    approve_add_task(draft, board_case, writes=writes, dry_run=False)
    rows = store_mod.list_writes(db)
    assert len(rows) == 1
    assert "fix login timeout" in rows[0].evidence_quote
    assert rows[0].operation == "create_item"
    assert writes.undo(int(rows[0].id or 0)) is True
    assert store_mod.list_writes(db)[0].status == "undone"


def test_approve_unknown_draft_raises(board_case: Any) -> None:
    with pytest.raises(ValueError, match="unknown add-task draft"):
        approve_add_task("add-task-999999", board_case)


def test_console_card_matches_slack_draft() -> None:
    from pam_pm.web.app import _add_task_card_html

    draft = propose_add_task(
        "@Pam add a task: fix login timeout, 3h, assign Maya", ROSTER, None
    )
    assert draft is not None
    card = _add_task_card_html(draft, ROSTER, dry_run=True)
    assert "fix login timeout" in card
    assert "Maya" in card
    assert "3h" in card
    assert "Approve" in card
    assert "Dry run is on" in card
    assert post_add_task_draft  # slack renderer stays imported by the suite
