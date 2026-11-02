"""THE EVALUATION HARNESS: the headline accuracy claim, re-runnable from the repo.

`docs/model-comparison.md` says Nemotron 3.5 passed 70 of 70 analyze picks, Nemotron 3
67 of 70 and Opus 35 of 35. The scripts that produced those numbers lived outside the
repo, so a buyer could read the claim but not run it. This package puts the whole loop
in one place:

  cases     a JSON file of cases: which filing, which capability, which parameters,
            what must come out (a figure, a pass, or a refusal with a reason), and
            whether the case is grounded in a documented figure or still a draft
  runner    dispatches every case through the ordinary `dispatch_core` path N times
            and reads what the ledger recorded about each model call (tokens, time,
            cost), so the eval measures the system as it runs, not a copy of it
  stub      a local engine that answers from a recorded-answers file over the same
            HTTP seam a real engine uses, plus synthetic filings the source-cell
            checks can run against, so everything here works offline and without a key
  compare   deltas between a new summary and a baseline, failing when the pass rate
            drops below the baseline's 95% interval
  report    the Markdown tables of `docs/model-comparison.md`, rendered from summaries

Nothing here changes a check, a prompt or a capability. The model still picks row IDs
and copies numbers; `public_reports.check_and_compute` still judges every pick. The eval
only asks the same question many times and writes down the answers.

Run `python -m finrail.eval --help`.
"""
