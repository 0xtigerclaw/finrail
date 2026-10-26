"""THE TURN — which activity answers this message, and what the engine is told about it.

Stored per SESSION, checked per TURN. The session holds the activity in effect; each
turn a short model call asks whether it still applies. Most turns it does, so this
behaves like a per-session setting — and when the conversation has moved on, the next
turn moves with it instead of the person having to start a new session to fix a label.

Who decides, in order:

  1. a person — `/activity <name>` or `activity.set`, which PINS it. The check does not
     run against a pin; a pinned activity changes only when a person changes it.
  2. the check — a model call given the message, the activity in effect and the list.
     It may overrule an activity the agent chose, which is the point of it.
  3. the agent — `activity.set … pin=false` mid-turn, when it finds it was given the
     wrong one. Recorded as the agent's, and open to the next turn's check.
  4. otherwise it is kept.

The check is biased to KEEP. "ok, do it" and "check again" carry no signal about an
activity, and a check that re-chose from scratch on every message would flap on exactly
those. So it switches only when it names a different activity that exists; an empty
answer, an unknown name or a failed call all keep what is there.

The instructions are given ONCE, when the activity changes. Sessions resume, so what a
turn is told stays in the conversation; repeating it every turn would bloat the thread
and move the cacheable prefix. A turn that keeps the activity gets the header alone, and
a switch says plainly that the previous instructions no longer apply rather than leaving
two sets of them in the thread to be reconciled.

WHAT A NEW SESSION STARTS AS is `activity.default`. A session nobody has set one on has
no activity and no pin, so the check runs from its first turn — a model call in front of
every turn of every session until somebody pins it, which is seconds a turn spent
guessing at something an operator who types `/activity tasks` already knows.
`activity.default=none` starts each session with no activity, pinned: no check, no model
call. It is a STARTING state, not a lock — the first turn writes it into the session like
any other choice, and `/activity auto` hands that one session back to the check.

NOTHING CONFIGURED, NOTHING CHANGES. An agent with no activity folder and a session with
no activity get no block, no header and no model call — the turn is byte-for-byte what it
was before activities existed.
"""
import json

from ...core import llm, log, sessions
from . import store

_logger = log.get(__name__)

CHECK_SYSTEM = """You decide which ACTIVITY a message to an agent belongs to.

An activity is a named kind of work the agent has instructions for. You are given the
activities that exist, each with a one-line description, and the activity currently in
effect for this conversation (a dash when there is none).

Answer with the activity this message is part of:
  - If the activity in effect still fits, answer it. Short follow-ups — "ok", "do it",
    "check again", a question about what was just said — continue the activity in effect.
  - If the message has clearly moved on to a different listed activity, answer that one.
  - If no listed activity fits and none is in effect, answer an empty string.

Use only names from the list, exactly as written. Give a one-sentence reason."""

SCHEMA = json.dumps({"type": "object",
                     "properties": {"activity": {"type": "string"},
                                    "reason": {"type": "string"}},
                     "required": ["activity"]})


ENGINES = ("model", "jev", "shadow")
CHECK_LOG = "activity-check.jsonl"


def _setting(name: str, default):
    try:
        from ...core import registry
        v = registry.settings().get(name)
    except Exception:                                              # noqa: BLE001
        v = None
    return default if v in (None, "") else v


def engine() -> str:
    """Who answers the check — `activity.check_engine`. Anything unreadable is `model`:
    the check as it was before there was a choice."""
    v = str(_setting("activity.check_engine", "model"))
    return v if v in ENGINES else "model"


def _record(row: dict) -> None:
    """One line per check that involved Jev, so the two can be compared afterwards
    (`activity.checks`). Best effort — a log that cannot be written costs a turn nothing."""
    try:
        import time
        from ...core.settings import logs
        logs().mkdir(parents=True, exist_ok=True)
        with open(logs() / CHECK_LOG, "a") as f:
            f.write(json.dumps({"ts": int(time.time()), **row}) + "\n")
    except Exception:                                              # noqa: BLE001
        _logger.debug("activity check log not written", exc_info=True)


def _timed(fn, *a, **k):
    import time
    started = time.monotonic()
    out = fn(*a, **k)
    return out, int((time.monotonic() - started) * 1000)


def check(capability: str, message: str, current: str = "") -> dict:
    """{"activity": name or "", "reason": str} — what the message belongs to, judged
    against the activities on disk. Raises if nothing that can answer can be reached; the
    caller decides what a failed check means.

    Who judges is `engine()`. See `jev.py` for what the other two values mean; whatever
    they are, the answer has the same shape and an unknown name is never an activity."""
    rows = store.listing(capability)
    if not rows:
        return {"activity": "", "reason": f"{capability} has no activities"}
    mode = engine()
    if mode == "model":
        return _model_check(rows, message, current)

    from . import jev
    min_conf = int(_setting("activity.jev_min_conf", jev.MIN_CONF))
    row = {"capability": capability, "engine": mode, "message": (message or "")[:500],
           "current": current, "min_conf": min_conf}
    try:
        second = jev.pick(message, current, rows)
    except Exception as e:                                         # noqa: BLE001
        second = None
        row["jev_error"] = str(e)[:200]
    if second is not None:
        row["jev"] = second

    if mode == "jev" and jev.decides(second, min_conf):
        _record({**row, "used": "jev"})
        return {"activity": second["activity"], "named": second["named"],
                "reason": f"jev, {second['conf']}% sure"}

    try:
        out, ms = _timed(_model_check, rows, message, current)
    except Exception as e:
        _record({**row, "used": "none", "model_error": str(e)[:200]})
        raise
    _record({**row, "used": "model",
             "model": {"activity": out["activity"], "named": out.get("named", ""), "ms": ms}})
    return out


