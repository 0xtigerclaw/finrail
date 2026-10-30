"""The ledger is tamper-evident: every event log and every run history is a hash chain,
signed when the install has a key, and `finrail ledger verify` names the first line that
does not add up. Run history is append-only: a drop moves the record, never deletes it.
All offline; the file secret store stands in for the keychain."""
import hashlib
import hmac
import json

import pytest

from finrail.core import caller, dispatch_core, entrypoint, events, ledger, registry, secrets, tracker
from finrail.core.errors import ContractViolation


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FINRAIL_SECRET_BACKEND", "file")
    monkeypatch.delenv(events.KEY_ENV, raising=False)
    events.forget_key()
    registry.load(force=True)
    yield
    events.forget_key()


@pytest.fixture
def person(monkeypatch):
    monkeypatch.setattr(caller, "approval_refusal", lambda: "")


def _log(thread="audit", n=5):
    for i in range(n):
        events.append(thread, "message", {"i": i, "text": f"line {i}"})
    return events._file(thread)


def _lines(path):
    return [ln for ln in path.read_bytes().split(b"\n") if ln.strip()]


def _write_lines(path, lines):
    path.write_bytes(b"".join(ln + b"\n" for ln in lines))


def _release(text="# Meridian Components\n\nSynthetic memo."):
    proposal = {"memo_id": "demo-memo", "title": "Meridian Components",
                "full_text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    return tracker.save("finance.memo.release", "release demo-memo", proposal,
                        status=tracker.PENDING, kind="planned")


# ── the chain ───────────────────────────────────────────────────────────────
def test_every_line_carries_the_chain():
    path = _log()
    evs = [json.loads(ln) for ln in _lines(path)]
    assert [e["seq"] for e in evs] == [1, 2, 3, 4, 5]
    assert all(e["schema"] == 2 for e in evs)
    assert evs[0]["prev"] == events.GENESIS
    for before, after in zip(_lines(path), evs[1:]):
        assert after["prev"] == hashlib.sha256(before).hexdigest()
    for e in evs:
        assert e["hash"] == events.line_hash(e)
        assert "mac" not in e                      # no key on this install
    head = events.read_head(path)
    assert head["seq"] == 5 and head["hash"] == evs[-1]["hash"]


def test_unchanged_log_verifies_and_names_the_guarantee():
    _log()
    report = ledger.verify()
    assert report["ok"] and not report["key"]
    assert "hash chain only" in report["guarantee"]
    assert report["chained"] == 5 and report["unchained"] == 0
    assert "chain only" in ledger.render(report)


def test_an_edited_middle_line_is_reported_with_its_line_number():
    path = _log()
    lines = _lines(path)
    ev = json.loads(lines[2])
    ev["data"]["text"] = "line 2, reworded"
    lines[2] = events.canonical(ev)
    _write_lines(path, lines)
    report = events.verify_file(path)
    assert not report["ok"]
    assert report["break"]["line"] == 3
    assert "edited" in report["break"]["reason"]
    assert not ledger.verify()["ok"]


def test_a_deleted_line_is_reported():
    path = _log()
    lines = _lines(path)
    del lines[1]
    _write_lines(path, lines)
    report = events.verify_file(path)
    assert not report["ok"]
    assert report["break"]["line"] == 2
    assert "missing" in report["break"]["reason"]


def test_a_truncated_tail_is_reported():
    path = _log()
    _write_lines(path, _lines(path)[:3])
    report = events.verify_file(path)
    assert not report["ok"]
    assert report["break"]["line"] == 4
    assert "truncated" in report["break"]["reason"]
    assert "seq 5" in report["break"]["reason"]


