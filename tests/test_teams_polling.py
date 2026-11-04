"""Teams Graph polling: device flow, watermarks, mention and DM routing.

Covers both fake concerns: a fake Entra token endpoint for the
device code flow, and a fake Graph endpoint for chat polling. No
test touches the network.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.chat.teams import TeamsAdapter, TeamsError
from pam_pm.adapters.chat.teams_auth import (
    DEVICELOGIN_URL,
    DeviceAuthError,
    DeviceCodeExpired,
    DeviceCodePending,
    GraphTokenProvider,
    poll_device_token,
    refresh_access_token,
    start_device_code,
)
from pam_pm.adapters.chat.teams_poll import (
    GraphClient,
    TeamsPoller,
    graph_message_to_chat_message,
    is_dm,
    mentions_pam,
    publish_graph_message_to_bus,
    should_handle,
)

APP_ID = "teams-poll-app-id-1"
APP_PASSWORD = "teams-poll-app-password-1"
TENANT_ID = "teams-poll-tenant-id-1"
REFRESH = "teams-poll-refresh-token-1"
DEVICE_CODE = "teams-poll-device-code-1"

ENV_KEYS = (
    "TEAMS_APP_ID",
    "TEAMS_APP_PASSWORD",
    "TEAMS_TENANT_ID",
    "TEAMS_REFRESH_TOKEN",
)


@pytest.fixture()
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


def _graph_message(
    mid: str,
    text: str,
    created: str,
    sender: str = "user-1",
    message_type: str = "message",
    mentions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": mid,
        "messageType": message_type,
        "createdDateTime": created,
        "body": {"contentType": "html", "content": f"<p>{text}</p>"},
        "from": {"user": {"id": sender, "displayName": "Ana"}},
        "mentions": mentions or [],
    }


class StubGraph:
    """Fake Graph chat reader recording filter use."""

    def __init__(self, chats: dict[str, list[dict[str, Any]]]) -> None:
        self.chats = {key: list(val) for key, val in chats.items()}
        self.filters: dict[str, str | None] = {}
        self.calls = 0

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        return {"id": chat_id, "chatType": "oneOnOne"}

    def list_chat_messages(
        self, chat_id: str, since: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.calls += 1
        self.filters[chat_id] = since
        items = [dict(item) for item in self.chats.get(chat_id, [])]
        if (since or "").strip():
            items = [item for item in items if str(item.get("createdDateTime", "")) > since]
        return items[:limit]

    def list_channel_messages(
        self, team_id: str, channel_id: str, since: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return self.list_chat_messages(f"{team_id}/{channel_id}", since, limit)


def _poller(
    stub: StubGraph, db: Any, chats: list[dict[str, Any]], **kwargs: Any
) -> TeamsPoller:
    return TeamsPoller(stub, db, chats, interval=60, **kwargs)


def _fake_app_token(seen: dict[str, Any], status: int = 200) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode("utf-8")))
        seen["grant"] = body.get("grant_type", "")
        seen["scope"] = body.get("scope", "")
        seen["calls"] = seen.get("calls", 0) + 1
        if status != 200:
            return httpx.Response(status, json={"error": "invalid_client"})
        return httpx.Response(200, json={"access_token": "app-tok-1", "expires_in": 3600})

    return httpx.MockTransport(handle)


def test_app_token_mint_success_and_401() -> None:
    from pam_pm.adapters.chat.teams_auth import DeviceAuthError, app_token

    seen: dict[str, Any] = {}
    token = app_token(APP_ID, TENANT_ID, APP_PASSWORD, transport=_fake_app_token(seen))
    assert token == "app-tok-1"
    assert seen["grant"] == "client_credentials"
    assert seen["scope"] == "https://graph.microsoft.com/.default"
    with pytest.raises(DeviceAuthError):
        app_token(APP_ID, TENANT_ID, "bad", transport=_fake_app_token({}, status=401))


def test_app_provider_caches_until_near_expiry() -> None:
    from pam_pm.adapters.chat.teams_poll import AppTokenProvider

    seen: dict[str, Any] = {}
    clock = {"now": 1000.0}
    provider = AppTokenProvider(
        APP_ID,
        TENANT_ID,
        APP_PASSWORD,
        transport=_fake_app_token(seen),
        time_fn=lambda: clock["now"],
    )
    first = provider.access_token()
    assert first == "app-tok-1"
    assert provider.access_token() == first
    assert seen["calls"] == 1
    clock["now"] += 4000.0
    assert provider.access_token() == "app-tok-1"
    assert seen["calls"] == 2


def test_graph_client_accepts_token_callable() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"value": []})

    client = GraphClient(lambda: "callable-tok", transport=httpx.MockTransport(handle))
    try:
        assert client.list_chat_messages("chat-1") == []
        assert seen["auth"] == "Bearer callable-tok"
    finally:
        client.close()


# Device code flow with a fake token endpoint.


def _fake_entra(seen: dict[str, Any], mode: str = "pending_then_ok") -> httpx.MockTransport:
    state = {"token_calls": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/devicecode"):
            body = dict(httpx.QueryParams(request.content.decode("utf-8")))
            seen["devicecode_body"] = body
            return httpx.Response(
                200,
                json={
                    "device_code": DEVICE_CODE,
                    "user_code": "ABCD-1234",
                    "verification_uri": DEVICELOGIN_URL,
                    "expires_in": 900,
                    "interval": 5,
                    "message": "Go to the link and enter ABCD-1234.",
                },
            )
        if request.url.path.endswith("/oauth2/v2.0/token"):
            state["token_calls"] += 1
            seen["token_calls"] = state["token_calls"]
            body = dict(httpx.QueryParams(request.content.decode("utf-8")))
            seen.setdefault("token_bodies", []).append(body)
            if body.get("grant_type") == "refresh_token":
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"graph-access-{state['token_calls']}",
                        "refresh_token": f"graph-refresh-{state['token_calls']}",
                        "expires_in": 3600,
                    },
                )
            if mode == "expired":
                return httpx.Response(400, json={"error": "expired_token"})
            if state["token_calls"] < 2:
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(
                200,
                json={
                    "access_token": "graph-access-1",
                    "refresh_token": REFRESH,
                    "expires_in": 3600,
                },
            )
        return httpx.Response(404, json={"message": "unknown fake route"})

    return httpx.MockTransport(handle)


def test_device_start_shows_user_code_and_link(clean_env) -> None:
    seen: dict[str, Any] = {}
    session = start_device_code(
        APP_ID, TENANT_ID, transport=_fake_entra(seen)
    )
    assert session.user_code == "ABCD-1234"
    assert session.verification_uri == DEVICELOGIN_URL
    assert session.device_code == DEVICE_CODE
    assert seen["devicecode_body"]["client_id"] == APP_ID


def test_device_poll_pending_then_complete(clean_env) -> None:
    seen: dict[str, Any] = {}
    transport = _fake_entra(seen)
    session = start_device_code(APP_ID, TENANT_ID, transport=transport)
    with pytest.raises(DeviceCodePending):
        poll_device_token(session, transport=transport)
    result = poll_device_token(session, transport=transport)
    assert result.access_token == "graph-access-1"
    assert result.refresh_token == REFRESH


def test_device_poll_expired(clean_env) -> None:
    seen: dict[str, Any] = {}
    transport = _fake_entra(seen, mode="expired")
    session = start_device_code(APP_ID, TENANT_ID, transport=transport)
    with pytest.raises(DeviceCodeExpired):
        poll_device_token(session, transport=transport)


def test_client_secret_only_where_needed(clean_env) -> None:
    seen: dict[str, Any] = {}
    transport = _fake_entra(seen)
    session = start_device_code(APP_ID, TENANT_ID, transport=transport)
    with pytest.raises(DeviceCodePending):
        poll_device_token(session, transport=transport)
    assert "client_secret" not in seen["token_bodies"][-1]
    result = poll_device_token(session, client_secret=APP_PASSWORD, transport=transport)
    assert result.access_token == "graph-access-1"
    assert seen["token_bodies"][-1]["client_secret"] == APP_PASSWORD


def test_refresh_token_stored_0600_and_reused_silently(clean_env, tmp_path) -> None:
    from pam_pm.adapters.chat import teams_auth as teams_auth_mod
    from pam_pm.core import secrets as secrets_mod

    seen: dict[str, Any] = {}
    transport = _fake_entra(seen)
    session = start_device_code(APP_ID, TENANT_ID, transport=transport)
    with pytest.raises(DeviceCodePending):
        poll_device_token(session, transport=transport)
    result = poll_device_token(session, transport=transport)
    assert teams_auth_mod.store_teams_refresh_token(result.refresh_token, tmp_path) is True
    assert secrets_mod.load_secret("TEAMS_REFRESH_TOKEN", tmp_path) == REFRESH
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(tmp_path / "teams_refresh_token.txt").st_mode)
        assert mode == 0o600
    clock = {"now": 1000.0}
    provider = GraphTokenProvider(
        APP_ID,
        TENANT_ID,
        data_dir=tmp_path,
        transport=transport,
        time_fn=lambda: clock["now"],
    )
    first = provider.access_token()
    assert first.startswith("graph-access-")
    again = provider.access_token()
    assert again == first
    refresh_calls = seen["token_calls"]
    clock["now"] += 4000.0
    minted = provider.access_token()
    assert minted != first
    assert seen["token_calls"] > refresh_calls


def test_silent_mint_needs_signin(clean_env, tmp_path) -> None:
    provider = GraphTokenProvider(APP_ID, TENANT_ID, data_dir=tmp_path)
    with pytest.raises(DeviceAuthError, match="sign-in"):
        provider.access_token()


def test_refresh_grant_shape(clean_env) -> None:
    seen: dict[str, Any] = {}
    result = refresh_access_token(
        APP_ID, TENANT_ID, REFRESH, transport=_fake_entra(seen)
    )
    assert result.access_token.startswith("graph-access-")
    assert seen["token_bodies"][-1]["grant_type"] == "refresh_token"
    assert "client_secret" not in seen["token_bodies"][-1]


# Mention detection, DM detection, and routing guards.


def test_mentions_pam_structured_and_text() -> None:
    structured = _graph_message(
        "m-1",
        "hello there",
        "2026-11-02T08:00:00Z",
        mentions=[{"mentioned": {"displayName": "Pam"}}],
    )
    assert mentions_pam(structured) is True
    tagged = _graph_message("m-2", "hey @pam help", "2026-11-02T08:01:00Z")
    assert mentions_pam(tagged) is True
    other = _graph_message("m-3", "pamphlet draft done", "2026-11-02T08:02:00Z")
    assert mentions_pam(other) is False
    plain = _graph_message("m-4", "standup moved", "2026-11-02T08:03:00Z")
    assert mentions_pam(plain) is False


def test_is_dm_by_chat_type() -> None:
    assert is_dm({"chatType": "oneOnOne"}) is True
    assert is_dm({"chatType": "group"}) is False
    assert is_dm({}) is False
    assert is_dm(None) is False


def test_should_handle_dm_mention_and_skips() -> None:
    dm = _graph_message("m-1", "blocked on keys", "2026-11-02T08:00:00Z")
    assert should_handle(dm, "oneOnOne") is True
    channel_plain = _graph_message("m-2", "standup moved", "2026-11-02T08:01:00Z")
    assert should_handle(channel_plain, "channel") is False
    channel_mention = _graph_message(
        "m-3",
        "hey pam, triage this",
        "2026-11-02T08:02:00Z",
        mentions=[{"mentioned": {"displayName": "Pam"}}],
    )
    assert should_handle(channel_mention, "channel") is True
    own = _graph_message("m-4", "morning", "2026-11-02T08:03:00Z", sender="pam-bot")
    assert should_handle(own, "oneOnOne", bot_id="pam-bot") is False
    system = _graph_message(
        "m-5", "Ana joined", "2026-11-02T08:04:00Z", message_type="systemEventMessage"
    )
    assert should_handle(system, "oneOnOne") is False
    assert should_handle(None, "oneOnOne") is False  # type: ignore[arg-type]


def test_graph_message_to_chat_message_strips_html() -> None:
    message = graph_message_to_chat_message(
        _graph_message("m-9", "hello <b>pam</b>", "2026-11-02T08:00:00Z"),
        chat_id="chat-1",
    )
    assert message.text == "hello pam"
    assert message.inbound is True
    assert message.ts == "2026-11-02T08:00:00Z"


# Watermark advance, dedupe, bus events, interval reuse.


def test_watermark_advances_and_fetches_only_newer() -> None:
    db = sqlite3.connect(":memory:")
    try:
        stub = StubGraph(
            {
                "chat-1": [
                    _graph_message("m-1", "first", "2026-11-02T08:00:00Z"),
                    _graph_message("m-2", "second", "2026-11-02T08:05:00Z"),
                ]
            }
        )
        poller = _poller(stub, db, [{"id": "chat-1", "chatType": "oneOnOne"}])
        assert poller.get_watermark("chat-1") is None
        first = poller.poll_once()
        assert [item.id for item in first] == ["m-1", "m-2"]
        assert poller.get_watermark("chat-1") == "2026-11-02T08:05:00Z"
        stub.chats["chat-1"].append(
            _graph_message("m-3", "third", "2026-11-02T08:10:00Z")
        )
        second = poller.poll_once()
        assert [item.id for item in second] == ["m-3"]
        assert stub.filters["chat-1"] == "2026-11-02T08:05:00Z"
        assert poller.poll_once() == []
    finally:
        db.close()


def test_watermarks_are_per_chat() -> None:
    db = sqlite3.connect(":memory:")
    try:
        stub = StubGraph(
            {
                "chat-1": [_graph_message("m-1", "one", "2026-11-02T08:00:00Z")],
                "chat-2": [_graph_message("m-2", "two", "2026-11-02T09:00:00Z")],
            }
        )
        poller = _poller(
            stub,
            db,
            [
                {"id": "chat-1", "chatType": "oneOnOne"},
                {"id": "chat-2", "chatType": "oneOnOne"},
            ],
        )
        assert len(poller.poll_once()) == 2
        assert poller.get_watermark("chat-1") == "2026-11-02T08:00:00Z"
        assert poller.get_watermark("chat-2") == "2026-11-02T09:00:00Z"
    finally:
        db.close()


def test_dedupe_on_poll() -> None:
    db = sqlite3.connect(":memory:")
    try:
        stub = StubGraph(
            {
                "chat-1": [
                    _graph_message("m-1", "same", "2026-11-02T08:00:00Z"),
                    _graph_message("m-1", "same", "2026-11-02T08:00:00Z"),
                ]
            }
        )
        poller = _poller(stub, db, [{"id": "chat-1", "chatType": "oneOnOne"}])
        assert [item.id for item in poller.poll_once()] == ["m-1"]
        assert poller.poll_once() == []
    finally:
        db.close()


def test_channel_without_mention_skipped_but_watermark_moves() -> None:
    db = sqlite3.connect(":memory:")
    try:
        stub = StubGraph(
            {
                "team-1/chan-1": [
                    _graph_message("m-1", "standup moved", "2026-11-02T08:00:00Z"),
                ]
            }
        )
        poller = _poller(
            stub, db, [{"id": "team-1/chan-1", "chatType": "channel"}]
        )
        assert poller.poll_once() == []
        assert poller.get_watermark("team-1/chan-1") == "2026-11-02T08:00:00Z"
    finally:
        db.close()


def test_polled_matches_publish_to_shared_bus() -> None:
    from pam_pm.web.app import EventBus

    db = sqlite3.connect(":memory:")
    try:
        bus = EventBus(seed=[])
        stub = StubGraph(
            {"chat-1": [_graph_message("m-1", "blocked", "2026-11-02T08:00:00Z")]}
        )
        poller = _poller(
            stub, db, [{"id": "chat-1", "chatType": "oneOnOne"}], bus=bus
        )
        found = poller.poll_once()
        assert len(found) == 1
        history = bus.history()
        assert history[-1]["source"] == "teams"
        assert history[-1]["operation"] == "chat_message"
        assert history[-1]["text"] == "blocked"
        direct = publish_graph_message_to_bus(bus, found[0], "chat-1", "oneOnOne")
        assert direct["chat_id"] == "chat-1"
    finally:
        db.close()


def test_poller_reuses_shared_poll_interval(clean_env, tmp_path) -> None:
    from pam_pm.core import recovery as recovery_mod

    db_path = tmp_path / "state.db"
    assert TeamsPoller(StubGraph({}), db_path, []).interval == 60
    recovery_mod.kv_set(db_path, "settings.poll_interval", "42")
    assert TeamsPoller(StubGraph({}), db_path, []).interval == 42


def test_graph_client_sends_filter_and_bearer(clean_env) -> None:
    seen: dict[str, Any] = {}

    class Tokens:
        def access_token(self) -> str:
            return "graph-fake-token"

    def handle(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        seen["filter"] = request.url.params.get("$filter", "")
        return httpx.Response(
            200,
            json={"value": [_graph_message("m-1", "hi", "2026-11-02T08:00:00Z")]},
        )

    client = GraphClient(Tokens(), transport=httpx.MockTransport(handle))
    try:
        items = client.list_chat_messages("chat-1", since="2026-11-02T07:00:00Z")
        assert [item["id"] for item in items] == ["m-1"]
        assert seen["auth"].startswith("Bearer ")
        assert "graph-fake-token" in seen["auth"]
        from pam_pm.adapters.chat.teams_poll import redact as poll_redact

        assert poll_redact(seen["auth"]) == "Bearer " + "[REDACTED]"
        assert "2026-11-02T07:00:00Z" in seen["filter"]
        no_filter = client.list_chat_messages("chat-1")
        assert len(no_filter) == 1
        assert seen["filter"] == ""
    finally:
        client.close()


# Adapter poll mode and router handle path.


def test_adapter_poll_mode_queues_for_read_replies() -> None:
    db = sqlite3.connect(":memory:")
    try:
        stub = StubGraph(
            {"chat-1": [_graph_message("m-1", "done here", "2026-11-02T08:00:00Z")]}
        )
        adapter = TeamsAdapter(
            app_id=APP_ID,
            app_password=APP_PASSWORD,
            tenant_id=TENANT_ID,
            mode="poll",
        )
        try:
            assert adapter.poll_mode is True
            with pytest.raises(TeamsError, match="poller"):
                adapter.poll_once()
            adapter.attach_poller(
                _poller(stub, db, [{"id": "chat-1", "chatType": "oneOnOne"}])
            )
            found = adapter.poll_once()
            assert [item.text for item in found] == ["done here"]
            replies = adapter.read_replies("user-1")
            assert [item.text for item in replies] == ["done here"]
            assert adapter.read_replies("user-1") == []
        finally:
            adapter.close()
    finally:
        db.close()


def test_adapter_send_mode_poll_is_noop() -> None:
    adapter = TeamsAdapter(
        app_id=APP_ID, app_password=APP_PASSWORD, tenant_id=TENANT_ID
    )
    try:
        assert adapter.poll_mode is False
        assert adapter.poll_once() == []
    finally:
        adapter.close()


def test_adapter_poll_mode_send_still_uses_bot_connector(clean_env) -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "fake-bf-token"})
        if request.url.path.endswith("/activities"):
            seen["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"id": "activity-7"})
        return httpx.Response(404, json={"message": "unknown fake route"})

    adapter = TeamsAdapter(
        app_id=APP_ID,
        app_password=APP_PASSWORD,
        tenant_id=TENANT_ID,
        transport=httpx.MockTransport(handle),
        mode="poll",
    )
    try:
        assert adapter.send_dm("conv-1", "Hello") == "activity-7"
        assert seen["body"]["text"] == "Hello"
    finally:
        adapter.close()


def test_router_handles_teams_text_like_slack() -> None:
    from pam_pm.core import router as router_mod

    chat = InMemoryChat()
    result = router_mod.handle_teams_event(
        {
            "type": "teams_message",
            "text": "hey pam, status?",
            "user_id": "user-1",
            "channel": "chat-1",
            "message_id": "m-1",
        },
        chat=chat,
    )
    assert result["kind"] == "message"
    assert result["capability"] == router_mod.CAP_TEAMS_TEXT
    assert result["replied"] is False
    assert chat.dms == []


def test_router_handles_graph_message_and_ignores_own() -> None:
    from pam_pm.core import router as router_mod

    chat = InMemoryChat()
    graph = _graph_message("m-2", " blocked on keys ", "2026-11-02T08:00:00Z")
    graph["chat_id"] = "chat-1"
    result = router_mod.handle_teams_event(graph, chat=chat)
    assert result["kind"] == "message"
    assert result["user_id"] == "user-1"
    own = router_mod.handle_teams_event(graph, chat=chat, bot_id="user-1")
    assert own == {"kind": "ignored", "replied": False}
    empty = router_mod.handle_teams_event(
        {"text": "  ", "user_id": "user-2", "channel": "chat-2"}, chat=chat
    )
    assert empty == {"kind": "ignored", "replied": False}


def test_router_routes_teams_audio_like_slack() -> None:
    from pam_pm.core import router as router_mod

    chat = InMemoryChat()
    result = router_mod.handle_teams_event(
        {
            "text": "",
            "user_id": "user-1",
            "channel": "chat-1",
            "attachments": [
                {
                    "contentType": "reference",
                    "content": {
                        "contentUrl": "https://files/fake-audio",
                        "name": "standup.mp3",
                    },
                }
            ],
        },
        chat=chat,
        mimetype="audio/mpeg",
        filename="standup.mp3",
    )
    assert result["kind"] == "audio_declined"
    assert len(chat.dms) == 1
    assert "Listen page" in chat.dms[0]["text"]


def test_polled_message_flows_through_router() -> None:
    from pam_pm.adapters.chat.teams_poll import handle_inbound_message

    chat = InMemoryChat()
    message = graph_message_to_chat_message(
        _graph_message("m-3", "hey pam", "2026-11-02T08:00:00Z"), chat_id="chat-1"
    )
    result = handle_inbound_message(message, chat=chat, chat_id="chat-1")
    assert result["kind"] == "message"
    assert result["replied"] is False


# Secrets redaction.


def test_teams_redact_hides_refresh_token(clean_env, tmp_path, monkeypatch) -> None:
    from pam_pm.adapters.chat.teams import redact
    from pam_pm.core import secrets as secrets_mod

    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    secrets_mod.save_secret("TEAMS_REFRESH_TOKEN", REFRESH, tmp_path)
    assert REFRESH not in redact(f"token call used {REFRESH} here")
    assert redact("Authorization: Bearer " + "graph-fake-token") == (
        "Authorization: Bearer " + "[REDACTED]"
    )


def test_device_failures_never_log_secrets(clean_env, caplog) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "fake outage"})

    transport = httpx.MockTransport(handle)
    with (
        caplog.at_level(logging.INFO, logger="pam_pm.adapters.chat.teams_auth"),
        pytest.raises(DeviceAuthError),
    ):
        start_device_code(APP_ID, TENANT_ID, transport=transport)
    with (
        caplog.at_level(logging.INFO, logger="pam_pm.adapters.chat.teams_auth"),
        pytest.raises(DeviceAuthError),
    ):
        refresh_access_token(APP_ID, TENANT_ID, REFRESH, transport=transport)
    assert REFRESH not in caplog.text
    assert APP_ID not in caplog.text


# Wizard device endpoints with a fake Entra transport.


@pytest.fixture()
def device_client(tmp_path, monkeypatch):
    for key in (*ENV_KEYS, "PAM_SETUP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("PAM_SETUP_TOKEN", "teams-device-setup-token-1")
    import pam_pm.web.app as app_mod

    app_mod.SETUP_DONE = False
    app_mod.reset_teams_device_state()
    client = TestClient(app_mod.app)
    yield client, tmp_path, app_mod
    app_mod.reset_teams_device_state()
    app_mod.SETUP_DONE = False


def test_wizard_device_start_and_poll(device_client) -> None:
    client, home, app_mod = device_client
    seen: dict[str, Any] = {}
    app_mod.set_teams_test_transport(_fake_entra(seen))
    saved = client.post(
        "/api/setup/teams",
        json={
            "token": "teams-device-setup-token-1",
            "project": "teamsdev",
            "app_id": APP_ID,
            "app_password": APP_PASSWORD,
            "tenant_id": TENANT_ID,
        },
    )
    assert saved.status_code == 200
    start = client.post(
        "/api/setup/teams/device/start",
        json={"token": "teams-device-setup-token-1", "project": "teamsdev"},
    )
    assert start.status_code == 200
    body = start.json()
    assert body["ok"] is True
    assert body["user_code"] == "ABCD-1234"
    assert body["verification_uri"] == DEVICELOGIN_URL
    assert DEVICE_CODE not in json.dumps(body)
    pending = client.post(
        "/api/setup/teams/device/poll",
        json={"token": "teams-device-setup-token-1", "project": "teamsdev"},
    )
    assert pending.json()["status"] == "pending"
    done = client.post(
        "/api/setup/teams/device/poll",
        json={"token": "teams-device-setup-token-1", "project": "teamsdev"},
    )
    assert done.json()["status"] == "complete"
    assert (home / "teams_refresh_token.txt").read_text(encoding="utf-8") == REFRESH
    status = client.get("/api/setup/teams/device/status?project=teamsdev").json()
    assert status["signed_in"] is True
    blob = (home / "wizard_progress.json").read_text(encoding="utf-8")
    assert REFRESH not in blob
    assert APP_PASSWORD not in blob
    page = client.get("/setup?project=teamsdev").text
    assert REFRESH not in page
    assert "Step 2: Add permissions." in page
    assert "Chat.Read.All" in page
    assert "Grant admin consent" in page
    assert "Download app package" in page
    assert "microsoft.com/devicelogin" not in page


def test_wizard_device_poll_without_start(device_client) -> None:
    client, _, app_mod = device_client
    app_mod.set_teams_test_transport(_fake_entra({}))
    resp = client.post(
        "/api/setup/teams/device/poll",
        json={"token": "teams-device-setup-token-1", "project": "teamsdev"},
    )
    assert resp.status_code == 400
    assert resp.json()["status"] == "missing"

def test_teams_loop_publishes_dm_and_skips_own(tmp_path, monkeypatch) -> None:
    import pam_pm.web.app as app_mod
    from pam_pm.core import secrets as secrets_mod
    from pam_pm.core import store as store_mod

    for key in (*ENV_KEYS, "PAM_SETUP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    app_mod.reset_teams_poll_state()
    app_mod.reset_chat_state()
    try:
        secrets_mod.save_teams_config(
            tmp_path, app_id=APP_ID, app_password=APP_PASSWORD, tenant_id=TENANT_ID
        )
        secrets_mod.save_secret("TEAMS_REFRESH_TOKEN", REFRESH, tmp_path)
        store_mod.set_project_chat_backend(tmp_path / "state.db", "loop1", "teams")
        stub = StubGraph(
            {
                "chat-1": [
                    _graph_message("m-1", "hello pam", "2026-11-02T08:00:00Z"),
                    _graph_message(
                        "m-2", "own send", "2026-11-02T08:01:00Z", sender="pam-bot"
                    ),
                ]
            }
        )
        bus = app_mod.EventBus(seed=[])
        poller = TeamsPoller(
            stub,
            tmp_path / "state.db",
            [{"id": "chat-1", "chatType": "oneOnOne"}],
            interval=60,
            bus=bus,
            bot_id="pam-bot",
        )
        found = poller.poll_once()
        assert [m.id for m in found] == ["m-1"]
        assert bus.history()[-1]["source"] == "teams"
        assert bus.history()[-1]["text"] == "hello pam"
        assert poller.get_watermark("chat-1") == "2026-11-02T08:01:00Z"
    finally:
        app_mod.reset_teams_poll_state()
        app_mod.reset_chat_state()


def test_poll_teams_once_wrapper_uses_bus_and_watermark(tmp_path, monkeypatch) -> None:
    import pam_pm.web.app as app_mod
    from pam_pm.core import secrets as secrets_mod
    from pam_pm.core import store as store_mod

    for key in (*ENV_KEYS, "PAM_SETUP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    app_mod.reset_teams_poll_state()
    app_mod.reset_chat_state()
    try:
        secrets_mod.save_teams_config(
            tmp_path, app_id=APP_ID, app_password=APP_PASSWORD, tenant_id=TENANT_ID
        )
        secrets_mod.save_secret("TEAMS_REFRESH_TOKEN", REFRESH, tmp_path)
        store_mod.set_project_chat_backend(tmp_path / "state.db", "loop2", "teams")
        stub = StubGraph(
            {"chat-9": [_graph_message("m-9", "dm here", "2026-11-02T09:00:00Z")]}
        )
        app_mod.set_teams_test_graph("loop2", stub)
        app_mod.set_teams_test_chats("loop2", [{"id": "chat-9", "chatType": "oneOnOne"}])
        before = len(app_mod.bus.history())
        found = app_mod.poll_teams_once("loop2")
        assert [m.id for m in found] == ["m-9"]
        assert len(app_mod.bus.history()) == before + 1
        assert app_mod.bus.history()[-1]["source"] == "teams"
        assert app_mod.poll_teams_once("loop2") == []
        assert len(app_mod.bus.history()) == before + 1
        poller = app_mod.get_teams_poller("loop2")
        assert poller is not None
        assert poller.get_watermark("chat-9") == "2026-11-02T09:00:00Z"
    finally:
        app_mod.reset_teams_poll_state()
        app_mod.reset_chat_state()


def test_no_refresh_token_means_loop_off(tmp_path, monkeypatch) -> None:
    import pam_pm.web.app as app_mod
    from pam_pm.core import store as store_mod

    for key in (*ENV_KEYS, "PAM_SETUP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    app_mod.reset_teams_poll_state()
    try:
        store_mod.set_project_chat_backend(tmp_path / "state.db", "loopoff", "teams")
        assert app_mod.get_teams_poller("loopoff") is None
        assert app_mod.poll_teams_once("loopoff") == []
    finally:
        app_mod.reset_teams_poll_state()


def test_poller_runs_with_no_refresh_token(tmp_path, monkeypatch) -> None:
    import pam_pm.web.app as app_mod
    from pam_pm.core import secrets as secrets_mod
    from pam_pm.core import store as store_mod

    for key in (*ENV_KEYS, "PAM_SETUP_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    app_mod.reset_teams_poll_state()
    try:
        secrets_mod.save_teams_config(
            tmp_path, app_id=APP_ID, app_password=APP_PASSWORD, tenant_id=TENANT_ID
        )
        store_mod.set_project_chat_backend(tmp_path / "state.db", "loopapp", "teams")
        stub = StubGraph(
            {"chat-1": [_graph_message("m-1", "hello", "2026-11-02T08:00:00Z")]}
        )
        app_mod.set_teams_test_graph("loopapp", stub)
        app_mod.set_teams_test_chats("loopapp", [{"id": "chat-1", "chatType": "oneOnOne"}])
        poller = app_mod.get_teams_poller("loopapp")
        assert poller is not None
        found = app_mod.poll_teams_once("loopapp")
        assert [m.id for m in found] == ["m-1"]
    finally:
        app_mod.reset_teams_poll_state()


def test_rotated_refresh_saved_on_silent_mint(tmp_path, monkeypatch) -> None:
    import os
    import stat

    from pam_pm.adapters.chat import teams_auth as teams_auth_mod
    from pam_pm.core import secrets as secrets_mod

    for key in (*ENV_KEYS,):
        monkeypatch.delenv(key, raising=False)
    teams_auth_mod.store_teams_refresh_token("old-refresh-1", tmp_path)
    seen: dict[str, Any] = {}
    provider = GraphTokenProvider(
        APP_ID, TENANT_ID, data_dir=tmp_path, transport=_fake_entra(seen)
    )
    token = provider.access_token()
    assert token.startswith("graph-access-")
    assert secrets_mod.load_secret("TEAMS_REFRESH_TOKEN", tmp_path) != "old-refresh-1"
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(tmp_path / "teams_refresh_token.txt").st_mode)
        assert mode == 0o600
