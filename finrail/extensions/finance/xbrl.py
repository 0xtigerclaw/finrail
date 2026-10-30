"""SEC XBRL companion facts, read with the standard library only.

Every SEC filing with financial statements carries each number twice: once in the HTML
tables a person reads, and once as an XBRL fact a machine reads, tagged with a concept
(`us-gaap:Revenues`), a context (the period and any segment) and a unit. The HTML
checker in `public_reports.py` reads the table cell; this module reads the tag, so the
two can be reconciled and a row-ordinal mistake can no longer pass as a verified figure.

Three sources, in order of simplicity:

* The SEC `companyfacts` JSON API: every non-segment fact the issuer has ever filed, one
  request per issuer. Simplest, but it carries no segment facts, no `decimals`, and no
  issuer-custom concepts (NVIDIA's `nvda:` lines are absent; only dei, us-gaap, srt and
  the other standard taxonomies appear).
* The inline XBRL in the primary document (`ix:nonFraction` tags in the `.htm` that
  Finrail already archives and hash-checks). Complete, and needs no second download.
* The plain XBRL instance EDGAR extracts next to the primary document (`*_htm.xml`).

All three normalise to the same `Fact`. Anything downloaded from EDGAR is untrusted
data: it is parsed, never executed, never followed as an instruction, and capped in size.

Rules this module keeps, in the house style: a failure is raised as a `ContractViolation`
naming what was missing or ambiguous, never returned as a plausible number; and when
several facts could answer a lookup, none of them is picked.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from finrail.core.errors import ContractViolation, OpFailed

# ── concept map: the place a fund extends ───────────────────────────────────
# Finrail's definition names (the keys `public_reports.PROFILES` rows use) mapped to the
# us-gaap concepts issuers tag them with, in preference order. `lookup` tries the first
# concept, and moves to the next only when the first has NO consolidated fact for the
# period; it never picks between two concepts that both have one.
#
# `presented` says how the HTML table prints the tagged value:
#   as_tagged  the table sign is the XBRL sign. A net loss is a negative NetIncomeLoss
#              in XBRL and sits in parentheses in the table.
#   negated    the table prints the XBRL value with the opposite sign. Cash outflows
#              such as PaymentsToAcquirePropertyPlantAndEquipment are positive in XBRL
#              (the concept measures the payment) and sit in parentheses in the table.
#
# Add a definition here (or a candidate to an existing one) when an issuer tags a line
# with a concept this list does not know. Custom issuer concepts (`nvda:...`) are
# allowed; name them with their prefix.
CONCEPTS = {
    "revenue": {
        "concepts": ("us-gaap:Revenues",
                     "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                     "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
                     "us-gaap:SalesRevenueNet"),
        "period": "duration", "unit": "USD", "presented": "as_tagged"},
    "net_loss": {
        "concepts": ("us-gaap:NetIncomeLoss", "us-gaap:ProfitLoss"),
        "period": "duration", "unit": "USD", "presented": "as_tagged"},
    "operating_income": {
        "concepts": ("us-gaap:OperatingIncomeLoss",),
        "period": "duration", "unit": "USD", "presented": "as_tagged"},
    "operating_cash_flow": {
        "concepts": ("us-gaap:NetCashProvidedByUsedInOperatingActivities",
                     "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        "period": "duration", "unit": "USD", "presented": "as_tagged"},
    "cash_purchases": {
        "concepts": ("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                     "us-gaap:PaymentsToAcquireProductiveAssets",
                     "us-gaap:PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets"),
        "period": "duration", "unit": "USD", "presented": "negated"},
    "principal_payments": {
        # NVIDIA's "Principal payments on property and equipment and intangible assets".
        # NVIDIA's companyfacts (checked 30 October 2026) has no FinanceLeasePrincipalPayments
        # and companyfacts carries no issuer-custom namespace at all, so this line is most
        # likely an nvda: custom concept that only the inline document can supply. Confirm
        # the concept against the 10-Q's inline XBRL before relying on it.
        "concepts": ("us-gaap:FinanceLeasePrincipalPayments",),
        "period": "duration", "unit": "USD", "presented": "negated"},
    "stock_comp": {
        "concepts": ("us-gaap:ShareBasedCompensation",
                     "us-gaap:AllocatedShareBasedCompensationExpense"),
        "period": "duration", "unit": "USD", "presented": "as_tagged"},
}
CONCEPTS["capex"] = CONCEPTS["cash_purchases"]

# What a table's unit line means, as a power of ten applied to the printed figure.
SCALES = {"units": 0, "ones": 0, "thousands": 3, "thousand": 3,
          "millions": 6, "million": 6, "billions": 9, "billion": 9}

# SEC fair-access policy: declare who is asking. Set FINRAIL_SEC_USER_AGENT to
# "<your organisation> <contact email>"; without it EDGAR may refuse the request.
DEFAULT_USER_AGENT = "Finrail finance harness (set FINRAIL_SEC_USER_AGENT to your organisation and contact email)"
COMPANYFACTS_LIMIT = 60_000_000   # bytes; large issuers run past 10 MB
FILING_LIMIT = 60_000_000


# ── the normalised fact ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Fact:
    """One tagged number, with sign and scale already applied, in base units."""
    concept: str                                # "us-gaap:Revenues"
    value: Decimal                              # base units: 4500000000, not 4,500 million
    unit: str                                   # "USD", "shares", "USD/shares", "pure"
    start: date | None                          # None for an instant
    end: date                                   # period end, or the instant itself
    context: str                                # context id in the instance; synthetic for companyfacts
    dimensions: tuple[tuple[str, str], ...]     # ((axis, member), ...) sorted; () means consolidated
    source: str                                 # "companyfacts" | "inline" | "instance"
    accession: str                              # "0001628280-26-067168"
    decimals: str | None = None                 # the XBRL decimals attribute, when the source has one

    @property
    def instant(self) -> bool:
        return self.start is None

    @property
    def consolidated(self) -> bool:
        return not self.dimensions

    @property
    def months(self) -> int | None:
        """Duration in whole months, tolerant of 13-week quarters and 53-week years."""
        if self.start is None:
            return None
        days = (self.end - self.start).days + 1
        return round(days / 30.4375)

    @property
    def period(self) -> str:
        if self.start is None:
            return f"instant {self.end.isoformat()}"
        return f"{self.months} months {self.start.isoformat()} to {self.end.isoformat()}"

    def same_measure(self, other: "Fact") -> bool:
        return (self.concept, self.unit, self.start, self.end, self.dimensions) == \
               (other.concept, other.unit, other.start, other.end, other.dimensions)

    def as_dict(self) -> dict:
        return {"concept": self.concept, "value": plain(self.value), "unit": self.unit,
                "start": self.start.isoformat() if self.start else None, "end": self.end.isoformat(),
                "context": self.context, "dimensions": [list(d) for d in self.dimensions],
                "source": self.source, "accession": self.accession, "decimals": self.decimals}


# ── fetching: one injectable function, a descriptive User-Agent ─────────────

def user_agent() -> str:
    return os.environ.get("FINRAIL_SEC_USER_AGENT") or DEFAULT_USER_AGENT


def _request(url: str) -> Request:
    return Request(url, headers={"User-Agent": user_agent(), "Accept-Encoding": "identity"})


def http_get(url: str, *, limit: int = FILING_LIMIT, timeout: int = 60) -> bytes:
    """The only place this module touches the network. Tests pass their own `fetch`."""
    if not url.startswith("https://"):
        raise ContractViolation(f"Refusing a non-HTTPS fetch: {url}")
    try:
        with urlopen(_request(url), timeout=timeout) as response:
            data = response.read(limit + 1)
    except HTTPError as exc:
        raise OpFailed(f"EDGAR returned HTTP {exc.code} for {url}. No local fallback was used.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise OpFailed(f"EDGAR could not be reached for {url}. No local fallback was used.") from exc
    if len(data) > limit:
        raise OpFailed(f"EDGAR response for {url} exceeded {limit} bytes; refused rather than truncated.")
    return data


def cik10(cik) -> str:
    digits = str(cik).strip().upper().removeprefix("CIK").lstrip("0") or "0"
    if not digits.isdigit() or len(digits) > 10:
        raise ContractViolation(f"Not a CIK: {cik!r}")
    return digits.zfill(10)


def accession_dashed(accession: str) -> str:
    raw = str(accession).strip().replace("-", "")
    if not (raw.isdigit() and len(raw) == 18):
        raise ContractViolation(f"Not an accession number: {accession!r}")
    return f"{raw[:10]}-{raw[10:12]}-{raw[12:]}"


def companyfacts_url(cik) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(cik)}.json"


def filing_urls(cik, accession: str, primary_document: str | None = None) -> dict:
    """Where a filing's XBRL lives. `primary_document` is the .htm file name
    (e.g. `anthropicpbc-sx1.htm`); the extracted instance sits next to it as `*_htm.xml`."""
    folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik10(cik))}/{accession_dashed(accession).replace('-', '')}"
    out = {"folder": folder, "index": folder + "/index.json",
           "financial_report": folder + "/Financial_Report.xlsx"}
    if primary_document:
        name = primary_document.rsplit("/", 1)[-1]
        if "/" in primary_document or not re.fullmatch(r"[\w.-]+\.htm[l]?", name):
            raise ContractViolation(f"Not a primary document file name: {primary_document!r}")
        out["primary"] = folder + "/" + name
        out["instance"] = folder + "/" + name.rsplit(".", 1)[0] + "_htm.xml"
    return out


def load_companyfacts(cik, *, fetch=http_get) -> dict:
    raw = fetch(companyfacts_url(cik), limit=COMPANYFACTS_LIMIT)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise OpFailed("EDGAR companyfacts response was not JSON.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("facts"), dict):
        raise OpFailed("EDGAR companyfacts response did not carry a facts table.")
    return data


def load_filing_facts(cik, accession: str, primary_document: str, *, fetch=http_get) -> list[Fact]:
    """Facts from the inline XBRL of a filing's primary document, fetched from EDGAR."""
    urls = filing_urls(cik, accession, primary_document)
    return facts_from_inline(fetch(urls["primary"], limit=FILING_LIMIT), accession_dashed(accession))


