"""Wizard B2: Linear, Jira, Monday live checks with fake adapters.

No network, no real secrets. Mirrors tests/test_wizard_b1.py setup.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

SETUP_TOKEN = "wizard-test-setup-token-1"

LINEAR_TEAM = "ENG"
LINEAR_KEY = "b2-fake-linear-key-1"

JIRA_BASE = "https://b2test.atlassian.net"
JIRA_EMAIL = "b2tester@example.com"
JIRA_PROJECT = "B2T"
JIRA_TOKEN = "b2-fake-jira-token-1"

MONDAY_BOARD = "8765432"
MONDAY_TOKEN = "b2-fake-monday-token-1"

ENV_KEYS = (
    "LINEAR_TEAM",
    "LINEAR_TEAM_KEY",
    "LINEAR_API_KEY",
    "JIRA_BASE_URL",
    "JIRA_SITE_URL",
    "JIRA_EMAIL",
    "JIRA_PROJECT",
    "JIRA_PROJECT_KEY",
    "JIRA_API_TOKEN",
    "MONDAY_BOARD",
    "MONDAY_BOARD_ID",
    "MONDAY_API_TOKEN",
)


@pytest.fixture()
def b2_client(tmp_path, monkeypatch, caplog):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("PAM_SETUP_TOKEN", SETUP_TOKEN)
    import pam_pm.web.app as app_mod

    app_mod.SETUP_DONE = False
    app_mod.LINEAR_CHECK_FACTORY = None
    app_mod.JIRA_CHECK_FACTORY = None
    app_mod.MONDAY_CHECK_FACTORY = None
    caplog.set_level(logging.INFO)
    client = TestClient(app_mod.app)
    yield client, tmp_path, app_mod
    app_mod.LINEAR_CHECK_FACTORY = None
    app_mod.JIRA_CHECK_FACTORY = None
    app_mod.MONDAY_CHECK_FACTORY = None
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


class _FakeJiraOk:
    def __init__(self, count: int = 3, sprints: bool = True) -> None:
        self._items = [{"id": str(i)} for i in range(count)]
        self._sprints = [{"id": "7"}] if sprints else []

    def list_items(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._items[:limit])

    def list_iterations(self) -> list[dict[str, Any]]:
        return list(self._sprints)

    def close(self) -> None:
        pass


def _save_linear(client: TestClient) -> None:
    resp = client.post(
        "/api/setup/linear",
        json={
            "token": SETUP_TOKEN,
            "project": "b2wiz",
            "team": LINEAR_TEAM,
            "api_key": LINEAR_KEY,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def _save_jira(client: TestClient) -> None:
    resp = client.post(
        "/api/setup/jira",
        json={
            "token": SETUP_TOKEN,
            "project": "b2wiz",
            "base_url": JIRA_BASE,
            "email": JIRA_EMAIL,
            "project_key": JIRA_PROJECT,
            "api_token": JIRA_TOKEN,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def _save_monday(client: TestClient) -> None:
    resp = client.post(
        "/api/setup/monday",
        json={
            "token": SETUP_TOKEN,
            "project": "b2wiz",
            "board": MONDAY_BOARD,
            "api_token": MONDAY_TOKEN,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_linear_green(b2_client) -> None:
    client, _, app_mod = b2_client
    _save_linear(client)
    app_mod.LINEAR_CHECK_FACTORY = lambda: _FakeOk(count=5)
    data = client.post("/api/test/linear?project=b2wiz").json()
    assert data["ok"] is True
    assert data["name"] == LINEAR_TEAM
    assert data["count"] == 5


def test_linear_error_no_leak(b2_client, caplog) -> None:
    client, _, app_mod = b2_client
    _save_linear(client)
    app_mod.LINEAR_CHECK_FACTORY = lambda: _FakeFail(f"401 bad {LINEAR_KEY}")
    resp = client.post("/api/test/linear?project=b2wiz")
    data = resp.json()
    assert data["ok"] is False
    assert "rejected" in str(data.get("error", "")).lower()
    assert LINEAR_KEY not in resp.text
    assert LINEAR_KEY not in caplog.text


def test_jira_green_with_sprints(b2_client) -> None:
    client, _, app_mod = b2_client
    _save_jira(client)
    app_mod.JIRA_CHECK_FACTORY = lambda: _FakeJiraOk(count=3, sprints=True)
    data = client.post("/api/test/jira?project=b2wiz").json()
    assert data["ok"] is True
    assert data["name"] == JIRA_PROJECT
    assert data["count"] == 3
    assert data["sprints"] is True


def test_jira_green_no_sprints(b2_client) -> None:
    client, _, app_mod = b2_client
    _save_jira(client)
    app_mod.JIRA_CHECK_FACTORY = lambda: _FakeJiraOk(count=2, sprints=False)
    data = client.post("/api/test/jira?project=b2wiz").json()
    assert data["ok"] is True
    assert data["name"] == JIRA_PROJECT
    assert data["count"] == 2
    assert data["sprints"] is False


def test_jira_error_no_leak(b2_client, caplog) -> None:
    client, _, app_mod = b2_client
    _save_jira(client)
    app_mod.JIRA_CHECK_FACTORY = lambda: _FakeFail(f"invalid {JIRA_TOKEN}")
    resp = client.post("/api/test/jira?project=b2wiz")
    data = resp.json()
    assert data["ok"] is False
    assert "rejected" in str(data.get("error", "")).lower()
    assert JIRA_TOKEN not in resp.text
    assert JIRA_TOKEN not in caplog.text


def test_monday_green(b2_client) -> None:
    client, _, app_mod = b2_client
    _save_monday(client)
    app_mod.MONDAY_CHECK_FACTORY = lambda: _FakeOk(count=6)
    data = client.post("/api/test/monday?project=b2wiz").json()
    assert data["ok"] is True
    assert data["name"] == MONDAY_BOARD
    assert data["count"] == 6


def test_monday_error_no_leak(b2_client, caplog) -> None:
    client, _, app_mod = b2_client
    _save_monday(client)
    app_mod.MONDAY_CHECK_FACTORY = lambda: _FakeFail("unknown Monday board: nope")
    resp = client.post("/api/test/monday?project=b2wiz")
    data = resp.json()
    assert data["ok"] is False
    assert "could not find" in str(data.get("error", "")).lower()
    assert MONDAY_TOKEN not in resp.text
    assert MONDAY_TOKEN not in caplog.text


def test_setup_page_has_b2_help(b2_client) -> None:
    client, _, _ = b2_client
    page = client.get("/setup?project=b2wiz").text
    assert "Where do I find this?" in page
    assert "linear.app/settings/api" in page
    assert "id.atlassian.com/manage-profile/security/api-tokens" in page
    assert "developer.monday.com" in page
    assert "Required" in page
    assert "jira-base" in page
    assert "linear-team" in page
    assert "monday-board" in page
    assert "monday-url" in page
    assert "lin_api_" in page
    assert "atlassian.net" in page
    assert "Show" in page
    assert "\u2014" not in page
    for secret in (LINEAR_KEY, JIRA_TOKEN, MONDAY_TOKEN):
        assert secret not in page
