# Handoff: build Pam with Hermes Agent (profile `pam-builder`) and Muse Code

Read PRD.md (v0.4) first, then META_API_NOTES.md, REPO_SETUP.md and SUBMISSION.md. CREDENTIALS_SHEET.md lists credential variable names only. This document tells you how to execute it.

## Mission

Ship a working, public, open-source MVP by Tuesday Nov 3 night PT (hard deadline Nov 4, 6 PM PT). MVP means Slack plus Azure DevOps, a local web console, and three rules (morning check-in, follow-up on close, blocker intake). Do not overbuild. Working end to end beats feature count.

## Operating rules

1. Human gates: ask Samson before spending money, pushing to the public repo, printing secrets, or changing PRD scope.
2. Secrets never enter a model context. Anything you read or print is sent to the model provider, so: never open, cat, type, Get-Content, grep or print ~/pam-secrets.env or any .env file; never echo a secret variable; never paste a secret into a Muse Code task, a commit, a log or a Discord message. Load credentials only inside code or scripts (for example python-dotenv reading ~/pam-secrets.env, or PAM_SECRETS_FILE pointing to it) and refer to them by variable name. To check credentials, run check_pam_secrets.py, which prints names only. Do not create a .env file inside the repo during the build; Pam's local config for tests reads PAM_SECRETS_FILE. Redact tokens from error output and HTTP logs (mask anything matching xoxb-, xapp-, github_pat_, Bearer, or the ADO PAT) before printing. If a secret value ever appears in your output, stop, say so in the pam-build channel (without the value) so Samson can rotate it. In the shipped product, secrets live only in the app's local data directory, never in the repo.
3. Meta Model API only. Default tier Standard. Contributor tier only for dev runs with fake data.
4. Spend: you (Hermes on Muse Spark) and Muse Code both draw from the same $150 Meta credits, and Meta has no hard cap, so these limits are yours to enforce. Log tokens and cost per call for both, priced at the META_API_NOTES.md rates, in DEVLOG.md. Report the measured burn rate in the pam-build channel after the first hour, then at $25, $50 and $75 cumulative. Hard stop at $100 cumulative (Hermes plus Muse Code) and report. Reserve at least $30 for product test runs and the video recording. Set a max step limit on every run, and end each milestone with a commit and a short report instead of looping. Use only throwaway test credentials (test Slack workspace, test ADO project) while the build agent runs, never real accounts.
5. Mask prompts before any model call. Unit test it.
6. Priority is law: P0, then P1, then P2. Do not start P1 until the P0 live path works.
7. Dry-run is the default for board writes.
8. No em dashes in docs, UI text or generated messages.
9. Keep DEVLOG.md, three lines per entry.
10. Every feature needs a test with fake adapters or a scripted test path.
11. Desktop-app readiness: keep the service headless with the console as a plain localhost page, store data in the OS user data directory (use platformdirs), and never assume Docker. Install path is `pipx install git+https://github.com/OWNER/pam-pm.git` then `pam start` (no PyPI yet). Do not build a packaged desktop app; if Monday finishes early, a pywebview window around the console is the only stretch allowed.
12. Slack design constraint: the Slack app is always created by each user in their own workspace from `slack-app-manifest.yaml` (internal app, Socket Mode). Never build or document a shared distributed bot. Do not rely on conversations.history; keep our own conversation store.
13. Naming: product and Slack bot are Pam; repo `pam-pm`; Python package `pam_pm`; CLI `pam`. In M0 run `which pam` (`where pam` on Windows). If the name is taken, also register a `pam-pm` entry point and use that in docs.
14. Meta usage log: the hackathon submission asks how Meta products were used to build the project. Keep a "Meta usage" table in DEVLOG.md: product (Muse Spark via Model API, Muse Code, SAM 3.1), component or task, what it did. Record only real usage. Never claim what was not used.
15. Prompt caching: read META_API_NOTES.md section 3 BEFORE writing any model-calling code and implement its rules from day one (single `build_messages()`, stable prefix, volatile data last, cache warm-up before parallel interviews, per-call cached token logging, prefix stability test). Every model call logs cached vs uncached input tokens.
16. Submission: a public GitHub repo and a demo video of 3 minutes or less (YouTube) are required. Samson makes the repo public and uploads the video. You deliver SUBMISSION.md filled in from the real build.
17. Never subscribe to a paid Muse Code plan or any paid service. Authenticate Muse Code only with META_API_KEY (`muse auth set --api-key-stdin`).

## Roles

