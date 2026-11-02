"""Phase 0 batch A2: blocker technical action plan.

Full 6-step owner flow on both backends (Pam board and ADO through the
shared fake transport) with fake chat: cause question, 2 to 4 grounded
steps with plain language edits, per-step estimates, ETA from scheduler
working hours with confirmation, child task creation with write layer
evidence and Undo, waiter ETA summary, and the max 6 turn guard.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.agents.prompts.builder import TOOLS, build_messages
from pam_pm.agents.prompts.system import BLOCKER_PLAN_SYSTEM
from pam_pm.core import store as store_mod
from pam_pm.core.writes import WriteService
from pam_pm.models.client import ChatResult
from pam_pm.rules.scheduler import Scheduler
from pam_pm.rules.templates.blocker_plan import (
    apply_step_edits,
    blocker_action_plan,
    build_plan_messages,
    compute_eta,
    is_confirmed,
    parse_estimates,
    plan_project_context,
    plan_summary,
    propose_steps,
)
from tests.test_both_adapters import _make_board

MON_9AM = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


class _RecordingModel:
    """Scripted model that records every request payload."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        _ = max_tokens
        self.calls.append({"messages": messages, "tools": tools})
        idx = min(len(self.calls) - 1, len(self._texts) - 1)
        return ChatResult(text=self._texts[idx] if self._texts else "")


@pytest.fixture(params=["native", "ado-agile"], ids=["native", "ado-agile"])
def board_pair(request: Any) -> Any:
    board, _, _ = _make_board(str(request.param))
    try:
        yield (str(request.param), board)
    finally:
        board.close()


def _seed_blocking(board: Any) -> tuple[str, str]:
    blocked = board.create_item("Ship report", {"state": "Active"})
    blocking = board.create_item(
        "Fix export bug", {"state": "Active", "assigned_to": "U2"}
    )
    return str(blocked.id), str(blocking.id)


def _remaining_of(board: Any, item_id: str, kind: str) -> Any:
    fields = board.get_item(item_id).fields
    if kind == "native":
        return fields.get("remaining")
    return fields.get("Microsoft.VSTS.Scheduling.RemainingWork")


def _estimate_of(board: Any, item_id: str, kind: str) -> Any:
    fields = board.get_item(item_id).fields
    if kind == "native":
        return fields.get("estimate")
    return fields.get("Microsoft.VSTS.Scheduling.OriginalEstimate")


def _owner_of(board: Any, item_id: str) -> str:
    fields = board.get_item(item_id).fields
    for key in ("assigned_to", "owner", "System.AssignedTo"):
        value = fields.get(key)
        if isinstance(value, dict):
            value = value.get("displayName") or value.get("uniqueName") or ""
        if value:
            return str(value)
    return ""


def test_propose_steps_grounded_in_owner_words() -> None:
    steps = propose_steps("Redis cache stampede on login", "Fix export bug")
    assert 2 <= len(steps) <= 4
    blob = " ".join(steps).lower()
    assert "redis" in blob or "cache" in blob or "stampede" in blob
    assert "fix export bug" in blob
    assert "—" not in " ".join(steps)


def test_apply_step_edits_drop_add_change() -> None:
    steps = ["Fix cache", "Add regression test", "Deploy and verify"]
    dropped, changed = apply_step_edits(steps, "drop 2")
    assert changed is True
    assert dropped == ["Fix cache", "Deploy and verify"]
    added, changed = apply_step_edits(steps, "add verify on staging")
    assert changed is True
    assert added[-1] == "verify on staging"
    edited, changed = apply_step_edits(steps, "change step 1 to restart the worker")
    assert changed is True
    assert edited[0] == "restart the worker"
    kept, changed = apply_step_edits(steps, "keep")
    assert changed is False
    assert kept == steps
    combo, changed = apply_step_edits(steps, "drop 2 and add verify on staging")
    assert changed is True
    assert len(combo) == 3
    assert combo[-1] == "verify on staging"


def test_parse_estimates_formats() -> None:
    assert parse_estimates("2h, 1h, 3h", 3) == [2.0, 1.0, 3.0]
    assert parse_estimates("step 1: 2h step 2: 1h step 3: 3h", 3) == [2.0, 1.0, 3.0]
    assert parse_estimates("2 hours each", 3) == [2.0, 2.0, 2.0]
    assert parse_estimates("6h total", 3) == [2.0, 2.0, 2.0]
    assert parse_estimates("", 2) == [1.0, 1.0]


def test_is_confirmed() -> None:
    assert is_confirmed("yes") is True
    assert is_confirmed("Confirmed, looks good") is True
    assert is_confirmed("no, change step 1") is False
    assert is_confirmed("") is False


def test_compute_eta_same_day_and_multiday() -> None:
    sched = Scheduler(work_start=9, work_end=17)
    eta, _ = compute_eta(4.0, MON_9AM, sched)
    assert eta == "2026-11-02"
    eta2, _ = compute_eta(20.0, MON_9AM, sched)
    assert eta2 == "2026-11-04"
    friday = datetime(2026, 11, 6, 16, 0, tzinfo=UTC)
    eta3, _ = compute_eta(4.0, friday, sched)
    assert eta3 == "2026-11-09"
    narrow = Scheduler(work_start=9, work_end=10)
    eta4, _ = compute_eta(3.0, MON_9AM, narrow)
    assert eta4 == "2026-11-04"


