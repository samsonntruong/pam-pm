"""Team share mode: LAN bind guard, wizard toggle, invite box (fakes only)."""

from __future__ import annotations

import argparse
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pam_pm import cli as cli_mod
from pam_pm.core import auth as auth_mod
from pam_pm.core import store as store_mod
from pam_pm.core.config import Config
from pam_pm.web import app as app_mod

PASSWORD = "team-share-12"
TOKEN = "share-test-setup-token-1"


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("PAM_SETUP_TOKEN", TOKEN)
    monkeypatch.delenv("PAM_ALLOWED_HOST", raising=False)
    monkeypatch.delenv("PAM_CONSOLE_PASSWORD", raising=False)
    monkeypatch.delenv("PAM_SHARE_ON_LAN", raising=False)
    auth_mod.reset_sessions()
    auth_mod.reset_login_attempts()
    app_mod.reset_board_state()
    app_mod.SETUP_DONE = False
    yield tmp_path
    auth_mod.reset_sessions()
    auth_mod.reset_login_attempts()
    app_mod.reset_board_state()
    app_mod.SETUP_DONE = False


def _client() -> TestClient:
    return TestClient(app_mod.app)


def _db():
    return Config().db_path()


class _FakeBoard:
    """Minimal teammate board with one item."""

    supports_dependency_links = False
    supports_iterations = False
    supports_comments = False
    supports_close_iteration = False

    def list_items(self, limit: int = 500):  # type: ignore[no-untyped-def]
        from pam_pm.adapters.pm.base import WorkItem

        return [
            WorkItem(id="7", title="Shared task", state="New",
                     fields={"owner": "Ana"})
        ][:limit]

    def list_states(self) -> list[str]:
        return ["New", "Active", "Blocked", "Done"]

    def list_links(self, item_id: str) -> list:  # type: ignore[type-arg]
        return []


def test_share_defaults_off(isolated) -> None:
    assert Config().share_on_lan is False
    assert store_mod.get_project_share_on_lan(_db(), "default") is False
    assert store_mod.any_project_shared(_db()) is False


def test_share_env_enables_config(monkeypatch) -> None:
    monkeypatch.setenv("PAM_SHARE_ON_LAN", "1")
    assert Config().share_on_lan is True
    assert Config(share_on_lan=False).share_on_lan is False


def test_share_refuses_without_password(isolated, monkeypatch, capsys) -> None:
    store_mod.set_project_share_on_lan(_db(), "default", True)
    assert store_mod.get_project_share_on_lan(_db(), "default") is True

    error = auth_mod.share_console_error(isolated)
    assert error is not None
    assert "password" in error.lower()
    assert "\u2014" not in error

    import uvicorn

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        uvicorn, "run", lambda *a, **k: calls.append((k.get("host"), k.get("port")))
    )
    args = argparse.Namespace(host="127.0.0.1", port=8765, no_browser=True)
    with pytest.raises(SystemExit) as exc:
        cli_mod.cmd_start(args)
    assert exc.value.code == 2
    assert calls == []
    out = capsys.readouterr().out.lower()
    assert "password" in out
    assert "\u2014" not in out


