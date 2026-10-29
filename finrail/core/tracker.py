"""THE RUN LEDGER — process state owned by code, not by a model.

Every dispatch is persisted here BEFORE anything executes, so "what happened" is a fact
on disk rather than a claim in a transcript.

The state machine is deliberately small and total:

    pending  ──approve──▶ running ──▶ done
        └────────────────────────────▶ failed

There is no state that means "finished, possibly". An earlier version had `auto`, which
read like "done with nothing to show" and hid real failures; it is gone.

One self-referential edge case deserves its own explanation, because it is exactly the
kind of silent inconsistency this module exists to prevent: approving a `settings.set` for
`runs.dir` itself changes where `runs()` resolves WHILE that very run is executing. A
naive "always write to runs() right now" implementation had that run's own final status
update look for itself in the NEW location, fail to find it (it had only ever lived in the
OLD one), raise, and leave the record stuck at `running` forever — invisible to
`finrail runs` from then on, even though the setting change it was approving had, in
fact, already taken effect. (This happened; it is not hypothetical — see
test_runs_dir_relocation_mid_flight_stays_self_consistent.) The fix: a record's directory
is pinned into the record itself at `save()` time (the `_dir` field) and every subsequent
write for that record's lifetime goes back to that SAME directory, regardless of what the
record's own execution changes underneath it. Only the very first `load()` — before
anything is known about a record — needs to search: current `runs()` first, then the
built-in default, which covers moving away from the default and moving back to it. A
relocation from one non-default location directly to another, mid-flight, is not covered —
narrow enough, and rare enough, to accept rather than add real complexity for.

A record can also be FOUND somewhere it does not live. Two installs' state directories
were merged into one on this host, so `runs()` holds copies of records pinned to the
directory they were created in — and reading the copy while writing to the pin makes the
two permanently disagree. The reaper read 39 copies that said `pending`, closed them,
wrote `failed` to the originals, and found the same 39 saying `pending` on its next pass,
for ever. So the pin wins in `_find` as well as in `_write`, and a copy that is not in its
own home is left out of the listings: the install that owns a record is the only one
entitled to write it, and the only one whose view of it is true.
"""
import contextlib
import contextvars
import datetime
import hashlib
import json
import re
import pathlib
import pathlib
import uuid

from .errors import ContractViolation
from .settings import ensure, runs, state

#: The run this thread of execution is currently executing, so a subprocess started deep
#: inside a capability can be recorded against it. Nothing else knows both facts: the
#: dispatcher knows the run and not the pid, and the code that spawns the engine knows the
#: pid and not the run. Without the join, `kill` could reach the python worker and not the
#: engine it had started — a stop reporting success over work that carries on.
_CURRENT = contextvars.ContextVar("finrail_run", default="")


def current() -> str:
    """The run being executed here, or empty outside one."""
    return _CURRENT.get()


@contextlib.contextmanager
def executing(rid: str):
    """Mark this context as executing a run, for anything that spawns a process."""
    token = _CURRENT.set(rid or "")
    try:
        yield
    finally:
        _CURRENT.reset(token)

PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"
STATUSES = (PENDING, RUNNING, DONE, FAILED)
TRANSITIONS = {PENDING: (RUNNING, FAILED), RUNNING: (DONE, FAILED),
               DONE: (), FAILED: ()}
DISCARDABLE = (PENDING, FAILED)      # never DONE (the record) or RUNNING (in flight)

DIR_KEY = "_dir"                     # where this record lives, pinned at creation


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _search_roots() -> list:
    out, seen = [], set()
    for d in (runs(), state() / "runs"):
        if str(d) not in seen:
            seen.add(str(d))
            out.append(d)
    return out


def _home_of(rec: dict, p: pathlib.Path) -> pathlib.Path:
    """Where a record says its own file lives — `p` itself when it says nothing, which is
    every record written before DIR_KEY existed."""
    d = rec.get(DIR_KEY)
    return pathlib.Path(str(d)) / p.name if d else p


