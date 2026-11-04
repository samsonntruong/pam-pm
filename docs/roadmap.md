# Roadmap

Live in 1.0 and what comes next. Tracked here so the README stays honest about what Pam does not do yet.

## Done in 1.0

- Eight board backends over direct REST, chosen per project in the wizard: Pam board, Azure DevOps, Jira, Linear, Trello, ClickUp, Asana, and Monday.
- Teams two-way chat, chosen per project with Slack default. See [teams-adapter.md](teams-adapter.md).

## Near term

- PyPI release so `pipx install pam-pm` works without a git URL.
- Daily lead digest.
- Snooze and quiet hours.

## Longer term

- Agent teammates and orchestration. See [agent-teammates.md](agent-teammates.md).
- Multi-project dashboards and plain-English rule creation.
- Hosted multi-tenant version.

## Non-goals for the current build

- Simulated teammates, mock boards, or replay modes in the product. Fake adapters exist only in tests.
- Vendor lock-in: the model client stays OpenAI-compatible and the board backend stays pluggable.
