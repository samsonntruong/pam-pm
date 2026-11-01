"""LISTEN page test: real Chromium with fake media, synthetic wav, both backends.

Serves the app with uvicorn, drives /listen with Playwright, streams a
synthetic 24 kHz wav through the page capture hook, and checks finals,
notes, and the approval card. Fails when Chromium cannot launch.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import socket
import struct
import threading
import time
from typing import Any

import pytest

BACKENDS = ["pam_board", "azure_devops"]


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _synth_wav(seconds: float = 1.0, freq: float = 440.0) -> bytes:
    rate = 24000
    frames = int(rate * seconds)
    pcm = bytearray()
    for i in range(frames):
        sample = int(12000 * math.sin(2 * math.pi * freq * i / rate))
        pcm += struct.pack("<h", sample)
    data = bytes(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16, 1, 1,
        rate, rate * 2, 2, 16, b"data", len(data),
    )
    return header + data


class FakeMetaSocket:
    def __init__(self, script: list[Any]) -> None:
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self._incoming: asyncio.Queue[Any] = asyncio.Queue()
        for frame in script:
            self._incoming.put_nowait(frame)

    async def send(self, data: Any) -> None:
        if isinstance(data, (bytes, bytearray)):
            self.sent_bytes.append(bytes(data))
        else:
            self.sent_text.append(str(data))

    async def recv(self) -> Any:
        return await self._incoming.get()

    async def close(self) -> None:
        return None


def _txt(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


@pytest.mark.parametrize("backend", BACKENDS)
def test_listen_page_fake_media_both_backends(tmp_path, monkeypatch, backend) -> None:
    import uvicorn
    from playwright.sync_api import sync_playwright

    import pam_pm.web.app as app_mod
    from pam_pm.core import store as store_mod
    from pam_pm.core.config import Config

    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("META_API_KEY", "test-key")
    monkeypatch.setattr(app_mod, "SETUP_TOKEN", "page-token")
    db = Config().db_path()
    store_mod.set_project_backend(db, "demo", backend)

    wav = _synth_wav()
    wav_path = app_mod.STATIC_DIR / "__test_tone.wav"
    wav_path.write_bytes(wav)

    script = [
        _txt({"sessionId": "srv-page"}),
        _txt({"type": "transcript", "text": "zim zam", "final": False}),
        _txt({"type": "speechStart", "turnId": "t1"}),
        _txt({"type": "speechComplete", "turnId": "t1", "text": "Zim zam wobble."}),
    ]
    sock = FakeMetaSocket(script)

    async def _connect(url: str) -> FakeMetaSocket:
        return sock

    monkeypatch.setattr(app_mod, "LISTEN_CONNECT_FN", _connect)

    port = _free_port()
    config = uvicorn.Config(app_mod.app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not getattr(server, "started", False):
            time.sleep(0.1)
        assert getattr(server, "started", False), "uvicorn did not start"

        launch_kwargs: dict[str, Any] = {
            "args": [
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
            ]
        }
        try:
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                launch_kwargs["args"].append("--no-sandbox")
        except OSError:
            pass
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch_kwargs)
            try:
                page = browser.new_page()
                page.goto(
                    f"http://127.0.0.1:{port}/listen?project=demo",
                    wait_until="domcontentloaded",
                    timeout=10000,
                )
                assert "Listen" in page.title()
                page.click("#listen-start")
                page.wait_for_function(
                    "() => window.pamListen && pamListen.state === 'Transcribing'",
                    timeout=10000,
                )
                sent = page.evaluate(
                    "() => window.pamListen.sendTestAudio('/static/__test_tone.wav')"
                )
                assert sent > 0
                page.wait_for_function(
                    "() => window.pamListen.finals().length > 0",
                    timeout=10000,
                )
                finals = page.evaluate("() => window.pamListen.finals()")
                assert any("Zim zam wobble" in f for f in finals)
                session_id = page.evaluate("() => window.pamListen.sessionId")
                assert session_id
                page.evaluate("() => window.pamListen.stop()")
                page.wait_for_selector("#listen-notes-link a", timeout=10000)
                page.click("#listen-notes-link a")
                page.wait_for_selector("text=Generate notes", timeout=10000)
                page.click("text=Generate notes")
                page.wait_for_selector("text=Approval card", timeout=10000)
                body = page.content()
                assert "Approval card" in body
                assert "Zim zam" in body or "zim" in body.lower()
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        try:
            wav_path.unlink()
        except OSError:
            pass

    assert sock.sent_bytes, "page audio must reach realtime"
    segments = store_mod.list_listen_segments(db, session_id)
    assert [s["text"] for s in segments] == ["Zim zam wobble."]
