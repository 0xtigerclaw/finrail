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
NONE_TEXT = ("Something no workflow above covers: advice, forecasts of periods not yet reported, opinions, or a company other than "
             "NVIDIA, Nebius, ASML and Anthropic")
NAMED = ("nvidia", "nvda", "nebius", "asml", "northwind", "anthropic", "s-1")


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
    "ANTH-S1-2026": "Anthropic's S-1 registration statement (filed 26 October 2026), covering the six months ended 30 June 2026 "
                    "and fiscal 2023 to 2025; its only filing here",
}
SPAN = {"one": "One quarter", "both": "Both quarters, compared"}
#: Which single figure a question asks for. Only the analyze workflow uses it; a topic
#: question ("what does the S-1 say about compute") is none.
FIGURES = {
    "fcf": "Free cash flow (FCF) or cash generation",
    "operating_cash_flow": "Operating cash flow, cash from operations",
    "capex": "Capital expenditure, purchases of property and equipment",
    "revenue": "Revenue or sales",
    "net_loss": "Net loss or net income, the bottom line",
    "made_money": "Whether the company made money, was profitable, or lost money overall",
    "house_operating_income": "The operating result under the fund's own method (its method rules)",
    "adjusted_operating_income": "Adjusted operating income with stock compensation added back, as the company presents it",
    "none": "No single figure: a topic, a sentence to record, a valuation multiple, or anything else",
}
#: Set by hand for this router on ten sample investor questions (29 Oct), not measured
#: like decide.MIN_CONF. Below it nothing runs and the person picks.
ROUTE_MIN = 75
#: Workflows that run on one report, and the one that also reads the span.
NEEDS_REPORT = ("finance.reports.analyze", "finance.reports.search", "finance.reports.record",
                "finance.reports.webclaims")
SPAN_MATTERS = ("finance.reports.analyze",)
INSTRUCTIONS = (
    "An investment professional asked this question in a chat. Pick the one checked workflow "
    "that answers it. A short follow-up such as 'and Q1?' or 'compare that' continues the "
    "previous workflow shown in the state. Every filing offered is already published, so a figure "
    "for a 2026 period it covers is reported history, not a forecast. Pick none when no workflow answers it.")




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
                                         "(compare, versus, change, trend, the quarter before)?"},
                "figure": {"type": "choice", "criteria": FIGURES,
                           "instructions": "Which single figure does the question ask for? Pick none when it asks "
                                           "what a filing says about a topic, states a figure to record, or asks "
                                           "for a multiple at a valuation."}}}


#: Capitalised words a question about the covered companies may use. Any other one (a
#: "Microsoft", a "Tesla") stops the keyword router from guessing a company.
_PLAIN = set("""what how does do did is was were has have any and or but record compare show tell give find
check please can could would should which who why where when the a an in on for of to about vs versus last
latest same now then also ok okay write log put free cash flow fcf ocf capex revenue gaap usd us ceo cfo eps
ai gpu ttm yoy qoq i we our their its it that this web press analysts say said""".split())
_PERIOD_TOKEN = re.compile(r"^(q[1-4]|h[12]|fy\d{2,4}|\d.*)$", re.I)


def _unknown_name(question: str) -> str:
    for word in re.findall(r"\b[A-Z][A-Za-z0-9&.\-]*", question):
        w = re.sub(r"['’]s$", "", word).rstrip(".").lower()
        if w not in _PLAIN and w not in NAMED and not _PERIOD_TOKEN.match(w):
            return word
    return ""


