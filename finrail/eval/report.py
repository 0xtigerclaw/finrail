"""REPORT: the tables of `docs/model-comparison.md`, rendered from summaries.

One column per summary, the rows in the order the document uses, so the document can be
regenerated from summary files instead of being edited by hand. The header says where
each column's numbers came from: a live run, the stub, or a transcription of the old
document. Draft cases are named, and a report that holds no live run says so.
"""
import datetime


def _pct(x) -> str:
    return f"{(x or 0) * 100:.0f}%"


def _usd(x, places=5) -> str:
    return "n/a" if x is None else f"${x:.{places}f}"


def _int(x) -> str:
    return "n/a" if x is None else f"{round(x):,}"


def _amount(text: str) -> str:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return text or ""
    return f"{value:,.0f}" if value == int(value) else f"{value:,}"


def _column_title(s: dict) -> str:
    title = s.get("label") or s.get("engine") or "run"
    kind = (s.get("provenance") or {}).get("kind", "run")
    return f"{title} ({kind})" if kind != "run" else title


def results_table(summaries: list) -> str:
    cols = [_column_title(s) for s in summaries]
    lines = ["| | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]

    def row(title, cells):
        lines.append(f"| {title} | " + " | ".join(cells) + " |")

    row("Expected outcome met", [f"**{s['met']} / {s['calls']}** ({_pct(s['pass_rate'])})" for s in summaries])
    row("95% interval (Wilson)", [f"{_pct(s['wilson_95'][0])} to {_pct(s['wilson_95'][1])}" for s in summaries])
    row("Passed the code checks", [f"{s['checks']['answer_passed']} / {s['checks']['answer_calls']}"
                                   for s in summaries])
    if any(s["checks"].get("adversarial_calls") for s in summaries):
        row("Adversarial picks refused", [f"{s['checks']['adversarial_refused']} / {s['checks']['adversarial_calls']}"
                                         for s in summaries])
    row("Refused by the checks", [str(s["checks"].get("answer_refused", 0)) for s in summaries])
    row("Cut off by the reply cap", [str(s["outcomes"].get("cut_off", 0)) for s in summaries])
    row("Wrong figure shipped", [str(s["outcomes"].get("wrong_figure", 0)) for s in summaries])
    row("Latency, median / p90", [f"{s['latency_s']['median']:.1f} s / {s['latency_s']['p90']:.1f} s"
                                  for s in summaries])
    row("Input tokens per call (mean)", [_int(s["tokens"]["input_mean"]) for s in summaries])
    row("Output tokens per call (mean)", [_int(s["tokens"]["output_mean"]) for s in summaries])
    row("Cost per call, list, no caching", [_usd(s["cost_usd"].get("per_call_list")) for s in summaries])
    row("**Cost per checked answer, list**", [f"**{_usd(s['cost_usd'].get('per_checked_answer_list'))}**"
                                              for s in summaries])
    row("Per 1,000 checked answers", [
        "n/a" if s["cost_usd"].get("per_checked_answer_list") is None
        else f"${s['cost_usd']['per_checked_answer_list'] * 1000:.2f}" for s in summaries])
    return "\n".join(lines)


def by_task_table(summaries: list) -> str:
    order, seen = [], set()
    for s in summaries:
        for cid, c in s.get("by_case", {}).items():
            if cid not in seen:
                seen.add(cid)
                order.append((cid, c))
    cols = [_column_title(s) for s in summaries]
    lines = ["| Task | " + " | ".join(cols) + " | Checked amount (USD m) |",
             "|---|" + "---|" * (len(cols) + 1)]
    for cid, first in order:
        cells, amounts = [], []
        for s in summaries:
            c = s.get("by_case", {}).get(cid)
            if not c:
                cells.append("not run")
                continue
            text = f"{c['met']}/{c['runs']} · {c['median_s']:.1f} s"
            cells.append(f"**{text}**" if c["met"] < c["runs"] else text)
            amounts.extend(c.get("amounts") or [])
        expect = first.get("expect") or {}
        if expect.get("outcome") == "refused":
            amount = "refused by design"
        else:
            unique = sorted(set(amounts) or {expect.get("amount", "")})
            amount = ", ".join(_amount(a) for a in unique if a) or "n/a"
        title = first["title"] + (" (draft)" if first.get("draft") else "")
        lines.append(f"| {title} | " + " | ".join(cells) + f" | {amount} |")
    return "\n".join(lines)


def render(summaries: list, title: str = "Evaluation report") -> str:
    out = [f"# {title}", "",
           f"Rendered {datetime.date.today().isoformat()} by `python -m finrail.eval report`.", ""]
    for s in summaries:
        prov = s.get("provenance") or {}
        bits = [f"**{_column_title(s)}**", f"engine `{s.get('engine', '')}`"]
        if s.get("model"):
            bits.append(f"model `{s['model']}`")
        if s.get("max_tokens"):
            bits.append(f"reply cap {s['max_tokens']}")
        bits.append(f"{s.get('cases', len(s.get('by_case', {})))} cases × {s.get('runs_per_case', '?')} runs")
        if s.get("at"):
            bits.append(f"at {s['at']}")
        if s.get("git"):
            bits.append(f"commit {s['git']}")
        out.append("- " + ", ".join(bits) + (f". {prov.get('note')}" if prov.get("note") else ""))
    out += ["", "## Results", "", results_table(summaries), "",
            "Cells show the calls whose expected outcome was met. \"Passed the code checks\" counts the "
            "calls that were meant to produce a figure and did; \"Refused by the checks\" counts the "
            "misses among them, every one of which the harness turned into a refusal.", "",
            "## By task", "", by_task_table(summaries), "",
            "Cells show passes out of runs, then the median seconds. A bold cell has at least one miss.", ""]
    drafts = sorted({cid for s in summaries for cid in s.get("drafts", [])})
    out += ["## Drafts", ""]
    if drafts:
        out.append("These cases are not grounded in a documented figure. They are run and reported, and "
                   "they back no headline claim:")
        out.extend(f"- `{cid}`" for cid in drafts)
    else:
        out.append("No draft cases in these summaries.")
    out += ["", "## Provenance", ""]
    live = [s for s in summaries if (s.get("provenance") or {}).get("kind") == "run"]
    if not live:
        out.append("**No live engine run was made for this report.** Every column is either the stub "
                   "engine answering from a recorded-answers file over synthetic filings, or a "
                   "transcription of `docs/model-comparison.md`. A stub column proves the harness's "
                   "checks and bookkeeping, not what the live filings say.")
    else:
        out.append(f"{len(live)} of {len(summaries)} columns come from a live engine run.")
    return "\n".join(out) + "\n"
