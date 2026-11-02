"""End to end loop simulations with fakes, no new product features.

Six connected journeys using TestClient plus fake adapters plus a
fake model client. No network, no secrets.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.sqlite_board import NativeBoard
from pam_pm.agents import next_sprint as ns_mod
from pam_pm.agents.interviewer import Interviewer
from pam_pm.agents.kickoff import approve_kickoff, propose_kickoff
from pam_pm.agents.lead import create_retro_items, synthesize_review
from pam_pm.core import auth as auth_mod
from pam_pm.core import memory as memory_mod
from pam_pm.core import store as store_mod
from pam_pm.core.config import Config
from pam_pm.core.writes import WriteService
from pam_pm.models.client import ChatResult, ChatToolCall
from pam_pm.rules.templates.blocker import blocker_intake, on_blocker_closed
from pam_pm.rules.templates.follow_up import on_item_done
from pam_pm.rules.templates.morning import morning_checkin
from pam_pm.web import app as app_mod

TOKEN = "e2e-loops-setup-token-1"


class FakeModelClient:
    """Scripted model returning questions then one tool call."""

    def __init__(self, script: list[ChatResult]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        _ = (messages, tools, max_tokens)
        self.calls.append({"messages": messages, "tools": tools})
        if len(self.calls) <= len(self._script):
            return self._script[len(self.calls) - 1]
        return self._script[-1]


def _isolate(monkeypatch: Any, tmp_path: Any, dry_run: str = "1") -> TestClient:
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("PAM_SETUP_TOKEN", TOKEN)
    monkeypatch.setenv("PAM_DRY_RUN", dry_run)
    monkeypatch.setenv("META_MODEL", "fake-model")
    monkeypatch.setenv("META_BASE_URL", "https://fake.local")
    monkeypatch.setenv("META_API_KEY", "fake-key")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("DAILY_SPEND_CAP", "5")
    for key in ("PAM_ALLOWED_HOST", "PAM_CONSOLE_PASSWORD", "PAM_SHARE_ON_LAN"):
        monkeypatch.delenv(key, raising=False)
    auth_mod.reset_sessions()
    auth_mod.reset_login_attempts()
    app_mod.reset_board_state()
    app_mod.SETUP_DONE = False
    app_mod.LAST_KICKOFF["proposal"] = []
    app_mod.LAST_KICKOFF["approved"] = []
    app_mod.LAST_REVIEW["markdown"] = ""
    app_mod.LAST_REVIEW["retro_actions"] = []
    app_mod.LAST_REVIEW["low_confidence"] = []
    app_mod.LAST_NEXT_SPRINT["proposal"] = None
    app_mod.LAST_NEXT_SPRINT["project"] = "default"
    return TestClient(app_mod.app)


def _roster() -> list[dict[str, Any]]:
    return [
        {"name": "Maya", "skills": "backend api", "capacity": 10.0},
        {"name": "Sam", "skills": "design", "capacity": 8.0},
    ]


def _interview_script(specific: str) -> list[ChatResult]:
    return [
        ChatResult(text="What is the status of the task?", tool_calls=[]),
        ChatResult(text="Can you tell me more?", tool_calls=[]),
        ChatResult(
            text="",
            tool_calls=[
                ChatToolCall(
                    name="finish_interview",
                    arguments=json.dumps(
                        {
                            "status": "Active",
                            "remaining": 2.0,
                            "eta": "2026-11-06",
                            "evidence_quote": specific,
                            "blocker": None,
                            "blocker_owner": None,
                            "confidence": 0.8,
                        }
                    ),
                )
            ],
        ),
    ]


def test_lead_loop_setup_to_board_to_activity(tmp_path, monkeypatch) -> None:
    """Lead loop: setup completes, kickoff proposes, board shows, logged."""
    client = _isolate(monkeypatch, tmp_path)
    project = "e2e-lead"
    try:
        board = NativeBoard()
        app_mod.set_board_adapter(project, board)
        resp = client.post(
            "/api/setup/backend",
            json={"token": TOKEN, "project": project, "backend": "pam_board"},
        )
        assert resp.status_code == 200
        roster = client.post(
            "/api/settings/roster",
            json={"project": project, "entries": _roster()},
        )
        assert roster.status_code == 200
        assert client.post(f"/api/test/meta?project={project}").json()["ok"] is True
        assert client.post(f"/api/test/slack?project={project}").json()["ok"] is True
        assert client.post(f"/api/test/people?project={project}").json()["ok"] is True
        assert client.post(f"/api/test/rules?project={project}").json()["ok"] is True
        assert client.post(f"/api/test/safety?project={project}").json()["ok"] is True
        done = client.post("/api/setup/complete", json={"token": TOKEN})
        assert done.status_code == 200
        assert done.json()["ok"] is True

        brief = "Maya: build login api, 4h\nDesign landing page, 3h"
        db = Config().db_path()
        store_mod.set_project_backend(db, project, "pam_board")
        proposal = propose_kickoff(brief, _roster(), board, db=db)
        assert proposal["has_history"] is False
        items = proposal["items"]
        assert items[0]["owner"] == "Maya"
        assert "named in brief" in items[0]["reason"]
        assert items[1]["owner"] == "Sam"
        assert "design" in items[1]["reason"]

        created = approve_kickoff(
            items, board, db=db, evidence_quote="kickoff brief approved"
        )
        assert [i.title for i in created] == ["build login api", "Design landing page"]
        assert len(board.list_items()) == 2

        page = client.get(f"/board?project={project}").text
        assert "build login api" in page
        assert "Design landing page" in page
        assert "\u2014" not in page

        writes = store_mod.list_writes(db)
        assert len(writes) >= 2
        assert all(w.evidence_quote for w in writes)
        activity = client.get("/activity").text
        assert "evidence" in activity.lower()
        assert "kickoff brief approved" in activity or "create" in activity.lower()
    finally:
        app_mod.reset_board_state()
        app_mod.SETUP_DONE = False


def test_checkin_loop_vague_pushback_then_specific(tmp_path, monkeypatch) -> None:
    """Check-in loop: vague challenged, specific stored with evidence."""
    client = _isolate(monkeypatch, tmp_path)
    project = "e2e-checkin"
    board = NativeBoard()
    try:
        app_mod.set_board_adapter(project, board)
        store_mod.set_project_backend(Config().db_path(), project, "pam_board")
        item = board.create_item(
            "Fix morning sync query", {"state": "Active", "assigned_to": "U1"}
        )
        chat = InMemoryChat()
        vague = "Still working on it, almost done I think, soon."
        specific = "2 hours left, ETA 2026-11-06, finishing tests."
        chat.script_replies("U1", [vague, specific])
        model = FakeModelClient(_interview_script(specific))
        db = Config().db_path()
        writes = WriteService(db, dry_run=True)
        out = morning_checkin({"roster": ["U1"]}, chat, board, model, writes=writes)
        assert "U1" in out
        challenged = [
            dm
            for dm in chat.dms
            if "remain" in dm["text"].lower() and "eta" in dm["text"].lower()
        ]
        assert challenged, f"vague answer was not challenged: {chat.dms}"
        assert len(chat.dms) <= 6
        stored = board.get_item(item.id)
        assert stored.fields.get("evidence_quote") == specific
        rows = store_mod.list_writes(db)
        assert rows
        assert any(specific in (r.evidence_quote or "") for r in rows)
        assert writes.undo(rows[0].id or 0) is True
        assert store_mod.set_write_status(db, rows[0].id or 0, "proposed") is True
        board_page = client.get(f"/board?project={project}").text
        assert "Fix morning sync query" in board_page
        activity = client.get("/activity").text
        assert "2 hours" in activity
        assert "\u2014" not in activity
    finally:
        board.close()
        app_mod.reset_board_state()


def test_blocker_loop_link_notify_close_clear(tmp_path, monkeypatch) -> None:
    """Blocker loop: report, link, notify, close, reporter hears clear."""
    client = _isolate(monkeypatch, tmp_path)
    project = "e2e-blocker"
    board = NativeBoard()
    try:
        app_mod.set_board_adapter(project, board)
        db = Config().db_path()
        store_mod.set_project_backend(db, project, "pam_board")
        writes = WriteService(db, dry_run=True)
        chat = InMemoryChat()
        blocked = board.create_item(
            "Login form", {"state": "Active", "assigned_to": "U1"}
        )
        chat.script_replies(
            "U1", ["Login form is blocked", "U2 owns the review", "Misses sprint goal"]
        )
        result = blocker_intake("U1", chat, board, writes, blocked_item_id=blocked.id)
        blocking_id = str(result["blocking_item"].id)
        assert blocking_id != str(blocked.id)
        assert "U2 owns the review" in str(result["evidence_quote"])
        links = board.list_links(str(blocked.id))
        assert len(links) == 1
        assert links[0].source_id == blocking_id
        assert links[0].target_id == str(blocked.id)
        assert links[0].rel == "System.LinkTypes.Dependency-Forward"
        eta_dms = [dm for dm in chat.dms if dm["user_id"] == "U2"]
        assert len(eta_dms) == 1
        assert "eta" in eta_dms[0]["text"].lower()

        board.close_item(blocking_id)
        dms_before = len(chat.dms)
        notify_id = on_blocker_closed(blocking_id, "U1", chat, board)
        assert notify_id
        sent = on_item_done(blocking_id, chat, board)
        assert len(sent) == 1
        clear_dms = chat.dms[dms_before:]
        assert [dm for dm in clear_dms if dm["user_id"] == "U1"]
        assert any("clear" in dm["text"].lower() for dm in clear_dms)
        assert any("status" in dm["text"].lower() for dm in clear_dms)
        rows = store_mod.list_writes(db)
        assert len(rows) >= 2
        assert all(r.evidence_quote for r in rows)
        data = client.get(f"/api/board?project={project}").json()
        assert data["ok"] is True
        assert any(i["title"] == "Login form" for i in data["items"])
        assert "\u2014" not in client.get(f"/board?project={project}").text
    finally:
        board.close()
        app_mod.reset_board_state()


def test_review_loop_flags_to_retro_to_next_sprint(tmp_path, monkeypatch) -> None:
    """Review loop: flags, retro saved, carryover, approve starts sprint."""
    client = _isolate(monkeypatch, tmp_path, dry_run="0")
    project = "e2e-review"
    board = NativeBoard()
    try:
        app_mod.set_board_adapter(project, board)
        db = Config().db_path()
        store_mod.set_project_backend(db, project, "pam_board")
        writes = WriteService(db, dry_run=True)
        sprint = board.create_iteration("Sprint 1")
        first = board.create_item("Login form", {"state": "New", "assigned_to": "Maya"})
        board.set_item_iteration(first.id, sprint.id)
        second = board.create_item("API keys", {"state": "Active", "assigned_to": "Sam"})
        board.set_item_iteration(second.id, sprint.id)
        board.add_comment(second.id, "Waiting on API keys.")
        interviews = {
            str(first.id): {
                "status": "New",
                "evidence_quote": "Login form is done, only tests remain.",
                "confidence": 0.9,
            },
            str(second.id): {
                "status": "Active",
                "evidence_quote": "Waiting on API keys from Sam.",
                "confidence": 0.2,
            },
        }
        events = [{"operation": "comment", "item_id": str(second.id)}]
        review = synthesize_review(
            [
                {"id": str(first.id), "title": "Login form", "state": "New"},
                {"id": str(second.id), "title": "API keys", "state": "Active"},
            ],
            interviews,
            events,
        )
        assert "## Facts" in review["review_markdown"]
        assert review["low_confidence"] == [str(second.id)]
        actions = review["retro_actions"]
        assert actions
        assert all(a["tags"] == ["retro-action"] for a in actions)
        assert "\u2014" not in review["review_markdown"]

        app_mod.LAST_REVIEW["markdown"] = review["review_markdown"]
        app_mod.LAST_REVIEW["retro_actions"] = actions
        app_mod.LAST_REVIEW["low_confidence"] = review["low_confidence"]
        retro_items = create_retro_items(
            board, actions, writes=writes, evidence_quote="retro from sprint review"
        )
        assert len(retro_items) == len(actions)

        roster = [{"name": "Maya", "capacity": 10.0}, {"name": "Sam", "capacity": 10.0}]
        proposal = ns_mod.propose_next_sprint(
            board, roster, retro_actions=actions, current_iteration_id=sprint.id
        )
        assert proposal["items"][0]["kind"] == "retro"
        carry = [e for e in proposal["items"] if e["kind"] == "carryover"]
        assert carry
        assert carry[0]["owner"] in ("Maya", "Sam")
        live = ns_mod.approve_next_sprint(board, proposal, "Sprint 2", dry_run=False)
        assert live["applied"] is True
        names = [e.name for e in board.list_iterations()]
        assert "Sprint 2" in names
        run = client.post(f"/api/next-sprint/run?project={project}")
        assert run.status_code == 200
        assert run.json()["count"] >= 1
    finally:
        board.close()
        app_mod.reset_board_state()


def test_listen_loop_stop_to_selected_approve(tmp_path, monkeypatch) -> None:
    """Listen loop: stop, proposals, edit one, approve selected only."""
    client = _isolate(monkeypatch, tmp_path, dry_run="0")
    project = "e2e-listen"
    board = NativeBoard()
    try:
        app_mod.set_board_adapter(project, board)
        db = Config().db_path()
        store_mod.set_project_backend(db, project, "pam_board")
        memory_mod.upsert_person(
            db, "Avery", role="Backend Engineer",
            skillsets=["checkout", "api"], source="test",
        )
        session_id = "e2e-listen-1"
        store_mod.create_listen_session(db, session_id, project, "Standup")
        store_mod.insert_listen_segment(
            db, session_id, project,
            "Avery will draft the rollout checklist by Friday. "
            "Blake will confirm the staging window tomorrow.",
        )
        finish = client.post(f"/api/meetings/{session_id}/finish?project={project}")
        assert finish.status_code == 200, finish.text
        body = finish.json()
        assert body["ok"] is True
        assert len(body["proposals"]) >= 2
        assert body["proposals"][0]["meeting_id"] == session_id
        assert all(p["estimate"] > 0 for p in body["proposals"])
        assert board.list_items() == []

        edit = client.post(
            f"/api/meetings/{session_id}/proposals/0?project={project}",
            json={"title": "Edited checklist", "owner": "Sam", "estimate": 6.5},
        )
        assert edit.status_code == 200, edit.text
        assert edit.json()["proposal"]["title"] == "Edited checklist"
        approve = client.post(
            f"/api/meetings/{session_id}/proposals/approve?project={project}",
            json={"selected": [1]},
        )
        assert approve.status_code == 200, approve.text
        items = board.list_items()
        assert len(items) == 1
        assert "staging window" in items[0].title
        assert session_id in str(items[0].fields.get("description", ""))
        assert "Transcript" in str(items[0].fields.get("description", ""))
        rows = store_mod.list_writes(db)
        assert rows
        assert all(r.evidence_quote for r in rows)
        saved = store_mod.get_listen_proposals(db, session_id)
        assert saved is not None and saved["status"] == "partial"
    finally:
        board.close()
        app_mod.reset_board_state()


def test_share_plus_memory_loop(tmp_path, monkeypatch) -> None:
    """Share plus memory: roles captured, capacity and skill used, guard."""
    client = _isolate(monkeypatch, tmp_path)
    project = "e2e-share"
    board = NativeBoard()
    try:
        app_mod.set_board_adapter(project, board)
        db = Config().db_path()
        store_mod.set_project_backend(db, project, "pam_board")
        propose_kickoff(
            "Maya: build login api, 4h",
            [
                {"name": "Maya", "skills": "backend api", "capacity": 10.0},
                {"name": "Sam", "skills": "design", "capacity": 8.0},
            ],
            board,
            db=db,
        )
        maya = memory_mod.get_person(db, "Maya")
        assert maya is not None and maya["source"] == "kickoff"
        assert "backend" in maya["skillsets"]
        assert maya["weekly_capacity"] == 10.0

        memory = {
            "people": [
                {"name": "Maya", "weekly_capacity": 10.0, "skillsets": ["api"]},
                {"name": "Sam", "weekly_capacity": 10.0, "skillsets": ["design"]},
            ],
            "bias": 1.0,
        }
        skill = ns_mod.propose_next_sprint(
            board,
            [{"name": "Maya"}, {"name": "Sam"}],
            backlog=[{"title": "Build login api", "estimate": 4.0}],
            current_iteration_id="nope",
            memory=memory,
        )
        assert skill["items"][0]["owner"] == "Maya"
        assert "Skill match (api)" in skill["items"][0]["reason"]
        tight = {
            "people": [{"name": "Maya", "weekly_capacity": 4.0, "skillsets": []}],
            "bias": 1.0,
        }
        held = ns_mod.propose_next_sprint(
            board,
            [{"name": "Maya"}],
            backlog=[{"title": "Big rewrite", "estimate": 8.0}],
            current_iteration_id="nope",
            memory=tight,
        )
        assert held["items"] == []
        assert "Held for capacity: Big rewrite." in held["risk_note"]

        store_mod.set_project_share_on_lan(db, project, True)
        error = auth_mod.share_console_error(tmp_path)
        assert error is not None
        assert "password" in error.lower()
        assert "\u2014" not in error

        password = "team-share-12"
        auth_mod.set_password(password, tmp_path)
        assert auth_mod.has_password(tmp_path) is True
        anon = client
        redirect = anon.get(f"/board?project={project}", follow_redirects=False)
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/login"
        assert anon.post("/login", json={"password": "wrong-password-1"}).status_code == 401
        good = anon.post("/login", json={"password": password})
        assert good.status_code == 200
        assert anon.get(f"/board?project={project}").status_code == 200
        assert Interviewer.MAX_TURNS if hasattr(Interviewer, "MAX_TURNS") else True
    finally:
        auth_mod.reset_sessions()
        auth_mod.reset_login_attempts()
        board.close()
        app_mod.reset_board_state()
