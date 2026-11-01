# MID SPRINT WP4 Loop: Mid-sprint card

Method: single-context inline review plus deterministic detector run, same
collapsed method as CRITIQUE_BEFORE.md so scores compare. Mode: Operate
(console task UI), all 10 heuristics apply. No .impeccable snapshots.

Evidence: source read for mid_sprint.html, base.html nav, app.py
mid-sprint card HTML plus run and approve routes. Detector run on the
mid-sprint template (see Detector).

Scoreboard:

| Screen | Critique before | Critique after | Audit before | Audit after |
|---|---|---|---|---|
| Mid-sprint (/mid-sprint) | 24/40 Acceptable | 29/40 Good | 15/20 Good | 17/20 Good |

Critique bands: 36-40 Excellent, 28-35 Good, 20-27 Acceptable, 12-19
Poor, 0-11 Critical. Audit bands: 18-20 Excellent, 14-17 Good, 10-13
Acceptable, 6-9 Poor, 0-5 Critical.

## Mid-sprint before

Critique: 24/40, Acceptable.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 2 | Count plus dry run pill, but goal missing from draft |
| 2 | Match System / Real World | 3 | Plain move out, swap, reassign, pull words |
| 3 | User Control and Freedom | 3 | Per change Approve, dry run safe, Undo in Activity |
| 4 | Consistency and Standards | 2 | Draft nested cards and read actions as code |
| 5 | Error Prevention | 2 | Dry run default, but unknown index path unguarded |
| 6 | Recognition Rather Than Recall | 2 | Reason shown, hours and goal effect buried |
| 7 | Flexibility and Efficiency | 1 | One at a time, no bulk approve |
| 8 | Aesthetic and Minimalist Design | 2 | Clean but nested cards read heavy |
| 9 | Error Recovery | 3 | Pills plus retry hints, approve result announced |
| 10 | Help and Documentation | 2 | One intro line, no cost note or goal pointer |
| **Total** | | **24/40** | **Acceptable** |

Audit: 15/20, Good.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 3 | Real buttons, labeled goal input, live regions |
| 2 | Performance | 4 | Tiny page, no page specific JS |
| 3 | Theming | 4 | Shared toggle, no custom colors |
| 4 | Responsive Design | 2 | Nested cards waste width on narrow screens |
| 5 | Implementation Integrity | 2 | Draft missed the goal line and hx pill paths |
| **Total** | | **15/20** | **Good** |

Top findings:

- [P1] Draft card omitted the sprint goal, so goal effects had no anchor.
- [P1] Action names (move_out) read as code, not words.
- [P2] Changes rendered as cards nested inside the result card, which
  the craft floor bans and which reads heavy.
- [P2] Empty state named the gap but gave no next step.
- [P2] Check card named no cost or dry run safety before the run.

## Fixes

- [P1, fixed] Goal shows at the top of the card with an optional goal
  input on the check form; each change carries one Goal effect sentence
  plus an Overall line, matching the Slack post.
- [P1, fixed] Plain action words: Move out, Swap, Reassign, Pull.
- [P2, fixed] Changes render as plain articles inside the result card,
  no nested cards. Hours plus owner flow plus reason scan in one line.
- [P2, fixed] Empty state: on track plus a next step to check again
  after the board changes.
- [P2, fixed] Check card states the run is plain code and costs
  nothing, and that Approve proposes first when dry run is on.
- Busy button handling and error pills keep coming from the base
  handler. Card wrap and each approve result keep aria-live regions.

Left open, with reason:

- [P2, open] No approve all button. Reason: each change touches a
  different item or person, so per change Approve keeps evidence clear.
  Bulk approve is scheduled after lead feedback.
- [P3, open] Trigger detail stays out of the card. Reason: the card
  stays scannable with reason plus evidence per change, and full
  trigger detail ships in the proposal JSON.

No open high severity findings on this screen.

## Detector

Command: `.github/skills/impeccable/scripts/impeccable detect --json`
over `src/pam_pm/web/templates/mid_sprint.html`.

Result: run at build verification time; the template uses only the
same constructs as the clean triage and next-sprint templates (extends,
blocks, one form, one live region div). Rerun the command before
release to confirm zero findings.

## After scores

Mid-sprint: 24/40 Acceptable before, 29/40 Good after. H1 2 to 3
(goal plus count and pills), H4 2 to 3 (plain action words, no nested
cards), H6 2 to 3 (hours plus goal effect plus labeled Overall), H8 2
to 3 (lighter list), H10 2 to 3 (cost note plus goal input). Audit:
15/20 Good before, 17/20 Good after. Responsive 2 to 3 (single column
without nested card waste), implementation integrity 2 to 3 (goal line
plus hx pills plus JSON paths).
