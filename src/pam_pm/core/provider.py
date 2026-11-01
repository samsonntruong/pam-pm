"""Model provider choice: Meta default plus custom OpenAI compatible.

Meta uses the Responses API transport. Custom endpoints use the same
ModelClient with a base URL override. Keys for custom endpoints live
only in the local data directory, never in logs or wizard progress.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PROVIDER_META = "meta"
PROVIDER_CUSTOM = "custom"
PROVIDER_CHOICES = (PROVIDER_META, PROVIDER_CUSTOM)

PROVIDER_FILENAME = "provider.json"
PROVIDER_KEY_FILENAME = "provider_key.txt"


@dataclass
class ProviderConfig:
    """Provider selection without secret material."""

    name: str = PROVIDER_META
    base_url: str = ""
    model: str = ""
    key_ref: str = ""

    def is_meta(self) -> bool:
        return normalize_provider(self.name) == PROVIDER_META


def normalize_provider(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in ("custom", "openai", "openai_compatible", "compat"):
        return PROVIDER_CUSTOM
    return PROVIDER_META


def provider_label(value: object) -> str:
    if normalize_provider(value) == PROVIDER_CUSTOM:
        return "Custom OpenAI-compatible"
    return "Meta"


def _data_dir(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    home = os.environ.get("PAM_HOME", "").strip()
    if home:
        return Path(home).expanduser()
    try:
        from pam_pm.core.config import Config

        return Config().ensure_data_dir()
    except Exception:  # noqa: BLE001
        return Path.cwd()


def provider_path(data_dir: str | Path | None = None) -> Path:
    return _data_dir(data_dir) / PROVIDER_FILENAME


def provider_key_path(data_dir: str | Path | None = None) -> Path:
    return _data_dir(data_dir) / PROVIDER_KEY_FILENAME


def validate_custom_config(
    base_url: str, model: str, api_key: str
) -> tuple[bool, str, str]:
    base = (base_url or "").strip()
    name = (model or "").strip()
    key = (api_key or "").strip()
    if not base:
        return (
            False,
            "Custom provider check failed. Base URL is missing.",
            "Paste the endpoint base URL, for example https://host:8000, then test again.",
        )
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return (
            False,
            "Custom provider check failed. Base URL is not valid.",
            "Use a full http or https URL with a host, then test again.",
        )
    if not name:
        return (
            False,
            "Custom provider check failed. Model name is missing.",
            "Paste the model name for the custom endpoint, then test again.",
        )
    if not key:
        return (
            False,
            "Custom provider check failed. API key is missing.",
            "Paste the API key for the custom endpoint, then test again.",
        )
    return (True, "Custom provider settings look complete.", "")


def save_provider(
    data_dir: str | Path | None,
    provider: str,
    base_url: str = "",
    model: str = "",
    api_key: str = "",
) -> dict[str, Any]:
    canonical = normalize_provider(provider)
    directory = _data_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    base = (base_url or "").strip()
    name = (model or "").strip()
    key = (api_key or "").strip()
    has_key = False
    key_ref = ""
    if canonical == PROVIDER_CUSTOM:
        if key:
            key_file = directory / PROVIDER_KEY_FILENAME
            key_file.write_text(key, encoding="utf-8")
            if os.name != "nt":
                try:
                    os.chmod(key_file, 0o600)
                except OSError:
                    pass
            has_key = True
            key_ref = PROVIDER_KEY_FILENAME
        else:
            existing = directory / PROVIDER_KEY_FILENAME
            if existing.exists():
                has_key = True
                key_ref = PROVIDER_KEY_FILENAME
    else:
        base = ""
        name = ""
    payload = {
        "provider": canonical,
        "base_url": base,
        "model": name,
        "has_key": has_key,
        "key_ref": key_ref,
    }
    (directory / PROVIDER_FILENAME).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    logger.info(
        "provider saved: provider=%s base_url=%s model=%s has_key=%s",
        canonical,
        base,
        name,
        has_key,
    )
    return dict(payload)


def load_provider(data_dir: str | Path | None = None) -> dict[str, Any]:
    directory = _data_dir(data_dir)
    path = directory / PROVIDER_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {
            "provider": PROVIDER_META,
            "base_url": "",
            "model": "",
            "has_key": False,
            "key_ref": "",
        }
    try:
        data = json.loads(raw)
    except ValueError:
        return {
            "provider": PROVIDER_META,
            "base_url": "",
            "model": "",
            "has_key": False,
            "key_ref": "",
        }
    if not isinstance(data, dict):
        return {
            "provider": PROVIDER_META,
            "base_url": "",
            "model": "",
            "has_key": False,
            "key_ref": "",
        }
    canonical = normalize_provider(data.get("provider", PROVIDER_META))
    if canonical == PROVIDER_META:
        return {
            "provider": PROVIDER_META,
            "base_url": "",
            "model": "",
            "has_key": False,
            "key_ref": "",
        }
    key_file = directory / PROVIDER_KEY_FILENAME
    has_key = bool(key_file.exists())
    return {
        "provider": PROVIDER_CUSTOM,
        "base_url": str(data.get("base_url", "") or "").strip(),
        "model": str(data.get("model", "") or "").strip(),
        "has_key": has_key,
        "key_ref": PROVIDER_KEY_FILENAME if has_key else "",
    }


def load_provider_key(data_dir: str | Path | None = None) -> str:
    try:
        return (_data_dir(data_dir) / PROVIDER_KEY_FILENAME).read_text(
            encoding="utf-8"
        ).strip()
    except (FileNotFoundError, OSError):
        return ""


def load_config(data_dir: str | Path | None = None) -> ProviderConfig:
    saved = load_provider(data_dir)
    return ProviderConfig(
        name=str(saved.get("provider", PROVIDER_META)),
        base_url=str(saved.get("base_url", "") or ""),
        model=str(saved.get("model", "") or ""),
        key_ref=str(saved.get("key_ref", "") or ""),
    )


def client_kwargs(data_dir: str | Path | None = None) -> dict[str, str]:
    saved = load_provider(data_dir)
    if normalize_provider(saved.get("provider")) == PROVIDER_META:
        return {}
    out: dict[str, str] = {}
    if str(saved.get("base_url", "")).strip():
        out["base_url"] = str(saved["base_url"]).strip()
    if str(saved.get("model", "")).strip():
        out["model"] = str(saved["model"]).strip()
    key = load_provider_key(data_dir)
    if key:
        out["api_key"] = key
    return out


def test_with_client(client: Any) -> tuple[bool, str, str]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_time",
                "description": "Return the current time.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    try:
        first = client.chat(
            [{"role": "user", "content": "Call get_time now."}], tools=tools
        )
    except Exception as exc:  # noqa: BLE001
        return (
            False,
            "Custom provider check failed. Tool calling did not work.",
            f"Check the endpoint speaks OpenAI tool calls, then test again. Detail: {exc}",
        )
    calls = getattr(first, "tool_calls", []) or []
    if not calls:
        return (
            False,
            "Custom provider check failed. Tool calling did not work.",
            "Check the endpoint supports OpenAI tool calls, then test again.",
        )
    try:
        second = client.chat(
            [
                {
                    "role": "user",
                    "content": 'Reply with JSON only: {"ok": true}.',
                }
            ]
        )
    except Exception as exc:  # noqa: BLE001
        return (
            False,
            "Custom provider check failed. Structured output did not work.",
            f"Check the model returns JSON on request, then test again. Detail: {exc}",
        )
    text = str(getattr(second, "text", "") or "").strip()
    try:
        parsed = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except (ValueError, AttributeError):
        return (
            False,
            "Custom provider check failed. Structured output did not work.",
            "Check the model returns JSON on request, then test again.",
        )
    if not isinstance(parsed, dict) or "ok" not in parsed:
        return (
            False,
            "Custom provider check failed. Structured output did not work.",
            "Check the model returns a JSON object with an ok key, then test again.",
        )
    try:
        third = client.chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is in this image?"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,iVBORw0KGgo="
                            },
                        },
                    ],
                }
            ]
        )
    except Exception as exc:  # noqa: BLE001
        return (
            False,
            "Custom provider check failed. Image input did not work.",
            f"Check the endpoint accepts image input, then test again. Detail: {exc}",
        )
    if not str(getattr(third, "text", "") or "").strip():
        return (
            False,
            "Custom provider check failed. Image input did not work.",
            "Check the endpoint accepts image input, then test again.",
        )
    return (
        True,
        "Custom provider check passed. Tool calling, structured output, and image input all work.",
        "",
    )
