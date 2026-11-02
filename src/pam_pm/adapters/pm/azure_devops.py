"""Azure DevOps adapter over REST 7.1 with httpx.

Org, project, team, and PAT resolve explicit, then env, then saved
wizard values. The PAT is never accepted as an argument and never
appears in errors or logs.
"""

from __future__ import annotations

import base64
import logging
import os
import re
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

API_VERSION = "7.1"
COMMENTS_VERSION = "7.1-preview.3"

STATE_MAP = {
    "Agile": {"open": "New", "active": "Active", "done": "Closed"},
    "Scrum": {"open": "To Do", "active": "In Progress", "done": "Done"},
}

DEPENDENCY_FORWARD = "System.LinkTypes.Dependency-Forward"
DEPENDENCY_REVERSE = "System.LinkTypes.Dependency-Reverse"
HIERARCHY_FORWARD = "System.LinkTypes.Hierarchy-Forward"
HIERARCHY_REVERSE = "System.LinkTypes.Hierarchy-Reverse"

_REL_ALIASES = {
    "Dependency-Forward": DEPENDENCY_FORWARD,
    "Dependency-Reverse": DEPENDENCY_REVERSE,
    DEPENDENCY_FORWARD: DEPENDENCY_FORWARD,
    DEPENDENCY_REVERSE: DEPENDENCY_REVERSE,
    "Hierarchy-Forward": HIERARCHY_FORWARD,
    "Hierarchy-Reverse": HIERARCHY_REVERSE,
    HIERARCHY_FORWARD: HIERARCHY_FORWARD,
    HIERARCHY_REVERSE: HIERARCHY_REVERSE,
}

FIELD_MAP = {
    "title": "System.Title",
    "state": "System.State",
    "description": "System.Description",
    "tags": "System.Tags",
    "assigned_to": "System.AssignedTo",
    "owner": "System.AssignedTo",
    "remaining": "Microsoft.VSTS.Scheduling.RemainingWork",
    "estimate": "Microsoft.VSTS.Scheduling.OriginalEstimate",
    "due_date": "Microsoft.VSTS.Scheduling.DueDate",
}

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+")


def redact(text: object, *secrets: str) -> str:
    """Redact bearer values and PAT material from log text."""
    out = _BEARER_RE.sub("Bearer [REDACTED]", str(text))
    for val in secrets:
        if val and len(val) >= 4 and val in out:
            out = out.replace(val, "[REDACTED]")
    return out


logger = logging.getLogger(__name__)

_STATE_KEYS = {"state", "System.State", "/fields/System.State"}
_ASSIGNEE_KEYS = {
    "assigned_to",
    "owner",
    "System.AssignedTo",
    "/fields/System.AssignedTo",
}
_CANONICAL_STATES = {"open", "active", "done"}


