"""Chat picker: saving slack or teams persists and selects the adapter."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import pam_pm.adapters.chat.slack as slack_mod
import pam_pm.adapters.chat.teams as teams_mod
from pam_pm.core import store as store_mod
from pam_pm.web import app as app_mod


class _FakeSlackAdapter:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


class _FakeTeamsAdapter:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


@pytest.fixture()
def _chat_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("PAM_SETUP_TOKEN", "test-setup-token")
    app_mod.reset_chat_state()
    monkeypatch.setattr(slack_mod, "SlackAdapter", _FakeSlackAdapter)
    monkeypatch.setattr(teams_mod, "TeamsAdapter", _FakeTeamsAdapter)
    monkeypatch.setattr(
        app_mod.secrets_mod,
        "effective_teams_config",
        lambda data_dir=None: {
            "app_id": "fake-app-id",
            "app_password": "fake-secret",
            "tenant_id": "fake-tenant",
            "refresh_token": "",
        },
    )
    monkeypatch.setattr(app_mod, "_teams_watched_chats", lambda name: [])
    monkeypatch.setattr(app_mod, "_roster_entries", lambda name: [])
    yield
    app_mod.reset_chat_state()


def test_chat_backend_choices_persist(_chat_env: Any) -> None:
    db = app_mod._db_path()
    assert store_mod.get_project_chat_backend(db, "demo") == "slack"
    for choice in ("slack", "teams"):
        stored = store_mod.set_project_chat_backend(db, "demo", choice)
        assert stored == choice
        assert store_mod.get_project_chat_backend(db, "demo") == choice
    assert store_mod.set_project_chat_backend(db, "demo", "bogus") == "slack"


def test_setup_chat_endpoint_persists_and_page_reflects(_chat_env: Any) -> None:
    client = TestClient(app_mod.app)
    for choice in ("slack", "teams"):
        resp = client.post(
            "/api/setup/chat?project=demo&token=test-setup-token",
            json={"chat_backend": choice},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["chat_backend"] == choice
        assert store_mod.get_project_chat_backend(app_mod._db_path(), "demo") == choice
        page = client.get("/setup?project=demo")
        assert page.status_code == 200
        html = page.text
        assert 'name="chat_backend"' in html
        assert "/static/icons/slack.svg" in html
        assert "/static/icons/teams.svg" in html
        assert "/api/test/slack" in html
        assert "/api/test/teams" in html
        if choice == "teams":
            assert 'value="teams" checked' in html
            assert 'value="slack" checked' not in html
        else:
            assert 'value="slack" checked' in html
            assert 'value="teams" checked' not in html


def test_get_chat_adapter_matches_saved_choice(_chat_env: Any) -> None:
    db = app_mod._db_path()
    store_mod.set_project_chat_backend(db, "demo", "slack")
    assert isinstance(app_mod.get_chat_adapter("demo"), _FakeSlackAdapter)
    store_mod.set_project_chat_backend(db, "demo", "teams")
    assert isinstance(app_mod.get_chat_adapter("demo"), _FakeTeamsAdapter)
    assert isinstance(app_mod.get_chat_adapter("unknown-project"), _FakeSlackAdapter)
