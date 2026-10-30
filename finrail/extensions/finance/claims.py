"""Reading a claim about NVIDIA's cash flow, so code can check it.

A sentence such as "NVIDIA generated $24.1bn of cash from operations in Q2" carries three
things: a figure, the line it is about, and the period. Code reads the figure exactly as
typed (`stated_figure`). Jev (TypeSafe) reads the line and the period, as typed choices
with a confidence; it can only pick from the lines below and cannot write a number. Then
code compares the figure with that line of a completed source-checked run. Below the
confidence bar the claim is called unclear and nothing is decided.
"""
from __future__ import annotations

import re
from decimal import Decimal

from finrail.core import decide
from finrail.core.errors import ContractViolation, NotConfigured
from finrail.plugin import Plugin

LINES = {
    "free_cash_flow": "Free cash flow (FCF)",
    "operating_cash_flow": "Operating cash flow: net cash provided by operating activities",
    "cash_purchases": "Capital expenditure: purchases of property, equipment and intangible assets",
    "principal_payments": "Principal payments on property, equipment and intangible assets",
    "none": "None of these cash-flow lines: revenue, earnings, guidance, buybacks or anything else",
}
LABELS = {"free_cash_flow": "free cash flow", "operating_cash_flow": "operating cash flow",
          "cash_purchases": "capital expenditure", "principal_payments": "principal payments", "none": "not a cash-flow line"}
QUARTERS = {
    "NVDA-Q1-FY2027": "NVIDIA Q1 fiscal 2027, the quarter ended April 2026",
    "NVDA-Q2-FY2027": "NVIDIA Q2 fiscal 2027, the quarter ended July 2026",
    "other": "Another period, a full year, a forecast, or no period stated",
}
#: Set by hand on the sample sentences in tests/test_claims.py (29 Oct), like the router's.
READ_MIN = 75
_LINE_INSTRUCTIONS = ("Which cash-flow line is this sentence's figure about? Read the words, not the number. "
                      "'Cash from operations' is operating cash flow; 'capex' is capital expenditure.")
_QUARTER_INSTRUCTIONS = ("Which period is the figure for? If the sentence names no period, choose the one in "
                         "the context when given, else other.")

_MONEY = re.compile(
    r"(?<![A-Za-z\d.,])(?P<open>\(\s*)?(?P<neg>[-−–]\s*|(?:minus|negative)\s+)?"
    r"(?P<cur>US\$|\$|€|£|¥|₹|(?:USD|EUR|GBP|JPY|CNY|INR|CHF)\b)?\s*(?P<neg2>[-−–]\s*)?"
    r"(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?)\s*"
    r"(?P<unit>trillion|tn|billion|bn|b|million|mn|m)?\b\s*"
    r"(?P<cur2>(?:USD|EUR|GBP|JPY|CNY|INR|CHF|dollars|euros|pounds|yen)\b)?(?P<close>\s*\))?", re.I)
_USD = {"$", "us$", "usd", "dollars"}
_SCALE = {"trillion": 1_000_000, "tn": 1_000_000, "billion": 1000, "bn": 1000, "b": 1000,
          "million": 1, "mn": 1, "m": 1}


def parse_figure(text: str) -> dict:
    """The one money figure in a sentence, as typed: {"value", "tolerance"} in USD millions,
    or {"problem": why it cannot be checked}. Code refuses rather than guesses: the figure
    must be in US dollars, carry a unit (m or bn), and be the only amount stated. A minus
    sign or brackets keep it negative. The tolerance is half the last typed digit:
    "$21.3bn" is 21,300 ± 50, "$21,341m" is exact. "FY2027", "Q2" and "40%" are not amounts."""
    found = []
    for m in _MONEY.finditer(text or ""):
        cur = (m["cur"] or m["cur2"] or "").lower()
        if not (cur or m["unit"]):
            continue  # a bare number: a year, a count, a percentage
        found.append(m)
    if not found:
        return {"problem": "no figure stated"}
    if len(found) > 1:
        return {"problem": "more than one figure stated (" + ", ".join(f[0].strip() for f in found[:3]) + ")"}
    m = found[0]
    cur = (m["cur"] or m["cur2"] or "").lower()
    if not cur:
        return {"problem": f"no currency on {m[0].strip()}; write it in US dollars, for example $21,341m"}
    if cur not in _USD:
        return {"problem": f"{m[0].strip()} is not in US dollars; Finrail's checked figures are"}
    if not m["unit"]:
        return {"problem": f"no unit on {m[0].strip()}; write m or bn, for example $21,341m"}
    scale = _SCALE[m["unit"].lower()]
    step = Decimal(1).scaleb(-len(m["dec"] or ""))
    value = Decimal(m["num"].replace(",", "")) * scale
    if m["neg"] or m["neg2"] or (m["open"] and m["close"]):
        value = -value
    tolerance = (step * scale / 2) if (m["dec"] or scale > 1) else Decimal(0)
    return {"value": value, "tolerance": tolerance}


