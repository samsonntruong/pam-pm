# SUBMISSION: Meta Global AI Developer Hackathon (Cerebral Valley)

Form: "Submit Project". Deadline: Nov 4, 2026, 9:00 PM ET (6:00 PM PT). Aim to submit by 3 PM PT on Nov 4, or Nov 3 night.

The form screenshot shows these required fields (there may be a few above it such as project name or track; fill those from the "Basics" block below):
1. Project Description
2. Public GitHub Repository
3. 3-Minute Demo Video (YouTube link)
4. Describe your use of Meta products to build the submitted project
5. Why are Meta's models important to your solution?

Rules for whoever fills this in: only claim what was really built and used. No em dashes.

## Basics

- Project name: Pam
- One-liner: Pam is an open-source AI project manager that replaces status meetings.
- Track: Indie Dev
- Theme: Build for Your World
- Repo name: pam-pm

## 1. Project Description (paste)

Pam (Projects Autonomously Managed) is an open-source AI project manager that replaces status meetings.

Instead of a standup, Pam sends each teammate a private Slack message about their work and pushes back on vague answers like "almost done" until it has what remains and an ETA. It keeps the board in sync on the built-in Pam board or Azure DevOps, and every board change shows the person's own words as evidence and can be undone. When someone says they are blocked, Pam asks a few questions, creates a real dependency link between the work items, asks the blocking owner for an ETA, and tells the waiting person as soon as the blocker clears. At sprint end it synthesizes a review and retro and turns retro actions into new work items.

Planning starts three ways: kickoff from a short written brief with owner suggestions on one approval card, a photo of a sticky-note board where SAM 3.1 segments each note and Muse Spark reads it before Pam proposes a sprint for approval, or a next-sprint proposal built from retro actions plus carryover plus backlog. During the sprint, board triage scans for stale, unassigned, and blocked items with proposed fixes, mid-sprint adjustment proposes goal-safe changes when the sprint drifts off track, and anyone can type `@Pam add task` in chat to draft one task for approval. A RAID log tracks risks, assumptions, issues, and decisions with owners and links to work.

Setup is one PowerShell command on Windows (one shell command on macOS or Linux), then a local web console walks you through a wizard with Test buttons on every step, including a model provider picker (Meta by default, or a custom OpenAI-compatible endpoint). Listen (beta) captures meetings in the browser with live transcription, notes, and an approval card for action items. A desktop wrapper (beta) runs Pam in its own window with a tray icon. Autostart at sign-in, exactly-once delivery after restarts, and scheduler catch-up keep it always on.

It runs on any always-on machine with no inbound ports (Slack Socket Mode, board polling). The product defaults to the Standard tier for real team text; the Contributor tier was used only for building and testing with fake data.

The problem: I lead data and AI delivery teams, and chasing status is the most repetitive part of the job. Standups are shallow, boards drift from reality, and nobody tells you when the thing you are waiting on is done. Pam is the teammate I wish I had.

## 2. Public GitHub Repository (paste)

https://github.com/samsonntruong/pam-pm

Status at time of writing: the repo is private and Samson makes it public in the morning. Verified in the sandbox before handover: all 272 tests pass, ruff is clean, the wheel builds with templates and static files and contains no test audio, and the README quickstart matches the installer scripts.

## 3. 3-Minute Demo Video (paste)

YouTube link to be added after Samson records in the morning. Target length 2:50 or shorter, set to Public or Unlisted.

Video outline (2:50, see VIDEO_SCRIPT.md for the shot list with timestamps and exact text to type): the PowerShell one-liner install, the setup wizard choosing the Pam board, kickoff from a short brief with owner suggestions, a check-in where "almost done" gets pushed back, a blocker that creates a dependency link and then clears with a notification, the RAID log, and review plus next-sprint proposal. Record the whole thing live on the Standard tier: real Slack with the throwaway test accounts, the real Pam board, and the real console, including the setup wizard.

## 4. Describe your use of Meta products to build the submitted project (paste)