def _model_check(rows: list, message: str, current: str = "") -> dict:
    out = llm.call(
        system=CHECK_SYSTEM, prompt=message,
        context={"activities": "\n".join(f"{r['name']} — {r['description'] or '–'}"
                                         for r in rows),
                 "activity in effect": current or "–"},
        schema=SCHEMA, capability="activity.check", expect_json=True, timeout=60,
        wants=("activity",))
    name = str((out or {}).get("activity") or "").strip()
    known = {r["name"] for r in rows}
    return {"activity": name if name in known else "",
            "named": name, "reason": str((out or {}).get("reason") or "")}


def _default() -> str:
    """`activity.default` — "auto" or "none". Read per turn rather than held, so changing
    it reaches the next session without a restart."""
    from ...core import registry as _reg
    try:
        return str(_reg.settings().get("activity.default") or "auto")
    except Exception:                                              # noqa: BLE001
        return "auto"


def _initial() -> dict:
    """What a session nobody has set an activity on holds. Empty is today's answer — no
    name and no pin, so the check decides from turn one."""
    if _default() != "none":
        return {}
    return {"name": "", "pinned": True, "how": "none by default"}


def decide(capability: str, sid: str, message: str) -> dict:
    """{"name", "how", "pinned"} for this turn, and the session updated to match."""
    rec = sessions.load(sid)
    cur = sessions.activity(rec) or _initial()
    available = store.names(capability)
    name = cur.get("name") or ""
    pinned = bool(cur.get("pinned"))
    how = cur.get("how") or ""

    if name and name not in available:
        # Deleted, or renamed on disk, since it was chosen. Said with the name it had —
        # the raw key, not a guess at what replaced it — and cleared, pin and all: a pin
        # on something that is not there pins nothing.
        gone = name
        name, pinned = "", False
        how = f"{gone} no longer exists"
    elif pinned:
        pass
    elif not available:
        how = "none"
    else:
        try:
            verdict = check(capability, message, current=name)
        except Exception:                                          # noqa: BLE001
            _logger.debug("activity check unavailable; keeping %r", name, exc_info=True)
            verdict = None
        picked = (verdict or {}).get("activity") or ""
        if verdict is None:
            how = "kept — the check was unavailable" if name else "none — the check was unavailable"
        elif picked and picked != name:
            how = f"switched from {name} by the check" if name else "chosen by the check"
            name = picked
        elif not cur.get("fresh"):
            how = "kept" if name else "none"

    sessions.set_activity(sid, name=name, pinned=pinned, how=how, fresh=False)
    sessions.note_activity(sid, name, how)
    return {"name": name, "how": how, "pinned": pinned}


def current_name(sid: str) -> str:
    """The activity in effect for a session, or "" — what its lessons are selected by."""
    try:
        return sessions.activity(sessions.load(sid)).get("name") or ""
    except Exception:                                              # noqa: BLE001
        return ""


def for_turn(capability: str, sid: str, message: str) -> dict:
    """What a session agent calls, inside its progress sink, before the engine: the
    context to merge, with the header reported as the turn's first step so it is on
    screen while the agent works.

    It does not raise. An activity is guidance for a turn, not a precondition of one, so
    a broken activity file or a store it cannot read says so in the step and the turn
    runs without an activity — it does not fail the person's message."""
    from ...core import progress
    try:
        ctx = prepare(capability, sid, message)
    except Exception as e:                                         # noqa: BLE001
        _logger.warning("activity for session %s unavailable: %s", sid, e)
        progress.emit("activity", f"activity: unavailable ({str(e)[:120]})")
        return {}
    if ctx:
        progress.emit("activity", ctx["activity"].split("\n", 1)[0])
    return ctx


def prepare(capability: str, sid: str, message: str) -> dict:
    """The context block for this turn's engine call, or {} when there is nothing to say.

    Called by each session agent after the person's turn is recorded and before the
    engine is called. The block is merged into the call's context, after the lessons."""
    rec = sessions.load(sid)
    cur = sessions.activity(rec)
    available = store.names(capability)
    if not available and not cur.get("name") and not cur.get("told"):
        return {}

    turn = decide(capability, sid, message)
    name = turn["name"]
    told = sessions.activity(sessions.load(sid)).get("told") or ""

    lines = [sessions.activity_line(turn),
             f"session: {sid}",
             f"activities for {capability}: {', '.join(available) or '–'}",
             f"to change it: finrail do activity.set session={sid} "
             f"name=<activity>|none pin=false"]
    if name != told:
        if told:
            lines += ["", f"The previous activity, {told}, no longer applies. Disregard "
                          f"its instructions."]
        if name:
            act = store.load(capability, name)
            lines += ["", f"Instructions for {name}:", act["instructions"] or "–"]
        sessions.set_activity(sid, told=name)
    return {"activity": "\n".join(lines)}


def judged(capability: str, sid: str, message: str, text: str) -> str:
    """The answer, with the judge's line under it when there is one to say (`judge.py`).
    What a session agent calls on its answer before recording it. Never raises, and with
    `activity.judge` off — the default — returns `text` untouched without asking anyone."""
    from . import judge
    said = judge.after_turn(capability, sid, message, text)
    return f"{text}\n\n{said}" if said else text
