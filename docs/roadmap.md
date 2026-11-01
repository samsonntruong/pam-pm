# Roadmap

Out of scope for the current build, in rough priority order. Tracked here so the README stays honest about what Pam does not do yet.

## Near term

- PyPI release so `pipx install pam-pm` works without a git URL.
- Daily summary digest for the lead.
- Per-person snooze and quiet hours polish, plus Undo polish on the Activity screen.
- More review channel options and per-project rule schedules.

## Integrations (design notes only today)

- Teams adapter: stub plus design doc. Needs a public HTTPS endpoint and a bot registration. See [teams-adapter.md](teams-adapter.md).
- MCP connectors for Jira, Linear, and Asana. See [mcp-adapter.md](mcp-adapter.md).

## Longer term

- Agent teammates and orchestration. See [agent-teammates.md](agent-teammates.md).
- Multi-project dashboards and plain-English rule creation.
- Packaged desktop app (Mac and Windows installers with code signing via GitHub Actions). The `pam desktop` window and tray beta already ships; see [desktop.md](desktop.md).
- Hosted multi-tenant version.

## Non-goals for the current build

- Simulated teammates, mock boards, or replay modes in the product. Fake adapters exist only in tests.
- Vendor lock-in: the model client stays OpenAI-compatible and the board backend stays pluggable.
