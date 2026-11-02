"""Resume finds roster-name state when inbound Slack events carry a user ID."""

from __future__ import annotations

from typing import Any

from pam_pm.agents import interviewer as interviewer_mod
from pam_pm.core import router as router_mod
from pam_pm.models.client import ChatResult


class FakeChat:
    """Chat fake resolving one Slack user ID to a roster display name."""

    def __init__(self) -> None:
        self.dms: list[dict[str, str]] = []
        self.names = {"U0CD123": "Maya Pam"}

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


def test_router_event_with_user_id_resumes_name_stored_state(tmp_path: Any) -> None:
    db = str(tmp_path / "identity.db")
    _seed(db)
    chat = FakeChat()
    event = {
        "type": "message",
        "user": "U0CD123",
        "text": "almost done, working on it, soon",
        "channel": "D123",
        "ts": "111.222",
    }
    outcome = router_mod.handle_slack_event(
        event, chat=chat, board=None, db=db, model_client=_model()
    )
    assert outcome["kind"] == "interview_reply"
    assert outcome["replied"] is True
    assert outcome["resume"]["kind"] == "reply"
    assert chat.dms, "expected a pushback reply"
    pushed = chat.dms[-1]["text"].lower()
    assert "remain" in pushed and "eta" in pushed


def test_roster_name_and_raw_id_find_same_state(tmp_path: Any) -> None:
    db = str(tmp_path / "identity.db")
    _seed(db)
    chat = FakeChat()
    by_name = interviewer_mod.resume_interview(
        "Maya Pam", "working on it", chat, None, _model(), db=db
    )
    assert by_name["kind"] == "reply"
    assert by_name["replied"] is True
    by_id = interviewer_mod.resume_interview(
        "U0CD123", "almost done, soon", chat, None, _model(), db=db
    )
    assert by_id["kind"] == "reply"
    assert by_id["replied"] is True
    pushed = chat.dms[-1]["text"].lower()
    assert "remain" in pushed and "eta" in pushed
