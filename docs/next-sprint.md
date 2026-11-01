# Next-sprint proposal

Runs as a step after the sprint review, or on demand from the console at `/next-sprint`.

## Inputs

- Carryover: open items in the current sprint, each with a slip reason from its latest comment or state.
- Ordered backlog: open items with no sprint, in board order.
- Dependencies: an item is held out when a predecessor is neither closed nor already selected.
- Throughput plus capacity: closed hours per owner from past sprints, plus roster capacity fields.
- Retro actions as first-class items, placed first. With no history the card says so and uses capacity and estimates.

## Output

One Slack post and one console card with items, owners and reasons, load per person, and a short risk note. Approve creates the next iteration and moves the items. Dry run proposes first; Undo works from Activity.
