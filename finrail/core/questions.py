"""QUESTIONS — a running engine asks the person, and waits for the answer.

The engine runs one shot (`claude -p`), so a permission prompt had nowhere to go: with
prompts off it became a silent refusal, and before that it hung the turn (2026-09-14).
The engine does wait for a PreToolUse hook, though, as long as the hook's timeout allows
(measured 2026-10-05: a hook that slept 70s was waited for, and the tool then ran). So
the hook is where a question goes up and its answer comes down:

    the gate         ask(thread, run, person, text)  -> writes the question, then waits
    the transport    sees a `question` event in the conversation's log, posts it
    the person       /answer yes | no
    converse         answer(qid, yes, by)            -> the waiting gate returns

Two kinds. A PERMISSION question ("may a coding agent write X?") is answered yes or no. A
CLARIFYING question is the engine's own question tool (AskUserQuestion: "which folder?"),
answered in words or by picking one of its options, with `reply(qid, text, by)`.

One file per question under `<state>/questions/`, and every change is also an event in
the conversation's log (`question`, `answer`), so "what was asked, who answered, what
happened when nobody did" is read from the log like everything else.

A question nobody answers in time is a NO, said as such. Silence never allows anything.
A clarifying question nobody answers closes with no answer at all, said as such: nobody
chose for the agent, and the gate tells it so (core/scope.clarify).

That holds whether or not the gate that asked is still there to say it. The record
carries its own deadline (`due`), and a question past it is closed as expired by whoever
reads it next: `open_in`, or an answer. A run that is stopped, that fails, or that is
found dead has its questions closed at once (`expire_run`), because its gate went with
it. A stop closes them before it signals anything, so the ending is in the log while
the transport is still reading the turn. "Its questions" are the ones filed under it,
and under any run the same process executed: a turn's under its converse run, which
is also where a folded turn's failure finds them; a backgrounded run's under that run
itself (core/dispatch_core.py: `_unask`, `run_job`).
"""
import json
import os
import pathlib
import time
import uuid

from . import atomic, events

DIRNAME = "questions"

#: How long a question waits for the person, by default. Bounded by the hook's own
#: timeout, which `scope.materialise` sets above it.
WAIT_S = 600

#: How long a question stays answerable after its stated time, with the gate still
#: waiting on it. The person is told WAIT_S; this is said nowhere. On 6 Oct the operator's
#: `/answer 1` came 5 seconds after a ten-minute question had closed: the gate had
#: already told the agent "no answer", the answer was refused as "no open question",
#: and the agent stopped. An answer that arrives before the asking side has acted on
#: the silence should count, and the asking side is the gate, so the gate goes on
#: looking for this long before it closes the question. Nothing else changes: the
#: question is closed only under the lock, by the gate (`wait`) or by a reader once the
#: gate's time is over too (`_overdue`), so a late yes still cannot land on a no.
LATE_S = 120

#: How long past the gate's own time (`due` + LATE_S) a question is left to it. The gate
#: is the usual closer and looks once a second; after this, whoever reads the question
#: next closes it, so one whose gate was killed does not stay open.
GRACE_S = 5

#: How long after a question closed unanswered an answer to it is still recognised as
#: one, so the person is told it came too late rather than that nothing was asked.
TOO_LATE_S = 3600

#: A closed question's file older than this is removed when the folder is next read
#: (`open_in`). The log keeps the question and its answer for good; the file is only how
#: the gate waiting on it learns the answer, and no gate waits for days.
PRUNE_S = 3 * 86400

OPEN, ANSWERED, EXPIRED = "open", "answered", "expired"

PERMISSION, CLARIFY = "permission", "clarify"

#: The longest answer kept. A reply is words for the model to read, not a document.
MAX_REPLY = 2000


def directory() -> pathlib.Path:
    from .settings import state
    d = state() / DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(qid: str) -> pathlib.Path:
    safe = "".join(c for c in str(qid) if c.isalnum())[:32]
    return directory() / f"{safe}.json"


def load(qid: str) -> dict:
    try:
        return json.loads(_path(qid).read_text())
    except (OSError, ValueError):
        return {}


def _save(rec: dict) -> None:
    atomic.write_text(_path(rec["id"]), json.dumps(rec, indent=2))