def _period_problem(question: str, report_id: str, span: str) -> str:
    """Code's check that the quarter and year a question names are on file for the picked
    company. A question about a period Finrail has no filing for is never run on another."""
    company = report_id.split("-")[0]
    on_file = [r for r in REPORTS if r.startswith(company + "-")]
    quarters = {r.split("-")[1] for r in on_file}
    year = report_id.split("-")[2].replace("FY", "")
    q = question.lower()
    named_q = {f"Q{n}" for n in re.findall(r"\bq([1-4])\b", q)}
    named_q |= {f"Q{i}" for i, w in enumerate(("first", "second", "third", "fourth"), 1) if f"{w} quarter" in q}
    fiscal = re.findall(r"\b(?:fy\s*'?|fiscal\s+(?:year\s+)?)(\d{4}|\d{2})\b", q)
    years = {y if len(y) == 4 else "20" + y for y in fiscal}
    years |= set(re.findall(r"\b(?:19|20)\d{2}\b", re.sub(r"\b(?:fy\s*'?|fiscal\s+(?:year\s+)?)(\d{4}|\d{2})\b", " ", q)))
    period = ", ".join(REPORTS[r].split(",")[0] for r in on_file)
    if named_q - quarters or years - {year}:
        return f"It names a period Finrail has no filing for. On file: {period}."
    if span == "one" and named_q and report_id.split("-")[1] not in named_q:
        return f"It names {', '.join(sorted(named_q))} but the pick was {REPORTS[report_id].split(',')[0]}."
    return ""


def _keywords(question: str, previous: dict) -> dict:
    """The fallback router: plain rules, used only when Jev cannot be reached. It runs only
    when the question names a covered company (or follows one) and a period on file."""
    q = question.lower()
    prev = previous or {}
    named = ("NVDA" if "nvidia" in q or "nvda" in q else "NBIS" if "nebius" in q else "ASML" if "asml" in q
             else "ANTH" if "anthropic" in q or "s-1" in q else "")
    stranger = _unknown_name(question)
    company = named or ("" if stranger else (prev.get("report_id") or "").split("-")[0])
    latest = {"NVDA": "NVDA-Q2-FY2027", "NBIS": "NBIS-Q2-2026", "ASML": "ASML-Q3-2026",
              "ANTH": "ANTH-S1-2026"}.get(company, "NVDA-Q2-FY2027")
    quarter = re.search(r"\bq([1-4])\b", q)
    report_id = next((r for r in REPORTS if company and r.startswith(company) and quarter
                      and f"-Q{quarter.group(1)}-" in r), latest)
    figure = ("made_money" if any(w in q for w in ("make money", "made money", "profitable", "lose money", "lost money")) else
              "adjusted_operating_income" if "add back" in q or "adjusted operating" in q else
              "house_operating_income" if "house" in q or "method" in q else
              "fcf" if "free cash" in q or "fcf" in q else
              "operating_cash_flow" if "operating cash" in q or "cash from operations" in q else
              "capex" if "capex" in q or "capital expenditure" in q or "purchases of property" in q else
              "revenue" if "revenue" in q or "sales" in q else
              "net_loss" if "net loss" in q or "net income" in q or "bottom line" in q else "none")
    fcf = "finance.reports.analyze"
    if any(w in q for w in ("record", "write down", "log ")) and re.search(r"\d", q):
        workflow = "finance.reports.record"
    elif "northwind" in q or "red flag" in q or "risk" in q:
        workflow = "finance.screen.fixture"
    elif any(w in q for w in ("the web", "the press", "analysts say", "online", "media say", "news say")):
        workflow = "finance.reports.webclaims"
    elif any(w in q for w in ("multiple", "price-to-sales", "price to sales", "p/s")) or ("valuation" in q and re.search(r"\$\s?\d", q)):
        workflow, figure = "finance.reports.multiple", "none"
    elif figure == "fcf" or (prev.get("workflow") == fcf and quarter):
        workflow = fcf
    elif company == "ANTH" and figure != "none" and not any(w in q for w in ("say", "said", "about")):
        workflow = fcf
    elif any(w in q for w in ("say", "said", "capex", "guidance", "revenue", "cash", "margin", "what")):
        workflow = "finance.reports.search"
    else:
        workflow = NONE
    span = "both" if any(w in q for w in ("compare", " vs", "versus", "change", "trend")) else "one"
    if span == "both" and prev.get("workflow") == fcf:
        workflow = fcf
    unsure = ""
    if workflow in NEEDS_REPORT:
        if not company:
            unsure = (f"It names {stranger}, " if stranger else "It names no company, ") + \
                     "and Finrail has filings for NVIDIA, Nebius, ASML and Anthropic only."
        else:
            unsure = _period_problem(question, report_id, span)
    return {"workflow": workflow, "report_id": report_id, "span": span, "unsure": unsure, "figure": figure}


