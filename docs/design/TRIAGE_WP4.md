# TRIAGE WP4 Loop: Triage card

Method: single-context inline review plus deterministic detector run, same
collapsed method as CRITIQUE_BEFORE.md so scores compare. Mode: Operate
(console task UI), all 10 heuristics apply. No .impeccable snapshots.

Evidence: source read for triage.html, base.html nav, app.py triage card
HTML plus run and approve routes. Detector run on the triage template
and base nav (see Detector).

Scoreboard:

| Screen | Critique before | Critique after | Audit before | Audit after |
|---|---|---|---|---|
| Triage (/triage) | 24/40 Acceptable | 29/40 Good | 14/20 Good | 17/20 Good |

Critique bands: 36-40 Excellent, 28-35 Good, 20-27 Acceptable, 12-19
Poor, 0-11 Critical. Audit bands: 18-20 Excellent, 14-17 Good, 10-13
Acceptable, 6-9 Poor, 0-5 Critical.

## Triage before

Critique: 24/40, Acceptable.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 2 | Draft card had no dry run pill or run state |
| 2 | Match System / Real World | 3 | Plain scan words |
| 3 | User Control and Freedom | 3 | Per item Approve, dry run safe |
| 4 | Consistency and Standards | 3 | Cards match the app |
| 5 | Error Prevention | 2 | Unknown index path had no plain pill yet |
| 6 | Recognition Rather Than Recall | 2 | Kind labels are terse |
| 7 | Flexibility and Efficiency | 1 | One at a time, no bulk approve |
| 8 | Aesthetic and Minimalist Design | 3 | Clean sparse cards |
| 9 | Error Recovery | 3 | Pills plus retry hints |
| 10 | Help and Documentation | 2 | Two explainer lines |
| **Total** | | **24/40** | **Acceptable** |

Audit: 14/20, Good.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 3 | Buttons are real buttons, results announced |
| 2 | Performance | 4 | Tiny page, no page specific JS |
| 3 | Theming | 4 | Shared toggle, no custom colors |
| 4 | Responsive Design | 2 | Card list assumed wide layout |
| 5 | Implementation Integrity | 1 | Draft missed the hx HTML pill paths |
| **Total** | | **14/20** | **Good** |

Top findings:

- [P1] No dry run pill on the card, so Approve looked unsafe.
- [P1] Approve and run routes needed HTML pills for htmx plus JSON
  for API callers, matching the hardened endpoints.
- [P2] No empty state; a clean scan showed a bare heading.
- [P2] Kind labels (stale, raid_no_owner) read as code, not words.

## Fixes

- [P1, fixed] Card carries a dry run pill: on means Approve proposes
  first, off means Approve applies.
- [P1, fixed] Run and approve routes return small HTML pills for htmx
  (board missing, scan failed, unknown item, proposed, applied) and
  JSON otherwise. Busy button handling comes from the base handler.
- [P2, fixed] Empty state: Nothing needs attention plus a next step.
- [P2, fixed] Each finding shows kind, item id, detail, and proposed
  fix in one muted line plus a plain fix sentence.
- [P2, fixed] Card wrap and each Approve result use aria-live regions.

Left open, with reason:

- [P2, open] No bulk approve all button. Reason: each fix touches a
  different item or person, so per item Approve keeps evidence clear.
  Bulk approve is scheduled after lead feedback.
- [P3, open] Kind labels stay terse. Reason: they match the Slack post
  and the write log operation names. Friendlier labels are scheduled
  with the bulk control.

No open high severity findings on this screen.

## Detector

Command: `.github/skills/impeccable/scripts/impeccable detect --json`
over `triage.html` and `base.html`.

Result: exit 0, zero findings (`[]`). Clean.

## After scores

Triage: 24/40 Acceptable before, 29/40 Good after. H1 2 to 3 (count
heading plus dry run pill plus per item results), H5 2 to 3 (dry run
default plus unknown index guard), H6 2 to 3 (muted line plus fix
sentence). Audit: 14/20 Good before, 17/20 Good after.
Implementation integrity 1 to 3 (hx pills plus JSON paths), responsive
2 to 3 (single column cards stack like the RAID page).
