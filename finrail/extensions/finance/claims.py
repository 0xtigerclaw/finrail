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


_BOUND = re.compile(r"\b(?P<w>more than|over|above|at least|upwards of|in excess of|exceeding|nearly|almost|just under|"
                    r"close to|about|around|roughly|approximately|some)\s+(?:US\$|\$)\s*\d", re.I)
_BOUND_KIND = {"more than": "min", "over": "min", "above": "min", "at least": "min", "upwards of": "min",
               "in excess of": "min", "exceeding": "min", "nearly": "max", "almost": "max", "just under": "max",
               "close to": "approx", "about": "approx", "around": "approx", "roughly": "approx",
               "approximately": "approx", "some": "approx"}


def stated_bound(sentence: str) -> dict:
    """The word that qualifies a figure, read by code: {"kind": min | max | approx, "word"},
    or {} when the figure is stated plainly. "More than $80 billion" is a floor, not $80bn."""
    m = _BOUND.search(sentence or "")
    return {"kind": _BOUND_KIND[m["w"].lower()], "word": m["w"].lower()} if m else {}


def agrees(value: Decimal, tolerance: Decimal, truth: Decimal, bound: dict | None = None) -> bool:
    """Whether a stated figure is consistent with the checked one. Plain: within half the last
    typed digit. "More than X": at least X, by no more than one unit of that digit. "Nearly X":
    at most X, by no more than one unit. "About X": within one unit. Sizes are compared, since
    press words ("loss", "lost") carry the sign."""
    v, t = abs(value), abs(truth)
    unit = 2 * tolerance
    kind = (bound or {}).get("kind")
    if kind == "min":
        return v - tolerance <= t <= v + unit
    if kind == "max":
        return v - unit <= t <= v + tolerance
    if kind == "approx":
        return abs(t - v) <= unit
    return abs(t - v) <= tolerance


def _as_stated(value: Decimal, bound: dict | None) -> str:
    return ((bound or {}).get("word", "") + " " if bound else "") + money(abs(value) if bound else value)


def stated_figure(text: str):
    """(value, tolerance) in USD millions, or None when `parse_figure` finds a problem."""
    found = parse_figure(text)
    return None if "problem" in found else (found["value"], found["tolerance"])


# ── Anthropic's S-1: the profit and revenue lines a press sentence may be about ──

ANTHROPIC = "ANTH-S1-2026"
ANTH_LINES = {
    "revenue": "Revenue or sales",
    "net_loss": "Net loss or net income: the bottom line, what the company lost or earned overall",
    "operating_loss": "Operating result under GAAP: operating loss, or operating income or operating profit without 'adjusted'",
    "adjusted_operating_income": "Adjusted operating income, adjusted or non-GAAP profit: the company's own measure with stock compensation added back",
    "convertible_notes_charge": "The non-cash charge from revaluing convertible notes or bonds (change in their fair value)",
    "none": "None of these: valuation, funding, run-rate, compute spending, customers, a one-off charge or anything else",
}
ANTH_LABELS = {"revenue": "revenue", "net_loss": "net loss", "operating_loss": "operating result",
               "adjusted_operating_income": "adjusted operating income",
               "convertible_notes_charge": "non-cash charge on convertible notes", "none": "not a line Finrail checks"}
ANTH_PERIODS = {
    ANTHROPIC: "Anthropic's first half of 2026: the six months to 30 June 2026, as reported in its S-1",
    "other": "Another period: one quarter, a full year, an annualised run-rate, a forecast, or no period stated",
}
_ANTH_LINE_INSTRUCTIONS = ("Which line of Anthropic's results is this sentence's figure about? Read the words, not the number. "
                           "'Operating profit' or 'operating income' without 'adjusted' is the operating result; 'adjusted' "
                           "or 'non-GAAP' is adjusted operating income; 'lost', 'net loss' or a loss not called "
                           "'operating' is the net loss; a non-cash charge on convertible notes or bonds is its own line.")
