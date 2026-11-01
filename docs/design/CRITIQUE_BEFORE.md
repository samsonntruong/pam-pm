# WP4a Critique Before Pass

Method: single-context inline review plus deterministic detector run. The
`critique` dual-agent split was collapsed into one pass because this task
requires a single combined BEFORE file with no extra files and no
`.impeccable` snapshots. Mode: Operate (console task UI), so all 10
heuristics apply on every screen.

Evidence: source read for `base.html`, `setup.html`, `board.html`,
`activity.html`, `plan.html`, `review.html`, `rules.html`, `style.css`,
`board.js`; every page rendered through TestClient with built-in sample
data (all 200, no em dashes in output); `impeccable detect --json`
over `src/pam_pm/web/templates/` returned clean (exit 0, zero findings).
No servers were left running. No code was changed.

Scoreboard:

| Screen | Critique | Band | Audit | Band |
|---|---|---|---|---|
| Setup wizard | 21/40 | Acceptable | 12/20 | Acceptable |
| Board | 24/40 | Acceptable | 11/20 | Acceptable |
| Activity | 20/40 | Acceptable | 13/20 | Acceptable |
| Plan | 14/40 | Poor | 10/20 | Acceptable |
| Review | 15/40 | Poor | 10/20 | Acceptable |
| Rules | 23/40 | Acceptable | 14/20 | Good |
| Settings | 5/40 | Critical | 6/20 | Poor |

Critique bands: 36-40 Excellent, 28-35 Good, 20-27 Acceptable, 12-19
Poor, 0-11 Critical. Audit bands: 18-20 Excellent, 14-17 Good, 10-13
Acceptable, 6-9 Poor, 0-5 Critical.

## 1. Setup wizard (`setup.html` + setup flow)

Critique: 21/40, Acceptable.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 3 | No step progress map across 7 steps |
| 2 | Match System / Real World | 3 | Env var names leak, acceptable for setup user |
| 3 | User Control and Freedom | 2 | Backend radio flips immediately, no undo |
| 4 | Consistency and Standards | 3 | People step splits Save and Test, others do not |
| 5 | Error Prevention | 2 | No confirm on backend switch |
| 6 | Recognition Rather Than Recall | 2 | Secrets file path never named |
| 7 | Flexibility and Efficiency | 1 | Strictly linear, no skip or shortcut |
| 8 | Aesthetic and Minimalist Design | 2 | Eight stacked cards, long scroll |
| 9 | Error Recovery | 2 | Result regions exist, message quality varies |
| 10 | Help and Documentation | 1 | One explainer line, plus a likely dead docs link |
| **Total** | | **21/40** | **Acceptable** |

Cognitive load: moderate. All 8 cards compete on one page (single focus
and one-thing-at-a-time fail); backend choice itself is a clean 3-way
decision.

Top findings, ordered by severity:

- [P1] No step progress map. Seven steps render as equal cards with no
  stepper, checkmarks, or sticky position. Why it matters: on a long
  page users cannot tell what is done or what is next. Fix: sticky
  stepper with done/current states driven by the existing progress JSON.
- [P1] Backend radio saves on change with no confirm, and failures are
  silent (`saveBackend` catch ignores errors, no result pill on the
  failure path). Why it matters: one misclick flips project storage
  with no visible outcome on failure. Fix: confirm copy plus a result
  pill on both success and failure paths.
- [P2] Secrets file is referenced by every step but its path is never
  named. Why it matters: first-timers must guess where
  `META_MODEL` and friends live. Fix: name the path once with a copy
  helper.
- [P2] People step splits Save and Test into two buttons while other
  steps combine action and progress save; Test buttons also race an
  `onclick` save against the htmx post. Why it matters: inconsistent
  pattern invites lost input. Fix: one primary action per step.
- [P3] `docs/adapters.md` relative link resolves outside the app router
  and likely 404s. Why it matters: dead end in the one help link the
  wizard offers. Fix: serve docs in app or drop the link.

