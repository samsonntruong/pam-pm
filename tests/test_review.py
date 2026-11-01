"""M3b review retro tests: facts versus interpretation, no invented status."""

from pam_pm.agents.lead import REVIEW_MAX_TOKENS, synthesize_review
from pam_pm.cli import main as cli_main


def _fake_inputs():
    items = [
        {"id": "101", "title": "Login form", "state": "To Do"},
        {"id": "102", "title": "API keys", "state": "Doing"},
    ]
    interviews = {
        "101": {
            "status": "To Do",
            "evidence_quote": "Login form is done, only tests remain.",
            "confidence": 0.9,
        },
        "102": {
            "status": "Doing",
            "evidence_quote": "Waiting on API keys from Sam.",
            "confidence": 0.2,
        },
    }
    events = [
        {"operation": "comment", "item_id": "102", "detail": "Note blocker on 102."}
    ]
    return items, interviews, events


def test_review_separates_facts_and_flags_low_confidence():
    items, interviews, events = _fake_inputs()
    result = synthesize_review(items, interviews, events)
    assert [f["item_id"] for f in result["facts"]] == ["101"]
    assert [i["item_id"] for i in result["interpretations"]] == ["102"]
    assert result["low_confidence"] == ["102"]
    markdown = result["review_markdown"]
    assert "## Facts" in markdown
    assert "## Interpretations" in markdown
    assert "102" in markdown


def test_review_retro_actions_tagged_and_grounded():
    items, interviews, events = _fake_inputs()
    result = synthesize_review(items, interviews, events)
    actions = result["retro_actions"]
    assert actions, "low confidence plus blocker evidence must yield actions"
    for action in actions:
        assert action["tags"] == ["retro-action"]
        assert action["title"].strip()
    assert any("102" in a["title"] for a in actions)


def test_review_invents_no_status():
    items, interviews, events = _fake_inputs()
    result = synthesize_review(items, interviews, events)
    markdown = result["review_markdown"]
    assert "Deployed to production" not in markdown
    assert "Shipped" not in markdown
    for record in result["facts"] + result["interpretations"]:
        assert record["status"] in {"To Do", "Doing"}


def test_review_uses_large_max_tokens():
    assert REVIEW_MAX_TOKENS >= 6000
    items, interviews, events = _fake_inputs()
    result = synthesize_review(items, interviews, events)
    assert result["max_tokens"] >= 6000


def test_review_live_model_path_uses_large_tokens():
    items, interviews, events = _fake_inputs()

    class StubClient:
        def __init__(self):
            self.seen = {}

        def chat(self, messages, tools=None, max_tokens=None):
            self.seen = {
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
            }
            return {"text": "# Live review\n\nDone."}

    stub = StubClient()
    result = synthesize_review(items, interviews, events, model_client=stub)
    assert stub.seen["max_tokens"] >= 6000
    assert stub.seen["messages"] and stub.seen["tools"] is not None
    assert result["model_path"] == "live"
    assert "Live review" in result["review_markdown"]


def test_cli_review_prints_markdown(capsys):
    cli_main(["review"])
    out = capsys.readouterr().out
    assert "# Sprint review" in out
    assert "## Facts" in out
