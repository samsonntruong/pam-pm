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

Instead of a standup, Pam sends each teammate a private Slack message about their work and pushes back on vague answers like "almost done" until it has what remains and an ETA. It keeps the Azure DevOps board in sync, and every board change shows the person's own words as evidence and can be undone. When someone says they are blocked, Pam asks a few questions, creates a real dependency link between the work items, asks the blocking owner for an ETA, and tells the waiting person as soon as the blocker clears. At sprint end it synthesizes a review and retro and turns retro actions into new work items. Planning can start from a photo of a sticky-note board: SAM 3.1 segments each note, Muse Spark reads it, and Pam proposes a sprint for approval.

It runs on any always-on machine with no inbound ports (Slack Socket Mode, Azure DevOps polling). A local web console walks you through setup with Test buttons and shows the live board, activity and undo.

The problem: I lead data and AI delivery teams, and chasing status is the most repetitive part of the job. Standups are shallow, boards drift from reality, and nobody tells you when the thing you are waiting on is done. Pam is the teammate I wish I had.

## 2. Public GitHub Repository (paste)

https://github.com/samsonntruong/pam-pm   [CONFIRM: Samson makes the repo public in the morning. Private now. Fresh clone check passed: pytest, wheel build with templates, pip install from wheel, pam version. Docker compose and real browser screenshots are morning tasks.]

## 3. 3-Minute Demo Video (paste)

[CONFIRM: record in the morning, 3:00 or shorter, Public or Unlisted]
Video outline (2:30): setup wizard, sticky-note photo to sprint, "almost done" challenged, blocker with dependency link, blocker clears, review and retro. Record the whole thing live on the Standard tier: real Slack, real Azure DevOps, and the real console, including the setup wizard.

## 4. Describe your use of Meta products to build the submitted project (paste after confirming)

Meta products used to BUILD it:
- Muse Spark 1.3 ran the build orchestrator: the Hermes Agent profile that planned, reviewed and tested the build used Muse Spark 1.3 through the Meta Model API. Hermes own usage is part of shared credits; measured build spend below covers Muse Code tasks plus real probes.
- Muse Code was the coding harness: Hermes Agent orchestrated the build and handed each task to Muse Code (`muse exec` with --max-model-steps and --yolo), which wrote the code, tests and docs. 12 Muse Code tasks: T0a minimal, T0b expansion, M0 scripts, M1a core, M1b adapters and client, M1c interviewer, M2a rules, M2b state map, M3a console, M3b review and plan, M6a docs, M6b shots. Muse Code wrote essentially all application code. Hermes wrote no application code; Hermes edited DEVLOG.md, MORNING.md and this file only. Measured Muse Code spend 0.1649 dollars over 14.8M input tokens at 94.3 percent cached, plus T0a estimate 0.01 and real probes 0.0001, total about 0.18 of the 100 cap. Tiers: Contributor for all build and test runs with fake data, priced at Contributor rates.
- Muse Spark 1.3 through Meta Model API during development: refined interviewer prompts and pushback wording, created scripted test personas for vague answers, and generated test fixtures for masking and cost meter tests. All test content was scrubbed with the masking function before committing.

Meta products used IN the product:
- Muse Spark 1.3 (Meta Model API, `muse-spark-1.3`) runs the interview agents, the blocker intake questions, the structured status extraction, and the sprint review and retro synthesis. It uses native tool calling and structured output, with one interviewer subagent per teammate running in parallel.
- SAM 3.1 (`sam-3.1`) segments each sticky note in a photo of a planning board. Confirmed sam-3.1 is listed in the models endpoint. The product tries SAM crops first and falls back to whole image read through Muse Spark vision when SAM shape is unavailable. Fallback path is implemented and tested with mocks; live SAM shape confirmation is a morning task.
- Muse Spark vision reads each note crop into a work item (title, type, owner, estimate).
- Prompt caching: all interviewers share one cached system prompt and tool list. Measured cached share: 94.3 percent across build tasks, 21 percent on the first cold real probe as expected. Console footer and pam doctor show cached share per call.
- Measured cost of a full run on Standard: not yet measured live. Build estimate from META_API_NOTES rates: one interview turn about 0.0084 dollars on Standard with caching, full sprint run well under 2 dollars. Morning task: record the live run cost and replace this line.

Build and video used the Contributor tier only for development with fake data. Standard is the product default for real teams.

## 5. Why are Meta's models important to your solution? (paste after confirming)

Pam is a lot of small, private conversations running in parallel, each ending in a tool call that changes a real board. That workload depends on three things Meta's stack provides together:

1. Agentic tool use at low cost. Each teammate is a separate subagent that calls tools to read and update Azure DevOps. Muse Spark's native tool calling, parallel agents, and structured output make that reliable, and the pricing (with cheap cached input for shared system prompts) makes frequent check-ins affordable for a small team rather than a luxury.
2. Vision and segmentation in one platform. Planning often starts on a wall of sticky notes. SAM 3.1 finds each note precisely and Muse Spark reads it, through the same API and key, so photo to sprint is one pipeline instead of stitching vendors together.
3. Privacy options and portability. Standard tier is not used to improve Meta products, which matters because Pam reads private status conversations, and the Contributor tier gives developers a very cheap way to test with fake data. The OpenAI-compatible API means the model client is swappable, so the open-source project is not locked to one vendor.

Concrete thing observed: repeated system prompts across parallel interviewers hit 94.3 percent cached input in real build usage, which is what makes daily checkins affordable.

## Final pre-submit checklist (Samson)

- [ ] Repo is public; README quickstart works from a fresh clone; secret scan passed.
- [ ] Video is 3:00 or shorter, plays while logged out, link pasted.
- [ ] All [CONFIRM] markers resolved; nothing claimed that was not built.
- [ ] No client names, employer names, or real work data in repo, video, or answers.
- [ ] Submitted before Nov 4, 6 PM PT (9 PM ET), ideally by 3 PM PT.
