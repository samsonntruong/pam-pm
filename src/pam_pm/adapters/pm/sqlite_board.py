"""NativeBoard: Pam's own editable SQLite board.

Real adapter, not a test fake. Items carry the canonical fields
(title, state, description, tags, due_date, owner, estimate,
remaining, iteration_id); states are New, Active, Blocked, Done with
Done as the canonical done state (see is_closed). The changed column
is the ChangedDate watermark used by poll_changes.

Human edits made through this adapter are logged as human actions by
the caller, not here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pam_pm.adapters.pm.base import Comment, Iteration, Link, PMAdapter, WorkItem

NATIVE_STATES = ["New", "Active", "Blocked", "Done"]

NATIVE_DONE_STATE = "Done"

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

ITERATIONS_DDL = """CREATE TABLE IF NOT EXISTS iterations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  start_date TEXT NOT NULL DEFAULT '',
  end_date TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT ''
)"""

COMMENTS_DDL = """CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL,
  text TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL
)"""

LINK_RELS = {
    "Dependency-Forward",
    "Dependency-Reverse",
    "System.LinkTypes.Dependency-Forward",
    "System.LinkTypes.Dependency-Reverse",
    "Hierarchy-Forward",
    "Hierarchy-Reverse",
    "System.LinkTypes.Hierarchy-Forward",
    "System.LinkTypes.Hierarchy-Reverse",
}


def _now() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class NativeBoard(PMAdapter):
    """Editable board over SQLite with iterations, links, and comments."""

    supports_dependency_links: bool = True
    supports_iterations: bool = True
    supports_comments: bool = True
    supports_close_iteration: bool = True

    def __init__(self, path: str | Path | None = None) -> None:
        name = ":memory:" if path is None else str(path)
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(name, check_same_thread=False)
        self._conn.execute(ITEMS_DDL)
        self._conn.execute(LINKS_DDL)
        self._conn.execute(ITERATIONS_DDL)
        self._conn.execute(COMMENTS_DDL)
        self._ensure_iteration_status()
        self._conn.commit()

    def _ensure_iteration_status(self) -> None:
        cols = [
            row[1]
            for row in self._conn.execute(
                "PRAGMA table_info(iterations)"
            ).fetchall()
        ]
        if "status" not in cols:
            self._conn.execute(
                "ALTER TABLE iterations ADD COLUMN status"
                " TEXT NOT NULL DEFAULT ''"
            )

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
        if "owner" in fields and "assigned_to" not in fields:
            fields["assigned_to"] = fields["owner"]
        if "assigned_to" in fields and "owner" not in fields:
            fields["owner"] = fields["assigned_to"]
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

    def delete_item(self, item_id: str) -> None:
        self.get_item(item_id)
        self._conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self._conn.execute(
            "DELETE FROM links WHERE source_id = ? OR target_id = ?",
            (item_id, item_id),
        )
        self._conn.execute(
            "DELETE FROM comments WHERE item_id = ?", (item_id,)
        )
        self._conn.commit()

    def list_states(self) -> list[str]:
        return list(NATIVE_STATES)

    def close_item(self, item_id: str) -> WorkItem:
        return self.update_item(item_id, {"state": NATIVE_DONE_STATE})

    def list_iterations(self) -> list[Iteration]:
        rows = self._conn.execute(
            "SELECT id, name, start_date, end_date FROM iterations ORDER BY id ASC"
        ).fetchall()
        return [
            Iteration(
                id=str(row[0]),
                name=row[1],
                start_date=row[2] or "",
                end_date=row[3] or "",
                path=row[1],
            )
            for row in rows
        ]

    def create_iteration(
        self, name: str, start_date: str = "", end_date: str = ""
    ) -> Iteration:
        label = name.strip()
        if not label:
            raise ValueError("iteration name is required")
        cur = self._conn.execute(
            "INSERT INTO iterations (name, start_date, end_date)"
            " VALUES (?, ?, ?)",
            (label, start_date.strip(), end_date.strip()),
        )
        self._conn.commit()
        created = self._conn.execute(
            "SELECT id, name, start_date, end_date FROM iterations WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        assert created is not None
        return Iteration(
            id=str(created[0]),
            name=created[1],
            start_date=created[2] or "",
            end_date=created[3] or "",
            path=created[1],
        )

    def close_iteration(self, iteration_id: str) -> Iteration:
        row = self._conn.execute(
            "SELECT id, name, start_date, end_date FROM iterations WHERE id = ?",
            (iteration_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown iteration: {iteration_id}")
        self._conn.execute(
            "UPDATE iterations SET status = 'closed' WHERE id = ?",
            (iteration_id,),
        )
        self._conn.commit()
        return Iteration(
            id=str(row[0]),
            name=row[1],
            start_date=row[2] or "",
            end_date=row[3] or "",
            path=row[1],
        )

    def closed_iteration_ids(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT id FROM iterations WHERE status = 'closed'"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def set_item_iteration(
        self, item_id: str, iteration_id: str
    ) -> WorkItem:
        if iteration_id:
            row = self._conn.execute(
                "SELECT id FROM iterations WHERE id = ?", (iteration_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown iteration: {iteration_id}")
        return self.update_item(item_id, {"iteration_id": iteration_id})

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

    def list_comments(self, item_id: str) -> list[Comment]:
        self.get_item(item_id)
        rows = self._conn.execute(
            "SELECT id, item_id, text, author, created FROM comments"
            " WHERE item_id = ? ORDER BY id ASC",
            (item_id,),
        ).fetchall()
        return [
            Comment(
                id=str(row[0]),
                item_id=str(row[1]),
                text=row[2],
                author=row[3] or "",
                created=row[4] or "",
            )
            for row in rows
        ]

    def add_comment(
        self, item_id: str, text: str, author: str = ""
    ) -> Comment:
        self.get_item(item_id)
        body = text.strip()
        if not body:
            raise ValueError("comment text is required")
        created = _now()
        cur = self._conn.execute(
            "INSERT INTO comments (item_id, text, author, created)"
            " VALUES (?, ?, ?, ?)",
            (item_id, body, author.strip(), created),
        )
        self._conn.commit()
        return Comment(
            id=str(cur.lastrowid),
            item_id=item_id,
            text=body,
            author=author.strip(),
            created=created,
        )

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


SQLiteBoard = NativeBoard
SqliteBoardAdapter = NativeBoard