_ANTH_PERIOD_INSTRUCTIONS = ("Which period is the figure for? The S-1 reports the first half of 2026, the six months to "
                             "30 June 2026. A single quarter, a full year, an annualised run-rate or a forecast is another "
                             "period. If the sentence names no period, choose other.")
_ANTH_OTHER = re.compile(r"\b(run[- ]rate|annuali[sz]ed|arr|q[1-4]|quarter|full[- ]year|annual|guidance|forecast|"
                         r"outlook|next year|second half|h2|nine months)\b", re.I)
_ANTH_HALF = re.compile(r"\b(first half|h1|six months|first six months|half[- ]year)\b", re.I)


def anthropic_period(sentence: str) -> str:
    """The period a sentence names in its own words, read by code: the S-1's first half of
    2026, "other", or "" when it names none."""
    s = _MONEY.sub(lambda m: " " if (m["cur"] or m["cur2"] or m["unit"]) else m[0], sentence or "").lower()
    years = set(_YEAR.findall(s))
    if _ANTH_OTHER.search(s) or years - {"2026"}:
        return "other"
    return ANTHROPIC if _ANTH_HALF.search(s) else ""


def _anth_keywords(sentence: str, context: str) -> dict:
    s = sentence.lower()
    line = ("convertible_notes_charge" if "convertible" in s or "non-cash" in s else
            "adjusted_operating_income" if "adjusted" in s or "non-gaap" in s else
            "operating_loss" if "operating" in s else
            "net_loss" if "net loss" in s or "net income" in s or " lost " in f" {s} " else
            "revenue" if "revenue" in s or "sales" in s else "none")
    stated = anthropic_period(sentence)
    return {"line": line, "quarter": stated or "other", "line_conf": None, "quarter_conf": None,
            "sure": line != "none" and bool(stated)}


#: What `read` offers Jev for each company, and how code reads the period and keywords.
READERS = {
    "NVIDIA": {"lines": None, "periods": None, "line_instructions": None, "period_instructions": None},
    "Anthropic": {"lines": ANTH_LINES, "periods": ANTH_PERIODS, "line_instructions": _ANTH_LINE_INSTRUCTIONS,
                  "period_instructions": _ANTH_PERIOD_INSTRUCTIONS},
}


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


def _body(sentences: list, context: str, titles: list | None = None, company: str = "NVIDIA") -> dict:
    """One question pair per sentence, each naming its sentence (and its page title, for
    the web), so the answers cannot blur together in a shared state."""
    lines, periods, line_i, period_i = _offer(company)
    questions = {}
    for i, sentence in enumerate(sentences):
        title = (titles or [""] * len(sentences))[i]
        quoted = f'Sentence: "{sentence[:600]}". ' + (f'It comes from a page titled "{title[:200]}". ' if title else "")
        questions[f"line_{i}"] = {"type": "choice", "criteria": lines, "instructions": quoted + line_i}
        questions[f"quarter_{i}"] = {"type": "choice", "criteria": periods, "instructions": quoted + period_i}
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


def _offer(company: str) -> tuple:
    r = READERS.get(company) or READERS["NVIDIA"]
    return (r["lines"] or LINES, r["periods"] or QUARTERS, r["line_instructions"] or _LINE_INSTRUCTIONS,
            r["period_instructions"] or _QUARTER_INSTRUCTIONS)