# ── companyfacts JSON ───────────────────────────────────────────────────────

def _date(text, what: str) -> date:
    try:
        return date.fromisoformat(str(text))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(f"XBRL {what} is not a date: {text!r}") from exc


def _decimal(text, what: str) -> Decimal:
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation as exc:
        raise ContractViolation(f"XBRL {what} is not a number: {text!r}") from exc
    if not value.is_finite():
        raise ContractViolation(f"XBRL {what} is not a finite number: {text!r}")
    return _canon(value)


def plain(value: Decimal) -> str:
    """A Decimal without exponent notation or trailing zeros: 4500000000, never 4.5E+9 or 0.400."""
    return format(value.normalize(), "f")


def _canon(value: Decimal) -> Decimal:
    return Decimal(plain(value))


def facts_from_companyfacts(data: dict, accession: str | None = None) -> list[Fact]:
    """Flatten a companyfacts document. With `accession`, keep only facts that filing
    reported; without it the same measure appears once per filing that carried it,
    which is what lets `lookup` catch a restatement."""
    wanted = accession_dashed(accession) if accession else None
    out = []
    for namespace, concepts in (data.get("facts") or {}).items():
        for local, body in (concepts or {}).items():
            for unit, rows in ((body or {}).get("units") or {}).items():
                for row in rows or []:
                    if not isinstance(row, dict) or "val" not in row or "end" not in row:
                        continue
                    accn = str(row.get("accn", ""))
                    if wanted and accn != wanted:
                        continue
                    start = _date(row["start"], "start") if row.get("start") else None
                    end = _date(row["end"], "end")
                    context = f"{accn}:{row.get('fy') or ''}{row.get('fp') or ''}:{start.isoformat() if start else ''}:{end.isoformat()}"
                    out.append(Fact(concept=f"{namespace}:{local}", value=_decimal(row["val"], f"{namespace}:{local} val"),
                                    unit=unit, start=start, end=end, context=context, dimensions=(),
                                    source="companyfacts", accession=accn))
    return out


