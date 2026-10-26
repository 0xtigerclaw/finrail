# Finrail design system

The console's visual world, as built in `console/app.py` (server-rendered,
no build step; the CSS constant in that file is the token source). Mode:
Operate. Direction: modern fintech light, owner-pinned 26 Oct 2026.

## Tokens

| Role | Value | Use |
| --- | --- | --- |
| `--paper` | `#f7f9fc` | Page ground |
| `--card` | `#ffffff` | Surfaces |
| `--ink` | `#12263f` | Primary text |
| `--ink-2` | `#54657d` | Secondary text (5.6:1 on ground, 5.9:1 on white) |
| `--side` | `#eef2f8` | Sidebar, the second neutral layer |
| `--side-dim` | `#54657d` | Sidebar secondary text (5.3:1) |
| `--accent` | `#1b4b8f` | Primary actions, current selection only |
| `--accent-hover` | `#143a70` | Primary action hover |
| `--code` | `#0e1a2c` | Terminal wells, the one dark element |
| `--line` | `#dfe6ef` | Table row rules, dividers |
| `--shadow` | `0 1px 2px rgba(18,38,63,.05), 0 1px 3px rgba(18,38,63,.07)` | The single elevation |

Semantic state colours, used only for verdicts and run states: ok
`#146c43` on `#e7f4ec`, warn `#8a5a00` on `#fdf3e0`, bad `#a02c2c` on
`#fdecec`, info `#1f4e79` on `#e9f1f9`. All badge pairs pass 4.5:1.

## Rules

- **One elevation language.** Cards and pills use the shadow alone; tables
  use horizontal row rules, never a border under a shadow. Card radius is
  0.875rem; pills are reserved for badges and status chips.
- **Accent discipline.** Navy appears on primary buttons, the current nav
  item, links, and focus outlines. It never decorates inactive surfaces.
- **Verdicts are the loudest element.** Status badges render at 0.82rem,
  bold, uppercased, before the claim they judge.
- **Type.** One system sans, fixed rem scale (1.5 / 1.05 / 0.97rem
  headings), `tabular-nums` for every number that is data, prose measure
  capped near 46rem.
- **Code wells.** Terminal equivalents render dark because they are code;
  copy buttons inside them take a light focus outline (`#9db9dd`).
- **States.** Every control ships default, hover, focus-visible (2px
  accent outline), disabled; errors appear inline next to the action with
  `role="alert"` and a text lead, never an icon glyph; empty states name
  the next action.
- **Responsive is structural.** Below 54rem the sidebar collapses to a
  wrapping nav row with the strapline hidden, so the page task reaches
  the first viewport on a 390px phone.

## Provenance

Built under the Impeccable skill (github.com/pbakaus/impeccable), Operate
mode, code-led. Surface brief: `.impeccable/surfaces/console-app-py.md`.
Review evidence: `.impeccable/review/` (gitignored). No raster assets
ship with this surface; nothing requires a generation provenance entry.
