"""PAM PM command line interface."""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import webbrowser

from pam_pm import __version__

HOST = "127.0.0.1"
PORT = 8765


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pam", description="Pam PM console")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    sub = parser.add_subparsers(dest="command")
    p_start = sub.add_parser("start", help="Start the local web console")
    p_start.add_argument("--host", default=HOST, help="Bind host (default 127.0.0.1)")
    p_start.add_argument(
        "--port", type=int, default=PORT, help="Bind port (default 8765)"
    )
    p_start.add_argument(
        "--no-browser", action="store_true", help="Do not open a browser"
    )
    p_desktop = sub.add_parser(
        "desktop", help="Open Pam in a desktop window with a tray icon"
    )
    p_desktop.add_argument("--host", default=HOST, help="Bind host (default 127.0.0.1)")
    p_desktop.add_argument(
        "--port", type=int, default=PORT, help="Bind port (default 8765)"
    )
    sub.add_parser("doctor", help="Check meta, slack, ado, cache, autostart, uptime")
    p_auto = sub.add_parser(
        "autostart", help="Start Pam at sign-in and restart it if it stops"
    )
    p_auto.add_argument("action", choices=["on", "off", "status"])
    sub.add_parser("review", help="Print the sprint review markdown")
    return parser


def cmd_start(args: argparse.Namespace) -> None:
    from pam_pm.core import auth as auth_mod
    from pam_pm.core.config import Config

    host = str(getattr(args, "host", HOST) or HOST)
    port = int(getattr(args, "port", PORT) or PORT)
    data_dir = Config().data_dir
    share_enabled = False
    try:
        share_enabled = bool(Config().share_on_lan)
    except Exception:  # noqa: BLE001
        share_enabled = False
    if not share_enabled:
        try:
            from pam_pm.core import store as store_mod

            db_path = data_dir / "state.db"
            if db_path.exists():
                share_enabled = store_mod.any_project_shared(db_path)
        except Exception:  # noqa: BLE001
            share_enabled = False
    if share_enabled and auth_mod.is_loopback_host(host):
        host = "0.0.0.0"
    try:
        if auth_mod.provision_password_from_env(data_dir):
            print("Console password saved from PAM_CONSOLE_PASSWORD (stored hashed).")
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(2)
    if share_enabled:
        share_error = auth_mod.share_console_error(data_dir)
        if share_error is not None:
            print(share_error)
            raise SystemExit(2)
    error = auth_mod.console_bind_error(host, data_dir)
    if error is not None:
        print(error)
        raise SystemExit(2)
    token = secrets.token_urlsafe(32)
    os.environ["PAM_SETUP_TOKEN"] = token
    url = f"http://{host}:{port}/setup?token={token}"
    print(f"Setup token: {token}")
    print(f"URL: {url}")
    if auth_mod.has_password(data_dir):
        print(f"Login: http://{host}:{port}/login")
    if not getattr(args, "no_browser", False):
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            print(f"Browser open skipped: {exc}")
    configure_server_logging()
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed.")
        return
    uvicorn.run("pam_pm.web.app:app", host=host, port=port)


def cmd_desktop(args: argparse.Namespace) -> None:
    from pam_pm import desktop

    host = str(getattr(args, "host", HOST) or HOST)
    port = int(getattr(args, "port", PORT) or PORT)
    result = desktop.main(host=host, port=port)
    if result:
        raise SystemExit(result)


def _presence(keys: list[str]) -> list[str]:
    return [k for k in keys if not os.environ.get(k, "").strip()]


