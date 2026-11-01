#!/usr/bin/env python3
"""M0 Azure DevOps connectivity check. One call per step, cheap reads.

Steps: list projects, get project/team/iteration, WIQL query recent items
by ChangedDate, create one test work item then delete it (or tag pam-test
and close), verify Dependency-Forward/Reverse link names.

Prints each step PASS/FAIL with IDs only. Never prints the PAT.

Secrets are read only via PAM_SECRETS_FILE env or ~/pam-secrets.env.
All output is redacted.
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_VERSION = "7.1"

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
        description="M0 ADO check: projects, team, WIQL, one test item, links."
    )
    p.add_argument("--org-url", default="", help="Override ADO_ORG_URL.")
    p.add_argument("--project", default="", help="Override ADO_PROJECT.")
    p.add_argument("--team", default="", help="Override ADO_TEAM.")
    p.add_argument(
        "--timeout", type=int, default=30, help="HTTP timeout seconds (default 30)."
    )
    p.add_argument(
        "--skip-write",
        action="store_true",
        help="Skip the create/delete test work item step.",
    )
    return p.parse_args(argv)


def auth_header(pat):
    token = base64.b64encode((":" + pat).encode("utf-8")).decode("ascii")
    return "Basic " + token


def do_request(url, pat, timeout, method="GET", payload=None, content_type=None):
    data = None
    headers = {"Authorization": auth_header(pat), "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = content_type or "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body) if body else {}, ""
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


def main(argv=None):
    args = parse_args(argv)
    load_secrets()
    org = args.org_url.strip() or os.environ.get("ADO_ORG_URL", "").strip()
    project = args.project.strip() or os.environ.get("ADO_PROJECT", "").strip()
    team = args.team.strip() or os.environ.get("ADO_TEAM", "").strip()
    pat = os.environ.get("ADO_PAT", "").strip()

    if not org:
        print(redact("FAIL: ADO_ORG_URL is missing. Set it in the secrets file."))
        return 1
    if not project:
        print(redact("FAIL: ADO_PROJECT is missing. Set it in the secrets file."))
        return 1
    if not team:
        print(redact("FAIL: ADO_TEAM is missing. Set it in the secrets file."))
        return 1
    if not pat:
        print(redact("FAIL: ADO_PAT is missing. Set it in the secrets file."))
        return 1

    org = org.rstrip("/")
    proj_enc = urllib.parse.quote(project, safe="")
    team_enc = urllib.parse.quote(team, safe="")
    failed = 0

    # Step 1: list projects.
    url = f"{org}/_apis/projects?api-version={API_VERSION}"
    status, data, raw = do_request(url, pat, args.timeout)
    if status == 200:
        items = data.get("value", []) if isinstance(data, dict) else []
        ids = [str(i.get("id", ""))[:8] for i in items if isinstance(i, dict)][:5]
        names = [str(i.get("name", "")) for i in items if isinstance(i, dict)]
        found = project in names
        print(redact(f"step list-projects: PASS count={len(items)} ids={','.join(ids)}"))
        if not found:
            print(redact(f"step list-projects: note project '{project}' not in list"))
    else:
        print(redact(f"step list-projects: FAIL status={status} detail={raw[:200]}"))
        failed += 1

    # Step 2: get project.
    url = f"{org}/_apis/projects/{proj_enc}?api-version={API_VERSION}"
    status, data, _raw = do_request(url, pat, args.timeout)
    process_template = "Agile"
    if status == 200 and isinstance(data, dict):
        pid = str(data.get("id", ""))[:36]
        print(redact(f"step get-project: PASS id={pid}"))
        try:
            caps = data.get("capabilities", {})
            template = caps.get("processTemplate", {}) if isinstance(caps, dict) else {}
            name = str(template.get("templateName", "")).strip() if isinstance(template, dict) else ""
            if name.lower() in ("agile", "scrum"):
                process_template = name.capitalize() if name.lower() == "agile" else "Scrum"
        except Exception:  # noqa: BLE001
            process_template = "Agile"
        print(redact(f"step process-template: note template={process_template}"))
    else:
        print(redact(f"step get-project: FAIL status={status}"))
        failed += 1

    # Step 3: get team.
    url = f"{org}/_apis/projects/{proj_enc}/teams/{team_enc}?api-version={API_VERSION}"
    status, data, _raw2 = do_request(url, pat, args.timeout)
    if status == 200 and isinstance(data, dict):
        tid = str(data.get("id", ""))[:36]
        print(redact(f"step get-team: PASS id={tid}"))
    else:
        print(redact(f"step get-team: FAIL status={status}"))
        failed += 1

    # Step 4: list iterations for the team.
    url = (
        f"{org}/{proj_enc}/{team_enc}/_apis/work/teamsettings/iterations"
        f"?api-version={API_VERSION}"
    )
    status, data, _raw3 = do_request(url, pat, args.timeout)
    if status == 200:
        items = data.get("value", []) if isinstance(data, dict) else []
        first = ""
        if items and isinstance(items[0], dict):
            first = str(items[0].get("id", ""))[:36]
        print(redact(f"step list-iterations: PASS count={len(items)} first={first}"))
    else:
        print(redact(f"step list-iterations: FAIL status={status}"))
        failed += 1

    # Step 5: WIQL query recent items by ChangedDate.
    url = f"{org}/{proj_enc}/_apis/wit/wiql?api-version={API_VERSION}"
    wiql = {
        "query": (
            "SELECT [System.Id] FROM WorkItems "
            "WHERE [System.ChangedDate] >= @Today - 30 "
            "ORDER BY [System.ChangedDate] DESC"
        )
    }
    status, data, _raw4 = do_request(url, pat, args.timeout, method="POST", payload=wiql)
    if status == 200 and isinstance(data, dict):
        refs = data.get("workItems", []) or []
        ids = [str(r.get("id", "")) for r in refs if isinstance(r, dict)][:5]
        print(redact(f"step wiql-recent: PASS count={len(refs)} ids={','.join(ids)}"))
    else:
        print(redact(f"step wiql-recent: FAIL status={status}"))
        failed += 1

    # Step 6: create one test work item then delete or tag and close.
    if args.skip_write:
        print(redact("step test-item: SKIP (--skip-write)"))
    else:
        url = f"{org}/{proj_enc}/_apis/wit/workitems/$Task?api-version={API_VERSION}"
        patch = [
            {
                "op": "add",
                "path": "/fields/System.Title",
                "value": "pam-test M0 connectivity check - safe to delete",
            },
            {"op": "add", "path": "/fields/System.Tags", "value": "pam-test"},
        ]
        status, data, _raw5 = do_request(
            url,
            pat,
            args.timeout,
            method="POST",
            payload=patch,
            content_type="application/json-patch+json",
        )
        if status in (200, 201) and isinstance(data, dict) and data.get("id"):
            wid = str(data.get("id"))
            print(redact(f"step test-item-create: PASS id={wid}"))
            del_url = (
                f"{org}/{proj_enc}/_apis/wit/workitems/{wid}"
                f"?api-version={API_VERSION}"
            )
            dstatus, _, _ = do_request(del_url, pat, args.timeout, method="DELETE")
            if dstatus == 200:
                print(redact(f"step test-item-delete: PASS id={wid}"))
            else:
                close_url = (
                    f"{org}/{proj_enc}/_apis/wit/workitems/{wid}"
                    f"?api-version={API_VERSION}"
                )
                mapped_closed = "Closed" if process_template == "Agile" else "Done"
                fallback_closed = "Done" if mapped_closed == "Closed" else "Closed"
                cstatus = 0
                for state_value in (mapped_closed, fallback_closed):
                    close_patch = [
                        {"op": "add", "path": "/fields/System.Tags", "value": "pam-test"},
                        {"op": "add", "path": "/fields/System.State", "value": state_value},
                    ]
                    cstatus, _, _ = do_request(
                        close_url,
                        pat,
                        args.timeout,
                        method="PATCH",
                        payload=close_patch,
                        content_type="application/json-patch+json",
                    )
                    if cstatus == 200:
                        break
                if cstatus == 200:
                    print(redact(f"step test-item-close: PASS id={wid} tagged pam-test"))
                else:
                    print(
                        redact(
                            f"step test-item-cleanup: FAIL id={wid} "
                            f"delete={dstatus} close={cstatus}"
                        )
                    )
                    failed += 1
        else:
            print(redact(f"step test-item-create: FAIL status={status}"))
            failed += 1

    # Step 7: verify Dependency link names.
    url = f"{org}/_apis/wit/workitemrelationtypes?api-version={API_VERSION}"
    status, data, _raw6 = do_request(url, pat, args.timeout)
    if status == 200 and isinstance(data, dict):
        names = set()
        for item in data.get("value", []) or []:
            if isinstance(item, dict):
                ref = str(item.get("referenceName", ""))
                if ref:
                    names.add(ref)
        fwd = "System.LinkTypes.Dependency-Forward" in names
        rev = "System.LinkTypes.Dependency-Reverse" in names
        if fwd and rev:
            print(redact("step link-names: PASS Dependency-Forward/Reverse found"))
        else:
            print(
                redact(
                    f"step link-names: FAIL forward={fwd} reverse={rev} "
                    f"count={len(names)}"
                )
            )
            failed += 1
    else:
        print(redact(f"step link-names: FAIL status={status}"))
        failed += 1

    if failed:
        print(redact(f"FAIL: ado check done with {failed} failing step(s)"))
        return 1
    print(redact("PASS: ado check ok"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
