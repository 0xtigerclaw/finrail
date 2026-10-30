"""Offline contracts for the SEC XBRL reader. Fixtures are built by hand; no network."""
import json
from datetime import date
from decimal import Decimal

import pytest

from finrail.core.errors import ContractViolation, OpFailed
from finrail.extensions.finance import xbrl

ACCN = "0001628280-26-067168"

# A tiny inline XBRL filing: one consolidated six-month period (c-1), the same period
# for one segment (c-2), an instant (c-3), the comparative six months (c-4).
INLINE = """<html><head><title>fixture</title></head><body>
<div style="display:none"><ix:header><ix:hidden>
<ix:nonFraction name="dei:EntityCommonStockSharesOutstanding" contextRef="c-3" unitRef="shares" decimals="0" format="ixt:num-dot-decimal">1,000,000</ix:nonFraction>
</ix:hidden><ix:resources>
<xbrli:context id="c-1"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0002130357</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="c-2"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0002130357</xbrli:identifier>
  <xbrli:segment><xbrldi:explicitMember dimension="srt:ProductOrServiceAxis">anthropic:ApiMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
  <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="c-3"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0002130357</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>
<xbrli:context id="c-4"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0002130357</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-06-30</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
<xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
<xbrli:unit id="usdPerShare"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator>
  <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
</ix:resources></ix:header></div>
<table>
<tr><td>Revenue</td><td>$</td><td><ix:nonFraction name="us-gaap:Revenues" contextRef="c-1" unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal" id="f-1">4,500</ix:nonFraction></td>
    <td><ix:nonFraction name="us-gaap:Revenues" contextRef="c-4" unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">1,500</ix:nonFraction></td></tr>
<tr><td>API revenue</td><td><ix:nonFraction name="us-gaap:Revenues" contextRef="c-2" unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">3,100</ix:nonFraction></td></tr>
<tr><td>Net loss</td><td>(<ix:nonFraction name="us-gaap:NetIncomeLoss" contextRef="c-1" unitRef="usd" decimals="-6" scale="6" sign="-" format="ixt:num-dot-decimal">3,000</ix:nonFraction>)</td></tr>
<tr><td>Purchases of property and equipment</td><td>(<ix:nonFraction name="us-gaap:PaymentsToAcquirePropertyPlantAndEquipment" contextRef="c-1" unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">800</ix:nonFraction>)</td></tr>
<tr><td>Operating loss</td><td>(<ix:nonFraction name="us-gaap:OperatingIncomeLoss" contextRef="c-1" unitRef="usd" decimals="-6" scale="6" sign="-" format="ixt:num-dot-decimal">2,000</ix:nonFraction>)</td></tr>
<tr><td>Net loss per share</td><td>(<ix:nonFraction name="us-gaap:EarningsPerShareBasic" contextRef="c-1" unitRef="usdPerShare" decimals="2" sign="-" format="ixt:num-dot-decimal">0.52</ix:nonFraction>)</td></tr>
<tr><td>Dividends</td><td><ix:nonFraction name="us-gaap:PaymentsOfDividends" contextRef="c-1" unitRef="usd" decimals="-6" scale="6" format="ixt:fixed-zero">&#8212;</ix:nonFraction></td></tr>
<tr><td>Cash</td><td><ix:nonFraction name="us-gaap:CashAndCashEquivalentsAtCarryingValue" contextRef="c-3" unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">9,876</ix:nonFraction></td></tr>
<tr><td>Nil</td><td><ix:nonFraction name="us-gaap:Goodwill" contextRef="c-3" unitRef="usd" xsi:nil="true"></ix:nonFraction></td></tr>
</table>
<p>Note 2. Revenue was $<ix:nonFraction name="us-gaap:Revenues" contextRef="c-1" unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">4,500</ix:nonFraction> million.
Operating loss was $<ix:nonFraction name="us-gaap:OperatingIncomeLoss" contextRef="c-1" unitRef="usd" decimals="-6" scale="6" sign="-" format="ixt:num-dot-decimal">2,100</ix:nonFraction> million (inconsistent on purpose).</p>
</body></html>"""

