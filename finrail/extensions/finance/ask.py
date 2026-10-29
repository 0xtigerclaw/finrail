"""Route an investor's question to one declared workflow. Never answer it.

Jev (TypeSafe) is asked typed choices: which workflow, which report, one quarter or two. It can
only pick from what it is offered and says how sure it is, so it cannot invent a workflow,
a report or a number. Below the confidence bar nothing runs; the person is asked to pick.
Without a Jev key the same choices are made by plain keyword rules, and the result says so.
"""
from __future__ import annotations

import re
import time

from finrail.core import decide
from finrail.core.errors import ContractViolation
from finrail.plugin import Plugin

NONE = "none"
NONE_TEXT = "Something no workflow above covers: advice, forecasts, opinions, other companies"
NAMED = ("nvidia", "nvda", "nebius", "asml", "northwind")


def workflows() -> dict:
    """{capability name: how a person asks for it}, read from the registry. A capability
    that declares no `ask` is never offered, so the router cannot reach it."""
    from finrail.core import registry
    registry.load()
    offered = {c.name: c.ask for c in sorted(registry.commands().values(), key=lambda c: c.name) if c.ask}
    return {**offered, NONE: NONE_TEXT}
REPORTS = {
    "NBIS-Q1-2026": "Nebius Q1 2026", "NBIS-Q2-2026": "Nebius Q2 2026, its latest quarter",
    "NVDA-Q1-FY2027": "NVIDIA Q1 FY2027", "NVDA-Q2-FY2027": "NVIDIA Q2 FY2027, its latest quarter",
    "ASML-Q2-2026": "ASML Q2 2026", "ASML-Q3-2026": "ASML Q3 2026, its latest quarter",
}
SPAN = {"one": "One quarter", "both": "Both quarters, compared"}
#: Set by hand for this router on ten sample investor questions (29 Oct), not measured
#: like decide.MIN_CONF. Below it nothing runs and the person picks.
ROUTE_MIN = 75
INSTRUCTIONS = (
    "An investment professional asked this question in a chat. Pick the one checked workflow "
    "that answers it. A short follow-up such as 'and Q1?' or 'compare that' continues the "
    "previous workflow shown in the state. Pick none when no workflow answers it.")




def _body(question: str, previous: dict) -> dict:
    # A question that names a company stands alone; only a bare follow-up ("and Q1?")
    # inherits the previous turn. Otherwise the last topic drags the pick.
    prev = {} if any(n in question.lower() for n in NAMED) else (previous or {})
    return {"state": {"question": question[:1500],
                      "previous_workflow": prev.get("workflow") or "-",
                      "previous_report": prev.get("report_id") or "-"},
            "questions": {
                "workflow": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": workflows()},
                "report": {"type": "choice", "criteria": REPORTS,
                           "instructions": "Which company report is the question about? "
                                           "'Last quarter' or 'latest' means that company's latest quarter. "
                                           "For a follow-up without a company, keep the previous report."},
                "span": {"type": "choice", "criteria": SPAN,
                         "instructions": "Does the question ask about one quarter, or compare two quarters "
                                         "(compare, versus, change, trend, the quarter before)?"}}}


def _keywords(question: str, previous: dict) -> dict:
    """The fallback router: plain rules, used only when Jev cannot be reached."""
    q = question.lower()
    prev = previous or {}
    company = ("NVDA" if "nvidia" in q else "NBIS" if "nebius" in q else "ASML" if "asml" in q
               else (prev.get("report_id") or "NVDA-Q2-FY2027").split("-")[0])
    latest = {"NVDA": "NVDA-Q2-FY2027", "NBIS": "NBIS-Q2-2026", "ASML": "ASML-Q3-2026"}[company]
    quarter = re.search(r"\bq([1-4])\b", q)
    report_id = next((r for r in REPORTS if r.startswith(company) and quarter and f"-Q{quarter.group(1)}-" in r), latest)
    fcf = "finance.reports.analyze"
    if any(w in q for w in ("record", "write down", "log ")) and re.search(r"\d", q):
        workflow = "finance.reports.record"
    elif "northwind" in q or "red flag" in q or "risk" in q:
        workflow = "finance.screen.fixture"
    elif "free cash" in q or "fcf" in q or (prev.get("workflow") == fcf and quarter):
        workflow = fcf
    elif company and any(w in q for w in ("say", "said", "capex", "guidance", "revenue", "cash", "margin", "what")):
        workflow = "finance.reports.search"
    else:
        workflow = NONE
    span = "both" if any(w in q for w in ("compare", " vs", "versus", "change", "trend")) else "one"
    if span == "both" and prev.get("workflow") == fcf:
        workflow = fcf
    return {"workflow": workflow, "report_id": report_id, "span": span}


def route(question: str, previous: dict | None = None, ask=None) -> dict:
    question = (question or "").strip()
    if not 3 <= len(question) <= 1500:
        raise ContractViolation("Ask a question between 3 and 1,500 characters.")
    started = time.monotonic()
    try:
        answers = (ask or decide.asker(whole=True, timeout=15, capability="finance.ask.route"))(_body(question, previous or {}))
        conf = lambda name: decide.confidence(answers.get(name))  # noqa: E731
        pick = {"workflow": answers["workflow"]["choice"], "report_id": answers["report"]["choice"],
                "span": answers["span"]["choice"]}
        if (pick["workflow"] not in workflows() or pick["report_id"] not in REPORTS
                or pick["span"] not in SPAN):
            raise decide.JevError("answer outside the offered choices")
        probs = {k: round(float(v), 3) for k, v in (answers["workflow"].get("probabilities") or {}).items()}
        out = {**pick, "router": "jev", "router_model": decide.MODEL, "event": answers.get("_event", 0),
               "confidence": conf("workflow"),
               "report_confidence": conf("report"), "probabilities": probs}
    except (decide.JevError, KeyError, TypeError, ValueError) as exc:
        out = {**_keywords(question, previous or {}), "router": "keywords", "confidence": None,
               "router_note": f"Jev unavailable ({str(exc)[:80]}); plain keyword rules routed this."}
    out["sure"] = out["router"] == "keywords" or out["confidence"] >= ROUTE_MIN
    out["min_conf"] = ROUTE_MIN
    out["ms"] = int((time.monotonic() - started) * 1000)
    return out


class FinanceRoute(Plugin):
    name = "finance.ask.route"
    description = "Pick which declared workflow answers an investor's question. Picks only; never answers."
    read_only = True
    confirm = False
    verified_by = "the pick must be one of the offered workflows, or nothing runs"
    steps = (
        {"name": "Offer", "by": "Kernel", "kind": "code", "where": "ask.workflows()",
         "rule": "Offers only the capabilities that declare ask, read from the registry. Nothing else is reachable."},
        {"name": "Pick", "by": "Jev (TypeSafe)", "kind": "model", "where": "ask.route()",
         "rule": "Picks one workflow, one report and one or two quarters, each with a confidence. Cannot write."},
        {"name": "Bar", "by": "Kernel", "kind": "check", "where": "ask.route()",
         "rule": f"Below {ROUTE_MIN}% sure nothing runs and the person picks. A pick outside the choices is discarded."},
    )

    def run(self, question: str = "", previous_workflow: str = "", previous_report: str = ""):
        return route(question, {"workflow": previous_workflow, "report_id": previous_report})