- Architect: interfaces, schemas, DB tables, rule definitions first.
- Orchestrator (Hermes, the `pam-builder` profile, running on Muse Spark 1.3 via the Meta Model API): plans, splits each milestone into small tasks with acceptance criteria, hands them to Muse Code, reviews diffs, runs tests, commits, reports. Hermes does not write application code itself.
- Developer (Muse Code, the required coding harness): writes all code, tests and docs via `muse exec --json --max-model-steps N` run headless inside the repo. Give each task: the files to touch, the interface or schema it must follow, the tests that must pass, and the relevant PRD or HANDOFF section. Keep tasks small (one module or one test file). Never put secret values or real user text in a Muse Code prompt. Model: if Muse Code lets you choose, use META_MODEL_DEV for code generation (the code is going public anyway) and switch to META_MODEL if throttled; Muse Code spend counts toward the build budget in rule 4. Log every task in the Meta usage table (task, files, result). If Muse Code fails the same task twice, Hermes may write that fix directly and logs it as an exception in DEVLOG.md.
- Reviewer: checks each change against the PRD, secrets, masking, tests; Developer iterates until approved.
- Tester: runs the end-to-end test with fake adapters, the real Slack and ADO run, then the fresh-clone setup test.

## Repo layout

```
pam-pm/
  README.md  DEVLOG.md  LICENSE  .env.example  slack-app-manifest.yaml
  Dockerfile  docker-compose.yml  pyproject.toml
  docs/       per REPO_SETUP.md section 2 (setup, slack-setup, azure-devops-setup, roadmap, design/, images/ ...)
  scripts/    check_meta.py  check_sam.py  check_ado.py  check_slack.py  (M0 connectivity checks)
  src/pam_pm/
    core/     schemas.py  config.py  masking.py  cost.py  store.py  writes.py
    models/   client.py  vision.py
    agents/   interviewer.py  lead.py  planner.py
      prompts/  builder.py (build_messages, the only place messages are assembled)  system/  tools.py
    rules/    engine.py  scheduler.py  poller.py  templates/ (morning, follow_up, blocker)
    adapters/
      chat/   base.py  slack.py  fake.py
      pm/     base.py  azure_devops.py  sqlite_board.py (fake, tests only)
    web/      app.py  templates/  static/   (FastAPI, Jinja, HTMX, SSE)
    cli.py    start, doctor, review, --version
  tests/      unit, end-to-end with fake adapters, test_prompt_prefix_stability.py, test_cost_meter.py
    fixtures/ recorded model replies (masked), fake roster, sticky-note test photo
```

## Milestones and done criteria

M0 Connectivity checks (Saturday night). Keep these cheap: one call each, no repeated runs.
- Muse Spark chat with tool calling and image input; note base URL, model IDs, tiers, limits in DEVLOG.md.
- SAM 3.1 availability and request shape; decide SAM or fallback within one hour.
- ADO: list projects, teams, iteration, items; create a dependency link; confirm link relation names.
- Slack: DM round trip over Socket Mode, button click received.
- Caching: do NOT run a dedicated caching test. Implement META_API_NOTES.md section 3 and read `cached_tokens` from the usage of the real calls you already make (M1 onward). Log it in DEVLOG.md after the first interview runs. `pam doctor` has a one-click cache check for later.
- API quirk check (fold into the Meta check, no extra runs): confirm `tool_choice` accepts only "auto"; confirm a small `max_tokens` can return empty content because reasoning tokens are spent first; confirm an image input works.
- Name check: `which pam`.
Done: four green scripts in `scripts/` (Meta chat plus tools, SAM, ADO, Slack), results in DEVLOG.md.

M1 Engine and interviewer (Sunday)
- Store, writes layer (dry-run, evidence log, undo records), masking, cost meter.
- Fake adapters. Interviewer with structured results and pushback, 6 turn cap.
Done: test with fake adapters shows a vague answer challenged and a logged board write with the evidence quote.

M2 Blocker loop and rules (Sunday into Monday)
- Scheduler and ADO poller with watermark.
- Templates: morning check-in, follow-up on close, blocker intake (questions, dependency link, ETA from blocker owner, notify waiting person on close).
Done: test with fake adapters runs all three; real Slack plus ADO run shows the blocker loop end to end.

M3 Web console (Sunday to Monday)
- `pam start` launches the server on 127.0.0.1 and opens the browser; setup token shown in terminal.
- Screens: Setup wizard with Test buttons, Rules, Activity feed with evidence and Undo, Review, Board (read-only).
- Jinja plus HTMX only. No frontend build step.
Done: Tester completes setup from a fresh clone using only the README in under 10 minutes.

M3b First run and live console polish (Monday, P0)
- Build PRD section 5.4: wizard with Test buttons, Board, Activity with Undo, Plan, Review. Live accounts only.
- The product has no demo, sample, offline or replay modes and the UI never uses those words. Fake adapters exist only in tests.
- Playwright test loads each screen against fake adapters and saves screenshots to `docs/images/` for the README.
Done: a new user completes the wizard with real accounts and sees the Board; Undo works; screenshots exist.

