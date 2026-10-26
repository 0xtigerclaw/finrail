# Finrail: product context

## What it is

A finance-first agent harness. A deterministic dispatcher runs pluggable
capabilities under rules each capability declares once in code: whether it
reads or acts, whether a person must approve, and how its result is verified
after it executes. The model is confined to judged work inside those rails.
One engine seam serves judged calls, so the same capability runs unchanged
on different model providers while the trust rules stay put.

## Who it serves

Investment and finance operators evaluating whether an AI system can be
trusted with figures that other people act on, and the judges and reviewers
who must verify that trust story from a public demo and repository without
credentials. Assumed, from the build brief: the primary scene is a reviewer
at a desk, in a browser, deciding within minutes whether the harness's
rules are real.

## The mechanism in one sentence

The model can suggest evidence, but only deterministic code can award it
trust, and only a human can release a decision.

## Surfaces

- `finrail` CLI: the primary surface. Three verbs (get, do, ask), approvals,
  the run ledger, capabilities and rules rendered from live declarations.
- Web console: a shell over the same dispatcher for demonstration and
  review. No privileged browser path; approvals obey the same caller rule
  as the CLI.

## Proof the product offers

- A planted false figure refused by the claim check, on the record.
- Evidence-gate verdicts (VERIFIED is never awarded by the model).
- Deterministic metrics cited to workbook cells and recomputed.
- A memo released only through a hash-bound human approval, re-hashed
  after release.
- A run ledger from which any judged call can be reconstructed.

## Brand commitments

- Say what is true, including about the system's own limits; limitations
  are stated on the surface, not discovered by the reviewer.
- Synthetic demonstration data is labelled synthetic wherever it appears.
- One accent, semantic state colours, no decoration that competes with
  verdicts.

## Assumptions (labelled, from the build brief)

- Reviewers arrive without credentials; read paths must work with none.
- The console is a demonstration surface; the CLI is the product surface.
- Light theme chosen by the owner (structured choice, 26 Oct 2026):
  modern fintech light, Mercury/Stripe-class calm. A dark OLED variant
  was built and rejected by the owner on 27 Oct; light is shipped.