COMPANYFACTS = {
    "cik": 2130357, "entityName": "Anthropic PBC",
    "facts": {
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2026-06-30", "val": 1000000, "accn": ACCN, "fy": 2026, "fp": "Q2", "form": "S-1", "filed": "2026-10-26"}]}}},
        "us-gaap": {
            "Revenues": {"units": {"USD": [
                {"start": "2026-01-01", "end": "2026-06-30", "val": 4500000000, "accn": ACCN, "fy": 2026, "fp": "Q2", "form": "S-1", "filed": "2026-10-26"},
                {"start": "2025-01-01", "end": "2025-06-30", "val": 1500000000, "accn": ACCN, "fy": 2026, "fp": "Q2", "form": "S-1", "filed": "2026-10-26"},
                # Fiscal 2025 as the S-1 reports it, and as an earlier filing reported it: a restatement.
                {"start": "2025-01-01", "end": "2025-12-31", "val": 4000000000, "accn": ACCN, "fy": 2026, "fp": "Q2", "form": "S-1", "filed": "2026-10-26"},
                {"start": "2025-01-01", "end": "2025-12-31", "val": 3900000000, "accn": "0000000000-26-000001", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-03-01"},
                # The same six-month figure carried into a later filing: agrees, so it is one fact.
                {"start": "2026-01-01", "end": "2026-06-30", "val": 4500000000, "accn": "0000000000-26-000002", "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-11-10"}]}},
            "NetIncomeLoss": {"units": {"USD": [
                {"start": "2026-01-01", "end": "2026-06-30", "val": -3000000000, "accn": ACCN, "fy": 2026, "fp": "Q2", "form": "S-1", "filed": "2026-10-26"}]}},
            "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
                {"start": "2026-01-01", "end": "2026-06-30", "val": 800000000, "accn": ACCN, "fy": 2026, "fp": "Q2", "form": "S-1", "filed": "2026-10-26"}]}},
            "EarningsPerShareBasic": {"units": {"USD/shares": [
                {"start": "2026-01-01", "end": "2026-06-30", "val": -0.52, "accn": ACCN, "fy": 2026, "fp": "Q2", "form": "S-1", "filed": "2026-10-26"}]}},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
                {"end": "2026-06-30", "val": 9876000000, "accn": ACCN, "fy": 2026, "fp": "Q2", "form": "S-1", "filed": "2026-10-26"}]}},
        }}}

INSTANCE = b"""<?xml version="1.0" encoding="utf-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:us-gaap="http://fasb.org/us-gaap/2025"
  xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <xbrli:context id="c-1"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0002130357</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="c-2"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0002130357</xbrli:identifier>
    <xbrli:segment><xbrldi:explicitMember dimension="srt:ProductOrServiceAxis">anthropic:ApiMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
  <xbrli:unit id="usdPerShare"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator>
    <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
  <us-gaap:Revenues contextRef="c-1" unitRef="usd" decimals="-6" id="f-1">4500000000</us-gaap:Revenues>
  <us-gaap:Revenues contextRef="c-2" unitRef="usd" decimals="-6" id="f-2">3100000000</us-gaap:Revenues>
  <us-gaap:NetIncomeLoss contextRef="c-1" unitRef="usd" decimals="-6" id="f-3">-3000000000</us-gaap:NetIncomeLoss>
  <us-gaap:EarningsPerShareBasic contextRef="c-1" unitRef="usdPerShare" decimals="2" id="f-4">-0.52</us-gaap:EarningsPerShareBasic>
  <us-gaap:Goodwill contextRef="c-1" unitRef="usd" xsi:nil="true"/>
</xbrli:xbrl>"""

END = date(2026, 6, 30)


@pytest.fixture
def inline():
    return xbrl.facts_from_inline(INLINE.encode(), ACCN)


@pytest.fixture
def cf():
    return xbrl.facts_from_companyfacts(COMPANYFACTS)


def one(facts, concept, context):
    """The fact for a concept and context; the same number tagged twice is still one fact."""
    found = {f for f in facts if f.concept == concept and f.context == context}
    assert len(found) == 1, found
    return found.pop()


# ── parse: inline ───────────────────────────────────────────────────────────

def test_inline_scale_sign_units_and_dimensions(inline):
    rev = one(inline, "us-gaap:Revenues", "c-1")
    assert rev.value == Decimal("4500000000") and rev.unit == "USD" and rev.decimals == "-6"
    assert rev.start == date(2026, 1, 1) and rev.end == END and rev.months == 6 and rev.consolidated
    assert rev.source == "inline" and rev.accession == ACCN
    loss = one(inline, "us-gaap:NetIncomeLoss", "c-1")
    assert loss.value == Decimal("-3000000000")
    seg = [f for f in inline if f.concept == "us-gaap:Revenues" and f.context == "c-2"][0]
    assert seg.dimensions == (("srt:ProductOrServiceAxis", "anthropic:ApiMember"),) and not seg.consolidated
    eps = one(inline, "us-gaap:EarningsPerShareBasic", "c-1")
    assert eps.value == Decimal("-0.52") and eps.unit == "USD/shares"
    shares = one(inline, "dei:EntityCommonStockSharesOutstanding", "c-3")
    assert shares.value == Decimal("1000000") and shares.unit == "shares" and shares.instant
    cash = one(inline, "us-gaap:CashAndCashEquivalentsAtCarryingValue", "c-3")
    assert cash.instant and cash.months is None and cash.period == "instant 2026-06-30"
    assert one(inline, "us-gaap:PaymentsOfDividends", "c-1").value == 0
    assert not [f for f in inline if f.concept == "us-gaap:Goodwill"]          # nil is skipped
    assert len([f for f in inline if f.concept == "us-gaap:Revenues" and f.context == "c-1"]) == 2