# ── shared context and unit resolution ──────────────────────────────────────

def _unit_name(measures: list[str], numerator: list[str], denominator: list[str]) -> str:
    strip = lambda m: m.rsplit(":", 1)[-1]
    if numerator or denominator:
        return "/".join(("*".join(strip(m) for m in numerator), "*".join(strip(m) for m in denominator)))
    return "*".join(strip(m) for m in measures)


def _make_fact(concept, value, unit_id, context_id, contexts, units, source, accession, decimals=None) -> Fact:
    ctx = contexts.get(context_id)
    if ctx is None:
        raise ContractViolation(f"XBRL fact {concept} refers to context {context_id!r}, which the instance does not define.")
    unit = units.get(unit_id)
    if unit is None:
        raise ContractViolation(f"XBRL fact {concept} refers to unit {unit_id!r}, which the instance does not define.")
    return Fact(concept=concept, value=value, unit=unit, start=ctx["start"], end=ctx["end"], context=context_id,
                dimensions=tuple(sorted(ctx["dimensions"])), source=source, accession=accession, decimals=decimals)


def _finish_context(ctx: dict, contexts: dict):
    if ctx.get("instant"):
        ctx["start"], ctx["end"] = None, _date(ctx["instant"], f"instant of context {ctx['id']}")
    elif ctx.get("startDate") and ctx.get("endDate"):
        ctx["start"] = _date(ctx["startDate"], f"start of context {ctx['id']}")
        ctx["end"] = _date(ctx["endDate"], f"end of context {ctx['id']}")
    else:
        raise ContractViolation(f"XBRL context {ctx['id']!r} has neither an instant nor a start and end date.")
    contexts[ctx["id"]] = ctx


