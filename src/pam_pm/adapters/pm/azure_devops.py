"""Azure DevOps adapter over REST 7.1 with httpx.

The PAT comes from the ADO_PAT env var only; it is never accepted as an
argument and never appears in errors or logs.
"""

from __future__ import annotations

import base64
import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from pam_pm.adapters.pm.base import Link, PMAdapter, WorkItem, is_closed

API_VERSION = "7.1"

STATE_MAP = {
    "Agile": {"open": "New", "active": "Active", "done": "Closed"},
    "Scrum": {"open": "To Do", "active": "In Progress", "done": "Done"},
}

DEPENDENCY_FORWARD = "System.LinkTypes.Dependency-Forward"
DEPENDENCY_REVERSE = "System.LinkTypes.Dependency-Reverse"

_REL_ALIASES = {
    "Dependency-Forward": DEPENDENCY_FORWARD,
    "Dependency-Reverse": DEPENDENCY_REVERSE,
    DEPENDENCY_FORWARD: DEPENDENCY_FORWARD,
    DEPENDENCY_REVERSE: DEPENDENCY_REVERSE,
}

FIELD_MAP = {
    "title": "System.Title",
    "state": "System.State",
    "description": "System.Description",
    "tags": "System.Tags",
    "assigned_to": "System.AssignedTo",
    "remaining": "Microsoft.VSTS.Scheduling.RemainingWork",
}

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+")


def redact(text: object, *secrets: str) -> str:
    """Redact bearer values and PAT material from log text."""
    out = _BEARER_RE.sub("Bearer [REDACTED]", str(text))
    for val in secrets:
        if val and len(val) >= 4 and val in out:
            out = out.replace(val, "[REDACTED]")
    return out


class ADOError(RuntimeError):
    """ADO call failure with the PAT already redacted."""


class ADOAdapter(PMAdapter):
    """PM adapter backed by the ADO work item REST API."""

    def __init__(
        self,
        org_url: str | None = None,
        project: str | None = None,
        team: str | None = None,
        timeout: int = 30,
        transport: httpx.BaseTransport | None = None,
        process: str | None = None,
    ) -> None:
        org = (org_url or os.environ.get("ADO_ORG_URL", "")).strip().rstrip("/")
        proj = (project or os.environ.get("ADO_PROJECT", "")).strip()
        pat = os.environ.get("ADO_PAT", "").strip()
        if not org or not proj:
            raise ADOError("ADO_ORG_URL and ADO_PROJECT are required.")
        if not pat:
            raise ADOError("ADO_PAT is missing; set it in the env.")
        self._org = org
        self._project = proj
        self._team = (team or os.environ.get("ADO_TEAM", "")).strip()
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
        proc = self._process if self._process in STATE_MAP else None
        if proc is None:
            proc = self.detect_process()
        mapping = STATE_MAP.get(str(proc), STATE_MAP["Agile"])
        return str(mapping.get(kind, mapping["open"]))

    def close_item(self, item_id: str) -> WorkItem:
        """Move one item to its mapped closed state."""
        return self.update_item(item_id, {"state": self._mapped_state("done")})

    def activate_item(self, item_id: str) -> WorkItem:
        """Move one item to its mapped active state."""
        return self.update_item(item_id, {"state": self._mapped_state("active")})

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
                self._clean(f"{method} {url} status={resp.status_code}: {detail}")
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
    def _patch_ops(fields: dict[str, Any]) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        for key, val in fields.items():
            if key.startswith("/fields/"):
                path = key
            elif key in FIELD_MAP:
                path = "/fields/" + FIELD_MAP[key]
            elif "." in key:
                path = "/fields/" + key
            else:
                path = "/fields/" + key
            ops.append({"op": "add", "path": path, "value": val})
        return ops

    def update_item(
        self, item_id: str, fields: dict[str, Any]
    ) -> WorkItem:
        url = self._url(
            f"/_apis/wit/workitems/{item_id}?api-version={API_VERSION}"
        )
        data = self._request(
            "PATCH",
            url,
            self._patch_ops(dict(fields)),
            "application/json-patch+json",
        )
        if not isinstance(data, dict) or not data.get("id"):
            raise ADOError(f"update failed for item: {item_id}")
        return self._to_item(data)

    def create_item(
        self, title: str, fields: dict[str, Any] | None = None
    ) -> WorkItem:
        given = dict(fields or {})
        item_type = str(given.pop("type", "Task"))
        given.setdefault("title", title)
        state_keys = {"state", "System.State", "/fields/System.State"}
        if state_keys.isdisjoint(given):
            given["state"] = self._mapped_state("open")
        url = self._url(
            f"/_apis/wit/workitems/${quote(item_type, safe='')}"
            f"?api-version={API_VERSION}"
        )
        data = self._request(
            "POST", url, self._patch_ops(given), "application/json-patch+json"
        )
        if not isinstance(data, dict) or not data.get("id"):
            raise ADOError("create failed: no work item id returned")
        return self._to_item(data)

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
            if rel not in (DEPENDENCY_FORWARD, DEPENDENCY_REVERSE):
                continue
            other = str(entry.get("url", "")).rstrip("/").rsplit("/", 1)[-1]
            if not other:
                continue
            out.append(Link(source_id=item_id, target_id=other, rel=rel))
        return out

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