def read(sentences: list, context: str = "", ask=None, titles: list | None = None,
         capability: str = "", company: str = "NVIDIA") -> dict:
    """{"reader": "jev" | "keywords", "readings": [{line, quarter, line_conf, quarter_conf, sure}]}"""
    if not sentences:
        return {"reader": "jev", "readings": []}
    lines, periods, _li, _pi = _offer(company)
    period_of = anthropic_period if company == "Anthropic" else stated_period
    fallback = _anth_keywords if company == "Anthropic" else _keywords
    try:
        answers = (ask or decide.asker(whole=True, timeout=20, capability=capability))(_body(sentences, context, titles, company))
        readings = []
        for i in range(len(sentences)):
            line, quarter = answers[f"line_{i}"]["choice"], answers[f"quarter_{i}"]["choice"]
            if line not in lines or quarter not in periods:
                raise decide.JevError("answer outside the offered choices")
            readings.append({"line": line, "quarter": quarter,
                             "line_conf": decide.confidence(answers[f"line_{i}"]),
                             "quarter_conf": decide.confidence(answers[f"quarter_{i}"])})
        reader, event = "jev", answers.get("_event", 0)
    except (decide.JevError, KeyError, TypeError, ValueError):
        readings, reader, event = [fallback(s, context) for s in sentences], "keywords", 0
    for sentence, r in zip(sentences, readings):
        if reader == "jev":
            r["sure"] = r["line_conf"] >= READ_MIN and r["quarter_conf"] >= READ_MIN
            # Code reads the period too. A sentence that names another quarter or year than
            # the one picked is not decided, however sure the pick.
            stated = period_of(sentence)
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


def judge(value: Decimal, tolerance: Decimal, line: str, rec: dict, bound: dict | None = None) -> dict:
    """Code's verdict on one read claim against one checked run."""
    period = rec["result"]["period"]
    truth = checked_line(rec, line)
    if truth is None:
        return {"verdict": "unchecked", "detail": f"{LABELS[line]} is not a line Finrail checked for {period}."}
    if agrees(value, tolerance, truth, bound) if bound else abs(value - truth) <= tolerance:
        exact = f', as stated ("{bound["word"]}")' if bound else "" if tolerance == 0 else ", as rounded"
        return {"verdict": "matches", "detail": f"Matches the filing{exact}: {LABELS[line]} for {period} is {money(truth)}."}
    ocf, capex = checked_line(rec, "operating_cash_flow"), checked_line(rec, "cash_purchases")
    if line == "free_cash_flow" and ocf is not None and capex is not None and abs(value - (ocf - capex)) <= tolerance:
        return {"verdict": "definition",
                "detail": f"Different definition: {money(value)} is operating cash flow minus capex ({money(ocf)} − {money(capex)}). "
                          f"NVIDIA's own free cash flow also subtracts principal payments: {money(truth)}."}
    other = next((k for k in LINES if k not in ("none", line) and (checked_line(rec, k) is not None)
                  and (agrees(value, tolerance, checked_line(rec, k), bound) if bound
                       else abs(value - checked_line(rec, k)) <= tolerance)), "")
    hint = f" {money(value)} is NVIDIA's {LABELS[other]}, not its {LABELS[line]}." if other else ""
    return {"verdict": "wrong_line" if other else "wrong",
            "detail": f"The filing's {LABELS[line]} for {period} is {money(truth)}. The claim says {_as_stated(value, bound)}.{hint}"}


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


_PROFIT_WORDS = re.compile(r"revenue|sales|net loss|net income|operating (?:loss|income|profit)|adjusted|non-gaap|convertible|"
                           r"\bprofit|\blost\b|\bloss\b", re.I)


def web_sentences_about(pages: list, words) -> list:
    """`web_sentences` with the topic words given: verbatim sentences naming a line and one figure."""
    out, seen = [], set()
    for page in pages:
        for sentence in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", page.get("content") or "")):
            sentence = sentence.strip()
            key = sentence.lower()
            if 20 <= len(sentence) <= 400 and key not in seen and words.search(sentence) and stated_figure(sentence):
                seen.add(key)
                out.append({"sentence": sentence, "url": page["url"], "publisher": page["publisher"],
                            "title": page.get("title", ""), "source_tier": page.get("source_tier", "")})
    return out[:MAX_CLAIMS]


