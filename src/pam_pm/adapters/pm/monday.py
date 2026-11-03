"""Monday adapter over GraphQL with an API token.

Config resolves explicit, then env, then saved wizard values:
MONDAY_BOARD, MONDAY_API_TOKEN. The token is never accepted as
an argument and never appears in errors or logs. Works headless
with a personal token against api.monday.com.

Boards map to Pam projects: items are scoped to one board.
Groups map to states in group order. Items carry column values
for description, due date, owner, and tags, plus a dependency
column for links. Comments are item updates. Change detection
polls the board for items updated after a watermark.
"""

from __future__ import annotations

import json
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

API_URL = "https://api.monday.com/v2"

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

ITEM_FIELDS = """
  id
  name
  group { id title }
  column_values { id text value column { id title type } }
  updated_at
"""


def redact(text: object, *secrets: str) -> str:
    """Redact token material from log text."""
    out = str(text)
    for val in secrets:
        if val and len(val) >= 4 and val in out:
            out = out.replace(val, "[REDACTED]")
    return out


class MondayError(RuntimeError):
    """Monday call failure with the token already redacted."""


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


def _due_from_text(value: object, raw: object) -> str:
    text = str(value or "").strip()
    if text and len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    try:
        payload = json.loads(str(raw or ""))
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        for key in ("date", "due_date", "value"):
            day = str(payload.get(key, "") or "").strip()
            if day:
                return day[:10]
    if text:
        return text[:10]
    return ""


def _linked_from_value(raw: object) -> list[str]:
    if raw is None or raw == "":
        return []
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if isinstance(payload, dict):
        for key in ("linked_item_ids", "linkedItemIds", "item_ids", "items"):
            rows = payload.get(key, [])
            if isinstance(rows, list):
                return [str(v).strip() for v in rows if str(v).strip()]
        linked = payload.get("linked", [])
        if isinstance(linked, list):
            out: list[str] = []
            for entry in linked:
                if isinstance(entry, dict) and entry.get("id"):
                    out.append(str(entry["id"]))
                elif isinstance(entry, str) and entry.strip():
                    out.append(entry.strip())
            return out
    if isinstance(payload, list):
        return [str(v).strip() for v in payload if str(v).strip()]
    return []


