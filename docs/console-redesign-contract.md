# Finrail console redesign contract

User request: production-level UI/UX redesign. Confirmed direction: investment workspace; company context, evidence, analysis and decisions lead, diagnostics are secondary. Retain the owner-pinned modern light fintech world. The explicit user selection overrides the surface seed (seed:ae83cb8cce12d797).

## Experience
- Persistent compact sidebar, task-oriented navigation, accessible mobile drawer, and truthful synthetic/shared/ephemeral demo context.
- Overview leads with the actual two demo company contexts and a live recent-work table; no invented portfolio statistics or fake actions.
- Northwind Components is the screening fixture. Meridian Components is the data-room/workbook/memo fixture. Never merge their identities.
- Data room: document list, search and readable source detail; labelled drafting form with examples, checked/unverified labels, claim-blocked recovery.
- Compute: selected metric, finance-friendly units, cited inputs and raw value/formula retained.
- Memos: drafting and actual artifact IDs; review complete text and hash before release proposal. Approval remains terminal-only.
- Runs: truthful status, links to GET detail pages, activity filtering and actual model evidence available behind disclosures.
- Forms: required labels, preserved input, useful inline validation, pending/busy state and duplicate-submission protection. GET-only status refresh; never replay POST.

## Implementation boundaries
Keep FastAPI SSR and the existing dispatcher. Extract static CSS/JS into packaged console/static assets; no frontend framework migration. No altered trust gates, fixtures, approval identity, persistent storage, auth or fake backend capabilities. UI polish does not claim multi-user production readiness.

## Verification
Existing 27 offline tests, new console contract tests, wheel asset packaging, browser flow checks, light desktop and 390/360px mobile screenshots, keyboard focus/navigation, no page-wide horizontal overflow, no unsafe HTML rendering. Independent finish review on actual screenshots and source.

## Design contract
THESIS: A precise investment workspace where a reviewer moves from company context to source-backed analysis to controlled decisions.
FIRST VIEWPORT: A compact light sidebar and topbar, synthetic workspace context, a clear overview heading, two real demo company rows with dataset/status/action, a current activity list and review queue. No oversized hero or decorative chart.
SIGNATURE: Evidence provenance stays adjacent to financial values and verdicts. Each workflow keeps the company and synthetic dataset visible, with verification and human authority explicit.
FORM: Seed reference seed:ae83cb8cce12d797; explicit user direction takes precedence. Light paper/slate surfaces, navy action color, semantic verdict colors, one sans family, tabular numeric data, crisp tables and consistent restrained elevation.