def test_plan_prefix_stable_across_owners_and_times() -> None:
    item = {"id": "42", "title": "Fix export bug"}
    conv = [{"role": "user", "content": "Redis cache stampede on login"}]
    first = build_plan_messages(
        item, conv, {"now": "2026-11-02T09:00:00", "person": "U2", "turn": 1}
    )
    second = build_plan_messages(
        item, conv, {"now": "2026-11-02T15:30:00", "person": "U9", "turn": 3}
    )
    assert first["tools"] is second["tools"]
    assert first["tools"] is TOOLS
    assert first["messages"][0]["content"] == BLOCKER_PLAN_SYSTEM
    head_first = json.dumps(first["messages"][:-1], sort_keys=True)
    head_second = json.dumps(second["messages"][:-1], sort_keys=True)
    assert head_first == head_second
    assert "U2" not in head_first
    assert "U9" not in head_second
    assert "2026-11-02" not in head_first
    tail_first = json.dumps(first["messages"][-1], sort_keys=True)
    tail_second = json.dumps(second["messages"][-1], sort_keys=True)
    assert tail_first != tail_second


def test_plan_project_context_has_no_names_or_time() -> None:
    ctx = plan_project_context({"id": "7", "title": "Fix export bug"})
    assert "U2" not in ctx
    assert "2026" not in ctx
    assert "Fix export bug" in ctx


def test_full_flow_both_backends(board_pair: Any, tmp_path: Any) -> None:
    kind, board = board_pair
    _, blocking_id = _seed_blocking(board)
    chat = InMemoryChat()
    chat.script_replies(
        "U2",
        [
            "Redis cache stampede on login",
            "keep",
            "2h, 1h, 3h",
            "yes, confirmed",
        ],
    )
    db = tmp_path / "a2.db"
    writes = WriteService(db, dry_run=False)
    sched = Scheduler(work_start=9, work_end=17)
    out = blocker_action_plan(
        "U2",
        "U1",
        chat,
        board,
        None,
        writes=writes,
        blocking_item_id=blocking_id,
        scheduler=sched,
        now=MON_9AM,
    )
    assert out["status"] == "confirmed"
    assert out["turns"] == 4
    assert out["turns"] <= 6
    assert len(out["steps"]) == 3
    assert out["estimates"] == [2.0, 1.0, 3.0]
    assert out["total_hours"] == 6.0
    assert out["eta"] == "2026-11-02"
    assert out["children"] and len(out["children"]) == 3
    assert len(out["links"]) == 3
    assert all(link.rel.endswith("Hierarchy-Forward") for link in out["links"])
    parent_links = board.list_links(blocking_id)
    assert len([lk for lk in parent_links if "Hierarchy" in lk.rel]) == 3
    for child, want in zip(out["children"], [2.0, 1.0, 3.0]):
        assert _estimate_of(board, child.id, kind) == want
        assert _remaining_of(board, child.id, kind) == want
        assert _owner_of(board, child.id) == "U2"
    assert _remaining_of(board, blocking_id, kind) == 6.0
    comments = board.list_comments(blocking_id)
    assert len(comments) == 1
    assert "2026-11-02" in comments[0].text
    assert "Redis" in comments[0].text or "stampede" in comments[0].text
    owner_texts = [dm["text"] for dm in chat.dms if dm["user_id"] == "U2"]
    assert len(owner_texts) == 4
    assert "?" in owner_texts[0]
    assert "drop 2" in owner_texts[1]
    assert "2h, 1h, 3h" in owner_texts[2] or "per step" in owner_texts[2].lower()
    assert "2026-11-02" in owner_texts[3]
    waiter_texts = [dm["text"] for dm in chat.dms if dm["user_id"] == "U1"]
    assert len(waiter_texts) == 1
    assert "2026-11-02" in waiter_texts[0]
    assert "\n" not in waiter_texts[0]
    assert out["summary"] in waiter_texts[0]
    assert plan_summary(out["steps"]) == out["summary"]
    logged = store_mod.list_writes(db)
    assert len(logged) >= 8
    assert all(row.evidence_quote for row in logged)
    assert any("stampede" in row.evidence_quote.lower() for row in logged)
    assert writes.undo(out["write_ids"][0]) is True
    assert "—" not in json.dumps(out, default=str)
    assert "—" not in " ".join(owner_texts + waiter_texts)


