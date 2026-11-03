"""Clickable app icons for Pam on Windows, macOS, and Linux.

``pam desktop`` opens the Pam window, but users expect a clickable icon.
This module generates the per-OS launcher files; the installers
(install.ps1, install.sh) write them during setup, only when the desktop
option is wanted. Every writer takes explicit paths so tests can aim at
tmp dirs; nothing here touches the real Start Menu, Applications, or
applications folders on its own.

Double-click behavior on every OS: the launcher runs ``pam desktop``,
which starts the server when it is not already running, opens the
console window, and logs server output (including the first-run setup
token) to desktop-server.log in the Pam data directory. The macOS and
Linux launchers also append their own output to desktop-app.log.
"""

from __future__ import annotations

import shutil
from pathlib import Path

SHORTCUT_NAME = "Pam.lnk"
APP_NAME = "Pam.app"
DESKTOP_FILE_NAME = "Pam.desktop"
BUNDLE_ID = "com.pam-pm.pam"
BUNDLE_EXECUTABLE = "Pam"
DESKTOP_ARG = "desktop"

ICON_ICO_NAME = "logo.ico"
ICON_PNG_NAME = "logo.png"
ICON_SVG_NAME = "logo-light.svg"

APP_LOG_FILENAME = "desktop-app.log"
MAC_ICON_BASENAME = "pam-logo"
LINUX_ICON_NAME = "pam-logo.svg"


def static_dir() -> Path:
    """Return the shipped web static dir holding the logo files."""
    return Path(__file__).resolve().parent / "web" / "static"


def package_icon(name: str) -> Path:
    """Return the path of a shipped logo file by name."""
    return static_dir() / name


def ps_quote(value: str) -> str:
    """Quote a string for PowerShell 5.1 and 7 single-quoted literals."""
    return "'" + value.replace("'", "''") + "'"


def sh_quote(value: str) -> str:
    """Quote a string for POSIX sh single-quoted literals."""
    return "'" + value.replace("'", "'\\''") + "'"


def windows_shortcut_script(
    pam_exe: str, shortcut_path: str, icon_path: str | None = None
) -> str:
    """Return PS 5.1-safe code creating the Start Menu shortcut Pam.

    The shortcut runs ``pam desktop``. Spaced paths need no extra
    quoting for the COM object; values are single-quoted literals.
    """
    lines = [
        "$WshShell = New-Object -ComObject WScript.Shell",
        f"$Shortcut = $WshShell.CreateShortcut({ps_quote(shortcut_path)})",
        f"$Shortcut.TargetPath = {ps_quote(pam_exe)}",
        f"$Shortcut.Arguments = {ps_quote(DESKTOP_ARG)}",
    ]
    parent = str(Path(pam_exe).parent)
    if parent and parent != ".":
        lines.append(f"$Shortcut.WorkingDirectory = {ps_quote(parent)}")
    if icon_path:
        lines.append(f"$Shortcut.IconLocation = {ps_quote(icon_path + ',0')}")
    lines.append("$Shortcut.Save()")
    return "\n".join(lines) + "\n"


def mac_launcher_stub(pam_exe: str = "$HOME/.local/bin/pam") -> str:
    """Return the Pam.app launcher stub calling ``pam desktop``."""
    if pam_exe.startswith("$"):
        exe_line = f'PAM_EXE="{pam_exe}"'
    else:
        exe_line = f"PAM_EXE={sh_quote(pam_exe)}"
    return (
        "#!/bin/sh\n"
        "# Pam launcher: runs pam desktop, which starts the server when\n"
        "# needed and opens the console window. Output goes to\n"
        "# desktop-app.log; the first-run setup token is in\n"
        "# desktop-server.log in the same folder.\n"
        f"{exe_line}\n"
        'if [ ! -x "$PAM_EXE" ]; then\n'
        '    PAM_EXE="pam"\n'
        "fi\n"
        'LOG_DIR="${PAM_HOME:-$HOME/Library/Application Support/pam-pm}"\n'
        'mkdir -p "$LOG_DIR" 2>/dev/null || true\n'
        f'exec "$PAM_EXE" {DESKTOP_ARG} '
        f'>>"$LOG_DIR/{APP_LOG_FILENAME}" 2>&1\n'
    )


def mac_info_plist(version: str, icon_basename: str | None = None) -> str:
    """Return the minimal Pam.app Info.plist text."""
    icon_key = ""
    if icon_basename:
        icon_key = (
            "  <key>CFBundleIconFile</key>"
            f"<string>{icon_basename}</string>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>CFBundleName</key><string>Pam</string>\n"
        f"  <key>CFBundleIdentifier</key><string>{BUNDLE_ID}</string>\n"
        f"  <key>CFBundleVersion</key><string>{version}</string>\n"
        '  <key>CFBundlePackageType</key><string>APPL</string>\n'
        f"  <key>CFBundleExecutable</key><string>{BUNDLE_EXECUTABLE}</string>\n"
        f"{icon_key}"
        "  <key>NSHighResolutionCapable</key><true/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def create_mac_app_bundle(
    parent: Path,
    *,
    pam_exe: str = "$HOME/.local/bin/pam",
    version: str = "1.0.0",
    icon_src: Path | None = None,
) -> Path:
    """Create Pam.app under parent, overwriting any stale bundle."""
    bundle = parent / APP_NAME
    shutil.rmtree(bundle, ignore_errors=True)
    macos_dir = bundle / "Contents" / "MacOS"
    resources_dir = bundle / "Contents" / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)
    icon_basename: str | None = None
    if icon_src is not None and icon_src.is_file():
        target = resources_dir / (MAC_ICON_BASENAME + icon_src.suffix)
        shutil.copyfile(icon_src, target)
        icon_basename = MAC_ICON_BASENAME
    (bundle / "Contents" / "Info.plist").write_text(
        mac_info_plist(version, icon_basename), encoding="utf-8"
    )
    stub = macos_dir / BUNDLE_EXECUTABLE
    stub.write_text(mac_launcher_stub(pam_exe), encoding="utf-8")
    stub.chmod(0o755)
    return bundle


def linux_desktop_entry(pam_exe: str, icon_path: str | None = None) -> str:
    """Return the Pam.desktop entry text running ``pam desktop``."""
    if " " in pam_exe and not pam_exe.startswith('"'):
        exe = f'"{pam_exe}"'
    else:
        exe = pam_exe
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Pam",
        "Comment=Pam PM console",
        f"Exec={exe} {DESKTOP_ARG}",
    ]
    if icon_path:
        lines.append(f"Icon={icon_path}")
    lines.extend(["Terminal=false", "Categories=Utility;", "StartupNotify=true"])
    return "\n".join(lines) + "\n"


def write_linux_desktop_file(
    applications_dir: Path,
    *,
    pam_exe: str,
    icon_src: Path | None = None,
    icons_dir: Path | None = None,
) -> Path:
    """Write Pam.desktop, copying the SVG icon when one is given."""
    applications_dir.mkdir(parents=True, exist_ok=True)
    icon_path: str | None = None
    if icon_src is not None and icon_src.is_file():
        dest_dir = icons_dir or applications_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / LINUX_ICON_NAME
        shutil.copyfile(icon_src, dest)
        icon_path = str(dest)
    desktop_file = applications_dir / DESKTOP_FILE_NAME
    desktop_file.write_text(
        linux_desktop_entry(pam_exe, icon_path), encoding="utf-8"
    )
    return desktop_file


def remove_launcher(path: Path) -> bool:
    """Remove a launcher file or bundle; True when something was removed."""
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
        return True
    path.unlink(missing_ok=True)
    return True
