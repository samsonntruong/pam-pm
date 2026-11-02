"""ADO list_comments parses both comments and value shapes, oldest first."""

from __future__ import annotations

import os
from typing import Any

import httpx

from pam_pm.adapters.pm.azure_devops import ADOAdapter


def _env() -> None:
    os.environ["ADO_ORG_URL"] = "https://dev.azure.com/fake"
    os.environ["ADO_PROJECT"] = "proj"
    os.environ["ADO_TEAM"] = "team"
    os.environ["ADO_PAT"] = "test-pat-value"


def _make_adapter(handler: Any) -> ADOAdapter:
    _env()
    return ADOAdapter(transport=httpx.MockTransport(handler), process="Agile")


def _comment_payload(
    cid: int, text: str, created: str, author: str = "ADO User"
) -> dict[str, Any]:
    return {
        "id": cid,
        "text": text,
        "createdBy": {"displayName": author},
        "createdDate": created,
    }


def test_list_comments_prefers_comments_shape() -> None:
    payload = {
        "totalCount": 2,
        "count": 2,
        "comments": [
            _comment_payload(1, "first note", "2026-01-01T00:00:00Z"),
            _comment_payload(2, "second note", "2026-01-02T00:00:00Z"),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/comments")
        return httpx.Response(200, json=payload)

    adapter = _make_adapter(handler)
    try:
        comments = adapter.list_comments("42")
        assert [c.text for c in comments] == ["first note", "second note"]
        assert [c.id for c in comments] == ["1", "2"]
        assert all(c.item_id == "42" for c in comments)
        assert comments[0].created <= comments[1].created
    finally:
        adapter.close()


def test_list_comments_falls_back_to_value_shape() -> None:
    payload = {
        "count": 2,
        "value": [
            _comment_payload(1, "first note", "2026-01-01T00:00:00Z"),
            _comment_payload(2, "second note", "2026-01-02T00:00:00Z"),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/comments")
        return httpx.Response(200, json=payload)

    adapter = _make_adapter(handler)
    try:
        comments = adapter.list_comments("42")
        assert [c.text for c in comments] == ["first note", "second note"]
        assert comments[0].created <= comments[1].created
    finally:
        adapter.close()


def test_list_comments_prefers_comments_when_both_present() -> None:
    payload = {
        "count": 1,
        "totalCount": 1,
        "comments": [_comment_payload(7, "live note", "2026-01-03T00:00:00Z")],
        "value": [_comment_payload(9, "stale note", "2026-01-01T00:00:00Z")],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    adapter = _make_adapter(handler)
    try:
        comments = adapter.list_comments("42")
        assert [c.text for c in comments] == ["live note"]
    finally:
        adapter.close()
