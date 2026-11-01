#!/usr/bin/env python3
"""M0 SAM 3.1 request shape discovery. At most one API call.

Tries GET {BASE}/models to discover SAM availability. On any 400 or 404,
prints the fallback decision (use whole image to Muse Spark vision) and
exits 0 with a FALLBACK note. Never burns more than one call.

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
        description="M0 SAM check: discover SAM 3.1 shape with one call."
    )
    p.add_argument("--base-url", default="", help="Override META_BASE_URL.")
    p.add_argument("--model", default="", help="Override META_VISION_MODEL.")
    p.add_argument(
        "--timeout", type=int, default=30, help="HTTP timeout seconds (default 30)."
    )
    return p.parse_args(argv)


def do_get(url, api_key, timeout):
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": "Bearer " + api_key},
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


def print_fallback(reason):
    print(redact(f"SAM probe result: {reason}"))
    print(
        redact(
            "FALLBACK: use whole image to Muse Spark vision "
            "(no SAM call, send the photo as vision input)."
        )
    )


def main(argv=None):
    args = parse_args(argv)
    load_secrets()
    base_url = args.base_url.strip() or os.environ.get("META_BASE_URL", "").strip()
    model = (
        args.model.strip()
        or os.environ.get("META_VISION_MODEL", "").strip()
        or "sam-3.1"
    )
    api_key = os.environ.get("META_API_KEY", "").strip()

    if not base_url:
        print(redact("FAIL: META_BASE_URL is missing. Set it in the secrets file."))
        return 1
    if not api_key:
        print(redact("FAIL: META_API_KEY is missing. Set it in the secrets file."))
        return 1

    host = urllib.parse.urlparse(base_url).hostname or base_url
    print(redact(f"base_url host: {host}"))
    print(redact(f"vision model: {model}"))

    base = base_url.strip().rstrip("/")
    models_url = base + "/models"
    print(redact("probe: GET /models (one call max)"))
    status, data, raw = do_get(models_url, api_key, args.timeout)

    if status == 0:
        print(redact(f"FAIL: request error: {raw}"))
        return 1
    if status in (400, 404):
        print_fallback(f"{status} on GET /models")
        print(redact("PASS with FALLBACK note: sam check done, one call used"))
        return 0
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
        print(redact(f"FAIL: GET /models returned {status}: {err or raw[:500]}"))
        return 1

    names = []
    if isinstance(data, dict):
        items = data.get("data", [])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    names.append(str(item.get("id"))[:80])
    shown = ", ".join(names[:20]) if names else "(no model ids listed)"
    print(redact(f"models: {shown}"))
    want = model.lower()
    found = any(want in n.lower() or n.lower() in want for n in names)
    if found:
        print(redact(f"PASS: {model} listed in /models; shape still TBD from docs"))
    else:
        print_fallback(f"{model} not listed in /models")
        print(redact("PASS with FALLBACK note: sam check done, one call used"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