def route(question: str, previous: dict | None = None, ask=None) -> dict:
    question = (question or "").strip()
    if not 3 <= len(question) <= 1500:
        raise ContractViolation("Ask a question between 3 and 1,500 characters.")
    started = time.monotonic()
    try:
        answers = (ask or decide.asker(whole=True, timeout=15, capability="finance.ask.route"))(_body(question, previous or {}))
        conf = lambda name: decide.confidence(answers.get(name))  # noqa: E731
        pick = {"workflow": answers["workflow"]["choice"], "report_id": answers["report"]["choice"],
                "span": answers["span"]["choice"], "figure": (answers.get("figure") or {}).get("choice", "none")}
        if (pick["workflow"] not in workflows() or pick["report_id"] not in REPORTS
                or pick["span"] not in SPAN or pick["figure"] not in FIGURES):
            raise decide.JevError("answer outside the offered choices")
        probs = {k: round(float(v), 3) for k, v in (answers["workflow"].get("probabilities") or {}).items()}
        out = {**pick, "router": "jev", "router_model": decide.MODEL, "event": answers.get("_event", 0),
               "confidence": conf("workflow"), "report_confidence": conf("report"),
               "span_confidence": conf("span"), "probabilities": probs}
        # Every choice the run depends on must clear the bar, not just the workflow.
        gates = [("workflow", out["confidence"])]
        if pick["workflow"] in NEEDS_REPORT:
            # Comparing both quarters runs both, so which one is labelled the report does not
            # matter; what must clear the bar is the company. Sum Jev's report probabilities
            # over that company's filings (falls back to the single report when none are given).
            if pick["workflow"] in SPAN_MATTERS and pick["span"] == "both":
                rp = (answers["report"].get("probabilities") or {}) if isinstance(answers.get("report"), dict) else {}
                company = pick["report_id"].split("-")[0]
                if rp:
                    out["report_confidence"] = min(100, round(100 * sum(float(v) for k, v in rp.items()
                                                                       if str(k).split("-")[0] == company)))
                    out["report_confidence_of"] = "company"
            gates.append(("report", out["report_confidence"]))
        if pick["workflow"] in SPAN_MATTERS:
            gates.append(("span", out["span_confidence"]))
        low = [(name, c) for name, c in gates if c < ROUTE_MIN]
        out["unsure_of"] = low[0][0] if low else ""
        out["unsure"] = (f"Jev was {low[0][1]}% sure of the {low[0][0]}, under the {ROUTE_MIN}% bar." if low else
                         _period_problem(question, pick["report_id"], pick["span"]) if pick["workflow"] in NEEDS_REPORT else "")
        if out["unsure"] and not low:
            out["unsure_of"] = "period"
    except (decide.JevError, KeyError, TypeError, ValueError) as exc:
        out = {**_keywords(question, previous or {}), "router": "keywords", "confidence": None,
               "router_note": f"Jev unavailable ({str(exc)[:80]}); plain keyword rules routed this."}
        out["unsure_of"] = "period" if out["unsure"] else ""
    out["sure"] = not out["unsure"]
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
         "rule": "Offers only the workflows declared askable in code. Nothing else can be reached."},
        {"name": "Pick", "by": "Jev (TypeSafe)", "kind": "model", "where": "ask.route()",
         "rule": "Picks one workflow, one report, one or two quarters and which figure, each with a confidence. Cannot write."},
        {"name": "Bar", "by": "Kernel", "kind": "check", "where": "ask.route()",
         "rule": f"The workflow, the report and the period must each be {ROUTE_MIN}% sure, and the quarter and year "
                 "the question names must be on file. Otherwise nothing runs and the person picks."},
    )

    def run(self, question: str = "", previous_workflow: str = "", previous_report: str = ""):
        return route(question, {"workflow": previous_workflow, "report_id": previous_report})
