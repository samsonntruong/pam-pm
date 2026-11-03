"""Trello adapter over REST with API key plus token.

Config resolves explicit, then env, then saved wizard values:
TRELLO_BOARD, TRELLO_API_KEY, TRELLO_TOKEN. The key and token are
never accepted as arguments and never appear in errors or logs.
Works headless with a personal token against api.trello.com.

Boards map to projects: cards are scoped to one board. Lists map
to states in list order. Cards carry due dates, members, labels,
checklist and attachment counts. Dependencies ride on card
attachments that link to another card. Comments are card actions.
Change detection polls board actions.
"""

from __future__ import annotations

import logging
import os
import re
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

API_BASE = "https://api.trello.com/1"

CARD_FIELDS = (
    "id,name,desc,due,idList,idMembers,idLabels,labels,"
    "dateLastActivity,shortLink,shortUrl,url,badges"
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

_QUERY_SECRET_RE = re.compile(r"([?&](?:key|token)=)[^&\s]+")


def redact(text: object, *secrets: str) -> str:
    """Redact key and token material from log text."""
    out = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", str(text))
    for val in secrets:
        if val and len(val) >= 4 and val in out:
            out = out.replace(val, "[REDACTED]")
    return out


class _HttpxSecretFilter(logging.Filter):
    """Redact key/token query params from httpx request logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            try:
                text = record.getMessage()
            except Exception:  # noqa: BLE001
                text = str(record.msg)
            record.msg = redact(text)
            record.args = ()
        except Exception:  # noqa: BLE001, S110
            pass
        return True


def _ensure_httpx_redaction() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(f, _HttpxSecretFilter) for f in logger.filters):
        logger.addFilter(_HttpxSecretFilter())


_ensure_httpx_redaction()


def _sleep(seconds: float) -> None:
    """Sleep hook for 429 backoff; monkeypatch in tests."""
    import time

    time.sleep(seconds)


class TrelloError(RuntimeError):
    """Trello call failure with secrets already redacted."""


_CARD_URL_RE = re.compile(r"/c/([^/?#/\s]+)")


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
    if not text or text.lower() == "null":
        return ""
    return text[:10]


def _card_ref_from_url(url: object) -> str:
    match = _CARD_URL_RE.search(str(url or ""))
    return match.group(1).strip() if match else ""


class TrelloAdapter(PMAdapter):
    """PM adapter backed by the Trello REST API."""

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
        saved_key = ""
        saved_token = ""
        try:
            from pam_pm.core import secrets as secrets_mod

            effective = secrets_mod.effective_trello_config(data_dir)
            saved_board = str(effective.get("board", "") or "")
            saved_key = str(effective.get("api_key", "") or "")
            saved_token = str(effective.get("token", "") or "")
        except Exception:  # noqa: BLE001
            saved_board = os.environ.get("TRELLO_BOARD", "")
            saved_key = os.environ.get("TRELLO_API_KEY", "")
            saved_token = os.environ.get("TRELLO_TOKEN", "")
        want_board = (board or "").strip() or saved_board.strip()
        key = saved_key.strip()
        token = saved_token.strip()
        if not want_board:
            raise TrelloError("TRELLO_BOARD is required.")
        if not key:
            raise TrelloError(
                "TRELLO_API_KEY is missing; save it in Setup or set TRELLO_API_KEY."
            )
        if not token:
            raise TrelloError(
                "TRELLO_TOKEN is missing; save it in Setup or set TRELLO_TOKEN."
            )
        self._board_ref = want_board
        self._key = key
        self._token = token
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._board_id: str | None = None
        self._board_url = ""
        self._sleeper = _sleep

    def close(self) -> None:
        self._client.close()

    def _clean(self, text: object) -> str:
        return redact(text, self._key, self._token)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        query: dict[str, Any] = {"key": self._key, "token": self._token}
        for source in (params or {}, payload or {}):
            for name, val in source.items():
                if val is None:
                    continue
                if isinstance(val, list):
                    query[name] = ",".join(str(v) for v in val)
                else:
                    query[name] = val
        url = API_BASE + path
        for attempt in range(5):
            try:
                resp = self._client.request(method, url, params=query)
            except httpx.HTTPError as exc:
                raise TrelloError(self._clean(f"request failed: {exc}")) from exc
            if resp.status_code == 429:
                if attempt >= 4:
                    detail = resp.text[:500]
                    raise TrelloError(
                        self._clean(
                            f"{method} {path} status={resp.status_code}: {detail}"
                        )
                    )
                delay = float(min(2**attempt, 8))
                raw = resp.headers.get("Retry-After", "")
                try:
                    candidate = float(str(raw).strip())
                except (TypeError, ValueError):
                    candidate = -1.0
                if candidate >= 0:
                    delay = max(delay, candidate)
                sleeper = getattr(self, "_sleeper", _sleep)
                sleeper(delay)
                continue
            if resp.status_code == 204 or not resp.text.strip():
                return {}
            if resp.status_code >= 400:
                detail = resp.text[:500]
                raise TrelloError(
                    self._clean(f"{method} {path} status={resp.status_code}: {detail}")
                )
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text[:2000]}
        raise TrelloError(self._clean(f"{method} {path} status=429: rate limited"))

    def _board_id_for_ref(self) -> str:
        if self._board_id:
            return self._board_id
        data = self._request(
            "GET",
            "/members/me/boards",
            {"fields": "id,name,shortLink,shortUrl,url"},
        )
        rows = data if isinstance(data, list) else []
        want = self._board_ref.strip().lower()
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            candidates = [
                str(entry.get("id", "") or ""),
                str(entry.get("shortLink", "") or ""),
                str(entry.get("name", "") or ""),
                str(entry.get("shortUrl", "") or ""),
                str(entry.get("url", "") or ""),
            ]
            if any(text.strip().lower() == want for text in candidates if text):
                self._board_id = str(entry.get("id", ""))
                self._board_url = str(
                    entry.get("shortUrl", "") or entry.get("url", "") or ""
                )
                return self._board_id
        raise TrelloError(f"unknown Trello board: {self._board_ref}")

    def _open_lists(self) -> list[dict[str, Any]]:
        bid = self._board_id_for_ref()
        data = self._request(
            "GET",
            f"/boards/{quote(bid, safe='')}/lists",
            {"fields": "id,name,pos,closed"},
        )
        rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
        out = [r for r in rows if not r.get("closed")]
        try:
            out.sort(key=lambda r: float(r.get("pos", 0) or 0))
        except (TypeError, ValueError):
            out.sort(key=lambda r: str(r.get("name", "")))
        return out

    def _list_id_for_name(self, name: str) -> str:
        want = (name or "").strip().lower()
        if not want:
            raise TrelloError("Trello list name is required")
        for entry in self._open_lists():
            if str(entry.get("name", "")).strip().lower() == want:
                return str(entry.get("id", ""))
        raise TrelloError(f"unknown Trello list: {name}")

    def _members_map(self) -> dict[str, str]:
        bid = self._board_id_for_ref()
        data = self._request(
            "GET",
            f"/boards/{quote(bid, safe='')}/members",
            {"fields": "id,fullName,username"},
        )
        rows = data if isinstance(data, list) else []
        out: dict[str, str] = {}
        for entry in rows:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            name = str(entry.get("fullName", "") or entry.get("username", "") or "")
            out[str(entry["id"])] = name.strip()
        return out

    def _member_id_for_name(self, name: str) -> str:
        want = (name or "").strip().lower()
        if not want:
            return ""
        bid = self._board_id_for_ref()
        data = self._request(
            "GET",
            f"/boards/{quote(bid, safe='')}/members",
            {"fields": "id,fullName,username"},
        )
        rows = data if isinstance(data, list) else []
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            for field in ("fullName", "username"):
                if str(entry.get(field, "") or "").strip().lower() == want:
                    return str(entry.get("id", ""))
        return ""

    def _labels_map(self) -> dict[str, str]:
        bid = self._board_id_for_ref()
        data = self._request(
            "GET",
            f"/boards/{quote(bid, safe='')}/labels",
            {"fields": "id,name,color"},
        )
        rows = data if isinstance(data, list) else []
        out: dict[str, str] = {}
        for entry in rows:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            name = str(entry.get("name", "") or entry.get("color", "") or "")
            out[str(entry["id"])] = name.strip()
        return out

    def _label_ids_for_tags(self, tags: list[str]) -> list[str]:
        labels = self._labels_map()
        by_name = {name.lower(): lid for lid, name in labels.items() if name}
        return [by_name[tag.lower()] for tag in tags if tag.lower() in by_name]

    @staticmethod
    def _split_names(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [
            part
            for part in (p.strip() for p in str(value or "").replace(",", ";").split(";"))
            if part
        ]

    def _board_cards(self) -> list[dict[str, Any]]:
        bid = self._board_id_for_ref()
        data = self._request(
            "GET",
            f"/boards/{quote(bid, safe='')}/cards",
            {"fields": CARD_FIELDS},
        )
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    @classmethod
    def _to_item(
        cls,
        data: dict[str, Any],
        lists_map: dict[str, str],
        members_map: dict[str, str],
        labels_map: dict[str, str],
    ) -> WorkItem:
        list_name = lists_map.get(str(data.get("idList", "") or ""), "")
        member_ids = data.get("idMembers", [])
        if not isinstance(member_ids, list):
            member_ids = []
        members = [
            members_map.get(str(mid), str(mid)) for mid in member_ids if str(mid).strip()
        ]
        owner = members[0] if members else ""
        tags: list[str] = []
        inline = data.get("labels", [])
        if isinstance(inline, list):
            for entry in inline:
                if isinstance(entry, dict) and str(entry.get("name", "") or "").strip():
                    tags.append(str(entry["name"]).strip())
        id_labels = data.get("idLabels", [])
        if isinstance(id_labels, list):
            for lid in id_labels:
                name = labels_map.get(str(lid), "")
                if name and name not in tags:
                    tags.append(name)
        badges = data.get("badges", {})
        if not isinstance(badges, dict):
            badges = {}
        try:
            checklist_count = int(badges.get("checklists", 0) or 0)
        except (TypeError, ValueError):
            checklist_count = 0
        try:
            attachments_count = int(badges.get("attachments", 0) or 0)
        except (TypeError, ValueError):
            attachments_count = 0
        try:
            comments_count = int(badges.get("comments", 0) or 0)
        except (TypeError, ValueError):
            comments_count = 0
        card_id = str(data.get("id", ""))
        item_fields: dict[str, Any] = {
            "title": str(data.get("name", "")),
            "state": list_name,
            "description": str(data.get("desc", "") or ""),
            "tags": tags,
            "due_date": _due_date(data.get("due")),
            "owner": owner,
            "assigned_to": owner,
            "members": members,
            "member_ids": [str(m) for m in member_ids],
            "label_ids": [str(v) for v in id_labels] if isinstance(id_labels, list) else [],
            "estimate": None,
            "remaining": None,
            "iteration_id": "",
            "list_id": str(data.get("idList", "") or ""),
            "short_link": str(data.get("shortLink", "") or ""),
            "short_url": str(data.get("shortUrl", "") or ""),
            "url": str(data.get("shortUrl", "") or data.get("url", "") or ""),
            "checklist_count": checklist_count,
            "attachments_count": attachments_count,
            "comments_count": comments_count,
            "is_closed": is_closed(list_name),
        }
        return WorkItem(
            id=card_id,
            title=str(data.get("name", "")),
            state=list_name,
            changed=str(data.get("dateLastActivity", "") or ""),
            fields=item_fields,
        )

    def _convert(self, data: dict[str, Any]) -> WorkItem:
        lists_map = {str(e.get("id", "")): str(e.get("name", "")) for e in self._open_lists()}
        return self._to_item(data, lists_map, self._members_map(), self._labels_map())

    def _write_payload(self, fields: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, val in fields.items():
            if key == "title":
                out["name"] = val
            elif key == "description":
                out["desc"] = str(val or "")
            elif key == "state":
                name = str(val or "").strip()
                if name:
                    out["idList"] = self._list_id_for_name(name)
            elif key == "tags":
                tags = self._split_names(val)
                out["idLabels"] = self._label_ids_for_tags(tags)
            elif key == "due_date":
                text = str(val or "").strip()
                out["due"] = text if text else "null"
            elif key == "owner" or key == "assigned_to":
                names = self._split_names(val)
                if names:
                    ids = [self._member_id_for_name(n) for n in names]
                    found = [i for i in ids if i]
                    if found:
                        out["idMembers"] = found
                else:
                    out["idMembers"] = []
        return out

    def _require_id(self, item_id: str) -> str:
        cleaned = (item_id or "").strip()
        if not cleaned:
            raise TrelloError("card id is required")
        return cleaned

    def list_items(self, limit: int = 50) -> list[WorkItem]:
        lists_map = {str(e.get("id", "")): str(e.get("name", "")) for e in self._open_lists()}
        members_map = self._members_map()
        labels_map = self._labels_map()
        items = [
            self._to_item(row, lists_map, members_map, labels_map)
            for row in self._board_cards()
        ]
        items.sort(key=lambda item: item.changed, reverse=True)
        return items[:limit]

    def get_item(self, item_id: str) -> WorkItem:
        card_id = self._require_id(item_id)
        data = self._request(
            "GET",
            f"/cards/{quote(card_id, safe='')}",
            {"fields": CARD_FIELDS},
        )
        if not isinstance(data, dict) or not data.get("id"):
            raise TrelloError(f"unknown item: {card_id}")
        return self._convert(data)

    def create_item(self, title: str, fields: dict[str, Any] | None = None) -> WorkItem:
        given = dict(fields or {})
        given.pop("iteration_id", None)
        state = str(given.pop("state", "") or "").strip()
        if state:
            list_id = self._list_id_for_name(state)
        else:
            opened = self._open_lists()
            if not opened:
                raise TrelloError("no open lists on this board")
            list_id = str(opened[0].get("id", ""))
        payload = {"name": title, "idList": list_id, **self._write_payload(given)}
        data = self._request("POST", "/cards", {}, payload)
        if not isinstance(data, dict) or not data.get("id"):
            raise TrelloError("create failed: no card id returned")
        return self.get_item(str(data["id"]))

    def update_item(self, item_id: str, fields: dict[str, Any]) -> WorkItem:
        card_id = self._require_id(item_id)
        given = dict(fields)
        sprint = str(given.pop("iteration_id", "") or "").strip()
        if sprint:
            self.set_item_iteration(card_id, sprint)
        payload = self._write_payload(given)
        if payload:
            self._request("PUT", f"/cards/{quote(card_id, safe='')}", {}, payload)
        return self.get_item(card_id)

    def delete_item(self, item_id: str) -> None:
        card_id = self._require_id(item_id)
        self._request("DELETE", f"/cards/{quote(card_id, safe='')}")

    def list_states(self) -> list[str]:
        """List board lists in list order."""
        return [str(e.get("name", "")) for e in self._open_lists()]

    def close_item(self, item_id: str) -> WorkItem:
        card_id = self._require_id(item_id)
        current = self.get_item(card_id)
        if is_closed(current.state):
            return current
        done_list = ""
        for entry in self._open_lists():
            name = str(entry.get("name", ""))
            if name and is_closed(name):
                done_list = str(entry.get("id", ""))
                break
        if not done_list:
            raise TrelloError("no Done list on this board")
        self._request("PUT", f"/cards/{quote(card_id, safe='')}", {}, {"idList": done_list})
        return self.get_item(card_id)

    def list_iterations(self) -> list[Iteration]:
        """Trello has no sprints, so this is always empty."""
        return []

    def create_iteration(
        self, name: str, start_date: str = "", end_date: str = ""
    ) -> Iteration:
        raise TrelloError("Trello has no sprints; iterations are not supported.")

    def set_item_iteration(self, item_id: str, iteration_id: str) -> WorkItem:
        """Clearing is a no-op; setting raises since Trello has no sprints."""
        if not iteration_id.strip():
            return self.get_item(item_id)
        raise TrelloError("Trello has no sprints; iterations are not supported.")

    @staticmethod
    def _normalize_rel(rel: str) -> str:
        text = (rel or "").strip().lower().replace("_", "-")
        if text in _BLOCKS_ALIASES:
            return REL_BLOCKS
        if text in _BLOCKED_BY_ALIASES:
            return REL_BLOCKED_BY
        raise TrelloError(f"unknown link rel: {rel}")

    def add_link(self, source_id: str, target_id: str, rel: str) -> Link:
        kind = self._normalize_rel(rel)
        source = self._require_id(source_id)
        target = self._require_id(target_id)
        if kind == REL_BLOCKS:
            blocker, blocked = source, target
        else:
            blocker, blocked = target, source
        blocked_card = self.get_item(blocked)
        self.get_item(blocker)
        url = str(blocked_card.fields.get("short_url", "") or "")
        if not url:
            url = f"https://trello.com/c/{blocked}"
        self._request(
            "POST",
            f"/cards/{quote(blocker, safe='')}/attachments",
            {},
            {"url": url, "name": f"blocks {blocked}"},
        )
        return Link(source_id=source, target_id=target, rel=kind)

    def _known_cards(self) -> tuple[dict[str, str], dict[str, str]]:
        by_ref: dict[str, str] = {}
        short_of: dict[str, str] = {}
        for row in self._board_cards():
            cid = str(row.get("id", ""))
            short = str(row.get("shortLink", "") or "")
            if not cid:
                continue
            by_ref[cid] = cid
            short_of[cid] = short
            if short:
                by_ref[short] = cid
        return by_ref, short_of

    def list_links(self, item_id: str) -> list[Link]:
        card_id = self._require_id(item_id)
        by_ref, short_of = self._known_cards()
        if card_id not in by_ref and card_id not in short_of:
            self.get_item(card_id)
            by_ref, short_of = self._known_cards()
        mine = short_of.get(card_id, "")
        refs = {card_id}
        if mine:
            refs.add(mine)
        out: list[Link] = []
        seen: set[tuple[str, str, str]] = set()

        def _targets(rows: Any) -> list[str]:
            found: list[str] = []
            for entry in rows if isinstance(rows, list) else []:
                if not isinstance(entry, dict):
                    continue
                ref = _card_ref_from_url(entry.get("url", ""))
                if ref and ref in by_ref:
                    found.append(by_ref[ref])
            return found

        mine_rows = self._request("GET", f"/cards/{quote(card_id, safe='')}/attachments")
        for other in _targets(mine_rows):
            if other == card_id:
                continue
            key = (card_id, other, REL_BLOCKS)
            if key not in seen:
                seen.add(key)
                out.append(Link(source_id=card_id, target_id=other, rel=REL_BLOCKS))
        bid = self._board_id_for_ref()
        actions = self._request(
            "GET",
            f"/boards/{quote(bid, safe='')}/actions",
            {"filter": "addAttachmentToCard", "limit": 1000},
        )
        canonical = by_ref.get(card_id, card_id)
        blockers: set[str] = set()
        for action in actions if isinstance(actions, list) else []:
            if not isinstance(action, dict):
                continue
            inner = action.get("data", {})
            if not isinstance(inner, dict):
                continue
            card = inner.get("card", {})
            att = inner.get("attachment", {})
            if not isinstance(card, dict) or not isinstance(att, dict):
                continue
            raw_other = str(card.get("id", "") or "")
            if not raw_other:
                raw_other = str(card.get("shortLink", "") or "")
            if not raw_other:
                continue
            other_id = by_ref.get(raw_other, raw_other)
            if other_id == card_id or other_id == canonical:
                continue
            if other_id not in short_of:
                continue
            ref = _card_ref_from_url(att.get("url", ""))
            if not ref:
                continue
            if ref in refs or by_ref.get(ref, ref) == canonical:
                blockers.add(other_id)
        for other_id in sorted(blockers):
            key = (card_id, other_id, REL_BLOCKED_BY)
            if key not in seen:
                seen.add(key)
                out.append(
                    Link(source_id=card_id, target_id=other_id, rel=REL_BLOCKED_BY)
                )
        return out

    @staticmethod
    def _to_comment(item_id: str, data: dict[str, Any]) -> Comment:
        creator = data.get("memberCreator", {})
        name = ""
        if isinstance(creator, dict):
            name = str(creator.get("fullName", "") or creator.get("username", "") or "")
        inner = data.get("data", {})
        text = ""
        if isinstance(inner, dict):
            text = str(inner.get("text", "") or "")
        return Comment(
            id=str(data.get("id", "")),
            item_id=item_id,
            text=text,
            author=name,
            created=str(data.get("date", "") or ""),
        )

    def list_comments(self, item_id: str) -> list[Comment]:
        card_id = self._require_id(item_id)
        data = self._request(
            "GET",
            f"/cards/{quote(card_id, safe='')}/actions",
            {"filter": "commentCard"},
        )
        rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
        comments = [self._to_comment(card_id, r) for r in rows]
        comments.sort(key=lambda c: c.created)
        return comments

    def add_comment(self, item_id: str, text: str, author: str = "") -> Comment:
        card_id = self._require_id(item_id)
        body = text.strip()
        if not body:
            raise TrelloError("comment text is required")
        data = self._request(
            "POST",
            f"/cards/{quote(card_id, safe='')}/actions/comments",
            {},
            {"text": body},
        )
        if not isinstance(data, dict) or not data.get("id"):
            raise TrelloError(f"add comment failed for item: {card_id}")
        comment = self._to_comment(card_id, data)
        if author.strip() and not comment.author:
            comment.author = author.strip()
        return comment

    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        """List changed cards via board actions; closed means is_closed."""
        bid = self._board_id_for_ref()
        watermark = _parse_ts(since) if since else None
        params: dict[str, Any] = {
            "filter": "createCard,updateCard,commentCard",
            "limit": 1000,
        }
        if watermark is not None:
            params["since"] = watermark.isoformat()
        data = self._request(
            "GET", f"/boards/{quote(bid, safe='')}/actions", params
        )
        rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
        ordered_ids: list[str] = []
        for entry in sorted(rows, key=lambda r: str(r.get("date", "") or "")):
            inner = entry.get("data", {})
            card = inner.get("card", {}) if isinstance(inner, dict) else {}
            cid = str(card.get("id", "") or "") if isinstance(card, dict) else ""
            if cid and cid not in ordered_ids:
                ordered_ids.append(cid)
        if not ordered_ids and watermark is None:
            items = [
                self._to_item(
                    row,
                    {str(e.get("id", "")): str(e.get("name", "")) for e in self._open_lists()},
                    self._members_map(),
                    self._labels_map(),
                )
                for row in self._board_cards()
            ]
            items.sort(key=lambda item: item.changed)
            return items
        lists_map = {str(e.get("id", "")): str(e.get("name", "")) for e in self._open_lists()}
        members_map = self._members_map()
        labels_map = self._labels_map()
        cards_by_id = {str(r.get("id", "")): r for r in self._board_cards()}
        out: list[WorkItem] = []
        for cid in ordered_ids:
            row = cards_by_id.get(cid)
            if row is None:
                continue
            item = self._to_item(row, lists_map, members_map, labels_map)
            if watermark is None:
                out.append(item)
                continue
            changed = _parse_ts(item.changed)
            if changed is not None and changed > watermark:
                out.append(item)
        out.sort(key=lambda item: item.changed)
        return out


TrelloBoardAdapter = TrelloAdapter
