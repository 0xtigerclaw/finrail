"""THE JUDGE AN ACTIVITY DOES NOT HAVE — its answer, held to the activity's own checks.

A command is verified in code: `verify()` runs after `execute()` and a failure fails the
run. An activity is a prompt, so there is no `execute()` to stand behind, and the only
check on it is the agent's own word. This is the missing half, as far as it can honestly
go: the activity's file lists `checks` — yes/no statements about the ANSWER — and after
the turn each one is put to Jev (see `jev.py`), which answers yes or no and says how sure
it is.

What this is NOT. It reads text. It can say the answer never names a source document; it
cannot say the page was written, the file moved or the total balances. Anything whose
proof is in the world rather than in the answer is a command with a `verify()`, and
stays one. A check here should be a statement a careful reader could settle from the
answer alone.

`activity.judge` decides what happens, and the default is nothing:

    off     nothing runs. An activity with checks behaves as it did without them.
    log     each check is asked and the verdicts are logged. The answer is untouched.
    note    as log, and a failed check the judge is sure of is said under the answer —
            one line the person reads, not a retry and not a block.

WHO JUDGES is `activity.judge_engine`, and it is a cascade by default because that is
what measuring it said (41 labelled answers, 132 checks, 2026-09-21). When Jev was at
least 90% sure it was right on 115 of 116 and raised no false alarm — but it was only
that sure about 18 of 32 real defects. The model caught 29 of 32 and takes ten times as
long. So Jev goes first, and only the checks it is NOT sure of are put to the model, in
one call. On that set the model was needed for a third of the answers.

    cascade   Jev first; the model for the checks Jev is unsure of, or could not be asked
    jev       Jev alone. A check it is unsure of is logged and never said

One request per check, in parallel: the question shape is the one already measured
(`choice`, two criteria), and a slow or failed check costs that check only. A judge that
cannot be reached judges nothing and the turn is unaffected.

What leaves the machine: the person's message, the agent's answer, and the check. An
answer can carry more than a message does, so this is its own setting and is not turned
on by `activity.check_engine`.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor

from . import jev

MODES = ("off", "log", "note")
ENGINES = ("cascade", "jev")
LOG = "activity-judge.jsonl"
MAX_REQUEST = 2000
#: Jev's window is 32k tokens and it is weaker on long input. An answer longer than this
#: is judged on its head and tail, where a report says what it did and what it found.
MAX_ANSWER = 12000

INSTRUCTIONS = (
    "This is a request a person made to an agent, and the agent's final answer. Decide "
    "whether the statement in `check` is true of the answer. Judge only from the text of "
    "the answer: what it actually says, not what the agent probably did. A vague or "
    "implied mention does not count. Is the check satisfied?"
)
CRITERIA = {"yes": "The answer clearly satisfies the check",
            "no": "The answer does not satisfy the check, or it is not clear that it does"}


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n[…]\n" + text[-half:]


def request_body(message: str, answer: str, check: str, model: str = jev.MODEL) -> dict:
    return {"model": model,
            "state": {"request": _clip(message, MAX_REQUEST),
                      "answer": _clip(answer, MAX_ANSWER), "check": check},
            "questions": {"activity": {"type": "choice", "instructions": INSTRUCTIONS,
                                       "criteria": CRITERIA}}}


def judge(message: str, answer: str, checks: list, ask=None, workers: int = 4) -> list:
    """[{"check", "ok": bool|None, "conf": 0..100, "ms"}] — one per check, in order.
    `ok` is None when that check could not be asked; the others still stand."""
    if not checks:
        return []
    ask = ask or jev.asker()

    def one(check):
        started = time.monotonic()
        try:
            ans = ask(request_body(message, answer, check)) or {}
        except Exception as e:                                     # noqa: BLE001
            return {"check": check, "ok": None, "conf": 0, "error": str(e)[:200],
                    "ms": int((time.monotonic() - started) * 1000)}
        choice = str(ans.get("choice") or "").strip().lower()
        try:
            conf = int(round(float(ans.get("confidence") or 0.0) * 100))
        except (TypeError, ValueError):
            conf = 0
        return {"check": check, "ok": {"yes": True, "no": False}.get(choice),
                "conf": max(0, min(100, conf)),
                "ms": int((time.monotonic() - started) * 1000)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, checks))


MODEL_SYSTEM = """You check an agent's answer against a list of statements.

