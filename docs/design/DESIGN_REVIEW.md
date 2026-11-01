# WP4 Design Review: Before and After

Method: single-context inline re-review plus deterministic detector run.
This uses the same collapsed method as `CRITIQUE_BEFORE.md` so the before
and after scores are comparable. No `.impeccable` snapshots were written;
this file is the record. Mode: Operate (console task UI), so all 10
heuristics apply on every screen.

Evidence: source read for `base.html`, `setup.html`, `board.html`,
`activity.html`, `plan.html`, `review.html`, `rules.html`,
`settings.html`, `style.css`, `board.js`, and the `app.py` render helpers;
every page rendered through TestClient (all 200, no em dashes in output);
`impeccable detect --json` over `src/pam_pm/web/` (exit 2, one warning,
see Detector result). No servers were left running.

Critique bands: 36-40 Excellent, 28-35 Good, 20-27 Acceptable, 12-19
Poor, 0-11 Critical. Audit bands: 18-20 Excellent, 14-17 Good, 10-13
Acceptable, 6-9 Poor, 0-5 Critical.

Scoreboard:

| Screen | Critique before | Critique after | Audit before | Audit after |
|---|---|---|---|---|
| Setup wizard | 21/40 Acceptable | 22/40 Acceptable | 12/20 Acceptable | 14/20 Good |
| Board | 24/40 Acceptable | 25/40 Acceptable | 11/20 Acceptable | 13/20 Acceptable |
| Activity | 20/40 Acceptable | 23/40 Acceptable | 13/20 Acceptable | 15/20 Good |
| Plan | 14/40 Poor | 19/40 Poor | 10/20 Acceptable | 13/20 Acceptable |
| Review | 15/40 Poor | 20/40 Acceptable | 10/20 Acceptable | 13/20 Acceptable |
| Rules | 23/40 Acceptable | 25/40 Acceptable | 14/20 Good | 16/20 Good |
| Settings | 5/40 Critical | 26/40 Acceptable | 6/20 Poor | 15/20 Good |

Severity key: P0 blocks the task, P1 causes major difficulty, P2 is an
annoyance with a workaround, P3 is polish. High severity means P0 or P1.

## 1. Setup wizard

Critique: 21/40 Acceptable before, 22/40 Acceptable after.

| # | Heuristic | Before | After | Note |
|---|---|---|---|---|
| 1 | Visibility of System Status | 3 | 3 | Saving pill added, still no step map |
| 2 | Match System / Real World | 3 | 3 | Env names remain, fine for setup user |
| 3 | User Control and Freedom | 2 | 2 | Radio still flips at once, no undo |
| 4 | Consistency and Standards | 3 | 3 | People Save/Test split remains |
| 5 | Error Prevention | 2 | 2 | Still no confirm on backend switch |
| 6 | Recognition Rather Than Recall | 2 | 2 | Secrets path still unnamed |
| 7 | Flexibility and Efficiency | 1 | 1 | Still strictly linear |
| 8 | Aesthetic and Minimalist Design | 2 | 2 | Still eight stacked cards |
| 9 | Error Recovery | 2 | 3 | Backend fail pill plus retry |
| 10 | Help and Documentation | 1 | 1 | Dead docs link remains |
| **Total** | | **21/40** | **22/40** | **Acceptable** |

Audit: 12/20 Acceptable before, 14/20 Good after. Theming 2 to 3
(shared toggle and head script on every page). Implementation integrity
2 to 3 (backend failure path now has a visible result and retry).

Findings fixed:

- [P1, fixed] Backend radio failures were silent. `saveBackend` now
  shows a Saving pill, disables the radios while saving, and shows a
  success or fail pill on every path, with a Try again button on
  failure.

Findings left, with reason:

- [P1, open] No step progress map. Reason: partly mitigated by the
  Welcome back pill that names the current step and by per-step result
  pills; progress saves after each step so nothing is lost. A sticky
  stepper is the next scheduled setup change.
