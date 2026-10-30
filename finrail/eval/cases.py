"""THE CASE FORMAT: what is asked, and what must come out.

A case file is one JSON object: `{"schema": "finrail.eval.cases/1", "name": ..., "cases": [...]}`.
Each case:

    id          unique, kebab-case; baselines and summaries key on it
    title       the row label a report shows
    report_id   a catalog source id (NVDA-Q1-FY2027, ANTH-S1-2026, ...)
    capability  finance.reports.analyze or finance.reports.search
    params      the capability's parameters, without report_id: `definition` for
                analyze, `query` for search
    expect      {"outcome": "passed"}: the call must produce an answer
                {"outcome": "figure", "amount": "48554"}: and the amount must be this
                {"outcome": "refused", "reason": "Source-cell check"}: the call must be
                refused, and the refusal must carry this substring or this `code`
    inject      optional tamper applied to the model's answer AT THE SEAM, before the
                code checks: other_column, invented_row, drop_input, wrong_report. This
                is how an adversarial case is driven on a live engine, whose answer
                cannot be chosen; the same checks that refuse a model's miss must refuse
                the tamper
    tags        free strings for filtering (--tag)
    draft       true when the expected outcome is not grounded in a documented figure;
                a report names every draft, and a draft never backs a headline claim
    source      where the expectation comes from, in words

Validation refuses a malformed case with the id and the field, never a traceback.
"""
import json
import pathlib
import re

SCHEMA = "finrail.eval.cases/1"
CAPABILITIES = ("finance.reports.analyze", "finance.reports.search")
OUTCOMES = ("passed", "figure", "refused")
TAMPERS = ("other_column", "invented_row", "drop_input", "wrong_report")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,79}$")
_AMOUNT = re.compile(r"^-?\d{1,12}(?:\.\d{1,6})?$")


class CaseError(ValueError):
    """A case file that cannot be run. The message names the case and the field."""


def _known_reports() -> set:
    from finrail.extensions.finance import public_reports
    return {item["source_id"] for item in public_reports.catalog()}


def load(path) -> dict:
    """Read and validate a case file. Returns the parsed document."""
    p = pathlib.Path(path)
    try:
        doc = json.loads(p.read_text())
    except OSError as e:
        raise CaseError(f"{p}: cannot read: {e}") from e
    except ValueError as e:
        raise CaseError(f"{p}: not JSON: {e}") from e
    validate(doc, where=str(p))
    return doc


def validate(doc: dict, where: str = "cases") -> list:
    """Every case, checked. Returns the list of cases, or raises CaseError."""
    if not isinstance(doc, dict):
        raise CaseError(f"{where}: the case file must be a JSON object")
    if doc.get("schema") != SCHEMA:
        raise CaseError(f"{where}: schema must be {SCHEMA!r}, got {doc.get('schema')!r}")
    cases = doc.get("cases")
    if not isinstance(cases, list) or not cases:
        raise CaseError(f"{where}: 'cases' must be a non-empty list")
    known = _known_reports()
    seen = set()
    for n, case in enumerate(cases):
        label = f"{where} case #{n + 1}"
        if not isinstance(case, dict):
            raise CaseError(f"{label}: must be an object")
        cid = case.get("id")
        if not isinstance(cid, str) or not _ID.match(cid):
            raise CaseError(f"{label}: 'id' must be kebab-case (a-z, 0-9, -), got {cid!r}")
        label = f"{where} case {cid!r}"
        if cid in seen:
            raise CaseError(f"{label}: duplicate id")
        seen.add(cid)
        for field in ("title", "report_id", "capability"):
            if not isinstance(case.get(field), str) or not case[field].strip():
                raise CaseError(f"{label}: '{field}' must be a non-empty string")
        if case["capability"] not in CAPABILITIES:
            raise CaseError(f"{label}: 'capability' must be one of {', '.join(CAPABILITIES)}")
        params = case.get("params")
        if not isinstance(params, dict):
            raise CaseError(f"{label}: 'params' must be an object")
        if "report_id" in params:
            raise CaseError(f"{label}: put report_id on the case, not in params")
        expect = case.get("expect")
        if not isinstance(expect, dict) or expect.get("outcome") not in OUTCOMES:
            raise CaseError(f"{label}: 'expect.outcome' must be one of {', '.join(OUTCOMES)}")
        outcome = expect["outcome"]
        if outcome == "figure":
            amount = expect.get("amount")
            if not isinstance(amount, str) or not _AMOUNT.match(amount):
                raise CaseError(f"{label}: 'expect.amount' must be a decimal string in "
                                f"millions, got {amount!r}")
        elif outcome == "passed" and "amount" in expect:
            raise CaseError(f"{label}: an expected amount means outcome 'figure'")
        elif outcome == "refused":
            if not (isinstance(expect.get("reason"), str) and expect["reason"].strip()) \
                    and not (isinstance(expect.get("code"), str) and expect["code"].strip()):
                raise CaseError(f"{label}: a refusal needs 'expect.reason' (substring) or "
                                f"'expect.code'")
        if case["capability"] == "finance.reports.analyze":
            if not isinstance(params.get("definition"), str) or not params["definition"]:
                raise CaseError(f"{label}: analyze needs params.definition")
        else:
            if not isinstance(params.get("query"), str) or not params["query"].strip():
                raise CaseError(f"{label}: search needs params.query")
        if case["report_id"] not in known and outcome != "refused":
            raise CaseError(f"{label}: report {case['report_id']!r} is not in the catalog; "
                            f"only a case that expects a refusal may name one")
        inject = case.get("inject")
        if inject is not None:
            if not isinstance(inject, dict) or inject.get("tamper") not in TAMPERS:
                raise CaseError(f"{label}: 'inject.tamper' must be one of {', '.join(TAMPERS)}")
            if outcome != "refused":
                raise CaseError(f"{label}: a tampered answer must expect a refusal")
        tags = case.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise CaseError(f"{label}: 'tags' must be a list of strings")
        if not isinstance(case.get("draft", False), bool):
            raise CaseError(f"{label}: 'draft' must be true or false")
    return cases


def select(cases: list, tags=(), ids=()) -> list:
    """The cases that carry every requested tag and, when ids are given, one of the ids."""
    out = []
    for case in cases:
        if tags and not set(tags) <= set(case.get("tags") or []):
            continue
        if ids and case["id"] not in ids:
            continue
        out.append(case)
    return out
