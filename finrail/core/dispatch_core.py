"""THE EXECUTION MECHANICS — one place where a side effect can happen.

Routing (plain language → which capability) lives in the dispatcher PLUGIN. What lives
here is the part that must be identical for every capability no matter who wrote it:

  * resolve + type-check the parameters                (schema)
  * build the proposal: plan() for planned, the LLM for judged
  * validate() it — hallucinated ids and invented paths die here
  * persist it BEFORE executing                        (tracker)
  * execute exactly once, recording done or failed     (_execute)
  * background it when the capability says so          (_spawn)

`_execute` is the only function in the system that calls `capability.execute()`. That is
why "did it really happen?" has one answer instead of one per plugin.
"""
import json
import os
import signal
import subprocess
import sys

from . import events, owners, progress, registry, schema, tracker
from .errors import FinrailError, ContractViolation, NeedsPerson, OpFailed
from .requirements import path_env


# ── building a proposal ─────────────────────────────────────────────────────
def propose(name: str, payload: dict, task: str = "") -> tuple:
    """(capability, proposal, errors). No side effects."""
    cap = registry.command(name)
    payload = schema.coerce(cap, dict(payload or {}))
    # Free text belongs to whichever capability DECLARES a place for it, whether it
    # arrived as a positional ("finrail do x \"...\"") or as task=. Normalise before
    # validating, so the strict unknown/missing check applies to both forms identically.
    #
    # This was once restricted to judged capabilities, on the assumption that only they
    # take prose. `chat` disproved it: it is PLANNED (its plan is deterministic — what
    # will be said, in which session) and its whole input is prose. So
    # `finrail do chat "..."` silently dropped the message and asked the engine to
    # answer nothing — while chat's own hint text, and the command `ask` returns for its
    # chat fallback, both used exactly that form. The test is whether the capability
    # accepts the parameter, not which kind it happens to be.
    if task and not payload.get(schema.TASK_PARAM) and schema.accepts(cap, schema.TASK_PARAM):
        payload[schema.TASK_PARAM] = task
    schema.validate_params(cap, payload)

    if cap.kind == "read":
        return cap, payload, []

    if cap.kind == "planned":
        proposal = cap.plan(**payload)
        if proposal is None:
            raise OpFailed(f"{name}: plan() produced nothing to do")
    else:
        proposal = _judge(cap, task or payload.get(schema.TASK_PARAM, ""), payload)

    return cap, proposal, cap.validate(proposal)


def _judge(cap, task: str, payload: dict) -> dict:
    """Ask the model for a proposal — unless the caller already supplied everything.

    A judged capability is judged because turning prose into a proposal needs judgment.
    When the caller states the fields outright there is nothing to judge, and calling a
    model anyway would be slower, costlier and less predictable for no gain.
    """
    from . import llm
    explicit = {k: v for k, v in payload.items()
                if k != schema.TASK_PARAM and v not in (None, "", [], {})}
    required = _required_fields(cap)
    if required and required.issubset(explicit):
        return dict(explicit)

    declared = set(getattr(cap, "takes", ()) or ())
    if not task and not (declared & set(explicit)):
        # Free text is how a judged capability is USUALLY told what to do, and it is
        # required when nothing else says. But a capability that declares `takes` and is
        # given one of those has been told: `music.suggest from=<playlist url>` is a
        # complete request, and demanding a sentence alongside it asks the caller to
        # repeat in prose what they just said in a parameter.
        raise ContractViolation(
            f"{cap.name}: needs a description of what you want"
            + (f", or one of: {', '.join(sorted(declared))}" if declared else "")
            + f" (e.g. finrail do {cap.name} \"...\")")
    ctx = {}
    ctx.update(cap.static_context() or {})
    ctx.update(cap.context(task, {k: v for k, v in payload.items()
                                  if k != schema.TASK_PARAM}) or {})
    try:
        # A capability may build its proposal by looking things up first (Plugin.resolve).
        # None means the ordinary one-shot call, which is what nearly everything wants.
        proposal = cap.resolve(task, ctx)
        if proposal is None:
            # The schema is checked AFTER the caller's own values are merged in, below.
            # Checking it here failed on fields the caller had supplied: passing `body=`
            # to `mail.forward` could not satisfy the requirement, because the model's
            # reply was rejected before the merge that would have added it. The complaint
            # named the model — "model omitted required field(s): body" — for a field the
            # human had just provided.
            proposal = llm.call(system=cap.system, prompt=task, context=ctx,
                                schema=None, capability=cap.name,
                                wants=_required_fields(cap))
    except OpFailed as e:
        # Opt-in only: a capability that has nothing true to say without the model must
        # still fail. See Plugin.degrade.
        fallback = cap.degrade(task, e)
        if fallback is None:
            raise
        return dict(fallback)
    if not isinstance(proposal, dict):
        raise OpFailed(f"{cap.name}: model did not return an object")
    for k, v in payload.items():                 # explicit args always win over the model
        if k != "task" and v not in (None, "", [], {}):
            proposal[k] = v
    # Now that the caller's values are in, fit them to the declared shapes and hold the
    # whole thing to the contract.
    llm.check_schema(llm.fit_schema(proposal, cap.schema), cap.schema)
    return proposal


