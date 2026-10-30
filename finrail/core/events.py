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

EVERY LINE IS CHAINED (schema 2). Each event carries `prev`, the SHA-256 of the line
before it as stored (a fixed genesis value for the first), and `hash`, the SHA-256 of its
own canonical JSON (sorted keys, compact separators, UTF-8) with `prev` included. An edit
to a line breaks its hash; a deletion or an insertion breaks the next line's `prev`; the
`<log>.head` sidecar beside each file records the last seq and hash written, so a cut-off
tail is seen too. When the install has a ledger key (`FINRAIL_LEDGER_KEY`, or the secret
`ledger-hmac-key` in the secret store) every line and every head also carries `mac`, an
HMAC-SHA256 of the hash under that key, so a rewrite by someone without the key is
detectable even when they recomputed every hash. `finrail ledger verify` checks all of
it and says which guarantee applied. Lines written before schema 2 have no chain fields;
readers accept them and verify reports them as unchained.

IT RECORDS; IT DOES NOT YET DECIDE. The runs, threads and sessions files are still the
source of truth. `project_thread` and `project_run` rebuild them from the log alone, and
`compare` says where the two disagree; once they agree for long enough, the old files
become projections of this and not the other way round.

Never raises into the work it records: a log that cannot be written is logged as such,
and the run goes on. Owned by core (docs/LAYERS.md): plugins append through `append`.
"""
import contextlib
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re

from . import log

_logger = log.get(__name__)

EPOCH = 1
#: The schema every line written from now on carries. 1 (or absent) is a line written
#: before the chain existed: readable, verified as "unchained, pre-v2".
SCHEMA = 2
#: `prev` of the first line in a file.
GENESIS = "0" * 64
#: Where the install-level key lives: the environment first (how Cloud Run mounts a
#: Secret Manager secret), then the secret store (core/secrets.py).
KEY_ENV = "FINRAIL_LEDGER_KEY"
KEY_SECRET = "ledger-hmac-key"
#: The sidecar beside each log that records the last line written: `<name>.head`.
HEAD_SUFFIX = ".head"
#: The log a run lands in when no conversation made it.
LEDGER = "_ledger"
DIRNAME = "events"

#: The kinds this module writes, for readers that switch on them.
KINDS = ("message", "reply", "command", "run", "run_update", "run_dropped", "approval",
         "step", "model_call", "model_round", "turn", "delivered", "question", "answer")


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


def _tail(fh) -> tuple:
    """(seq, raw bytes) of the last complete line that parses, read from the end: a log
    only ever grows, so the tail is the only part an append needs. (0, None) for an
    empty log. The raw bytes are what the next line's `prev` is taken over."""
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    if not size:
        return 0, None
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
                return int(json.loads(ln).get("seq") or 0), ln
            except (ValueError, AttributeError):
                continue
        if back == size:
            return 0, None
        back = min(size, back * 4)


def _last_seq(fh) -> int:
    return _tail(fh)[0]


# ── the chain ───────────────────────────────────────────────────────────────
def canonical(ev: dict) -> bytes:
    """One spelling of an event: sorted keys, compact separators, UTF-8. What is hashed
    and what is written, so the bytes on disk are the bytes the hash covers."""
    return json.dumps(ev, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def line_hash(ev: dict) -> str:
    """The hash of an event: its canonical JSON with `prev` in and `hash`/`mac` out."""
    body = {k: v for k, v in ev.items() if k not in ("hash", "mac")}
    return sha256(canonical(body))


def _mac(k: bytes, text: str) -> str:
    return hmac.new(k, text.encode("utf-8"), hashlib.sha256).hexdigest()


def head_mac(k: bytes, seq: int, h: str) -> str:
    return _mac(k, f"{int(seq)}:{h}")


_KEY = {}


def key():
    """The install-level ledger key as bytes, or None when the install has none.

    Looked up once per process (per state directory for the file store): the keychain
    answers through a subprocess, and an event is appended on every step of every run."""
    env = os.environ.get(KEY_ENV, "").strip()
    if env:
        return env.encode("utf-8")
    try:
        from . import secrets
        from .settings import state
        name = secrets.default_name()
        slot = (name, str(state()) if name == "file" else "")
    except Exception:                                            # noqa: BLE001
        return None
    if slot not in _KEY:
        try:
            _KEY[slot] = (secrets.get(KEY_SECRET).encode("utf-8")
                          if secrets.exists(KEY_SECRET) else None)
        except Exception:                                        # noqa: BLE001
            _KEY[slot] = None
    return _KEY[slot]


def forget_key() -> None:
    """Drop the cached key: for a process that has just stored one."""
    _KEY.clear()


def chain(ev: dict, prev_raw, k=None) -> dict:
    """`ev` with its chain fields filled: schema, prev (over the line before, or
    genesis), hash, and mac when a key is given."""
    out = {kk: v for kk, v in ev.items() if kk not in ("prev", "hash", "mac")}
    out["schema"] = SCHEMA
    out["prev"] = GENESIS if prev_raw is None else sha256(prev_raw)
    out["hash"] = line_hash(out)
    if k:
        out["mac"] = _mac(k, out["hash"])
    return out


def head_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + HEAD_SUFFIX)


