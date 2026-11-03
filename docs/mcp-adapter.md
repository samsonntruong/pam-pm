# MCP adapter

Superseded in 1.0: Jira, Linear, Trello, ClickUp, Asana, and Monday now ship as direct REST connectors. This page keeps the original design note for history.

## Idea

Each tracker gets a PMAdapter implementation behind the same interface the ADO adapter uses today: list items, update state, comment, create items, link dependencies, and poll for changes. An MCP client would carry the transport, with one connector module per tracker.

## Planned shape

- `PMAdapter` stays the contract: no rule or agent code changes per tracker.
- State mapping per tracker, mirroring the Agile `Closed` vs Scrum `Done` handling in the ADO adapter.
- Polling or webhooks per tracker capability, with the same watermark and idempotent handler pattern.
- Secrets per connector in the local data directory, with Test buttons in the wizard.

## MVP contents

This doc only. No connector code, no MCP dependency, no live tests.
