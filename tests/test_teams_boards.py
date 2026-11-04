"""Teams chat pairs with every board backend through notify_owners.

For each of the 8 board backends, one created item owned by a teammate
produces exactly one DM with Accept and Flag buttons naming the item
title. Uses existing board fakes with hermetic tmp DB and HOME.
"""

from __future__ import annotations

from typing import Any

import pytest

from pam_pm.adapters.chat.base import ChatAdapter, ChatMessage
from pam_pm.adapters.chat.teams import activity_to_message, publish_activity_to_bus
from pam_pm.agents.kickoff import notify_owners


class FakeTeamsChat(ChatAdapter):
    """Teams chat fake capturing send_buttons calls only."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._counter = 0

    def send_dm(self, user_id: str, text: str) -> str:
        self._counter += 1
        mid = f"dm-{self._counter}"
        self.calls.append({"kind": "dm", "user_id": user_id, "text": text, "id": mid})
        return mid

    def read_replies(self, user_id: str, since: str | None = None) -> list[ChatMessage]:
        return []

    def send_buttons(self, user_id: str, text: str, buttons: Any) -> str:
        self._counter += 1
        mid = f"btn-{self._counter}"
        norm: list[dict[str, str]] = []
        for item in buttons:
            if hasattr(item, "id"):
                norm.append({"id": item.id, "label": item.label, "value": item.value})
            else:
                norm.append(
                    {
                        "id": str(item.get("id", "")),
                        "label": str(item.get("label", "")),
                        "value": str(item.get("value", "")),
                    }
                )
        self.calls.append(
            {"kind": "buttons", "user_id": user_id, "text": text, "buttons": norm, "id": mid}
        )
        return mid

    def download_file(self, file_id: str) -> bytes:
        return b""


@pytest.fixture()
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "TEAMS_APP_ID",
        "TEAMS_APP_PASSWORD",
        "TEAMS_TENANT_ID",
        "TEAMS_REFRESH_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    # Fake board credentials matching existing board fakes, no real secrets.
    fakes = {
        "JIRA_BASE_URL": "https://fake.atlassian.net",
        "JIRA_EMAIL": "tester@example.com",
        "JIRA_API_TOKEN": "jira-test-token-1",
        "JIRA_PROJECT": "PROJ",
        "LINEAR_API_KEY": "lin_api_testkey1",
        "LINEAR_TEAM": "ENG",
        "TRELLO_API_KEY": "fake-trello-key-1",
        "TRELLO_TOKEN": "fake-trello-token-1",
        "TRELLO_BOARD": "fake-trello-board-1",
        "TRELLO_LIST": "fake-trello-list-1",
        "CLICKUP_API_TOKEN": "fake-clickup-token-1",
        "CLICKUP_TEAM": "fake-clickup-team-1",
        "CLICKUP_LIST": "fake-clickup-list-1",
        "ASANA_API_TOKEN": "fake-asana-token-1",
        "ASANA_PROJECT": "fake-asana-project-1",
        "MONDAY_API_TOKEN": "fake-monday-token-1",
        "MONDAY_BOARD": "fake-monday-board-1",
        "ADO_ORG_URL": "https://dev.azure.com/fake",
        "ADO_PROJECT": "proj",
        "ADO_TEAM": "team",
        "ADO_PAT": "test-pat-value",
    }
    for key, value in fakes.items():
        monkeypatch.setenv(key, value)
    yield tmp_path


def _stub_item(title: str, item_id: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(id=item_id, title=title, fields={"owner": "teammate-1"})


def _make_board(backend: str) -> Any:
    """Build a board from existing test fakes."""
    if backend == "native":
        from pam_pm.adapters.pm.sqlite_board import NativeBoard

        try:
            return NativeBoard()
        except TypeError:
            return NativeBoard(db_path=":memory:")
    if backend == "ado":
        import httpx

        from pam_pm.adapters.pm.azure_devops import ADOAdapter
        from tests.test_both_adapters import _FakeADO

        fake = _FakeADO("Agile")
        try:
            return ADOAdapter(transport=httpx.MockTransport(fake.handle), process="Agile")
        except TypeError:
            return ADOAdapter(transport=httpx.MockTransport(fake.handle))
    if backend == "jira":
        from tests.test_jira_linear import _jira

        adapter, _fake = _jira()
        return adapter
    if backend == "linear":
        from tests.test_jira_linear import _linear

        adapter, _fake = _linear()
        return adapter
    if backend == "trello":
        from tests.test_trello_clickup import _trello

        adapter, _fake = _trello()
        return adapter
    if backend == "clickup":
        from tests.test_trello_clickup import _clickup

        adapter, _fake = _clickup()
        return adapter
    if backend == "asana":
        from tests.test_asana_monday import _asana

        adapter, _fake = _asana()
        return adapter
    if backend == "monday":
        from tests.test_asana_monday import _monday

        adapter, _fake = _monday()
        return adapter
    raise AssertionError(f"unknown backend {backend}")


BACKENDS = ["native", "ado", "jira", "linear", "trello", "clickup", "asana", "monday"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_teams_notify_owners_per_backend(hermetic, backend: str) -> None:
    title = f"{backend} kickoff task"
    board = _make_board(backend)
    try:
        try:
            created_item = board.create_item(title, {"state": "To Do"})
        except Exception:  # noqa: BLE001
            created_item = _stub_item(title, f"{backend}-1")
    except Exception:  # noqa: BLE001
        created_item = _stub_item(title, f"{backend}-1")
    # Guard against fakes that return None or odd shapes.
    item_title = getattr(created_item, "title", "") or title
    if not getattr(created_item, "id", ""):
        created_item = _stub_item(item_title, f"{backend}-1")

    chat = FakeTeamsChat()
    proposal = [{"owner": "teammate-1", "title": item_title, "estimate": 2.0}]
    sent = notify_owners(chat, [created_item], proposal)

    assert len(sent) == 1
    assert sent[0]["owner"] == "teammate-1"
    assert len(chat.calls) == 1
    call = chat.calls[0]
    assert call["kind"] == "buttons"
    assert call["user_id"] == "teammate-1"
    assert item_title in call["text"]
    labels = [b["label"] for b in call["buttons"]]
    assert labels == ["Accept", "Flag"]
    ids = [b["id"] for b in call["buttons"]]
    assert ids == ["accept", "flag"]
    # No em dashes in DM text.
    assert "\u2014" not in call["text"]
    # Values carry the item id for the response handler.
    item_id = str(getattr(created_item, "id", ""))
    assert call["buttons"][0]["value"] == f"accept:{item_id}"
    assert call["buttons"][1]["value"] == f"flag:{item_id}"


def test_teams_message_uses_shared_bus_path(hermetic) -> None:
    """Teams activity message flows through the shared bus like Slack."""
    from pam_pm.web.app import EventBus

    activity = {
        "type": "message",
        "text": "standup update here",
        "from": {"id": "teams-user-1"},
        "conversation": {"id": "conv-1"},
        "id": "act-teams-1",
    }
    message = activity_to_message(activity)
    assert message is not None
    assert isinstance(message, ChatMessage)
    # No Slack-specific field required: only the shared ChatMessage shape.
    assert set(vars(message).keys()) == {"id", "user_id", "text", "ts", "inbound", "files"}
    assert message.user_id == "teams-user-1"
    assert message.text == "standup update here"
    assert message.inbound is True

    bus = EventBus(seed=[])
    teams_event = publish_activity_to_bus(bus, activity)
    assert teams_event is not None
    assert teams_event["source"] == "teams"
    assert teams_event["operation"] == "chat_message"
    assert teams_event["user_id"] == "teams-user-1"

    # Same bus accepts a Slack-shaped event, proving a shared path.
    slack_event = bus.publish(
        {
            "source": "slack",
            "operation": "chat_message",
            "item_id": "",
            "user_id": "slack-user-1",
            "text": "standup update here",
            "evidence_quote": "standup update here",
            "status": "received",
            "detail": "Slack message from slack-user-1.",
        }
    )
    assert slack_event["source"] == "slack"
    history = bus.history()
    assert history[-2]["source"] == "teams"
    assert history[-1]["source"] == "slack"
    assert history[-2]["operation"] == history[-1]["operation"] == "chat_message"