def test_an_append_after_truncation_keeps_the_evidence():
    """Cutting the log back and letting the service write again must not heal it: the
    next line records what the head said, hashed and signed with the rest."""
    path = _log()
    _write_lines(path, _lines(path)[:3])
    events.append("audit", "message", {"text": "after the cut"})
    report = events.verify_file(path)
    assert not report["ok"]
    assert report["break"]["line"] == 4
    assert "truncated before this line" in report["break"]["reason"]
    assert "seq 5" in report["break"]["reason"] and "seq 3" in report["break"]["reason"]
    last = json.loads(_lines(path)[-1])
    assert last["found_break"]["head_seq"] == 5 and last["found_break"]["tail_seq"] == 3
    # Editing the evidence out breaks the hash like any other edit.
    del last["found_break"]
    _write_lines(path, _lines(path)[:3] + [events.canonical(last)])
    assert "edited" in events.verify_file(path)["break"]["reason"]


def test_a_swapped_last_line_is_seen_at_the_next_append():
    path = _log()
    lines = _lines(path)
    last = json.loads(lines[-1])
    last["data"]["text"] = "swapped"
    last = {k: v for k, v in last.items() if k not in ("hash", "mac")}
    last["hash"] = events.line_hash(last)
    _write_lines(path, lines[:-1] + [events.canonical(last)])
    events.append("audit", "message", {"text": "after the swap"})
    assert "truncated before this line" in events.verify_file(path)["break"]["reason"]


def test_a_log_emptied_to_nothing_is_reported():
    path = _log()
    path.write_bytes(b"")
    report = events.verify_file(path)
    assert not report["ok"]
    assert report["break"]["line"] == 1
    assert "truncated" in report["break"]["reason"] and "seq 5" in report["break"]["reason"]


def test_the_command_exits_non_zero_on_a_break(capsys):
    path = _log()
    assert entrypoint.main(["ledger", "verify"]) == 0
    _write_lines(path, _lines(path)[:2])
    assert entrypoint.main(["ledger", "verify"]) == 1
    out = capsys.readouterr().out
    assert "✗ events/audit.jsonl: line 3: truncated" in out


# ── the key ─────────────────────────────────────────────────────────────────
def _rechain_without_key(path, edit_line, change):
    """What someone with disk access and no key can do: edit a line and recompute
    every hash, prev and the head after it."""
    lines = _lines(path)
    prev = None
    for i, raw in enumerate(lines):
        ev = json.loads(raw)
        if i == edit_line:
            change(ev)
        ev = {k: v for k, v in ev.items() if k not in ("prev", "hash", "mac")}
        ev["prev"] = events.GENESIS if prev is None else hashlib.sha256(prev).hexdigest()
        ev["hash"] = events.line_hash(ev)
        lines[i] = prev = events.canonical(ev)
    _write_lines(path, lines)
    last = json.loads(lines[-1])
    events.head_path(path).write_text(json.dumps({"seq": last["seq"], "hash": last["hash"]}))


def test_hmac_detects_an_edit_that_recomputed_the_hashes():
    secrets.put(events.KEY_SECRET, "install-key-for-the-test")
    events.forget_key()
    path = _log()
    ev = json.loads(_lines(path)[0])
    assert hmac.compare_digest(ev["mac"], hmac.new(b"install-key-for-the-test",
                                                   ev["hash"].encode(),
                                                   hashlib.sha256).hexdigest())
    report = ledger.verify()
    assert report["ok"] and report["key"]
    assert "HMAC" in report["guarantee"]

    def reword(e):
        e["data"]["text"] = "line 2, reworded"
    _rechain_without_key(path, 2, reword)
    report = events.verify_file(path)
    assert not report["ok"]
    assert report["break"]["line"] == 1                 # the rewrite dropped every mac
    assert "no HMAC" in report["break"]["reason"]
    assert not ledger.verify()["ok"]


def test_hmac_fails_on_the_edited_line_when_the_other_macs_are_kept():
    secrets.put(events.KEY_SECRET, "install-key-for-the-test")
    events.forget_key()
    path = _log()
    lines = _lines(path)
    ev = json.loads(lines[2])
    ev["data"]["text"] = "line 2, reworded"
    ev["hash"] = events.line_hash(ev)                     # recomputed, mac left as it was
    lines[2] = events.canonical(ev)
    for i in range(3, len(lines)):                       # and every prev after it
        nxt = json.loads(lines[i])
        nxt["prev"] = hashlib.sha256(lines[i - 1]).hexdigest()
        nxt["hash"] = events.line_hash(nxt)
        lines[i] = events.canonical(nxt)
    _write_lines(path, lines)
    report = events.verify_file(path)
    assert not report["ok"]
    assert report["break"]["line"] == 3
    assert "HMAC does not match" in report["break"]["reason"]


