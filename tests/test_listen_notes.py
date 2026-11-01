"""LISTEN notes tests: recorded model responses, no network, both backends."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pam_pm.agents import notes as notes_mod
from pam_pm.core import store as store_mod

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "listen"

TRANSCRIPT = (
    "Avery: we agreed to ship the checklist before the demo. "
    "Avery will draft the rollout checklist by Friday. "
    "Someone needs to confirm the staging window. "
    "Risk: the staging window might slip."
)


class RecordedClient:
    """Model client replaying one recorded response body."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.seen: list[Any] = []

    def chat(self, messages: Any, tools: Any = None, **kwargs: Any) -> dict[str, str]:
        self.seen.append(messages)
        return {"text": self.body}


def _recorded(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_extract_notes_recorded_valid() -> None:
    body = _recorded("notes_valid.json")
    result = notes_mod.extract_notes(
        TRANSCRIPT,
        project_context="Meeting notes. Board items:\n- 101: rollout checklist [New]",
        existing_items=[{"id": "101", "title": "rollout checklist"}],
        model_client=RecordedClient(body),
    )
    assert result["model_path"] == "live"
    assert result["summary"].startswith("Team agreed")
    assert result["decisions"][0]["text"] == "Ship the checklist before the demo."
    assert len(result["action_items"]) == 2
    assert result["action_items"][0]["owner"] == "Avery"
    assert result["action_items"][1]["owner"] is None
    assert result["proposed_raid"][0]["kind"] == "risk"
    assert result["matches"][0]["item_id"] == "101"
    for action in result["action_items"]:
        assert action["evidence_quote"]


def test_extract_notes_recorded_invalid_falls_back() -> None:
    body = _recorded("notes_invalid.txt")
    result = notes_mod.extract_notes(
        TRANSCRIPT, model_client=RecordedClient(body)
    )
    assert result["model_path"] == "local"
    assert "checklist" in result["summary"].lower()
    assert result["action_items"], "local fallback must find actions"
    for action in result["action_items"]:
        words = action["evidence_quote"].lower()
        assert any(w in TRANSCRIPT.lower() for w in words.split()[:4])


def test_extract_notes_prefix_stable_with_recorded() -> None:
    first = notes_mod.extract_notes(
        "alpha meeting words",
        project_context="Meeting notes. Board items:\nNo board items.",
        model_client=RecordedClient(_recorded("notes_valid.json")),
    )
    assert first["model_path"] == "live"
    local = notes_mod.extract_notes("alpha meeting words")
    assert local["model_path"] == "local"


class _FakeBoard:
    supports_dependency_links = True

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self._next = 1

    def list_items(self, limit: int = 500) -> list[dict[str, Any]]:
        return [{"id": "101", "title": "rollout checklist", "state": "New"}][:limit]

    def list_states(self) -> list[str]:
        return ["New", "Active", "Done"]

    def create_item(self, title: str, fields: dict[str, Any]) -> dict[str, Any]:
        item = {"id": str(self._next), "title": title, **fields}
        self._next += 1
        self.created.append(item)
        return item


@pytest.mark.parametrize("backend", ["pam_board", "azure_devops"])
def test_notes_approve_dry_run_both_backends(tmp_path, monkeypatch, backend) -> None:
    import pam_pm.web.app as app_mod

    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("PAM_DRY_RUN", "1")
    from pam_pm.core.config import Config

    db = Config().db_path()
    store_mod.set_project_backend(db, "demo", backend)
    store_mod.create_listen_session(db, "notes-1", "demo", "Standup")
    store_mod.insert_listen_segment(db, "notes-1", "demo", TRANSCRIPT)
    board = _FakeBoard()
    app_mod._BOARD_OVERRIDES["demo"] = board
    try:
        client = TestClient(app_mod.app)
        resp = client.post("/api/meetings/notes-1/notes?project=demo")
        assert resp.status_code == 200, resp.text
        saved = store_mod.get_listen_notes(db, "notes-1")
        assert saved is not None
        assert saved["payload"]["action_items"]
        approve = client.post(
            "/api/meetings/notes-1/approve?project=demo",
            data={"notify": "off", "owner-0": "Avery"},
        )
        assert approve.status_code == 200, approve.text
        assert board.created, "approve must create action items"
        assert "Evidence" in board.created[0].get("description", "")
        assert json.loads(_recorded("notes_valid.json"))["action_items"]
    finally:
        app_mod._BOARD_OVERRIDES.pop("demo", None)
