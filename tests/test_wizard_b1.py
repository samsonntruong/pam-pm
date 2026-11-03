"""Wizard B1: Trello, ClickUp, Asana live checks with fake adapters.

No network, no real secrets. Mirrors tests/test_wizard_secrets.py setup.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

SETUP_TOKEN = "wizard-test-setup-token-1"

TRELLO_BOARD = "B1 Board"
TRELLO_KEY = "b1-fake-trello-key-1"
TRELLO_TOKEN = "b1-fake-trello-token-1"

CLICKUP_LIST = "B1 Sprint list"
CLICKUP_TOKEN = "pk_b1_fake_clickup_token_1"

ASANA_PROJECT = "B1 Project"
ASANA_TOKEN = "b1-fake-asana-token-1"

ENV_KEYS = (
    "TRELLO_BOARD",
    "TRELLO_API_KEY",
    "TRELLO_TOKEN",
    "CLICKUP_TEAM",
    "CLICKUP_LIST",
    "CLICKUP_LIST_ID",
    "CLICKUP_API_TOKEN",
    "ASANA_PROJECT",
    "ASANA_PROJECT_ID",
    "ASANA_API_TOKEN",
)


@pytest.fixture()
def b1_client(tmp_path, monkeypatch, caplog):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("PAM_SETUP_TOKEN", SETUP_TOKEN)
    import pam_pm.web.app as app_mod

    app_mod.SETUP_DONE = False
    app_mod.TRELLO_CHECK_FACTORY = None
    app_mod.CLICKUP_CHECK_FACTORY = None
    app_mod.ASANA_CHECK_FACTORY = None
    caplog.set_level(logging.INFO)
    client = TestClient(app_mod.app)
    yield client, tmp_path, app_mod
    app_mod.TRELLO_CHECK_FACTORY = None
    app_mod.CLICKUP_CHECK_FACTORY = None
    app_mod.ASANA_CHECK_FACTORY = None
    app_mod.reset_board_state()
    app_mod.SETUP_DONE = False


class _FakeOk:
    def __init__(self, count: int = 3) -> None:
        self._items = [{"id": str(i)} for i in range(count)]

    def list_items(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._items[:limit])

    def close(self) -> None:
        pass


class _FakeFail:
    def __init__(self, message: str) -> None:
        self._message = message

    def list_items(self, limit: int = 50) -> list[dict[str, Any]]:
        raise RuntimeError(self._message)

    def close(self) -> None:
        pass


def _save_trello(client: TestClient) -> None:
    resp = client.post(
        "/api/setup/trello",
        json={
            "token": SETUP_TOKEN,
            "project": "b1wiz",
            "board": TRELLO_BOARD,
            "api_key": TRELLO_KEY,
            "trello_token": TRELLO_TOKEN,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def _save_clickup(client: TestClient, list_val: str = CLICKUP_LIST) -> None:
    resp = client.post(
        "/api/setup/clickup",
        json={
            "token": SETUP_TOKEN,
            "project": "b1wiz",
            "team": "B1 Team",
            "list": list_val,
            "api_token": CLICKUP_TOKEN,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def _save_asana(client: TestClient) -> None:
    resp = client.post(
        "/api/setup/asana",
        json={
            "token": SETUP_TOKEN,
            "project": "b1wiz",
            "project_name": ASANA_PROJECT,
            "api_token": ASANA_TOKEN,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_trello_green(b1_client) -> None:
    client, _, app_mod = b1_client
    _save_trello(client)
    app_mod.TRELLO_CHECK_FACTORY = lambda: _FakeOk(count=3)
    data = client.post("/api/test/trello?project=b1wiz").json()
    assert data["ok"] is True
    assert data["name"] == TRELLO_BOARD
    assert data["count"] == 3


def test_trello_error_no_leak(b1_client, caplog) -> None:
    client, _, app_mod = b1_client
    _save_trello(client)
    app_mod.TRELLO_CHECK_FACTORY = lambda: _FakeFail(f"401 bad {TRELLO_TOKEN}")
    resp = client.post("/api/test/trello?project=b1wiz")
    data = resp.json()
    assert data["ok"] is False
    assert "rejected" in str(data.get("error", "")).lower()
    assert TRELLO_KEY not in resp.text
    assert TRELLO_TOKEN not in resp.text
    assert TRELLO_KEY not in caplog.text
    assert TRELLO_TOKEN not in caplog.text


def test_clickup_green(b1_client) -> None:
    client, _, app_mod = b1_client
    _save_clickup(client)
    app_mod.CLICKUP_CHECK_FACTORY = lambda: _FakeOk(count=2)
    data = client.post("/api/test/clickup?project=b1wiz").json()
    assert data["ok"] is True
    assert data["name"] == CLICKUP_LIST
    assert data["count"] == 2


def test_clickup_error_no_leak(b1_client, caplog) -> None:
    client, _, app_mod = b1_client
    _save_clickup(client)
    app_mod.CLICKUP_CHECK_FACTORY = lambda: _FakeFail(f"invalid {CLICKUP_TOKEN}")
    resp = client.post("/api/test/clickup?project=b1wiz")
    data = resp.json()
    assert data["ok"] is False
    assert "rejected" in str(data.get("error", "")).lower()
    assert CLICKUP_TOKEN not in resp.text
    assert CLICKUP_TOKEN not in caplog.text


def test_clickup_blank_list_needs_setup(b1_client) -> None:
    client, _, app_mod = b1_client
    app_mod.CLICKUP_CHECK_FACTORY = lambda: _FakeOk(count=1)
    _save_clickup(client, list_val="")
    data = client.post("/api/test/clickup?project=b1wiz").json()
    assert data["ok"] is False
    assert data.get("needs_setup") is True
    assert "list" in str(data.get("error", "")).lower()


def test_asana_green(b1_client) -> None:
    client, _, app_mod = b1_client
    _save_asana(client)
    app_mod.ASANA_CHECK_FACTORY = lambda: _FakeOk(count=4)
    data = client.post("/api/test/asana?project=b1wiz").json()
    assert data["ok"] is True
    assert data["name"] == ASANA_PROJECT
    assert data["count"] == 4


def test_asana_error_no_leak(b1_client, caplog) -> None:
    client, _, app_mod = b1_client
    _save_asana(client)
    app_mod.ASANA_CHECK_FACTORY = lambda: _FakeFail("unknown Asana project: nope")
    resp = client.post("/api/test/asana?project=b1wiz")
    data = resp.json()
    assert data["ok"] is False
    assert "could not find" in str(data.get("error", "")).lower()
    assert ASANA_TOKEN not in resp.text
    assert ASANA_TOKEN not in caplog.text


def test_setup_page_has_b1_help(b1_client) -> None:
    client, _, _ = b1_client
    page = client.get("/setup?project=b1wiz").text
    assert "Where do I find this?" in page
    assert "trello.com/app-key" in page
    assert "Required" in page
    assert "CLICKUP_LIST" in page
    assert "Leave blank and Pam will find or create a list for this project." in page
    assert "pk_" in page
    assert "trello-url" in page
    assert "asana-url" in page
    assert "Show" in page
    assert "\u2014" not in page
    for secret in (TRELLO_KEY, TRELLO_TOKEN, CLICKUP_TOKEN, ASANA_TOKEN):
        assert secret not in page