def anthropic_checked() -> tuple:
    """({line: signed checked figure}, run id) from the completed house run on the S-1,
    whose lines code read from the archived cells; ({}, "") when there is none."""
    from .public_reports import checked_run
    rec = checked_run(ANTHROPIC, "house_operating_income")
    if not rec:
        return {}, ""
    by = {line["key"]: Decimal(line["amount"]) for line in rec["result"].get("lines") or []}
    names = {"revenue": "revenue", "net_loss": "net_loss", "operating_loss": "aoi_operating_loss",
             "adjusted_operating_income": "adjusted_operating_income", "convertible_notes_charge": "fv_convertibles"}
    return {line: by[key] for line, key in names.items() if key in by}, rec["id"]


def judge_anthropic(value: Decimal, tolerance: Decimal, line: str, truth: dict, bound: dict | None = None) -> dict:
    """Code's verdict on one read Anthropic claim. Sizes are compared as typed; the sign is
    the filing's, shown in the verdict, because press words carry it ("lost", "profit")."""
    period = "the first half of 2026"
    if line not in truth:
        return {"verdict": "unchecked", "detail": f"{ANTH_LABELS[line].capitalize()} is not a line Finrail checked in the S-1."}
    if agrees(value, tolerance, truth[line], bound):
        exact = f', as stated ("{bound["word"]}")' if bound else "" if tolerance == 0 else ", as rounded"
        return {"verdict": "matches", "detail": f"Matches the S-1{exact}: {ANTH_LABELS[line]} for {period} is {money(truth[line])}."}
    if not bound and tolerance and agrees(value, tolerance, truth[line], {"kind": "approx"}):
        # Within one unit of the last typed digit but not rounded to it: "$80 billion" for $80.7bn.
        return {"verdict": "loose", "detail": f"Close, but loosely rounded: the S-1's {ANTH_LABELS[line]} for {period} is "
                                              f"{money(truth[line])}. The claim says {money(abs(value))}."}
    other = next((k for k in truth if k != line and agrees(value, tolerance, truth[k], bound)), "")
    hint = (f" {money(abs(value))} is Anthropic's {ANTH_LABELS[other]} ({money(truth[other])}), "
            f"not its {ANTH_LABELS[line]}." if other else "")
    return {"verdict": "wrong_line" if other else "wrong",
            "detail": f"The S-1's {ANTH_LABELS[line]} for {period} is {money(truth[line])}. The claim says {_as_stated(abs(value), bound or {})}.{hint}"}


#: Two plain searches, merged by URL: one pass on its own often returns only a few snippets.
ANTH_QUERIES = ("Anthropic S-1 first half 2026 net loss operating loss",
                "Anthropic IPO filing adjusted operating income stock-based compensation")


