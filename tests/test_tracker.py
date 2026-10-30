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


def test_concurrent_updates_keep_every_change(tmp_path, monkeypatch):
    # `_spawn` records the worker's pid while that worker records its own progress. Each
    # read the record, changed it and wrote it back, so one could erase the other.
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    from finrail.core import tracker

    rid = tracker.save("finance.screen.fixture", "lost update", {"company_id": "04123456"})["id"]

    def writer(name):
        for i in range(100):
            tracker.update(rid, **{f"{name}_{i}": True})

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("spawner", "worker")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    rec = tracker.load(rid)
    missing = [f"{name}_{i}" for name in ("spawner", "worker") for i in range(100)
               if f"{name}_{i}" not in rec]
    assert not missing
