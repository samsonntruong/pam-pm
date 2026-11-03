"""Linear adapter over GraphQL with an API key.

Config resolves explicit, then env, then saved wizard values:
LINEAR_TEAM and LINEAR_API_KEY. The key is never accepted as an
argument and never appears in errors or logs. Works headless with
a personal API key against api.linear.app.

Teams map to projects: issues are scoped to one team key. Cycles
map to iterations. Dependencies use issue relations of type
blocks. Change detection polls on updatedAt.
"""

from __future__ import annotations

import os
import re
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

API_URL = "https://api.linear.app/graphql"

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

_KEY_RE = re.compile(r"lin_api_[A-Za-z0-9]+")


def redact(text: object, *secrets: str) -> str:
    """Redact API key material from log text."""
    out = _KEY_RE.sub("[REDACTED]", str(text))
    for val in secrets:
        if val and len(val) >= 4 and val in out:
            out = out.replace(val, "[REDACTED]")
    return out


class LinearError(RuntimeError):
    """Linear call failure with the key already redacted."""


ISSUE_FIELDS = """
  id
  identifier
  title
  description
  state { id name type }
  labels(first: 20) { nodes { name } }
  dueDate
  assignee { displayName }
  estimate
  priority
  updatedAt
  cycle { id name }
  url
"""


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