def test_inline_refuses_unknown_transform_and_dangling_refs():
    bad = INLINE.replace('format="ixt:fixed-zero"', 'format="ixt:num-words-fr"')
    with pytest.raises(ContractViolation, match="transform"):
        xbrl.facts_from_inline(bad, ACCN)
    with pytest.raises(ContractViolation, match="context 'c-9'"):
        xbrl.facts_from_inline(INLINE.replace('contextRef="c-3"', 'contextRef="c-9"'), ACCN)
    with pytest.raises(ContractViolation, match="unit 'eur'"):
        xbrl.facts_from_inline(INLINE.replace('unitRef="shares"', 'unitRef="eur"'), ACCN)


def test_inline_number_transforms():
    n = lambda text, fmt, **kw: xbrl.inline_number(text, fmt, scale=kw.get("scale"), sign=kw.get("sign"), where="t")
    assert n("1,234.5", "ixt:num-dot-decimal", scale="3") == Decimal("1234500")
    assert n("1.234,5", "ixt:num-comma-decimal") == Decimal("1234.5")
    assert n("1,234", "ixt:numdotdecimal", sign="-") == Decimal("-1234")
    assert n("\u2014", "ixt:zerodash") == 0 and n("none", "ixt-sec:numwordsen") == 0
    with pytest.raises(ContractViolation):
        n("twelve", "ixt-sec:numwordsen")
    with pytest.raises(ContractViolation):
        n("1,234", None, sign="+")
    with pytest.raises(ContractViolation):
        n("abc", "ixt:num-dot-decimal")


# ── parse: companyfacts and plain instance ──────────────────────────────────

def test_companyfacts_flatten_and_accession_filter(cf):
    assert all(f.source == "companyfacts" and f.consolidated for f in cf)
    loss = [f for f in cf if f.concept == "us-gaap:NetIncomeLoss"][0]
    assert loss.value == Decimal("-3000000000") and loss.months == 6 and loss.accession == ACCN
    assert [f for f in cf if f.concept == "us-gaap:EarningsPerShareBasic"][0].value == Decimal("-0.52")
    cash = [f for f in cf if f.concept == "us-gaap:CashAndCashEquivalentsAtCarryingValue"][0]
    assert cash.instant and cash.end == END
    only = xbrl.facts_from_companyfacts(COMPANYFACTS, accession=ACCN.replace("-", ""))
    assert {f.accession for f in only} == {ACCN} and len(only) == len(cf) - 2
    with pytest.raises(ContractViolation, match="facts table|not a date|not a number"):
        xbrl.facts_from_companyfacts({"facts": {"us-gaap": {"X": {"units": {"USD": [{"end": "soon", "val": 1}]}}}}})


def test_instance_xml(inline):
    facts = xbrl.facts_from_instance(INSTANCE, ACCN)
    assert {f.source for f in facts} == {"instance"}
    rev = one(facts, "us-gaap:Revenues", "c-1")
    assert rev.value == Decimal("4500000000") and rev.unit == "USD" and rev.months == 6
    assert one(facts, "us-gaap:Revenues", "c-2").dimensions == (("srt:ProductOrServiceAxis", "anthropic:ApiMember"),)
    assert one(facts, "us-gaap:EarningsPerShareBasic", "c-1").unit == "USD/shares"
    assert not [f for f in facts if f.concept == "us-gaap:Goodwill"]
    assert rev.same_measure(one(inline, "us-gaap:Revenues", "c-1"))
    with pytest.raises(ContractViolation, match="well-formed"):
        xbrl.facts_from_instance(b"<xbrli:xbrl", ACCN)


# ── lookup ──────────────────────────────────────────────────────────────────

