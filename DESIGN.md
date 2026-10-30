---
name: "Finrail"
description: "A precise, light investment workspace with evidence beside every decision."
colors:
  paper: "#f6f8fb"
  surface: "#ffffff"
  sidebar: "#f9fafc"
  ink: "#172b46"
  ink-secondary: "#52627a"
  ink-muted: "#65758b"
  line: "#e2e8f0"
  line-strong: "#ccd6e3"
  accent: "#214d83"
  accent-hover: "#183d6b"
  accent-soft: "#eaf0f8"
  success: "#216440"
  success-bg: "#eaf4ed"
  warning: "#815a16"
  warning-bg: "#fbf2df"
  danger: "#a33434"
  danger-bg: "#fbeded"
  info: "#28598a"
  info-bg: "#eaf1fa"
  code: "#17263b"
typography:
  headline:
    fontSize: "30px"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.025em"
  title:
    fontSize: "18px"
    fontWeight: 650
    lineHeight: 1.35
    letterSpacing: "-0.015em"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif"
    fontSize: "14px"
    lineHeight: 1.55
  label:
    fontSize: "12px"
    fontWeight: 550
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace"
    fontSize: "12px"
  metric:
    fontSize: "42px"
    fontWeight: 650
    lineHeight: 1.15
    letterSpacing: "-0.035em"
rounded:
  tag: "4px"
  control: "7px"
  inset: "8px"
  terminal: "9px"
  panel: "12px"
spacing:
  compact: "8px"
  control: "12px"
  small: "16px"
  medium: "20px"
  panel: "22px"
  section: "24px"
  large: "28px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "9px 14px"
    typography: "{typography.label}"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink-secondary}"
    rounded: "{rounded.control}"
    padding: "9px 14px"
    typography: "{typography.label}"
  button-danger:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.danger}"
    rounded: "{rounded.control}"
    padding: "9px 14px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
  navigation-current:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.accent}"
    rounded: "{rounded.control}"
    padding: "8px 11px"
  badge-verified:
    backgroundColor: "{colors.success-bg}"
    textColor: "{colors.success}"
    rounded: "{rounded.tag}"
    padding: "3px 7px"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "22px"
---

# Design System: Finrail

## Overview

**Creative North Star: "Evidence-first investment workspace"**

Finrail uses the owner-confirmed modern fintech light direction: paper and slate neutrals, navy actions, compact financial reading surfaces, and restrained semantic verdict colors. The owner selected light on 26 October 2026 and rejected a dark OLED transformation on 27 October; preserve this light world.

The system is recorded from the implemented console in code-led mode, without a visual comp. The normative asset source is `console/static/app.css`; `console/static/app.js` supplies progressive interaction, `console/views.py` supplies shared presentation, and `console/app.py` composes server-rendered routes. The surface brief records the confirmed investment-workspace hierarchy; this document records reusable visual behavior.

**Key Characteristics:**
- Quiet, light surfaces with navy selection and actions.
- Compact tables, tabular figures, and readable evidence prose.
- Verification, synthetic provenance, and human authority visible in context.
- Borders and tonal layers rather than decorative elevation.

## Colors

The palette combines cool paper neutrals with a single navy action family; semantic tones describe recorded states.

### Primary
- **Investment Navy** (`accent`, `accent-hover`, `accent-soft`): primary actions, links, current navigation, selected documents and focus treatment.

### Neutral
- **Cool Paper** (`paper`): the page ground behind white panels.
- **White Surface** (`surface`): panels, fields and the topbar.
- **Quiet Slate** (`sidebar`): persistent navigation ground.
- **Deep Ink / Reading Slate / Metadata Slate** (`ink`, `ink-secondary`, `ink-muted`): primary content, supporting prose and metadata.
- **Fine Rule / Field Rule** (`line`, `line-strong`): structural separators and input boundaries.
- **Terminal Navy** (`code`): command examples; the application itself remains light.

Semantic success, warning, danger and information foreground/background pairs describe verification, pending or unverified work, refusal or failure, and running or model-assisted states. Their text labels carry the meaning alongside color.

**The Accent Discipline Rule.** Use navy for actions, links and selection; use semantic colors to communicate recorded state.

## Typography

**Body Font:** the platform sans stack in the frontmatter. Headings inherit this stack; no dedicated display face is established.
**Label/Mono Font:** the platform monospace stack for IDs, hashes, formulas, commands and raw records.

The hierarchy supports a dense reading interface. Page headings lead, compact panel titles organize, and prose remains separated from metadata. Financial values and table cells use tabular numerals.

### Hierarchy
- **Headline:** page title; reduced to 27px below 760px and 25px below 390px.
- **Title:** section heading; panel headers use 14px at weight 650, and smaller headings use 15px.
- **Body:** base reading size; document prose uses 13px with line-height 1.85 and a maximum measure of 72ch.
- **Label:** form and action labels; metadata commonly uses 11px.
- **Mono:** exact machine-readable references; long hashes and IDs wrap rather than truncating the value.
- **Metric:** prominent financial result; reduced to 38px below 760px. Percentages and leverage multiples retain their units and raw values nearby.