Audit: 12/20, Acceptable.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 2 | Radio group has no fieldset/legend or stepper semantics |
| 2 | Performance | 3 | Small page; htmx from CDN without SRI |
| 3 | Theming | 2 | Stored theme ignored here; tokens used well |
| 4 | Responsive Design | 3 | Cards stack; backend cards have no grid style |
| 5 | Implementation Integrity | 2 | Inconsistent save/test split; dead docs link |
| **Total** | | **12/20** | **Acceptable** |

## 2. Board (`board.html` + `board.js`)

Critique: 24/40, Acceptable. Highest scoring screen; also the most
interactive, so its gaps carry the most user impact.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 3 | Moves give no saving/saved feedback |
| 2 | Match System / Real World | 3 | Solid kanban language |
| 3 | User Control and Freedom | 3 | Undo lives only in Activity, not inline |
| 4 | Consistency and Standards | 2 | Card focusable but inert; mixed move patterns |
| 5 | Error Prevention | 2 | Close sprint has no confirm |
| 6 | Recognition Rather Than Recall | 3 | Tabs, counts, filters all visible |
| 7 | Flexibility and Efficiency | 2 | Drag plus Move-to menu; no shortcuts or bulk |
| 8 | Aesthetic and Minimalist Design | 3 | Clean columns, status dots, tags |
| 9 | Error Recovery | 2 | Error pill names problem; drawer errors drop context |
| 10 | Help and Documentation | 1 | Only the import column hint |
| **Total** | | **24/40** | **Acceptable** |

Cognitive load: moderate. Toolbar carries 7+ controls at once (minimal
choices fails); card field grids stay scannable.

Top findings, ordered by severity:

- [P1] Cards carry `tabindex="0"` but keyboard Enter/Space does nothing;
  only the Open button activates. Why it matters: keyboard users land on
  inert stops and conclude the board is broken. Fix: add a key handler
  on the card or remove the tabindex.
- [P1] Close sprint fires with no confirmation and no inline undo. Why it
  matters: one click ends a sprint with no recovery path on the page.
  Fix: confirm step plus a surfaced Activity undo link.
- [P2] View tabs use tablist roles without arrow-key behavior or roving
  tabindex. Why it matters: screen reader users get tab semantics that
  do not behave like tabs. Fix: implement the full tabs pattern or use
  plain buttons.
- [P2] Moves refresh silently by replacing column HTML, with no
  saving/saved state and no focus preservation. Why it matters: users
  cannot tell a drag registered until content swaps under them. Fix:
  status pill transitions plus focus retention.
- [P3] Drawer dialog has no focus trap and no `aria-modal`; Esc closes it
  but focus is not returned. Why it matters: keyboard and screen reader
  users lose their place. Fix: trap focus while open, return it on close.

Audit: 11/20, Acceptable.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 2 | Inert focusable cards; tabs pattern incomplete |
| 2 | Performance | 2 | Every change refetches and reparses full board HTML |
| 3 | Theming | 3 | Toggle present; flash before JS applies stored theme |
| 4 | Responsive Design | 2 | Drag unusable on touch (menu alternative saves it) |
| 5 | Implementation Integrity | 2 | Mixed control patterns; read-only vs editable diverge |
| **Total** | | **11/20** | **Acceptable** |

## 3. Activity (`activity.html`)

Critique: 20/40, Acceptable, at the band floor. Core loop (see writes
with evidence, undo) works; legibility and stream controls lag.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 2 | New events prepend silently; events lack timestamps |
| 2 | Match System / Real World | 2 | Raw operation names and item ids |
| 3 | User Control and Freedom | 2 | Undo per event; no filter, pause, or dismiss |
| 4 | Consistency and Standards | 3 | Uniform event cards match app language |
| 5 | Error Prevention | 2 | Undo is the guardrail; no confirm needed |
| 6 | Recognition Rather Than Recall | 2 | Evidence quotes help; no links to items |
| 7 | Flexibility and Efficiency | 1 | Single list; no filter, search, or shortcut |
| 8 | Aesthetic and Minimalist Design | 3 | Clean stream, calm cards |
| 9 | Error Recovery | 2 | Stream status plus inline undo results |
| 10 | Help and Documentation | 1 | One explainer line only |
| **Total** | | **20/40** | **Acceptable** |

