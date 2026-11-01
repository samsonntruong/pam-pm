"""Per-user autostart: start Pam at sign-in and restart it if it stops.

Windows: a per-user Scheduled Task at sign-in running ``pam desktop``
when the desktop extra is installed, else ``pam start --no-browser``,
with restart on failure (every 1 minute, up to 10 tries). No admin
rights needed. macOS: a LaunchAgent with KeepAlive. Linux: a systemd
user service with Restart=on-failure.

Set PAM_AUTOSTART_FAKE to a file path to sandbox enable/disable/
status to a marker file (used by tests so they never touch the real
scheduler, launchd, or systemd entries).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "Pam"
LAUNCH_LABEL = "com.pam-pm.pam"
SERVICE_NAME = "pam.service"
PLIST_NAME = LAUNCH_LABEL + ".plist"

RESTART_INTERVAL = "PT1M"
RESTART_COUNT = 10

CHECKBOX_LABEL = "Start Pam automatically when I sign in and restart it if it stops"


def current_platform() -> str:
    """Return windows, macos, linux, or other for this machine."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def _fake_path() -> Path | None:
    raw = os.environ.get("PAM_AUTOSTART_FAKE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return None


def pam_command() -> str:
    """Return the pam executable path, falling back to the module runner."""
    found = shutil.which("pam")
    if found:
        return found
    return sys.executable + " -m pam_pm.cli"


def desktop_available() -> bool:
    """Return True when the desktop extra (pywebview plus pystray) is installed."""
    try:
        from importlib.util import find_spec
    except ImportError:
        return False
    try:
        return find_spec("webview") is not None and find_spec("pystray") is not None
    except Exception:  # noqa: BLE001
        return False


def start_args() -> list[str]:
    """Return the sign-in subcommand: desktop when available, else start."""
    if desktop_available():
        return ["desktop"]
    return ["start", "--no-browser"]


def start_command_line() -> str:
    """Return the sign-in command line run by every backend."""
    return pam_command() + " " + " ".join(start_args())


def _home() -> Path:
    # Path.home() honors USERPROFILE on Windows and HOME elsewhere.
    # Do not read HOME directly: tools like Git Bash can set a POSIX
    # style HOME that points outside the real Windows profile.
    return Path.home()


def _base_dir() -> Path | None:
    raw = os.environ.get("PAM_AUTOSTART_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return None


def launch_agent_path() -> Path:
    """Return the macOS LaunchAgent plist path."""
    base = _base_dir()
    if base is not None:
        return base / PLIST_NAME
    return _home() / "Library" / "LaunchAgents" / PLIST_NAME


def systemd_unit_path() -> Path:
    """Return the Linux systemd user unit path."""
    base = _base_dir()
    if base is not None:
        return base / SERVICE_NAME
    return _home() / ".config" / "systemd" / "user" / SERVICE_NAME


def plist_content() -> str:
    """Return the LaunchAgent plist text with KeepAlive."""
    exe = pam_command()
    args = "".join(f"    <string>{escape(bit)}</string>\n" for bit in start_args())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        "<plist version=\"1.0\">\n"
        "<dict>\n"
        f"  <key>Label</key><string>{escape(LAUNCH_LABEL)}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"    <string>{escape(exe)}</string>\n"
        f"{args}"
        "  </array>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><true/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _quoted_command_line() -> str:
    """Return exe plus args with the exe quoted when it has spaces."""
    exe = pam_command()
    rest = " ".join(start_args())
    if " " in exe and not exe.startswith('"'):
        exe = f'"{exe}"'
    return f"{exe} {rest}" if rest else exe


def systemd_unit_content() -> str:
    """Return the systemd user unit text with Restart=on-failure."""
    return (
        "[Unit]\n"
        "Description=Pam PM agent\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={_quoted_command_line()}\n"
        "Restart=on-failure\n"
        "RestartSec=60\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def windows_task_xml() -> str:
    """Return the Scheduled Task XML with restart on failure."""
    exe_xml = escape(pam_command())
    args_xml = escape(" ".join(start_args()))
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2"'
        ' xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>Start Pam at sign-in and restart it if it stops."
        "</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        "    <Principal id=\"Author\">\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <Enabled>true</Enabled>\n"
        "    <RestartOnFailure>\n"
        f"      <Interval>{RESTART_INTERVAL}</Interval>\n"
        f"      <Count>{RESTART_COUNT}</Count>\n"
        "    </RestartOnFailure>\n"
        "  </Settings>\n"
        "  <Actions Context=\"Author\">\n"
        "    <Exec>\n"
        f"      <Command>{exe_xml}</Command>\n"
        f"      <Arguments>{args_xml}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _run_quiet(args: list[str]) -> bool:
    try:
        done = subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return False
    return done.returncode == 0


def _windows_enabled() -> bool:
    return _run_quiet(["schtasks", "/Query", "/TN", TASK_NAME])


def _windows_enable() -> str:
    import tempfile

    xml = windows_task_xml()
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".xml", delete=False, encoding="utf-16"
        ) as handle:
            handle.write(xml)
            path = handle.name
    except OSError as exc:
        return f"autostart: could not write task file: {exc}"
    try:
        ok = _run_quiet(
            ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"]
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if ok:
        return "autostart: on (Scheduled Task Pam at sign-in)"
    fallback = _run_quiet(
        [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            _quoted_command_line(),
            "/SC",
            "ONLOGON",
            "/F",
        ]
    )
    if fallback:
        return "autostart: on (Scheduled Task Pam at sign-in)"
    return "autostart: could not create the Scheduled Task"


def _windows_disable() -> str:
    if not _windows_enabled():
        return "autostart: already off"
    if _run_quiet(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]):
        return "autostart: off"
    return "autostart: could not remove the Scheduled Task"


def _macos_uid() -> int | None:
    try:
        return os.getuid()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None


def _macos_enable() -> str:
    path = launch_agent_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plist_content(), encoding="utf-8")
    except OSError as exc:
        return f"autostart: could not write {path}: {exc}"
    uid = _macos_uid()
    if uid is not None and _run_quiet(["launchctl", "bootstrap", f"gui/{uid}", str(path)]):
            return f"autostart: on ({path})"
    _run_quiet(["launchctl", "load", "-w", str(path)])
    return f"autostart: on ({path})"


def _macos_disable() -> str:
    path = launch_agent_path()
    uid = _macos_uid()
    if uid is not None:
        _run_quiet(["launchctl", "bootout", f"gui/{uid}", str(path)])
    _run_quiet(["launchctl", "unload", "-w", str(path)])
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return f"autostart: could not remove {path}: {exc}"
    return "autostart: off"


def _linux_enable() -> str:
    path = systemd_unit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(systemd_unit_content(), encoding="utf-8")
    except OSError as exc:
        return f"autostart: could not write {path}: {exc}"
    _run_quiet(["systemctl", "--user", "daemon-reload"])
    _run_quiet(["systemctl", "--user", "enable", SERVICE_NAME])
    return f"autostart: on ({path})"


def _linux_disable() -> str:
    path = systemd_unit_path()
    _run_quiet(["systemctl", "--user", "disable", SERVICE_NAME])
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return f"autostart: could not remove {path}: {exc}"
    return "autostart: off"


def is_enabled() -> bool:
    """Return True when autostart is installed for the current user."""
    fake = _fake_path()
    if fake is not None:
        return fake.exists()
    platform = current_platform()
    if platform == "windows":
        return _windows_enabled()
    if platform == "macos":
        return launch_agent_path().exists()
    if platform == "linux":
        return systemd_unit_path().exists()
    return False


def enable() -> str:
    """Install autostart for the current user, returning a status line."""
    fake = _fake_path()
    if fake is not None:
        try:
            fake.parent.mkdir(parents=True, exist_ok=True)
            fake.write_text("on\n", encoding="utf-8")
        except OSError as exc:
            return f"autostart: could not write {fake}: {exc}"
        return "autostart: on"
    platform = current_platform()
    if platform == "windows":
        return _windows_enable()
    if platform == "macos":
        return _macos_enable()
    if platform == "linux":
        return _linux_enable()
    return "autostart: unsupported platform"


def disable() -> str:
    """Remove autostart for the current user, returning a status line."""
    fake = _fake_path()
    if fake is not None:
        try:
            fake.unlink(missing_ok=True)
        except OSError as exc:
            return f"autostart: could not remove {fake}: {exc}"
        return "autostart: off"
    platform = current_platform()
    if platform == "windows":
        return _windows_disable()
    if platform == "macos":
        return _macos_disable()
    if platform == "linux":
        return _linux_disable()
    return "autostart: unsupported platform"


def status() -> dict[str, str]:
    """Return the autostart state for doctor and status output."""
    platform = current_platform()
    enabled = is_enabled()
    if enabled:
        detail = "on"
    else:
        detail = "off"
    if platform == "windows":
        where = f"Scheduled Task {TASK_NAME} at sign-in"
    elif platform == "macos":
        where = f"LaunchAgent {LAUNCH_LABEL} with KeepAlive"
    elif platform == "linux":
        where = f"systemd user service {SERVICE_NAME} with Restart=on-failure"
    else:
        where = "unsupported platform"
    return {"platform": platform, "enabled": detail, "detail": where}


def status_text() -> str:
    """Return one autostart status line for console output."""
    info = status()
    return f"autostart: {info['enabled']} ({info['detail']})"
