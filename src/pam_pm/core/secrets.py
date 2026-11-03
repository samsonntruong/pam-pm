"""Wizard secrets store: files in the data dir with env override.

Secrets live one per file with 0600 permissions, following the provider
key pattern. Non secret config lives in small JSON files next to them.
Effective helpers return the explicit env value when set, else the saved
value. Explicit env wins so servers keep working. Never log values.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SECRET_FILES = {
    "META_API_KEY": "meta_api_key.txt",
    "SLACK_BOT_TOKEN": "slack_bot_token.txt",
    "SLACK_APP_TOKEN": "slack_app_token.txt",
    "TEAMS_APP_ID": "teams_app_id.txt",
    "TEAMS_APP_PASSWORD": "teams_app_password.txt",
    "TEAMS_TENANT_ID": "teams_tenant_id.txt",
    "TEAMS_REFRESH_TOKEN": "teams_refresh_token.txt",
    "ADO_PAT": "ado_pat.txt",
    "JIRA_API_TOKEN": "jira_api_token.txt",
    "LINEAR_API_KEY": "linear_api_key.txt",
    "TRELLO_API_KEY": "trello_api_key.txt",
    "TRELLO_TOKEN": "trello_token.txt",
    "CLICKUP_API_TOKEN": "clickup_api_token.txt",
    "ASANA_API_TOKEN": "asana_api_token.txt",
    "MONDAY_API_TOKEN": "monday_api_token.txt",
}

META_FILENAME = "meta.json"
SLACK_FILENAME = "slack.json"
TEAMS_FILENAME = "teams.json"
ADO_FILENAME = "ado.json"
JIRA_FILENAME = "jira.json"
LINEAR_FILENAME = "linear.json"
TRELLO_FILENAME = "trello.json"
CLICKUP_FILENAME = "clickup.json"
ASANA_FILENAME = "asana.json"
MONDAY_FILENAME = "monday.json"


def _data_dir(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    home = os.environ.get("PAM_HOME", "").strip()
    if home:
        return Path(home).expanduser()
    try:
        from platformdirs import user_data_dir

        return Path(user_data_dir("pam-pm"))
    except Exception:  # noqa: BLE001
        return Path.cwd()


def secret_path(name: str, data_dir: str | Path | None = None) -> Path:
    key = (name or "").strip()
    filename = SECRET_FILES.get(key)
    if not filename:
        raise ValueError(f"unknown secret: {key}")
    return _data_dir(data_dir) / filename


def save_secret(name: str, value: str, data_dir: str | Path | None = None) -> bool:
    """Save one secret, returning True when a value is stored.

    An empty value keeps the existing file, matching the provider
    pattern. Never logs the value.
    """
    key = (name or "").strip()
    if key not in SECRET_FILES:
        raise ValueError(f"unknown secret: {key}")
    directory = _data_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    text = (value or "").strip()
    if text:
        path = directory / str(SECRET_FILES[key])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        logger.info("secret saved: name=%s has_value=True", key)
        return True
    existing = directory / str(SECRET_FILES[key])
    has_value = False
    try:
        has_value = bool(existing.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError):
        has_value = False
    logger.info("secret saved: name=%s has_value=%s", key, has_value)
    return has_value


def load_secret(name: str, data_dir: str | Path | None = None) -> str:
    """Return the saved secret value, or empty when missing."""
    key = (name or "").strip()
    if key not in SECRET_FILES:
        raise ValueError(f"unknown secret: {key}")
    try:
        return (
            (_data_dir(data_dir) / str(SECRET_FILES[key]))
            .read_text(encoding="utf-8")
            .strip()
        )
    except (FileNotFoundError, OSError):
        return ""


def has_secret(name: str, data_dir: str | Path | None = None) -> bool:
    """Return True when a saved secret value exists."""
    try:
        return bool(load_secret(name, data_dir))
    except ValueError:
        return False


def env_first(*names: str) -> str:
    """Return the first non-blank value among env names, else ''."""
    for name in names:
        value = os.environ.get((name or "").strip(), "").strip()
        if value:
            return value
    return ""


def effective_secret(name: str, data_dir: str | Path | None = None) -> str:
    """Return env value when set, else saved value.

    Explicit env wins so servers and CI keep working.
    """
    env_value = os.environ.get((name or "").strip(), "").strip()
    if env_value:
        return env_value
    try:
        return load_secret(name, data_dir)
    except ValueError:
        return ""


def _read_json(filename: str, data_dir: str | Path | None) -> dict[str, Any]:
    try:
        raw = (_data_dir(data_dir) / filename).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return dict(data) if isinstance(data, dict) else {}


def _write_json(
    filename: str, payload: dict[str, Any], data_dir: str | Path | None
) -> None:
    directory = _data_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def save_meta_config(
    data_dir: str | Path | None = None,
    model: str = "",
    base_url: str = "",
    api_key: str = "",
) -> dict[str, Any]:
    saved = _read_json(META_FILENAME, data_dir)
    new_model = (model or "").strip() or str(saved.get("model", "") or "")
    new_base = (base_url or "").strip() or str(saved.get("base_url", "") or "")
    has_key = save_secret("META_API_KEY", api_key, data_dir)
    payload = {
        "model": new_model.strip(),
        "base_url": new_base.strip(),
        "has_key": bool(has_key),
    }
    _write_json(META_FILENAME, payload, data_dir)
    logger.info(
        "meta config saved: model=%s base_url=%s has_key=%s",
        payload["model"],
        payload["base_url"],
        payload["has_key"],
    )
    return dict(payload)


def load_meta_config(data_dir: str | Path | None = None) -> dict[str, Any]:
    saved = _read_json(META_FILENAME, data_dir)
    return {
        "model": str(saved.get("model", "") or "").strip(),
        "base_url": str(saved.get("base_url", "") or "").strip(),
        "has_key": has_secret("META_API_KEY", data_dir),
    }


def effective_meta_config(
    data_dir: str | Path | None = None,
) -> dict[str, str]:
    saved = load_meta_config(data_dir)
    model = os.environ.get("META_MODEL", "").strip() or saved["model"]
    base = os.environ.get("META_BASE_URL", "").strip() or saved["base_url"]
    key = effective_secret("META_API_KEY", data_dir)
    return {"model": model, "base_url": base, "api_key": key}


def save_slack_config(
    data_dir: str | Path | None = None,
    bot_token: str = "",
    app_token: str = "",
) -> dict[str, Any]:
    has_bot = save_secret("SLACK_BOT_TOKEN", bot_token, data_dir)
    has_app = save_secret("SLACK_APP_TOKEN", app_token, data_dir)
    payload = {"has_bot_token": bool(has_bot), "has_app_token": bool(has_app)}
    _write_json(SLACK_FILENAME, payload, data_dir)
    logger.info(
        "slack config saved: has_bot_token=%s has_app_token=%s",
        payload["has_bot_token"],
        payload["has_app_token"],
    )
    return dict(payload)


def load_slack_config(data_dir: str | Path | None = None) -> dict[str, Any]:
    return {
        "has_bot_token": has_secret("SLACK_BOT_TOKEN", data_dir),
        "has_app_token": has_secret("SLACK_APP_TOKEN", data_dir),
    }


def effective_slack_tokens(
    data_dir: str | Path | None = None,
) -> dict[str, str]:
    return {
        "bot_token": effective_secret("SLACK_BOT_TOKEN", data_dir),
        "app_token": effective_secret("SLACK_APP_TOKEN", data_dir),
    }


def save_teams_config(
    data_dir: str | Path | None = None,
    app_id: str = "",
    app_password: str = "",
    tenant_id: str = "",
    refresh_token: str = "",
) -> dict[str, Any]:
    has_app_id = save_secret("TEAMS_APP_ID", app_id, data_dir)
    has_password = save_secret("TEAMS_APP_PASSWORD", app_password, data_dir)
    has_tenant = save_secret("TEAMS_TENANT_ID", tenant_id, data_dir)
    if (refresh_token or "").strip():
        has_refresh = save_secret("TEAMS_REFRESH_TOKEN", refresh_token, data_dir)
    else:
        has_refresh = has_secret("TEAMS_REFRESH_TOKEN", data_dir)
    payload = {
        "has_app_id": bool(has_app_id),
        "has_app_password": bool(has_password),
        "has_tenant_id": bool(has_tenant),
        "has_refresh_token": bool(has_refresh),
    }
    _write_json(TEAMS_FILENAME, payload, data_dir)
    logger.info(
        "teams config saved: has_app_id=%s has_app_password=%s has_tenant_id=%s"
        " has_refresh_token=%s",
        payload["has_app_id"],
        payload["has_app_password"],
        payload["has_tenant_id"],
        payload["has_refresh_token"],
    )
    return dict(payload)


def load_teams_config(data_dir: str | Path | None = None) -> dict[str, Any]:
    return {
        "has_app_id": has_secret("TEAMS_APP_ID", data_dir),
        "has_app_password": has_secret("TEAMS_APP_PASSWORD", data_dir),
        "has_tenant_id": has_secret("TEAMS_TENANT_ID", data_dir),
        "has_refresh_token": has_secret("TEAMS_REFRESH_TOKEN", data_dir),
    }


def effective_teams_config(
    data_dir: str | Path | None = None,
) -> dict[str, str]:
    return {
        "app_id": effective_secret("TEAMS_APP_ID", data_dir),
        "app_password": effective_secret("TEAMS_APP_PASSWORD", data_dir),
        "tenant_id": effective_secret("TEAMS_TENANT_ID", data_dir),
        "refresh_token": effective_secret("TEAMS_REFRESH_TOKEN", data_dir),
    }


def save_ado_config(
    data_dir: str | Path | None = None,
    org_url: str = "",
    project: str = "",
    team: str = "",
    pat: str = "",
) -> dict[str, Any]:
    saved = _read_json(ADO_FILENAME, data_dir)
    new_org = (org_url or "").strip() or str(saved.get("org_url", "") or "")
    new_project = (project or "").strip() or str(saved.get("project", "") or "")
    new_team = (team or "").strip() or str(saved.get("team", "") or "")
    has_pat = save_secret("ADO_PAT", pat, data_dir)
    payload = {
        "org_url": new_org.strip().rstrip("/"),
        "project": new_project.strip(),
        "team": new_team.strip(),
        "has_pat": bool(has_pat),
    }
    _write_json(ADO_FILENAME, payload, data_dir)
    logger.info(
        "ado config saved: org_url=%s project=%s team=%s has_pat=%s",
        payload["org_url"],
        payload["project"],
        payload["team"],
        payload["has_pat"],
    )
    return dict(payload)


def load_ado_config(data_dir: str | Path | None = None) -> dict[str, Any]:
    saved = _read_json(ADO_FILENAME, data_dir)
    return {
        "org_url": str(saved.get("org_url", "") or "").strip().rstrip("/"),
        "project": str(saved.get("project", "") or "").strip(),
        "team": str(saved.get("team", "") or "").strip(),
        "has_pat": has_secret("ADO_PAT", data_dir),
    }


def effective_ado_config(
    data_dir: str | Path | None = None,
) -> dict[str, str]:
    saved = load_ado_config(data_dir)
    org = os.environ.get("ADO_ORG_URL", "").strip().rstrip("/") or saved["org_url"]
    proj = os.environ.get("ADO_PROJECT", "").strip() or saved["project"]
    team = os.environ.get("ADO_TEAM", "").strip() or saved["team"]
    pat = effective_secret("ADO_PAT", data_dir)
    return {"org_url": org, "project": proj, "team": team, "pat": pat}


def save_jira_config(
    data_dir: str | Path | None = None,
    base_url: str = "",
    email: str = "",
    project: str = "",
    api_token: str = "",
) -> dict[str, Any]:
    saved = _read_json(JIRA_FILENAME, data_dir)
    new_base = (base_url or "").strip() or str(saved.get("base_url", "") or "")
    new_email = (email or "").strip() or str(saved.get("email", "") or "")
    new_project = (project or "").strip() or str(saved.get("project", "") or "")
    has_token = save_secret("JIRA_API_TOKEN", api_token, data_dir)
    payload = {
        "base_url": new_base.strip().rstrip("/"),
        "email": new_email.strip(),
        "project": new_project.strip().upper(),
        "has_token": bool(has_token),
    }
    _write_json(JIRA_FILENAME, payload, data_dir)
    logger.info(
        "jira config saved: base_url=%s email=%s project=%s has_token=%s",
        payload["base_url"],
        payload["email"],
        payload["project"],
        payload["has_token"],
    )
    return dict(payload)


def load_jira_config(data_dir: str | Path | None = None) -> dict[str, Any]:
    saved = _read_json(JIRA_FILENAME, data_dir)
    return {
        "base_url": str(saved.get("base_url", "") or "").strip().rstrip("/"),
        "email": str(saved.get("email", "") or "").strip(),
        "project": str(saved.get("project", "") or "").strip().upper(),
        "has_token": has_secret("JIRA_API_TOKEN", data_dir),
    }


def effective_jira_config(
    data_dir: str | Path | None = None,
) -> dict[str, str]:
    saved = load_jira_config(data_dir)
    base = (
        env_first("JIRA_BASE_URL", "JIRA_SITE_URL").rstrip("/") or saved["base_url"]
    )
    mail = os.environ.get("JIRA_EMAIL", "").strip() or saved["email"]
    proj = (
        env_first("JIRA_PROJECT", "JIRA_PROJECT_KEY").upper() or saved["project"]
    )
    token = effective_secret("JIRA_API_TOKEN", data_dir)
    return {"base_url": base, "email": mail, "project": proj, "api_token": token}


def save_linear_config(
    data_dir: str | Path | None = None,
    team: str = "",
    api_key: str = "",
) -> dict[str, Any]:
    saved = _read_json(LINEAR_FILENAME, data_dir)
    new_team = (team or "").strip() or str(saved.get("team", "") or "")
    has_key = save_secret("LINEAR_API_KEY", api_key, data_dir)
    payload = {
        "team": new_team.strip(),
        "has_key": bool(has_key),
    }
    _write_json(LINEAR_FILENAME, payload, data_dir)
    logger.info(
        "linear config saved: team=%s has_key=%s",
        payload["team"],
        payload["has_key"],
    )
    return dict(payload)


def load_linear_config(data_dir: str | Path | None = None) -> dict[str, Any]:
    saved = _read_json(LINEAR_FILENAME, data_dir)
    return {
        "team": str(saved.get("team", "") or "").strip(),
        "has_key": has_secret("LINEAR_API_KEY", data_dir),
    }


def effective_linear_config(
    data_dir: str | Path | None = None,
) -> dict[str, str]:
    saved = load_linear_config(data_dir)
    team = env_first("LINEAR_TEAM", "LINEAR_TEAM_KEY") or saved["team"]
    key = effective_secret("LINEAR_API_KEY", data_dir)
    return {"team": team, "api_key": key}


def save_trello_config(
    data_dir: str | Path | None = None,
    board: str = "",
    api_key: str = "",
    token: str = "",
) -> dict[str, Any]:
    saved = _read_json(TRELLO_FILENAME, data_dir)
    new_board = (board or "").strip() or str(saved.get("board", "") or "")
    has_key = save_secret("TRELLO_API_KEY", api_key, data_dir)
    has_token = save_secret("TRELLO_TOKEN", token, data_dir)
    payload = {
        "board": new_board.strip(),
        "has_key": bool(has_key),
        "has_token": bool(has_token),
    }
    _write_json(TRELLO_FILENAME, payload, data_dir)
    logger.info(
        "trello config saved: board=%s has_key=%s has_token=%s",
        payload["board"],
        payload["has_key"],
        payload["has_token"],
    )
    return dict(payload)


def load_trello_config(data_dir: str | Path | None = None) -> dict[str, Any]:
    saved = _read_json(TRELLO_FILENAME, data_dir)
    return {
        "board": str(saved.get("board", "") or "").strip(),
        "has_key": has_secret("TRELLO_API_KEY", data_dir),
        "has_token": has_secret("TRELLO_TOKEN", data_dir),
    }


def effective_trello_config(
    data_dir: str | Path | None = None,
) -> dict[str, str]:
    saved = load_trello_config(data_dir)
    board = os.environ.get("TRELLO_BOARD", "").strip() or saved["board"]
    key = effective_secret("TRELLO_API_KEY", data_dir)
    token = effective_secret("TRELLO_TOKEN", data_dir)
    return {"board": board, "api_key": key, "token": token}


def save_clickup_config(
    data_dir: str | Path | None = None,
    team: str = "",
    list_ref: str = "",
    api_token: str = "",
) -> dict[str, Any]:
    saved = _read_json(CLICKUP_FILENAME, data_dir)
    new_team = (team or "").strip() or str(saved.get("team", "") or "")
    new_list = (list_ref or "").strip() or str(saved.get("list", "") or "")
    has_token = save_secret("CLICKUP_API_TOKEN", api_token, data_dir)
    payload = {
        "team": new_team.strip(),
        "list": new_list.strip(),
        "has_token": bool(has_token),
    }
    _write_json(CLICKUP_FILENAME, payload, data_dir)
    logger.info(
        "clickup config saved: team=%s list=%s has_token=%s",
        payload["team"],
        payload["list"],
        payload["has_token"],
    )
    return dict(payload)


def load_clickup_config(data_dir: str | Path | None = None) -> dict[str, Any]:
    saved = _read_json(CLICKUP_FILENAME, data_dir)
    return {
        "team": str(saved.get("team", "") or "").strip(),
        "list": str(saved.get("list", "") or "").strip(),
        "has_token": has_secret("CLICKUP_API_TOKEN", data_dir),
    }


def effective_clickup_config(
    data_dir: str | Path | None = None,
) -> dict[str, str]:
    saved = load_clickup_config(data_dir)
    team = env_first("CLICKUP_TEAM", "CLICKUP_TEAM_ID") or saved["team"]
    wanted = env_first("CLICKUP_LIST", "CLICKUP_LIST_ID") or saved["list"]
    token = effective_secret("CLICKUP_API_TOKEN", data_dir)
    return {"team": team, "list": wanted, "api_token": token}


def save_asana_config(
    data_dir: str | Path | None = None,
    project: str = "",
    api_token: str = "",
) -> dict[str, Any]:
    saved = _read_json(ASANA_FILENAME, data_dir)
    new_project = (project or "").strip() or str(saved.get("project", "") or "")
    has_token = save_secret("ASANA_API_TOKEN", api_token, data_dir)
    payload = {
        "project": new_project.strip(),
        "has_token": bool(has_token),
    }
    _write_json(ASANA_FILENAME, payload, data_dir)
    logger.info(
        "asana config saved: project=%s has_token=%s",
        payload["project"],
        payload["has_token"],
    )
    return dict(payload)


def load_asana_config(data_dir: str | Path | None = None) -> dict[str, Any]:
    saved = _read_json(ASANA_FILENAME, data_dir)
    return {
        "project": str(saved.get("project", "") or "").strip(),
        "has_token": has_secret("ASANA_API_TOKEN", data_dir),
    }


def effective_asana_config(
    data_dir: str | Path | None = None,
) -> dict[str, str]:
    saved = load_asana_config(data_dir)
    project = env_first("ASANA_PROJECT", "ASANA_PROJECT_ID") or saved["project"]
    token = effective_secret("ASANA_API_TOKEN", data_dir)
    return {"project": project, "api_token": token}


def save_monday_config(
    data_dir: str | Path | None = None,
    board: str = "",
    api_token: str = "",
) -> dict[str, Any]:
    saved = _read_json(MONDAY_FILENAME, data_dir)
    new_board = (board or "").strip() or str(saved.get("board", "") or "")
    has_token = save_secret("MONDAY_API_TOKEN", api_token, data_dir)
    payload = {
        "board": new_board.strip(),
        "has_token": bool(has_token),
    }
    _write_json(MONDAY_FILENAME, payload, data_dir)
    logger.info(
        "monday config saved: board=%s has_token=%s",
        payload["board"],
        payload["has_token"],
    )
    return dict(payload)


def load_monday_config(data_dir: str | Path | None = None) -> dict[str, Any]:
    saved = _read_json(MONDAY_FILENAME, data_dir)
    return {
        "board": str(saved.get("board", "") or "").strip(),
        "has_token": has_secret("MONDAY_API_TOKEN", data_dir),
    }


def effective_monday_config(
    data_dir: str | Path | None = None,
) -> dict[str, str]:
    saved = load_monday_config(data_dir)
    board = env_first("MONDAY_BOARD", "MONDAY_BOARD_ID") or saved["board"]
    token = effective_secret("MONDAY_API_TOKEN", data_dir)
    return {"board": board, "api_token": token}
