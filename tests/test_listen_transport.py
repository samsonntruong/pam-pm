"""LISTEN transport tests: fake realtime server, no network."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pam_pm.core import store as store_mod
from pam_pm.models import realtime as rt_mod


class FakeMetaSocket:
    """In-memory stand-in for the Meta realtime WebSocket."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self._incoming: asyncio.Queue[Any] = asyncio.Queue()
        for frame in script or []:
            self._incoming.put_nowait(frame)
        self.closed = False

    async def send(self, data: Any) -> None:
        if isinstance(data, (bytes, bytearray)):
            self.sent_bytes.append(bytes(data))
        else:
            self.sent_text.append(str(data))

    async def recv(self) -> Any:
        item = await self._incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    def feed(self, frame: Any) -> None:
        self._incoming.put_nowait(frame)


def _connect_factory(sock: FakeMetaSocket, seen: dict[str, Any]):
    async def _connect(url: str) -> FakeMetaSocket:
        seen["url"] = url
        return sock

    return _connect


def _txt(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def test_handshake_shape_and_redaction(monkeypatch) -> None:
    monkeypatch.setenv("META_API_KEY", "secret-key-1234")
    hs = rt_mod.build_handshake()
    assert hs["authorization"]["accessToken"] == "Bearer secret-key-1234"
    assert hs["model"] == rt_mod.TRANSCRIBE_MODEL
    assert hs["audioEncoding"] == "PCM_24KHZ"
    logged = rt_mod.redact_for_log(hs, "secret-key-1234")
    assert "secret-key-1234" not in json.dumps(logged)
    assert rt_mod.redact_text("Bearer secret-key-1234 ok") == "Bearer [REDACTED] ok"


def test_default_mode_is_endpointing(monkeypatch) -> None:
    monkeypatch.setenv("META_API_KEY", "test-key")
    assert rt_mod.build_handshake()["mode"] == "ENDPOINTING"
    assert rt_mod.RealtimeClient("s-def", "demo", None).mode == "ENDPOINTING"
    explicit = rt_mod.RealtimeClient("s-ptt", "demo", None, mode="PUSH_TO_TALK")
    assert explicit.mode == "PUSH_TO_TALK"


def test_pcm_framing_helpers() -> None:
    assert rt_mod.FRAME_BYTES == 3840
    blob = b"\x01\x02" * 5000
    frames = rt_mod.frame_pcm(blob)
    assert frames[0] == blob[:3840]
    assert b"".join(frames) == blob
    assert rt_mod.pcm_silence(80) == b"\x00" * 3840
    assert rt_mod.pcm_duration_ms(b"\x00" * 48000) == pytest.approx(1000.0)
    assert rt_mod.ensure_pcm16(b"\x01") == b"\x01\x00"


def test_cumulative_partials_replace() -> None:
    acc = rt_mod.TranscriptAccumulator()
    first = acc.handle_event({"type": "transcript", "text": "hello", "final": False})
    assert first.kind == "partial" and acc.partial_text == "hello"
    second = acc.handle_event(
        {"type": "transcript", "text": "hello world", "final": False}
    )
    assert second.kind == "partial" and acc.partial_text == "hello world"


def test_final_via_flag_and_speech_complete() -> None:
    acc = rt_mod.TranscriptAccumulator()
    acc.handle_event({"type": "speechStart", "turnId": "t1"})
    acc.handle_event({"type": "speaker", "turnId": "t1", "label": "A"})
    final = acc.handle_event(
        {"type": "transcript", "text": "done deal", "final": True, "turnId": "t1"}
    )
    assert final.kind == "final"
    assert (final.text, final.turn_id, final.speaker) == ("done deal", "t1", "A")
    assert acc.partial_text == ""
    acc2 = rt_mod.TranscriptAccumulator()
    acc2.handle_event({"type": "speechStart", "turnId": "t2"})
    done = acc2.handle_event(
        {"type": "speechComplete", "turnId": "t2", "text": "All set.",
         "startMs": 100, "endMs": 900}
    )
    assert done.kind == "final" and done.start_ms == 100 and done.end_ms == 900


def test_audio_buffer_caps_at_30s() -> None:
    buf = rt_mod.AudioBuffer()
    buf.append(b"\x01" * (rt_mod.BUFFER_MAX_BYTES + 1000))
    assert len(buf) == rt_mod.BUFFER_MAX_BYTES
    assert buf.snapshot() == b"\x01" * rt_mod.BUFFER_MAX_BYTES
    assert rt_mod.backoff_delay(0) == 1.0
    assert rt_mod.backoff_delay(2) == 4.0


def test_notes_builder_prefix_stable() -> None:
    first = rt_mod.build_notes_messages("Project Atlas.", "alpha bravo", {"now": "a"})
    second = rt_mod.build_notes_messages("Project Atlas.", "completely other", {"now": "b"})
    assert first["messages"][0] == second["messages"][0]
    assert first["messages"][1] == second["messages"][1]
    assert first["messages"][2] != second["messages"][2]
    assert "transcript" in first["messages"][2]["content"].lower()
    assert "Live context" in first["messages"][3]["content"]


@pytest.mark.parametrize("backend", ["pam_board", "azure_devops"])
def test_client_stream_persists_finals(tmp_path, monkeypatch, backend) -> None:
    monkeypatch.setenv("META_API_KEY", "test-key")
    db = tmp_path / f"listen-{backend}.db"
    store_mod.set_project_backend(db, "demo", backend)
    store_mod.create_listen_session(db, "s1", "demo", "Standup")
    script = [
        _txt({"sessionId": "srv-1"}),
        _txt({"type": "speechStart", "turnId": "t1"}),
        _txt({"type": "transcript", "text": "zim zam", "final": False}),
        _txt({"type": "transcript", "text": "zim zam wobble",
              "final": False}),
        _txt({"type": "transcript", "text": "zim zam wobble.",
              "final": True, "turnId": "t1"}),
    ]
    sock = FakeMetaSocket(script)
    seen: dict[str, Any] = {}
    client = rt_mod.RealtimeClient(
        "s1", "demo", db, connect_fn=_connect_factory(sock, seen)
    )

    async def _run() -> list[str]:
        await client.connect()
        kinds = []
        await client.send_audio(b"\x01\x02" * 2000)
        for _ in range(4):
            update = await client.recv_event(timeout=2.0)
            assert update is not None
            kinds.append(update.kind)
        await client.close()
        return kinds

    kinds = asyncio.run(_run())
    assert seen["url"] == rt_mod.REALTIME_URL
    assert kinds == ["progress", "partial", "partial", "final"]
    assert client.accumulator.partial_text == ""
    assert sock.sent_bytes, "audio must reach the fake server"
    hs = json.loads(sock.sent_text[0])
    assert hs["authorization"]["accessToken"] == "Bearer test-key"
    segments = store_mod.list_listen_segments(db, "s1")
    assert len(segments) == 1
    assert segments[0]["text"] == "zim zam wobble."
    assert segments[0]["turn_id"] == "t1"
    assert store_mod.get_listen_session(db, "s1")["backend"] == backend


def test_client_resend_after_reconnect(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("META_API_KEY", "test-key")
    db = tmp_path / "resend.db"
    sock = FakeMetaSocket([_txt({"sessionId": "srv-9"})])
    seen: dict[str, Any] = {}
    client = rt_mod.RealtimeClient(
        "s9", "demo", db, connect_fn=_connect_factory(sock, seen)
    )

    async def _run() -> None:
        await client.connect()
        await client.send_audio(b"\x05\x06" * 2000)
        assert len(client.buffer) > 0
        sock.feed(_txt({"sessionId": "srv-9b"}))
        ok = await client.reconnect_with_resend(max_attempts=1)
        assert ok is True
        await client.close()

    asyncio.run(_run())
    assert len(sock.sent_bytes) >= 2, "resend must replay buffered audio"


def test_ws_listen_rejects_bad_token(monkeypatch) -> None:
    import pam_pm.web.app as app_mod

    monkeypatch.setattr(app_mod, "SETUP_TOKEN", "good-token")
    client = TestClient(app_mod.app)
    with pytest.raises(Exception), client.websocket_connect(  # noqa: B017
        "/ws/listen?token=bad-token"
    ):
        pass


def test_ws_listen_relays_audio_and_transcript(tmp_path, monkeypatch) -> None:
    import pam_pm.web.app as app_mod

    monkeypatch.setenv("META_API_KEY", "test-key")
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setattr(app_mod, "SETUP_TOKEN", "good-token")
    sock = FakeMetaSocket([
        _txt({"sessionId": "srv-ws"}),
        _txt({"type": "transcript", "text": "plim plam", "final": False}),
        _txt({"type": "speechStart", "turnId": "t7"}),
        _txt({"type": "speechComplete", "turnId": "t7",
              "text": "Plim plam plo."}),
    ])
    monkeypatch.setattr(
        app_mod, "LISTEN_CONNECT_FN", _connect_factory(sock, {})
    )
    client = TestClient(app_mod.app)
    with client.websocket_connect(
        "/ws/listen?token=good-token&project=demo&session_id=ws1"
    ) as ws:
        status = ws.receive_json()
        assert status["state"] == "transcribing"
        ws.send_bytes(b"\x01\x02" * 2000)
        got_partial = ws.receive_json()
        assert got_partial["type"] == "partial"
        assert got_partial["text"] == "plim plam"
        got_final = ws.receive_json()
        assert got_final["type"] == "final"
        assert got_final["text"] == "Plim plam plo."
        ws.send_text(json.dumps({"type": "end"}))
    assert sock.sent_bytes, "browser audio must reach realtime"
    from pam_pm.core.config import Config

    db = Config().db_path()
    segments = store_mod.list_listen_segments(db, "ws1")
    assert [s["text"] for s in segments] == ["Plim plam plo."]
    assert all(b"test-key" not in blob for blob in sock.sent_bytes)
