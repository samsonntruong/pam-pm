"""Jira Blocks link direction: outward is blocked, inward is blocking."""

from __future__ import annotations

from typing import Any

from pam_pm.adapters.pm.jira import REL_BLOCKS, JiraAdapter


def test_blocks_link_stores_outward_blocked_inward_blocking() -> None:
    posted: list[dict[str, Any]] = []

    def _request(self: Any, method: str, path: str, payload: Any = None, params: Any = None) -> Any:
        if path == "/rest/api/3/issueLink":
            posted.append(payload)
            return {}
        raise AssertionError(f"unexpected call {method} {path}")

    adapter = JiraAdapter.__new__(JiraAdapter)
    adapter._request = _request.__get__(adapter, JiraAdapter)  # type: ignore[method-assign]
    adapter.add_link("BLK-1", "BLD-2", "System.LinkTypes.Dependency-Forward")
    assert posted, "expected the link POST"
    body = posted[-1]
    assert body["outwardIssue"] == {"key": "BLD-2"}
    assert body["inwardIssue"] == {"key": "BLK-1"}
    assert body["type"] == {"name": "Blocks"}
    assert REL_BLOCKS == "blocks"
