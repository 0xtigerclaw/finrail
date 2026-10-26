"""THE SAME CHECK, FROM A MODEL THAT CANNOT WRITE — and says how sure it is.

`turn.check` asks a full model, every turn, to pick one name from a short list. That is a
classification, and Jev (TypeSafe) does nothing else:
it returns a choice and a confidence, never text. Reached at TypeSafe's own endpoint, or through OpenRouter's Decisions API when that is the key the machine has. Two things follow. It cannot invent a
name, because it can only pick from what it was given. And the check's bias to KEEP stops
being a sentence in a prompt and becomes a number: the activity switches only when the
pick is a different activity AND the confidence clears the bar.

Off unless `activity.check_engine` says otherwise, and three values only:

    model    the check as it was. Nothing here runs.
    shadow   the model decides; Jev is asked as well and both answers are logged. For
             measuring it against the check it would replace, on real turns, at no risk.
    jev      Jev decides when it is sure; below the bar, or when it cannot be reached,
             the model is asked exactly as before. A turn never fails on this.

What leaves the machine is the one message, the activity in effect, and the names and
one-line descriptions of the activities. Never the transcript, never the instructions.

Standard library only, and the caller is injectable (`ask`), so tests touch no network.
"""
import time

#: TWO DOORS TO THE SAME MODEL, ONE REQUEST SHAPE. TypeSafe's own endpoint and
#: OpenRouter's Decisions API take the same body (model, state, questions) and return the
#: same answers; only the address, the model id and the key differ. The direct door is
#: the default: the key comes from TypeSafe's console and nothing sits in between.
#: OpenRouter is kept for a machine that has only that key.
#: The door (address, model, key, caller) lives in core/decide since 8 Oct, so the platform
#: can ask the same model without importing this plugin. These names stay for this plugin's
#: callers and tests.
from ...core.decide import (JevError, MODEL, MIN_CONF, OPENROUTER_ENV_KEY,  # noqa: F401
                            OPENROUTER_MODEL, OPENROUTER_SECRET, OPENROUTER_URL, SECRET,
                            ENV_KEY, URL, asker, decides, door, http_ask, key)

#: The choice that means "no listed activity". `store.RESERVED` keeps an activity from
#: ever being called this, so it cannot collide with a real one.
NONE = "none"
#: A message is a sentence or a paragraph. A pasted log is not what decides an activity,
#: and the model this goes to is weak on long noisy input.
MAX_MESSAGE = 4000

INSTRUCTIONS = (
    "This is one message a person sent to an agent, with the activity currently in effect "
    "for the conversation (a dash when there is none). An activity is a named kind of work. "
    "If the activity in effect still fits, choose it: short follow-ups such as 'ok', "
    "'do it', 'check again', or a question about what was just said, continue the activity "
    "in effect. If the message has clearly moved on to a different activity, choose that "
    "one. If no activity fits, choose none. Which activity is this message part of?"
)


def request_body(message: str, current: str, rows: list, model: str = MODEL) -> dict:
    criteria = {r["name"]: (r.get("description") or r["name"]) for r in rows}
    criteria[NONE] = "No listed activity fits this message"
    return {"model": model,
            "state": {"message": (message or "")[:MAX_MESSAGE],
                      "activity_in_effect": current or "-"},
            "questions": {"activity": {"type": "choice", "instructions": INSTRUCTIONS,
                                       "criteria": criteria}}}


def pick(message: str, current: str, rows: list, ask=None) -> dict:
    """{"activity": name or "", "named": raw choice, "conf": 0..100, "ms": int}.

    `activity` is "" for the none choice and for anything that is not on the list — the
    second cannot happen with a choice question, and is held to anyway."""
    ask = ask or asker()
    started = time.monotonic()
    ans = ask(request_body(message, current, rows)) or {}
    ms = int((time.monotonic() - started) * 1000)
    named = str(ans.get("choice") or "").strip()
    try:
        conf = int(round(float(ans.get("confidence") or 0.0) * 100))
    except (TypeError, ValueError):
        conf = 0
    known = {r["name"] for r in rows}
    return {"activity": named if named in known else "", "named": named,
            "conf": max(0, min(100, conf)), "ms": ms}


# ── measuring it against the model ──────────────────────────────────────────