class MondayAdapter(PMAdapter):
    """PM adapter backed by the Monday GraphQL API."""

    supports_dependency_links: bool = True
    supports_iterations: bool = False
    supports_comments: bool = True

    def __init__(
        self,
        board: str | None = None,
        timeout: int = 30,
        transport: httpx.BaseTransport | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        saved_board = ""
        saved_token = ""
        try:
            from pam_pm.core import secrets as secrets_mod

            effective = secrets_mod.effective_monday_config(data_dir)
            saved_board = str(effective.get("board", "") or "")
            saved_token = str(effective.get("api_token", "") or "")
        except Exception:  # noqa: BLE001
            saved_board = env_first("MONDAY_BOARD", "MONDAY_BOARD_ID")
            saved_token = os.environ.get("MONDAY_API_TOKEN", "")
        want_board = (board or "").strip() or saved_board.strip()
        token = saved_token.strip()
        if not want_board:
            raise MondayError("MONDAY_BOARD is required.")
        if not token:
            raise MondayError(
                "MONDAY_API_TOKEN is missing; save it in Setup or set MONDAY_API_TOKEN."
            )
        self._board_ref = want_board
        self._token = token
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": token,
                "API-Version": "2024-01",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        self._board_id: str | None = None
        self._board_name = ""
        self._groups: list[dict[str, Any]] | None = None
        self._columns: list[dict[str, Any]] | None = None

    def close(self) -> None:
        self._client.close()

    def _clean(self, text: object) -> str:
        return redact(text, self._token)

    def _graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            resp = self._client.post(
                API_URL, json={"query": query, "variables": variables or {}}
            )
        except httpx.HTTPError as exc:
            raise MondayError(self._clean(f"request failed: {exc}")) from exc
        if resp.status_code >= 400:
            detail = resp.text[:500]
            raise MondayError(
                self._clean(f"graphql status={resp.status_code}: {detail}")
            )
        try:
            data = resp.json()
        except ValueError:
            raise MondayError("graphql returned invalid JSON") from None
        if not isinstance(data, dict):
            raise MondayError("graphql returned an unexpected payload")
        errors = data.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) else errors
            message = ""
            if isinstance(first, dict):
                message = str(first.get("message", ""))
            raise MondayError(self._clean(f"graphql error: {message}"[:500]))
        payload = data.get("data", {})
        return payload if isinstance(payload, dict) else {}

    def _board_lookup(self) -> list[dict[str, Any]]:
        data = self._graphql(
            "query BoardLookup { boards(limit: 50)"
            " { id name groups { id title } columns { id title type } } }"
        )
        rows = data.get("boards", [])
        return (
            [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        )

    def _board_id_for_ref(self) -> str:
        if self._board_id:
            return self._board_id
        want = self._board_ref.strip().lower()
        for entry in self._board_lookup():
            for field in ("id", "name"):
                if str(entry.get(field, "") or "").strip().lower() == want:
                    self._board_id = str(entry.get("id", ""))
                    self._board_name = str(entry.get("name", "") or "")
                    groups = entry.get("groups", [])
                    if isinstance(groups, list) and groups:
                        self._groups = [g for g in groups if isinstance(g, dict)]
                    columns = entry.get("columns", [])
                    if isinstance(columns, list) and columns:
                        self._columns = [c for c in columns if isinstance(c, dict)]
                    return self._board_id
        raise MondayError(f"unknown Monday board: {self._board_ref}")

    def _board_groups(self) -> list[dict[str, Any]]:
        if self._groups is not None:
            return self._groups
        board_id = self._board_id_for_ref()
        data = self._graphql(
            "query BoardGroups($boardId: ID!) { boards(ids: [$boardId])"
            " { groups { id title } } }",
            {"boardId": board_id},
        )
        rows = data.get("boards", [])
        groups: list[dict[str, Any]] = []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            raw = rows[0].get("groups", [])
            groups = (
                [g for g in raw if isinstance(g, dict)] if isinstance(raw, list) else []
            )
        self._groups = groups
        return groups

    def _board_columns(self) -> list[dict[str, Any]]:
        if self._columns is not None:
            return self._columns
        board_id = self._board_id_for_ref()
        data = self._graphql(
            "query BoardColumns($boardId: ID!) { boards(ids: [$boardId])"
            " { columns { id title type } } }",
            {"boardId": board_id},
        )
        rows = data.get("boards", [])
        columns: list[dict[str, Any]] = []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            raw = rows[0].get("columns", [])
            columns = (
                [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
            )
        self._columns = columns
        return columns

    def _group_id_for_name(self, name: str) -> str:
        want = (name or "").strip().lower()
        if not want:
            raise MondayError("Monday group name is required")
        for entry in self._board_groups():
            if str(entry.get("title", "") or "").strip().lower() == want:
                return str(entry.get("id", ""))
        raise MondayError(f"unknown Monday group: {name}")

    def _column_id_for(self, kind: str) -> str:
        columns = self._board_columns()
        if kind == "description":
            for entry in columns:
                ctype = (
                    str(entry.get("type", "") or "").strip().lower().replace("_", "-")
                )
                if ctype in ("long-text", "text"):
                    return str(entry.get("id", ""))
            for entry in columns:
                title = str(entry.get("title", "") or "").strip().lower()
                if "descript" in title or "notes" in title:
                    return str(entry.get("id", ""))
        elif kind == "due_date":
            for entry in columns:
                if str(entry.get("type", "") or "").strip().lower() == "date":
                    return str(entry.get("id", ""))
            for entry in columns:
                title = str(entry.get("title", "") or "").strip().lower()
                if "due" in title or "date" in title:
                    return str(entry.get("id", ""))
        elif kind == "owner":
            for entry in columns:
                ctype = str(entry.get("type", "") or "").strip().lower()
                if ctype in ("person", "people", "assignee"):
                    return str(entry.get("id", ""))
            for entry in columns:
                title = str(entry.get("title", "") or "").strip().lower()
                if "owner" in title or "assign" in title or "person" in title:
                    return str(entry.get("id", ""))
        elif kind == "tags":
            for entry in columns:
                ctype = str(entry.get("type", "") or "").strip().lower()
                if ctype in ("tags", "tag", "dropdown"):
                    return str(entry.get("id", ""))
            for entry in columns:
                title = str(entry.get("title", "") or "").strip().lower()
                if "tag" in title or "label" in title:
                    return str(entry.get("id", ""))
        elif kind == "dependency":
            for entry in columns:
                ctype = (
                    str(entry.get("type", "") or "").strip().lower().replace("_", "-")
                )
                if ctype in ("dependency", "board-relation"):
                    return str(entry.get("id", ""))
                if ctype.replace("-", "") in ("dependency", "boardrelation"):
                    return str(entry.get("id", ""))
            for entry in columns:
                if str(entry.get("type", "") or "").strip().lower() == "link":
                    return str(entry.get("id", ""))
            for entry in columns:
                blob = f"{entry.get('id', '')} {entry.get('title', '')}".strip().lower()
                if "depend" in blob:
                    return str(entry.get("id", ""))
            for entry in columns:
                title = str(entry.get("title", "") or "").strip().lower()
                if "link" in title:
                    return str(entry.get("id", ""))
        return ""

    def _user_id_for_name(self, name: str) -> str:
        want = (name or "").strip().lower()
        if not want:
            return ""
        try:
            data = self._graphql("query UserLookup { users(limit: 100) { id name } }")
        except MondayError:
            return ""
        rows = data.get("users", [])
        for entry in rows if isinstance(rows, list) else []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("name", "") or "").strip().lower() == want:
                return str(entry.get("id", ""))
        return ""

    @staticmethod
    def _split_names(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [
            part
            for part in (
                p.strip() for p in str(value or "").replace(",", ";").split(";")
            )
            if part
        ]

    def _column_values_for(self, fields: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, val in fields.items():
            if key == "description":
                column = self._column_id_for("description")
                if column:
                    out[column] = str(val or "")
            elif key == "due_date":
                column = self._column_id_for("due_date")
                if column:
                    text = str(val or "").strip()
                    out[column] = {"date": text[:10]} if text else {"date": None}
            elif key == "owner" or key == "assigned_to":
                column = self._column_id_for("owner")
                if column:
                    names = self._split_names(val)
                    if names:
                        found = self._user_id_for_name(names[0])
                        if found:
                            out[column] = {
                                "personsAndTeams": [{"id": found, "kind": "person"}]
                            }
                    else:
                        out[column] = {"personsAndTeams": []}
            elif key == "tags":
                column = self._column_id_for("tags")
                if column:
                    out[column] = {"labels": self._split_names(val)}
        return out

    def _values_map(self, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        rows = data.get("column_values", [])
        out: dict[str, dict[str, Any]] = {}
        for entry in rows if isinstance(rows, list) else []:
            if not isinstance(entry, dict):
                continue
            column = entry.get("column", {})
            cid = str(entry.get("id", "") or "")
            if isinstance(column, dict) and column.get("id"):
                cid = str(column.get("id", "") or cid)
            if cid:
                out[cid] = entry
        return out

    def _to_item(self, data: dict[str, Any]) -> WorkItem:
        group = data.get("group", {})
        state = ""
        group_id = ""
        if isinstance(group, dict):
            state = str(group.get("title", "") or "")
            group_id = str(group.get("id", "") or "")
        values = self._values_map(data)
        desc_col = self._column_id_for("description")
        due_col = self._column_id_for("due_date")
        owner_col = self._column_id_for("owner")
        tags_col = self._column_id_for("tags")
        description = ""
        if desc_col and desc_col in values:
            description = str(values[desc_col].get("text", "") or "")
        due_date = ""
        if due_col and due_col in values:
            due_date = _due_from_text(
                values[due_col].get("text", ""), values[due_col].get("value", "")
            )
        owner = ""
        owners: list[str] = []
        if owner_col and owner_col in values:
            owners = self._split_names(str(values[owner_col].get("text", "") or ""))
            owner = owners[0] if owners else ""
        tags: list[str] = []
        if tags_col and tags_col in values:
            raw_text = str(values[tags_col].get("text", "") or "")
            tags = self._split_names(raw_text)
            if not tags:
                try:
                    payload = json.loads(str(values[tags_col].get("value", "") or ""))
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict) and isinstance(
                    payload.get("labels"), list
                ):
                    tags = [str(v).strip() for v in payload["labels"] if str(v).strip()]
        board_id = self._board_id or ""
        item_id = str(data.get("id", ""))
        url = str(data.get("url", "") or "")
        if not url and board_id and item_id:
            url = f"https://monday.com/boards/{board_id}/pulses/{item_id}"
        item_fields: dict[str, Any] = {
            "title": str(data.get("name", "")),
            "state": state,
            "description": description,
            "tags": tags,
            "due_date": due_date,
            "owner": owner,
            "assigned_to": owner,
            "owners": owners,
            "estimate": None,
            "remaining": None,
            "iteration_id": "",
            "group_id": group_id,
            "board_id": board_id,
            "board_name": self._board_name,
            "url": url,
            "is_closed": is_closed(state),
        }
        return WorkItem(
            id=item_id,
            title=str(data.get("name", "")),
            state=state,
            changed=str(data.get("updated_at", "") or ""),
            fields=item_fields,
        )

    def _board_items(self) -> list[dict[str, Any]]:
        board_id = self._board_id_for_ref()
        data = self._graphql(
            "query BoardItems($boardId: ID!, $limit: Int!)"
            " { boards(ids: [$boardId])"
            " { items_page(limit: $limit) { items {" + ITEM_FIELDS + " } } } }",
            {"boardId": board_id, "limit": 100},
        )
        rows = data.get("boards", [])
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return []
        page = rows[0].get("items_page", {})
        items = page.get("items", []) if isinstance(page, dict) else []
        return (
            [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []
        )

    def _fetch_item(self, item_id: str) -> dict[str, Any]:
        data = self._graphql(
            "query ItemGet($itemId: ID!) { items(ids: [$itemId]) {"
            + ITEM_FIELDS
            + " board { id } } }",
            {"itemId": item_id},
        )
        rows = data.get("items", [])
        if isinstance(rows, list):
            for entry in rows:
                if isinstance(entry, dict) and str(entry.get("id", "")) == item_id:
                    return entry
        return {}

    def _require_id(self, item_id: str) -> str:
        cleaned = (item_id or "").strip()
        if not cleaned:
            raise MondayError("item id is required")
        return cleaned

    def list_items(self, limit: int = 50) -> list[WorkItem]:
        items = [self._to_item(row) for row in self._board_items()]
        items.sort(key=lambda item: item.changed, reverse=True)
        return items[:limit]

    def get_item(self, item_id: str) -> WorkItem:
        cleaned = self._require_id(item_id)
        entry = self._fetch_item(cleaned)
        if not entry.get("id"):
            raise MondayError(f"unknown item: {cleaned}")
        return self._to_item(entry)

    def create_item(self, title: str, fields: dict[str, Any] | None = None) -> WorkItem:
        given = dict(fields or {})
        given.pop("iteration_id", None)
        state = str(given.pop("state", "") or "").strip()
        title_text = str(given.pop("title", "") or "").strip() or title
        if state:
            group_id = self._group_id_for_name(state)
        else:
            groups = self._board_groups()
            if not groups:
                raise MondayError("no groups on this board")
            group_id = str(groups[0].get("id", ""))
        values = self._column_values_for(given)
        data = self._graphql(
            "mutation ItemCreate($boardId: ID!, $groupId: String!,"
            " $name: String!, $values: JSON!)"
            " { create_item(board_id: $boardId group_id: $groupId"
            " item_name: $name column_values: $values) { id } }",
            {
                "boardId": self._board_id_for_ref(),
                "groupId": group_id,
                "name": title_text or title,
                "values": json.dumps(values),
            },
        )
        created = data.get("create_item", {})
        new_id = str(created.get("id", "") or "") if isinstance(created, dict) else ""
        if not new_id:
            raise MondayError("create failed: no item id returned")
        return self.get_item(new_id)

    def update_item(self, item_id: str, fields: dict[str, Any]) -> WorkItem:
        cleaned = self._require_id(item_id)
        given = dict(fields)
        sprint = str(given.pop("iteration_id", "") or "").strip()
        if sprint:
            self.set_item_iteration(cleaned, sprint)
        board_id = self._board_id_for_ref()
        if "title" in given:
            name = str(given.pop("title", "") or "").strip()
            if name:
                self._graphql(
                    "mutation ItemRename($boardId: ID!, $itemId: ID!, $name: String!)"
                    " { change_item_name(board_id: $boardId item_id: $itemId"
                    " value: $name) { id } }",
                    {"boardId": board_id, "itemId": cleaned, "name": name},
                )
        state = str(given.pop("state", "") or "").strip()
        if state:
            self._graphql(
                "mutation ItemMove($boardId: ID!, $itemId: ID!, $groupId: String!)"
                " { move_item_to_group(board_id: $boardId item_id: $itemId"
                " group_id: $groupId) { id } }",
                {
                    "boardId": board_id,
                    "itemId": cleaned,
                    "groupId": self._group_id_for_name(state),
                },
            )
        values = self._column_values_for(given)
        if values:
            self._graphql(
                "mutation ItemColumns($boardId: ID!, $itemId: ID!, $values: JSON!)"
                " { change_multiple_column_values(board_id: $boardId item_id: $itemId"
                " column_values: $values) { id } }",
                {"boardId": board_id, "itemId": cleaned, "values": json.dumps(values)},
            )
        return self.get_item(cleaned)

    def delete_item(self, item_id: str) -> None:
        cleaned = self._require_id(item_id)
        self._graphql(
            "mutation ItemDelete($itemId: ID!) { delete_item(item_id: $itemId) { id } }",
            {"itemId": cleaned},
        )

    def list_states(self) -> list[str]:
        """List board groups in group order."""
        return [str(e.get("title", "")) for e in self._board_groups()]

    def close_item(self, item_id: str) -> WorkItem:
        cleaned = self._require_id(item_id)
        current = self.get_item(cleaned)
        if is_closed(current.state):
            return current
        by_title: dict[str, str] = {}
        for entry in self._board_groups():
            title = str(entry.get("title", "") or "").strip().lower()
            if title and title not in by_title:
                by_title[title] = str(entry.get("id", ""))
        done_group = ""
        for name in ("done", "completed", "closed"):
            if name in by_title:
                done_group = by_title[name]
                break
        if not done_group:
            raise MondayError("no Done group on this board")
        self._graphql(
            "mutation ItemMove($boardId: ID!, $itemId: ID!, $groupId: String!)"
            " { move_item_to_group(board_id: $boardId item_id: $itemId"
            " group_id: $groupId) { id } }",
            {
                "boardId": self._board_id_for_ref(),
                "itemId": cleaned,
                "groupId": done_group,
            },
        )
        return self.get_item(cleaned)

    def list_iterations(self) -> list[Iteration]:
        """Monday boards have no sprints here, so this is always empty."""
        return []

    def create_iteration(
        self, name: str, start_date: str = "", end_date: str = ""
    ) -> Iteration:
        raise MondayError(
            "Monday boards have no sprints; iterations are not supported."
        )

    def set_item_iteration(self, item_id: str, iteration_id: str) -> WorkItem:
        """Clearing is a no-op; setting raises since sprints are unsupported."""
        if not iteration_id.strip():
            return self.get_item(item_id)
        raise MondayError(
            "Monday boards have no sprints; iterations are not supported."
        )

    @staticmethod
    def _normalize_rel(rel: str) -> str:
        text = (rel or "").strip().lower().replace("_", "-")
        if text in _BLOCKS_ALIASES:
            return REL_BLOCKS
        if text in _BLOCKED_BY_ALIASES:
            return REL_BLOCKED_BY
        raise MondayError(f"unknown link rel: {rel}")

    def _dependency_ids(self, entry: dict[str, Any]) -> list[str]:
        column = self._column_id_for("dependency")
        if not column:
            return []
        for row in (
            entry.get("column_values", [])
            if isinstance(entry.get("column_values"), list)
            else []
        ):
            if not isinstance(row, dict):
                continue
            cid = str(row.get("id", "") or "")
            column_block = row.get("column", {})
            if isinstance(column_block, dict) and column_block.get("id"):
                cid = str(column_block.get("id", "") or cid)
            if cid == column:
                return _linked_from_value(row.get("value", ""))
        return []

    def add_link(self, source_id: str, target_id: str, rel: str) -> Link:
        kind = self._normalize_rel(rel)
        source = self._require_id(source_id)
        target = self._require_id(target_id)
        if kind == REL_BLOCKS:
            blocked, blocker = target, source
        else:
            blocked, blocker = source, target
        entry = self._fetch_item(blocked)
        if not entry.get("id"):
            raise MondayError(f"unknown item: {blocked}")
        other = self._fetch_item(blocker)
        if not other.get("id"):
            raise MondayError(f"unknown item: {blocker}")
        column = self._column_id_for("dependency")
        if not column:
            raise MondayError("add a Dependency or Link column to the board")
        current = self._dependency_ids(entry)
        if blocker not in current:
            current.append(blocker)
        self._graphql(
            "mutation ItemColumns($boardId: ID!, $itemId: ID!, $values: JSON!)"
            " { change_multiple_column_values(board_id: $boardId item_id: $itemId"
            " column_values: $values) { id } }",
            {
                "boardId": self._board_id_for_ref(),
                "itemId": blocked,
                "values": json.dumps({column: {"linked_item_ids": current}}),
            },
        )
        return Link(source_id=source, target_id=target, rel=kind)

    def list_links(self, item_id: str) -> list[Link]:
        cleaned = self._require_id(item_id)
        entry = self._fetch_item(cleaned)
        if not entry.get("id"):
            raise MondayError(f"unknown item: {cleaned}")
        out: list[Link] = []
        for other in self._dependency_ids(entry):
            if other and other != cleaned:
                out.append(Link(source_id=cleaned, target_id=other, rel=REL_BLOCKED_BY))
        for row in self._board_items():
            other_id = str(row.get("id", "") or "")
            if not other_id or other_id == cleaned:
                continue
            if cleaned in self._dependency_ids(row):
                out.append(Link(source_id=cleaned, target_id=other_id, rel=REL_BLOCKS))
        return out

    @staticmethod
    def _to_comment(item_id: str, data: dict[str, Any]) -> Comment:
        creator = data.get("creator", {})
        name = ""
        if isinstance(creator, dict):
            name = str(creator.get("name", "") or "")
        return Comment(
            id=str(data.get("id", "")),
            item_id=item_id,
            text=str(data.get("body", "") or ""),
            author=name,
            created=str(data.get("created_at", "") or ""),
        )

    def list_comments(self, item_id: str) -> list[Comment]:
        cleaned = self._require_id(item_id)
        data = self._graphql(
            "query ItemUpdates($itemId: ID!) { items(ids: [$itemId])"
            " { updates(limit: 50) { id body creator { name } created_at } } }",
            {"itemId": cleaned},
        )
        rows = data.get("items", [])
        updates: list[dict[str, Any]] = []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            raw = rows[0].get("updates", [])
            updates = (
                [u for u in raw if isinstance(u, dict)] if isinstance(raw, list) else []
            )
        comments = [self._to_comment(cleaned, u) for u in updates]
        comments.sort(key=lambda c: c.created)
        return comments

    def add_comment(self, item_id: str, text: str, author: str = "") -> Comment:
        cleaned = self._require_id(item_id)
        body = text.strip()
        if not body:
            raise MondayError("comment text is required")
        data = self._graphql(
            "mutation UpdateCreate($itemId: ID!, $body: String!)"
            " { create_update(item_id: $itemId body: $body)"
            " { id body creator { name } created_at } }",
            {"itemId": cleaned, "body": body},
        )
        node = data.get("create_update", {})
        if not isinstance(node, dict) or not node.get("id"):
            raise MondayError(f"add comment failed for item: {cleaned}")
        comment = self._to_comment(cleaned, node)
        if author.strip() and not comment.author:
            comment.author = author.strip()
        return comment

    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        """List changed items; closed means is_closed on the group."""
        watermark = _parse_ts(since) if since else None
        items = [self._to_item(row) for row in self._board_items()]
        items.sort(key=lambda item: item.changed)
        if watermark is None:
            return items
        out: list[WorkItem] = []
        for item in items:
            changed = _parse_ts(item.changed)
            if changed is not None and changed > watermark:
                out.append(item)
        return out


MondayBoardAdapter = MondayAdapter