# ── inline XBRL in an HTML filing ───────────────────────────────────────────

_ZERO_FORMATS = {"zerodash", "fixedzero", "nocontent", "fixedempty", "numdash"}
_DOT_FORMATS = {"numdotdecimal", "numcommadot", "numspacedot", "numunitdecimal", ""}
_COMMA_FORMATS = {"numcommadecimal", "numdotcomma", "numspacecomma", "numcomma"}
_ZERO_WORDS = {"no", "none", "nil", "zero"}


def inline_number(text: str, fmt: str | None, *, scale: str | None, sign: str | None, where: str) -> Decimal:
    """Apply an ix transform, then scale and sign. Unknown transforms are refused, not guessed."""
    local = (fmt or "").rsplit(":", 1)[-1].lower().replace("-", "")
    body = "".join(text.split())
    if local in _ZERO_FORMATS:
        value = Decimal(0)
    elif local == "numwordsen":
        if body.lower() not in _ZERO_WORDS:
            raise ContractViolation(f"Inline XBRL number words {text!r} at {where} are not supported beyond zero.")
        value = Decimal(0)
    elif local in _DOT_FORMATS or local in _COMMA_FORMATS:
        if local in _COMMA_FORMATS:
            body = body.replace(".", "").replace(",", ".")
        else:
            body = body.replace(",", "")
        if not body:
            raise ContractViolation(f"Inline XBRL fact at {where} has no number to read.")
        value = _decimal(body, f"inline fact at {where}")
    else:
        raise ContractViolation(f"Inline XBRL transform {fmt!r} at {where} is not supported; the fact was refused, not guessed.")
    if scale not in (None, ""):
        try:
            value = value.scaleb(int(scale))
        except ValueError as exc:
            raise ContractViolation(f"Inline XBRL scale {scale!r} at {where} is not an integer.") from exc
    if sign == "-":
        value = -value
    elif sign not in (None, ""):
        raise ContractViolation(f"Inline XBRL sign {sign!r} at {where} is not understood.")
    return _canon(value)


