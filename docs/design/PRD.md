# PRD: Pam (repo: pam-pm)

An AI scrum master that replaces status meetings. It checks in with people one at a time, keeps the board accurate, and manages dependencies between them.
Owner: Samson Truong. Track: Indie Dev. Theme: Build for Your World.
Hackathon: Meta Global AI Developer Hackathon. Hard deadline Nov 4, 2026, 9 PM ET (6 PM PT). Target submit: Nov 3 night PT or Nov 4 by 3 PM PT.
Status: v0.5 (built: Slack + board backends, live console, six rules, kickoff, RAID, triage, next sprint, mid-sprint, add-task, provider picker, listen beta, desktop beta)
Naming: product and Slack bot are Pam. Repo `pam-pm`, Python package `pam_pm`, CLI command `pam`. Tagline: "Pam: Projects Autonomously Managed. Replaces status meetings." (Alternative backronym if autonomy sounds too strong: Project Async Manager.)

## 1. Problem

Leads chase status. Standups are synchronous and shallow ("almost done"), the board drifts from reality, blockers sit unnoticed, and nobody tells the waiting person when their blocker clears.

## 2. Product in one paragraph

A small self-hosted app, run on any always-on machine, that talks to team members in Slack and syncs with the board. A local web console walks the lead through setup and lets them turn on rules such as a morning check-in, follow-ups when a task closes, and blocker intake. The agent challenges vague answers, creates real dependency links on the board, and tells the waiting person when the blocker clears. At sprint end it synthesizes a review and retro. Planning can start from a sticky-note photo using SAM 3.1 and Muse Spark, or from a written brief with owner suggestions.

## 3. Scope

Built (works, in the repo, anyone can run it)
- Slack connector and board connectors: Azure DevOps plus the built-in editable Pam board, chosen per project in the wizard.
- Local web console: guided setup, rules, activity feed, board view, kickoff, RAID, triage, next sprint, mid-sprint, review, listen, meetings, settings.
- First-run experience and live console (section 5.4): `pam start` opens a guided setup wizard, then the live console against the user's own Slack and board backend.
- Rules engine with six built-in templates: morning check-in, follow-up on task close, blocker intake, board triage, next sprint, mid-sprint adjustment.
- Interviewer with pushback on vague answers.
- Board sync with dry-run default and an evidence log.
- Sprint review and retro with retro actions created as work items.
- Sticky-note photo to sprint (P1, time-boxed, with fallback).
- Kickoff from a document with owner suggestions and RAID assumption proposals.
- RAID log: risks, assumptions, issues, dependencies with propose plus accept and export.
- Board triage, next-sprint proposal, mid-sprint adjustment, chat add-task: each with an approval card.
- Provider picker: Meta default plus custom OpenAI-compatible endpoint, SAM 3.1 planning Meta only.
- Listen meetings beta: realtime transcription, notes, action item approval.
- Desktop wrapper beta: `pam desktop` window plus tray (the allowed pywebview stretch).
- One-command installers, autostart, exactly-once outbox, scheduler catch-up, server deploy docs with console password auth.

Out (roadmap, mention in README, do not build)
- Agent teammates and orchestration (design only).
- Teams adapter (stub plus design doc only; it needs a public HTTPS endpoint and a bot registration).
- MCP connectors for Jira, Linear, Asana (design note only).
- Plain-English rule creation, multi-project dashboards, hosted version.
- Packaged desktop app with installers and code signing (the beta window and tray ship; signed installers come after the hackathon via GitHub Actions).

Rule of thumb: ship fewer things that work end to end. No feature ships without a test or a scripted test path.

## 4. Users

- Primary: the team lead who installs and configures it.
- Secondary: team members who only ever see Slack DMs and buttons.

## 5. User experience

### 5.1 Team members (Slack only)
- Short, one question at a time, respect working hours, snooze and skip buttons, a daily message cap.
- Private by default: raw answers stay between the person and the agent. The lead sees synthesized status, blockers, and what was written to the board. This is stated in the README as a design principle.
- Pushback targets vagueness, never people.

