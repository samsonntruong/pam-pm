# RAID log

Risks, Assumptions, Issues, Dependencies. One log per backend and project, stored in SQLite keyed by (backend, external_id).

## Flow

1. Pam proposes entries with an evidence quote: check-in risks, kickoff assumptions, blocker issues, board link dependencies.
2. Proposals stay proposed until the lead presses Accept on the RAID page.
3. Manual add and edit always work and are logged with evidence.
4. Close marks work done. Closing a board item closes its linked open issues. Delete removes an entry after a confirm step.
5. Undo reverses proposal writes: an accepted entry goes back to proposed, a still proposed entry is removed.

## Console

Open `/raid` for four tabs (Risks, Assumptions, Issues, Dependencies) with counts, add, inline edit, accept, close, delete, and CSV plus Markdown export. Kickoff shows how many assumptions were proposed and links to this page. Activity shows human verbs for each RAID operation. Exports from the console are the lead view with full quotes; non-lead exports show Lead only instead of quotes.

## Rules

Morning nudges cover open issues and high or critical risks per owner. The review appends new plus closed RAID items. The triage rule flags entries with no owner or past due dates.
