"""Approval contracts: a run that waits on a person executes only after a person
approved it, and only as the proposal they approved.

Three ways round that used to exist: `finrail _job <id>` executed an unapproved run,
the console's approve route judged the server process rather than the request, and
approval went by run id alone, so an edited proposal ran as if it had been read."""

import hashlib
import json

import pytest

from finrail.core import caller, dispatch_core, registry, tracker
from finrail.core.errors import ContractViolation, NeedsPerson


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    registry.load(force=True)


@pytest.fixture
def person(monkeypatch):
    """This process approves as a person would at the operator's terminal."""
    monkeypatch.setattr(caller, "approval_refusal", lambda: "")


def _release(text="# Meridian Components\n\nSynthetic memo."):
    proposal = {"memo_id": "demo-memo", "title": "Meridian Components",
                "full_text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    return tracker.save("finance.memo.release", "release demo-memo", proposal,
                        status=tracker.PENDING, kind="planned")


def _edit_proposal(rid, **changes):
    """What any process running as the install's user can do to a run file."""
    path = tracker.runs() / f"{rid}.json"
    rec = json.loads(path.read_text())
    rec["proposal"].update(changes)
    path.write_text(json.dumps(rec))


def _released():
    from finrail.extensions.finance import _root
    return (_root() / "released" / "demo-memo.md")


def test_worker_refuses_a_run_nobody_approved():
    rec = _release()
    with pytest.raises(NeedsPerson):
        dispatch_core.run_job(rec["id"])
    assert tracker.load(rec["id"])["status"] == tracker.PENDING
    assert not tracker.load(rec["id"]).get("worker_claimed")
    assert not _released().exists()


def test_proposal_edited_before_approval_is_refused(person):
    rec = _release()
    _edit_proposal(rec["id"], full_text="# Meridian Components\n\n24.5% EBITDA margin.")
    with pytest.raises(ContractViolation, match="changed after it was proposed"):
        dispatch_core.approve(rec["id"])
    assert tracker.load(rec["id"])["status"] == tracker.PENDING
    assert not _released().exists()


def test_approval_holds_only_for_the_reviewed_hash(person):
    rec = _release()
    reviewed = tracker.proposal_digest(rec["proposal"])[:12]
    # Proposal and its stored hash both rewritten after the person read it.
    _edit_proposal(rec["id"], title="Rewritten")
    path = tracker.runs() / f"{rec['id']}.json"
    stored = json.loads(path.read_text())
    stored["proposal_sha256"] = tracker.proposal_digest(stored["proposal"])
    path.write_text(json.dumps(stored))
    with pytest.raises(ContractViolation, match="not the proposal you reviewed"):
        dispatch_core.approve(f"{rec['id']}@{reviewed}")
    assert not _released().exists()


def test_approval_with_the_reviewed_hash_releases(person):
    rec = _release()
    ref = f"{rec['id']}@{tracker.proposal_digest(rec['proposal'])[:12]}"
    done = dispatch_core.approve(ref)
    assert done["status"] == tracker.DONE
    assert tracker.load(rec["id"])["approved_sha256"] == tracker.proposal_digest(rec["proposal"])
    assert _released().read_text() == rec["proposal"]["full_text"]


def test_short_hash_prefix_is_refused(person):
    rec = _release()
    with pytest.raises(ContractViolation, match="not the proposal you reviewed"):
        dispatch_core.approve(f"{rec['id']}@{tracker.proposal_digest(rec['proposal'])[:4]}")


def test_worker_refuses_a_proposal_edited_after_approval():
    rec = _release()
    tracker.update(rec["id"], verdict="approved",
                   approved_sha256=tracker.proposal_digest(rec["proposal"]))
    _edit_proposal(rec["id"], title="Rewritten after approval")
    with pytest.raises(ContractViolation, match="changed after it was approved"):
        dispatch_core.run_job(rec["id"])
    assert not _released().exists()


def test_memo_release_recomputes_its_hash():
    from finrail.extensions.finance import MemoRelease
    bad = {"memo_id": "demo-memo", "title": "t", "full_text": "edited",
           "sha256": hashlib.sha256(b"original").hexdigest()}
    with pytest.raises(ContractViolation, match="does not match the hash"):
        MemoRelease().execute(bad)
    assert not _released().exists()
