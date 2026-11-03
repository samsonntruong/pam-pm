"""Task C4: setup Test buttons verify live, not just presence.

Fake transports only, no network. One test per backend proving fake
creds return ok:false and valid fakes return ok:true. Teams stays
presence only with an explicit message.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

SETUP_TOKEN = "c4-test-setup-token-1"

META_MODEL = "c4-fake-model"
META_BASE = "https://c4fake.local"
META_GOOD = "c4-good-meta-key-1"
META_BAD = "c4-bad-meta-key-1"

SLACK_GOOD = "xoxb-c4-good-1"
SLACK_BAD = "xoxb-c4-bad-1"

ADO_ORG = "https://c4fake.visualstudio.com"
ADO_PROJECT = "C4Proj"
ADO_GOOD = "c4-good-pat-1"
ADO_BAD = "c4-bad-pat-1"

PROV_BASE = "https://c4prov.local"
PROV_MODEL = "c4-model-1"
PROV_GOOD = "c4-good-prov-key-1"
PROV_BAD = "c4-bad-prov-key-1"

JIRA_BASE = "https://c4test.atlassian.net"
JIRA_EMAIL = "c4tester@example.com"
JIRA_PROJECT = "C4T"
JIRA_TOKEN = "c4-fake-jira-token-1"

TRELLO_BOARD = "C4 Board"
TRELLO_KEY = "c4-fake-trello-key-1"
TRELLO_TOKEN = "c4-fake-trello-token-1"

ENV_KEYS = (
    "META_MODEL",
    "META_BASE_URL",
    "META_API_KEY",
    "SLACK_BOT_TOKEN",
    "ADO_ORG_URL",
    "ADO_PROJECT",
    "ADO_PAT",
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_PROJECT",
    "JIRA_API_TOKEN",
    "TRELLO_BOARD",
    "TRELLO_API_KEY",
    "TRELLO_TOKEN",
    "TEAMS_APP_ID",
    "TEAMS_APP_PASSWORD",
    "TEAMS_TENANT_ID",
)


@pytest.fixture()
def c4_client(tmp_path, monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("PAM_SETUP_TOKEN", SETUP_TOKEN)
    import pam_pm.web.app as app_mod

    app_mod.SETUP_DONE = False
    app_mod.META_CHECK_TRANSPORT = None
    app_mod.PROVIDER_CHECK_TRANSPORT = None
    app_mod.SLACK_CHECK_TRANSPORT = None
    app_mod.ADO_CHECK_TRANSPORT = None
    app_mod.PROVIDER_CLIENT_FACTORY = None
    app_mod.JIRA_CHECK_FACTORY = None
    app_mod.TRELLO_CHECK_FACTORY = None
    client = TestClient(app_mod.app)
    yield client, tmp_path, app_mod
    app_mod.META_CHECK_TRANSPORT = None
    app_mod.PROVIDER_CHECK_TRANSPORT = None
    app_mod.SLACK_CHECK_TRANSPORT = None
    app_mod.ADO_CHECK_TRANSPORT = None
    app_mod.PROVIDER_CLIENT_FACTORY = None
    app_mod.JIRA_CHECK_FACTORY = None
    app_mod.TRELLO_CHECK_FACTORY = None
    try:
        app_mod.reset_board_state()
    except Exception:  # noqa: BLE001, S110
        pass
    app_mod.SETUP_DONE = False


def _models_handler(good_key: str):
    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models") and request.method == "GET":
            if request.headers.get("Authorization") == "Bearer " + good_key:
                return httpx.Response(200, json={"data": [{"id": "m1"}]})
            return httpx.Response(401, json={"error": "bad key"})
        return httpx.Response(404, json={"error": "not found"})

    return _handle


def test_meta_live_probe(c4_client, monkeypatch) -> None:
    client, _, app_mod = c4_client
    monkeypatch.setenv("META_MODEL", META_MODEL)
    monkeypatch.setenv("META_BASE_URL", META_BASE)
    app_mod.META_CHECK_TRANSPORT = httpx.MockTransport(_models_handler(META_GOOD))
    monkeypatch.setenv("META_API_KEY", META_BAD)
    bad = client.post("/api/test/meta").json()
    assert bad["ok"] is False
    assert META_BAD not in str(bad)
    monkeypatch.setenv("META_API_KEY", META_GOOD)
    good = client.post("/api/test/meta").json()
    assert good["ok"] is True


def test_provider_custom_models_probe(c4_client) -> None:
    from pam_pm.core import provider as provider_mod

    client, tmp_path, app_mod = c4_client
    provider_mod.save_provider(tmp_path, "custom", PROV_BASE, PROV_MODEL, PROV_BAD)
    app_mod.PROVIDER_CHECK_TRANSPORT = httpx.MockTransport(
        _models_handler(PROV_GOOD)
    )
    bad = client.post("/api/test/provider").json()
    assert bad["ok"] is False
    provider_mod.save_provider(tmp_path, "custom", PROV_BASE, PROV_MODEL, PROV_GOOD)
    good = client.post("/api/test/provider").json()
    assert good["ok"] is True


def test_slack_authtest_probe(c4_client, monkeypatch) -> None:
    client, _, app_mod = c4_client

    def _handle(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer " + SLACK_GOOD:
            return httpx.Response(200, json={"ok": True, "user": "pam"})
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    app_mod.SLACK_CHECK_TRANSPORT = httpx.MockTransport(_handle)
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_BAD)
    bad = client.post("/api/test/slack").json()
    assert bad["ok"] is False
    assert "rejected" in str(bad.get("hint", "")).lower()
    assert SLACK_BAD not in str(bad)
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_GOOD)
    good = client.post("/api/test/slack").json()
    assert good["ok"] is True


def test_ado_connectiondata_probe(c4_client, monkeypatch) -> None:
    import base64

    client, _, app_mod = c4_client
    want = "Basic " + base64.b64encode((":" + ADO_GOOD).encode()).decode()

    def _handle(request: httpx.Request) -> httpx.Response:
        if "connectionData" in str(request.url):
            if request.headers.get("Authorization") == want:
                return httpx.Response(200, json={"authenticatedUser": {}})
            return httpx.Response(401, json={"message": "bad pat"})
        return httpx.Response(404, json={})

    app_mod.ADO_CHECK_TRANSPORT = httpx.MockTransport(_handle)
    monkeypatch.setenv("ADO_ORG_URL", ADO_ORG)
    monkeypatch.setenv("ADO_PROJECT", ADO_PROJECT)
    monkeypatch.setenv("ADO_PAT", ADO_BAD)
    bad = client.post("/api/test/ado").json()
    assert bad["ok"] is False
    assert ADO_BAD not in str(bad)
    monkeypatch.setenv("ADO_PAT", ADO_GOOD)
    good = client.post("/api/test/ado").json()
    assert good["ok"] is True


def _save_jira(client: TestClient, api_token: str = JIRA_TOKEN) -> None:
    resp = client.post(
        "/api/setup/jira",
        json={
            "token": SETUP_TOKEN,
            "project": "c4wiz",
            "base_url": JIRA_BASE,
            "email": JIRA_EMAIL,
            "project_key": JIRA_PROJECT,
            "api_token": api_token,
        },
    )
    assert resp.json()["ok"] is True


def test_jira_live_auth(c4_client) -> None:
    import base64

    client, _, app_mod = c4_client
    good_token = "c4-good-jira-token-1"
    bad_token = "c4-bad-jira-token-1"
    want = "Basic " + base64.b64encode((JIRA_EMAIL + ":" + good_token).encode()).decode()

    def _handle(request: httpx.Request) -> httpx.Response:
        if "myself" in str(request.url):
            if request.headers.get("Authorization") == want:
                return httpx.Response(200, json={"accountId": "c4"})
            return httpx.Response(401, json={"message": "bad credentials"})
        return httpx.Response(404, json={})

    app_mod.JIRA_CHECK_TRANSPORT = httpx.MockTransport(_handle)
    try:
        _save_jira(client, bad_token)
        bad_resp = client.post("/api/test/jira?project=c4wiz")
        bad = bad_resp.json()
        assert bad["ok"] is False
        assert "rejected" in str(bad.get("hint", "")).lower()
        assert bad_token not in bad_resp.text
        _save_jira(client, good_token)
        good = client.post("/api/test/jira?project=c4wiz").json()
        assert good["ok"] is True
    finally:
        app_mod.JIRA_CHECK_TRANSPORT = None


def _save_trello(
    client: TestClient, api_key: str = TRELLO_KEY, token: str = TRELLO_TOKEN
) -> None:
    resp = client.post(
        "/api/setup/trello",
        json={
            "token": SETUP_TOKEN,
            "project": "c4wiz",
            "board": TRELLO_BOARD,
            "api_key": api_key,
            "trello_token": token,
        },
    )
    assert resp.json()["ok"] is True


def test_trello_live_auth(c4_client) -> None:
    client, _, app_mod = c4_client
    good_key = "c4-good-trello-key-1"
    good_token = "c4-good-trello-token-1"
    bad_token = "c4-bad-trello-token-1"

    def _handle(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if params.get("key") == good_key and params.get("token") == good_token:
            return httpx.Response(200, json={"id": "c4me"})
        return httpx.Response(401, json={"message": "bad key"})

    app_mod.TRELLO_CHECK_TRANSPORT = httpx.MockTransport(_handle)
    try:
        _save_trello(client, good_key, bad_token)
        bad_resp = client.post("/api/test/trello?project=c4wiz")
        bad = bad_resp.json()
        assert bad["ok"] is False
        assert "rejected" in str(bad.get("hint", "")).lower()
        assert bad_token not in bad_resp.text
        assert good_key not in bad_resp.text
        _save_trello(client, good_key, good_token)
        good = client.post("/api/test/trello?project=c4wiz").json()
        assert good["ok"] is True
    finally:
        app_mod.TRELLO_CHECK_TRANSPORT = None


def test_teams_presence_only_message(c4_client, monkeypatch) -> None:
    client, _, _ = c4_client
    monkeypatch.setenv("TEAMS_APP_ID", "c4-app-id")
    monkeypatch.setenv("TEAMS_APP_PASSWORD", "c4-app-pw")
    monkeypatch.setenv("TEAMS_TENANT_ID", "c4-tenant")
    data = client.post("/api/test/teams").json()
    assert data["ok"] is True
    assert "(presence only, send tested on Save)" in data["message"]