- [P2, open] Backend switch has no confirm (remainder of the P1 above,
  re-scored). Reason: the choice is reversible by clicking another
  option, the saved state is now visible, and switching backends does
  not delete data. A confirm step is scheduled.
- [P2, open] Secrets file path never named. Reason: every failing check
  names the missing keys and says where to put them; the exact path
  varies by platform. Naming it once with a copy helper is scheduled.
- [P2, open] People step splits Save and Test, and Test can race an
  unsaved edit. Reason: the buttons sit together with clear labels, and
  the people check tells the user to save first. One primary action per
  step is scheduled.
- [P3, open] `docs/adapters.md` link likely 404s. Reason: the link sits
  on the disabled Coming soon card, so it blocks nothing. Serving docs
  in the app is scheduled.

## 2. Board

Critique: 24/40 Acceptable before, 25/40 Acceptable after.

| # | Heuristic | Before | After | Note |
|---|---|---|---|---|
| 1 | Visibility of System Status | 3 | 3 | Saving states added, no saved state |
| 2 | Match System / Real World | 3 | 3 | Solid kanban language |
| 3 | User Control and Freedom | 3 | 3 | Undo still only in Activity |
| 4 | Consistency and Standards | 2 | 2 | Cards still focusable but inert |
| 5 | Error Prevention | 2 | 2 | Close sprint still has no confirm |
| 6 | Recognition Rather Than Recall | 3 | 3 | Tabs, counts, filters visible |
| 7 | Flexibility and Efficiency | 2 | 2 | No shortcuts or bulk yet |
| 8 | Aesthetic and Minimalist Design | 3 | 3 | Clean columns, dots, tags |
| 9 | Error Recovery | 2 | 3 | Pending lines plus plain errors |
| 10 | Help and Documentation | 1 | 1 | Only the import hint |
| **Total** | | **24/40** | **25/40** | **Acceptable** |

Audit: 11/20 Acceptable before, 13/20 Acceptable after. Theming 3 to 4
(shared toggle, no flash before JS). Implementation integrity 2 to 3
(one pending and error pattern across moves, sprints, drawer forms).

Findings fixed:

- [P2, fixed] Moves refreshed silently. `moveItem` and the sprint
  change handler now show a Saving pill and mark the trigger busy
  until the server answers.

Findings left, with reason:

- [P1, open] Cards carry `tabindex="0"` but Enter and Space do nothing.
  Reason: every card has a labeled Open button one Tab stop away, so
  keyboard users can open any card; adding a card key handler (or
  removing the tabindex) is a small scheduled change.
- [P1, open] Close sprint has no confirmation and no inline undo.
  Reason: closing flips only the sprint status; items stay intact and
  the event appears in Activity. A confirm step plus an undo link is
  the next scheduled board change.
- [P2, open] View tabs use tablist roles without arrow-key behavior.
  Reason: the tabs are Tab reachable and click to switch; the full
  roving pattern is scheduled.
- [P3, open] Refresh swaps column HTML without focus preservation
  (remainder of the moves item). Reason: only panel content swaps, so
  toolbar focus is unaffected. Restoring card focus after refresh is
  scheduled.
- [P3, open] Drawer has no focus trap and no `aria-modal`; Esc closes
  it but focus is not returned. Reason: Esc always closes the drawer
  and the board behind it stays usable. A trap plus focus return is
  scheduled.

## 3. Activity

Critique: 20/40 Acceptable before, 23/40 Acceptable after.