def _at_home(rec: dict, p: pathlib.Path) -> bool:
    """Is `p` the record's own file, or a copy of it in somebody else's directory?"""
    home = _home_of(rec, p)
    return home == p or home.resolve() == p.resolve()


def _find(rid: str) -> pathlib.Path:
    """Where a run's record actually is, checked rather than assumed — and, when what is
    found is a copy, where the record itself says it lives.

    Honouring the pin here is what keeps `load` and `_write` talking about the same file.
    A copy whose original is gone is still read where it was found: a stale record that
    can be read beats "no run".
    """
    for d in _search_roots():
        p = d / f"{rid}.json"
        if not p.is_file():
            continue
        try:
            rec = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return p
        home = _home_of(rec, p)
        return p if _at_home(rec, p) or not home.is_file() else home
    return runs() / f"{rid}.json"          # doesn't exist anywhere known; the caller's
                                            # own is_file() check produces "no run"


def artifacts(rid: str, make: bool = True) -> pathlib.Path:
    """Where a run's files go: `runs/<id>/`, beside `runs/<id>.json`.

    Files produced by a run used to land in a shared bucket keyed by whatever the domain
    thought identified them — extracted attachments went to `attachments/<message-sid>/`.
    That put artifacts nowhere near the record explaining why they exist, made two runs
    over the same message overwrite each other, and left a top-level directory that grows
    forever with no way to tell what any of it belongs to.

    A run already has an id and a lifetime. Its files share both: `finrail view <id>`
    explains them, and dropping the run drops them with it.
    """
    d = runs() / str(rid)
    if make:
        d.mkdir(parents=True, exist_ok=True)
    return d


def actor_default() -> str:
    """Who is acting when the door did not say: the agent this process runs as, else the
    OS user that ran it. Every door names somebody, so no row is anonymous."""
    from . import agents as _agents
    if _agents.current():
        return f"agent:{_agents.current()}"
    import getpass
    try:
        return f"os:{getpass.getuser()}"
    except Exception:                                            # noqa: BLE001
        return "os:?"


_DOOR = {}


def _door_caller() -> dict:
    """What the door said about this process's caller, checked once per process."""
    import os as _os
    from . import caller as _caller
    if not _os.environ.get(_caller.ENV):
        return {}
    if "v" not in _DOOR:
        who = _caller.verified()
        claim = _caller.current()
        _DOOR["v"] = ({**{k: who.get(k) for k in ("kind", "actor", "unit")},
                       "verified": True} if who else
                      {"kind": claim.get("kind") or "?", "verified": False})
    return dict(_DOOR["v"])


def _door_actor() -> str:
    d = _door_caller()
    return d.get("actor") or "" if d.get("verified") else ""


def caller() -> str:
    """The capability whose run is making this one, or "" at a door.

    In this process it is the run being executed here; across a process it is the
    parent run the context carries. Recorded so a chain of calls reads from one row:
    which plugin asked, not only which run.
    """
    from . import context as _context
    rid = current()
    if not rid:
        try:
            rid = _context.current().get("parent_run") or ""
        except ContractViolation:
            rid = ""
    if not rid:
        return ""
    try:
        return load(rid).get("capability") or ""
    except Exception:                                            # noqa: BLE001
        return ""