def stated_figure(text: str):
    """(value, tolerance) in USD millions, or None when `parse_figure` finds a problem."""
    found = parse_figure(text)
    return None if "problem" in found else (found["value"], found["tolerance"])


_FISCAL = re.compile(r"\b(?:fy\s*'?|fiscal\s+(?:year\s+)?)(\d{4}|\d{2})\b", re.I)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_OTHER_PERIOD = re.compile(r"\b(q[34]|third quarter|fourth quarter|full[- ]year|annual|ttm|trailing|"
                           r"h[12]|first half|second half|nine months|year[- ]to[- ]date|ytd|next quarter|outlook|guidance)\b", re.I)


def stated_period(sentence: str) -> str:
    """The checked quarter a sentence names in its own words, "other" when it names any
    other period, or "" when it names none. Code reads this, so a model's pick can be
    checked against it: "Q1 FY2025" is never NVIDIA's Q1 FY2027."""
    s = _MONEY.sub(lambda m: " " if (m["cur"] or m["cur2"] or m["unit"]) else m[0], sentence or "").lower()
    years = {y if len(y) == 4 else "20" + y for y in _FISCAL.findall(s)}
    bare = set(_YEAR.findall(_FISCAL.sub(" ", s)))
    if bare or years - {"2027"} or _OTHER_PERIOD.search(s):
        return "other"
    q1, q2 = re.search(r"\b(q1|first quarter)\b", s), re.search(r"\b(q2|second quarter)\b", s)
    if q1 and q2:
        return "other"
    return "NVDA-Q1-FY2027" if q1 else "NVDA-Q2-FY2027" if q2 else ""


def _body(sentences: list, context: str, titles: list | None = None) -> dict:
    """One question pair per sentence, each naming its sentence (and its page title, for
    the web), so the answers cannot blur together in a shared state."""
    questions = {}
    for i, sentence in enumerate(sentences):
        title = (titles or [""] * len(sentences))[i]
        quoted = f'Sentence: "{sentence[:600]}". ' + (f'It comes from a page titled "{title[:200]}". ' if title else "")
        questions[f"line_{i}"] = {"type": "choice", "criteria": LINES, "instructions": quoted + _LINE_INSTRUCTIONS}
        questions[f"quarter_{i}"] = {"type": "choice", "criteria": QUARTERS, "instructions": quoted + _QUARTER_INSTRUCTIONS}
    return {"state": {"context_period": context or "-"}, "questions": questions}


def _keywords(sentence: str, context: str) -> dict:
    """The fallback reader. It is sure only when the sentence names both its line and its
    quarter; a period taken from the context, or none, is not enough to decide on."""
    s = sentence.lower()
    line = ("free_cash_flow" if "free cash" in s or "fcf" in s else
            "operating_cash_flow" if "operating" in s or "from operations" in s else
            "cash_purchases" if "capex" in s or "capital expenditure" in s or "purchases" in s else
            "principal_payments" if "principal" in s else "none")
    stated = stated_period(sentence)
    return {"line": line, "quarter": stated or context or "other", "line_conf": None, "quarter_conf": None,
            "sure": line != "none" and bool(stated)}


