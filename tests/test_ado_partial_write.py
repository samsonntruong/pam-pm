"""ADO partial writes plus honest record DMs on total failure."""

from __future__ import annotations

import json
import os
from typing import Any, ClassVar

import httpx
import pytest

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.azure_devops import ADOAdapter, ADOError
from pam_pm.adapters.pm.base import Comment, WorkItem
from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.agents.interviewer import Interviewer, resume_interview
from pam_pm.core import recovery as recovery_mod
from pam_pm.models.client import ChatResult, ChatToolCall


def _env() -> None:
    os.environ["ADO_ORG_URL"] = "https://dev.azure.com/fake"
    os.environ["ADO_PROJECT"] = "proj"
    os.environ["ADO_TEAM"] = "team"
    os.environ["ADO_PAT"] = "test-pat-value"


class _StrictADO:
    """Fake ADO that rejects unknown fields with 400 like real ADO."""

    KNOWN: ClassVar = {
        "System.Title",
        "System.State",
        "System.Description",
        "System.Tags",
        "System.AssignedTo",
        "Microsoft.VSTS.Scheduling.RemainingWork",
        "Microsoft.VSTS.Scheduling.OriginalEstimate",
        "Microsoft.VSTS.Scheduling.DueDate",
        "System.IterationPath",
    }

    def __init__(self) -> None:
        self.fields: dict[str, Any] = {
            "System.Title": "t",
            "System.State": "New",
            "System.ChangedDate": "2026-01-01T00:00:00Z",
        }
        self.add_rejected_paths: set[str] = set()
        self.comments: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/projects/proj"):
            return httpx.Response(
                200,
                json={
                    "capabilities": {
                        "processTemplate": {"templateName": "Agile"}
                    }
                },
            )
        if "/comments" in path and request.method == "POST":
            try:
                body = json.loads(request.content.decode("utf-8"))
            except ValueError:
                body = {}
            text = str(body.get("text", "")) if isinstance(body, dict) else ""
            self.comments.append({"text": text})
            return httpx.Response(
                200,
                json={
                    "id": len(self.comments),
                    "text": text,
                    "createdBy": {"displayName": "Pam"},
                    "createdDate": "2026-01-01T00:00:00Z",
                },
            )
        if request.method == "GET" and "/_apis/wit/workitems/5" in path:
            return httpx.Response(
                200, json={"id": 5, "fields": dict(self.fields)}
            )
        if request.method == "PATCH" and "/_apis/wit/workitems/5" in path:
            return self._patch(request)
        return httpx.Response(404, json={"message": "unknown fake route"})

    def _patch(self, request: httpx.Request) -> httpx.Response:
        try:
            ops = json.loads(request.content.decode("utf-8")) if request.content else []
        except ValueError:
            ops = []
        if not isinstance(ops, list):
            ops = []
        if len(ops) > 1:
            return httpx.Response(
                400, json={"message": "TF401347: one op in the batch is invalid."}
            )
        for op in ops:
            if not isinstance(op, dict):
                continue
            field = str(op.get("path", "")).removeprefix("/fields/")
            if field not in self.KNOWN:
                return httpx.Response(
                    400,
                    json={"message": f"TF401347: field '{field}' does not exist."},
                )
            if op.get("op") == "add" and str(op.get("path")) in self.add_rejected_paths:
                self.add_rejected_paths.discard(str(op.get("path")))
                return httpx.Response(
                    400,
                    json={"message": "TF400013: add cannot update an existing value."},
                )
        for op in ops:
            if isinstance(op, dict) and str(op.get("path", "")).startswith("/fields/"):
                name = str(op["path"]).removeprefix("/fields/")
                self.fields[name] = op.get("value")
        return httpx.Response(200, json={"id": 5, "fields": dict(self.fields)})


def _make_adapter(fake: _StrictADO) -> ADOAdapter:
    _env()
    return ADOAdapter(transport=httpx.MockTransport(fake.handle), process="Agile")


def test_partial_field_rejection_keeps_good_fields() -> None:
    fake = _StrictADO()
    adapter = _make_adapter(fake)
    try:
        item = adapter.update_item(
            "5", {"remaining": 2.0, "evidence_quote": "2h left"}
        )
        assert "remaining" in item.fields.get("applied_fields", [])
        rejected = item.fields.get("rejected_fields", {})
        assert "evidence_quote" in rejected
        assert "does not exist" in rejected["evidence_quote"]
        assert fake.fields.get("Microsoft.VSTS.Scheduling.RemainingWork") == 2.0
        assert "evidence_quote" not in fake.fields
    finally:
        adapter.close()


