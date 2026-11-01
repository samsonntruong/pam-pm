---
name: Pam
description: Calm console for small software and data teams.
default-theme: "dark"
colors:
  accent: "#1e40af"
  accent-deep: "#1e3a8a"
  dark-accent: "#3768dc"
  dark-accent-deep: "#2f5cc4"
  app-bg: "#f4f6f8"
  surface: "#ffffff"
  ink: "#16202a"
  muted: "#5b6b7b"
  line: "#dde5ec"
  ok-bg: "#dcfce7"
  ok-ink: "#166534"
  fail-bg: "#fee2e2"
  fail-ink: "#991b1b"
  todo: "#b45309"
  doing: "#1d4ed8"
  done: "#15803d"
typography:
  display:
    fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif'
    fontSize: "1.6rem"
    fontWeight: 700
    lineHeight: 1.2
  headline:
    fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif'
    fontSize: "1.1rem"
    fontWeight: 700
    lineHeight: 1.3
  title:
    fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif'
    fontSize: "1rem"
    fontWeight: 700
    lineHeight: 1.4
  body:
    fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif'
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif'
    fontSize: "0.85rem"
    fontWeight: 700
    lineHeight: 1.4
rounded:
  sm: "6px"
  md: "8px"
  lg: "10px"
  pill: "999px"
  full: "50%"
spacing:
  xs: "0.25rem"
  sm: "0.5rem"
  md: "0.8rem"
  lg: "1rem"
  xl: "1.5rem"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "0.5rem 0.9rem"
  button-primary-hover:
    backgroundColor: "{colors.accent-deep}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "0.5rem 0.9rem"
  nav-link:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.muted}"
    rounded: "{rounded.sm}"
    padding: "0.5rem 0.7rem"
  nav-link-active:
    backgroundColor: "{colors.accent}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "0.5rem 0.7rem"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "1rem 1.1rem"
  pill:
    backgroundColor: "{colors.app-bg}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "0.25rem 0.6rem"
  pill-ok:
    backgroundColor: "{colors.ok-bg}"
    textColor: "{colors.ok-ink}"
    rounded: "{rounded.pill}"
    padding: "0.25rem 0.6rem"
  pill-fail:
    backgroundColor: "{colors.fail-bg}"
    textColor: "{colors.fail-ink}"
    rounded: "{rounded.pill}"
    padding: "0.25rem 0.6rem"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0.45rem 0.6rem"
  kanban-col:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "0.8rem"
  kanban-card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "0.7rem 0.8rem"
---

# Design System: Pam

## Overview

**Creative North Star: "The Quiet Control Room"**

Pam looks like a finished tool a lead can trust at 9am on a Monday. One deep blue accent, neutral surfaces, system type, and flat cards with thin borders. Density is medium: generous card padding, a narrow 190px side nav, and a 980px content column that keeps lines short. Motion is subtle and only confirms state: a test pill flips to green, a new activity card prepends, a board card moves column.

The same shell serves all eight screens: Setup wizard, Board, Activity, RAID, Plan, Review, Rules, and Settings. Each screen is a stack of cards under a short title plus a muted helper line. Async results land in live regions. Dark theme is the default and paints first with no script; light theme reuses the same roles with flipped surface and ink values, one toggle click away. A stored preference wins on return visits.

**Key Characteristics:**

- Calm and plain-spoken, never loud or playful
- One accent, used sparingly for actions and active nav
- Flat surfaces with borders, no shadows
- System font stack, no web fonts, no build step
- Status always pairs color with a label

## Colors

One blue accent on neutral surfaces. Semantic colors only for status and feedback.

### Primary

