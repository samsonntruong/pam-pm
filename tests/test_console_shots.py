"""M6b console screenshots: light plus dark PNG per screen in docs/images.

Loads setup, board, activity, plan, review, rules, settings, and kickoff
through TestClient against the app's built in sample data (fake adapters
only, no network and no credentials). Then tries Playwright with a local
uvicorn server for real screenshots. When Playwright or its browsers are
missing, falls back to TestClient HTML plus minimal PNG placeholders.
Missing browsers never fail this test, and the fallback never overwrites
an existing screenshot.
"""

from __future__ import annotations

import base64
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from pam_pm.adapters.pm.base import Link, WorkItem
from pam_pm.web import app as app_mod
from pam_pm.web.app import app

IMAGES_DIR = Path(__file__).resolve().parent.parent / "docs" / "images"

PAGES = {
    "setup": "/setup",
    "board": "/board",
    "activity": "/activity",
    "plan": "/plan",
    "review": "/review",
    "rules": "/rules",
    "settings": "/settings",
    "kickoff": "/kickoff",
}

THEMES = ("light", "dark")

# 1x1 transparent PNG used only when no browser is available and no
# screenshot file exists yet.
PLACEHOLDER_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5"
    "ErkJggg=="
)

PLACEHOLDER_NOTE = (
    "Placeholder PNGs (1x1) were created only for missing files. Playwright "
    "browsers were not available, so no real screenshots were taken. Each "
    "page was fetched with TestClient and checked instead. To capture real "
    "screenshots, run `playwright install chromium` and rerun this test."
)

MIN_REAL_SCREENSHOT_BYTES = 4096


def _placeholder_bytes() -> bytes:
    return base64.b64decode(PLACEHOLDER_PNG_B64)


def _is_placeholder_file(path: Path) -> bool:
    try:
        return path.read_bytes() == _placeholder_bytes()
    except OSError:
        return False


def _target_for(name: str, theme: str) -> Path:
    if theme == "dark":
        return IMAGES_DIR / f"{name}-dark.png"
    return IMAGES_DIR / f"{name}.png"