def _check_web_anthropic(item: dict, search=None, ask=None) -> dict:
    from . import research
    truth, run_id = anthropic_checked()
    if not truth:
        raise ContractViolation("There are no source-checked S-1 lines for Anthropic yet. Ask whether Anthropic made "
                                "money first; nothing was searched.")
    if not (search or research.configured()):
        raise NotConfigured("Tavily is not configured on this instance.")
    pages, seen = [], set()
    for q in ANTH_QUERIES:
        for page in (search or research._search_one)(q, "financials"):
            if page.get("url") not in seen:
                seen.add(page.get("url"))
                pages.append(page)
    query = "” and “".join(ANTH_QUERIES)
    found = web_sentences_about(pages, _PROFIT_WORDS)
    reading = read([f["sentence"] for f in found], context="", ask=ask, titles=[f["title"] for f in found],
                   capability=WebClaims.name, company="Anthropic")
    claims = []
    for f, r in zip(found, reading["readings"]):
        value, tolerance = stated_figure(f["sentence"])
        row = {**f, **r, "figure": money(value), "tolerance": str(tolerance), "line_label": ANTH_LABELS[r["line"]]}
        stated = anthropic_period(f["sentence"])
        if not stated:
            # Code reads the period first: a sentence that names none is never tied to the S-1's half.
            row.update(verdict="unchecked", detail="The sentence names no period, so it is not tied to the S-1's first half of 2026.")
        elif stated != ANTHROPIC:
            # A quarter, a run rate or a full year, named in the sentence's own words: code rules it out.
            row.update(verdict="unchecked", detail="It names another period. The S-1 covers the first half of 2026.")
        elif not r["sure"]:
            row.update(verdict="unclear", detail="Jev was not sure which line or period this is about, so nothing was decided.")
        elif r["line"] == "none":
            row.update(verdict="unchecked", detail="Not about a line Finrail checks in the S-1.")
        elif r["quarter"] != ANTHROPIC:
            row.update(verdict="unchecked", detail="About a period other than the S-1's first half of 2026.")
        else:
            row.update(judge_anthropic(value, tolerance, r["line"], truth, stated_bound(f["sentence"])), checked_run=run_id)
        claims.append(row)
    counts = {k: sum(1 for c in claims if c["verdict"] == k)
              for k in ("matches", "loose", "definition", "wrong", "wrong_line", "unclear", "unchecked")}
    return {"report_id": item["source_id"], "query": query, "pages": len(pages), "reader": reading["reader"],
            "jev_event": reading.get("event", 0), "claims": claims, "counts": counts, "checked_run": run_id}


def check_web(report_id: str, search=None, ask=None) -> dict:
    from . import research
    from .public_reports import checked_fcf, report
    item = report(report_id)
    if item["company"] == "Anthropic":
        return _check_web_anthropic(item, search, ask)
    if item["company"] != "NVIDIA":
        raise ContractViolation("Web claims are checked against NVIDIA's cash flow and Anthropic's S-1 only.")
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
            row.update(judge(value, tolerance, r["line"], checked_fcf(r["quarter"]), stated_bound(f["sentence"])),
                       checked_run=checked_fcf(r["quarter"])["id"])
        claims.append(row)
    counts = {k: sum(1 for c in claims if c["verdict"] == k)
              for k in ("matches", "loose", "definition", "wrong", "wrong_line", "unclear", "unchecked")}
    return {"report_id": report_id, "query": query, "pages": len(pages), "reader": reading["reader"],
            "jev_event": reading.get("event", 0),
            "claims": claims, "counts": counts}


class WebClaims(Plugin):
    name = "finance.reports.webclaims"
    description = ("Find what the web says about NVIDIA's cash flow or Anthropic's S-1 results with Tavily, have Jev "
                   "read each claim, and check every figure against the source-checked filing.")
    autonomous = True
    confirm = False
    reason = "it reads public pages and writes only its own result; a web page is never evidence"
    verified_by = "every verdict compares a figure code read with a line checked against the filing"
    ask = ("What the web, press or analysts say about NVIDIA's cash flow, or about Anthropic's revenue, profit or "
           "loss, checked against the filing")
    ask_label = "Check web claims"
    steps = (
        {"name": "Find", "by": "Tavily", "kind": "retrieve", "where": "research._search_one()",
         "rule": "Discovery only. A web page is a claim to check, never evidence."},
        {"name": "Pick sentences", "by": "Kernel", "kind": "code", "where": "claims.web_sentences()",
         "rule": "Keeps sentences, word for word, that name a checked line and state one figure."},
        {"name": "Read claims", "by": "Jev (TypeSafe)", "kind": "model", "where": "claims.read()",
         "rule": "Picks which line and period each sentence is about. Cannot write a number."},
        {"name": "Check", "by": "Kernel", "kind": "check", "where": "claims.judge()",
         "rule": "Compares each figure, as typed, with that line of the source-checked run."},
    )

    def plan(self, report_id: str = "NVDA-Q2-FY2027"):
        from .public_reports import report
        report(report_id)
        return {"report_id": report_id}

    def execute(self, p) -> dict:
        return check_web(p["report_id"])
