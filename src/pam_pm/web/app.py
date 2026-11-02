"""Pam web console: setup wizard, rules, activity stream, board."""

from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import socket
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pam_pm import autostart as autostart_mod
from pam_pm.adapters.pm.base import WorkItem, is_closed
from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.agents.lead import synthesize_review
from pam_pm.agents.planner import approve_proposal, propose_sprint
from pam_pm.core import auth as auth_mod
from pam_pm.core import memory as memory_mod
from pam_pm.core import provider as provider_mod
from pam_pm.core import raid as raid_mod
from pam_pm.core import recovery as recovery_mod
from pam_pm.core import router as router_mod
from pam_pm.core import secrets as secrets_mod
from pam_pm.core import store as store_mod
from pam_pm.core.config import (
    BACKEND_ASANA,
    BACKEND_AZURE_DEVOPS,
    BACKEND_CLICKUP,
    BACKEND_JIRA,
    BACKEND_LINEAR,
    BACKEND_MONDAY,
    BACKEND_PAM_BOARD,
    BACKEND_TRELLO,
    Config,
    backend_label,
    normalize_backend,
)
from pam_pm.core.cost import CALLS_TABLE_DDL
from pam_pm.core.writes import WriteService
from pam_pm.models.vision import VisionClient, plan_from_photo

_SLACK_POLLER: Any = None
_SLACK_POLL_TASK: Any = None
_SLACK_SOCKET: Any = None
_SLACK_SOCKET_PROJECT = "default"

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

mimetypes.add_type("image/svg+xml", ".svg")

app = FastAPI(title="Pam")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

HOST = "127.0.0.1"
PORT = 8765

PROJECTS = ["default", "atlas", "demo"]

WIZARD_ORDER = ["model", "slack", "ado", "people", "rules", "safety", "done"]
WIZARD_PROGRESS_FILENAME = "wizard_progress.json"

_SETUP_TOKEN_FALLBACK = ""
SETUP_TOKEN = ""


def get_setup_token() -> str:
    """Return the current setup token, resolving late.

    Reads PAM_SETUP_TOKEN from the environment on every call so a value
    set after import (for example by pam start) is honored. Falls back
    to an explicitly patched SETUP_TOKEN global (tests) and finally to
    a lazily created module-global random value.
    """
    env = os.environ.get("PAM_SETUP_TOKEN", "").strip()
    if env:
        return env
    current = str(globals().get("SETUP_TOKEN", "") or "").strip()
    if current:
        return current
    global _SETUP_TOKEN_FALLBACK
    if not _SETUP_TOKEN_FALLBACK:
        _SETUP_TOKEN_FALLBACK = secrets.token_urlsafe(32)
        globals()["SETUP_TOKEN"] = _SETUP_TOKEN_FALLBACK
    return _SETUP_TOKEN_FALLBACK


SETUP_DONE = False

LISTEN_CONNECT_FN: Any = None


def _db_path() -> Path:
    return Config().db_path()


def _progress_path() -> Path:
    return Config().ensure_data_dir() / WIZARD_PROGRESS_FILENAME


def _read_all_progress() -> dict[str, Any]:
    try:
        raw = _progress_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_project_progress(project: str) -> dict[str, Any]:
    all_progress = _read_all_progress()
    item = all_progress.get(project or "default", {})
    return dict(item) if isinstance(item, dict) else {}


def _write_project_step(
    project: str, step: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    name = (project or "default").strip() or "default"
    all_progress = _read_all_progress()
    entry = all_progress.get(name, {})
    if not isinstance(entry, dict):
        entry = {}
    steps = entry.get("steps", {})
    if not isinstance(steps, dict):
        steps = {}
    steps[str(step)] = dict(data or {})
    done = entry.get("done", [])
    if not isinstance(done, list):
        done = []
    if str(step) not in done:
        done = [*done, str(step)]
    entry["steps"] = steps
    entry["done"] = done
    entry["current"] = str(step)
    all_progress[name] = entry
    path = _progress_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(all_progress, indent=2), encoding="utf-8")
    return dict(entry)


def _project_backend(name: str) -> str:
    try:
        return normalize_backend(store_mod.get_project_backend(_db_path(), name))
    except Exception:  # noqa: BLE001
        return BACKEND_AZURE_DEVOPS


def _known_project_names() -> list[str]:
    names = list(PROJECTS)
    try:
        for row in store_mod.list_projects(_db_path()):
            label = str(row.get("name", "")).strip()
            if label and label not in names:
                names.append(label)
    except Exception:  # noqa: BLE001
        return sorted(names)
    return sorted(names)


def _projects_with_backends() -> list[dict[str, str]]:
    return [
        {
            "name": name,
            "backend": _project_backend(name),
            "label": backend_label(_project_backend(name)),
        }
        for name in _known_project_names()
    ]


def get_lan_ip() -> str:
    """Return the machine LAN IP, falling back to the hostname."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(2)
            sock.connect(("8.8.8.8", 80))
            ip = str(sock.getsockname()[0] or "").strip()
            if ip and not ip.startswith("127."):
                return ip
        finally:
            try:
                sock.close()
            except OSError:
                pass
    except OSError:
        pass
    try:
        name = socket.gethostname().strip()
    except OSError:
        return "localhost"
    if not name:
        return "localhost"
    try:
        resolved = socket.gethostbyname(name)
    except OSError:
        return name
    if resolved and not resolved.startswith("127."):
        return resolved
    return name


def get_share_url(project: str, port: int = PORT) -> str:
    """Return the LAN URL teammates open for a shared project."""
    name = (project or "default").strip() or "default"
    return f"http://{get_lan_ip()}:{int(port)}/board?project={name}"


def _project_share_on_lan(name: str) -> bool:
    try:
        return store_mod.get_project_share_on_lan(_db_path(), name)
    except Exception:  # noqa: BLE001
        return False


RULE_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "morning",
        "title": "Morning check-in",
        "description": "Send each teammate a short morning message about their open work.",
        "trigger": "Runs each morning.",
    },
    {
        "name": "follow_up",
        "title": "Follow up when work closes",
        "description": "Send the next owner a message when an item is finished.",
        "trigger": "Runs when an item closes.",
    },
    {
        "name": "blocker",
        "title": "Help with blocked work",
        "description": "Ask what is blocked, link the related items, and ask for a finish date.",
        "trigger": "Runs when someone reports blocked work.",
    },
    {
        "name": "triage",
        "title": "Board triage",
        "description": "Scan the board for stale, unassigned, and blocked items and propose a fix for each.",
        "trigger": "Runs each day and when the board changes.",
    },
    {
        "name": "mid_sprint",
        "title": "Mid-sprint adjustment",
        "description": "Propose move out, swap, reassign, or pull from backlog when the sprint is off track, with the effect on the goal.",
        "trigger": "Runs midday and when the board changes.",
    },
]

RULE_STATES: dict[str, bool] = {
    "morning": True,
    "follow_up": True,
    "blocker": False,
    "triage": True,
    "mid_sprint": True,
}

SAMPLE_EVENTS: list[dict[str, Any]] = [
    {
        "id": 1,
        "operation": "update_state",
        "item_id": "101",
        "evidence_quote": "Login form is done, only tests remain.",
        "status": "proposed",
        "detail": "Move 101 to Done.",
    },
    {
        "id": 2,
        "operation": "comment",
        "item_id": "102",
        "evidence_quote": "Waiting on API keys from Sam.",
        "status": "proposed",
        "detail": "Note blocker on 102.",
    },
    {
        "id": 3,
        "operation": "add_link",
        "item_id": "103",
        "evidence_quote": "Deploy follows the migration step.",
        "status": "applied",
        "detail": "Predecessor 102 Successor 103.",
    },
]

SAMPLE_ITEMS: list[dict[str, Any]] = [
    {"id": "101", "title": "Login form", "state": "To Do", "owner": "Ana"},
    {"id": "102", "title": "API keys", "state": "Doing", "owner": "Sam"},
    {"id": "103", "title": "Deploy", "state": "Done", "owner": "Ben"},
]

SAMPLE_LINKS: list[dict[str, str]] = [
    {
        "source_id": "102",
        "target_id": "103",
        "rel": "System.LinkTypes.Dependency-Forward",
        "label": "102 blocks 103",
    },
    {
        "source_id": "101",
        "target_id": "102",
        "rel": "System.LinkTypes.Dependency-Forward",
        "label": "101 blocks 102",
    },
]


class EventBus:
    """In memory event bus with history for the activity stream."""

    def __init__(self, seed: list[dict[str, Any]] | None = None) -> None:
        self._history: list[dict[str, Any]] = [dict(e) for e in (seed or [])]
        self._queues: list[asyncio.Queue[dict[str, Any]]] = []

    def history(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self._history]

    def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        item = dict(event)
        self._history.append(item)
        for queue in self._queues:
            queue.put_nowait(dict(item))
        return item

    def mark_undone(self, write_id: int) -> bool:
        for event in self._history:
            if int(event.get("id", -1)) == write_id:
                event["status"] = "undone"
                return True
        return False

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if queue in self._queues:
            self._queues.remove(queue)


bus = EventBus(SAMPLE_EVENTS)

LAST_REVIEW: dict[str, Any] = {
    "markdown": "",
    "retro_actions": [],
    "low_confidence": [],
}

LAST_PLAN: dict[str, Any] = {
    "readings": [],
    "masks": [],
    "path": "",
    "proposal": [],
    "approved": [],
}

_SEEN_SLACK_EVENT_IDS: set[str] = set()

_TEAMS_DEVICE_FLOWS: dict[str, Any] = {}
TEAMS_TEST_TRANSPORT: Any = None


def set_teams_test_transport(transport: Any) -> None:
    """Pin a fake Entra transport for wizard device tests."""
    global TEAMS_TEST_TRANSPORT
    TEAMS_TEST_TRANSPORT = transport


def reset_teams_device_state() -> None:
    """Clear pending device flows and the pinned test transport."""
    _TEAMS_DEVICE_FLOWS.clear()
    global TEAMS_TEST_TRANSPORT
    TEAMS_TEST_TRANSPORT = None


def reset_slack_events() -> None:
    """Clear remembered Slack event ids (tests and restarts)."""
    _SEEN_SLACK_EVENT_IDS.clear()


def _remember_slack_event(event_id: str) -> bool:
    """Return False when event_id was already seen, else remember it."""
    key = str(event_id or "").strip()
    if not key:
        return True
    if key in _SEEN_SLACK_EVENT_IDS:
        return False
    _SEEN_SLACK_EVENT_IDS.add(key)
    if len(_SEEN_SLACK_EVENT_IDS) > 2000:
        for old in list(_SEEN_SLACK_EVENT_IDS)[:500]:
            _SEEN_SLACK_EVENT_IDS.discard(old)
    return True


def _slack_poller_event_id(envelope: dict[str, Any]) -> str:
    """Return the poller style dedupe id for a webhook envelope, or blank."""
    inner = envelope.get("event")
    if not isinstance(inner, dict):
        inner = envelope if isinstance(envelope, dict) else {}
    if str(inner.get("type", "") or "") != "message":
        return ""
    channel = str(inner.get("channel", "") or "").strip()
    ts = str(inner.get("ts", "") or inner.get("event_ts", "") or "").strip()
    if not channel or not ts:
        return ""
    return f"slack:{channel}:{ts}"


def slack_configured() -> bool:
    """Return True when a Slack bot token is available."""
    try:
        tokens = secrets_mod.effective_slack_tokens()
    except Exception:  # noqa: BLE001
        tokens = {"bot_token": os.environ.get("SLACK_BOT_TOKEN", "")}
    return bool(str(tokens.get("bot_token", "") or "").strip())


def get_slack_poller(
    project: str = "default", chat: Any = None, db: Any = None
) -> Any | None:
    """Return the shared Slack DM poller, creating it when configured.

    The poller shares _remember_slack_event with the webhook route so
    a message handled on one path never fires again on the other. An
    explicitly passed chat (tests) skips the configured check.
    """
    global _SLACK_POLLER
    if _SLACK_POLLER is not None:
        return _SLACK_POLLER
    use_chat = chat if chat is not None else get_chat_adapter(project)
    if use_chat is None:
        return None
    if chat is None and not slack_configured():
        return None
    try:
        from pam_pm.adapters.chat.slack_poll import SlackPoller
    except Exception:  # noqa: BLE001
        return None
    poll_db = db if db is not None else _db_path()

    def _resume_polled(message: Any) -> None:
        try:
            raw_files = getattr(message, "files", None)
            kept = [
                dict(item)
                for item in (raw_files or [])
                if isinstance(item, dict)
            ]
            router_mod.handle_slack_event(
                {
                    "type": "message",
                    "channel": str(getattr(message, "user_id", "") or ""),
                    "user": str(getattr(message, "user_id", "") or ""),
                    "text": str(getattr(message, "text", "") or ""),
                    "ts": str(
                        getattr(message, "ts", "")
                        or getattr(message, "id", "")
                        or ""
                    ),
                    "files": kept,
                },
                chat=use_chat,
                board=get_board_adapter(project),
                db=poll_db,
                model_client=_resume_model_client(project),
                event_sink=bus.publish,
            )
        except Exception:  # noqa: BLE001
            logger.info("slack poll resume failed, keeping local result")

    try:
        poller = SlackPoller(
            source=use_chat,
            db=poll_db,
            bus=bus,
            seen_hook=_remember_slack_event,
            on_message=_resume_polled,
        )
    except Exception:  # noqa: BLE001
        return None
    try:
        attach = getattr(use_chat, "attach_poller", None)
        if callable(attach):
            attach(poller)
    except Exception:  # noqa: BLE001
        logger.info("slack poller attach skipped")
    _SLACK_POLLER = poller
    return poller


def poll_slack_once(project: str = "default") -> list[Any]:
    """Poll Slack DMs once through the attached adapter poller."""
    poller = get_slack_poller(project)
    if poller is None:
        return []
    try:
        source = getattr(poller, "source", None)
        adapter_poll = getattr(source, "poll_once", None)
        if callable(adapter_poll):
            found = list(adapter_poll())
        else:
            found = list(poller.poll_once())
        if found:
            logger.info("slack poll found %d inbound messages", len(found))
        return found
    except Exception:  # noqa: BLE001
        logger.info("slack poll_once failed, will retry next interval")
        return []


async def _slack_poll_loop(project: str = "default") -> None:
    """Poll Slack DMs forever on the normal cadence.

    The poller never stands down, even while the socket is live.
    Socket Mode is the fast path; the poller always runs beside it
    and shared _remember_slack_event dedupe prevents double handling.
    """
    while True:
        try:
            await asyncio.to_thread(poll_slack_once, project)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.info("slack poll loop failed, will retry")
        await asyncio.sleep(max(5, _effective_poll_interval()))


def start_slack_polling(project: str = "default") -> Any | None:
    """Start the Slack DM poller and its background loop when configured."""
    global _SLACK_POLL_TASK
    poller = get_slack_poller(project)
    if poller is None:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return poller
    if _SLACK_POLL_TASK is None or _SLACK_POLL_TASK.done():
        _SLACK_POLL_TASK = loop.create_task(_slack_poll_loop(project))
        try:
            interval = _effective_poll_interval()
        except Exception:  # noqa: BLE001
            interval = 60
        logger.info("slack poller loop started: interval=%ss", interval)
    return poller


def stop_slack_polling() -> None:
    """Stop the Slack background loop and drop the shared poller."""
    global _SLACK_POLL_TASK, _SLACK_POLLER
    task = _SLACK_POLL_TASK
    _SLACK_POLL_TASK = None
    _SLACK_POLLER = None
    if task is not None:
        try:
            task.cancel()
        except Exception:  # noqa: BLE001
            logger.info("slack poll task cancel skipped")


def slack_app_token() -> str:
    """Return the configured Slack app token (xapp), or blank."""
    try:
        tokens = secrets_mod.effective_slack_tokens()
    except Exception:  # noqa: BLE001
        tokens = {"app_token": os.environ.get("SLACK_APP_TOKEN", "")}
    token = str((tokens or {}).get("app_token", "") or "").strip()
    if not token:
        token = os.environ.get("SLACK_APP_TOKEN", "").strip()
    return token


def slack_socket_configured() -> bool:
    """Return True when a Slack app token is available."""
    return bool(slack_app_token())


def slack_socket_healthy() -> bool:
    """Return True while the Socket Mode websocket is live."""
    sock = _SLACK_SOCKET
    if sock is None:
        return False
    try:
        return bool(sock.is_healthy)
    except Exception:  # noqa: BLE001
        return False


def get_slack_socket() -> Any | None:
    """Return the shared SocketModeInbound, if any."""
    return _SLACK_SOCKET


def _on_socket_status(live: bool) -> None:
    """Log socket transitions; the poller always runs beside the socket."""
    if live:
        logger.info("slack socket live, poller still active")
    else:
        logger.info("slack socket down, poller still active")


def _route_socket_envelope(
    envelope: dict[str, Any], project: str
) -> dict[str, Any]:
    """Route one Socket Mode envelope through handle_slack_event.

    Shares _SEEN_SLACK_EVENT_IDS with the webhook and poller so a
    message handled on one path never fires again on another.
    Logs one routing line per envelope (type only, never content).
    """
    if not isinstance(envelope, dict):
        logger.info(
            "slack socket envelope routed: type=unknown decision=ignored "
            "reason=not-dict"
        )
        return {}
    outer = str(envelope.get("type", "") or "").strip() or "unknown"
    inner = envelope.get("event")
    inner_type = (
        str(inner.get("type", "") or "").strip()
        if isinstance(inner, dict)
        else ""
    )
    etype = f"{outer}/{inner_type}" if inner_type else outer
    if outer == "url_verification":
        logger.info(
            "slack socket envelope routed: type=%s decision=ignored "
            "reason=url-verification",
            etype,
        )
        return {}
    event_id = str(envelope.get("event_id", "") or "").strip()
    if event_id and event_id in _SEEN_SLACK_EVENT_IDS:
        logger.info(
            "slack socket envelope routed: type=%s decision=ignored "
            "reason=duplicate",
            etype,
        )
        return {"ok": True, "duplicate": True}
    try:
        poller_id = _slack_poller_event_id(envelope)
    except Exception:  # noqa: BLE001
        poller_id = ""
    if poller_id and poller_id in _SEEN_SLACK_EVENT_IDS:
        logger.info(
            "slack socket envelope routed: type=%s decision=ignored "
            "reason=duplicate",
            etype,
        )
        return {"ok": True, "duplicate": True}
    chat = get_chat_adapter(project)
    if chat is None:
        logger.info(
            "slack socket envelope routed: type=%s decision=ignored "
            "reason=no-chat",
            etype,
        )
        return {}
    try:
        result = router_mod.handle_slack_event(
            envelope,
            chat=chat,
            board=get_board_adapter(project),
            db=_db_path(),
            model_client=_resume_model_client(project),
            event_sink=bus.publish,
        )
    except Exception:  # noqa: BLE001
        logger.info(
            "slack socket envelope routed: type=%s decision=ignored "
            "reason=resume-failed",
            etype,
        )
        return {}
    if event_id:
        _remember_slack_event(event_id)
    if poller_id:
        _remember_slack_event(poller_id)
    logger.info(
        "slack socket envelope routed: type=%s decision=resume",
        etype,
    )
    return result if isinstance(result, dict) else {}


def start_slack_socket(
    project: str = "default",
    client_factory: Any = None,
    app_token: str | None = None,
) -> Any | None:
    """Start SocketModeInbound when an app token is configured.

    Opens one outbound websocket (no inbound ports). Returns the shared
    inbound, or None when no app token is configured.
    """
    global _SLACK_SOCKET, _SLACK_SOCKET_PROJECT
    if _SLACK_SOCKET is not None:
        return _SLACK_SOCKET
    token = str(app_token).strip() if app_token is not None else slack_app_token()
    if not token:
        logger.info("slack socket skipped: reason=no app token")
        return None
    try:
        from pam_pm.adapters.chat.slack_socket import SocketModeInbound
    except Exception:  # noqa: BLE001
        return None
    name = (project or "default").strip() or "default"
    _SLACK_SOCKET_PROJECT = name

    def _on_envelope(payload: dict[str, Any]) -> None:
        _route_socket_envelope(payload, _SLACK_SOCKET_PROJECT)

    try:
        inbound = SocketModeInbound(
            token,
            _on_envelope,
            client_factory=client_factory,
            on_status=_on_socket_status,
        )
    except Exception:  # noqa: BLE001
        return None
    _SLACK_SOCKET = inbound
    try:
        inbound.start()
        logger.info("slack socket connected: project=%s", name)
    except Exception:  # noqa: BLE001
        logger.info("slack socket skipped: reason=start failed")
    return inbound


def stop_slack_socket() -> None:
    """Stop the Socket Mode websocket and drop the shared inbound."""
    global _SLACK_SOCKET
    inbound = _SLACK_SOCKET
    _SLACK_SOCKET = None
    if inbound is None:
        return
    try:
        inbound.stop()
    except Exception:  # noqa: BLE001
        logger.info("slack socket shutdown skipped")


@app.on_event("startup")
async def _startup_slack_poll() -> None:
    """Start Socket Mode when configured; the poller always runs too."""
    try:
        start_slack_socket()
    except Exception:  # noqa: BLE001
        logger.info("slack socket startup skipped")
    try:
        start_slack_polling()
    except Exception:  # noqa: BLE001
        logger.info("slack poll startup skipped")


@app.on_event("shutdown")
async def _shutdown_slack_poll() -> None:
    """Stop the Socket Mode websocket and the Slack DM poller."""
    try:
        stop_slack_socket()
    except Exception:  # noqa: BLE001
        logger.info("slack socket shutdown skipped")
    try:
        stop_slack_polling()
    except Exception:  # noqa: BLE001
        logger.info("slack poll shutdown skipped")


_DEMO_BOARD: SQLiteBoard | None = None


def _demo_board() -> SQLiteBoard:
    global _DEMO_BOARD
    if _DEMO_BOARD is None:
        _DEMO_BOARD = SQLiteBoard()
    return _DEMO_BOARD


def _sample_interviews() -> dict[str, dict[str, Any]]:
    return {
        "101": {
            "status": "To Do",
            "evidence_quote": "Login form is done, only tests remain.",
            "confidence": 0.8,
        },
        "102": {
            "status": "Doing",
            "evidence_quote": "Waiting on API keys from Sam.",
            "confidence": 0.7,
        },
        "103": {
            "status": "Done",
            "evidence_quote": "Deploy follows the migration step.",
            "confidence": 0.4,
        },
    }


def _run_review_now(project: str = "default") -> dict[str, Any]:
    try:
        raid_backend = _project_backend(project)
        raid_db: Any = _db_path()
    except Exception:  # noqa: BLE001
        raid_backend = BACKEND_AZURE_DEVOPS
        raid_db = None
    model_client: Any = None
    try:
        from pam_pm.models.client import ModelClient

        candidate = ModelClient()
        if candidate.model and candidate.base_url and candidate._api_key:
            model_client = candidate
        else:
            router_mod.log_route(
                router_mod.CAP_REVIEW, "", "no Meta creds, local render"
            )
    except Exception:  # noqa: BLE001
        model_client = None
    try:
        result = synthesize_review(
            SAMPLE_ITEMS,
            _sample_interviews(),
            bus.history(),
            model_client=model_client,
            raid_db=raid_db,
            raid_backend=raid_backend,
            raid_project=project,
        )
    except Exception:  # noqa: BLE001
        result = synthesize_review(
            SAMPLE_ITEMS,
            _sample_interviews(),
            bus.history(),
            raid_db=raid_db,
            raid_backend=raid_backend,
            raid_project=project,
        )
    LAST_REVIEW["markdown"] = result["review_markdown"]
    LAST_REVIEW["retro_actions"] = result["retro_actions"]
    LAST_REVIEW["low_confidence"] = result["low_confidence"]
    logger.info(
        "review run: %d retro actions, %d low confidence",
        len(result["retro_actions"]),
        len(result["low_confidence"]),
    )
    return result


REVIEW_HEADINGS = {
    "Sprint review": "Sprint review",
    "Facts": "Facts",
    "Interpretations": "Unsure items",
    "Low confidence flags": "Needs follow up",
    "Events": "Recent changes",
    "Retro actions": "Follow up actions",
}


def _markdown_inline(text: str) -> str:
    """Render inline markdown over escaped text (code, bold, italic, links)."""
    esc = html.escape(text, quote=False)
    esc = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", esc)
    esc = re.sub(r"\*\*([^*]+?)\*\*", r"<strong>\1</strong>", esc)
    esc = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", esc)

    def _link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2).strip()
        if re.match(r"(?i)^https?://", url):
            return f'<a href="{html.escape(url, quote=True)}">{label}</a>'
        return label

    return re.sub(r"\[([^\]]+?)\]\(([^)\s]+?)\)", _link, esc)


def _markdown_to_html(raw: str) -> str:
    """Render basic markdown to sanitized HTML (headings, lists, quotes, code).

    All text is escaped first, so raw HTML in the input never passes through.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    out: list[str] = []
    para: list[str] = []
    in_list = ""
    in_code = False
    code_lines: list[str] = []

    def flush_para() -> None:
        if para:
            body = " ".join(para).strip()
            if body:
                out.append(f"<p>{_markdown_inline(body)}</p>")
            para.clear()

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = ""

    for line in text.splitlines() + [""]:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                out.append(
                    "<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>"
                )
                code_lines.clear()
                in_code = False
            else:
                flush_para()
                close_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(line.rstrip())
            continue
        if not stripped:
            flush_para()
            close_list()
            continue
        if stripped.startswith("### "):
            flush_para()
            close_list()
            out.append(f"<h3>{_markdown_inline(stripped[4:].strip())}</h3>")
        elif stripped.startswith("## "):
            flush_para()
            close_list()
            heading = REVIEW_HEADINGS.get(stripped[3:].strip(), stripped[3:].strip())
            out.append(f"<h2>{_markdown_inline(heading)}</h2>")
        elif stripped.startswith("# "):
            flush_para()
            close_list()
            heading = REVIEW_HEADINGS.get(stripped[2:].strip(), stripped[2:].strip())
            out.append(f"<h2>{_markdown_inline(heading)}</h2>")
        elif stripped.startswith("> "):
            flush_para()
            close_list()
            out.append(
                f"<blockquote>{_markdown_inline(stripped[2:].strip())}</blockquote>"
            )
        elif stripped.startswith(("- ", "* ")):
            flush_para()
            if in_list != "ul":
                close_list()
                out.append("<ul>")
                in_list = "ul"
            out.append(f"<li>{_markdown_inline(stripped[2:].strip())}</li>")
        elif re.match(r"^\d+\.\s+", stripped):
            flush_para()
            if in_list != "ol":
                close_list()
                out.append("<ol>")
                in_list = "ol"
            content = re.sub(r"^\d+\.\s+", "", stripped)
            out.append(f"<li>{_markdown_inline(content)}</li>")
        elif stripped in ("---", "***", "___"):
            flush_para()
            close_list()
            out.append("<hr>")
        else:
            close_list()
            para.append(stripped)
    return "\n".join(out)


def _review_result_html(result: dict[str, Any]) -> str:
    body = _markdown_to_html(str(result.get("review_markdown", "")))
    if body:
        parts = [f'<div class="review-body">{body}</div>']
    else:
        parts = ['<p class="muted">No review yet. Choose Run review to make one.</p>']
    parts.append('<section class="card"><h2>Follow up actions</h2>')
    actions = result.get("retro_actions", [])
    if not actions:
        parts.append(
            '<p class="muted">No follow up actions. Nothing needs attention.</p>'
        )
    for index, action in enumerate(actions):
        title = html.escape(str(action.get("title", "")))
        desc = html.escape(str(action.get("description", "")))
        parts.append(
            "<article>"
            f"<h3>{title}</h3>"
            f'<p class="muted">{desc}</p>'
            f'<button hx-post="/api/review/retro/create?index={index}" '
            f'hx-target="#retro-result-{index}" hx-swap="innerHTML">'
            "Create task</button>"
            f'<span id="retro-result-{index}" class="result" aria-live="polite"></span>'
            "</article>"
        )
    parts.append("</section>")
    return "\n".join(parts)