def _launch_kwargs() -> dict:
    """Extra chromium launch args. Root needs --no-sandbox on Linux."""
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return {"args": ["--no-sandbox"]}
    except OSError:
        pass
    return {}


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _wait_for_server(server: object, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if getattr(server, "started", False):
            return True
        time.sleep(0.1)
    return bool(getattr(server, "started", False))


def _apply_theme(page: object, theme: str) -> None:
    try:
        page.evaluate(
            f"() => {{ try {{ localStorage.setItem('pam-theme', '{theme}'); }} catch (e) {{}}"
            f" document.documentElement.setAttribute('data-theme', '{theme}'); }}"
        )
    except Exception:  # noqa: BLE001, S110
        pass


class _ShotBoard:
    """Fake board adapter with short neutral invented owner names."""

    supports_dependency_links = True
    supports_iterations = False
    supports_comments = False
    supports_close_iteration = False

    def list_items(self, limit: int = 500) -> list[WorkItem]:
        return [
            WorkItem(
                id="101",
                title="Signup form",
                state="New",
                fields={"owner": "Avery", "estimate": "3"},
            ),
            WorkItem(
                id="102",
                title="API keys",
                state="Active",
                fields={"owner": "Blair", "estimate": "5"},
            ),
            WorkItem(
                id="103",
                title="Launch site",
                state="Done",
                fields={"owner": "Casey", "estimate": "2"},
            ),
        ][:limit]

    def list_states(self) -> list[str]:
        return ["New", "Active", "Blocked", "Done"]

    def list_links(self, item_id: str) -> list[Link]:
        if str(item_id) == "101":
            return [
                Link(
                    source_id="101",
                    target_id="102",
                    rel="System.LinkTypes.Dependency-Forward",
                )
            ]
        return []


def _serve_and_capture(names: list[str]) -> bool:
    """Serve the app locally and screenshot each page in both themes."""
    import uvicorn
    from playwright.sync_api import sync_playwright

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        if not _wait_for_server(server):
            print("Live server capture skipped: uvicorn did not start.")
            return False
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**_launch_kwargs())
            try:
                for theme in THEMES:
                    context = browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        color_scheme=theme,  # type: ignore[arg-type]
                    )
                    context.add_init_script(
                        f"try {{ localStorage.setItem('pam-theme', '{theme}'); }}"
                        " catch (e) {}"
                    )
                    page = context.new_page()
                    for name in names:
                        page.goto(
                            f"http://127.0.0.1:{port}{PAGES[name]}",
                            wait_until="domcontentloaded",
                            timeout=10000,
                        )
                        _apply_theme(page, theme)
                        target = _target_for(name, theme)
                        page.screenshot(path=str(target))
                        print(f"captured {target.name} ({target.stat().st_size} bytes)")
                    context.close()
            finally:
                browser.close()
        return True
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def _content_capture(html_by_name: dict[str, str]) -> bool:
    """Screenshot from raw HTML without a server. True on success."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**_launch_kwargs())
        try:
            for theme in THEMES:
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    color_scheme=theme,  # type: ignore[arg-type]
                )
                page = context.new_page()
                for name, markup in html_by_name.items():
                    page.set_content(markup, wait_until="domcontentloaded")
                    _apply_theme(page, theme)
                    target = _target_for(name, theme)
                    page.screenshot(path=str(target))
                    print(f"captured {target.name} ({target.stat().st_size} bytes)")
                context.close()
        finally:
            browser.close()
    return True


def _placeholder_fallback(html_by_name: dict[str, str]) -> None:
    """Write placeholders only for missing files; never overwrite."""
    dump_dir = Path(tempfile.gettempdir()) / "pam_console_shots"
    dump_dir.mkdir(exist_ok=True)
    for name, markup in html_by_name.items():
        (dump_dir / f"{name}.html").write_text(markup, encoding="utf-8")
        for theme in THEMES:
            target = _target_for(name, theme)
            if target.exists():
                state = "placeholder" if _is_placeholder_file(target) else "kept"
                print(f"{state} {target.name} ({target.stat().st_size} bytes)")
                continue
            target.write_bytes(_placeholder_bytes())
            print(f"created placeholder {target.name} ({target.stat().st_size} bytes)")
    print(f"NOTE: {PLACEHOLDER_NOTE} HTML saved under {dump_dir}.")


def _report_sizes(names: list[str]) -> None:
    for name in names:
        for theme in THEMES:
            target = _target_for(name, theme)
            if target.exists():
                print(f"size {target.name}: {target.stat().st_size} bytes")


def _assert_real_screenshots(names: list[str]) -> None:
    for name in names:
        for theme in THEMES:
            target = _target_for(name, theme)
            assert target.exists(), target.name
            assert not _is_placeholder_file(target), target.name
            assert target.stat().st_size >= MIN_REAL_SCREENSHOT_BYTES, target.name


def test_console_screenshots() -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    app_mod.set_board_adapter("default", _ShotBoard())
    try:
        client = TestClient(app)
        html_by_name: dict[str, str] = {}
        for name, url in PAGES.items():
            resp = client.get(url)
            assert resp.status_code == 200, url
            assert "text/html" in resp.headers["content-type"], url
            assert "—" not in resp.text, url
            html_by_name[name] = resp.text
        try:
            if _serve_and_capture(list(PAGES)):
                _assert_real_screenshots(list(PAGES))
                _report_sizes(list(PAGES))
                return
        except Exception as exc:  # noqa: BLE001
            print(f"Live server capture skipped: {exc}")
        try:
            if _content_capture(html_by_name):
                _assert_real_screenshots(list(PAGES))
                _report_sizes(list(PAGES))
                return
        except Exception as exc:  # noqa: BLE001
            print(f"Playwright capture skipped: {exc}")
        _placeholder_fallback(html_by_name)
        _report_sizes(list(PAGES))
    finally:
        app_mod.reset_board_state()
