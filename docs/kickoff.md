# Kickoff from a document

Start a sprint from a written brief instead of a blank board. Open `/kickoff`, paste the brief text or upload the file, and Pam drafts the sprint for approval.

## Flow

1. Paste: drop the brief into the text box or upload the file.
2. Propose: Pam splits the brief into candidate items, each with a title, type, owner suggestion with a reason, and estimate.
3. Review: one approval card shows every item with its owner reason. Owner order: names in the text win, then skill match, then load against capacity, else Unassigned flagged as needing an owner.
4. Approve: Approve creates the items on the current backend. Dry run is on by default: Approve proposes the writes with evidence quotes first, and applies them only when dry run is off. Undo works from the Activity screen like other writes.

## Assumptions

Phrases that sound like assumptions ("should handle", "probably", "assume") are proposed to the RAID log as assumptions with evidence quotes. The kickoff card says how many were proposed and links to the RAID page, where the lead presses Accept. See [raid.md](raid.md).

## Owner suggestions

Each item shows why its owner was picked: named in the brief, skill match from past items, or least loaded against capacity. Items with no good match show Unassigned with a warning instead of a guess.
