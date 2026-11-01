"""RAID log tests on native and ADO backends (fakes only)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from pam_pm.core import raid as raid_mod
from pam_pm.core.writes import WriteService


class FakeWrites:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.proposed: list[dict[str, Any]] = []
        self.applied: list[int] = []
        self._next = 1

    def propose(self, entry: dict[str, Any]) -> int:
        self.proposed.append(dict(entry))
        ident = self._next
        self._next += 1
        return ident

    def apply(self, write_id: int) -> bool:
        self.applied.append(int(write_id))
        return True


@pytest.fixture(params=["pam_board", "azure_devops"], ids=["native", "ado"])
def backend(request: Any) -> str:
    return str(request.param)


def test_propose_accept_close_delete(tmp_path: Any, backend: str) -> None:
    db = tmp_path / "raid.db"
    writes = FakeWrites()
    item = raid_mod.propose_item(
        db, backend, "atlas", "risk", "API keys may slip",
        owner="Sam", severity="high", evidence_quote="Waiting on keys.",
        writes=writes,
    )
    assert item["status"] == "proposed"
    assert item["write_id"] > 0
    opened = raid_mod.accept_item(db, backend, item["external_id"], writes=writes)
    assert opened is not None and opened["status"] == "open"
    closed = raid_mod.close_item(db, backend, item["external_id"], writes=writes)
    assert closed is not None and closed["status"] == "closed"
    assert raid_mod.delete_item(db, backend, item["external_id"]) is True
    assert raid_mod.get_item(db, backend, item["external_id"]) is None


def test_assumptions_from_text_with_dedupe(tmp_path: Any, backend: str) -> None:
    db = tmp_path / "raid.db"
    text = "We assume the API is stable.\nWe assume the API is stable.\nPlain line."
    first = raid_mod.propose_assumptions_from_text(
        db, backend, "atlas", text, writes=FakeWrites()
    )
    assert len(first) == 1
    assert first[0]["kind"] == "assumption"
    second = raid_mod.propose_assumptions_from_text(
        db, backend, "atlas", text, writes=FakeWrites()
    )
    assert second == []


def test_backends_keep_separate_logs(tmp_path: Any) -> None:
    db = tmp_path / "raid.db"
    raid_mod.save_item(db, "pam_board", "risk-1", kind="risk", title="Native risk")
    raid_mod.save_item(db, "azure_devops", "risk-1", kind="risk", title="ADO risk")
    native = raid_mod.get_item(db, "pam_board", "risk-1")
    ado = raid_mod.get_item(db, "azure_devops", "risk-1")
    assert native and native["title"] == "Native risk"
    assert ado and ado["title"] == "ADO risk"


def test_exports_mask_quote_for_non_lead(tmp_path: Any, backend: str) -> None:
    db = tmp_path / "raid.db"
    raid_mod.save_item(
        db, backend, "issue-9", kind="issue", title="Blocked deploy",
        evidence_quote="Sam said the keys are late.",
    )
    items = raid_mod.list_items(db, backend)
    assert "Sam said" in raid_mod.export_csv(items, for_lead=True)
    assert raid_mod.LEAD_ONLY_QUOTE in raid_mod.export_csv(items, for_lead=False)
    assert raid_mod.LEAD_ONLY_QUOTE in raid_mod.export_markdown(items, for_lead=False)


def test_undo_reverts_proposed_and_open(tmp_path: Any, backend: str) -> None:
    db = tmp_path / "raid.db"
    writes = WriteService(db, dry_run=False)
    item = raid_mod.propose_item(
        db, backend, "atlas", "issue", "Deploy blocked",
        evidence_quote="Blocked on keys.", writes=writes,
    )
    assert raid_mod.on_write_undone(db, int(item["write_id"])) is not None
    assert raid_mod.get_item(db, backend, item["external_id"]) is None
    item2 = raid_mod.propose_item(
        db, backend, "atlas", "risk", "Might miss Friday",
        evidence_quote="At risk.", writes=writes,
    )
    writes2 = FakeWrites()
    raid_mod.accept_item(db, backend, item2["external_id"], writes=writes2)
    back = raid_mod.on_write_undone(db, int(item2["write_id"]))
    assert back is not None and back["status"] == "proposed"


def test_raid_page_and_api_routes(tmp_path: Any, monkeypatch: Any) -> None:
    from pam_pm.web import app as app_mod

    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    client = TestClient(app_mod.app)
    assert client.get("/raid").status_code == 200
    assert 'href="/raid"' in client.get("/setup").text
    created = client.post(
        "/api/raid?project=default",
        json={"kind": "risk", "title": "Keys may slip", "owner": "Sam"},
    )
    assert created.status_code == 200
    ident = created.json()["item"]["external_id"]
    patched = client.patch(
        f"/api/raid/{ident}?project=default", json={"severity": "high"}
    )
    assert patched.status_code == 200
    assert patched.json()["item"]["severity"] == "high"
    listed = client.get("/api/raid?project=default&kind=risk")
    assert listed.status_code == 200 and len(listed.json()["items"]) >= 1
    assert client.get("/api/raid/export?project=default&format=csv").status_code == 200
    assert client.get("/api/raid/export?project=default&format=md").status_code == 200
    assert client.delete(f"/api/raid/{ident}?project=default").status_code == 200


def test_kickoff_propose_reports_assumption_count(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from pam_pm.web import app as app_mod

    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    client = TestClient(app_mod.app)
    resp = client.post(
        "/api/kickoff/propose?project=default",
        data={"brief": "We assume the API is stable and ready.\nShip login, 2h"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["assumptions_count"] == 1
    assert len(payload["assumptions"]) == 1
