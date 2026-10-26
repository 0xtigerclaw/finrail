"""THE EVENT LOG — one ordered record per conversation. Layer 2 of the gap map.

State used to be four partial records written by four modules: runs by the tracker,
steps by the progress sink, threads by converse, the crash journal by the Node
transport. None could be rebuilt from another, so "what happened in this turn, in
order" had no single answer. This is the single answer.

One append-only file per thread under `<state>/events/`, one JSON object per line:

    {"seq": 12, "epoch": 1, "at": "...", "thread": "...", "kind": "...",
     "run": "...", "data": {...}}

`seq` counts from 1 per thread and never repeats: it is assigned under a lock on the
file, so two processes appending at once cannot share one. `epoch` changes only if a
log is ever rewritten (compaction is a future logged replace, never a hidden edit), so a
reader holding (epoch, seq) knows whether its place still means what it did.

THE RULE, taken from DeepSeek Harness: if the model saw it, it is a record. So the log
holds each message in, each reply out, every run's life (made, waiting, approved by whom,
done or failed), every step, and every model call with what it was shown and the usage it
reported (its system prompt stored once, under a digest, in `blobs/`), each ROUND of a
model call (`model_round`: wall, model and tool seconds, what the model was shown), and
one `turn` per call that sums it up, with the call's own cost. A run made outside any conversation (a terminal, a cron tick) goes to the
`_ledger` log, so every run is somewhere.

IT RECORDS; IT DOES NOT YET DECIDE. The runs, threads and sessions files are still the
source of truth. `project_thread` and `project_run` rebuild them from the log alone, and
`compare` says where the two disagree; once they agree for long enough, the old files
become projections of this and not the other way round.

Never raises into the work it records: a log that cannot be written is logged as such,
and the run goes on. Owned by core (docs/LAYERS.md): plugins append through `append`.
"""
import contextlib
import datetime
import json
import os
import pathlib
import re

from . import log

_logger = log.get(__name__)

EPOCH = 1
#: The log a run lands in when no conversation made it.
LEDGER = "_ledger"
DIRNAME = "events"

#: The kinds this module writes, for readers that switch on them.
KINDS = ("message", "reply", "command", "run", "run_update", "run_dropped", "step",
         "model_call", "model_round", "turn", "delivered", "question", "answer")


def directory() -> pathlib.Path:
    from .settings import state
    return state() / DIRNAME


def _file(thread: str) -> pathlib.Path:
    # A thread id is a channel id, a name a person chose, or `_ledger`; it becomes a file
    # name, so anything that could leave the directory is filtered rather than trusted.
    safe = re.sub(r"[^A-Za-z0-9._@+-]", "_", str(thread or LEDGER))[:120] or LEDGER
    if safe.startswith("."):
        safe = "_" + safe
    return directory() / f"{safe}.jsonl"