| # | Heuristic | Before | After | Note |
|---|---|---|---|---|
| 1 | Visibility of System Status | 2 | 3 | Status pill announced, no timestamps |
| 2 | Match System / Real World | 2 | 3 | Human verbs, ids still raw |
| 3 | User Control and Freedom | 2 | 2 | Still no filter, pause, or dismiss |
| 4 | Consistency and Standards | 3 | 3 | Uniform event cards |
| 5 | Error Prevention | 2 | 2 | Undo remains the guardrail |
| 6 | Recognition Rather Than Recall | 2 | 2 | Still no links to items |
| 7 | Flexibility and Efficiency | 1 | 1 | Single list, no search |
| 8 | Aesthetic and Minimalist Design | 3 | 3 | Clean stream, calm cards |
| 9 | Error Recovery | 2 | 3 | Undo result pills on both paths |
| 10 | Help and Documentation | 1 | 1 | One explainer line only |
| **Total** | | **20/40** | **23/40** | **Acceptable** |

Audit: 13/20 Acceptable before, 15/20 Good after. Theming 2 to 3
(shared toggle). Implementation integrity 2 to 3 (streamed cards now
match server cards, including Undo).

Findings fixed:

- [P1, fixed] Live-streamed cards omitted Undo. The stream handler now
  builds the same Undo button, posts to `/api/undo`, and shows Undone
  or plain error pills.
- [P3, fixed] Stream status pill lacked `role="status"`. The role is
  now present, matching the board pill.

Findings left, with reason:

- [P2, open] Event titles use human verbs now, but item ids have no
  link to the item (remainder, re-scored). Reason: ids are short and
  stable and match the board card ids. A link to the board item is
  scheduled.
- [P2, open] No filter, search, or pause on the stream. Reason: lists
  stay short at the target team size, and the status pill shows
  liveness. A text filter plus pause is scheduled for busier projects.

No open high-severity findings on this screen.

## 4. Plan

Critique: 14/40 Poor before, 19/40 Poor after (band ceiling).

| # | Heuristic | Before | After | Note |
|---|---|---|---|---|
| 1 | Visibility of System Status | 1 | 2 | Busy state, no skeleton yet |
| 2 | Match System / Real World | 2 | 2 | Pipeline terms remain |
| 3 | User Control and Freedom | 1 | 1 | Still no cancel or undo |
| 4 | Consistency and Standards | 2 | 3 | Disabled button replaces live approve |
| 5 | Error Prevention | 1 | 3 | Create guarded until proposal ready |
| 6 | Recognition Rather Than Recall | 2 | 2 | Sections preview the output shape |
| 7 | Flexibility and Efficiency | 0 | 0 | Single rigid path |
| 8 | Aesthetic and Minimalist Design | 3 | 3 | Clean and sparse |
| 9 | Error Recovery | 1 | 2 | Plain error pills with next steps |
| 10 | Help and Documentation | 1 | 1 | No photo capture guidance yet |
| **Total** | | **14/40** | **19/40** | **Poor** |

Audit: 10/20 Acceptable before, 13/20 Acceptable after. Accessibility
2 to 3 (busy state announced). Theming 2 to 3 (shared toggle).
Implementation integrity 1 to 2 (approve-before-proposal closed).

Findings fixed:

- [P0, fixed] Approve rendered inside the empty placeholder before any
  proposal existed. The button now renders disabled with an explainer
  and only enables alongside a real proposal.
- [P1, fixed] Upload and vision processing looked dead. The shared
  busy handler now disables the submit button, shows Working, and
  marks the target busy; the result lands in the live region.

Findings left, with reason:

- [P2, open] No pending skeleton or progress announcement during the
  vision pass (remainder of the P1 above, re-scored). Reason: the
  button state change shows work is running, so the dead look is
  gone. A skeleton plus progress note is scheduled.
- [P2, open] No guidance on good board photos. Reason: a poor read now
  ends with advice to try a closer, brighter photo. Upfront capture
  tips plus the size limit are scheduled.
- [P2, open] Approve shows no summary of what will be created and no
  undo pointer. Reason: the proposal table above the button already
  lists title, type, owner, and estimate. A counts line plus an
  Activity link is scheduled.

No open high-severity findings on this screen.

## 5. Review

Critique: 15/40 Poor before, 20/40 Acceptable after.