- **Deep Blue** (#1e40af in light, #3768dc in dark): primary actions, brand wordmark, active nav. Hover deepens to #1e3a8a in light and #2f5cc4 in dark.

### Neutral

- **App Wash** (#f4f6f8): page background, pill rest background, evidence quote fill.
- **Card White** (#ffffff): topbar, side nav, cards, inputs, kanban columns and cards.
- **Ink** (#16202a): headings and body text.
- **Slate Muted** (#5b6b7b): helper text, idle nav links, item IDs, meta lines.
- **Hairline** (#dde5ec): borders on topbar, nav, cards, inputs, pills, and kanban.

### Status and feedback

- **Amber To Do** (#b45309): To Do column titles and dots.
- **Blue Doing** (#1d4ed8): Doing column titles and dots.
- **Green Done** (#15803d): Done column titles and dots.
- **Pass Mint** (#dcfce7 with #166534 text): OK pills and setup pass states.
- **Fail Rose** (#fee2e2 with #991b1b text): error pills and failed test states.

### Named Rules

**The One Accent Rule.** Only blue means clickable or active. Status colors never style buttons or nav.

**The Label With Color Rule.** Every status color ships with a text label. Dots never stand alone.

## Typography

**Display Font:** system stack (with Segoe UI fallback)
**Body Font:** system stack (same as display)
**Label/Mono Font:** system stack, no separate mono in use today

**Character:** plain and readable. System glyphs, tight headings, relaxed body. No display face, no uppercase labels.

### Hierarchy

- **Display** (700, 1.6rem, 1.2): page titles such as Board, Activity, Setup wizard. One per screen.
- **Headline** (700, 1.1rem, 1.3): card titles and kanban column titles.
- **Title** (700, 1rem, 1.4): card item titles inside kanban cards and retro actions.
- **Body** (400, 1rem, 1.5): paragraphs, helper lines, table cells. Content column caps at 980px.
- **Label** (700, 0.85rem, 1.4): item IDs such as #101, small meta. Muted color.

### Named Rules

**The System Stack Rule.** Type stays on the system stack everywhere. No web font is added to fix one screen.

## Layout

Topbar plus left nav plus single content column. Topbar holds the brand left and the project switcher right, with a hairline below. Side nav is 190px, full height, with six to eight links stacked at 0.25rem gaps. Content flexes to fill, pads at 1.5rem, and caps at 980px. Cards stack with 1rem vertical margins.

Board uses a 3-column kanban grid at 0.9rem gaps. Each column pads at 0.8rem with a 10px radius. Cards inside pad at 0.7rem 0.8rem with 0.6rem vertical gaps. Review and Plan reuse the card stack; proposal tables live inside a card.

Responsive: below 800px the shell stacks vertically, side nav goes full width, and kanban collapses to one column. No other breakpoints exist today.

Spacing rhythm follows a small rem scale: 0.25rem for tight gaps, 0.5rem to 0.8rem for control padding, 1rem for card padding and stack gaps, 1.5rem for page padding.

## Elevation & Depth

Flat by design. Depth comes from hairline borders and background contrast, not shadows. No box-shadow values exist in the current CSS.

Cards, columns, inputs, and pills all sit at the same level. State changes show through color and label: nav active fills Deep Blue, test pills flip to mint or rose, kanban titles take status color.

### Named Rules

**The Flat By Default Rule.** No shadow is added for decoration. If depth is ever needed, it must signal state such as hover, drag, or focus.

## Shapes

Gently rounded rectangles with thin borders. Small controls use 6px: buttons, inputs, nav links, evidence quote right corners. Cards and kanban cards use 8px. Kanban columns use 10px to read as containers. Pills are fully round at 999px. Status dots are circles at 50%.

Borders are 1px hairlines except the evidence quote, which keeps top, right, and bottom borders with an open left edge and a 0 6px 6px 0 radius.

### Named Rules

**The Container Steps Rule.** Controls 6px, cards 8px, columns 10px, pills 999px. New shapes pick the closest step instead of adding one.

## Components

### Buttons

- **Shape:** softly rounded (6px), no border.
- **Primary:** Deep Blue fill (#1e40af in light, #3768dc in dark) with white text, padding 0.5rem 0.9rem, weight 600.
- **Hover / Focus:** fill deepens to #1e3a8a in light and #2f5cc4 in dark. Focus stays visible with an outline that meets contrast in both themes.
- **Secondary / Ghost:** not present today. If added, use a hairline button with ink text rather than a second accent.

### Pills

- **Style:** 999px capsule, App Wash fill with hairline border at rest, 0.9rem text.
- **State:** pass uses Pass Mint fill and ink; fail uses Fail Rose fill and ink; stream status reuses the rest pill with text such as Stream: live.

### Cards / Containers

- **Corner Style:** 8px for cards and kanban cards, 10px for kanban columns.
- **Background:** Card White on App Wash.
- **Shadow Strategy:** none. See Elevation.
- **Border:** 1px Hairline.
- **Internal Padding:** 1rem 1.1rem for cards, 0.7rem 0.8rem for kanban cards, 0.8rem for columns.

### Inputs / Fields

- **Style:** Card White fill, 1px Hairline border, 6px radius, 0.45rem 0.6rem padding, ink text.
- **Focus:** border and outline treatment must stay visible in both themes; no custom glow exists today.
- **Error / Disabled:** no dedicated styles today. Errors surface as Fail Rose pills in a live region next to the control.

### Navigation

Topbar brand in Deep Blue at 800 weight, 1.25rem, 0.02em tracking. Side nav links pad at 0.5rem 0.7rem, 6px radius, 600 weight, muted at rest. Hover fills App Wash with ink text. Active fills Deep Blue with white text. Project switcher is a native select in the topbar. Mobile stacks the nav above content.

### Kanban board

Three labeled columns with status-colored titles. Each card shows an ID plus status dot row, a title, and a muted state and owner line. Dependency links list below the grid in a plain card with muted relation labels.

### Evidence quote

Italic blockquote on App Wash with top, right, and bottom hairlines and a 0 6px 6px 0 radius. Used in Activity for the quoted words behind each write, and in Setup for the team announcement. Always paired with a muted status line and, for writes, an Undo action.

## Do's and Don'ts

### Do:

- **Do** keep one accent: blue for actions and active states only, hover at #1e3a8a in light and #2f5cc4 in dark.
- **Do** pair every status color with a label and keep dots at 0.7rem circles.
- **Do** use the card stack for new screens such as RAID and Settings, with a title plus muted helper line.
- **Do** announce async results in a live region next to the control that ran them.
- **Do** reuse 6px, 8px, 10px, and 999px before adding a radius.
- **Do** keep content at 980px max with 1.5rem page padding, and collapse kanban at 800px.

### Don't:

- **Don't** add shadows, gradients, or a second accent without replacing the world in full.
- **Don't** use status colors for buttons, nav, or brand text.
- **Don't** add a web font, icon font, or CSS framework. Inline SVG only, system stack only.
- **Don't** put raw transcripts in the console. Show synthesized status plus short evidence quotes.
- **Don't** invent tokens. Both themes are resolved in CSS; keep them in sync and keep dark values normative.