def test_lookup_returns_the_one_consolidated_fact(inline, cf):
    for facts in (inline, cf):
        rev = xbrl.lookup(facts, ["us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"],
                          end=END, months=6)
        assert rev.value == Decimal("4500000000") and rev.consolidated
        assert xbrl.lookup(facts, "us-gaap:NetIncomeLoss", end="2026-06-30", months=6).value == Decimal("-3000000000")
        cash = xbrl.lookup(facts, "us-gaap:CashAndCashEquivalentsAtCarryingValue", end=END, months=None)
        assert cash.value == Decimal("9876000000")
    # the second candidate is used only when the first has nothing for the period
    rev = xbrl.lookup(inline, ["us-gaap:SalesRevenueNet", "us-gaap:Revenues"], end=END, months=6)
    assert rev.concept == "us-gaap:Revenues"


def test_lookup_excludes_segment_facts_and_says_so(inline):
    only_segment = [f for f in inline if not (f.concept == "us-gaap:Revenues" and f.context == "c-1")]
    with pytest.raises(ContractViolation) as err:
        xbrl.lookup(only_segment, "us-gaap:Revenues", end=END, months=6)
    assert "segment fact(s)" in str(err.value) and "anthropic:ApiMember" in str(err.value)


def test_lookup_refuses_ambiguous_duplicates(inline, cf):
    with pytest.raises(ContractViolation) as err:
        xbrl.lookup(inline, "us-gaap:OperatingIncomeLoss", end=END, months=6)
    msg = str(err.value)
    assert "2 different consolidated values" in msg and "-2,000,000,000" in msg and "-2,100,000,000" in msg
    # companyfacts: fiscal 2025 restated between two filings; refused without an accession
    with pytest.raises(ContractViolation, match="Pass accession="):
        xbrl.lookup(cf, "us-gaap:Revenues", end="2025-12-31", months=12)
    assert xbrl.lookup(cf, "us-gaap:Revenues", end="2025-12-31", months=12, accession=ACCN).value == Decimal("4000000000")
    # the same value in two filings is one fact, not an ambiguity
    assert xbrl.lookup(cf, "us-gaap:Revenues", end=END, months=6).value == Decimal("4500000000")


def test_lookup_refuses_missing_with_nearest(inline):
    with pytest.raises(ContractViolation) as err:
        xbrl.lookup(inline, "us-gaap:Revenues", end=END, months=12)
    msg = str(err.value)
    assert "No consolidated XBRL fact" in msg and "12 months ended 2026-06-30" in msg and "6 months 2026-01-01 to 2026-06-30" in msg
    with pytest.raises(ContractViolation, match="instant 2026-06-30"):
        xbrl.lookup(inline, "us-gaap:Revenues", end=END, months=None)
    with pytest.raises(ContractViolation, match="in EUR"):
        xbrl.lookup(inline, "us-gaap:Revenues", end=END, months=6, unit="EUR")
    with pytest.raises(ContractViolation, match="in filing"):
        xbrl.lookup(inline, "us-gaap:Revenues", end=END, months=6, accession="0000000000-26-000009")
    with pytest.raises(ContractViolation):
        xbrl.lookup(inline, [], end=END, months=6)
    with pytest.raises(ContractViolation):
        xbrl.lookup(inline, "us-gaap:Revenues", end=END, months=0)


def test_find_uses_the_concept_map(inline):
    assert xbrl.find(inline, "revenue", end=END, months=6).concept == "us-gaap:Revenues"
    assert xbrl.find(inline, "capex", end=END, months=6).value == Decimal("800000000")
    with pytest.raises(ContractViolation, match="Unknown XBRL definition"):
        xbrl.find(inline, "ebitda", end=END, months=6)
    with pytest.raises(ContractViolation, match="duration"):
        xbrl.find(inline, "revenue", end=END, months=None)
    for name, spec in xbrl.CONCEPTS.items():
        assert spec["presented"] in ("as_tagged", "negated") and spec["concepts"], name


# ── reconcile ───────────────────────────────────────────────────────────────

def test_reconcile_as_tagged_both_directions(inline):
    loss = xbrl.find(inline, "net_loss", end=END, months=6)
    # the table prints (3,000) in millions: a negative figure, as XBRL stores it
    ok = xbrl.reconcile(loss, Decimal("-3000"), "millions")
    assert ok["matched"] and ok["difference"] == "0" and ok["concept"] == "us-gaap:NetIncomeLoss"
    assert ok["table_value_base_units"] == "-3000000000" and ok["context"] == "c-1" and ok["xbrl_decimals"] == "-6"
    # passing the unsigned figure is the sign mistake the verdict must catch
    bad = xbrl.reconcile(loss, Decimal("3000"), "millions")
    assert not bad["matched"] and bad["difference"] == "-6000000000"
    rev = xbrl.find(inline, "revenue", end=END, months=6)
    assert xbrl.reconcile(rev, "4500", "millions")["matched"]
    assert xbrl.reconcile(rev, "4500000", "thousands")["matched"]
    assert xbrl.reconcile(rev, "4500000000", "units")["matched"]
    assert not xbrl.reconcile(rev, "4500", "thousands")["matched"]


