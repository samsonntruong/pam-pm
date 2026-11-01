# MORNING.md: phase 2 final status for Samson

Phase 2 is complete through WP8. Repo is private at samsonntruong/pam-pm, branch main at v0.2.0 tag, all pushed. Phase-2 branch merged. Read VIDEO_SCRIPT.md first, then record the video, make the repo public, submit.

## What works
- One-command install: install.ps1 and install.sh with uv 0.11.1, verified end to end in sandbox (install, pam version, server start). Correct form is `uv tool install "pam-pm @ source"`, proven live.
- Setup wizard with zero terminal steps: backend choice, provider, Slack and ADO credentials all save from the browser to 0600 files in the data directory. Full wizard walk passes on the real installed artifact with no env vars: saves plus Test buttons plus complete all green.
- Two P0 bugs found and fixed by fresh-install testing: uv package spec mismatch and a setup token that could never validate (cli imported web.app too early).
- Native Pam board: editable kanban, sprints, CSV, dependency links, Move to menu, realtime SSE, read-only ADO view with link-outs.
- Pam-owned features on both backends: kickoff from a document, RAID log, triage rule, next-sprint proposal, at-Pam add task, mid-sprint adjustment, provider picker (Meta default plus custom endpoint).
- Listen (beta): realtime transcription verified live with synthetic speech, notes plus approval card, meetings list, consent flow, no audio stored by default.
- Desktop wrapper (beta): pam desktop window plus tray, [desktop] extra, installers default it on Win and Mac.
- Autostart and recovery: autostart on all OS, WAL outbox exactly once, misfire catch-up, offline banner, kill-and-restart tests.
- Web console: 15 screens with Settings, global theme, light and dark, detector exit 0, 20-plus real screenshots.
- 278 tests pass with no credentials, ruff clean, gitleaks clean on full history, fresh-install check green.

## Tested live
- Meta Responses API probes, tool calling, image input, SAM segmentation returning masks and boxes on synthetic images.
- ADO full loop: list, create, delete, Dependency links, Agile Closed mapping, waiter blocker link, poll watermark.
- Slack auth plus DM delivery (a few test DMs went to the test user during verification).
- Realtime transcription: partials building to a full sentence on synthetic speech in ENDPOINTING mode, final persisted. PUSH_TO_TALK yields nothing, so ENDPOINTING is the default.
- Fresh install plus full wizard on the real packaged artifact with test credentials in dry run, timed and green.
- install.ps1 analyzer zero warnings, install.sh end to end, server start on 127.0.0.1:8765.

## Tested with fakes only
- All rules end to end, interviewer pushback, review and retro, kickoff owner flow, RAID cascades, triage model gating, notes extraction, desktop single instance and tray (mocked GUI).
- Multi person live Slack story with humans answering (no teammate tokens were available to the builder).
- Live SIGKILL against production services (simulated kill plus restart is covered by tests on both backends).
- Windows and macOS install-test workflow runs plus Docker compose build (no Docker or Mac in the sandbox, token cannot read Actions to confirm CI).

## Cut or deferred
- Nothing cut from WP1 to WP4, WP3b, WP7, WP8, tests, or README. WP3c desktop is beta as specified.
- Fly.io managed-host guide is marked untested (no paid account used). VPS plus Caddy guide follows the standard pattern.
- Docker compose up was not run here (no Docker in the sandbox).

## Spend and caching
- Phase 1 build about 0.18. Phase 2 Muse Code tasks about 1.35. Real probes about 0.001. Combined total about 1.54 of the 100 cap. No alerts needed.
- Cached share 93 to 95 percent across build tasks. Product calls log cached tokens per call per META_API_NOTES rules.

## Samson checklist (only human items remain)
1. Add two people to the Slack workspace and the ADO org with matching emails, or add PAM_TEST_SLACK_USER_TOKEN_1 to 3 for the scripted live story.
2. Try Pam yourself for a few minutes: install with the one-liner, run the wizard, answer a check-in vaguely and watch the pushback.
3. Record the video from VIDEO_SCRIPT.md (2:50 or less): one-liner install, wizard choosing Pam board, kickoff, check-in pushback, blocker with link then clear, RAID log, review plus next sprint. Record live on Standard.
4. Retake any console screenshots you want with real data (current ones use neutral invented names).
5. Check CI on GitHub (Actions tab): ci.yml plus install-test.yml on Windows and Linux.
6. Run gitleaks yourself if you want a second opinion (clean here on 29 plus commits).
7. Set the repo description and topics (ai, scrum, slack, azure-devops, agents, muse-spark, project-management): the build token lacks permission (403), so do it in repo Settings.
8. Make the repo public yourself, upload the video to YouTube, paste SUBMISSION.md into the form.
9. After submitting: revoke the pam-build Meta key, GitHub token, ADO PAT, Slack tokens. Optionally remove the pam-builder route and the phase2-watchdog cron job.
