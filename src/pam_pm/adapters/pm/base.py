"""PM adapter interface: items, states, iterations, links, comments, polling.

A new backend is one file implementing PMAdapter (see docs/adapters.md).

Canonical done: an item counts as closed when is_closed(state) is True.
That covers Done on Scrum projects, Closed on Agile projects, and Done
on the native board. Matching is case-insensitive with whitespace
stripped. Use is_closed instead of comparing state strings directly.

Canonical item fields (stored in WorkItem.fields by every adapter):
title, state, description, tags, due_date (YYYY-MM-DD), owner (ADO
spells it assigned_to), estimate (original estimate), remaining
(remaining work), iteration_id. Adapters may carry extra backend
specific keys alongside these.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


def is_closed(state: object) -> bool:
    """Return True when a work item state means closed.

    Closed covers Closed on Agile, Done on Scrum, and Done on the
    native board. Matching is case-insensitive.
    """
    return str(state or "").strip().lower() in {"done", "closed"}


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


@dataclass
class Iteration:
    """One sprint with its date range and backend path."""

    id: str
    name: str
    start_date: str = ""
    end_date: str = ""
    path: str = ""


@dataclass
class Comment:
    """One comment on a work item."""

    id: str
    item_id: str
    text: str
    author: str = ""
    created: str = ""


class PMAdapter(ABC):
    """Board reads, writes, states, iterations, links, comments, polling.

    Capability flags tell the UI what a backend can do. Adapters set
    the flags they support to True.
    """

    supports_dependency_links: bool = False
    supports_iterations: bool = False
    supports_comments: bool = False

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
    def delete_item(self, item_id: str) -> None:
        """Delete one item, raising when it is missing."""

    @abstractmethod
    def list_states(self) -> list[str]:
        """List the canonical states for this backend, in order."""

    @abstractmethod
    def close_item(self, item_id: str) -> WorkItem:
        """Move one item to its canonical done state, returning it."""

    @abstractmethod
    def list_iterations(self) -> list[Iteration]:
        """List sprints on this backend, oldest first."""

    @abstractmethod
    def create_iteration(
        self, name: str, start_date: str = "", end_date: str = ""
    ) -> Iteration:
        """Create a sprint with an optional date range, returning it."""

    @abstractmethod
    def set_item_iteration(
        self, item_id: str, iteration_id: str
    ) -> WorkItem:
        """Move one item to a sprint, returning the updated item."""

    @abstractmethod
    def add_link(self, source_id: str, target_id: str, rel: str) -> Link:
        """Add a dependency link, returning it."""

    @abstractmethod
    def list_links(self, item_id: str) -> list[Link]:
        """List dependency links touching one item."""

    @abstractmethod
    def list_comments(self, item_id: str) -> list[Comment]:
        """List comments on one item, oldest first."""

    @abstractmethod
    def add_comment(
        self, item_id: str, text: str, author: str = ""
    ) -> Comment:
        """Add a comment to one item, returning it."""

    @abstractmethod
    def poll_changes(self, since: str | None = None) -> list[WorkItem]:
        """List items changed after a ChangedDate watermark, oldest first."""
