"""Opt-in, labelled synthetic activity for the investment-workspace demo.

No historical model calls are fabricated. Source data is the bundled finance
fixture; arithmetic and validation run through its real capability classes.
Nothing is approved or released here. The only artifacts written are synthetic
screen evidence and a manually authored memo draft in this instance's state.

Set FINRAIL_DEMO_WORKSPACE=1 to seed. Call seed_demo_workspace() after the
normal demo-entity seed. It returns the seed manifest, or None when disabled.
Every activity record carries demo_fixture=True and source='demo-fixture'.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from finrail.core import atomic, tracker
from finrail.core.errors import ContractViolation
from finrail.core.settings import state
from finrail.extensions.finance import (
    ComputeMetric,
    DataroomPropose,
    MemoDraft,
    MemoRelease,
    _classify,
    _room,
    _root,
)

DEMO_VERSION = 1
DEMO_LABEL = "Illustrative demo record"
_FIXTURE = Path(__file__).parent / "fixtures" / "workspace-demo.json"


def _record(key: str, capability: str, task: str, proposal: dict,
            status: str, result=None, errors: list | None = None,
            company: str = "Meridian Components") -> dict:
    # Reuse already-completed seed steps if a prior startup stopped midway.
    existing = next((r for r in tracker.recent(None)
                     if r.get("demo_fixture")
                     and r.get("demo_version") == DEMO_VERSION
                     and r.get("demo_key") == key), None)
    if existing:
        return existing
    rec = tracker.save(
        capability, "Illustrative demo: " + task, proposal, status=status,
        errors=errors, source="demo-fixture", actor="demo-fixture")
    return tracker.update(
        rec["id"], result=result, demo_fixture=True,
        demo_label=DEMO_LABEL, demo_version=DEMO_VERSION,
        demo_key=key, demo_company=company,
        demo_provenance="Manually prepared synthetic example; not a model-generated historical run.")


def _validate(capability, proposal: dict) -> None:
    problems = capability.validate(proposal)
    if problems:
        raise ContractViolation("Synthetic demo proposal failed its real claim check: "
                                + "; ".join(problems))


def seed_demo_workspace() -> dict | None:
    """Seed each state directory once; never make model calls or release actions.

    The manifest intentionally survives user drops: refreshing a page must not
    recreate a proposal the operator chose to discard. A fresh instance/state
    directory gets its own clearly labelled set of examples.
    """
    if os.environ.get("FINRAIL_DEMO_WORKSPACE") != "1":
        return None
    manifest_path = state() / "console-demo-workspace.json"
    with atomic.locked(manifest_path, timeout=10):
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("version") == DEMO_VERSION:
                return manifest
        examples = json.loads(_FIXTURE.read_text())
        room = _room()
        records = {}

        # A real deterministic computation, displayed as prepared sample work.
        computed = ComputeMetric().run(metric="margin")
        rec = _record("margin", "finance.compute.metric", "Meridian EBITDA margin from workbook cells",
                      {"metric": "margin"}, tracker.DONE, computed)
        records["margin"] = rec["id"]

        # The Northwind screen is a separate synthetic company, never silently
        # merged into Meridian's data-room/memo evidence.
        fixture = copy.deepcopy(room["screens"]["northwind"])
        findings, counts, discarded = _classify(fixture["candidates"])
        screen = {"company": fixture["company"], "company_id": fixture["company_id"],
                  "source": "fixture", "findings": findings, "status_counts": counts,
                  "quotes_discarded": discarded, "demo_fixture": True,
                  "demo_provenance": DEMO_LABEL}
        screen_path = _root() / "screens" / f"{fixture['company_id']}.json"
        # Never replace a screen produced by a person after the demo started.
        if not screen_path.exists():
            atomic.write_json(screen_path, screen, indent=2)
        rec = _record("northwind-screen", "finance.screen.run", "Northwind evidence-gate review",
                      {"company": fixture["company"], "company_id": fixture["company_id"]},
                      tracker.DONE, {**screen, "record": str(screen_path)},
                      company=fixture["company"])
        records["northwind-screen"] = rec["id"]

        # This is a genuine refusal by the existing validator, not a simulated
        # success followed by a visual error. No record artifact is written.
        blocked = examples["blocked_record"]
        refused = DataroomPropose().validate(blocked)
        if not refused:
            raise ContractViolation("The planted demo claim was unexpectedly accepted.")
        rec = _record("blocked-margin", "finance.dataroom.propose", "Unsupported 24.5% margin claim blocked",
                      blocked, tracker.FAILED,
                      {"blocked": True, "record_created": False, "source_doc": "draft-note"}, refused)
        records["blocked-margin"] = rec["id"]

        # Manually authored, clearly disclosed prose is validated by precisely
        # the same claim check as a model draft. No model usage is invented.
        memo = examples["memo"]
        _validate(MemoDraft(), memo)
        memo_result = MemoDraft().execute(memo)
        memo_path = Path(memo_result["draft"])
        saved = json.loads(memo_path.read_text())
        if saved.get("id") != memo_result["memo_id"]:
            raise ContractViolation("Synthetic memo artifact did not match its ID.")
        _validate(MemoDraft(), saved)
        rec = _record("memo-draft", "finance.memo.draft", "Preliminary Meridian investment review with explicit gaps",
                      memo, tracker.DONE, memo_result)
        records["memo-draft"] = rec["id"]

        # Pending proposals go through the normal terminal-only authority; this
        # seed never calls MemoRelease.execute or approves a tracker record.
        release = MemoRelease().plan(memo_id=memo_result["memo_id"])
        rec = _record("memo-review", "finance.memo.release", "Review the full memo and hash before release",
                      release, tracker.PENDING)
        records["memo-review"] = rec["id"]

        supported = examples["supported_record"]
        _validate(DataroomPropose(), supported)
        rec = _record("revenue-review", "finance.dataroom.propose", "Review the source-backed Meridian revenue record",
                      supported, tracker.PENDING)
        records["revenue-review"] = rec["id"]

        manifest = {"version": DEMO_VERSION, "synthetic": True,
                    "provenance": examples["provenance"], "records": records,
                    "memo_id": memo_result["memo_id"]}
        atomic.write_json(manifest_path, manifest, indent=2)
        return manifest