def test_the_key_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(events.KEY_ENV, "from-the-environment")
    path = _log()
    assert "mac" in json.loads(_lines(path)[0])
    assert ledger.verify()["key"]


# ── run history ─────────────────────────────────────────────────────────────
def test_every_update_keeps_the_prior_version():
    rec = tracker.save("finance.screen.fixture", "history", {"company_id": "04123456"})
    assert rec["schema"] == 2
    tracker.update(rec["id"], status=tracker.RUNNING)
    tracker.update(rec["id"], progress=["one"])
    versions = tracker.history(rec["id"])
    assert [v["reason"] for v in versions] == ["update", "update"]
    assert versions[0]["record"]["status"] == tracker.PENDING
    assert versions[1]["record"]["status"] == tracker.RUNNING
    assert tracker.load(rec["id"])["progress"] == ["one"]
    hist = tracker.history_path(rec["id"])
    assert events.verify_file(hist)["ok"]
    assert any(f["file"] == str(hist) for f in ledger.verify()["files"])


def test_drop_moves_the_record_to_history_with_the_verdict():
    rec = tracker.save("finance.screen.fixture", "to drop", {"company_id": "04123456"})
    tracker.drop(rec["id"], reason="not this one", by="os:auditor")
    with pytest.raises(ContractViolation, match="no run"):
        tracker.load(rec["id"])
    versions = tracker.history(rec["id"])
    assert versions[-1]["reason"] == "dropped"
    final = versions[-1]["record"]
    assert final["verdict"] == "dropped"
    assert final["drop_reason"] == "not this one" and final["dropped_by"] == "os:auditor"
    assert final["proposal"] == {"company_id": "04123456"}
    assert events.verify_file(tracker.history_path(rec["id"]))["ok"]
    assert ledger.verify()["ok"]


def test_an_edit_to_history_is_seen():
    rec = tracker.save("finance.screen.fixture", "edit history", {"company_id": "04123456"})
    tracker.update(rec["id"], status=tracker.RUNNING)
    tracker.update(rec["id"], status=tracker.DONE, result={"ok": True})
    hist = tracker.history_path(rec["id"])
    lines = _lines(hist)
    v = json.loads(lines[0])
    v["record"]["proposal"]["company_id"] = "99999999"
    lines[0] = events.canonical(v)
    _write_lines(hist, lines)
    report = events.verify_file(hist)
    assert not report["ok"] and report["break"]["line"] == 1


# ── approval against the chain ──────────────────────────────────────────────
def test_approval_refuses_an_edited_run_file_even_with_its_hash_rewritten(person):
    rec = _release()
    path = tracker.runs() / f"{rec['id']}.json"
    stored = json.loads(path.read_text())
    stored["proposal"]["full_text"] = "# Meridian Components\n\n24.5% EBITDA margin."
    stored["proposal"]["sha256"] = hashlib.sha256(
        stored["proposal"]["full_text"].encode()).hexdigest()
    stored["proposal_sha256"] = tracker.proposal_digest(stored["proposal"])
    path.write_text(json.dumps(stored))
    with pytest.raises(ContractViolation, match="the event log recorded"):
        dispatch_core.approve(rec["id"])
    assert tracker.load(rec["id"])["status"] == tracker.PENDING
    from finrail.extensions.finance import _root
    assert not (_root() / "released" / "demo-memo.md").exists()


def test_approval_is_an_event_in_the_chain(person):
    rec = _release()
    done = dispatch_core.approve(rec["id"])
    assert done["status"] == tracker.DONE
    approvals = [e for e in events.read(events.LEDGER, kinds=("approval",))
                 if e["run"] == rec["id"]]
    assert len(approvals) == 1
    assert approvals[0]["data"]["proposal_sha256"] == tracker.proposal_digest(rec["proposal"])
    assert tracker.chained_approval_digest(rec) == tracker.proposal_digest(rec["proposal"])
    assert ledger.verify()["ok"]


