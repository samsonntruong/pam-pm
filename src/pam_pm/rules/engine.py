"""Rules engine: triggers plus actions per project."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

TRIGGER_SCHEDULE = "schedule"
TRIGGER_ADO_CHANGE = "ado_change"
TRIGGER_SLACK_BLOCKER = "slack_blocker"
TRIGGER_SLACK_MESSAGE = "slack_message"

KNOWN_TRIGGERS = (
    TRIGGER_SCHEDULE,
    TRIGGER_ADO_CHANGE,
    TRIGGER_SLACK_BLOCKER,
    TRIGGER_SLACK_MESSAGE,
)

BLOCKER_KEYWORDS = (
    "blocked",
    "blocker",
    "stuck",
    "waiting on",
    "wait on",
    "dependency",
    "cannot proceed",
    "cant proceed",
)

Action = Callable[..., Any]


def is_blocker_message(text: str) -> bool:
    """Return True when chat text reads like a blocker report."""
    lowered = (text or "").lower()
    return any(mark in lowered for mark in BLOCKER_KEYWORDS)


def event_trigger(event: dict[str, Any]) -> str:
    raw = event.get("trigger", event.get("type", ""))
    return str(raw or "").strip()


def event_id(event: dict[str, Any]) -> str | None:
    for key in ("id", "event_id", "delivery_id"):
        value = event.get(key)
        if value:
            return str(value)
    return None


def event_project(event: dict[str, Any]) -> str:
    for key in ("project", "project_id", "project_name"):
        value = event.get(key)
        if value:
            return str(value)
    return "default"


class RuleEngine:
    """Dispatch trigger events to registered actions with dedupe.

    Triggers are schedule (cron in each person time zone), ado_change
    (polled changes), and slack_blocker (chat classified as a blocker).
    Actions typically call the interviewer or the blocker intake.
    Handlers are idempotent: each event id runs at most once.
    """

    def __init__(
        self, project_settings: dict[str, dict[str, Any]] | None = None
    ) -> None:
        self.project_settings: dict[str, dict[str, Any]] = dict(
            project_settings or {}
        )
        self._handlers: dict[str, list[tuple[str | None, Action]]] = {}
        self._seen: set[str] = set()

    def configure_project(self, project: str, settings: dict[str, Any]) -> None:
        current = self.project_settings.get(project, {})
        merged = dict(current)
        merged.update(settings)
        self.project_settings[project] = merged

    def settings_for(self, project: str) -> dict[str, Any]:
        return dict(self.project_settings.get(project, {}))

    def register(
        self, trigger: str, action: Action, *, project: str | None = None
    ) -> None:
        """Register an action for a trigger, optionally one project only."""
        name = str(trigger or "").strip()
        if not name:
            raise ValueError("trigger name is required")
        if not callable(action):
            raise TypeError("action must be callable")
        self._handlers.setdefault(name, []).append((project, action))

    def handler_count(self, trigger: str | None = None) -> int:
        if trigger is None:
            return sum(len(items) for items in self._handlers.values())
        return len(self._handlers.get(str(trigger), []))

    def seen_ids(self) -> set[str]:
        return set(self._seen)

    def _resolve_trigger(self, event: dict[str, Any]) -> str:
        name = event_trigger(event)
        if name == TRIGGER_SLACK_MESSAGE:
            text = str(event.get("text", event.get("message", "")) or "")
            if is_blocker_message(text):
                return TRIGGER_SLACK_BLOCKER
        return name

    def handle_event(self, event: dict[str, Any]) -> bool:
        """Run matching actions once per event id. True when handled."""
        eid = event_id(event)
        if eid is not None:
            if eid in self._seen:
                return False
            self._seen.add(eid)
        trigger = self._resolve_trigger(event)
        project = event_project(event)
        targets = list(self._handlers.get(trigger, []))
        if not targets:
            return eid is not None
        ran = False
        context = {
            "project": project,
            "settings": self.settings_for(project),
            "trigger": trigger,
            "event_id": eid,
        }
        for only_project, action in targets:
            if only_project is not None and only_project != project:
                continue
            try:
                action(event, context)
            except TypeError:
                action(event)
            ran = True
        return ran
