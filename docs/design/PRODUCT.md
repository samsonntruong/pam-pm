# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: a team lead running a small software or data team. The lead sets up Pam once, turns on rules, watches the console, and stays accountable for board accuracy and follow through. The lead works in the console daily but does not live in it.

Secondary: teammates who do focused work and answer short check-ins in team chat. They never open the console in normal use. They see one question at a time, with snooze and skip options, and they expect raw answers to stay private.

## Product Purpose

Pam is an open-source AI project manager for small software and data teams. It replaces status meetings with short async check-ins, keeps the work tracker accurate, chases blockers until they clear, and gives the lead a calm console for supervision.

Success means the lead stops chasing status, the board matches reality, blockers get linked and cleared with an ETA, and sprint review and retro write-up take minutes instead of hours.

## Positioning

Pam does the follow through, not just the summary. It asks one question at a time, pushes back on vague answers until remaining work and an ETA are stated, writes tracked changes with a quoted evidence line behind each write, links blockers as real dependencies, and notifies the waiting person when the blocker clears.

## Operating Context

One local process holds the console, the chat listener, the scheduler, the tracker poller, and the agent runtime. State lives in local SQLite. The same event stream feeds the console views and the rules engine.

Console screens, in one shared shell with left navigation and a project switcher at the top:

- Setup wizard: guided first run. Each step has a Test action that turns green. When all steps pass, the console lands on the live Board. Runs on loopback only with a one-time setup token.
- Board: read-only sprint board with dependency links. Updates live as Pam writes. Cards group into To Do, Doing, and Done.
- Activity: live feed of agent actions. Each board write shows its evidence quote and an Undo action for reversible writes.
- RAID: log of risks, assumptions, issues, and decisions. Each entry has an owner, a status, and a link to the related work item when one exists.
- Plan: planning from a whiteboard photo. Shows detected note regions, text readings per note, a proposed sprint table, and an Approve action that creates items.
- Review: sprint review and retro. Shows synthesized per-item status with low confidence flags, plus retro actions that can be created in the tracker.
- Rules: per-project rule templates with toggles and a small set of settings such as time, days, and review channel. Three templates ship built in: morning check-in, follow-up on close, and blocker intake.
- Settings: project list, safety controls including dry run default and daily spend cap, working hours, and connection status with retest.

Daily loop: morning check-in in chat, board writes with evidence, blocker intake when someone reports being blocked, ETA request to the blocking owner, notification when the blocker clears, then review and retro at sprint end.

## Capabilities and Constraints

Confirmed functionality:

- Async interviews in chat with pushback on vague answers and a structured result per item including status, remaining work, ETA, evidence quote, blocker, and confidence.
- Tracker sync with dry run default, evidence log on every write, and Undo for reversible writes.
- Three rule templates: morning check-in on a schedule per person time zone, follow-up on close when an item closes, and blocker intake with dependency link plus ETA request.
- Sprint review synthesis with low confidence flags and retro actions created in the tracker.
- Planning from a photo with region masks, readings, proposal table, and Approve gate.
- RAID log with owner, status, and links to work.
- Cost meter with per-day hard cap and cached token share shown in the console footer and in the doctor check.

Technical constraints:

- FastAPI with Jinja templates and HTMX, hand-written CSS, small vanilla JS only, no build step, no CSS framework.
- Light and dark theme from the same token set.
- WCAG 2.2 AA for console screens.
- Local first: loopback bind by default, secrets in the OS data directory with restrictive permissions, never in the repo.
- Privacy: raw answers stay between the person and the agent. The lead sees synthesized status, evidence quotes, and board writes, never full transcripts. Emails, phones, and tokens are masked before any model call.
- Plain language: no em dashes in docs, UI text, or generated messages.

Undecided: hosted version scope, additional tracker connectors beyond the current fake plus real adapter split, multi-project dashboards, and plain language rule authoring. These stay out of MVP scope until confirmed.

Terminology: item (one tracked unit of work), write (one tracker change with evidence), evidence quote (the quoted words behind a write), rule (a trigger plus an action with per-project settings), blocker (an item or wait that stops another item).

## Brand Commitments

Name: Pam. Console wordmark is the plain name in the accent color. No logo image is confirmed.

Voice: calm, trustworthy, and plain-spoken. Short sentences. No hype. Errors say what happened and what to try next. Pushback targets vagueness, never people.

Feel: a finished product, not a prototype. One accent color, neutral surfaces, readable type, clear status colors, subtle motion when cards move. Light theme only exists in code today; dark theme is a committed constraint and reuses the same roles.

## Evidence on Hand

Real and usable today:

- Console routes and views in `src/pam_pm/web/app.py` with templates in `src/pam_pm/web/templates/` and tokens in `src/pam_pm/web/static/style.css`.
- Setup flow in `docs/setup.md`, rules in `docs/rules.md`, privacy and safety in `docs/privacy.md`, roadmap in `docs/roadmap.md`.
- Screen captures in `docs/images/` for setup, rules, board, activity, plan, and review.
- Tests in `tests/` covering masking, cost meter, prompt prefix stability, rules with fake adapters, review, plan, and web routes.

Absences that future work must not fabricate: no customer names, no testimonials, no benchmarks, no pricing, no license claims beyond MIT, no deployment claims beyond local loopback plus a compose option, and no RAID or Settings screen captures yet. Do not invent user quotes or success numbers.

## Product Principles

- Evidence first: every board write carries the quoted words behind it, and reversible writes can be undone.
- Private by default: raw answers stay in chat. The console shows outcomes, not transcripts.
- Small steps that stay green: setup, rules, and review each move through short checks with clear pass states.
- Boring technology on purpose: server rendered pages, minimal JS, and hand-written CSS keep the console fast and maintainable.
- Finish the loop: a blocker is not done at intake. It is done when the ETA is set, the link exists, and the waiting person is told it cleared.

## Accessibility & Inclusion

WCAG 2.2 AA for all console screens. Concretely: visible focus states, sufficient contrast in both themes, status never shown by color alone (labels plus dots), live regions for async results and the activity stream, keyboard access to all actions including Undo and Approve, and reduced motion respected for card moves and stream updates.