class ADOError(RuntimeError):
    """ADO call failure with the PAT already redacted."""

    def __init__(
        self,
        message: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.applied: list[str] = []
        self.rejected: dict[str, str] = {}


class ADOAdapter(PMAdapter):
    """PM adapter backed by the ADO work item REST API."""

    supports_dependency_links: bool = True
    supports_iterations: bool = True
    supports_comments: bool = True

    def __init__(
        self,
        org_url: str | None = None,
        project: str | None = None,
        team: str | None = None,
        timeout: int = 30,
        transport: httpx.BaseTransport | None = None,
        process: str | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        saved_org = ""
        saved_proj = ""
        saved_team = ""
        saved_pat = ""
        try:
            from pam_pm.core import secrets as secrets_mod

            effective = secrets_mod.effective_ado_config(data_dir)
            saved_org = str(effective.get("org_url", "") or "")
            saved_proj = str(effective.get("project", "") or "")
            saved_team = str(effective.get("team", "") or "")
            saved_pat = str(effective.get("pat", "") or "")
        except Exception:  # noqa: BLE001
            saved_org = os.environ.get("ADO_ORG_URL", "")
            saved_proj = os.environ.get("ADO_PROJECT", "")
            saved_team = os.environ.get("ADO_TEAM", "")
            saved_pat = os.environ.get("ADO_PAT", "")
        org = (org_url or "").strip().rstrip("/") or saved_org.strip().rstrip("/")
        proj = (project or "").strip() or saved_proj.strip()
        pat = saved_pat.strip()
        if not org or not proj:
            raise ADOError("ADO_ORG_URL and ADO_PROJECT are required.")
        if not pat:
            raise ADOError("ADO_PAT is missing; save it in Setup or set ADO_PAT.")
        self._org = org
        self._project = proj
        self._team = (team or "").strip() or saved_team.strip()
        self._pat = pat
        basic = base64.b64encode((":" + pat).encode("utf-8")).decode("ascii")
        self._basic = "Basic " + basic
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": self._basic,
                "Accept": "application/json",
            },
        )
        cleaned = (process or "").strip()
        self._process: str | None = cleaned if cleaned in STATE_MAP else None
        self._warnings: list[str] = []

    def close(self) -> None:
        self._client.close()

    def detect_process(self, project: str | None = None) -> str:
        """Detect Agile or Scrum for a project, defaulting to Agile.

        Uses project capabilities first, then a Task states probe.
        """
        proj = (project or self._project).strip() or self._project
        if proj == self._project and self._process in STATE_MAP:
            return str(self._process)
        found = self._process_from_properties(proj)
        if found is None:
            found = self._process_from_probe(proj)
        if found is None:
            found = "Agile"
        if proj == self._project:
            self._process = found
        return found

    def _process_from_properties(self, proj: str) -> str | None:
        url = f"{self._org}/_apis/projects/{quote(proj, safe='')}"
        url += f"?api-version={API_VERSION}"
        try:
            data = self._request("GET", url)
        except ADOError:
            return None
        if not isinstance(data, dict):
            return None
        caps = data.get("capabilities")
        if not isinstance(caps, dict):
            return None
        template = caps.get("processTemplate")
        if not isinstance(template, dict):
            return None
        name = str(template.get("templateName", "")).strip()
        for key in STATE_MAP:
            if name.lower() == key.lower():
                return key
        return None

    def _process_from_probe(self, proj: str) -> str | None:
        prefix = f"{self._org}/{quote(proj, safe='')}"
        url = prefix + f"/_apis/wit/workitemtypes/Task/states?api-version={API_VERSION}"
        try:
            data = self._request("GET", url)
        except ADOError:
            return None
        values = data.get("value", []) if isinstance(data, dict) else []
        names = set()
        for entry in values if isinstance(values, list) else []:
            if isinstance(entry, dict):
                names.add(str(entry.get("name", "")).strip().lower())
        if "new" in names:
            return "Agile"
        if "to do" in names:
            return "Scrum"
        return None

    def _mapped_state(self, kind: str) -> str:
        if self._process not in STATE_MAP:
            self._process = self.detect_process()
        mapping = STATE_MAP.get(str(self._process), STATE_MAP["Agile"])
        return str(mapping.get(kind, mapping["open"]))

    def pop_warnings(self) -> list[str]:
        """Return and clear pending adapter warnings for Activity."""
        out = list(self._warnings)
        self._warnings.clear()
        return out

    def _identities_base(self) -> str:
        org = self._org.rstrip("/")
        if "dev.azure.com" in org:
            return org.replace("dev.azure.com", "vssps.dev.azure.com")
        return org

    @staticmethod
    def _looks_like_descriptor(value: str) -> bool:
        text = value.strip()
        if not text:
            return True
        return (
            ";" in text
            or "\\" in text
            or text.startswith(("aad.", "Microsoft."))
        )

    def _resolve_identity_descriptor(self, name: str) -> str | None:
        """Resolve a display name to an ADO identity descriptor."""
        query = name.strip()
        if not query or self._looks_like_descriptor(query):
            return None
        url = (
            f"{self._identities_base()}/_apis/identities"
            f"?searchFilter=General&query={quote(query)}"
            f"&api-version={API_VERSION}"
        )
        try:
            data = self._request("GET", url)
        except ADOError:
            return None
        values = data.get("value", []) if isinstance(data, dict) else []
        if not isinstance(values, list) or not values:
            return None
        lowered = query.lower()
        for entry in values:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("displayName", "")).strip().lower() == lowered:
                desc = str(entry.get("descriptor", "")).strip()
                if desc:
                    return desc
        for entry in values:
            if isinstance(entry, dict):
                desc = str(entry.get("descriptor", "")).strip()
                if desc:
                    return desc
        return None

    def _resolve_assignee(self, raw: object) -> tuple[object, str | None]:
        if raw is None:
            return raw, None
        text = str(raw).strip()
        if not text or self._looks_like_descriptor(text):
            return raw, None
        try:
            descriptor = self._resolve_identity_descriptor(text)
        except Exception:  # noqa: BLE001
            descriptor = None
        if descriptor:
            return descriptor, None
        msg = (
            f"assignee '{text}' could not be resolved; "
            "item may be unassigned"
        )
        logger.warning("%s", self._clean(msg))
        self._warnings.append(msg)
        try:
            from pam_pm.web.app import bus as activity_bus

            activity_bus.publish(
                {
                    "operation": "warning",
                    "item_id": "",
                    "evidence_quote": msg,
                    "status": "applied",
                    "detail": msg,
                }
            )
        except Exception:  # noqa: BLE001, S110
            pass
        return raw, msg

    def _normalize_state_value(self, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in _CANONICAL_STATES:
            return self._mapped_state(value.strip().lower())
        return value

    def _normalize_fields(
        self, fields: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        out = dict(fields)
        warnings: list[str] = []
        for key in list(out.keys()):
            if key in _STATE_KEYS:
                out[key] = self._normalize_state_value(out[key])
            elif key in _ASSIGNEE_KEYS:
                resolved, warn = self._resolve_assignee(out[key])
                out[key] = resolved
                if warn:
                    warnings.append(warn)
            elif key in ("tags", "System.Tags", "/fields/System.Tags"):
                val = out[key]
                if isinstance(val, (list, tuple)):
                    out[key] = "; ".join(
                        str(v).strip() for v in val if str(v).strip()
                    )
        return out, warnings

    def close_item(self, item_id: str) -> WorkItem:
        """Move one item to its mapped closed state."""
        return self.update_item(item_id, {"state": self._mapped_state("done")})

    def activate_item(self, item_id: str) -> WorkItem:
        """Move one item to its mapped active state."""
        return self.update_item(item_id, {"state": self._mapped_state("active")})

    def list_states(self) -> list[str]:
        """List the canonical open, active, and done states in order."""
        if self._process not in STATE_MAP:
            self._process = self.detect_process()
        mapping = STATE_MAP.get(str(self._process), STATE_MAP["Agile"])
        return [
            str(mapping.get("open", "")),
            str(mapping.get("active", "")),
            str(mapping.get("done", "")),
        ]

    def delete_item(self, item_id: str) -> None:
        """Delete one item (ADO moves it to the recycle bin)."""
        url = self._url(
            f"/_apis/wit/workitems/{item_id}?api-version={API_VERSION}"
        )
        self._request("DELETE", url)

    def _clean(self, text: object) -> str:
        return redact(text, self._pat, self._basic)

    def _url(self, path: str) -> str:
        return f"{self._org}/{quote(self._project, safe='')}{path}"

    def _request(
        self,
        method: str,
        url: str,
        payload: Any = None,
        content_type: str | None = None,
    ) -> Any:
        headers = dict(self._client.headers)
        if content_type:
            headers["Content-Type"] = content_type
        try:
            resp = self._client.request(method, url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ADOError(self._clean(f"request failed: {exc}")) from exc
        if resp.status_code >= 400:
            detail = resp.text[:500]
            raise ADOError(
                self._clean(f"{method} {url} status={resp.status_code}: {detail}"),
                status_code=resp.status_code,
            )
        if not resp.text.strip():
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text[:2000]}

    @staticmethod
    def _to_item(data: dict[str, Any]) -> WorkItem:
        fields = data.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}
        return WorkItem(
            id=str(data.get("id", "")),
            title=str(fields.get("System.Title", "")),
            state=str(fields.get("System.State", "")),
            changed=str(fields.get("System.ChangedDate", "")),
            fields=dict(fields),
        )

    def _wiql(self, query: str) -> list[str]:
        url = self._url(f"/_apis/wit/wiql?api-version={API_VERSION}")
        data = self._request("POST", url, {"query": query})
        refs = data.get("workItems", []) if isinstance(data, dict) else []
        ids: list[str] = []
        for ref in refs:
            if isinstance(ref, dict) and ref.get("id") is not None:
                ids.append(str(ref["id"]))
        return ids

    def _batch_get(self, ids: list[str]) -> list[WorkItem]:
        items: list[WorkItem] = []
        for start in range(0, len(ids), 200):
            chunk = ids[start : start + 200]
            url = (
                self._url("/_apis/wit/workitems")
                + f"?ids={','.join(chunk)}&api-version={API_VERSION}"
            )
            data = self._request("GET", url)
            values = data.get("value", []) if isinstance(data, dict) else []
            by_id = {
                str(row.get("id")): row
                for row in values
                if isinstance(row, dict)
            }
            for want in chunk:
                if want in by_id:
                    items.append(self._to_item(by_id[want]))
        return items

    def list_items(self, limit: int = 50) -> list[WorkItem]:
        query = (
            "SELECT [System.Id] FROM WorkItems"
            " ORDER BY [System.ChangedDate] DESC"
        )
        return self._batch_get(self._wiql(query)[:limit])

    def get_item(self, item_id: str) -> WorkItem:
        url = self._url(
            f"/_apis/wit/workitems/{item_id}"
            f"?$expand=relations&api-version={API_VERSION}"
        )
        data = self._request("GET", url)
        if not isinstance(data, dict) or not data.get("id"):
            raise ADOError(f"unknown item: {item_id}")
        item = self._to_item(data)
        relations = data.get("relations")
        if isinstance(relations, list):
            item.fields["relations"] = relations
        return item

    @staticmethod
    def _field_path(key: str) -> str:
        if key.startswith("/fields/"):
            return key
        if key in FIELD_MAP:
            return "/fields/" + FIELD_MAP[key]
        return "/fields/" + key

    @classmethod
    def _patch_ops(cls, fields: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"op": "add", "path": cls._field_path(key), "value": val}
            for key, val in fields.items()
        ]

    def _attach_write_meta(
        self,
        item: WorkItem,
        warnings: list[str],
        applied: list[str],
        rejected: dict[str, str],
    ) -> WorkItem:
        if warnings:
            item.fields["warning"] = warnings[0]
            if len(warnings) > 1:
                item.fields["warnings"] = list(warnings)
        item.fields["applied_fields"] = list(applied)
        item.fields["rejected_fields"] = dict(rejected)
        return item

    def _update_fields_one_by_one(
        self,
        item_id: str,
        sendable: dict[str, Any],
        warnings: list[str],
        url: str,
    ) -> WorkItem:
        """Retry a rejected bulk PATCH one field at a time.

        Keeps every field ADO accepts and records ADO's reason for
        each rejected field. One bad field (unknown on this process,
        invalid state, wrong op) no longer fails the rest. Each field
        tries add first, then replace. Raises only when zero fields
        persisted, carrying applied and rejected on the error.
        """
        applied: list[str] = []
        rejected: dict[str, str] = {}
        for key, val in sendable.items():
            path = self._field_path(key)
            try:
                self._request(
                    "PATCH",
                    url,
                    [{"op": "add", "path": path, "value": val}],
                    "application/json-patch+json",
                )
                applied.append(key)
                continue
            except ADOError as first:
                if first.status_code != 400:
                    first.applied = list(applied)
                    first.rejected = dict(rejected)
                    raise
                try:
                    self._request(
                        "PATCH",
                        url,
                        [{"op": "replace", "path": path, "value": val}],
                        "application/json-patch+json",
                    )
                    applied.append(key)
                except ADOError as second:
                    if second.status_code != 400:
                        second.applied = list(applied)
                        second.rejected = dict(rejected)
                        raise
                    rejected[key] = str(first)
        if not applied:
            detail = "; ".join(f"{key}: {rejected[key]}" for key in rejected)
            err = ADOError(
                self._clean(
                    "update failed for item"
                    f" {item_id}: no fields persisted. {detail}"
                ),
                status_code=400,
            )
            err.applied = []
            err.rejected = dict(rejected)
            raise err
        try:
            item = self.get_item(item_id)
        except ADOError:
            item = WorkItem(id=str(item_id), title="", state="", changed="")
        return self._attach_write_meta(item, warnings, applied, rejected)

    def update_item(
        self, item_id: str, fields: dict[str, Any]
    ) -> WorkItem:
        normalized, warnings = self._normalize_fields(dict(fields))
        sendable = {
            key: val for key, val in normalized.items() if val is not None
        }
        if not sendable:
            try:
                item = self.get_item(item_id)
            except ADOError:
                item = WorkItem(id=str(item_id), title="", state="", changed="")
            return self._attach_write_meta(item, warnings, [], {})
        url = self._url(
            f"/_apis/wit/workitems/{item_id}?api-version={API_VERSION}"
        )
        try:
            data = self._request(
                "PATCH",
                url,
                self._patch_ops(sendable),
                "application/json-patch+json",
            )
        except ADOError as exc:
            if exc.status_code != 400:
                raise
            return self._update_fields_one_by_one(
                item_id, sendable, warnings, url
            )
        if not isinstance(data, dict) or not data.get("id"):
            raise ADOError(f"update failed for item: {item_id}")
        item = self._to_item(data)
        return self._attach_write_meta(item, warnings, list(sendable), {})

    def create_item(
        self, title: str, fields: dict[str, Any] | None = None
    ) -> WorkItem:
        given = dict(fields or {})
        item_type = str(given.pop("type", "Task"))
        given.setdefault("title", title)
        if _STATE_KEYS.isdisjoint(given):
            given["state"] = self._mapped_state("open")
        normalized, warnings = self._normalize_fields(given)
        url = self._url(
            f"/_apis/wit/workitems/${quote(item_type, safe='')}"
            f"?api-version={API_VERSION}"
        )
        data = self._request(
            "POST",
            url,
            self._patch_ops(normalized),
            "application/json-patch+json",
        )
        if not isinstance(data, dict) or not data.get("id"):
            raise ADOError("create failed: no work item id returned")
        item = self._to_item(data)
        if warnings:
            item.fields["warning"] = warnings[0]
            if len(warnings) > 1:
                item.fields["warnings"] = list(warnings)
        return item

    def add_link(self, source_id: str, target_id: str, rel: str) -> Link:
        full = _REL_ALIASES.get(rel)
        if full is None:
            raise ADOError(f"unknown link rel: {rel}")
        target_url = self._url(f"/_apis/wit/workItems/{target_id}")
        url = self._url(
            f"/_apis/wit/workitems/{source_id}?api-version={API_VERSION}"
        )
        self._request(
            "PATCH",
            url,
            [
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {"rel": full, "url": target_url},
                }
            ],
            "application/json-patch+json",
        )
        return Link(source_id=source_id, target_id=target_id, rel=full)

    def list_links(self, item_id: str) -> list[Link]:
        item = self.get_item(item_id)
        relations = item.fields.get("relations", [])
        if not isinstance(relations, list):
            return []
        out: list[Link] = []
        for entry in relations:
            if not isinstance(entry, dict):
                continue
            rel = str(entry.get("rel", ""))
            if rel not in (
                DEPENDENCY_FORWARD,
                DEPENDENCY_REVERSE,
                HIERARCHY_FORWARD,
                HIERARCHY_REVERSE,
            ):
                continue
            other = str(entry.get("url", "")).rstrip("/").rsplit("/", 1)[-1]
            if not other:
                continue
            out.append(Link(source_id=item_id, target_id=other, rel=rel))
        return out

    def _team_prefix(self) -> str:
        if not self._team:
            raise ADOError("ADO_TEAM is required for iterations.")
        return (
            f"{self._org}/{quote(self._project, safe='')}"
            f"/{quote(self._team, safe='')}"
        )

    @staticmethod
    def _to_iteration(data: dict[str, Any]) -> Iteration:
        attrs = data.get("attributes", {})
        if not isinstance(attrs, dict):
            attrs = {}
        return Iteration(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            start_date=str(attrs.get("startDate", "") or "")[:10],
            end_date=str(attrs.get("finishDate", "") or "")[:10],
            path=str(data.get("path", "")),
        )

    def list_iterations(self) -> list[Iteration]:
        """List team sprints, oldest first."""
        url = self._team_prefix()
        url += f"/_apis/work/teamsettings/iterations?api-version={API_VERSION}"
        data = self._request("GET", url)
        values = data.get("value", []) if isinstance(data, dict) else []
        out = [
            self._to_iteration(entry)
            for entry in values
            if isinstance(entry, dict)
        ]
        return sorted(out, key=lambda it: (it.start_date, it.id))

    def create_iteration(
        self, name: str, start_date: str = "", end_date: str = ""
    ) -> Iteration:
        """Create a sprint node with dates, then add it to the team."""
        label = name.strip()
        if not label:
            raise ADOError("iteration name is required")
        if not self._team:
            raise ADOError("ADO_TEAM is required for iterations.")
        attrs: dict[str, str] = {}
        if start_date.strip():
            attrs["startDate"] = start_date.strip()
        if end_date.strip():
            attrs["finishDate"] = end_date.strip()
        node_url = self._url(
            "/_apis/wit/classificationnodes/iterations"
            f"?api-version={API_VERSION}"
        )
        node = self._request(
            "POST", node_url, {"name": label, "attributes": attrs}
        )
        guid = ""
        if isinstance(node, dict):
            guid = str(node.get("identifier", "") or node.get("id", ""))
        team_url = self._team_prefix()
        team_url += (
            f"/_apis/work/teamsettings/iterations?api-version={API_VERSION}"
        )
        data = self._request("POST", team_url, {"id": guid})
        if not isinstance(data, dict) or not data.get("id"):
            raise ADOError("create iteration failed: no id returned")
        return self._to_iteration(data)

    def set_item_iteration(
        self, item_id: str, iteration_id: str
    ) -> WorkItem:
        """Move one item to a sprint by id, name, or path."""
        path = iteration_id
        for cand in self.list_iterations():
            if iteration_id in (cand.id, cand.name, cand.path):
                path = cand.path or cand.name
                break
        item = self.update_item(item_id, {"System.IterationPath": path})
        item.fields["iteration_id"] = iteration_id
        return item

    @staticmethod
    def _to_comment(item_id: str, data: dict[str, Any]) -> Comment:
        made_by = data.get("createdBy", {})
        author = ""
        if isinstance(made_by, dict):
            author = str(made_by.get("displayName", ""))
        return Comment(
            id=str(data.get("id", "")),
            item_id=item_id,
            text=str(data.get("text", "")),
            author=author,
            created=str(data.get("createdDate", "")),
        )

    def list_comments(self, item_id: str) -> list[Comment]:
        """List comments on one item, oldest first."""
        url = self._url(
            f"/_apis/wit/workitems/{item_id}/comments"
            f"?api-version={COMMENTS_VERSION}"
        )
        data = self._request("GET", url)
        if isinstance(data, dict):
            values = data.get("comments")
            if values is None:
                values = data.get("value", [])
        else:
            values = []
        if not isinstance(values, list):
            values = []
        return [
            self._to_comment(item_id, entry)
            for entry in values
            if isinstance(entry, dict)
        ]

    def add_comment(
        self, item_id: str, text: str, author: str = ""
    ) -> Comment:
        """Add a comment; ADO sets the author from the PAT identity."""
        body = text.strip()
        if not body:
            raise ADOError("comment text is required")
        url = self._url(
            f"/_apis/wit/workitems/{item_id}/comments"
            f"?api-version={COMMENTS_VERSION}"
        )
        data = self._request("POST", url, {"text": body})
        if not isinstance(data, dict) or not data.get("id"):
            raise ADOError(f"add comment failed for item: {item_id}")
        comment = self._to_comment(item_id, data)
        if author.strip() and not comment.author:
            comment.author = author.strip()
        return comment

    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        """List changed items; closed means is_closed state on any process."""
        if since:
            tick = since.replace("'", "''")
            query = (
                "SELECT [System.Id] FROM WorkItems"
                f" WHERE [System.ChangedDate] > '{tick}'"
                " ORDER BY [System.ChangedDate] ASC"
            )
        else:
            query = (
                "SELECT [System.Id] FROM WorkItems"
                " WHERE [System.ChangedDate] >= @Today - 30"
                " ORDER BY [System.ChangedDate] ASC"
            )
        items = self._batch_get(self._wiql(query))
        for item in items:
            item.fields.setdefault("is_closed", is_closed(item.state))
        return items


AzureDevOpsAdapter = ADOAdapter
