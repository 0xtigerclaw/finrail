#!/usr/bin/env python3
"""Cut a committed test slice out of a real HTML filing, with a sidecar that ties it to
the original bytes.

The slice keeps only the tables the issuer's profile references, each preceded by the
prose immediately before it (the S-1 states a statement's units in the caption above
the table, not inside it). Every other table up to the highest referenced ordinal is
replaced by an empty `<table></table>` placeholder, so table N of the slice is table N
of the original and `public_reports.source_cells` reads the slice exactly as it reads
the archived filing. The sidecar JSON records the original's SHA-256 and size, the
byte offsets of every kept segment, each segment's own SHA-256, and the slice's
SHA-256, so an offline test can show which bytes of which original the slice holds and
an online check can cut the original again and expect the same slice.

The filing bytes are untrusted data. This script parses them with the standard
library's HTMLParser only; nothing in them is executed or rendered.

    python3 -I scripts/slice_filing.py ORIGINAL.html --source-id ANTH-S1-2026 --out tests/filings

Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from finrail.extensions.finance import public_reports as r  # noqa: E402  (read-only use)

PREFIX_BYTES = 8192


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TableSpans(HTMLParser):
    """Character offsets of every `<table>` ... `</table>`, numbered in start-tag order,
    which is how `public_reports.Tables` numbers them. A table nested inside another
    is recorded in `nested` (by ordinal) because the parser cannot read the outer one
    whole; slicing refuses when such a table is at or before a kept ordinal."""

    def __init__(self, text: str):
        super().__init__()
        self._starts = [0]
        for line in text.split("\n"):
            self._starts.append(self._starts[-1] + len(line) + 1)
        self.spans: list[tuple[int, int]] = []
        self.nested: list[int] = []
        self._open: list[int] = []
        self.feed(text)
        self.close()

    def _pos(self) -> int:
        line, col = self.getpos()
        return self._starts[line - 1] + col

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.spans.append((self._pos(), -1))
            if self._open:
                self.nested.append(len(self.spans))
            self._open.append(len(self.spans) - 1)

    def handle_endtag(self, tag):
        if tag == "table" and self._open:
            index = self._open.pop()
            self.spans[index] = (self.spans[index][0], self._pos() + len("</table>"))


def prefix_start(text: str, table_start: int, previous_end: int, limit: int) -> int:
    """Where the kept prose before a table begins: after the previous table, at most
    `limit` characters back, moved forward to the next tag boundary so the slice never
    opens mid-tag."""
    start = max(previous_end, table_start - limit)
    if start == previous_end:
        return start
    cut = text.find("<", start, table_start)
    return cut if cut != -1 else table_start


def slice_filing(raw: bytes, ordinals: list[int], prefix_bytes: int = PREFIX_BYTES) -> tuple[bytes, dict]:
    text = raw.decode("utf-8")
    parsed = TableSpans(text)
    if max(ordinals) > len(parsed.spans):
        raise SystemExit(f"The filing has {len(parsed.spans)} tables; table {max(ordinals)} does not exist.")
    if any(n <= max(ordinals) for n in parsed.nested):
        raise SystemExit(f"Nested tables at ordinals {parsed.nested[:5]}; the kept ordinals would be ambiguous.")
    if any(end < 0 for _s, end in parsed.spans[:max(ordinals)]):
        raise SystemExit("A table before the last kept ordinal is never closed.")
    ascii_only = raw.isascii()
    parts = ["<html><body>\n<!-- finrail test slice: see the sidecar JSON for the original's SHA-256 and byte offsets -->\n"]
    kept, placeholders, cursor = [], [], len(parts[0].encode())
    for number in range(1, max(ordinals) + 1):
        table_start, table_end = parsed.spans[number - 1]
        if number not in ordinals:
            placeholder = "<table></table>\n"
            parts.append(placeholder)
            placeholders.append(number)
            cursor += len(placeholder)
            continue
        previous_end = parsed.spans[number - 2][1] if number > 1 else 0
        start = prefix_start(text, table_start, previous_end, prefix_bytes)
        segment = text[start:table_end] + "\n"
        segment_bytes = segment.encode("utf-8")
        # Byte offsets in the original; equal to character offsets for an ASCII file.
        byte_start = start if ascii_only else len(text[:start].encode("utf-8"))
        byte_table_start = table_start if ascii_only else len(text[:table_start].encode("utf-8"))
        byte_end = table_end if ascii_only else len(text[:table_end].encode("utf-8"))
        kept.append({"table": number, "slice_table": number,
                     "original": {"prose_start": byte_start, "table_start": byte_table_start, "table_end": byte_end,
                                  "sha256": sha256(raw[byte_start:byte_end])},
                     "slice": {"start": cursor, "end": cursor + len(segment_bytes) - 1}})
        parts.append(segment)
        cursor += len(segment_bytes)
    parts.append("</body></html>\n")
    slice_bytes = "".join(parts).encode("utf-8")
    for entry in kept:
        a, b = entry["slice"]["start"], entry["slice"]["end"]
        assert sha256(slice_bytes[a:b]) == entry["original"]["sha256"], entry["table"]
    manifest = {"table_count": len(parsed.spans), "nested_tables": parsed.nested, "kept": kept,
                "placeholders": placeholders, "prefix_bytes": prefix_bytes}
    return slice_bytes, manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original", type=Path, help="the filing as retrieved from the issuer (untrusted bytes)")
    ap.add_argument("--source-id", required=True, help="catalog source_id, e.g. ANTH-S1-2026")
    ap.add_argument("--out", type=Path, default=Path("tests/filings"))
    ap.add_argument("--tables", help="comma-separated table ordinals to keep; default: the profile's tables")
    ap.add_argument("--prefix-bytes", type=int, default=PREFIX_BYTES, help="prose kept before each table, at most")
    ap.add_argument("--retrieved-at", default=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    ap.add_argument("--served-sha256", help="SHA-256 of the HTTP body as served, when ORIGINAL is a normalised form of it")
    ap.add_argument("--served-bytes", type=int)
    ap.add_argument("--normalization-kind", choices=["edgar-document"],
                    help="machine-readable rule scripts/check_originals.py applies to a fresh download to get ORIGINAL back")
    ap.add_argument("--normalization", help="how ORIGINAL was derived from the served body, in one sentence")
    ap.add_argument("--retrieval-only", action="store_true",
                    help="write only a sidecar recording the original's hash (PDFs: no profile, nothing is sliced)")
    ap.add_argument("--note", help="anything the reader of the sidecar must know, e.g. a catalog hash mismatch")
    args = ap.parse_args()

    item = r.report(args.source_id)
    raw = args.original.read_bytes()
    original_hash = sha256(raw)
    sidecar = {
        "source_id": args.source_id,
        "company": item["company"],
        "format": item["format"],
        "catalog_sha256": item["sha256"],
        "catalog_match": original_hash == item["sha256"] or args.served_sha256 == item["sha256"],
        "original": {"url": item["url"], "retrieved_at": args.retrieved_at, "sha256": original_hash, "bytes": len(raw)},
    }
    if item.get("retrieved_url") and item["retrieved_url"] != item["url"]:
        sidecar["original"]["retrieved_url"] = item["retrieved_url"]
    if args.served_sha256:
        sidecar["original"]["served_sha256"] = args.served_sha256
        sidecar["original"]["served_bytes"] = args.served_bytes
    if args.normalization_kind:
        sidecar["original"]["normalization_kind"] = args.normalization_kind
        sidecar["original"]["normalization"] = args.normalization or ""
    if args.note:
        sidecar["note"] = args.note
    args.out.mkdir(parents=True, exist_ok=True)

    if args.retrieval_only:
        sidecar["retrieval_only"] = True
        (args.out / f"{args.source_id}.json").write_text(json.dumps(sidecar, indent=2) + "\n")
        print(f"{args.source_id}: retrieval-only; original {len(raw):,} bytes {original_hash[:12]}; "
              f"catalog {'matches' if sidecar['catalog_match'] else 'DOES NOT MATCH'}; nothing sliced")
        return

    prof = r.profile(item)
    ordinals = sorted(int(x) for x in args.tables.split(",")) if args.tables else sorted(prof["tables"])
    slice_bytes, manifest = slice_filing(raw, ordinals, args.prefix_bytes)
    html_path = args.out / f"{args.source_id}.html"
    html_path.write_bytes(slice_bytes)
    sidecar["original"]["table_count"] = manifest["table_count"]
    sidecar["original"]["nested_tables"] = manifest["nested_tables"]
    sidecar["slice"] = {"file": html_path.name, "sha256": sha256(slice_bytes), "bytes": len(slice_bytes),
                        "tool": "scripts/slice_filing.py", "prefix_bytes": manifest["prefix_bytes"]}
    sidecar["kept"] = manifest["kept"]
    sidecar["placeholders"] = manifest["placeholders"]
    (args.out / f"{args.source_id}.json").write_text(json.dumps(sidecar, indent=2) + "\n")
    print(f"{args.source_id}: kept tables {ordinals} of {manifest['table_count']}; "
          f"slice {len(slice_bytes):,} bytes {sidecar['slice']['sha256'][:12]}; original {len(raw):,} bytes "
          f"{original_hash[:12]}; catalog {'matches' if sidecar['catalog_match'] else 'DOES NOT MATCH'}")


if __name__ == "__main__":
    main()
