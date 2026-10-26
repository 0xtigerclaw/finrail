"""SESSIONS — the second ledger: conversations that span several runs.

`tracker` records every dispatch; this records the far smaller set of interactions where
the MODEL must remember what came before. Two different things, deliberately separate: a
run is one action with one outcome, a session is a thread of them.

It lives in core rather than in the dispatcher plugin because being conversational is part
of the capability contract (`Plugin.session_kind`), not a private arrangement between two
plugins. It previously sat inside the dispatcher, which meant `full_agent` reached across
into another plugin's internals to use it — and, more to the point, that a capability could
only become session-bearing by knowing where that module happened to live.

Continuation is delegated to the ENGINE. The transcript here is a readable record, and what
the learning plugin reads; it is not the mechanism, and replaying it into a prompt would be
a lossy imitation of one. See `open_or_resume`.
"""
import datetime
import json
import uuid

from .errors import ContractViolation
from .settings import ensure, sessions as sessions_dir


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _file(sid: str):
    return sessions_dir() / f"{sid}.json"


def _agent() -> str:
    """Which agent this session belongs to. Carried from the environment, never derived
    from a message — the same rule `owner` follows, and for the same reason."""
    from . import agents
    return agents.current()


def record(kind: str, title: str = "", engine_session: str = "",
           meta: dict = None, owner: str = "", agent: str = None) -> dict:
    """Open a new session.

    `owner` is WHO this conversation belongs to — an authenticated identity handed
    down from the front door (the harness knows the signed pubkey of whoever spoke),
    never something a router or a model may decide. It is a field of its own rather
    than a key in `meta` because it is filtered on: `meta` is a free-form bag, and a
    filter over a bag fails silently and unfalsifiably when a key is misspelled.

    Empty means UNOWNED, which is what every session predating this field is. Unowned
    is visible to everyone deliberately — hiding records nobody can claim would only
    make them unreachable.
    """
    ensure()
    sid = uuid.uuid4().hex[:8]
    rec = {"id": sid, "kind": kind, "title": title or kind,
           # THE ORDER THIS SESSION OPENED IN, among all of them. `created` is to the
           # second, and two sessions opened in the same second have no order in it; a
           # number a person types (`/switch 2`) must not depend on which uuid sorted
           # first. One more than the highest seq on disk; a tie between two processes
           # opening at once costs a shared number, never a moved one.
           "seq": _next_seq(),
           "engine_session": engine_session, "created": _now(), "updated": _now(),
           "status": "open", "owner": owner or "",
           # A FIELD, not a key in `meta`, for the reason spelled out above `owner`: it
           # is filtered on, and a filter over a free-form bag fails silently and
           # unfalsifiably when a key is misspelled.
           "agent": _agent() if agent is None else agent,
           "turns": [], "meta": dict(meta or {})}
    _write(rec)
    return rec


def _next_seq() -> int:
    top = 0
    for p in sessions_dir().glob("*.json"):
        try:
            top = max(top, int(json.loads(p.read_text()).get("seq") or 0))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return top + 1


def exists(sid: str) -> bool:
    """Whether a session record is there to be continued. Asked before a resume, because
    a thread record can outlive the session it names."""
    return bool(sid) and _file(sid).is_file()


def load(sid: str) -> dict:
    p = _file(sid)
    if not p.is_file():
        raise ContractViolation(f"no session '{sid}' (try: finrail sessions)")
    return json.loads(p.read_text())


def append(sid: str, role: str, text: str, engine_session: str = "", **stamp) -> dict:
    """Add a turn. `stamp` carries what the turn is known to be about when it is written —
    for a channel transcript, `by`: the local agent that said it — kept on the turn and in
    the history, so learning can tell whose conversation a stretch of it was."""
    rec = load(sid)
    turn = {"role": role, "at": _now(), "text": text,
            **{k: v for k, v in stamp.items() if v not in (None, "")}}
    # Kept as it arrives. Nothing trims a turn today, but the history exists so lessons
    # can be re-derived from what was actually said — and that must not depend on the
    # session file surviving, being found, or still meaning the same thing later.
    from . import retention
    retention.preserve("session.turn", sid, {**turn, "agent": rec.get("agent") or "",
                                             "owner": rec.get("owner") or ""})
    rec["turns"].append(turn)
    rec["updated"] = _now()
    if role == "agent" and rec.get("status") == "failed":
        # ONE CRASHED TURN IS NOT THE SESSION. `close()` writes `failed` and the
        # conversation goes on, which is right; but nothing wrote `open` back, so a 155
        # turn session read "failed" in the table for the rest of its life (the operator,
        # 2026-09-21). The turn that answered is the proof it is open.
        rec["status"] = "open"
    if engine_session:
        rec["engine_session"] = engine_session
    _write(rec)
    return rec


