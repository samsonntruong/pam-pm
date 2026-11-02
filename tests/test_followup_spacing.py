"""Regression: follow-ups never glue ack and question without separator."""
import re

from pam_pm.agents.interviewer import (
    join_ack_question,
    normalize_followup_spacing,
    resume_interview,
)
from pam_pm.rules.templates.follow_up import on_item_done

RUNON = re.compile(r"[a-z][A-Z]")
PUNCT_GLUE = re.compile(r"[.!?][^\s]")

class FakeChat:
    def __init__(self, replies=None):
        self.sent = []
        self.replies = list(replies or [])
    def send_dm(self, user, text):
        self.sent.append(text)
        return f"m{len(self.sent)}"
    def read_replies(self, user, *a):
        return []

class FakeBoard:
    def __init__(self, items=None):
        self.items = items or []
    def get_item(self, item_id):
        for it in self.items:
            if it.get("id") == item_id:
                return dict(it)
        raise KeyError(item_id)
    def list_links(self, item_id):
        return []
    def update_item(self, item_id, fields):
        return {"id": item_id}

class FakeModel:
    def __init__(self, text):
        self.text = text
    def chat(self, messages, tools=None, *a, **k):
        return {"text": self.text, "tool_calls": []}

def _assert_spaced(text):
    assert not RUNON.search(text), f"lower-to-upper run-on in: {text!r}"
    assert not PUNCT_GLUE.search(text), f"punctuation glue in: {text!r}"

def test_normalize_leo_dm_examples():
    a = normalize_followup_spacing("Got it. about an hour left. I'll check the expected finish time.Got it. when do you expect to finish?")
    _assert_spaced(a)
    assert "time. Got" in a
    b = normalize_followup_spacing("Got the timing. quick blocker check and I'll wrap up.Anything blocking you?")
    _assert_spaced(b)
    assert "up. Anything" in b

def test_join_ack_question_single_space():
    joined = join_ack_question("Got it. about an hour left.", "When do you expect to finish?")
    assert "  " not in joined
    _assert_spaced(joined)
    assert joined == "Got it. about an hour left. When do you expect to finish?"

def test_resume_interview_no_runon_with_fakes(tmp_path):
    import sqlite3

    from pam_pm.core import recovery as rec
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    init = getattr(rec, 'init_db', getattr(rec, 'init', None))
    if callable(init):
        try:
            init(db)
        except TypeError:
            pass
    rec.save_conversation(db, rec.thread_key("leo", "I1"), "leo", "I1", [{"role": "assistant", "content": "What is the status of I1?"}], "", "open")
    chat = FakeChat()
    board = FakeBoard()
    model = FakeModel("Got the timing. quick blocker check and I'll wrap up.Anything blocking you?")
    out = resume_interview("leo", "almost done", chat, board, model, db=db, message_id="2")
    assert out["kind"] in ("reply", "finished")
    for text in chat.sent:
        _assert_spaced(text)

def test_follow_up_template_spaced():
    class B2(FakeBoard):
        def get_item(self, item_id):
            if item_id == "D1":
                return {"id": "D1", "title": "Done work", "state": "Done"}
            return {"id": "S1", "title": "Next work", "state": "To Do", "assigned_to": "leo"}
        def list_links(self, item_id):
            return [{"source_id": "D1", "target_id": "S1", "rel": "System.LinkTypes.Dependency-Forward"}]
    chat = FakeChat()
    sent = on_item_done("D1", chat, B2())
    assert sent
    for text in chat.sent:
        _assert_spaced(text)