### 5.2 Lead (local web console, http://localhost:8765)
Screens:
1. Setup: backend choice plus a wizard where each step has a Test button that turns green.
2. Rules: per project, template cards with toggles and a few settings (time, days, channel).
3. Triage: board scan findings with proposed fixes and Approve buttons.
4. Board: sprint board (Azure DevOps or the editable Pam board) with dependency links, updating live as Pam writes.
5. Activity: live feed of what the agent did, the evidence quote behind each board write, and an Undo button for board writes.
6. Plan: upload or receive a sticky-note photo, see SAM 3.1 masks over each note, Muse Spark's reading of each note, the proposed sprint, and an Approve button.
7. Kickoff: paste a brief or upload a file, review owner suggestions with reasons on one approval card, Approve to create items.
8. Add task: console card matching the Slack add-task draft, with Approve.
9. RAID: four tabs with counts, add, edit, accept, close, delete, and CSV plus Markdown export.
10. Next sprint: proposal card with owners, load, and risk, Approve to create the next sprint.
11. Mid-sprint: adjustment proposals with the effect on the goal, Approve buttons only.
12. Review: run or view the sprint review and retro.
13. Listen: capture meeting audio with live transcripts.
14. Meetings: review transcripts, generate notes, approve action items.
15. Settings: provider, safety, spend cap, and project options.

### 5.3 How it launches
- One-command installer (`install.ps1` / `install.sh`), or `pipx install git+https://github.com/OWNER/pam-pm.git` then `pam start` (PyPI release is roadmap).
- Docker option: see `docs/deploy.md`.
- The console binds to 127.0.0.1 only and requires a one-time setup token printed at start. Secrets are stored in a local data directory with restrictive file permissions, never in the repo. Network bind needs a console password.
- No inbound ports are needed: Slack uses Socket Mode and the board is polled.

### 5.4 First run and live console

Goal: a new user goes from install to a running Pam in under 10 minutes, and the console shows their real work.

Everything runs against real accounts: the user's Slack workspace and board backend. There are no simulated teammates, mock boards or replay modes in the product. (Tests use fake adapters internally; they are not a user-facing mode.)

First run: `pam start` opens the console on the setup wizard (section 9): backend choice, provider, Slack (manifest, two tokens), Azure DevOps when chosen (org, project, team, PAT), people, rules, safety, done with test check-in. Each step has a Test button that turns green. When all are green the console lands on the live Board.

Console: left navigation (section 5.2), project switcher at the top, board in the center, activity feed with evidence quotes and Undo on the right. Chat happens in Slack; raw answers stay private and the lead sees synthesized status, evidence quotes and board writes.

The story the console and Slack show (and the video records, live):
1. Connect: the setup wizard goes green, step by step.
2. Plan from a sticky-note photo: SAM 3.1 masks, readings, proposal, Approve, board fills.
3. Morning check-in: a vague answer ("almost done") is challenged; structured update; board write with evidence.
4. Blocker: Pam asks two questions, links a dependency on the board, asks the blocking owner for an ETA.
5. Blocker clears: the owner closes the item; Pam tells the waiting person and asks for status.
6. Review and retro: synthesized review, retro actions appear as new cards.

Implementation: same FastAPI app, Jinja plus HTMX plus server-sent events from one event stream (the same event bus the rules engine uses). Board, activity and setup status are views over that stream. Hand-written CSS, no build step.

Visual direction: clean and modern, one accent color for Pam, neutral surfaces, readable type, kanban cards with clear status colors, subtle motion when cards move. It should look like a finished product in a screen recording. Light plus dark themes.

## 6. Rules engine

A rule is a trigger plus an action, with per-project settings.