@contextlib.contextmanager
def _locked(path: pathlib.Path):
    """An exclusive lock on the log for the length of one append."""
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield fh
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _last_seq(fh) -> int:
    """The seq of the last complete line, read from the end: a log only ever grows, so
    the tail is the only part an append needs."""
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    if not size:
        return 0
    back = min(size, 1 << 16)
    while True:
        fh.seek(size - back)
        tail = fh.read(back)
        lines = [ln for ln in tail.split(b"\n") if ln.strip()]
        # The first line in a partial window may be cut; only trust it when the window
        # reaches the start of the file.
        candidates = lines if back == size else lines[1:]
        for ln in reversed(candidates):
            try:
                return int(json.loads(ln).get("seq") or 0)
            except (ValueError, AttributeError):
                continue
        if back == size:
            return 0
        back = min(size, back * 4)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def _plain(value):
    """What json can write; anything else becomes its text, rather than losing the
    record."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(k): _plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_plain(v) for v in value]
        return str(value)


def append(thread: str, kind: str, data: dict = None, run: str = "") -> int:
    """Append one event and return its seq, or 0 when it could not be written."""
    if not enabled():
        return 0
    thread = str(thread or LEDGER)
    path = _file(thread)
    try:
        with _locked(path) as fh:
            seq = _last_seq(fh) + 1
            # A write cut off by a crash leaves no newline; start on a fresh line, or
            # this event is glued onto the torn one and lost with it.
            fh.seek(0, os.SEEK_END)
            if fh.tell():
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    fh.seek(0, os.SEEK_END)
                    fh.write(b"\n")
            line = json.dumps({"seq": seq, "epoch": EPOCH, "at": _now(), "thread": thread,
                               "kind": kind, "run": run or "",
                               "data": _plain(data or {})}, ensure_ascii=False)
            fh.seek(0, os.SEEK_END)
            fh.write(line.encode("utf-8") + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        return seq
    except Exception:                                            # noqa: BLE001
        _logger.warning("event log: could not append %s to %s", kind, thread,
                        exc_info=True)
        return 0


def _parse(path: pathlib.Path, lines, since: int = 0, kinds=None) -> list:
    """The events in these raw lines, in order. A line that does not parse (a write cut
    off by a crash) is skipped and said once, never guessed at."""
    out, bad = [], 0
    for raw in lines:
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            bad += 1
            continue
        if int(ev.get("seq") or 0) <= since:
            continue
        if kinds and ev.get("kind") not in kinds:
            continue
        out.append(ev)
    if bad:
        _logger.warning("event log %s: %d line(s) did not parse and were skipped",
                        path.name, bad)
    return out


def read(thread: str, since: int = 0, kinds=None) -> list:
    """Every event after `since`, in order."""
    path = _file(thread)
    if not path.is_file():
        return []
    with open(path, "rb") as fh:
        return _parse(path, fh, since, kinds)


def read_from(thread: str, offset: int = 0, kinds=None) -> tuple:
    """The events written after byte `offset`, and the offset to read from next time.

    How a watcher follows a log: `read` parses the whole file to hand back its tail,
    and a poll every few seconds on a log that only grows paid for every event the
    conversation ever had (10 MB parsed for one new line). Reading from where the last
    call stopped costs what is new and nothing else.

    Only complete lines count. A last line with no newline yet is a write in progress,
    so it is left for the next call and the offset stops before it. An offset past the
    end of the file means the log was rewritten under the reader (compaction, a new
    epoch), and it is read from the start again."""
    path = _file(thread)
    if not path.is_file():
        return [], 0
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = offset if 0 <= offset <= size else 0
        fh.seek(start)
        chunk = fh.read()
    end = chunk.rfind(b"\n") + 1
    return _parse(path, chunk[:end].split(b"\n"), kinds=kinds), start + end


# ── what many events share: stored once ─────────────────────────────────────
BLOBS = "blobs"


def put_blob(text: str) -> str:
    """Store a text once under its digest and return the digest, or "" when it could not
    be written (the caller then keeps the text inline, so nothing is lost).

    A system prompt is the same few kilobytes on every call an agent makes: measured on
    the dev agents, 192 model calls carried 19 different prompts, and the copies were 43%
    of every byte the logs held. The event names the digest; the text is here."""
    import hashlib
    body = text or ""
    sha = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:16]
    path = directory() / BLOBS / f"{sha}.txt"
    try:
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{sha}.{os.getpid()}.tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, path)
        return sha
    except Exception:                                            # noqa: BLE001
        _logger.warning("event log: could not store blob %s", sha, exc_info=True)
        return ""


def blob(sha: str) -> str:
    """The text stored under this digest, or "" when there is none."""
    if not re.fullmatch(r"[0-9a-f]{16}", str(sha or "")):
        return ""
    path = directory() / BLOBS / f"{sha}.txt"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


COSTS = "costs"


def call_cost(engine_session: str, total) -> tuple:
    """(this call's cost, the session's running total) from the engine's figure.

    The engine reports `total_cost_usd` for the whole SESSION, not the call: measured
    2026-10-07, a resumed call that cost $0.009 reported $0.090, its own cost plus the
    first call's. Summing those per turn counted every earlier turn again. So the last
    total seen for each engine session is kept, and the call's cost is the difference.
    A total that went DOWN means a new conversation under a reused id: it is all new."""
    try:
        total = float(total)
    except (TypeError, ValueError):
        return None, None
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(engine_session or ""))[:120]
    if not safe:
        return round(total, 6), round(total, 6)
    path = directory() / COSTS / f"{safe}.json"
    before = 0.0
    try:
        before = float(json.loads(path.read_text()).get("total") or 0)
    except (OSError, ValueError, AttributeError):
        before = 0.0
    delta = total - before if total >= before else total
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{safe}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"total": total, "at": _now()}))
        os.replace(tmp, path)
    except OSError:
        _logger.debug("event log: could not keep the cost total for %s", safe)
    return round(delta, 6), round(total, 6)


def threads() -> list:
    d = directory()
    return sorted(p.stem for p in d.glob("*.jsonl")) if d.is_dir() else []


def enabled() -> bool:
    """On unless the setting `events.enabled` says otherwise."""
    try:
        from .registry import settings
        v = settings().get("events.enabled")
    except Exception:                                            # noqa: BLE001
        return True
    return True if v is None else bool(v)


# ── which conversation a run belongs to ─────────────────────────────────────
def thread_of_run(rec: dict) -> str:
    """The conversation a run belongs to: its own `thread` when converse made it, else
    the thread of the run that made it, else the ledger log."""
    if rec.get("thread"):
        return rec["thread"]
    proposal = rec.get("proposal") if isinstance(rec.get("proposal"), dict) else {}
    if proposal.get("thread") and str(rec.get("capability") or "").startswith("converse"):
        return proposal["thread"]
    return LEDGER


# ── projections: the old records, rebuilt from the log alone ────────────────
def project_run(thread: str, rid: str) -> dict:
    """A run as the log tells it: its first record, then every change, in order."""
    rec = {}
    for ev in read(thread):
        if ev.get("run") != rid:
            continue
        if ev["kind"] == "run":
            rec = dict(ev["data"])
        elif ev["kind"] == "run_update":
            rec.update(ev["data"])
        elif ev["kind"] == "run_dropped":
            rec["dropped"] = True
    return rec


def project_thread(thread: str) -> dict:
    """What a conversation looks like from the log alone: its session, its capability,
    the messages it was sent, the replies it gave, and its runs in order."""
    out = {"thread": thread, "session": "", "capability": "", "messages": 0,
           "replies": [], "runs": []}
    for ev in read(thread):
        d = ev.get("data") or {}
        if ev["kind"] == "message":
            out["messages"] += 1
        elif ev["kind"] == "reply":
            out["replies"].append(d.get("text") or "")
            if d.get("session"):
                out["session"] = d["session"]
            if d.get("capability"):
                out["capability"] = d["capability"]
        elif ev["kind"] == "run" and ev.get("run") not in out["runs"]:
            out["runs"].append(ev["run"])
    return out


#: Fields a run record carries that the log compares. `progress` is left out: steps are
#: their own events, and the record keeps only the last 40 of them.
RUN_FIELDS = ("capability", "status", "actor", "on_behalf_of", "called_by", "source",
              "approved_by", "verdict", "errors")


def compare(thread: str, thread_record: dict = None) -> list:
    """Where the log and the files disagree for one conversation, as plain lines. Empty
    means the log alone rebuilds what the files hold.

    `thread_record` is converse's record of the thread, passed in by the caller: core
    does not read a bundled plugin's store (docs/LAYERS.md, rule 1)."""
    from . import tracker
    problems = []
    for rid in project_thread(thread)["runs"]:
        logged = project_run(thread, rid)
        try:
            real = tracker.load(rid)
        except Exception:                                        # noqa: BLE001
            if not logged.get("dropped"):
                problems.append(f"run {rid}: in the log, not in the ledger")
            continue
        for f in RUN_FIELDS:
            if (logged.get(f) or None) != (real.get(f) or None):
                problems.append(f"run {rid}: {f} is {logged.get(f)!r} in the log, "
                                f"{real.get(f)!r} in the ledger")
    if thread != LEDGER and thread_record is not None:
        proj = project_thread(thread)
        rec = thread_record
        if proj["session"] and rec.get("session") and proj["session"] != rec["session"]:
            problems.append(f"thread {thread}: session {proj['session']!r} in the log, "
                            f"{rec.get('session')!r} in the thread record")
    return problems