class _InlineReader(HTMLParser):
    """Collects contexts, units and ix:nonFraction facts. HTMLParser lowercases tag
    and attribute names, so everything below is matched in lower case."""

    def __init__(self, accession: str):
        super().__init__(convert_charrefs=True)
        self.accession = accession
        self.contexts, self.units = {}, {}
        self.raw_facts = []          # (attrs, text) in document order
        self.open_facts = []         # stack of [attrs, [text parts]]
        self.ctx = None              # context being read
        self.unit = None
        self.member = None           # (dimension, [text parts]) while inside a member
        self.text_target = None      # key on ctx receiving character data
        self.measure_target = None   # "measures" | "numerator" | "denominator"
        self.in_measure = False

    @staticmethod
    def _local(tag: str) -> str | None:
        return tag.split(":", 1)[1] if ":" in tag else None

    def handle_starttag(self, tag, attrs):
        local, a = self._local(tag), dict(attrs)
        if tag == "ix:nonfraction":
            self.open_facts.append([a, []])
        elif local == "context" and "id" in a:
            self.ctx = {"id": a["id"], "dimensions": []}
        elif self.ctx is not None:
            if local in ("startdate", "enddate", "instant"):
                self.text_target = {"startdate": "startDate", "enddate": "endDate", "instant": "instant"}[local]
                self.ctx.setdefault(self.text_target, "")
            elif local in ("explicitmember", "typedmember") and a.get("dimension"):
                self.member = (a["dimension"], [])
        elif local == "unit" and "id" in a:
            self.unit = {"id": a["id"], "measures": [], "numerator": [], "denominator": []}
            self.measure_target = "measures"
        elif self.unit is not None:
            if local == "unitnumerator":
                self.measure_target = "numerator"
            elif local == "unitdenominator":
                self.measure_target = "denominator"
            elif local == "measure":
                self.in_measure = True
                self.unit[self.measure_target].append("")

    def handle_endtag(self, tag):
        local = self._local(tag)
        if tag == "ix:nonfraction" and self.open_facts:
            attrs, parts = self.open_facts.pop()
            self.raw_facts.append((attrs, "".join(parts)))
        elif local == "context" and self.ctx is not None:
            _finish_context(self.ctx, self.contexts)
            self.ctx = None
        elif self.ctx is not None:
            if local in ("startdate", "enddate", "instant"):
                self.text_target = None
            elif local in ("explicitmember", "typedmember") and self.member:
                dim, parts = self.member
                self.ctx["dimensions"].append((dim, "".join(parts).strip()))
                self.member = None
        elif local == "unit" and self.unit is not None:
            u = self.unit
            self.units[u["id"]] = _unit_name(u["measures"], u["numerator"], u["denominator"])
            self.unit, self.measure_target = None, None
        elif self.unit is not None:
            if local == "measure":
                self.in_measure = False
            elif local in ("unitnumerator", "unitdenominator"):
                self.measure_target = "measures"

    def handle_data(self, data):
        for fact in self.open_facts:
            fact[1].append(data)
        if self.ctx is not None:
            if self.text_target:
                self.ctx[self.text_target] += data.strip()
            elif self.member:
                self.member[1].append(data)
        elif self.unit is not None and self.in_measure:
            self.unit[self.measure_target][-1] += data.strip()


