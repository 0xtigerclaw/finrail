# Finrail design system

The console's visual world, as built in `console/app.py` (server-rendered,
no build step; the CSS constant in that file is the token source). Mode:
Operate. Direction: dark OLED terminal, set 26 Oct 2026. This replaces the
earlier owner-pinned light theme; the flow structure (numbered demo flow,
one primary action, progressive disclosure) is unchanged.

## Tokens

| Role | Value | Use |
| --- | --- | --- |
| `--paper` | `#020617` | Page ground |
| `--card` | `#0d1329` | Surfaces |
| `--ink` | `#f2f6fc` | Primary text (17:1 on card) |
| `--ink-2` | `#a3b1c6` | Secondary text (8.5:1 on card) |
| `--side` | `#050b1d` | Sidebar, the second neutral layer |
| `--side-ink` | `#c3cede` | Sidebar primary text (12.3:1) |
| `--side-dim` | `#8fa0ba` | Sidebar secondary text (7.4:1) |
| `--accent` | `#22c55e` | Primary actions, current selection, links (8.9:1 on ground) |
| `--accent-hover` | `#4ade80` | Primary action hover |
| `--accent-ink` | `#04140a` | Text on the accent (8.3:1) |
| `--code` | `#0a1122` | Terminal wells, inputs |
| `--line` | `#26334d` | Card borders, table row rules, dividers |
| `--shadow` | `0 1px 2px rgba(0,0,0,.35)` | The single elevation |

Semantic state colours, used only for verdicts and run states: ok
`#4ade80` on `#0a2e1c`, warn `#fbbf24` on `#2e2105`, bad `#f87171` on
`#331014`, info `#7dd3fc` on `#0b2438`. Every badge pair passes 6:1.

## Rules

- **One elevation language.** Cards and pills use the shadow alone; tables
  use horizontal row rules. Depth comes from the border-and-shadow pair
  against the near-black ground; there is no second elevation. Card radius
  is 0.875rem; pills are reserved for badges and status chips.
- **Accent discipline.** The green signal appears on primary buttons, the
  current nav item, links, focus rings, step numbers, and the brand mark.
  It never decorates inactive surfaces.
- **Verdicts are the loudest element.** Status badges render at 0.82rem,
  bold, uppercased, before the claim they judge.
- **Type.** One family, IBM Plex Sans (400/500/600/700), fixed rem scale
  (1.5 / 1.05 / 0.97rem headings), `tabular-nums` for every number that is
  data, prose measure capped near 46rem.
- **Icons are drawn.** Nav entries carry authored 16px stroke SVGs in one
  weight; the brand mark is a two-rail track glyph. No emoji or unicode
  glyphs stand in for icons.
- **States.** Every control ships default, hover (150ms), focus-visible
  (2px accent outline at 11.6:1 against the ground), disabled; touch
  targets are 44px. Errors appear inline next to the action with
  `role="alert"` and a text lead, never an icon glyph; empty states name
  the next action. `prefers-reduced-motion` removes all transitions.
- **Flow before inventory.** Navigation is two groups: the numbered demo
  flow (screen, data room, compute, memo, approvals) and Inspect (ledger,
  capabilities, rules, CLI). The overview is one numbered path with one
  primary action, never a card grid repeating the nav. Terminal
  equivalents and archived results sit behind disclosures.
- **Responsive is structural.** Below 54rem the sidebar collapses to a
  wrapping nav row with the strapline hidden, so the page task reaches
  the first viewport on a 390px phone.

## Provenance

Design direction generated with the UI/UX Pro Max skill
(github.com/nextlevelbuilder/ui-ux-pro-max-skill; dark OLED style,
IBM Plex Sans, green signal accent), adapted to an Operate console: its
landing-page pattern sections do not apply here, and its scroll-reveal
motion is implemented as state transitions only, with no JavaScript
dependency. Flow structure from the Impeccable distill pass; craft-floor
checks (contrast, drawn icons, no colored side borders, one elevation)
still hold. Detector: `impeccable detect` returns no findings on
`console/app.py`. No raster assets ship with this surface.
