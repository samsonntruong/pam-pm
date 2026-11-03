"""ClickUp adapter over REST with a personal token.

Config resolves explicit, then env, then saved wizard values:
CLICKUP_TEAM, CLICKUP_LIST, CLICKUP_API_TOKEN. The token is never
accepted as an argument and never appears in errors or logs. Works
headless with a personal token against api.clickup.com.

Workspaces and teams hold spaces, spaces hold folders and lists,
and tasks live on one list. Task statuses map through canonical
states in list order. Tasks carry due dates, assignees,
priorities, and tags. Dependencies use the link task endpoint.
Change detection polls the list for updated tasks.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

API_BASE = "https://api.clickup.com/api/v2"

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

_PRIORITY_TO_NUM = {"urgent": 1, "high": 2, "normal": 3, "low": 4}
_NUM_TO_PRIORITY = {1: "urgent", 2: "high", 3: "normal", 4: "low"}

_DONE_TYPES = {"done", "closed", "complete", "completed"}

_MS_PER_HOUR = 3600000


def redact(text: object, *secrets: str) -> str:
    """Redact token material from log text."""
    out = str(text)
    for val in secrets:
        if val and len(val) >= 4 and val in out:
            out = out.replace(val, "[REDACTED]")
    return out


class ClickUpError(RuntimeError):
    """ClickUp call failure with the token already redacted."""


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        try:
            stamp = int(text) / 1000.0
        except ValueError:
            return None
        return datetime.fromtimestamp(stamp, tz=UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _iso_from_ms(value: object) -> str:
    text = str(value or "").strip()
    if not text or not text.lstrip("-").isdigit():
        return ""
    try:
        stamp = int(text) / 1000.0
    except ValueError:
        return ""
    return datetime.fromtimestamp(stamp, tz=UTC).isoformat().replace("+00:00", "Z")


def _ms_from_due_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        day = datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return ""
    return str(int(day.timestamp() * 1000))


def _due_date_from_ms(value: object) -> str:
    iso = _iso_from_ms(value)
    return iso[:10] if iso else ""


def _priority_name(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        raw = str(value.get("priority", "") or "").strip().lower()
        if raw in _PRIORITY_TO_NUM:
            return raw
        try:
            num = int(str(value.get("id", "") or ""))
        except (TypeError, ValueError):
            return raw
        return _NUM_TO_PRIORITY.get(num, raw)
    text = str(value).strip().lower()
    if text in _PRIORITY_TO_NUM:
        return text
    try:
        return _NUM_TO_PRIORITY.get(int(text), "")
    except (TypeError, ValueError):
        return ""


def _priority_num(value: object) -> int | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in _PRIORITY_TO_NUM:
        return _PRIORITY_TO_NUM[text]
    try:
        num = int(text)
    except (TypeError, ValueError):
        return None
    return num if num in _NUM_TO_PRIORITY else None


def _is_permission_error(exc: Exception) -> bool:
    low = str(exc).lower()
    return any(
        s in low
        for s in ("401", "403", "unauthor", "forbidden", "permission", "access denied")
    )


def _permission_message() -> str:
    return (
        "ClickUp token lacks permission to read or create Spaces and Lists. "
        "In ClickUp, open Settings > Apps > ClickUp API and enable access "
        "for this token, then try again."
    )


class ClickUpAdapter(PMAdapter):
    """PM adapter backed by the ClickUp REST API."""

    supports_dependency_links: bool = True
    supports_iterations: bool = False
    supports_comments: bool = True

    def __init__(
        self,
        team: str | None = None,
        list_ref: str | None = None,
        timeout: int = 30,
        transport: httpx.BaseTransport | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        saved_team = ""
        saved_list = ""
        saved_token = ""
        try:
            from pam_pm.core import secrets as secrets_mod

            effective = secrets_mod.effective_clickup_config(data_dir)
            saved_team = str(effective.get("team", "") or "")
            saved_list = str(effective.get("list", "") or "")
            saved_token = str(effective.get("api_token", "") or "")
        except Exception:  # noqa: BLE001
            saved_team = env_first("CLICKUP_TEAM", "CLICKUP_TEAM_ID")
            saved_list = env_first("CLICKUP_LIST", "CLICKUP_LIST_ID")
            saved_token = os.environ.get("CLICKUP_API_TOKEN", "")
        want_team = (team or "").strip() or saved_team.strip()
        want_list = (list_ref or "").strip() or saved_list.strip()
        token = saved_token.strip()
        if not token:
            raise ClickUpError(
                "CLICKUP_API_TOKEN is missing; save it in Setup or set CLICKUP_API_TOKEN."
            )
        self._team_ref = want_team
        self._list_ref = want_list
        self._token = token
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        self._team_id: str | None = None
        self._list_id: str | None = None
        self._statuses: list[dict[str, Any]] | None = None

    def close(self) -> None:
        self._client.close()

    def _clean(self, text: object) -> str:
        return redact(text, self._token)

    def _request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = API_BASE + path
        try:
            resp = self._client.request(method, url, json=payload, params=params)
        except httpx.HTTPError as exc:
            raise ClickUpError(self._clean(f"request failed: {exc}")) from exc
        if resp.status_code == 204 or not resp.text.strip():
            return {}
        if resp.status_code >= 400:
            detail = resp.text[:500]
            raise ClickUpError(
                self._clean(f"{method} {path} status={resp.status_code}: {detail}")
            )
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text[:2000]}

    def _teams(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/team")
        rows = data.get("teams", []) if isinstance(data, dict) else []
        return [r for r in rows if isinstance(r, dict)]

    def _wanted_team_ids(self) -> list[str]:
        teams = self._teams()
        if not self._team_ref.strip():
            return [str(t.get("id", "")) for t in teams if t.get("id")]
        want = self._team_ref.strip().lower()
        out: list[str] = []
        for entry in teams:
            for field in ("id", "name"):
                if str(entry.get(field, "") or "").strip().lower() == want:
                    out.append(str(entry.get("id", "")))
                    break
        return out

    def _spaces_of(self, team_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/team/{team_id}/space", params={"archived": "false"})
        rows = data.get("spaces", []) if isinstance(data, dict) else []
        return [r for r in rows if isinstance(r, dict)]

    def _lists_of_space(self, space_id: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        data = self._request("GET", f"/space/{space_id}/list", params={"archived": "false"})
        rows = data.get("lists", []) if isinstance(data, dict) else []
        found.extend(r for r in rows if isinstance(r, dict))
        folders = self._request("GET", f"/space/{space_id}/folder", params={"archived": "false"})
        folder_rows = folders.get("folders", []) if isinstance(folders, dict) else []
        for folder in folder_rows:
            if not isinstance(folder, dict) or not folder.get("id"):
                continue
            inner = self._request("GET", f"/folder/{folder['id']}/list")
            inner_rows = inner.get("lists", []) if isinstance(inner, dict) else []
            found.extend(r for r in inner_rows if isinstance(r, dict))
        return found

    def _list_id_for_ref(self) -> str:
        if self._list_id:
            return self._list_id
        if not self._list_ref.strip():
            project = self._team_ref.strip()
            resolved = self.resolve_list(project)
            self._list_id = resolved
            return resolved
        want = self._list_ref.strip().lower()
        team_ids = self._wanted_team_ids()
        if not team_ids:
            raise ClickUpError(f"unknown ClickUp team: {self._team_ref or self._list_ref}")
        for team_id in team_ids:
            for space in self._spaces_of(team_id):
                if not space.get("id"):
                    continue
                for entry in self._lists_of_space(str(space["id"])):
                    for field in ("id", "name"):
                        if str(entry.get(field, "") or "").strip().lower() == want:
                            self._team_id = team_id
                            self._list_id = str(entry.get("id", ""))
                            statuses = entry.get("statuses", [])
                            if isinstance(statuses, list) and statuses:
                                self._statuses = [s for s in statuses if isinstance(s, dict)]
                            return self._list_id
        raise ClickUpError(f"unknown ClickUp list: {self._list_ref}")

    def resolve_list(self, project_name: str, dry_run: bool = False) -> str:
        """Find or create a ClickUp list named project_name; return its id."""
        name = (project_name or "").strip()
        if not name:
            raise ClickUpError("project name is required")
        want = name.lower()
        try:
            team_ids = self._wanted_team_ids()
        except ClickUpError as exc:
            if _is_permission_error(exc):
                raise ClickUpError(_permission_message()) from exc
            raise
        if not team_ids:
            raise ClickUpError(f"unknown ClickUp team: {self._team_ref or name}")
        for team_id in team_ids:
            try:
                spaces = self._spaces_of(team_id)
            except ClickUpError as exc:
                if _is_permission_error(exc):
                    raise ClickUpError(_permission_message()) from exc
                raise
            for space in spaces:
                sid = str(space.get("id", "") or "").strip()
                if not sid:
                    continue
                try:
                    lists = self._lists_of_space(sid)
                except ClickUpError as exc:
                    if _is_permission_error(exc):
                        raise ClickUpError(_permission_message()) from exc
                    raise
                for entry in lists:
                    if str(entry.get("name", "") or "").strip().lower() == want:
                        lid = str(entry.get("id", "") or "").strip()
                        if lid:
                            return lid
        if dry_run:
            return ""
        target_team = team_ids[0]
        try:
            space_data = self._request(
                "POST", f"/team/{target_team}/space", {"name": name}
            )
        except ClickUpError as exc:
            if _is_permission_error(exc):
                raise ClickUpError(_permission_message()) from exc
            raise
        space_id = ""
        if isinstance(space_data, dict):
            space_id = str(space_data.get("id", "") or "").strip()
            if not space_id and isinstance(space_data.get("space"), dict):
                space_id = str(space_data["space"].get("id", "") or "").strip()
        if not space_id:
            raise ClickUpError(f"ClickUp Space create failed for: {name}")
        try:
            list_data = self._request(
                "POST", f"/space/{space_id}/list", {"name": name}
            )
        except ClickUpError as exc:
            if _is_permission_error(exc):
                raise ClickUpError(_permission_message()) from exc
            raise
        list_id = ""
        if isinstance(list_data, dict):
            list_id = str(list_data.get("id", "") or "").strip()
            if not list_id and isinstance(list_data.get("list"), dict):
                list_id = str(list_data["list"].get("id", "") or "").strip()
        if not list_id:
            raise ClickUpError(f"ClickUp List create failed for: {name}")
        return list_id

    def _list_statuses(self) -> list[dict[str, Any]]:
        if self._statuses is not None:
            return self._statuses
        list_id = self._list_id_for_ref()
        data = self._request("GET", f"/list/{list_id}")
        rows = data.get("statuses", []) if isinstance(data, dict) else []
        out = [r for r in rows if isinstance(r, dict)]
        try:
            out.sort(key=lambda r: int(r.get("orderindex", 0) or 0))
        except (TypeError, ValueError):
            pass
        self._statuses = out
        return out

    def _status_names(self) -> list[str]:
        return [str(s.get("status", "")) for s in self._list_statuses() if s.get("status")]

    def _known_status(self, name: str) -> str:
        want = (name or "").strip().lower()
        for entry in self._list_statuses():
            if str(entry.get("status", "")).strip().lower() == want:
                return str(entry.get("status", ""))
        raise ClickUpError(f"unknown ClickUp status: {name}")

    def _closing_status(self) -> str:
        fallback = ""
        for entry in self._list_statuses():
            name = str(entry.get("status", ""))
            stype = str(entry.get("type", "")).strip().lower()
            if is_closed(name):
                return name
            if stype in _DONE_TYPES and not fallback:
                fallback = name
        if fallback:
            return fallback
        raise ClickUpError("no completed status on this list")

    def _assignee_id(self, owner: str) -> str:
        name = (owner or "").strip().lower()
        if not name:
            return ""
        try:
            data = self._request("GET", f"/list/{self._list_id_for_ref()}/member")
        except ClickUpError:
            return ""
        rows = data.get("members", []) if isinstance(data, dict) else []
        for entry in rows if isinstance(rows, list) else []:
            user = entry.get("user", entry) if isinstance(entry, dict) else {}
            if not isinstance(user, dict):
                continue
            for field in ("username", "email"):
                if str(user.get(field, "") or "").strip().lower() == name:
                    return str(user.get("id", ""))
        return ""

    @staticmethod
    def _split_tags(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(t).strip() for t in value if str(t).strip()]
        return [
            part
            for part in (p.strip() for p in str(value or "").replace(",", ";").split(";"))
            if part
        ]

    def _task_input(self, fields: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, val in fields.items():
            if key == "title":
                out["name"] = val
            elif key == "description":
                out["description"] = str(val or "")
            elif key == "state":
                name = str(val or "").strip()
                if name:
                    out["status"] = self._known_status(name)
            elif key == "tags":
                out["tags"] = self._split_tags(val)
            elif key == "due_date":
                text = str(val or "").strip()
                out["due_date"] = int(_ms_from_due_date(text)) if text else None
            elif key == "owner" or key == "assigned_to":
                names = self._split_tags(val)
                ids = [self._assignee_id(n) for n in names]
                found = [i for i in ids if i]
                if found:
                    out["assignees"] = found
                elif not names:
                    out["assignees"] = []
            elif key == "estimate":
                try:
                    out["time_estimate"] = int(float(str(val)) * _MS_PER_HOUR)
                except (TypeError, ValueError):
                    pass
            elif key == "priority":
                num = _priority_num(val)
                if num is not None:
                    out["priority"] = num
                elif str(val or "").strip() == "":
                    out["priority"] = None
        return out

    @classmethod
    def _to_item(cls, data: dict[str, Any]) -> WorkItem:
        status = data.get("status", {})
        state = ""
        status_type = ""
        if isinstance(status, dict):
            state = str(status.get("status", "") or "")
            status_type = str(status.get("type", "") or "").strip().lower()
        tag_rows = data.get("tags", [])
        tags: list[str] = []
        if isinstance(tag_rows, list):
            for entry in tag_rows:
                if isinstance(entry, dict) and entry.get("name"):
                    tags.append(str(entry["name"]))
                elif isinstance(entry, str) and entry.strip():
                    tags.append(entry.strip())
        assignee_rows = data.get("assignees", [])
        assignees: list[str] = []
        if isinstance(assignee_rows, list):
            for entry in assignee_rows:
                if isinstance(entry, dict):
                    name = str(entry.get("username", "") or entry.get("email", "") or "")
                    if name.strip():
                        assignees.append(name.strip())
        owner = assignees[0] if assignees else ""
        estimate = None
        raw_estimate = data.get("time_estimate")
        if raw_estimate not in (None, ""):
            try:
                estimate = round(float(str(raw_estimate)) / _MS_PER_HOUR, 2)
            except (TypeError, ValueError):
                estimate = None
        task_list = data.get("list", {})
        list_id = ""
        list_name = ""
        if isinstance(task_list, dict):
            list_id = str(task_list.get("id", "") or "")
            list_name = str(task_list.get("name", "") or "")
        closed = is_closed(state) or status_type in _DONE_TYPES
        item_fields: dict[str, Any] = {
            "title": str(data.get("name", "")),
            "state": state,
            "description": str(data.get("text_content", "") or data.get("description", "") or ""),
            "tags": tags,
            "due_date": _due_date_from_ms(data.get("due_date")),
            "owner": owner,
            "assigned_to": owner,
            "assignees": assignees,
            "estimate": estimate,
            "remaining": None,
            "priority": _priority_name(data.get("priority")),
            "status_type": status_type,
            "iteration_id": "",
            "list_id": list_id,
            "list_name": list_name,
            "url": str(data.get("url", "") or ""),
            "is_closed": closed,
        }
        return WorkItem(
            id=str(data.get("id", "")),
            title=str(data.get("name", "")),
            state=state,
            changed=_iso_from_ms(data.get("date_updated", "")),
            fields=item_fields,
        )

    def _require_id(self, item_id: str) -> str:
        cleaned = (item_id or "").strip()
        if not cleaned:
            raise ClickUpError("task id is required")
        return cleaned

    def list_items(self, limit: int = 50) -> list[WorkItem]:
        list_id = self._list_id_for_ref()
        data = self._request(
            "GET",
            f"/list/{list_id}/task",
            params={"order_by": "updated", "reverse": "true", "subtasks": "true"},
        )
        rows = data.get("tasks", []) if isinstance(data, dict) else []
        items = [self._to_item(r) for r in rows if isinstance(r, dict)]
        items.sort(key=lambda item: item.changed, reverse=True)
        return items[:limit]

    def get_item(self, item_id: str) -> WorkItem:
        task_id = self._require_id(item_id)
        data = self._request("GET", f"/task/{task_id}")
        if not isinstance(data, dict) or not data.get("id"):
            raise ClickUpError(f"unknown item: {task_id}")
        return self._to_item(data)

    def create_item(self, title: str, fields: dict[str, Any] | None = None) -> WorkItem:
        given = dict(fields or {})
        given.pop("iteration_id", None)
        payload = {"name": title, **self._task_input(given)}
        data = self._request("POST", f"/list/{self._list_id_for_ref()}/task", payload)
        if not isinstance(data, dict) or not data.get("id"):
            raise ClickUpError("create failed: no task id returned")
        return self.get_item(str(data["id"]))

    def update_item(self, item_id: str, fields: dict[str, Any]) -> WorkItem:
        task_id = self._require_id(item_id)
        given = dict(fields)
        sprint = str(given.pop("iteration_id", "") or "").strip()
        if sprint:
            self.set_item_iteration(task_id, sprint)
        payload = self._task_input(given)
        if payload:
            self._request("PUT", f"/task/{task_id}", payload)
        return self.get_item(task_id)

    def delete_item(self, item_id: str) -> None:
        task_id = self._require_id(item_id)
        self._request("DELETE", f"/task/{task_id}")

    def list_states(self) -> list[str]:
        """List task statuses in list order."""
        return self._status_names()

    def close_item(self, item_id: str) -> WorkItem:
        task_id = self._require_id(item_id)
        current = self.get_item(task_id)
        if bool(current.fields.get("is_closed", False)) or is_closed(current.state):
            return current
        self._request("PUT", f"/task/{task_id}", {"status": self._closing_status()})
        return self.get_item(task_id)

    def list_iterations(self) -> list[Iteration]:
        """ClickUp lists have no sprints here, so this is always empty."""
        return []

    def create_iteration(
        self, name: str, start_date: str = "", end_date: str = ""
    ) -> Iteration:
        raise ClickUpError("ClickUp lists have no sprints; iterations are not supported.")

    def set_item_iteration(self, item_id: str, iteration_id: str) -> WorkItem:
        """Clearing is a no-op; setting raises since sprints are unsupported."""
        if not iteration_id.strip():
            return self.get_item(item_id)
        raise ClickUpError("ClickUp lists have no sprints; iterations are not supported.")

    @staticmethod
    def _normalize_rel(rel: str) -> str:
        text = (rel or "").strip().lower().replace("_", "-")
        if text in _BLOCKS_ALIASES:
            return REL_BLOCKS
        if text in _BLOCKED_BY_ALIASES:
            return REL_BLOCKED_BY
        raise ClickUpError(f"unknown link rel: {rel}")

    def add_link(self, source_id: str, target_id: str, rel: str) -> Link:
        kind = self._normalize_rel(rel)
        source = self._require_id(source_id)
        target = self._require_id(target_id)
        if kind == REL_BLOCKS:
            blocked, blocker = target, source
        else:
            blocked, blocker = source, target
        self._request("POST", f"/task/{blocked}/link/{blocker}")
        return Link(source_id=source, target_id=target, rel=kind)

    def list_links(self, item_id: str) -> list[Link]:
        task_id = self._require_id(item_id)
        data = self._request("GET", f"/task/{task_id}")
        if not isinstance(data, dict):
            return []
        out: list[Link] = []

        def _ids(rows: Any) -> list[str]:
            found: list[str] = []
            for entry in rows if isinstance(rows, list) else []:
                if isinstance(entry, dict):
                    for field in ("id", "task_id", "depends_on", "linked_id"):
                        if entry.get(field):
                            found.append(str(entry[field]))
                            break
                elif isinstance(entry, str) and entry.strip():
                    found.append(entry.strip())
            return found

        for other in _ids(data.get("dependents", [])):
            if other and other != task_id:
                out.append(Link(source_id=task_id, target_id=other, rel=REL_BLOCKS))
        for other in _ids(data.get("dependencies", [])):
            if other and other != task_id:
                out.append(Link(source_id=task_id, target_id=other, rel=REL_BLOCKED_BY))
        return out

    @staticmethod
    def _to_comment(item_id: str, data: dict[str, Any]) -> Comment:
        user = data.get("user", {})
        name = ""
        if isinstance(user, dict):
            name = str(user.get("username", "") or user.get("email", "") or "")
        return Comment(
            id=str(data.get("id", "")),
            item_id=item_id,
            text=str(data.get("comment_text", "") or data.get("text_content", "") or ""),
            author=name,
            created=_iso_from_ms(data.get("date", "")),
        )

    def list_comments(self, item_id: str) -> list[Comment]:
        task_id = self._require_id(item_id)
        data = self._request("GET", f"/task/{task_id}/comment")
        rows = data.get("comments", []) if isinstance(data, dict) else []
        comments = [self._to_comment(task_id, r) for r in rows if isinstance(r, dict)]
        comments.sort(key=lambda c: c.created)
        return comments

    def add_comment(self, item_id: str, text: str, author: str = "") -> Comment:
        task_id = self._require_id(item_id)
        body = text.strip()
        if not body:
            raise ClickUpError("comment text is required")
        data = self._request("POST", f"/task/{task_id}/comment", {"comment_text": body})
        if not isinstance(data, dict) or not data.get("id"):
            raise ClickUpError(f"add comment failed for item: {task_id}")
        comment = self._to_comment(task_id, data)
        if author.strip() and not comment.author:
            comment.author = author.strip()
        return comment

    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        """List changed tasks; closed means is_closed on the status."""
        list_id = self._list_id_for_ref()
        watermark = _parse_ts(since) if since else None
        params: dict[str, Any] = {"order_by": "updated", "reverse": "false", "subtasks": "true"}
        if watermark is not None:
            params["date_updated_gt"] = str(int(watermark.timestamp() * 1000))
        data = self._request("GET", f"/list/{list_id}/task", params=params)
        rows = data.get("tasks", []) if isinstance(data, dict) else []
        items = [self._to_item(r) for r in rows if isinstance(r, dict)]
        items.sort(key=lambda item: item.changed)
        if watermark is None:
            return items
        out: list[WorkItem] = []
        for item in items:
            changed = _parse_ts(item.changed)
            if changed is not None and changed > watermark:
                out.append(item)
        return out


def resolve_list(
    project_name: str,
    team: str | None = None,
    transport: httpx.BaseTransport | None = None,
    data_dir: str | Path | None = None,
    dry_run: bool = False,
    timeout: int = 30,
) -> str:
    """Find or create a list named project_name; return its id."""
    adapter = ClickUpAdapter(
        team=team, list_ref="", transport=transport, data_dir=data_dir,
        timeout=timeout,
    )
    try:
        return adapter.resolve_list(project_name, dry_run=dry_run)
    finally:
        adapter.close()


ClickUpListAdapter = ClickUpAdapter
