"""Tests for the desktop wrapper. GUI libraries are always mocked."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error

import pytest

from pam_pm import autostart, cli, desktop


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    return tmp_path


class _FakeResp:
    status = 200

    def __init__(self, body: bytes = b'{"status": "ok"}') -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _Closing:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, func):  # type: ignore[no-untyped-def]
        self.handlers.append(func)
        return self


class _Events:
    def __init__(self) -> None:
        self.closing = _Closing()


class _FakeWindow:
    def __init__(self) -> None:
        self.events = _Events()
        self.hidden = 0
        self.shown = 0
        self.restored = 0
        self.destroyed = 0

    def hide(self) -> None:
        self.hidden += 1

    def show(self) -> None:
        self.shown += 1

    def restore(self) -> None:
        self.restored += 1

    def destroy(self) -> None:
        self.destroyed += 1


class _FakeWebview:
    def __init__(self) -> None:
        self.calls: list = []
        self.started = 0

    def create_window(self, title, url, **kwargs):  # type: ignore[no-untyped-def]
        window = _FakeWindow()
        self.calls.append({"title": title, "url": url, "kwargs": kwargs})
        return window

    def start(self) -> None:
        self.started += 1


class _FakeProc:
    def __init__(self) -> None:
        self.terminated = 0
        self.killed = 0

    def terminate(self) -> None:
        self.terminated += 1

    def wait(self, timeout=None) -> int:  # type: ignore[no-untyped-def]
        return 0

    def kill(self) -> None:
        self.killed += 1


class _FakeTray:
    def __init__(self) -> None:
        self.stopped = 0
        self.title = ""
        self.icon = None
        self.menu = None

    def stop(self) -> None:
        self.stopped += 1

    def run(self) -> None:
        pass

    def update_menu(self) -> None:
        pass


def test_import_needs_no_gui_libs():
    assert "webview" not in sys.modules
    assert "pystray" not in sys.modules
    assert desktop.WINDOW_TITLE == "Pam"
    assert callable(desktop.main)


def test_server_running_true(monkeypatch):
    monkeypatch.setattr(
        desktop.urllib.request, "urlopen", lambda *a, **k: _FakeResp()
    )
    assert desktop.is_server_running() is True


def test_server_running_false_on_error(monkeypatch):
    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(desktop.urllib.request, "urlopen", _boom)
    assert desktop.is_server_running() is False


def test_server_running_false_on_bad_body(monkeypatch):
    monkeypatch.setattr(
        desktop.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResp(b"{}"),
    )
    assert desktop.is_server_running() is False


def test_lock_round_trip():
    assert desktop.is_desktop_running() is False
    assert desktop.acquire_lock() is True
    assert desktop.is_desktop_running() is False
    assert desktop.acquire_lock() is True
    desktop.release_lock()
    assert desktop.lock_path().exists() is False


def test_lock_blocks_foreign_live_pid():
    parent = os.getppid()
    assert parent != os.getpid()
    desktop.lock_path().write_text(str(parent), encoding="utf-8")
    assert desktop.is_desktop_running() is True
    assert desktop.acquire_lock() is False
    desktop.release_lock()
    assert desktop.lock_path().exists() is True


def test_lock_ignores_stale_pid():
    desktop.lock_path().write_text("999999999", encoding="utf-8")
    if desktop.pid_alive(999999999):
        pytest.skip("stale pid is alive on this machine")
    assert desktop.is_desktop_running() is False
    assert desktop.acquire_lock() is True


def test_lock_ignores_corrupt_file():
    desktop.lock_path().write_text("not-a-pid", encoding="utf-8")
    assert desktop.read_lock_pid() is None
    assert desktop.is_desktop_running() is False
    assert desktop.acquire_lock() is True


def test_create_window_falls_back_to_browser(monkeypatch, capsys):
    opened: list = []
    monkeypatch.setattr(desktop, "load_webview", lambda: None)
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    assert desktop.create_window("http://127.0.0.1:8765/board") is None
    assert opened == ["http://127.0.0.1:8765/board"]
    out = capsys.readouterr().out
    assert "browser" in out
    assert "desktop extra" in out


def test_create_window_uses_title_and_hides_on_close(monkeypatch):
    monkeypatch.setattr(desktop, "ensure_icon_file", lambda *a, **k: None)
    view = _FakeWebview()
    window = desktop.create_window("http://127.0.0.1:8765/board", view=view)
    assert isinstance(window, _FakeWindow)
    assert view.calls[0]["title"] == "Pam"
    assert view.calls[0]["url"] == "http://127.0.0.1:8765/board"
    assert len(window.events.closing.handlers) == 1
    assert window.events.closing.handlers[0]() is False
    assert window.hidden == 1


def test_create_window_passes_icon_when_supported(monkeypatch, tmp_path):
    class _IconWebview(_FakeWebview):
        def create_window(self, title, url, icon=None):  # type: ignore[no-untyped-def]
            return super().create_window(title, url, icon=icon)

    monkeypatch.setattr(desktop, "ensure_icon_file", lambda *a, **k: str(tmp_path / "pam.png"))
    view = _IconWebview()
    desktop.create_window("http://127.0.0.1:8765/board", view=view)
    assert view.calls[0]["kwargs"] == {"icon": str(tmp_path / "pam.png")}


def test_menu_items_labels_and_states():
    items = desktop.menu_items(paused=False, autostart_on=True)
    assert [i["label"] for i in items] == [
        "Open Pam",
        "Pause check-ins",
        "Start at sign-in",
        "Quit",
    ]
    assert [i["id"] for i in items] == ["open", "pause", "autostart", "quit"]
    assert items[2]["checked"] is True
    assert items[0]["checked"] is None

    paused_items = desktop.menu_items(paused=True, autostart_on=False)
    assert paused_items[1]["label"] == "Resume check-ins"
    assert paused_items[2]["checked"] is False


def test_pause_toggle_round_trip(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        desktop, "_notify_pause_rule", lambda *a, **k: calls.append(a)
    )
    assert desktop.is_checkins_paused() is False
    assert desktop.set_checkins_paused(True) is True
    assert desktop.pause_marker_path().exists() is True
    assert desktop.toggle_checkins_paused() is False
    assert desktop.is_checkins_paused() is False
    assert len(calls) == 2


def test_pause_notifies_morning_rule(monkeypatch):
    seen: list = []

    class _Req:
        def __init__(self, url, data=None, headers=None, method=None):  # type: ignore[no-untyped-def]
            seen.append({"url": url, "data": data})

    monkeypatch.setattr(desktop.urllib.request, "Request", _Req)
    monkeypatch.setattr(
        desktop.urllib.request, "urlopen", lambda *a, **k: _FakeResp(b"{}")
    )
    desktop.set_checkins_paused(True)
    assert seen[0]["url"].endswith("/api/rules/toggle")
    payload = json.loads(seen[0]["data"].decode("utf-8"))
    assert payload == {"name": "morning", "enabled": False}


def test_tray_status_mapping(monkeypatch):
    monkeypatch.setattr(desktop, "is_server_running", lambda *a, **k: False)
    assert desktop.tray_status() == "offline"
    monkeypatch.setattr(desktop, "is_server_running", lambda *a, **k: True)
    assert desktop.tray_status() == "running"
    desktop.pause_marker_path().write_text("paused\n", encoding="utf-8")
    assert desktop.tray_status() == "listening"


def test_ensure_server_already_running(monkeypatch):
    monkeypatch.setattr(desktop, "is_server_running", lambda *a, **k: True)

    def _fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("must not start a second server")

    monkeypatch.setattr(desktop, "start_server_subprocess", _fail)
    assert desktop.ensure_server() is None


def test_ensure_server_starts_when_down(monkeypatch):
    proc = _FakeProc()
    monkeypatch.setattr(desktop, "is_server_running", lambda *a, **k: False)
    monkeypatch.setattr(desktop, "start_server_subprocess", lambda *a, **k: proc)
    monkeypatch.setattr(desktop, "wait_for_server", lambda *a, **k: True)
    assert desktop.ensure_server() is proc


def test_ensure_server_raises_when_it_will_not_come_up(monkeypatch):
    proc = _FakeProc()
    monkeypatch.setattr(desktop, "is_server_running", lambda *a, **k: False)
    monkeypatch.setattr(desktop, "start_server_subprocess", lambda *a, **k: proc)
    monkeypatch.setattr(desktop, "wait_for_server", lambda *a, **k: False)
    with pytest.raises(RuntimeError, match="did not start"):
        desktop.ensure_server()
    assert proc.terminated == 1


def test_app_open_window_prefers_window(monkeypatch):
    app = desktop.DesktopApp()
    app.window = _FakeWindow()

    def _fail(url):  # type: ignore[no-untyped-def]
        raise AssertionError("must not open the browser")

    monkeypatch.setattr(desktop.webbrowser, "open", _fail)
    app.open_window()
    assert app.window.shown == 1
    assert app.window.restored == 1


def test_app_open_window_browser_fallback(monkeypatch):
    opened: list = []
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    desktop.DesktopApp().open_window()
    assert opened == ["http://127.0.0.1:8765/board"]


def test_app_toggle_pause(monkeypatch, capsys):
    monkeypatch.setattr(
        desktop, "_notify_pause_rule", lambda *a, **k: None
    )
    monkeypatch.setattr(desktop, "refresh_tray", lambda app: None)
    app = desktop.DesktopApp()
    assert app.toggle_pause() is True
    assert "paused" in capsys.readouterr().out
    assert app.toggle_pause() is False
    assert "resumed" in capsys.readouterr().out


def test_app_toggle_autostart(monkeypatch, capsys):
    monkeypatch.setattr(desktop, "refresh_tray", lambda app: None)
    monkeypatch.setattr(autostart, "is_enabled", lambda: False)
    monkeypatch.setattr(autostart, "enable", lambda: "autostart: on")
    app = desktop.DesktopApp()
    assert app.toggle_autostart() == "autostart: on"
    assert "on" in capsys.readouterr().out


def test_app_quit_stops_owned_server(monkeypatch, capsys):
    proc = _FakeProc()
    stopped: list = []
    monkeypatch.setattr(desktop, "stop_process", stopped.append)
    app = desktop.DesktopApp()
    app.tray = _FakeTray()
    app.window = _FakeWindow()
    app.proc = proc
    app.started_server = True
    desktop.lock_path().write_text(str(os.getpid()), encoding="utf-8")
    app.request_quit()
    assert stopped == [proc]
    assert desktop.lock_path().exists() is False
    assert "Pam stopped" in capsys.readouterr().out


def test_app_quit_leaves_foreign_server(monkeypatch, capsys):
    def _fail(proc):  # type: ignore[no-untyped-def]
        raise AssertionError("must not stop a foreign server")

    monkeypatch.setattr(desktop, "stop_process", _fail)
    app = desktop.DesktopApp()
    app.tray = _FakeTray()
    app.request_quit()
    assert "still running" in capsys.readouterr().out


def test_autostart_prefers_desktop_when_extra_installed(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    assert autostart.desktop_available() is True
    assert autostart.start_args() == ["desktop"]
    assert autostart.start_command_line().endswith(" desktop")
    assert "<string>desktop</string>" in autostart.plist_content()


def test_autostart_falls_back_to_start(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert autostart.desktop_available() is False
    assert autostart.start_args() == ["start", "--no-browser"]
    assert autostart.start_command_line().endswith(" start --no-browser")
    assert "<string>start</string>" in autostart.plist_content()


def test_cli_desktop_wiring(monkeypatch):
    args = cli.build_parser().parse_args(["desktop", "--port", "9999"])
    assert args.command == "desktop"
    assert args.port == 9999
    calls: list = []

    def _fake_main(host="h", port=1):  # type: ignore[no-untyped-def]
        calls.append((host, port))
        return 0

    monkeypatch.setattr(desktop, "main", _fake_main)
    cli.cmd_desktop(args)
    assert calls == [("127.0.0.1", 9999)]
    cli.main(["desktop"])
    assert calls == [("127.0.0.1", 9999), ("127.0.0.1", 8765)]


def test_cli_desktop_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr(desktop, "main", lambda host, port: 1)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_desktop(argparse.Namespace(host="127.0.0.1", port=8765))
    assert exc.value.code == 1