class LinearAdapter(PMAdapter):
    """PM adapter backed by the Linear GraphQL API."""

    supports_dependency_links: bool = True
    supports_iterations: bool = True
    supports_comments: bool = True

    def __init__(
        self,
        team: str | None = None,
        timeout: int = 30,
        transport: httpx.BaseTransport | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        saved_team = ""
        saved_key = ""
        try:
            from pam_pm.core import secrets as secrets_mod

            effective = secrets_mod.effective_linear_config(data_dir)
            saved_team = str(effective.get("team", "") or "")
            saved_key = str(effective.get("api_key", "") or "")
        except Exception:  # noqa: BLE001
            saved_team = env_first("LINEAR_TEAM", "LINEAR_TEAM_KEY")
            saved_key = os.environ.get("LINEAR_API_KEY", "")
        want_team = (team or "").strip() or saved_team.strip()
        key = saved_key.strip()
        if not want_team:
            raise LinearError("LINEAR_TEAM is required.")
        if not key:
            raise LinearError(
                "LINEAR_API_KEY is missing; save it in Setup or set LINEAR_API_KEY."
            )
        self._team_key = want_team
        self._key = key
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        self._team_id: str | None = None
        self._states: list[dict[str, Any]] | None = None
        self._labels: dict[str, str] | None = None

    def close(self) -> None:
        self._client.close()

    def _clean(self, text: object) -> str:
        return redact(text, self._key)

    def _graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            resp = self._client.post(
                API_URL, json={"query": query, "variables": variables or {}}
            )
        except httpx.HTTPError as exc:
            raise LinearError(self._clean(f"request failed: {exc}")) from exc
        if resp.status_code >= 400:
            detail = resp.text[:500]
            raise LinearError(
                self._clean(f"graphql status={resp.status_code}: {detail}")
            )
        try:
            data = resp.json()
        except ValueError:
            raise LinearError("graphql returned invalid JSON") from None
        if not isinstance(data, dict):
            raise LinearError("graphql returned an unexpected payload")
        errors = data.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) else errors
            message = ""
            if isinstance(first, dict):
                message = str(first.get("message", ""))
            raise LinearError(self._clean(f"graphql error: {message}"[:500]))
        payload = data.get("data", {})
        return payload if isinstance(payload, dict) else {}

    def _team_id_for_key(self) -> str:
        if self._team_id:
            return self._team_id
        data = self._graphql(
            "query TeamLookup { teams(first: 50) { nodes { id key name } } }"
        )
        teams = data.get("teams", {})
        nodes = teams.get("nodes", []) if isinstance(teams, dict) else []
        want = self._team_key.strip().lower()
        for entry in nodes if isinstance(nodes, list) else []:
            if not isinstance(entry, dict):
                continue
            for field in ("key", "name", "id"):
                if str(entry.get(field, "") or "").strip().lower() == want:
                    self._team_id = str(entry.get("id", ""))
                    return self._team_id
        raise LinearError(f"unknown Linear team: {self._team_key}")

    def _workflow_states(self) -> list[dict[str, Any]]:
        if self._states is not None:
            return self._states
        team_id = self._team_id_for_key()
        data = self._graphql(
            "query TeamStates($teamId: String!) {"
            " team(id: $teamId) { states(first: 50)"
            " { nodes { id name type position } } } }",
            {"teamId": team_id},
        )
        team = data.get("team", {})
        states = team.get("states", {}) if isinstance(team, dict) else {}
        nodes = states.get("nodes", []) if isinstance(states, dict) else []
        rows = [row for row in nodes if isinstance(row, dict)]
        rows.sort(key=lambda row: (float(row.get("position", 0) or 0)))
        self._states = rows
        return rows

    def _state_id_for_name(self, name: str) -> str:
        want = name.strip().lower()
        for entry in self._workflow_states():
            if str(entry.get("name", "")).strip().lower() == want:
                return str(entry.get("id", ""))
        raise LinearError(f"unknown Linear state: {name}")

    def _closing_state_id(self) -> str:
        completed = ""
        closed_named = ""
        for entry in self._workflow_states():
            state_id = str(entry.get("id", ""))
            name = str(entry.get("name", ""))
            state_type = str(entry.get("type", "")).strip().lower()
            if state_type == "completed" and not completed:
                completed = state_id
            if is_closed(name) and not closed_named:
                closed_named = state_id
        return completed or closed_named

    def _team_labels(self) -> dict[str, str]:
        if self._labels is not None:
            return self._labels
        team_id = self._team_id_for_key()
        data = self._graphql(
            "query TeamLabels($teamId: String!) {"
            " team(id: $teamId) { labels(first: 100)"
            " { nodes { id name } } } }",
            {"teamId": team_id},
        )
        team = data.get("team", {})
        labels = team.get("labels", {}) if isinstance(team, dict) else {}
        nodes = labels.get("nodes", []) if isinstance(labels, dict) else []
        out: dict[str, str] = {}
        for entry in nodes if isinstance(nodes, list) else []:
            if isinstance(entry, dict) and entry.get("name"):
                out[str(entry["name"]).strip().lower()] = str(entry.get("id", ""))
        self._labels = out
        return out

    def _assignee_id(self, owner: str) -> str:
        name = owner.strip()
        if not name:
            return ""
        try:
            data = self._graphql(
                "query UserLookup($name: String!) { users("
                'filter: {displayName: {eq: $name}} first: 5)'
                " { nodes { id displayName } } }",
                {"name": name},
            )
        except LinearError:
            return ""
        users = data.get("users", {})
        nodes = users.get("nodes", []) if isinstance(users, dict) else []
        for entry in nodes if isinstance(nodes, list) else []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("displayName", "")).strip().lower() == name.lower():
                return str(entry.get("id", ""))
        return ""

    @staticmethod
    def _split_tags(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(tag).strip() for tag in value if str(tag).strip()]
        return [
            part
            for part in (
                p.strip() for p in str(value or "").replace(",", ";").split(";")
            )
            if part
        ]

    def _issue_input(self, fields: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, val in fields.items():
            if key == "title":
                out["title"] = val
            elif key == "description":
                out["description"] = str(val or "")
            elif key == "state":
                name = str(val or "").strip()
                if name:
                    out["stateId"] = self._state_id_for_name(name)
            elif key == "tags":
                labels = self._team_labels()
                ids = [
                    labels[tag.lower()]
                    for tag in self._split_tags(val)
                    if tag.lower() in labels
                ]
                out["labelIds"] = ids
            elif key == "due_date":
                text = str(val or "").strip()
                out["dueDate"] = text or None
            elif key == "owner" or key == "assigned_to":
                found = self._assignee_id(str(val or ""))
                if found:
                    out["assigneeId"] = found
            elif key == "estimate":
                try:
                    out["estimate"] = float(val)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    pass
            elif key == "priority":
                try:
                    out["priority"] = int(str(val).strip())
                except (TypeError, ValueError):
                    pass
            elif key == "iteration_id":
                text = str(val or "").strip()
                out["cycleId"] = text or None
        return out

    @classmethod
    def _to_item(cls, data: dict[str, Any]) -> WorkItem:
        state = data.get("state", {})
        state_name = ""
        if isinstance(state, dict):
            state_name = str(state.get("name", "") or "")
        labels = data.get("labels", {})
        nodes = labels.get("nodes", []) if isinstance(labels, dict) else []
        tags = [
            str(entry.get("name", ""))
            for entry in nodes
            if isinstance(entry, dict) and entry.get("name")
        ]
        assignee = data.get("assignee", {})
        owner = ""
        if isinstance(assignee, dict):
            owner = str(assignee.get("displayName", "") or "")
        cycle = data.get("cycle", {})
        cycle_id = ""
        cycle_name = ""
        if isinstance(cycle, dict):
            cycle_id = str(cycle.get("id", "") or "")
            cycle_name = str(cycle.get("name", "") or "")
        ident = str(data.get("identifier", "") or data.get("id", ""))
        item_fields: dict[str, Any] = {
            "title": str(data.get("title", "")),
            "state": state_name,
            "description": str(data.get("description", "") or ""),
            "tags": tags,
            "due_date": str(data.get("dueDate", "") or ""),
            "owner": owner,
            "assigned_to": owner,
            "estimate": data.get("estimate"),
            "priority": data.get("priority"),
            "iteration_id": cycle_id,
            "iteration_name": cycle_name,
            "identifier": ident,
            "linear_id": str(data.get("id", "")),
            "url": str(data.get("url", "") or ""),
            "is_closed": is_closed(state_name),
        }
        return WorkItem(
            id=ident,
            title=str(data.get("title", "")),
            state=state_name,
            changed=str(data.get("updatedAt", "") or ""),
            fields=item_fields,
        )

    def _issue_pages(
        self, filt: dict[str, Any], want: int
    ) -> list[dict[str, Any]]:
        query = (
            "query TeamIssues($filter: IssueFilter, $first: Int!, $after: String)"
            " { issues(filter: $filter first: $first after: $after"
            " orderBy: updatedAt)"
            " { nodes {" + ISSUE_FIELDS + " }"
            " pageInfo { hasNextPage endCursor } } }"
        )
        rows: list[dict[str, Any]] = []
        after: str | None = None
        for _ in range(5):
            data = self._graphql(
                query,
                {"filter": filt, "first": min(max(want, 1), 50), "after": after},
            )
            issues = data.get("issues", {})
            if not isinstance(issues, dict):
                break
            nodes = issues.get("nodes", [])
            if isinstance(nodes, list):
                rows.extend(row for row in nodes if isinstance(row, dict))
            page = issues.get("pageInfo", {})
            if not isinstance(page, dict) or not page.get("hasNextPage"):
                break
            after = str(page.get("endCursor", "") or "") or None
            if not after or len(rows) >= 250:
                break
        return rows

    def _team_filter(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        filt: dict[str, Any] = {"team": {"id": {"eq": self._team_id_for_key()}}}
        if extra:
            filt.update(extra)
        return filt

    def list_items(self, limit: int = 50) -> list[WorkItem]:
        rows = self._issue_pages(self._team_filter(), max(limit, 1))
        items = [self._to_item(row) for row in rows]
        items.sort(key=lambda item: item.changed, reverse=True)
        return items[:limit]

    def get_item(self, item_id: str) -> WorkItem:
        key = (item_id or "").strip()
        if not key:
            raise LinearError("issue id is required")
        data = self._graphql(
            "query IssueGet($id: String!) { issue(id: $id) {" + ISSUE_FIELDS + " } }",
            {"id": key},
        )
        issue = data.get("issue")
        if not isinstance(issue, dict) or not issue.get("id"):
            raise LinearError(f"unknown item: {key}")
        return self._to_item(issue)

    def _uuid_for(self, item_id: str) -> str:
        key = (item_id or "").strip()
        if not key:
            raise LinearError("issue id is required")
        uuid_re = (
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{12}"
        )
        if re.fullmatch(uuid_re, key):
            return key
        item = self.get_item(key)
        uuid = str(item.fields.get("linear_id", "") or "").strip()
        if not uuid:
            raise LinearError(f"unknown item: {key}")
        if re.fullmatch(uuid_re, uuid):
            return uuid
        return key

    def create_item(
        self, title: str, fields: dict[str, Any] | None = None
    ) -> WorkItem:
        given = dict(fields or {})
        payload: dict[str, Any] = {
            "teamId": self._team_id_for_key(),
            "title": title,
            **self._issue_input(given),
        }
        data = self._graphql(
            "mutation IssueCreate($input: IssueCreateInput!) {"
            " issueCreate(input: $input)"
            " { success issue {" + ISSUE_FIELDS + " } } }",
            {"input": payload},
        )
        created = data.get("issueCreate", {})
        issue = created.get("issue", {}) if isinstance(created, dict) else {}
        if not isinstance(issue, dict) or not issue.get("id"):
            raise LinearError("create failed: no issue returned")
        return self._to_item(issue)

    def update_item(
        self, item_id: str, fields: dict[str, Any]
    ) -> WorkItem:
        key = (item_id or "").strip()
        if not key:
            raise LinearError("issue id is required")
        data = self._graphql(
            "mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {"
            " issueUpdate(id: $id input: $input)"
            " { success issue {" + ISSUE_FIELDS + " } } }",
            {"id": key, "input": self._issue_input(dict(fields))},
        )
        updated = data.get("issueUpdate", {})
        issue = updated.get("issue", {}) if isinstance(updated, dict) else {}
        if not isinstance(issue, dict) or not issue.get("id"):
            raise LinearError(f"update failed for item: {key}")
        return self._to_item(issue)

    def delete_item(self, item_id: str) -> None:
        key = (item_id or "").strip()
        if not key:
            raise LinearError("issue id is required")
        self._graphql(
            "mutation IssueDelete($id: String!) {"
            " issueDelete(id: $id) { success } }",
            {"id": key},
        )

    def list_states(self) -> list[str]:
        """List team workflow states in workflow order."""
        return [str(entry.get("name", "")) for entry in self._workflow_states()]

    def close_item(self, item_id: str) -> WorkItem:
        key = (item_id or "").strip()
        if not key:
            raise LinearError("issue id is required")
        current = self.get_item(key)
        if is_closed(current.state):
            return current
        state_id = self._closing_state_id()
        if not state_id:
            raise LinearError("no completed state on this team")
        data = self._graphql(
            "mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {"
            " issueUpdate(id: $id input: $input)"
            " { success issue {" + ISSUE_FIELDS + " } } }",
            {"id": key, "input": {"stateId": state_id}},
        )
        updated = data.get("issueUpdate", {})
        issue = updated.get("issue", {}) if isinstance(updated, dict) else {}
        if not isinstance(issue, dict) or not issue.get("id"):
            raise LinearError(f"close failed for item: {key}")
        return self._to_item(issue)

    @staticmethod
    def _to_iteration(data: dict[str, Any]) -> Iteration:
        return Iteration(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            start_date=str(data.get("startsAt", "") or "")[:10],
            end_date=str(data.get("endsAt", "") or "")[:10],
            path=str(data.get("name", "")),
        )

    def list_iterations(self) -> list[Iteration]:
        """List team cycles, oldest first."""
        team_id = self._team_id_for_key()
        data = self._graphql(
            "query TeamCycles($teamId: String!) {"
            " team(id: $teamId) { cycles(first: 50)"
            " { nodes { id name startsAt endsAt } } } }",
            {"teamId": team_id},
        )
        team = data.get("team", {})
        cycles = team.get("cycles", {}) if isinstance(team, dict) else {}
        nodes = cycles.get("nodes", []) if isinstance(cycles, dict) else []
        out = [
            self._to_iteration(entry)
            for entry in nodes
            if isinstance(entry, dict)
        ]
        return sorted(out, key=lambda it: (it.start_date, it.id))

    def create_iteration(
        self, name: str, start_date: str = "", end_date: str = ""
    ) -> Iteration:
        label = name.strip()
        if not label:
            raise LinearError("iteration name is required")
        payload: dict[str, Any] = {
            "teamId": self._team_id_for_key(),
            "name": label,
        }
        if start_date.strip():
            payload["startsAt"] = start_date.strip()
        if end_date.strip():
            payload["endsAt"] = end_date.strip()
        data = self._graphql(
            "mutation CycleCreate($input: CycleCreateInput!) {"
            " cycleCreate(input: $input)"
            " { success cycle { id name startsAt endsAt } } }",
            {"input": payload},
        )
        created = data.get("cycleCreate", {})
        cycle = created.get("cycle", {}) if isinstance(created, dict) else {}
        if not isinstance(cycle, dict) or not cycle.get("id"):
            raise LinearError("create iteration failed: no cycle returned")
        return self._to_iteration(cycle)

    def set_item_iteration(
        self, item_id: str, iteration_id: str
    ) -> WorkItem:
        """Move one issue to a cycle, or off the cycle when empty."""
        return self.update_item(item_id, {"iteration_id": iteration_id.strip()})

    @staticmethod
    def _normalize_rel(rel: str) -> str:
        text = (rel or "").strip().lower().replace("_", "-")
        if text in _BLOCKS_ALIASES:
            return REL_BLOCKS
        if text in _BLOCKED_BY_ALIASES:
            return REL_BLOCKED_BY
        raise LinearError(f"unknown link rel: {rel}")

    def add_link(self, source_id: str, target_id: str, rel: str) -> Link:
        kind = self._normalize_rel(rel)
        source = (source_id or "").strip()
        target = (target_id or "").strip()
        if not source or not target:
            raise LinearError("source and target ids are required")
        if kind == REL_BLOCKS:
            blocker, blocked = source, target
        else:
            blocker, blocked = target, source
        blocker_uuid = self._uuid_for(blocker)
        blocked_uuid = self._uuid_for(blocked)
        self._graphql(
            "mutation RelationCreate($input: IssueRelationCreateInput!) {"
            " issueRelationCreate(input: $input)"
            " { success issueRelation { id } } }",
            {
                "input": {
                    "issueId": blocker_uuid,
                    "relatedIssueId": blocked_uuid,
                    "type": "blocks",
                }
            },
        )
        return Link(source_id=source, target_id=target, rel=kind)

    def list_links(self, item_id: str) -> list[Link]:
        key = (item_id or "").strip()
        if not key:
            raise LinearError("issue id is required")
        uuid = self._uuid_for(key)
        data = self._graphql(
            "query IssueRelations($id: String!) { issue(id: $id) {"
            " relations(first: 50)"
            " { nodes { type relatedIssue { id identifier } } }"
            " inverseRelations(first: 50)"
            " { nodes { type relatedIssue { id identifier } } } } }",
            {"id": uuid},
        )
        issue = data.get("issue", {})
        if not isinstance(issue, dict):
            return []
        out: list[Link] = []
        for field, rel in (("relations", REL_BLOCKS), ("inverseRelations", REL_BLOCKED_BY)):
            block = issue.get(field, {})
            nodes = block.get("nodes", []) if isinstance(block, dict) else []
            for entry in nodes if isinstance(nodes, list) else []:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("type", "")).strip().lower() != "blocks":
                    continue
                related = entry.get("relatedIssue", {})
                if not isinstance(related, dict):
                    continue
                related_id = str(related.get("id", "") or "").strip()
                other = str(
                    related.get("identifier", "") or related_id
                ).strip()
                if not other:
                    continue
                if related_id and uuid and related_id == uuid:
                    continue
                if other == key or other == uuid:
                    continue
                out.append(Link(source_id=key, target_id=other, rel=rel))
        return out

    @staticmethod
    def _to_comment(item_id: str, data: dict[str, Any]) -> Comment:
        user = data.get("user", {})
        name = ""
        if isinstance(user, dict):
            name = str(user.get("displayName", "") or "")
        return Comment(
            id=str(data.get("id", "")),
            item_id=item_id,
            text=str(data.get("body", "") or ""),
            author=name,
            created=str(data.get("createdAt", "") or ""),
        )

    def list_comments(self, item_id: str) -> list[Comment]:
        key = (item_id or "").strip()
        if not key:
            raise LinearError("issue id is required")
        data = self._graphql(
            "query IssueComments($id: String!) { issue(id: $id) {"
            " comments(first: 50)"
            " { nodes { id body user { displayName } createdAt } } } }",
            {"id": key},
        )
        issue = data.get("issue", {})
        block = issue.get("comments", {}) if isinstance(issue, dict) else {}
        nodes = block.get("nodes", []) if isinstance(block, dict) else []
        comments = [
            self._to_comment(key, entry)
            for entry in nodes
            if isinstance(entry, dict)
        ]
        comments.sort(key=lambda entry: entry.created)
        return comments

    def add_comment(
        self, item_id: str, text: str, author: str = ""
    ) -> Comment:
        key = (item_id or "").strip()
        if not key:
            raise LinearError("issue id is required")
        body = text.strip()
        if not body:
            raise LinearError("comment text is required")
        data = self._graphql(
            "mutation CommentCreate($input: CommentCreateInput!) {"
            " commentCreate(input: $input) { success comment"
            " { id body user { displayName } createdAt } } }",
            {"input": {"issueId": key, "body": body}},
        )
        created = data.get("commentCreate", {})
        node = created.get("comment", {}) if isinstance(created, dict) else {}
        if not isinstance(node, dict) or not node.get("id"):
            raise LinearError(f"add comment failed for item: {key}")
        comment = self._to_comment(key, node)
        if author.strip() and not comment.author:
            comment.author = author.strip()
        return comment

    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        """List changed issues; closed means is_closed on the state."""
        filt = self._team_filter()
        watermark = _parse_ts(since) if since else None
        if watermark is not None:
            filt["updatedAt"] = {"gte": watermark.isoformat()}
        rows = self._issue_pages(filt, 50)
        items = [self._to_item(row) for row in rows]
        items.sort(key=lambda item: item.changed)
        if watermark is None:
            return items
        out: list[WorkItem] = []
        for item in items:
            changed = _parse_ts(item.changed)
            if changed is not None and changed > watermark:
                out.append(item)
        return out
