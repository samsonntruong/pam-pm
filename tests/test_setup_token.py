"""Setup token late-resolution tests (no network, no hosting)."""

from __future__ import annotations

import argparse

from fastapi.testclient import TestClient


def test_printed_token_validates_in_same_process(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.delenv("PAM_CONSOLE_PASSWORD", raising=False)
    monkeypatch.delenv("PAM_SETUP_TOKEN", raising=False)
    import pam_pm.cli as cli_mod
    import pam_pm.web.app as app_mod

    opened: list[str] = []
    monkeypatch.setattr(cli_mod.webbrowser, "open", lambda url: opened.append(url))
    import sys
    import types

    seen: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        seen["args"] = args
        seen["kwargs"] = kwargs

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = fake_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    args = argparse.Namespace(host="127.0.0.1", port=8765, no_browser=True)
    cli_mod.cmd_start(args)
    out = capsys.readouterr().out
    token = ""
    for line in out.splitlines():
        if line.startswith("Setup token:"):
            token = line.split(":", 1)[1].strip()
    assert token, out
    import os

    assert os.environ.get("PAM_SETUP_TOKEN") == token
    assert app_mod.is_valid_token(token) is True
    assert app_mod.is_valid_token("wrong-token-value") is False
    assert app_mod.is_valid_token("") is False
    assert app_mod.is_valid_token(None) is False


def test_env_set_after_import_is_honored(monkeypatch) -> None:
    monkeypatch.delenv("PAM_SETUP_TOKEN", raising=False)
    import importlib

    import pam_pm.web.app as app_mod

    importlib.reload(app_mod)
    monkeypatch.setenv("PAM_SETUP_TOKEN", "late-token-after-import-1")
    assert app_mod.is_valid_token("late-token-after-import-1") is True
    assert app_mod.is_valid_token("stale-or-wrong") is False


def test_setup_endpoints_reject_bad_tokens(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("PAM_SETUP_TOKEN", "setup-token-check-1")
    for key in ("META_MODEL", "META_BASE_URL", "META_API_KEY", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    import pam_pm.web.app as app_mod

    app_mod.SETUP_DONE = False
    client = TestClient(app_mod.app)
    try:
        assert client.get("/health").status_code == 200
        assert client.get("/setup").status_code == 200
        bad = [
            ("/api/setup/progress", {"project": "tok", "step": "people", "data": {"people": "Ana"}}),
            ("/api/setup/backend", {"project": "tok", "backend": "pam_board"}),
            ("/api/setup/provider", {"project": "tok", "provider": "meta", "model": "m", "base_url": "", "api_key": ""}),
            ("/api/setup/slack", {"project": "tok", "bot_token": "", "app_token": ""}),
            ("/api/setup/ado", {"project": "tok", "org_url": "", "project_name": "", "team": "", "pat": ""}),
            ("/api/setup/complete", {"token": "wrong-token-value"}),
        ]
        for path, payload in bad:
            resp = client.post(path, json=payload)
            body = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
            assert body.get("ok") is False, (path, resp.status_code, body)
        wrong = client.post("/api/setup/backend", json={"token": "wrong-token-value", "project": "tok", "backend": "pam_board"})
        assert wrong.json().get("ok") is False
        good = client.post("/api/setup/backend", json={"token": "setup-token-check-1", "project": "tok", "backend": "pam_board"})
        assert good.status_code == 200
        assert good.json().get("ok") is True
        done = client.post("/api/setup/complete", json={"token": "setup-token-check-1"})
        assert done.json().get("ok") is True
    finally:
        app_mod.SETUP_DONE = False
