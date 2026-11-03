"""Trello 429 retry tests with fake transport only."""

from __future__ import annotations

import time

import httpx
import pytest

from pam_pm.adapters.pm import trello as trello_mod
from pam_pm.adapters.pm.trello import TrelloAdapter, TrelloError


def _make_adapter(monkeypatch, handler):
    monkeypatch.setattr(
        "pam_pm.core.secrets.effective_trello_config",
        lambda data_dir=None: {
            "board": "test-board",
            "api_key": "fake-key",
            "token": "fake-token",
        },
    )
    transport = httpx.MockTransport(handler)
    adapter = TrelloAdapter(board="test-board", transport=transport)
    return adapter


def test_retries_429_then_succeeds(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) <= 2:
            return httpx.Response(
                429, headers={"Retry-After": "0"}, text="rate limited"
            )
        return httpx.Response(200, json={"ok": True, "id": "card1"})

    adapter = _make_adapter(monkeypatch, handler)
    sleeps: list[float] = []
    adapter._sleeper = sleeps.append
    monkeypatch.setattr(trello_mod, "_sleep", sleeps.append)
    monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("real sleep")))

    try:
        result = adapter._request("GET", "/cards/test")
    finally:
        adapter.close()

    assert result == {"ok": True, "id": "card1"}
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_five_429s_raise(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "0"}, text="rate limited")

    adapter = _make_adapter(monkeypatch, handler)
    sleeps: list[float] = []
    adapter._sleeper = sleeps.append
    monkeypatch.setattr(trello_mod, "_sleep", sleeps.append)
    monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("real sleep")))

    try:
        with pytest.raises(TrelloError):
            adapter._request("GET", "/cards/test")
    finally:
        adapter.close()

    assert len(calls) == 5
    assert len(sleeps) == 4