Triggers: schedule (cron in each person's time zone), board change (polled every 60 seconds using WIQL on ChangedDate for ADO, changed watermark for the Pam board), Slack message classified as a blocker.

Built-in templates:
1. Morning check-in: at the set time, DM each person about their active items. Skips people with nothing active.
2. Follow-up on close: when an item moves to Done, DM the next owner in the chain (successor item) and ask for status.
3. Blocker intake: a person says they are blocked. The agent asks 2 to 3 questions (what, who or what blocks, impact), finds or creates the blocking item, creates a Predecessor and Successor dependency link, and DMs the blocking owner for an ETA. When the blocking item closes, the agent notifies the original person.
4. Board triage: daily scan for stale, unassigned, and blocked items with one proposed fix per finding on an approval card.
5. Next sprint: proposes the next sprint after the review lands, with owners, load, and risk.
6. Mid-sprint adjustment: proposes goal-safe changes when the sprint drifts off track.

## 7. Functional requirements

P0
- F1: Slack adapter, Socket Mode, DMs, threads, buttons, file download.
- F2: ADO adapter: projects, teams, iteration, items via WIQL, update state and remaining work, comments, create items, dependency links, change polling.
- F2b: Pam board adapter: full CRUD, states, iterations, links, comments, polling, editable kanban in the console.
- F3: Interviewer agent, structured result per item: {status, remaining, eta, evidence_quote, blocker, blocker_owner, confidence}; max 6 turns; pushback rules.
- F4: Rules engine with the six templates above.
- F5: Write safety layer: dry-run default, every write logged with evidence, Undo for reversible writes.
- F6: Local web console: setup wizard, rules, activity feed.
- F7: Sprint review and retro synthesis; retro actions created in ADO tagged `retro-action`; posted to Slack.
- F8: Prompt masking (emails, phones, tokens) before any model call.
- F9: Cost meter with per-day hard cap.
- F10: Fake adapters (in-memory chat and SQLite board) for tests only.
- F15: First-run wizard and live console (section 5.4): setup with Test buttons, Board, Activity with Undo, Plan, Review.
- F16: Kickoff from a document with owner suggestions and RAID assumption proposals.
- F17: RAID log with propose plus accept, edit, close, delete, export.
- F18: Board triage with model-gated title fixes and an approval card.
- F19: Next-sprint proposal with dependency guard and throughput load.
- F20: Chat add-task with draft and approve.
- F21: Mid-sprint adjustment with goal effects, Approve only.
- F22: Provider picker with Meta default and custom endpoint test.

P1
- F11: Sticky-note photo to sprint: SAM 3.1 segments notes, Muse Spark reads crops, proposal with Approve button, items created in ADO. Fallback: whole image to Muse Spark. Time box: 1 hour for SAM before falling back.
- F12: Undo and activity polish, per-person snooze and quiet hours.
- F23: Listen meetings beta: realtime transcription, notes, action item approval.
- F24: Desktop wrapper beta: window plus tray.

P2 (only if ahead)
- F13: Daily summary digest for the lead.
- F14: Teams stub and docs.

## 8. Architecture

Interfaces: ChatAdapter (Slack impl), PMAdapter (ADO impl, Pam board impl, SQLite fake), ModelClient (OpenAI-compatible, tool calling, structured output, image input), VisionClient (SAM 3.1 plus fallback).

Services in one process: FastAPI web console (Jinja templates plus HTMX, no frontend build step), Slack Socket Mode listener, scheduler (APScheduler), board poller, agent runtime. State in SQLite.

Stack: Python 3.12, FastAPI, slack-bolt async, httpx, APScheduler, pydantic, pytest, Docker.

## 9. Setup wizard (web console)

Principle: no hand-editing config, no hunting for IDs.
1. Backend choice: Pam board (built in) or Azure DevOps, saved per project.
2. Model provider: Meta default or custom OpenAI-compatible endpoint; Test makes a call and shows tool, structured, and image results in plain words. Explain Standard vs Contributor (Contributor trains on inputs and outputs, so default Standard).
3. Slack: each team creates one internal app from our manifest in their own workspace (never a shared, distributed app: non-Marketplace distributed apps get much lower history rate limits and must go through the Marketplace). Works on the Slack Free plan (10 app limit, 90 day visibility; we store our own conversations and never read Slack history). Console shows a ready-made app manifest and a create-app link; user pastes bot and app tokens; Test checks scopes, Socket Mode, and sends a DM to the user.
4. Azure DevOps: shown only when Azure DevOps is chosen. Paste org URL and PAT; dropdowns for project, team, current iteration; Test verifies Work Items read and write and detects Agile or Scrum template.
5. People: teammates pulled from ADO and auto-matched to Slack users by email where available, else one name per line; user fixes mismatches; time zones and working hours.
6. Rules: toggle templates, set times, choose the review channel.
7. Safety: dry-run on by default, daily spend cap.
8. Done: a "send me a test check-in" button plus the setup token and autostart checkbox.
Quality bar: a stranger reaches a working dry-run in under 10 minutes using only the README.

## 10. Model policy

- `model.tier`: `standard` (default) or `contributor`.
- Contributor is far cheaper ($0.10 input, $0.20 output per million tokens) but uses inputs and outputs to improve Meta's products, is token rate limited, and is limited to select countries. Because this app reads private team conversations, it is not the default. Use Contributor only for development with fake data.
- Standard is $1.25 input, $0.15 cached input, $4.25 output per million tokens. Model ID is config. Verify IDs in the Meta console during M0.

## 11. Azure DevOps notes

- Auth: PAT with Work Items read and write, Project and Team read.
- Create item: POST `https://dev.azure.com/{org}/{project}/_apis/wit/workitems/${type}?api-version=7.1` as `application/json-patch+json`.
- Query: POST `.../_apis/wit/wiql?api-version=7.1`.
- Dependency links: relation types `System.LinkTypes.Dependency-Forward` (Successor) and `System.LinkTypes.Dependency-Reverse` (Predecessor); verify availability in the test project.
- Iterations: classification nodes plus team settings iterations endpoints.
- Detect process template; map states and fields (Agile vs Scrum).

## 12. Success metrics

- A full run with 3 teammates on real Slack and board: morning check-in, one challenged vague answer, one blocker with dependency link and notification when cleared, review and retro.
- Photo to sprint in under 60 seconds (P1).
- A stranger follows the README, installs, completes the wizard with their own accounts and sees the Board live in under 10 minutes.
- Full run under $2 on Standard.

## 13. Risks

| Risk | Mitigation |
|---|---|
| 2 days is tight | Strict priority; cut F11 polish, then F12, then retro polish; never cut tests or README |
| Web console eats time | Jinja plus HTMX, no SPA; wizard steps share functions with a CLI fallback |
| SAM 3.1 awkward | 1 hour time box, fallback |
| Meta API preview limits | Retry, backoff, swappable client |
| Polling misses changes | Poll by ChangedDate with a stored watermark; idempotent handlers |
| Bad board writes | Dry-run default, evidence log, Undo |
| Privacy concerns | Private-by-default design, masking, Standard tier default |
| Slack setup friction | Manifest-based app, Test buttons, plain-English errors |
| Console scope creep | One event stream, Jinja plus HTMX plus SSE, hand-written CSS, 6 hour time box |
| `pam` command name already used on a machine | M0 checks `which pam`; fallback alias `pam-pm` documented |

## 14. Timeline (Pacific)

- Sat Oct 31 night: repo skeleton, M0 connectivity checks.
- Sun Nov 1: core engine, interviewer, ADO adapter and writes, blocker loop, fake adapters, first console screens (setup).
- Mon Nov 2: rules and activity screens, live console (wizard, board, activity), review and retro, README, Docker, vision planning (time boxed).
- Tue Nov 3: fresh-clone test, video, public repo, submit. Nov 4 is buffer only.

## 15. Open questions

1. Meta API key and exact model IDs for Muse Spark 1.3 and SAM 3.1.
2. ADO org, project and Slack workspace for the live run and the video.
3. Decided: product and bot Pam, repo `pam-pm`, MIT license. Open: backronym wording (Projects Autonomously Managed or Project Async Manager).

## 16. Changelog

- v0.5: moved built items to Built (native Pam board, six rules, kickoff, RAID, triage, next sprint, mid-sprint, add-task, provider picker, listen beta, desktop beta, installers, autostart, deploy auth). Screens grow from 6 to 15. Wizard gains backend choice, provider, and conditional ADO step.
- v0.4: MVP scope locked (Slack + Azure DevOps, local web console).
