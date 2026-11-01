"""Pam web console: setup wizard, rules, activity stream, board."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.agents.lead import synthesize_review
from pam_pm.agents.planner import approve_proposal, propose_sprint
from pam_pm.core import store as store_mod
from pam_pm.core.config import (
    BACKEND_AZURE_DEVOPS,
    BACKEND_PAM_BOARD,
    Config,
    backend_label,
    normalize_backend,
)
from pam_pm.models.vision import plan_from_photo

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Pam")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

HOST = "127.0.0.1"
PORT = 8765

PROJECTS = ["default", "atlas", "demo"]

WIZARD_ORDER = ["model", "slack", "ado", "people", "rules", "safety", "done"]
WIZARD_PROGRESS_FILENAME = "wizard_progress.json"

SETUP_TOKEN = os.environ.get("PAM_SETUP_TOKEN", "").strip() or secrets.token_urlsafe(32)
SETUP_DONE = False


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


def _run_review_now() -> dict[str, Any]:
    result = synthesize_review(SAMPLE_ITEMS, _sample_interviews(), bus.history())
    LAST_REVIEW["markdown"] = result["review_markdown"]
    LAST_REVIEW["retro_actions"] = result["retro_actions"]
    LAST_REVIEW["low_confidence"] = result["low_confidence"]
    logger.info(
        "review run: %d retro actions, %d low confidence",
        len(result["retro_actions"]),
        len(result["low_confidence"]),
    )
    return result


def _review_result_html(result: dict[str, Any]) -> str:
    parts = [f"<pre class=\"review-markdown\">{html.escape(result['review_markdown'])}</pre>"]
    parts.append('<section class="card"><h2>Retro actions</h2>')
    actions = result.get("retro_actions", [])
    if not actions:
        parts.append('<p class="muted">No retro actions. Nothing flagged.</p>')
    for index, action in enumerate(actions):
        title = html.escape(str(action.get("title", "")))
        desc = html.escape(str(action.get("description", "")))
        parts.append(
            "<article class=\"card\">"
            f"<h3>{title}</h3>"
            f"<p class=\"muted\">{desc}</p>"
            f"<button hx-post=\"/api/review/retro/create?index={index}\" "
            f"hx-target=\"#retro-result-{index}\" hx-swap=\"innerHTML\">"
            "Create in ADO</button>"
            f"<span id=\"retro-result-{index}\" class=\"result\" aria-live=\"polite\"></span>"
            "</article>"
        )
    parts.append("</section>")
    return "\n".join(parts)


def _plan_result_html(photo: dict[str, Any], proposal: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    masks = photo.get("masks", [])
    path = html.escape(str(photo.get("path", "")))
    parts.append('<section class="card"><h2>Masks preview</h2>')
    if masks:
        items = "".join(f"<li>Mask {html.escape(str(m))}</li>" for m in masks)
        parts.append(f"<p>Vision path: {path}. Masks: {len(masks)}.</p><ul>{items}</ul>")
    else:
        parts.append(
            f"<p>Vision path: {path}. No masks, full image read used.</p>"
        )
    parts.append("</section>")
    parts.append('<section class="card"><h2>Readings</h2>')
    readings = photo.get("readings", [])
    if readings:
        items = "".join(f"<li>{html.escape(str(r))}</li>" for r in readings)
        parts.append(f"<ul>{items}</ul>")
    else:
        parts.append('<p class="muted">No readings found in the photo.</p>')
    parts.append("</section>")
    parts.append('<section class="card"><h2>Proposal</h2>')
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
            "<table class=\"proposal\"><thead><tr>"
            "<th>Title</th><th>Type</th><th>Owner</th><th>Estimate</th>"
            "</tr></thead><tbody>"
            f"{rows}</tbody></table>"
        )
    else:
        parts.append('<p class="muted">No proposal. Upload a photo with notes.</p>')
    parts.append(
        '<button hx-post="/api/plan/approve" hx-target="#approve-result" '
        'hx-swap="innerHTML">Approve</button>'
        '<div id="approve-result" class="result" aria-live="polite"></div>'
        "</section>"
    )
    return "\n".join(parts)


def is_valid_token(token: str | None) -> bool:
    if not token or not SETUP_TOKEN:
        return False
    return secrets.compare_digest(str(token), SETUP_TOKEN)


def _project(request: Request) -> str:
    want = request.query_params.get("project", "default").strip() or "default"
    if want in PROJECTS:
        return want
    if want in _known_project_names():
        return want
    if len(want) <= 64 and all(c.isalnum() or c in ("-", "_", ".") for c in want):
        return want
    return "default"


def _context(request: Request, active: str) -> dict[str, Any]:
    project = _project(request)
    return {
        "request": request,
        "active": active,
        "project": project,
        "projects": _projects_with_backends(),
        "project_backend": _project_backend(project),
        "project_backend_label": backend_label(_project_backend(project)),
    }


def _check_meta() -> tuple[bool, str, str]:
    missing = [
        key
        for key in ("META_MODEL", "META_BASE_URL", "META_API_KEY")
        if not os.environ.get(key, "").strip()
    ]
    if missing:
        return (
            False,
            "Model check failed. Pam cannot reach the model.",
            "Set " + ", ".join(missing) + " in your secrets file, then test again.",
        )
    return (
        True,
        "Model check passed. Pam can reach the model.",
        "",
    )


def _check_slack() -> tuple[bool, str, str]:
    if not os.environ.get("SLACK_BOT_TOKEN", "").strip():
        return (
            False,
            "Slack check failed. Pam cannot send DMs.",
            "Set SLACK_BOT_TOKEN in your secrets file, then test again.",
        )
    return (
        True,
        "Slack check passed. Pam can send DMs.",
        "",
    )


def _check_ado() -> tuple[bool, str, str]:
    missing = [
        key
        for key in ("ADO_ORG_URL", "ADO_PROJECT", "ADO_PAT")
        if not os.environ.get(key, "").strip()
    ]
    if missing:
        return (
            False,
            "Azure DevOps check failed. Pam cannot read the project.",
            "Set " + ", ".join(missing) + " in your secrets file, then test again.",
        )
    return (
        True,
        "Azure DevOps check passed. Pam can read the project.",
        "",
    )


def _check_people(project: str = "default") -> tuple[bool, str, str]:
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
    people = [p for p in people if p]
    if people:
        return (
            True,
            f"People check passed. {len(people)} teammate(s) added.",
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
        return (
            True,
            f"Rules check passed. {len(enabled)} rule(s) turned on.",
            "",
        )
    return (
        False,
        "Rules check failed. All rules are turned off.",
        "Turn on at least one rule on the Rules page, then test again.",
    )


def _check_safety() -> tuple[bool, str, str]:
    try:
        cfg = Config()
    except Exception:  # noqa: BLE001
        return (
            False,
            "Safety check failed. Settings could not be read.",
            "Check the data directory is writable, then test again.",
        )
    if cfg.daily_spend_cap <= 0:
        return (
            False,
            "Safety check failed. Daily spend cap is missing.",
            "Set DAILY_SPEND_CAP to a value above zero, then test again.",
        )
    mode = "Dry run is on" if cfg.dry_run else "Dry run is off"
    return (
        True,
        f"Safety check passed. {mode} and spend cap is set.",
        "",
    )


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
    ctx.update(
        {
            "token": token,
            "token_ok": is_valid_token(token) if token else False,
            "setup_done": SETUP_DONE,
            "backend": backend,
            "backend_label": backend_label(backend),
            "show_ado": backend == BACKEND_AZURE_DEVOPS,
            "progress": progress,
            "progress_json": json.dumps(progress),
            "wizard_order": WIZARD_ORDER,
            "spend_cap": spend_cap,
            "dry_run": dry_run,
            "people_names": people_entry.get("people", ""),
            "pam_board_value": BACKEND_PAM_BOARD,
            "ado_value": BACKEND_AZURE_DEVOPS,
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
    ctx["plan"] = LAST_PLAN
    return templates.TemplateResponse(request, "plan.html", ctx)  # type: ignore[return-value]


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request) -> HTMLResponse:
    ctx = _context(request, "review")
    ctx["review_markdown"] = LAST_REVIEW.get("markdown", "")
    ctx["retro_actions"] = LAST_REVIEW.get("retro_actions", [])
    return templates.TemplateResponse(request, "review.html", ctx)  # type: ignore[return-value]


@app.post("/api/test/{which}")
async def test_connection(which: str, request: Request) -> Any:
    name = (which or "").strip().lower()
    project = request.query_params.get("project", "default").strip() or "default"
    if name in ("meta", "model"):
        ok, message, hint = _check_meta()
    elif name == "slack":
        ok, message, hint = _check_slack()
    elif name == "ado":
        ok, message, hint = _check_ado()
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
            {"ok": False, "message": "Unknown target.", "hint": "Use one of: model, slack, ado, people, rules, safety."},
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
            project = str(payload.get("project", project or "default")).strip() or "default"
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
        return JSONResponse({"ok": False, "message": "Missing step.", "hint": "Send a step name."}, status_code=400)
    entry = _write_project_step(project, step, data)
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, f"Saved step {step}. Progress restored on return."))
    return {"ok": True, "project": project, "progress": entry}


@app.post("/api/setup/backend")
async def save_setup_backend(request: Request) -> Any:
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
    _write_project_step(project, "backend", {"backend": stored})
    label = backend_label(stored)
    if "hx-request" in request.headers:
        return HTMLResponse(_test_result_html(True, f"Tasks will live on {label}."))
    return {"ok": True, "project": project, "backend": stored, "label": label}


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


@app.post("/api/review/run")
async def run_review(request: Request) -> Any:
    result = _run_review_now()
    if "hx-request" in request.headers:
        return HTMLResponse(_review_result_html(result))
    return {
        "ok": True,
        "markdown": result["review_markdown"],
        "retro_actions": result["retro_actions"],
        "low_confidence": result["low_confidence"],
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
            _test_result_html(True, f"Created item {item.id} tagged retro-action")
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
    photo = plan_from_photo(data if data else b"")
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
        msg = "No proposal to approve. Upload a photo first."
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