Meta products used to BUILD it:
- Muse Spark 1.3 ran the build orchestrator: the Hermes Agent profile that planned, reviewed and tested the build used Muse Spark 1.3 through the Meta Model API. Hermes own usage is part of shared credits; measured build spend below covers Muse Code tasks plus real probes.
- Muse Code was the coding harness: Hermes Agent orchestrated the build and handed each task to Muse Code (`muse exec` with --max-model-steps and --yolo), which wrote the code, tests and docs. 27 Muse Code tasks are logged in the DEVLOG.md Meta usage table: T0a minimal skeleton, T0b expansion, M0 scripts, M1a core, M1b adapters and client, M1c interviewer, M2a rules, M2b state map, M3a console, M3b review and plan, WP-1 Slack script fix, WP-1 SAM discovery, WP1 backends and wizard, WP2 native board, WP3 installer, WP3b recovery, WP3c desktop, WP4 UI quality (joint with Impeccable 4.8.0), WP5 kickoff, WP5 RAID, WP5 Listen, WP5 triage, WP5 next sprint, WP5 add task, WP5 mid sprint, WP5 provider picker, WP6 cloud readiness. Muse Code wrote essentially all application code. DEVLOG.md logs no exceptions where Hermes wrote application code directly; the build rule requires logging each such exception, and none is logged. Hermes edited DEVLOG.md, MORNING.md, and submission docs only. Measured build spend logged in DEVLOG.md: phase 1 about 0.16 dollars at M3 rates (T0b about 0.010, M0 scripts about 0.014, M1 about 0.049, M2a about 0.016, M3 about 0.071, real probes about 0.0001), cached share 94 to 95 percent across build tasks, cold real probe 113 of 539 input tokens cached (about 21 percent) as expected. Cumulative build spend tracked in PHASE2_PROGRESS.md is about 1.24 dollars of the 100 cap. Build and testing used the Contributor tier with fake data only, priced at Contributor rates.
- Muse Spark 1.3 through Meta Model API during development: refined interviewer prompts and pushback wording, created scripted test personas for vague answers, and generated test fixtures for masking and cost meter tests. All test content was scrubbed with the masking function before committing.

Meta products used IN the product:
- Muse Spark 1.3 (Meta Model API, `muse-spark-1.3`) runs the interview agents, the blocker intake questions, the structured status extraction, and the sprint review and retro synthesis. It uses native tool calling and structured output, with one interviewer subagent per teammate running in parallel. Standard is the product default for real teams.
- SAM 3.1 (`sam-3.1`) segments each sticky note in a photo of a planning board. The Responses API shape was confirmed live on a synthetic image in WP-1 (masks and boxes returned). The product tries SAM crops first and falls back to whole image read through Muse Spark vision when SAM output is unavailable. The fallback path is implemented and tested with mocks. With a custom provider endpoint, planning always uses the whole-image read because SAM needs the Meta endpoint.
- Muse Spark vision reads each note crop into a work item (title, type, owner, estimate).
- Realtime transcription for Listen (beta) uses model `muse-voice-transcribe-1.0` with mode ENDPOINTING, verified live with synthetic speech (cumulative partials plus one speechComplete final per turn). PUSH_TO_TALK returned no transcript for the same audio, so ENDPOINTING is the default.
- Prompt caching: all interviewers share one cached system prompt and tool list built by a single prompt builder with byte-stable prefixes (covered by a prefix stability test). Measured cached share: 94 to 95 percent across build tasks per DEVLOG.md. Console footer and pam doctor show cached share per call.
- Measured cost of a full run on Standard: not measured live. Build estimate from META_API_NOTES rates: one interview turn about 0.0084 dollars on Standard with caching, full sprint run well under 2 dollars.

Testing: 272 tests pass with ruff clean (DEVLOG.md WP6 row: 272 pass; verified in the sandbox during WP7). Tested live against real services with invented data: Meta Responses probe with tool and image, SAM segmentation on a synthetic image, Azure DevOps link creation plus polling (Agile uses Closed, not Done), Slack auth and DM delivery, the blocker link and poll probe, and the Listen realtime streaming run. Everything else is tested with fake adapters, including the full story test (plan, check-in with pushback, blocker with dependency link, blocker clears, review and retro) on both backends.

Build and testing used the Contributor tier only for development with fake data. Standard is the product default for real teams.

## 5. Why are Meta's models important to your solution? (paste)

Pam is a lot of small, private conversations running in parallel, each ending in a tool call that changes a real board. That workload depends on three things Meta's stack provides together:

1. Agentic tool use at low cost. Each teammate is a separate subagent that calls tools to read and update the board. Muse Spark's native tool calling, parallel agents, and structured output make that reliable, and the pricing (with cheap cached input for shared system prompts) makes frequent check-ins affordable for a small team rather than a luxury.
2. Vision, segmentation, and speech in one platform. Planning often starts on a wall of sticky notes, and meetings need notes. SAM 3.1 finds each note precisely, Muse Spark reads it, and the realtime endpoint transcribes meetings, through the same API and key, so photo to sprint and meeting to action items are each one pipeline instead of stitching vendors together.
3. Privacy options and portability. Standard tier is not used to improve Meta products, which matters because Pam reads private status conversations, and the Contributor tier gives developers a very cheap way to test with fake data. The OpenAI-compatible API means the model client is swappable, so the open-source project is not locked to one vendor.

Concrete thing observed: repeated system prompts across parallel interviewers hit 94 to 95 percent cached input in real build usage per DEVLOG.md, which is what makes daily checkins affordable.

## Final pre-submit checklist (Samson)

- [ ] Repo is public; README quickstart works from a fresh clone; secret scan passed.
- [ ] Video is 3:00 or shorter, plays while logged out, link pasted.
- [ ] All numbers above match DEVLOG.md; nothing claimed that was not built.
- [ ] No client names, employer names, or real work data in repo, video, or answers.
- [ ] Submitted before Nov 4, 6 PM PT (9 PM ET), ideally by 3 PM PT.