def _plan_result_html(photo: dict[str, Any], proposal: list[dict[str, Any]]) -> str:
    from pam_pm.models.vision import pair_masks_with_readings

    parts: list[str] = []
    pairs = pair_masks_with_readings(photo)
    parts.append('<section class="card"><h2>Notes found</h2>')
    if pairs:
        word = "note" if len(pairs) == 1 else "notes"
        parts.append(f"<p>Found {len(pairs)} {word} in the photo.</p>")
        items = "".join(
            '<li class="note-pair" style="display:flex;gap:1rem;align-items:baseline">'
            f"<span class=\"note-mask\">{html.escape(str(entry['mask_label']))}</span>"
            f"<span class=\"note-reading\">"
            f"{html.escape(str(entry['reading']) or '(no words found)')}</span>"
            "</li>"
            for entry in pairs
        )
        parts.append(f'<ul class="note-pairs">{items}</ul>')
    else:
        parts.append("<p>No separate notes found. The whole photo was read as one.</p>")
        parts.append(
            '<p class="muted">No words were found in the photo. '
            "Try a closer, brighter photo and upload again.</p>"
        )
    parts.append("</section>")
    parts.append('<section class="card"><h2>Suggested tasks</h2>')
    if proposal:
        rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(d.get('title', '')))}</td>"
            f"<td>{html.escape(str(d.get('type', '')))}</td>"
            f"<td>{html.escape(str(d.get('owner') or 'Unassigned'))}</td>"
            f"<td>{html.escape(str(d.get('estimate') if d.get('estimate') is not None else '-'))}</td>"
            "</tr>"
            for d in proposal
        )
        parts.append(
            '<table class="proposal"><thead><tr>'
            '<th scope="col">Title</th><th scope="col">Type</th>'
            '<th scope="col">Owner</th><th scope="col">Estimate</th>'
            "</tr></thead><tbody>"
            f"{rows}</tbody></table>"
        )
        parts.append(
            '<button hx-post="/api/plan/approve" hx-target="#approve-result" '
            'hx-swap="innerHTML">Create these tasks</button>'
        )
    else:
        parts.append(
            '<p class="muted">No suggested tasks yet. Upload a board photo first. '
            "The create button stays off until suggestions are ready.</p>"
        )
        parts.append(
            '<button hx-post="/api/plan/approve" hx-target="#approve-result" '
            'hx-swap="innerHTML" disabled>Create these tasks</button>'
        )
    parts.append(
        '<div id="approve-result" class="result" aria-live="polite"></div></section>'
    )
    return "\n".join(parts)


def is_valid_token(token: str | None) -> bool:
    expected = get_setup_token()
    if not token or not expected:
        return False
    return secrets.compare_digest(str(token), expected)


async def _setup_token_ok(request: Request) -> bool:
    """Check the setup token from query, header, cookie, or body."""
    candidate = request.query_params.get("token", "").strip()
    if candidate and is_valid_token(candidate):
        return True
    candidate = request.headers.get("x-setup-token", "").strip()
    if candidate and is_valid_token(candidate):
        return True
    try:
        candidate = request.cookies.get("pam_setup_token", "").strip()
    except Exception:  # noqa: BLE001
        candidate = ""
    if candidate and is_valid_token(candidate):
        return True
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = None
        if isinstance(payload, dict):
            candidate = str(payload.get("token", "") or "").strip()
            if candidate and is_valid_token(candidate):
                return True
        return False
    try:
        form = await request.form()
        candidate = str(form.get("token", "") or "").strip()
        if candidate and is_valid_token(candidate):
            return True
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = None
    if isinstance(payload, dict):
        candidate = str(payload.get("token", "") or "").strip()
        if candidate and is_valid_token(candidate):
            return True
    return False


def _setup_token_denied(request: Request) -> Any:
    msg = "That setup token did not match."
    hint = "Paste the token printed when you ran pam start and try again."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(False, msg, hint))
    return JSONResponse({"ok": False, "message": msg, "hint": hint})


AUTH_EXEMPT_PATHS = frozenset(
    {"/", "/health", "/healthz", "/login", "/api/slack/events"}
)
AUTH_EXEMPT_PREFIXES = ("/static", "/api/auth")


def _auth_data_dir() -> Path | None:
    try:
        return Config().data_dir
    except Exception:  # noqa: BLE001
        return None


def _auth_allowed_host() -> str:
    try:
        return Config().allowed_host
    except Exception:  # noqa: BLE001
        return ""


def _client_ip(request: Request) -> str:
    client = request.client
    if client is not None and client.host:
        return str(client.host)
    return "unknown"


def _request_session_ok(request: Request) -> bool:
    try:
        return auth_mod.validate_session(
            request.cookies.get(auth_mod.SESSION_COOKIE, "")
        )
    except Exception:  # noqa: BLE001
        return False


@app.middleware("http")
async def _console_auth_guard(request: Request, call_next: Any) -> Any:
    """Require a login session once a console password is set.

    Localhost mode with no stored password passes through unchanged
    (the setup token flow keeps working). Health checks stay open so
    container hosts can probe the app.
    """
    path = request.url.path
    if path not in ("/", "/health", "/healthz") and not auth_mod.host_allowed(
        request.headers.get("host", ""), _auth_allowed_host()
    ):
        return JSONResponse(
            {
                "ok": False,
                "message": "This host name is not allowed.",
                "hint": "Open Pam with the PAM_ALLOWED_HOST name set on the server.",
            },
            status_code=400,
        )
    if path in AUTH_EXEMPT_PATHS or path.startswith(AUTH_EXEMPT_PREFIXES):
        return await call_next(request)
    data_dir = _auth_data_dir()
    password_set = False
    if data_dir is not None:
        try:
            password_set = auth_mod.has_password(data_dir)
        except Exception:  # noqa: BLE001
            password_set = False
    if not password_set:
        return await call_next(request)
    if _request_session_ok(request):
        return await call_next(request)
    if path.startswith("/api"):
        return JSONResponse(
            {
                "ok": False,
                "message": "Login is required.",
                "hint": "Open /login in a browser and enter the console password.",
            },
            status_code=401,
        )
    return RedirectResponse(url="/login", status_code=303)


def _login_page_html(mode: str, message: str) -> str:
    if mode == "setup":
        title = "Choose a console password"
        intro = (
            "No console password is set yet. Choose one now. "
            "It is stored hashed on this server."
        )
        action = "/api/auth/setup"
        button = "Save password"
    else:
        title = "Log in to Pam"
        intro = "Enter the console password to open Pam."
        action = "/login"
        button = "Log in"
    note = ""
    if message:
        note = f'<p class="message" role="alert">{html.escape(message)}</p>'
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<style>"
        'body{font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",'
        'Roboto,"Helvetica Neue",Arial,sans-serif;background:#0e141a;color:#e8eef3;'
        "margin:0;display:flex;min-height:100vh;align-items:center;"
        "justify-content:center;padding:1rem;box-sizing:border-box;}"
        ".card{background:#161e26;border:1px solid #2c3742;border-radius:0.75rem;"
        "padding:2rem;max-width:26rem;width:100%;box-sizing:border-box;}"
        "h1{font-size:1.25rem;margin:0 0 0.5rem;font-weight:600;"
        "letter-spacing:-0.01em;}"
        "p{color:#9fb0bf;}"
        ".message{color:#fecaca;}"
        "label{display:block;margin:1rem 0 0.25rem;font-weight:600;}"
        'input[type="password"]{width:100%;padding:0.6rem;box-sizing:border-box;'
        "background:#0e141a;color:#e8eef3;"
        "border:1px solid #2c3742;border-radius:0.375rem;font-size:1rem;}"
        "button{margin-top:1.25rem;padding:0.6rem 1.25rem;font-size:1rem;"
        "background:#3768dc;color:#fff;border:none;border-radius:0.5rem;"
        "cursor:pointer;}button:hover{background:#2f5cc4;}"
        "</style></head><body>"
        '<main class="card">'
        f"<h1>{html.escape(title)}</h1>"
        f"<p>{html.escape(intro)}</p>"
        f"{note}"
        f'<form method="post" action="{action}">'
        '<label for="password">Password</label>'
        '<input type="password" id="password" name="password" '
        'autocomplete="current-password" minlength="8" required>'
        f'<button type="submit">{html.escape(button)}</button>'
        "</form></main></body></html>"
    )


async def _login_password(request: Request) -> str:
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            return ""
        if isinstance(payload, dict):
            return str(payload.get("password", "") or "")
        return ""
    form = await request.form()
    return str(form.get("password", "") or "")


