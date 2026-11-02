"""COMPARE: a new summary against a baseline, and a verdict.

The pass rate is a sample; the baseline carries its 95% Wilson interval. A new run whose
pass rate falls below the baseline's lower bound has dropped outside what sampling noise
explains, and that is the regression this exits non-zero on. A wrong figure shipped is a
regression at any rate: the harness's whole promise is that a miss becomes a refusal.
"""
import json
import pathlib

ROWS = (
    ("pass_rate", "Expected outcome met", "rate"),
    ("outcomes.passed", "Passed the code checks", "int"),
    ("outcomes.refused", "Refused by the checks", "int"),
    ("outcomes.cut_off", "Cut off by the reply cap", "int"),
    ("outcomes.wrong_figure", "Wrong figure shipped", "int"),
    ("latency_s.median", "Latency, median (s)", "num"),
    ("latency_s.p90", "Latency, p90 (s)", "num"),
    ("tokens.input_mean", "Input tokens per call (mean)", "num"),
    ("tokens.output_mean", "Output tokens per call (mean)", "num"),
    ("cost_usd.per_call_list", "Cost per call, list (USD)", "usd"),
    ("cost_usd.per_checked_answer_list", "Cost per checked answer, list (USD)", "usd"),
)


def load(path) -> dict:
    doc = json.loads(pathlib.Path(path).read_text())
    if not isinstance(doc, dict) or not str(doc.get("schema", "")).startswith("finrail.eval.summary/"):
        raise ValueError(f"{path}: not an eval summary")
    return doc


def _get(doc: dict, dotted: str):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _fmt(value, kind: str) -> str:
    if value is None:
        return "n/a"
    if kind == "rate":
        return f"{value * 100:.1f}%"
    if kind == "int":
        return f"{int(value)}"
    if kind == "usd":
        return f"${value:.5f}"
    return f"{value:,.1f}"


def compare(new: dict, base: dict) -> dict:
    """{"rows": [...], "regressions": [...], "ok": bool}."""
    rows = []
    same_size = new.get("calls") == base.get("calls")
    for key, title, kind in ROWS:
        a, b = _get(new, key), _get(base, key)
        # A count means nothing against a different number of calls; the rates still do.
        delta = None if a is None or b is None or (kind == "int" and not same_size) else a - b
        rows.append({"key": key, "title": title, "new": a, "base": b, "delta": delta,
                     "text": (title, _fmt(a, kind), _fmt(b, kind),
                              "n/a" if delta is None else (f"{delta * 100:+.1f} pts" if kind == "rate"
                                                           else f"{delta:+,.5f}" if kind == "usd"
                                                           else f"{delta:+,.1f}"))})
    regressions = []
    low = (base.get("wilson_95") or [0.0, 1.0])[0]
    if new.get("pass_rate") is not None and new["pass_rate"] < low:
        regressions.append(f"pass rate {new['pass_rate'] * 100:.1f}% is below the baseline's 95% "
                           f"interval (lower bound {low * 100:.1f}%, baseline "
                           f"{(base.get('pass_rate') or 0) * 100:.1f}%)")
    wrong = (new.get("outcomes") or {}).get("wrong_figure") or 0
    if wrong:
        regressions.append(f"{wrong} wrong figure(s) shipped; the baseline shipped none")
    if new.get("calls") != base.get("calls"):
        rows.append({"key": "calls", "title": "Calls", "new": new.get("calls"), "base": base.get("calls"),
                     "delta": None, "text": ("Calls", str(new.get("calls")), str(base.get("calls")),
                                             "different sample size; rates compared, not counts")})
    return {"rows": rows, "regressions": regressions, "ok": not regressions}


def render(new: dict, base: dict, result: dict) -> str:
    out = [f"new: {new.get('label') or new.get('engine')} ({(new.get('provenance') or {}).get('kind', 'run')}, "
           f"{new.get('calls')} calls)",
           f"baseline: {base.get('label') or base.get('engine')} ({(base.get('provenance') or {}).get('kind', 'run')}, "
           f"{base.get('calls')} calls)", "",
           "| | new | baseline | delta |", "|---|---|---|---|"]
    for row in result["rows"]:
        out.append("| " + " | ".join(row["text"]) + " |")
    out.append("")
    if result["ok"]:
        out.append("No regression: the pass rate is inside the baseline's 95% interval and no wrong figure shipped.")
    else:
        out.append("REGRESSION:")
        out.extend(f"- {r}" for r in result["regressions"])
    return "\n".join(out)
