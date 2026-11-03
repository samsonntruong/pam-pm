# Features

Everything Pam does, in one list. The README links here for the full detail behind each line.

## Daily flow

- Morning check-ins in Slack DM: one question at a time, with pushback on vague answers until remaining work and an ETA are stated.
- Board sync: status updates, comments, and dependency links written with a quoted evidence line behind each write. Works with the Pam board, Azure DevOps, Jira, Linear, Trello, ClickUp, Asana, and Monday.
- Blocker intake: asks what is blocked, finds or creates the blocking item, links it with a Predecessor/Successor dependency, asks the owner for an ETA, and notifies the waiting person when it clears.

## Planning

- Sticky-note photo to sprint: SAM 3.1 segmentation with a Muse Spark vision read as fallback, proposed sprint, Approve to create items. See [setup.md](setup.md) for the provider limits.
- Kickoff from a document: paste a brief or upload a file, review owner suggestions with reasons on one approval card, Approve to create items. See [kickoff.md](kickoff.md).
- Next-sprint proposal: retro plus carryover plus backlog with owners, load, and risk on one approval card. Approve creates the next sprint. See [next-sprint.md](next-sprint.md).
- Chat add-task: mention Pam with `add a task: ...` to draft one task with owner and estimate; Approve creates it. See [add-task.md](add-task.md).

## Oversight

- Sprint review and retro: per-item status synthesized into a review, retro actions created tagged `retro-action`.
- RAID log: risks, assumptions, issues, and dependencies with propose plus accept, manual add and edit, and CSV plus Markdown export. See [raid.md](raid.md).
- Board triage: daily scan for stale, unassigned, and blocked items with proposed fixes on one approval card. See [triage.md](triage.md).
- Mid-sprint adjustment: proposes move out, swap, reassign, or pull from backlog when the sprint is off track, with the effect on the goal. See [mid-sprint.md](mid-sprint.md).

## Meetings and access

- Listen meetings (beta): capture audio in Chrome or Edge at `/listen`, review finals at `/meetings`, generate notes and approve action items from the card. See [listen.md](listen.md).
- Provider picker: Meta by default plus a custom OpenAI-compatible endpoint. See [provider.md](provider.md).
- Desktop wrapper (beta): `pam desktop` opens Pam in its own window with a tray icon. See [desktop.md](desktop.md).
- Always-on resilience: autostart at sign-in, exactly-once delivery after restarts, scheduler catch-up for missed runs. See [install.md](install.md).
