"""Trello list_links reverse direction uses one board actions call."""

from __future__ import annotations

import httpx
import pytest

from pam_pm.adapters.pm.trello import TrelloAdapter

TRELLO_BOARD = "Test Board"
TRELLO_KEY = "trello-test-key-1"
TRELLO_TOKEN = "trello-test-token-1"


@pytest.fixture()
def trello_env(monkeypatch):
    monkeypatch.setenv("TRELLO_BOARD", TRELLO_BOARD)
    monkeypatch.setenv("TRELLO_API_KEY", TRELLO_KEY)
    monkeypatch.setenv("TRELLO_TOKEN", TRELLO_TOKEN)


def test_list_links_two_calls_both_directions(trello_env) -> None:
    cards = [f"card{i}" for i in range(1, 6)]
    by_ref = {}
    short_of = {}
    for i, cid in enumerate(cards, start=1):
        short = f"sl{i}"
        by_ref[cid] = cid
        by_ref[short] = cid
        short_of[cid] = short

    own_attachments = [
        {"id": "att1", "url": "https://trello.com/c/sl2/target", "name": "blocks card2"},
        {"id": "att2", "url": "https://example.com/not-a-card", "name": "note"},
    ]
    board_actions = [
        {
            "id": "a1",
            "type": "addAttachmentToCard",
            "date": "2026-02-01T00:00:01Z",
            "data": {
                "card": {"id": "card3"},
                "attachment": {
                    "id": "att9",
                    "url": "https://trello.com/c/sl1/source",
                    "name": "blocks card1",
                },
            },
        },
        {
            "id": "a2",
            "type": "addAttachmentToCard",
            "date": "2026-02-01T00:00:02Z",
            "data": {
                "card": {"id": "card4"},
                "attachment": {"id": "att10", "url": "https://example.com/x"},
            },
        },
        {"id": "a3", "type": "createCard", "data": {"card": {"id": "card5"}}},
    ]

    calls: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/cards/card1/attachments") and request.method == "GET":
            return httpx.Response(200, json=list(own_attachments))
        if path.endswith("/boards/b1/actions") and request.method == "GET":
            assert request.url.params.get("filter") == "addAttachmentToCard"
            assert request.url.params.get("limit")
            return httpx.Response(200, json=list(board_actions))
        raise AssertionError(f"unexpected call: {request.method} {path}")

    adapter = TrelloAdapter(transport=httpx.MockTransport(handle))
    try:
        adapter._board_id = "b1"
        adapter._known_cards = lambda: (dict(by_ref), dict(short_of))  # type: ignore[method-assign]
        links = adapter.list_links("card1")
    finally:
        adapter.close()

    assert len(calls) == 2
    assert calls[0] == ("GET", "/1/cards/card1/attachments")
    assert calls[1] == ("GET", "/1/boards/b1/actions")
    got = {(e.source_id, e.target_id, e.rel) for e in links}
    assert ("card1", "card2", "blocks") in got
    assert ("card1", "card3", "is blocked by") in got
    assert len(got) == 2