def start_turn(sid: str, run: str = "") -> dict:
    """Mark that a turn is IN FLIGHT, before the engine is called.

    A turn was invisible while it ran: the instruction was appended, the engine was
    called — for up to an hour, with tools — and the reply was appended only when it
    finished. For that whole time the session held one turn and nothing else, so a
    session working hard was byte-identical to one abandoned after the instruction. The
    run ledger knew, but the session carried no run id and the run carried no session id
    until the end, so neither could lead you to the other while it mattered.
    """
    rec = load(sid)
    rec["working"] = {"run": run, "since": _now()}
    # THE STAMP: which run, of which command, answered the person's latest turn. The
    # session's own `agent` field says which PROCESS wrote the record and is often empty or
    # wrong (62 coding-agent conversations said the named agent); the run that answered is the
    # fact, and it is what the nightly learning files a conversation's lessons under.
    if run:
        capability = ""
        try:
            from . import tracker
            capability = tracker.load(run).get("capability") or ""
        except Exception:                                          # noqa: BLE001
            pass
        for turn in reversed(rec.get("turns") or []):
            if turn.get("role") == "user":
                turn.update({"run": run, "capability": capability})
                from . import retention
                retention.preserve("session.stamp", sid, {"at": turn.get("at"), "run": run,
                                                          "capability": capability})
                break
    rec["updated"] = _now()
    _write(rec)
    return rec


def end_turn(sid: str, detail: str = "") -> dict:
    """The turn is over — however it ended. Called from a `finally`, because a session
    left marked as working after a crash is a worse lie than one never marked at all."""
    from . import retention
    rec = load(sid)
    was = rec.pop("working", None)
    # `was` stops existing here. Every step of the turn goes with it, and only
    # `detail[:200]` survives in `meta.last_turn`. The steps themselves were archived one
    # by one as they arrived, so what is kept here is the ENVELOPE — run id, when it
    # started, when and how it ended — which is what turns that stream of steps back into
    # a turn. Deliberately without `steps`: they are already in the archive, and a second
    # copy of the last 40 would only be the trimmed ones.
    if was:
        retention.preserve("session.turn", sid,
                           {**{k: v for k, v in was.items() if k != "steps"},
                            "ended": _now(), "detail": detail,
                            "steps_kept": len(was.get("steps") or [])})
    if was and detail:
        rec.setdefault("meta", {})["last_turn"] = {**was, "ended": _now(),
                                                   "detail": detail[:200]}
    rec["updated"] = _now()
    _write(rec)
    return rec


def _log_step(rid: str, sid: str, entry: dict) -> None:
    try:
        from . import events, tracker
        run = rid or tracker.current()
        thread = events.LEDGER
        if run:
            try:
                thread = events.thread_of_run(tracker.load(run))
            except Exception:                                    # noqa: BLE001
                pass
        data = {k: v for k, v in entry.items() if k != "full"}
        if entry.get("full"):
            data["text"] = entry["full"]
        events.append(thread, "step", {**data, "session": sid}, run=run)
    except Exception:                                            # noqa: BLE001
        pass