def test_reconcile_negated_outflow(inline):
    capex = xbrl.find(inline, "cash_purchases", end=END, months=6)
    assert capex.value == Decimal("800000000")
    presented = xbrl.CONCEPTS["cash_purchases"]["presented"]
    assert presented == "negated"
    # the cash flow statement prints (800): negative in the table, positive in XBRL
    assert xbrl.reconcile(capex, Decimal("-800"), "millions", presented=presented)["matched"]
    assert not xbrl.reconcile(capex, Decimal("-800"), "millions")["matched"]
    assert not xbrl.reconcile(capex, Decimal("800"), "millions", presented=presented)["matched"]


def test_reconcile_tolerance_in_table_units(inline):
    rev = xbrl.find(inline, "revenue", end=END, months=6)
    off = xbrl.reconcile(rev, Decimal("4500.4"), "millions")
    assert not off["matched"] and off["difference_in_table_units"] == "-0.4"
    within = xbrl.reconcile(rev, Decimal("4500.4"), "millions", Decimal("0.5"))
    assert within["matched"] and within["tolerance"] == "0.5" and within["tolerance_base_units"] == "500000"
    with pytest.raises(ContractViolation, match="negative"):
        xbrl.reconcile(rev, "4500", "millions", Decimal("-1"))
    with pytest.raises(ContractViolation, match="unit scale"):
        xbrl.reconcile(rev, "4500", "lakhs")
    with pytest.raises(ContractViolation, match="presented"):
        xbrl.reconcile(rev, "4500", "millions", presented="flipped")
    with pytest.raises(ContractViolation, match="table value"):
        xbrl.reconcile(rev, "4,500", "millions")


# ── fetching ────────────────────────────────────────────────────────────────

def test_urls_and_identifiers():
    assert xbrl.cik10("2130357") == "0002130357" and xbrl.cik10(1045810) == "0001045810" and xbrl.cik10("CIK0001045810") == "0001045810"
    assert xbrl.companyfacts_url("0002130357") == "https://data.sec.gov/api/xbrl/companyfacts/CIK0002130357.json"
    assert xbrl.accession_dashed("000162828026067168") == ACCN
    urls = xbrl.filing_urls("0002130357", ACCN, "anthropicpbc-sx1.htm")
    assert urls["primary"] == "https://www.sec.gov/Archives/edgar/data/2130357/000162828026067168/anthropicpbc-sx1.htm"
    assert urls["instance"].endswith("/anthropicpbc-sx1_htm.xml") and urls["index"].endswith("/index.json")
    for bad in ("12345678901", "abc"):
        with pytest.raises(ContractViolation):
            xbrl.cik10(bad)
    with pytest.raises(ContractViolation):
        xbrl.accession_dashed("1234")
    with pytest.raises(ContractViolation):
        xbrl.filing_urls("2130357", ACCN, "../evil.htm")


def test_fetch_is_injected_and_user_agent_is_set(monkeypatch):
    calls = []

    def fetch(url, *, limit):
        calls.append((url, limit))
        if url.endswith(".json"):
            return json.dumps(COMPANYFACTS).encode()
        return INLINE.encode()

    data = xbrl.load_companyfacts("2130357", fetch=fetch)
    assert data["entityName"] == "Anthropic PBC"
    facts = xbrl.load_filing_facts("2130357", ACCN, "anthropicpbc-sx1.htm", fetch=fetch)
    assert xbrl.lookup(facts, "us-gaap:Revenues", end=END, months=6).value == Decimal("4500000000")
    assert [c[0] for c in calls] == [xbrl.companyfacts_url("2130357"),
                                     xbrl.filing_urls("2130357", ACCN, "anthropicpbc-sx1.htm")["primary"]]
    monkeypatch.setenv("FINRAIL_SEC_USER_AGENT", "Example Fund research@example.com")
    assert xbrl._request("https://data.sec.gov/x").get_header("User-agent") == "Example Fund research@example.com"
    monkeypatch.delenv("FINRAIL_SEC_USER_AGENT")
    assert "FINRAIL_SEC_USER_AGENT" in xbrl._request("https://data.sec.gov/x").get_header("User-agent")
    with pytest.raises(ContractViolation, match="HTTPS"):
        xbrl.http_get("http://data.sec.gov/x")
    with pytest.raises(OpFailed, match="not JSON"):
        xbrl.load_companyfacts("2130357", fetch=lambda url, *, limit: b"<html>")