def test_worker_refuses_a_proposal_that_disagrees_with_the_chain():
    rec = _release()
    digest = tracker.proposal_digest(rec["proposal"])
    tracker.update(rec["id"], verdict="approved", approved_sha256=digest)
    tracker.record_approval(rec, digest, "approved")
    path = tracker.runs() / f"{rec['id']}.json"
    stored = json.loads(path.read_text())
    stored["proposal"]["title"] = "Rewritten after approval"
    stored["proposal_sha256"] = stored["approved_sha256"] = \
        tracker.proposal_digest(stored["proposal"])
    path.write_text(json.dumps(stored))
    with pytest.raises(ContractViolation, match="not the one the event log holds"):
        dispatch_core.run_job(rec["id"])


def test_approval_refuses_when_the_run_log_is_broken(person):
    rec = _release()
    path = events._file(events.LEDGER)
    lines = _lines(path)
    ev = json.loads(lines[0])
    ev["data"]["task"] = "something else"
    lines[0] = events.canonical(ev)
    _write_lines(path, lines)
    with pytest.raises(ContractViolation, match="broken at line 1"):
        dispatch_core.approve(rec["id"])


# ── schema 1 ────────────────────────────────────────────────────────────────
def _v1_log(thread="old", n=3):
    path = events._file(thread)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for i in range(1, n + 1):
            fh.write(json.dumps({"seq": i, "epoch": 1, "at": "2026-10-01T00:00:00.000",
                                 "thread": thread, "kind": "message", "run": "",
                                 "data": {"i": i}}) + "\n")
    return path


def test_v1_lines_are_read_and_reported_as_unchained():
    path = _v1_log()
    assert [e["data"]["i"] for e in events.read("old")] == [1, 2, 3]
    report = events.verify_file(path)
    assert report["ok"] and report["unchained"] == 3 and report["chained"] == 0
    assert "3 unchained, pre-v2" in ledger.render(ledger.verify())
    # A new line chains onto the old ones, and from then on the file is checked.
    events.append("old", "message", {"i": 4})
    report = events.verify_file(path)
    assert report["ok"] and report["unchained"] == 3 and report["chained"] == 1
    assert [e["seq"] for e in events.read("old")] == [1, 2, 3, 4]


def test_v1_run_records_still_load():
    rec = tracker.save("finance.screen.fixture", "v1", {"company_id": "04123456"})
    path = tracker.runs() / f"{rec['id']}.json"
    stored = json.loads(path.read_text())
    del stored["schema"]
    path.write_text(json.dumps(stored))
    assert "schema" not in tracker.load(rec["id"])
    tracker.update(rec["id"], status=tracker.RUNNING)
    assert tracker.load(rec["id"])["status"] == tracker.RUNNING


def test_migrate_chains_a_v1_state_dir_once():
    _v1_log("old", 3)
    rec = tracker.save("finance.screen.fixture", "v1", {"company_id": "04123456"})
    path = tracker.runs() / f"{rec['id']}.json"
    stored = json.loads(path.read_text())
    del stored["schema"]
    path.write_text(json.dumps(stored))
    before = ledger.verify()
    assert before["ok"] and before["unchained"] == 3

    done = ledger.migrate()
    assert done["lines"] >= 4 and done["records"] == 1 and not done["signed"]
    after = ledger.verify()
    assert after["ok"] and after["unchained"] == 0
    assert [e["seq"] for e in events.read("old")] == [1, 2, 3]
    assert tracker.load(rec["id"])["schema"] == 2
    backup = events.directory().parent / ledger.BACKUP
    assert (backup / "events" / "old.jsonl").read_text().count('"prev"') == 0
    assert (backup / "MIGRATED.json").is_file()
    with pytest.raises(ContractViolation, match="already migrated"):
        ledger.migrate()
    assert entrypoint.main(["ledger", "verify"]) == 0
