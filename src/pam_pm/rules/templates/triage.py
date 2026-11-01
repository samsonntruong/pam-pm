"""Board triage helper for RAID items (ADDONS item 2 hook).

Flags RAID entries with no owner or past due. The full scheduled
board triage rule (ADDONS item 3) will call this next to its board
scan; the RAID console shows the same flags in a Needs attention card.
"""

from __future__ import annotations

from typing import Any


def format_triage_message(flags: dict[str, list[dict[str, Any]]]) -> str:
    """Render no-owner and past-due RAID flags as one chat message."""
    no_owner = flags.get("no_owner", [])
    past_due = flags.get("past_due", [])
    if not no_owner and not past_due:
        return "RAID triage: nothing needs attention."
    lines = ["RAID triage: items need attention.", ""]
    if no_owner:
        lines.append("No owner:")
        for item in no_owner:
            lines.append(f"- {item.get('title', '')} [{item.get('kind', '')}]")
        lines.append("")
    if past_due:
        lines.append("Past due:")
        for item in past_due:
            lines.append(
                f"- {item.get('title', '')} (due {item.get('due_date', '')})"
            )
    return "\n".join(lines).rstrip() + "\n"


def triage_raid(
    db: Any,
    backend: str = "azure_devops",
    project: str = "default",
    chat: Any = None,
    lead: str = "",
    today: str = "",
) -> dict[str, Any]:
    """Flag ownerless and overdue RAID items, DMing the lead when asked.

    Returns the flags plus the message text. A DM goes out only when a
    chat adapter and lead id are given and at least one item is flagged.
    """
    from pam_pm.core import raid as raid_mod

    flags = raid_mod.triage_flags(
        db,
        str(backend or "azure_devops"),
        str(project or "default"),
        today=today,
    )
    message = format_triage_message(flags)
    sent = False
    if chat is not None and lead and (flags["no_owner"] or flags["past_due"]):
        try:
            chat.send_dm(str(lead), message)
            sent = True
        except Exception:  # noqa: BLE001
            sent = False
    return {
        "no_owner": flags["no_owner"],
        "past_due": flags["past_due"],
        "message": message,
        "sent": sent,
    }
