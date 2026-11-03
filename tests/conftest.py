"""Hermetic tests: isolate PAM_HOME per test so no test touches the real DB."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _live_check_transports_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default all setup Test-button live probes to success.

    Probes verify live over HTTP; without a transport they hit the real
    network. Tests that need failure cases set the transports themselves.
    """
    try:
        import httpx
    except ImportError:
        return

    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_ok)
    try:
        import pam_pm.web.app as app_mod
    except ImportError:
        return
    for attr in (
        "META_CHECK_TRANSPORT",
        "PROVIDER_CHECK_TRANSPORT",
        "SLACK_CHECK_TRANSPORT",
        "ADO_CHECK_TRANSPORT",
        "JIRA_CHECK_TRANSPORT",
        "TRELLO_CHECK_TRANSPORT",
    ):
        monkeypatch.setattr(app_mod, attr, transport, raising=False)


@pytest.fixture(autouse=True)
def _isolated_pam_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point PAM_HOME at a fresh tmp dir for every test."""
    home = tmp_path / "pam-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAM_HOME", str(home))
    old_cwd = Path.cwd()
    try:
        yield home
    finally:
        try:
            os.chdir(old_cwd)
        except OSError:
            pass
