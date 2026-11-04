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
        message_id = adapter.send_dm("19:conv-1", "Hello from Pam")
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
            "19:conv-1", "Pick one", [{"id": "a", "label": "Alpha"}]
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
            adapter.send_dm("19:conv-1", "Hello")
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
            adapter.send_dm("19:conv-1", "boom " + APP_PASSWORD)
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


def _fake_app_token_ok(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/oauth2/v2.0/token"):
        return httpx.Response(200, json={"access_token": "fake-app-token", "expires_in": 3600})
    return httpx.Response(404, json={"message": "unknown fake route"})


def test_wizard_save_and_test_teams(teams_client) -> None:
    from pam_pm.core import store as store_mod

    client, home, app_mod = teams_client
    app_mod.TEAMS_CHECK_TRANSPORT = httpx.MockTransport(_fake_app_token_ok)
    try:
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
        assert client.post("/api/test/teams?project=teamswiz").json()["ok"] is True
        assert store_mod.get_project_chat_backend(home / "state.db", "teamswiz") == "teams"
        blob = (home / "wizard_progress.json").read_text(encoding="utf-8")
        assert APP_PASSWORD not in blob
        page = client.get("/setup?project=teamswiz").text
        assert APP_PASSWORD not in page
        assert "Microsoft Teams" in page
    finally:
        app_mod.TEAMS_CHECK_TRANSPORT = None

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
    app_mod.TEAMS_CHECK_TRANSPORT = httpx.MockTransport(_fake_app_token_ok)
    try:
        ok, msg, _hint = app_mod._check_teams()
        assert ok is True
    finally:
        app_mod.TEAMS_CHECK_TRANSPORT = None


def test_app_token_mint_success_and_401() -> None:
    from pam_pm.adapters.chat.teams_auth import DeviceAuthError, app_token

    def _ok(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode("utf-8")))
        assert body["grant_type"] == "client_credentials"
        assert body["scope"] == "https://graph.microsoft.com/.default"
        return httpx.Response(200, json={"access_token": "app-tok-1", "expires_in": 3600})

    token = app_token(APP_ID, TENANT_ID, APP_PASSWORD, transport=httpx.MockTransport(_ok))
    assert token == "app-tok-1"

    def _denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    with pytest.raises(DeviceAuthError):
        app_token(APP_ID, TENANT_ID, "bad-secret", transport=httpx.MockTransport(_denied))


