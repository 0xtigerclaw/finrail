"""The checker against the real filings, offline.

`tests/filings/` holds slices cut from the issuers' own bytes by `scripts/slice_filing.py`:
only the tables each profile references, each with the prose above it, every other
table an empty placeholder so table N of the slice is table N of the filing. Each
slice's sidecar JSON records the original's SHA-256, the byte range and SHA-256 of every
kept segment, and the slice's own SHA-256. These tests prove, without a network:

  * the committed bytes are the ones the sidecar describes, segment by segment;
  * the profile's table ordinals land on the statements they claim to;
  * every row label the profile names exists exactly once in its table;
  * `source_cells` reads the published figures from those tables;
  * the definitions compute from them and reconcile where the profile says they do;
  * whether the sidecar's original hash is the catalog hash (strict xfail where the
    issuer's bytes could not be matched to the catalog when the slice was cut).

`scripts/check_originals.py`, run weekly by CI, re-downloads the originals and checks
that they still cut to these exact slices. Nothing here edits the profiles or the catalog.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from finrail.core.errors import ContractViolation
from finrail.extensions.finance import public_reports as r

FILINGS = Path(__file__).parent / "filings"
SIDECARS = {path.stem: json.loads(path.read_text()) for path in sorted(FILINGS.glob("*.json"))}
SLICED = [sid for sid, side in SIDECARS.items() if "slice" in side]
RETRIEVAL_ONLY = [sid for sid, side in SIDECARS.items() if side.get("retrieval_only")]

# What the issuers published, in millions, as the unsigned magnitudes `source_cells` stores.
PUBLISHED = {
    "ANTH-S1-2026": {  # six months ended 30 June 2026
        "revenue": "16286", "net_loss": "80748", "fv_convertibles": "72388",
        "operating_cash_flow": "3929", "cash_purchases": "482", "cash_flow_net_loss": "80748",
        "summary_operating_cash_flow": "3929", "revenue_total": "16286",
        "adjusted_operating_income": "1618", "stock_comp": "2185", "aoi_operating_loss": "693",
        "aoi_charitable": "126", "aoi_settlement": "0"},
    "NVDA-Q2-FY2027": {  # quarter ended 26 July 2026
        "operating_cash_flow": "24077", "cash_purchases": "2677", "principal_payments": "59", "reported_fcf": "21341"},
    "NVDA-Q1-FY2027": {  # quarter ended 26 April 2026
        "operating_cash_flow": "50344", "cash_purchases": "1757", "principal_payments": "33", "reported_fcf": "48554"},
}
# What the statement at each profile ordinal must call itself, in its header or the caption above it.
STATEMENTS = {
    "ANTH-S1-2026": {61: "Adjusted Operating Income (Loss)", 72: "summary of our cash flows",
                     109: "Consolidated Statements of Operations", 114: "Consolidated Statements of Cash Flows",
                     120: "consumption-based and subscription-based revenue"},
    "NVDA-Q1-FY2027": {5: "CONDENSED CONSOLIDATED STATEMENTS OF CASH FLOWS", 6: "RECONCILIATION OF GAAP TO NON-GAAP FINANCIAL MEASURES"},
    "NVDA-Q2-FY2027": {5: "CONDENSED CONSOLIDATED STATEMENTS OF CASH FLOWS", 6: "RECONCILIATION OF GAAP TO NON-GAAP FINANCIAL MEASURES"},
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(sid: str):
    """The slice's bytes and a catalog entry that expects the slice's hash, so
    `source_cells` runs on the slice exactly as it runs on the archived original."""
    side = SIDECARS[sid]
    raw = (FILINGS / side["slice"]["file"]).read_bytes()
    item = copy.deepcopy(r.report(sid))
    item["sha256"] = side["slice"]["sha256"]
    return side, raw, item


def parsed(raw: bytes) -> r.Tables:
    parser = r.Tables()
    parser.feed(raw.decode("utf-8"))
    parser.close()
    return parser


def test_every_catalogued_report_has_a_sidecar():
    assert set(SIDECARS) == {item["source_id"] for item in r.catalog()}
    assert sorted(SLICED) == ["ANTH-S1-2026", "NVDA-Q1-FY2027", "NVDA-Q2-FY2027"]
    for sid in RETRIEVAL_ONLY:
        assert r.report(sid)["format"] == "pdf" and not (FILINGS / f"{sid}.pdf").exists()


def test_committed_filings_stay_small():
    assert sum(path.stat().st_size for path in FILINGS.iterdir()) < 1_000_000


@pytest.mark.parametrize("sid", SLICED)
def test_slice_bytes_are_the_ones_the_sidecar_describes(sid):
    side, raw, _item = load(sid)
    assert sha256(raw) == side["slice"]["sha256"] and len(raw) == side["slice"]["bytes"]
    assert side["slice"]["tool"] == "scripts/slice_filing.py"
    for kept in side["kept"]:
        segment = raw[kept["slice"]["start"]:kept["slice"]["end"]]
        # The same bytes, by hash, as the range of the original the sidecar names.
        assert sha256(segment) == kept["original"]["sha256"]
        assert len(segment) == kept["original"]["table_end"] - kept["original"]["prose_start"]
        assert segment.lstrip().startswith(b"<") and segment.rstrip().endswith(b"</table>")
        assert kept["original"]["prose_start"] <= kept["original"]["table_start"] < kept["original"]["table_end"]
    ordinals = sorted(k["table"] for k in side["kept"])
    assert ordinals == sorted(r.profile(r.report(sid))["tables"])
    assert all(k["slice_table"] == k["table"] for k in side["kept"])
    assert sorted(ordinals + side["placeholders"]) == list(range(1, max(ordinals) + 1))
    assert len(parsed(raw).tables) == max(ordinals)


@pytest.mark.parametrize("sid", [pytest.param(sid, marks=[] if SIDECARS[sid]["catalog_match"] else
                                              pytest.mark.xfail(strict=True, reason=SIDECARS[sid].get("note", "catalog hash not reproduced")))
                                 for sid in SIDECARS])
def test_sidecar_original_hash_is_the_catalog_hash(sid):
    side, item = SIDECARS[sid], r.report(sid)
    assert side["catalog_sha256"] == item["sha256"]
    assert item["sha256"] in (side["original"]["sha256"], side["original"].get("served_sha256"))


@pytest.mark.parametrize("sid", SIDECARS)
def test_sidecar_records_where_the_bytes_came_from(sid):
    side, item = SIDECARS[sid], r.report(sid)
    assert side["original"]["url"] == item["url"] and side["original"]["retrieved_at"].startswith("2026-")
    assert len(side["original"]["sha256"]) == 64 and side["original"]["bytes"] > 0
    assert side["catalog_match"] == (item["sha256"] in (side["original"]["sha256"], side["original"].get("served_sha256")))
    if not side["catalog_match"]:
        assert "catalog" in side["note"].lower()


@pytest.mark.parametrize("sid,table", [(sid, table) for sid in SLICED for table in sorted(r.profile(r.report(sid))["tables"])])
def test_profile_table_ordinal_lands_on_the_statement_it_names(sid, table):
    _side, raw, item = load(sid)
    prof, parser = r.profile(item), parsed(raw)
    rows, caption = parser.tables[table - 1], parser.captions[table - 1]
    header = " ".join(" ".join(row) for row in rows[:12])
    period_text, unit_text = prof["tables"][table]["header"]
    assert period_text in header, (sid, table, header[:300])
    assert unit_text.lower() in (header + " " + caption).lower(), (sid, table, caption[-300:])
    assert item["period_end"][:4] in header
    assert STATEMENTS[sid][table] in header + " " + caption, (sid, table, caption[-300:])
    if prof["column"]["kind"] == "first":
        assert item["source_date_label"] in header


@pytest.mark.parametrize("sid,key", [(sid, key) for sid in SLICED for key in r.profile(r.report(sid))["rows"]])
def test_each_profile_row_label_exists_exactly_once_in_its_table(sid, key):
    _side, raw, item = load(sid)
    spec = r.profile(item)["rows"][key]
    rows = parsed(raw).tables[spec["table"] - 1]
    assert sum(spec["label"] in row for row in rows) == 1, (sid, key, spec["label"])


@pytest.mark.parametrize("sid", SLICED)
def test_source_cells_read_the_published_figures(sid):
    side, raw, item = load(sid)
    cells = r.source_cells(item, raw)
    assert {key: cell["value"] for key, cell in cells.items()} == PUBLISHED[sid]
    prof = r.profile(item)
    for key, cell in cells.items():
        assert cell["table"] == prof["rows"][key]["table"] and cell["sign"] == prof["rows"][key]["sign"]
        assert cell["row"] == prof["rows"][key]["label"] and cell["row"] in cell["source_row"]
    if prof["column"]["kind"] == "under":
        for cell in cells.values():
            assert prof["column"]["period"] in cell["column"] and item["period_end"][:4] in cell["column"]
            assert cell["html_cell"] == 10  # fiscal years 2023-2025, six months 2025, then six months 2026
    else:
        assert {cell["column"] for cell in cells.values()} == {prof["column"]["note"]}


@pytest.mark.parametrize("sid", SLICED)
def test_source_cells_refuse_a_slice_whose_bytes_moved(sid):
    _side, raw, item = load(sid)
    altered = raw.replace(b"16,286", b"16,287", 1) if sid.startswith("ANTH") else raw.replace(b"24,077", b"24,078", 1) if sid.endswith("Q2-FY2027") else raw.replace(b"50,344", b"50,345", 1)
    assert altered != raw
    with pytest.raises(ContractViolation, match="SHA-256 mismatch"):
        r.source_cells(item, altered)


def candidate(sid: str, definition: str):
    """What a model would propose for `definition`, every value the archived cell's."""
    side, raw, item = load(sid)
    cells = r.source_cells(item, raw)
    prof = r.profile(item)
    names = r.labels(prof)
    lines = {key: number for number, key in enumerate(names, 1)}
    evidence = [{"id": "E1", "text": "\n".join(cells[key]["source_row"] for key in names), "source_uri": item["indexed_uris"][0]}]
    found = {"report_id": sid, "evidence": evidence, "backend": "committed slice"}
    terms = prof["definitions"][definition]["terms"]
    proposed = {"report_id": sid, "period_end": item["period_end"], "currency": "USD", "unit": "million", "definition": definition,
                "inputs": [{"metric": key, "value": cells[key]["value"], "row_id": f"E1:L{lines[key]}"} for _sign, key in terms]}
    return item, cells, found, proposed


