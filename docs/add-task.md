# Chat add-task

Mention Pam in Slack with an add-task request and Pam drafts one task,
shows it with owner and estimate, and creates it after Approve.

## Message format

```text
@Pam add a task: fix login timeout, 3h, assign Maya
```

- The trigger is `add task` or `add a task`, with an optional colon,
  after stripping a leading `@Pam` or `<@U...>` mention.
- The estimate reads like `3h` or `30m` (minutes convert to fractional
  hours). Without one the draft shows `Not set`.
- The owner reads from `assign Maya`, `assigned to Maya`, `assignee
  Maya`, or `owner Maya`. Without one Pam falls back to the kickoff
  owner order: names in the text win, then skill match, then load
  against capacity, else Unassigned flagged as needing an owner.
- Anything else in the mention becomes the title. Text without the
  trigger is ignored and posts nothing.

## Output

One Slack message with Approve and Cancel buttons, and a matching
console card at `/add-task` with the same fields: title, owner and
reason, estimate, evidence quote, and capacity or owner warnings.
Approve creates the item through the shared planner approval path.
Dry run is on by default: Approve proposes the write with its evidence
quote first, and applies it only when dry run is off. Undo works from
the Activity screen like other writes.
