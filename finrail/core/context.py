"""INVOCATION CONTEXT — who a turn is for, carried to the commands its agent runs.

A scoped agent's turn has a person behind it: converse knows who spoke, and a caller that
dispatches the agent on somebody's behalf says who. None of that reached the SHELL the
engine runs. When A production agent ran `finrail do example.company.propose …` from inside a turn,
that command was a fresh process with no user, no idea which turn caused it, and no way
to know it was a conversation turn at all — so a rule like "a conversation turn may not
propose" could only be written in her briefing, which is a request and not a rule.

So a turn's context travels the way the agent's identity already does (`agents.ENV`): in
the ENVIRONMENT of the processes it starts, set by the code that starts them, never read
out of a message.

    user        the person the turn is for
    request     what the turn is about, when the caller gave it one (e.g. a pedido id)
    stage       which step of that request the turn is (e.g. `talk`, `propose`)
    parent_run  the run that started the process — the nearest one, at every hop

A PROCESS CANNOT WIDEN WHAT IT INHERITED. `user`, `request` and `stage` are fixed by the
first process that set them: a child passing its own — a `user=` argument, a `context=`
on a nested dispatch — is overridden by the value in the environment, not the reverse.
Otherwise an agent told "this turn may not propose" could start a second turn saying it
may. `parent_run` is the exception by definition: each hop names its own run.

The parent's `os.environ` is never written. A harness runs many turns in one long-lived
process, and a context set there would be every later turn's too; `child_env` returns a
copy for the subprocess instead.

Beside the context a turn has three ADDRESSES, set by converse for everything the turn
runs: the thread it is in, the person it is for, and its run. Everything a turn hands the
processes it starts is named here and nowhere else (`TURN_VARS`): converse sets the
addresses with `turn_addresses`, a scoped agent hands its engine the context with
`child_env`, and the agent's door restores the whole turn from the run record with
`turn_env`. A variable added here reaches all three; one added to any of them alone
reached nothing the door ran (review, 5 Oct).
"""
import json
import os

from .errors import ContractViolation

ENV = "FINRAIL_CONTEXT"

FIELDS = ("user", "request", "stage", "parent_run")

#: What a child may not change once a parent has set it. `parent_run` is not here: every
#: hop names its own run, which narrows nothing.
FIXED = ("user", "request", "stage")

#: The turn's addresses: where it is, whom it is for, which run it is.
THREAD = "FINRAIL_THREAD"
TURN_USER = "FINRAIL_TURN_USER"
RUN = "FINRAIL_RUN"


def _clean(ctx) -> dict:
    """Only the known fields, only non-empty strings."""
    if not isinstance(ctx, dict):
        return {}
    return {k: str(ctx[k]).strip() for k in FIELDS
            if ctx.get(k) not in (None, "") and str(ctx[k]).strip()}


def current() -> dict:
    """The context this process inherited, or {} when it has none.

    An environment variable that is present and unreadable RAISES rather than reading as
    empty. Empty means "no context", which is the unrestricted CLI case — so a garbled
    value silently turning into it would lift every rule the context exists to carry.
    """
    raw = os.environ.get(ENV)
    if raw is None or not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ContractViolation(
            f"{ENV} is set but is not a JSON object — refusing to act without knowing "
            f"what turn this is part of")
    return _clean(body)


def merge(wanted: dict = None, inherited: dict = None) -> dict:
    """What a child's context is: what it asks for, under what it inherited.

    `inherited` defaults to this process's own. Its `user`, `request` and `stage` win
    over anything asked for; `parent_run` is the child's when it gives one.
    """
    base = current() if inherited is None else _clean(inherited)
    out = {**base, **_clean(wanted)}
    for k in FIXED:
        if base.get(k):
            out[k] = base[k]
    return out


def user(given: str = "") -> str:
    """Who this process acts for: the inherited user when there is one, else `given`.

    The environment wins over an argument, because the argument is the one a model in
    the turn can type.
    """
    return current().get("user") or str(given or "").strip()


def child_env(ctx: dict, base: dict = None, inherited: dict = None) -> dict:
    """A COPY of `base` (default: this process's environment) carrying `ctx` for a child.

    `ctx` is merged under what this process inherited (`inherited`, when a caller holds
    the turn's context without being inside it), so the child cannot be handed wider
    than this process holds. An empty result leaves the variable out.
    """
    env = dict(os.environ if base is None else base)
    merged = merge(ctx, inherited=inherited)
    if merged:
        env[ENV] = json.dumps(merged, sort_keys=True)
    else:
        env.pop(ENV, None)
    return env


def turn_addresses(person: str = "", thread: str = "", run: str = "") -> dict:
    """The turn's three addresses, as the processes it starts read them. Every key is
    present, empty when the turn has no value, so a value an earlier turn left in the
    same environment does not stand. The person is lower-cased, like every key."""
    return {THREAD: str(thread or "").strip(),
            TURN_USER: str(person or "").strip().lower(),
            RUN: str(run or "").strip()}


#: Everything a turn hands the processes it starts, by name: the keys of
#: `turn_addresses` and the context. The door strips these from a caller's environment
#: and sets them again from the turn's record (core/caller.child_env).
TURN_VARS = tuple(turn_addresses()) + (ENV,)


def turn_env(ctx: dict, thread: str = "", inherited: dict = None,
             base: dict = None) -> dict:
    """A COPY of `base` carrying a whole turn for a child: its context (`child_env`) and
    its addresses. The person is the context's `user` and the run its `parent_run`, so
    the addresses and the context cannot name different people. An address the turn has
    no value for is REMOVED, never left as the caller set it: the door relies on that to
    hand a caller's own claims to nobody."""
    merged = merge(ctx, inherited=inherited)
    env = child_env(merged, base=base, inherited={})
    for k, v in turn_addresses(merged.get("user"), thread, merged.get("parent_run")).items():
        if v:
            env[k] = v
        else:
            env.pop(k, None)
    return env


def for_record() -> dict:
    """The context as the run ledger records it: `on_behalf_of` rather than `user`, so a
    row reads as whose request it was rather than who typed the command."""
    ctx = current()
    out = {}
    if ctx.get("user"):
        out["on_behalf_of"] = ctx["user"]
    for k in FIELDS:
        if k != "user" and ctx.get(k):
            out[k] = ctx[k]
    return out


def of_record(rec: dict) -> dict:
    """The context a turn's run record holds, for the processes the turn starts: the
    inverse of `for_record`, except that the turn's own run is the child's
    `parent_run`. What the door hands a command run inside the turn."""
    return _clean({"user": rec.get("on_behalf_of"), "parent_run": rec.get("id"),
                   **{k: rec.get(k) for k in FIELDS if k not in ("user", "parent_run")}})