def _required_fields(cap) -> set:
    """The schema fields a judged capability cannot do without."""
    try:
        spec = json.loads(cap.schema) if isinstance(cap.schema, str) else dict(cap.schema)
    except (json.JSONDecodeError, TypeError, ValueError):
        return set()
    return set(spec.get("required") or [])


# ── the one side-effect point ───────────────────────────────────────────────
def _unsuccessful(cap, proposal: dict, result) -> str:
    """A result that reports its own failure — asked of the CAPABILITY, not of the shape
    of its output.

    This used to inspect every result for `ok`, `verified` or `success` and fail the run
    when one was False. That is still the default (`Plugin.verify`), so nothing changes
    for a capability that does not override it — but the question is now declared on the
    thing that knows the answer rather than being three magic key names discovered by
    reading core.
    """
    try:
        ok, evidence = cap.verify(proposal, result)
    except Exception as e:                                         # noqa: BLE001
        # A verification that cannot run has not verified anything. Failing closed is the
        # whole point: the alternative is a capability whose check raised being reported
        # as a success.
        return f"verification raised: {type(e).__name__}: {str(e)[:160]}"
    return "" if ok else (str(evidence) or "verify() reported failure with no evidence")


def _execute(rid: str, cap, proposal: dict) -> dict:
    """Run it, and record what actually happened. The ONLY caller of execute()."""
    # THE PROCESS DOING THE WORK, for liveness only. A backgrounded run has `worker_pid`; a
    # foreground one had nothing, so a foreground run whose process was killed said
    # `running` for ever — the reaper cannot close what it cannot check, and rightly will
    # not guess. Kept apart from `worker_pid` on purpose: a stop signals `worker_pid`, and
    # a foreground process can be executing a converse run and the agent run inside it at
    # once, so signalling this would take both down.
    from . import caller as _caller
    tracker.update(rid, status=tracker.RUNNING,
                   started=tracker._now(), process_pid=os.getpid(),
                   process_started=_caller.started(os.getpid()))
    # WHICH RUN THIS IS, for anything below that starts a process. `llm._stream` is the
    # one that matters: it spawns the engine, and until it could name the run, nothing
    # wrote down the pid that a stop actually has to reach.
    with tracker.executing(rid):
        return _execute_body(rid, cap, proposal)


def _execute_body(rid: str, cap, proposal: dict) -> dict:
    try:
        # The run id travels WITH the proposal, so a capability writing a file knows where
        # it belongs. Not persisted — it is added at call time, and the record already
        # carries its own id.
        # Steps land on the RUN, for every capability rather than only the ones that
        # happen to hold a session: `finrail view <id>` is where someone looks when a
        # command is taking a long time, and until now it had nothing to show them.
        def _step(entry):
            rec = tracker.load(rid)
            tracker.update(rid, progress=progress.trim(
                (rec.get("progress") or []) + [entry]))
            # The live list keeps 40; the log keeps every step, in order.
            events.append(events.thread_of_run(rec), "step",
                          {k: v for k, v in entry.items() if k != "full"}
                          | ({"text": entry["full"]} if entry.get("full") else {}),
                          run=rid)

        # WHICH PLUGIN IS WRITING, for rule 2: a write into a store another plugin owns
        # is recorded on this run as it happens (core/owners.py).
        with progress.sink(_step), owners.acting(getattr(cap, "bundle", ""), rid):
            # A READ that detached runs `run()`, because a read-only capability has no
            # executor — `Plugin.execute` raises for one on purpose. The contract test
            # has always permitted `read_only` with `background` ("a slow read may
            # legitimately detach") and nothing implemented it, so the first capability to
            # declare both died with "no executor". A declaration the dispatcher cannot
            # honour is the drift this system exists to prevent.
            result = (cap.run(**{k: v for k, v in proposal.items()
                                 if not k.startswith("_")})
                      if cap.read_only else cap.execute({**proposal, "_run": rid}))
    except Exception as e:
        rec = tracker.update(rid, status=tracker.FAILED,
                             errors=[f"{type(e).__name__}: {e}"])
        # ITS QUESTIONS CLOSE WITH IT. Nobody stopped it, so `kill` never ran: the
        # engine's own timeout kills the engine's whole group, the waiting gate with
        # it, and the question stayed open until its own time ran out. For up to ten
        # minutes `/answer yes` said "allowed" and a new question needed its id
        # (measured 6 Oct, the engine killed under a waiting gate). The failure is
        # written by the process that ran the engine, after the engine is gone, so no
        # gate of this run is left to hear an answer.
        _unask(rec, "the run that asked failed")
        registry.hooks().emit("after_run", rec)
        raise
    bad = _unsuccessful(cap, proposal, result)
    status = tracker.FAILED if bad else tracker.DONE
    try:
        rec = tracker.update(rid, status=status, result=result,
                             errors=[bad] if bad else [])
    except ContractViolation as e:
        # ONLY this update, and only this exception: the ledger refusing to record an
        # ending is not a reason to throw the ending away. Everything else in this
        # function still raises, including a ContractViolation out of the capability
        # itself, which is a real failure and is caught above.
        rec = _delivered_anyway(rid, result, e)
    registry.hooks().emit("after_run", rec)
    if bad:
        raise OpFailed(f"{cap.name}: {bad}")
    return rec


