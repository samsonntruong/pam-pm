"""ADO link rel aliases across PM adapters plus honest blocker link failure."""

from __future__ import annotations

import pytest

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.asana import REL_BLOCKED_BY as ASANA_BLOCKED_BY
from pam_pm.adapters.pm.asana import REL_BLOCKS as ASANA_BLOCKS
from pam_pm.adapters.pm.asana import AsanaAdapter
from pam_pm.adapters.pm.clickup import REL_BLOCKED_BY as CLICKUP_BLOCKED_BY
from pam_pm.adapters.pm.clickup import REL_BLOCKS as CLICKUP_BLOCKS
from pam_pm.adapters.pm.clickup import ClickUpAdapter
from pam_pm.adapters.pm.jira import REL_BLOCKED_BY as JIRA_BLOCKED_BY
from pam_pm.adapters.pm.jira import REL_BLOCKS as JIRA_BLOCKS
from pam_pm.adapters.pm.jira import JiraAdapter
from pam_pm.adapters.pm.linear import REL_BLOCKED_BY as LINEAR_BLOCKED_BY
from pam_pm.adapters.pm.linear import REL_BLOCKS as LINEAR_BLOCKS
from pam_pm.adapters.pm.linear import LinearAdapter
from pam_pm.adapters.pm.monday import REL_BLOCKED_BY as MONDAY_BLOCKED_BY
from pam_pm.adapters.pm.monday import REL_BLOCKS as MONDAY_BLOCKS
from pam_pm.adapters.pm.monday import MondayAdapter
from pam_pm.adapters.pm.trello import REL_BLOCKED_BY as TRELLO_BLOCKED_BY
from pam_pm.adapters.pm.trello import REL_BLOCKS as TRELLO_BLOCKS
from pam_pm.adapters.pm.trello import TrelloAdapter
from pam_pm.agents import interviewer as iv
from pam_pm.agents.interviewer import resume_interview

FORWARD = "System.LinkTypes.Dependency-Forward"
REVERSE = "System.LinkTypes.Dependency-Reverse"

ADAPTER_CASES = [
    (TrelloAdapter, TRELLO_BLOCKS, TRELLO_BLOCKED_BY),
    (LinearAdapter, LINEAR_BLOCKS, LINEAR_BLOCKED_BY),
    (MondayAdapter, MONDAY_BLOCKS, MONDAY_BLOCKED_BY),
    (JiraAdapter, JIRA_BLOCKS, JIRA_BLOCKED_BY),
    (AsanaAdapter, ASANA_BLOCKS, ASANA_BLOCKED_BY),
    (ClickUpAdapter, CLICKUP_BLOCKS, CLICKUP_BLOCKED_BY),
]


@pytest.mark.parametrize("adapter_cls,blocks,blocked_by", ADAPTER_CASES)
def test_normalize_rel_maps_ado_strings(adapter_cls, blocks, blocked_by) -> None:
    assert adapter_cls._normalize_rel(FORWARD) == blocks
    assert adapter_cls._normalize_rel(REVERSE) == blocked_by
    assert adapter_cls._normalize_rel(FORWARD.lower()) == blocks
    assert adapter_cls._normalize_rel(REVERSE.lower()) == blocked_by


class _FailingLinkBoard:
    def get_item(self, item_id: str) -> dict:
        return {"id": item_id, "fields": {"tags": []}}

    def update_item(self, item_id: str, patch: dict) -> dict:
        return {"id": item_id}

    def add_link(self, source_id: str, target_id: str, rel: str) -> dict:
        raise RuntimeError("link backend down")


def test_blocker_confirm_link_failure_is_honest(tmp_path) -> None:
    chat = InMemoryChat()
    board = _FailingLinkBoard()
    db = str(tmp_path / "linkfail.db")
    iv._save_pending_blocker(db, "Leo", {
        "blocked_id": "blocked-1",
        "blocked_title": "Session timeout",
        "blocking_id": "blocking-2",
        "blocking_title": "Refresh tokens",
        "report": "tokens return 500",
    })
    out = resume_interview("Leo", "yes, that is right", chat, board, None, db=db)
    assert out["kind"] == "blocker_link_failed"
    assert out["error"] == "link backend down"
    assert out["item_id"] == "blocked-1"
    assert out["blocking_item_id"] == "blocking-2"
    texts = [d["text"] for d in chat.dms if d["user_id"] == "Leo"]
    assert texts, "expected a failure DM to Leo"
    assert all(not t.startswith("Linked ") for t in texts)
    assert "did not stick" in texts[-1]