| # | Heuristic | Before | After | Note |
|---|---|---|---|---|
| 1 | Visibility of System Status | 1 | 2 | Busy state, no progress or cancel |
| 2 | Match System / Real World | 3 | 3 | Natural review language |
| 3 | User Control and Freedom | 1 | 1 | Still no cancel, create is one-way |
| 4 | Consistency and Standards | 2 | 3 | Rendered sections match cards |
| 5 | Error Prevention | 1 | 2 | Submit guard, still no confirm |
| 6 | Recognition Rather Than Recall | 2 | 2 | Actions visible, body still long |
| 7 | Flexibility and Efficiency | 1 | 1 | Per-item create only |
| 8 | Aesthetic and Minimalist Design | 2 | 3 | Blob replaced by sections |
| 9 | Error Recovery | 1 | 2 | Plain error pills, some guidance |
| 10 | Help and Documentation | 1 | 1 | One explainer line only |
| **Total** | | **15/40** | **20/40** | **Acceptable** |

Audit: 10/20 Acceptable before, 13/20 Acceptable after. Accessibility
2 to 3 (rendered sections plus announced busy state). Theming 2 to 3
(shared toggle). Implementation integrity 1 to 2 (rendered body plus
submit guard).

Findings fixed:

- [P1, fixed] Review output dumped raw markdown into a `pre` block.
  `_markdown_to_html` now renders headings, lists, quotes, and code
  into `.review-body` sections with styled callouts.

Findings left, with reason:

- [P2, open] Run review has a busy guard but no progress, cancel, or
  cost hint (remainder of the P1, re-scored). Reason: the button shows
  Working while the run completes server-side and the result lands in
  the live region, so the page no longer looks dead. Progress plus
  cancel plus a spend note is scheduled.
- [P2, open] Create buttons have a disabled-while-pending guard but no
  confirm and no created-item link (remainder of the P1, re-scored).
  Reason: the guard stops double submits on the single click path and
  a result pill confirms each create. A confirm plus item link is
  scheduled.
- [P2, open] Retro actions offer no undo path after creation. Reason:
  created items live in the external tracker, so undo means cleanup
  there. A created-item link plus an undo note is scheduled.

No open high-severity findings on this screen.

## 6. Rules

Critique: 23/40 Acceptable before, 25/40 Acceptable after.

| # | Heuristic | Before | After | Note |
|---|---|---|---|---|
| 1 | Visibility of System Status | 2 | 2 | Still no dirty indicator |
| 2 | Match System / Real World | 3 | 3 | Plain titles and triggers |
| 3 | User Control and Freedom | 3 | 3 | Every toggle reverses |
| 4 | Consistency and Standards | 3 | 3 | Uniform cards and forms |
| 5 | Error Prevention | 3 | 3 | Explicit Save, nothing destructive |
| 6 | Recognition Rather Than Recall | 3 | 4 | Plain effect lines on every rule |
| 7 | Flexibility and Efficiency | 1 | 1 | One rule at a time |
| 8 | Aesthetic and Minimalist Design | 3 | 3 | Clean uniform list |
| 9 | Error Recovery | 1 | 2 | Failure pills via shared handler |
| 10 | Help and Documentation | 1 | 1 | One line per rule only |
| **Total** | | **23/40** | **25/40** | **Acceptable** |

Audit: 14/20 Good before, 16/20 Good after. Theming 2 to 3 (shared
toggle). Implementation integrity 3 to 4 (uniform cards plus plain
trigger lines).

Findings fixed:

- [P2, fixed] Trigger showed raw template key text. Each rule now
  carries one plain effect line such as Runs each morning.

Findings left, with reason:

- [P2, open] Checkbox plus Save is two steps with no dirty indicator.
  Reason: Save applies at once and the state pill restates the saved
  value, so staged versus saved confusion is brief. Dirty styling or
  an instant toggle with undo is scheduled.
- [P3, open] No bulk enable or disable. Reason: only three rules
  exist, so three saves are a rare path. Enable-all plus disable-all
  is scheduled if the list grows.