def _read(cap, proposal: dict):
    """A foreground read. It has no run record, but a read that writes into another
    plugin's store is still a crossing, and the suite still counts it."""
    with owners.acting(getattr(cap, "bundle", ""), ""):
        return cap.run(**proposal)


def _delivered_anyway(rid: str, result, why: ContractViolation) -> dict:
    """The run finished, and the ledger had already closed it.

    `failed` is terminal on purpose — a run that was killed did not finish, and
    `TRANSITIONS` says so — so a run closed while it was in fact still working cannot
    record its outcome as a status. What must not follow is what did: the refusal
    propagated out of `_execute`, killed the `converse` run that was delivering the
    answer, and the operator read "illegal transition failed -> done" instead of their
    report (runs f23390df and b9da61c8, 2026-09-15). The ledger lost an argument with
    reality and took the answer down with it.

    Delivery is separated from the record here rather than by widening `TRANSITIONS`. The
    status stays exactly what the ledger decided, because a killed run really was killed
    and every reader of that field can go on trusting it. The RESULT is written to the
    record anyway — `update` permits any write that does not change the status — so the
    one place an operator looks, `finrail view <id>`, shows a failed run with its answer
    attached and a line in `errors` saying why the two disagree. No new reader, no new
    state, and nothing silently dropped.
    """
    rec = tracker.load(rid)
    note = (f"finished after the ledger closed it as {rec['status']}, so its outcome "
            f"could not be recorded as a status ({why}) — but it did finish, and the "
            f"result below was delivered to whoever asked.")
    try:
        return tracker.update(rid, result=result, delivered=True,
                              errors=[*(rec.get("errors") or []), note])
    except Exception:                                            # noqa: BLE001
        # Recording that the answer was delivered must never become the reason it is not.
        return {**rec, "result": result, "delivered": True}


# ── backgrounding ───────────────────────────────────────────────────────────
def _spawn(rid: str) -> dict:
    """Hand the run to a detached worker: `finrail _job <id>`.

    start_new_session detaches it from this process group, so it survives the CLI
    exiting; PATH is set explicitly because a detached process inherits almost nothing.
    """
    env = dict(os.environ, PATH=path_env())
    log = (tracker.runs().parent / "logs")
    log.mkdir(parents=True, exist_ok=True)
    out = open(log / f"job-{rid}.log", "ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, "-P", "-m", "finrail", "_job", rid],   # -P: see serve._child_argv
        stdout=out, stderr=out, stdin=subprocess.DEVNULL,
        start_new_session=True, env=env)
    from . import caller as _caller
    return tracker.update(rid, worker_pid=proc.pid, worker_started=_caller.started(proc.pid),
                          background=True)


def run_job(rid: str) -> dict:
    """The detached worker's entry point."""
    with _claim_lock(rid):
        rec = tracker.load(rid)
        if rec["status"] != tracker.PENDING or rec.get("worker_claimed"):
            raise ContractViolation(
                f"run {rid} is {rec['status']}"
                + (" and claimed by a worker" if rec.get("worker_claimed") else "")
                + ", not pending — refusing to run it twice")
        tracker.update(rid, worker_claimed=os.getpid())
    cap = registry.command(rec["capability"])
    # ITS OWN RUN, for a question its gate asks (core/scope.settle files one under
    # FINRAIL_RUN). A worker starts with the environment of whoever spawned it, and
    # that named the spawner's run: the turn that sent this to the background, over
    # long before the worker asks anything. A question filed there was closed by
    # neither a stop of this run nor its reaping, so it went on being offered, and
    # `/answer yes` said "allowed" about a run that was gone (measured 6 Oct with a
    # real /background turn).
    os.environ["FINRAIL_RUN"] = rid
    try:
        return _execute(rid, cap, rec["proposal"])
    finally:
        report_back(rid)