M4 Review and retro (Monday)
- Lead synthesis, Slack post with Block Kit, retro actions created in ADO tagged `retro-action`.
Done: one click in the console produces the Slack post and new items.

M5 Vision planning (Monday, P1, time boxed)
- Photo in Slack, SAM or fallback, note reading, proposal with Approve button, ADO items created.
Done: a test photo becomes a populated sprint in under 60 seconds.

M6 Release (Tuesday)
- Run the story live: Samson plus one or two extra Slack accounts (email aliases or friends) answering by hand. Personas are scripted only inside tests. The submission video is recorded live on real Slack, real Azure DevOps and the real console.
- README: what it is, short video or GIF, quickstart, architecture, privacy principle, roadmap.
- Docker compose works from a fresh clone. License file. Public repo.
- Video script run live on Standard tier. Video must be 3 minutes or less; target 2:30.
- Fill SUBMISSION.md placeholders from DEVLOG.md (Meta usage table, measured cost, what was tested for real) and list anything not verified in MORNING.md.
Done: the six story steps (PRD section 5.4) run back to back with no manual board edits, and SUBMISSION.md is ready to paste.

## Prompt guidance

Interviewer: one question at a time, concise and friendly, never accept vague status without remaining work and an ETA, quote the person's words as evidence, never invent status, finish with the structured result via the finish_interview tool.

Blocker intake: ask what, who or what blocks, and impact; confirm the blocking item before linking; never contact the blocking owner without telling the reporter first.

Lead: separate facts from interpretation, flag low-confidence items.

All handoffs between components use structured outputs validated with pydantic; retry once on failure.

## Reference

- Hackathon: https://cerebralvalley.ai/e/meta-ai-developer-hackathon
- Meta Model API console and docs: https://dev.meta.ai (cookbook has OpenAI-compatible quickstart and agent examples). Muse Spark 1.3: Standard $1.25 in, $0.15 cached in, $4.25 out per million tokens; Contributor $0.10, $0.002, $0.20.
- Muse Code: https://dev.meta.ai/products/muse-code (`muse exec`, `--json`, `--max-model-steps`, key via META_API_KEY).
- ADO REST 7.1: work items, WIQL, links, classification nodes, team iterations.
- Slack Bolt for Python, Socket Mode. Starting scopes: chat:write, im:write, im:history, users:read, users:read.email, files:read, channels:read.

## Meta API facts to verify in M0

Reported by third-party integrations (LiteLLM, harn-vm probes) and Meta's docs index; confirm each in M0 and record the result in DEVLOG.md.
- Base URL `https://api.meta.ai/v1`, OpenAI-compatible chat completions (also Responses and Messages formats). Auth via META_API_KEY bearer token.
- Model IDs: `muse-spark-1.3` (Standard), `muse-spark-1.3-contributor`, `sam-3.1`. Older: `muse-spark-1.2`, `muse-spark-1.1`.
- `tool_choice`: only `"auto"` is accepted; `required`, `none` and named functions return 400. So never rely on forcing `finish_interview`. Instruct it in the prompt, and if the model replies without calling it, retry once with a reminder, then fall back to a structured-output extraction call.
- Thinking is always on and reasoning tokens are spent before visible output. Set generous `max_tokens` (4,000 or more for interviews, more for review synthesis) or content can come back empty with `finish_reason` "length". `reasoning_effort` appears to have no effect. Reasoning tokens are billed as output, so cost per call is higher than the visible text suggests; the cost meter must count them.
- No assistant prefill. Prefer Markdown scaffolding in prompts over XML tags.
- Native structured output (JSON) is supported. Vision input is supported.
- Rate limits are per tier (RPM, TPM, TPD) and return 429. Use exponential backoff with jitter and cap parallel interviews with a semaphore (default 4).

## Prompt caching

The full playbook is in META_API_NOTES.md section 3 and wins on any conflict. Summary:

Muse Spark supports prompt caching with cached input priced at $0.15 per million tokens vs $1.25 uncached. Third-party probes show cached tokens reported in `usage.prompt_tokens_details.cached_tokens` on repeated prefixes, which suggests caching is automatic, but Meta's own docs page was not retrievable. Confirm it from the usage of real calls (no dedicated test runs) and do not assume TTL or minimum size.
How to design for cache hits:
- Put everything stable first and in a fixed order: system prompt, tool definitions, project and team context. Then the conversation, append-only.
- Never put timestamps, random IDs, request counters or per-user data in the stable prefix. Put volatile data in the last user message.
- Keep tool definitions byte-identical and in the same order on every call.
- Do not rewrite or reorder earlier messages in a conversation. Summarize old turns only at turn boundaries and sparingly.
- All interviewers share one system prompt and tool list so parallel interviews can share the cached prefix.
How to measure it:
- Log `cached_tokens` and total input tokens for every call in `cost.py`. Show cached share and dollars saved in the console and in `pam doctor`.
- `pam doctor` includes an optional cache check (two calls with the same long prefix, reports hit or miss). It is for users; do not run it repeatedly during the build.
Build-agent rule (you run on Meta too, so this applies to Hermes and Muse Code): after the first hour of the overnight build, compute the cached share from your own usage data and Muse Code's JSON output. If it is below 50 percent, report it in MORNING.md and reduce context churn (fewer fresh sessions, no mid-session instruction edits, smaller tool output). If cost extrapolates past the $100 cap, stop and report.