Cognitive load: low. One list, one action per card; busy projects will
need the missing filter.

Top findings, ordered by severity:

- [P1] Live-streamed cards omit the Undo button that server-rendered
  cards have (the `onmessage` handler builds title, quote, and meta
  only). Why it matters: new events cannot be undone until reload,
  exactly when undo matters most. Fix: render the same Undo control in
  the stream handler.
- [P2] Event titles show raw operation and item id with no link to the
  item. Why it matters: users must memorize ids to connect events to
  board cards. Fix: human verb plus link to the board item.
- [P2] No filter, search, or pause on the stream. Why it matters: busy
  projects bury signal under a moving list. Fix: text filter plus pause
  toggle.
- [P3] Stream status pill lacks `role="status"` (the board pill has it).
  Why it matters: state changes are not announced. Fix: add the role.

Audit: 13/20, Acceptable.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 3 | Simple and labeled; dynamic cards not announced |
| 2 | Performance | 3 | Light SSE updates |
| 3 | Theming | 2 | No toggle; stored theme ignored |
| 4 | Responsive Design | 3 | Simple list reflows cleanly |
| 5 | Implementation Integrity | 2 | Streamed cards diverge from server cards (no Undo) |
| **Total** | | **13/20** | **Acceptable** |

## 4. Plan (`plan.html`)

Critique: 14/40, Poor. Thinnest flow; the approve-before-proposal flaw
is the top cross-screen correctness issue with Review.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 1 | Slow upload/vision pass has no pending state |
| 2 | Match System / Real World | 2 | Masks/Readings/Proposal are pipeline terms |
| 3 | User Control and Freedom | 1 | No cancel; no undo of approve |
| 4 | Consistency and Standards | 2 | Approve sits in the empty placeholder |
| 5 | Error Prevention | 1 | Approve clickable with no proposal |
| 6 | Recognition Rather Than Recall | 2 | Labeled sections preview the output shape |
| 7 | Flexibility and Efficiency | 0 | Single rigid path, no alternative |
| 8 | Aesthetic and Minimalist Design | 3 | Clean and sparse |
| 9 | Error Recovery | 1 | Upload errors land in the result blob, no guidance |
| 10 | Help and Documentation | 1 | No photo capture guidance |
| **Total** | | **14/40** | **Poor** |

Cognitive load: low, but only because the screen says too little;
guidance load falls on the user.

Top findings, ordered by severity:

- [P0] Approve renders inside the empty Proposal placeholder before any
  proposal exists. Why it matters: the UI permits approving nothing and
  forces the server to reject a UI-blessed action. Fix: render Approve
  only alongside a real proposal.
- [P1] Upload and vision processing show no progress or pending state.
  Why it matters: the slowest step looks dead, inviting re-uploads.
  Fix: pending skeleton plus progress announcement in the live region.
- [P2] No guidance on what makes a good board photo (framing, light,
  size limit). Why it matters: poor input yields poor proposals with no
  recourse. Fix: short capture tips plus stated max size.
- [P2] Approve shows no summary of what will be created and no undo
  pointer. Why it matters: blind commit with no recovery story. Fix:
  confirm with counts plus an Activity link.

Audit: 10/20, Acceptable, at the band floor.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 2 | File input labeled; no progress announcement |
| 2 | Performance | 2 | Vision latency with no progress or streaming |
| 3 | Theming | 2 | No toggle; stored theme ignored |
| 4 | Responsive Design | 3 | Single column reflows fine |
| 5 | Implementation Integrity | 1 | Approve-before-proposal; no error states designed |
| **Total** | | **10/20** | **Acceptable** |

## 5. Review (`review.html`)