def read(sentences: list, context: str = "", ask=None, titles: list | None = None,
         capability: str = "") -> dict:
    """{"reader": "jev" | "keywords", "readings": [{line, quarter, line_conf, quarter_conf, sure}]}"""
    if not sentences:
        return {"reader": "jev", "readings": []}
    try:
        answers = (ask or decide.asker(whole=True, timeout=20, capability=capability))(_body(sentences, context, titles))
        readings = []
        for i in range(len(sentences)):
            line, quarter = answers[f"line_{i}"]["choice"], answers[f"quarter_{i}"]["choice"]
            if line not in LINES or quarter not in QUARTERS:
                raise decide.JevError("answer outside the offered choices")
            readings.append({"line": line, "quarter": quarter,
                             "line_conf": decide.confidence(answers[f"line_{i}"]),
                             "quarter_conf": decide.confidence(answers[f"quarter_{i}"])})
        reader, event = "jev", answers.get("_event", 0)
    except (decide.JevError, KeyError, TypeError, ValueError):
        readings, reader, event = [_keywords(s, context) for s in sentences], "keywords", 0
    for sentence, r in zip(sentences, readings):
        if reader == "jev":
            r["sure"] = r["line_conf"] >= READ_MIN and r["quarter_conf"] >= READ_MIN
            # Code reads the period too. A sentence that names another quarter or year than
            # the one picked is not decided, however sure the pick.
            stated = stated_period(sentence)
            if stated and stated != r["quarter"]:
                r.update(sure=False, period_conflict=stated)
    return {"reader": reader, "readings": readings, "event": event}


def checked_line(rec: dict, line: str):
    """The checked value of one line from a completed FCF run, or None."""
    v = rec["result"]
    if line == "free_cash_flow":
        return Decimal(v["amount"])
    return next((Decimal(i["amount"]) for i in v["inputs"] if i["metric"] == line), None)


def money(value) -> str:
    value = Decimal(value)
    sign, value = ("−" if value < 0 else ""), abs(value)
    return sign + (f"${int(value):,}m" if value == value.to_integral() else f"${value.normalize():,f}m")


def judge(value: Decimal, tolerance: Decimal, line: str, rec: dict) -> dict:
    """Code's verdict on one read claim against one checked run."""
    period = rec["result"]["period"]
    truth = checked_line(rec, line)
    if truth is None:
        return {"verdict": "unchecked", "detail": f"{LABELS[line]} is not a line Finrail checked for {period}."}
    if abs(value - truth) <= tolerance:
        exact = "" if tolerance == 0 else ", as rounded"
        return {"verdict": "matches", "detail": f"Matches the filing{exact}: {LABELS[line]} for {period} is {money(truth)}."}
    ocf, capex = checked_line(rec, "operating_cash_flow"), checked_line(rec, "cash_purchases")
    if line == "free_cash_flow" and ocf is not None and capex is not None and abs(value - (ocf - capex)) <= tolerance:
        return {"verdict": "definition",
                "detail": f"Different definition: {money(value)} is operating cash flow minus capex ({money(ocf)} − {money(capex)}). "
                          f"NVIDIA's own free cash flow also subtracts principal payments: {money(truth)}."}
    other = next((k for k in LINES if k not in ("none", line) and (checked_line(rec, k) is not None)
                  and abs(value - checked_line(rec, k)) <= tolerance), "")
    hint = f" {money(value)} is NVIDIA's {LABELS[other]}, not its {LABELS[line]}." if other else ""
    return {"verdict": "wrong_line" if other else "wrong",
            "detail": f"The filing's {LABELS[line]} for {period} is {money(truth)}; the claim says {money(value)}.{hint}"}


# ── what the web says, checked against the filing ────────────────────────────

_CASH_WORDS = re.compile(r"free cash|cash flow|cash from operations|operating cash|capex|capital expenditure|"
                         r"purchases of property|principal payments", re.I)
MAX_CLAIMS = 10