def test_step_edit_flow_both_backends(board_pair: Any, tmp_path: Any) -> None:
    kind, board = board_pair
    _ = kind
    _, blocking_id = _seed_blocking(board)
    chat = InMemoryChat()
    chat.script_replies(
        "U2",
        [
            "Export query times out on large tenants",
            "drop 2 and add verify on staging",
            "2h, 1h, 1h",
            "yes",
        ],
    )
    out = blocker_action_plan(
        "U2",
        "U1",
        chat,
        board,
        None,
        db=tmp_path / "a2-edit.db",
        blocking_item_id=blocking_id,
        scheduler=Scheduler(work_start=9, work_end=17),
        now=MON_9AM,
    )
    assert out["status"] == "confirmed"
    assert len(out["steps"]) == 3
    assert out["steps"][-1] == "verify on staging"
    assert "regression" not in " ".join(out["steps"]).lower()
    titles = [board.get_item(c.id).title for c in out["children"]]
    assert titles[-1] == "verify on staging"


def test_change_step_flow_both_backends(board_pair: Any, tmp_path: Any) -> None:
    _, board = board_pair
    _, blocking_id = _seed_blocking(board)
    chat = InMemoryChat()
    chat.script_replies(
        "U2",
        [
            "Worker queue backs up after deploys",
            "change step 1 to restart the worker and drain the queue",
            "1h, 1h, 1h",
            "ok",
        ],
    )
    out = blocker_action_plan(
        "U2",
        "U1",
        chat,
        board,
        None,
        db=tmp_path / "a2-change.db",
        blocking_item_id=blocking_id,
        scheduler=Scheduler(work_start=9, work_end=17),
        now=MON_9AM,
    )
    assert out["status"] == "confirmed"
    assert out["steps"][0] == "restart the worker and drain the queue"
    assert board.get_item(out["children"][0].id).title.startswith("restart the worker")


def test_uncertain_cause_asks_question(board_pair: Any, tmp_path: Any) -> None:
    _, board = board_pair
    _, blocking_id = _seed_blocking(board)
    chat = InMemoryChat()
    chat.script_replies("U2", ["not sure", "keep", "1h, 1h, 1h", "yes"])
    out = blocker_action_plan(
        "U2",
        "U1",
        chat,
        board,
        None,
        db=tmp_path / "a2-unsure.db",
        blocking_item_id=blocking_id,
        now=MON_9AM,
    )
    assert out["status"] == "confirmed"
    proposal = [dm["text"] for dm in chat.dms if dm["user_id"] == "U2"][1]
    assert "or something else?" in proposal


def test_decline_creates_no_children(board_pair: Any, tmp_path: Any) -> None:
    _, board = board_pair
    _, blocking_id = _seed_blocking(board)
    before = len(board.list_items(limit=200))
    chat = InMemoryChat()
    chat.script_replies("U2", ["Cache stampede", "keep", "2h, 1h, 1h", "no, wait"])
    out = blocker_action_plan(
        "U2",
        "U1",
        chat,
        board,
        None,
        db=tmp_path / "a2-decline.db",
        blocking_item_id=blocking_id,
        now=MON_9AM,
    )
    assert out["status"] == "declined"
    assert len(board.list_items(limit=200)) == before
    assert [dm for dm in chat.dms if dm["user_id"] == "U1"] == []


def test_max_turns_guard(board_pair: Any, tmp_path: Any) -> None:
    _, board = board_pair
    _, blocking_id = _seed_blocking(board)
    before = len(board.list_items(limit=200))
    chat = InMemoryChat()
    chat.script_replies("U2", ["Cache stampede", "keep", "2h, 1h, 1h", "yes"])
    out = blocker_action_plan(
        "U2",
        "U1",
        chat,
        board,
        None,
        db=tmp_path / "a2-cap.db",
        blocking_item_id=blocking_id,
        max_turns=2,
        now=MON_9AM,
    )
    assert out["status"] == "max_turns"
    assert out["turns"] <= 2
    assert len(board.list_items(limit=200)) == before
    assert [dm for dm in chat.dms if dm["user_id"] == "U1"] == []


def test_model_calls_use_stable_prefix(board_pair: Any, tmp_path: Any) -> None:
    _, board = board_pair
    _, blocking_id = _seed_blocking(board)
    chat = InMemoryChat()
    chat.script_replies("U2", ["Cache stampede", "keep", "2h, 1h, 1h", "yes"])
    model = _RecordingModel(
        ["What is causing the problem?", "Rough estimate per step in hours?"]
    )
    out = blocker_action_plan(
        "U2",
        "U1",
        chat,
        board,
        model,
        db=tmp_path / "a2-model.db",
        blocking_item_id=blocking_id,
        now=MON_9AM,
    )
    assert out["status"] == "confirmed"
    assert len(model.calls) == 2
    for call in model.calls:
        assert call["tools"] is TOOLS
        assert call["messages"][0]["content"] == BLOCKER_PLAN_SYSTEM
        assert call["messages"][-1]["role"] == "user"
        assert "Live context" in call["messages"][-1]["content"]
    first_msgs = model.calls[0]["messages"]
    second_msgs = model.calls[1]["messages"]
    assert first_msgs[0] == second_msgs[0]
    assert first_msgs[1] == second_msgs[1]
    assert len(second_msgs) > len(first_msgs)
    payload = build_messages("blocker_plan", "ctx", [], {"person": "U2"})
    assert payload["messages"][0]["content"] == BLOCKER_PLAN_SYSTEM
