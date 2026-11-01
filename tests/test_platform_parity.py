"""Cross-platform parity: autostart, perms, tray fallback."""

from __future__ import annotations

import os
import stat
import sys

import pytest

from pam_pm import autostart, desktop, launchers


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


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_repo_script(name):
    with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def test_windows_shortcut_script_points_at_desktop_with_icon(tmp_path):
    script = launchers.windows_shortcut_script(
        "C:\\Program Files\\pam\\pam.exe",
        str(tmp_path / "Pam.lnk"),
        str(tmp_path / "pam-logo.ico"),
    )
    assert "WScript.Shell" in script
    assert "CreateShortcut" in script
    assert "C:\\Program Files\\pam\\pam.exe" in script
    assert "desktop" in script
    assert "pam-logo.ico,0" in script
    assert ".Save()" in script


def test_windows_shortcut_script_escapes_single_quotes():
    script = launchers.windows_shortcut_script(
        "C:\\Users\\O'Brien\\pam.exe", "C:\\cut\\Pam.lnk"
    )
    assert "O''Brien" in script
    assert "IconLocation" not in script


def test_install_ps1_shortcut_guarded_by_no_desktop():
    text = _read_repo_script("install.ps1")
    assert "WScript.Shell" in text
    assert "Pam.lnk" in text
    assert "-NoDesktop" in text
    assert text.index("DesktopWanted") < text.index("WScript.Shell")
    assert text.index("Uninstall") < text.index("Get-PamShortcutPath")
    for token in ("??", "?.", "&&", "||"):
        assert token not in text


def test_mac_app_bundle_layout_and_overwrite(tmp_path):
    icon = launchers.package_icon(launchers.ICON_PNG_NAME)
    bundle = launchers.create_mac_app_bundle(
        tmp_path, pam_exe="/Users/test/.local/bin/pam", icon_src=icon
    )
    assert bundle.name == "Pam.app"
    plist = bundle / "Contents" / "Info.plist"
    stub = bundle / "Contents" / "MacOS" / "Pam"
    text = plist.read_text(encoding="utf-8")
    assert "CFBundleExecutable" in text
    assert "com.pam-pm.pam" in text
    body = stub.read_text(encoding="utf-8")
    assert "desktop" in body
    assert "desktop-app.log" in body
    assert stat.S_IMODE(stub.stat().st_mode) & 0o111 == 0o111
    assert (bundle / "Contents" / "Resources" / "pam-logo.png").exists()
    stray = bundle / "Contents" / "stale.txt"
    stray.write_text("stale", encoding="utf-8")
    launchers.create_mac_app_bundle(tmp_path, icon_src=icon)
    assert not stray.exists()
    assert str(bundle).startswith(str(tmp_path))


def test_mac_launcher_stub_quotes_spaced_exe():
    stub = launchers.mac_launcher_stub("/opt/my tools/pam")
    assert "'/opt/my tools/pam'" in stub
    assert 'exec "$PAM_EXE" desktop' in stub
    assert "$HOME/.local/bin/pam" in launchers.mac_launcher_stub()


def test_linux_desktop_file_with_svg_icon(tmp_path):
    apps = tmp_path / "applications"
    icons = tmp_path / "icons"
    svg = launchers.package_icon(launchers.ICON_SVG_NAME)
    desktop_file = launchers.write_linux_desktop_file(
        apps, pam_exe="/opt/my tools/pam", icon_src=svg, icons_dir=icons
    )
    assert desktop_file.name == "Pam.desktop"
    body = desktop_file.read_text(encoding="utf-8")
    assert "Name=Pam" in body
    assert "Type=Application" in body
    assert '"/opt/my tools/pam" desktop' in body
    assert f"Icon={icons / 'pam-logo.svg'}" in body
    assert (icons / "pam-logo.svg").exists()
    assert str(desktop_file).startswith(str(tmp_path))


def test_install_sh_launchers_guarded_and_sudo_free():
    text = _read_repo_script("install.sh")
    assert "Pam.app" in text
    assert "Pam.desktop" in text
    assert "pam-logo.svg" in text
    assert "iconutil" in text
    assert "desktop-app.log" in text
    assert "sudo" not in text
    assert '[ "$DESKTOP_WANTED" -eq 1 ]' in text
    assert '[ "$NO_DESKTOP" -eq 0 ]' in text
    assert "install_mac_app" in text
    assert "install_linux_desktop_file" in text


def test_logo_ico_is_real_ico():
    data = launchers.package_icon(launchers.ICON_ICO_NAME).read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00"
    count = int.from_bytes(data[4:6], "little")
    assert count >= 3
    assert len(data) > 4096
    png = launchers.package_icon(launchers.ICON_PNG_NAME).read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_remove_launcher_cleans_up(tmp_path):
    bundle = launchers.create_mac_app_bundle(tmp_path)
    desktop_file = launchers.write_linux_desktop_file(tmp_path, pam_exe="pam")
    assert launchers.remove_launcher(bundle) is True
    assert launchers.remove_launcher(desktop_file) is True
    assert not bundle.exists()
    assert not desktop_file.exists()
    assert launchers.remove_launcher(bundle) is False