def facts_from_inline(raw: bytes | str, accession: str) -> list[Fact]:
    """Every numeric fact in an inline XBRL HTML document, with scale and sign applied.
    Nil facts are skipped; a fact whose transform, context or unit cannot be resolved
    refuses the whole parse, because a silently dropped fact looks like an absent one."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    reader = _InlineReader(accession_dashed(accession))
    reader.feed(text)
    reader.close()
    out = []
    for attrs, body in reader.raw_facts:
        concept = attrs.get("name", "")
        where = f"{concept or '<unnamed>'} in context {attrs.get('contextref', '?')}"
        if attrs.get("xsi:nil", "").lower() == "true":
            continue
        if not concept or not attrs.get("contextref") or not attrs.get("unitref"):
            raise ContractViolation(f"Inline XBRL fact {where} lacks a name, contextRef or unitRef.")
        value = inline_number(body, attrs.get("format"), scale=attrs.get("scale"), sign=attrs.get("sign"), where=where)
        out.append(_make_fact(concept, value, attrs["unitref"], attrs["contextref"], reader.contexts, reader.units,
                              "inline", reader.accession, attrs.get("decimals")))
    return out


# ── plain XBRL instance (the *_htm.xml EDGAR extracts) ──────────────────────

def facts_from_instance(raw: bytes, accession: str) -> list[Fact]:
    """Numeric facts from a standalone XBRL instance document."""
    prefixes, root = {}, None
    try:
        for event, payload in ElementTree.iterparse(BytesIO(raw), events=("start-ns", "start")):
            if event == "start-ns":
                prefixes.setdefault(payload[1], payload[0])
            elif root is None:
                root = payload
    except ElementTree.ParseError as exc:
        raise ContractViolation(f"XBRL instance is not well-formed XML: {exc}") from exc
    if root is None:
        raise ContractViolation("XBRL instance is empty.")

    def qname(tag: str) -> str:
        if tag.startswith("{"):
            uri, local = tag[1:].split("}", 1)
            prefix = prefixes.get(uri)
            return f"{prefix}:{local}" if prefix else local
        return tag

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    contexts, units = {}, {}
    for el in root.iter():
        name = local(el.tag)
        if name == "context" and el.get("id"):
            ctx = {"id": el.get("id"), "dimensions": []}
            for child in el.iter():
                cn = local(child.tag)
                if cn in ("startDate", "endDate", "instant"):
                    ctx[cn] = (child.text or "").strip()
                elif cn in ("explicitMember", "typedMember") and child.get("dimension"):
                    ctx["dimensions"].append((child.get("dimension"), "".join(child.itertext()).strip()))
            _finish_context(ctx, contexts)
        elif name == "unit" and el.get("id"):
            measures, num, den = [], [], []
            for child in el:
                if local(child.tag) == "measure":
                    measures.append((child.text or "").strip())
                elif local(child.tag) == "divide":
                    for part in child:
                        target = {"unitNumerator": num, "unitDenominator": den}.get(local(part.tag))
                        if target is not None:
                            target.extend((m.text or "").strip() for m in part if local(m.tag) == "measure")
            units[el.get("id")] = _unit_name(measures, num, den)
    out = []
    for el in root:
        if el.get("contextRef") is None or el.get("unitRef") is None:
            continue
        if el.get("{http://www.w3.org/2001/XMLSchema-instance}nil", "").lower() == "true":
            continue
        concept = qname(el.tag)
        value = _decimal((el.text or "").strip(), f"{concept} in context {el.get('contextRef')}")
        out.append(_make_fact(concept, value, el.get("unitRef"), el.get("contextRef"), contexts, units,
                              "instance", accession_dashed(accession), el.get("decimals")))
    return out


# ── lookup: exactly one consolidated fact, or a refusal that says why ───────

def _fmt(value: Decimal) -> str:
    return f"{value:,f}"


def lookup(facts: list[Fact], concepts, *, end, months: int | None, unit: str = "USD",
           accession: str | None = None) -> Fact:
    """Return the one consolidated fact for a concept, period and unit.

    `concepts` is one concept or an ordered list of candidates; the first candidate with
    any consolidated fact for the period decides, and later candidates are not consulted.
    `months` is 3, 6, 9 or 12 for a duration, or None for an instant at `end`.
    Several facts that agree on the value count as one (the same number printed twice).
    Several that disagree are refused: that is a restatement or a tagging error, and
    guessing between them is exactly what this module exists to stop.
    """
    candidates = [concepts] if isinstance(concepts, str) else list(concepts)
    if not candidates:
        raise ContractViolation("XBRL lookup needs at least one concept.")
    end_date = end if isinstance(end, date) else _date(end, "lookup end")
    if months is not None and (not isinstance(months, int) or months <= 0):
        raise ContractViolation(f"XBRL lookup months must be a positive integer or None for an instant, not {months!r}.")
    wanted_accn = accession_dashed(accession) if accession else None
    pool = [f for f in facts if wanted_accn is None or f.accession == wanted_accn]
    period = f"instant {end_date.isoformat()}" if months is None else f"{months} months ended {end_date.isoformat()}"
    near = []
    for concept in candidates:
        with_concept = [f for f in pool if f.concept == concept]
        in_period = [f for f in with_concept if f.end == end_date and f.unit == unit and
                     ((months is None and f.instant) or (months is not None and f.months == months))]
        consolidated = [f for f in in_period if f.consolidated]
        if not consolidated:
            if in_period:
                near.append(f"{concept}: {len(in_period)} segment fact(s) for {period} excluded "
                            f"({'; '.join(', '.join(f'{a}={m}' for a, m in f.dimensions) for f in in_period[:3])})")
            elif with_concept:
                periods = sorted({f"{f.period} in {f.unit}" for f in with_concept})
                near.append(f"{concept}: only {', '.join(periods[:6])}")
            continue
        values = sorted({f.value for f in consolidated})
        if len(values) > 1:
            shown = "; ".join(f"{_fmt(f.value)} ({f.source} context {f.context}, accession {f.accession})"
                              for f in consolidated)
            hint = "" if wanted_accn else " Pass accession= to choose the filing."
            raise ContractViolation(f"Ambiguous XBRL fact: {concept} for {period} in {unit} has "
                                    f"{len(values)} different consolidated values: {shown}.{hint}")
        return consolidated[0]
    detail = f" Nearest: {' | '.join(near)}." if near else ""
    scope = f" in filing {wanted_accn}" if wanted_accn else ""
    raise ContractViolation(f"No consolidated XBRL fact for {' or '.join(candidates)} for {period} in {unit}{scope}.{detail}")


def definition(name: str) -> dict:
    spec = CONCEPTS.get(name)
    if not spec:
        raise ContractViolation(f"Unknown XBRL definition {name!r}; add it to xbrl.CONCEPTS. Known: {', '.join(CONCEPTS)}.")
    return spec


def find(facts: list[Fact], name: str, *, end, months: int | None, accession: str | None = None) -> Fact:
    """`lookup` driven by the concept map: the fact behind a Finrail definition name."""
    spec = definition(name)
    if spec["period"] == "duration" and months is None:
        raise ContractViolation(f"{name} is a duration figure; give months (3, 6 or 12), not an instant.")
    if spec["period"] == "instant" and months is not None:
        raise ContractViolation(f"{name} is an instant figure; months must be None.")
    return lookup(facts, spec["concepts"], end=end, months=months, unit=spec["unit"], accession=accession)


# ── reconcile: the HTML cell against the tag ────────────────────────────────

def reconcile(fact: Fact, value, unit_scale: str, tolerance=Decimal(0), *, presented: str = "as_tagged") -> dict:
    """Compare a figure read from an HTML table with an XBRL fact.

    `value` is the table figure as the table prints it, signed: a number in parentheses
    is negative. `unit_scale` is the table's unit line ("millions", "thousands", "units").
    `tolerance` is in the table's units too (0.5 means half a million on a table in
    millions). The fact is in base units; the table figure is scaled up to meet it.

    Sign: with `presented="as_tagged"` (net loss, revenue, operating cash flow) the table
    sign is the XBRL sign, so a loss printed "(3,000)" matches NetIncomeLoss of
    -3,000,000,000. With `presented="negated"` (capital expenditure and other cash
    outflows) the XBRL concept measures the payment, so the same parentheses match a
    positive PaymentsToAcquirePropertyPlantAndEquipment. `CONCEPTS` says which applies.
    """
    if presented not in ("as_tagged", "negated"):
        raise ContractViolation(f"presented must be 'as_tagged' or 'negated', not {presented!r}.")
    exponent = SCALES.get(str(unit_scale).strip().lower())
    if exponent is None:
        raise ContractViolation(f"Unknown table unit scale {unit_scale!r}; one of {', '.join(SCALES)}.")
    table = _decimal(value, "table value")
    tol = _decimal(tolerance, "tolerance")
    if tol < 0:
        raise ContractViolation("tolerance cannot be negative.")
    table_base = _canon(table.scaleb(exponent))
    expected = table_base if presented == "as_tagged" else -table_base
    difference = _canon(fact.value - expected)
    tol_base = _canon(tol.scaleb(exponent))
    return {
        "matched": abs(difference) <= tol_base,
        "difference": plain(difference),                    # base units, XBRL minus table
        "difference_in_table_units": plain(difference.scaleb(-exponent)),
        "tolerance": plain(tol), "tolerance_base_units": plain(tol_base),
        "table_value": plain(table), "table_scale": unit_scale, "table_value_base_units": plain(table_base),
        "presented": presented, "expected_xbrl_value": plain(expected),
        "xbrl_value": plain(fact.value), "xbrl_decimals": fact.decimals, "unit": fact.unit,
        "concept": fact.concept, "context": fact.context, "period": fact.period,
        "dimensions": [list(d) for d in fact.dimensions],
        "source": fact.source, "accession": fact.accession,
    }