# ── reporting a detached run back to the turn that started it ────────────────
#: Characters of a finished run's result put in the notice. The notice says the work is
#: over and where to read it; the whole result stays on the run (`finrail view`).
REPORT_CHARS = 600


def _turn_addresses() -> dict:
    """Where this process's turn is: its thread, its person and its agent, or {} when it
    is not inside a turn. Read from the environment converse set for the turn (the agent's
    door sets it again from the run record), never from anything the model typed."""
    from . import agents
    from . import context as _context
    thread = (os.environ.get(_context.THREAD) or "").strip()
    agent = agents.current()
    if not thread or not agent:
        return {}
    return {"thread": thread, "agent": agent,
            "person": (os.environ.get(_context.TURN_USER) or "").strip()}


def _will_report(rid: str, source: str) -> None:
    """Mark a run that is about to leave its turn so its ending is said in the thread.

    WORK THAT OUTLIVES ITS TURN USED TO END IN SILENCE, OR NOT AT ALL. An agent with a
    long step had two ways to run it: in its own shell, which the engine sends to the
    background past ten minutes and kills when the turn ends; or detached here, which
    survived and then said nothing. On 8 Oct a record the person asked for was killed
    with its turn, the agent said "running again" for a run already dead, and the result
    arrived partial four days later. Detached, the run now carries its turn's addresses,
    and the worker says how it ended in the same thread (`report_back`).

    Not for `converse.background`: that is a whole turn moved aside by the person, and
    `check` and `foreground` are how it comes back."""
    if source == "converse.background":
        return
    where = _turn_addresses()
    if where:
        tracker.update(rid, notify=where)


def report_back(rid: str) -> dict:
    """Say in the thread that started it how a detached run ended. Once per run; a failure
    to say it is recorded on the run and never raised, because the run's own ending is
    already on the ledger and must not be turned into a crash by the notice."""
    try:
        rec = tracker.load(rid)
    except Exception:                                              # noqa: BLE001
        return {}
    where = rec.get("notify") or {}
    if not where.get("thread") or rec.get("reported"):
        return {}
    text = report_text(rec)
    try:
        from . import agents, transport
        here = agents.load(where["agent"])
        sent = transport.for_agent(here).reply(here, where["thread"], text,
                                               where.get("person") or "")
        tracker.update(rid, reported=tracker._now())
        events.append(where["thread"], "notice",
                      {"text": text, "about": rid, "sent": sent.get("id") or ""}, run=rid)
        return sent
    except Exception as e:                                         # noqa: BLE001
        tracker.update(rid, report_error=f"{type(e).__name__}: {e}"[:300])
        return {}


def report_text(rec: dict) -> str:
    """The notice: what ran, how it ended, and where the rest is. Plain words first, the
    run id last, as every answer ends."""
    task = (rec.get("task") or rec.get("capability") or "the work")[:160]
    rid = rec.get("id") or ""
    if rec.get("status") == tracker.DONE:
        body = _result_text(rec.get("result"))
        head = f"Finished: {task}"
        return (head + (f"\n\n{body}" if body else "")
                + f"\n\nrun {rid} · finrail view {rid}")
    why = "; ".join(str(e) for e in (rec.get("errors") or []))[:300] or rec.get("status", "")
    return (f"Did not finish: {task}\n\n{why}"
            f"\n\nrun {rid} · finrail view {rid}")


def _result_text(result) -> str:
    if isinstance(result, dict):
        for k in ("text", "summary", "note", "message"):
            if isinstance(result.get(k), str) and result[k].strip():
                result = result[k]
                break
        else:
            result = ", ".join(f"{k}={v}" for k, v in result.items()
                               if isinstance(v, (str, int, float)) and str(v).strip())
    text = str(result or "").strip()
    return text if len(text) <= REPORT_CHARS else text[:REPORT_CHARS].rstrip() + " …"


#: Keys that describe the machinery, not the request.
_NOT_THE_ASK = {"task", "_run", "_task", "_candidates", "_model_error", "_looked_up"}


