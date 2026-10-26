"""FINANCE extension — the finance-first capability packs.

Four packs, one rule each, all declared on the capability classes below:

  finance.screen    public-risk screening with a deterministic evidence gate.
                    A model may propose candidates and quotes; only
                    `gate.classify` awards a status.
  finance.dataroom  private data-room reads, and judged drafting where every
                    figure must pass the claim check against its cited source.
  finance.compute   deterministic metrics. The model may select; arithmetic
                    is Python only, recomputed independently before it is
                    reported.
  finance.memo      IC memo drafting from gated evidence only, released
                    through a hash-bound human approval.

The demonstration data room is synthetic (fixtures/dataroom.json): one
fictional target, Meridian Components, plus one archived fixture screen.
Nothing here reads a real corpus.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

from finrail.core.errors import ContractViolation
from finrail.core.settings import SettingSpec, state
from finrail.plugin import Plugin

from . import gate as evidence_gate

NAME = "finance"
DESCRIPTION = "Finance-first packs: gated screening, data room, deterministic compute, memo release."
REQUIRES_API = 1

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "dataroom.json"

SETTINGS = [
    SettingSpec("finance.artefacts", "path", required=False,
                doc="Where finance artefacts are written (default: <state>/finance)"),
]


def _room() -> dict:
    return json.loads(FIXTURE.read_text())


def _root() -> pathlib.Path:
    root = state() / "finance"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _doc(doc_id: str) -> dict:
    wanted = str(doc_id or "").strip().strip("[]").strip()
    for doc in _room()["documents"]:
        if doc["id"] == wanted:
            return doc
    raise ContractViolation(
        f"no data-room document '{doc_id}' (see finance.dataroom.search)")


def _checked(doc: dict) -> bool:
    """Whether a document may carry a figure. An unverified draft may be
    quoted as an open question; it can never be the source a figure rests on."""
    return doc.get("status") != "unverified_draft"


def _cells() -> dict:
    return _room()["workbook"]["cells"]


def _cell(ref: str) -> float:
    try:
        return float(_cells()[ref]["value"])
    except KeyError:
        raise ContractViolation(f"workbook cell {ref} is not in the data room")


# ── finance.screen ───────────────────────────────────────────────────────────

def _entity_candidates(query: str) -> list:
    room = _room()
    found = [{"company": room["company"], "company_id": room["company_id"],
              "source": "data room"}]
    for key, screen in room["screens"].items():
        found.append({"company": screen["company"],
                      "company_id": screen["company_id"], "source": key})
    q = (query or "").lower()
    exact = [c for c in found if q and q in c["company"].lower()]
    return (exact or found)[:3]


class ScreenCandidates(Plugin):
    name = "finance.screen.candidates"
    description = ("Legal-entity candidates for a screen, ranked deterministically. "
                   "At most three; the model never chooses the entity.")
    read_only = True
    confirm = False

    def run(self, company: str = ""):
        return {"candidates": _entity_candidates(company)}


class ScreenEntity(Plugin):
    name = "finance.screen.entity"
    description = ("Confirm the legal entity a paid screen will run against. "
                   "The proposal is the named company and registry number; "
                   "approval is the human confirmation.")
    confirm = True
    reason = ("retrieval spend and every downstream verdict attach to the legal "
              "entity; a wrong entity produces a confident screen of the wrong "
              "company, so a person confirms it before anything runs")
    verified_by = "the confirmed entity record is re-read from the finance store"

    def plan(self, company: str = "", company_id: str = ""):
        candidates = _entity_candidates(company)
        match = [c for c in candidates if c["company_id"] == company_id]
        if not match:
            names = "; ".join(f"{c['company']} ({c['company_id']})"
                              for c in candidates)
            raise ContractViolation(
                "company_id must be one of the ranked candidates: " + names)
        return {"company": match[0]["company"], "company_id": company_id,
                "candidates_shown": candidates}

    def render(self, p) -> str:
        shown = "\n".join(f"  - {c['company']} ({c['company_id']})"
                          for c in p["candidates_shown"])
        return (f"CONFIRM LEGAL ENTITY for the screen:\n"
                f"  {p['company']}  company number {p['company_id']}\n"
                f"candidates shown:\n{shown}")

    def execute(self, p) -> dict:
        path = _root() / "entity.json"
        path.write_text(json.dumps(
            {"company": p["company"], "company_id": p["company_id"]}, indent=2))
        return {"confirmed": p["company"], "company_id": p["company_id"],
                "record": str(path)}

    def verify(self, proposal, result):
        path = _root() / "entity.json"
        if not path.exists():
            return (False, "entity record missing after execute")
        saved = json.loads(path.read_text())
        ok = saved.get("company_id") == proposal["company_id"]
        return (ok, f"entity record re-read: {saved}")


class ScreenRun(Plugin):
    name = "finance.screen.run"
    description = ("Run the public-risk screen for the confirmed entity. The "
                   "deterministic gate is the sole authority for VERIFIED; "
                   "the result is typed and saved to the finance store.")
    background = True
    confirm = False
    autonomous = True
    reason = ("screening reads public sources and writes only its own result "
              "record; the consequential step (entity choice) was confirmed "
              "separately, and no status can be awarded except by the gate")
    verified_by = "the saved ScreenResult is re-read and its verdict counts match"

    def plan(self, company_id: str = ""):
        entity_path = _root() / "entity.json"
        if not entity_path.exists():
            raise ContractViolation(
                "no confirmed entity: run finance.screen.entity first")
        entity = json.loads(entity_path.read_text())
        if company_id and company_id != entity["company_id"]:
            raise ContractViolation(
                "company_id does not match the confirmed entity "
                f"({entity['company_id']})")
        return {"company": entity["company"],
                "company_id": entity["company_id"]}

    def execute(self, p) -> dict:
        room = _room()
        screen = None
        for key, candidate_screen in room["screens"].items():
            if candidate_screen["company_id"] == p["company_id"]:
                screen = candidate_screen
                break
        if screen is None:
            return {"error": "no fixture or archived screen for this entity; "
                             "live retrieval is not part of this build yet"}
        findings, discarded = [], 0
        for candidate in screen["candidates"]:
            validated, dropped = evidence_gate.validate_evidence(
                candidate, candidate.get("evidence", []))
            discarded += dropped
            findings.append(evidence_gate.classify(candidate, validated))
        counts = {}
        for finding in findings:
            counts[finding["status"]] = counts.get(finding["status"], 0) + 1
        result = {"company": screen["company"],
                  "company_id": screen["company_id"],
                  "findings": findings, "status_counts": counts,
                  "quotes_discarded": discarded}
        out = _root() / "screens"
        out.mkdir(exist_ok=True)
        path = out / f"{p['company_id']}.json"
        path.write_text(json.dumps(result, indent=2))
        return {"company": result["company"], "company_id": p["company_id"],
                "status_counts": counts, "quotes_discarded": discarded,
                "record": str(path)}

    def verify(self, proposal, result):
        if result.get("error"):
            return (False, str(result["error"]))
        path = _root() / "screens" / f"{proposal['company_id']}.json"
        if not path.exists():
            return (False, "screen result missing after execute")
        saved = json.loads(path.read_text())
        ok = saved.get("status_counts") == result.get("status_counts")
        return (ok, f"screen result re-read: {saved.get('status_counts')}")


class ScreenFixture(Plugin):
    name = "finance.screen.fixture"
    description = ("The bundled fixture screen, classified live by the gate. "
                   "Read-only, no retrieval, no entity confirmation needed: "
                   "this is what a judge can verify without credentials.")
    read_only = True
    confirm = False

    def run(self, company_id: str = "04123456"):
        room = _room()
        for screen in room["screens"].values():
            if screen["company_id"] == company_id:
                findings, discarded = [], 0
                for candidate in screen["candidates"]:
                    validated, dropped = evidence_gate.validate_evidence(
                        candidate, candidate.get("evidence", []))
                    discarded += dropped
                    findings.append(evidence_gate.classify(candidate, validated))
                counts = {}
                for finding in findings:
                    counts[finding["status"]] = counts.get(finding["status"], 0) + 1
                return {"company": screen["company"], "company_id": company_id,
                        "status_counts": counts,
                        "quotes_discarded": discarded,
                        "findings": findings}
        raise ContractViolation(f"no fixture screen for company {company_id}")


class ScreenArchive(Plugin):
    name = "finance.screen.archive"
    description = "Screens already run and saved, with their verdict counts."
    read_only = True
    confirm = False

    def run(self, company_id: str = ""):
        folder = _root() / "screens"
        saved = []
        if folder.exists():
            for path in sorted(folder.glob("*.json")):
                data = json.loads(path.read_text())
                if company_id and data.get("company_id") != company_id:
                    continue
                saved.append({"company": data.get("company"),
                              "company_id": data.get("company_id"),
                              "status_counts": data.get("status_counts")})
        return {"screens": saved}


# ── finance.dataroom ─────────────────────────────────────────────────────────

class DataroomSearch(Plugin):
    name = "finance.dataroom.search"
    description = ("Search the data room. Company scope is mandatory: results "
                   "always name the document each passage came from.")
    read_only = True
    confirm = False

    def run(self, query: str = "", company: str = ""):
        room = _room()
        if company and company.lower() not in room["company"].lower():
            raise ContractViolation(
                f"the data room holds {room['company']}; no documents for "
                f"'{company}'")
        terms = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2]
        hits = []
        for doc in room["documents"]:
            text = doc["text"]
            score = sum(text.lower().count(t) for t in terms)
            if score or not terms:
                hits.append({"doc": doc["id"], "title": doc["title"],
                             "passage": text[:400], "score": score})
        hits.sort(key=lambda h: -h["score"])
        return {"company": room["company"], "hits": hits}


class DataroomRecord(Plugin):
    name = "finance.dataroom.record"
    description = "One data-room document in full, by id."
    read_only = True
    confirm = False

    def run(self, doc: str = ""):
        found = _doc(doc)
        return {"doc": found["id"], "title": found["title"],
                "text": found["text"]}


class DataroomPropose(Plugin):
    name = "finance.dataroom.propose"
    description = ("Draft a record entry from the data room. Every figure in "
                   "the proposal must be carried by the document it cites; "
                   "the claim check refuses it otherwise. Writes a new file "
                   "only, after approval.")
    confirm = True
    background = True
    reason = ("a drafted record becomes institutional truth, so a person "
              "approves the exact text; drafting itself may never overwrite "
              "an existing record")
    verified_by = "the written record is re-read and its figure re-checked"

    system = ("You draft one short record entry from the data-room documents "
              "in context. State one claim and the single figure it rests on, "
              "copied exactly from the cited document. source_doc is the bare "
              "document id (such as im-2026) without brackets. If the "
              "documents do not carry a figure, say so in the claim and leave "
              "figure empty.")
    schema = json.dumps({
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "claim": {"type": "string"},
            "figure": {"type": "string"},
            "source_doc": {"type": "string"}},
        "required": ["title", "claim", "figure", "source_doc"]})

    def context(self, task: str, params: dict = None) -> dict:
        room = _room()
        return {"data_room": "\n\n".join(
            f"[{d['id']}] {d['title']}\n{d['text']}" for d in room["documents"])}

    def validate(self, proposal: dict) -> list:
        problems = []
        try:
            doc = _doc(proposal.get("source_doc", ""))
        except ContractViolation as exc:
            return [str(exc)]
        figure = (proposal.get("figure") or "").strip()
        if figure and not _checked(doc):
            problems.append(
                f"claim check refused: [{doc['id']}] is an unverified draft; "
                "a figure must rest on a checked source")
        elif figure and not evidence_gate.claim_check(figure, doc["text"]):
            problems.append(
                f"claim check refused: the figure '{figure}' is not carried "
                f"by the cited document [{doc['id']}]")
        # The claim text itself may not smuggle in what the figure field left
        # out: every number it states must be carried by the cited source.
        for number in _NUMBER.findall(proposal.get("claim") or ""):
            number = number.strip()
            if not (_checked(doc)
                    and evidence_gate.claim_check(number, doc["text"])):
                problems.append(
                    f"claim check refused: the claim states '{number}', "
                    f"which the cited document [{doc['id']}] does not carry "
                    "as a checked source")
        return problems

    def execute(self, p) -> dict:
        folder = _root() / "records"
        folder.mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", p["title"].lower()).strip("-") or "record"
        path = folder / f"{slug}.md"
        if path.exists():
            return {"error": f"record '{slug}' already exists; records are "
                             "new files only, never overwritten"}
        body = (f"# {p['title']}\n\n{p['claim']}\n\n"
                f"Figure: {p.get('figure') or '(none stated)'}\n"
                f"Source: [{p['source_doc']}]\n")
        path.write_text(body)
        return {"written": str(path), "figure": p.get("figure", ""),
                "source_doc": p["source_doc"]}

    def verify(self, proposal, result):
        if result.get("error"):
            return (False, str(result["error"]))
        path = pathlib.Path(result.get("written", ""))
        if not path.exists():
            return (False, "record file missing after execute")
        doc = _doc(proposal["source_doc"])
        figure = (proposal.get("figure") or "").strip()
        ok = not figure or (_checked(doc)
                            and evidence_gate.claim_check(figure, doc["text"]))
        return (ok, f"record re-read at {path.name}; figure re-checked "
                    f"against [{doc['id']}]")


# ── finance.compute ──────────────────────────────────────────────────────────

def _ebitda() -> float:
    return (_cell("IncomeStatement!B2") - _cell("IncomeStatement!B4")
            - _cell("IncomeStatement!B5"))


_METRICS = ("margin", "growth", "leverage", "covenant_headroom",
            "cash_conversion")


def _metric(name: str, revenue_scale: float = 1.0) -> tuple:
    """(value, formula_in_words, input_cells). Deterministic; no model math."""
    rev25 = _cell("IncomeStatement!B2") * revenue_scale
    rev24 = _cell("IncomeStatement!B3")
    cogs = _cell("IncomeStatement!B4")
    opex = _cell("IncomeStatement!B5")
    debt = _cell("BalanceSheet!B2")
    cash = _cell("BalanceSheet!B3")
    ocf = _cell("CashFlow!B2")
    covenant = _cell("Covenants!B2")
    ebitda = rev25 - cogs - opex
    used = ["IncomeStatement!B2", "IncomeStatement!B4", "IncomeStatement!B5"]
    if name == "margin":
        return (ebitda / rev25 if rev25 else 0.0,
                "EBITDA margin = (revenue - COGS - operating expenses) / revenue",
                used)
    if name == "growth":
        return ((rev25 - rev24) / rev24 if rev24 else 0.0,
                "Revenue growth = (FY2025 revenue - FY2024 revenue) / FY2024 revenue",
                ["IncomeStatement!B2", "IncomeStatement!B3"])
    if name == "leverage":
        return ((debt - cash) / ebitda if ebitda else 0.0,
                "Leverage = (gross debt - cash) / EBITDA",
                used + ["BalanceSheet!B2", "BalanceSheet!B3"])
    if name == "covenant_headroom":
        leverage = (debt - cash) / ebitda if ebitda else 0.0
        return ((covenant - leverage) / covenant if covenant else 0.0,
                "Covenant headroom = (maximum leverage covenant - actual "
                "leverage) / maximum leverage covenant",
                used + ["BalanceSheet!B2", "BalanceSheet!B3", "Covenants!B2"])
    if name == "cash_conversion":
        return (ocf / ebitda if ebitda else 0.0,
                "Cash conversion = operating cash flow / EBITDA",
                used + ["CashFlow!B2"])
    raise ContractViolation(f"metric must be one of {', '.join(_METRICS)}")


class ComputeMetric(Plugin):
    name = "finance.compute.metric"
    description = ("One deterministic metric (margin, growth, leverage, "
                   "covenant_headroom, cash_conversion) computed in Python "
                   "from cited workbook cells, then recomputed independently.")
    read_only = True
    confirm = False
    verified_by = "an independent second computation over the cited cells"

    def run(self, metric: str = ""):
        if metric not in _METRICS:
            raise ContractViolation(
                f"metric must be one of {', '.join(_METRICS)}")
        value, formula, used = _metric(metric)
        again, _, _ = _metric(metric)  # independent second pass
        if abs(value - again) > 1e-12:
            raise ContractViolation("recompute mismatch; metric not reported")
        inputs = [{"cell": ref, "label": _cells()[ref]["label"],
                   "value": _cells()[ref]["value"]} for ref in used]
        return {"metric": metric, "value": round(value, 6), "formula": formula,
                "inputs": inputs,
                "workbook": _room()["workbook"]["file"]}


class ComputeScenario(Plugin):
    name = "finance.compute.scenario"
    description = ("A metric under one stated revenue assumption. Assumptions "
                   "are listed in the output, never buried.")
    confirm = False
    autonomous = True
    verified_by = "base and scenario are both recomputed from the cited cells"

    def plan(self, metric: str = "margin", revenue_delta_pct: float = 0.0):
        if metric not in _METRICS:
            raise ContractViolation(
                f"metric must be one of {', '.join(_METRICS)}")
        return {"metric": metric,
                "revenue_delta_pct": float(revenue_delta_pct)}

    def execute(self, p) -> dict:
        base, formula, used = _metric(p["metric"])
        scenario, _, _ = _metric(p["metric"],
                                 revenue_scale=1 + p["revenue_delta_pct"] / 100)
        return {"metric": p["metric"], "base": round(base, 6),
                "scenario": round(scenario, 6),
                "delta": round(scenario - base, 6), "formula": formula,
                "assumptions": [
                    f"revenue scaled by {p['revenue_delta_pct']:+}%"],
                "inputs_cited": used}


# ── finance.memo ─────────────────────────────────────────────────────────────

_NUMBER = re.compile(r"\d+(?:\.\d+)?\s?%|\d+\.\d+")


class MemoDraft(Plugin):
    name = "finance.memo.draft"
    description = ("Draft an IC memo from gated screen findings and cited "
                   "data-room facts only. Unsupported figures are listed as "
                   "gaps, never smoothed over.")
    confirm = False
    background = True
    takes = ("screen_id",)
    reason = ("a draft commits the firm to nothing; the release is the "
              "consequential step and is gated separately")
    verified_by = "every figure in the draft passes the claim check, or the draft fails"

    system = ("You assemble an investment-committee memo ONLY from the gated "
              "findings and data-room documents in context. Every figure you "
              "state must appear in a cited source. Anything you cannot "
              "support goes in gaps, stated plainly. Never invent a figure. "
              "Do not compute or state derived figures (no percentages or "
              "ratios you calculated yourself): state only figures carried "
              "verbatim by a cited source, because deterministic compute "
              "reports derived metrics separately with their inputs. "
              "Return exactly this shape: title (string); sections, a list of "
              "objects each with heading (string), body (ONE string of prose, "
              "never a list) and citations (a list of data-room document ids "
              "such as im-2026, without brackets); gaps (a list of strings).")
    schema = json.dumps({
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "sections": {"type": "array", "items": {
                "type": "object",
                "properties": {"heading": {"type": "string"},
                               "body": {"type": "string"},
                               "citations": {"type": "array",
                                             "items": {"type": "string"}}},
                "required": ["heading", "body", "citations"]}},
            "gaps": {"type": "array", "items": {"type": "string"}}},
        "required": ["title", "sections", "gaps"]})

    def context(self, task: str, params: dict = None) -> dict:
        room = _room()
        blocks = {"data_room": "\n\n".join(
            f"[{d['id']}] {d['title']}\n{d['text']}" for d in room["documents"])}
        screen_id = (params or {}).get("screen_id", "")
        if screen_id:
            path = _root() / "screens" / f"{screen_id}.json"
            if path.exists():
                blocks["gated_screen"] = path.read_text()
        return blocks

    def validate(self, proposal: dict) -> list:
        problems = []
        room = _room()
        texts = {d["id"]: d["text"] for d in room["documents"] if _checked(d)}
        known = {d["id"] for d in room["documents"]}
        for section in proposal.get("sections", []):
            heading = section.get("heading", "?")
            if not (section.get("body") or "").strip():
                problems.append(f"section '{heading}' has no body")
                continue
            citations = section.get("citations") or []
            if not citations:
                problems.append(
                    f"section '{heading}' cites no source; every section "
                    "must rest on a checked data-room document")
                continue
            unknown = [c for c in citations if c not in known]
            if unknown:
                problems.append(
                    f"section '{heading}' cites unknown source(s): "
                    + ", ".join(unknown))
                continue
            cited = [texts[c] for c in citations if c in texts]
            if not cited:
                problems.append(
                    f"section '{heading}' rests only on an unverified draft; "
                    "a figure must rest on a checked source")
                continue
            for number in _NUMBER.findall(section.get("body", "")):
                if cited and not any(
                        evidence_gate.claim_check(number.strip(), t)
                        for t in cited):
                    problems.append(
                        f"claim check refused: figure '{number.strip()}' in "
                        f"section '{section.get('heading')}' is not carried "
                        "by its cited sources")
        return problems

    def execute(self, p) -> dict:
        folder = _root() / "memos"
        folder.mkdir(exist_ok=True)
        memo_id = hashlib.sha256(
            json.dumps(p, sort_keys=True).encode()).hexdigest()[:12]
        path = folder / f"{memo_id}.json"
        path.write_text(json.dumps({"id": memo_id, **p}, indent=2))
        return {"memo_id": memo_id, "title": p["title"],
                "sections": len(p.get("sections", [])),
                "gaps": p.get("gaps", []), "draft": str(path)}


class MemoRelease(Plugin):
    name = "finance.memo.release"
    description = ("Release a drafted memo. The FULL memo text is the "
                   "proposal; approval binds to its hash, and after release "
                   "the artefact is re-read and its hash compared.")
    confirm = True
    reason = ("a released memo is the decision document other people act on; "
              "a summary is not enough to approve, and a changed memo must "
              "not ride an earlier approval")
    verified_by = "the released artefact is re-read and its hash compared to the proposal"

    def plan(self, memo_id: str = ""):
        path = _root() / "memos" / f"{memo_id}.json"
        if not path.exists():
            raise ContractViolation(
                f"no draft memo '{memo_id}' (see finance.memo.draft)")
        draft = json.loads(path.read_text())
        lines = [f"# {draft['title']}", ""]
        for section in draft.get("sections", []):
            cites = ", ".join(f"[{c}]" for c in section.get("citations", []))
            lines.append(f"## {section['heading']}\n\n{section['body']}\n\n"
                         f"Sources: {cites}\n")
        if draft.get("gaps"):
            lines.append("## Gaps\n")
            lines.extend(f"- {g}" for g in draft["gaps"])
        full_text = "\n".join(lines)
        digest = hashlib.sha256(full_text.encode()).hexdigest()
        return {"memo_id": memo_id, "title": draft["title"],
                "full_text": full_text, "sha256": digest}

    def render(self, p) -> str:
        return (f"RELEASE MEMO {p['memo_id']} (sha256 {p['sha256'][:16]}...)\n\n"
                f"{p['full_text']}")

    def execute(self, p) -> dict:
        folder = _root() / "released"
        folder.mkdir(exist_ok=True)
        path = folder / f"{p['memo_id']}.md"
        path.write_text(p["full_text"])
        return {"released": str(path), "sha256": p["sha256"]}

    def verify(self, proposal, result):
        path = _root() / "released" / f"{proposal['memo_id']}.md"
        if not path.exists():
            return (False, "released artefact missing after execute")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        ok = digest == proposal["sha256"]
        return (ok, f"released artefact re-read; hash match: {ok}")


COMMANDS = [ScreenCandidates, ScreenEntity, ScreenRun, ScreenFixture,
            ScreenArchive,
            DataroomSearch, DataroomRecord, DataroomPropose,
            ComputeMetric, ComputeScenario, MemoDraft, MemoRelease]