def step(sid: str, entry: dict) -> None:
    """Record one progress step against the in-flight turn.

    On the `working` block rather than as a turn: these are what the engine is DOING, not
    what it said, and mixing them into the transcript would rewrite the record of the
    conversation with commentary. They vanish when the turn ends and the reply — the
    thing that is actually the answer — takes their place.
    """
    from . import progress, retention
    rec = load(sid)
    live = rec.get("working")
    if not live:
        return
    # Archived BEFORE the trim, and with the untruncated text `progress.emit` passed
    # under `full`. `trim` keeps the last 40 so the live view stays a fixed size, which
    # means the steps of a long turn stop being visible while the turn is still going;
    # and `end_turn` then discards the whole block. Both are right for a live view and
    # wrong for the only record of what the turn actually did.
    retention.preserve("session.step", sid, {"run": live.get("run") or "", **entry})
    # And in the event log, in full: an agent's steps come here rather than to its run's
    # sink, so this is the only place the log can hear them (core/events.py).
    _log_step(live.get("run") or "", sid, entry)
    # `full` and `input` are for the log, not the live view: the session record keeps a
    # window of 40 steps and is rewritten on every one.
    entry = {k: v for k, v in entry.items() if k not in ("full", "input")}
    # NUMBERED, BECAUSE POSITION IS NOT AN IDENTITY IN A LIST THAT IS TRIMMED.
    #
    # A reader following the turn asks for "everything after what I have". While the
    # list only grows, its length answers that; once `trim` starts dropping the head it
    # does not, and the reader's count runs past the end of a list that never gets any
    # longer. Measured: after the 40th step of a turn `steps[after:]` returned [] on
    # every subsequent call, for the rest of the turn — so a long turn went silent at
    # step 40 and every 💬 the agent produced after that reached nobody.
    #
    # `n` counts steps EMITTED, not steps kept, so it keeps meaning what the reader
    # thinks it means after the window slides.
    live["n"] = int(live.get("n") or 0) + 1
    entry = {**entry, "n": live["n"]}
    live["steps"] = progress.trim((live.get("steps") or []) + [entry])
    rec["updated"] = _now()
    _write(rec)


def activity(rec: dict) -> dict:
    """The session's current activity, or {}. Read defensively — most records have none.

    {"name", "pinned", "how", "told"}: which one is in effect, whether a person fixed it,
    what put it there, and which one the engine was last given the instructions for.
    Owned here, beside `working`, because the transcript, converse and `activity.status`
    all render it, and a fact with three renderers belongs to the record, not to one of
    them. What DECIDES it each turn is the activity bundle's."""
    return (rec or {}).get("activity") or {}


def set_activity(sid: str, **fields) -> dict:
    rec = load(sid)
    rec["activity"] = {**activity(rec), **fields}
    rec["updated"] = _now()
    _write(rec)
    return rec


def note_activity(sid: str, name: str, how: str) -> dict:
    """Record on the latest person's turn which activity answered it, and why. Per turn,
    so a session that moved between activities says where it moved."""
    rec = load(sid)
    for turn in reversed(rec.get("turns") or []):
        if turn.get("role") == "user":
            turn["activity"] = {"name": name or "", "how": how or ""}
            # In the history too: a lesson learned from this turn is tagged with its
            # activity, and a rebuild must be able to tag it the same way.
            from . import retention
            retention.preserve("session.activity", sid, {"at": turn.get("at"),
                                                         "name": name or "", "how": how or ""})
            break
    rec["updated"] = _now()
    _write(rec)
    return rec


def activity_line(note: dict) -> str:
    """`activity: <name> (<how>)` — the one rendering of it. No activity is a dash."""
    note = note or {}
    how = note.get("how") or ""
    return f"activity: {note.get('name') or '–'}" + (f" ({how})" if how else "")


def working(rec: dict) -> dict:
    """The in-flight turn, or {}. Read defensively — an old record has no such key."""
    return (rec or {}).get("working") or {}


def is_closed(rec: dict) -> bool:
    """Is this session over for good? The one definition of the question.

    THE LITERAL `closed`, AND ONLY THAT. `close()` also writes `failed` — a turn that
    crashed — and a conversation whose last turn crashed is one to pick up again, not
    one to refuse. The listing's "open" filter and the placement column already tested
    this same literal for the same reason; they ask here now, so the two views and the
    lifecycle cannot drift into three different answers.
    """
    return ((rec or {}).get("status") or "") == "closed"