def asked(name: str, task: str, payload: dict) -> str:
    """What was actually requested, in one line, for the run record.

    2,337 of 2,426 runs carried an EMPTY `task`. Only judged capabilities take free text;
    for every deterministic one the request lives in the PARAMS, and nothing wrote them
    down — so the ledger recorded that `mail.delete` ran without recording what it was
    asked to delete. Every other finding about a run is harder to diagnose for it, and
    the audit trail an approval gate exists to produce was missing its subject.
    """
    if (task or "").strip():
        return task.strip()
    bits = []
    for k, v in (payload or {}).items():
        if k in _NOT_THE_ASK or v in (None, "", [], {}, False):
            continue
        text = ",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
        bits.append(f"{k}={text}" if len(text) < 60 else f"{k}={text[:57]}…")
    return f"{name} {' '.join(bits)}".strip() if bits else name


# ── the two entry points ─────────────────────────────────────────────────────
def dispatch(name: str, payload: dict, task: str = "", source: str = "cli",
             on_record=None, actor: str = "", detach: bool = False) -> dict:
    """Propose, then execute now or wait for approval, exactly as the capability's own
    rules dictate.

    `on_record(rid)` is called once the run has an id and before it executes. For a
    capability with no approval step, execution happens INSIDE this call, so the id is
    otherwise learned only when the work is over — which is how converse came to claim a
    coding-agent or production thread after each turn had finished, leaving the thread `idle`
    for the whole of the turn it was meant to describe.

    `detach` asks for what `cap.background` declares, for this one call: a run that
    executes now executes in a detached worker (`_spawn`), and this returns as soon as
    the worker has started. It changes nothing about approval: a run that waits for
    approval still waits."""
    # A JUDGED capability does its thinking inside propose(), which happens before the
    # run record exists — so there is no id to attach steps to yet, and the sink set
    # around execute() below never sees them. Collect here and store them with the
    # record. They arrive with the proposal rather than during it; for a long judged
    # call (the apps.add loop, when it lands) that is the next thing to fix, and it
    # means reordering dispatch to save the record before judging.
    thinking: list = []
    with progress.sink(thinking.append):
        cap, proposal, errors = propose(name, payload, task)

    if cap.kind == "read":
        if not (cap.background or detach):
            return {"status": tracker.DONE, "capability": cap.name, "kind": "read",
                    "result": _read(cap, proposal), "id": None}
        rec = tracker.save(cap.name, asked(cap.name, task, payload), proposal,
                           kind="read", source=source, actor=actor)
        if thinking:
            tracker.update(rec["id"], progress=progress.trim(thinking))
        _will_report(rec["id"], source)
        return _spawn(rec["id"])

    rec = tracker.save(cap.name, asked(cap.name, task or payload.get("task", ""),
                                      payload), proposal,
                       status=tracker.PENDING, errors=errors, kind=cap.kind,
                       source=source, actor=actor)
    if thinking:
        rec = tracker.update(rec["id"], progress=progress.trim(thinking))

    if errors:
        rec = tracker.update(rec["id"], status=tracker.FAILED)
        raise OpFailed(f"{cap.name}: " + "; ".join(str(e) for e in errors))

    if on_record:
        on_record(rec["id"])

    if cap.background or detach:
        # Remembered on the run, so an approval given later still detaches it, and its
        # ending is said in the thread it came from.
        if detach:
            tracker.update(rec["id"], detach=True)
        _will_report(rec["id"], source)

    # autonomous means "no separate approval step" — it does NOT mean "unrecorded".
    if cap.autonomous and not cap.confirm:
        # DETACHED WHEN ASKED, not only when declared. `converse.background` wraps an
        # agent that declares no `background`, and an autonomous one executed right
        # here, inside the turn that asked not to wait: on 6 Oct a production
        # `/background` held the operator's turn for seven minutes, and "running detached"
        # arrived with the work already over (runs e89ee9cd and 4d992761, both ending
        # 13:34:04, neither with a worker).
        if cap.background or detach:
            return _spawn(rec["id"])
        return _execute(rec["id"], cap, proposal)

    # A PERSON'S OWN CALL IS THE APPROVAL, where the capability says so
    # (`person_call_approves`): asking them to approve what they just asked for asks the
    # same question twice. It goes through `approve`, so it meets the check every
    # approval meets; from inside an agent that check fails here as well, and the run
    # waits for a person (memory.learn, 8 Oct: the full agent wrote nine lessons into
    # other agents' prompts with nobody asked). Recorded as `asked`, not `approved`:
    # nobody reviewed the proposal, and the journal keeps verdicts as judgements.
    if cap.confirm and getattr(cap, "person_call_approves", False):
        from . import caller as _caller
        if not _caller.approval_refusal():
            return approve(rec["id"], actor=actor, verdict="asked", detach=detach)

    return rec                                     # waits for approve()


def _claim_lock(rid: str):
    """One lock per run, held while a caller decides it is the one to execute it. Kept
    beside the runs, not on the record's own file, which `tracker` writes under its own."""
    from . import atomic
    return atomic.locked(tracker.runs().parent / "locks" / f"run-{rid}")


