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
    "ADO_PAT": "ado_pat.txt",
}

META_FILENAME = "meta.json"
SLACK_FILENAME = "slack.json"
ADO_FILENAME = "ado.json"


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


def save_secret(
    name: str, value: str, data_dir: str | Path | None = None
) -> bool:
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
        path.write_text(text, encoding="utf-8")
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
        return (_data_dir(data_dir) / str(SECRET_FILES[key])).read_text(
            encoding="utf-8"
        ).strip()
    except (FileNotFoundError, OSError):
        return ""


def has_secret(name: str, data_dir: str | Path | None = None) -> bool:
    """Return True when a saved secret value exists."""
    try:
        return bool(load_secret(name, data_dir))
    except ValueError:
        return False


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
    (directory / filename).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


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