def _lock():
    """One lock for every change of state: answering and expiring both read `open`,
    decide, and write, and without the lock a late `yes` could land on top of a `no`
    that had already closed the question (review, 5 Oct)."""
    return atomic.locked(directory() / "questions")


def _create(thread, run, person, text, kind, wait_s, options=(), multi=False) -> dict:
    """A new question under an id nobody has used. Created exclusively, so a clash of
    ids retries rather than replacing another question (open or closed) in place."""
    from . import agents
    for _ in range(20):
        rec = {"id": uuid.uuid4().hex[:8], "thread": thread or "", "run": run or "",
               "agent": agents.current() or "",
               "person": (person or "").strip().lower(), "text": text, "kind": kind,
               "status": OPEN, "asked": _now(), "wait_s": int(wait_s),
               # When its time is up, as a clock time. Expiry is read from here
               # (`_overdue`), so it does not depend on the asking process living.
               "due": round(time.time() + int(wait_s), 3),
               "pid": os.getpid()}
        if options:
            rec.update(options=[str(o) for o in options], multi=bool(multi))
        try:
            fd = os.open(_path(rec["id"]), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(rec, indent=2))
        return rec
    raise RuntimeError("could not find a free question id")


def _now() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def _overdue(rec: dict) -> bool:
    """Has this question's own time run out, with LATE_S and GRACE_S over?

    Read from the record, not from the process waiting on it. Only that process used to
    close a question, so one whose gate was killed with its turn (a /stop, the engine's
    timeout, a restart) stayed open for good: `/answer yes` said "allowed" about a turn
    that was gone, and the next question in the conversation needed its id."""
    due = rec.get("due")
    if not isinstance(due, (int, float)):
        # Asked before the deadline was written down: worked out from when it was asked.
        import datetime
        wait_s = rec.get("wait_s")
        try:
            due = (datetime.datetime.fromisoformat(rec.get("asked")).timestamp()
                   + int(WAIT_S if wait_s is None else wait_s))
        except (TypeError, ValueError):
            return False                 # no way to tell, and unknown is not expired
    return time.time() > due + LATE_S + GRACE_S


def _expire(rec: dict, why: str = "", unposted: bool = False) -> dict:
    """Close an open question as EXPIRED and say so in the log: a no, or for a
    clarifying question no answer at all. The one writer of EXPIRED. The caller holds
    the lock and has just read `rec` as open. `why` is kept when it was not the clock
    that closed it: the run that asked was stopped, failed, or was found dead.

    `unposted` is the question nobody was ever shown (`post`): it is closed the same
    way and marked so the gate can say what happened, and no `answer` event is written,
    because the log holds no question for it to answer."""
    said = "" if rec.get("kind") == CLARIFY else "no"
    rec.update(status=EXPIRED, answer=said, by="", closed=_now())
    # `kind`, `person` and the wait ride on the answer as well as the question, so a
    # reader that never saw the question, or whose record was swept, can still word
    # the outcome, with the minutes the question really had.
    data = {"id": rec["id"], "answer": said, "by": "", "person": rec.get("person") or "",
            "kind": rec.get("kind") or PERMISSION, "expired": True,
            "wait_s": rec.get("wait_s")}
    if why:
        rec["why"] = data["why"] = why
    if unposted:
        rec["unposted"] = True
    _save(rec)
    if not unposted:
        events.append(rec.get("thread"), "answer", data, run=rec.get("run"))
    return rec


def _lapse(qid: str) -> dict:
    """The record as it stands, closed first if its time has run out. Read again under
    the lock: an answer may have landed since the caller looked."""
    with _lock():
        rec = load(qid)
        if rec.get("status") == OPEN and _overdue(rec):
            _expire(rec)
        return rec


def _prune() -> None:
    """Remove the files of questions closed days ago (`PRUNE_S`).

    Nothing removed a question's file, and `open_in` reads every one of them to find a
    conversation's open question, so a bare `/answer yes` read every question ever
    asked on the install (review, 5 Oct). Pruned here, where the folder is next read,
    and under the lock, so a closing write cannot be lost to the removal. Never an open
    one: an open question days old is past its time, `open_in` closes it when its own
    thread is next read (and the log gets its ending), and its file goes on a later
    read. By the file's own time: a closed question was written when it closed."""
    now = time.time()
    with _lock():
        for p in directory().glob("*.json"):
            try:
                if now - p.stat().st_mtime < PRUNE_S:
                    continue
                rec = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if rec.get("status") == OPEN:
                continue
            try:
                p.unlink()
            except OSError:
                pass


