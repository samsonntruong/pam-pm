"""Teams replies resume the interview, mirroring the Slack path."""

from __future__ import annotations

import json
from typing import Any

import httpx

from pam_pm.adapters.chat.teams import TeamsAdapter
from pam_pm.agents import interviewer as interviewer_mod
from pam_pm.core import router as router_mod
from pam_pm.models.client import ChatResult


class FakeTeamsChat:
    """Chat fake resolving one Teams user GUID to a roster name."""

    def __init__(self) -> None:
        self.dms: list[dict[str, str]] = []
        self.names = {"teams-guid-1": "Maya Pam"}

    def display_name_for_user(self, user_id: str) -> str | None:
        return self.names.get(str(user_id))

    def send_dm(self, person: str, text: str) -> str:
        self.dms.append({"person": str(person), "text": str(text)})
        return f"mid-{len(self.dms)}"


class FakeModelClient:
    def __init__(self, script: list[ChatResult]) -> None:
        self._script = list(script)
        self.calls = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Any = None,
        max_tokens: Any = None,
    ) -> ChatResult:
        _ = (messages, tools, max_tokens)
        self.calls += 1
        return self._script[min(self.calls - 1, len(self._script) - 1)]


def _seed(db: Any) -> None:
    interviewer_mod._save_interview_state(
        db,
        "Maya Pam",
        "item-1",
        [{"role": "assistant", "content": "What is the status?"}],
        "",
    )


def _model() -> FakeModelClient:
    return FakeModelClient([ChatResult(text="Can you tell me more?", tool_calls=[])])


def _clear_name_cache() -> None:
    try:
        interviewer_mod._DISPLAY_NAME_CACHE.clear()
    except Exception:  # noqa: BLE001, S110
        pass


def test_display_name_for_user_resolves_and_caches() -> None:
    calls = {"members": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "fake-graph-token"})
        if path.endswith("/chats/chat-1/members"):
            calls["members"] += 1
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "mship-bot", "userId": "bot-id", "displayName": "Pam Bot"},
                        {"id": "mship-maya", "userId": "teams-guid-1", "displayName": "Maya Pam"},
                    ]
                },
            )
        return httpx.Response(404, json={"message": "unknown fake route"})

    adapter = TeamsAdapter(
        app_id="fake-id",
        app_password="fake-pw",
        tenant_id="fake-tenant",
        transport=httpx.MockTransport(handle),
        chats=[{"id": "chat-1", "chatType": "oneOnOne"}],
    )
    try:
        assert adapter.display_name_for_user("teams-guid-1") == "Maya Pam"
        assert calls["members"] == 1
        assert adapter.display_name_for_user("teams-guid-1") == "Maya Pam"
        assert calls["members"] == 1
        assert adapter.display_name_for_user("unknown-guid") is None
    finally:
        adapter.close()


def test_teams_text_resumes_open_interview(tmp_path: Any) -> None:
    _clear_name_cache()
    db = str(tmp_path / "teams-resume.db")
    _seed(db)
    chat = FakeTeamsChat()
    event = {
        "type": "teams_message",
        "text": "almost done, working on it, soon",
        "user_id": "teams-guid-1",
        "channel": "chat-1",
        "message_id": "m-1",
    }
    outcome = router_mod.handle_teams_event(
        event, chat=chat, board=None, db=db, model_client=_model()
    )
    assert outcome["kind"] == "interview_reply"
    assert outcome["replied"] is True
    assert outcome["resume"]["kind"] == "reply"
    assert chat.dms, "expected a pushback reply"
    pushed = chat.dms[-1]["text"].lower()
    assert "remain" in pushed and "eta" in pushed


def test_teams_text_without_db_stays_bus_only(tmp_path: Any) -> None:
    _clear_name_cache()
    db = str(tmp_path / "teams-nodb.db")
    _seed(db)
    chat = FakeTeamsChat()
    event = {
        "type": "teams_message",
        "text": "almost done, working on it, soon",
        "user_id": "teams-guid-1",
        "channel": "chat-1",
        "message_id": "m-2",
    }
    no_db = router_mod.handle_teams_event(
        event, chat=chat, board=None, model_client=_model()
    )
    assert no_db["kind"] == "message"
    assert no_db["replied"] is False
    assert chat.dms == []
    no_model = router_mod.handle_teams_event(event, chat=chat, board=None, db=db)
    assert no_model["kind"] == "message"
    assert no_model["replied"] is False
    assert chat.dms == []


def test_member_lookup_never_logs_token(caplog: Any) -> None:
    token = "fake-graph-token-abc123"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": token})
        if request.url.path.endswith("/chats/chat-1/members"):
            body = json.dumps({"value": []})
            assert token not in body
            return httpx.Response(200, json={"value": []})
        return httpx.Response(404, json={"message": "unknown"})

    adapter = TeamsAdapter(
        app_id="fake-id",
        app_password="fake-pw",
        tenant_id="fake-tenant",
        transport=httpx.MockTransport(handle),
        chats=[{"id": "chat-1", "chatType": "oneOnOne"}],
    )
    try:
        with caplog.at_level("INFO"):
            assert adapter.display_name_for_user("missing") is None
    finally:
        adapter.close()
    for record in caplog.records:
        assert token not in str(record.message)
        assert token not in str(record.args)
