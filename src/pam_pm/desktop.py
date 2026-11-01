"""Desktop wrapper: Pam in its own window with a tray icon.

`pam desktop` starts the local server when it is not already running
(health check on 127.0.0.1:8765, never two instances), then opens the
console in a pywebview window titled Pam with a pystray tray icon.
Closing the window hides it; only Quit stops Pam.

All GUI imports (pywebview, pystray, Pillow) are lazy and optional, so
the base package works without them. When pywebview is missing, the
console opens in the default browser with a plain message instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from pam_pm import autostart as autostart_mod
from pam_pm.core.config import Config
from pam_pm.web.app import HOST, PORT

WINDOW_TITLE = "Pam"
TRAY_NAME = "pam"
LOCK_FILENAME = "desktop.lock"
PAUSE_FILENAME = "checkins.paused"
ICON_FILENAME = "desktop-icon.png"
SERVER_LOG_FILENAME = "desktop-server.log"
HEALTH_PATH = "/health"
CONSOLE_PATH = "/board"

STATUS_RUNNING = "running"
STATUS_LISTENING = "listening"
STATUS_OFFLINE = "offline"

STATUS_COLORS = {
    STATUS_RUNNING: (46, 160, 67),
    STATUS_LISTENING: (202, 138, 4),
    STATUS_OFFLINE: (120, 120, 120),
}

BROWSER_FALLBACK_MESSAGE = (
    "The Pam desktop view (pywebview) is not installed, "
    "so the console opened in your browser instead. "
    "To get the Pam window, install the desktop extra: "
    'pip install "pam-pm[desktop]".'
)


def data_dir() -> Path:
    """Return the Pam data directory, creating it if needed."""
    return Config().ensure_data_dir()


def console_url(host: str = HOST, port: int = PORT) -> str:
    """Return the console URL shown in the desktop window."""
    return f"http://{host}:{port}{CONSOLE_PATH}"


def health_url(host: str = HOST, port: int = PORT) -> str:
    """Return the health check URL used for single-instance checks."""
    return f"http://{host}:{port}{HEALTH_PATH}"


def is_server_running(
    host: str = HOST, port: int = PORT, timeout: float = 2.0
) -> bool:
    """Return True when the Pam console answers the health check."""
    try:
        with urllib.request.urlopen(health_url(host, port), timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return False
    return "ok" in body


def lock_path() -> Path:
    """Return the desktop single-instance lock file path."""
    return data_dir() / LOCK_FILENAME


def pause_marker_path() -> Path:
    """Return the check-ins pause marker path (present means paused)."""
    return data_dir() / PAUSE_FILENAME


def read_lock_pid(path: Path | None = None) -> int | None:
    """Return the pid stored in the lock file, or None when missing."""
    try:
        text = (path or lock_path()).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(text.split()[0])
    except (ValueError, IndexError):
        return None
    return pid if pid > 0 else None


def pid_alive(pid: int) -> bool:
    """Return True when a process with this pid exists."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_desktop_running() -> bool:
    """Return True when another desktop instance holds the lock."""
    pid = read_lock_pid()
    if pid is None or pid == os.getpid():
        return False
    return pid_alive(pid)


def acquire_lock() -> bool:
    """Take the desktop lock unless another instance holds it."""
    if is_desktop_running():
        return False
    try:
        lock_path().write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return False
    return True


def release_lock() -> None:
    """Drop the desktop lock when this process holds it."""
    try:
        if read_lock_pid() != os.getpid():
            return
        lock_path().unlink(missing_ok=True)
    except OSError:
        pass