def _write_head(path: pathlib.Path, ev: dict, k) -> None:
    """The last line written, kept beside the log so a cut-off tail is seen. Written in
    one step (core/atomic.py) under the log's own lock."""
    from . import atomic
    head = {"seq": int(ev.get("seq") or 0), "hash": ev["hash"], "at": _now()}
    if k:
        head["mac"] = head_mac(k, head["seq"], head["hash"])
    atomic.write_text(head_path(path), json.dumps(head, sort_keys=True))


def read_head(path: pathlib.Path):
    try:
        return json.loads(head_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _head_disagrees(path: pathlib.Path, tail_seq: int, tail_raw) -> dict | None:
    """What the head sidecar says that the log's tail does not: the log was cut back
    (the head records a later seq) or its last line swapped (same seq, another hash).
    None when they agree, or when there is no head to compare."""
    head = read_head(path)
    if not head:
        return None
    hseq, hhash = int(head.get("seq") or 0), str(head.get("hash") or "")
    tail_hash = ""
    if tail_raw is not None:
        try:
            tail_hash = str(json.loads(tail_raw).get("hash") or "")
        except (ValueError, AttributeError):
            tail_hash = ""
    if hseq > tail_seq or (hseq == tail_seq and tail_hash and hhash and hhash != tail_hash):
        return {"head_seq": hseq, "head_hash": hhash, "tail_seq": tail_seq,
                "tail_hash": tail_hash}
    return None


def append_chained(path: pathlib.Path, build) -> int:
    """Append one chained line to any log and return its seq, or raise.

    `build(seq)` returns the event's own fields; the chain fields are added here. Used
    for the event logs and for a run's history (core/tracker.py), which chain the same
    way and verify with the same walk.

    Before the line goes on, the head sidecar is held against the tail. When they
    disagree the log was cut back or its last line swapped since the last append, and
    rewriting the head now would sign over that. So the evidence goes into the line
    itself (`found_break`, hashed and signed with the rest) where verify reports it,
    and the append goes ahead: a service does not stop writing its audit trail because
    someone damaged it."""
    k = key()
    with _locked(path) as fh:
        seq, prev_raw = _tail(fh)
        found = _head_disagrees(path, seq, prev_raw)
        if found:
            _logger.error("%s: the head records seq %s but the log ends at seq %s; "
                          "recorded in the next line", path.name, found["head_seq"], seq)
        seq += 1
        # A write cut off by a crash leaves no newline; start on a fresh line, or
        # this event is glued onto the torn one and lost with it.
        fh.seek(0, os.SEEK_END)
        if fh.tell():
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                fh.seek(0, os.SEEK_END)
                fh.write(b"\n")
        fields = {**build(seq), "seq": seq}
        if found:
            fields["found_break"] = found
        ev = chain(fields, prev_raw, k)
        fh.seek(0, os.SEEK_END)
        fh.write(canonical(ev) + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
        _write_head(path, ev, k)
    return seq


def verify_file(path: pathlib.Path) -> dict:
    """Walk one log and report the first break, with its line number.

    Returns {"file", "lines", "chained", "unchained", "torn", "ok", "break"}:
    `chained` and `unchained` count the schema 2 and the pre-v2 lines, `torn` lists the
    lines that do not parse (a write cut off by a crash: the chain steps over them, as
    the appender did, and they are said rather than counted as damage), and `break` is
    {"line": n, "reason": ...} or None. With a key, a line or head without an HMAC, or
    with a wrong one, is a break."""
    k = key()
    path = pathlib.Path(path)
    out = {"file": str(path), "lines": 0, "chained": 0, "unchained": 0, "torn": [],
           "ok": True, "break": None, "last_seq": 0}

    def broken(n, why):
        out["ok"], out["break"] = False, {"line": n, "reason": why}
        return out

    try:
        raw_lines = path.read_bytes().split(b"\n")
    except OSError as e:
        return broken(0, f"cannot be read: {e}")
    prev_raw, last_seq, last_hash, n = None, 0, "", 0
    for i, raw in enumerate(raw_lines, 1):
        if not raw.strip():
            continue
        n = i
        out["lines"] += 1
        try:
            ev = json.loads(raw)
            if not isinstance(ev, dict):
                raise ValueError("not an object")
        except ValueError:
            out["torn"].append(n)
            continue
        seq = int(ev.get("seq") or 0)
        if "hash" not in ev:
            # Pre-v2: nothing to check, and its seq is what the next line counts from.
            out["unchained"] += 1
            prev_raw, last_seq, last_hash = raw, seq or last_seq, ""
            continue
        if last_seq and seq != last_seq + 1:
            return broken(n, f"seq {seq} follows seq {last_seq}: "
                             f"{'a line is missing' if seq > last_seq + 1 else 'out of order'}")
        if not last_seq and prev_raw is None and seq != 1:
            return broken(n, f"the first line has seq {seq}, not 1: the start is missing")
        want = GENESIS if prev_raw is None else sha256(prev_raw)
        if ev.get("prev") != want:
            return broken(n, "prev does not match the line before it (that line was "
                             "edited, replaced or removed)")
        if ev.get("hash") != line_hash(ev):
            return broken(n, "hash does not match the line's content (the line was edited)")
        if k:
            if not ev.get("mac"):
                return broken(n, "no HMAC on a line, and this install has a ledger key")
            if not hmac.compare_digest(str(ev["mac"]), _mac(k, ev["hash"])):
                return broken(n, "HMAC does not match: rewritten without the ledger key")
        out["chained"] += 1
        prev_raw, last_seq, last_hash = raw, seq, ev["hash"]
        found = ev.get("found_break")
        if isinstance(found, dict):
            return broken(n, f"truncated before this line: when it was appended the head "
                             f"recorded seq {found.get('head_seq')}, the file ended at seq "
                             f"{found.get('tail_seq')}")
    out["last_seq"] = last_seq
    head = read_head(path)
    # A head is checked whenever there are chained lines, and also over a log with none
    # left: a head that records a seq over an empty file is a log cut back to nothing.
    if head and (out["chained"] or int(head.get("seq") or 0) > 0):
        hseq, hhash = int(head.get("seq") or 0), str(head.get("hash") or "")
        if k:
            if not head.get("mac") or not hmac.compare_digest(
                    str(head["mac"]), head_mac(k, hseq, hhash)):
                return broken(n + 1, "the head sidecar's HMAC does not match")
        if hseq > last_seq:
            return broken(n + 1, f"truncated: the head records seq {hseq}, the file "
                                 f"ends at seq {last_seq}")
        if hseq == last_seq and hhash != last_hash:
            return broken(n, "the last line is not the one the head records")
    elif k and out["chained"] and head is None:
        return broken(n + 1, "no head sidecar, and this install has a ledger key")
    return out


def guarantee() -> str:
    """Which guarantee this install's ledger gives, in one line, for verify's report."""
    if key():
        return ("hash chain and HMAC: an edit, a deletion or a cut-off tail is seen, and "
                "a rewrite needs the ledger key")
    return ("hash chain only (no ledger key): an edit, a deletion or a cut-off tail is "
            "seen, but someone with disk access could rewrite the chain and its head")


def verify_all() -> list:
    """One report per event log (`verify_file`), every log in the directory."""
    d = directory()
    return [verify_file(p) for p in sorted(d.glob("*.jsonl"))] if d.is_dir() else []


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
        return append_chained(path, lambda seq: {
            "seq": seq, "epoch": EPOCH, "at": _now(), "thread": thread, "kind": kind,
            "run": run or "", "data": _plain(data or {})})
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
