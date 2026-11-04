# Changelog

## v1.0.0 - 2026-11-03

- Live PM backends: Pam board, Azure DevOps, Jira, Linear, Trello, ClickUp, Asana, and Monday, chosen per project in the wizard.
- Guided setup wizard with per-connector help and Test buttons.
- Slack Socket Mode plus polling fallback.
- Listen meetings beta with realtime transcription, notes, and action item approval.
- LAN share mode for team access on the local network.
- Desktop wrapper beta: `pam desktop` window plus tray on Windows and macOS.
- Teams two-way: per-project chat choice with Slack default, send via Bot Connector with Accept/Flag buttons, reply polling through Graph app permissions with per-chat watermarks, generated app package in the wizard.

## Unreleased

- Teams two-way via Graph polling: Entra device code sign-in in the wizard Teams step, per-chat watermarks on the shared poll interval, DM plus mention detection onto the shared event bus. No public endpoint needed.

## v0.2.0 - 2026-11-01

- Board backends: built-in editable Pam board plus Azure DevOps, chosen per project in the wizard; capability flags hide what a backend cannot do.
- Rules grow to six templates: morning check-in, follow-up on close, blocker intake, board triage, next sprint, mid-sprint adjustment.
- Kickoff from a document with owner suggestions and RAID assumption proposals.
- RAID log: risks, assumptions, issues, dependencies with propose plus accept, edit, close, delete, and CSV plus Markdown export.
- Board triage, next-sprint proposal, mid-sprint adjustment, and chat add-task, each with an approval card and Undo.
- Provider picker: Meta default plus custom OpenAI-compatible endpoint; SAM 3.1 planning and realtime transcription stay Meta only.
- Listen meetings beta: realtime transcription, notes, and action item approval.
- Desktop wrapper beta: `pam desktop` window plus tray on Windows and macOS.
- One-command installers, autostart, exactly-once outbox, scheduler catch-up, server deploy docs with console password auth.
- Console: 15 screens with light plus dark themes and real screenshots in `docs/images/`, including RAID.

## v0.1.0 - 2026-11-01

- Local web console: setup wizard with Test buttons, Rules, Board, Activity with Undo, Plan, Review.
- CLI: `pam start`, `pam doctor`, `pam review`, `pam --version`, plus the `pam-pm` alias.
- Rules engine with morning check-in, follow-up on close, and blocker intake templates.
- ADO adapter with Agile `Closed` vs Scrum `Done` state mapping, dependency links, and change polling.
- Slack adapter over Socket Mode with one internal app per team from `slack-app-manifest.yaml`.
- Interviewer with pushback and structured results; review and retro synthesis; photo to sprint plan with SAM plus vision fallback.
- Docs: README quickstart, setup guides, rules, privacy, adapters, roadmap, troubleshooting.

## 0.1.0

Initial skeleton: CLI placeholders, stub packages, docs outline, and CI.
