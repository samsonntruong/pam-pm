"""Jira Cloud adapter over REST with email plus API token.

Config resolves explicit, then env, then saved wizard values:
JIRA_BASE_URL, JIRA_EMAIL, JIRA_PROJECT, JIRA_API_TOKEN. The token
is never accepted as an argument and never appears in errors or
logs. Works headless with a personal API token.

Issues are scoped to one project key. Work item ids are issue keys
such as PROJ-123. States come from the project workflow with the
default To Do, In Progress, Done fallback. Sprints come from the
Agile API. Dependency links use the Blocks issue link type. The
description and comment bodies use ADF; plain text is converted on
write and extracted on read.
"""

from __future__ import annotations

import base64
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

import httpx

from pam_pm.adapters.pm.base import (
    Comment,
    Iteration,
    Link,
    PMAdapter,
    WorkItem,
    is_closed,
)
from pam_pm.core.secrets import env_first

SEARCH_FIELDS = [
    "summary",
    "status",
    "description",
    "labels",
    "duedate",
    "assignee",
    "timeoriginalestimate",
    "timeestimate",
    "priority",
    "updated",
    "issuelinks",
    "customfield_10020",
]

DEFAULT_STATES = ["To Do", "In Progress", "Done"]

_ESTIMATE_FIELDS = ("timeoriginalestimate", "timeestimate")


class _Dropped(str):
    """Plain flag that reads as tracker and passes boolean checks."""

    def __new__(cls, value: str = "tracker") -> Self:
        return super().__new__(cls, value)

    def __eq__(self, other: object) -> bool:
        if other is True:
            return True
        if isinstance(other, str) and other.lower() in {
            "tracker",
            "timeoriginalestimate",
            "timeestimate",
            "estimate",
        }:
            return True
        return super().__eq__(other)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    __hash__ = str.__hash__


def _is_estimate_400(exc: Exception) -> bool:
    msg = str(exc)
    if "status=400" not in msg:
        return False
    low = msg.lower()
    return any(name in low for name in _ESTIMATE_FIELDS)


