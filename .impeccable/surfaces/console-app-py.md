---
version: 1
slug: "console-app-py"
primary_target: "console/app.py"
related_targets:
  - "console/views.py"
  - "console/static/app.css"
  - "console/static/app.js"
  - "console/demo.py"
---

# Surface brief: Finrail investment workspace

Scope: all console routes. Mode: Operate, code-led. No visual comp was used.
This approved system redesign preserves the owner-pinned modern fintech light
world and changes the surface hierarchy to an investment workspace: company
context, evidence, analysis and decisions first; diagnostics secondary.

Audience: investment and finance reviewers evaluating the demonstration without
credentials. Job: understand company context, inspect source-backed figures,
prepare a record or memo, review an exact proposal and reconstruct the work.
The console remains FastAPI server-rendered and uses the existing dispatcher;
the browser provides no privileged approval path.

## Direction contract

THESIS: A precise investment workspace where a reviewer moves from company
context to source-backed analysis to controlled decisions.

OWN-WORLD: Cool paper ground, white bordered panels, quiet slate sidebar and a
restrained navy action family. Semantic tones identify actual states. Compact
financial reading views use tabular numerals, readable units, explicit citations
and nearby provenance. Depth comes from tonal separation and fine rules; dark
code wells remain literal terminal examples. Preserve the owner-pinned light
direction; the dark OLED transformation was rejected.

STORY: The reviewer sees two distinct synthetic company contexts, follows the
source, checks a deterministic calculation, prepares an investment memo, and
reviews a hash-bound proposal. The model suggests evidence, deterministic code
awards verification, and the operator releases a decision through the terminal.

FIRST VIEWPORT: Compact sidebar and topbar, synthetic workspace context, clear
overview title, two actual demo company rows with dataset/status/action, recent
work and a review queue. Company context and task actions lead over engine
diagnostics. No invented portfolio statistics or decorative chart.

FORM: Modern fintech light, owner-confirmed on 26 October 2026 and retained in
this redesign. Verified surface seed: `seed:ae83cb8cce12d797`. Explicit user
selection takes precedence over the seed. One inherited sans stack, navy
actions, semantic verdict colors, tabular financial data and restrained panels.

## Implemented expression

- Assets: `console/static/app.css` is the token and layout source;
  `console/static/app.js` provides progressive enhancements. Shared HTML patterns
  live in `console/views.py`; routes and composition live in `console/app.py`.
  No framework migration or raster assets. The brand and control icons are
  authored inline SVGs; no image-generation provenance is required.
- Navigation: Workspace contains Overview, Screening, Data room, Analysis,
  IC memos and Approvals; System contains ledger, capabilities, rules and command
  reference. The mobile drawer replaces wrapping navigation, with focus trap,
  Escape/backdrop/close dismissal, inert background and focus return. Without
  JavaScript, narrow-screen navigation remains available in the flow.
- Fixtures: Northwind Components is the screening context; Meridian Components
  is the data-room, workbook and memo context. Keep their identities separate.
  Synthetic dataset and illustrative-record labels disclose manually prepared
  examples and shared/ephemeral demonstration limits.
- Evidence: selectable source list, search, checked/unverified detail and a
  labelled drafting task with reviewable examples. Claim-blocked recovery keeps
  the next useful source action visible.
- Analysis: readable units, cited inputs, formulas, raw values and independent
  recomputation. Screening findings lead with readable verdicts and evidence;
  raw records remain behind disclosures rather than replacing the reading view.
- Memos: source-linked section text, open diligence, artifact IDs distinct from
  run IDs, full proposals and complete SHA-256 for operator review. Browser
  release remains a proposal; approval executes only in the terminal.
- Controls: labelled required fields, preserved invalid input, local recovery,
  accepted-submission busy state, duplicate-submit protection, truthful copy
  announcements, keyboard focus and reduced-motion behavior. Pending work uses
  GET detail refreshes and does not replay POST requests.
- Responsive: supporting columns stack, tables scroll in their own focusable
  regions and long IDs/hashes wrap. The 760px drawer threshold and 390/360px
  density adjustments are documented in `DESIGN.md` and its sidecar.

## Finish and evidence status

Documentation refreshed from implemented source on 27 October 2026.
The independent finish reviewer has visually accepted the quality bar; the
required complete approval hash is now visible. Native keyboard recapture passed initial focus, forward/reverse Tab wrapping
and Escape focus return. The independent verdict scored all three material fixes resolved
and returned ship, scoped to those fixes. This does not claim multi-user production readiness. The parent review owns the
final verification record in `.impeccable/review/`.
