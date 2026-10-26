"""Finance pack tests: gate, claim check, compute, screen, memo release.

Everything here is offline and deterministic. The judged capabilities
(dataroom.propose, memo.draft) are tested at their validate/execute/verify
surface with hand-written proposals, which is exactly the surface the
dispatcher enforces after a model fills the schema.
"""

import json
import pathlib

import pytest

from finrail.extensions.finance import (
    ComputeMetric,
    DataroomPropose,
    MemoDraft,
    MemoRelease,
    ScreenEntity,
    ScreenRun,
    _room,
)
from finrail.extensions.finance import gate


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))


# ── evidence gate ────────────────────────────────────────────────────────────

def test_fabricated_quote_is_discarded_never_repaired():
    candidate = {"assertions": ["something happened"]}
    raw = [{"quote": "a quote that was never written",
            "content": "entirely different extracted text",
            "source_tier": "primary", "publisher": "x",
            "supports_assertions": [0], "contradicts_assertions": []}]
    validated, discarded = gate.validate_evidence(candidate, raw)
    assert validated == [] and discarded == 1
    assert gate.classify(candidate, validated)["status"] == "rejected"


def test_fixture_screen_verdicts():
    screen = _room()["screens"]["northwind"]
    verdicts = []
    for candidate in screen["candidates"]:
        validated, _ = gate.validate_evidence(candidate,
                                              candidate.get("evidence", []))
        verdicts.append(gate.classify(candidate, validated)["status"])
    # One credible-secondary publisher per assertion: REPORTED, not VERIFIED.
    # The planted margin claim has no supporting quote: REJECTED.
    assert verdicts == ["reported", "rejected"]


def test_two_publishers_per_assertion_would_verify():
    candidate = {"assertions": ["an event happened"]}
    content = "Publisher text states an event happened on a date."
    evidence = [
        {"quote": "an event happened", "content": content,
         "source_tier": "credible_secondary", "publisher": "a.example",
         "supports_assertions": [0], "contradicts_assertions": []},
        {"quote": "an event happened", "content": content,
         "source_tier": "credible_secondary", "publisher": "b.example",
         "supports_assertions": [0], "contradicts_assertions": []},
    ]
    validated, _ = gate.validate_evidence(candidate, evidence)
    assert gate.classify(candidate, validated)["status"] == "verified"


# ── claim check ──────────────────────────────────────────────────────────────

def test_planted_figure_refused_against_checked_source():
    propose = DataroomPropose()
    planted = {"title": "Margin", "claim": "Meridian margin was strong",
               "figure": "24.5%", "source_doc": "im-2026"}
    problems = propose.validate(planted)
    assert problems and "claim check refused" in problems[0]


def test_planted_figure_refused_against_the_draft_itself():
    propose = DataroomPropose()
    planted = {"title": "Margin", "claim": "Meridian margin was strong",
               "figure": "24.5%", "source_doc": "draft-note"}
    problems = propose.validate(planted)
    assert problems and "unverified draft" in problems[0]


def test_figure_smuggled_in_claim_text_is_refused():
    propose = DataroomPropose()
    smuggled = {"title": "Margin",
                "claim": "Meridian achieved a 24.5% EBITDA margin in FY2025 "
                         "(figure not found in verified documents)",
                "figure": "", "source_doc": "draft-note"}
    problems = propose.validate(smuggled)
    assert problems and "claim states '24.5%'" in problems[0]


def test_carried_figure_passes():
    propose = DataroomPropose()
    carried = {"title": "Revenue", "claim": "Revenue FY2025 was EUR 48.2m",
               "figure": "48.2", "source_doc": "im-2026"}
    assert propose.validate(carried) == []


# ── deterministic compute (hand-verified against the fixture) ───────────────

def test_compute_metrics_match_hand_arithmetic():
    metric = ComputeMetric()
    # EBITDA = 48.2 - 29.1 - 11.4 = 7.7 (values are reported rounded to 6dp)
    assert metric.run(metric="margin")["value"] == pytest.approx(
        7.7 / 48.2, abs=1e-6)
    assert metric.run(metric="growth")["value"] == pytest.approx(
        6.5 / 41.7, abs=1e-6)
    assert metric.run(metric="leverage")["value"] == pytest.approx(
        15.3 / 7.7, abs=1e-6)
    assert metric.run(metric="cash_conversion")["value"] == pytest.approx(
        6.1 / 7.7, abs=1e-6)
    headroom = metric.run(metric="covenant_headroom")["value"]
    assert headroom == pytest.approx((3.0 - 15.3 / 7.7) / 3.0, abs=1e-6)


def test_compute_cites_its_cells():
    out = ComputeMetric().run(metric="margin")
    cells = {i["cell"] for i in out["inputs"]}
    assert cells == {"IncomeStatement!B2", "IncomeStatement!B4",
                     "IncomeStatement!B5"}


# ── screen flow: entity confirmation, run, verify ────────────────────────────

def test_screen_requires_confirmed_entity():
    with pytest.raises(Exception):
        ScreenRun().plan(company_id="04123456")


def test_screen_end_to_end():
    entity = ScreenEntity()
    proposal = entity.plan(company="Northwind Components",
                           company_id="04123456")
    result = entity.execute(proposal)
    ok, _ = entity.verify(proposal, result)
    assert ok

    run = ScreenRun()
    run_proposal = run.plan(company_id="04123456")
    run_result = run.execute(run_proposal)
    assert run_result["status_counts"] == {"reported": 1, "rejected": 1}
    ok, _ = run.verify(run_proposal, run_result)
    assert ok


# ── memo: draft validation and hash-bound release ────────────────────────────

def _draft_proposal():
    return {"title": "Meridian Components: IC memo",
            "sections": [{"heading": "Scale",
                          "body": "Revenue FY2025 was EUR 48.2m.",
                          "citations": ["im-2026"]}],
            "gaps": ["No segment margin is disclosed in the data room."]}


def test_memo_draft_rejects_uncarried_figure():
    draft = MemoDraft()
    bad = _draft_proposal()
    bad["sections"][0]["body"] = "EBITDA margin reached 24.5% in FY2025."
    problems = draft.validate(bad)
    assert problems and "claim check refused" in problems[0]


def test_memo_draft_rejects_uncited_section():
    draft = MemoDraft()
    bad = _draft_proposal()
    bad["sections"][0]["citations"] = []
    problems = draft.validate(bad)
    assert problems and "cites no source" in problems[0]


def test_memo_release_binds_to_hash():
    draft = MemoDraft()
    saved = draft.execute(_draft_proposal())
    release = MemoRelease()
    proposal = release.plan(memo_id=saved["memo_id"])
    assert "48.2" in proposal["full_text"]
    result = release.execute(proposal)
    ok, detail = release.verify(proposal, result)
    assert ok, detail
    released = pathlib.Path(result["released"])
    assert released.read_text() == proposal["full_text"]
