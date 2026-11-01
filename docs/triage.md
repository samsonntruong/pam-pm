# Board triage

One daily scan plus a board change trigger. The scan is plain code and
costs nothing. The model runs only when the board changed since the
last scan and a title needs judgment.

## Detectors

- Stale: no change in N days (default 3) while in progress.
- Unassigned: open with no owner. The fix suggests the least loaded
  roster member.
- Missing estimate: open with no estimate. The fix sets 4h as a start.
- Overloaded: more open items than the threshold (default 5). The fix
  reassigns the oldest item to someone with room.
- Blocked predecessor: blocked by a closed item (fix: move back to the
  open state) or by a missing item (fix: nudge comment to relink).
- Unclear title: fewer than three words or a generic label. Only this
  detector needs the model, which suggests a clearer title.
- RAID flags via the existing hook: entries with no owner or past due.

## Output

One Slack post and one console card at `/triage`, both headed "N items
need attention". Each finding shows its proposed fix with an Approve
button. Dry run is on by default: Approve proposes the write with an
evidence quote first, and applies it only when dry run is off. Undo
works from the Activity screen like other writes.

## Wiring

`schedule_triage` registers the daily job (persisted for catch-up) and
`register_triage` binds the schedule plus board change triggers. The
Rules screen toggles the template per project. Quality notes for the
card live in `docs/design/TRIAGE_WP4.md`.