def approve(rid: str, actor: str = "", verdict: str = "approved",
            detach: bool = False) -> dict:
    """See `_approve`. Held under the run's claim lock: two approvals that both read
    `pending` used to both execute it, which for a send or a deploy is the side effect
    twice (review, 5 Oct). Now the first to take the lock records the verdict, and the
    second reads it and is told the run is already approved.

    `verdict` is what is recorded; `detach` runs it in a detached worker, as `dispatch`
    does for a call that asked not to wait. Both for a person's own call (`dispatch`)."""
    with _claim_lock(rid):
        go = _approve(rid, actor, verdict)
    if not go.get("_claimed"):
        return go
    cap, rec = go["cap"], go["rec"]
    if cap.background or detach or rec.get("detach"):
        return _spawn(rid)
    return _execute(rid, cap, rec["proposal"])


def _approve(rid: str, actor: str = "", verdict: str = "approved") -> dict:
    """Make this run approved. Idempotent, on purpose.

    `actor` is WHO approved, when that is knowable — an operator's pubkey arriving over a
    transport that verified the signature on their message. It is recorded beside the
    verdict, because "a human said yes" and "which human said yes" are different facts and
    only the first was ever kept. On a laptop the terminal is the answer and nobody needs
    telling; an approval that crossed a network is the case that wants a name on it.

    Carried, never derived — the same rule `agents.ENV` follows, and for the same reason:
    the message asking for the approval must not be the thing that says who asked.

    Approving something already approved, already running or already done is not an
    ERROR — the caller asked for a state, and the state holds. It used to raise, which
    made the CLI exit non-zero, which silently broke every `approve A && approve B &&
    approve C` an operator (or an assistant) had been handed: the first already-done id
    aborted the chain and B and C were never approved at all. The operator then read
    "nothing to approve" and believed all three had gone through. Reported here, twice,
    from both ends of that misunderstanding.

    A FAILED run still raises: it did not happen, and saying nothing would imply it did.
    """
    rec = tracker.load(rid)
    if rec["status"] in (tracker.DONE, tracker.RUNNING) or (
            rec["status"] == tracker.PENDING and not tracker.awaiting(rec)):
        when = (rec.get("updated") or rec.get("created") or "")[:16].replace("T", " ")
        if rec["status"] == tracker.DONE:
            note = f"already done at {when} — nothing to approve"
        elif rec["status"] == tracker.RUNNING:
            note = f"already approved and running (started {when})"
        else:
            note = "already approved, queued behind a running job"
        return {**rec, "already": True, "note": note}
    if rec["status"] != tracker.PENDING:
        # Say what HAPPENED, not what the state machine permits. "only a pending run can
        # be approved" reads as a quibble about state, so an operator took it as a
        # curiosity and kept treating finished work as outstanding. The run already
        # answered the question being asked; the reply should be that answer.
        when = (rec.get("updated") or rec.get("created") or "")[:16].replace("T", " ")
        if rec["status"] == tracker.DONE:
            detail = f"already done at {when} — nothing to approve"
        elif rec["status"] == tracker.FAILED:
            problems = "; ".join(str(e) for e in (rec.get("errors") or []))[:160]
            detail = (f"failed at {when} and cannot be approved — run it again"
                      + (f"\n  why: {problems}" if problems else ""))
        elif rec["status"] == tracker.RUNNING:
            detail = f"still running (started {when}) — wait for it"
        else:
            detail = f"is {rec['status']}"
        raise ContractViolation(
            f"{rec['capability']} run {rid} {detail}.\n"
            f"  what it did: finrail view {rid}\n"
            f"  what is actually outstanding: finrail status")
    if rec.get("errors"):
        raise OpFailed(f"run {rid} did not validate: {'; '.join(rec['errors'])}")
    cap = registry.command(rec["capability"])
    # THE AGENT IS NOT THE PERSON. Every way to approve ends here (the CLI, /approve,
    # converse's approve=true, a task gate), so this is where it is held, not in how a
    # shell spells the command.
    if getattr(cap, "confirm", False):
        from . import caller as _caller
        refused = _caller.approval_refusal()
        if refused:
            # NeedsPerson, a ContractViolation: the same refusal, said so the person
            # reading it knows the run waits for them and how to approve it.
            raise NeedsPerson(rid, rec["capability"],
                              refused.removeprefix("a run that waits on a person is "))
    # A human looked at this and said yes. Worth distinguishing from an autonomous run:
    # the proposal was judged good, which is evidence in the other direction from a drop.
    # A person's own call says `asked`: they asked for it, and did not judge a proposal.
    tracker.update(rid, verdict=verdict, **({"approved_by": actor} if actor else {}))
    return {"_claimed": True, "cap": cap, "rec": rec}


