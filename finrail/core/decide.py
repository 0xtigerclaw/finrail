"""THE DECISION MODEL'S DOOR: a model that returns a choice and a confidence, never text.

Jev (TypeSafe) answers typed questions about a state: pick one of these, score on this
scale, yes or no. It cannot invent an answer that was not offered, and it says how sure
it is. That makes it the right engine for the platform's own judgements (which activity a
message belongs to, whether an answer meets a check, whether a rule shown to a turn was
honoured) and the wrong one for anything that needs prose.

This module is the door only: where the model is, which key opens it, how a request is
sent and an answer read back. The questions live with whoever asks them (`bundled/activity`
for the per-turn check and the answer judge, `bundled/memory/check` for the rule check).
It sits in core so the platform can ask without importing a runtime plugin (LAYERS.md,
rule 4); moved here from `bundled/activity/jev.py` on 8 Oct, which keeps its name for the
activity plugin's own questions.

TWO DOORS TO THE SAME MODEL, ONE REQUEST SHAPE. TypeSafe's own endpoint and OpenRouter's
Decisions API take the same body (model, state, questions) and return the same answers;
only the address, the model id and the key differ. The direct door is the default: the key
comes from TypeSafe's console and nothing sits in between. OpenRouter is kept for a machine
that has only that key.

Standard library only, and every caller is injectable (`ask`), so tests touch no network.
"""
import json
import os
import time
import urllib.request

URL = "https://api.typesafe.ai/v1/systemone"
OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
#: Pinned on purpose: a confidence bar is measured on one version. An alias moves when a
#: release ships and takes the calibration with it.
MODEL = "jev-1.13.0"
OPENROUTER_MODEL = "typesafe/jev-1.13"
#: Per cent, because settings have no float kind. Measured 21 Sept on the activity
#: check and judge: at 90 the model was right on 115 of 116 and raised no false alarm.
#: Its confidence is a spread score, not a calibrated probability, and jitters a few
#: points between identical calls: a bar is a margin, not a line.
MIN_CONF = 90
SECRET = "activity.typesafe_key"
ENV_KEY = "TYPESAFE_API_KEY"
OPENROUTER_SECRET = "activity.openrouter_key"
OPENROUTER_ENV_KEY = "OPENROUTER_API_KEY"


class JevError(Exception):
    pass


def http_ask(key: str, url: str = URL, timeout: int = 15, tries: int = 2,
             whole: bool = False):
    """The real caller: body in, the answer out. With `whole`, every answer the body asked
    for, keyed as the questions were; without, the one answer to the body's one question,
    whatever it was named. Short timeout and few tries: this sits in front of a person's
    turn, and the generation model is right behind it."""
    def ask(body: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        name = next(iter(body.get("questions") or {"activity": None}))
        last = None
        for attempt in range(tries):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    answers = json.load(r)["answers"]
                    return answers if whole else answers[name]
            except Exception as exc:       # noqa: BLE001 — network, 429, a changed shape
                last = exc
                time.sleep(0.5 * (attempt + 1))
        raise JevError(f"Jev did not answer: {last}")
    return ask


def door() -> dict:
    """{"key", "url", "model"}: which door this machine can use. The TypeSafe key is
    looked for first, in the secret store and then the environment (a replay run from a
    person's shell); the OpenRouter key second, the same way. Raises JevError when there
    is neither, which the caller treats like any other way of Jev being unavailable."""
    for secret, env, url, model in ((SECRET, ENV_KEY, URL, MODEL),
                                    (OPENROUTER_SECRET, OPENROUTER_ENV_KEY,
                                     OPENROUTER_URL, OPENROUTER_MODEL)):
        found = ""
        try:
            from . import secrets
            if secrets.exists(secret):
                found = secrets.get(secret)
        except Exception:                                          # noqa: BLE001
            pass
        found = (found or os.environ.get(env, "")).strip()
        if found:
            return {"key": found, "url": url, "model": model}
    raise JevError(f"no TypeSafe key — finrail do secret.set name={SECRET} "
                   f"prompt=true, or {ENV_KEY} in the environment")


def key() -> str:
    """The key of whichever door is open. Kept for callers that only want the key."""
    return door()["key"]


def asker(timeout: int = 15, tries: int = 2, whole: bool = False):
    """The real caller through whichever door is open, with the body's model set to
    that door's id, so a body built for one door is sent correctly through the other."""
    d = door()
    ask = http_ask(d["key"], url=d["url"], timeout=timeout, tries=tries, whole=whole)

    def through(body: dict) -> dict:
        return ask({**body, "model": d["model"]})
    return through


def confidence(answer: dict) -> int:
    """An answer's confidence as a whole per cent, 0 when it has none."""
    try:
        return max(0, min(100, int(round(float((answer or {}).get("confidence") or 0.0) * 100))))
    except (TypeError, ValueError):
        return 0


def decides(verdict: dict, min_conf: int = MIN_CONF) -> bool:
    """Whether a verdict ({"conf": per cent}) is one to act on alone."""
    return bool(verdict) and int(verdict.get("conf") or 0) >= int(min_conf)