def start_server_subprocess(
    host: str = HOST, port: int = PORT
) -> subprocess.Popen[Any]:
    """Launch `pam start --no-browser` as a background process."""
    log_path = data_dir() / SERVER_LOG_FILENAME
    handle = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    kwargs: dict[str, Any] = {
        "stdout": handle,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        flags = 0
        for name in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS", "CREATE_NO_WINDOW"):
            flags |= int(getattr(subprocess, name, 0) or 0)
        if flags:
            kwargs["creationflags"] = flags
    elif os.name == "posix":
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pam_pm.cli",
                "start",
                "--host",
                host,
                "--port",
                str(port),
                "--no-browser",
            ],
            **kwargs,
        )
    except Exception:
        handle.close()
        raise


def wait_for_server(
    host: str = HOST,
    port: int = PORT,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> bool:
    """Poll the health check until the server answers or time runs out."""
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if is_server_running(host, port):
            return True
        time.sleep(interval)
    return is_server_running(host, port)


def stop_process(proc: Any) -> None:
    """Terminate a server process, killing it when it will not stop."""
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        return
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001, S110
            pass


def ensure_server(
    host: str = HOST, port: int = PORT, timeout: float = 30.0
) -> subprocess.Popen[Any] | None:
    """Start the server unless the health check already answers.

    Returns None when the server was already running, else the new
    process handle. Raises RuntimeError when the server will not come up.
    """
    if is_server_running(host, port):
        return None
    proc = start_server_subprocess(host, port)
    if wait_for_server(host, port, timeout=timeout):
        return proc
    stop_process(proc)
    raise RuntimeError(
        "Pam did not start in time. Run 'pam doctor' to check the setup, "
        "then try 'pam desktop' again."
    )


def is_checkins_paused() -> bool:
    """Return True when check-ins are paused."""
    try:
        return pause_marker_path().exists()
    except OSError:
        return False


def _notify_pause_rule(paused: bool, host: str, port: int) -> None:
    """Best-effort toggle of the morning scheduler rule on the server."""
    import json

    payload = json.dumps({"name": "morning", "enabled": not paused}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{host}:{port}/api/rules/toggle",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0):
            pass
    except Exception:  # noqa: BLE001, S110
        pass


def set_checkins_paused(
    paused: bool, host: str = HOST, port: int = PORT
) -> bool:
    """Pause or resume check-ins; toggles the morning scheduler rule."""
    try:
        if paused:
            pause_marker_path().write_text("paused\n", encoding="utf-8")
        else:
            pause_marker_path().unlink(missing_ok=True)
    except OSError:
        pass
    _notify_pause_rule(paused, host, port)
    return paused


def toggle_checkins_paused(host: str = HOST, port: int = PORT) -> bool:
    """Flip the paused state and return the new value."""
    return set_checkins_paused(not is_checkins_paused(), host, port)


def tray_status(host: str = HOST, port: int = PORT) -> str:
    """Return running, listening (paused), or offline for the tray icon."""
    if not is_server_running(host, port):
        return STATUS_OFFLINE
    if is_checkins_paused():
        return STATUS_LISTENING
    return STATUS_RUNNING


def menu_items(*, paused: bool, autostart_on: bool) -> list[dict[str, Any]]:
    """Return the tray menu as plain data (no GUI imports needed)."""
    return [
        {"id": "open", "label": "Open Pam", "checked": None},
        {
            "id": "pause",
            "label": "Resume check-ins" if paused else "Pause check-ins",
            "checked": None,
        },
        {
            "id": "autostart",
            "label": "Start at sign-in",
            "checked": bool(autostart_on),
        },
        {"id": "quit", "label": "Quit", "checked": None},
    ]


def load_webview() -> Any | None:
    """Return the pywebview module, or None when unavailable."""
    try:
        import webview
    except Exception:  # noqa: BLE001
        return None
    return webview


def load_pystray() -> Any | None:
    """Return the pystray module, or None when unavailable."""
    try:
        import pystray
    except Exception:  # noqa: BLE001
        return None
    return pystray


def make_icon_image(status: str = STATUS_RUNNING) -> Any:
    """Draw the Pam tray icon for a status. Needs Pillow."""
    from PIL import Image, ImageDraw

    color = STATUS_COLORS.get(status, STATUS_COLORS[STATUS_RUNNING])
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([2, 2, size - 3, size - 3], radius=14, fill=color)
    text = "P"
    box = draw.textbbox((0, 0), text)
    width, height = box[2] - box[0], box[3] - box[1]
    draw.text(
        ((size - width) / 2 - box[0], (size - height) / 2 - box[1]),
        text,
        fill="white",
    )
    return image


def ensure_icon_file(status: str = STATUS_RUNNING) -> str | None:
    """Write the app icon PNG and return its path, or None without Pillow."""
    try:
        image = make_icon_image(status)
    except ImportError:
        return None
    except Exception:  # noqa: BLE001
        return None
    path = data_dir() / ICON_FILENAME
    try:
        image.save(path)
    except Exception:  # noqa: BLE001
        return None
    return str(path)


def _window_supports_icon(view: Any) -> bool:
    try:
        import inspect

        return "icon" in inspect.signature(view.create_window).parameters
    except (TypeError, ValueError):
        return False


def _on_window_closing(window: Any) -> bool:
    """Hide the window instead of closing it. False cancels the close."""
    try:
        window.hide()
    except Exception:  # noqa: BLE001, S110
        pass
    return False


def create_window(url: str, view: Any | None = None) -> Any | None:
    """Open the Pam window; fall back to the browser without pywebview."""
    webview_mod = view if view is not None else load_webview()
    if webview_mod is None:
        webbrowser.open(url)
        print(BROWSER_FALLBACK_MESSAGE)
        return None
    kwargs: dict[str, Any] = {}
    icon = ensure_icon_file()
    if icon and _window_supports_icon(webview_mod):
        kwargs["icon"] = icon
    try:
        window = webview_mod.create_window(WINDOW_TITLE, url, **kwargs)
    except Exception:  # noqa: BLE001
        webbrowser.open(url)
        print(BROWSER_FALLBACK_MESSAGE)
        return None
    try:
        window.events.closing += lambda *args, **kws: _on_window_closing(window)
    except Exception:  # noqa: BLE001, S110
        pass
    return window


def build_tray_menu(app: DesktopApp, pystray: Any) -> Any:
    """Build the pystray menu for Open, Pause, Autostart, and Quit."""
    items = menu_items(
        paused=is_checkins_paused(),
        autostart_on=autostart_mod.is_enabled(),
    )
    by_id = {item["id"]: item for item in items}
    return pystray.Menu(
        pystray.MenuItem(
            by_id["open"]["label"],
            lambda icon, item: app.open_window(),
            default=True,
        ),
        pystray.MenuItem(
            lambda item: (
                "Resume check-ins" if is_checkins_paused() else "Pause check-ins"
            ),
            lambda icon, item: app.toggle_pause(),
        ),
        pystray.MenuItem(
            by_id["autostart"]["label"],
            lambda icon, item: app.toggle_autostart(),
            checked=lambda item: autostart_mod.is_enabled(),
        ),
        pystray.MenuItem(
            by_id["quit"]["label"], lambda icon, item: app.request_quit()
        ),
    )


def refresh_tray(app: DesktopApp) -> None:
    """Refresh the tray icon, title, and menu after a state change."""
    icon = app.tray
    if icon is None:
        return
    try:
        status = tray_status(app.host, app.port)
        try:
            icon.icon = make_icon_image(status)
        except Exception:  # noqa: BLE001, S110
            pass
        icon.title = f"Pam ({status})"
        pystray = load_pystray()
        if pystray is not None:
            icon.menu = build_tray_menu(app, pystray)
            icon.update_menu()
    except Exception:  # noqa: BLE001, S110
        pass


def start_tray(app: DesktopApp) -> Any | None:
    """Create the tray icon, or None when pystray or Pillow is missing."""
    pystray = load_pystray()
    if pystray is None:
        print(
            "The tray icon needs pystray, which is not installed. "
            "The Pam window still works."
        )
        return None
    try:
        status = tray_status(app.host, app.port)
        image = make_icon_image(status)
    except ImportError:
        print(
            "The tray icon needs Pillow, which is not installed. "
            "The Pam window still works."
        )
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"The tray icon could not start ({exc}). The Pam window still works.")
        return None
    try:
        return pystray.Icon(
            TRAY_NAME, image, f"Pam ({status})", build_tray_menu(app, pystray)
        )
    except Exception as exc:  # noqa: BLE001
        print(f"The tray icon could not start ({exc}). The Pam window still works.")
        return None


class DesktopApp:
    """Owns the server process, window, and tray icon for one session."""

    def __init__(self, host: str = HOST, port: int = PORT) -> None:
        self.host = host
        self.port = port
        self.url = console_url(host, port)
        self.proc: subprocess.Popen[Any] | None = None
        self.started_server = False
        self.window: Any | None = None
        self.tray: Any | None = None

    def open_window(self) -> None:
        """Show the Pam window, or the browser when there is no window."""
        if self.window is None:
            webbrowser.open(self.url)
            return
        for action in ("restore", "show"):
            try:
                getattr(self.window, action)()
            except Exception:  # noqa: BLE001, S110
                pass

    def toggle_pause(self) -> bool:
        """Pause or resume check-ins and refresh the tray."""
        paused = toggle_checkins_paused(self.host, self.port)
        print("Check-ins paused." if paused else "Check-ins resumed.")
        refresh_tray(self)
        return paused

    def toggle_autostart(self) -> str:
        """Flip start at sign-in and refresh the tray."""
        try:
            if autostart_mod.is_enabled():
                result = autostart_mod.disable()
            else:
                result = autostart_mod.enable()
        except Exception as exc:  # noqa: BLE001
            result = f"autostart: could not switch ({exc})"
        print(result)
        refresh_tray(self)
        return result

    def request_quit(self) -> None:
        """Close the window and tray; stop the server only when we started it."""
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:  # noqa: BLE001, S110
                pass
            self.tray = None
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:  # noqa: BLE001, S110
                pass
            self.window = None
        if self.started_server and self.proc is not None:
            stop_process(self.proc)
            self.proc = None
            print("Pam stopped.")
        elif not self.started_server:
            print("Pam desktop closed. The Pam server is still running.")
        release_lock()

    def run(self) -> int:
        """Start the server if needed, then run the window and tray."""
        if is_desktop_running() or not acquire_lock():
            print("Pam desktop is already running. Use the tray icon to open Pam.")
            return 0
        try:
            try:
                self.proc = ensure_server(self.host, self.port)
            except RuntimeError as exc:
                print(str(exc))
                return 1
            self.started_server = self.proc is not None
            if self.started_server:
                print(f"Pam started at {self.url}")
            else:
                print(f"Pam is already running at {self.url}")
            view = load_webview()
            self.window = create_window(self.url, view=view)
            self.tray = start_tray(self)
            try:
                if self.window is not None and view is not None:
                    if self.tray is not None:
                        thread = threading.Thread(target=self.tray.run, daemon=True)
                        thread.start()
                    view.start()
                elif self.tray is not None:
                    self.tray.run()
                else:
                    print("Press Ctrl+C to stop Pam.")
                    try:
                        while True:
                            time.sleep(3600)
                    except KeyboardInterrupt:
                        pass
                    self.request_quit()
            except Exception as exc:  # noqa: BLE001
                print(f"Pam desktop could not start ({exc}). The Pam window still works after a retry.")
                if self.started_server and self.proc is not None:
                    stop_process(self.proc)
                    self.proc = None
                return 1
        finally:
            release_lock()
        return 0


def main(host: str = HOST, port: int = PORT) -> int:
    """Entry point for `pam desktop`."""
    return DesktopApp(host=host, port=port).run()
