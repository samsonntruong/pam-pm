# LISTEN WP4 Loop: Listen and Meetings

Method: single-context inline review plus deterministic detector run, same
collapsed method as CRITIQUE_BEFORE.md so scores compare. Mode: Operate
(console task UI), all 10 heuristics apply. No .impeccable snapshots.

Evidence before: source read for listen.html, meetings.html, meeting.html,
listen.js, style.css pills, app.py notes approval and delete API. Detector
run on the four listen files before fixes (see Detector before).

Scoreboard:

| Screen | Critique before | Critique after | Audit before | Audit after |
|---|---|---|---|---|
| Listen (/listen) | 22/40 Acceptable | 26/40 Acceptable | 13/20 Acceptable | 15/20 Good |
| Meetings list (/meetings) | 22/40 Acceptable | 24/40 Acceptable | 13/20 Acceptable | 15/20 Good |
| Meeting detail (/meetings/id) | 21/40 Acceptable | 24/40 Acceptable | 14/20 Good | 16/20 Good |

Critique bands: 36-40 Excellent, 28-35 Good, 20-27 Acceptable, 12-19
Poor, 0-11 Critical. Audit bands: 18-20 Excellent, 14-17 Good, 10-13
Acceptable, 6-9 Poor, 0-5 Critical.

## 1. Listen before

Critique: 22/40, Acceptable.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 2 | Finals not announced, notes link has no live region |
| 2 | Match System / Real World | 3 | Plain capture words, Keep audio line confuses |
| 3 | User Control and Freedom | 3 | Start and Stop reverse, title stays editable |
| 4 | Consistency and Standards | 3 | Cards match the app |
| 5 | Error Prevention | 2 | Title editable mid stream but read only at start |
| 6 | Recognition Rather Than Recall | 2 | No support hint or cap note on the page |
| 7 | Flexibility and Efficiency | 1 | Single path, no shortcut |
| 8 | Aesthetic and Minimalist Design | 3 | Clean sparse cards |
| 9 | Error Recovery | 2 | Error pill plus retry by Start again |
| 10 | Help and Documentation | 1 | Privacy line has a broken docs link |
| **Total** | | **22/40** | **Acceptable** |

Audit: 13/20, Acceptable.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 2 | Labels and roles present, finals not announced |
| 2 | Performance | 3 | Light page plus worklet |
| 3 | Theming | 3 | Shared toggle |
| 4 | Responsive Design | 3 | Cards stack |
| 5 | Implementation Integrity | 2 | Broken docs link, Keep audio text mismatches build |
| **Total** | | **13/20** | **Acceptable** |

Top findings:

- [P1] Finals append to a plain list with no live region, and the notes
  link box has no live region. Screen reader users miss new finals.
- [P1] Privacy card links to /static/../docs/listen which is not a route,
  and says Keep audio stays off unless turned on though no control exists.
- [P2] Title input stays enabled while transcribing though the socket URL
  reads it only at Start. Mid stream edits look saved but do nothing.
- [P2] No consent reminder or two hour cap note on the page. Docs carry
  both, the page carries neither.

## 2. Meetings list before

Critique: 22/40, Acceptable.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 2 | Status is plain text, no pill |
| 2 | Match System / Real World | 3 | Plain sessions language |
| 3 | User Control and Freedom | 2 | Open and Start only, no delete in UI |
| 4 | Consistency and Standards | 2 | Status plain vs pills elsewhere |
| 5 | Error Prevention | 3 | Links only, nothing destructive |
| 6 | Recognition Rather Than Recall | 3 | All sessions visible |
| 7 | Flexibility and Efficiency | 1 | One at a time, no filter |
| 8 | Aesthetic and Minimalist Design | 3 | Clean table |
| 9 | Error Recovery | 2 | Empty state guides to Listen |
| 10 | Help and Documentation | 1 | One explainer line |
| **Total** | | **22/40** | **Acceptable** |

Audit: 13/20, Acceptable.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 3 | Scoped headers, table has no caption |
| 2 | Performance | 3 | Tiny page |
| 3 | Theming | 3 | Shared toggle |
| 4 | Responsive Design | 2 | Wide table with no wrap note |
| 5 | Implementation Integrity | 2 | Status plain, delete only in API |
| **Total** | | **13/20** | **Acceptable** |

## 3. Meeting detail before

Critique: 21/40, Acceptable.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 2 | Status plain, notes cost not named |
| 2 | Match System / Real World | 3 | Transcript plus notes plus approval |
| 3 | User Control and Freedom | 2 | No delete or cancel in UI |
| 4 | Consistency and Standards | 3 | Cards match the app |
| 5 | Error Prevention | 2 | Approve guarded by notes check, delete absent |
| 6 | Recognition Rather Than Recall | 2 | Matches are text only, no board links |
| 7 | Flexibility and Efficiency | 1 | One meeting at a time |
| 8 | Aesthetic and Minimalist Design | 3 | Clean sections |
| 9 | Error Recovery | 2 | Result regions with plain pills |
| 10 | Help and Documentation | 1 | One explainer line |
| **Total** | | **21/40** | **Acceptable** |

