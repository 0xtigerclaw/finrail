"""RETENTION — a full copy of whatever the system is about to throw away.

Several places here deliberately keep less than they saw. Each one is defensible on its
own: a live progress view must not grow without bound, a lesson file injected into every
prompt must not grow without bound, a dropped proposal is not something the ledger should
keep forever. Together they mean the raw material the learning system is supposed to
distil is thinner than what actually happened, and in two cases (a dropped run, a lesson
pushed out by a newer one) it is gone completely.

This module is the archive those places write to FIRST. It is off by default and does
nothing at all until `SAVE_INTERACTION_DETAIL` is true, because an always-on full-fidelity
copy of every step of every turn is a cost nobody asked for.

WHAT IT IS NOT. It is not a second source of truth and nothing reads it to make a
decision. The trimmed structures stay exactly as they were — same caps, same deletes —
so behaviour with the setting on is byte-identical to behaviour with it off, plus a file.
That is the only way an archive can be added to a running system without changing it.

SHAPE. One append-only JSONL file per day, `<folder>/YYYY-MM-DD.jsonl`, one record per
line:

    {"at": "2026-09-09T11:04:12", "kind": "session.step", "subject": "<session id>",
     "payload": { ...the thing, in full, untruncated... }}

Append-only and line-oriented on purpose: this is written from several processes at once
(a detached job worker, a CLI, a cron tick), and an append of one line under the size of
a pipe buffer is the one write that does not need a lock to stay readable. A rewritten
JSON document would need one, and would lose everything on a torn write.

`read_back()` is the other half of the contract — an archive nothing can read is a
write-only hole. It exists so the lessons can be reconstructed from the detail after the
fact, which is the whole reason the detail is kept.

Nothing here may raise. Preserving a copy must never be able to fail the operation whose
data it is preserving — the same rule `progress.emit` and `tracker._verdict` follow, and
for the same reason.
"""
import datetime
import json
import pathlib

from . import log as _log

_logger = _log.get(__name__)

#: Declared by the memory plugin (see `finrail/bundled/memory/__init__.py`). Read by
#: name here rather than imported, so core does not depend on a plugin.
SAVE = "SAVE_INTERACTION_DETAIL"
FOLDER = "INTERACTION_DETAIL_FOLDER"


def _setting(name):
    """One setting, without ever raising or requiring the registry to be loaded.

    Read straight off finrail's own settings.json, the same bypass `raw_location` uses
    and for the same reason: this is consulted from `progress.emit`, which runs inside
    arbitrary capability code and cannot assume plugins have been loaded — or that
    loading them would be free.
    """
    from .settings import state
    p = state() / "settings.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text()).get(name)
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def enabled() -> bool:
    v = _setting(SAVE)
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def folder():
    """Where the archive goes, or None when it is not configured.

    Deliberately no default. A folder guessed under the state directory would be the one
    thing a state-directory move loses, and this exists precisely so that nothing is
    lost. If the setting is unset the archive is off, whatever `SAVE_INTERACTION_DETAIL`
    says, and `preserve` says so once in the log rather than inventing a location.
    """
    v = _setting(FOLDER)
    if not v:
        return None
    return pathlib.Path(str(v)).expanduser()


def active() -> bool:
    """Both halves configured. This is what call sites should test."""
    return enabled() and folder() is not None


#: kind -> where it goes. A typed layout rather than one flat stream, because the point
#: of keeping this is to READ IT BACK: re-deriving lessons means walking the runs, or the
#: turns of one session, or every extraction pass — and grepping a single interleaved file
#: for that is not reading it back, it is hoping.
#:
#: Anything not named here falls to `events/`, which is the honest place for a record
#: whose shape nobody has decided yet.
#: Keyed on the PREFIX before the first dot, which is what `_stream` looks up — keying
#: on the full kind meant `session.step` matched nothing and fell to `events/`.
STREAMS = {
    "journal": "journal",       # every run row, results untruncated
    "run": "runs",              # every run record, including dropped ones
    "session": "sessions",      # what was said, and what was done while saying it
    "chat": "chat",             # channel messages, verbatim
    "lesson": "lessons",        # every change to what is known
    "extraction": "extractions",  # prompt, raw response, what was written
    "ingested": "ingested",     # what the learning has read: a line per run row or session
}


def _stream(kind: str) -> str:
    return STREAMS.get(str(kind).split(".")[0], "events")


def _file(when: datetime.datetime, kind: str = "", subject: str = "") -> pathlib.Path:
    """Where one record lands.

    Session material is filed under the session, because reconstructing a conversation
    means reading one place rather than filtering a year of everything. Runs are filed
    by id for the same reason. The rest is a dated stream.
    """
    root = folder() / _stream(kind)
    safe = "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in str(subject))[:120]
    if kind.startswith("session.") and safe:
        return root / safe / f"{kind.split('.', 1)[1]}s.jsonl"
    if kind.startswith("run") and safe:
        return root / f"{safe}.jsonl"
    if kind.startswith("extraction") and safe:
        return root / f"{safe}.jsonl"
    return root / f"{when.date().isoformat()}.jsonl"


def preserve(kind: str, subject: str, payload) -> bool:
    """Write one full-fidelity copy. Returns whether it was written.

    Call this BEFORE the trim or the delete, never after — the point is the copy exists
    even if the process dies between the two.
    """
    if not active():
        return False
    try:
        now = datetime.datetime.now()
        path = _file(now, kind, subject)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": now.isoformat(timespec="seconds"), "kind": kind,
               "subject": str(subject or ""), "payload": payload}
        line = json.dumps(row, default=str, ensure_ascii=False) + "\n"
        # Append in one write, opened per call: several processes archive concurrently,
        # and a single small append is atomic where a held handle plus a flush is not.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except Exception as e:                                         # noqa: BLE001
        # Never raise. A failure to archive must not fail the work being archived; but
        # silence would make "archiving" indistinguishable from "configured and broken" —
        # and it was logged at debug, which nobody reads. A warning, and a count the nightly
        # history check reports (`memory/history.py`).
        _logger.warning("could not preserve %s for %s: %s", kind, subject, e)
        _count_failure(f"{kind} {subject}: {e}")
        return False


FAILURES = "history-write-failures.json"


def _count_failure(what: str) -> None:
    """Add one to the failed-write count in the state directory. Never raises."""
    try:
        from .settings import state
        p = state() / FAILURES
        data = json.loads(p.read_text()) if p.is_file() else {}
        data = {"count": int(data.get("count") or 0) + 1, "last": str(what)[:300],
                "at": datetime.datetime.now().isoformat(timespec="seconds")}
        p.write_text(json.dumps(data))
    except Exception:                                              # noqa: BLE001
        pass


def read_back(kind: str = "", subject: str = "", since: str = "") -> list:
    """Everything archived, oldest first — optionally narrowed.

    The reason the archive is worth writing: the detail can be walked again later and the
    lessons re-derived from what actually happened, rather than from what survived the
    caps.
    """
    d = folder()
    if d is None or not d.is_dir():
        return []
    out = []
    # "**": the layout is typed, so a reader that only looked at the top level would
    # silently find nothing and report an empty history rather than a mis-shaped one.
    for path in sorted(d.glob("**/*.jsonl")):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue          # a torn final line never hides the ones before it
            if kind and row.get("kind") != kind:
                continue
            if subject and row.get("subject") != subject:
                continue
            if since and (row.get("at") or "") <= since:
                continue
            out.append(row)
    out.sort(key=lambda r: r.get("at") or "")
    return out