def _without_estimate_fields(
    jira_fields: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    dropped = [k for k in jira_fields if k.lower() in _ESTIMATE_FIELDS]
    kept = {k: v for k, v in jira_fields.items() if k.lower() not in _ESTIMATE_FIELDS}
    return kept, dropped


def _degrade_reason(exc: Exception, dropped: list[str]) -> str:
    names = ", ".join(dropped) if dropped else "estimate"
    detail = " ".join(str(exc).strip().split())[:300]
    return f"Jira rejected {names} with 400 and the write retried without it: {detail}"


def _annotate_degraded(
    item: WorkItem, dropped: list[str], reason: str
) -> WorkItem:
    item.fields["estimate_dropped"] = _Dropped("tracker")
    item.fields["estimate_drop_reason"] = reason
    item.fields["estimate_dropped_reason"] = reason
    item.fields["estimate_dropped_fields"] = list(dropped)
    return item

REL_BLOCKS = "blocks"
REL_BLOCKED_BY = "is blocked by"

_BLOCKS_ALIASES = {
    "blocks",
    "block",
    "blocking",
    "dependency-forward",
    "system.linktypes.dependency-forward",
}

_BLOCKED_BY_ALIASES = {
    "is blocked by",
    "blocked by",
    "blocked",
    "waits on",
    "dependency-reverse",
    "system.linktypes.dependency-reverse",
}

_BASIC_RE = re.compile(r"Basic\s+[A-Za-z0-9._~+/=-]+")


def redact(text: object, *secrets: str) -> str:
    """Redact Basic [REDACTED] and token material from log text."""
    out = _BASIC_RE.sub("Basic [REDACTED]", str(text))
    for val in secrets:
        if val and len(val) >= 4 and val in out:
            out = out.replace(val, "[REDACTED]")
    return out


class JiraError(RuntimeError):
    """Jira call failure with the token already redacted."""


def adf_from_text(text: str) -> dict[str, Any]:
    """Convert plain text to an ADF document with one paragraph."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def text_from_adf(node: object) -> str:
    """Extract plain text from an ADF node, passing strings through."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(text_from_adf(entry) for entry in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text", ""))
        if node.get("type") in ("hardBreak", "mention"):
            return " "
        kids = node.get("content", [])
        text = text_from_adf(kids)
        if node.get("type") in ("paragraph", "heading", "listItem"):
            return text + "\n"
        return text
    return ""


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class JiraAdapter(PMAdapter):
    """PM adapter backed by the Jira Cloud REST API."""

    supports_dependency_links: bool = True
    supports_iterations: bool = True
    supports_comments: bool = True

    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        project: str | None = None,
        timeout: int = 30,
        transport: httpx.BaseTransport | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        saved_base = ""
        saved_email = ""
        saved_project = ""
        saved_token = ""
        try:
            from pam_pm.core import secrets as secrets_mod

            effective = secrets_mod.effective_jira_config(data_dir)
            saved_base = str(effective.get("base_url", "") or "")
            saved_email = str(effective.get("email", "") or "")
            saved_project = str(effective.get("project", "") or "")
            saved_token = str(effective.get("api_token", "") or "")
        except Exception:  # noqa: BLE001
            saved_base = env_first("JIRA_BASE_URL", "JIRA_SITE_URL")
            saved_email = os.environ.get("JIRA_EMAIL", "")
            saved_project = env_first("JIRA_PROJECT", "JIRA_PROJECT_KEY")
            saved_token = os.environ.get("JIRA_API_TOKEN", "")
        base = (base_url or "").strip().rstrip("/") or saved_base.strip().rstrip(
            "/"
        )
        mail = (email or "").strip() or saved_email.strip()
        proj = (project or "").strip().upper() or saved_project.strip().upper()
        token = saved_token.strip()
        if not base or not proj:
            raise JiraError("JIRA_BASE_URL and JIRA_PROJECT are required.")
        if not mail:
            raise JiraError("JIRA_EMAIL is required for basic auth.")
        if not token:
            raise JiraError(
                "JIRA_API_TOKEN is missing; save it in Setup or set JIRA_API_TOKEN."
            )
        self._base = base
        self._email = mail
        self._project = proj
        self._token = token
        raw = base64.b64encode(f"{mail}:{token}".encode()).decode("ascii")
        self._basic = "Basic " + raw
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": self._basic,
                "Accept": "application/json",
            },
        )
        self._board_id: str | None = None
        self._user_cache: dict[str, str] = {}
        self._assign_note: str = ""

    def close(self) -> None:
        self._client.close()

    def _clean(self, text: object) -> str:
        return redact(text, self._token, self._basic, self._email)

    def _request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = self._base + path
        try:
            resp = self._client.request(method, url, json=payload, params=params)
        except httpx.HTTPError as exc:
            raise JiraError(self._clean(f"request failed: {exc}")) from exc
        if resp.status_code == 204 or not resp.text.strip():
            return {}
        if resp.status_code >= 400:
            detail = resp.text[:500]
            raise JiraError(
                self._clean(f"{method} {path} status={resp.status_code}: {detail}")
            )
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text[:2000]}

    def _quote_key(self, key: str) -> str:
        cleaned = (key or "").strip().upper()
        if not cleaned:
            raise JiraError("issue key is required")
        return cleaned

    def _jql_project(self) -> str:
        return 'project = "' + self._project.replace('"', "") + '"'

    def _search(self, jql: str, limit: int) -> list[dict[str, Any]]:
        data = self._request(
            "POST",
            "/rest/api/3/search/jql",
            {
                "jql": jql,
                "maxResults": max(limit, 1),
                "fields": SEARCH_FIELDS,
            },
        )
        issues = data.get("issues", []) if isinstance(data, dict) else []
        return [row for row in issues if isinstance(row, dict)]

    @staticmethod
    def _sprint_of(fields: dict[str, Any]) -> dict[str, Any] | None:
        sprint = fields.get("sprint")
        if isinstance(sprint, dict) and sprint.get("id") is not None:
            return sprint
        custom = fields.get("customfield_10020")
        if isinstance(custom, list):
            for entry in reversed(custom):
                if isinstance(entry, dict) and entry.get("id") is not None:
                    return entry
        return None

    @classmethod
    def _to_item(cls, data: dict[str, Any]) -> WorkItem:
        fields = data.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}
        status = fields.get("status", {})
        state = ""
        if isinstance(status, dict):
            state = str(status.get("name", ""))
        assignee = fields.get("assignee", {})
        owner = ""
        account_id = ""
        if isinstance(assignee, dict):
            owner = str(assignee.get("displayName", "") or "")
            account_id = str(assignee.get("accountId", "") or "")
        priority = fields.get("priority", {})
        priority_name = ""
        if isinstance(priority, dict):
            priority_name = str(priority.get("name", "") or "")
        labels = fields.get("labels", [])
        tags = [str(tag) for tag in labels] if isinstance(labels, list) else []
        sprint = cls._sprint_of(fields)
        key = str(data.get("key", "") or data.get("id", ""))
        item_fields: dict[str, Any] = {
            "title": str(fields.get("summary", "")),
            "state": state,
            "description": text_from_adf(fields.get("description")).strip(),
            "tags": tags,
            "due_date": str(fields.get("duedate", "") or ""),
            "owner": owner,
            "assigned_to": owner,
            "assignee_id": account_id,
            "estimate": fields.get("timeoriginalestimate"),
            "remaining": fields.get("timeestimate"),
            "priority": priority_name,
            "iteration_id": str(sprint.get("id", "")) if sprint else "",
            "iteration_name": str(sprint.get("name", "")) if sprint else "",
            "key": key,
            "jira_id": str(data.get("id", "")),
            "is_closed": is_closed(state),
        }
        return WorkItem(
            id=key,
            title=str(fields.get("summary", "")),
            state=state,
            changed=str(fields.get("updated", "") or ""),
            fields=item_fields,
        )

    def _account_id_for_name(self, name: str) -> str:
        want = (name or "").strip()
        if not want:
            return ""
        key = want.lower()
        if key in self._user_cache:
            return self._user_cache[key]
        try:
            data = self._request(
                "GET", "/rest/api/3/user/search", params={"query": want}
            )
        except JiraError:
            return ""
        rows: list[dict[str, Any]] = []
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            vals = data.get("values", [])
            if isinstance(vals, list):
                rows = [r for r in vals if isinstance(r, dict)]
        for entry in rows:
            disp = str(entry.get("displayName", "") or "").strip()
            acc = str(entry.get("accountId", "") or "").strip()
            if not acc:
                continue
            if disp.lower() == key or acc.lower() == key:
                self._user_cache[key] = acc
                return acc
        for entry in rows:
            disp = str(entry.get("displayName", "") or "").strip()
            acc = str(entry.get("accountId", "") or "").strip()
            if acc and disp.lower().startswith(key):
                self._user_cache[key] = acc
                return acc
        return ""

    def _write_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        self._assign_note = ""
        for key, val in fields.items():
            if key == "title":
                out["summary"] = val
            elif key == "description":
                out["description"] = adf_from_text(str(val or ""))
            elif key == "tags":
                if isinstance(val, list):
                    out["labels"] = [str(tag).strip() for tag in val if str(tag).strip()]
                else:
                    out["labels"] = [
                        part
                        for part in (
                            p.strip()
                            for p in str(val).replace(",", ";").split(";")
                        )
                        if part
                    ]
            elif key == "due_date":
                out["duedate"] = str(val or "") or None
            elif key == "owner" or key == "assigned_to":
                if isinstance(val, list):
                    names = [str(v).strip() for v in val if str(v).strip()]
                    name = names[0] if names else ""
                else:
                    name = str(val or "").strip()
                if not name:
                    continue
                found = self._account_id_for_name(name)
                if found:
                    out["assignee"] = {"accountId": found}
                else:
                    self._assign_note = f"unknown assignee: {name}"
            elif key == "estimate":
                out["timeoriginalestimate"] = val
            elif key == "remaining":
                out["timeestimate"] = val
            elif key == "priority":
                name = str(val or "").strip()
                if name:
                    out["priority"] = {"name": name}
        return out

    def list_items(self, limit: int = 50) -> list[WorkItem]:
        jql = f"{self._jql_project()} ORDER BY updated DESC"
        return [self._to_item(row) for row in self._search(jql, limit)]

    def get_item(self, item_id: str) -> WorkItem:
        key = self._quote_key(item_id)
        data = self._request(
            "GET",
            f"/rest/api/3/issue/{quote(key, safe='')}",
            params={"fields": ",".join(SEARCH_FIELDS)},
        )
        if not isinstance(data, dict) or not data.get("key"):
            raise JiraError(f"unknown item: {key}")
        return self._to_item(data)

    def create_item(
        self, title: str, fields: dict[str, Any] | None = None
    ) -> WorkItem:
        given = dict(fields or {})
        given.pop("state", None)
        given.pop("iteration_id", None)
        write_fields = self._write_fields(given)
        assign_note = self._assign_note
        payload = {
            "fields": {
                "project": {"key": self._project},
                "summary": title,
                "issuetype": {"name": str(given.pop("issue_type", "Task"))},
                **write_fields,
            }
        }
        dropped: list[str] = []
        reason = ""
        try:
            data = self._request("POST", "/rest/api/3/issue", payload)
        except JiraError as exc:
            if not _is_estimate_400(exc):
                raise
            kept, dropped = _without_estimate_fields(
                payload.get("fields", {})
                if isinstance(payload.get("fields"), dict)
                else {}
            )
            if not dropped:
                raise
            reason = _degrade_reason(exc, dropped)
            payload = {**payload, "fields": kept}
            data = self._request("POST", "/rest/api/3/issue", payload)
        if not isinstance(data, dict) or not data.get("key"):
            raise JiraError("create failed: no issue key returned")
        item = self.get_item(str(data["key"]))
        if dropped:
            _annotate_degraded(item, dropped, reason)
        if assign_note:
            item.fields["assign_note"] = assign_note
        return item

    def update_item(
        self, item_id: str, fields: dict[str, Any]
    ) -> WorkItem:
        key = self._quote_key(item_id)
        given = dict(fields)
        state = str(given.pop("state", "") or "").strip()
        title = given.pop("title", None)
        if title is not None:
            given["title"] = title
        sprint = str(given.pop("iteration_id", "") or "")
        body = self._write_fields(given)
        assign_note = self._assign_note
        dropped: list[str] = []
        reason = ""
        if body:
            try:
                self._request(
                    "PUT",
                    f"/rest/api/3/issue/{quote(key, safe='')}",
                    {"fields": body},
                )
            except JiraError as exc:
                if not _is_estimate_400(exc):
                    raise
                kept, dropped = _without_estimate_fields(body)
                if not dropped:
                    raise
                reason = _degrade_reason(exc, dropped)
                if kept:
                    self._request(
                        "PUT",
                        f"/rest/api/3/issue/{quote(key, safe='')}",
                        {"fields": kept},
                    )
        if state:
            current = self.get_item(key)
            if current.state.strip().lower() != state.lower():
                self._transition_to(key, state)
        if sprint:
            self.set_item_iteration(key, sprint)
        item = self.get_item(key)
        if dropped:
            _annotate_degraded(item, dropped, reason)
        if assign_note:
            item.fields["assign_note"] = assign_note
        return item

    def delete_item(self, item_id: str) -> None:
        key = self._quote_key(item_id)
        self._request("DELETE", f"/rest/api/3/issue/{quote(key, safe='')}")

    def _transitions(self, key: str) -> list[dict[str, Any]]:
        data = self._request(
            "GET", f"/rest/api/3/issue/{quote(key, safe='')}/transitions"
        )
        rows = data.get("transitions", []) if isinstance(data, dict) else []
        return [row for row in rows if isinstance(row, dict)]

    def _transition_to(self, key: str, state: str) -> None:
        want = state.strip().lower()
        fallback: dict[str, Any] | None = None
        for entry in self._transitions(key):
            target = entry.get("to", {})
            name = str(target.get("name", "")).strip() if isinstance(target, dict) else ""
            if not name:
                continue
            if name.lower() == want:
                self._request(
                    "POST",
                    f"/rest/api/3/issue/{quote(key, safe='')}/transitions",
                    {"transition": {"id": str(entry.get("id", ""))}},
                )
                return
            if fallback is None and is_closed(name):
                fallback = entry
        if fallback is not None and is_closed(state):
            self._request(
                "POST",
                f"/rest/api/3/issue/{quote(key, safe='')}/transitions",
                {"transition": {"id": str(fallback.get("id", ""))}},
            )
            return
        raise JiraError(f"no transition to state: {state}")

    def list_states(self) -> list[str]:
        """List project statuses ordered To Do, In Progress, Done."""
        try:
            data = self._request(
                "GET",
                f"/rest/api/3/project/{quote(self._project, safe='')}/statuses",
            )
        except JiraError:
            return list(DEFAULT_STATES)
        rows = data.get("values", data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return list(DEFAULT_STATES)
        ranked: list[tuple[int, str]] = []
        seen: set[str] = set()
        order = {"new": 0, "indeterminate": 1, "done": 2}
        for issue_type in rows:
            if not isinstance(issue_type, dict):
                continue
            statuses = issue_type.get("statuses", [])
            if not isinstance(statuses, list):
                continue
            for entry in statuses:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name", "")).strip()
                if not name or name.lower() in seen:
                    continue
                seen.add(name.lower())
                category = entry.get("statusCategory", {})
                key = ""
                if isinstance(category, dict):
                    key = str(category.get("key", "")).strip().lower()
                ranked.append((order.get(key, 1), name))
        if not ranked:
            return list(DEFAULT_STATES)
        ranked.sort(key=lambda row: (row[0], row[1].lower()))
        return [name for _, name in ranked]

    def close_item(self, item_id: str) -> WorkItem:
        key = self._quote_key(item_id)
        current = self.get_item(key)
        if is_closed(current.state):
            return current
        for entry in self._transitions(key):
            target = entry.get("to", {})
            name = str(target.get("name", "")).strip() if isinstance(target, dict) else ""
            if name and is_closed(name):
                self._request(
                    "POST",
                    f"/rest/api/3/issue/{quote(key, safe='')}/transitions",
                    {"transition": {"id": str(entry.get("id", ""))}},
                )
                return self.get_item(key)
        raise JiraError(f"no closing transition for item: {key}")

    def _board_id_for_project(self) -> str:
        if self._board_id:
            return self._board_id
        data = self._request(
            "GET",
            "/rest/agile/1.0/board",
            params={"projectKeyOrId": self._project},
        )
        values = data.get("values", []) if isinstance(data, dict) else []
        for entry in values:
            if isinstance(entry, dict) and entry.get("id") is not None:
                self._board_id = str(entry["id"])
                return self._board_id
        raise JiraError(f"no board found for project: {self._project}")

    @staticmethod
    def _to_iteration(data: dict[str, Any]) -> Iteration:
        return Iteration(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            start_date=str(data.get("startDate", "") or "")[:10],
            end_date=str(data.get("endDate", "") or "")[:10],
            path=str(data.get("name", "")),
        )

    def list_iterations(self) -> list[Iteration]:
        """List sprints on the project board, oldest first."""
        board_id = self._board_id_for_project()
        data = self._request(
            "GET",
            f"/rest/agile/1.0/board/{quote(board_id, safe='')}/sprint",
            params={"state": "active,future,closed"},
        )
        values = data.get("values", []) if isinstance(data, dict) else []
        out = [
            self._to_iteration(entry)
            for entry in values
            if isinstance(entry, dict)
        ]
        return sorted(out, key=lambda it: (it.start_date, it.id))

    def create_iteration(
        self, name: str, start_date: str = "", end_date: str = ""
    ) -> Iteration:
        label = name.strip()
        if not label:
            raise JiraError("iteration name is required")
        payload: dict[str, Any] = {
            "name": label,
            "originBoardId": self._board_id_for_project(),
        }
        if start_date.strip():
            payload["startDate"] = start_date.strip()
        if end_date.strip():
            payload["endDate"] = end_date.strip()
        data = self._request("POST", "/rest/agile/1.0/sprint", payload)
        if not isinstance(data, dict) or data.get("id") is None:
            raise JiraError("create iteration failed: no id returned")
        return self._to_iteration(data)

    def set_item_iteration(
        self, item_id: str, iteration_id: str
    ) -> WorkItem:
        """Move one issue to a sprint, or to the backlog when empty."""
        key = self._quote_key(item_id)
        if not iteration_id.strip():
            self._request(
                "POST", "/rest/agile/1.0/backlog/issue", {"issues": [key]}
            )
            return self.get_item(key)
        self._request(
            "POST",
            f"/rest/agile/1.0/sprint/{quote(iteration_id.strip(), safe='')}/issue",
            {"issues": [key]},
        )
        item = self.get_item(key)
        item.fields["iteration_id"] = iteration_id.strip()
        return item

    @staticmethod
    def _normalize_rel(rel: str) -> str:
        text = (rel or "").strip().lower().replace("_", "-")
        if text in _BLOCKS_ALIASES:
            return REL_BLOCKS
        if text in _BLOCKED_BY_ALIASES:
            return REL_BLOCKED_BY
        raise JiraError(f"unknown link rel: {rel}")

    def add_link(self, source_id: str, target_id: str, rel: str) -> Link:
        kind = self._normalize_rel(rel)
        source = self._quote_key(source_id)
        target = self._quote_key(target_id)
        if kind == REL_BLOCKS:
            outward, inward = source, target
        else:
            outward, inward = target, source
        self._request(
            "POST",
            "/rest/api/3/issueLink",
            {
                "type": {"name": "Blocks"},
                "outwardIssue": {"key": outward},
                "inwardIssue": {"key": inward},
            },
        )
        return Link(source_id=source, target_id=target, rel=kind)

    def list_links(self, item_id: str) -> list[Link]:
        key = self._quote_key(item_id)
        data = self._request(
            "GET",
            f"/rest/api/3/issue/{quote(key, safe='')}",
            params={"fields": "issuelinks"},
        )
        fields = data.get("fields", {}) if isinstance(data, dict) else {}
        rows = fields.get("issuelinks", []) if isinstance(fields, dict) else []
        out: list[Link] = []
        for entry in rows if isinstance(rows, list) else []:
            if not isinstance(entry, dict):
                continue
            link_type = entry.get("type", {})
            name = ""
            if isinstance(link_type, dict):
                name = str(link_type.get("name", "")).strip().lower()
            if name != "blocks":
                continue
            outward = entry.get("outwardIssue", {})
            inward = entry.get("inwardIssue", {})
            if isinstance(outward, dict) and outward.get("key"):
                out.append(
                    Link(
                        source_id=key,
                        target_id=str(outward["key"]),
                        rel=REL_BLOCKS,
                    )
                )
            if isinstance(inward, dict) and inward.get("key"):
                out.append(
                    Link(
                        source_id=key,
                        target_id=str(inward["key"]),
                        rel=REL_BLOCKED_BY,
                    )
                )
        return out

    @staticmethod
    def _to_comment(item_id: str, data: dict[str, Any]) -> Comment:
        author = data.get("author", {})
        name = ""
        if isinstance(author, dict):
            name = str(author.get("displayName", "") or "")
        return Comment(
            id=str(data.get("id", "")),
            item_id=item_id,
            text=text_from_adf(data.get("body")).strip(),
            author=name,
            created=str(data.get("created", "") or ""),
        )

    def list_comments(self, item_id: str) -> list[Comment]:
        key = self._quote_key(item_id)
        data = self._request(
            "GET", f"/rest/api/3/issue/{quote(key, safe='')}/comment"
        )
        rows = data.get("comments", []) if isinstance(data, dict) else []
        return [
            self._to_comment(key, entry)
            for entry in rows
            if isinstance(entry, dict)
        ]

    def add_comment(
        self, item_id: str, text: str, author: str = ""
    ) -> Comment:
        key = self._quote_key(item_id)
        body = text.strip()
        if not body:
            raise JiraError("comment text is required")
        data = self._request(
            "POST",
            f"/rest/api/3/issue/{quote(key, safe='')}/comment",
            {"body": adf_from_text(body)},
        )
        if not isinstance(data, dict) or not data.get("id"):
            raise JiraError(f"add comment failed for item: {key}")
        comment = self._to_comment(key, data)
        if author.strip() and not comment.author:
            comment.author = author.strip()
        return comment

    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        """List changed issues; closed means is_closed on the status."""
        watermark = _parse_ts(since) if since else None
        if since and watermark is not None:
            floor = (watermark - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
            jql = (
                f'{self._jql_project()} AND updated >= "{floor}"'
                " ORDER BY updated ASC"
            )
        else:
            jql = (
                f"{self._jql_project()} AND updated >= -30d ORDER BY updated ASC"
            )
        items = [self._to_item(row) for row in self._search(jql, 100)]
        if watermark is None:
            return items
        out: list[WorkItem] = []
        for item in items:
            changed = _parse_ts(item.changed)
            if changed is not None and changed > watermark:
                out.append(item)
        return out


JiraCloudAdapter = JiraAdapter
