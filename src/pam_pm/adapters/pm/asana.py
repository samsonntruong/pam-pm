"""Asana adapter over REST with a personal token.

Config resolves explicit, then env, then saved wizard values:
ASANA_PROJECT, ASANA_API_TOKEN. The token is never accepted as
an argument and never appears in errors or logs. Works headless
with a personal token against app.asana.com.

Projects map to Pam projects: tasks are scoped to one project.
Sections map to states in section order. Tasks carry notes, tags,
due dates, assignees, and permalinks. Dependencies use the
depends-on links where the waiting task depends on its blocker.
Comments are task stories. Change detection polls the project
for tasks modified after a watermark.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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

API_BASE = "https://app.asana.com/api/1.0"

TASK_FIELDS = (
    "gid,name,notes,memberships.section.gid,memberships.section.name,"
    "memberships.project.gid,tags.name,due_on,assignee.name,"
    "modified_at,permalink_url,projects.gid,projects.name"
)

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


def redact(text: object, *secrets: str) -> str:
    """Redact token material from log text."""
    out = str(text)
    for val in secrets:
        if val and len(val) >= 4 and val in out:
            out = out.replace(val, "[REDACTED]")
    return out


class AsanaError(RuntimeError):
    """Asana call failure with the token already redacted."""


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


def _due_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:10]


def _data_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("data", [])
    elif isinstance(payload, list):
        rows = payload
    else:
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _data_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        data = payload.get("data", {})
        return dict(data) if isinstance(data, dict) else {}
    return dict(payload) if isinstance(payload, dict) else {}


class AsanaAdapter(PMAdapter):
    """PM adapter backed by the Asana REST API."""

    supports_dependency_links: bool = True
    supports_iterations: bool = False
    supports_comments: bool = True

    def __init__(
        self,
        project: str | None = None,
        timeout: int = 30,
        transport: httpx.BaseTransport | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        saved_project = ""
        saved_token = ""
        try:
            from pam_pm.core import secrets as secrets_mod

            effective = secrets_mod.effective_asana_config(data_dir)
            saved_project = str(effective.get("project", "") or "")
            saved_token = str(effective.get("api_token", "") or "")
        except Exception:  # noqa: BLE001
            saved_project = os.environ.get("ASANA_PROJECT", "")
            saved_token = os.environ.get("ASANA_API_TOKEN", "")
        want_project = (project or "").strip() or saved_project.strip()
        token = saved_token.strip()
        if not want_project:
            raise AsanaError("ASANA_PROJECT is required.")
        if not token:
            raise AsanaError(
                "ASANA_API_TOKEN is missing; save it in Setup or set ASANA_API_TOKEN."
            )
        self._project_ref = want_project
        self._token = token
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        self._project_gid: str | None = None
        self._project_name = ""
        self._workspace_gid = ""

    def close(self) -> None:
        self._client.close()

    def _clean(self, text: object) -> str:
        return redact(text, self._token)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = API_BASE + path
        body = {"data": dict(payload)} if payload is not None else None
        try:
            resp = self._client.request(method, url, params=params, json=body)
        except httpx.HTTPError as exc:
            raise AsanaError(self._clean(f"request failed: {exc}")) from exc
        if resp.status_code == 204 or not resp.text.strip():
            return {"data": {}}
        if resp.status_code >= 400:
            detail = resp.text[:500]
            try:
                err_body = resp.json()
                errors = err_body.get("errors", []) if isinstance(err_body, dict) else []
                if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                    detail = str(errors[0].get("message", "") or detail)[:500]
            except ValueError:
                pass
            raise AsanaError(
                self._clean(f"{method} {path} status={resp.status_code}: {detail}")
            )
        try:
            return resp.json()
        except ValueError:
            return {"data": {"raw": resp.text[:2000]}}

    def _project_gid_for_ref(self) -> str:
        if self._project_gid:
            return self._project_gid
        data = self._request(
            "GET",
            "/projects",
            {"opt_fields": "gid,name,workspace.gid,workspace.name", "limit": "100"},
        )
        want = self._project_ref.strip().lower()
        for entry in _data_list(data):
            for field in ("gid", "name"):
                if str(entry.get(field, "") or "").strip().lower() == want:
                    self._project_gid = str(entry.get("gid", ""))
                    self._project_name = str(entry.get("name", "") or "")
                    workspace = entry.get("workspace", {})
                    if isinstance(workspace, dict):
                        self._workspace_gid = str(workspace.get("gid", "") or "")
                    return self._project_gid
        raise AsanaError(f"unknown Asana project: {self._project_ref}")

    def _workspace_for_project(self) -> str:
        if self._workspace_gid:
            return self._workspace_gid
        gid = self._project_gid_for_ref()
        data = self._request(
            "GET",
            f"/projects/{quote(gid, safe='')}",
            {"opt_fields": "gid,name,workspace.gid,workspace.name"},
        )
        entry = _data_dict(data)
        self._project_name = str(entry.get("name", "") or self._project_name)
        workspace = entry.get("workspace", {})
        if isinstance(workspace, dict):
            self._workspace_gid = str(workspace.get("gid", "") or "")
        return self._workspace_gid

    def _sections(self) -> list[dict[str, Any]]:
        gid = self._project_gid_for_ref()
        data = self._request(
            "GET",
            f"/projects/{quote(gid, safe='')}/sections",
            {"opt_fields": "gid,name"},
        )
        return _data_list(data)

    def _section_gid_for_name(self, name: str) -> str:
        want = (name or "").strip().lower()
        if not want:
            raise AsanaError("Asana section name is required")
        for entry in self._sections():
            if str(entry.get("name", "")).strip().lower() == want:
                return str(entry.get("gid", ""))
        raise AsanaError(f"unknown Asana section: {name}")

    def _user_id_for_name(self, name: str) -> str:
        want = (name or "").strip().lower()
        if not want:
            return ""
        workspace = self._workspace_for_project()
        params: dict[str, Any] = {"opt_fields": "gid,name", "limit": "100"}
        if workspace:
            params["workspace"] = workspace
        try:
            data = self._request("GET", "/users", params)
        except AsanaError:
            return ""
        for entry in _data_list(data):
            if str(entry.get("name", "") or "").strip().lower() == want:
                return str(entry.get("gid", ""))
        return ""

    def _tag_ids_for_names(self, names: list[str]) -> list[str]:
        if not names:
            return []
        workspace = self._workspace_for_project()
        params: dict[str, Any] = {"opt_fields": "gid,name", "limit": "100"}
        if workspace:
            params["workspace"] = workspace
        try:
            data = self._request("GET", "/tags", params)
        except AsanaError:
            return []
        by_name = {
            str(e.get("name", "") or "").strip().lower(): str(e.get("gid", ""))
            for e in _data_list(data)
            if str(e.get("name", "") or "").strip()
        }
        return [by_name[n.lower()] for n in names if n.lower() in by_name]

    @staticmethod
    def _split_names(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [
            part
            for part in (p.strip() for p in str(value or "").replace(",", ";").split(";"))
            if part
        ]

    def _project_tasks(self, modified_since: str = "") -> list[dict[str, Any]]:
        gid = self._project_gid_for_ref()
        params: dict[str, Any] = {"project": gid, "opt_fields": TASK_FIELDS, "limit": "100"}
        if modified_since.strip():
            params["modified_since"] = modified_since.strip()
        data = self._request("GET", "/tasks", params)
        return _data_list(data)

    def _state_of(self, data: dict[str, Any]) -> tuple[str, str]:
        project_gid = self._project_gid or ""
        memberships = data.get("memberships", [])
        fallback_name = ""
        fallback_gid = ""
        if isinstance(memberships, list):
            for entry in memberships:
                if not isinstance(entry, dict):
                    continue
                section = entry.get("section", {})
                proj = entry.get("project", {})
                name = ""
                sgid = ""
                pgid = ""
                if isinstance(section, dict):
                    name = str(section.get("name", "") or "")
                    sgid = str(section.get("gid", "") or "")
                if isinstance(proj, dict):
                    pgid = str(proj.get("gid", "") or "")
                if not fallback_name and name:
                    fallback_name = name
                    fallback_gid = sgid
                if project_gid and pgid == project_gid and name:
                    return name, sgid
        return fallback_name, fallback_gid

    def _to_item(self, data: dict[str, Any]) -> WorkItem:
        state, section_gid = self._state_of(data)
        tag_rows = data.get("tags", [])
        tags: list[str] = []
        if isinstance(tag_rows, list):
            for entry in tag_rows:
                if isinstance(entry, dict) and str(entry.get("name", "") or "").strip():
                    tags.append(str(entry["name"]).strip())
                elif isinstance(entry, str) and entry.strip():
                    tags.append(entry.strip())
        assignee = data.get("assignee", {})
        owner = ""
        if isinstance(assignee, dict):
            owner = str(assignee.get("name", "") or "")
        project_rows = data.get("projects", [])
        project_gid = self._project_gid or ""
        project_name = self._project_name
        if isinstance(project_rows, list) and project_rows:
            first = project_rows[0] if isinstance(project_rows[0], dict) else {}
            project_gid = str(first.get("gid", "") or project_gid)
            project_name = str(first.get("name", "") or project_name)
        url = str(data.get("permalink_url", "") or "")
        gid = str(data.get("gid", "") or "")
        item_fields: dict[str, Any] = {
            "title": str(data.get("name", "")),
            "state": state,
            "description": str(data.get("notes", "") or ""),
            "tags": tags,
            "due_date": _due_date(data.get("due_on")),
            "owner": owner,
            "assigned_to": owner,
            "estimate": None,
            "remaining": None,
            "iteration_id": "",
            "section_gid": section_gid,
            "section_id": section_gid,
            "project_gid": project_gid,
            "project_name": project_name,
            "url": url,
            "permalink_url": url,
            "is_closed": is_closed(state),
        }
        return WorkItem(
            id=gid,
            title=str(data.get("name", "")),
            state=state,
            changed=str(data.get("modified_at", "") or ""),
            fields=item_fields,
        )

    def _write_payload(self, fields: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, val in fields.items():
            if key == "title":
                out["name"] = val
            elif key == "description":
                out["notes"] = str(val or "")
            elif key == "due_date":
                text = str(val or "").strip()
                out["due_on"] = text[:10] if text else None
            elif key == "owner" or key == "assigned_to":
                names = self._split_names(val)
                if names:
                    found = self._user_id_for_name(names[0])
                    if found:
                        out["assignee"] = found
                else:
                    out["assignee"] = None
            elif key == "tags":
                names = self._split_names(val)
                out["tags"] = self._tag_ids_for_names(names)
        return out

    def _require_id(self, item_id: str) -> str:
        cleaned = (item_id or "").strip()
        if not cleaned:
            raise AsanaError("task id is required")
        return cleaned

    def _move_to_section(self, task_gid: str, section_gid: str) -> None:
        self._request(
            "POST",
            f"/sections/{quote(section_gid, safe='')}/addTask",
            None,
            {"task": task_gid},
        )

    def list_items(self, limit: int = 50) -> list[WorkItem]:
        items = [self._to_item(row) for row in self._project_tasks()]
        items.sort(key=lambda item: item.changed, reverse=True)
        return items[:limit]

    def get_item(self, item_id: str) -> WorkItem:
        gid = self._require_id(item_id)
        data = self._request(
            "GET",
            f"/tasks/{quote(gid, safe='')}",
            {"opt_fields": TASK_FIELDS},
        )
        entry = _data_dict(data)
        if not entry.get("gid"):
            raise AsanaError(f"unknown item: {gid}")
        return self._to_item(entry)

    def create_item(self, title: str, fields: dict[str, Any] | None = None) -> WorkItem:
        given = dict(fields or {})
        given.pop("iteration_id", None)
        state = str(given.pop("state", "") or "").strip()
        if state:
            section_gid = self._section_gid_for_name(state)
        else:
            sections = self._sections()
            if not sections:
                raise AsanaError("no sections on this project")
            section_gid = str(sections[0].get("gid", ""))
        payload = {
            "name": title,
            "projects": [self._project_gid_for_ref()],
            **self._write_payload(given),
        }
        data = self._request("POST", "/tasks", None, payload)
        entry = _data_dict(data)
        if not entry.get("gid"):
            raise AsanaError("create failed: no task gid returned")
        gid = str(entry["gid"])
        self._move_to_section(gid, section_gid)
        return self.get_item(gid)

    def update_item(self, item_id: str, fields: dict[str, Any]) -> WorkItem:
        gid = self._require_id(item_id)
        given = dict(fields)
        sprint = str(given.pop("iteration_id", "") or "").strip()
        if sprint:
            self.set_item_iteration(gid, sprint)
        state = str(given.pop("state", "") or "").strip()
        if state:
            self._move_to_section(gid, self._section_gid_for_name(state))
        payload = self._write_payload(given)
        if payload:
            self._request("PUT", f"/tasks/{quote(gid, safe='')}", None, payload)
        return self.get_item(gid)

    def delete_item(self, item_id: str) -> None:
        gid = self._require_id(item_id)
        self._request("DELETE", f"/tasks/{quote(gid, safe='')}")

    def list_states(self) -> list[str]:
        """List project sections in section order."""
        return [str(e.get("name", "")) for e in self._sections()]

    def close_item(self, item_id: str) -> WorkItem:
        gid = self._require_id(item_id)
        current = self.get_item(gid)
        if is_closed(current.state):
            return current
        done_gid = ""
        for entry in self._sections():
            name = str(entry.get("name", ""))
            if name and is_closed(name):
                done_gid = str(entry.get("gid", ""))
                break
        if not done_gid:
            raise AsanaError("no Done section on this project")
        self._move_to_section(gid, done_gid)
        return self.get_item(gid)

    def list_iterations(self) -> list[Iteration]:
        """Asana projects have no sprints here, so this is always empty."""
        return []

    def create_iteration(
        self, name: str, start_date: str = "", end_date: str = ""
    ) -> Iteration:
        raise AsanaError("Asana projects have no sprints; iterations are not supported.")

    def set_item_iteration(self, item_id: str, iteration_id: str) -> WorkItem:
        """Clearing is a no-op; setting raises since sprints are unsupported."""
        if not iteration_id.strip():
            return self.get_item(item_id)
        raise AsanaError("Asana projects have no sprints; iterations are not supported.")

    @staticmethod
    def _normalize_rel(rel: str) -> str:
        text = (rel or "").strip().lower().replace("_", "-")
        if text in _BLOCKS_ALIASES:
            return REL_BLOCKS
        if text in _BLOCKED_BY_ALIASES:
            return REL_BLOCKED_BY
        raise AsanaError(f"unknown link rel: {rel}")

    def add_link(self, source_id: str, target_id: str, rel: str) -> Link:
        kind = self._normalize_rel(rel)
        source = self._require_id(source_id)
        target = self._require_id(target_id)
        if kind == REL_BLOCKS:
            blocked, blocker = target, source
        else:
            blocked, blocker = source, target
        self._request(
            "POST",
            f"/tasks/{quote(blocked, safe='')}/addDependencies",
            None,
            {"dependencies": [blocker]},
        )
        return Link(source_id=source, target_id=target, rel=kind)

    def list_links(self, item_id: str) -> list[Link]:
        gid = self._require_id(item_id)
        out: list[Link] = []
        deps = self._request(
            "GET",
            f"/tasks/{quote(gid, safe='')}/dependencies",
            {"opt_fields": "gid"},
        )
        for entry in _data_list(deps):
            other = str(entry.get("gid", "") or "").strip()
            if other and other != gid:
                out.append(Link(source_id=gid, target_id=other, rel=REL_BLOCKED_BY))
        dents = self._request(
            "GET",
            f"/tasks/{quote(gid, safe='')}/dependents",
            {"opt_fields": "gid"},
        )
        for entry in _data_list(dents):
            other = str(entry.get("gid", "") or "").strip()
            if other and other != gid:
                out.append(Link(source_id=gid, target_id=other, rel=REL_BLOCKS))
        return out

    @staticmethod
    def _to_comment(item_id: str, data: dict[str, Any]) -> Comment | None:
        subtype = str(data.get("resource_subtype", "") or "").strip().lower()
        kind = str(data.get("type", "") or "").strip().lower()
        text = str(data.get("text", "") or "")
        if not text.strip():
            return None
        if subtype and "comment" not in subtype and kind != "comment":
            return None
        creator = data.get("created_by", {})
        name = ""
        if isinstance(creator, dict):
            name = str(creator.get("name", "") or "")
        return Comment(
            id=str(data.get("gid", "") or data.get("id", "")),
            item_id=item_id,
            text=text,
            author=name,
            created=str(data.get("created_at", "") or ""),
        )

    def list_comments(self, item_id: str) -> list[Comment]:
        gid = self._require_id(item_id)
        data = self._request(
            "GET",
            f"/tasks/{quote(gid, safe='')}/stories",
            {"opt_fields": "gid,text,created_by.name,created_at,resource_subtype,type"},
        )
        comments: list[Comment] = []
        for entry in _data_list(data):
            comment = self._to_comment(gid, entry)
            if comment is not None:
                comments.append(comment)
        comments.sort(key=lambda c: c.created)
        return comments

    def add_comment(self, item_id: str, text: str, author: str = "") -> Comment:
        gid = self._require_id(item_id)
        body = text.strip()
        if not body:
            raise AsanaError("comment text is required")
        data = self._request(
            "POST",
            f"/tasks/{quote(gid, safe='')}/stories",
            None,
            {"text": body},
        )
        entry = _data_dict(data)
        if not entry.get("gid") and not entry.get("id"):
            raise AsanaError(f"add comment failed for item: {gid}")
        comment = self._to_comment(gid, entry)
        if comment is None:
            comment = Comment(
                id=str(entry.get("gid", "") or entry.get("id", "")),
                item_id=gid,
                text=body,
                created="",
            )
        if author.strip() and not comment.author:
            comment.author = author.strip()
        return comment

    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        """List changed tasks; closed means is_closed on the section."""
        watermark = _parse_ts(since) if since else None
        stamp = watermark.isoformat() if watermark is not None else ""
        rows = self._project_tasks(modified_since=stamp)
        items = [self._to_item(r) for r in rows]
        items.sort(key=lambda item: item.changed)
        if watermark is None:
            return items
        out: list[WorkItem] = []
        for item in items:
            changed = _parse_ts(item.changed)
            if changed is not None and changed > watermark:
                out.append(item)
        return out


AsanaProjectAdapter = AsanaAdapter
