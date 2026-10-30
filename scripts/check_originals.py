#!/usr/bin/env python3
"""Re-download the catalogued filings from their issuers and fail loudly if the bytes
moved.

For every sidecar in tests/filings/, the script fetches the catalog URL (falling back
to the catalog's `retrieved_url`, as the original ingest did), hashes the body, applies
the sidecar's recorded normalisation (EDGAR's SGML envelope and injected script tags),
and compares:

  * for a sliced HTML filing, a fresh cut of the same tables with the committed slice
    (differs -> CHANGED, fails; the statements moved). A whole-page hash that differs
    while the slice is reproduced is a WARN: investor pages are forms whose chrome
    varies by client, and sec.gov injects a script tag at its edge;
  * for a retrieval-only PDF, the bytes with the sidecar's original SHA-256
    (differs -> CHANGED, fails);
  * the bytes with the catalog SHA-256 (reported; fails only when a source that
    matched the catalog when it was sliced no longer does).

sec.gov refuses requests whose User-Agent is not "company name email", so the agent
string below is deliberately plain.

Downloads are untrusted data: they are written to a fresh temporary directory, parsed
with the standard library only, never executed or rendered. Stdlib only.

    python3 -I scripts/check_originals.py [--blocked-ok NVDA-Q1-FY2027,NVDA-Q2-FY2027]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from finrail.extensions.finance import public_reports as r  # noqa: E402  (read-only use)
from scripts.slice_filing import slice_filing  # noqa: E402

FILINGS = ROOT / "tests" / "filings"
USER_AGENT = "Finrail CI swayam@scipioholding.com"
EDGAR_ENVELOPE = re.compile(rb"\A<DOCUMENT>\n<TYPE>[^\n]*\n<SEQUENCE>[^\n]*\n<FILENAME>[^\n]*\n<DESCRIPTION>[^\n]*\n<TEXT>\n")
EDGAR_TAIL = re.compile(rb"</TEXT>\n</DOCUMENT>\n?\Z")  # the document keeps its final newline
SCRIPT_TAG = re.compile(rb"<script\b[^>]*>.*?</script>", re.S)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str, limit: int = 64_000_000) -> tuple[int, bytes]:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(req, timeout=120) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise SystemExit(f"{url}: response larger than {limit} bytes")
            return response.status, data
    except HTTPError as exc:
        return exc.code, exc.read(limit) if exc.fp else b""
    except URLError as exc:
        return 0, str(exc.reason).encode()


def normalise(data: bytes, kind: str) -> bytes:
    """`edgar-document`: sec.gov wraps a primary document in the EDGAR SGML envelope and
    its edge injects a bot-manager script tag; strip both to get the archived document."""
    if kind == "edgar-document":
        data = EDGAR_ENVELOPE.sub(b"", data, count=1)
        data = EDGAR_TAIL.sub(b"", data, count=1)
        data = SCRIPT_TAG.sub(b"", data)
    elif kind:
        raise SystemExit(f"Unknown normalisation {kind!r}")
    return data


def looks_blocked(status: int, body: bytes) -> bool:
    return status in (401, 403, 429, 503) or b"Just a moment..." in body[:2000] or b"Access Denied" in body[:2000]


def check(sidecar: dict, workdir: Path, blocked_ok: set[str]) -> tuple[str, str]:
    """-> (status, detail). status in OK, WARN, CHANGED, BLOCKED, SKIPPED, ERROR."""
    sid = sidecar["source_id"]
    item = r.report(sid)
    original = sidecar["original"]
    urls = [item["url"]] + ([item["retrieved_url"]] if item.get("retrieved_url") not in (None, item["url"]) else [])
    status, body = 0, b""
    for url in urls:
        status, body = fetch(url)
        if status == 200 and not looks_blocked(status, body):
            break
    if status != 200 or looks_blocked(status, body):
        detail = f"HTTP {status} from {urls[-1]} ({len(body)} bytes)"
        if sid in blocked_ok:
            return "SKIPPED", detail + " (issuer blocks non-browser clients; listed as blocked-ok)"
        return "BLOCKED", detail
    (workdir / f"{sid}.bin").write_bytes(body)
    served = sha256(body)
    kind = original.get("normalization_kind", "")
    document = normalise(body, kind) if kind else body
    document_hash = sha256(document)
    catalog_now = item["sha256"] in (served, document_hash)
    same_bytes = document_hash == original["sha256"]
    notes = []
    if "slice" in sidecar:
        fresh, _manifest = slice_filing(document, [k["table"] for k in sidecar["kept"]], sidecar["slice"]["prefix_bytes"])
        committed = (FILINGS / sidecar["slice"]["file"]).read_bytes()
        if sha256(committed) != sidecar["slice"]["sha256"]:
            return "CHANGED", "the committed slice does not hash to what its sidecar says"
        if sha256(fresh) != sha256(committed):
            return "CHANGED", (f"the statements moved: a fresh cut of the original ({document_hash[:12]}, {len(document):,} bytes) "
                               f"no longer reproduces the committed slice; catalog {'matches' if catalog_now else 'does not match'} the new bytes")
        notes.append(f"slice {sidecar['slice']['file']} reproduced from today's bytes")
        if not same_bytes:
            notes.append(f"whole page differs ({document_hash[:12]}, {len(document):,} bytes now; "
                         f"{original['sha256'][:12]}, {original['bytes']:,} when sliced) but the kept tables are identical")
    elif not same_bytes:
        return "CHANGED", (f"issuer bytes changed: {document_hash[:12]} now vs {original['sha256'][:12]} when retrieved "
                           f"({len(document):,} vs {original['bytes']:,} bytes); catalog {'matches' if catalog_now else 'does not match'} the new bytes")
    if sidecar.get("catalog_match") and not catalog_now:
        return "CHANGED", "bytes equal the sidecar's copy but no longer the catalog hash; the catalog or the sidecar is wrong"
    if not catalog_now:
        notes.append(f"catalog SHA-256 {item['sha256'][:12]} still not reproducible from the issuer (known since the sidecar was written)")
    if any("differs" in n or "catalog" in n for n in notes):
        return "WARN", "; ".join(notes)
    return "OK", "; ".join(notes) or "bytes unchanged, catalog matches"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--blocked-ok", default="", help="comma-separated source_ids whose issuer is known to refuse non-browser clients")
    ap.add_argument("--only", action="append", default=[], metavar="SOURCE_ID")
    args = ap.parse_args()
    blocked_ok = {s for s in args.blocked_ok.split(",") if s}
    sidecars = [json.loads(p.read_text()) for p in sorted(FILINGS.glob("*.json"))]
    if args.only:
        sidecars = [s for s in sidecars if s["source_id"] in args.only]
    if not sidecars:
        raise SystemExit("No sidecars found under tests/filings/")
    failures = 0
    with tempfile.TemporaryDirectory(prefix="finrail-originals-") as tmp:
        for sidecar in sidecars:
            try:
                status, detail = check(sidecar, Path(tmp), blocked_ok)
            except Exception as exc:  # noqa: BLE001  (one bad source must not hide the others)
                status, detail = "ERROR", f"{type(exc).__name__}: {exc}"
            print(f"{status:8} {sidecar['source_id']:16} {detail}")
            failures += status in ("CHANGED", "BLOCKED", "ERROR")
    if failures:
        raise SystemExit(f"{failures} source(s) changed, blocked or failed; see above.")
    print("All fetchable originals are unchanged.")


if __name__ == "__main__":
    main()
