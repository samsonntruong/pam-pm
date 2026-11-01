"""PM adapter interface: items, links, polling."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkItem:
    """One board work item with its change watermark."""

    id: str
    title: str
    state: str = ""
    changed: str = ""
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class Link:
    """One directed dependency link between two items."""

    source_id: str
    target_id: str
    rel: str


class PMAdapter(ABC):
    """Board reads, writes, dependency links, and change polling."""

    @abstractmethod
    def list_items(self, limit: int = 50) -> list[WorkItem]:
        """List board items, newest or by id depending on backend."""

    @abstractmethod
    def get_item(self, item_id: str) -> WorkItem:
        """Fetch one item by id, raising when it is missing."""

    @abstractmethod
    def update_item(
        self, item_id: str, fields: dict[str, Any]
    ) -> WorkItem:
        """Update fields on one item, returning the updated item."""

    @abstractmethod
    def create_item(
        self, title: str, fields: dict[str, Any] | None = None
    ) -> WorkItem:
        """Create one item, returning it with its new id."""

    @abstractmethod
    def add_link(self, source_id: str, target_id: str, rel: str) -> Link:
        """Add a dependency link, returning it."""

    @abstractmethod
    def list_links(self, item_id: str) -> list[Link]:
        """List dependency links touching one item."""

    @abstractmethod
    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        """List items changed after a ChangedDate watermark, oldest first."""