def test_add_fallback_to_replace_keeps_field() -> None:
    fake = _StrictADO()
    fake.add_rejected_paths.add(
        "/fields/Microsoft.VSTS.Scheduling.RemainingWork"
    )
    adapter = _make_adapter(fake)
    try:
        item = adapter.update_item("5", {"remaining": 3.0})
        assert item.fields.get("applied_fields") == ["remaining"]
        assert item.fields.get("rejected_fields") == {}
        assert fake.fields.get("Microsoft.VSTS.Scheduling.RemainingWork") == 3.0
    finally:
        adapter.close()


def test_total_rejection_raises_with_reasons() -> None:
    fake = _StrictADO()
    adapter = _make_adapter(fake)
    try:
        with pytest.raises(ADOError) as excinfo:
            adapter.update_item("5", {"evidence_quote": "x", "eta": "soon"})
        assert excinfo.value.status_code == 400
        assert excinfo.value.applied == []
        assert set(excinfo.value.rejected) == {"evidence_quote", "eta"}
        assert "does not exist" in excinfo.value.rejected["eta"]
    finally:
        adapter.close()


class _FailingBoard:
    """Board where every write is rejected, like live ADO 400s."""

    def update_item(self, item_id: str, fields: dict[str, Any]) -> Any:
        err = ADOError(
            "PATCH status=400: TF401347: one op in the batch is invalid.",
            status_code=400,
        )
        err.applied = []
        err.rejected = {str(k): "TF401347: invalid" for k in fields}
        raise err

    def add_comment(self, item_id: str, text: str, author: str = "") -> Any:
        raise ADOError("POST status=400: comment rejected", status_code=400)


class _PartialBoard:
    """Board where fields partially stick and the comment posts."""

    def __init__(self) -> None:
        self.stored: dict[str, Any] = {}
        self.comments: list[Comment] = []

    def update_item(self, item_id: str, fields: dict[str, Any]) -> WorkItem:
        good = {k: v for k, v in fields.items() if k in ("state", "remaining")}
        self.stored.update(good)
        return WorkItem(
            id=str(item_id),
            title="",
            state="",
            changed="",
            fields={
                "applied_fields": sorted(good),
                "rejected_fields": {
                    "eta": "unknown field",
                    "evidence_quote": "unknown field",
                },
            },
        )

    def add_comment(self, item_id: str, text: str, author: str = "") -> Comment:
        comment = Comment(
            id=str(len(self.comments) + 1),
            item_id=str(item_id),
            text=text,
            author=author,
        )
        self.comments.append(comment)
        return comment


class _ScriptedModel:
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
        self.calls.append({"messages": messages})
        if len(self.calls) <= len(self._script):
            return self._script[len(self.calls) - 1]
        return self._script[-1]


def _finish_call(quote: str) -> ChatResult:
    return ChatResult(
        text="",
        tool_calls=[
            ChatToolCall(
                name="finish_interview",
                arguments=json.dumps(
                    {
                        "status": "In progress on Fix login",
                        "remaining": 2.0,
                        "eta": "2026-11-06",
                        "evidence_quote": quote,
                        "blocker": None,
                        "blocker_owner": None,
                        "confidence": 0.8,
                    }
                ),
            )
        ],
    )


def _seed_open(tmp_path: Any) -> tuple[str, str]:
    db = str(tmp_path / "partial.db")
    board = SQLiteBoard()
    try:
        item = board.create_item("Fix login", {"state": "Doing"})
        chat = InMemoryChat()
        model = _ScriptedModel(
            [ChatResult(text="What is the status of Fix login?", tool_calls=[])]
        )
        out = Interviewer().run("U1", [item], chat, board, model, db=db)
        assert out == {}
        return db, item.id
    finally:
        board.close()


def test_total_failure_dm_is_honest(tmp_path: Any) -> None:
    db, _ = _seed_open(tmp_path)
    chat = InMemoryChat()
    specific = "2 hours left, done Friday, ETA 2026-11-06"
    model = _ScriptedModel([_finish_call(specific)])
    outcome = resume_interview(
        "U1", specific, chat, _FailingBoard(), model, db=db, message_id="1.1"
    )
    assert outcome["kind"] == "write_failed"
    assert outcome["kind"] != "finished"
    assert chat.dms, "failure must still send one honest DM"
    text = chat.dms[-1]["text"]
    assert "could not write this to the board" in text.lower()
    assert "recorded" not in text.lower()
    assert recovery_mod.list_open_conversations(db), "state must stay open"


def test_partial_success_still_records(tmp_path: Any) -> None:
    db, _ = _seed_open(tmp_path)
    chat = InMemoryChat()
    board = _PartialBoard()
    specific = "2 hours left, done Friday, ETA 2026-11-06"
    model = _ScriptedModel([_finish_call(specific)])
    outcome = resume_interview(
        "U1", specific, chat, board, model, db=db, message_id="2.2"
    )
    assert outcome["kind"] == "finished"
    assert "recorded" in chat.dms[-1]["text"].lower()
    assert board.stored.get("remaining") == 2.0
    assert len(board.comments) == 1
    assert recovery_mod.list_open_conversations(db) == []
