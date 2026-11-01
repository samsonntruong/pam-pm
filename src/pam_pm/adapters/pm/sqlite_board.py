"""SQLite fake board for tests only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pam_pm.adapters.pm.base import Link, PMAdapter, WorkItem

ITEMS_DDL = """CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT '',
  changed TEXT NOT NULL,
  extra TEXT NOT NULL DEFAULT '{}'
)"""

LINKS_DDL = """CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  rel TEXT NOT NULL
)"""

LINK_RELS = {
    "Dependency-Forward",
    "Dependency-Reverse",
    "System.LinkTypes.Dependency-Forward",
    "System.LinkTypes.Dependency-Reverse",
}


def _now() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class SQLiteBoard(PMAdapter):
    """PM fake over SQLite with a dependency links table.

    The changed column is the ChangedDate watermark used by poll_changes.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        name = ":memory:" if path is None else str(path)
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(name, check_same_thread=False)
        self._conn.execute(ITEMS_DDL)
        self._conn.execute(LINKS_DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_item(row: tuple) -> WorkItem:
        item_id, title, state, changed, extra = row
        try:
            rest = json.loads(extra) if extra else {}
        except ValueError:
            rest = {}
        if not isinstance(rest, dict):
            rest = {}
        fields: dict[str, Any] = dict(rest)
        fields.setdefault("title", title)
        fields.setdefault("state", state)
        return WorkItem(
            id=str(item_id),
            title=title,
            state=state,
            changed=changed,
            fields=fields,
        )

    def list_items(self, limit: int = 50) -> list[WorkItem]:
        rows = self._conn.execute(
            "SELECT id, title, state, changed, extra FROM items"
            " ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def get_item(self, item_id: str) -> WorkItem:
        row = self._conn.execute(
            "SELECT id, title, state, changed, extra FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown item: {item_id}")
        return self._row_to_item(row)

    def create_item(
        self, title: str, fields: dict[str, Any] | None = None
    ) -> WorkItem:
        given = dict(fields or {})
        state = str(given.pop("state", ""))
        changed = _now()
        cur = self._conn.execute(
            "INSERT INTO items (title, state, changed, extra) VALUES (?, ?, ?, ?)",
            (title, state, changed, json.dumps(given)),
        )
        self._conn.commit()
        return self.get_item(str(cur.lastrowid))

    def update_item(
        self, item_id: str, fields: dict[str, Any]
    ) -> WorkItem:
        current = self.get_item(item_id)
        given = dict(fields)
        title = str(given.pop("title", current.title))
        state = str(given.pop("state", current.state))
        rest = {
            key: val
            for key, val in current.fields.items()
            if key not in ("title", "state")
        }
        rest.update(given)
        changed = _now()
        self._conn.execute(
            "UPDATE items SET title = ?, state = ?, changed = ?, extra = ?"
            " WHERE id = ?",
            (title, state, changed, json.dumps(rest), item_id),
        )
        self._conn.commit()
        return self.get_item(item_id)

    def add_link(self, source_id: str, target_id: str, rel: str) -> Link:
        if rel not in LINK_RELS:
            raise ValueError(f"unknown link rel: {rel}")
        self.get_item(source_id)
        self.get_item(target_id)
        self._conn.execute(
            "INSERT INTO links (source_id, target_id, rel) VALUES (?, ?, ?)",
            (source_id, target_id, rel),
        )
        self._conn.commit()
        return Link(source_id=source_id, target_id=target_id, rel=rel)

    def list_links(self, item_id: str) -> list[Link]:
        rows = self._conn.execute(
            "SELECT source_id, target_id, rel FROM links"
            " WHERE source_id = ? OR target_id = ? ORDER BY id ASC",
            (item_id, item_id),
        ).fetchall()
        return [
            Link(source_id=row[0], target_id=row[1], rel=row[2]) for row in rows
        ]

    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        if since:
            rows = self._conn.execute(
                "SELECT id, title, state, changed, extra FROM items"
                " WHERE changed > ? ORDER BY changed ASC, id ASC",
                (since,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, title, state, changed, extra FROM items"
                " ORDER BY changed ASC, id ASC"
            ).fetchall()
        return [self._row_to_item(row) for row in rows]


SqliteBoardAdapter = SQLiteBoard
