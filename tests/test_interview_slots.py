"""Regression: deterministic slot finish, follow-up cap, no run-on seams."""
import re
import sqlite3

from pam_pm.agents.interviewer import (
    CHECKIN_SLOTS,
    _one_question,
    filled_slots,
    join_ack_question,
    missing_slots,
    resume_interview,
)
from pam_pm.core import recovery as rec

RUNON = re.compile(r"[a-z][A-Z]")
PUNCT_GLUE = re.compile(r"[.!?][^\s]")
LOWER_BEFORE_WHAT = re.compile(r"[a-z] What remains")


class FakeChat:
    def __init__(self):
        self.sent = []

    def send_dm(self, user, text):
        self.sent.append(text)
        return f"m{len(self.sent)}"

    def read_replies(self, user, *a):
        return []


class FakeBoard:
    def __init__(self):
        self.updates = []
        self.comments = []

    def update_item(self, item_id, fields):
        self.updates.append((item_id, dict(fields)))
        return {"id": item_id}

    def add_comment(self, item_id, text, author="Pam"):
        self.comments.append((item_id, text, author))
        return "c1"


class ScriptModel:
    """Model that never finishes: proves the finish is deterministic."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0

    def chat(self, messages, tools=None, *a, **k):
        self.calls += 1
        idx = min(self.calls - 1, len(self.texts) - 1)
        return {"text": self.texts[idx], "tool_calls": []}


def _open_db(person="maya", item="I5"):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    init = getattr(rec, "init_db", getattr(rec, "init", None))
    if callable(init):
        try:
            init(db)
        except TypeError:
            pass
    rec.save_conversation(
        db,
        rec.thread_key(person, item),
        person,
        item,
        [{"role": "assistant", "content": f"What is the status of {item}?"}],
        "",
        "open",
    )
    return db


def _assert_spaced(text):
    assert not RUNON.search(text), f"lower-to-upper run-on in: {text!r}"
    assert not PUNCT_GLUE.search(text), f"punctuation glue in: {text!r}"
    assert not LOWER_BEFORE_WHAT.search(text), f"ack/question seam in: {text!r}"


def test_three_turn_slot_fill_finishes_without_fourth_question():
    db = _open_db()
    chat = FakeChat()
    board = FakeBoard()
    thanks = "Thanks Maya. Recorded: retry tests, done by noon, no blockers."
    model = ScriptModel(
        [
            "What remains and what is your ETA?",
            "What remains and what is your ETA?",
            thanks,
        ]
    )
    first = resume_interview(
        "maya", "retry tests", chat, board, model, db=db, message_id="2"
    )
    assert first["kind"] == "reply"
    second = resume_interview(
        "maya", "done by noon", chat, board, model, db=db, message_id="3"
    )
    assert second["kind"] == "reply"
    third = resume_interview(
        "maya", "No blockers", chat, board, model, db=db, message_id="4"
    )
    assert third["kind"] == "finished"
    assert third.get("deterministic") is True
    assert len(chat.sent) == 3
    assert chat.sent[-1] == thanks
    assert "?" not in chat.sent[-1]
    assert model.calls == 3
    assert len(board.updates) == 1
    assert board.comments
    result = third["result"]
    assert "retry tests" in result.status
    assert result.blocker is None
    assert (
        missing_slots(
            [
                {"role": "user", "content": "retry tests"},
                {"role": "user", "content": "done by noon"},
                {"role": "user", "content": "No blockers"},
            ]
        )
        == []
    )
    assert set(filled_slots([{"role": "user", "content": "retry tests"}])) == {
        "remaining_work"
    }
    assert set(CHECKIN_SLOTS) == {"remaining_work", "eta", "blockers"}
    for text in chat.sent:
        _assert_spaced(text)


def test_cap_finishes_honestly_with_gaps():
    db = _open_db()
    chat = FakeChat()
    board = FakeBoard()
    model = ScriptModel(["What remains and what is your ETA?"])
    kinds = []
    out = {}
    answers = ["dunno", "still thinking", "not sure yet", "hmm"]
    for num, answer in enumerate(answers, start=2):
        out = resume_interview(
            "maya", answer, chat, board, model, db=db, message_id=str(num)
        )
        kinds.append(out["kind"])
    assert kinds == ["reply", "reply", "reply", "finished"]
    assert out.get("turn_cap") is True
    closing = chat.sent[-1]
    assert "Not reported: ETA, blockers." in closing
    assert ". Not reported" in closing
    assert len(chat.sent) == 4
    assert model.calls == 3
    assert any("Not reported: ETA, blockers." in text for _, text, _ in board.comments)
    for text in chat.sent:
        _assert_spaced(text)


def test_no_lowercase_runon_seams():
    joined = _one_question("wrapping up #5 with what you said")
    assert joined == (
        "wrapping up #5 with what you said. What remains and what is your ETA?"
    )
    assert join_ack_question("noted", "What remains?") == "noted. What remains?"
    db = _open_db()
    chat = FakeChat()
    board = FakeBoard()
    model = ScriptModel(["wrapping up 5 with what you said"])
    out = resume_interview("maya", "ok", chat, board, model, db=db, message_id="2")
    assert out["kind"] == "reply"
    assert ". What remains" in chat.sent[0]
    for text in chat.sent:
        _assert_spaced(text)
