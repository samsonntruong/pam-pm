# NEXT SPRINT WP4 Loop: Next sprint card

Method: single-context inline review plus deterministic detector run, same
collapsed method as CRITIQUE_BEFORE.md so scores compare. Mode: Operate
(console task UI), all 10 heuristics apply. No .impeccable snapshots.

Evidence: source read for next_sprint.html, base.html nav, app.py next
sprint card HTML plus run and approve routes. Detector run on the next
sprint template before and after fixes (see Detector).

Scoreboard:

| Screen | Critique before | Critique after | Audit before | Audit after |
|---|---|---|---|---|
| Next sprint (/next-sprint) | 24/40 Acceptable | 29/40 Good | 15/20 Good | 17/20 Good |

Critique bands: 36-40 Excellent, 28-35 Good, 20-27 Acceptable, 12-19
Poor, 0-11 Critical. Audit bands: 18-20 Excellent, 14-17 Good, 10-13
Acceptable, 6-9 Poor, 0-5 Critical.

## Next sprint before

Critique: 24/40, Acceptable.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 2 | Count plus dry run pill, but empty state has no next step |
| 2 | Match System / Real World | 3 | Plain sprint words |
| 3 | User Control and Freedom | 3 | Propose reverses, Approve guarded by dry run, Undo in Activity |
| 4 | Consistency and Standards | 2 | Card nested in card, kind labels read as code |
| 5 | Error Prevention | 2 | Dry run default, but cost and safety unnamed until after propose |
| 6 | Recognition Rather Than Recall | 2 | Kind plus owner plus reason in one line, hours missing |
| 7 | Flexibility and Efficiency | 1 | Single path, no shortcut |
| 8 | Aesthetic and Minimalist Design | 2 | Clean but nested cards read heavy |
| 9 | Error Recovery | 3 | Pills plus retry hints, approve result announced |
| 10 | Help and Documentation | 2 | One intro line, no cost note or Board pointer |
| **Total** | | **24/40** | **Acceptable** |

Audit: 15/20, Good.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 3 | Real buttons, labels, live regions throughout |
| 2 | Performance | 4 | Tiny page, no page specific JS |
| 3 | Theming | 4 | Shared toggle, no custom colors |
| 4 | Responsive Design | 2 | Nested cards waste width on narrow screens |
| 5 | Implementation Integrity | 2 | Hours missing vs Slack, terse kinds, bare empty state |
| **Total** | | **15/20** | **Good** |

Top findings:

- [P1] Console card omits estimate hours though the Slack post shows
  them, so the two renderings of one proposal disagree.
- [P1] Kind labels (retro, carryover, backlog) read as code, not words.
- [P2] Items render as cards nested inside the result card, which the
  craft floor bans and which reads heavy.
- [P2] Empty state names the gap but gives no next step.
- [P2] Propose card names no cost or dry run safety before the run.

## Fixes

- [P1, fixed] Each item line now shows plain kind, hours, owner, and
  reason, matching the Slack post: Retro action, Carryover, Backlog.
- [P2, fixed] Items render as plain articles inside the result card,
  no nested cards. Load and Risk carry strong labels for scanning.
- [P2, fixed] Empty state: nothing proposed plus a next step to add
  backlog items on the Board, then Propose again.
- [P2, fixed] Propose card states the run is plain code and costs
  nothing, and that Approve proposes first when dry run is on.
- Busy button handling and error pills keep coming from the base
  handler. Card wrap and approve result keep their aria-live regions.

Left open, with reason:

- [P2, open] Approve moves the whole sprint as one set, with no per
  item remove. Reason: curation happens on the Board before proposing,
  and one Approve keeps evidence clear. Per item control is scheduled
  after lead feedback.
- [P3, open] Throughput per person stays out of the card. Reason: the
  card stays scannable with load only, and full numbers ship in the
  proposal JSON. A load detail row is scheduled.

No open high severity findings on this screen.

## Detector

Command: `.github/skills/impeccable/scripts/impeccable detect --json`
over `src/pam_pm/web/templates/next_sprint.html`.

Result before fixes: exit 0, zero findings (`[]`). Clean baseline.
Result after fixes: exit 0, zero findings (`[]`). Clean, no new warnings.

## After scores

Next sprint: 24/40 Acceptable before, 29/40 Good after. H1 2 to 3
(empty next step plus count and pills), H4 2 to 3 (plain kind words,
no nested cards), H6 2 to 3 (hours plus plain kinds plus labeled Load
and Risk), H8 2 to 3 (lighter list), H10 2 to 3 (cost note plus Board
pointer). Audit: 15/20 Good before, 17/20 Good after. Responsive 2 to
3 (single column without nested card waste), implementation integrity
2 to 3 (Slack parity on hours, plain kinds, guided empty state).
