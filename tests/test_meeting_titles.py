"""Meeting action titles are short imperatives."""

from __future__ import annotations

import datetime as _dt

from pam_pm.agents import listen_pipeline as lp
from pam_pm.agents import notes as notes_mod


def _coming_weekday(name: str) -> _dt.date:
    today = _dt.date.today()
    target = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].index(name.lower())
    delta = (target - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return today + _dt.timedelta(days=delta)


def test_example_sentence_title_owner_due_estimate_evidence():
    sentence = "Maya, can you add rate limiting to the login endpoint before Friday?"
    transcript = sentence + " About four hours."
    result = notes_mod.extract_notes(transcript)
    actions = result["action_items"]
    assert actions, "expected one action"
    action = actions[0]
    assert action["title"] == "Add rate limiting to the login endpoint"
    assert action["evidence_quote"] == sentence
    people = [{"name": "Maya Pam", "role": "backend", "skillsets": ["login", "rate limiting"]}]
    proposals = lp.propose_listen_tasks(transcript, result, people, [], "m1")
    assert proposals
    prop = proposals[0]
    assert prop["title"] == "Add rate limiting to the login endpoint"
    assert prop["owner"] in ("Maya", "Maya Pam")
    assert str(_coming_weekday("friday")) in str(prop["due_date"])
    assert float(prop["estimate"]) == 4.0
    assert prop["evidence_quote"] == sentence


def test_could_you_please_stripping():
    title = notes_mod.concise_title("Could you please update the runbook by Thursday?")
    assert title == "Update the runbook"


def test_no_deadline_keeps_due_empty():
    result = notes_mod.extract_notes("Maya will update the runbook.")
    assert result["action_items"]
    assert result["action_items"][0]["due_date"] in ("", None)


def test_half_an_hour_to_point_five():
    assert lp.stated_hours("it takes half an hour") == 0.5
