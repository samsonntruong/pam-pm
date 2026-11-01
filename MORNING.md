# MORNING.md: status for Samson, Nov 1 morning

Overnight build done through M6 docs. Repo is private at samsonntruong/pam-pm, branch main, all pushed.

## What works
- Repo skeleton, packaging, CLI with pam and pam-pm entry points, Docker files, CI.
- Core engine: masking, cost meter, store with evidence log and undo, writes with dry run default.
- Prompt caching from day one: one build_messages function, stable prefix, volatile last, per call cached logging, prefix stability test.
- ModelClient on Meta Responses API with chat fallback, tool_choice auto, 4000 plus max tokens, retry with backoff, 4 parallel cap.
- Interviewer with pushback on vague answers, 6 turn cap, structured result.
- Rules engine with scheduler, ADO poller watermark, morning checkin, follow up on close, blocker intake.
- ADO state map: Agile uses Closed, Scrum uses Done, close_item and is_closed helpers.
- Web console on 127.0.0.1:8765 with setup wizard Test buttons, rules, board read only, activity with evidence and Undo, review, plan with photo upload and Approve.
- Review synthesis with 6000 plus tokens, retro actions tagged retro-action, vision with SAM first then fallback to full image read.
- 56 tests pass plus fresh clone check with wheel build and template check.

## Tested for real
- Meta Responses API probe with tool and 1px image: pass, 539 in 113 cached 302 out, cost 0.000103 dollars.
- SAM 3.1 listed in models: pass, request shape still TBD, fallback path implemented.
- ADO: list projects teams iterations pass, create and delete items pass, Dependency Forward and Reverse link names verified, waiter blocker link pass, poll sees new items pass, Agile Closed verified with probe item 4 created and closed then deleted.
- Slack: auth test pass, DM to test user pass, app token api test fails with missing scope because app tokens cannot call Web API, Socket Mode live test skipped overnight.
- which pam: free, both pam and pam-pm registered.
- Fresh clone check in sandbox: pytest, wheel build, template presence, pip install from wheel, pam version: all pass.
- No Docker in sandbox: docker command not found, host Docker path untested.

## Tested with fakes only
- All three rules end to end, blocker notify on close, interviewer pushback, review and retro, photo plan proposal.
- Web screens load via TestClient. Console screenshots in docs/images are 1x1 placeholders because Playwright browsers are not installed in the sandbox. Retake them with a real browser before the video.
- Full multi person Slack run with humans answering. Morning task with the video.

## Failed or cut
- Slack app token check script gives FAIL missing_scope. It is a script bug, not a credential bug. Fix: validate xapp prefix and skip Web API call, verify Socket Mode with a live connection in the morning.
- SAM 3.1 request shape not confirmed. Fallback to whole image read is in place. Time box it to 1 hour in the morning or ship fallback.
- Docker compose up not run here. Run it on your machine before making public.
- Gitleaks binary not installed here. Test fixtures contain fake tokens like xoxb-fake123 for redaction unit tests. Run gitleaks before going public and add an allowlist for tests if it flags them.
- No em dashes in source. Design docs in docs/design still mention Samson by name as build owner. That is the author name, kept intentionally.

## Spend so far
- Muse Code measured 0.1649 dollars over 11 tasks, 14.8M input tokens at 94.3 percent cached.
- T0a first task usage not captured, estimate 0.01. Real Meta checks 0.0001. Total about 0.18 of the 100 cap.
- No spend alerts needed. Reserve held for product test runs and video recording.

## You must do in the morning
1. Add two people to the Slack workspace and the ADO org with matching emails so roster auto match works.
2. Run pam start, complete the wizard with real accounts, press each Test button until green.
3. Run the blocker loop live: morning checkin, vague answer challenged, blocker intake with link, owner ETA, close and notify, review and retro.
4. Record the video live, 3 minutes or less: setup, photo plan, checkin, blocker, blocker clears, review.
5. Retake console screenshots with a real browser into docs/images.
6. Run docker compose up on your machine, then the fresh clone check script.
7. Run gitleaks, then make the repo public yourself, upload the video, paste SUBMISSION.md.
8. Revoke the pam-build Meta key, GitHub token, ADO PAT, Slack tokens after submitting.
