"""WP3b recovery: resume after kill, exactly-once effects, catch-up, misfire.

The same tests run on the native SQLite board and on ADO backed by an
in-memory fake transport, pinning identical recovery behavior on both.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

import pam_pm
from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.azure_devops import ADOAdapter
from pam_pm.adapters.pm.sqlite_board import NativeBoard
from pam_pm.core import outbox as outbox_mod
from pam_pm.core import recovery as recovery_mod
from pam_pm.core import store as store_mod
from pam_pm.rules.poller import ADOPoller


class _FakeADOTransport:
    """Minimal in-memory ADO REST surface for recovery tests."""

    def __init__(self) -> None:
        self._items: dict[int, dict[str, Any]] = {}
        self._comments: dict[int, list[dict[str, Any]]] = {}
        self._next_id = 1
        self._next_comment = 1
        self._clock = 0

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-02-01T00:00:00.{self._clock:06d}Z"

    def _payload(self, item_id: int) -> dict[str, Any]:
        return {"id": item_id, "fields": dict(self._items[item_id])}

    def _apply_ops(self, item_id: int, ops: Any) -> None:
        fields = self._items[item_id]
        for op in ops if isinstance(ops, list) else []:
            if not isinstance(op, dict):
                continue
            path = str(op.get("path", ""))
            if path.startswith("/fields/"):
                fields[path[len("/fields/"):]] = op.get("value")
        fields["System.ChangedDate"] = self._stamp()

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else None
        except ValueError:
            body = None
        if path.endswith("/_apis/wit/wiql") and method == "POST":
            return self._wiql(body)
        if path.endswith("/comments") and "/_apis/wit/workitems/" in path:
            return self._comments_route(path, method, body)
        if "/_apis/wit/workitems" in path and method == "POST" and "/$" in path:
            return self._create(body)
        match = re.search(r"/_apis/wit/workitems/([^/]+)$", path)
        if match:
            return self._item_route(match.group(1), method, body)
        if path.endswith("/_apis/wit/workitems") and method == "GET":
            return self._batch(request)
        return httpx.Response(404, json={"message": "unknown fake route"})

    def _wiql(self, body: Any) -> httpx.Response:
        query = str(body.get("query", "")) if isinstance(body, dict) else ""
        ids = sorted(
            self._items,
            key=lambda i: self._items[i].get("System.ChangedDate", ""),
        )
        tick = re.search(r">\s*'([^']*)'", query)
        if tick:
            ids = [
                i
                for i in ids
                if str(self._items[i].get("System.ChangedDate", "")) > tick.group(1)
            ]
        elif "DESC" in query:
            ids = list(reversed(ids))
        return httpx.Response(200, json={"workItems": [{"id": i} for i in ids]})

    def _create(self, body: Any) -> httpx.Response:
        item_id = self._next_id
        self._next_id += 1
        self._items[item_id] = {
            "System.Title": "",
            "System.State": "",
            "System.ChangedDate": self._stamp(),
        }
        self._apply_ops(item_id, body)
        return httpx.Response(200, json=self._payload(item_id))

    def _batch(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.params.get("ids", "")
        out = [
            self._payload(int(part))
            for part in raw.split(",")
            if part.strip().isdigit() and int(part) in self._items
        ]
        return httpx.Response(200, json={"value": out})

    def _item_route(
        self, item_id: str, method: str, body: Any
    ) -> httpx.Response:
        if not item_id.isdigit() or int(item_id) not in self._items:
            return httpx.Response(404, json={"message": "not found"})
        num = int(item_id)
        if method == "GET":
            return httpx.Response(200, json=self._payload(num))
        if method == "PATCH":
            self._apply_ops(num, body)
            return httpx.Response(200, json=self._payload(num))
        return httpx.Response(404, json={"message": "unknown fake route"})

    def _comments_route(
        self, path: str, method: str, body: Any
    ) -> httpx.Response:
        match = re.search(r"/workitems/([^/]+)/comments$", path)
        item_id = match.group(1) if match else ""
        if not item_id.isdigit() or int(item_id) not in self._items:
            return httpx.Response(404, json={"message": "not found"})
        num = int(item_id)
        if method == "GET":
            return httpx.Response(
                200, json={"value": list(self._comments.get(num, []))}
            )
        text = str(body.get("text", "")) if isinstance(body, dict) else ""
        comment = {
            "id": self._next_comment,
            "text": text,
            "createdBy": {"displayName": "ADO User"},
            "createdDate": self._stamp(),
        }
        self._next_comment += 1
        self._comments.setdefault(num, []).append(comment)
        return httpx.Response(200, json=comment)


@pytest.fixture(params=["native", "ado"], ids=["native", "ado"])
def backend_case(
    request: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    kind = str(request.param)
    db = str(tmp_path / "pam.db")
    if kind == "native":
        board_path = str(tmp_path / "board.db")
        board = NativeBoard(board_path)

        def reopen_native() -> NativeBoard:
            board.close()
            return NativeBoard(board_path)

        reopen = reopen_native
    else:
        monkeypatch.setenv("ADO_ORG_URL", "https://dev.azure.com/fake")
        monkeypatch.setenv("ADO_PROJECT", "proj")
        monkeypatch.setenv("ADO_TEAM", "team")
        monkeypatch.setenv("ADO_PAT", "test-pat-value")
        fake = _FakeADOTransport()
        board = ADOAdapter(transport=httpx.MockTransport(fake.handle))

        def reopen_ado() -> ADOAdapter:
            board.close()
            return ADOAdapter(transport=httpx.MockTransport(fake.handle))

        reopen = reopen_ado
    case = {"kind": kind, "db": db, "board": board, "reopen": reopen}
    yield case
    if not case.get("reopened", False):
        board.close()


_CHILD_APPEND = (
    "import sys, time\n"
    "from pam_pm.core import recovery\n"
    "db, key, person, item = sys.argv[1:5]\n"
    "for i in range(200):\n"
    "    role = 'assistant' if i % 2 == 0 else 'user'\n"
    "    recovery.append_turn(db, key, person, item, role, 'turn-%d' % i)\n"
    "    time.sleep(0.05)\n"
)


def _ensure_newer(board: Any, item_id: str, watermark: str) -> None:
    for attempt in range(60):
        current = board.get_item(item_id)
        if str(current.changed or "") > watermark:
            return
        time.sleep(0.05)
        board.update_item(item_id, {"remaining": attempt})
    raise AssertionError("board timestamps never advanced past the watermark")


def test_sigkill_resumes_at_right_turn(backend_case: Any) -> None:
    db = str(backend_case["db"])
    board = backend_case["board"]
    item = board.create_item("Interview item", {"state": "New"})
    key = recovery_mod.thread_key("ana", item.id)
    recovery_mod.note_startup(db, now="2026-05-01T08:00:00Z")

    env = dict(os.environ)
    src_dir = str(Path(pam_pm.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_APPEND, db, key, "ana", item.id],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    killed = False
    deadline = time.time() + 30
    try:
        while time.time() < deadline:
            conv = recovery_mod.load_conversation(db, key)
            if conv is not None and len(conv["turns"]) >= 4:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert proc.poll() is None, "child exited before the kill"
        proc.kill()
        proc.wait(timeout=10)
        killed = True
    finally:
        if not killed and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode != 0

    before = recovery_mod.load_conversation(db, key)
    assert before is not None
    assert len(before["turns"]) >= 4
    assert before["turns"][0]["content"] == "turn-0"

    info = recovery_mod.note_startup(db, now="2026-05-01T09:00:00Z")
    assert info["crashed"] is True
    events = [entry["event"] for entry in recovery_mod.list_lifecycle(db)]
    assert "crash" in events
    assert "start" in events

    count = recovery_mod.append_turn(
        db, key, "ana", item.id, "assistant", "welcome-back"
    )
    assert count == len(before["turns"]) + 1
    after = recovery_mod.load_conversation(db, key)
    assert after is not None
    assert [t["content"] for t in after["turns"][:-1]] == [
        t["content"] for t in before["turns"]
    ]
    assert after["turns"][-1]["content"] == "welcome-back"


def test_no_double_dm(backend_case: Any) -> None:
    db = str(backend_case["db"])
    chat = InMemoryChat()
    first = outbox_mod.make_key("dm", "ana", "morning-1")
    outbox_mod.enqueue(
        db,
        kind=outbox_mod.KIND_SLACK,
        key=first,
        payload={"user_id": "ana", "text": "morning check-in"},
    )
    assert outbox_mod.flush(db, chat=chat) == {"sent": 1, "skipped": 0, "failed": 0}
    assert outbox_mod.flush(db, chat=chat)["sent"] == 0
    second = outbox_mod.make_key("dm", "ana", "morning-2")
    outbox_mod.enqueue(
        db,
        kind=outbox_mod.KIND_SLACK,
        key=second,
        payload={"user_id": "ana", "text": "follow-up"},
    )
    assert outbox_mod.flush(db, chat=chat)["sent"] == 1
    assert outbox_mod.flush(db, chat=chat)["sent"] == 0
    assert [dm["text"] for dm in chat.dms] == ["morning check-in", "follow-up"]


def test_no_double_board_write(backend_case: Any) -> None:
    db = str(backend_case["db"])
    board = backend_case["board"]
    item = board.create_item("Write-once item", {"state": "New"})
    key = outbox_mod.make_key("board", backend_case["kind"], item.id, "c1")
    outbox_mod.enqueue(
        db,
        kind=outbox_mod.KIND_BOARD,
        key=key,
        payload={
            "operation": "comment",
            "item_id": item.id,
            "text": "status note",
            "author": "pam",
            "evidence_quote": "done yesterday",
        },
    )
    assert outbox_mod.flush(db, board=board) == {
        "sent": 1,
        "skipped": 0,
        "failed": 0,
    }
    fresh = backend_case["reopen"]()
    try:
        result = outbox_mod.flush(db, board=fresh)
        assert result["sent"] == 0
        comments = fresh.list_comments(item.id)
    finally:
        fresh.close()
    assert [comment.text for comment in comments] == ["status note"]
    applied = [w for w in store_mod.list_writes(db) if w.status == "applied"]
    assert len(applied) == 1


def test_downtime_replies_via_catch_up(backend_case: Any) -> None:
    db = str(backend_case["db"])
    board = backend_case["board"]
    item = board.create_item("Check-in item", {"state": "New"})
    key = recovery_mod.thread_key("ana", item.id)
    recovery_mod.save_conversation(
        db, key, "ana", item.id, [{"role": "assistant", "content": "morning q"}]
    )
    poller = ADOPoller(board, db)
    assert [entry.id for entry in poller.poll_once()] == [item.id]
    watermark = poller.get_watermark()
    assert watermark
    recovery_mod.note_startup(db, now="2026-05-01T08:00:00Z")
    recovery_mod.note_stop(db, detail="test stop", now="2026-05-01T08:30:00Z")

    downtime = board.create_item("Downtime item", {"state": "New"})
    _ensure_newer(board, downtime.id, watermark)
    chat = InMemoryChat()
    chat.script_replies("ana", ["still blocked on keys", "eta friday"])

    result = recovery_mod.catch_up(
        db, poller=poller, chat=chat, now="2026-05-01T09:00:00Z"
    )
    assert result["replies"] == 2
    assert result["board_changes"] == 1
    assert result["banner"].startswith("Pam was offline from 2026-05-01T08:30:00Z")
    assert "Caught up: 2 replies" in result["banner"]
    assert recovery_mod.get_offline_banner(db) == result["banner"]

    conv = recovery_mod.load_conversation(db, key)
    assert conv is not None
    assert [t["content"] for t in conv["turns"][-2:]] == [
        "still blocked on keys",
        "eta friday",
    ]
    assert conv["last_seen"] != ""
    assert poller.poll_once() == []


def test_misfire_within_outside_grace_coalescing(backend_case: Any) -> None:
    db = str(backend_case["db"])
    recovery_mod.upsert_job(db, "morning", rule="morning", misfire_grace_hours=3.0)
    recovery_mod.upsert_job(db, "weekly", rule="weekly", misfire_grace_hours=3.0)
    recovery_mod.upsert_job(db, "tight", rule="tight", misfire_grace_hours=0.25)
    now = "2026-05-01T12:00:00Z"
    missed = [
        {"job_id": "morning", "due_at": "2026-05-01T11:30:00Z"},
        {"job_id": "morning", "due_at": "2026-05-01T09:30:00Z"},
        {"job_id": "morning", "due_at": "2026-05-01T06:00:00Z"},
        {"job_id": "weekly", "due_at": "2026-04-30T06:00:00Z"},
        {"job_id": "tight", "due_at": "2026-05-01T11:00:00Z"},
    ]
    calls: list[tuple[str, str]] = []
    result = recovery_mod.apply_misfires(
        db, missed, now, lambda j, d: calls.append((j, d))
    )
    assert result == {"ran": ["morning"], "skipped": 4}
    assert calls == [("morning", "2026-05-01T11:30:00Z")]

    morning = {
        (run["due_at"], run["status"])
        for run in recovery_mod.list_runs(db, "morning")
    }
    assert morning == {
        ("2026-05-01T11:30:00Z", "ran"),
        ("2026-05-01T09:30:00Z", "coalesced"),
        ("2026-05-01T06:00:00Z", "coalesced"),
    }
    assert [r["status"] for r in recovery_mod.list_runs(db, "weekly")] == ["skipped"]
    assert [r["status"] for r in recovery_mod.list_runs(db, "tight")] == ["skipped"]
    assert recovery_mod.get_job(db, "morning")["last_run"] == now

    assert recovery_mod.decide_misfire("2026-05-01T09:00:00Z", now, 3.0) == "run"
    assert recovery_mod.decide_misfire("2026-05-01T08:59:00Z", now, 3.0) == "skip"
