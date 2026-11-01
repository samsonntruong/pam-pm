"""WP5 item 7 provider picker: Meta default, custom validation, fallback."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient


def _setup_token() -> str:
    try:
        import pam_pm.web.app as _m

        return _m.get_setup_token()
    except Exception:  # noqa: BLE001
        import os

        return os.environ.get("PAM_SETUP_TOKEN", "")



@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("META_MODEL", "fake-model")
    monkeypatch.setenv("META_BASE_URL", "https://fake.local")
    monkeypatch.setenv("META_API_KEY", "fake-key")
    import pam_pm.web.app as app_mod

    app_mod.SETUP_DONE = False
    app_mod.PROVIDER_CLIENT_FACTORY = None
    client = TestClient(app_mod.app)
    yield client, tmp_path, app_mod
    app_mod.SETUP_DONE = False
    app_mod.PROVIDER_CLIENT_FACTORY = None


class _FakeResult:
    def __init__(self, text="", calls=None):
        self.text = text
        self.tool_calls = calls or []


class _FakeClient:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.mode == "no_tools":
            return _FakeResult(text="hello", calls=[])
        if self.calls == 1:
            from pam_pm.models.client import ChatToolCall

            return _FakeResult(
                text="", calls=[ChatToolCall(name="get_time", arguments="{}")]
            )
        if self.calls == 2:
            if self.mode == "bad_json":
                return _FakeResult(text="not json", calls=[])
            return _FakeResult(text='{"ok": true}', calls=[])
        if self.mode == "no_image":
            return _FakeResult(text="", calls=[])
        return _FakeResult(text="two notes", calls=[])

    def close(self):
        return None


def test_meta_is_default(isolated) -> None:
    from pam_pm.core import provider as provider_mod

    client, tmp_path, _ = isolated
    saved = provider_mod.load_provider(tmp_path)
    assert saved["provider"] == "meta"
    page = client.get("/setup?project=default").text
    assert "Step 1: Model provider" in page
    assert "Meta" in page
    assert "Recommended" in page
    assert "Custom OpenAI compatible" in page
    assert "Base URL" in page
    assert "Model name" in page
    assert "API key" in page
    assert "/api/test/provider" in page
    assert "\u2014" not in page
    assert "fake-key" not in page


def test_custom_config_validation(isolated) -> None:
    from pam_pm.core import provider as provider_mod

    _, tmp_path, _ = isolated
    ok, _, hint = provider_mod.validate_custom_config("", "m", "k")
    assert ok is False and hint
    ok, _, _ = provider_mod.validate_custom_config("notaurl", "m", "k")
    assert ok is False
    ok, _, _ = provider_mod.validate_custom_config("https://h", "", "k")
    assert ok is False
    ok, _, _ = provider_mod.validate_custom_config("https://h", "m", "")
    assert ok is False
    ok, _, _ = provider_mod.validate_custom_config("https://h", "m", "k")
    assert ok is True
    saved = provider_mod.save_provider(
        tmp_path, "custom", "https://h", "m", "super-secret-key"
    )
    assert saved["provider"] == "custom"
    assert saved["has_key"] is True
    raw = (tmp_path / "provider.json").read_text(encoding="utf-8")
    assert "super-secret-key" not in raw
    assert "has_key" in raw
    assert (tmp_path / "provider_key.txt").read_text(encoding="utf-8") == "super-secret-key"


def test_provider_test_endpoint_with_fakes(isolated) -> None:
    from pam_pm.core import provider as provider_mod

    client, tmp_path, app_mod = isolated
    provider_mod.save_provider(tmp_path, "custom", "https://h", "m", "k1")
    app_mod.PROVIDER_CLIENT_FACTORY = lambda b, m, k: _FakeClient("ok")
    resp = client.post("/api/test/provider?project=default")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert "passed" in resp.json()["message"].lower()
    app_mod.PROVIDER_CLIENT_FACTORY = lambda b, m, k: _FakeClient("no_tools")
    resp = client.post("/api/test/provider?project=default")
    assert resp.json()["ok"] is False
    assert "tool" in resp.json()["message"].lower()


def test_save_provider_on_both_backends(isolated) -> None:
    client, tmp_path, _ = isolated
    for project, backend in (("pamp", "pam_board"), ("adop", "azure_devops")):
        resp = client.post(
            "/api/setup/backend", json={"token": _setup_token(), "project": project, "backend": backend}
        )
        assert resp.status_code == 200
        resp = client.post(
            "/api/setup/provider",
            json={"token": _setup_token(), "project": project,
                "provider": "custom",
                "base_url": "https://custom.local",
                "model": "cm-1",
                "api_key": "backend-key-xyz",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
    raw = (tmp_path / "provider.json").read_text(encoding="utf-8")
    assert "backend-key-xyz" not in raw
    progress = json.loads((tmp_path / "wizard_progress.json").read_text(encoding="utf-8"))
    assert "backend-key-xyz" not in json.dumps(progress)
    assert progress["pamp"]["steps"]["model"]["provider"] == "custom"
    assert progress["adop"]["steps"]["model"]["provider"] == "custom"


def test_fallback_planner_path_with_custom_provider() -> None:
    from pam_pm.models.vision import PATH_FALLBACK, VisionClient, plan_from_photo

    class _Reader:
        def chat(self, messages):
            from pam_pm.models.client import ChatResult

            return ChatResult(text="Alpha\nBeta")

    def _boom(image):
        raise RuntimeError("SAM missing")

    out = plan_from_photo(b"fake-bytes", VisionClient(_Reader()), _boom, provider="custom")
    assert out["path"] == PATH_FALLBACK
    assert out["masks"] == []
    assert "Alpha" in out["notes_text"]
    client = VisionClient(_Reader(), segmenter=lambda img: ["c1"], provider="custom")
    assert client.segment(b"x") == []


def test_key_never_in_logs(tmp_path, caplog) -> None:
    from pam_pm.core import provider as provider_mod

    secret = "log-secret-abc-123"
    with caplog.at_level(logging.INFO, logger="pam_pm.core.provider"):
        provider_mod.save_provider(tmp_path, "custom", "https://h", "m", secret)
        provider_mod.load_provider(tmp_path)
    assert secret not in caplog.text
