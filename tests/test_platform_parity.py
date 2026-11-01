"""Cross-platform parity: autostart, perms, tray fallback."""

from __future__ import annotations

import os
import stat
import sys

import pytest

from pam_pm import autostart, desktop


def test_autostart_backends_exist():
    assert callable(autostart.enable)
    assert callable(autostart.disable)
    assert callable(autostart.is_enabled)
    assert "Restart=on-failure" in autostart.systemd_unit_content()
    assert "LaunchAgent" in autostart.plist_content() or "Label" in autostart.plist_content()
    assert "<Task" in autostart.windows_task_xml()


def test_windows_task_xml_keeps_spaced_exe_intact(monkeypatch):
    monkeypatch.setattr(autostart, "pam_command", lambda: "C:\\Program Files\\pam\\pam.exe")
    monkeypatch.setattr(autostart, "start_args", lambda: ["start", "--no-browser"])
    xml = autostart.windows_task_xml()
    assert "C:\\Program Files\\pam\\pam.exe" in xml
    assert "start --no-browser" in xml or "start" in xml


def test_systemd_execstart_quotes_spaced_exe(monkeypatch):
    monkeypatch.setattr(autostart, "pam_command", lambda: "/opt/my tools/pam")
    monkeypatch.setattr(autostart, "start_args", lambda: ["start", "--no-browser"])
    assert '"/opt/my tools/pam"' in autostart.systemd_unit_content()


def test_home_ignores_bogus_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", "/bogus/should/not/be/used")
    monkeypatch.setattr(autostart.Path, "home", classmethod(lambda cls: tmp_path))
    assert str(autostart.launch_agent_path()).startswith(str(tmp_path))
    assert str(autostart.systemd_unit_path()).startswith(str(tmp_path))


def test_pid_alive_permission_means_alive(monkeypatch):
    def _deny(pid, sig):
        raise PermissionError("denied")
    monkeypatch.setattr(desktop.os, "kill", _deny)
    assert desktop.pid_alive(1234) is True


def test_loaders_return_none_on_any_error(monkeypatch):
    import builtins
    real_import = builtins.__import__
    def _boom(name, *a, **k):
        if name in ("webview", "pystray"):
            raise OSError("no display")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _boom)
    assert desktop.load_webview() is None
    assert desktop.load_pystray() is None


def test_create_window_falls_back_when_backend_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    opened = []
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    class _Boom:
        def create_window(self, *a, **k):
            raise RuntimeError("no webview backend")
    assert desktop.create_window("http://127.0.0.1:8765/board", view=_Boom()) is None
    assert opened == ["http://127.0.0.1:8765/board"]


def test_start_server_detaches_on_windows(monkeypatch, tmp_path):
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    # Windows-only subprocess constants do not exist on Linux; stub them
    # so the win32 branch in start_server_subprocess is exercised.
    monkeypatch.setattr(
        desktop.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
    )
    monkeypatch.setattr(desktop.subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(
        desktop.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False
    )
    seen = {}
    class _P:
        def __init__(self, *a, **k):
            seen.update(k)
    monkeypatch.setattr(desktop.subprocess, "Popen", _P)
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    desktop.start_server_subprocess()
    assert "creationflags" in seen


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only")
def test_secret_file_mode_0600(tmp_path):
    from pam_pm.core import secrets as secrets_mod
    secrets_mod.save_secret("META_API_KEY", "parity-key-1", tmp_path)
    assert stat.S_IMODE((tmp_path / "meta_api_key.txt").stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only")
def test_auth_file_mode_0600(tmp_path):
    from pam_pm.core import auth as auth_mod
    auth_mod.set_password("parity-pass-1", tmp_path)
    assert stat.S_IMODE((tmp_path / auth_mod.AUTH_FILENAME).stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only detach flag")
def test_start_server_new_session_on_posix(monkeypatch, tmp_path):
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    seen = {}
    class _P:
        def __init__(self, *a, **k):
            seen.update(k)
    monkeypatch.setattr(desktop.subprocess, "Popen", _P)
    monkeypatch.setattr(desktop.os, "name", "posix")
    desktop.start_server_subprocess()
    assert seen.get("start_new_session") is True