def web_sentences(pages: list) -> list:
    """Sentences from Tavily's pages that state a cash-flow figure, verbatim, with their
    source. Code picks them by words and a readable figure; no model chooses them."""
    out, seen = [], set()
    for page in pages:
        for sentence in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", page.get("content") or "")):
            sentence = sentence.strip()
            key = sentence.lower()
            if 20 <= len(sentence) <= 400 and key not in seen and _CASH_WORDS.search(sentence) and stated_figure(sentence):
                seen.add(key)
                out.append({"sentence": sentence, "url": page["url"], "publisher": page["publisher"],
                            "title": page.get("title", ""), "source_tier": page.get("source_tier", "")})
    return out[:MAX_CLAIMS]


def check_web(report_id: str, search=None, ask=None) -> dict:
    from . import research
    from .public_reports import checked_fcf, report
    item = report(report_id)
    if item["company"] != "NVIDIA":
        raise ContractViolation("Web claims are checked against NVIDIA's source-checked cash flow only.")
    if not checked_fcf(report_id):
        raise ContractViolation(f"There is no source-checked cash flow for {item['report_period']} yet. "
                                "Ask for its free cash flow first.")
    if not (search or research.configured()):
        raise NotConfigured("Tavily is not configured on this instance.")
    query = f"NVIDIA {item['report_period'].replace('FY', 'fiscal ')} free cash flow operating cash flow"
    pages = (search or research._search_one)(query, "cash_flow")
    found = web_sentences(pages)
    # No default period for the web: a sentence that names no quarter is not assumed to be this one.
    reading = read([f["sentence"] for f in found], context="", ask=ask, titles=[f["title"] for f in found],
                   capability=WebClaims.name)
    claims = []
    for f, r in zip(found, reading["readings"]):
        value, tolerance = stated_figure(f["sentence"])
        row = {**f, **r, "figure": money(value), "tolerance": str(tolerance), "line_label": LABELS[r["line"]]}
        if not r["sure"]:
            row.update(verdict="unclear", detail="Jev was not sure which line or period this is about, so nothing was decided.")
        elif r["line"] == "none":
            row.update(verdict="unchecked", detail="Not about a cash-flow line Finrail checks.")
        elif r["quarter"] == "other" or not checked_fcf(r["quarter"]):
            row.update(verdict="unchecked", detail="About a period Finrail has not checked.")
        else:
            row.update(judge(value, tolerance, r["line"], checked_fcf(r["quarter"])), checked_run=checked_fcf(r["quarter"])["id"])
        claims.append(row)
    counts = {k: sum(1 for c in claims if c["verdict"] == k)
              for k in ("matches", "definition", "wrong", "wrong_line", "unclear", "unchecked")}
    return {"report_id": report_id, "query": query, "pages": len(pages), "reader": reading["reader"],
            "jev_event": reading.get("event", 0),
            "claims": claims, "counts": counts}


class WebClaims(Plugin):
    name = "finance.reports.webclaims"
    description = ("Find what the web says about NVIDIA's cash flow with Tavily, have Jev read each claim, "
                   "and check every figure against the source-checked filing.")
    autonomous = True
    confirm = False
    reason = "it reads public pages and writes only its own result; a web page is never evidence"
    verified_by = "every verdict compares a figure code read with a line checked against the filing"
    ask = "What the web, press or analysts say about NVIDIA's cash flow, checked against the filing"
    ask_label = "Check web claims"
    steps = (
        {"name": "Find", "by": "Tavily", "kind": "retrieve", "where": "research._search_one()",
         "rule": "Discovery only. A web page is a claim to check, never evidence."},
        {"name": "Pick sentences", "by": "Kernel", "kind": "code", "where": "claims.web_sentences()",
         "rule": "Keeps verbatim sentences that name a cash-flow line and state a figure."},
        {"name": "Read claims", "by": "Jev (TypeSafe)", "kind": "model", "where": "claims.read()",
         "rule": "Picks which line and quarter each sentence is about. Cannot write a number."},
        {"name": "Check", "by": "Kernel", "kind": "check", "where": "claims.judge()",
         "rule": "Compares each figure, as typed, with that line of the source-checked run."},
    )

    def plan(self, report_id: str = "NVDA-Q2-FY2027"):
        from .public_reports import report
        report(report_id)
        return {"report_id": report_id}

    def execute(self, p) -> dict:
        return check_web(p["report_id"])
