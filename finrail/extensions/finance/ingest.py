"""One command brings a filing into the catalog: download, fingerprint, archive, extract, index.

The operator used to build a bundle by hand, run a script under their own gcloud
login, poll an async import themselves and paste RAG file IDs back into reports.json.
Each hand-off was a place for the wrong hash or the wrong ID to land. Here the local
steps always run and the cloud steps run when credentials are present; when they are
not, the result names each step that was skipped, so a catalog entry with no RAG file
is an honest state, never a surprise.

The downloaded bytes are a document, not a program and not a message: they are hashed,
written to disk and parsed for their tables. Nothing in them is executed or followed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from finrail.core.errors import ContractViolation, NotConfigured, OpFailed
from finrail.core.settings import state
from finrail.plugin import Plugin

from . import public_reports as reports

ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
REPORT_ID = re.compile(r"^[A-Z0-9]{2,8}-[A-Za-z0-9]{1,12}(?:-[A-Za-z0-9]{1,12})?$")
#: The SEC asks every automated client to say who it is. Set FINRAIL_EDGAR_USER_AGENT
#: to "<org> <contact email>"; the default is honest but EDGAR may refuse it.
EDGAR_USER_AGENT = os.environ.get("FINRAIL_EDGAR_USER_AGENT", "Finrail public-report ingest")
MAX_BYTES = 40_000_000
CLOUD_STEPS = ("upload the original to the archive bucket", "upload the extraction to the archive bucket",
               "start the managed RAG import", "poll the import to completion", "bind the RAG file ID")


# ── fetching: a document comes back as bytes and the URL it finally came from ──

def download(url: str) -> tuple[str, bytes]:
    """(final URL, bytes). Capped, with a client name EDGAR accepts; nothing is decoded here."""
    req = Request(url, headers={"User-Agent": EDGAR_USER_AGENT, "Accept": "text/html,application/pdf,*/*"})
    try:
        with urlopen(req, timeout=120) as response:
            data = response.read(MAX_BYTES + 1)
            final = response.geturl()
    except HTTPError as exc:
        raise OpFailed(f"Download of {url} failed with HTTP {exc.code}."
                       + (" EDGAR refuses unnamed clients: set FINRAIL_EDGAR_USER_AGENT to your organisation and contact email."
                          if "sec.gov" in url and exc.code in (403, 429) else "")) from exc
    except (URLError, TimeoutError) as exc:
        raise OpFailed(f"Download of {url} could not complete: {exc.reason if hasattr(exc, 'reason') else exc}.") from exc
    if len(data) > MAX_BYTES:
        raise OpFailed(f"The document at {url} exceeds the allowed size.")
    if not data:
        raise OpFailed(f"The document at {url} was empty.")
    return final, data


def edgar_folder(cik: str, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"


def edgar_primary_document(cik: str, accession: str) -> str:
    """The filing's primary document URL from the EDGAR folder index.

    The folder lists the primary document beside the rendered R-pages, the exhibits and
    the index files. Only an unambiguous choice is made: exactly one .htm that is not an
    R-page, an index or an exhibit. Anything else asks the operator for the direct URL,
    naming the candidates, rather than guessing which file is the filing."""
    folder = edgar_folder(cik, accession)
    _final, data = download(folder + "index.json")
    try:
        items = json.loads(data.decode("utf-8"))["directory"]["item"]
    except (ValueError, KeyError, TypeError) as exc:
        raise OpFailed(f"The EDGAR folder index at {folder} could not be read.") from exc
    names = [str(i.get("name", "")) for i in items if isinstance(i, dict)]
    candidates = [n for n in names if n.lower().endswith((".htm", ".html"))
                  and not re.match(r"(?i)^(r\d+\.htm|.*-index.*|ex-?\d.*|.*_ex\d.*)$", n)]
    if len(candidates) != 1:
        raise ContractViolation(f"EDGAR folder {folder} does not name exactly one primary document "
                                f"(candidates: {', '.join(candidates) or 'none'}); pass the document's direct URL as source.")
    return folder + candidates[0]


# ── the local steps ──────────────────────────────────────────────────────────

def archive_dir(report_id: str) -> Path:
    return state() / "reports" / report_id


def catalog_entry(p: dict, final_url: str, raw: bytes, archived: Path, parsed: Path) -> dict:
    """The reports.json entry for a filing archived locally; cloud fields are added only once they are true."""
    entry = {"source_id": p["report_id"], "company": p["company"], "report_period": p["report_period"],
             "published_at": p.get("published_at") or "", "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "url": p["url"], "retrieved_url": final_url, "local_file": f"raw/{archived.name}",
             "sha256": hashlib.sha256(raw).hexdigest(), "format": "html", "source_type": p["source_type"],
             "audit_status": p.get("audit_status") or "unaudited", "period_end": p["period_end"],
             "source_date_label": p["source_date_label"], "indexed_uris": [], "rag_file_ids": [],
             "archived_locally": str(archived), "extraction_locally": str(parsed),
             "extraction_sha256": hashlib.sha256(parsed.read_bytes()).hexdigest()}
    if p.get("edgar_accession"):
        entry["edgar_accession"] = p["edgar_accession"]
        entry["edgar_cik"] = p["edgar_cik"]
    return entry


def write_catalog(entry: dict, path: Path | None = None) -> None:
    """Replace or append this report's entry; the rest of the catalog is written back as data.

    The catalog path is read at call time, not bound at import, so a test or an
    operator can point the runtime at another catalog without the entry landing in
    the shipped one."""
    path = path or reports.DATA
    doc = json.loads(path.read_text())
    doc["reports"] = [e for e in doc["reports"] if e["source_id"] != entry["source_id"]] + [entry]
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def source_check(entry: dict, raw: bytes) -> dict:
    """Whether the profile reads every declared cell of this filing, with the reason when it does not.

    A filing whose cells the profile cannot read still archives and indexes (search
    works on it); analysis would refuse at run time with the same message, so the
    operator hears it now."""
    try:
        cells = reports.source_cells(entry, raw)
    except ContractViolation as exc:
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "cells": {key: cell["value"] for key, cell in cells.items()}}


# ── the cloud steps, each one skipped by name when it cannot run ─────────────

def cloud_config() -> dict | None:
    """Bucket, corpus and a token, or None with no guessing: all three or nothing."""
    bucket, corpus = os.environ.get("FINRAIL_REPORTS_BUCKET", ""), os.environ.get("FINRAIL_RAG_CORPUS", "")
    if not bucket or not re.fullmatch(r"projects/[\w-]+/locations/us-central1/ragCorpora/\d+", corpus):
        return None
    try:
        access = reports.token()
    except NotConfigured:
        return None
    return {"bucket": bucket, "corpus": corpus, "token": access,
            "project": os.environ.get("FINRAIL_GCP_PROJECT", corpus.split("/")[1])}


def _request(cfg: dict, url: str, data: bytes | None = None, content_type: str = "application/json") -> dict:
    req = Request(url, data=data, headers={"Authorization": "Bearer " + cfg["token"], "Content-Type": content_type,
                                           "x-goog-user-project": cfg["project"]})
    try:
        with urlopen(req, timeout=120) as response:
            return json.load(response)
    except HTTPError as exc:
        raise OpFailed(f"Google returned HTTP {exc.code} for {url.split('?')[0]}; nothing further was bound.") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise OpFailed(f"Google could not complete {url.split('?')[0]}; nothing further was bound.") from exc


def upload(cfg: dict, obj: str, content: bytes, mime: str) -> dict:
    """Create the object, or accept it only if the archived bytes are these bytes."""
    base = "https://storage.googleapis.com/storage/v1/b/" + cfg["bucket"] + "/o/" + quote(obj, safe="")
    try:
        existing = _request(cfg, base)
    except OpFailed as exc:
        if "HTTP 404" not in str(exc):
            raise
        url = ("https://storage.googleapis.com/upload/storage/v1/b/" + cfg["bucket"]
               + "/o?uploadType=media&ifGenerationMatch=0&name=" + quote(obj, safe=""))
        return _request(cfg, url, content, mime)
    req = Request(base + "?alt=media", headers={"Authorization": "Bearer " + cfg["token"]})
    try:
        with urlopen(req, timeout=120) as response:
            same = hashlib.sha256(response.read(MAX_BYTES + 1)).digest() == hashlib.sha256(content).digest()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise OpFailed(f"The archived object {obj} could not be re-read to compare it.") from exc
    if not same:
        raise OpFailed(f"The archive already holds a different {obj}; use a new prefix rather than overwrite it.")
    return existing


def start_import(cfg: dict, uri: str) -> dict:
    payload = {"importRagFilesConfig": {"gcsSource": {"uris": [uri]}, "maxEmbeddingRequestsPerMin": 60,
               "ragFileTransformationConfig": {"ragFileChunkingConfig": {"fixedLengthChunking": {"chunkSize": 1024, "chunkOverlap": 150}}}}}
    return _request(cfg, "https://us-central1-aiplatform.googleapis.com/v1beta1/" + cfg["corpus"] + "/ragFiles:import",
                    json.dumps(payload).encode())


def poll(cfg: dict, operation: dict, *, every: float = 10.0, limit: float = 1800.0) -> dict:
    """Wait for the import to finish; a failed or overlong import is an error, never a bound ID."""
    name = operation.get("name", "")
    if not name:
        raise OpFailed("The import did not return an operation to poll.")
    deadline = time.time() + limit
    current = operation
    while not current.get("done"):
        if time.time() > deadline:
            raise OpFailed(f"The import {name} did not finish within {int(limit)} seconds; its RAG file was not bound.")
        time.sleep(every)
        current = _request(cfg, "https://us-central1-aiplatform.googleapis.com/v1beta1/" + name)
    if current.get("error"):
        raise OpFailed(f"The import {name} failed: {current['error'].get('message', 'no message')}.")
    return current


def bound_file_id(cfg: dict, uri: str) -> str:
    """The one ACTIVE RAG file whose source is this URI; anything else is a refusal."""
    files = _request(cfg, "https://us-central1-aiplatform.googleapis.com/v1beta1/" + cfg["corpus"] + "/ragFiles?pageSize=100").get("ragFiles", [])
    matches = [f for f in files if uri in ((f.get("gcsSource") or {}).get("uris") or [])]
    if len(matches) != 1:
        raise OpFailed(f"The corpus holds {len(matches)} files for {uri}; expected exactly one, so none was bound.")
    if ((matches[0].get("fileStatus") or {}).get("state")) != "ACTIVE":
        raise OpFailed(f"The imported file for {uri} is not ACTIVE; it was not bound.")
    return matches[0]["name"].rsplit("/", 1)[-1]


# ── the capability ───────────────────────────────────────────────────────────

class ReportIngest(Plugin):
    name = "finance.reports.ingest"
    description = ("Bring one filing into the catalog from an EDGAR accession or a direct URL: download, SHA-256, "
                   "archive, extract with the issuer's profile, write the catalog entry, and index it when cloud "
                   "credentials are present.")
    confirm = True
    person_call_approves = True
    reason = ("it writes a catalog entry other runs will check figures against, so the operator who asked for "
              "it is the one who approves it")
    verified_by = ("the archived bytes are re-read and their SHA-256 compared with the catalog entry before the "
                   "run is called done")
    steps = (
        {"name": "Resolve", "by": "Kernel", "kind": "code", "where": "ReportIngest.plan()",
         "rule": "An accession number becomes the EDGAR primary document URL from the issuer's CIK; a direct URL is taken as given."},
        {"name": "Download", "by": "Kernel", "kind": "retrieve", "where": "download()",
         "rule": "The bytes are a document: hashed and archived, never executed, never read as instructions."},
        {"name": "Extract", "by": "Kernel", "kind": "code", "where": "public_reports.extract()",
         "rule": "The profile's extraction, in source order, with no computed figure."},
        {"name": "Check", "by": "Kernel", "kind": "check", "where": "source_check()",
         "rule": "The profile must read every declared cell, or the entry says it cannot."},
        {"name": "Index", "by": "Google Vertex RAG", "kind": "retrieve", "where": "upload(), start_import(), poll(), bound_file_id()",
         "rule": "Runs only with bucket, corpus and a token. Skipped steps are named in the result; the RAG file ID is bound only once the import is ACTIVE."},
    )

    def plan(self, source: str, company: str, report_id: str, report_period: str, period_end: str,
             source_date_label: str = "", published_at: str = "", source_type: str = "",
             audit_status: str = "", prefix: str = ""):
        prof = reports.PROFILES.get(company)
        if not prof:
            raise ContractViolation(f"No issuer profile named {company!r}; profiles exist for {', '.join(reports.PROFILES)}.")
        if not REPORT_ID.match(report_id or ""):
            raise ContractViolation("report_id must look like NVDA-Q3-FY2027: an issuer code, a period, upper-case letters and digits.")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period_end or ""):
            raise ContractViolation("period_end must be the statement's period end as YYYY-MM-DD.")
        if published_at and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_at):
            raise ContractViolation("published_at must be YYYY-MM-DD.")
        if prof["column"]["kind"] == "first" and not source_date_label:
            raise ContractViolation(f"{company}'s profile finds the current column by its date header; pass source_date_label, "
                                    "for example 'July 26,'.")
        if not report_period.strip():
            raise ContractViolation("report_period names the period in the issuer's words, for example 'Q3 FY2027'.")
        if prefix and not re.fullmatch(r"[\w-]{1,80}", prefix):
            raise ContractViolation("prefix must be a plain object prefix: letters, digits, hyphens.")
        source = source.strip()
        out = {"company": company, "report_id": report_id, "report_period": report_period.strip(),
               "period_end": period_end, "source_date_label": source_date_label or prof["column"].get("period", ""),
               "published_at": published_at, "audit_status": audit_status,
               "source_type": source_type or ("registration_statement_s1" if "S-1" in report_period or "S1" in report_id
                                              else "issuer_earnings_release"),
               "prefix": prefix or f"{dt.date.today().isoformat()}-{report_id.lower()}"}
        if ACCESSION.match(source):
            cik = prof.get("edgar_cik")
            if not cik:
                raise ContractViolation(f"{company}'s profile has no edgar_cik, so an accession number cannot be resolved; "
                                        "add it to the profile or pass the document's direct URL.")
            out.update(edgar_accession=source, edgar_cik=cik, url="", resolve="edgar")
        else:
            parts = urlsplit(source)
            if parts.scheme != "https" or not parts.netloc:
                raise ContractViolation("source must be an EDGAR accession number (0001628280-26-067168) or an https URL.")
            out.update(url=source, resolve="direct")
        return out

    def execute(self, p: dict) -> dict:
        url = p["url"] or edgar_primary_document(p["edgar_cik"], p["edgar_accession"])
        p = {**p, "url": url}
        final_url, raw = download(url)
        folder = archive_dir(p["report_id"])
        folder.mkdir(parents=True, exist_ok=True)
        archived = folder / f"{p['report_id']}.html"
        archived.write_bytes(raw)
        try:
            text = reports.extract(raw, p["report_id"], p["company"])
        except UnicodeDecodeError as exc:
            raise OpFailed("The document is not UTF-8 HTML; only HTML filings can be ingested by this command.") from exc
        preamble = (f"REPORT {p['report_id']}; {p['company']}; {p['report_period']}; period ending {p['period_end']}.\n"
                    f"Source: {url}\nOriginal SHA-256: {hashlib.sha256(raw).hexdigest()}\n"
                    "Source-preserving extraction; original row/column order retained. Not a benchmark answer key.\n\n")
        parsed = folder / f"{p['report_id']}.txt"
        parsed.write_text(preamble + text, encoding="utf-8")
        entry = catalog_entry(p, final_url, raw, archived, parsed)
        check = source_check(entry, raw)
        skipped, cloud = [], {}
        cfg = cloud_config()
        if cfg is None:
            skipped = list(CLOUD_STEPS)
        else:
            original_obj = f"{p['prefix']}/raw/{archived.name}"
            parsed_obj = f"{p['prefix']}/parsed/{parsed.name}"
            meta = upload(cfg, original_obj, raw, "text/html")
            cloud["object_generation"] = str(meta.get("generation", ""))
            meta = upload(cfg, parsed_obj, parsed.read_bytes(), "text/plain")
            cloud["parsed_object_generation"] = str(meta.get("generation", ""))
            uri = f"gs://{cfg['bucket']}/{parsed_obj}"
            operation = start_import(cfg, uri)
            cloud["import_operation"] = operation.get("name", "")
            poll(cfg, operation)
            file_id = bound_file_id(cfg, uri)
            entry.update(bucket=cfg["bucket"], object=original_obj, parsed_object=parsed_obj, indexed_uris=[uri],
                         rag_file_ids=[file_id], object_generation=cloud["object_generation"],
                         parsed_object_generation=cloud["parsed_object_generation"])
        write_catalog(entry)
        return {"report_id": p["report_id"], "company": p["company"], "url": url, "retrieved_url": final_url,
                "sha256": entry["sha256"], "bytes": len(raw), "archived": str(archived), "extraction": str(parsed),
                "catalog": str(reports.DATA), "source_check": check, "cloud": cloud, "skipped": skipped,
                "indexed": bool(entry["rag_file_ids"]), "rag_file_ids": entry["rag_file_ids"],
                "note": ("Indexed; search and analysis are available." if entry["rag_file_ids"] else
                         "Archived locally only. Set FINRAIL_REPORTS_BUCKET, FINRAIL_RAG_CORPUS and a Google token "
                         "(FINRAIL_GCP_ACCESS_TOKEN or workload identity) and run again to index it; the archived bytes are reused.")}

    def verify(self, proposal: dict, result: dict) -> tuple:
        try:
            again = hashlib.sha256(Path(result["archived"]).read_bytes()).hexdigest()
            listed = reports.report(result["report_id"])["sha256"]
        except (OSError, KeyError, ContractViolation):
            return (False, "the archived file or its catalog entry could not be re-read")
        ok = again == result["sha256"] == listed
        return (ok, f"archived bytes re-read; SHA-256 matches the catalog entry: {ok}")
