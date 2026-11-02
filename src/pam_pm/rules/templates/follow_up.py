"""Follow-up on close template: DM the next owner in the chain."""

from __future__ import annotations

from typing import Any

from pam_pm.adapters.pm.base import is_closed
from pam_pm.agents.interviewer import normalize_followup_spacing


def close_via_adapter(board: Any, item_id: str) -> Any:
    """Close one item through the adapter mapped state when available."""
    close_item = getattr(board, "close_item", None)
    if callable(close_item):
        return close_item(item_id)
    return board.update_item(item_id, {"state": "Closed"})

FORWARD_RELS = {
    "Dependency-Forward",
    "System.LinkTypes.Dependency-Forward",
    "Successor",
    "Forward",
}

REVERSE_RELS = {
    "Dependency-Reverse",
    "System.LinkTypes.Dependency-Reverse",
    "Predecessor",
    "Reverse",
}


def _item_id(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("id", item.get("item_id", "")) or "")
    return str(getattr(item, "id", getattr(item, "item_id", "")) or "")


def _item_state(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("state", "") or "")
    return str(getattr(item, "state", "") or "")


def _item_title(item: Any) -> str:
    if isinstance(item, dict):
        title = item.get("title", "")
        return str(title or _item_id(item))
    title = getattr(item, "title", "")
    return str(title or _item_id(item))


OWNER_KEYS = (
    "assigned_to",
    "owner",
    "System.AssignedTo",
    "user_id",
    "assignee",
)


def _owner_text(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, dict):
        for key in ("displayName", "uniqueName", "name", "id"):
            cand = value.get(key)
            if cand:
                return str(cand)
        return ""
    return str(value)


def _owner_of(item: Any) -> str:
    if isinstance(item, dict):
        fields = item.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}
        for key in OWNER_KEYS:
            value = _owner_text(fields.get(key)) or _owner_text(item.get(key))
            if value:
                return value
        return ""
    fields = getattr(item, "fields", None)
    if not isinstance(fields, dict):
        fields = {}
    for key in OWNER_KEYS:
        value = _owner_text(fields.get(key))
        if value:
            return value
    return ""


def _link_endpoints(link: Any) -> tuple[str, str, str]:
    if isinstance(link, dict):
        return (
            str(link.get("source_id", "") or ""),
            str(link.get("target_id", "") or ""),
            str(link.get("rel", "") or ""),
        )
    return (
        str(getattr(link, "source_id", "") or ""),
        str(getattr(link, "target_id", "") or ""),
        str(getattr(link, "rel", "") or ""),
    )


def successor_ids(item_id: str, links: list[Any]) -> list[str]:
    """Return successor item ids for a predecessor item id."""
    found: list[str] = []
    for link in links:
        source, target, rel = _link_endpoints(link)
        if source == item_id and rel in FORWARD_RELS and target:
            if target not in found:
                found.append(target)
        elif target == item_id and rel in REVERSE_RELS and source and source not in found:
            found.append(source)
    return found


def on_item_done(
    item: Any,
    chat: Any,
    board: Any,
    *,
    message: str | None = None,
    db: Any = None,
    backend: str = "azure_devops",
    project: str = "default",
) -> list[str]:
    """DM each successor owner when an item closes. Else no-op."""
    resolved = item
    if isinstance(item, str):
        try:
            resolved = board.get_item(item)
        except Exception:  # noqa: BLE001
            return []
    if not is_closed(_item_state(resolved)):
        return []
    done_id = _item_id(resolved)
    if not done_id:
        return []
    if db is not None:
        try:
            from pam_pm.core import raid as raid_mod

            raid_mod.close_issues_for_board_item(
                db,
                str(backend or "azure_devops"),
                str(project or "default"),
                done_id,
            )
        except Exception:  # noqa: BLE001, S110
            pass
    try:
        links = list(board.list_links(done_id))
    except Exception:  # noqa: BLE001
        return []
    next_ids = successor_ids(done_id, links)
    if not next_ids:
        return []
    done_title = _item_title(resolved)
    sent: list[str] = []
    for succ_id in next_ids:
        try:
            succ = board.get_item(succ_id)
        except Exception:  # noqa: BLE001, S112
            continue
        owner = _owner_of(succ)
        if not owner:
            continue
        succ_title = _item_title(succ)
        text = normalize_followup_spacing(message) if message else (
            f"'{done_title}' is done. What is the status of '{succ_title}'?"
        )
        try:
            mid = chat.send_dm(owner, text)
        except Exception:  # noqa: BLE001, S112
            continue
        sent.append(str(mid))
    return sent


def follow_up_on_close(
    item: Any, chat: Any, board: Any, **kwargs: Any
) -> list[str]:
    """Alias for on_item_done."""
    return on_item_done(item, chat, board, **kwargs)


def run_follow_up(
    item: Any = None, chat: Any = None, board: Any = None, **kwargs: Any
) -> list[str]:
    """Alias for on_item_done kept for older imports."""
    if item is None or chat is None or board is None:
        return []
    return on_item_done(item, chat, board, **kwargs)