def approve_safely(rid: str, actor: str = "") -> dict:
    """`approve`, with a refusal RETURNED rather than raised — for a caller holding a list.

    THE SAME FAILURE `approve` DESCRIBES, ON A DIFFERENT TRIGGER. That docstring records
    an `approve A && approve B && approve C` chain aborting on the first already-done id,
    and fixes it by making an id that is already approved non-fatal. It does not cover a
    run that RAISES — one that fails while executing, or that cannot be approved at all —
    and the consequence is identical: `finrail approve` given twenty ids ran five, raised
    on the sixth, and never attempted the remaining fourteen. They stayed pending with
    nothing printed to say so, so the operator read one error line and had every reason to
    believe the other nineteen had gone through (2026-09-16).

    So a batch attempts EVERY id and gets a row for every one of them: `approved: False`
    and the reason, in place of an exception that takes the rest of the list with it.
    Raising is still right for a caller with ONE id — it asked for one thing, and the one
    thing did not happen.
    """
    try:
        return {**approve(rid, actor=actor), "approved": True}
    except Exception as exc:                                     # noqa: BLE001
        # The RECORD, not just the message: a run that failed while executing has already
        # written its status and its errors, and they say more about what happened than
        # the exception this caller happens to be holding.
        try:
            rec = tracker.load(rid)
        except Exception:                                        # noqa: BLE001
            rec = {"id": rid, "capability": "", "status": "unknown"}
        # A deliberate error's message was written to be read; anything else needs its
        # type, or a batch line reads `settings.set: 'setting'` and explains nothing.
        said = (str(exc) if isinstance(exc, FinrailError)
                else f"{type(exc).__name__}: {exc}")
        return {**rec, "id": rid, "approved": False, "already": False,
                "note": said.split("\n")[0],
                "errors": rec.get("errors") or [said]}


def _claimant(rid: str):
    """The pid that claimed a thread for this run, when the run has no detached worker.

    Read from the thread records rather than held anywhere new: converse already writes
    {run, pid} there precisely so a killed process can be told from a working one.
    """
    import json as _json
    from .settings import state
    d = state() / "threads"
    if not d.is_dir():
        return None
    for path in d.glob("**/*.json"):
        try:
            busy = (_json.loads(path.read_text()) or {}).get("busy") or {}
        except Exception:                                        # noqa: BLE001
            continue
        if busy.get("run") == rid and busy.get("pid"):
            return busy["pid"]
    return None


def kill(rid: str) -> dict:
    """Stop the process doing this run, and mark it failed (it did not finish).

    A BACKGROUNDED run has `worker_pid` — the detached `finrail _job` child. A
    FOREGROUND one does not: the work is being done by the process that dispatched it,
    which is the process converse recorded on the thread as `busy.pid`. Signalling only
    worker_pid therefore marked a foreground run failed and killed nothing at all — the
    engine carried on, the turn finished normally, and `/stop` reported success over work
    that never stopped.

    Both are the same question — who is doing this? — so both are asked.
    """
    rec = tracker.load(rid)
    was = rec["status"]
    # BOTH PROCESSES, in this order. The engine is what is doing the work and what owns
    # the agent's own shell children; the worker is the python process waiting on it.
    # Signalling only the worker left the engine running and reported a successful stop —
    # the difference between a stop that worked and one that merely said so.
    targets = []
    if rec.get("engine_pid"):
        targets.append(("the engine", rec["engine_pid"]))
    holder = rec.get("worker_pid") or _claimant(rid)
    if holder:
        targets.append(("the worker", holder))

    killed, notes = False, []
    if was in (tracker.PENDING, tracker.RUNNING):
        # ITS QUESTIONS FIRST, before anything is signalled. The transport reads a turn's
        # feed for as long as the turn's process lives and no longer, and this used to
        # run after the stop: the question's ending landed in the log about 80 ms after
        # that process had gone (measured 6 Oct), nothing read it, and the chat message
        # went on asking. Written first, it is in the log before that process can end.
        # A gate that reads it in the moment before the signal says no, which is safe.
        _unask(rec)
    # WHY, not just whether — because "stopped 0 of 1" is a mystery and "it was waiting
    # for approval, not running" is an answer. A stop that killed nothing has a reason,
    # and it is usually that there was nothing to kill: the run had already finished, or
    # was still pending and never started, or its worker had already exited.
    if was not in (tracker.PENDING, tracker.RUNNING):
        why = f"already {was} — nothing was running to stop"
    elif was == tracker.PENDING:
        why = "was waiting for approval, not running — marked failed so it cannot start"
    elif not targets:
        why = "no live worker to signal — it had already exited"
    else:
        for what, pid in targets:
            note, reached = _stop_pid(int(pid))
            killed = killed or reached
            notes.append(f"{what} (pid {pid}) {note}")
        why = "; ".join(notes)
    if was in (tracker.PENDING, tracker.RUNNING):
        rec = tracker.update(rid, status=tracker.FAILED,
                             errors=[f"killed by operator ({why})"])
    return {"run": rid, "killed": killed, "status": rec["status"], "was": was, "why": why}