You are given a request a person made, the agent's final answer, and numbered checks. For
each check decide whether it is true of the answer. Judge only from the text of the
answer: what it actually says, not what the agent probably did. A vague or implied
mention does not count.

Return JSON of exactly this shape, one entry per check, in the same order:
{"verdicts": [{"ok": true}, {"ok": false}]}"""

MODEL_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}},
    "required": ["verdicts"]})


def model_judge(message: str, answer: str, checks: list) -> list:
    """[True | False | None] per check, from one model call. None where the reply did not
    cover that check."""
    from ...core import llm
    out = llm.call(system=MODEL_SYSTEM, prompt="Judge the answer against the checks.",
                   context={"request": _clip(message, MAX_REQUEST),
                            "answer": _clip(answer, MAX_ANSWER * 4),
                            "checks": "\n".join(f"{i + 1}. {c}"
                                                for i, c in enumerate(checks))},
                   schema=MODEL_SCHEMA, capability="activity.judge", expect_json=True,
                   timeout=120, wants=("verdicts",))
    got = (out or {}).get("verdicts") or []
    verdicts = []
    for i in range(len(checks)):
        v = got[i] if i < len(got) else None
        if isinstance(v, dict):                  # the schema is a hint to the engine, not
            v = v.get("ok", v.get("verdict"))    # a guarantee: accept the near misses
        verdicts.append(v if isinstance(v, bool) else None)
    return verdicts


def cascade(message: str, answer: str, checks: list, min_conf: int = jev.MIN_CONF,
            ask=None, model=None) -> list:
    """Jev first, the model for what Jev is not sure of. Same rows as `judge`, and a row
    the model settled carries `by: "model"` and keeps Jev's own reading under `jev`.

    Each half may fail without the other: Jev unreachable means every check goes to the
    model, and the model unreachable leaves Jev's verdicts as they were."""
    try:
        verdicts = judge(message, answer, checks, ask=ask)
    except Exception as e:                       # noqa: BLE001 — no key, no network
        verdicts = [{"check": c, "ok": None, "conf": 0, "error": str(e)[:200], "ms": 0}
                    for c in checks]
    unsure = [i for i, v in enumerate(verdicts)
              if v.get("ok") is None or v.get("conf", 0) < min_conf]
    if not unsure:
        return verdicts
    started = time.monotonic()
    try:
        second = (model or model_judge)(message, answer, [checks[i] for i in unsure])
    except Exception as e:                                         # noqa: BLE001
        for i in unsure:
            verdicts[i]["model_error"] = str(e)[:200]
        return verdicts
    ms = int((time.monotonic() - started) * 1000)
    for i, ok in zip(unsure, second):
        if ok is None:
            continue
        v = verdicts[i]
        verdicts[i] = {"check": v["check"], "ok": ok, "conf": v.get("conf", 0),
                       "by": "model", "ms": v.get("ms", 0) + ms,
                       "jev": {"ok": v.get("ok"), "conf": v.get("conf", 0)}}
    return verdicts


def _decided(v: dict, min_conf: int) -> bool:
    """A verdict to act on: the model's, or Jev's when it is sure enough."""
    return v.get("by") == "model" or v.get("conf", 0) >= min_conf


def failed(verdicts: list, min_conf: int = jev.MIN_CONF) -> list:
    """The checks that failed AND were decided. Only these are ever said."""
    return [v for v in verdicts if v.get("ok") is False and _decided(v, min_conf)]


def note(verdicts: list, min_conf: int = jev.MIN_CONF) -> str:
    bad = failed(verdicts, min_conf)
    if not bad:
        return ""
    lines = [f"checks for this activity: {len(bad)} of {len(verdicts)} not met"]
    for v in bad:
        how = "the model's reading" if v.get("by") == "model" else f"{v['conf']}% sure"
        lines.append(f"  - not shown in the answer: {v['check']} ({how})")
    return "\n".join(lines)


def mode() -> str:
    from . import turn
    v = str(turn._setting("activity.judge", "off"))
    return v if v in MODES else "off"


def engine() -> str:
    from . import turn
    v = str(turn._setting("activity.judge_engine", "cascade"))
    return v if v in ENGINES else "cascade"


