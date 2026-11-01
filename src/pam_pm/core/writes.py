"""Write safety layer: dry-run default, evidence log, undo (F5)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pam_pm.core import store
from pam_pm.core.schemas import WriteRecord

EntryType = WriteRecord | Mapping[str, Any]


def _coerce(entry: EntryType) -> WriteRecord:
    if isinstance(entry, WriteRecord):
        return entry
    if isinstance(entry, Mapping):
        return WriteRecord.model_validate(dict(entry))
    raise TypeError(f"entry must be a WriteRecord or mapping, got {type(entry)}")


class WriteService:
    """Gate for board writes. Dry run is on by default: nothing is applied."""

    def __init__(
        self, db: str | Path | sqlite3.Connection, *, dry_run: bool = True
    ) -> None:
        self._db = db
        self.dry_run = dry_run

    def propose(self, entry: EntryType) -> int:
        """Log a proposed write with its evidence quote, returning its ID."""
        record = _coerce(entry).model_copy(update={"id": None, "status": "proposed"})
        return store.insert_write(self._db, record)

    def apply(self, entry: EntryType | int) -> bool:
        """Apply a write unless dry run is on. Returns True only if applied.

        New records are first logged as proposed so the evidence quote is
        kept either way. Integer entries refer to an existing write ID.
        """
        if isinstance(entry, int):
            if self.dry_run:
                return False
            return store.set_write_status(self._db, entry, "applied")
        record = _coerce(entry)
        write_id = record.id
        if write_id is None:
            record = record.model_copy(update={"status": "proposed"})
            write_id = store.insert_write(self._db, record)
        if self.dry_run:
            return False
        return store.set_write_status(self._db, write_id, "applied")

    def undo(self, write_id: int) -> bool:
        """Mark a write as undone, returning True when a row was updated."""
        return store.undo_write(self._db, write_id)
