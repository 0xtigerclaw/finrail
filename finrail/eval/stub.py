"""THE STUB ENGINE: an eval that runs with no key and no network, over the real seam.

Two things are stubbed, both at the edges the live system already treats as untrusted
inputs. Nothing between them is touched: the capability, the row index, the source-cell
checks and the ledger run exactly as they run live.

1. The model. A local HTTP server speaks the OpenAI chat-completions shape that
   `llm._http_call` sends, and answers from a recorded-answers file. Because it is reached
   through `FINRAIL_ENGINE_BASE_URL`, every call is written to the ledger as a `model_call`
   event with usage, like a call to Token Factory. The runner reads those events, so the
   stub exercises the same bookkeeping a live run does.

2. The filings. The archived HTML is private (and the S-1 is 14 MB), so the stub serves
   synthetic statements with the shape the profiles declare and the figures the docs
   state, and rewrites each catalog item's SHA-256 to the synthetic bytes. The evidence
   `retrieve` would fetch from Vertex is built from the same rows. The figures are the
   documented ones (NVIDIA Q1 and Q2 FY2027 cash flow lines, Anthropic's six-month 2026
   column); the layout is a stand-in. A stub pass therefore proves the harness's checks
   and bookkeeping, not that the live filings say so: that is what a live run is for.

The recorded-answers file:

    {"schema": "finrail.eval.stub-answers/1",
     "usage": {"input_tokens": 1469, "output_tokens": 1050},      # default per answer
     "answers": {
        "NVDA-Q1-FY2027:issuer_fcf": {"reply": {... the model's JSON ...}},
        "some-case-id": {"empty": true},                          # a reply cut off: no content
        "another-case-id": {"content": "not json", "usage": {...}}}}

Lookup is by case id first, then by `report_id:definition` (analyze) or `report_id:search`.
A case with no recorded answer gets an empty reply, which the seam reports as
EMPTY_RESPONSE; the run record says why.
"""
import contextlib
import copy
import hashlib
import json
import os
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ANSWERS_SCHEMA = "finrail.eval.stub-answers/1"
DEFAULT_ANSWERS = pathlib.Path(__file__).parent / "cases" / "stub_answers.json"
ENGINE = "stub"
MODEL = "stub"

#: What the runner tells the server about the call in flight. One process, one call at a time.
CURRENT = {"case": "", "key": ""}


# ── synthetic filings ────────────────────────────────────────────────────────
#: NVIDIA's three cash-flow lines and reported FCF per quarter (USD millions), from the
#: README's worked example (Q2) and tests/test_public_reports.py and docs/model-comparison.md
#: (Q1). The comparative column is the prior quarter, as the release prints it.
NVIDIA = {
    "NVDA-Q1-FY2027": {"date": "April 26,", "comparative_date": "January 25,",
                       "lines": {"operating_cash_flow": ("50,344", "27,414"),
                                 "cash_purchases": ("1,757", "1,227"),
                                 "principal_payments": ("33", "52")},
                       "fcf": ("48,554", "34,902")},
    "NVDA-Q2-FY2027": {"date": "July 26,", "comparative_date": "April 26,",
                       "lines": {"operating_cash_flow": ("24,077", "50,344"),
                                 "cash_purchases": ("2,677", "1,757"),
                                 "principal_payments": ("59", "33")},
                       "fcf": ("21,341", "48,554")},
}


def nvidia_html(report_id: str) -> bytes:
    """A release with the cash-flow table at position 5 and the FCF table at 6, as the
    NVIDIA profile declares, in the cell layout the live release uses (the closing
    parenthesis of a negative sits in its own cell)."""
    from finrail.extensions.finance import public_reports as r
    spec = NVIDIA[report_id]
    header = ("<tr><td>($ in millions)</td></tr><tr><td>Three Months Ended</td></tr>"
              f"<tr><td>{spec['date']}</td><td>{spec['comparative_date']}</td></tr>"
              "<tr><td>2026</td><td>2026</td></tr>")
    rows = []
    for key, (value, comparative) in spec["lines"].items():
        label = r.ROWS[key]
        if key == "operating_cash_flow":
            rows.append(f"<tr><td>{label}</td><td></td><td>{value}</td><td></td><td>{comparative}</td></tr>")
        else:
            rows.append(f"<tr><td>{label}</td><td></td><td>({value}</td><td>)</td><td>({comparative}</td></tr>")
    table5 = "<table>" + header + "".join(rows) + "</table>"
    table6 = ("<table>" + header + f"<tr><td>Free cash flow</td><td>$</td><td>{spec['fcf'][0]}</td>"
              f"<td>{spec['fcf'][1]}</td></tr></table>")
    return ("<table></table>" * 4 + table5 + table6).encode()


_PERIODS = ('<tr><td></td><td colspan="5">Year Ended December 31,</td><td></td>'
            '<td colspan="3">Six Months Ended June 30,</td></tr>')
