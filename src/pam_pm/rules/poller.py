"""ADO change poller with a stored ChangedDate watermark."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

POLL_INTERVAL = 60

POLL_INTERVAL_KEY = "settings.poll_interval"


def effective_poll_interval(db: Any = None) -> int:
    """Return the poll interval in seconds, default 60.

    A console saved value wins, then Config, then POLL_INTERVAL.
    """
    if db is not None:
        try:
            from pam_pm.core import recovery as recovery_mod

            raw = recovery_mod.kv_get(db, POLL_INTERVAL_KEY)
            if str(raw or "").strip():
                return max(1, int(str(raw).strip()))
        except (TypeError, ValueError):
            pass
        except Exception:  # noqa: BLE001, S110
            pass
    try:
        from pam_pm.core.config import Config

        return max(1, int(Config().poll_interval))
    except Exception:  # noqa: BLE001
        return POLL_INTERVAL


POLLER_DDL = """CREATE TABLE IF NOT EXISTS poller_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
)"""


def build_changed_wiql(since: str | None) -> str:
    """Return WIQL ordered by ChangedDate ascending."""
    if since:
        tick = since.replace("'", "''").split("T")[0]
        return (
            "SELECT [System.Id] FROM WorkItems"
            f" WHERE [System.ChangedDate] >= '{tick}'"
            " ORDER BY [System.ChangedDate] ASC"
        )
    return (
        "SELECT [System.Id] FROM WorkItems"
        " WHERE [System.ChangedDate] >= @Today - 30"
        " ORDER BY [System.ChangedDate] ASC"
    )


def _changed_of(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("changed", "ChangedDate", "changed_date"):
            value = item.get(key)
            if value:
                return str(value)
        fields = item.get("fields", {})
        if isinstance(fields, dict):
            for key in ("System.ChangedDate", "changed"):
                value = fields.get(key)
                if value:
                    return str(value)
        return ""
    value = getattr(item, "changed", "")
    if value:
        return str(value)
    fields = getattr(item, "fields", None)
    if isinstance(fields, dict):
        cand = fields.get("System.ChangedDate", "")
        if cand:
            return str(cand)
    return ""


class ADOPoller:
    """Poll board changes by ChangedDate, storing the watermark in SQLite.

    Each change is yielded only once: the watermark advances to the max
    ChangedDate seen, and the next poll asks only for newer rows.
    """

    def __init__(
        self,
        board: Any,
        db: str | Path | sqlite3.Connection,
        *,
        project: str = "default",
        interval: int | None = None,
    ) -> None:
        self.board = board
        self._db = db
        self.project = project
        self._interval_override = interval
        if interval is None:
            self.interval = effective_poll_interval(db)
        else:
            self.interval = interval
        self._key = f"watermark:{project}"

    def _connect(self) -> sqlite3.Connection:
        if isinstance(self._db, sqlite3.Connection):
            return self._db
        path = str(self._db)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(path)

    def _close(self, conn: sqlite3.Connection) -> None:
        if not isinstance(self._db, sqlite3.Connection):
            conn.commit()
            conn.close()
        else:
            conn.commit()

    def get_watermark(self) -> str | None:
        conn = self._connect()
        try:
            conn.execute(POLLER_DDL)
            row = conn.execute(
                "SELECT value FROM poller_state WHERE key = ?", (self._key,)
            ).fetchone()
        finally:
            self._close(conn)
        if row is None or not row[0]:
            return None
        return str(row[0])

    def set_watermark(self, value: str) -> None:
        conn = self._connect()
        try:
            conn.execute(POLLER_DDL)
            conn.execute(
                "INSERT INTO poller_state (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self._key, str(value)),
            )
        finally:
            self._close(conn)

    def build_wiql(self, since: str | None = None) -> str:
        mark = since if since is not None else self.get_watermark()
        return build_changed_wiql(mark)

    def poll_once(self) -> list[Any]:
        since = self.get_watermark()
        items = list(self.board.poll_changes(since))
        fresh: list[Any] = []
        for item in items:
            changed = _changed_of(item)
            if since and changed and changed <= since:
                continue
            fresh.append(item)
        if fresh:
            marks = [_changed_of(item) for item in fresh]
            marks = [mark for mark in marks if mark]
            if marks:
                self.set_watermark(max(marks))
            elif since is None:
                self.set_watermark("")
        return fresh

    def poll(
        self, max_rounds: int | None = None, sleep: bool = True
    ) -> Iterator[list[Any]]:
        rounds = 0
        while max_rounds is None or rounds < max_rounds:
            yield self.poll_once()
            rounds += 1
            if max_rounds is not None and rounds >= max_rounds:
                break
            if self._interval_override is None:
                self.interval = effective_poll_interval(self._db)
            if sleep and self.interval > 0:
                time.sleep(self.interval)


Poller = ADOPoller
