"""Teams adapter: construct, send DM, receive, missing creds.

Mirrors the Slack adapter coverage using a fake Bot Framework
transport (httpx.MockTransport), so no test touches the network.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pam_pm.adapters.chat.teams import (
    TeamsAdapter,
    TeamsError,
    activity_to_message,
    publish_activity_to_bus,
)

APP_ID = "teams-test-app-id-1"
APP_PASSWORD = "teams-test-app-password-1"
TENANT_ID = "teams-test-tenant-id-1"

ENV_KEYS = ("TEAMS_APP_ID", "TEAMS_APP_PASSWORD", "TEAMS_TENANT_ID")


@pytest.fixture()
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


def _fake_bot_framework(seen: dict[str, Any]):
    """Fake token endpoint plus conversation send endpoint."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            seen["token_calls"] = seen.get("token_calls", 0) + 1
            body = dict(httpx.QueryParams(request.content.decode("utf-8")))
            seen["token_body"] = body
            return httpx.Response(200, json={"access_token": "fake-bf-token"})
        if request.url.path.endswith("/activities") and request.method == "POST":
            seen["send_calls"] = seen.get("send_calls", 0) + 1
            seen["auth"] = request.headers.get("Authorization", "")
            seen["send_body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"id": "activity-1"})
        return httpx.Response(404, json={"message": "unknown fake route"})

    return httpx.MockTransport(handle)


def _adapter(seen: dict[str, Any], **kwargs: Any) -> TeamsAdapter:
    return TeamsAdapter(
        app_id=APP_ID,
        app_password=APP_PASSWORD,
        tenant_id=TENANT_ID,
        transport=_fake_bot_framework(seen),
        **kwargs,
    )


def test_construct_explicit(clean_env) -> None:
    adapter = TeamsAdapter(
        app_id="  " + APP_ID + " ",
        app_password=APP_PASSWORD,
        tenant_id=TENANT_ID,
    )
    try:
        assert adapter.app_id_configured is True
        assert adapter.tenant_id == TENANT_ID
    finally:
        adapter.close()


def test_construct_from_env(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("TEAMS_APP_ID", APP_ID)
    monkeypatch.setenv("TEAMS_APP_PASSWORD", APP_PASSWORD)
    monkeypatch.setenv("TEAMS_TENANT_ID", TENANT_ID)
    adapter = TeamsAdapter()
    try:
        assert adapter.app_id_configured is True
        assert adapter.tenant_id == TENANT_ID
    finally:
        adapter.close()


def test_construct_from_saved(clean_env, tmp_path) -> None:
    from pam_pm.core import secrets as secrets_mod

    secrets_mod.save_teams_config(
        tmp_path,
        app_id=APP_ID,
        app_password=APP_PASSWORD,
        tenant_id=TENANT_ID,
    )
    adapter = TeamsAdapter(data_dir=tmp_path)
    try:
        assert adapter.app_id_configured is True
        assert adapter.tenant_id == TENANT_ID
    finally:
        adapter.close()


def test_send_dm_posts_activity(clean_env) -> None:
    seen: dict[str, Any] = {}
    adapter = _adapter(seen)
    try:
        message_id = adapter.send_dm("conv-1", "Hello from Pam")
    finally:
        adapter.close()
    assert message_id == "activity-1"
    assert seen["token_calls"] == 1
    assert seen["token_body"]["client_id"] == APP_ID
    assert seen["token_body"]["grant_type"] == "client_credentials"
    assert seen["auth"] == "Bearer fake-bf-token"
    assert seen["send_body"]["text"] == "Hello from Pam"
    assert seen["send_body"]["type"] == "message"


def test_send_buttons_posts_choice_activity(clean_env) -> None:
    seen: dict[str, Any] = {}
    adapter = _adapter(seen)
    try:
        message_id = adapter.send_buttons(
            "conv-1", "Pick one", [{"id": "a", "label": "Alpha"}]
        )
    finally:
        adapter.close()
    assert message_id == "activity-1"
    assert seen["send_body"]["suggestedActions"]["actions"] == [
        {"type": "imBack", "title": "Alpha", "value": "a"}
    ]


def test_receive_handler_queues_replies(clean_env) -> None:
    adapter = TeamsAdapter(
        app_id=APP_ID, app_password=APP_PASSWORD, tenant_id=TENANT_ID
    )
    try:
        assert adapter.read_replies("user-1") == []
        message = adapter.handle_activity(
            {
                "type": "message",
                "text": "Done with the form",
                "from": {"id": "user-1"},
                "conversation": {"id": "conv-1"},
                "id": "act-9",
            }
        )
        assert message is not None
        assert message.text == "Done with the form"
        assert message.inbound is True
        replies = adapter.read_replies("user-1")
        assert [item.text for item in replies] == ["Done with the form"]
        assert adapter.read_replies("user-1") == []
        assert adapter.handle_activity({"type": "typing"}) is None
    finally:
        adapter.close()


def test_receive_publishes_to_event_bus() -> None:
    from pam_pm.web.app import EventBus

    bus = EventBus(seed=[])
    event = publish_activity_to_bus(
        bus,
        {
            "type": "message",
            "text": "Blocked on keys",
            "from": {"id": "user-2"},
            "conversation": {"id": "conv-2"},
            "id": "act-10",
        },
    )
    assert event is not None
    assert event["text"] == "Blocked on keys"
    assert bus.history()[-1]["user_id"] == "user-2"
    assert activity_to_message({"type": "typing"}) is None
    assert publish_activity_to_bus(bus, {"type": "typing"}) is None


def test_missing_creds_error(clean_env, tmp_path) -> None:
    adapter = TeamsAdapter(data_dir=tmp_path)
    try:
        assert adapter.app_id_configured is False
        with pytest.raises(TeamsError, match="TEAMS_APP_ID"):
            adapter.send_dm("conv-1", "Hello")
    finally:
        adapter.close()


def test_no_secret_in_logs(clean_env, tmp_path, caplog) -> None:
    def _fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "fake outage"})

    adapter = TeamsAdapter(
        app_id=APP_ID,
        app_password=APP_PASSWORD,
        tenant_id=TENANT_ID,
        transport=httpx.MockTransport(_fail),
        data_dir=tmp_path,
    )
    try:
        with (
            caplog.at_level(logging.INFO, logger="pam_pm.adapters.chat.teams"),
            pytest.raises(TeamsError),
        ):
            adapter.send_dm("conv-1", "boom " + APP_PASSWORD)
    finally:
        adapter.close()
    assert APP_PASSWORD not in caplog.text