def _unask(rec: dict, why: str = "the run that asked was stopped") -> None:
    """Close the questions a run that is ending without its gate was waiting on
    (core/questions.py): one being stopped, or one that has just failed (`why`).

    The gate that asked is the engine's child and goes down with it, so nobody is left
    to hear an answer. Left open, the question was still offered to `/answer`, which
    said "allowed" about a turn that was gone, and the next question in the conversation
    could not be answered without its id.

    A question is filed under the TURN's run, the converse run, and a stop is aimed at
    the run inside it, the one holding the engine: two ids for one turn (measured 6 Oct
    with the fake engine, a bare /stop on a turn waiting for an answer). So a question
    in this conversation is also this turn's when the run it is filed under was executed
    by the same process as this one (`_same_process`). A run in the background is
    executed by a worker of its own, and its questions are filed under it (`run_job`).

    The same two ids when the run inside fails: converse catches a folded turn's failure
    and its own run ends done, so a question filed under the converse run was closed by
    nothing but its deadline (second review, 6 Oct). Closing by process, not by id, ends
    it with the run that asked.

    Never raises: the stop, or the failure's own error, is what has to travel, and it
    goes ahead whatever happens here. A question this could not close still ends at
    its own deadline.
    """
    try:
        from . import questions
        runs = {rec["id"]}
        thread = rec.get("thread") or ""
        for q in (questions.open_in(thread) if thread else ()):
            if _same_process(q.get("run"), rec):
                runs.add(q["run"])
        for rid in runs:
            questions.expire_run(rid, why)
    except Exception:                                            # noqa: BLE001
        pass


def _same_process(rid: str, rec: dict) -> bool:
    """Was run `rid` executed by the process executing `rec`?

    One foreground process executes the converse run and the agent run inside it at once
    (`_execute`), and that is all that ties the two in the ledger. Its status is not
    asked: once the engine is gone that process writes the converse run's ending within
    moments, and an engine that went by itself a moment before the stop leaves the
    question filed under a run already ended. A turn parked in the background runs in
    another process, and its questions are not this turn's.
    """
    pid = rec.get("process_pid")
    if not rid or not pid:
        return False
    try:
        other = tracker.load(rid)
    except Exception:                                            # noqa: BLE001
        return False                     # filed under no run this ledger has
    when, then = rec.get("process_started"), other.get("process_started")
    # With its start time where both wrote one: a pid can be reused.
    return other.get("process_pid") == pid and (when or then) == (then or when)


#: How long a signalled process is given to end before it is ended for it. Short, because
#: whoever typed stop is watching; long enough for ordinary cleanup to finish.
KILL_GRACE_S = 4.0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True                  # someone else owns it, but it is there


def _signal(pid: int, sig: int) -> bool:
    """Signal a process, or its whole group when it leads one.

    Whether it leads a group is asked, never assumed. Our own detached workers and engines
    are session leaders, so the group IS the work and signalling it catches every child
    the agent started. A pid that leads no group belongs to somebody ELSE's group — a
    foreground turn shares the group of the shell or the unit that started finrail — and
    signalling that group would take the caller down with the run.
    """
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, ValueError, OSError):
        return False


def _stop_pid(pid: int, grace: float = KILL_GRACE_S) -> tuple:
    """Ask it to stop, wait, then stop it. Returns (what happened, whether we reached it).

    A single TERM was the whole of this, and a process that ignores TERM — or that is
    wedged in a way that cannot service one — was reported as stopped while it carried on
    working. The escalation is what makes the word honest.
    """
    import time
    if not _alive(pid):
        return ("was already gone", False)
    if not _signal(pid, signal.SIGTERM):
        return ("could not be signalled", False)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _alive(pid):
            return ("stopped", True)
        time.sleep(0.1)
    if _signal(pid, signal.SIGKILL):
        # NOT RE-CHECKED, deliberately. SIGKILL cannot be caught or ignored, so delivering
        # it IS the outcome — and a process whose parent has not yet reaped it still
        # answers signal 0, so looking again reported "would not die" about a process that
        # was already dead. The honest failure here is a signal that could not be SENT,
        # which is what the branch below says.
        return (f"ignored the signal for {grace:g}s and was killed", True)
    return ("stopped", True)