Critique: 15/40, Poor. Good plain-language framing undercut by a raw
markdown dump and unguarded write actions.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 1 | Slow LLM run has no progress or cancel |
| 2 | Match System / Real World | 3 | Sprint review and retro language is natural |
| 3 | User Control and Freedom | 1 | No cancel; Create in ADO is one-way |
| 4 | Consistency and Standards | 2 | Review body is a raw pre dump, unlike cards |
| 5 | Error Prevention | 1 | Create in ADO has no confirm or pending guard |
| 6 | Recognition Rather Than Recall | 2 | Retro actions visible; markdown blob must be parsed |
| 7 | Flexibility and Efficiency | 1 | Per-item create only; no bulk or shortcut |
| 8 | Aesthetic and Minimalist Design | 2 | Monospace blob dominates the page |
| 9 | Error Recovery | 1 | Run failures land in the result blob, no guidance |
| 10 | Help and Documentation | 1 | One explainer line only |
| **Total** | | **15/40** | **Poor** |

Cognitive load: low-moderate. The `pre` blob breaks visual hierarchy
and pushes parsing effort onto the reader.

Top findings, ordered by severity:

- [P1] Review output dumps raw markdown into a `pre` block instead of
  rendered sections. Why it matters: low-confidence flags drown in
  monospace and the page reads as a log, not a review. Fix: render
  markdown with callouts for flagged items.
- [P1] Run review is a slow interview plus LLM pass with no progress,
  cancel, or cost hint. Why it matters: users stare at a dead page
  during the most expensive action in the console. Fix: progress states
  plus cancel plus a spend note.
- [P1] Create in ADO buttons have no confirm, no disabled-while-pending,
  and no idempotency signal. Why it matters: double clicks can duplicate
  retro items in the external system. Fix: disable on submit, confirm,
  and show the created item link.
- [P2] Retro actions offer no undo path after creation. Why it matters:
  a mistaken create requires cleanup in ADO by hand. Fix: link the
  created item plus an undo note.

Audit: 10/20, Acceptable, at the band floor.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 2 | Pre blob is hard to scan; no progress announcement |
| 2 | Performance | 2 | LLM run with no progress or streaming |
| 3 | Theming | 2 | No toggle; stored theme ignored |
| 4 | Responsive Design | 3 | Single column reflows fine |
| 5 | Implementation Integrity | 1 | Raw markdown dump; nested cards; no submit guard |
| **Total** | | **10/20** | **Acceptable** |

## 6. Rules (`rules.html`)

Critique: 23/40, Acceptable. Simplest complete screen; uniform cards and
reversible toggles carry it.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 2 | No dirty indicator between toggle and Save |
| 2 | Match System / Real World | 3 | Plain titles and descriptions |
| 3 | User Control and Freedom | 3 | Every toggle reverses immediately |
| 4 | Consistency and Standards | 3 | Uniform cards and forms |
| 5 | Error Prevention | 3 | Explicit Save; nothing destructive exists |
| 6 | Recognition Rather Than Recall | 3 | All rules visible with descriptions |
| 7 | Flexibility and Efficiency | 1 | One rule at a time; no bulk or shortcut |
| 8 | Aesthetic and Minimalist Design | 3 | Clean uniform list |
| 9 | Error Recovery | 1 | Save failures surface in the region, quality unknown |
| 10 | Help and Documentation | 1 | One line per rule, no deeper docs |
| **Total** | | **23/40** | **Acceptable** |

Cognitive load: low. Three uniform cards, one decision each.

Top findings, ordered by severity:

- [P2] Checkbox plus Save is two steps with no dirty indicator. Why it
  matters: users cannot tell staged from saved. Fix: dirty styling or
  an instant toggle with undo.
- [P2] Trigger shows as raw template key text and effect scope is
  unclear. Why it matters: users enable rules without knowing what will
  change. Fix: one-line effect statement per rule.
- [P3] No bulk enable or disable; setup means three separate saves. Why
  it matters: small repeated friction on a common path. Fix: enable-all
  and disable-all, or a setup preset link.
- [P3] Toggle state text restates the checkbox instead of confirming the
  save. Why it matters: after Save the confirmation reads ambiguous.
  Fix: explicit Saved pill with timestamp.