- [P3, open] Saved state text restates the checkbox instead of
  confirming the save. Reason: the pill refreshes on every save, so
  the change is visible. An explicit Saved pill with timestamp is
  scheduled.

No open high-severity findings on this screen.

## 7. Settings

Critique: 5/40 Critical before, 26/40 Acceptable after.

| # | Heuristic | Before | After | Note |
|---|---|---|---|---|
| 1 | Visibility of System Status | 0 | 3 | Save pills plus cost display |
| 2 | Match System / Real World | 1 | 3 | Plain team and safety language |
| 3 | User Control and Freedom | 1 | 3 | Editable, reversible saves |
| 4 | Consistency and Standards | 1 | 3 | Card stack matches the app |
| 5 | Error Prevention | 1 | 3 | Explicit Save, constrained inputs |
| 6 | Recognition Rather Than Recall | 0 | 3 | All settings visible in one place |
| 7 | Flexibility and Efficiency | 0 | 1 | Section at a time, no shortcuts |
| 8 | Aesthetic and Minimalist Design | 1 | 3 | Clean cards, calm layout |
| 9 | Error Recovery | 0 | 2 | Result regions, quality varies |
| 10 | Help and Documentation | 0 | 2 | Inline explainer lines |
| **Total** | | **5/40** | **26/40** | **Acceptable** |

Audit: 6/20 Poor before, 15/20 Good after. Accessibility 1 to 3
(labels and live regions). Performance 2 to 3 (tiny page, plain
forms). Theming 1 to 3 (shared toggle). Responsive 2 to 3 (cards
stack). Implementation integrity 0 to 3 (one coherent surface).

Findings fixed:

- [P0, fixed] Settings surface did not exist. `/settings` now holds
  Team (names, skills, capacity), Safety (dry run, spend cap), Model
  cost, and Console access, linked from the sidenav.
- [P1, fixed] Stored theme applied only on the board page. A shared
  head script plus topbar toggle in `base.html` now serves every
  page with no flash.
- [P2, fixed] Safety values were read-only text in setup step 6. Dry
  run and spend cap are editable controls with Save and result pills.

No open findings on this screen.

## Cross-screen patterns

1. Theme choice was board-only. Fixed: one shared script and toggle
   in `base.html` now serves all seven screens.
2. Slow actions went quiet. Fixed for the dead look: a shared busy
   pattern (Working label, disabled trigger, `aria-busy`, result in
   the live region) covers htmx posts, backend save, board moves,
   sprint changes, drawer forms, upload, and review run. Dedicated
   skeletons plus cancel remain scheduled for Plan and Review.
3. Streamed content diverged from server content. Fixed on Activity
   (streamed cards match server cards, including Undo). Board refresh
   still drops card focus; restore is scheduled.
4. Touch targets use 0.5rem padding (about 34px tall). Accepted: this
   meets the WCAG 2.2 AA minimum of 24px. The larger 44px guideline
   remains an enhancement for touch-heavy use.
5. No skip link; htmx loads from CDN without SRI. Open (P3). Reason:
   the layout has one main column and seven nav links, so keyboard
   reach is short. A skip link plus SRI is scheduled in `base.html`.

## Detector result

Command: `npx --yes impeccable@4.8.0 detect --json src/pam_pm/web/`

Result: exit 2 with one warning.

- `low-contrast` (warning, quality) in `base.html`: hover state 3.2:1,
  text `#ffffff` on `#14a192`.

Reason: the rest state passes; only the dark-theme button hover token
sits below 4.5:1 for its bold label. No error-severity findings. A
darker hover token is scheduled. The new `design-lint` CI job runs
this detector in non-blocking mode so the warning is tracked without
breaking builds.

## Screenshots

Tool: Playwright chromium against a local uvicorn server running the
app with its built-in sample data (fake adapters only, no network, no
credentials). Sample names are short neutral invented names. Files:

| Screen | Light | Dark |
|---|---|---|
| Setup | `docs/images/setup.png` | `docs/images/setup-dark.png` |
| Board | `docs/images/board.png` | `docs/images/board-dark.png` |
| Activity | `docs/images/activity.png` | `docs/images/activity-dark.png` |
| Plan | `docs/images/plan.png` | `docs/images/plan-dark.png` |
| Review | `docs/images/review.png` | `docs/images/review-dark.png` |
| Rules | `docs/images/rules.png` | `docs/images/rules-dark.png` |
| Settings | `docs/images/settings.png` | `docs/images/settings-dark.png` |

Light shots reuse the existing file names so current doc links keep
working. Dark shots set `pam-theme` to dark before load, which is the
same path the topbar toggle uses.

## Open high-severity list

Three P1 findings stay open, each with a reason above: setup step
progress map, board inert card stops, board close sprint confirm.
Every other P0 and P1 from the before pass is fixed or re-scored to
P2 on the evidence that the worst user harm is gone.

## 8. RAID

Critique: 5/40 Critical before (no surface), 24/40 Acceptable after.

| # | Heuristic | Before | After | Note |
|---|---|---|---|---|
| 1 | Visibility of System Status | 0 | 3 | Ready/Saving pill plus per-card result pills |
| 2 | Match System / Real World | 1 | 3 | Plain RAID language with counts on tabs |
| 3 | User Control and Freedom | 1 | 3 | Add, edit, accept, close, delete all reversible except delete with confirm |
| 4 | Consistency and Standards | 1 | 3 | Card stack and toolbar match the board |
| 5 | Error Prevention | 0 | 3 | Required title, confirm on delete, explicit Save |
| 6 | Recognition Rather Than Recall | 0 | 3 | All four kinds visible with counts |
| 7 | Flexibility and Efficiency | 0 | 1 | One entry at a time, no bulk |
| 8 | Aesthetic and Minimalist Design | 1 | 2 | Clean cards, edit forms add bulk |
| 9 | Error Recovery | 0 | 2 | Result pills with retry hints |
| 10 | Help and Documentation | 1 | 1 | One explainer line only |
| **Total** | | **5/40** | **24/40** | **Acceptable** |

Audit: 6/20 Poor before, 15/20 Good after. Accessibility 1 to 3 (labels, tablist, live regions). Theming 1 to 3 (shared toggle). Responsive 2 to 3 (cards stack). Implementation integrity 0 to 3 (one coherent surface).

Findings fixed:

- [P0, fixed] RAID surface did not exist. `/raid` now holds four tabs with counts, New entry, inline edit, accept, close, delete with confirm, and CSV plus Markdown export, linked from the sidenav.
- [P1, fixed] RAID operations had no human verbs. Activity now maps all ten RAID operations in both server and stream paths.

Detector: `python -m ruff check src tests` exit 0 after fix; rendered `/raid` checked for 200, four tabs, no em dashes. No servers left running.

## 9. Fresh pass over all 16 screens

Method: single-context inline critique plus audit per screen, same
collapsed method as above so scores compare. Mode: Operate, all 10
heuristics apply. Before scores are the last recorded After scores
from sections 1 to 8 and the WP4 notes (TRIAGE, NEXT_SPRINT,
MID_SPRINT, LISTEN); Kickoff and Add task had no prior scores and
were scored for the first time. No redesign, no theme change.

Evidence: source read for every template in
`src/pam_pm/web/templates/`, `style.css`, and the card HTML helpers
in `app.py`; all 15 page routes rendered through TestClient (all
200, no em dashes); `impeccable detect --json` over
`src/pam_pm/web/` exit 0. No servers left running.

Scoreboard (before to after, this pass):