def _session_cookie_response(request: Request, token: str, *, as_json: bool) -> Any:
    if as_json:
        resp: Any = JSONResponse({"ok": True, "message": "Logged in."})
    else:
        resp = RedirectResponse(url="/board", status_code=303)
    resp.set_cookie(
        auth_mod.SESSION_COOKIE,
        token,
        max_age=auth_mod.SESSION_TTL_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


def _rate_limited_response(request: Request, key: str, *, as_json: bool) -> Any:
    retry = auth_mod.login_retry_after(key)
    msg = "Too many login attempts. Try again in a moment."
    if as_json:
        return JSONResponse(
            {"ok": False, "message": msg},
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
    resp = HTMLResponse(_login_page_html("login", msg), status_code=429)
    resp.headers["Retry-After"] = str(retry)
    return resp


def _project(request: Request) -> str:
    want = request.query_params.get("project", "default").strip() or "default"
    if want in PROJECTS:
        return want
    if want in _known_project_names():
        return want
    if len(want) <= 64 and all(c.isalnum() or c in ("-", "_", ".") for c in want):
        return want
    return "default"


def _offline_banner() -> str:
    try:
        return recovery_mod.get_offline_banner(_db_path())
    except Exception:  # noqa: BLE001
        return ""


def _autostart_enabled() -> bool:
    try:
        return bool(autostart_mod.is_enabled())
    except Exception:  # noqa: BLE001
        return False


def _autostart_choice(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("on", "true", "1", "yes", "checked")


def _context(request: Request, active: str) -> dict[str, Any]:
    project = _project(request)
    return {
        "request": request,
        "active": active,
        "project": project,
        "projects": _projects_with_backends(),
        "project_backend": _project_backend(project),
        "project_backend_label": backend_label(_project_backend(project)),
        "offline_banner": _offline_banner(),
    }


def _check_meta() -> tuple[bool, str, str]:
    try:
        effective = secrets_mod.effective_meta_config()
    except Exception:  # noqa: BLE001
        effective = {
            "model": os.environ.get("META_MODEL", ""),
            "base_url": os.environ.get("META_BASE_URL", ""),
            "api_key": os.environ.get("META_API_KEY", ""),
        }
    missing: list[str] = []
    if not str(effective.get("model", "") or "").strip():
        missing.append("model name")
    if not str(effective.get("base_url", "") or "").strip():
        missing.append("base URL")
    if not str(effective.get("api_key", "") or "").strip():
        missing.append("API key")
    if missing:
        return (
            False,
            "Model check failed. Pam cannot reach the model.",
            "Open Step 1, add the missing "
            + ", ".join(missing)
            + ", choose Save, then Test again.",
        )
    return (
        True,
        "Model check passed. Pam can reach the model.",
        "",
    )


PROVIDER_CLIENT_FACTORY: Any = None


def _check_provider() -> tuple[bool, str, str]:
    try:
        saved = provider_mod.load_provider(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        saved = {"provider": "meta"}
    if provider_mod.normalize_provider(saved.get("provider")) == "meta":
        return _check_meta()
    base = str(saved.get("base_url", "") or "").strip()
    model = str(saved.get("model", "") or "").strip()
    try:
        key = provider_mod.load_provider_key(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        key = ""
    ok, message, hint = provider_mod.validate_custom_config(base, model, key)
    if not ok:
        return ok, message, hint
    try:
        if PROVIDER_CLIENT_FACTORY is not None:
            client = PROVIDER_CLIENT_FACTORY(base, model, key)
        else:
            from pam_pm.models.client import ModelClient

            client = ModelClient(model=model, base_url=base, api_key=key)
        try:
            return provider_mod.test_with_client(client)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001, S110
                    pass
    except Exception as exc:  # noqa: BLE001
        return (
            False,
            "Custom provider check failed. Pam could not reach the endpoint.",
            f"Check the base URL and key, then test again. Detail: {exc}",
        )


def _check_slack() -> tuple[bool, str, str]:
    try:
        tokens = secrets_mod.effective_slack_tokens()
    except Exception:  # noqa: BLE001
        tokens = {"bot_token": os.environ.get("SLACK_BOT_TOKEN", "")}
    if not str(tokens.get("bot_token", "") or "").strip():
        return (
            False,
            "Slack check failed. Pam cannot send messages.",
            "Open Step 2, paste the bot token, choose Save, then Test again.",
        )
    return (
        True,
        "Slack check passed. Pam can send messages.",
        "",
    )


def _check_teams() -> tuple[bool, str, str]:
    try:
        effective = secrets_mod.effective_teams_config()
    except Exception:  # noqa: BLE001
        effective = {
            "app_id": os.environ.get("TEAMS_APP_ID", ""),
            "app_password": os.environ.get("TEAMS_APP_PASSWORD", ""),
            "tenant_id": os.environ.get("TEAMS_TENANT_ID", ""),
        }
    missing: list[str] = []
    if not str(effective.get("app_id", "") or "").strip():
        missing.append("app ID")
    if not str(effective.get("app_password", "") or "").strip():
        missing.append("app password")
    if not str(effective.get("tenant_id", "") or "").strip():
        missing.append("tenant ID")
    if missing:
        return (
            False,
            "Teams check failed. Pam cannot send messages.",
            "Open Step 2, add the missing "
            + ", ".join(missing)
            + ", choose Save, then Test again.",
        )
    return (
        True,
        (
            "Teams check passed. Pam can send messages, and polling brings "
            "replies in with no public endpoint."
        ),
        "",
    )


def _check_ado() -> tuple[bool, str, str]:
    try:
        effective = secrets_mod.effective_ado_config()
    except Exception:  # noqa: BLE001
        effective = {
            "org_url": os.environ.get("ADO_ORG_URL", ""),
            "project": os.environ.get("ADO_PROJECT", ""),
            "team": os.environ.get("ADO_TEAM", ""),
            "pat": os.environ.get("ADO_PAT", ""),
        }
    missing: list[str] = []
    if not str(effective.get("org_url", "") or "").strip():
        missing.append("org URL")
    if not str(effective.get("project", "") or "").strip():
        missing.append("project")
    if not str(effective.get("pat", "") or "").strip():
        missing.append("access token")
    if missing:
        return (
            False,
            "Azure DevOps check failed. Pam cannot read the project.",
            "Open Step 3, add the missing "
            + ", ".join(missing)
            + ", choose Save, then Test again.",
        )
    return (
        True,
        "Azure DevOps check passed. Pam can read the project.",
        "",
    )


def _check_jira() -> tuple[bool, str, str]:
    try:
        effective = secrets_mod.effective_jira_config()
    except Exception:  # noqa: BLE001
        effective = {
            "base_url": os.environ.get("JIRA_BASE_URL", ""),
            "email": os.environ.get("JIRA_EMAIL", ""),
            "project": os.environ.get("JIRA_PROJECT", ""),
            "api_token": os.environ.get("JIRA_API_TOKEN", ""),
        }
    missing: list[str] = []
    if not str(effective.get("base_url", "") or "").strip():
        missing.append("site URL")
    if not str(effective.get("email", "") or "").strip():
        missing.append("email")
    if not str(effective.get("project", "") or "").strip():
        missing.append("project key")
    if not str(effective.get("api_token", "") or "").strip():
        missing.append("API token")
    if missing:
        return (
            False,
            "Jira check failed. Pam cannot read the project.",
            "Open the Jira step, add the missing "
            + ", ".join(missing)
            + ", choose Save, then Test again.",
        )
    return (
        True,
        "Jira check passed. Pam can read the project.",
        "",
    )


def _check_linear() -> tuple[bool, str, str]:
    try:
        effective = secrets_mod.effective_linear_config()
    except Exception:  # noqa: BLE001
        effective = {
            "team": os.environ.get("LINEAR_TEAM", ""),
            "api_key": os.environ.get("LINEAR_API_KEY", ""),
        }
    missing: list[str] = []
    if not str(effective.get("team", "") or "").strip():
        missing.append("team key")
    if not str(effective.get("api_key", "") or "").strip():
        missing.append("API key")
    if missing:
        return (
            False,
            "Linear check failed. Pam cannot read the team.",
            "Open the Linear step, add the missing "
            + ", ".join(missing)
            + ", choose Save, then Test again.",
        )
    return (
        True,
        "Linear check passed. Pam can read the team.",
        "",
    )


def _check_trello() -> tuple[bool, str, str]:
    try:
        effective = secrets_mod.effective_trello_config()
    except Exception:  # noqa: BLE001
        effective = {
            "board": os.environ.get("TRELLO_BOARD", ""),
            "api_key": os.environ.get("TRELLO_API_KEY", ""),
            "token": os.environ.get("TRELLO_TOKEN", ""),
        }
    missing: list[str] = []
    if not str(effective.get("board", "") or "").strip():
        missing.append("board")
    if not str(effective.get("api_key", "") or "").strip():
        missing.append("API key")
    if not str(effective.get("token", "") or "").strip():
        missing.append("token")
    if missing:
        return (
            False,
            "Trello check failed. Pam cannot read the board.",
            "Open the Trello step, add the missing "
            + ", ".join(missing)
            + ", choose Save, then Test again.",
        )
    return (
        True,
        "Trello check passed. Pam can read the board.",
        "",
    )


def _check_clickup() -> tuple[bool, str, str]:
    try:
        effective = secrets_mod.effective_clickup_config()
    except Exception:  # noqa: BLE001
        effective = {
            "team": os.environ.get("CLICKUP_TEAM", ""),
            "list": os.environ.get("CLICKUP_LIST", ""),
            "api_token": os.environ.get("CLICKUP_API_TOKEN", ""),
        }
    missing: list[str] = []
    if not str(effective.get("list", "") or "").strip():
        missing.append("list")
    if not str(effective.get("api_token", "") or "").strip():
        missing.append("API token")
    if missing:
        return (
            False,
            "ClickUp check failed. Pam cannot read the list.",
            "Open the ClickUp step, add the missing "
            + ", ".join(missing)
            + ", choose Save, then Test again.",
        )
    return (
        True,
        "ClickUp check passed. Pam can read the list.",
        "",
    )


def _check_asana() -> tuple[bool, str, str]:
    try:
        effective = secrets_mod.effective_asana_config()
    except Exception:  # noqa: BLE001
        effective = {
            "project": os.environ.get("ASANA_PROJECT", ""),
            "api_token": os.environ.get("ASANA_API_TOKEN", ""),
        }
    missing: list[str] = []
    if not str(effective.get("project", "") or "").strip():
        missing.append("project")
    if not str(effective.get("api_token", "") or "").strip():
        missing.append("API token")
    if missing:
        return (
            False,
            "Asana check failed. Pam cannot read the project.",
            "Open the Asana step, add the missing "
            + ", ".join(missing)
            + ", choose Save, then Test again.",
        )
    return (
        True,
        "Asana check passed. Pam can read the project.",
        "",
    )


def _check_monday() -> tuple[bool, str, str]:
    try:
        effective = secrets_mod.effective_monday_config()
    except Exception:  # noqa: BLE001
        effective = {
            "board": os.environ.get("MONDAY_BOARD", ""),
            "api_token": os.environ.get("MONDAY_API_TOKEN", ""),
        }
    missing = []
    if not str(effective.get("board", "") or "").strip():
        missing.append("board")
    if not str(effective.get("api_token", "") or "").strip():
        missing.append("API token")
    if missing:
        return (
            False,
            "Monday check failed. Pam cannot read the board.",
            "Open the Monday step, add the missing "
            + ", ".join(missing)
            + ", choose Save, then Test again.",
        )
    return (
        True,
        "Monday check passed. Pam can read the board.",
        "",
    )


def _people_names(project: str = "default") -> list[str]:
    progress = _read_project_progress(project)
    steps = progress.get("steps", {})
    people: list[str] = []
    if isinstance(steps, dict):
        entry = steps.get("people", {})
        if isinstance(entry, dict):
            raw = entry.get("people", entry.get("names", []))
            if isinstance(raw, str):
                people = [p.strip() for p in raw.replace(",", "\n").splitlines()]
            elif isinstance(raw, list):
                people = [str(p).strip() for p in raw]
    env_people = os.environ.get("PAM_PEOPLE", "").strip()
    if not people and env_people:
        people = [p.strip() for p in env_people.replace(",", "\n").splitlines()]
    return [p for p in people if p]


def _check_people(project: str = "default") -> tuple[bool, str, str]:
    people = _people_names(project)
    if people:
        word = "teammate" if len(people) == 1 else "teammates"
        return (
            True,
            f"People check passed. {len(people)} {word} added.",
            "",
        )
    return (
        False,
        "People check failed. No teammates added yet.",
        "Add at least one teammate name, save the step, then test again.",
    )


def _check_rules() -> tuple[bool, str, str]:
    enabled = [name for name, on in RULE_STATES.items() if on]
    if enabled:
        word = "rule" if len(enabled) == 1 else "rules"
        return (
            True,
            f"Rules check passed. {len(enabled)} {word} turned on.",
            "",
        )
    return (
        False,
        "Rules check failed. All rules are turned off.",
        "Turn on at least one rule on the Rules page, then test again.",
    )


SAFETY_DRY_RUN_KEY = "settings.dry_run"
SAFETY_SPEND_CAP_KEY = "settings.spend_cap"


def _apply_safety_overrides(dry_run: bool, spend_cap: float) -> tuple[bool, float]:
    try:
        override_dry = recovery_mod.kv_get(_db_path(), SAFETY_DRY_RUN_KEY)
    except Exception:  # noqa: BLE001
        override_dry = ""
    if override_dry.strip().lower() in ("on", "off"):
        dry_run = override_dry.strip().lower() == "on"
    try:
        override_cap = recovery_mod.kv_get(_db_path(), SAFETY_SPEND_CAP_KEY)
    except Exception:  # noqa: BLE001
        override_cap = ""
    if override_cap.strip():
        try:
            spend_cap = float(override_cap.strip())
        except ValueError:
            pass
    return dry_run, spend_cap


def _effective_safety() -> tuple[bool, float]:
    try:
        cfg = Config()
        dry_run, spend_cap = cfg.dry_run, cfg.daily_spend_cap
    except Exception:  # noqa: BLE001
        dry_run, spend_cap = True, 0.0
    return _apply_safety_overrides(dry_run, spend_cap)


POLL_INTERVAL_KEY = "settings.poll_interval"


def _effective_poll_interval() -> int:
    try:
        from pam_pm.rules.poller import effective_poll_interval

        return effective_poll_interval(_db_path())
    except Exception:  # noqa: BLE001
        return 60


def _check_safety() -> tuple[bool, str, str]:
    try:
        cfg = Config()
    except Exception:  # noqa: BLE001
        return (
            False,
            "Safety check failed. Settings could not be read.",
            "Check the data directory is writable, then test again.",
        )
    dry_run, spend_cap = _apply_safety_overrides(cfg.dry_run, cfg.daily_spend_cap)
    if spend_cap <= 0:
        return (
            False,
            "Safety check failed. Daily spend cap is missing.",
            "Set DAILY_SPEND_CAP to a value above zero, or save a cap on the Settings page, then test again.",
        )
    mode = "Dry run is on" if dry_run else "Dry run is off"
    return (
        True,
        f"Safety check passed. {mode} and spend cap is set.",
        "",
    )


def _parse_capacity(value: Any) -> float:
    try:
        num = float(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, num)


def _roster_entries(project: str) -> list[dict[str, Any]]:
    progress = _read_project_progress(project)
    steps = progress.get("steps", {})
    entry = steps.get("roster", {}) if isinstance(steps, dict) else {}
    details: dict[str, dict[str, Any]] = {}
    if isinstance(entry, dict):
        raw = entry.get("entries", [])
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and str(item.get("name", "")).strip():
                    label = str(item.get("name")).strip()
                    details[label] = {
                        "skills": str(item.get("skills", "") or ""),
                        "capacity": _parse_capacity(item.get("capacity", 0)),
                    }
    names = _people_names(project)
    out = [
        {
            "name": name,
            "skills": details.get(name, {}).get("skills", ""),
            "capacity": details.get(name, {}).get("capacity", 0.0),
        }
        for name in names
    ]
    for label in sorted(details):
        if label not in names:
            out.append({"name": label, **details[label]})
    return out


def _merge_roster_entries(
    people_text: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_name = {
        str(row.get("name", "")).strip(): row
        for row in rows
        if str(row.get("name", "")).strip()
    }
    ordered = [p.strip() for p in people_text.replace(",", "\n").splitlines()]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for name in ordered:
        if not name or name in seen:
            continue
        seen.add(name)
        row = by_name.get(name, {})
        out.append(
            {
                "name": name,
                "skills": str(row.get("skills", "") or "").strip(),
                "capacity": _parse_capacity(row.get("capacity", 0)),
            }
        )
    return out


def _cost_summary() -> dict[str, Any]:
    calls, dollars_total, prompt_total, cached_total = 0, 0.0, 0, 0
    try:
        import sqlite3

        conn = sqlite3.connect(str(_db_path()))
        try:
            conn.execute(CALLS_TABLE_DDL)
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(dollars), 0),"
                " COALESCE(SUM(prompt_tokens), 0),"
                " COALESCE(SUM(cached_tokens), 0) FROM calls"
            ).fetchone()
        finally:
            conn.close()
        if row:
            calls = int(row[0] or 0)
            dollars_total = float(row[1] or 0.0)
            prompt_total = int(row[2] or 0)
            cached_total = int(row[3] or 0)
    except Exception:  # noqa: BLE001
        calls, dollars_total, prompt_total, cached_total = 0, 0.0, 0, 0
    share = (cached_total / prompt_total) if prompt_total > 0 else 0.0
    return {
        "calls": calls,
        "dollars": dollars_total,
        "prompt_tokens": prompt_total,
        "cached_tokens": cached_total,
        "cached_pct": round(share * 100),
    }


def _check_backend(project: str = "default") -> tuple[bool, str, str]:
    backend = _project_backend(project)
    label = backend_label(backend)
    return (
        True,
        f"Backend check passed. Tasks will live on {label}.",
        "",
    )


def _test_result_html(ok: bool, message: str, hint: str = "") -> str:
    safe_message = html.escape(message)
    safe_hint = html.escape(hint)
    if ok:
        return f'<span class="pill pill-ok">Pass: {safe_message}</span>'
    if safe_hint:
        return (
            f'<span class="pill pill-fail">Fail: {safe_message}</span>'
            f'<p class="muted fix-hint">Fix: {safe_hint}</p>'
        )
    return f'<span class="pill pill-fail">Fail: {safe_message}</span>'


def _load_writes() -> list[dict[str, Any]]:
    try:
        rows = store_mod.list_writes(Config().db_path(), limit=50)
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "id": rec.id or 0,
            "operation": rec.operation,
            "item_id": rec.item_id,
            "evidence_quote": rec.evidence_quote,
            "status": rec.status,
            "detail": rec.detail,
        }
        for rec in rows
    ]


def _activity_events() -> list[dict[str, Any]]:
    writes = _load_writes()
    if writes:
        return writes
    return bus.history()


@app.get("/")
def health() -> dict[str, str]:
    """Health check."""
    return {"status": "ok"}


@app.get("/health")
def health_alias() -> dict[str, str]:
    """Health check alias."""
    return {"status": "ok"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Docker and K8s health endpoint. Public, no auth, no token, no data."""
    return {"status": "ok"}


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, token: str = "") -> HTMLResponse:
    ctx = _context(request, "setup")
    project = str(ctx["project"])
    backend = _project_backend(project)
    progress = _read_project_progress(project)
    try:
        cfg = Config()
        spend_cap = cfg.daily_spend_cap
        dry_run = cfg.dry_run
    except Exception:  # noqa: BLE001
        spend_cap = 0.0
        dry_run = True
    people_entry: dict[str, Any] = {}
    steps = progress.get("steps", {})
    if isinstance(steps, dict) and isinstance(steps.get("people"), dict):
        people_entry = dict(steps["people"])
    try:
        saved_provider = provider_mod.load_provider(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        saved_provider = {
            "provider": "meta",
            "base_url": "",
            "model": "",
            "has_key": False,
        }
    try:
        saved_meta = secrets_mod.load_meta_config(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        saved_meta = {"model": "", "base_url": "", "has_key": False}
    try:
        saved_slack = secrets_mod.load_slack_config(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        saved_slack = {"has_bot_token": False, "has_app_token": False}
    try:
        saved_teams = secrets_mod.load_teams_config(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        saved_teams = {
            "has_app_id": False,
            "has_app_password": False,
            "has_tenant_id": False,
            "has_refresh_token": False,
        }
    try:
        saved_ado = secrets_mod.load_ado_config(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        saved_ado = {"org_url": "", "project": "", "team": "", "has_pat": False}
    try:
        saved_jira = secrets_mod.load_jira_config(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        saved_jira = {"base_url": "", "email": "", "project": "", "has_token": False}
    try:
        saved_linear = secrets_mod.load_linear_config(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        saved_linear = {"team": "", "has_key": False}
    try:
        saved_trello = secrets_mod.load_trello_config(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        saved_trello = {"board": "", "has_key": False, "has_token": False}
    try:
        saved_clickup = secrets_mod.load_clickup_config(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        saved_clickup = {"team": "", "list": "", "has_token": False}
    try:
        saved_asana = secrets_mod.load_asana_config(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        saved_asana = {"project": "", "has_token": False}
    try:
        saved_monday = secrets_mod.load_monday_config(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001
        saved_monday = {"board": "", "has_token": False}
    ctx.update(
        {
            "token": token,
            "token_ok": is_valid_token(token) if token else False,
            "setup_done": SETUP_DONE,
            "backend": backend,
            "backend_label": backend_label(backend),
            "show_ado": backend == BACKEND_AZURE_DEVOPS,
            "show_jira": backend == BACKEND_JIRA,
            "show_linear": backend == BACKEND_LINEAR,
            "show_trello": backend == BACKEND_TRELLO,
            "show_clickup": backend == BACKEND_CLICKUP,
            "show_asana": backend == BACKEND_ASANA,
            "show_monday": backend == BACKEND_MONDAY,
            "progress": progress,
            "progress_json": json.dumps(progress),
            "wizard_order": WIZARD_ORDER,
            "spend_cap": spend_cap,
            "dry_run": dry_run,
            "people_names": people_entry.get("people", ""),
            "pam_board_value": BACKEND_PAM_BOARD,
            "ado_value": BACKEND_AZURE_DEVOPS,
            "jira_value": BACKEND_JIRA,
            "linear_value": BACKEND_LINEAR,
            "trello_value": BACKEND_TRELLO,
            "clickup_value": BACKEND_CLICKUP,
            "asana_value": BACKEND_ASANA,
            "monday_value": BACKEND_MONDAY,
            "autostart_label": autostart_mod.CHECKBOX_LABEL,
            "autostart_enabled": _autostart_enabled(),
            "provider": str(saved_provider.get("provider", "meta")),
            "provider_base_url": str(saved_provider.get("base_url", "") or ""),
            "provider_model": str(saved_provider.get("model", "") or ""),
            "provider_has_key": bool(saved_provider.get("has_key", False)),
            "meta_model": str(saved_meta.get("model", "") or ""),
            "meta_base_url": str(saved_meta.get("base_url", "") or ""),
            "meta_has_key": bool(saved_meta.get("has_key", False)),
            "slack_has_bot": bool(saved_slack.get("has_bot_token", False)),
            "slack_has_app": bool(saved_slack.get("has_app_token", False)),
            "teams_has_app_id": bool(saved_teams.get("has_app_id", False)),
            "teams_has_password": bool(saved_teams.get("has_app_password", False)),
            "teams_has_tenant": bool(saved_teams.get("has_tenant_id", False)),
            "teams_signed_in": bool(saved_teams.get("has_refresh_token", False)),
            "ado_org_url": str(saved_ado.get("org_url", "") or ""),
            "ado_project": str(saved_ado.get("project", "") or ""),
            "ado_team": str(saved_ado.get("team", "") or ""),
            "ado_has_pat": bool(saved_ado.get("has_pat", False)),
            "jira_base_url": str(saved_jira.get("base_url", "") or ""),
            "jira_email": str(saved_jira.get("email", "") or ""),
            "jira_project": str(saved_jira.get("project", "") or ""),
            "jira_has_token": bool(saved_jira.get("has_token", False)),
            "linear_team": str(saved_linear.get("team", "") or ""),
            "linear_has_key": bool(saved_linear.get("has_key", False)),
            "trello_board": str(saved_trello.get("board", "") or ""),
            "trello_has_key": bool(saved_trello.get("has_key", False)),
            "trello_has_token": bool(saved_trello.get("has_token", False)),
            "clickup_team": str(saved_clickup.get("team", "") or ""),
            "clickup_list": str(saved_clickup.get("list", "") or ""),
            "clickup_has_token": bool(saved_clickup.get("has_token", False)),
            "asana_project": str(saved_asana.get("project", "") or ""),
            "asana_has_token": bool(saved_asana.get("has_token", False)),
            "monday_board": str(saved_monday.get("board", "") or ""),
            "monday_has_token": bool(saved_monday.get("has_token", False)),
            "share_on_lan": _project_share_on_lan(project),
            "share_url": (
                get_share_url(project) if _project_share_on_lan(project) else ""
            ),
            "console_has_password": auth_mod.has_password(Config().ensure_data_dir()),
        }
    )
    return templates.TemplateResponse(request, "setup.html", ctx)  # type: ignore[return-value]


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "rules")
    ctx["rules"] = [
        {
            "name": item["name"],
            "title": item["title"],
            "description": item["description"],
            "trigger": item["trigger"],
            "enabled": RULE_STATES.get(str(item["name"]), False),
        }
        for item in RULE_TEMPLATES
    ]
    return templates.TemplateResponse(request, "rules.html", ctx)  # type: ignore[return-value]


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "activity")
    ctx["events"] = _activity_events()
    try:
        ctx["checkin_people"] = _people_names(ctx["project"])
    except Exception:  # noqa: BLE001
        ctx["checkin_people"] = []
    try:
        ctx["lifecycle"] = recovery_mod.list_lifecycle(_db_path(), limit=50)
    except Exception:  # noqa: BLE001
        ctx["lifecycle"] = []
    return templates.TemplateResponse(request, "activity.html", ctx)  # type: ignore[return-value]


BOARD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("new", "New"),
    ("active", "Active"),
    ("blocked", "Blocked"),
    ("done", "Done"),
)

BOARD_ITEM_FIELDS: tuple[str, ...] = (
    "state",
    "description",
    "tags",
    "due_date",
    "owner",
    "estimate",
    "remaining",
    "priority",
    "iteration_id",
)

BOARD_CSV_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "state",
    "owner",
    "estimate",
    "remaining",
    "priority",
    "tags",
    "due_date",
    "iteration_id",
    "description",
)

BOARD_REFRESH_OPS = frozenset(
    {
        "create",
        "update",
        "move",
        "add_link",
        "set_sprint",
        "create_sprint",
        "close_sprint",
        "comment",
    }
)

_BOARD_OVERRIDES: dict[str, Any] = {}
_NATIVE_BOARDS: dict[str, SQLiteBoard] = {}
_ADO_BOARD: Any | None = None
_JIRA_BOARD: Any | None = None
_LINEAR_BOARD: Any | None = None
_TRELLO_BOARD: Any | None = None
_CLICKUP_BOARD: Any | None = None
_ASANA_BOARD: Any | None = None
_MONDAY_BOARD: Any | None = None


def set_board_adapter(project: str, adapter: Any) -> None:
    """Pin a board adapter for one project (tests use fakes here)."""
    _BOARD_OVERRIDES[_safe_project(project)] = adapter


def reset_board_state() -> None:
    """Clear pinned adapters and cached boards (tests call this)."""
    global \
        _ADO_BOARD, \
        _JIRA_BOARD, \
        _LINEAR_BOARD, \
        _TRELLO_BOARD, \
        _CLICKUP_BOARD, \
        _ASANA_BOARD, \
        _MONDAY_BOARD
    _BOARD_OVERRIDES.clear()
    for board in _NATIVE_BOARDS.values():
        try:
            board.close()
        except Exception:  # noqa: BLE001, S110
            pass
    _NATIVE_BOARDS.clear()
    for cached in (
        _ADO_BOARD,
        _JIRA_BOARD,
        _LINEAR_BOARD,
        _TRELLO_BOARD,
        _CLICKUP_BOARD,
        _ASANA_BOARD,
        _MONDAY_BOARD,
    ):
        if cached is not None:
            try:
                cached.close()
            except Exception:  # noqa: BLE001, S110
                pass
    _ADO_BOARD = None
    _JIRA_BOARD = None
    _LINEAR_BOARD = None
    _TRELLO_BOARD = None
    _CLICKUP_BOARD = None
    _ASANA_BOARD = None
    _MONDAY_BOARD = None


def _safe_project(value: str) -> str:
    want = (value or "default").strip() or "default"
    if len(want) <= 64 and all(c.isalnum() or c in ("-", "_", ".") for c in want):
        return want
    return "default"


def get_board_adapter(project: str) -> Any | None:
    """Return the board adapter for a project, or None when missing.

    Native projects get a per-project SQLite board file. Other
    backends share one client each, or None when not configured.
    """
    global \
        _ADO_BOARD, \
        _JIRA_BOARD, \
        _LINEAR_BOARD, \
        _TRELLO_BOARD, \
        _CLICKUP_BOARD, \
        _ASANA_BOARD, \
        _MONDAY_BOARD
    name = _safe_project(project)
    if name in _BOARD_OVERRIDES:
        return _BOARD_OVERRIDES[name]
    backend = _project_backend(name)
    if backend == BACKEND_PAM_BOARD:
        if name not in _NATIVE_BOARDS:
            path = Config().ensure_data_dir() / f"board_{name}.db"
            _NATIVE_BOARDS[name] = SQLiteBoard(path)
        return _NATIVE_BOARDS[name]
    if backend == BACKEND_JIRA:
        if _JIRA_BOARD is None:
            try:
                from pam_pm.adapters.pm.jira import JiraAdapter

                _JIRA_BOARD = JiraAdapter()
            except Exception:  # noqa: BLE001
                return None
        return _JIRA_BOARD
    if backend == BACKEND_LINEAR:
        if _LINEAR_BOARD is None:
            try:
                from pam_pm.adapters.pm.linear import LinearAdapter

                _LINEAR_BOARD = LinearAdapter()
            except Exception:  # noqa: BLE001
                return None
        return _LINEAR_BOARD
    if backend == BACKEND_TRELLO:
        if _TRELLO_BOARD is None:
            try:
                from pam_pm.adapters.pm.trello import TrelloAdapter

                _TRELLO_BOARD = TrelloAdapter()
            except Exception:  # noqa: BLE001
                return None
        return _TRELLO_BOARD
    if backend == BACKEND_CLICKUP:
        if _CLICKUP_BOARD is None:
            try:
                from pam_pm.adapters.pm.clickup import ClickUpAdapter

                _CLICKUP_BOARD = ClickUpAdapter()
            except Exception:  # noqa: BLE001
                return None
        return _CLICKUP_BOARD
    if backend == BACKEND_ASANA:
        if _ASANA_BOARD is None:
            try:
                from pam_pm.adapters.pm.asana import AsanaAdapter

                _ASANA_BOARD = AsanaAdapter()
            except Exception:  # noqa: BLE001
                return None
        return _ASANA_BOARD
    if backend == BACKEND_MONDAY:
        if _MONDAY_BOARD is None:
            try:
                from pam_pm.adapters.pm.monday import MondayAdapter

                _MONDAY_BOARD = MondayAdapter()
            except Exception:  # noqa: BLE001
                return None
        return _MONDAY_BOARD
    if _ADO_BOARD is None:
        try:
            from pam_pm.adapters.pm.azure_devops import ADOAdapter

            _ADO_BOARD = ADOAdapter()
        except Exception:  # noqa: BLE001
            return None
    return _ADO_BOARD


_CHAT_OVERRIDES: dict[str, Any] = {}
_MODEL_OVERRIDES: dict[str, Any] = {}


def set_chat_adapter(project: str, adapter: Any) -> None:
    """Pin a chat adapter for a project (tests and local runs)."""
    _CHAT_OVERRIDES[_safe_project(project)] = adapter


def set_model_client(project: str, client: Any) -> None:
    """Pin a model client for a project (tests and local runs)."""
    _MODEL_OVERRIDES[_safe_project(project)] = client


def reset_chat_state() -> None:
    """Clear pinned chat and model clients."""
    _CHAT_OVERRIDES.clear()
    _MODEL_OVERRIDES.clear()


def get_chat_adapter(project: str) -> Any | None:
    """Return the chat adapter for a project, or None when missing."""
    name = _safe_project(project)
    if name in _CHAT_OVERRIDES:
        return _CHAT_OVERRIDES[name]
    try:
        from pam_pm.adapters.chat.slack import SlackAdapter

        return SlackAdapter()
    except Exception:  # noqa: BLE001
        return None


def get_model_client(project: str) -> Any | None:
    """Return the pinned model client for a project, if any."""
    return _MODEL_OVERRIDES.get(_safe_project(project))


def _board_capabilities(project: str, adapter: Any | None) -> dict[str, bool]:
    native = _project_backend(project) == BACKEND_PAM_BOARD
    return {
        "editable": bool(native and adapter is not None),
        "dependency_links": bool(getattr(adapter, "supports_dependency_links", False)),
        "iterations": bool(getattr(adapter, "supports_iterations", False)),
        "comments": bool(getattr(adapter, "supports_comments", False)),
        "close_iteration": bool(getattr(adapter, "supports_close_iteration", False)),
    }


def _board_column(state: str) -> str:
    text = (state or "").strip().lower()
    if text == "blocked":
        return "blocked"
    if is_closed(state):
        return "done"
    if text in (
        "active",
        "doing",
        "in progress",
        "inprogress",
        "committed",
        "resolved",
    ):
        return "active"
    return "new"


def _pick(fields: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in fields and fields[key] not in (None, ""):
            return fields[key]
    return default


def _tags_list(fields: dict[str, Any]) -> list[str]:
    raw = _pick(fields, "tags", "System.Tags", default="")
    if isinstance(raw, list):
        return [str(tag).strip() for tag in raw if str(tag).strip()]
    if isinstance(raw, str):
        return [
            part
            for part in (p.strip() for p in raw.replace(",", ";").split(";"))
            if part
        ]
    return []


def _ado_board_url() -> str:
    org = os.environ.get("ADO_ORG_URL", "").strip().rstrip("/")
    proj = os.environ.get("ADO_PROJECT", "").strip()
    if not org or not proj:
        return ""
    return f"{org}/{proj}/_boards"


def _ado_item_url(item_id: str) -> str:
    base = _ado_board_url()
    if not base:
        return ""
    org = os.environ.get("ADO_ORG_URL", "").strip().rstrip("/")
    proj = os.environ.get("ADO_PROJECT", "").strip()
    return f"{org}/{proj}/_workitems/edit/{item_id}"


def _external_board_url(backend: str) -> str:
    if backend == BACKEND_JIRA:
        try:
            base = secrets_mod.effective_jira_config().get("base_url", "")
        except Exception:  # noqa: BLE001
            base = os.environ.get("JIRA_BASE_URL", "")
        base = str(base or "").strip().rstrip("/")
        return f"{base}/projects" if base else ""
    if backend == BACKEND_LINEAR:
        return "https://linear.app"
    if backend == BACKEND_TRELLO:
        return "https://trello.com"
    if backend == BACKEND_CLICKUP:
        return "https://app.clickup.com"
    if backend == BACKEND_ASANA:
        return "https://app.asana.com"
    if backend == BACKEND_MONDAY:
        return "https://monday.com"
    return _ado_board_url()


def _external_item_url(backend: str, item: WorkItem) -> str:
    if backend == BACKEND_JIRA:
        try:
            base = secrets_mod.effective_jira_config().get("base_url", "")
        except Exception:  # noqa: BLE001
            base = os.environ.get("JIRA_BASE_URL", "")
        base = str(base or "").strip().rstrip("/")
        if not base:
            return ""
        fields = dict(getattr(item, "fields", {}) or {})
        key = str(fields.get("key", "") or item.id).strip()
        return f"{base}/browse/{key}" if key else ""
    if backend == BACKEND_LINEAR:
        fields = dict(getattr(item, "fields", {}) or {})
        return str(fields.get("url", "") or "")
    if backend == BACKEND_TRELLO:
        fields = dict(getattr(item, "fields", {}) or {})
        return str(fields.get("short_url", "") or fields.get("url", "") or "")
    if backend == BACKEND_CLICKUP:
        fields = dict(getattr(item, "fields", {}) or {})
        return str(fields.get("url", "") or "")
    if backend == BACKEND_ASANA:
        fields = dict(getattr(item, "fields", {}) or {})
        return str(fields.get("permalink_url", "") or fields.get("url", "") or "")
    if backend == BACKEND_MONDAY:
        fields = dict(getattr(item, "fields", {}) or {})
        return str(fields.get("url", "") or "")
    return _ado_item_url(str(item.id))


def _serialize_item(
    item: WorkItem,
    iteration_names: dict[str, str] | None = None,
    external_url: str = "",
) -> dict[str, Any]:
    fields = dict(getattr(item, "fields", {}) or {})
    names = iteration_names or {}
    iteration_id = str(_pick(fields, "iteration_id", default=""))
    path = str(_pick(fields, "System.IterationPath", default=""))
    sprint = names.get(iteration_id, "")
    if not sprint and path:
        sprint = path.replace("/", "\\").rsplit("\\", 1)[-1]
    if not sprint:
        sprint = iteration_id
    tags = _tags_list(fields)
    state = str(getattr(item, "state", "") or "")
    return {
        "id": str(item.id),
        "title": str(getattr(item, "title", "") or fields.get("title", "")),
        "state": state,
        "column": _board_column(state),
        "description": str(_pick(fields, "description", "System.Description")),
        "tags": tags,
        "tags_text": "; ".join(tags),
        "due_date": str(_pick(fields, "due_date", "Microsoft.VSTS.Scheduling.DueDate")),
        "owner": str(_pick(fields, "owner", "assigned_to", "System.AssignedTo")),
        "estimate": _pick(
            fields, "estimate", "Microsoft.VSTS.Scheduling.OriginalEstimate"
        ),
        "remaining": _pick(
            fields, "remaining", "Microsoft.VSTS.Scheduling.RemainingWork"
        ),
        "priority": str(_pick(fields, "priority", "Microsoft.VSTS.Common.Priority")),
        "iteration_id": iteration_id,
        "sprint": sprint,
        "changed": str(getattr(item, "changed", "") or ""),
        "external_url": external_url,
    }


def _link_label(source_id: str, target_id: str, rel: str) -> str:
    if rel.endswith("Dependency-Reverse"):
        return f"{source_id} waits on {target_id}"
    return f"{source_id} blocks {target_id}"


def _board_iterations(adapter: Any) -> list[dict[str, str]]:
    getter = getattr(adapter, "list_iterations", None)
    if not callable(getter):
        return []
    try:
        found = getter()
    except Exception:  # noqa: BLE001
        return []
    closed: set[str] = set()
    closer = getattr(adapter, "closed_iteration_ids", None)
    if callable(closer):
        try:
            closed = {str(i) for i in closer()}
        except Exception:  # noqa: BLE001
            closed = set()
    out: list[dict[str, str]] = []
    for entry in found or []:
        ident = str(getattr(entry, "id", ""))
        out.append(
            {
                "id": ident,
                "name": str(getattr(entry, "name", "")),
                "start_date": str(getattr(entry, "start_date", "") or ""),
                "end_date": str(getattr(entry, "end_date", "") or ""),
                "path": str(getattr(entry, "path", "") or ""),
                "status": "closed" if ident in closed else "open",
            }
        )
    return out


def _board_links(adapter: Any, items: list[WorkItem]) -> list[dict[str, str]]:
    if not bool(getattr(adapter, "supports_dependency_links", False)):
        return []
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, str]] = []
    for item in items:
        try:
            found = adapter.list_links(item.id)
        except Exception:  # noqa: BLE001, S112
            continue
        for link in found or []:
            key = (
                str(link.source_id),
                str(link.target_id),
                str(link.rel),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "source_id": key[0],
                    "target_id": key[1],
                    "rel": key[2],
                    "label": _link_label(key[0], key[1], key[2]),
                }
            )
    return out


def _record_board_write(
    project: str,
    operation: str,
    item_id: str,
    evidence: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {"actor": "human", "project": project}
    if extra:
        detail.update(extra)
    try:
        write_id = store_mod.insert_write(
            _db_path(),
            {
                "operation": operation,
                "item_id": str(item_id),
                "evidence_quote": evidence,
                "status": "applied",
                "detail": json.dumps(detail),
            },
        )
    except Exception:  # noqa: BLE001
        past = bus.history()
        write_id = int(past[-1].get("id", 0)) + 1 if past else 1
    event = {
        "id": write_id,
        "operation": operation,
        "item_id": str(item_id),
        "evidence_quote": evidence,
        "status": "applied",
        "detail": json.dumps(detail),
    }
    bus.publish(event)
    logger.info("board write: %s %s on %s", operation, item_id, project)
    return event


def _board_snapshot(project: str) -> dict[str, Any]:
    """Read items, sprints, and links for one project, never raising."""
    name = _safe_project(project)
    backend = _project_backend(name)
    adapter = get_board_adapter(name)
    caps = _board_capabilities(name, adapter)
    snapshot: dict[str, Any] = {
        "project": name,
        "backend": backend,
        "backend_label": backend_label(backend),
        "capabilities": caps,
        "editable": caps["editable"],
        "states": [],
        "iterations": [],
        "items": [],
        "links": [],
        "error": "",
    }
    if adapter is None:
        if backend == BACKEND_JIRA:
            snapshot["error"] = (
                "Jira is not configured yet. Set JIRA_BASE_URL, JIRA_EMAIL, "
                "JIRA_PROJECT, and JIRA_API_TOKEN, then reload."
            )
        elif backend == BACKEND_LINEAR:
            snapshot["error"] = (
                "Linear is not configured yet. Set LINEAR_TEAM and "
                "LINEAR_API_KEY, then reload."
            )
        elif backend == BACKEND_TRELLO:
            snapshot["error"] = (
                "Trello is not configured yet. Set TRELLO_BOARD, "
                "TRELLO_API_KEY, and TRELLO_TOKEN, then reload."
            )
        elif backend == BACKEND_CLICKUP:
            snapshot["error"] = (
                "ClickUp is not configured yet. Set CLICKUP_LIST and "
                "CLICKUP_API_TOKEN, then reload."
            )
        elif backend == BACKEND_ASANA:
            snapshot["error"] = (
                "Asana is not configured yet. Set ASANA_PROJECT and "
                "ASANA_API_TOKEN, then reload."
            )
        elif backend == BACKEND_MONDAY:
            snapshot["error"] = (
                "Monday is not configured yet. Set MONDAY_BOARD and "
                "MONDAY_API_TOKEN, then reload."
            )
        else:
            snapshot["error"] = (
                "Azure DevOps is not configured yet. Set ADO_ORG_URL, "
                "ADO_PROJECT, and ADO_PAT, then reload."
            )
        return snapshot
    try:
        raw_items = adapter.list_items(limit=500)
    except Exception as exc:  # noqa: BLE001
        logger.warning("board read failed: %s", exc)
        snapshot["error"] = "The board could not be loaded. Try reloading the page."
        return snapshot
    raw_items = list(raw_items or [])
    try:
        snapshot["states"] = [str(s) for s in adapter.list_states()]
    except Exception:  # noqa: BLE001
        snapshot["states"] = []
    iterations = _board_iterations(adapter) if caps["iterations"] else []
    snapshot["iterations"] = iterations
    names = {entry["id"]: entry["name"] for entry in iterations}
    native = backend == BACKEND_PAM_BOARD
    cards = []
    for item in raw_items:
        url = "" if native else _external_item_url(backend, item)
        cards.append(_serialize_item(item, names, url))
    snapshot["items"] = cards
    snapshot["links"] = _board_links(adapter, raw_items)
    return snapshot


@app.get("/board", response_class=HTMLResponse)
def board_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "board")
    project = str(ctx["project"])
    snap = _board_snapshot(project)
    cards: list[dict[str, Any]] = snap["items"]
    counts: dict[str, int] = {}
    for link in snap["links"]:
        for end in (link["source_id"], link["target_id"]):
            counts[end] = counts.get(end, 0) + 1
    for card in cards:
        card["link_count"] = counts.get(str(card["id"]), 0)
    ctx["columns"] = [
        {
            "key": key,
            "title": title,
            "cards": [c for c in cards if c["column"] == key],
        }
        for key, title in BOARD_COLUMNS
    ]
    ctx["links"] = snap["links"]
    ctx["iterations"] = snap["iterations"]
    ctx["states"] = snap["states"] or ["New", "Active", "Blocked", "Done"]
    ctx["editable"] = snap["editable"]
    ctx["capabilities"] = snap["capabilities"]
    ctx["backend"] = snap["backend"]
    ctx["board_error"] = snap["error"]
    ctx["backend_label"] = snap["backend_label"]
    ctx["external_board_url"] = (
        "" if snap["editable"] else _external_board_url(snap["backend"])
    )
    ctx["backlog"] = [c for c in cards if not c["iteration_id"]]
    sprints = []
    for entry in snap["iterations"]:
        sprints.append(
            {
                **entry,
                "cards": [c for c in cards if c["iteration_id"] == entry["id"]],
            }
        )
    ctx["sprints"] = sprints
    return templates.TemplateResponse(request, "board.html", ctx)  # type: ignore[return-value]


@app.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "plan")
    ctx["plan"] = LAST_PLAN
    if LAST_PLAN.get("proposal") or LAST_PLAN.get("readings") or LAST_PLAN.get("masks"):
        photo = {
            "masks": LAST_PLAN.get("masks", []),
            "readings": LAST_PLAN.get("readings", []),
            "path": LAST_PLAN.get("path", ""),
        }
        ctx["plan_html"] = _plan_result_html(photo, LAST_PLAN.get("proposal", []))
    else:
        ctx["plan_html"] = ""
    return templates.TemplateResponse(request, "plan.html", ctx)  # type: ignore[return-value]


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "review")
    ctx["review_markdown"] = LAST_REVIEW.get("markdown", "")
    ctx["review_html"] = _markdown_to_html(LAST_REVIEW.get("markdown", ""))
    ctx["retro_actions"] = LAST_REVIEW.get("retro_actions", [])
    return templates.TemplateResponse(request, "review.html", ctx)  # type: ignore[return-value]


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "settings")
    project = str(ctx["project"])
    dry_run, spend_cap = _effective_safety()
    entries = _roster_entries(project)
    cost = _cost_summary()
    try:
        memory_people = memory_mod.list_people(_db_path())
        memory_life = memory_mod.get_lifecycle(_db_path(), project)
        memory_bias = memory_mod.bias_factor(_db_path(), project)
    except Exception:  # noqa: BLE001
        memory_people = []
        memory_life = {"throughput": {}, "style_note": ""}
        memory_bias = 1.0
    ctx.update(
        {
            "people_text": "\n".join(entry["name"] for entry in entries),
            "roster": entries,
            "dry_run": dry_run,
            "spend_cap": spend_cap,
            "cost": cost,
            "cost_dollars": f"{cost['dollars']:.4f}",
            "memory_people": memory_people,
            "memory_throughput": dict(memory_life.get("throughput", {})),
            "memory_bias": memory_bias,
            "memory_style_note": str(memory_life.get("style_note", "") or ""),
            "poll_interval": _effective_poll_interval(),
        }
    )
    return templates.TemplateResponse(request, "settings.html", ctx)  # type: ignore[return-value]


@app.post("/api/settings/roster")
async def save_settings_roster(request: Request) -> Any:
    project = request.query_params.get("project", "default").strip() or "default"
    people_text = ""
    rows: list[dict[str, Any]] = []
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            project = str(payload.get("project", project)).strip() or "default"
            raw_entries = payload.get("entries", [])
            if isinstance(raw_entries, list):
                rows = [dict(item) for item in raw_entries if isinstance(item, dict)]
            if "people" in payload:
                people_text = str(payload.get("people", ""))
            elif rows:
                people_text = "\n".join(str(row.get("name", "")) for row in rows)
    else:
        form = await request.form()
        if "project" in form:
            project = str(form.get("project", project)).strip() or "default"
        people_text = str(form.get("people", ""))
        names = form.getlist("name")
        skills = form.getlist("skills")
        capacities = form.getlist("capacity")
        for index, label in enumerate(names):
            rows.append(
                {
                    "name": str(label),
                    "skills": str(skills[index]) if index < len(skills) else "",
                    "capacity": str(capacities[index])
                    if index < len(capacities)
                    else 0,
                }
            )
        if not people_text.strip() and names:
            people_text = "\n".join(str(label) for label in names)
    entries = _merge_roster_entries(people_text, rows)
    _write_project_step(project, "roster", {"entries": entries})
    _write_project_step(
        project, "people", {"people": "\n".join(e["name"] for e in entries)}
    )
    word = "teammate" if len(entries) == 1 else "teammates"
    message = f"Saved team with {len(entries)} {word}."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "entries": entries}


@app.post("/api/settings/safety")
async def save_settings_safety(request: Request) -> Any:
    dry_raw: Any = None
    cap_raw: Any = None
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            dry_raw = payload.get("dry_run")
            cap_raw = payload.get("spend_cap", payload.get("daily_spend_cap"))
    else:
        form = await request.form()
        dry_raw = form.get("dry_run") if "dry_run" in form else "off"
        cap_raw = form.get("spend_cap", "")
    current_dry, _current_cap = _effective_safety()
    if dry_raw is None:
        dry_run = current_dry
    else:
        dry_run = _autostart_choice(dry_raw, default=True)
    try:
        spend_cap = float(str(cap_raw).strip())
    except (TypeError, ValueError):
        spend_cap = 0.0
    if spend_cap <= 0:
        msg = "Spend cap must be a number above zero."
        hint = "Type a cap such as 2.0, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=400
        )
    try:
        recovery_mod.kv_set(_db_path(), SAFETY_DRY_RUN_KEY, "on" if dry_run else "off")
        recovery_mod.kv_set(_db_path(), SAFETY_SPEND_CAP_KEY, str(spend_cap))
    except Exception:  # noqa: BLE001
        msg = "Safety settings could not be saved."
        hint = "Check the data directory is writable, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    mode = "on" if dry_run else "off"
    message = f"Safety saved. Dry run is {mode} and spend cap is {spend_cap}."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "dry_run": dry_run, "spend_cap": spend_cap}


@app.post("/api/settings/poll-interval")
async def save_poll_interval(request: Request) -> Any:
    raw: Any = None
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            raw = payload.get("poll_interval", payload.get("interval"))
    else:
        form = await request.form()
        raw = form.get("poll_interval", form.get("interval", ""))
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        seconds = 0
    if seconds < 1:
        msg = "Poll interval must be a whole number of seconds, at least 1."
        hint = "Type a value such as 10, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=400
        )
    try:
        recovery_mod.kv_set(_db_path(), POLL_INTERVAL_KEY, str(seconds))
    except Exception:  # noqa: BLE001
        msg = "Poll interval could not be saved."
        hint = "Check the data directory is writable, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    message = (
        f"Poll interval saved. The board poller checks every {seconds} seconds."
    )
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "poll_interval": seconds}


@app.post("/api/checkin/test")
async def test_checkin_route(request: Request) -> Any:
    project = request.query_params.get("project", "default").strip() or "default"
    person = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            project = str(payload.get("project", project)).strip() or "default"
            person = str(payload.get("person", payload.get("teammate", "")))
    else:
        form = await request.form()
        if "project" in form:
            project = str(form.get("project", project)).strip() or "default"
        person = str(form.get("person", form.get("teammate", "")))
    person = person.strip()
    if not person:
        try:
            names = _people_names(project)
        except Exception:  # noqa: BLE001
            names = []
        person = names[0] if names else ""
    if not person:
        msg = "Choose a teammate for the test check-in."
        hint = "Type a teammate name, then press Send test check-in again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=400
        )
    chat = get_chat_adapter(project)
    board = get_board_adapter(project)
    if chat is None or board is None:
        msg = "Chat or board is not connected for this project."
        hint = "Finish the Setup steps for Slack and the board, then try again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=400
        )
    model = get_model_client(project)
    if model is None:
        try:
            from pam_pm.models.client import ModelClient

            candidate = ModelClient()
            if candidate.model and candidate.base_url and candidate._api_key:
                model = candidate
        except Exception:  # noqa: BLE001
            model = None
    if model is None:
        try:
            mid = chat.send_dm(
                person, "Morning check-in: what is the status of your open work?"
            )
        except Exception:  # noqa: BLE001
            msg = "The test check-in could not be sent."
            hint = "Check the chat connection, then try again."
            if "hx-request" in request.headers:
                return HTMLResponse(_test_result_html(False, msg, hint))
            return JSONResponse(
                {"ok": False, "message": msg, "hint": hint}, status_code=500
            )
        bus.publish(
            {
                "id": len(bus.history()) + 1,
                "operation": "test_checkin",
                "item_id": person,
                "evidence_quote": "Console test check-in sent.",
                "status": "applied",
                "detail": f"Test check-in sent to {person}.",
            }
        )
        message = f"Test check-in sent to {person}."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(True, message))
        return {"ok": True, "person": person, "dm_id": mid, "fallback": True}
    from pam_pm.rules.templates.morning import test_checkin

    dry_run, _cap = _effective_safety()
    try:
        out = test_checkin(
            person,
            chat,
            board,
            model,
            project=project,
            db=_db_path(),
            dry_run=dry_run,
            backend=_project_backend(project),
        )
    except Exception as exc:  # noqa: BLE001
        msg = "The test check-in did not finish."
        hint = f"Detail: {exc}"
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    results = out.get(person, {})
    count = len(results) if isinstance(results, dict) else 0
    bus.publish(
        {
            "id": len(bus.history()) + 1,
            "operation": "test_checkin",
            "item_id": person,
            "evidence_quote": "Console test check-in sent.",
            "status": "applied",
            "detail": f"Test check-in sent to {person} ({count} items).",
        }
    )
    message = f"Test check-in sent to {person} ({count} items)."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "person": person, "items": count}


@app.get("/api/settings/memory")
async def view_settings_memory(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    people = memory_mod.list_people(_db_path())
    life = memory_mod.get_lifecycle(_db_path(), project)
    return {
        "ok": True,
        "project": project,
        "people": people,
        "lifecycle": {
            "throughput": dict(life.get("throughput", {})),
            "bias": memory_mod.bias_factor(estimates=life.get("estimates", [])),
            "style_note": str(life.get("style_note", "") or ""),
        },
    }


@app.post("/api/settings/memory/person")
async def save_settings_memory_person(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    entries: list[dict[str, Any]] = []
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            project = str(payload.get("project", project)).strip() or project
            raw = payload.get("entries", [])
            if isinstance(raw, list) and raw:
                entries = [dict(item) for item in raw if isinstance(item, dict)]
            elif str(payload.get("name", "")).strip():
                entries = [dict(payload)]
    else:
        form = await request.form()
        if "project" in form:
            project = str(form.get("project", project)).strip() or project
        names = form.getlist("name")
        roles = form.getlist("role")
        skills = form.getlist("skills")
        capacities = form.getlist("capacity")
        for index, label in enumerate(names):
            entries.append(
                {
                    "name": str(label),
                    "role": str(roles[index]) if index < len(roles) else "",
                    "skills": str(skills[index]) if index < len(skills) else "",
                    "capacity": str(capacities[index])
                    if index < len(capacities)
                    else 0,
                }
            )
    saved: list[dict[str, Any]] = []
    for item in entries:
        label = str(item.get("name", "")).strip()
        if not label:
            continue
        try:
            hours = float(
                str(item.get("capacity", item.get("weekly_capacity", 0)) or 0)
            )
        except (TypeError, ValueError):
            hours = 0.0
        try:
            saved.append(
                memory_mod.upsert_person(
                    _db_path(),
                    label,
                    role=str(item.get("role", "") or "") or None,
                    skillsets=str(item.get("skills", item.get("skillsets", "")) or "")
                    or None,
                    weekly_capacity=hours if hours > 0 else None,
                    source="settings",
                )
            )
        except ValueError:
            continue
    word = "teammate" if len(saved) == 1 else "teammates"
    message = f"Saved {len(saved)} {word} to team memory."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "people": saved}


@app.post("/api/settings/memory/person/delete")
async def delete_settings_memory_person(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    name = request.query_params.get("name", "").strip()
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and str(payload.get("name", "")).strip():
            name = str(payload.get("name", "")).strip()
    else:
        form = await request.form()
        if str(form.get("name", "")).strip():
            name = str(form.get("name", "")).strip()
    if not name:
        msg = "Name is required to delete a teammate."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    removed = memory_mod.delete_person(_db_path(), name)
    if not removed:
        msg = f"No teammate named {name} was found."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=404)
    message = f"Removed {name} from team memory."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "name": name}


@app.delete("/api/settings/memory/person")
async def remove_settings_memory_person(request: Request) -> Any:
    return await delete_settings_memory_person(request)


@app.post("/api/settings/memory/clear")
async def clear_settings_memory(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    confirmed = False
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            confirmed = payload.get("confirm", False) in (
                True,
                "on",
                "true",
                "1",
                1,
                "yes",
            )
    else:
        form = await request.form()
        confirmed = str(form.get("confirm", "")).lower() in (
            "on",
            "true",
            "1",
            "yes",
        )
    if not confirmed:
        msg = "Tick the confirm box to clear all of team memory."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    count = memory_mod.clear_people(_db_path())
    word = "teammate" if count == 1 else "teammates"
    message = f"Cleared {count} {word} from team memory."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "cleared": count}


@app.post("/api/settings/memory/style")
async def save_settings_memory_style(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    note = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            project = str(payload.get("project", project)).strip() or project
            note = str(payload.get("style_note", payload.get("note", "")) or "")
    else:
        form = await request.form()
        if "project" in form:
            project = str(form.get("project", project)).strip() or project
        note = str(form.get("style_note", form.get("note", "") or "") or "")
    life = memory_mod.save_style_note(_db_path(), project, note)
    message = "Saved the lifecycle style note."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "style_note": life.get("style_note", "")}


@app.post("/api/test/{which}")
async def test_connection(which: str, request: Request) -> Any:
    name = (which or "").strip().lower()
    project = request.query_params.get("project", "default").strip() or "default"
    if name in ("meta", "model"):
        ok, message, hint = _check_meta()
    elif name in ("provider", "custom"):
        ok, message, hint = _check_provider()
    elif name == "slack":
        ok, message, hint = _check_slack()
    elif name == "teams":
        ok, message, hint = _check_teams()
    elif name == "ado":
        ok, message, hint = _check_ado()
    elif name == "jira":
        ok, message, hint = _check_jira()
    elif name == "linear":
        ok, message, hint = _check_linear()
    elif name == "trello":
        ok, message, hint = _check_trello()
    elif name == "clickup":
        ok, message, hint = _check_clickup()
    elif name == "asana":
        ok, message, hint = _check_asana()
    elif name == "monday":
        ok, message, hint = _check_monday()
    elif name == "people":
        ok, message, hint = _check_people(project)
    elif name == "rules":
        ok, message, hint = _check_rules()
    elif name == "safety":
        ok, message, hint = _check_safety()
    elif name == "backend":
        ok, message, hint = _check_backend(project)
    else:
        return JSONResponse(
            {
                "ok": False,
                "message": "Unknown target.",
                "hint": "Use one of: model, slack, teams, ado, jira, linear, trello, clickup, asana, monday, people, rules, safety.",
            },
            status_code=404,
        )
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(ok, message, hint))
    return {"ok": ok, "message": message, "hint": hint}


@app.get("/api/setup/progress")
def get_setup_progress(request: Request) -> dict[str, Any]:
    project = request.query_params.get("project", "default").strip() or "default"
    return {"ok": True, "project": project, "progress": _read_project_progress(project)}


@app.post("/api/setup/progress")
async def save_setup_progress(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "").strip()
    step = request.query_params.get("step", "").strip()
    data: dict[str, Any] = {}
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            project = (
                str(payload.get("project", project or "default")).strip() or "default"
            )
            step = str(payload.get("step", step)).strip()
            raw_data = payload.get("data", {})
            if isinstance(raw_data, dict):
                data = {str(k): v for k, v in raw_data.items()}
            for key in ("people", "names", "notes"):
                if key in payload and key not in data:
                    data[key] = payload[key]
    else:
        form = await request.form()
        project = str(form.get("project", project or "default")).strip() or "default"
        step = str(form.get("step", step)).strip()
        for key in ("people", "names", "notes", "backend"):
            if key in form:
                data[key] = str(form.get(key, ""))
    if not step:
        return JSONResponse(
            {"ok": False, "message": "Step name is missing.", "hint": "Try again."},
            status_code=400,
        )
    entry = _write_project_step(project, step, data)
    if "hx-request" in request.headers:
        return HTMLResponse(
            _test_result_html(
                True,
                f"Step {step} saved. Your progress is kept if you leave and come back.",
            )
        )
    return {"ok": True, "project": project, "progress": entry}


@app.post("/api/setup/backend")
async def save_setup_backend(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    backend = request.query_params.get("backend", "").strip()
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            project = str(payload.get("project", project)).strip() or "default"
            backend = str(payload.get("backend", backend)).strip()
    else:
        form = await request.form()
        if "project" in form:
            project = str(form.get("project", project)).strip() or "default"
        if "backend" in form:
            backend = str(form.get("backend", backend)).strip()
    canonical = normalize_backend(backend)
    try:
        stored = store_mod.set_project_backend(_db_path(), project, canonical)
    except Exception:  # noqa: BLE001
        stored = canonical
    try:
        _write_project_step(project, "backend", {"backend": stored})
    except Exception:  # noqa: BLE001
        msg = "That choice could not be saved."
        hint = "Check your connection and try again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    label = backend_label(stored)
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, f"Tasks will live on {label}."))
    return {"ok": True, "project": project, "backend": stored, "label": label}


@app.get("/api/setup/share")
def get_setup_share(request: Request) -> dict[str, Any]:
    project = request.query_params.get("project", "default").strip() or "default"
    enabled = _project_share_on_lan(project)
    try:
        has_pw = auth_mod.has_password(Config().data_dir)
    except Exception:  # noqa: BLE001
        has_pw = False
    return {
        "ok": True,
        "project": project,
        "share_on_lan": enabled,
        "share_url": get_share_url(project) if enabled else "",
        "has_password": has_pw,
    }


@app.post("/api/setup/share")
async def save_setup_share(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    enabled_raw: Any = request.query_params.get("enabled", "")
    password = ""
    confirm = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            project = str(payload.get("project", project)).strip() or "default"
            if "enabled" in payload:
                enabled_raw = payload.get("enabled")
            elif "share_on_lan" in payload:
                enabled_raw = payload.get("share_on_lan")
            password = str(payload.get("password", "") or "")
            confirm = str(payload.get("confirm", "") or "")
    else:
        form = await request.form()
        if "project" in form:
            project = str(form.get("project", project)).strip() or "default"
        if "enabled" in form:
            enabled_raw = str(form.get("enabled", ""))
        password = str(form.get("password", "") or "")
        confirm = str(form.get("confirm", "") or "")
    if isinstance(enabled_raw, bool):
        enabled = enabled_raw
    else:
        enabled = str(enabled_raw).strip().lower() in ("1", "true", "yes", "on")
    data_dir = Config().ensure_data_dir()
    password_set = auth_mod.has_password(data_dir)
    new_session = ""
    if enabled:
        if password.strip():
            if not confirm.strip():
                return JSONResponse(
                    {
                        "ok": False,
                        "message": "Type the console password twice to confirm it.",
                    },
                    status_code=400,
                )
            if password.strip() != confirm.strip():
                return JSONResponse(
                    {
                        "ok": False,
                        "message": "The two passwords did not match. Try again.",
                    },
                    status_code=400,
                )
            try:
                auth_mod.set_password(password, data_dir)
            except ValueError as exc:
                return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
            password_set = True
            new_session = auth_mod.create_session()
            auth_mod.record_login_success(_client_ip(request))
        if not password_set:
            return JSONResponse(
                {
                    "ok": False,
                    "message": (
                        "Set a console password to share this board. "
                        "Anyone on your network with the link needs the "
                        "password to get in."
                    ),
                },
                status_code=400,
            )
    try:
        stored = store_mod.set_project_share_on_lan(_db_path(), project, enabled)
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "message": "Sharing choice could not be saved. Try again."},
            status_code=500,
        )
    try:
        _write_project_step(project, "share", {"share_on_lan": stored})
    except Exception:  # noqa: BLE001, S110
        pass
    if "hx-request" in request.headers:
        note = "Sharing is on." if stored else "Sharing is off."
        return HTMLResponse(_test_result_html(True, note))
    resp = JSONResponse(
        {
            "ok": True,
            "project": project,
            "share_on_lan": stored,
            "share_url": get_share_url(project) if stored else "",
            "has_password": auth_mod.has_password(data_dir),
        }
    )
    if new_session:
        resp.set_cookie(
            auth_mod.SESSION_COOKIE,
            new_session,
            max_age=auth_mod.SESSION_TTL_SECONDS,
            path="/",
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
    return resp


@app.post("/api/setup/provider")
async def save_setup_provider(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    provider = "meta"
    base_url = ""
    model = ""
    api_key = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            project = str(payload.get("project", project)).strip() or "default"
            provider = str(payload.get("provider", "meta"))
            base_url = str(
                payload.get("base_url", payload.get("meta_base_url", "") or "") or ""
            )
            model = str(payload.get("model", payload.get("meta_model", "") or "") or "")
            api_key = str(
                payload.get("api_key", payload.get("meta_api_key", "") or "") or ""
            )
    else:
        form = await request.form()
        if "project" in form:
            project = str(form.get("project", project)).strip() or "default"
        provider = str(form.get("provider", "meta"))
        base_url = str(form.get("base_url", form.get("meta_base_url", "") or "") or "")
        model = str(form.get("model", form.get("meta_model", "") or "") or "")
        api_key = str(form.get("api_key", form.get("meta_api_key", "") or "") or "")
    canonical = provider_mod.normalize_provider(provider)
    data_dir = Config().ensure_data_dir()
    if canonical == "custom":
        try:
            existing_key = provider_mod.load_provider_key(data_dir)
        except Exception:  # noqa: BLE001
            existing_key = ""
        check_key = api_key.strip() or existing_key
        ok, message, hint = provider_mod.validate_custom_config(
            base_url, model, check_key
        )
        if not ok:
            if "hx-request" in request.headers:
                return HTMLResponse(_test_result_html(False, message, hint))
            return JSONResponse(
                {"ok": False, "message": message, "hint": hint}, status_code=400
            )
    try:
        saved = provider_mod.save_provider(
            data_dir, canonical, base_url, model, api_key
        )
        meta_saved: dict[str, Any] = {}
        if canonical == "meta":
            meta_saved = secrets_mod.save_meta_config(
                data_dir, model=model, base_url=base_url, api_key=api_key
            )
    except Exception:  # noqa: BLE001
        msg = "Provider choice could not be saved."
        hint = "Check the data directory is writable, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    try:
        if canonical == "meta":
            _write_project_step(
                project,
                "model",
                {
                    "provider": "meta",
                    "model": meta_saved.get("model", ""),
                    "base_url": meta_saved.get("base_url", ""),
                    "has_key": meta_saved.get("has_key", False),
                },
            )
        else:
            _write_project_step(
                project,
                "model",
                {
                    "provider": saved.get("provider"),
                    "base_url": saved.get("base_url", ""),
                    "model": saved.get("model", ""),
                    "has_key": saved.get("has_key", False),
                },
            )
    except Exception:  # noqa: BLE001, S110
        pass
    label = provider_mod.provider_label(canonical)
    message = f"Provider saved. Pam will use {label}."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "provider": saved}


def _setup_payload(request: Request, payload: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(payload, dict):
        for key, val in payload.items():
            out[str(key)] = str(val or "")
    for key, val in request.query_params.items():
        out.setdefault(str(key), str(val or ""))
    return out


@app.post("/api/setup/slack")
async def save_setup_slack(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    bot_token = ""
    app_token = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        fields = _setup_payload(request, payload if isinstance(payload, dict) else {})
        project = fields.get("project", project).strip() or "default"
        bot_token = fields.get("bot_token", fields.get("SLACK_BOT_TOKEN", ""))
        app_token = fields.get("app_token", fields.get("SLACK_APP_TOKEN", ""))
    else:
        form = await request.form()
        project = str(form.get("project", project)).strip() or "default"
        bot_token = str(
            form.get("bot_token", form.get("SLACK_BOT_TOKEN", "") or "") or ""
        )
        app_token = str(
            form.get("app_token", form.get("SLACK_APP_TOKEN", "") or "") or ""
        )
    try:
        saved = secrets_mod.save_slack_config(
            Config().ensure_data_dir(),
            bot_token=bot_token,
            app_token=app_token,
        )
    except Exception:  # noqa: BLE001
        msg = "Slack settings could not be saved."
        hint = "Check the data directory is writable, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    try:
        _write_project_step(
            project,
            "slack",
            {
                "has_bot_token": saved.get("has_bot_token", False),
                "has_app_token": saved.get("has_app_token", False),
            },
        )
    except Exception:  # noqa: BLE001, S110
        pass
    message = "Slack settings saved."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "slack": saved}


def _resume_model_client(project: str) -> Any | None:
    """Return the model client for interview resume, if one is usable."""
    model = get_model_client(project)
    if model is not None:
        return model
    try:
        from pam_pm.models.client import ModelClient

        candidate = ModelClient()
        if candidate.model and candidate.base_url and candidate._api_key:
            return candidate
    except Exception:  # noqa: BLE001
        return None
    return None


@app.post("/api/slack/events")
async def slack_events_route(request: Request) -> Any:
    """Accept Slack Events API envelopes and route them to the planner.

    Handles url_verification by echoing the challenge. All other
    envelopes go to handle_slack_event. Slack retries share one
    event_id, so ids already seen return a duplicate response without
    posting again.
    """
    project = _safe_project(request.query_params.get("project", "default"))
    try:
        envelope = await request.json()
    except Exception:  # noqa: BLE001
        envelope = None
    if not isinstance(envelope, dict):
        return JSONResponse(
            {"ok": False, "message": "Invalid Slack envelope."}, status_code=400
        )
    if str(envelope.get("type", "") or "") == "url_verification":
        result = router_mod.handle_slack_event(
            envelope, chat=get_chat_adapter(project)
        )
        return {"challenge": str(result.get("challenge", "") or "")}
    event_id = str(envelope.get("event_id", "") or "").strip()
    if event_id and event_id in _SEEN_SLACK_EVENT_IDS:
        logger.info("slack event duplicate ignored: event_id=%s", event_id)
        return {"ok": True, "duplicate": True}
    poller_event_id = ""
    try:
        poller_event_id = _slack_poller_event_id(envelope)
    except Exception:  # noqa: BLE001
        poller_event_id = ""
    if poller_event_id and poller_event_id in _SEEN_SLACK_EVENT_IDS:
        logger.info("slack event duplicate ignored: poller_id=%s", poller_event_id)
        return {"ok": True, "duplicate": True}
    chat = get_chat_adapter(project)
    if chat is None:
        return JSONResponse(
            {"ok": False, "message": "Chat is not configured."}, status_code=502
        )
    board = get_board_adapter(project)
    try:
        result = router_mod.handle_slack_event(
            envelope,
            chat=chat,
            board=board,
            event_sink=bus.publish,
            db=_db_path(),
            model_client=_resume_model_client(project),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack event failed: %s", exc)
        return JSONResponse(
            {"ok": False, "message": "Slack event could not be handled."},
            status_code=502,
        )
    if event_id:
        _remember_slack_event(event_id)
    if poller_event_id:
        _remember_slack_event(poller_event_id)
    return {
        "ok": True,
        "kind": str(result.get("kind", "") or ""),
        "replied": bool(result.get("replied", False)),
        "duplicate": False,
    }


@app.post("/api/setup/teams")
async def save_setup_teams(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    app_id = ""
    app_password = ""
    tenant_id = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        fields = _setup_payload(request, payload if isinstance(payload, dict) else {})
        project = fields.get("project", project).strip() or "default"
        app_id = fields.get("app_id", fields.get("TEAMS_APP_ID", ""))
        app_password = fields.get("app_password", fields.get("TEAMS_APP_PASSWORD", ""))
        tenant_id = fields.get("tenant_id", fields.get("TEAMS_TENANT_ID", ""))
    else:
        form = await request.form()
        project = str(form.get("project", project)).strip() or "default"
        app_id = str(form.get("app_id", form.get("TEAMS_APP_ID", "") or "") or "")
        app_password = str(
            form.get("app_password", form.get("TEAMS_APP_PASSWORD", "") or "") or ""
        )
        tenant_id = str(
            form.get("tenant_id", form.get("TEAMS_TENANT_ID", "") or "") or ""
        )
    try:
        saved = secrets_mod.save_teams_config(
            Config().ensure_data_dir(),
            app_id=app_id,
            app_password=app_password,
            tenant_id=tenant_id,
        )
    except Exception:  # noqa: BLE001
        msg = "Teams settings could not be saved."
        hint = "Check the data directory is writable, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    try:
        _write_project_step(
            project,
            "teams",
            {
                "has_app_id": saved.get("has_app_id", False),
                "has_app_password": saved.get("has_app_password", False),
                "has_tenant_id": saved.get("has_tenant_id", False),
            },
        )
    except Exception:  # noqa: BLE001, S110
        pass
    message = "Teams settings saved."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "teams": saved}


@app.post("/api/setup/teams/device/start")
async def start_teams_device(request: Request) -> Any:
    """Start the Entra device code sign-in for Teams polling.

    Returns the user code plus the microsoft.com/devicelogin link for
    the wizard to show. The device code stays server side, keyed by
    project, and is never logged or returned to the browser.
    """
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = _safe_project(request.query_params.get("project", "default"))
    app_id = ""
    tenant_id = ""
    ctype = request.headers.get("content-type", "")
    try:
        if "application/json" in ctype:
            payload = await request.json()
            fields = _setup_payload(
                request, payload if isinstance(payload, dict) else {}
            )
            project = _safe_project(fields.get("project", project))
            app_id = fields.get("app_id", fields.get("TEAMS_APP_ID", ""))
            tenant_id = fields.get("tenant_id", fields.get("TEAMS_TENANT_ID", ""))
        else:
            form = await request.form()
            project = _safe_project(str(form.get("project", project) or project))
            app_id = str(form.get("app_id", form.get("TEAMS_APP_ID", "") or "") or "")
            tenant_id = str(
                form.get("tenant_id", form.get("TEAMS_TENANT_ID", "") or "") or ""
            )
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "message": "Device sign-in could not start."},
            status_code=400,
        )
    try:
        from pam_pm.adapters.chat import teams_auth as teams_auth_mod

        if app_id.strip() or tenant_id.strip():
            secrets_mod.save_teams_config(
                Config().ensure_data_dir(),
                app_id=app_id,
                tenant_id=tenant_id,
            )
        effective = secrets_mod.effective_teams_config(Config().ensure_data_dir())
        session = teams_auth_mod.start_device_code(
            str(effective.get("app_id", "") or ""),
            str(effective.get("tenant_id", "") or ""),
            transport=TEAMS_TEST_TRANSPORT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("teams device start failed")
        detail = str(exc)[:200] if str(exc) else "Check the App ID and Tenant ID."
        return JSONResponse({"ok": False, "message": detail}, status_code=502)
    _TEAMS_DEVICE_FLOWS[project] = session
    return {
        "ok": True,
        "project": project,
        "user_code": session.user_code,
        "verification_uri": session.verification_uri,
        "message": session.message,
        "expires_in": session.expires_in,
        "interval": session.interval,
    }


@app.post("/api/setup/teams/device/poll")
async def poll_teams_device(request: Request) -> Any:
    """Poll once for device code completion, storing the refresh token.

    Pending returns status pending so the wizard can retry. Complete
    stores the refresh token in the secrets store (0600) and marks
    the project signed in for silent hourly token minting.
    """
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = _safe_project(request.query_params.get("project", "default"))
    try:
        if "application/json" in request.headers.get("content-type", ""):
            payload = await request.json()
            if isinstance(payload, dict):
                fields = _setup_payload(request, payload)
                project = _safe_project(fields.get("project", project))
    except Exception:  # noqa: BLE001, S110
        pass
    session = _TEAMS_DEVICE_FLOWS.get(project)
    if session is None:
        return JSONResponse(
            {"ok": False, "status": "missing", "message": "Start sign-in first."},
            status_code=400,
        )
    try:
        from pam_pm.adapters.chat import teams_auth as teams_auth_mod

        effective = secrets_mod.effective_teams_config(Config().ensure_data_dir())
        result = teams_auth_mod.poll_device_token(
            session,
            client_secret=str(effective.get("app_password", "") or ""),
            transport=TEAMS_TEST_TRANSPORT,
        )
    except Exception as exc:  # noqa: BLE001
        from pam_pm.adapters.chat import teams_auth as teams_auth_mod

        if isinstance(exc, teams_auth_mod.DeviceCodePending):
            return {"ok": True, "status": "pending", "project": project}
        if isinstance(exc, teams_auth_mod.DeviceCodeSlowDown):
            return {"ok": True, "status": "pending", "slow_down": True, "project": project}
        if isinstance(exc, teams_auth_mod.DeviceCodeExpired):
            _TEAMS_DEVICE_FLOWS.pop(project, None)
            return JSONResponse(
                {"ok": False, "status": "expired", "message": str(exc)},
                status_code=410,
            )
        logger.warning("teams device poll failed")
        return JSONResponse(
            {"ok": False, "status": "error", "message": str(exc)[:200]},
            status_code=502,
        )
    teams_auth_mod.store_teams_refresh_token(
        result.refresh_token, Config().ensure_data_dir()
    )
    _TEAMS_DEVICE_FLOWS.pop(project, None)
    try:
        secrets_mod.save_teams_config(Config().ensure_data_dir())
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        _write_project_step(project, "teams_signin", {"signed_in": True})
    except Exception:  # noqa: BLE001, S110
        pass
    return {"ok": True, "status": "complete", "project": project, "signed_in": True}


@app.get("/api/setup/teams/device/status")
async def teams_device_status(request: Request) -> Any:
    """Return whether Teams polling is signed in for a project."""
    project = _safe_project(request.query_params.get("project", "default"))
    try:
        saved = secrets_mod.load_teams_config(Config().ensure_data_dir())
        signed_in = bool(saved.get("has_refresh_token", False))
    except Exception:  # noqa: BLE001
        signed_in = False
    return {
        "ok": True,
        "project": project,
        "signed_in": signed_in,
        "pending": project in _TEAMS_DEVICE_FLOWS,
    }


@app.post("/api/setup/ado")
async def save_setup_ado(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    org_url = ""
    ado_project = ""
    team = ""
    pat = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        fields = _setup_payload(request, payload if isinstance(payload, dict) else {})
        project = fields.get("project", project).strip() or "default"
        org_url = fields.get("org_url", fields.get("ADO_ORG_URL", ""))
        ado_project = (
            fields.get("project_name", "")
            or fields.get("ado_project", "")
            or fields.get("ADO_PROJECT", "")
        )
        team = fields.get("team", fields.get("ADO_TEAM", ""))
        pat = fields.get("pat", fields.get("ADO_PAT", ""))
    else:
        form = await request.form()
        project = str(form.get("project", project)).strip() or "default"
        org_url = str(form.get("org_url", form.get("ADO_ORG_URL", "") or "") or "")
        ado_project = str(
            form.get("project_name", form.get("ado_project", "") or "") or ""
        )
        if not ado_project:
            ado_project = str(form.get("ADO_PROJECT", "") or "")
        team = str(form.get("team", form.get("ADO_TEAM", "") or "") or "")
        pat = str(form.get("pat", form.get("ADO_PAT", "") or "") or "")
    try:
        saved = secrets_mod.save_ado_config(
            Config().ensure_data_dir(),
            org_url=org_url,
            project=ado_project,
            team=team,
            pat=pat,
        )
    except Exception:  # noqa: BLE001
        msg = "Azure DevOps settings could not be saved."
        hint = "Check the data directory is writable, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    try:
        _write_project_step(
            project,
            "ado",
            {
                "org_url": saved.get("org_url", ""),
                "project": saved.get("project", ""),
                "team": saved.get("team", ""),
                "has_pat": saved.get("has_pat", False),
            },
        )
    except Exception:  # noqa: BLE001, S110
        pass
    message = "Azure DevOps settings saved."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "ado": saved}


@app.post("/api/setup/jira")
async def save_setup_jira(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    base_url = ""
    email = ""
    jira_project = ""
    api_token = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        fields = _setup_payload(request, payload if isinstance(payload, dict) else {})
        project = fields.get("project", project).strip() or "default"
        base_url = fields.get("base_url", fields.get("JIRA_BASE_URL", ""))
        email = fields.get("email", fields.get("JIRA_EMAIL", ""))
        jira_project = fields.get(
            "project_key", fields.get("project", fields.get("JIRA_PROJECT", ""))
        )
        api_token = fields.get("api_token", fields.get("JIRA_API_TOKEN", ""))
    else:
        form = await request.form()
        project = str(form.get("project", project)).strip() or "default"
        base_url = str(form.get("base_url", form.get("JIRA_BASE_URL", "") or "") or "")
        email = str(form.get("email", form.get("JIRA_EMAIL", "") or "") or "")
        jira_project = str(
            form.get("project_key", form.get("project", "") or "") or ""
        ) or str(form.get("JIRA_PROJECT", "") or "")
        api_token = str(
            form.get("api_token", form.get("JIRA_API_TOKEN", "") or "") or ""
        )
    try:
        saved = secrets_mod.save_jira_config(
            Config().ensure_data_dir(),
            base_url=base_url,
            email=email,
            project=jira_project,
            api_token=api_token,
        )
    except Exception:  # noqa: BLE001
        msg = "Jira settings could not be saved."
        hint = "Check the data directory is writable, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    try:
        _write_project_step(
            project,
            "jira",
            {
                "base_url": saved.get("base_url", ""),
                "email": saved.get("email", ""),
                "project": saved.get("project", ""),
                "has_token": saved.get("has_token", False),
            },
        )
    except Exception:  # noqa: BLE001, S110
        pass
    message = "Jira settings saved."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "jira": saved}


@app.post("/api/setup/linear")
async def save_setup_linear(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    team = ""
    api_key = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        fields = _setup_payload(request, payload if isinstance(payload, dict) else {})
        project = fields.get("project", project).strip() or "default"
        team = fields.get("team", fields.get("LINEAR_TEAM", ""))
        api_key = fields.get("api_key", fields.get("LINEAR_API_KEY", ""))
    else:
        form = await request.form()
        project = str(form.get("project", project)).strip() or "default"
        team = str(form.get("team", form.get("LINEAR_TEAM", "") or "") or "")
        api_key = str(form.get("api_key", form.get("LINEAR_API_KEY", "") or "") or "")
    try:
        saved = secrets_mod.save_linear_config(
            Config().ensure_data_dir(),
            team=team,
            api_key=api_key,
        )
    except Exception:  # noqa: BLE001
        msg = "Linear settings could not be saved."
        hint = "Check the data directory is writable, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    try:
        _write_project_step(
            project,
            "linear",
            {
                "team": saved.get("team", ""),
                "has_key": saved.get("has_key", False),
            },
        )
    except Exception:  # noqa: BLE001, S110
        pass
    message = "Linear settings saved."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "linear": saved}


@app.post("/api/setup/trello")
async def save_setup_trello(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    board = ""
    api_key = ""
    token = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        fields = _setup_payload(request, payload if isinstance(payload, dict) else {})
        project = fields.get("project", project).strip() or "default"
        board = fields.get("board", fields.get("TRELLO_BOARD", ""))
        api_key = fields.get("api_key", fields.get("TRELLO_API_KEY", ""))
        token = fields.get("trello_token", fields.get("TRELLO_TOKEN", ""))
    else:
        form = await request.form()
        project = str(form.get("project", project)).strip() or "default"
        board = str(form.get("board", form.get("TRELLO_BOARD", "") or "") or "")
        api_key = str(form.get("api_key", form.get("TRELLO_API_KEY", "") or "") or "")
        token = str(form.get("trello_token", form.get("TRELLO_TOKEN", "") or "") or "")
    try:
        saved = secrets_mod.save_trello_config(
            Config().ensure_data_dir(),
            board=board,
            api_key=api_key,
            token=token,
        )
    except Exception:  # noqa: BLE001
        msg = "Trello settings could not be saved."
        hint = "Check the data directory is writable, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    try:
        _write_project_step(
            project,
            "trello",
            {
                "board": saved.get("board", ""),
                "has_key": saved.get("has_key", False),
                "has_token": saved.get("has_token", False),
            },
        )
    except Exception:  # noqa: BLE001, S110
        pass
    message = "Trello settings saved."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "trello": saved}


@app.post("/api/setup/clickup")
async def save_setup_clickup(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    team = ""
    wanted = ""
    api_token = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        fields = _setup_payload(request, payload if isinstance(payload, dict) else {})
        project = fields.get("project", project).strip() or "default"
        team = fields.get("team", fields.get("CLICKUP_TEAM", ""))
        wanted = fields.get("list", fields.get("CLICKUP_LIST", ""))
        api_token = fields.get("api_token", fields.get("CLICKUP_API_TOKEN", ""))
    else:
        form = await request.form()
        project = str(form.get("project", project)).strip() or "default"
        team = str(form.get("team", form.get("CLICKUP_TEAM", "") or "") or "")
        wanted = str(form.get("list", form.get("CLICKUP_LIST", "") or "") or "")
        api_token = str(
            form.get("api_token", form.get("CLICKUP_API_TOKEN", "") or "") or ""
        )
    try:
        saved = secrets_mod.save_clickup_config(
            Config().ensure_data_dir(),
            team=team,
            list_ref=wanted,
            api_token=api_token,
        )
    except Exception:  # noqa: BLE001
        msg = "ClickUp settings could not be saved."
        hint = "Check the data directory is writable, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    try:
        _write_project_step(
            project,
            "clickup",
            {
                "team": saved.get("team", ""),
                "list": saved.get("list", ""),
                "has_token": saved.get("has_token", False),
            },
        )
    except Exception:  # noqa: BLE001, S110
        pass
    message = "ClickUp settings saved."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "clickup": saved}


@app.post("/api/setup/asana")
async def save_setup_asana(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    asana_project = ""
    api_token = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        fields = _setup_payload(request, payload if isinstance(payload, dict) else {})
        project = fields.get("project", project).strip() or "default"
        asana_project = fields.get(
            "project_name", fields.get("project", fields.get("ASANA_PROJECT", ""))
        )
        api_token = fields.get("api_token", fields.get("ASANA_API_TOKEN", ""))
    else:
        form = await request.form()
        project = str(form.get("project", project)).strip() or "default"
        asana_project = str(
            form.get("project_name", form.get("project", "") or "") or ""
        ) or str(form.get("ASANA_PROJECT", "") or "")
        api_token = str(
            form.get("api_token", form.get("ASANA_API_TOKEN", "") or "") or ""
        )
    try:
        saved = secrets_mod.save_asana_config(
            Config().ensure_data_dir(),
            project=asana_project,
            api_token=api_token,
        )
    except Exception:  # noqa: BLE001
        msg = "Asana settings could not be saved."
        hint = "Check the data directory is writable, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    try:
        _write_project_step(
            project,
            "asana",
            {
                "project": saved.get("project", ""),
                "has_token": saved.get("has_token", False),
            },
        )
    except Exception:  # noqa: BLE001, S110
        pass
    message = "Asana settings saved."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "asana": saved}


@app.post("/api/setup/monday")
async def save_setup_monday(request: Request) -> Any:
    if not await _setup_token_ok(request):
        return _setup_token_denied(request)
    project = request.query_params.get("project", "default").strip() or "default"
    board = ""
    api_token = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        fields = _setup_payload(request, payload if isinstance(payload, dict) else {})
        project = fields.get("project", project).strip() or "default"
        board = fields.get("board", fields.get("MONDAY_BOARD", ""))
        api_token = fields.get("api_token", fields.get("MONDAY_API_TOKEN", ""))
    else:
        form = await request.form()
        project = str(form.get("project", project)).strip() or "default"
        board = str(form.get("board", form.get("MONDAY_BOARD", "") or "") or "")
        api_token = str(
            form.get("api_token", form.get("MONDAY_API_TOKEN", "") or "") or ""
        )
    try:
        saved = secrets_mod.save_monday_config(
            Config().ensure_data_dir(), board=board, api_token=api_token
        )
    except Exception:  # noqa: BLE001
        msg = "Monday settings could not be saved."
        hint = "Check the data directory is writable, then save again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    try:
        _write_project_step(
            project,
            "monday",
            {
                "board": saved.get("board", ""),
                "has_token": saved.get("has_token", False),
            },
        )
    except Exception:  # noqa: BLE001, S110
        pass
    message = "Monday settings saved."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "project": project, "monday": saved}


@app.post("/api/rules/toggle")
async def toggle_rule(request: Request) -> Any:
    name = ""
    enabled = False
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            name = str(payload.get("name", "")).strip()
            enabled = payload.get("enabled", False) in (True, "on", "true", "1", 1)
    else:
        form = await request.form()
        name = str(form.get("name", "")).strip()
        enabled = str(form.get("enabled", "")).lower() in ("on", "true", "1", "yes")
    titles = {str(r["name"]): str(r["title"]) for r in RULE_TEMPLATES}
    if name not in titles:
        msg = "Sorry, that rule was not found."
        hint = "Reload the page and try again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=404
        )
    RULE_STATES[name] = enabled
    state = "on" if enabled else "off"
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, f"{titles[name]} is now {state}."))
    return {"ok": True, "name": name, "enabled": enabled}


def _sse_events() -> Any:
    for event in _activity_events():
        yield f"data: {json.dumps(event)}\n\n"
    yield ": ping\n\n"


@app.get("/api/activity/stream")
async def activity_stream(request: Request) -> StreamingResponse:
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    if request.query_params.get("live", "") != "1":
        return StreamingResponse(
            _sse_events(),
            media_type="text/event-stream",
            headers=headers,
        )

    async def _live_events() -> Any:
        for event in _activity_events():
            yield f"data: {json.dumps(event)}\n\n"
        queue = bus.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        _live_events(),
        media_type="text/event-stream",
        headers=headers,
    )


@app.post("/api/undo/{write_id}")
def undo_event(write_id: int, request: Request) -> Any:
    try:
        updated = store_mod.undo_write(Config().db_path(), write_id)
    except Exception:  # noqa: BLE001
        updated = False
    try:
        raid_mod.on_write_undone(_db_path(), write_id)
    except Exception:  # noqa: BLE001, S110
        pass
    undone = bool(updated or bus.mark_undone(write_id))
    if "hx-request" in request.headers:
        if undone:
            return HTMLResponse(
                _test_result_html(True, "Undone. The change was reversed.")
            )
        return HTMLResponse(
            _test_result_html(
                False,
                "Sorry, this change can no longer be undone.",
                "Reload the page to see the current state.",
            )
        )
    if undone:
        return {"ok": True, "id": write_id}
    return {"ok": False, "message": "Write not found"}


@app.post("/api/setup/complete")
async def setup_complete(request: Request) -> Any:
    global SETUP_DONE
    if SETUP_DONE:
        if "hx-request" in request.headers:
            return HTMLResponse(
                _test_result_html(
                    True, "Setup is already complete. Nothing else to do."
                )
            )
        return JSONResponse({"ok": False, "message": "Setup already complete"})
    token = ""
    autostart_on = True
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            token = str(payload.get("token", "")).strip()
            autostart_on = _autostart_choice(
                payload.get("autostart", True), default=True
            )
    else:
        form = await request.form()
        token = str(form.get("token", "")).strip()
        # An unchecked box sends no field, so a missing field means off
        # here while the JSON API defaults to on.
        autostart_on = _autostart_choice(
            form.get("autostart") if "autostart" in form else False,
            default=True,
        )
    if not token:
        token = request.query_params.get("token", "").strip()
    if not token:
        token = request.headers.get("x-setup-token", "").strip()
    if not token:
        try:
            token = request.cookies.get("pam_setup_token", "").strip()
        except Exception:  # noqa: BLE001
            token = ""
    if not is_valid_token(token):
        msg = "That setup token did not match."
        hint = "Paste the token printed when you ran pam start and try again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse({"ok": False, "message": msg, "hint": hint})
    try:
        if autostart_on:
            autostart_message = autostart_mod.enable()
        else:
            autostart_message = autostart_mod.disable()
    except Exception as exc:  # noqa: BLE001
        autostart_message = f"autostart: unknown ({exc})"
    SETUP_DONE = True
    if "hx-request" in request.headers:
        return HTMLResponse(
            _test_result_html(True, "Setup is complete. Open the Board page to start.")
        )
    return {"ok": True, "message": "Setup complete", "autostart": autostart_message}


@app.get("/api/autostart")
def get_autostart() -> dict[str, Any]:
    """Return autostart status and the wizard checkbox label."""
    try:
        info = autostart_mod.status()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"autostart: unknown ({exc})"}
    return {
        "ok": True,
        "enabled": info.get("enabled") == "on",
        "platform": info.get("platform", ""),
        "detail": info.get("detail", ""),
        "label": autostart_mod.CHECKBOX_LABEL,
    }


@app.post("/api/autostart")
async def set_autostart(request: Request) -> Any:
    """Turn autostart on or off from JSON, form, or query input."""
    enabled: Any = None
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            enabled = payload.get("enabled", payload.get("autostart"))
    else:
        form = await request.form()
        if "enabled" in form:
            enabled = form.get("enabled")
        elif "autostart" in form:
            enabled = form.get("autostart")
    if "enabled" in request.query_params:
        enabled = request.query_params.get("enabled")
    on = _autostart_choice(enabled, default=True)
    try:
        if on:
            message = autostart_mod.enable()
        else:
            message = autostart_mod.disable()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": f"autostart: unknown ({exc})"})
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "enabled": on, "message": message}


@app.post("/api/review/run")
async def run_review(request: Request) -> Any:
    from pam_pm.agents import lead as lead_mod

    project = _safe_project(request.query_params.get("project", "default"))
    result = _run_review_now(project)
    posted = False
    try:
        chat = get_chat_adapter(project)
        channel = os.environ.get(lead_mod.REVIEW_CHANNEL_ENV, "").strip()
        target = channel
        if not target:
            try:
                names = _people_names(project)
            except Exception:  # noqa: BLE001
                names = []
            target = str(names[0]).strip() if names else ""
        if chat is not None and target:
            posted = bool(lead_mod.post_review(chat, result, channel=target))
    except Exception:  # noqa: BLE001
        posted = False
    result["posted"] = posted
    if "hx-request" in request.headers:
        return HTMLResponse(_review_result_html(result))
    return {
        "ok": True,
        "markdown": result["review_markdown"],
        "retro_actions": result["retro_actions"],
        "low_confidence": result["low_confidence"],
        "posted": posted,
    }


async def _retro_index(request: Request) -> int | None:
    query = request.query_params.get("index", "").strip()
    if query:
        try:
            return int(query)
        except ValueError:
            return None
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            return None
        if isinstance(payload, dict) and "index" in payload:
            try:
                return int(str(payload["index"]))
            except ValueError:
                return None
        return None
    form = await request.form()
    raw = str(form.get("index", "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@app.post("/api/review/retro/create")
async def create_retro_action(request: Request) -> Any:
    actions = LAST_REVIEW.get("retro_actions", [])
    if not actions:
        _run_review_now()
        actions = LAST_REVIEW.get("retro_actions", [])
    index = await _retro_index(request)
    if index is None or index < 0 or index >= len(actions):
        msg = "Unknown retro action. Run a review first."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=404)
    action = actions[index]
    item = _demo_board().create_item(
        str(action.get("title", "Retro action")),
        {
            "type": "Task",
            "state": "To Do",
            "description": str(action.get("description", "")),
            "tags": ["retro-action"],
        },
    )
    logger.info("retro action created: %s -> item %s", action.get("title"), item.id)
    if "hx-request" in request.headers:
        return HTMLResponse(
            _test_result_html(True, f"Follow up task created as item {item.id}")
        )
    return {"ok": True, "id": item.id, "tags": ["retro-action"]}


@app.post("/api/plan/upload")
async def upload_plan_photo(request: Request) -> Any:
    form = await request.form()
    upload = form.get("photo")
    data = b""
    if upload is not None and hasattr(upload, "read"):
        try:
            raw = await upload.read()  # type: ignore[no-untyped-call]
        except Exception:  # noqa: BLE001
            raw = b""
        if isinstance(raw, (bytes, bytearray)):
            data = bytes(raw)
    try:
        current_provider = str(
            provider_mod.load_provider(Config().ensure_data_dir()).get(
                "provider", "meta"
            )
        )
    except Exception:  # noqa: BLE001
        current_provider = "meta"
    project = _safe_project(request.query_params.get("project", "default"))
    vision_client = VisionClient(
        model_client=_resume_model_client(project), provider=current_provider
    )
    photo = plan_from_photo(
        data if data else b"", vision=vision_client, provider=current_provider
    )
    proposal = propose_sprint(photo.get("readings") or photo.get("notes_text", ""))
    LAST_PLAN["readings"] = photo.get("readings", [])
    LAST_PLAN["masks"] = photo.get("masks", [])
    LAST_PLAN["path"] = photo.get("path", "")
    LAST_PLAN["proposal"] = proposal
    LAST_PLAN["approved"] = []
    logger.info(
        "plan upload: path=%s readings=%d proposal=%d",
        photo.get("path"),
        len(photo.get("readings", [])),
        len(proposal),
    )
    try:
        router_mod.log_route(
            router_mod.CAP_PLAN,
            str(photo.get("model", "") or router_mod.text_model()),
            f"planner path={photo.get('path', '')}",
        )
        bus.publish(
            {
                "operation": "plan_from_photo",
                "item_id": "",
                "evidence_quote": (
                    f"Planner path: {photo.get('path', '')} "
                    f"({len(photo.get('readings', []))} readings, "
                    f"{len(proposal)} proposed)."
                ),
                "status": "proposed",
                "detail": (
                    f"path={photo.get('path', '')} "
                    f"model={photo.get('model', '') or router_mod.text_model()}"
                ),
            }
        )
    except Exception:  # noqa: BLE001, S110
        pass
    if "hx-request" in request.headers:
        return HTMLResponse(_plan_result_html(photo, proposal))
    return {
        "ok": True,
        "readings": photo.get("readings", []),
        "masks": photo.get("masks", []),
        "path": photo.get("path", ""),
        "proposal": proposal,
    }


@app.post("/api/plan/approve")
async def approve_plan(request: Request) -> Any:
    _ = request
    proposal = LAST_PLAN.get("proposal", [])
    if not proposal:
        msg = "No suggested tasks to create. Upload a photo first."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    created = approve_proposal(proposal, _demo_board())
    done = [{"id": item.id, "title": item.title} for item in created]
    LAST_PLAN["approved"] = done
    logger.info("plan approved: %d items created", len(done))
    if "hx-request" in request.headers:
        ids = ", ".join(item["id"] for item in done)
        return HTMLResponse(_test_result_html(True, f"Created items: {ids}"))
    return {"ok": True, "created": done}


async def _board_payload(request: Request) -> dict[str, Any]:
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}
    form = await request.form()
    return {str(k): str(v) for k, v in form.items()}


def _board_project(request: Request) -> str:
    return _safe_project(request.query_params.get("project", "default"))


def _read_only_response() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "message": "This board is read only in Pam.",
            "hint": "Open it in Azure DevOps to make changes.",
        },
        status_code=403,
    )


def _editable_board(project: str) -> tuple[Any | None, dict[str, bool]]:
    adapter = get_board_adapter(project)
    caps = _board_capabilities(project, adapter)
    if adapter is None or not caps["editable"]:
        return None, caps
    return adapter, caps


def _board_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, ValueError):
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    logger.warning("board write failed: %s", exc)
    return JSONResponse(
        {
            "ok": False,
            "message": "That change could not be saved.",
            "hint": "Try again in a moment.",
        },
        status_code=502,
    )


def _iteration_names(adapter: Any) -> dict[str, str]:
    return {entry["id"]: entry["name"] for entry in _board_iterations(adapter)}


@app.get("/api/board")
def board_data(request: Request) -> Any:
    project = _board_project(request)
    snap = _board_snapshot(project)
    if snap["error"] and not snap["items"]:
        payload = {
            "ok": True,
            "project": snap["project"],
            "backend": snap["backend"],
            "backend_label": snap["backend_label"],
            "editable": snap["editable"],
            "capabilities": snap["capabilities"],
            "states": snap["states"],
            "iterations": snap["iterations"],
            "items": [],
            "links": [],
            "error": snap["error"],
        }
        return payload
    return {
        "ok": True,
        "project": snap["project"],
        "backend": snap["backend"],
        "backend_label": snap["backend_label"],
        "editable": snap["editable"],
        "capabilities": snap["capabilities"],
        "states": snap["states"],
        "iterations": snap["iterations"],
        "items": snap["items"],
        "links": snap["links"],
        "error": snap["error"],
    }


@app.post("/api/board/items")
async def board_create_item(request: Request) -> Any:
    project = _board_project(request)
    adapter, _ = _editable_board(project)
    if adapter is None:
        return _read_only_response()
    payload = await _board_payload(request)
    title = str(payload.get("title", "")).strip()
    if not title:
        return JSONResponse(
            {
                "ok": False,
                "message": "Title is required.",
                "hint": "Type a title, then save again.",
            },
            status_code=400,
        )
    fields: dict[str, Any] = {}
    for key in BOARD_ITEM_FIELDS:
        value = payload.get(key)
        if value is not None and str(value) != "":
            fields[key] = value
    if "state" not in fields:
        try:
            states = [str(s) for s in adapter.list_states()]
        except Exception:  # noqa: BLE001
            states = []
        fields["state"] = states[0] if states else "New"
    try:
        item = adapter.create_item(title, fields)
    except Exception as exc:  # noqa: BLE001
        return _board_error_response(exc)
    evidence = str(payload.get("evidence", "")).strip() or (
        f"Human edit on {project} board: created {title}."
    )
    event = _record_board_write(
        project, "create", str(item.id), evidence, {"title": title}
    )
    return {
        "ok": True,
        "item": _serialize_item(item, _iteration_names(adapter)),
        "write_id": event["id"],
    }


@app.get("/api/board/items/{item_id}")
def board_item_detail(request: Request, item_id: str) -> Any:
    project = _board_project(request)
    adapter = get_board_adapter(project)
    if adapter is None:
        return JSONResponse(
            {
                "ok": False,
                "message": "The board is not available right now.",
                "hint": "Try again in a moment.",
            },
            status_code=502,
        )
    try:
        item = adapter.get_item(item_id)
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "message": f"Item {item_id} was not found."},
            status_code=404,
        )
    caps = _board_capabilities(project, adapter)
    links: list[dict[str, str]] = []
    if caps["dependency_links"]:
        try:
            for link in adapter.list_links(item_id) or []:
                links.append(
                    {
                        "source_id": str(link.source_id),
                        "target_id": str(link.target_id),
                        "rel": str(link.rel),
                        "label": _link_label(
                            str(link.source_id),
                            str(link.target_id),
                            str(link.rel),
                        ),
                    }
                )
        except Exception:  # noqa: BLE001
            links = []
    comments: list[dict[str, str]] = []
    if caps["comments"]:
        try:
            for entry in adapter.list_comments(item_id) or []:
                comments.append(
                    {
                        "id": str(entry.id),
                        "text": str(entry.text),
                        "author": str(entry.author or ""),
                        "created": str(entry.created or ""),
                    }
                )
        except Exception:  # noqa: BLE001
            comments = []
    detail_backend = _project_backend(project)
    url = (
        ""
        if detail_backend == BACKEND_PAM_BOARD
        else _external_item_url(detail_backend, item)
    )
    data = _serialize_item(item, _iteration_names(adapter), url)
    data["link_count"] = len(links)
    return {
        "ok": True,
        "item": data,
        "links": links,
        "comments": comments,
        "capabilities": caps,
        "editable": caps["editable"],
    }


@app.patch("/api/board/items/{item_id}")
async def board_edit_item(request: Request, item_id: str) -> Any:
    project = _board_project(request)
    adapter, _ = _editable_board(project)
    if adapter is None:
        return _read_only_response()
    payload = await _board_payload(request)
    fields: dict[str, Any] = {}
    title = str(payload.get("title", "")).strip()
    if title:
        fields["title"] = title
    for key in BOARD_ITEM_FIELDS:
        if key in payload and payload[key] is not None:
            fields[key] = payload[key]
    if not fields:
        return JSONResponse(
            {
                "ok": False,
                "message": "Nothing changed.",
                "hint": "Edit a field, then save again.",
            },
            status_code=400,
        )
    try:
        item = adapter.update_item(item_id, fields)
    except Exception as exc:  # noqa: BLE001
        if "unknown item" in str(exc).lower():
            return JSONResponse(
                {"ok": False, "message": f"Item {item_id} was not found."},
                status_code=404,
            )
        return _board_error_response(exc)
    evidence = str(payload.get("evidence", "")).strip() or (
        f"Human edit on {project} board: updated item {item_id}."
    )
    event = _record_board_write(
        project, "update", item_id, evidence, {"fields": sorted(fields)}
    )
    raid_closed: list[dict[str, Any]] = []
    try:
        if is_closed(str(item.state)):
            raid_closed = raid_mod.close_issues_for_board_item(
                _db_path(), _project_backend(project), project, item_id
            )
    except Exception:  # noqa: BLE001
        raid_closed = []
    return {
        "ok": True,
        "item": _serialize_item(item, _iteration_names(adapter)),
        "write_id": event["id"],
        "raid_closed": len(raid_closed),
    }


@app.post("/api/board/items/{item_id}/move")
async def board_move_item(request: Request, item_id: str) -> Any:
    project = _board_project(request)
    adapter, _ = _editable_board(project)
    if adapter is None:
        return _read_only_response()
    payload = await _board_payload(request)
    state = str(payload.get("state", "")).strip()
    if not state:
        return JSONResponse(
            {
                "ok": False,
                "message": "State is required.",
                "hint": "Choose a state, then try again.",
            },
            status_code=400,
        )
    try:
        states = [str(s) for s in adapter.list_states()]
    except Exception:  # noqa: BLE001
        states = []
    if states and state not in states:
        return JSONResponse(
            {
                "ok": False,
                "message": f"Unknown state {state}.",
                "hint": "Use one of: " + ", ".join(states) + ".",
            },
            status_code=400,
        )
    try:
        before = adapter.get_item(item_id)
        item = adapter.update_item(item_id, {"state": state})
    except Exception as exc:  # noqa: BLE001
        if "unknown item" in str(exc).lower():
            return JSONResponse(
                {"ok": False, "message": f"Item {item_id} was not found."},
                status_code=404,
            )
        return _board_error_response(exc)
    evidence = str(payload.get("evidence", "")).strip() or (
        f"Human edit on {project} board: moved item {item_id} to {state}."
    )
    event = _record_board_write(
        project,
        "move",
        item_id,
        evidence,
        {"from": str(before.state), "to": state},
    )
    raid_closed: list[dict[str, Any]] = []
    try:
        if is_closed(str(item.state)):
            raid_closed = raid_mod.close_issues_for_board_item(
                _db_path(), _project_backend(project), project, item_id
            )
    except Exception:  # noqa: BLE001
        raid_closed = []
    return {
        "ok": True,
        "item": _serialize_item(item, _iteration_names(adapter)),
        "write_id": event["id"],
        "raid_closed": len(raid_closed),
    }


@app.post("/api/board/links")
async def board_add_link(request: Request) -> Any:
    project = _board_project(request)
    adapter, caps = _editable_board(project)
    if adapter is None:
        return _read_only_response()
    if not caps["dependency_links"]:
        return JSONResponse(
            {
                "ok": False,
                "message": "Links are not available on this board.",
            },
            status_code=403,
        )
    payload = await _board_payload(request)
    source_id = str(payload.get("source_id", "")).strip()
    target_id = str(payload.get("target_id", "")).strip()
    rel = str(payload.get("rel", "")).strip() or ("System.LinkTypes.Dependency-Forward")
    if not source_id or not target_id:
        return JSONResponse(
            {
                "ok": False,
                "message": "A target item number is required.",
                "hint": "Type the target item number, then try again.",
            },
            status_code=400,
        )
    try:
        link = adapter.add_link(source_id, target_id, rel)
    except Exception as exc:  # noqa: BLE001
        return _board_error_response(exc)
    evidence = str(payload.get("evidence", "")).strip() or (
        f"Human edit on {project} board: linked {source_id} to {target_id}."
    )
    event = _record_board_write(
        project,
        "add_link",
        source_id,
        evidence,
        {"source_id": source_id, "target_id": target_id, "rel": rel},
    )
    raid_external_id = ""
    try:
        writes = WriteService(_db_path())
        dep = raid_mod.propose_dependency_from_link(
            _db_path(),
            _project_backend(project),
            project,
            source_id,
            target_id,
            evidence_quote=evidence,
            writes=writes,
        )
        raid_external_id = str(dep.get("external_id", ""))
        if dep.get("write_id"):
            bus.publish(
                {
                    "id": int(dep["write_id"]),
                    "operation": "raid_propose_dependency",
                    "item_id": raid_external_id,
                    "evidence_quote": evidence[:500],
                    "status": "proposed",
                    "detail": f"Dependency proposed: {source_id} blocks {target_id}.",
                }
            )
    except Exception:  # noqa: BLE001
        raid_external_id = ""
    return {
        "ok": True,
        "link": {
            "source_id": str(link.source_id),
            "target_id": str(link.target_id),
            "rel": str(link.rel),
            "label": _link_label(
                str(link.source_id), str(link.target_id), str(link.rel)
            ),
        },
        "write_id": event["id"],
        "raid_external_id": raid_external_id,
    }


@app.post("/api/board/items/{item_id}/sprint")
async def board_set_sprint(request: Request, item_id: str) -> Any:
    project = _board_project(request)
    adapter, caps = _editable_board(project)
    if adapter is None:
        return _read_only_response()
    if not caps["iterations"]:
        return JSONResponse(
            {"ok": False, "message": "Sprints are not available on this board."},
            status_code=403,
        )
    payload = await _board_payload(request)
    iteration_id = str(payload.get("iteration_id", "")).strip()
    try:
        item = adapter.set_item_iteration(item_id, iteration_id)
    except Exception as exc:  # noqa: BLE001
        return _board_error_response(exc)
    evidence = str(payload.get("evidence", "")).strip() or (
        f"Human edit on {project} board: set sprint on item {item_id}."
    )
    event = _record_board_write(
        project, "set_sprint", item_id, evidence, {"iteration_id": iteration_id}
    )
    return {
        "ok": True,
        "item": _serialize_item(item, _iteration_names(adapter)),
        "write_id": event["id"],
    }


@app.post("/api/board/sprints")
async def board_create_sprint(request: Request) -> Any:
    project = _board_project(request)
    adapter, caps = _editable_board(project)
    if adapter is None:
        return _read_only_response()
    if not caps["iterations"]:
        return JSONResponse(
            {"ok": False, "message": "Sprints are not available on this board."},
            status_code=403,
        )
    payload = await _board_payload(request)
    name = str(payload.get("name", "")).strip()
    if not name:
        return JSONResponse(
            {
                "ok": False,
                "message": "Sprint name is required.",
                "hint": "Type a name, then try again.",
            },
            status_code=400,
        )
    try:
        sprint = adapter.create_iteration(
            name,
            start_date=str(payload.get("start_date", "")).strip(),
            end_date=str(payload.get("end_date", "")).strip(),
        )
    except Exception as exc:  # noqa: BLE001
        return _board_error_response(exc)
    evidence = str(payload.get("evidence", "")).strip() or (
        f"Human edit on {project} board: created sprint {name}."
    )
    event = _record_board_write(
        project, "create_sprint", str(sprint.id), evidence, {"name": name}
    )
    return {
        "ok": True,
        "sprint": {
            "id": str(sprint.id),
            "name": str(sprint.name),
            "start_date": str(getattr(sprint, "start_date", "") or ""),
            "end_date": str(getattr(sprint, "end_date", "") or ""),
        },
        "write_id": event["id"],
    }


@app.post("/api/board/sprints/{sprint_id}/close")
async def board_close_sprint(request: Request, sprint_id: str) -> Any:
    project = _board_project(request)
    adapter, caps = _editable_board(project)
    if adapter is None:
        return _read_only_response()
    if not caps["close_iteration"]:
        return JSONResponse(
            {
                "ok": False,
                "message": "Sprints cannot be closed here.",
            },
            status_code=403,
        )
    closer = getattr(adapter, "close_iteration", None)
    if not callable(closer):
        return JSONResponse(
            {
                "ok": False,
                "message": "Sprints cannot be closed here.",
            },
            status_code=403,
        )
    try:
        sprint = closer(sprint_id)
    except Exception as exc:  # noqa: BLE001
        if "unknown iteration" in str(exc).lower():
            return JSONResponse(
                {"ok": False, "message": f"Sprint {sprint_id} was not found."},
                status_code=404,
            )
        return _board_error_response(exc)
    name = str(getattr(sprint, "name", sprint_id))
    payload = await _board_payload(request)
    evidence = str(payload.get("evidence", "")).strip() or (
        f"Human edit on {project} board: closed sprint {name}."
    )
    event = _record_board_write(
        project, "close_sprint", str(sprint.id), evidence, {"name": name}
    )
    return {
        "ok": True,
        "sprint": {
            "id": str(sprint.id),
            "name": name,
            "start_date": str(getattr(sprint, "start_date", "") or ""),
            "end_date": str(getattr(sprint, "end_date", "") or ""),
        },
        "write_id": event["id"],
    }


@app.post("/api/board/items/{item_id}/comments")
async def board_add_comment(request: Request, item_id: str) -> Any:
    project = _board_project(request)
    adapter, caps = _editable_board(project)
    if adapter is None:
        return _read_only_response()
    if not caps["comments"]:
        return JSONResponse(
            {"ok": False, "message": "Comments are not available on this board."},
            status_code=403,
        )
    payload = await _board_payload(request)
    text = str(payload.get("text", "")).strip()
    if not text:
        return JSONResponse(
            {
                "ok": False,
                "message": "Comment text is required.",
                "hint": "Type a comment, then try again.",
            },
            status_code=400,
        )
    try:
        comment = adapter.add_comment(
            item_id, text, author=str(payload.get("author", "")).strip()
        )
    except Exception as exc:  # noqa: BLE001
        return _board_error_response(exc)
    event = _record_board_write(
        project, "comment", item_id, text[:200], {"comment_id": str(comment.id)}
    )
    return {
        "ok": True,
        "comment": {
            "id": str(comment.id),
            "text": str(comment.text),
            "author": str(comment.author or ""),
            "created": str(comment.created or ""),
        },
        "write_id": event["id"],
    }


@app.get("/api/board/export")
def board_export_csv(request: Request) -> Any:
    project = _board_project(request)
    adapter = get_board_adapter(project)
    if adapter is None:
        return JSONResponse(
            {
                "ok": False,
                "message": "The board is not available right now.",
                "hint": "Try again in a moment.",
            },
            status_code=502,
        )
    try:
        raw_items = list(adapter.list_items(limit=2000) or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("board export failed: %s", exc)
        return JSONResponse(
            {
                "ok": False,
                "message": "The board could not be loaded.",
                "hint": "Try reloading the page.",
            },
            status_code=502,
        )
    names = _iteration_names(adapter)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(BOARD_CSV_FIELDS))
    writer.writeheader()
    for item in raw_items:
        data = _serialize_item(item, names)
        writer.writerow(
            {
                "id": data["id"],
                "title": data["title"],
                "state": data["state"],
                "owner": data["owner"],
                "estimate": data["estimate"],
                "remaining": data["remaining"],
                "priority": data["priority"],
                "tags": data["tags_text"],
                "due_date": data["due_date"],
                "iteration_id": data["iteration_id"],
                "description": data["description"],
            }
        )
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="board-{project}.csv"'},
    )


@app.post("/api/board/import")
async def board_import_csv(request: Request) -> Any:
    project = _board_project(request)
    adapter, _ = _editable_board(project)
    if adapter is None:
        return _read_only_response()
    ctype = request.headers.get("content-type", "")
    text = ""
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            text = str(payload.get("csv", ""))
    elif "multipart/form-data" in ctype or "urlencoded" in ctype:
        form = await request.form()
        upload = form.get("file")
        if upload is not None and hasattr(upload, "read"):
            try:
                raw = await upload.read()  # type: ignore[no-untyped-call]
            except Exception:  # noqa: BLE001
                raw = b""
            if isinstance(raw, (bytes, bytearray)):
                text = bytes(raw).decode("utf-8", errors="replace")
        elif "csv" in form:
            text = str(form.get("csv", ""))
    else:
        raw_body = await request.body()
        text = raw_body.decode("utf-8", errors="replace")
    if not text.strip():
        return JSONResponse(
            {
                "ok": False,
                "message": "CSV body is empty.",
                "hint": "Paste CSV text or attach a CSV file.",
            },
            status_code=400,
        )
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {
                "ok": False,
                "message": "That file could not be read.",
                "hint": "Check it is valid CSV and try again.",
            },
            status_code=400,
        )
    created: list[str] = []
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        title = str(row.get("title", "") or "").strip()
        if not title:
            skipped += 1
            continue
        fields: dict[str, Any] = {}
        for key in BOARD_ITEM_FIELDS:
            value = row.get(key)
            if value is not None and str(value).strip() != "":
                fields[key] = str(value).strip()
        if "state" not in fields:
            fields["state"] = "New"
        try:
            item = adapter.create_item(title, fields)
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        created.append(str(item.id))
        _record_board_write(
            project,
            "create",
            str(item.id),
            f"Human edit on {project} board: imported {title} from CSV.",
            {"title": title, "source": "csv_import"},
        )
    return {"ok": True, "created": len(created), "skipped": skipped, "ids": created}


LAST_KICKOFF: dict[str, Any] = {
    "proposal": [],
    "has_history": False,
    "approved": [],
    "flagged": [],
    "external_id": "",
    "assumptions_count": 0,
}


def _kickoff_result_html(
    items: list[dict[str, Any]],
    has_history: bool,
    roster: list[dict[str, Any]],
    assumptions_count: int = 0,
) -> str:
    parts: list[str] = []
    if not has_history:
        parts.append(
            '<p class="pill">No board history yet. '
            "Owners use names in the brief plus load balancing.</p>"
        )
    try:
        assumed = int(assumptions_count or 0)
    except (TypeError, ValueError):
        assumed = 0
    if assumed > 0:
        word = "assumption" if assumed == 1 else "assumptions"
        parts.append(
            f'<p class="pill">{assumed} {word} proposed to the RAID log. '
            'Review them on the <a href="/raid">RAID page</a>.</p>'
        )
    parts.append('<section class="card"><h2>Approval card</h2>')
    if not items:
        parts.append(
            '<p class="muted">No tasks found. Paste a brief with one '
            "task per line, then choose Suggest owners.</p>"
        )
        parts.append("</section>")
        return "\n".join(parts)
    names = [entry["name"] for entry in roster]
    rows = []
    for index, draft in enumerate(items):
        title = html.escape(str(draft.get("title", "")))
        reason = html.escape(str(draft.get("reason", "")))
        conf = html.escape(str(draft.get("confidence", "low")))
        current = str(draft.get("owner") or "")
        options = ['<option value="">Unassigned</option>']
        for label in names:
            sel = " selected" if label == current else ""
            options.append(
                f'<option value="{html.escape(label)}"{sel}>'
                f"{html.escape(label)}</option>"
            )
        if current and current not in names:
            options.append(
                f'<option value="{html.escape(current)}" selected>'
                f"{html.escape(current)}</option>"
            )
        warn = ""
        if draft.get("warning"):
            warn = ' <span class="pill pill-fail">Over capacity</span>'
        if draft.get("needs_owner"):
            warn += ' <span class="pill pill-fail">Needs an owner</span>'
        est = draft.get("estimate")
        rows.append(
            "<tr>"
            f"<td>{title}</td>"
            f"<td>{html.escape(str(draft.get('type', '')))}</td>"
            f'<td><select name="owner-{index}" '
            f'aria-label="Owner for {title}">{"".join(options)}</select></td>'
            f"<td>{html.escape(str(est) if est is not None else '-')}</td>"
            f'<td>{reason}<br><span class="muted">Confidence: {conf}</span>'
            f"{warn}</td>"
            "</tr>"
        )
    parts.append(
        '<table class="proposal"><thead><tr>'
        '<th scope="col">Title</th><th scope="col">Type</th>'
        '<th scope="col">Owner</th><th scope="col">Est (h)</th>'
        '<th scope="col">Reason</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )
    parts.append(
        '<form hx-post="/api/kickoff/approve" hx-target="#kickoff-approve-result" '
        'hx-swap="innerHTML">'
        '<label class="toggle"><input type="checkbox" name="notify" '
        'value="on" checked>Notify new owners</label>'
        '<div class="form-actions">'
        '<button type="submit">Approve and create these tasks</button>'
        "</div>"
        '<div id="kickoff-approve-result" class="result" aria-live="polite">'
        "</div></form>"
    )
    parts.append("</section>")
    return "\n".join(parts)


@app.get("/kickoff", response_class=HTMLResponse)
def kickoff_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "kickoff")
    project = str(ctx["project"])
    proposal = LAST_KICKOFF.get("proposal", [])
    if proposal:
        ctx["kickoff_html"] = _kickoff_result_html(
            proposal,
            LAST_KICKOFF.get("has_history", False),
            _roster_entries(project),
            LAST_KICKOFF.get("assumptions_count", 0),
        )
    else:
        ctx["kickoff_html"] = ""
    return templates.TemplateResponse(request, "kickoff.html", ctx)  # type: ignore[return-value]


@app.post("/api/kickoff/propose")
async def propose_kickoff_route(request: Request) -> Any:
    from pam_pm.agents.kickoff import intake_document_bytes, propose_kickoff

    project = _board_project(request)
    backend = _project_backend(project)
    form = await request.form()
    text = str(form.get("brief", "") or "")
    upload = form.get("brief_file")
    filename = ""
    data = b""
    if upload is not None and hasattr(upload, "read"):
        try:
            raw = await upload.read()  # type: ignore[no-untyped-call]
        except Exception:  # noqa: BLE001
            raw = b""
        if isinstance(raw, (bytes, bytearray)) and bytes(raw):
            data = bytes(raw)
            filename = str(getattr(upload, "filename", "") or "")
    roster = _roster_entries(project)
    adapter = get_board_adapter(project)
    if data:
        result = intake_document_bytes(data, roster, adapter, filename)
        text = result.get("source_text", text)
    else:
        result = propose_kickoff(text, roster, adapter)
    items = result.get("items", [])
    proposed_assumptions: list[dict[str, Any]] = []
    try:
        writes = WriteService(_db_path())
        proposed_assumptions = raid_mod.propose_assumptions_from_text(
            _db_path(),
            backend,
            project,
            text,
            writes=writes,
        )
    except Exception:  # noqa: BLE001
        proposed_assumptions = []
    for entry in proposed_assumptions:
        try:
            if entry.get("write_id"):
                bus.publish(
                    {
                        "id": int(entry["write_id"]),
                        "operation": "raid_propose_assumption",
                        "item_id": str(entry.get("external_id", "")),
                        "evidence_quote": str(entry.get("evidence_quote", ""))[:500],
                        "status": "proposed",
                        "detail": f"Assumption proposed: {str(entry.get('title', ''))[:200]}",
                    }
                )
        except Exception:  # noqa: BLE001, S110
            pass
    external_id = f"console-{len(text)}-{len(items)}"
    LAST_KICKOFF["proposal"] = items
    LAST_KICKOFF["has_history"] = result.get("has_history", False)
    LAST_KICKOFF["approved"] = []
    LAST_KICKOFF["external_id"] = external_id
    LAST_KICKOFF["assumptions_count"] = len(proposed_assumptions)
    try:
        store_mod.save_kickoff_proposal(
            _db_path(),
            backend,
            external_id,
            project,
            {"items": items, "has_history": result.get("has_history", False)},
        )
    except Exception:  # noqa: BLE001, S110
        pass
    if "hx-request" in request.headers:
        return HTMLResponse(
            _kickoff_result_html(
                items,
                result.get("has_history", False),
                roster,
                len(proposed_assumptions),
            )
        )
    return {
        "ok": True,
        "items": items,
        "has_history": result.get("has_history", False),
        "assumptions_count": len(proposed_assumptions),
        "assumptions": [
            str(entry.get("external_id", "")) for entry in proposed_assumptions
        ],
    }


@app.post("/api/kickoff/approve")
async def approve_kickoff_route(request: Request) -> Any:
    from pam_pm.agents.kickoff import approve_kickoff, notify_owners

    project = _board_project(request)
    proposal = [dict(d) for d in LAST_KICKOFF.get("proposal", [])]
    if not proposal:
        msg = "No kickoff suggestions to create. Paste a brief first."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    payload = await _board_payload(request)
    for index, draft in enumerate(proposal):
        key = f"owner-{index}"
        if key in payload:
            pick = str(payload[key]).strip()
            draft["owner"] = pick or None
            if pick:
                draft["needs_owner"] = False
    adapter = get_board_adapter(project)
    if adapter is None:
        msg = "Board is not configured for this project."
        hint = "Finish the Setup wizard, then approve again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    dry_run, _cap = _effective_safety()
    created = approve_kickoff(
        proposal,
        adapter,
        db=_db_path(),
        dry_run=dry_run,
        evidence_quote="Approved from the kickoff card.",
    )
    done = [{"id": str(item.id), "title": str(item.title)} for item in created]
    LAST_KICKOFF["approved"] = done
    try:
        store_mod.save_kickoff_proposal(
            _db_path(),
            _project_backend(project),
            LAST_KICKOFF.get("external_id", "console"),
            project,
            {"items": proposal},
            status="approved",
        )
    except Exception:  # noqa: BLE001, S110
        pass
    notified = 0
    if str(payload.get("notify", "on")).lower() in ("on", "true", "1", "yes"):
        try:
            from pam_pm.adapters.chat.slack import SlackAdapter

            if os.environ.get("SLACK_BOT_TOKEN", "").strip():
                notified = len(notify_owners(SlackAdapter(), created, proposal))
        except Exception:  # noqa: BLE001
            notified = 0
    message = f"Created {len(done)} tasks."
    if notified:
        message += f" Notified {notified} owners."
    elif str(payload.get("notify", "on")).lower() in ("on", "true", "1", "yes"):
        message += " Owner DMs were skipped (Slack is not configured)."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "created": done, "notified": notified}


@app.post("/api/kickoff/owner-response")
async def kickoff_owner_response(request: Request) -> Any:
    from pam_pm.agents.kickoff import handle_owner_response

    payload = await _board_payload(request)
    item_id = str(payload.get("item_id", "")).strip()
    text = str(payload.get("text", ""))
    if not item_id:
        return JSONResponse(
            {"ok": False, "message": "item_id is required."}, status_code=400
        )
    result = handle_owner_response(item_id, text)
    if result["status"] == "flagged":
        flagged = LAST_KICKOFF.get("flagged", [])
        if item_id not in flagged:
            flagged.append(item_id)
        LAST_KICKOFF["flagged"] = flagged
    return {"ok": True, **result}


LAST_ADD_TASK: dict[str, Any] = {
    "draft": None,
    "message": "",
    "created": [],
}


def _add_task_card_html(
    draft: dict[str, Any],
    roster: list[dict[str, Any]],
    dry_run: bool,
) -> str:
    title = html.escape(str(draft.get("title", "") or "Untitled"))
    owner = str(draft.get("owner") or "")
    reason = html.escape(str(draft.get("reason", "") or ""))
    conf = html.escape(str(draft.get("confidence", "low")))
    estimate = draft.get("estimate")
    if estimate is None:
        est_text = "Not set"
    else:
        try:
            est_text = f"{float(estimate):g}h"
        except (TypeError, ValueError):
            est_text = "Not set"
    quote = html.escape(str(draft.get("evidence_quote", "") or ""))
    names = [str(entry.get("name", "")) for entry in roster if entry.get("name")]
    options = ['<option value="">Unassigned</option>']
    for label in names:
        sel = " selected" if label == owner else ""
        options.append(
            f'<option value="{html.escape(label)}"{sel}>{html.escape(label)}</option>'
        )
    if owner and owner not in names:
        options.append(
            f'<option value="{html.escape(owner)}" selected>'
            f"{html.escape(owner)}</option>"
        )
    pills = ""
    if dry_run:
        pills += '<p class="pill">Dry run is on. Approve proposes first.</p>'
    else:
        pills += '<p class="pill">Dry run is off. Approve creates the task.</p>'
    if draft.get("warning"):
        pills += ' <span class="pill pill-fail">Over capacity</span>'
    if draft.get("needs_owner"):
        pills += ' <span class="pill pill-fail">Needs an owner</span>'
    return (
        '<section class="card" id="add-task-card">'
        "<h2>Draft</h2>"
        f"{pills}"
        '<dl class="card-fields">'
        f"<div><dt>Title</dt><dd>{title}</dd></div>"
        f"<div><dt>Estimate</dt><dd>{html.escape(est_text)}</dd></div>"
        f"<div><dt>Reason</dt><dd>{reason}"
        f'<br><span class="muted">Confidence: {conf}</span></dd></div>'
        "</dl>"
        '<form hx-post="/api/add-task/approve" hx-target="#add-task-approve-result" '
        'hx-swap="innerHTML">'
        '<label for="add-task-owner">Owner</label>'
        f'<select id="add-task-owner" name="owner">{"".join(options)}</select>'
        f'<blockquote class="evidence">{quote}</blockquote>'
        '<div class="form-actions">'
        '<button type="submit">Approve and create task</button>'
        "</div>"
        '<div id="add-task-approve-result" class="result" aria-live="polite">'
        "</div></form>"
        "</section>"
    )


@app.get("/add-task", response_class=HTMLResponse)
def add_task_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "add-task")
    project = str(ctx["project"])
    draft = LAST_ADD_TASK.get("draft")
    if draft:
        dry_run, _ = _effective_safety()
        ctx["add_task_html"] = _add_task_card_html(
            dict(draft), _roster_entries(project), dry_run
        )
    else:
        ctx["add_task_html"] = ""
    return templates.TemplateResponse(request, "add_task.html", ctx)  # type: ignore[return-value]


@app.post("/api/add-task/parse")
async def parse_add_task_route(request: Request) -> Any:
    from pam_pm.agents.add_task import propose_add_task

    project = _board_project(request)
    payload = await _board_payload(request)
    message = str(payload.get("message", "") or "")
    draft = propose_add_task(
        message, _roster_entries(project), get_board_adapter(project)
    )
    if draft is None:
        msg = "That did not look like an add-task request."
        hint = "Start with add task, for example: add a task: fix login, 3h."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=400
        )
    LAST_ADD_TASK["draft"] = draft
    LAST_ADD_TASK["message"] = message
    LAST_ADD_TASK["created"] = []
    dry_run, _ = _effective_safety()
    if "hx-request" in request.headers:
        return HTMLResponse(
            _add_task_card_html(draft, _roster_entries(project), dry_run)
        )
    return {"ok": True, "draft": draft}


@app.post("/api/add-task/approve")
async def approve_add_task_route(request: Request) -> Any:
    from pam_pm.agents.add_task import approve_add_task

    project = _board_project(request)
    stored = LAST_ADD_TASK.get("draft")
    if not stored:
        msg = "No draft to approve. Preview a mention first."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    draft = dict(stored)
    payload = await _board_payload(request)
    if "owner" in payload:
        pick = str(payload["owner"]).strip()
        draft["owner"] = pick or None
        draft["needs_owner"] = not pick
    adapter = get_board_adapter(project)
    if adapter is None:
        msg = "Board is not configured for this project."
        hint = "Finish the Setup wizard, then approve again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=500
        )
    dry_run, _ = _effective_safety()
    try:
        created = approve_add_task(draft, adapter, db=_db_path(), dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        msg = f"Approve failed: {exc}"
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=502)
    done = [{"id": str(item.id), "title": str(item.title)} for item in created]
    LAST_ADD_TASK["created"] = done
    LAST_ADD_TASK["draft"] = None
    message = f"Created task {done[0]['title']}." if done else "Nothing created."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {"ok": True, "created": done}


LAST_TRIAGE: dict[str, Any] = {
    "findings": [],
    "message": "",
    "project": "default",
}


def _triage_project(request: Request) -> str:
    return _safe_project(request.query_params.get("project", "default"))


def _triage_result_html(findings: list[dict[str, Any]], dry_run: bool) -> str:
    parts: list[str] = []
    count = len(findings)
    parts.append('<section class="card" id="triage-card">')
    if count == 0:
        parts.append("<h2>Nothing needs attention</h2>")
        parts.append(
            '<p class="muted">The board scan found no stale, unassigned, '
            "or blocked items. Run triage again after the board changes.</p>"
        )
    else:
        word = "item" if count == 1 else "items"
        parts.append(f"<h2>{count} {word} need attention</h2>")
        if dry_run:
            parts.append(
                '<p class="pill">Dry run is on. Approve proposes a change first.</p>'
            )
        else:
            parts.append(
                '<p class="pill">Dry run is off. Approve applies the change.</p>'
            )
    for index, finding in enumerate(findings):
        title = html.escape(str(finding.get("title", "")))
        kind = html.escape(str(finding.get("kind", "")))
        detail = html.escape(str(finding.get("detail", "")))
        fix = html.escape(str(finding.get("fix_label", "")))
        item_id = html.escape(str(finding.get("item_id", "")))
        parts.append(
            "<article>"
            f"<h3>{title}</h3>"
            f'<p class="muted">{kind} &middot; item {item_id} &middot; {detail}</p>'
            f"<p>Proposed fix: {fix}</p>"
            f'<button hx-post="/api/triage/approve?index={index}" '
            f'hx-target="#triage-result-{index}" hx-swap="innerHTML">'
            "Approve</button>"
            f'<span id="triage-result-{index}" class="result" aria-live="polite">'
            "</span></article>"
        )
    parts.append("</section>")
    return "\n".join(parts)


@app.get("/triage", response_class=HTMLResponse)
def triage_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "triage")
    project = str(ctx["project"])
    dry_run, _ = _effective_safety()
    if LAST_TRIAGE.get("project") == project:
        stored = [dict(entry) for entry in LAST_TRIAGE.get("findings", [])]
    else:
        stored = []
    ctx["triage_html"] = _triage_result_html(stored, dry_run)
    return templates.TemplateResponse(request, "triage.html", ctx)  # type: ignore[return-value]


@app.post("/api/triage/run")
def triage_run_route(request: Request) -> Any:
    from pam_pm.rules.templates import triage as triage_mod

    project = _triage_project(request)
    adapter = get_board_adapter(project)
    if adapter is None:
        msg = "The board is not configured yet."
        hint = "Set ADO_ORG_URL, ADO_PROJECT, and ADO_PAT, then try again."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, hint))
        return JSONResponse(
            {"ok": False, "message": msg, "hint": hint}, status_code=502
        )
    roster = [entry["name"] for entry in _roster_entries(project)]
    backend = _project_backend(project)
    try:
        findings = triage_mod.scan_board(adapter, roster=roster)
        findings.extend(triage_mod.scan_raid(_db_path(), backend, project))
    except Exception as exc:  # noqa: BLE001
        logger.warning("triage scan failed: %s", exc)
        msg = "The triage scan could not be read."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg, "Try again in a moment."))
        return JSONResponse({"ok": False, "message": msg}, status_code=502)
    LAST_TRIAGE["findings"] = findings
    LAST_TRIAGE["message"] = triage_mod.format_board_triage_message(findings)
    LAST_TRIAGE["project"] = project
    dry_run, _ = _effective_safety()
    if "hx-request" in request.headers:
        return HTMLResponse(_triage_result_html(findings, dry_run))
    return {
        "ok": True,
        "project": project,
        "count": len(findings),
        "findings": findings,
    }


@app.post("/api/triage/approve")
async def triage_approve_route(request: Request) -> Any:
    from pam_pm.rules.templates import triage as triage_mod

    project = request.query_params.get("project", "").strip()
    if not project:
        project = str(LAST_TRIAGE.get("project", "default"))
    findings = [dict(entry) for entry in LAST_TRIAGE.get("findings", [])]
    index = await _retro_index(request)
    if index is None or index < 0 or index >= len(findings):
        msg = "Unknown triage item. Run triage first."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=404)
    adapter = get_board_adapter(project)
    if adapter is None:
        msg = "The board is not configured yet."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=502)
    dry_run, _ = _effective_safety()
    writes = WriteService(_db_path(), dry_run=dry_run)
    result = triage_mod.apply_finding(
        adapter,
        findings[index],
        db=_db_path(),
        backend=_project_backend(project),
        project=project,
        writes=writes,
        dry_run=dry_run,
    )
    if "hx-request" in request.headers:
        if result.get("applied"):
            return HTMLResponse(_test_result_html(True, "Applied."))
        if dry_run and result.get("write_id"):
            return HTMLResponse(
                _test_result_html(True, "Proposed. Dry run is on, so nothing changed.")
            )
        detail = str(result.get("detail", ""))
        if detail:
            return HTMLResponse(_test_result_html(False, detail))
        return HTMLResponse(
            _test_result_html(False, "Approve did not work. Try again.")
        )
    return {
        "ok": bool(result.get("applied") or result.get("write_id")),
        **result,
    }


RAID_KINDS: tuple[tuple[str, str], ...] = (
    ("risk", "Risks"),
    ("assumption", "Assumptions"),
    ("issue", "Issues"),
    ("dependency", "Dependencies"),
)


def _raid_project(request: Request) -> str:
    return _safe_project(request.query_params.get("project", "default"))


def _record_raid_write(
    project: str,
    operation: str,
    item_id: str,
    evidence: str,
    detail_text: str = "",
) -> dict[str, Any]:
    detail = {"actor": "human", "project": project}
    if detail_text:
        detail["detail"] = detail_text
    try:
        write_id = store_mod.insert_write(
            _db_path(),
            {
                "operation": operation,
                "item_id": str(item_id),
                "evidence_quote": evidence,
                "status": "applied",
                "detail": json.dumps(detail),
            },
        )
    except Exception:  # noqa: BLE001
        past = bus.history()
        write_id = int(past[-1].get("id", 0)) + 1 if past else 1
    event = {
        "id": write_id,
        "operation": operation,
        "item_id": str(item_id),
        "evidence_quote": evidence,
        "status": "applied",
        "detail": json.dumps(detail),
    }
    bus.publish(event)
    logger.info("raid write: %s %s on %s", operation, item_id, project)
    return event


def _raid_linked_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value or "").replace(",", " ").replace(";", " ")
    return [part for part in (p.strip() for p in text.split()) if part]


@app.get("/raid", response_class=HTMLResponse)
def raid_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "raid")
    project = str(ctx["project"])
    backend = _project_backend(project)
    try:
        items = raid_mod.list_items(_db_path(), backend, project=project)
    except Exception:  # noqa: BLE001
        items = []
    grouped = [
        {
            "key": key,
            "title": title,
            "cards": [entry for entry in items if entry["kind"] == key],
        }
        for key, title in RAID_KINDS
    ]
    ctx["groups"] = grouped
    ctx["raid_error"] = ""
    ctx["kinds"] = [key for key, _ in RAID_KINDS]
    ctx["statuses"] = list(raid_mod.STATUSES)
    ctx["severities"] = list(raid_mod.SEVERITIES)
    return templates.TemplateResponse(request, "raid.html", ctx)  # type: ignore[return-value]


@app.get("/api/raid")
def raid_list(request: Request) -> Any:
    project = _raid_project(request)
    backend = _project_backend(project)
    kind = request.query_params.get("kind", "").strip() or None
    status = request.query_params.get("status", "").strip() or None
    try:
        items = raid_mod.list_items(
            _db_path(), backend, project=project, kind=kind, status=status
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("raid list failed: %s", exc)
        return JSONResponse(
            {"ok": False, "message": "The RAID log could not be loaded."},
            status_code=502,
        )
    return {"ok": True, "project": project, "items": items}


@app.post("/api/raid")
async def raid_create(request: Request) -> Any:
    project = _raid_project(request)
    backend = _project_backend(project)
    payload = await _board_payload(request)
    title = str(payload.get("title", "")).strip()
    if not title:
        return JSONResponse(
            {"ok": False, "message": "Title is required."}, status_code=400
        )
    kind = raid_mod.normalize_kind(payload.get("kind", "risk"))
    try:
        item = raid_mod.save_item(
            _db_path(),
            backend,
            raid_mod.new_external_id(kind),
            project=project,
            kind=kind,
            title=title,
            owner=str(payload.get("owner", "")).strip(),
            severity=str(payload.get("severity", "medium")),
            status="open",
            due_date=str(payload.get("due_date", "")).strip(),
            linked_ids=_raid_linked_ids(payload.get("linked_ids", "")),
            evidence_quote=str(payload.get("evidence_quote", "")).strip(),
            evidence_date=str(payload.get("evidence_date", "")).strip(),
        )
    except Exception as exc:  # noqa: BLE001
        return _board_error_response(exc)
    event = _record_raid_write(
        project,
        "raid_create",
        str(item["external_id"]),
        str(item["evidence_quote"] or title)[:500],
        f"RAID {kind} added: {title[:200]}",
    )
    return {"ok": True, "item": item, "write_id": event["id"]}


@app.patch("/api/raid/{external_id}")
async def raid_edit(request: Request, external_id: str) -> Any:
    project = _raid_project(request)
    backend = _project_backend(project)
    payload = await _board_payload(request)
    fields: dict[str, Any] = {}
    for key in (
        "title",
        "owner",
        "severity",
        "status",
        "due_date",
        "evidence_quote",
        "evidence_date",
        "kind",
        "project",
    ):
        if key in payload and payload[key] is not None:
            fields[key] = payload[key]
    if "linked_ids" in payload and payload["linked_ids"] is not None:
        fields["linked_ids"] = _raid_linked_ids(payload["linked_ids"])
    if not fields:
        return JSONResponse(
            {"ok": False, "message": "Nothing changed."}, status_code=400
        )
    updated = raid_mod.update_item(
        db=_db_path(), backend=backend, external_id=external_id, fields=fields
    )
    if updated is None:
        return JSONResponse(
            {"ok": False, "message": f"Entry {external_id} was not found."},
            status_code=404,
        )
    event = _record_raid_write(
        project,
        "raid_update",
        external_id,
        str(updated["evidence_quote"] or updated["title"])[:500],
        f"RAID {updated['kind']} updated: {str(updated['title'])[:200]}",
    )
    return {"ok": True, "item": updated, "write_id": event["id"]}


@app.post("/api/raid/{external_id}/accept")
def raid_accept(request: Request, external_id: str) -> Any:
    project = _raid_project(request)
    backend = _project_backend(project)
    current = raid_mod.get_item(_db_path(), backend, external_id)
    if current is None:
        return JSONResponse(
            {"ok": False, "message": f"Entry {external_id} was not found."},
            status_code=404,
        )
    if current["status"] != "proposed":
        return JSONResponse(
            {"ok": False, "message": "Only proposed entries can be accepted."},
            status_code=400,
        )
    updated = raid_mod.update_item(_db_path(), backend, external_id, {"status": "open"})
    if updated is None:
        return JSONResponse(
            {"ok": False, "message": f"Entry {external_id} was not found."},
            status_code=404,
        )
    if current.get("write_id"):
        try:
            store_mod.set_write_status(_db_path(), int(current["write_id"]), "applied")
        except Exception:  # noqa: BLE001, S110
            pass
    event = _record_raid_write(
        project,
        "raid_accept",
        external_id,
        str(updated["evidence_quote"] or updated["title"])[:500],
        f"RAID {updated['kind']} accepted: {str(updated['title'])[:200]}",
    )
    return {"ok": True, "item": updated, "write_id": event["id"]}


@app.post("/api/raid/{external_id}/close")
def raid_close(request: Request, external_id: str) -> Any:
    project = _raid_project(request)
    backend = _project_backend(project)
    current = raid_mod.get_item(_db_path(), backend, external_id)
    if current is None:
        return JSONResponse(
            {"ok": False, "message": f"Entry {external_id} was not found."},
            status_code=404,
        )
    if current["status"] == "closed":
        return JSONResponse(
            {"ok": False, "message": "That entry is already closed."},
            status_code=400,
        )
    updated = raid_mod.update_item(
        _db_path(), backend, external_id, {"status": "closed"}
    )
    if updated is None:
        return JSONResponse(
            {"ok": False, "message": f"Entry {external_id} was not found."},
            status_code=404,
        )
    event = _record_raid_write(
        project,
        "raid_close",
        external_id,
        str(updated["evidence_quote"] or updated["title"])[:500],
        f"RAID {updated['kind']} closed: {str(updated['title'])[:200]}",
    )
    return {"ok": True, "item": updated, "write_id": event["id"]}


@app.delete("/api/raid/{external_id}")
def raid_delete(request: Request, external_id: str) -> Any:
    project = _raid_project(request)
    backend = _project_backend(project)
    current = raid_mod.get_item(_db_path(), backend, external_id)
    if current is None:
        return JSONResponse(
            {"ok": False, "message": f"Entry {external_id} was not found."},
            status_code=404,
        )
    ok = raid_mod.delete_item(_db_path(), backend, external_id)
    if not ok:
        return JSONResponse(
            {"ok": False, "message": f"Entry {external_id} was not found."},
            status_code=404,
        )
    event = _record_raid_write(
        project,
        "raid_delete",
        external_id,
        str(current["evidence_quote"] or current["title"])[:500],
        f"RAID {current['kind']} deleted: {str(current['title'])[:200]}",
    )
    return {"ok": True, "write_id": event["id"]}


@app.get("/api/raid/export")
def raid_export(request: Request) -> Any:
    project = _raid_project(request)
    backend = _project_backend(project)
    fmt = request.query_params.get("format", "csv").strip().lower()
    try:
        items = raid_mod.list_items(_db_path(), backend, project=project)
    except Exception as exc:  # noqa: BLE001
        logger.warning("raid export failed: %s", exc)
        return JSONResponse(
            {"ok": False, "message": "The RAID log could not be loaded."},
            status_code=502,
        )
    if fmt in ("md", "markdown"):
        body = raid_mod.export_markdown(items, for_lead=True)
        return Response(
            body,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="raid-{project}.md"'
            },
        )
    body = raid_mod.export_csv(items, for_lead=True)
    return Response(
        body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="raid-{project}.csv"'},
    )


def _listen_board_items(project: str) -> list[Any]:
    try:
        adapter = get_board_adapter(project)
    except Exception:  # noqa: BLE001
        return []
    if adapter is None:
        return []
    getter = getattr(adapter, "list_items", None)
    if not callable(getter):
        return []
    try:
        return list(getter() or [])
    except Exception:  # noqa: BLE001
        return []


def _notes_approval_html(
    payload: dict[str, Any],
    roster: list[dict[str, Any]],
    session_id: str,
    project: str,
) -> str:
    parts: list[str] = []
    summary = html.escape(str(payload.get("summary", "") or "No transcript yet."))
    parts.append('<section class="card"><h2>Approval card</h2>')
    parts.append(f"<p>{summary}</p>")
    decisions = payload.get("decisions", []) or []
    parts.append("<h3>Decisions</h3>")
    if decisions:
        items = "".join(
            f"<li>{html.escape(str(d.get('text', '')))} "
            f'<span class="muted">Evidence: '
            f"{html.escape(str(d.get('evidence_quote', '')))}</span></li>"
            for d in decisions
            if isinstance(d, dict)
        )
        parts.append(f"<ul>{items}</ul>")
    else:
        parts.append('<p class="muted">No decisions recorded.</p>')
    actions = payload.get("action_items", []) or []
    parts.append("<h3>Action items</h3>")
    names = [str(e.get("name", "")) for e in roster if str(e.get("name", ""))]
    if not actions:
        parts.append('<p class="muted">No action items recorded.</p>')
    else:
        rows = []
        for index, draft in enumerate(actions):
            if not isinstance(draft, dict):
                continue
            title = html.escape(str(draft.get("title", "")))
            current = str(draft.get("owner") or "")
            due = html.escape(str(draft.get("due_date", "") or "none"))
            quote = html.escape(str(draft.get("evidence_quote", "")))
            conf = draft.get("confidence", 0.5)
            options = ['<option value="">Unassigned</option>']
            for label in names:
                sel = " selected" if label == current else ""
                options.append(
                    f'<option value="{html.escape(label)}"{sel}>'
                    f"{html.escape(label)}</option>"
                )
            if current and current not in names:
                options.append(
                    f'<option value="{html.escape(current)}" selected>'
                    f"{html.escape(current)}</option>"
                )
            rows.append(
                "<tr>"
                f'<td>{title}<br><span class="muted">Evidence: {quote}</span></td>'
                f'<td><select name="owner-{index}" '
                f'aria-label="Owner for {title}">{"".join(options)}</select></td>'
                f"<td>{due}</td>"
                f"<td>{html.escape(str(conf))}</td>"
                "</tr>"
            )
        parts.append(
            '<table class="proposal">'
            '<caption class="muted">Action items with owner, due date, and confidence.</caption>'
            "<thead><tr>"
            '<th scope="col">Title</th><th scope="col">Owner</th>'
            '<th scope="col">Due</th><th scope="col">Confidence</th>'
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )
    matches = payload.get("matches", []) or []
    parts.append("<h3>Matches</h3>")
    if matches:
        items = "".join(
            f"<li>{html.escape(str(m.get('note_title', '')))} matches "
            f"{html.escape(str(m.get('item_id', '')))}. "
            f"{html.escape(str(m.get('reason', '')))}</li>"
            for m in matches
            if isinstance(m, dict)
        )
        parts.append(f"<ul>{items}</ul>")
    else:
        parts.append('<p class="muted">No matches to existing items.</p>')
    raid = payload.get("proposed_raid", []) or []
    parts.append("<h3>Proposed RAID</h3>")
    if raid:
        items = "".join(
            f"<li>[{html.escape(str(r.get('kind', 'risk')))}] "
            f"{html.escape(str(r.get('title', '')))}</li>"
            for r in raid
            if isinstance(r, dict)
        )
        parts.append(f"<ul>{items}</ul>")
    else:
        parts.append('<p class="muted">No RAID entries proposed.</p>')
    parts.append(
        f'<form hx-post="/api/meetings/{html.escape(session_id)}/approve?project={html.escape(project)}" '
        'hx-target="#notes-approve-result" hx-swap="innerHTML">'
        '<label class="toggle"><input type="checkbox" name="notify" '
        'value="on" checked>DM action owners</label>'
        '<label class="toggle"><input type="checkbox" name="slack_post" '
        'value="on">Post notes to Slack channel</label>'
        '<label>Channel <input type="text" name="channel" placeholder="#standup"></label>'
        '<p class="muted">Board writes are proposed first and logged with an evidence quote.</p>'
        '<div class="form-actions">'
        '<button type="submit">Approve and create action items</button>'
        "</div>"
        '<div id="notes-approve-result" class="result" aria-live="polite"></div>'
        "</form>"
    )
    parts.append("</section>")
    return "\n".join(parts)


def _listen_board_owners(project: str) -> list[str]:
    owners: list[str] = []
    for item in _listen_board_items(project):
        if isinstance(item, dict):
            raw = item.get("assigned_to", item.get("owner", ""))
        else:
            fields = getattr(item, "fields", {}) or {}
            raw = fields.get("assigned_to", fields.get("owner", ""))
        name = str(raw or "").strip()
        if name and name not in owners:
            owners.append(name)
    return owners


def _listen_team_people(project: str) -> list[dict[str, Any]]:
    from pam_pm.core import memory as memory_mod

    try:
        people = [dict(p) for p in memory_mod.list_people(_db_path())]
    except Exception:  # noqa: BLE001
        people = []
    seen = {str(p.get("name", "")).strip().lower() for p in people}
    for entry in _roster_entries(project):
        name = str(entry.get("name", "")).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        people.append(
            {
                "name": name,
                "role": "",
                "skillsets": str(entry.get("skills", "") or ""),
            }
        )
    return people


def _listen_proposals_html(
    proposals: list[dict[str, Any]],
    roster: list[dict[str, Any]],
    session_id: str,
    project: str,
) -> str:
    if not proposals:
        return (
            '<section class="card"><h2>Proposed tasks</h2>'
            '<p class="muted">No tasks proposed yet.</p></section>'
        )
    names = [str(e.get("name", "")) for e in roster if str(e.get("name", ""))]
    cards: list[str] = []
    for index, draft in enumerate(proposals):
        title = str(draft.get("title", ""))
        owner = str(draft.get("owner") or "")
        try:
            hours = float(draft.get("estimate", 2.0))
        except (TypeError, ValueError):
            hours = 2.0
        quote = str(draft.get("evidence_quote", ""))
        options = ['<option value="">Unassigned</option>']
        for label in names:
            sel = " selected" if label == owner else ""
            options.append(
                f'<option value="{html.escape(label)}"{sel}>'
                f"{html.escape(label)}</option>"
            )
        if owner and owner not in names:
            options.append(
                f'<option value="{html.escape(owner)}" selected>'
                f"{html.escape(owner)}</option>"
            )
        cards.append(
            "<article>"
            f"<h3><label>"
            f'<input type="checkbox" name="selected-{index}" '
            f'value="on" checked> Proposal {index + 1}</label></h3>'
            f"<label>Title "
            f'<input type="text" name="title-{index}" '
            f'value="{html.escape(title)}" maxlength="200"></label>'
            f"<label>Owner "
            f'<select name="owner-{index}" '
            f'aria-label="Owner for proposal {index + 1}">'
            f"{''.join(options)}</select></label>"
            f"<label>Estimate (hours) "
            f'<input type="number" name="estimate-{index}" '
            f'value="{hours:g}" min="0" max="80" step="0.5"></label>'
            f'<p class="muted">Evidence: {html.escape(quote)}</p>'
            "</article>"
        )
    sid = html.escape(session_id)
    proj = html.escape(project)
    return (
        '<section class="card"><h2>Proposed tasks</h2>'
        '<p class="muted">Edit title, owner, and estimate inline. '
        "Nothing is posted until you approve.</p>"
        f'<form hx-post="/api/meetings/{sid}/proposals/approve'
        f'?project={proj}" hx-target="#listen-proposals-result" '
        'hx-swap="innerHTML">' + "".join(cards) + '<div class="form-actions">'
        '<button type="submit">Approve selected</button>'
        "</div></form>"
        '<div class="form-actions">'
        f'<button hx-post="/api/meetings/{sid}/proposals/approve'
        f'?project={proj}&amp;all=1" '
        'hx-target="#listen-proposals-result" hx-swap="innerHTML">'
        "Approve all</button>"
        f'<button hx-post="/api/meetings/{sid}/proposals/discard'
        f'?project={proj}" hx-target="#listen-proposals-result" '
        'hx-swap="innerHTML">Discard</button>'
        "</div>"
        '<div id="listen-proposals-result" class="result" '
        'aria-live="polite"></div></section>'
    )


@app.get("/listen", response_class=HTMLResponse)
def listen_page(request: Request) -> HTMLResponse:
    import uuid as _uuid

    ctx = _context(request, "listen")
    ctx["listen_token"] = get_setup_token()
    ctx["listen_session"] = "listen-" + _uuid.uuid4().hex[:12]
    ctx["listen_title"] = ""
    return templates.TemplateResponse(request, "listen.html", ctx)  # type: ignore[return-value]


@app.get("/meetings", response_class=HTMLResponse)
def meetings_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "meetings")
    project = str(ctx["project"])
    try:
        ctx["meetings"] = store_mod.list_meetings(_db_path(), project)
    except Exception:  # noqa: BLE001
        ctx["meetings"] = []
    return templates.TemplateResponse(request, "meetings.html", ctx)  # type: ignore[return-value]


@app.get("/meetings/{session_id}", response_class=HTMLResponse)
def meeting_detail_page(request: Request, session_id: str) -> HTMLResponse:
    ctx = _context(request, "meetings")
    project = str(ctx["project"])
    detail = None
    try:
        detail = store_mod.get_meeting_detail(_db_path(), session_id)
    except Exception:  # noqa: BLE001
        detail = None
    if detail is None:
        return templates.TemplateResponse(  # type: ignore[return-value]
            request,
            "meeting.html",
            {
                **ctx,
                "session": {"session_id": session_id, "status": "missing"},
                "segments": [],
                "notes_html": "",
            },
        )
    notes = detail.get("notes") or {}
    payload = notes.get("payload", {}) if isinstance(notes, dict) else {}
    notes_html = ""
    if payload:
        notes_html = _notes_approval_html(
            payload, _roster_entries(project), session_id, project
        )
    ctx.update(
        {
            "session": detail.get("session", {}),
            "segments": detail.get("segments", []),
            "notes_html": notes_html,
        }
    )
    return templates.TemplateResponse(request, "meeting.html", ctx)  # type: ignore[return-value]


@app.get("/api/meetings")
def meetings_list_api(request: Request) -> Any:
    project = _board_project(request)
    try:
        return {"ok": True, "meetings": store_mod.list_meetings(_db_path(), project)}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": str(exc)[:300]}, status_code=500)


@app.get("/api/meetings/{session_id}")
def meeting_detail_api(request: Request, session_id: str) -> Any:
    try:
        detail = store_mod.get_meeting_detail(_db_path(), session_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": str(exc)[:300]}, status_code=500)
    if detail is None:
        return JSONResponse(
            {"ok": False, "message": "Meeting not found."}, status_code=404
        )
    return {"ok": True, **detail}


@app.post("/api/meetings/{session_id}/notes")
async def meeting_notes_route(request: Request, session_id: str) -> Any:
    from pam_pm.agents import notes as notes_mod

    project = _board_project(request)
    try:
        detail = store_mod.get_meeting_detail(_db_path(), session_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": str(exc)[:300]}, status_code=500)
    if detail is None:
        return JSONResponse(
            {"ok": False, "message": "Meeting not found."}, status_code=404
        )
    transcript = str(detail.get("transcript", "") or "")
    existing = _listen_board_items(project)
    roster = _roster_entries(project)
    context = notes_mod.build_project_context(existing, roster)
    result = notes_mod.extract_notes(
        transcript,
        project_context=context,
        existing_items=existing,
        roster=roster,
    )
    try:
        store_mod.save_listen_notes(_db_path(), session_id, project, result)
    except Exception:  # noqa: BLE001, S110
        pass
    if "hx-request" in request.headers:
        return HTMLResponse(_notes_approval_html(result, roster, session_id, project))
    return {"ok": True, "notes": result}


@app.post("/api/meetings/{session_id}/approve")
async def meeting_approve_route(request: Request, session_id: str) -> Any:
    from pam_pm.agents import notes as notes_mod
    from pam_pm.agents.kickoff import notify_owners

    project = _board_project(request)
    try:
        saved = store_mod.get_listen_notes(_db_path(), session_id)
    except Exception:  # noqa: BLE001
        saved = None
    if saved is None or not (saved.get("payload") or {}).get("action_items"):
        msg = "No notes to approve. Generate notes first."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    actions = [dict(a) for a in saved["payload"]["action_items"]]
    payload = await _board_payload(request)
    for index, draft in enumerate(actions):
        key = f"owner-{index}"
        if key in payload:
            pick = str(payload[key]).strip()
            draft["owner"] = pick or None
    adapter = get_board_adapter(project)
    if adapter is None:
        msg = "Board is not configured for this project."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=500)
    dry_run, _cap = _effective_safety()
    created = notes_mod.create_action_items(
        adapter,
        actions,
        db=_db_path(),
        dry_run=dry_run,
    )
    try:
        store_mod.set_listen_notes_status(_db_path(), session_id, "approved")
    except Exception:  # noqa: BLE001, S110
        pass
    notified = 0
    if str(payload.get("notify", "on")).lower() in ("on", "true", "1", "yes"):
        try:
            from pam_pm.adapters.chat.slack import SlackAdapter

            if os.environ.get("SLACK_BOT_TOKEN", "").strip():
                notified = len(notify_owners(SlackAdapter(), created, actions))
        except Exception:  # noqa: BLE001
            notified = 0
    posted = ""
    if str(payload.get("slack_post", "off")).lower() in ("on", "true", "1", "yes"):
        channel = str(payload.get("channel", "") or "").strip()
        if channel:
            try:
                from pam_pm.adapters.chat.slack import SlackAdapter

                if os.environ.get("SLACK_BOT_TOKEN", "").strip():
                    markdown = str(saved["payload"].get("markdown", "") or "")[:3000]
                    posted = SlackAdapter().send_dm(
                        channel, markdown or "Meeting notes."
                    )
            except Exception:  # noqa: BLE001
                posted = ""
    message = f"Created {len(created)} action items."
    if notified:
        message += f" Notified {notified} owners."
    if posted:
        message += " Posted notes to Slack."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {
        "ok": True,
        "created": len(created),
        "notified": notified,
        "posted": bool(posted),
    }


@app.post("/api/meetings/{session_id}/finish")
async def meeting_finish_route(request: Request, session_id: str) -> Any:
    """Auto pipeline: transcribe saved finals, summarize, propose tasks."""
    from pam_pm.agents import listen_pipeline as pipeline_mod
    from pam_pm.agents import notes as notes_mod

    project = _board_project(request)
    try:
        detail = store_mod.get_meeting_detail(_db_path(), session_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": str(exc)[:300]}, status_code=500)
    if detail is None:
        return JSONResponse(
            {"ok": False, "message": "Meeting not found."}, status_code=404
        )
    transcript = str(detail.get("transcript", "") or "")
    existing = _listen_board_items(project)
    roster = _roster_entries(project)
    context = notes_mod.build_project_context(existing, roster)
    result = notes_mod.extract_notes(
        transcript,
        project_context=context,
        existing_items=existing,
        roster=roster,
    )
    try:
        store_mod.save_listen_notes(_db_path(), session_id, project, result)
    except Exception:  # noqa: BLE001, S110
        pass
    people = _listen_team_people(project)
    owners = _listen_board_owners(project)
    proposals = pipeline_mod.propose_listen_tasks(
        transcript, result, people, owners, session_id
    )
    try:
        store_mod.save_listen_proposals(_db_path(), session_id, project, proposals)
    except Exception:  # noqa: BLE001, S110
        pass
    if "hx-request" in request.headers:
        return HTMLResponse(
            _listen_proposals_html(proposals, roster, session_id, project)
        )
    return {
        "ok": True,
        "session_id": session_id,
        "transcript_chars": len(transcript),
        "summary": str(result.get("summary", "")),
        "proposals": proposals,
    }


@app.get("/api/meetings/{session_id}/proposals")
def meeting_proposals_api(request: Request, session_id: str) -> Any:
    _ = _board_project(request)
    try:
        saved = store_mod.get_listen_proposals(_db_path(), session_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": str(exc)[:300]}, status_code=500)
    if saved is None:
        return JSONResponse(
            {"ok": False, "message": "No proposals yet."}, status_code=404
        )
    return {"ok": True, **saved}


def _proposal_edits(payload: dict[str, Any], proposals: list[dict[str, Any]]) -> None:
    for index, draft in enumerate(proposals):
        title_key, owner_key, est_key = (
            f"title-{index}",
            f"owner-{index}",
            f"estimate-{index}",
        )
        if title_key in payload:
            title = str(payload[title_key]).strip()
            if title:
                draft["title"] = title[:200]
        if owner_key in payload:
            draft["owner"] = str(payload[owner_key]).strip() or None
        if est_key in payload:
            try:
                draft["estimate"] = max(0.0, min(80.0, float(payload[est_key])))
            except (TypeError, ValueError):
                pass
    raw_list = payload.get("proposals")
    if isinstance(raw_list, list):
        for pos, edit in enumerate(raw_list):
            if pos < len(proposals) and isinstance(edit, dict):
                if str(edit.get("title", "")).strip():
                    proposals[pos]["title"] = str(edit["title"]).strip()[:200]
                if "owner" in edit:
                    proposals[pos]["owner"] = str(edit["owner"]).strip() or None
                if "estimate" in edit:
                    try:
                        proposals[pos]["estimate"] = max(
                            0.0, min(80.0, float(edit["estimate"]))
                        )
                    except (TypeError, ValueError):
                        pass


def _proposal_selection(payload: dict[str, Any], count: int) -> list[int]:
    picked: list[int] = []
    raw = payload.get("selected")
    if isinstance(raw, list):
        for value in raw:
            try:
                picked.append(int(value))
            except (TypeError, ValueError):
                continue
    for index in range(count):
        if str(payload.get(f"selected-{index}", "")).lower() in (
            "on",
            "1",
            "true",
            "yes",
        ):
            picked.append(index)
    return sorted({i for i in picked if 0 <= i < count})


@app.post("/api/meetings/{session_id}/proposals/approve")
async def meeting_proposals_approve_route(request: Request, session_id: str) -> Any:
    from pam_pm.agents import listen_pipeline as pipeline_mod

    project = _board_project(request)
    try:
        saved = store_mod.get_listen_proposals(_db_path(), session_id)
    except Exception:  # noqa: BLE001
        saved = None
    if (
        saved is None
        or saved.get("status") == "discarded"
        or not saved.get("proposals")
    ):
        msg = "No proposals to approve. Finish the meeting first."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    proposals = [dict(p) for p in saved["proposals"]]
    payload = await _board_payload(request)
    _proposal_edits(payload, proposals)
    want_all = request.query_params.get("all", "").lower() in ("1", "true", "on", "yes")
    if not want_all:
        want_all = str(payload.get("all", "")).lower() in ("1", "true", "on", "yes")
    selected = (
        list(range(len(proposals)))
        if want_all
        else _proposal_selection(payload, len(proposals))
    )
    if not selected:
        msg = "Select at least one proposal."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    adapter = get_board_adapter(project)
    if adapter is None:
        msg = "Board is not configured for this project."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=500)
    try:
        detail = store_mod.get_meeting_detail(_db_path(), session_id)
    except Exception:  # noqa: BLE001
        detail = None
    transcript = str((detail or {}).get("transcript", "") or "")
    title = str(((detail or {}).get("session", {}) or {}).get("title", "") or "")
    dry_run, _cap = _effective_safety()
    created = pipeline_mod.approve_listen_proposals(
        adapter,
        proposals,
        selected,
        session_id,
        transcript,
        db=_db_path(),
        dry_run=dry_run,
        meeting_title=title,
    )
    status = "approved" if len(selected) == len(proposals) else "partial"
    try:
        store_mod.save_listen_proposals(
            _db_path(), session_id, project, proposals, status=status
        )
    except Exception:  # noqa: BLE001, S110
        pass
    message = f"Created {len(created)} of {len(proposals)} proposed tasks."
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, message))
    return {
        "ok": True,
        "created": len(created),
        "total": len(proposals),
        "status": status,
    }


@app.post("/api/meetings/{session_id}/proposals/discard")
async def meeting_proposals_discard_route(request: Request, session_id: str) -> Any:
    project = _board_project(request)
    try:
        saved = store_mod.get_listen_proposals(_db_path(), session_id)
    except Exception:  # noqa: BLE001
        saved = None
    if saved is None:
        msg = "No proposals to discard."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=404)
    try:
        store_mod.save_listen_proposals(
            _db_path(), session_id, project, [], status="discarded"
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": str(exc)[:300]}, status_code=500)
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, "Discarded. Nothing was posted."))
    return {"ok": True, "discarded": True}


@app.post("/api/meetings/{session_id}/proposals/{index}")
async def meeting_proposal_edit_route(
    request: Request, session_id: str, index: int
) -> Any:
    project = _board_project(request)
    try:
        saved = store_mod.get_listen_proposals(_db_path(), session_id)
    except Exception:  # noqa: BLE001
        saved = None
    if saved is None or saved.get("status") == "discarded":
        return JSONResponse(
            {"ok": False, "message": "No proposals to edit."}, status_code=400
        )
    proposals = [dict(p) for p in saved.get("proposals", [])]
    if index < 0 or index >= len(proposals):
        return JSONResponse(
            {"ok": False, "message": "Unknown proposal."}, status_code=404
        )
    payload = await _board_payload(request)
    draft = proposals[index]
    if str(payload.get("title", "")).strip():
        draft["title"] = str(payload["title"]).strip()[:200]
    if "owner" in payload:
        draft["owner"] = str(payload["owner"]).strip() or None
    if "estimate" in payload:
        try:
            draft["estimate"] = max(0.0, min(80.0, float(payload["estimate"])))
        except (TypeError, ValueError):
            pass
    try:
        store_mod.save_listen_proposals(
            _db_path(),
            session_id,
            project,
            proposals,
            status=str(saved.get("status", "proposed")),
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": str(exc)[:300]}, status_code=500)
    return {"ok": True, "index": index, "proposal": draft}


@app.delete("/api/meetings/{session_id}")
def meeting_delete_api(session_id: str) -> Any:
    try:
        found = store_mod.delete_meeting(_db_path(), session_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": str(exc)[:300]}, status_code=500)
    if not found:
        return JSONResponse(
            {"ok": False, "message": "Meeting not found."}, status_code=404
        )
    return {"ok": True}


def _listen_token_ok(websocket: WebSocket) -> bool:
    token = websocket.query_params.get("token", "").strip()
    if not token:
        token = websocket.cookies.get("pam_setup_token", "").strip()
    return is_valid_token(token)


@app.websocket("/ws/listen")
async def listen_socket(websocket: WebSocket) -> None:
    """Relay browser audio to Meta realtime and transcripts back.

    Auth is the console session token as ?token= or cookie. The Meta
    API key never reaches the browser. Audio frames from the browser
    are raw bytes; control frames are JSON text with a type field.
    """
    if not _listen_token_ok(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    params = websocket.query_params
    project = (params.get("project", "default") or "default").strip() or "default"
    title = (params.get("title", "") or "").strip()
    mode = (params.get("mode", "PUSH_TO_TALK") or "PUSH_TO_TALK").strip()
    session_id = (params.get("session_id", "") or "").strip()
    if not session_id:
        import uuid

        session_id = "listen-" + uuid.uuid4().hex[:12]
    try:
        store_mod.create_listen_session(_db_path(), session_id, project, title)
    except Exception as exc:  # noqa: BLE001
        logger.warning("listen session create failed: %s", exc)
    from pam_pm.models import realtime as rt_mod

    client = rt_mod.RealtimeClient(
        session_id,
        project,
        _db_path(),
        mode=mode,
        connect_fn=LISTEN_CONNECT_FN,
    )
    try:
        await client.connect()
    except Exception as exc:  # noqa: BLE001
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "error",
                        "message": "Could not reach transcription. Check the Meta key and try again.",
                    }
                )
            )
        except Exception:  # noqa: BLE001, S110
            pass
        logger.warning("listen realtime connect failed: %s", exc)
        try:
            await websocket.close(code=1011)
        except Exception:  # noqa: BLE001, S110
            pass
        return
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "status",
                    "state": "transcribing",
                    "session_id": session_id,
                }
            )
        )
    except Exception:  # noqa: BLE001, S110
        pass
    stop = False

    async def _meta_to_browser() -> None:
        nonlocal stop
        while not stop:
            try:
                update = await client.recv_event(timeout=5.0)
            except TimeoutError:
                if client.is_expired():
                    break
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("listen realtime recv failed: %s", exc)
                try:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "status",
                                "state": "reconnecting",
                            }
                        )
                    )
                except Exception:  # noqa: BLE001, S110
                    pass
                ok = await client.reconnect_with_resend(max_attempts=3)
                if not ok:
                    try:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "error",
                                    "message": "Connection lost. Audio was kept for 30 seconds.",
                                }
                            )
                        )
                    except Exception:  # noqa: BLE001, S110
                        pass
                    break
                continue
            if update is None:
                break
            if update.kind in ("partial", "final", "speaker"):
                try:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": update.kind,
                                "text": update.text,
                                "turn_id": update.turn_id,
                                "speaker": update.speaker,
                                "start_ms": update.start_ms,
                                "end_ms": update.end_ms,
                                "session_id": session_id,
                            }
                        )
                    )
                except Exception:  # noqa: BLE001
                    break
            elif update.kind == "error" and update.text:
                try:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": update.text[:300],
                            }
                        )
                    )
                except Exception:  # noqa: BLE001, S110
                    pass

    meta_task = asyncio.create_task(_meta_to_browser())
    try:
        while not stop:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                break
            except Exception:  # noqa: BLE001
                break
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                try:
                    await client.send_audio(bytes(data))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("listen send audio failed: %s", exc)
                    try:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "error",
                                    "message": "Audio send failed. Try stopping and starting again.",
                                }
                            )
                        )
                    except Exception:  # noqa: BLE001, S110
                        pass
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                control = json.loads(text)
            except ValueError:
                continue
            if not isinstance(control, dict):
                continue
            action = str(control.get("type", "")).strip().lower()
            if action in ("end", "stop"):
                stop = True
                try:
                    await client.end_stream()
                except Exception:  # noqa: BLE001, S110
                    pass
            elif action == "silence":
                try:
                    await client.send_silence(int(control.get("ms", 80)))
                except Exception:  # noqa: BLE001, S110
                    pass
            if client.is_expired():
                try:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "status",
                                "state": "max_length",
                                "message": "Two hour limit reached. Stopping.",
                            }
                        )
                    )
                except Exception:  # noqa: BLE001, S110
                    pass
                stop = True
    finally:
        stop = True
        if not meta_task.done():
            meta_task.cancel()
        try:
            await client.close()
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            store_mod.set_listen_session_status(_db_path(), session_id, "stopped")
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001, S110
            pass


LAST_NEXT_SPRINT: dict[str, Any] = {"proposal": None, "project": "default"}


def _next_sprint_project(request: Request) -> str:
    return _safe_project(request.query_params.get("project", "default"))


def _next_sprint_result_html(proposal: dict[str, Any], dry_run: bool) -> str:
    from pam_pm.agents import next_sprint as ns_mod

    entries = list((proposal or {}).get("items", []))
    labels = {"retro": "Retro action", "carryover": "Carryover", "backlog": "Backlog"}
    parts: list[str] = ['<section class="card" id="next-sprint-card">']
    if not entries:
        parts.append("<h2>Next sprint: nothing proposed</h2>")
        parts.append(
            '<p class="muted">No carryover, retro actions, or backlog items found.</p>'
        )
        parts.append(
            '<p class="muted">Add backlog items on the Board, then choose Propose next sprint.</p>'
        )
    else:
        word = "item" if len(entries) == 1 else "items"
        parts.append(f"<h2>Next sprint: {len(entries)} {word} proposed</h2>")
        if dry_run:
            parts.append('<p class="pill">Dry run is on. Approve proposes first.</p>')
        else:
            parts.append(
                '<p class="pill">Dry run is off. Approve creates the sprint.</p>'
            )
        if (proposal or {}).get("has_history") is False:
            parts.append(
                '<p class="pill">No history yet, plan uses capacity and estimates.</p>'
            )
        for entry in entries:
            title = html.escape(str(entry.get("title", "")))
            owner = html.escape(str(entry.get("owner", "") or "Unassigned"))
            reason = html.escape(str(entry.get("reason", "")))
            raw_kind = str(entry.get("kind", "") or "").strip().lower()
            kind = html.escape(labels.get(raw_kind, "Item"))
            slip = html.escape(str(entry.get("slip_reason", "") or ""))
            try:
                hours = f"{float(entry.get('estimate', 4.0)):g}h"
            except (TypeError, ValueError):
                hours = "4h"
            parts.append(
                "<article>"
                f"<h3>{title}</h3>"
                f'<p class="muted">{kind} &middot; {hours} &middot; {owner} &middot; {reason}</p>'
                + (f"<p>Slipped because: {slip}</p>" if slip else "")
                + "</article>"
            )
        loads = dict((proposal or {}).get("loads", {}))
        if loads:
            load_text = ", ".join(f"{n} {h:g}h" for n, h in sorted(loads.items()))
            parts.append(f"<p><strong>Load:</strong> {html.escape(load_text)}.</p>")
        risk = str((proposal or {}).get("risk_note", "") or "").strip()
        if risk:
            parts.append(f"<p><strong>Risk:</strong> {html.escape(risk)}.</p>")
        if entries:
            parts.append(
                '<form hx-post="/api/next-sprint/approve" hx-target="#next-sprint-result" '
                'hx-swap="innerHTML">'
                '<label for="ns-name">Sprint name</label>'
                '<input id="ns-name" name="name" value="Sprint 2">'
                '<div class="form-actions">'
                '<button type="submit">Approve and create sprint</button>'
                "</div>"
                '<div id="next-sprint-result" class="result" aria-live="polite"></div>'
                "</form>"
            )
    parts.append("</section>")
    _ = ns_mod
    return "\n".join(parts)


@app.get("/next-sprint", response_class=HTMLResponse)
def next_sprint_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "next-sprint")
    project = str(ctx["project"])
    dry_run, _ = _effective_safety()
    stored = LAST_NEXT_SPRINT.get("proposal")
    if stored and LAST_NEXT_SPRINT.get("project") == project:
        ctx["next_sprint_html"] = _next_sprint_result_html(stored, dry_run)
    else:
        ctx["next_sprint_html"] = _next_sprint_result_html({"items": []}, dry_run)
    return templates.TemplateResponse(request, "next_sprint.html", ctx)  # type: ignore[return-value]


@app.post("/api/next-sprint/run")
def next_sprint_run_route(request: Request) -> Any:
    from pam_pm.agents import next_sprint as ns_mod

    project = _next_sprint_project(request)
    adapter = get_board_adapter(project)
    if adapter is None:
        msg = "The board is not configured yet."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=502)
    roster = _roster_entries(project)
    retro = [dict(a) for a in LAST_REVIEW.get("retro_actions", [])]
    db_path = _db_path()
    snapshot = None
    try:
        from pam_pm.core.memory import planning_context

        snapshot = planning_context(db_path, project)
    except Exception:  # noqa: BLE001
        snapshot = None
    try:
        proposal = ns_mod.propose_next_sprint(
            adapter, roster, retro_actions=retro, memory=snapshot, db=db_path
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("next sprint propose failed: %s", exc)
        msg = "The sprint could not be proposed."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=502)
    LAST_NEXT_SPRINT["proposal"] = proposal
    LAST_NEXT_SPRINT["project"] = project
    router_mod.log_route(
        router_mod.CAP_NEXT_SPRINT, router_mod.text_model(), "proposal built"
    )
    dry_run, _ = _effective_safety()
    if "hx-request" in request.headers:
        return HTMLResponse(_next_sprint_result_html(proposal, dry_run))
    return {
        "ok": True,
        "project": project,
        "count": len(proposal["items"]),
        "proposal": proposal,
    }


@app.post("/api/next-sprint/approve")
async def next_sprint_approve_route(request: Request) -> Any:
    from pam_pm.agents import next_sprint as ns_mod

    project = str(LAST_NEXT_SPRINT.get("project", "default"))
    proposal = LAST_NEXT_SPRINT.get("proposal")
    if not proposal or not proposal.get("items"):
        msg = "No sprint proposal to approve. Run Propose first."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=404)
    form = await request.form()
    name = str(
        form.get("name", "") or request.query_params.get("name", "") or "Next sprint"
    ).strip()
    adapter = get_board_adapter(project)
    if adapter is None:
        msg = "The board is not configured yet."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=502)
    dry_run, _ = _effective_safety()
    writes = WriteService(_db_path(), dry_run=dry_run)
    try:
        result = ns_mod.approve_next_sprint(
            adapter,
            proposal,
            name,
            writes=writes,
            db=_db_path(),
            dry_run=dry_run,
            evidence_quote="Approved from the next sprint card.",
        )
    except Exception as exc:  # noqa: BLE001
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, f"Approve failed: {exc}"))
        return JSONResponse(
            {"ok": False, "message": f"Approve failed: {exc}"}, status_code=502
        )
    if "hx-request" in request.headers:
        if result.get("applied"):
            return HTMLResponse(_test_result_html(True, f"Created {name}."))
        return HTMLResponse(
            _test_result_html(True, "Proposed. Dry run is on, so nothing changed.")
        )
    return {"ok": True, "applied": result.get("applied"), "name": name}


LAST_MID_SPRINT: dict[str, Any] = {
    "proposal": None,
    "project": "default",
    "sprint": "",
}

_MID_SPRINT_ACTIONS = {
    "move_out": "Move out",
    "swap": "Swap",
    "reassign": "Reassign",
    "pull": "Pull",
}


def _mid_sprint_project(request: Request) -> str:
    return _safe_project(request.query_params.get("project", "default"))


def _mid_sprint_sprint(adapter: Any) -> str:
    try:
        iters = list(adapter.list_iterations() or [])
    except Exception:  # noqa: BLE001
        return ""
    if not iters:
        return ""
    return str(getattr(iters[-1], "id", "") or "")


def _mid_sprint_result_html(
    proposal: dict[str, Any], dry_run: bool, sprint: str
) -> str:
    changes = list((proposal or {}).get("changes", []))
    goal = str((proposal or {}).get("goal", "") or "")
    parts: list[str] = ['<section class="card" id="mid-sprint-card">']
    if not changes:
        parts.append("<h2>Mid-sprint: on track</h2>")
        if goal:
            parts.append(f"<p><strong>Goal:</strong> {html.escape(goal)}</p>")
        parts.append(
            '<p class="muted">No stalled blockers, overload, urgent scope, '
            "or surplus found. Check again after the board changes.</p>"
        )
    else:
        word = "change" if len(changes) == 1 else "changes"
        parts.append(f"<h2>Mid-sprint: {len(changes)} {word} proposed</h2>")
        if dry_run:
            parts.append(
                '<p class="pill">Dry run is on. Approve proposes a change first.</p>'
            )
        else:
            parts.append(
                '<p class="pill">Dry run is off. Approve applies the change.</p>'
            )
        if goal:
            parts.append(f"<p><strong>Goal:</strong> {html.escape(goal)}</p>")
        for index, change in enumerate(changes):
            title = html.escape(str(change.get("title", "")))
            raw = str(change.get("action", "") or "").strip()
            action = html.escape(_MID_SPRINT_ACTIONS.get(raw, "Change"))
            reason = html.escape(str(change.get("reason", "")))
            effect = html.escape(str(change.get("goal_effect", "")))
            evidence = html.escape(str(change.get("evidence", "")))
            owner = str(change.get("owner", "") or "").strip()
            target = str(change.get("target_owner", "") or "").strip()
            if owner and target:
                who = f"{owner} to {target}"
            else:
                who = owner or target or "Unassigned"
            try:
                hours = f"{float(change.get('estimate', 0.0)):g}h"
            except (TypeError, ValueError):
                hours = ""
            meta = " &middot; ".join(
                part for part in (action, hours, html.escape(who)) if part
            )
            parts.append(
                "<article>"
                f"<h3>{title}</h3>"
                f'<p class="muted">{meta}</p>'
                f"<p>{reason}</p>"
                + (f"<p><strong>Goal effect:</strong> {effect}</p>" if effect else "")
                + (f'<p class="muted">Evidence: {evidence}</p>' if evidence else "")
                + f'<button hx-post="/api/mid-sprint/approve?index={index}" '
                f'hx-target="#mid-sprint-result-{index}" hx-swap="innerHTML">'
                "Approve</button>"
                f'<span id="mid-sprint-result-{index}" class="result" '
                'aria-live="polite"></span>'
                "</article>"
            )
        overall = str((proposal or {}).get("goal_effect", "") or "").strip()
        if overall:
            parts.append(f"<p><strong>Overall:</strong> {html.escape(overall)}</p>")
    _ = sprint
    parts.append("</section>")
    return "\n".join(parts)


@app.get("/mid-sprint", response_class=HTMLResponse)
def mid_sprint_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "mid-sprint")
    project = str(ctx["project"])
    dry_run, _ = _effective_safety()
    stored = LAST_MID_SPRINT.get("proposal")
    if stored and LAST_MID_SPRINT.get("project") == project:
        ctx["mid_sprint_html"] = _mid_sprint_result_html(
            stored, dry_run, str(LAST_MID_SPRINT.get("sprint", ""))
        )
    else:
        ctx["mid_sprint_html"] = _mid_sprint_result_html(
            {"changes": [], "goal": ""}, dry_run, ""
        )
    return templates.TemplateResponse(request, "mid_sprint.html", ctx)  # type: ignore[return-value]


@app.post("/api/mid-sprint/run")
async def mid_sprint_run_route(request: Request) -> Any:
    from pam_pm.agents import mid_sprint as ms_mod

    project = _mid_sprint_project(request)
    adapter = get_board_adapter(project)
    if adapter is None:
        msg = "The board is not configured yet."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=502)
    goal = request.query_params.get("goal", "").strip()
    if not goal:
        try:
            form = await request.form()
            goal = str(form.get("goal", "") or "").strip()
        except Exception:  # noqa: BLE001
            goal = ""
    roster = _roster_entries(project)
    sprint = _mid_sprint_sprint(adapter)
    try:
        proposal = ms_mod.propose_adjustment(adapter, roster, sprint or None, goal=goal)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mid-sprint propose failed: %s", exc)
        msg = "The sprint could not be checked."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=502)
    LAST_MID_SPRINT["proposal"] = proposal
    LAST_MID_SPRINT["project"] = project
    LAST_MID_SPRINT["sprint"] = sprint
    dry_run, _ = _effective_safety()
    if "hx-request" in request.headers:
        return HTMLResponse(_mid_sprint_result_html(proposal, dry_run, sprint))
    return {
        "ok": True,
        "project": project,
        "count": len(proposal["changes"]),
        "proposal": proposal,
    }


@app.post("/api/mid-sprint/approve")
async def mid_sprint_approve_route(request: Request) -> Any:
    from pam_pm.agents import mid_sprint as ms_mod

    project = str(LAST_MID_SPRINT.get("project", "default"))
    proposal = LAST_MID_SPRINT.get("proposal") or {}
    changes = list(proposal.get("changes", []))
    index = await _retro_index(request)
    if index is None or index < 0 or index >= len(changes):
        msg = "Unknown change. Check the sprint first."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=404)
    adapter = get_board_adapter(project)
    if adapter is None:
        msg = "The board is not configured yet."
        if "hx-request" in request.headers:
            return HTMLResponse(_test_result_html(False, msg))
        return JSONResponse({"ok": False, "message": msg}, status_code=502)
    dry_run, _ = _effective_safety()
    writes = WriteService(_db_path(), dry_run=dry_run)
    result = ms_mod.apply_change(
        adapter,
        changes[index],
        str(LAST_MID_SPRINT.get("sprint", "") or ""),
        writes=writes,
        db=_db_path(),
        dry_run=dry_run,
    )
    if "hx-request" in request.headers:
        if result.get("applied"):
            return HTMLResponse(_test_result_html(True, "Applied."))
        if dry_run and result.get("write_id"):
            return HTMLResponse(
                _test_result_html(True, "Proposed. Dry run is on, so nothing changed.")
            )
        detail = str(result.get("detail", ""))
        if detail:
            return HTMLResponse(_test_result_html(False, detail))
        return HTMLResponse(
            _test_result_html(False, "Approve did not work. Try again.")
        )
    return {
        "ok": bool(result.get("applied") or result.get("write_id")),
        **result,
    }


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    """Show the password setup form on first run, else the login form."""
    data_dir = _auth_data_dir()
    password_set = bool(data_dir is not None and auth_mod.has_password(data_dir))
    return HTMLResponse(_login_page_html("login" if password_set else "setup", ""))


@app.post("/login")
async def login_submit(request: Request) -> Any:
    """Check the console password and set a session cookie."""
    ctype = request.headers.get("content-type", "")
    as_json = "application/json" in ctype
    data_dir = _auth_data_dir()
    if data_dir is None or not auth_mod.has_password(data_dir):
        msg = "No console password is set yet."
        hint = "Open /login in a browser and choose a password first."
        if as_json:
            return JSONResponse(
                {"ok": False, "message": msg, "hint": hint}, status_code=400
            )
        return HTMLResponse(_login_page_html("setup", f"{msg} {hint}"), status_code=400)
    key = _client_ip(request)
    if not auth_mod.login_allowed(key):
        return _rate_limited_response(request, key, as_json=as_json)
    password = await _login_password(request)
    if password and auth_mod.verify_password(password, data_dir):
        auth_mod.record_login_success(key)
        return _session_cookie_response(
            request, auth_mod.create_session(), as_json=as_json
        )
    auth_mod.record_login_failure(key)
    msg = "That password did not match. Try again."
    if as_json:
        return JSONResponse({"ok": False, "message": msg}, status_code=401)
    return HTMLResponse(_login_page_html("login", msg), status_code=401)


@app.post("/api/auth/setup")
async def auth_setup(request: Request) -> Any:
    """Set the console password on first run (logged in users may change it)."""
    ctype = request.headers.get("content-type", "")
    as_json = "application/json" in ctype
    data_dir = _auth_data_dir()
    if data_dir is None:
        return JSONResponse(
            {"ok": False, "message": "The data directory is not available."},
            status_code=500,
        )
    if auth_mod.has_password(data_dir) and not _request_session_ok(request):
        return JSONResponse(
            {
                "ok": False,
                "message": "A console password is already set.",
                "hint": "Log in first to change it.",
            },
            status_code=403,
        )
    key = _client_ip(request)
    if not auth_mod.login_allowed(key):
        return _rate_limited_response(request, key, as_json=True)
    password = await _login_password(request)
    try:
        auth_mod.set_password(password, data_dir)
    except ValueError as exc:
        auth_mod.record_login_failure(key)
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    auth_mod.record_login_success(key)
    return _session_cookie_response(request, auth_mod.create_session(), as_json=as_json)


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> Any:
    """Drop the current session and clear its cookie."""
    auth_mod.destroy_session(request.cookies.get(auth_mod.SESSION_COOKIE, ""))
    resp = JSONResponse({"ok": True, "message": "Logged out."})
    resp.delete_cookie(auth_mod.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/status")
def auth_status(request: Request) -> Any:
    """Report whether a password is set and this request is logged in."""
    data_dir = _auth_data_dir()
    password_set = bool(data_dir is not None and auth_mod.has_password(data_dir))
    return {
        "ok": True,
        "password_set": password_set,
        "authenticated": _request_session_ok(request),
    }