**The Exact Reference Rule.** Keep exact IDs, source references and complete approval hashes readable even when the friendly financial view leads.

## Layout

The desktop shell has a sticky 232px sidebar, a sticky topbar and a content column capped at 1512px. Wide content has 38px horizontal padding. Panels use a 22px interior and 24px section gaps. Tables carry compact horizontal rules. Nested panels flatten instead of multiplying frames.

The main/supporting split uses a flexible main column and a 298px support column. Below 1200px gutters tighten; below 1100px task and metric layouts stack and supporting content can use two columns. Below 1000px the sidebar narrows to 208px. Below 760px it becomes a modal drawer when JavaScript is available, and content uses 20px gutters. Below 390px gutters become 16px and document lists, result fields and events stack; below 360px gutters tighten to 13px.

Tables scroll within labelled, focusable regions. Do not force the page canvas wider to accommodate their minimum reading widths. Without JavaScript the narrow-screen navigation remains visible in document flow.

## Elevation & Depth

The system is flat at rest: white surfaces, fine border rules and quiet background shifts distinguish areas. Panels do not use drop shadows. The mobile drawer uses a translucent dark backdrop and fixed positioning rather than a new card-shadow vocabulary. Dark command wells indicate literal terminal content.

**The Flat Surface Rule.** Use tonal separation and fine borders for ordinary panels; do not restore the obsolete shadow-only card treatment.

## Shapes

Panels have softly rounded corners using the panel token. Controls and navigation share the tighter control radius; tags and verdict badges use the compact tag radius. Insets and terminal wells use their corresponding intermediate radii. Authored stroke SVGs provide icons with consistent strokes and explicit text labels where an action needs a name.

## Components

### Buttons
Compact and deliberate. Primary buttons use navy; secondary buttons use white with the stronger field border; destructive buttons use danger text and a restrained danger border. Regular controls have a 38px minimum height. Hover deepens or softly tints the surface. Disabled buttons reduce opacity and block interaction. Accepted submissions show “Working…” and disable submitters until navigation resolves.

### Inputs / Fields
White, outlined fields with visible labels, hints, a 40px minimum height and a navy caret. Hover strengthens the border; focus adds a navy border and the shared outline. Invalid fields use the danger border. Textareas resize vertically. Examples fill a real drafting field and return focus to it for review.

### Navigation
Grouped sidebar links use authored SVGs, muted default text, a subtle hover fill and navy-on-soft-navy current state with `aria-current`. The mobile drawer has a close control, backdrop, Escape dismissal, focus trapping and focus return; background content is inert while open. Keep the non-JavaScript navigation available.

### Chips / Verdicts
Compact rounded rectangles communicate verified, reported, unverified, pending, running and failed states with text. Synthetic context and illustrative-record labels stay distinct from verification verdicts. Do not award success styling to a model assertion merely because it reads confidently.

### Cards / Containers
White bordered panels contain a distinct header and reading body. A panel containing a table lets the table meet its edges; nested panels lose their extra frame. Empty states name a useful next action. Alerts stay next to the task, describe the failure and provide recovery.

### Financial Evidence
Company identity, synthetic dataset context, verification and source citations stay near the result they qualify. Metrics use readable percentages or multiples while preserving formulas, raw values and workbook cell references. Source documents have a selectable list, checked or unverified status and a readable detail pane. Raw records and terminal equivalents remain in disclosures.

### Memo / Approval Reading
Memo sections use readable paragraphs with explicit source links and an open-diligence list. Show actual memo artifact IDs separately from run IDs. Approval panels expose the complete proposal and full SHA-256, with a terminal approval command. Illustrative synthetic memo and ledger records disclose their provenance in text.

### Interaction States
Focusable controls have a 2px navy outline with a 3px offset; copy controls in dark wells use a light outline. Navigation transitions take 160ms, controls and disclosures 150ms, and the drawer 180ms. Respect reduced-motion preferences. Clipboard feedback is announced only after a confirmed copy. Pending work refreshes a safe GET detail page rather than replaying a submission.

## Do's and Don'ts

### Do:
- **Do** preserve the owner-pinned light palette and restrained navy action family.
- **Do** keep company identity, synthetic provenance and evidence labels adjacent to the financial content they qualify.
- **Do** use tabular numerals, readable financial units and exact source references together.
- **Do** retain labelled controls, visible focus, inline recovery and keyboard-operable disclosures.
- **Do** keep complete proposals and hashes available before a controlled decision.

### Don't:
- **Don't** revive the rejected dark application theme or obsolete wrapping mobile navigation.
- **Don't** style illustrative synthetic records as proof of a live model call.
- **Don't** turn decorative shadows, oversized heroes or charts without evidence into the system's identity.
- **Don't** replace drawn icons with emoji or truncate the only readable copy of a hash or artifact ID.
