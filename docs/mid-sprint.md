# Mid-sprint adjustment

Triggered by the rules engine (a midday schedule check plus a board
change trigger) when a sprint is off track. The check is plain code and
costs nothing.

## Triggers

- Stalled blocker: a sprint item in Blocked state, or waiting on an
  open predecessor, with no change in N days (default 3).
- Falling behind: a person holding more sprint hours than their
  capacity, or more open sprint items than the threshold (default 4).
- Urgent scope: a sprint item with high priority or an `urgent` tag.
- Surplus capacity: sprint load below half of roster capacity, or
  someone with 4h or more free, while the backlog waits.

## Output

One optional Slack post and one console card at `/mid-sprint`, headed
"N changes proposed". Each change is move out, swap, reassign, or pull
from backlog, with a reason, evidence, and one sentence on the effect
on the sprint goal. The goal is optional free text on the card and
defaults to finishing the planned scope.

Approve buttons only; Pam never changes the sprint on its own. Dry run
is on by default: Approve proposes the write with an evidence quote
first, and applies it only when dry run is off. Undo works from the
Activity screen like other writes.

## Wiring

`register_mid_sprint` binds the schedule plus board change triggers and
`schedule_mid_sprint` registers the midday job (persisted for
catch-up). Proposal helpers reuse the next-sprint module
(`carryover_items`, `ordered_backlog`, `predecessor_map`, owner and
estimate readers). Quality notes for the card live in
`docs/design/MID_SPRINT_WP4.md`.
