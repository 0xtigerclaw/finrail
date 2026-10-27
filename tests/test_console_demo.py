"""Synthetic workspace examples must be useful, truthful, and non-authoritative."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from console.demo import seed_demo_workspace
from finrail.core import llm, tracker
from finrail.core.settings import state
from finrail.extensions.finance import DataroomPropose, MemoDraft, MemoRelease


@pytest.fixture(autouse=True)
def demo_state(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FINRAIL_DEMO_WORKSPACE", "1")
    def no_call(*args, **kwargs):
        pytest.fail("Demo data seeding must not make model calls")
    monkeypatch.setattr(llm, "call", no_call)
    def no_release(*args, **kwargs):
        pytest.fail("Demo seeding must not release a memo")
    monkeypatch.setattr(MemoRelease, "execute", no_release)


def test_seeding_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("FINRAIL_DEMO_WORKSPACE")
    assert seed_demo_workspace() is None
    assert not state().exists()


def test_seed_is_idempotent_and_every_record_is_labelled():
    first = seed_demo_workspace()
    before = tracker.recent(None)
    assert seed_demo_workspace() == first
    after = tracker.recent(None)
    assert len(before) == len(after) == 6
    assert {r["id"] for r in before} == {r["id"] for r in after}
    assert all(r["demo_fixture"] and r["source"] == "demo-fixture" for r in after)
    assert all(r["task"].startswith("Illustrative demo:") for r in after)
    assert all(r["demo_label"] == "Illustrative demo record" for r in after)
    ledger = state() / "events" / "_ledger.jsonl"
    assert not any(json.loads(line).get("kind") == "model_call"
                   for line in ledger.read_text().splitlines())


def test_seed_keeps_financial_truth_and_companies_separate():
    manifest = seed_demo_workspace()
    margin = tracker.load(manifest["records"]["margin"])
    assert margin["result"]["value"] == 0.159751
    assert [row["value"] for row in margin["result"]["inputs"]] == [48.2, 29.1, 11.4]
    screen = tracker.load(manifest["records"]["northwind-screen"])
    assert screen["result"]["company"] == "Northwind Components"
    assert screen["result"]["status_counts"] == {"reported": 1, "rejected": 1}
    assert screen["demo_company"] != margin["demo_company"]
    assert screen["result"]["source"] == "fixture"


def test_seed_rejects_planted_claim_and_leaves_no_record_artifact():
    manifest = seed_demo_workspace()
    blocked = tracker.load(manifest["records"]["blocked-margin"])
    assert blocked["status"] == "failed"
    assert blocked["result"]["record_created"] is False
    assert DataroomPropose().validate(blocked["proposal"])
    assert not (state() / "finance" / "records").exists()


def test_memo_is_grounded_and_release_is_pending_hash_bound():
    manifest = seed_demo_workspace()
    drafted = tracker.load(manifest["records"]["memo-draft"])
    assert MemoDraft().validate(drafted["proposal"]) == []
    artifact = json.loads(Path(drafted["result"]["draft"]).read_text())
    assert artifact["id"] == manifest["memo_id"]
    assert len(artifact["gaps"]) == 4
    review = tracker.load(manifest["records"]["memo-review"])
    assert tracker.awaiting(review)
    assert review["proposal"]["sha256"] == hashlib.sha256(review["proposal"]["full_text"].encode()).hexdigest()
    assert "Northwind" in review["proposal"]["full_text"]
    assert "Sources: [im-2026]" in review["proposal"]["full_text"]
    assert not (state() / "finance" / "released").exists()
    assert not review.get("verdict")
    supported = tracker.load(manifest["records"]["revenue-review"])
    assert tracker.awaiting(supported)
    assert DataroomPropose().validate(supported["proposal"]) == []


def test_refresh_does_not_restore_a_dropped_demo_proposal():
    manifest = seed_demo_workspace()
    rid = manifest["records"]["revenue-review"]
    tracker.drop(rid, by="console")
    seed_demo_workspace()
    assert not any(r["id"] == rid for r in tracker.recent(None))