def close(sid: str, status: str = "closed") -> dict:
    """End the session. A closed session is not working, and it does not reopen.

    Closing used to set `status` and leave any `working` block untouched — and the
    listing prefers `working` over `status` (see `listing`), so six sessions closed by
    hand still rendered `▶ a turn is in flight`. The close reported "closed 1 session(s)"
    and the view disagreed with it, which is the same record contradicting itself twice.

    The MIRROR of that was still here. A message arriving for a closed session went on
    continuing it, and nothing sets `status` back to open after this — line for line,
    `record()` is the only place it is ever written. So the transcript grew under a
    record that said `closed`, and the listing answered `working` mid-turn and `closed`
    between turns: one session, one state, two answers. Three sessions on this host
    were in it, one of them 54 turns past its close. `open_or_resume` is where that
    stops; the note there says why the answer is a fresh session, not a reopened one.
    """
    rec = load(sid)
    if working(rec):
        end_turn(sid, detail=f"session {status}")
        rec = load(sid)
    rec["status"] = status
    rec["updated"] = _now()
    _write(rec)
    return rec


def reap(agent: str = None) -> list:
    """Clear `working` blocks whose run is no longer running. Returns what was cleared.

    `end_turn` runs in a `finally`, which covers an exception but not a kill: nine of
    these were left pointing at runs that had been `failed` for days, and the listing went
    on showing `▶ a turn is in flight` for every one. `tracker.reap()` already does the
    equivalent for RUNS and is already called each cron tick; nothing did it for sessions,
    so the two records drifted apart and only the ledger was right.

    Keyed on the run's own status rather than on age: a legitimately long turn is still
    running, and a clock is a worse test than asking the thing that knows.
    """
    from . import tracker
    cleared = []
    for rec in listing(limit=10_000, agent=agent):
        sid = rec.get("id")
        live = working(rec)
        rid = live.get("run")
        if not sid or not live:
            continue
        if not rid:
            # A working block naming no run cannot be reconciled against anything, and it
            # will never clear itself. Treat it as dead rather than leaving it forever.
            end_turn(sid, detail="abandoned (no run recorded)")
            cleared.append({"session": sid, "run": "", "status": "unknown"})
            continue
        try:
            status = tracker.load(rid).get("status")
        except Exception:                                        # noqa: BLE001
            status = "missing"
        if status == tracker.RUNNING:
            continue
        end_turn(sid, detail=f"reaped — run {rid} is {status}")
        cleared.append({"session": sid, "run": rid, "status": status})
    return cleared