def test_share_binds_and_guards_with_password(isolated, monkeypatch) -> None:
    auth_mod.set_password(PASSWORD, isolated)
    store_mod.set_project_share_on_lan(_db(), "default", True)

    import uvicorn

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        uvicorn, "run", lambda *a, **k: calls.append((k.get("host"), k.get("port")))
    )
    args = argparse.Namespace(host="127.0.0.1", port=8765, no_browser=True)
    cli_mod.cmd_start(args)
    assert calls == [("0.0.0.0", 8765)]

    anon = _client()
    redirect = anon.get("/board", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/login"
    assert anon.post("/login", json={"password": "wrong-password-1"}).status_code == 401
    good = anon.post("/login", json={"password": PASSWORD})
    assert good.status_code == 200
    assert anon.get("/board").status_code == 200


def test_teammate_path_lands_on_board(isolated) -> None:
    auth_mod.set_password(PASSWORD, isolated)
    store_mod.set_project_backend(_db(), "team", "pam_board")
    store_mod.set_project_share_on_lan(_db(), "team", True)
    app_mod.set_board_adapter("team", _FakeBoard())

    teammate = _client()
    first = teammate.get("/board?project=team", follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/login"
    login = teammate.post("/login", json={"password": PASSWORD})
    assert login.status_code == 200
    board = teammate.get("/board?project=team")
    assert board.status_code == 200
    assert "Shared task" in board.text


def test_wizard_toggle_round_trip(isolated) -> None:
    client = _client()

    missing = client.post(
        "/api/setup/share",
        json={"token": TOKEN, "project": "team", "enabled": True},
    )
    assert missing.status_code == 400
    assert "password" in missing.json()["message"].lower()

    mismatch = client.post(
        "/api/setup/share",
        json={
            "token": TOKEN, "project": "team", "enabled": True,
            "password": PASSWORD, "confirm": "something-else-12",
        },
    )
    assert mismatch.status_code == 400

    short = client.post(
        "/api/setup/share",
        json={
            "token": TOKEN, "project": "team", "enabled": True,
            "password": "tiny", "confirm": "tiny",
        },
    )
    assert short.status_code == 400

    enable = client.post(
        "/api/setup/share",
        json={
            "token": TOKEN, "project": "team", "enabled": True,
            "password": PASSWORD, "confirm": PASSWORD,
        },
    )
    assert enable.status_code == 200
    body = enable.json()
    assert body["ok"] is True
    assert body["share_on_lan"] is True
    assert body["share_url"].startswith("http://")
    assert store_mod.get_project_share_on_lan(_db(), "team") is True
    assert auth_mod.has_password(isolated) is True

    status = client.get("/api/setup/share?project=team").json()
    assert status["share_on_lan"] is True
    assert status["share_url"].startswith("http://")

    disable = client.post(
        "/api/setup/share",
        json={"token": TOKEN, "project": "team", "enabled": False},
    )
    assert disable.status_code == 200
    assert disable.json()["share_on_lan"] is False
    assert store_mod.get_project_share_on_lan(_db(), "team") is False


def test_invite_box_present(isolated) -> None:
    client = _client()
    enable = client.post(
        "/api/setup/share",
        json={
            "token": TOKEN, "project": "team", "enabled": True,
            "password": PASSWORD, "confirm": PASSWORD,
        },
    )
    assert enable.status_code == 200
    share_url = enable.json()["share_url"]

    page = client.get("/setup?project=team").text
    assert "Share this board on my network" in page
    assert "Tell your team" in page
    assert share_url in page
    assert "same Wi-Fi or network only" in page
    assert "no encryption" in page.lower()
    assert "anyone with the url plus" in page.lower()
    assert PASSWORD not in page
    assert "\u2014" not in page


def test_lan_ip_falls_back_to_hostname(monkeypatch) -> None:
    import socket as socket_mod

    class _Boom:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise OSError("no network")

    monkeypatch.setattr(socket_mod, "socket", _Boom)
    monkeypatch.setattr(socket_mod, "gethostbyname", lambda _h: (_ for _ in ()).throw(
        OSError("no dns")))
    monkeypatch.setattr(socket_mod, "gethostname", lambda: "my-laptop")
    assert app_mod.get_lan_ip() == "my-laptop"
    assert "my-laptop" in app_mod.get_share_url("team")


def test_no_em_dashes_in_share_surfaces(isolated) -> None:
    client = _client()
    assert "\u2014" not in client.get("/setup?project=team").text
    denied = client.post(
        "/api/setup/share",
        json={"token": TOKEN, "project": "team", "enabled": True},
    )
    assert "\u2014" not in denied.text
    error = auth_mod.share_console_error(isolated)
    assert error is not None
    assert "\u2014" not in error
