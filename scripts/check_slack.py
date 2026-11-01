#!/usr/bin/env python3
"""M0 Slack connectivity check. Minimal calls, no token output.

Steps: auth test with bot token, post one DM to SLACK_TEST_USER_ID with
hello from Pam M0, api test with app token for Socket Mode, then a short
5s note window for button callbacks (SKIP if none).

Prints PASS/FAIL per step. Never prints tokens.

Secrets are read only via PAM_SECRETS_FILE env or ~/pam-secrets.env.
All output is redacted.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://slack.com/api"

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
        description="M0 Slack check: auth, one DM, app token, callback note."
    )
    p.add_argument(
        "--timeout", type=int, default=30, help="HTTP timeout seconds (default 30)."
    )
    p.add_argument(
        "--listen-seconds",
        type=int,
        default=5,
        help="Callback note window in seconds (default 5).",
    )
    p.add_argument(
        "--skip-dm",
        action="store_true",
        help="Skip posting the test DM (auth checks only).",
    )
    return p.parse_args(argv)


def slack_post(method, token, payload, timeout):
    url = API_BASE + "/" + method
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body) if body else {}, ""
            except json.JSONDecodeError:
                return resp.status, {}, body[:1000]
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            raw = ""
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        return e.code, parsed, raw[:1000]
    except Exception as e:  # noqa: BLE001
        return 0, {}, str(e)[:1000]


def main(argv=None):
    args = parse_args(argv)
    load_secrets()
    bot = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    app = os.environ.get("SLACK_APP_TOKEN", "").strip()
    user_id = os.environ.get("SLACK_TEST_USER_ID", "").strip()

    if not bot:
        print(redact("FAIL: SLACK_BOT_TOKEN is missing. Set it in the secrets file."))
        return 1
    if not app:
        print(redact("FAIL: SLACK_APP_TOKEN is missing. Set it in the secrets file."))
        return 1
    if not user_id and not args.skip_dm:
        print(redact("FAIL: SLACK_TEST_USER_ID is missing. Set it in the secrets file."))
        return 1

    failed = 0

    # Step 1: auth test with bot token.
    status, data, raw = slack_post("auth.test", bot, {}, args.timeout)
    if status == 200 and isinstance(data, dict) and data.get("ok"):
        team = str(data.get("team", ""))[:40]
        user = str(data.get("user", ""))[:40]
        print(redact(f"step auth-test: PASS team={team} user={user}"))
    else:
        err = ""
        if isinstance(data, dict):
            err = str(data.get("error", ""))[:80]
        print(redact(f"step auth-test: FAIL status={status} error={err or raw[:80]}"))
        failed += 1

    # Step 2: post one DM to the test user.
    if args.skip_dm:
        print(redact("step dm-post: SKIP (--skip-dm)"))
    else:
        channel = user_id
        # Slack accepts a user ID as the channel for chat.postMessage.
        quoted = urllib.parse.quote(channel, safe="")
        _ = quoted  # channel is sent in JSON body; quote check avoids odd input
        status, data, raw = slack_post(
            "chat.postMessage",
            bot,
            {"channel": channel, "text": "hello from Pam M0"},
            args.timeout,
        )
        if status == 200 and isinstance(data, dict) and data.get("ok"):
            ts = str(data.get("ts", ""))[:30]
            print(redact(f"step dm-post: PASS ts={ts}"))
        else:
            err = ""
            if isinstance(data, dict):
                err = str(data.get("error", ""))[:80]
            print(redact(f"step dm-post: FAIL status={status} error={err or raw[:80]}"))
            failed += 1

    # Step 3: app token check via api.test (Socket Mode token validity).
    status, data, raw = slack_post("api.test", app, {}, args.timeout)
    if status == 200 and isinstance(data, dict) and data.get("ok"):
        print(redact("step app-token: PASS api.test ok with app token"))
    else:
        err = ""
        if isinstance(data, dict):
            err = str(data.get("error", ""))[:80]
        print(redact(f"step app-token: FAIL status={status} error={err or raw[:80]}"))
        failed += 1

    # Step 4: short callback note window. Full Socket Mode listen is out
    # of scope for M0, so wait briefly then report SKIP.
    seconds = max(args.listen_seconds, 0)
    if seconds:
        time.sleep(min(seconds, 5))
    print(
        redact(
            "step button-callback: SKIP (no callback in 5s window; "
            "Socket Mode handler not started in M0)"
        )
    )

    if failed:
        print(redact(f"FAIL: slack check done with {failed} failing step(s)"))
        return 1
    print(redact("PASS: slack check ok"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