def listing(kind: str = "", limit: int = 25, owner: str = "",
            agent: str = None) -> list:
    """Sessions, newest first. `owner` narrows to that identity's, plus unowned ones.

    The limit is applied AFTER filtering, not before. Slicing first would let one
    person's recent sessions push another's out of their own listing — a filtered
    view that silently omits rows matching the filter is worse than no filter.
    """
    ensure()
    files = sorted(sessions_dir().glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files:
        if len(out) >= limit:
            break
        try:
            rec = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if kind and rec.get("kind") != kind:
            continue
        if not rec.get("id"):
            continue
        if owner and (rec.get("owner") or "") not in ("", owner):
            continue
        # Two empties, two different meanings, and both are deliberate.
        # An empty FILTER is a plain CLI invocation: the operator at the terminal is not
        # one of the agents and should see everything.
        # An empty agent ON THE RECORD is a session written before agents existed. It
        # belongs to nobody, so it is visible to whoever asks — the same rule `owns`
        # applies to an unowned session, and what carries this conversation across the
        # cutover instead of losing its history to a filter.
        if agent and (rec.get("agent") or "") not in ("", agent):
            continue
        # READ DEFENSIVELY. A listing exists to show what is there; a single record
        # missing a field is a reason to show that record poorly, never a reason to
        # raise and show NONE of them. One zero-turn caller session with no `updated`
        # made this raise KeyError, and every session became invisible at once.
        out.append({"id": rec["id"], "kind": rec.get("kind") or "?",
                    "title": rec.get("title") or "",
                    "owner": rec.get("owner") or "",
                    "agent": rec.get("agent") or "",
                    "status": ("working" if working(rec) else rec.get("status") or "?"),
                    "working": working(rec),
                    "turns": len(rec.get("turns") or []),
                    "updated": rec.get("updated")
                    or datetime.datetime.fromtimestamp(
                        p.stat().st_mtime).isoformat(timespec="seconds")})
    return out


def transcript(sid: str) -> str:
    rec = load(sid)
    body = "\n\n".join(
        f"[{t['role']} {t['at']}]\n"
        + (activity_line(t["activity"]) + "\n" if t.get("activity") else "")
        + t["text"] for t in rec["turns"])
    live = working(rec)
    if live:
        # Said at the END, where a reader looking for the answer actually stops.
        body += (f"\n\n[working since {live.get('since', '?')}]"
                 + (f" — run {live['run']}" if live.get("run") else ""))
        for s in (live.get("steps") or [])[-8:]:
            body += f"\n  {s.get('at', '')[11:]} {s.get('kind', '')}: {s.get('text', '')}"
        body += "\n(a turn is in flight; the reply lands here when it returns)"
    return body


def _write(rec: dict) -> None:
    ensure()
    _file(rec["id"]).write_text(json.dumps(rec, indent=2, default=str))


def owns(rec: dict, owner: str = "") -> bool:
    """May this identity use this session?

    Three yeses, and they are all deliberate. An EMPTY `owner` is the operator at the
    CLI, who sees everything — the scoping exists to keep one person's conversations
    out of another's listing, not to keep anything from the machine's owner, who can
    read `<state>/sessions` with `cat` regardless. An UNOWNED session belongs to
    nobody and is reachable by anybody. Otherwise it must match.
    """
    if not owner:
        return True
    held = (rec or {}).get("owner") or ""
    return held in ("", owner)


# ── a conversation that has grown too big or gone quiet ─────────────────────
#: Turns handed to a fresh engine conversation, newest last, and the most of each.
HANDOVER_TURNS = 8
HANDOVER_TURN_CHARS = 1500
HANDOVER_CHARS = 12000


def _setting(name: str, default: int) -> int:
    try:
        from .registry import settings
        v = settings().get(name)
    except Exception:                                              # noqa: BLE001
        return default
    try:
        return default if v is None else int(v)
    except (TypeError, ValueError):
        return default


def _why_fresh(rec: dict, engine: str) -> str:
    """Why this conversation should continue in a fresh engine conversation, or "".

    A CONVERSATION THAT NEVER ENDS IS RE-READ WHOLE ON EVERY MODEL ROUND. One thread ran
    as a single engine conversation for 26 days and 166 turns, 540,000 to 965,000 tokens
    a call; on the dev agents a turn deep in a long conversation cost 7.7 times as much
    per tool call as a first turn and took ten times as long (13 Oct). So past a size, or
    after a quiet spell, the next turn starts fresh and is handed the recent turns."""
    cap = _setting("sessions.max_context_tokens", 150000)
    if cap > 0:
        from . import llm
        size = llm.context_size(engine)
        if size >= cap:
            return f"the conversation had reached {size:,} tokens"
    hours = _setting("sessions.idle_hours", 12)
    if hours > 0:
        try:
            last = datetime.datetime.fromisoformat(str(rec.get("updated") or ""))
        except ValueError:
            last = None
        if last is not None:
            now = datetime.datetime.now(last.tzinfo) if last.tzinfo else datetime.datetime.now()
            quiet = (now - last).total_seconds() / 3600
            if quiet >= hours:
                return f"nobody had spoken in it for {int(quiet)} hours"
    return ""


def _continue_fresh(rec: dict, why: str) -> dict:
    """The same session, in a new engine conversation, with a handover waiting for its
    first turn (`take_handover`). The session's id, owner, title and transcript do not
    change: a person sees one conversation, and only the engine starts again."""
    from . import llm
    engine = llm.new_session_id()
    rec = load(rec["id"])
    rec["handover"] = handover_text(rec, why)
    rec["engine_session"] = engine
    rec["fresh_engine"] = {"at": _now(), "why": why}
    rec["updated"] = _now()
    _write(rec)
    return append(rec["id"], "system",
                  f"continued in a fresh engine conversation: {why}", engine_session=engine)


def handover_text(rec: dict, why: str) -> str:
    """What a fresh engine conversation is told about the one it continues."""
    said = [t for t in (rec.get("turns") or []) if t.get("role") in ("user", "agent", "assistant")]
    picked, total = [], 0
    for t in reversed(said[-HANDOVER_TURNS:]):
        text = str(t.get("text") or "").strip()
        if len(text) > HANDOVER_TURN_CHARS:
            text = text[:HANDOVER_TURN_CHARS].rstrip() + " …"
        line = f"[{'person' if t['role'] == 'user' else 'you'}] {text}"
        if total + len(line) > HANDOVER_CHARS:
            break
        picked.append(line)
        total += len(line)
    picked.reverse()
    head = (f"This conversation continues from an earlier part ({why}), so you start "
            f"without its history. The last {len(picked)} messages are below; the whole "
            f"transcript is `finrail get session id={rec['id']}`. Read it only if you "
            f"need something not shown here.")
    return "<earlier>\n" + head + "\n\n" + "\n\n".join(picked) + "\n</earlier>"


def take_handover(sid: str, prompt: str) -> str:
    """`prompt`, preceded by the handover if this session has one waiting. Once only: the
    handover is removed as it is taken, so only the fresh conversation's first turn
    carries it."""
    try:
        rec = load(sid)
    except ContractViolation:
        return prompt
    text = rec.pop("handover", "")
    if not text:
        return prompt
    _write(rec)
    return f"{text}\n\n{prompt}"


def open_or_resume(kind: str, sid: str = "", title: str = "", meta: dict = None,
                   owner: str = "") -> tuple:
    """(record, engine_session, resume) — the one place that decides new-thread vs continue.

    Every session-bearing capability needs exactly this and needs it identically, and the
    decision has three traps that are easy to get subtly wrong in each re-implementation:

      * a CLOSED session is not resumed at all — see below.
      * `resume` is decided by whether the record already HAS TURNS, not by whether an id
        exists. An id that was stored but never used has no conversation behind it, and
        resuming one the engine has never seen fails.
      * a record written before engine ids existed has turns but NO id. Those turns cannot
        be handed back to the engine, so it honestly opens a fresh thread and says so in
        the transcript rather than pretending the context survived.

    The id is ASSIGNED here rather than read back from engine output, so it exists before
    the first call and cannot be lost if that call fails partway.
    """
    from . import llm
    if sid:
        rec = load(sid)
        if is_closed(rec):
            # A CLOSED SESSION DOES NOT REOPEN, so the message opens a fresh one.
            #
            # `converse.end` tells the operator exactly this before it asks them to
            # approve it — "closing a session ends it for everyone using it; there is
            # no reopen" — and `/new` is already the verb for the other reading: it
            # abandons a conversation WITHOUT closing it, deliberately, so that
            # switching back has something to go back to. If `/end` reopened on the
            # next message the two would be the same command with different labels.
            #
            # What happened instead was neither. The conversation continued AND the
            # record stayed marked `closed`, which is not a lifecycle at all; it is a
            # record disagreeing with the behaviour it records.
            #
            # HERE rather than in either listing, because both of them read this one
            # record: `converse.sessions` and `finrail get sessions` were each half
            # wrong, and fixing the renderer would have fixed one of them. The fresh id
            # travels back in the result, and `converse._run` rebinds the thread to it.
            engine = llm.new_session_id()
            fresh = record(kind, title=title, engine_session=engine, meta=meta,
                           owner=owner)
            # Owned by whoever is speaking now and titled by what they just said — it
            # is a NEW conversation, opened exactly as the branch at the bottom of this
            # function opens one. The system turn is the only thing that differs: it is
            # what stops "why did it forget?" being unanswerable afterwards.
            return (append(fresh["id"], "system",
                           f"session {sid} was closed; this is a fresh conversation"),
                    engine, False)
        # An UNOWNED session is adopted by the first identified caller to continue it.
        # One way only: unowned -> owned. This is what carries the sessions that
        # existed before ownership did into the new world without a migration script,
        # and it can never take a conversation off the person already holding it.
        if owner and not (rec.get("owner") or ""):
            rec["owner"] = owner
            rec["updated"] = _now()
            _write(rec)
        engine = rec.get("engine_session", "")
        if engine and rec.get("turns"):
            why = _why_fresh(rec, engine)
            if why:
                fresh = _continue_fresh(rec, why)
                return fresh, fresh["engine_session"], False
        if engine:
            return rec, engine, bool(rec.get("turns"))
        engine = llm.new_session_id()
        rec = append(sid, "system",
                     "continued in a new engine conversation "
                     "(this session predates engine-side continuation)",
                     engine_session=engine)
        return rec, engine, False
    engine = llm.new_session_id()
    rec = record(kind, title=title, engine_session=engine, meta=meta, owner=owner)
    return rec, engine, False