def proposal_digest(proposal) -> str:
    """SHA-256 of a proposal exactly as it would execute: canonical JSON, keys sorted.

    Taken when the run is saved, checked when it is approved and again before a
    worker executes it, so what a person approved is what runs (`dispatch_core`)."""
    return hashlib.sha256(json.dumps(proposal, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def save(capability: str, task, proposal, status: str = PENDING,
         errors: list = None, kind: str = "", source: str = "cli",
         actor: str = "") -> dict:
    """Persist a new run. The proposal must be plain JSON data — a run has to survive
    being handed to a detached worker in another process, and anything that cannot be
    written down cannot be resumed.

    `actor` is who acted at the door: `os:<user>` at a terminal, `cron:<job>` on a
    tick, `agent:<name>` inside an agent, the door's own value when it passes one. It
    is beside `on_behalf_of` (whose request it is) and `agent` (which agent), so a row
    names who asked and who acted, from every door."""
    ensure()
    if status not in STATUSES:
        raise ContractViolation(f"unknown status '{status}'")
    try:
        json.dumps(proposal)
    except TypeError as e:
        raise ContractViolation(
            f"{capability}: proposal is not JSON-serialisable ({e}). A proposal must be "
            f"plain data so a background worker can pick it up.")
    rid = uuid.uuid4().hex[:8]
    from . import agents as _agents
    rec = {"id": rid, "capability": capability, "kind": kind, "task": task,
           "proposal": proposal, "proposal_sha256": proposal_digest(proposal),
           "status": status, "errors": list(errors or []),
           "result": None, "source": source, "created": _now(),
           # Which agent did this. One ledger for the whole install is only readable if
           # every row says whose action it was.
           "agent": _agents.current(),
           "actor": str(actor or "").strip() or _door_actor() or actor_default(),
           "called_by": caller(),
           DIR_KEY: str(runs())}          # pinned once, honoured for this record's life
    # WHO CALLED, as the door saw it (core/caller.py), checked with the door so a copied
    # variable does not pass. A run that did not come by the door has no entry.
    door_said = _door_caller()
    if door_said:
        rec["caller"] = door_said
    # And on whose behalf, when this process was started by a turn: the person, the run
    # that started it, and the request and stage it belongs to (`core/context.py`).
    # Recorded, never raised — an unreadable context is written down as such, and a
    # command that has to act on it refuses for itself.
    from . import context as _context
    try:
        rec.update(_context.for_record())
    except ContractViolation as e:
        rec["context_error"] = str(e)
    # IN THE SAME PROCESS the environment carries nothing (it is never written, so a
    # long-lived harness cannot leak one turn's person into the next), so a run made
    # inside another takes the person from the run that made it, and a door that names
    # the person in the proposal (`user=` on a converse turn) puts them on its own row.
    # Without this the row of a command run from chat named nobody it was for.
    if not rec.get("on_behalf_of"):
        who = proposal.get("user") if isinstance(proposal, dict) else ""
        if not who and current():
            try:
                who = load(current()).get("on_behalf_of") or ""
            except Exception:                                    # noqa: BLE001
                who = ""
        if isinstance(who, str) and who.strip():
            rec["on_behalf_of"] = who.strip()
    # WHICH CONVERSATION it belongs to: its own thread when converse made it, else the
    # thread of the run that made it. Written on the record so every later event of this
    # run lands in the same log (core/events.py).
    thread = _thread_for(capability, proposal, rec)
    if thread:
        rec["thread"] = thread
    _write(rec)
    _event(rec, "run", {k: v for k, v in rec.items() if not k.startswith("_")})
    return rec


def _thread_for(capability: str, proposal, rec: dict) -> str:
    if isinstance(proposal, dict) and proposal.get("thread") and \
            str(capability).startswith("converse"):
        return str(proposal["thread"])
    parent = current() or rec.get("parent_run") or ""
    if parent:
        try:
            return load(parent).get("thread") or ""
        except Exception:                                        # noqa: BLE001
            return ""
    return ""


def _event(rec: dict, kind: str, data: dict) -> None:
    """One line in the run's conversation log. Never raises: the ledger is written
    first, and a log that cannot follow is logged by events.append itself."""
    try:
        from . import events
        events.append(events.thread_of_run(rec), kind, data, run=rec.get("id") or "")
    except Exception:                                            # noqa: BLE001
        pass


def _elsewhere(rid: str) -> str:
    """The state directory of an installed service that holds this run, or "".

    The operator typed `finrail approve 6bf35453` in a terminal whose finrail used ~/.finrail
    while every service on the host runs with FINRAIL_STATE=/srv/finrail/state: "no
    run", three times, and an hour lost (2026-09-21). The services' units name their state
    directory; read them and say where the run is.
    """
    import os as _os
    import pathlib as _pathlib
    here = str(runs().parent.resolve())
    dirs = []
    try:
        import plistlib
        from .services import agents_dir
        for plist in sorted(agents_dir().glob("com.finrail.*.plist")):
            env = (plistlib.loads(plist.read_bytes()).get("EnvironmentVariables") or {})
            if env.get("FINRAIL_STATE"):
                dirs.append(env["FINRAIL_STATE"])
    except Exception:                                              # noqa: BLE001
        pass
    for unit in sorted(_pathlib.Path("/etc/systemd/system").glob("finrail-*.service")):
        try:
            for m in re.finditer(r"FINRAIL_STATE=(\S+)", unit.read_text()):
                dirs.append(m.group(1).strip('"'))
        except OSError:
            continue
    for d in dict.fromkeys(_os.path.expanduser(x) for x in dirs):
        try:
            if str(_pathlib.Path(d).resolve()) == here:
                continue
        except OSError:
            continue
        if (_pathlib.Path(d) / "runs" / f"{rid}.json").is_file():
            return d
    return ""


def load(rid: str) -> dict:
    p = _find(rid)
    if not p.is_file():
        other = _elsewhere(rid)
        if other:
            raise ContractViolation(
                f"no run '{rid}' here, but the services' state has it: "
                f"FINRAIL_STATE={other} finrail ... (this shell uses {runs().parent})")
        raise ContractViolation(f"no run '{rid}' (try: finrail runs)")
    return json.loads(p.read_text())


def update(rid: str, **kw) -> dict:
    rec = load(rid)
    new = kw.get("status")
    if new and new != rec["status"] and new not in TRANSITIONS[rec["status"]]:
        raise ContractViolation(
            f"run {rid}: illegal transition {rec['status']} -> {new}")
    rec.update(kw)
    rec["updated"] = _now()
    _write(rec)
    # Every change but the live step list, which the log carries as its own events.
    changed = {k: v for k, v in kw.items() if k != "progress"}
    if changed:
        _event(rec, "run_update", changed)
    if new in (DONE, FAILED):
        # A run that has reached its end will not change again, so this is the one
        # moment its whole story is true and complete. Kept because re-deriving a lesson
        # from history means reading the RESULT, not the 600 characters of it that the
        # journal row carries.
        _preserve(rec, new)
    return rec


def stale(rec: dict, seconds: int = 900) -> bool:
    """A `running` run whose worker has been silent too long — probably killed.

    Silence is measured from `updated`, which is the last time anything was heard from
    the worker. It used to be measured from `started`, which is a run's AGE — so a run
    reporting every few seconds went stale at fifteen minutes exactly like a dead one,
    and every long job on this box was declared silent while talking. `started`, then
    `created`, are fallbacks for a record that has not been through `update` yet.
    """
    if rec.get("status") != RUNNING:
        return False
    heard = rec.get("updated") or rec.get("started") or rec.get("created")
    try:
        return (datetime.datetime.now()
                - datetime.datetime.fromisoformat(heard)).total_seconds() > seconds
    except (TypeError, ValueError):
        return False


def _path_of(rec: dict) -> pathlib.Path:
    """The file backing an already-loaded record — its pinned directory when known,
    wherever `_find` actually located it otherwise (older records predate DIR_KEY)."""
    d = rec.get(DIR_KEY)
    return pathlib.Path(d) / f"{rec['id']}.json" if d else _find(rec["id"])


def drop(rid: str, verdict: str = "dropped", reason: str = "", by: str = "") -> dict:
    """Discard a proposal or a failed attempt. Refuses anything executed (the ledger keeps
    what happened) and anything running (its worker is still writing to it).

    `verdict` is what the journal is told. "dropped" is a person saying no to the
    proposal, the clearest judgement learning gets. "cleared" is housekeeping, as
    `drop_pending` says it: a replay discarding what its turn left waiting judged
    nothing, and must not teach that the proposal was wrong.

    `reason` is what the person said was wrong with it, and `by` who said it (the door's
    actor, or whoever this process acts as, when not given). Both ride on the verdict
    the drop already emits, as `drop_reason` and `dropped_by`, and on the
    `run_dropped` event, so a subscriber can keep the reason without core knowing who
    reads it. A housekeeping clear carries neither."""
    if verdict not in ("dropped", "cleared"):
        raise ContractViolation(f"drop: verdict {verdict!r}; one of dropped, cleared")
    rec = load(rid)
    if rec["status"] == DONE:
        raise ContractViolation(
            f"run {rid} already executed — it stays in the ledger")
    if rec["status"] == RUNNING:
        raise ContractViolation(
            f"run {rid} is still running — wait, or: finrail kill {rid}")
    reason = str(reason or "").strip() if verdict == "dropped" else ""
    said = {}
    if reason:
        said = {"drop_reason": reason,
                "dropped_by": str(by or "").strip() or _door_actor() or actor_default()}
    _verdict(rec, verdict, said)
    _preserve({**rec, **said}, verdict)
    _path_of(rec).unlink(missing_ok=True)
    _event(rec, "run_dropped", {"status": rec.get("status"),
                                **({"reason": said["drop_reason"],
                                    "by": said["dropped_by"]} if said else {})})
    return {**rec, **said}


def _verdict(rec: dict, verdict: str, said: dict = None) -> None:
    """Record a human judgement about a run, before the record stops existing.

    Dropping a proposal is the clearest signal this system ever gets — someone looked at a
    proposed action and said no — and it taught nothing, because `drop` deletes the file
    and `after_run` only fires on execution. The run vanished along with the verdict.

    Emitted through the same hook as a completed run, so the learning plugin needs no new
    reader and anything else subscribing sees rejections too.
    """
    try:
        from . import registry
        registry.hooks().emit("after_run", {**rec, "status": verdict,
                                            "verdict": verdict, **(said or {})})
    except Exception:                                            # noqa: BLE001
        pass                     # never let bookkeeping block the drop itself


def _preserve(rec: dict, verdict: str) -> None:
    """Keep the whole record before the file stops existing.

    `_verdict` already tells the learning plugin a decision was made, but what it hands on
    is a journal row: proposal and result both squeezed through `_small` at 600
    characters. For an executed run that loses nothing, because the record on disk still
    holds the full versions. For a DROPPED one the record is deleted a line later, and
    those 600 characters become the only account of a proposal a human looked at and
    refused — which is the single highest-signal thing this system ever observes.
    """
    try:
        from . import retention
        retention.preserve(f"run.{verdict}", rec.get("id") or "", rec)
    except Exception:                                            # noqa: BLE001
        pass                     # never let bookkeeping block the drop itself


def drop_pending() -> list:
    """Discard un-acted proposals and failed attempts.

    Deliberately NOT `status != done`: that would also delete a `running` record while its
    detached worker is still writing to it — the job would keep going with nowhere to
    report, which is worse than leaving it alone.
    """
    gone = []
    for rec in recent(1000):
        if rec["status"] in DISCARDABLE:
            # Marked apart from a considered drop: clearing the deck is housekeeping, and
            # weighing it as a rejection of each proposal would teach the wrong thing.
            _verdict(rec, "cleared")
            _preserve(rec, "cleared")
            _path_of(rec).unlink(missing_ok=True)
            gone.append(rec)
    return gone


def recent(n: int | None = 25) -> list:
    """The newest `n` records by mtime, or every one of them when `n` is None.

    `None` is for the callers that must not let a window decide what exists — the reaper
    looks for records stuck at `running`, and those are by definition the oldest files in
    the directory, the first a truncated window drops.
    """
    ensure()
    files = sorted(runs().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files:
        if n is not None and len(out) >= n:
            break
        try:
            rec = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if not _at_home(rec, p):
            # Another install's record, copied into this directory. Its own install is
            # what advances it; here it can only ever be a frozen copy — and `_path_of`
            # would aim any write, or any unlink, at their file rather than this one.
            continue
        out.append(rec)
    return out


def awaiting(rec: dict) -> bool:
    """Is this run waiting for a HUMAN DECISION, as opposed to waiting for a worker?

    Both read `pending` in the ledger, and telling them apart matters more than the word
    does: `approve` is recorded as a verdict BEFORE the job is handed to a background
    worker, and the worker does not set `running` until it starts. In that window a run
    that has already been approved still says `pending`, and every display invited the
    operator to approve it again. Two people were misled by exactly that today.
    """
    return (rec.get("status") == PENDING
            and not rec.get("verdict")
            and not rec.get("worker_pid"))


def reap(seconds: int = 900) -> list:
    """Close runs that say `running` while nothing is running them.

    A killed process cannot write its own ending. `/stop` signals the process doing the
    work, so the record it was updating stays `running` for ever — and so does anything
    it was in the middle of. Three such runs were left by an afternoon of testing stop,
    one of them a day old, all of them showing in `finrail status` as work in flight.

    Two conditions, both required, and NEITHER is sufficient. A pid that is gone is not
    enough on its own: a run that has only just been created has not had time to record
    one, so a run must ALSO be silent past `stale`. And silence is not enough on its own
    either, which this once got wrong in the costliest direction — with no worker_pid
    there is nothing to ask whether the process is alive, and a foreground run never has
    one, so every foreground run was closed on the timer with no liveness test at all.
    Run f23390df was marked failed at sixteen minutes, worked on for another nine, and
    could not record what it found because `failed` is terminal.

    So: no process recorded means UNKNOWN, and unknown is not dead. A foreground run now
    records the process executing it (`process_pid`, set by `dispatch_core._execute`), so
    that case is only the runs written before it did.

    A PENDING run is closed too, when a worker was spawned for it (`worker_pid`) and that
    worker is gone without ever marking it running: a background job that died before it
    started. A pending run with no worker is waiting for a PERSON, and is never touched.

    Every record is scanned, not a window of the newest. The runs this exists to find are
    the oldest files in the directory, so a window excludes them first: one had said
    `running` for six days from 724th place of 937, with the reaper reading 500.
    """
    closed = []
    for rec in recent(None):
        status = rec.get("status")
        if status == RUNNING and stale(rec, seconds):
            pid, why = holder_pid(rec), ("ended without recording an outcome — its process "
                                        "is gone (killed, or the host restarted)")
        elif status == PENDING and rec.get("worker_pid") and _silent(rec, seconds):
            pid, why = rec.get("worker_pid"), ("never started — the worker spawned for it "
                                               "is gone")
        else:
            continue
        if not pid or pid_alive(pid) is not False:
            continue                         # unknown, or still there: leave it alone
        # `ended_by` says it as a fact a reader can test, where the words above are for
        # a person: the chat transport tells the person their turn was cut off by the
        # restart from this field, whichever reaper closed the run (review, 6 Oct).
        closed.append(update(rec["id"], status=FAILED, ended_by="process_gone", errors=[why]))
        _unask(rec["id"])
    return closed


def reap_thread(thread: str) -> list:
    """Close, NOW, the runs in one conversation that say `running` while their process
    is gone. For a caller that already knows the turn died: a transport recovering after
    it was killed, whose crash took the turn's processes with it.

    `reap` waits for fifteen minutes of silence, because silence is its only evidence
    beside the pid. Here the caller brings the evidence: it watched the thread with a
    grace period and found nothing alive. So the silence test is dropped and the pid
    test is kept: a run with no recorded process is unknown, never dead, and a run whose
    process is still there is left alone. Measured 2026-10-05: a hard kill of a dev agent
    left both runs of the turn `running` for good, on an install with no cron to reap
    them, and the person was told only that the turn did not complete."""
    thread = str(thread or "")
    if not thread:
        return []
    closed = []
    for rec in recent(None):
        if rec.get("status") != RUNNING or rec.get("thread") != thread:
            continue
        if pid_alive(holder_pid(rec)) is not False:
            continue                         # unknown, or still there: leave it alone
        # `ended_by` is the machine marker, the same one `reap` writes: a transport
        # recovering from its own restart tests that, not the English beside it, to tell
        # this ending from a failure whichever reaper closed the run first.
        closed.append(update(rec["id"], status=FAILED, ended_by="process_gone", errors=[
            "ended without recording an outcome: its process is gone (the agent "
            "restarted while it ran)"]))
        _unask(rec["id"])
    return closed


def _unask(rid: str) -> None:
    """Close the questions a dead run was waiting on (core/questions.py). Its gate died
    with it, so nobody is left to hear an answer, and a question left open is still
    offered to `/answer`. Never raises: the run's own ending is already written."""
    try:
        from . import questions
        questions.expire_run(rid, "the run that asked ended without an outcome")
    except Exception:                                            # noqa: BLE001
        pass


def holder_pid(rec: dict):
    """The process that is — or was — doing this run's work: its detached worker, or the
    foreground process executing it. None when neither was recorded."""
    return (rec or {}).get("worker_pid") or (rec or {}).get("process_pid") or None


def pid_alive(pid):
    """True, False, or None when there is nothing to ask. A process that exists but is not
    ours to signal is alive."""
    import os as _os
    if not pid:
        return None
    try:
        _os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, TypeError, ValueError):
        return False


def _silent(rec: dict, seconds: int) -> bool:
    """Nothing heard from this record for `seconds`, whatever its status."""
    heard = rec.get("updated") or rec.get("started") or rec.get("created")
    try:
        return (datetime.datetime.now()
                - datetime.datetime.fromisoformat(heard)).total_seconds() > seconds
    except (TypeError, ValueError):
        return False


def open_runs() -> list:
    return [r for r in recent(200) if r["status"] in (PENDING, RUNNING)]


#: THE RUNS IN FLIGHT, as an index beside the records: one empty file per `running`
#: record, named by its id, kept by `_write`. The agent's door asks "which running run is
#: this caller inside" for every command the agent runs, and answered it by reading the
#: newest thousand records off disk each time (review, 5 Oct). Reading the index costs
#: one small folder and the few records in it.
RUNNING_DIR = ".running"


def _index(rec: dict, d: pathlib.Path) -> None:
    mark = d / RUNNING_DIR / str(rec["id"])
    if rec.get("status") == RUNNING:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.touch()
    else:
        mark.unlink(missing_ok=True)


def running() -> list:
    """Every record that says `running`, newest first: what `recent(None)` filtered on
    status would give, read from the index instead of from every record. A mark whose
    record is gone, has ended, or is another install's copy is dropped on the way.

    A record that cannot be read is SKIPPED, never dropped: `_write` truncates the file
    before it writes, the worker rewrites a turn's record at every step, and the door
    reads this index on every command. A read landing in between saw an empty file, and
    dropping the mark then put the turn out of the index until its next step, in which
    window the door sorted the turn's commands as nobody's (review, 6 Oct). `recent`
    skipped such a record for that one call, and so does this."""
    ensure()
    d = runs()
    idx = d / RUNNING_DIR
    if not idx.is_dir():
        return []
    out = []
    for mark in idx.iterdir():
        p = d / f"{mark.name}.json"
        try:
            rec = json.loads(p.read_text())
            when = p.stat().st_mtime
        except FileNotFoundError:
            mark.unlink(missing_ok=True)     # the record is gone
            continue
        except (OSError, json.JSONDecodeError):
            continue                         # caught mid-write: this call only
        if rec.get("status") != RUNNING or not _at_home(rec, p):
            mark.unlink(missing_ok=True)
            continue
        out.append((when, rec))
    out.sort(key=lambda t: t[0], reverse=True)
    return [rec for _, rec in out]


def _write(rec: dict) -> None:
    ensure()
    d = pathlib.Path(rec[DIR_KEY]) if DIR_KEY in rec else runs()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rec['id']}.json").write_text(json.dumps(rec, indent=2, default=str))
    _index(rec, d)
