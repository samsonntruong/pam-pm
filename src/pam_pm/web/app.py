"""Pam web console: setup wizard, rules, activity stream, board."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pam_pm.core import store as store_mod
from pam_pm.core.config import Config

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Pam")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

HOST = "127.0.0.1"
PORT = 8765

PROJECTS = ["default", "atlas", "demo"]

SETUP_TOKEN = os.environ.get("PAM_SETUP_TOKEN", "").strip() or secrets.token_urlsafe(32)
SETUP_DONE = False

RULE_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "morning",
        "title": "Morning check-in",
        "description": "DM each person with active items each morning.",
        "trigger": "schedule",
    },
    {
        "name": "follow_up",
        "title": "Follow up on close",
        "description": "DM the next owner when an item closes.",
        "trigger": "ado_change",
    },
    {
        "name": "blocker",
        "title": "Blocker intake",
        "description": "Ask what is blocked, link items, request ETA.",
        "trigger": "slack_blocker",
    },
]

RULE_STATES: dict[str, bool] = {
    "morning": True,
    "follow_up": True,
    "blocker": False,
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


def is_valid_token(token: str | None) -> bool:
    if not token or not SETUP_TOKEN:
        return False
    return secrets.compare_digest(str(token), SETUP_TOKEN)


def _project(request: Request) -> str:
    want = request.query_params.get("project", "default").strip()
    return want if want in PROJECTS else "default"


def _context(request: Request, active: str) -> dict[str, Any]:
    return {
        "request": request,
        "active": active,
        "project": _project(request),
        "projects": PROJECTS,
    }


def _check_meta() -> tuple[bool, str]:
    missing = [
        key
        for key in ("META_MODEL", "META_BASE_URL", "META_API_KEY")
        if not os.environ.get(key, "").strip()
    ]
    if missing:
        return False, "Missing: " + ", ".join(missing)
    return True, "Meta config looks good"


def _check_slack() -> tuple[bool, str]:
    if not os.environ.get("SLACK_BOT_TOKEN", "").strip():
        return False, "Missing: SLACK_BOT_TOKEN"
    return True, "Slack config looks good"


def _check_ado() -> tuple[bool, str]:
    missing = [
        key
        for key in ("ADO_ORG_URL", "ADO_PROJECT", "ADO_PAT")
        if not os.environ.get(key, "").strip()
    ]
    if missing:
        return False, "Missing: " + ", ".join(missing)
    return True, "ADO config looks good"


def _test_result_html(ok: bool, message: str) -> str:
    if ok:
        return f'<span class="pill pill-ok">OK: {message}</span>'
    return f'<span class="pill pill-fail">{message}</span>'


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


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, token: str = "") -> HTMLResponse:
    ctx = _context(request, "setup")
    ctx.update(
        {
            "token": token,
            "token_ok": is_valid_token(token) if token else False,
            "setup_done": SETUP_DONE,
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
    return templates.TemplateResponse(request, "activity.html", ctx)  # type: ignore[return-value]


@app.get("/board", response_class=HTMLResponse)
def board_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "board")
    groups = (
        ("todo", "To Do", ("To Do", "New", "Open")),
        ("doing", "Doing", ("Doing", "Active", "In Progress")),
        ("done", "Done", ("Done", "Closed", "Resolved")),
    )
    ctx["columns"] = [
        {
            "key": key,
            "title": title,
            "cards": [i for i in SAMPLE_ITEMS if i["state"] in states],
        }
        for key, title, states in groups
    ]
    ctx["links"] = SAMPLE_LINKS
    return templates.TemplateResponse(request, "board.html", ctx)  # type: ignore[return-value]


@app.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "plan")
    return templates.TemplateResponse(request, "plan.html", ctx)  # type: ignore[return-value]


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "review")
    return templates.TemplateResponse(request, "review.html", ctx)  # type: ignore[return-value]


@app.post("/api/test/{which}")
async def test_connection(which: str, request: Request) -> Any:
    name = (which or "").strip().lower()
    if name == "meta":
        ok, message = _check_meta()
    elif name == "slack":
        ok, message = _check_slack()
    elif name == "ado":
        ok, message = _check_ado()
    else:
        return JSONResponse({"ok": False, "message": "Unknown target"}, status_code=404)
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(ok, message))
    return {"ok": ok, "message": message}


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
    known = {str(r["name"]) for r in RULE_TEMPLATES}
    if name not in known:
        return JSONResponse({"ok": False, "message": "Unknown rule"}, status_code=404)
    RULE_STATES[name] = enabled
    state = "On" if enabled else "Off"
    if "hx-request" in request.headers:
        return HTMLResponse(f'<span class="toggle-state">Rule {name} is {state}</span>')
    return {"ok": True, "name": name, "enabled": enabled}


def _sse_events() -> Any:
    for event in _activity_events():
        yield f"data: {json.dumps(event)}\n\n"
    yield ": ping\n\n"


@app.get("/api/activity/stream")
async def activity_stream() -> StreamingResponse:
    return StreamingResponse(
        _sse_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/undo/{write_id}")
def undo_event(write_id: int) -> dict[str, Any]:
    try:
        updated = store_mod.undo_write(Config().db_path(), write_id)
    except Exception:  # noqa: BLE001
        updated = False
    if updated or bus.mark_undone(write_id):
        return {"ok": True, "id": write_id}
    return {"ok": False, "message": "Write not found"}


@app.post("/api/setup/complete")
async def setup_complete(request: Request) -> Any:
    global SETUP_DONE
    if SETUP_DONE:
        return JSONResponse({"ok": False, "message": "Setup already complete"})
    token = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            token = str(payload.get("token", "")).strip()
    else:
        form = await request.form()
        token = str(form.get("token", "")).strip()
    if not token:
        token = request.query_params.get("token", "").strip()
    if not is_valid_token(token):
        return JSONResponse({"ok": False, "message": "Invalid setup token"})
    SETUP_DONE = True
    return {"ok": True, "message": "Setup complete"}