Audit: 14/20, Good. Best audit score on the console.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 3 | Labels and live regions throughout |
| 2 | Performance | 3 | Tiny page, no heavy work |
| 3 | Theming | 2 | No toggle; stored theme ignored |
| 4 | Responsive Design | 3 | Cards stack cleanly |
| 5 | Implementation Integrity | 3 | Uniform, coherent, no drift |
| **Total** | | **14/20** | **Good** |

## 7. Settings (missing surface)

Critique: 5/40, Critical. No route, template, or nav entry exists. What
would be settings is scattered: backend choice, people, safety values,
and autostart live only in the setup wizard; the theme toggle lives only
on the board toolbar in `localStorage`. Scores below judge that
fragmented reality.

| # | Heuristic | Score | Key issue |
|---|---|---|---|
| 1 | Visibility of System Status | 0 | No surface; backend save failures are silent |
| 2 | Match System / Real World | 1 | Fragment labels are plain |
| 3 | User Control and Freedom | 1 | Changeable only by re-running setup |
| 4 | Consistency and Standards | 1 | Each fragment uses a different control pattern |
| 5 | Error Prevention | 1 | Backend switch unconfirmed |
| 6 | Recognition Rather Than Recall | 0 | Must recall where each setting lives |
| 7 | Flexibility and Efficiency | 0 | No path at all |
| 8 | Aesthetic and Minimalist Design | 1 | Fragments are clean cards, but scattered |
| 9 | Error Recovery | 0 | No surface, no messages |
| 10 | Help and Documentation | 0 | None |
| **Total** | | **5/40** | **Critical** |

Cognitive load: not scorable as a screen; cross-app recall burden is
high because every setting hides in a different place.

Top findings, ordered by severity:

- [P0] Settings surface does not exist. Why it matters: after setup,
  users cannot discover or change backend, people, spend cap, dry run,
  or autostart, and theme is per-browser only. Fix: build the Settings
  screen on the existing endpoints and link it from the sidenav.
- [P1] Stored theme is honored only on the board page (`board.js`
  applies it); every other page follows the OS only. Why it matters:
  the toggle appears to work, then the choice silently drops on
  navigation. Fix: shared theme script in `base.html`.
- [P2] Safety values (dry run, spend cap) render as read-only text in
  setup step 6 with no edit path in the UI. Why it matters: the two
  highest-stakes values are view-only. Fix: editable controls on the new
  Settings screen.

Audit: 6/20, Poor.

| # | Dimension | Score | Key finding |
|---|---|---|---|
| 1 | Accessibility | 1 | Fragments labeled; no coherent semantics |
| 2 | Performance | 2 | Nothing to load; fragment saves lack timeout handling |
| 3 | Theming | 1 | Stored choice dropped outside the board page |
| 4 | Responsive Design | 2 | Fragments stack; no dedicated layout |
| 5 | Implementation Integrity | 0 | No coherent surface |
| **Total** | | **6/20** | **Poor** |

## Cross-screen patterns (from both workflows)

1. Theme choice is board-only. Storage, toggle, and script all live in
   the board view; other pages ignore the stored value. One shared
   script in `base.html` fixes every screen at once.
2. Slow actions lack pending states. Plan upload, review run, and board
   moves all go quiet during async work. A shared pending pill pattern
   would raise H1 on three screens.
3. Touch targets run under 44px. Buttons use 0.5rem padding (about 34px
   tall); pass WCAG AA spacing or grow primary targets.
4. No skip link; htmx loads from CDN without SRI. Small global a11y and
   hardening wins in `base.html`.
5. Streamed content diverges from server content. Activity live cards
   lack Undo; board refresh swaps HTML without focus care. Keep one
   render path or mirror it exactly.
6. Unguarded writes cluster on Plan and Review. Approve-before-proposal
   and unconfirmed ADO creates are the two P0/P1 correctness fixes to
   schedule first.

RAID screen: skipped, does not exist yet. Nothing was fixed; fixes
belong to the follow-up pass.