def test_check_teams_rejected_creds(tmp_path, monkeypatch) -> None:
    import pam_pm.web.app as app_mod
    from pam_pm.core import secrets as secrets_mod

    for key in (*ENV_KEYS, "TEAMS_REFRESH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    secrets_mod.save_secret("TEAMS_APP_ID", APP_ID, tmp_path)
    secrets_mod.save_secret("TEAMS_APP_PASSWORD", "bad-secret", tmp_path)
    secrets_mod.save_secret("TEAMS_TENANT_ID", TENANT_ID, tmp_path)

    def _denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    app_mod.TEAMS_CHECK_TRANSPORT = httpx.MockTransport(_denied)
    try:
        ok, msg, _hint = app_mod._check_teams()
        assert ok is False and "rejected" in msg.lower()
    finally:
        app_mod.TEAMS_CHECK_TRANSPORT = None


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


def _fake_watched_members(
    seen: dict[str, Any], members_by_chat: dict[str, list[dict[str, str]]]
):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            body = dict(httpx.QueryParams(request.content.decode("utf-8")))
            scope = body.get("scope", "")
            seen.setdefault("token_scopes", []).append(scope)
            if scope == "https://graph.microsoft.com/.default":
                seen["graph_token_calls"] = seen.get("graph_token_calls", 0) + 1
                return httpx.Response(200, json={"access_token": "fake-graph-token"})
            seen["token_calls"] = seen.get("token_calls", 0) + 1
            return httpx.Response(200, json={"access_token": "fake-bf-token"})
        url = str(request.url)
        if url == "https://graph.microsoft.com/v1.0/chats":
            seen["listing_calls"] = seen.get("listing_calls", 0) + 1
            return httpx.Response(
                400, json={"error": "not supported in application-only context"}
            )
        if url.startswith("https://graph.microsoft.com/v1.0/chats/") and url.endswith(
            "/members"
        ):
            seen["graph_calls"] = seen.get("graph_calls", 0) + 1
            seen["graph_auth"] = request.headers.get("Authorization", "")
            chat_id = url.split("/v1.0/chats/", 1)[1].rsplit("/members", 1)[0]
            seen.setdefault("member_chat_ids", []).append(chat_id)
            return httpx.Response(
                200, json={"value": members_by_chat.get(chat_id, [])}
            )
        if request.url.path.endswith("/activities") and request.method == "POST":
            seen["send_calls"] = seen.get("send_calls", 0) + 1
            seen.setdefault("send_urls", []).append(str(request.url))
            seen["send_body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"id": "activity-1"})
        return httpx.Response(404, json={"message": "unknown fake route"})

    return httpx.MockTransport(handle)


def _watched_chats() -> list[dict[str, Any]]:
    return [
        {"id": "19:chat-owen", "chatType": "oneOnOne"},
        {"id": "19:chat-other", "chatType": "oneOnOne"},
        {"id": "team1/channel1", "chatType": "channel", "team_id": "team1"},
    ]


def _watched_members() -> dict[str, list[dict[str, str]]]:
    return {
        "19:chat-owen": [{"displayName": "Pam"}, {"displayName": "Owen Clark"}],
        "19:chat-other": [{"displayName": "Pam"}, {"displayName": "Someone Else"}],
    }


def test_resolve_member_match(clean_env) -> None:
    seen: dict[str, Any] = {}
    adapter = TeamsAdapter(
        app_id=APP_ID,
        app_password=APP_PASSWORD,
        tenant_id=TENANT_ID,
        transport=_fake_watched_members(seen, _watched_members()),
        chats=_watched_chats(),
    )
    try:
        assert adapter.resolve_conversation("Owen Clark") == "19:chat-owen"
        assert adapter.resolve_conversation("someone else") == "19:chat-other"
    finally:
        adapter.close()
    assert seen.get("listing_calls", 0) == 0
    assert seen["graph_auth"] == "Bearer fake-graph-token"
    assert "https://graph.microsoft.com/.default" in seen.get("token_scopes", [])


def test_resolve_name_caches_one_graph_call(clean_env) -> None:
    seen: dict[str, Any] = {}
    adapter = TeamsAdapter(
        app_id=APP_ID,
        app_password=APP_PASSWORD,
        tenant_id=TENANT_ID,
        transport=_fake_watched_members(seen, _watched_members()),
        chats=_watched_chats(),
    )
    try:
        assert adapter.send_dm("Owen Clark", "hi one") == "activity-1"
        assert adapter.send_buttons(
            "owen clark", "hi two", [{"id": "a", "label": "Alpha"}]
        ) == "activity-1"
    finally:
        adapter.close()
    assert seen.get("listing_calls", 0) == 0
    assert seen.get("graph_calls") == 1
    assert seen.get("send_calls") == 2
    assert seen["graph_auth"] == "Bearer fake-graph-token"
    assert "https://graph.microsoft.com/.default" in seen.get("token_scopes", [])
    assert all("19:chat-owen" in url for url in seen["send_urls"])


def test_resolve_caches_members_fetch(clean_env) -> None:
    seen: dict[str, Any] = {}
    adapter = TeamsAdapter(
        app_id=APP_ID,
        app_password=APP_PASSWORD,
        tenant_id=TENANT_ID,
        transport=_fake_watched_members(seen, _watched_members()),
        chats=_watched_chats(),
    )
    try:
        first = adapter.resolve_conversation("Owen Clark")
        calls_after_first = seen.get("graph_calls", 0)
        second = adapter.resolve_conversation("owen CLARK")
        assert first == second == "19:chat-owen"
        assert seen.get("graph_calls", 0) == calls_after_first
    finally:
        adapter.close()
    assert seen.get("listing_calls", 0) == 0
    assert seen.get("graph_calls") == 1


def test_roster_override_wins(clean_env) -> None:
    seen: dict[str, Any] = {}
    adapter = TeamsAdapter(
        app_id=APP_ID,
        app_password=APP_PASSWORD,
        tenant_id=TENANT_ID,
        transport=_fake_watched_members(seen, _watched_members()),
        chats=_watched_chats(),
        roster=[{"name": "Owen Clark", "teams_chat_id": "19:override-chat"}],
    )
    try:
        assert adapter.resolve_conversation("Owen Clark") == "19:override-chat"
        assert adapter.resolve_conversation("owen clark") == "19:override-chat"
    finally:
        adapter.close()
    assert seen.get("graph_calls", 0) == 0
    assert seen.get("listing_calls", 0) == 0


def test_roster_arg_override_wins_without_fetch(clean_env) -> None:
    seen: dict[str, Any] = {}
    adapter = TeamsAdapter(
        app_id=APP_ID,
        app_password=APP_PASSWORD,
        tenant_id=TENANT_ID,
        transport=_fake_watched_members(seen, _watched_members()),
        chats=_watched_chats(),
    )
    try:
        got = adapter.resolve_conversation(
            "Owen Clark", roster=[{"name": "owen clark", "teams_chat_id": "19:arg-chat"}]
        )
        assert got == "19:arg-chat"
    finally:
        adapter.close()
    assert seen.get("graph_calls", 0) == 0


def test_resolve_unknown_name_raises(clean_env) -> None:
    seen: dict[str, Any] = {}
    adapter = TeamsAdapter(
        app_id=APP_ID,
        app_password=APP_PASSWORD,
        tenant_id=TENANT_ID,
        transport=_fake_watched_members(seen, _watched_members()),
        chats=_watched_chats(),
    )
    try:
        with pytest.raises(TeamsError) as exc:
            adapter.send_dm("Nobody Here", "hello")
    finally:
        adapter.close()
    assert "Nobody Here" in str(exc.value)
    assert "watch the 1:1 in Setup, then retry" in str(exc.value)
    assert seen.get("listing_calls", 0) == 0


def test_explicit_conversation_ids_passthrough(clean_env) -> None:
    seen: dict[str, Any] = {}
    adapter = TeamsAdapter(
        app_id=APP_ID,
        app_password=APP_PASSWORD,
        tenant_id=TENANT_ID,
        transport=_fake_watched_members(seen, _watched_members()),
        chats=_watched_chats(),
    )
    try:
        for convo_id in ("19:abc123", "a:xyz789", "foo:bar"):
            assert adapter.send_dm(convo_id, "hi") == "activity-1"
        assert adapter.send_buttons("19:btn-convo", "pick", [{"id": "a"}]) == "activity-1"
    finally:
        adapter.close()
    assert seen.get("graph_calls", 0) == 0
    assert seen.get("graph_token_calls", 0) == 0
    assert seen.get("send_calls") == 4
    for convo_id, url in zip(
        ("19:abc123", "a:xyz789", "foo:bar", "19:btn-convo"), seen["send_urls"]
    ):
        assert convo_id in url