@pytest.fixture()
def teams_client(tmp_path, monkeypatch):
    for key in (*ENV_KEYS, "PAM_SETUP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("PAM_SETUP_TOKEN", "teams-test-setup-token-1")
    import pam_pm.web.app as app_mod

    app_mod.SETUP_DONE = False
    client = TestClient(app_mod.app)
    yield client, tmp_path, app_mod
    app_mod.SETUP_DONE = False


def test_wizard_save_and_test_teams(teams_client) -> None:
    from pam_pm.adapters.chat import teams_auth as teams_auth_mod
    from pam_pm.core import store as store_mod

    client, home, _ = teams_client
    resp = client.post(
        "/api/setup/teams",
        json={
            "token": "teams-test-setup-token-1",
            "project": "teamswiz",
            "app_id": APP_ID,
            "app_password": APP_PASSWORD,
            "tenant_id": TENANT_ID,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert teams_auth_mod.store_teams_refresh_token("teams-wiz-refresh-1", home) is True
    assert client.post("/api/test/teams?project=teamswiz").json()["ok"] is True
    assert store_mod.get_project_chat_backend(home / "state.db", "teamswiz") == "teams"
    blob = (home / "wizard_progress.json").read_text(encoding="utf-8")
    assert APP_PASSWORD not in blob
    page = client.get("/setup?project=teamswiz").text
    assert APP_PASSWORD not in page
    assert "Microsoft Teams" in page

def test_setup_save_persists_chat_backend(teams_client) -> None:
    from pam_pm.core import store as store_mod

    client, home, _ = teams_client
    resp = client.post(
        "/api/setup/teams",
        json={
            "token": "teams-test-setup-token-1",
            "project": "persist1",
            "app_id": APP_ID,
            "app_password": APP_PASSWORD,
            "tenant_id": TENANT_ID,
        },
    )
    assert resp.json()["ok"] is True
    assert store_mod.get_project_chat_backend(home / "state.db", "persist1") == "teams"
    slack = client.post(
        "/api/setup/slack",
        json={
            "token": "teams-test-setup-token-1",
            "project": "persist1",
            "bot_token": "xoxb-fake",
            "app_token": "",
        },
    )
    assert slack.json()["ok"] is True
    assert store_mod.get_project_chat_backend(home / "state.db", "persist1") == "slack"


def test_chat_adapter_selection_slack_vs_teams(tmp_path, monkeypatch) -> None:
    import pam_pm.web.app as app_mod
    from pam_pm.adapters.chat.slack import SlackAdapter
    from pam_pm.adapters.chat.teams import TeamsAdapter
    from pam_pm.core import secrets as secrets_mod
    from pam_pm.core import store as store_mod

    for key in (*ENV_KEYS, "PAM_SETUP_TOKEN", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    app_mod.reset_chat_state()
    try:
        db = tmp_path / "state.db"
        assert store_mod.get_project_chat_backend(db, "sel1") == "slack"
        assert isinstance(app_mod.get_chat_adapter("sel1"), SlackAdapter)
        store_mod.set_project_chat_backend(db, "sel1", "teams")
        assert app_mod.get_chat_adapter("sel1") is None
        secrets_mod.save_teams_config(
            tmp_path, app_id=APP_ID, app_password=APP_PASSWORD, tenant_id=TENANT_ID
        )
        got = app_mod.get_chat_adapter("sel1")
        assert isinstance(got, TeamsAdapter)
        try:
            got.close()
        except Exception:  # noqa: BLE001, S110
            pass
        store_mod.set_project_chat_backend(db, "sel1", "slack")
        assert isinstance(app_mod.get_chat_adapter("sel1"), SlackAdapter)
    finally:
        app_mod.reset_chat_state()


def test_check_teams_names_each_missing_piece(tmp_path, monkeypatch) -> None:
    import pam_pm.web.app as app_mod
    from pam_pm.core import secrets as secrets_mod

    for key in (*ENV_KEYS, "TEAMS_REFRESH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    ok, msg, _hint = app_mod._check_teams()
    assert ok is False and "App ID" in msg
    secrets_mod.save_secret("TEAMS_APP_ID", APP_ID, tmp_path)
    ok, msg, _hint = app_mod._check_teams()
    assert ok is False and "password" in msg.lower()
    secrets_mod.save_secret("TEAMS_APP_PASSWORD", APP_PASSWORD, tmp_path)
    ok, msg, _hint = app_mod._check_teams()
    assert ok is False and "Tenant" in msg
    secrets_mod.save_secret("TEAMS_TENANT_ID", TENANT_ID, tmp_path)
    ok, msg, _hint = app_mod._check_teams()
    assert ok is False and "not signed in" in msg.lower()
    secrets_mod.save_secret("TEAMS_REFRESH_TOKEN", "r-1", tmp_path)
    ok, msg, _hint = app_mod._check_teams()
    assert ok is True


def test_teams_package_zip_contents(teams_client) -> None:
    import io
    import zipfile

    import pam_pm

    client, _home, _ = teams_client
    saved = client.post(
        "/api/setup/teams",
        json={
            "token": "teams-test-setup-token-1",
            "project": "teampkg",
            "app_id": APP_ID,
            "app_password": APP_PASSWORD,
            "tenant_id": TENANT_ID,
        },
    )
    assert saved.json()["ok"] is True
    resp = client.get("/api/setup/teams/package?project=teampkg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert sorted(zf.namelist()) == ["color.png", "manifest.json", "outline.png"]
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        color = zf.read("color.png")
        outline = zf.read("outline.png")
    assert manifest["id"] == APP_ID
    assert manifest["bots"][0]["botId"] == APP_ID
    assert manifest["bots"][0]["notificationOnly"] is False
    assert manifest["bots"][0]["supportsChannelFeatures"] == "tier1"
    assert manifest["bots"][0]["scopes"] == ["personal", "team"]
    assert manifest["version"] == pam_pm.__version__
    assert manifest["icons"] == {"color": "color.png", "outline": "outline.png"}
    assert color[:8] == b"\x89PNG\r\n\x1a\n"
    assert outline[:8] == b"\x89PNG\r\n\x1a\n"


def test_teams_package_missing_app_id_errors_cleanly(teams_client) -> None:
    client, _home, _ = teams_client
    resp = client.get("/api/setup/teams/package?project=nopkg")
    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert "App ID" in body["message"]
