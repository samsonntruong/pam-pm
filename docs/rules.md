# Rules

A rule is a trigger plus an action, with per-project settings. Toggle templates on the Rules screen.

## Morning check-in

- Trigger: schedule (cron in each person's time zone).
- Action: DM each person about their active items, one question at a time.
- Skips people with nothing active. Respects working hours, snooze, and a daily message cap.

## Follow-up on close

- Trigger: ADO change (the poller runs WIQL on ChangedDate every 60 seconds).
- Action: when an item closes (Closed on Agile, Done on Scrum), DM the next owner in the chain and ask for status.

## Blocker intake

- Trigger: Slack message classified as a blocker.
- Action:
  1. Ask two or three questions: what is blocked, who or what blocks it, impact.
  2. Find or create the blocking item in ADO.
  3. Create the Predecessor/Successor dependency link.
  4. Tell the reporter first, then DM the blocking owner for an ETA.
  5. When the blocking item closes, notify the waiting person and ask for status.

## Settings

Each rule card holds a toggle plus a few settings: time, days, and the review channel where summaries post. Settings apply per project, chosen with the project switcher at the top of the console.

## Safety

Every rule action that touches the board goes through the write layer: dry run by default, an evidence quote logged with each write, and Undo for reversible writes on the Activity screen.
