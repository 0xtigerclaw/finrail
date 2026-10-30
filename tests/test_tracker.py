"""Run records: what a reader sees while a worker is rewriting one."""

import threading


def test_load_never_sees_a_half_written_record(tmp_path, monkeypatch):
    # A detached worker updates its run while the console reads it. An in-place write
    # empties the file first, so a read in that gap raised on bad JSON and the console
    # answered 404 for a run that existed (CI, test_demo_seed_makes_screen_run_completable).
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    from finrail.core import tracker

    rid = tracker.save("finance.screen.fixture", "torn read", {"company_id": "04123456"})["id"]
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            n += 1
            tracker.update(rid, progress=["step"] * (n % 50))

    worker = threading.Thread(target=writer)
    worker.start()
    try:
        for _ in range(2000):
            assert tracker.load(rid)["id"] == rid
    finally:
        stop.set()
        worker.join()
