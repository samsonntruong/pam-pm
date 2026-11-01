"""WP6 console auth tests (fakes only: tmp data dirs, no network, no hosting)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pam_pm import cli as cli_mod
from pam_pm.core import auth as auth_mod
from pam_pm.core.config import Config
from pam_pm.web import app as app_mod

PASSWORD = "correct-horse-12"


@pytest.fixture(autouse=True)
def _isolated_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.delenv("PAM_ALLOWED_HOST", raising=False)
    monkeypatch.delenv("PAM_CONSOLE_PASSWORD", raising=False)
    auth_mod.reset_sessions()
    auth_mod.reset_login_attempts()
    yield tmp_path
    auth_mod.reset_sessions()
    auth_mod.reset_login_attempts()


def _client() -> TestClient:
    return TestClient(app_mod.app)


def test_local_token_mode_unchanged(monkeypatch) -> None:
    """With no password stored, the setup token flow keeps working."""
    monkeypatch.delenv("PAM_SETUP_TOKEN", raising=False)
    monkeypatch.setattr(app_mod, "SETUP_TOKEN", "local-token-for-test")
    assert app_mod.is_valid_token("local-token-for-test") is True
    assert app_mod.is_valid_token("wrong") is False
    assert app_mod.is_valid_token("") is False
    client = _client()
    assert client.get("/board").status_code == 200
    assert client.get("/setup").status_code == 200
    status = client.get("/api/auth/status").json()
    assert status == {"ok": True, "password_set": False, "authenticated": False}


def test_password_setup_and_login(tmp_path) -> None:
    """First run sets a hashed password, then login gates pages by cookie."""
    client = _client()
    page = client.get("/login")
    assert page.status_code == 200
    assert "Choose a console password" in page.text

    setup = client.post("/api/auth/setup", json={"password": PASSWORD})
    assert setup.status_code == 200
    assert setup.json()["ok"] is True

    stored = (tmp_path / auth_mod.AUTH_FILENAME).read_text(encoding="utf-8")
    assert PASSWORD not in stored
    assert "pbkdf2-sha256" in stored
    assert auth_mod.has_password(tmp_path) is True
    assert auth_mod.verify_password(PASSWORD, tmp_path) is True
    assert auth_mod.verify_password("wrong-password-1", tmp_path) is False

    anon = _client()
    redirect = anon.get("/board", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/login"
    api = anon.get("/api/activity")
    assert api.status_code == 401

    bad = anon.post("/login", json={"password": "wrong-password-1"})
    assert bad.status_code == 401

    good = anon.post("/login", json={"password": PASSWORD})
    assert good.status_code == 200
    assert auth_mod.SESSION_COOKIE in (good.cookies or {})
    assert anon.get("/board").status_code == 200

    status = anon.get("/api/auth/status").json()
    assert status["password_set"] is True
    assert status["authenticated"] is True

    logout = anon.post("/api/auth/logout")
    assert logout.status_code == 200
    assert anon.get("/board", follow_redirects=False).status_code == 303


def test_setup_validates_and_locks_after_first_set() -> None:
    """Short passwords fail; a second setup needs a login session."""
    client = _client()
    short = client.post("/api/auth/setup", json={"password": "tiny"})
    assert short.status_code == 400

    first = client.post("/api/auth/setup", json={"password": PASSWORD})
    assert first.status_code == 200

    anon = _client()
    again = anon.post("/api/auth/setup", json={"password": "another-long-1"})
    assert again.status_code == 403

    change = client.post("/api/auth/setup", json={"password": "another-long-1"})
    assert change.status_code == 200
    assert auth_mod.verify_password("another-long-1") is True


def test_login_rate_limit() -> None:
    """Repeated wrong passwords are limited with a retry hint."""
    auth_mod.set_password(PASSWORD)
    client = _client()
    for _ in range(auth_mod.LOGIN_MAX_ATTEMPTS):
        resp = client.post("/login", json={"password": "wrong-password-1"})
        assert resp.status_code == 401
    limited = client.post("/login", json={"password": "wrong-password-1"})
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    locked_good = client.post("/login", json={"password": PASSWORD})
    assert locked_good.status_code == 429

    auth_mod.reset_login_attempts()
    good = client.post("/login", json={"password": PASSWORD})
    assert good.status_code == 200


def test_non_loopback_refused_without_auth(tmp_path) -> None:
    """Loopback binds pass; network binds need a stored password."""
    assert auth_mod.console_bind_error("127.0.0.1", tmp_path) is None
    assert auth_mod.console_bind_error("localhost", tmp_path) is None
    assert auth_mod.console_bind_error("::1", tmp_path) is None

    error = auth_mod.console_bind_error("0.0.0.0", tmp_path)
    assert error is not None
    assert "0.0.0.0" in error
    assert "password" in error.lower()
    assert "\u2014" not in error

    lan = auth_mod.console_bind_error("192.168.1.10", tmp_path)
    assert lan is not None

    auth_mod.set_password(PASSWORD, tmp_path)
    assert auth_mod.console_bind_error("0.0.0.0", tmp_path) is None


def test_cli_start_refuses_and_provisions(monkeypatch, tmp_path, capsys) -> None:
    """cmd_start exits before uvicorn on network binds without a password."""
    import uvicorn

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        uvicorn, "run", lambda *a, **k: calls.append((k.get("host"), k.get("port")))
    )
    args = argparse.Namespace(host="0.0.0.0", port=8765, no_browser=True)
    with pytest.raises(SystemExit) as exc:
        cli_mod.cmd_start(args)
    assert exc.value.code == 2
    assert calls == []
    assert "password" in capsys.readouterr().out.lower()

    local = argparse.Namespace(host="127.0.0.1", port=8765, no_browser=True)
    cli_mod.cmd_start(local)
    assert calls == [("127.0.0.1", 8765)]

    monkeypatch.setenv("PAM_CONSOLE_PASSWORD", "provisioned-12")
    net = argparse.Namespace(host="0.0.0.0", port=8765, no_browser=True)
    cli_mod.cmd_start(net)
    assert calls[-1] == ("0.0.0.0", 8765)
    stored = (tmp_path / auth_mod.AUTH_FILENAME).read_text(encoding="utf-8")
    assert "provisioned-12" not in stored


def test_allowed_host(monkeypatch) -> None:
    """PAM_ALLOWED_HOST pins the hostname; health stays open."""
    assert auth_mod.host_allowed("pam.example.com", "pam.example.com") is True
    assert auth_mod.host_allowed("pam.example.com:8765", "pam.example.com") is True
    assert auth_mod.host_allowed("evil.test", "pam.example.com") is False
    assert auth_mod.host_allowed("anything.test", "") is True

    auth_mod.set_password(PASSWORD)
    client = _client()
    assert client.post("/login", json={"password": PASSWORD}).status_code == 200

    monkeypatch.setenv("PAM_ALLOWED_HOST", "pam.example.com")
    ok = client.get("/board", headers={"host": "pam.example.com"})
    assert ok.status_code == 200
    bad = client.get("/board", headers={"host": "evil.test"})
    assert bad.status_code == 400
    assert "host name" in bad.json()["message"].lower()
    health = client.get("/health", headers={"host": "evil.test"})
    assert health.status_code == 200


def test_config_allowed_host(monkeypatch) -> None:
    """Config carries the allowed host from env or constructor."""
    assert Config().allowed_host == ""
    monkeypatch.setenv("PAM_ALLOWED_HOST", "pam.example.com")
    assert Config().allowed_host == "pam.example.com"
    assert Config(allowed_host="other.example.com").allowed_host == (
        "other.example.com"
    )


def test_no_em_dashes_in_auth_surfaces() -> None:
    """Login pages and the bind error stay em dash free."""
    client = _client()
    assert "\u2014" not in client.get("/login").text
    auth_mod.set_password(PASSWORD)
    assert "\u2014" not in client.get("/login").text
    assert "\u2014" not in client.get("/board", follow_redirects=False).text

    deploy = Path(__file__).resolve().parents[1] / "docs" / "deploy.md"
    text = deploy.read_text(encoding="utf-8")
    assert "\u2014" not in text
    assert "untested" in text
    assert "pam-data" in text
    assert "backup" in text.lower()