_YEARS = ('<tr><td>{unit}</td><td>2023</td><td></td><td>2024</td><td></td><td>2025</td><td></td>'
          '<td>2025</td><td></td><td>2026</td></tr>'
          '<tr><td></td><td colspan="5"></td><td></td><td colspan="3">(unaudited)</td></tr>')


def _row(label, *values):
    cells = "".join(f"<td>{v}</td><td></td>" for v in values).removesuffix("<td></td>")
    return f"<tr><td>{label}</td>{cells}</tr>"


def _line(text):
    return f'<div style="line-height:7pt;position:absolute">{text}</div>'


def _table(caption_lines, unit, *rows):
    return ("".join(_line(t) for t in caption_lines) + "<table>" + _PERIODS
            + _YEARS.format(unit=unit) + "".join(rows) + "</table>")


def anthropic_html() -> bytes:
    """Anthropic's S-1 statements at the table positions the profile declares (61, 72, 109,
    114, 120), five period columns wide, with the figures the filing prints for the six
    months ended 30 June 2026 and its comparatives. Same shape as tests/test_anthropic_s1.py."""
    unit = "(In millions, except share amounts which are in thousands, and per share data)"
    operations = _table(["F-7", "Anthropic, PBC", "Consolidated Statements of Operations", unit], "",
                        _row("Revenue", "$ 41", "$ 386", "$ 4,585", "$ 1,163", "$ 16,286"),
                        _row("Operating expenses:"),
                        _row("Total operating expenses", "592", "3,366", "12,645", "3,298", "16,979"),
                        _row("Change in fair value of convertible notes, derivative liabilities, and SAFEs",
                             "(1,045)", "(5,434)", "(34,308)", "(10,942)", "(72,388)"),
                        _row("Net loss", "$ (1,531)", "$ (8,309)", "$ (41,965)", "$ (12,969)", "$ (80,748)"),
                        _row("Net loss per share attributable to common stockholders, basic and diluted",
                             "$ (1.42)", "$ (5.43)", "$ (23.04)", "$ (7.25)", "$ (39.88)"))
    cash = _table(["F-12", "Anthropic, PBC", "Consolidated Statements of Cash Flows", "(In millions)"], "",
                  _row("Cash flows from operating activities:"),
                  _row("Net loss", "$ (1,531)", "$ (8,309)", "$ (41,965)", "$ (12,969)", "$ (80,748)"),
                  _row("Net cash provided by (used in) operating activities", "(1,627)", "(5,692)", "(2,637)", "(1,513)", "3,929"),
                  _row("Cash flows from investing activities:"),
                  _row("Purchases of property and equipment", "(2)", "(15)", "(131)", "(18)", "(482)"))
    summary = _table(["Cash Flows", "The following table shows a summary of our cash flows for the periods presented:"],
                     "(in millions)",
                     _row("Net cash provided by (used in)"),
                     _row("Operating activities", "$ (1,627)", "$ (5,692)", "$ (2,637)", "$ (1,513)", "$ 3,929"))
    note = _table(["The following table presents consumption-based and subscription-based revenue (in millions):"], "",
                  _row("Consumption-based", "$ 39", "$ 288", "$ 3,796", "$ 983", "$ 13,064"),
                  _row("Total", "$ 41", "$ 386", "$ 4,585", "$ 1,163", "$ 16,286"))
    bridge = _table(["Adjusted Operating Income (Loss)", "The following table presents a reconciliation of operating loss:"],
                    "(in millions, except for percentages)",
                    _row("Operating loss", "$ (551)", "$ (2,980)", "$ (8,060)", "$ (2,135)", "$ (693)"),
                    _row("Stock-based compensation expense", "29", "192", "1,165", "286", "2,185"),
                    _row("Non-cash charitable contributions (1)", "—", "—", "541", "—", "126"),
                    _row("Bartz settlement (2)", "—", "—", "1,500", "—", "—"),
                    _row("Adjusted Operating Income (Loss)", "$ (522)", "$ (2,788)", "$ (4,854)", "$ (1,849)", "$ 1,618"))
    parts = [{61: bridge, 72: summary, 109: operations, 114: cash, 120: note}.get(n, "<table></table>")
             for n in range(1, 121)]
    page = ('<div style="height:792pt;width:612pt">' + _line("Table of Contents")
            + _line("including to fund our substantial compute commitments, and our ability") + "</div>")
    html = ("<html><body>" + page + '<div style="height:792pt;width:612pt">' + "".join(parts)
            + "</div></body></html>")
    return html.encode()


#: Prose the search stub returns as a filing excerpt. Synthetic, figure-free, and labelled
#: as such in every search case; it exists so the quote-picking seam can be exercised.
SEARCH_TEXT = ("The company describes its compute commitments and the risks around them.\n"
               "It expects to fund those commitments from operations and from financing.\n"
               "Competition for talent and for compute capacity is described as intense.")