| Screen | Critique before | Critique after | Audit before | Audit after |
|---|---|---|---|---|
| Setup wizard | 22/40 Acceptable | 22/40 Acceptable | 14/20 Good | 14/20 Good |
| Board | 25/40 Acceptable | 25/40 Acceptable | 13/20 Acceptable | 13/20 Acceptable |
| Activity | 23/40 Acceptable | 23/40 Acceptable | 15/20 Good | 15/20 Good |
| RAID | 24/40 Acceptable | 24/40 Acceptable | 15/20 Good | 15/20 Good |
| Plan | 19/40 Poor | 19/40 Poor | 13/20 Acceptable | 13/20 Acceptable |
| Review | 20/40 Acceptable | 20/40 Acceptable | 13/20 Acceptable | 13/20 Acceptable |
| Rules | 25/40 Acceptable | 25/40 Acceptable | 16/20 Good | 16/20 Good |
| Settings | 26/40 Acceptable | 26/40 Acceptable | 15/20 Good | 15/20 Good |
| Kickoff | new, first score | 23/40 Acceptable | new, first score | 15/20 Good |
| Triage | 29/40 Good | 29/40 Good | 17/20 Good | 17/20 Good |
| Next sprint | 29/40 Good | 29/40 Good | 17/20 Good | 17/20 Good |
| Add task | new, first score | 27/40 Acceptable | new, first score | 17/20 Good |
| Mid sprint | 29/40 Good | 29/40 Good | 17/20 Good | 17/20 Good |
| Listen | 26/40 Acceptable | 26/40 Acceptable | 15/20 Good | 15/20 Good |
| Meetings | 24/40 Acceptable | 24/40 Acceptable | 15/20 Good | 15/20 Good |
| Meeting detail | 24/40 Acceptable | 24/40 Acceptable | 16/20 Good | 16/20 Good |

Fixes made (regressions and polish only, 5 small edits):

- [P1, fixed] Review title block contained a link element, so markup
  rendered inside `<title>`. `review.html` title is plain text again;
  the Plan next sprint link stays in the page body.
- [P2, fixed] Nested cards returned on two screens after the
  next-sprint and mid-sprint passes removed the pattern. Triage card
  HTML and both Review follow up action paths now use plain
  `article` elements, matching the fixed screens.
- [P3, fixed] Plan proposal table headers lacked `scope="col"`,
  unlike every other console table. Scopes added.

New findings left open, with reason:

- [P2, open] Sidenav grew from 7 to 15 links as screens shipped, so
  it exceeds the 5 item guidance. Reason: links are short, grouped
  by reading order, and each screen keeps its one task focus.
  Grouping with section headings is scheduled, not done here, to
  avoid redesigning shared navigation in a polish pass.
- [P2, open] Kickoff card names no dry run state, though approve
  respects it and every sibling card shows a pill. Reason: approve
  still guards through dry run server side, so no unsafe write is
  possible. A pill matching triage and add-task is scheduled.
- [P3, open] Kickoff and add-task name no cost note, unlike
  triage, next-sprint, and mid-sprint. Reason: both propose paths
  are local code, so the spend risk is nil. One shared cost line is
  scheduled.

First scores detail for the two unscored screens:

- Kickoff 23/40: H1 2 (no dry run pill), H2 3, H3 3, H4 2 (missing
  pill vs siblings), H5 2 (state unnamed), H6 3, H7 1, H8 3, H9 2,
  H10 2. Audit 15/20: A11y 3, Perf 4, Theming 4, Responsive 2
  (wide table), Integrity 2 (missing pill).
- Add task 27/40: H1 3, H2 3, H3 3, H4 3, H5 3, H6 3, H7 1, H8 3,
  H9 3, H10 2 (no cost note). Audit 17/20: A11y 3, Perf 4,
  Theming 4, Responsive 3, Integrity 3.

Detector result: `impeccable detect --json src/pam_pm/web/` exit 0,
zero findings, before and after. The earlier base.html low contrast
warning is gone: tokens now use the blue accent family in both
themes. `ruff check src tests` exit 0, `pytest` 329 passed.