## Video script (recorded live)

Total 2:30, hard limit 3:00. See PRD section 5.4.
1. Connect, 0:00 to 0:25: the real setup wizard connects Slack and Azure DevOps and the Test buttons go green.
2. Sticky-note photo to sprint, 0:25 to 0:55: SAM 3.1 masks, readings, approve, board fills.
3. Morning check-in, 0:55 to 1:25: "almost done" challenged, structured update, board write with evidence.
4. Blocker, 1:25 to 1:55: two questions, dependency link on the board, blocking owner asked for an ETA.
5. Blocker clears, 1:55 to 2:10: Pam tells the waiting person.
6. Review and retro, 2:10 to 2:30: synthesized review, retro actions become cards.

## Overnight mode (Samson is asleep; this overrides rule 1 for routine decisions)

- Credentials are pre-provided in `~/pam-secrets.env` (on Windows `%USERPROFILE%\pam-secrets.env`), outside the repo. Run `check_pam_secrets.py` from the handoff bundle first; it prints SET or MISSING per variable and never prints values. Variables: META_API_KEY, META_BASE_URL, META_MODEL, META_MODEL_DEV, META_VISION_MODEL, GITHUB_REPO, GITHUB_TOKEN, GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_TEST_USER_ID, SLACK_REVIEW_CHANNEL_ID, ADO_ORG_URL, ADO_PROJECT, ADO_TEAM, ADO_PAT. Discord routing to your channel is already configured by the main profile; do not touch Discord or gateway settings. Configure git with GIT_AUTHOR_NAME and GIT_AUTHOR_EMAIL before the first commit. Never copy this file into the repo, never print values, never commit them. Reference them by name only.
- Do not wait for answers. You may post a question in the pam-build Discord channel, but then decide the most reasonable option, log it in DEVLOG.md and continue. Stop only for a hard stop: spend cap reached, the same step failing 3 times, or a credential failing.
- Push only to the private repo in GITHUB_REPO. Never make it public. Never create paid resources or change billing.
- Real Slack testing overnight is limited to connectivity checks and at most one hello DM to SLACK_TEST_USER_ID. Everything else is tested with fake adapters. The full multi-person Slack run happens in the morning with humans.
- Seed the test ADO project (sprint, items, one dependency) only inside ADO_PROJECT. Assign seeded items to the PAT owner or leave unassigned; Samson assigns teammates in the morning.
- Final step: write MORNING.md listing what works, what was tested for real vs fake only, what failed or was cut, spend so far, and the exact steps Samson must do (invite teammates, run the real blocker loop, record the video, make the repo public).

## Reporting

Post in the pam-build Discord channel only (it is routed to you through the shared bot; never post anywhere else). After each milestone: at most 10 lines with what works, what was cut, spend so far (including Muse Code), cached share, next milestone. Post immediately on a hard stop. Never post secret values, tokens or IDs.

## Workspace and isolation

- You run in a Docker sandbox (Linux, Python 3.12, Node 20). Only ~/pam-build/ (read-write) and ~/pam-secrets.env (read-only) come from Samson's PC. Never try to reach the host machine, mount anything, or use a Docker socket.
- Work only in ~/pam-build/ (your terminal.cwd). Clone the repo to ~/pam-build/pam-pm/. The bundle is in ~/pam-build/handoff/.
- Tests that need Docker on the host (the Docker install path in REPO_SETUP.md section 9) cannot run in the sandbox: write the exact commands into MORNING.md for Samson instead. Everything else, including Playwright and the pipx install test, runs inside the sandbox.
- Never read or write other Hermes profiles, ~/.hermes/ outside your own profile, or other projects. Never message other channels, bots or agents.

## First actions

1. Read PRD.md, META_API_NOTES.md, REPO_SETUP.md and SUBMISSION.md. Create the repo skeleton and commit it, following REPO_SETUP.md for files, packaging, Docker, CI, README and the release checklist.
2. Run `python handoff/check_pam_secrets.py`, confirm `muse --version` works and Muse Code is authenticated, then load credentials from `~/pam-secrets.env` (see Overnight mode). Use `slack-app-manifest.yaml` from Samson as the starting manifest and keep it in the repo.
3. Run M0 and report before building anything else.