def open_in(thread: str) -> list:
    """Open questions in one conversation, oldest first. One whose time has run out is
    closed on the way and left out: nobody is waiting on it, so it is not offered for
    an answer."""
    _prune()
    out = []
    for p in sorted(directory().glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if rec.get("thread") != thread or rec.get("status") != OPEN:
            continue
        if _overdue(rec) and _lapse(rec.get("id") or p.stem).get("status") != OPEN:
            continue
        out.append(rec)
    return out


def expire_run(run: str, why: str = "") -> list:
    """Close, now, every open question filed under a run. Returns the ones it closed.

    For a caller that knows the run is over: it is being stopped, it failed, or its
    process was found gone. The gate that was waiting goes with it, so an answer would
    reach nobody, and the question would go on being offered until its time ran out.
    Closed the way silence is, with the same `answer` event."""
    run = str(run or "")
    if not run:
        return []                        # asked outside any run: not this caller's
    closed = []
    with _lock():
        for p in directory().glob("*.json"):
            try:
                rec = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if rec.get("run") == run and rec.get("status") == OPEN:
                closed.append(_expire(rec, why))
    return closed


def post(thread: str, run: str, person: str, text: str, kind: str = PERMISSION,
         wait_s: int = WAIT_S, options=(), multi: bool = False) -> dict:
    """Write an open question and its `question` event. Returns the record. `options`
    are a clarifying question's choices, in order; the person may pick one by its number
    or its words, or say something else.

    The event is the only road to the person: the transport posts a question when it
    reads the `question` line in the log, and nothing else does. So when the event could
    not be written (events off, or a log that cannot be appended; `events.append` says
    so with a 0), the question is closed on the spot, marked `unposted`, and `wait`
    returns at its first look. It used to wait the whole ten minutes for an answer to
    a question nobody had seen, then say the person did not answer (review, 5 Oct)."""
    rec = _create(thread, run, person, text, kind, wait_s, options, multi)
    seq = events.append(thread, "question", {"id": rec["id"], "text": text, "kind": kind,
                                             "person": rec["person"],
                                             "options": rec.get("options") or [],
                                             "multi": bool(rec.get("multi")),
                                             "wait_s": rec["wait_s"]}, run=run)
    if not seq:
        with _lock():
            rec = load(rec["id"]) or rec
            if rec.get("status") == OPEN:
                rec = _expire(rec, "the question could not be put to the person",
                              unposted=True)
    return rec


def wait(qid: str, wait_s: int = WAIT_S, poll_s: float = 1.0) -> dict:
    """Block until the question is answered or its time is up. Returns the record; a
    question that ran out of time is closed as EXPIRED, which every caller reads as no
    (a clarifying one: as no answer).

    Its time is `wait_s` and then LATE_S more, unannounced: an answer a few seconds past
    the stated time still reaches the turn that asked (6 Oct). The gate has not acted on
    the silence until it returns, so until then a late answer is not too late."""
    deadline = time.monotonic() + max(0, int(wait_s)) + LATE_S
    while True:
        rec = load(qid)
        if rec.get("status") != OPEN:
            return rec
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_s)
    with _lock():
        rec = load(qid)
        if rec.get("status") != OPEN:      # answered in the last poll's gap
            return rec
        return _expire(rec)


def ask(thread: str, run: str, person: str, text: str, kind: str = PERMISSION,
        wait_s: int = WAIT_S, poll_s: float = 1.0, options=(), multi: bool = False) -> dict:
    """Ask, and wait. {"id", "answer": "yes"|"no" (a clarifying question: the words),
    "by", "status"}."""
    rec = post(thread, run, person, text, kind, wait_s, options, multi)
    return wait(rec["id"], wait_s, poll_s)