@pytest.mark.parametrize("sid,definition,amount,reconciled", [
    ("NVDA-Q2-FY2027", "issuer_fcf", "21341", "Free cash flow"),
    ("NVDA-Q2-FY2027", "simple_fcf", "21400", None),
    ("NVDA-Q1-FY2027", "issuer_fcf", "48554", "Free cash flow"),
    ("ANTH-S1-2026", "revenue", "16286", "Total"),
    ("ANTH-S1-2026", "net_loss", "80748", "Net loss"),
    ("ANTH-S1-2026", "operating_cash_flow", "3929", "Operating activities"),
    ("ANTH-S1-2026", "capex", "482", None),
    ("ANTH-S1-2026", "simple_fcf", "3447", None),
    ("ANTH-S1-2026", "house_operating_income", "-567", "Anthropic's Adjusted Operating Income, recomputed from its own bridge"),
])
def test_definitions_compute_from_the_real_cells_and_reconcile(sid, definition, amount, reconciled):
    item, cells, found, proposed = candidate(sid, definition)
    out = r.check_and_compute(item, definition, proposed, found, cells)
    assert out["amount"] == amount and out["currency"] == "USD" and out["unit"] == "million"
    assert (out["reconciled"] or {}).get("label") == reconciled


def test_anthropic_adjusted_operating_income_recomputes_from_its_own_bridge():
    _side, raw, item = load("ANTH-S1-2026")
    cells = r.source_cells(item, raw)
    spec = r.profile(item)["definitions"]["house_operating_income"]["verify"]
    # Terms act on the unsigned magnitudes the cells store: minus the 693 operating loss, plus 2,185, 126 and nil.
    total = sum((-1 if sign == "-" else 1) * int(cells[key]["value"]) for sign, key in spec["terms"])
    assert total == int(cells[spec["target"]]["value"]) == 1618


@pytest.mark.parametrize("sid,value", [("ANTH-S1-2026", "1163"), ("ANTH-S1-2026", "4585"), ("NVDA-Q2-FY2027", "50344")])
def test_a_comparative_or_prior_quarter_figure_is_refused_on_the_real_rows(sid, value):
    definition = "revenue" if sid.startswith("ANTH") else "issuer_fcf"
    item, cells, found, proposed = candidate(sid, definition)
    proposed["inputs"][0]["value"] = value
    with pytest.raises(ContractViolation, match="Source-cell check refused"):
        r.check_and_compute(item, definition, proposed, found, cells)