def after_turn(capability: str, sid: str, message: str, answer: str) -> str:
    """What a session agent calls once it has its answer. Returns the line to put under
    the answer, which is "" in every mode but `note` and "" there too when nothing the
    judge is sure of failed. It does not raise: judging is a reading of a turn that has
    already happened, and cannot be allowed to lose it."""
    try:
        how = mode()
        if how == "off" or not (answer or "").strip():
            return ""
        from ...core import sessions
        from . import store, turn
        name = sessions.activity(sessions.load(sid)).get("name") or ""
        if not name or not store.exists(capability, name):
            return ""
        checks = store.load(capability, name).get("checks") or []
        if not checks:
            return ""
        min_conf = int(turn._setting("activity.jev_min_conf", jev.MIN_CONF))
        who = engine()
        verdicts = (cascade(message, answer, checks, min_conf) if who == "cascade"
                    else judge(message, answer, checks))
        _record({"capability": capability, "session": sid, "activity": name, "mode": how,
                 "engine": who,
                 "min_conf": min_conf, "message": (message or "")[:300],
                 "verdicts": verdicts})
        return note(verdicts, min_conf) if how == "note" else ""
    except Exception:                                              # noqa: BLE001
        from ...core import log
        log.get(__name__).debug("activity judge unavailable", exc_info=True)
        return ""


def _record(row: dict) -> None:
    try:
        from ...core.settings import logs
        logs().mkdir(parents=True, exist_ok=True)
        with open(logs() / LOG, "a") as f:
            f.write(json.dumps({"ts": int(time.time()), **row}) + "\n")
    except Exception:                                              # noqa: BLE001
        pass


# ── measuring it ────────────────────────────────────────────────────────────

def score(rows: list, min_conf: int = jev.MIN_CONF) -> dict:
    """Over labelled judgments: rows of {"expect": bool, "ok": bool|None, "conf"} where
    `expect` is whether the check truly holds. A DEFECT is a check that does not hold;
    catching one means saying no. Two numbers matter and they pull against each other:
    defects caught, and clean checks wrongly flagged."""
    asked = [{**r, "conf": 100} if r.get("by") == "model" else r
             for r in rows if r.get("ok") is not None]
    sure = [r for r in asked if r["conf"] >= min_conf]
    defects = [r for r in asked if r["expect"] is False]
    clean = [r for r in asked if r["expect"] is True]
    ms = sorted(r.get("ms", 0) for r in asked)
    return {"judgments": len(rows), "asked": len(asked), "min_conf": min_conf,
            "right": sum(1 for r in asked if r["ok"] == r["expect"]),
            "sure": len(sure), "sure_right": sum(1 for r in sure if r["ok"] == r["expect"]),
            "defects": len(defects),
            "caught": sum(1 for r in defects if r["ok"] is False),
            "caught_sure": sum(1 for r in defects if r["ok"] is False and r["conf"] >= min_conf),
            "missed_sure": sum(1 for r in defects if r["ok"] is True and r["conf"] >= min_conf),
            "clean": len(clean),
            "false_alarms": sum(1 for r in clean if r["ok"] is False),
            "false_alarms_sure": sum(1 for r in clean
                                     if r["ok"] is False and r["conf"] >= min_conf),
            "ms": ms[len(ms) // 2] if ms else 0}


def render(s: dict, who: str = "Jev") -> str:
    if not s["asked"]:
        return f"{who}: nothing was judged."
    return "\n".join([
        f"**{who}** — {s['asked']} judgments ({s['defects']} planted defects, "
        f"{s['clean']} checks that truly hold)",
        f"- right on {s['right']} of {s['asked']}",
        f"- at least {s['min_conf']}% sure on {s['sure']}: right on {s['sure_right']} of "
        f"{s['sure']}",
        f"- defects caught: {s['caught']} of {s['defects']} "
        f"({s['caught_sure']} of them said with confidence — these are what a person sees)",
        f"- defects waved through with confidence: {s['missed_sure']}",
        f"- false alarms: {s['false_alarms']} of {s['clean']} clean checks "
        f"({s['false_alarms_sure']} with confidence — these are what a person sees)",
        f"- median time per check: {s['ms']} ms"])
