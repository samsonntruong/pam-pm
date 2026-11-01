"""M6b console screenshots: one PNG per screen in docs/images.

Loads setup, rules, board, activity, plan, and review through TestClient
against the app's built in sample data (fake adapters only, no network
and no credentials). Then tries Playwright with a local uvicorn server
for real screenshots. When Playwright or its browsers are missing, falls
back to TestClient HTML plus minimal PNG placeholders. Missing browsers
never fail this test.
"""

from __future__ import annotations

import base64
import socket
import tempfile
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from pam_pm.web.app import app

IMAGES_DIR = Path(__file__).resolve().parent.parent / "docs" / "images"

PAGES = {
    "setup": "/setup",
    "rules": "/rules",
    "board": "/board",
    "activity": "/activity",
    "plan": "/plan",
    "review": "/review",
}

# 1x1 transparent PNG used when no browser is available.
PLACEHOLDER_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5"
    "ErkJggg=="
)

PLACEHOLDER_NOTE = (
    "Placeholder PNGs (1x1). Playwright browsers were not available, so no "
    "real screenshots were taken. Each page was fetched with TestClient and "
    "checked instead. To capture real screenshots, run "
    "`playwright install chromium` and rerun this test."
)


def _write_placeholder_png(path: Path) -> None:
    path.write_bytes(base64.b64decode(PLACEHOLDER_PNG_B64))


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _serve_and_capture(names: list[str]) -> bool:
    """Serve the app locally and screenshot each page. True on success."""
    import uvicorn
    from playwright.sync_api import sync_playwright

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.1)
        if not server.started:
            return False
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                for name in names:
                    page.goto(
                        f"http://127.0.0.1:{port}{PAGES[name]}",
                        wait_until="domcontentloaded",
                        timeout=10000,
                    )
                    page.screenshot(path=str(IMAGES_DIR / f"{name}.png"))
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
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            for name, markup in html_by_name.items():
                page.set_content(markup, wait_until="domcontentloaded")
                page.screenshot(path=str(IMAGES_DIR / f"{name}.png"))
        finally:
            browser.close()
    return True


def test_console_screenshots() -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    client = TestClient(app)
    html_by_name: dict[str, str] = {}
    for name, url in PAGES.items():
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert "text/html" in resp.headers["content-type"], url
        assert "\u2014" not in resp.text, url
        html_by_name[name] = resp.text
    try:
        if _serve_and_capture(list(PAGES)):
            return
    except Exception as exc:  # noqa: BLE001
        print(f"Live server capture skipped: {exc}")
    try:
        if _content_capture(html_by_name):
            return
    except Exception as exc:  # noqa: BLE001
        print(f"Playwright capture skipped: {exc}")
    dump_dir = Path(tempfile.gettempdir()) / "pam_console_shots"
    dump_dir.mkdir(exist_ok=True)
    for name, markup in html_by_name.items():
        (dump_dir / f"{name}.html").write_text(markup, encoding="utf-8")
        _write_placeholder_png(IMAGES_DIR / f"{name}.png")
    print(f"NOTE: {PLACEHOLDER_NOTE} HTML saved under {dump_dir}.")