Audit: 14/20, Good.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 3 | Live result region, transcript list has no label |
| 2 | Performance | 3 | Light page |
| 3 | Theming | 3 | Shared toggle |
| 4 | Responsive Design | 3 | Single column reflows |
| 5 | Implementation Integrity | 2 | No delete path, approval table has no caption |
| **Total** | | **14/20** | **Good** |

## Detector before

Command: `.github/skills/impeccable/scripts/impeccable detect --json`
over `listen.html`, `meetings.html`, `meeting.html`, `listen.js`.

Result: exit 0, zero findings (`[]`). Clean baseline.

## Fixes

Listen:

- [P1, fixed] Transcript list and notes link box now carry
  `aria-live="polite"`, and the list has an `aria-label`. Finals are
  announced as they arrive.
- [P1, fixed] Privacy card dropped the broken `/static/../docs/listen`
  link and the Keep audio line. It now states audio is not kept and
  carries the consent line to tell the room Pam is transcribing.
- [P2, fixed] Title input locks while transcribing and unlocks on stop,
  close, or mic block. Mid stream edits can no longer look saved.
- [P2, fixed] Session card names Chrome or Edge on desktop, the
  127.0.0.1 or HTTPS mic rule, and the two hour cap.
- [P3, fixed] Transcript list scrolls past 24rem so long sessions do
  not push the page down without bound.

Meetings list:

- [P2, fixed] Status renders as a pill, matching the rest of the app.
- [P2, fixed] Table carries a caption naming order and the delete
  control on the detail page.

Meeting detail:

- [P1, fixed] Delete control added: confirm step, DELETE call, status
  line with `role="status"`, redirect to the list on success.
- [P1, fixed] Status renders as a pill.
- [P2, fixed] Notes card names the cost: one model call, a few seconds,
  draft until approved.
- [P2, fixed] Transcript list carries an `aria-label`.
- [P2, fixed] Approval action table carries a caption, and the approve
  form notes board writes are proposed first with evidence quotes.

Left open, with reason:

- [P2, open] No filter or search on the meetings list. Reason: lists
  stay short at target team size and newest first order keeps the
  current meeting on top. A text filter is scheduled for busier teams.
- [P2, open] Matches are text only with no board item links. Reason:
  ids are short and match board card ids. Links are scheduled.
- [P3, open] Wide meetings table may scroll sideways on narrow screens.
  Reason: five short columns fit the 1280px console layout. A stacked
  narrow layout is scheduled.

No open high severity findings on these screens.

## Detector after

Same command over the same four files after fixes.

Result: exit 0, zero findings (`[]`). Clean, no new warnings.

## After scores

Listen: 22/40 Acceptable before, 26/40 Acceptable after. H1 2 to 3
(finals and notes link announced), H5 2 to 3 (title locked mid
stream), H6 2 to 3 (support hint plus cap note), H10 1 to 2 (accurate
privacy plus consent, no broken link). Audit: 13/20 Acceptable before,
15/20 Good after. Accessibility 2 to 3, implementation integrity 2
to 3.

Meetings list: 22/40 Acceptable before, 24/40 Acceptable after. H1 2
to 3 (status pill), H4 2 to 3 (pill plus caption match the app).
Audit: 13/20 Acceptable before, 15/20 Good after. Accessibility 3 to
4 (caption), implementation integrity 2 to 3 (pill plus delete
pointer to the detail page).

Meeting detail: 21/40 Acceptable before, 24/40 Acceptable after. H1 2
to 3 (pill plus notes cost hint), H3 2 to 3 (delete with confirm),
H5 2 to 3 (delete guarded, approve still guarded by the notes
check). Audit: 14/20 Good before, 16/20 Good after. Accessibility 3
to 4 (transcript label), implementation integrity 2 to 3 (delete
path plus approval table caption).

## Screenshots

Playwright chromium against a local uvicorn server, same path as the
console shots test. Neutral file names:

- `docs/images/listen.png` and `docs/images/listen-dark.png`
- `docs/images/meetings.png` and `docs/images/meetings-dark.png`

## Live sandbox run

One run on Contributor with synthetic invented words input only
(zim zam wobble quix florp), labeled synthetic input, never claimed
as ASR output. Transcript row plus live notes plus approval card
plus dry run board write, 1 live call of max 3.

Result: ok. Model `muse-spark-1.3-contributor`, tier contributor,
usage prompt 203, completion 1975, cached 0, 1 call logged. Notes
fell back to the local path (model text was not JSON), decisions 1,
actions 3, RAID 1, matches 2. Approval card rendered, 3 dry run
items created with evidence quotes. Key stayed in script, never
printed. No failures, no retry needed.
