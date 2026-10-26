"""ATOMIC JSON — a whole-file store that several agents can write at once.

Two failures, and they are different. Both were reproduced on this box with six
concurrent writers against one `settings.json`:

  TORN READ    `write_text` truncates the file to zero and then writes. A reader in that
               window sees an empty file, `json.loads` raises, and finrail stops with
               "settings.json is not valid JSON" — a hard failure, not a degraded one.
               Fixed by writing a temp file in the same directory and `os.replace`,
               which is atomic within a filesystem: a reader sees the old file or the
               new one, never half of either.

  LOST UPDATE  read, modify, write is three steps. Two writers interleave and one
               update disappears with no error anywhere. Fixed by holding an exclusive
               lock across all three, so the read that a write is based on cannot go
               stale before the write lands.

This mattered little while one agent had one state directory to itself. It is a
prerequisite for several agents sharing one, which is what `settings.json`, `store/`
and the cron record now are.

The lock is a SIDECAR file, never the store itself: locking the file you are about to
replace is pointless, because `os.replace` swaps the inode out from under the lock and
the next writer locks a file nobody else can see.
"""
import contextlib
import json
import os
import pathlib
import tempfile
import time

try:
    import fcntl
except ImportError:                                              # pragma: no cover
    fcntl = None                     # Windows: no locking, still atomic writes


def write_text(path, text: str) -> pathlib.Path:
    """Replace a text file in one step. A reader never sees a partial write.

    The same primitive `write_json` uses, for the files that are not JSON and are read
    just as concurrently. An agent's operators file is the case that asked for it: the
    websocket client re-reads it on EVERY message, deliberately, so that adding someone
    needs no restart — which means a plain `open(...).write()` is a window in which a
    live client reads a truncated allow list and tells the person who was just added
    that they are not authorised.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())    # the rename is atomic; the CONTENT still has to be
        _keep_mode(path, tmp)
        os.replace(tmp, path)        # there after a power cut, not just the name
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise
    return path


def _keep_mode(path: pathlib.Path, tmp: str) -> None:
    """A replaced file keeps its permissions. mkstemp makes 0600, so without this a
    rewrite of a root-owned 0644 agent definition (a deploy recording the switch)
    left a file the agent, running as another user, could no longer read."""
    try:
        os.chmod(tmp, path.stat().st_mode & 0o7777)
    except FileNotFoundError:
        pass


def write_json(path, data, **dumps) -> pathlib.Path:
    """Replace a JSON file in one step. A reader never sees a partial write."""
    return write_text(path, json.dumps(data, **dumps))


@contextlib.contextmanager
def locked(path, timeout: float = None):
    """Hold an exclusive lock for this store, for the whole read-modify-write.

    `timeout` is how many seconds to wait for another holder before giving up with
    `TimeoutError`. None waits as long as it takes, which suits a lock held for one
    write; a lock held across slow work gives one, so a holder that is stuck cannot keep
    everyone else waiting for ever. (A holder that dies frees the lock with its process.)
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:                                            # pragma: no cover
        yield
        return
    lock = path.parent / f".{path.name}.lock"
    with open(lock, "a+") as fh:
        _take(fh, timeout)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _take(fh, timeout) -> None:
    """The lock on an open sidecar: waited for as long as it takes, or `timeout` seconds."""
    if timeout is None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{fh.name} was still held after {timeout:g} seconds")
            time.sleep(0.1)


def update_json(path, change, default=None, **dumps):
    """Read, change, write — with nobody else in between. Returns the new document.

    `change` is called with the current document and returns the new one. It runs INSIDE
    the lock, so it must not itself dispatch, call the model, or do anything that could
    take a lock of its own.
    """
    path = pathlib.Path(path)
    with locked(path):
        current = default() if callable(default) else (default if default is not None else {})
        if path.is_file():
            try:
                current = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                pass                 # a corrupt store is replaced, not propagated
        updated = change(current)
        write_json(path, updated, **dumps)
    return updated