def _median(xs: list) -> int:
    xs = sorted(xs)
    return int(xs[len(xs) // 2]) if xs else 0


def _outcome(activity: str, row: dict) -> str:
    """What the session ends up with. `decide` keeps the activity in effect on an empty
    answer, so "" and the current name are the same outcome and are scored as one."""
    return activity or (row.get("current") or "")


def score(rows: list, min_conf: int = MIN_CONF) -> dict:
    """The scorecard over check-log rows (or replay rows — same shape).

    Only rows where BOTH answered can be compared. `sure` is the subset Jev would have
    decided alone; agreement there is the number that matters, and `coverage` is how
    often that is. A row may carry `expect` (a replay case labelled by a person), and
    then each engine is also scored against it."""
    def _same(r):
        return _outcome(r["jev"]["activity"], r) == _outcome(r["model"]["activity"], r)

    def _right(r, who):
        return _outcome(r[who]["activity"], r) == _outcome(r["expect"], r)

    both = [r for r in rows if r.get("jev") and r.get("model")]
    sure = [r for r in both if r["jev"]["conf"] >= min_conf]

    def agree(rs):
        return sum(1 for r in rs if _same(r))

    labelled = [r for r in both if "expect" in r]
    out = {"rows": len(rows), "compared": len(both), "agree": agree(both),
           "sure": len(sure), "sure_agree": agree(sure), "min_conf": min_conf,
           "coverage": round(len(sure) / len(both), 3) if both else 0.0,
           "jev_alone": sum(1 for r in rows if r.get("used") == "jev"),
           "jev_errors": sum(1 for r in rows if r.get("jev_error")),
           "model_ms": _median([r["model"]["ms"] for r in both]),
           "jev_ms": _median([r["jev"]["ms"] for r in both]),
           "differ": sorted((r for r in sure if not _same(r)),
                            key=lambda r: -r["jev"]["conf"])}
    if labelled:
        sure_l = [r for r in labelled if r["jev"]["conf"] >= min_conf]
        out.update({
            "labelled": len(labelled),
            "model_right": sum(1 for r in labelled if _right(r, "model")),
            "jev_right": sum(1 for r in labelled if _right(r, "jev")),
            "sure_labelled": len(sure_l),
            "jev_sure_right": sum(1 for r in sure_l if _right(r, "jev")),
            # A wrong SWITCH is the costly error: the person's conversation is handed a
            # different set of instructions. Counted apart from the rest.
            "jev_wrong_switches": sum(
                1 for r in sure_l
                if not _right(r, "jev")
                and r["jev"]["activity"] not in ("", r.get("current") or ""))})
    return out


def _cell(s) -> str:
    return str(s or "–").replace("|", "/").replace("\n", " ")[:80]


def render(s: dict) -> str:
    if not s["compared"]:
        return (f"{s['rows']} checks logged, none with both answers — set "
                f"activity.check_engine=shadow and let some turns through."
                + (f" Jev failed on {s['jev_errors']}." if s["jev_errors"] else ""))
    lines = [f"**Jev against the model check** — {s['compared']} turns where both answered",
             "",
             f"- agree on {s['agree']} of {s['compared']}",
             f"- Jev at least {s['min_conf']}% sure on {s['sure']} "
             f"({s['coverage']:.0%} of turns): agree on {s['sure_agree']} of {s['sure']}",
             f"- median time: Jev {s['jev_ms']} ms, model {s['model_ms']} ms"]
    if s.get("labelled"):
        lines += [f"- against {s['labelled']} labelled cases: model right on "
                  f"{s['model_right']}, Jev right on {s['jev_right']}; where Jev was sure, "
                  f"right on {s['jev_sure_right']} of {s['sure_labelled']}",
                  f"- wrong switches by Jev when sure: {s['jev_wrong_switches']}"]
    if s["jev_alone"]:
        lines.append(f"- decided by Jev alone (engine=jev): {s['jev_alone']}")
    if s["jev_errors"]:
        lines.append(f"- Jev unavailable on {s['jev_errors']}; the model answered those")
    if s["differ"]:
        lines += ["", "Where Jev was sure and they differ — a person decides who was right:",
                  "", "| Message | In effect | Model | Jev | Sure |", "|---|---|---|---|---|"]
        for r in s["differ"][:20]:
            lines.append(f"| {_cell(r.get('message'))} | {_cell(r.get('current'))} | "
                         f"{_cell(r['model']['activity'])} | {_cell(r['jev']['activity'])} | "
                         f"{r['jev']['conf']}% |")
    return "\n".join(lines)