def too_late(thread: str, agent: str = "") -> dict:
    """The question in this conversation that most recently closed with no answer, if
    it closed within TOO_LATE_S: what a late `/answer` was most likely meant for. {} if
    there is none. Only the asking agent's own (`agent`, when given)."""
    import datetime
    best, now = {}, time.time()
    for p in directory().glob("*.json"):
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if (rec.get("thread") != thread or rec.get("status") != EXPIRED
                or rec.get("unposted") or (agent and (rec.get("agent") or "") not in
                                           ("", agent))):
            continue
        try:
            closed = datetime.datetime.fromisoformat(rec.get("closed")).timestamp()
        except (TypeError, ValueError):
            continue
        if now - closed <= TOO_LATE_S and closed > best.get("_at", 0):
            best = {**rec, "_at": closed}
    best.pop("_at", None)
    return best


def too_late_note(rec: dict) -> str:
    """What a person is told when their answer came after the question closed: when it
    closed and that the agent went on, in plain words. "There is no open question here"
    read as if nothing had been asked (6 Oct), and the operator took his answer as given."""
    import datetime
    try:
        at = datetime.datetime.fromisoformat(rec.get("closed")).astimezone()
        when = at.strftime("%H:%M %Z").strip()
    except (TypeError, ValueError):
        when = "its time"
    how = (f" ({rec['why']})" if rec.get("why") else "")
    text = " ".join(str(rec.get("text") or "").split())
    if len(text) > 200:
        text = text[:200] + "\u2026"
    return (f"That question had already closed at {when}{how} and the agent went on "
            f"without an answer; ask it again if you still want this: {text}")


def answer(qid: str, yes: bool, by: str) -> dict:
    """Record the person's yes or no to a permission question. Refuses a question that
    is not open, so a late or repeated answer cannot reopen what already happened."""
    return _close(qid, "yes" if yes else "no", by, PERMISSION)


def reply(qid: str, text: str, by: str) -> dict:
    """Record the person's answer to a clarifying question, in their words (an option
    they picked by number is passed here as the option's own words)."""
    from .errors import ContractViolation
    said = pick(load(qid), text)[:MAX_REPLY]
    if not said:
        raise ContractViolation("an answer needs some words")
    return _close(qid, said, by, CLARIFY)


def pick(rec: dict, text: str) -> str:
    """The person's words, with an option picked by its number or its exact words
    turned into that option. `2` is the second option; `1, 3` two of them when the
    question allows several. Anything else is kept as said: the person may answer
    something the options did not offer."""
    import re
    said = " ".join(str(text or "").split())
    options = (rec or {}).get("options") or []
    if not options or not said:
        return said
    nums = re.fullmatch(r"\d+(?:\s*(?:,|and|&|\s)\s*\d+)*", said)
    if nums:
        picked = [int(n) for n in re.findall(r"\d+", said)]
        if all(1 <= n <= len(options) for n in picked) and (
                len(picked) == 1 or (rec or {}).get("multi")):
            return ", ".join(options[n - 1] for n in dict.fromkeys(picked))
    for o in options:
        if said.lower() == o.lower():
            return o
    return said


def _close(qid: str, said: str, by: str, kind: str) -> dict:
    from .errors import ContractViolation
    with _lock():
        rec = load(qid)
        if not rec:
            raise ContractViolation(f"no question {qid}")
        if rec.get("status") == OPEN and _overdue(rec):
            # Its time ran out with no gate left to say so. Closed here, so the refusal
            # below is true and the log has the question's ending.
            _expire(rec)
        if rec.get("status") != OPEN:
            raise ContractViolation(
                f"question {qid} is already {rec.get('status')}"
                + (f" ({rec.get('answer')})" if rec.get("answer") else ""))
        # A yes is never written into a clarifying question, nor words into a question
        # whose only answers are yes and no: each kind is closed by its own call.
        if (rec.get("kind") or PERMISSION) != kind:
            raise ContractViolation(
                f"question {qid} is answered "
                + ("in words" if rec.get("kind") == CLARIFY else "yes or no"))
        rec.update(status=ANSWERED, answer=said, by=(by or "").strip().lower(),
                   closed=_now())
        _save(rec)
        events.append(rec.get("thread"), "answer", {"id": qid, "answer": rec["answer"],
                                                    "kind": kind, "by": rec["by"],
                                                    "person": rec.get("person") or ""},
                      run=rec.get("run"))
    return rec