def _check_cache_prefix() -> tuple[bool, str]:
    try:
        import json

        from pam_pm.agents.prompts.builder import build_messages
    except Exception as exc:  # noqa: BLE001
        return False, f"error: {exc}"
    try:
        first = build_messages(
            "interviewer",
            "Project Atlas.",
            [{"role": "user", "content": "hi"}],
            {"person": "Ana", "now": "2026-11-01T09:00:00"},
        )
        second = build_messages(
            "interviewer",
            "Project Atlas.",
            [{"role": "user", "content": "hi"}],
            {"person": "Ben", "now": "2026-11-01T15:30:00"},
        )
        tools_same = json.dumps(first["tools"], sort_keys=True) == json.dumps(
            second["tools"], sort_keys=True
        )
        head_first = json.dumps(first["messages"][:-1], sort_keys=True)
        head_second = json.dumps(second["messages"][:-1], sort_keys=True)
        if tools_same and head_first == head_second:
            return True, "stable"
        return False, "unstable"
    except Exception as exc:  # noqa: BLE001
        return False, f"error: {exc}"


def cmd_autostart(args: argparse.Namespace) -> None:
    from pam_pm import autostart

    action = str(getattr(args, "action", "status") or "status")
    if action == "on":
        print(autostart.enable())
    elif action == "off":
        print(autostart.disable())
    else:
        print(autostart.status_text())


def _last_start() -> str:
    try:
        from pam_pm.core import recovery
        from pam_pm.core import store as store_mod
        from pam_pm.core.config import Config
    except Exception:  # noqa: BLE001
        return ""
    try:
        return recovery.kv_get(Config().db_path(), store_mod.STARTED_AT_KEY)
    except Exception:  # noqa: BLE001
        return ""


def _format_uptime(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _uptime_text(last_start: str) -> str:
    text = (last_start or "").strip()
    if not text:
        return "unknown"
    try:
        from datetime import UTC, datetime
    except Exception:  # noqa: BLE001
        return "unknown"
    try:
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        moment = datetime.fromisoformat(iso)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        delta = (datetime.now(UTC) - moment).total_seconds()
    except ValueError:
        return "unknown"
    return _format_uptime(delta)


def cmd_doctor(_args: argparse.Namespace) -> None:
    meta_missing = _presence(["META_MODEL", "META_BASE_URL", "META_API_KEY"])
    slack_missing = _presence(["SLACK_BOT_TOKEN"])
    ado_missing = _presence(["ADO_ORG_URL", "ADO_PROJECT", "ADO_PAT"])
    if meta_missing:
        print("meta: missing " + ", ".join(meta_missing))
    else:
        print("meta: ok")
    if slack_missing:
        print("slack: missing " + ", ".join(slack_missing))
    else:
        print("slack: ok")
    if ado_missing:
        print("ado: missing " + ", ".join(ado_missing))
    else:
        print("ado: ok")
    ok, detail = _check_cache_prefix()
    print(f"cache prefix: {detail}")
    try:
        from pam_pm import autostart

        print(autostart.status_text())
    except Exception as exc:  # noqa: BLE001
        print(f"autostart: unknown ({exc})")
    started = _last_start()
    print(f"last start: {started or 'unknown'}")
    print(f"uptime: {_uptime_text(started)}")
    if not meta_missing and not slack_missing and not ado_missing and ok:
        print("doctor: ok")
    else:
        print("doctor: issues found")


def cmd_review(_args: argparse.Namespace) -> None:
    from pam_pm.agents.lead import synthesize_review

    items = [
        {"id": "101", "title": "Login form", "state": "To Do"},
        {"id": "102", "title": "API keys", "state": "Doing"},
    ]
    interviews = {
        "101": {
            "status": "To Do",
            "evidence_quote": "Login form is done, only tests remain.",
            "confidence": 0.8,
        },
        "102": {
            "status": "Doing",
            "evidence_quote": "Waiting on API keys from Sam.",
            "confidence": 0.4,
        },
    }
    events = [
        {"operation": "comment", "item_id": "102", "detail": "Note blocker on 102."}
    ]
    result = synthesize_review(items, interviews, events)
    print(result["review_markdown"])


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return
    if args.command == "start":
        cmd_start(args)
    elif args.command == "desktop":
        cmd_desktop(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "autostart":
        cmd_autostart(args)
    elif args.command == "review":
        cmd_review(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()


def configure_server_logging() -> None:
    """Configure console logging for server runs at INFO level."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger().setLevel(logging.INFO)