def synthetic(report_id: str) -> bytes:
    if report_id in NVIDIA:
        return nvidia_html(report_id)
    if report_id == "ANTH-S1-2026":
        return anthropic_html()
    raise KeyError(f"no synthetic filing for {report_id}")


class Filings:
    """Catalog items rewritten to the synthetic bytes, and the evidence built from them."""

    def __init__(self):
        from finrail.extensions.finance import public_reports as r
        self._r = r
        self._real_report = r.report
        self.items, self.raw, self.cells = {}, {}, {}
        for item in r.catalog():
            sid = item["source_id"]
            try:
                raw = synthetic(sid)
            except KeyError:
                continue
            copy_ = copy.deepcopy(item)
            copy_["sha256"] = hashlib.sha256(raw).hexdigest()
            self.items[sid], self.raw[sid] = copy_, raw
            self.cells[sid] = r.source_cells(copy_, raw)

    def report(self, report_id: str) -> dict:
        return self.items.get(report_id) or self._real_report(report_id)

    def archived(self, item: dict) -> bytes:
        return self.raw[item["source_id"]]

    def retrieve(self, item: dict, query: str, *, top_k=6) -> dict:
        sid = item["source_id"]
        if "Adjusted Operating Income" in query or "Consolidated Statements" in query \
                or "Three Months Ended" in query:
            names = self._r.labels(self._r.profile(item))
            text = "\n".join(self.cells[sid][key]["source_row"] for key in names)
        else:
            text = SEARCH_TEXT
        return {"report_id": sid, "backend": "eval stub",
                "evidence": [{"id": "E1", "text": text, "source_uri": item["indexed_uris"][0]}]}

    def patch(self, module) -> None:
        """Make `module` (a loaded copy of public_reports) read the synthetic filings."""
        self._patched = getattr(self, "_patched", [])
        saved = {k: getattr(module, k) for k in ("report", "retrieve", "archived")}
        self._patched.append((module, saved))
        module.report, module.retrieve, module.archived = self.report, self.retrieve, self.archived_of

    def archived_of(self, item: dict) -> bytes:
        return self.archived(item)

    def unpatch(self) -> None:
        for module, saved in getattr(self, "_patched", []):
            for k, v in saved.items():
                setattr(module, k, v)
        self._patched = []


# ── the recorded-answers server ──────────────────────────────────────────────
def load_answers(path=None) -> dict:
    p = pathlib.Path(path or DEFAULT_ANSWERS)
    doc = json.loads(p.read_text())
    if doc.get("schema") != ANSWERS_SCHEMA or not isinstance(doc.get("answers"), dict):
        raise ValueError(f"{p}: not a recorded-answers file (schema {ANSWERS_SCHEMA})")
    return doc


class _Handler(BaseHTTPRequestHandler):
    answers: dict = {}
    requests: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        _Handler.requests.append({"case": CURRENT["case"], "key": CURRENT["key"],
                                  "model": body.get("model"), "max_tokens": body.get("max_tokens")})
        doc = _Handler.answers
        entry = doc["answers"].get(CURRENT["case"]) or doc["answers"].get(CURRENT["key"]) or {"empty": True}
        usage = {**(doc.get("usage") or {}), **(entry.get("usage") or {})}
        if entry.get("empty"):
            content = ""
        elif "content" in entry:
            content = str(entry["content"])
        else:
            content = json.dumps(entry.get("reply", {}))
        payload = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": int(usage.get("input_tokens") or 0),
                      "completion_tokens": int(usage.get("output_tokens") or 0)}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextlib.contextmanager
def installed(answers_path=None):
    """The stub's server and environment, for the duration of a run, then put back.

    Points the HTTP engine at the local server and names the engine `stub`, so every
    ledger event says which engine answered. The filing patches are applied separately,
    with `Filings.patch(module)`, once the registry has loaded the capability: the
    registry imports the finance extension under its own module name
    (`finrail_plugins.finance.public_reports`), and that copy is the one whose `report`,
    `retrieve` and `archived` the capability reads. Patching the `finrail.extensions`
    copy would change nothing the run sees."""
    from finrail.core import llm
    filings = Filings()
    _Handler.answers = load_answers(answers_path)
    _Handler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env_keys = ("FINRAIL_ENGINE", "FINRAIL_ENGINE_BASE_URL", "FINRAIL_ENGINE_API_KEY", "FINRAIL_MODEL")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    try:
        os.environ["FINRAIL_ENGINE"] = ENGINE
        os.environ["FINRAIL_ENGINE_BASE_URL"] = f"http://127.0.0.1:{server.server_port}/v1"
        os.environ["FINRAIL_ENGINE_API_KEY"] = "stub-not-a-credential"
        os.environ["FINRAIL_MODEL"] = MODEL
        yield {"filings": filings, "server": server, "requests": _Handler.requests,
               "engine": llm.engine()}
    finally:
        filings.unpatch()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        server.shutdown()
        server.server_close()
