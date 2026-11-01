# Teams adapter

Design doc only for MVP. Pam ships Slack plus Azure DevOps; this page records what a Teams adapter would need so the work is scoped later.

## Why it is out of MVP

Teams bots need a public HTTPS endpoint and a bot registration (Azure Bot resource plus app manifest), which does not fit the local-only install story. Slack works over Socket Mode with no inbound ports; Teams has no equivalent for a localhost app.

## Planned shape

- A `teams.py` ChatAdapter beside `slack.py`: send and receive 1:1 messages, buttons, and file download.
- Bot Framework messaging endpoint plus a small tunnel or relay story for local runs.
- Roster matching by email, same as Slack.
- Same rule triggers and write layer; only the chat transport changes.

## MVP contents

Stub module plus this doc. No endpoint, no registration, no tests against live Teams.
