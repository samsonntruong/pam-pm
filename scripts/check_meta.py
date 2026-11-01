#!/usr/bin/env python3
"""M0 Meta Model API connectivity check. One cheap call with tools and image.

Default is Responses API POST {BASE}/responses with META_MODEL, with fallback
to /chat/completions on 404. Uses a single short call that includes both a
get_status tool definition and a 1px PNG inline image.

Secrets are read only via PAM_SECRETS_FILE env or ~/pam-secrets.env.
Never prints secret values. All output is redacted.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 1px transparent PNG, base64 encoded. Used inline to test image input.
PNG_1PX_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5"
    "ErkJggg=="
)

# Pricing per million tokens. From META_API_NOTES.md.
STD_INPUT = 1.25 / 1_000_000
STD_CACHED = 0.15 / 1_000_000
STD_OUTPUT = 4.25 / 1_000_000
CONTRIB_INPUT = 0.10 / 1_000_000
CONTRIB_CACHED = 0.002 / 1_000_000
CONTRIB_OUTPUT = 0.20 / 1_000_000

REDACT_PATTERNS = [
    (re.compile(r"xoxb-[A-Za-z0-9-]+"), "[REDACTED_xoxb]"),
    (re.compile(r"xapp-[A-Za-z0-9-]+"), "[REDACTED_xapp]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]+"), "[REDACTED_github]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
]

SECRET_ENV_KEYS = (
    "META_API_KEY",
    "ADO_PAT",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "GITHUB_TOKEN",
)


def redact(text, extra=None):
    """Redact token-like values from log text. Never returns secrets."""
    s = str(text)
    for pattern, repl in REDACT_PATTERNS:
        s = pattern.sub(repl, s)
    secrets = []
    for key in SECRET_ENV_KEYS:
        val = os.environ.get(key, "")
        if val and len(val) >= 4:
            secrets.append(val)
    if extra:
        if isinstance(extra, str):
            secrets.append(extra)
        else:
            secrets.extend([v for v in extra if v and len(v) >= 4])
    for val in secrets:
        if val in s:
            s = s.replace(val, "[REDACTED]")
    return s


def secrets_path():
    """Return secrets file path. Only PAM_SECRETS_FILE or ~/pam-secrets.env."""
    custom = os.environ.get("PAM_SECRETS_FILE", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / "pam-secrets.env"


def load_secrets():
    """Load KEY=value pairs from the secrets file into os.environ.

    Only reads PAM_SECRETS_FILE or ~/pam-secrets.env. Never reads repo .env.
    Existing env vars win. Returns path or None if the file is missing.
    """
    path = secrets_path()
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val
    return path


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="M0 Meta check: one Responses API call with tool and image."
    )
    p.add_argument("--base-url", default="", help="Override META_BASE_URL.")
    p.add_argument("--model", default="", help="Override META_MODEL.")
    p.add_argument(
        "--max-tokens",
        type=int,
        default=1024,
        help="Max output tokens for the probe call (default 1024).",
    )
    p.add_argument(
        "--timeout", type=int, default=30, help="HTTP timeout seconds (default 30)."
    )
    p.add_argument(
        "--no-fallback",
        action="store_true",
        help="Do not fall back to chat/completions on 404.",
    )
    return p.parse_args(argv)


def split_base(base_url):
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        return base + "/responses", base + "/chat/completions"
    return base + "/v1/responses", base + "/v1/chat/completions"


def do_post(url, payload, api_key, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body), ""
            except json.JSONDecodeError:
                return resp.status, {}, body[:2000]
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            raw = ""
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        return e.code, parsed, raw[:2000]
    except Exception as e:  # noqa: BLE001
        return 0, {}, str(e)[:2000]


def extract_usage(payload):
    """Return (prompt_tokens, completion_tokens, cached_tokens or None)."""
    usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
    pt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    ct = usage.get("completion_tokens", usage.get("output_tokens", 0))
    cached = None
    details = usage.get("prompt_tokens_details", {}) or {}
    if isinstance(details, dict) and "cached_tokens" in details:
        cached = details.get("cached_tokens")
    if cached is None:
        details = usage.get("input_tokens_details", {}) or {}
        if isinstance(details, dict) and "cached_tokens" in details:
            cached = details.get("cached_tokens")
    if cached is None and "cached_tokens" in usage:
        cached = usage.get("cached_tokens")
    try:
        pt = int(pt)
    except (TypeError, ValueError):
        pt = 0
    try:
        ct = int(ct)
    except (TypeError, ValueError):
        ct = 0
    if cached is not None:
        try:
            cached = int(cached)
        except (TypeError, ValueError):
            cached = None
    return pt, ct, cached


def estimate_cost(prompt_tokens, cached_tokens, completion_tokens, model):
    is_contrib = "contributor" in model.lower()
    if is_contrib:
        rin, rcached, rout = CONTRIB_INPUT, CONTRIB_CACHED, CONTRIB_OUTPUT
    else:
        rin, rcached, rout = STD_INPUT, STD_CACHED, STD_OUTPUT
    cached = cached_tokens or 0
    fresh = max(prompt_tokens - cached, 0)
    return fresh * rin + cached * rcached + completion_tokens * rout, is_contrib


def build_responses_payload(model, max_tokens):
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Reply with the single word ok."},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64," + PNG_1PX_B64,
                    },
                ],
            }
        ],
        "tools": [
            {
                "type": "function",
                "name": "get_status",
                "description": "Return a short status string.",
                "parameters": {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                    "additionalProperties": False,
                },
            }
        ],
        "tool_choice": "auto",
        "max_output_tokens": max_tokens,
    }


def build_chat_payload(model, max_tokens):
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Reply with the single word ok."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + PNG_1PX_B64
                        },
                    },
                ],
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_status",
                    "description": "Return a short status string.",
                    "parameters": {
                        "type": "object",
                        "properties": {"detail": {"type": "string"}},
                    },
                },
            }
        ],
        "tool_choice": "auto",
        "max_tokens": max_tokens,
    }


def main(argv=None):
    args = parse_args(argv)
    load_secrets()
    base_url = args.base_url.strip() or os.environ.get("META_BASE_URL", "").strip()
    model = args.model.strip() or os.environ.get("META_MODEL", "").strip()
    api_key = os.environ.get("META_API_KEY", "").strip()

    if not base_url:
        print(redact("FAIL: META_BASE_URL is missing. Set it in the secrets file."))
        return 1
    if not model:
        print(redact("FAIL: META_MODEL is missing. Set it in the secrets file."))
        return 1
    if not api_key:
        print(redact("FAIL: META_API_KEY is missing. Set it in the secrets file."))
        return 1

    host = urllib.parse.urlparse(base_url).hostname or base_url
    tier = "contributor" if "contributor" in model.lower() else "standard"
    print(redact(f"base_url host: {host}"))
    print(redact(f"model: {model}"))
    print(redact(f"tier inference: {tier}"))

    responses_url, chat_url = split_base(base_url)
    payload = build_responses_payload(model, args.max_tokens)
    status, data, raw = do_post(responses_url, payload, api_key, args.timeout)
    used_url = responses_url

    if status == 404 and not args.no_fallback:
        print(redact("responses endpoint 404, falling back to chat/completions"))
        payload = build_chat_payload(model, args.max_tokens)
        status, data, raw = do_post(chat_url, payload, api_key, args.timeout)
        used_url = chat_url

    if status == 0:
        print(redact(f"FAIL: request error: {raw}"))
        return 1
    if status == 401:
        print(redact("FAIL: 401 unauthorized. Check META_API_KEY by name only."))
        return 1
    if status == 429:
        print(redact("FAIL: 429 rate limited. Retry later with backoff."))
        return 1
    if status != 200:
        err = ""
        if isinstance(data, dict):
            err = str(data.get("error", data))[:500]
        print(redact(f"FAIL: POST {used_url} returned {status}: {err or raw[:500]}"))
        if "tool_choice" in (err + raw):
            print(redact("tool_choice auto ok: no (see error above)"))
        return 1

    print(redact("tool_choice auto ok: yes"))
    print(
        redact(
            f"max_tokens note: probe used {args.max_tokens}; "
            "use 4000 or more for interviews because reasoning "
            "tokens are spent first and billed as output."
        )
    )

    # If the API returned 200 with no error block, image input is ok.
    if isinstance(data, dict) and data.get("error"):
        print(redact("image ok: no"))
    else:
        print(redact("image ok: yes (1px PNG inline accepted)"))

    pt, ct, cached = extract_usage(data)
    if cached is None:
        print(redact(f"usage: prompt_tokens={pt} completion_tokens={ct}"))
        print(redact(f"usage alias: completed_tokens={ct} cached_tokens=n/a"))
    else:
        print(
            redact(
                f"usage: prompt_tokens={pt} completion_tokens={ct} cached_tokens={cached}"
            )
        )
        print(redact(f"usage alias: completed_tokens={ct}"))

    cost, is_contrib = estimate_cost(pt, cached, ct, model)
    rate = "contributor" if is_contrib else "standard"
    print(redact(f"estimated cost: ${cost:.6f} at {rate} rates"))
    print(redact("PASS: meta check ok"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
