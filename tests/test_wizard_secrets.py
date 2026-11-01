"""Wizard secrets: save in the wizard, Test green with no env set."""

from __future__ import annotations

import json
import logging
import stat

import pytest
from fastapi.testclient import TestClient

ENV_KEYS = (
    "META_MODEL",
    "META_BASE_URL",
    "META_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "ADO_ORG_URL",
    "ADO_PROJECT",
    "ADO_TEAM",
    "ADO_PAT",
)

META_KEY = "meta-wizard-test-key-1"
BOT_TOKEN = "xoxb-wizard-test-bot-1"
APP_TOKEN = "xapp-wizard-test-app-1"
ADO_PAT_VALUE = "ado-wizard-test-pat-1"


@pytest.fixture()
def wiz_client(tmp_path, monkeypatch, caplog):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    import pam_pm.web.app as app_mod

    app_mod.SETUP_DONE = False
    caplog.set_level(logging.INFO)
    client = TestClient(app_mod.app)
    yield client, tmp_path, app_mod
    app_mod.SETUP_DONE = False


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_save_then_test_green_no_leaks(wiz_client, caplog) -> None:
    client, home, _ = wiz_client
    model = "wizard-test-model"
    base = "https://wizard.test.local"
    org = "https://dev.azure.com/wizardorg"
    project = "wizardproj"
    team = "wizardteam"

    resp = client.post(
        "/api/setup/provider",
        json={
            "project": "secwiz",
            "provider": "meta",
            "model": model,
            "base_url": base,
            "api_key": META_KEY,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = client.post(
        "/api/setup/slack",
        json={"project": "secwiz", "bot_token": BOT_TOKEN, "app_token": APP_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = client.post(
        "/api/setup/ado",
        json={
            "project": "secwiz",
            "org_url": org,
            "project_name": project,
            "team": team,
            "pat": ADO_PAT_VALUE,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    assert client.post("/api/test/meta?project=secwiz").json()["ok"] is True
    assert client.post("/api/test/provider?project=secwiz").json()["ok"] is True
    assert client.post("/api/test/slack?project=secwiz").json()["ok"] is True
    assert client.post("/api/test/ado?project=secwiz").json()["ok"] is True

    progress_path = home / "wizard_progress.json"
    assert progress_path.exists()
    blob = progress_path.read_text(encoding="utf-8")
    for secret in (META_KEY, BOT_TOKEN, APP_TOKEN, ADO_PAT_VALUE):
        assert secret not in blob
    progress = json.loads(blob)
    steps = progress["secwiz"]["steps"]
    assert steps["model"]["has_key"] is True
    assert steps["model"]["model"] == model
    assert steps["slack"]["has_bot_token"] is True
    assert steps["slack"]["has_app_token"] is True
    assert steps["ado"]["has_pat"] is True
    assert steps["ado"]["org_url"] == org
    assert steps["ado"]["project"] == project

    for name in (
        "meta_api_key.txt",
        "slack_bot_token.txt",
        "slack_app_token.txt",
        "ado_pat.txt",
    ):
        assert _mode(home / name) == 0o600

    logs = caplog.text
    for secret in (META_KEY, BOT_TOKEN, APP_TOKEN, ADO_PAT_VALUE):
        assert secret not in logs

    page = client.get("/setup?project=secwiz").text
    for secret in (META_KEY, BOT_TOKEN, APP_TOKEN, ADO_PAT_VALUE):
        assert secret not in page
    assert "secrets file" not in page.lower()


def test_env_override_wins(wiz_client, monkeypatch) -> None:
    _, home, _ = wiz_client
    from pam_pm.core import secrets as secrets_mod

    secrets_mod.save_secret("META_API_KEY", "saved-meta-1", home)
    secrets_mod.save_secret("SLACK_BOT_TOKEN", "saved-bot-1", home)
    secrets_mod.save_secret("ADO_PAT", "saved-pat-1", home)
    assert secrets_mod.effective_secret("META_API_KEY", home) == "saved-meta-1"

    monkeypatch.setenv("META_API_KEY", "env-meta-1")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "env-bot-1")
    monkeypatch.setenv("ADO_PAT", "env-pat-1")
    assert secrets_mod.effective_secret("META_API_KEY", home) == "env-meta-1"
    assert secrets_mod.effective_secret("SLACK_BOT_TOKEN", home) == "env-bot-1"
    assert secrets_mod.effective_secret("ADO_PAT", home) == "env-pat-1"

    tokens = secrets_mod.effective_slack_tokens(home)
    assert tokens["bot_token"] == "env-bot-1"
    ado = secrets_mod.effective_ado_config(home)
    assert ado["pat"] == "env-pat-1"


def test_ado_adapter_uses_saved_then_explicit(wiz_client) -> None:
    _, home, _ = wiz_client
    from pam_pm.adapters.pm.azure_devops import ADOAdapter
    from pam_pm.core import secrets as secrets_mod

    secrets_mod.save_ado_config(
        home,
        org_url="https://dev.azure.com/savedorg",
        project="savedproj",
        team="savedteam",
        pat=ADO_PAT_VALUE,
    )
    adapter = ADOAdapter(data_dir=home)
    try:
        assert adapter._org == "https://dev.azure.com/savedorg"
        assert adapter._project == "savedproj"
    finally:
        adapter.close()

    adapter2 = ADOAdapter(
        org_url="https://dev.azure.com/explicitorg",
        project="explicitproj",
        data_dir=home,
    )
    try:
        assert adapter2._org == "https://dev.azure.com/explicitorg"
        assert adapter2._project == "explicitproj"
    finally:
        adapter2.close()
