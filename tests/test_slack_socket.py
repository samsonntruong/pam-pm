"""Slack Socket Mode wiring on fakes: resume, shared dedupe, fallback, reconnect."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from pam_pm.adapters.chat.slack_socket import SocketModeInbound
from pam_pm.web import app as web_app


class FakeSocketClient:
    """Fake SocketModeClient: stays connected, records acks."""

    def __init__(self) -> None:
        self.socket_mode_request_listeners: list[Any] = []
        self.acks: list[dict[str, Any]] = []
        self.connected = False
        self.closed = False

    def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected and not self.closed

    def close(self) -> None:
        self.closed = True

    def send_socket_mode_response(self, payload: dict[str, Any]) -> None:
        self.acks.append(dict(payload))


class FakeChat:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []


def _envelope(
    event_id: str = "Ev123", channel: str = "D123", ts: str = "114.1"
) -> dict[str, Any]:
    return {
        "type": "event_callback",
        "event_id": event_id,
        "event": {
            "type": "message",
            "channel": channel,
            "user": "U123",
            "text": "hello",
            "ts": ts,
        },
    }


@pytest.fixture(autouse=True)
def _clean_slack_state():
    web_app.reset_slack_events()
    web_app.stop_slack_socket()
    web_app.stop_slack_polling()
    yield
    web_app.stop_slack_socket()
    web_app.stop_slack_polling()
    web_app.reset_slack_events()


def _pin_resume(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_handle(envelope: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append({"envelope": dict(envelope), "kwargs": kwargs})
        return {"ok": True, "kind": "message", "replied": True}

    monkeypatch.setattr(web_app, "get_chat_adapter", lambda project="default": FakeChat())
    monkeypatch.setattr(web_app, "get_board_adapter", lambda project="default": object())
    monkeypatch.setattr(web_app, "_resume_model_client", lambda project="default": None)
    monkeypatch.setattr(web_app.router_mod, "handle_slack_event", _fake_handle)
    return calls


def _wait_for(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return bool(condition())


def test_socket_event_reaches_resume(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _pin_resume(monkeypatch)
    made: list[FakeSocketClient] = []

    def _factory(app_token: str, web_client: Any = None) -> FakeSocketClient:
        client = FakeSocketClient()
        made.append(client)
        return client

    with caplog.at_level("INFO", logger="pam_pm.web.app"):
        inbound = web_app.start_slack_socket(
            client_factory=_factory, app_token="xapp-test"
        )
        assert inbound is not None
        assert inbound.wait_until_live(timeout=5)
        assert web_app.slack_socket_healthy() is True
        assert _wait_for(
            lambda: "slack socket live, poller still active" in caplog.text
        )

    envelope = _envelope()
    out = inbound.handle_request(
        made[0],
        {"type": "events_api", "envelope_id": "env-1", "payload": envelope},
    )
    assert out == envelope
    assert len(calls) == 1
    assert calls[0]["envelope"]["event_id"] == "Ev123"
    assert made[0].acks == [{"envelope_id": "env-1"}]
    assert "slack:D123:114.1" in web_app._SEEN_SLACK_EVENT_IDS


def test_socket_dedupe_shared_with_poller(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _pin_resume(monkeypatch)
    client = FakeSocketClient()
    inbound = SocketModeInbound("xapp-test", lambda payload: calls and None)
    assert inbound is not None

    def _route(payload: dict[str, Any]) -> dict[str, Any]:
        return web_app._route_socket_envelope(payload, "default")

    first = _route(_envelope())
    assert len(calls) == 1
    assert first.get("replied") is True

    second = _route(_envelope())
    assert second.get("duplicate") is True
    assert len(calls) == 1

    web_app._remember_slack_event("slack:D999:991.1")
    assert web_app._route_socket_envelope(
        _envelope(event_id="Ev999", channel="D999", ts="991.1"), "default"
    ).get("duplicate") is True
    assert len(calls) == 1
    assert client.acks == []


def test_poller_always_active_while_socket_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polls: list[str] = []

    def _fake_poll(project: str = "default") -> list[Any]:
        polls.append(project)
        return []

    async def _no_sleep(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(web_app, "poll_slack_once", _fake_poll)
    monkeypatch.setattr(web_app, "_effective_poll_interval", lambda: 5)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    class _Live:
        is_healthy = True

    class _Down:
        is_healthy = False

    monkeypatch.setattr(web_app, "_SLACK_SOCKET", _Live())
    assert web_app.slack_socket_healthy() is True
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(web_app._slack_poll_loop())
    assert polls == ["default"]

    monkeypatch.setattr(web_app, "_SLACK_SOCKET", _Down())
    assert web_app.slack_socket_healthy() is False
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(web_app._slack_poll_loop())
    assert polls == ["default", "default"]


def test_socket_and_poller_share_dedupe_no_double_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _pin_resume(monkeypatch)
    first = web_app._route_socket_envelope(_envelope(), "default")
    assert first.get("replied") is True
    assert len(calls) == 1
    assert web_app._remember_slack_event("slack:D123:114.1") is False

    web_app._remember_slack_event("slack:D456:991.2")
    dup = web_app._route_socket_envelope(
        _envelope(event_id="Ev456", channel="D456", ts="991.2"), "default"
    )
    assert dup.get("duplicate") is True
    assert len(calls) == 1


def test_socket_envelope_receipt_and_routing_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _pin_resume(monkeypatch)
    received: list[dict[str, Any]] = []
    inbound = SocketModeInbound(
        "xapp-test", lambda payload: received.append(payload)
    )
    client = FakeSocketClient()
    with caplog.at_level("INFO"):
        inbound.handle_request(
            client,
            {"type": "events_api", "envelope_id": "env-9", "payload": _envelope()},
        )
        inbound.handle_request(
            client, {"type": "disconnect", "envelope_id": "env-10"}
        )
    assert "slack socket envelope received: type=events_api" in caplog.text
    assert "slack socket envelope received: type=disconnect" in caplog.text
    assert len(received) == 1
    assert client.acks == [{"envelope_id": "env-9"}, {"envelope_id": "env-10"}]

    with caplog.at_level("INFO", logger="pam_pm.web.app"):
        result = web_app._route_socket_envelope(_envelope(), "default")
    assert result.get("replied") is True
    assert "decision=resume" in caplog.text
    with caplog.at_level("INFO", logger="pam_pm.web.app"):
        again = web_app._route_socket_envelope(_envelope(), "default")
    assert again.get("duplicate") is True
    assert "decision=ignored" in caplog.text


def test_socket_reconnects_after_drop() -> None:
    statuses: list[bool] = []
    clients: list[FakeSocketClient] = []
    drop = threading.Event()
    second_up = threading.Event()

    class _Flaky(FakeSocketClient):
        def is_connected(self) -> bool:
            if len(clients) == 1 and drop.is_set():
                return False
            return super().is_connected()

    def _factory(app_token: str, web_client: Any = None) -> _Flaky:
        client = _Flaky()
        clients.append(client)
        if len(clients) >= 2:
            second_up.set()
        return client

    inbound = SocketModeInbound(
        "xapp-test",
        lambda payload: None,
        client_factory=_factory,
        on_status=statuses.append,
        sleep=lambda _s: time.sleep(0.005),
        health_interval=0.01,
    )
    try:
        inbound.start()
        assert inbound.wait_until_live(timeout=5)
        drop.set()
        assert second_up.wait(timeout=5)
        assert inbound.wait_until_live(timeout=5)
    finally:
        inbound.stop()
    assert statuses[0] is True
    assert len(clients) >= 2


def test_socket_not_started_without_app_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_app, "slack_app_token", lambda: "")
    assert web_app.slack_socket_configured() is False
    assert web_app.start_slack_socket() is None
    assert web_app.slack_socket_healthy() is False
