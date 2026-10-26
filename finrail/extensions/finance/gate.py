"""The deterministic evidence gate for finance.screen.

Discovery may be probabilistic, but escalation is deterministic. A model
may propose a candidate and select quotes; only this code awards a status,
and only after every quote has been validated verbatim against the
extracted content it claims to come from.

    VERIFIED     every assertion has a primary source, or two independent
                 credible-secondary publishers
    REPORTED     every assertion has evidence, but at least one rests on a
                 single credible-secondary publisher
    PARTIAL      only some assertions meet a threshold
    CONFLICTING  qualifying evidence both supports and contradicts
    CONTRADICTED qualifying evidence directly refutes the claim
    UNRESOLVED   no qualifying evidence, but extraction failed somewhere
    REJECTED     searches completed; only unsupported or low-tier material
"""

from __future__ import annotations

import re

_PUNCT_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"',
                            "”": '"', "–": "-", "—": "-",
                            " ": " "})


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").translate(_PUNCT_MAP)).strip()


def quote_in_content(quote: str, content: str) -> bool:
    """True iff the quote occurs verbatim in the content, modulo whitespace
    and punctuation-variant normalization. Empty quotes never validate."""
    q = normalize_text(quote)
    return bool(q) and q in normalize_text(content)


def _assertion_level(evidence: list, index: int, relationship: str) -> str:
    matching = [e for e in evidence if index in e.get(relationship, [])]
    if any(e.get("source_tier") == "primary" for e in matching):
        return "verified"
    secondary = [e for e in matching
                 if e.get("source_tier") == "credible_secondary"]
    if len({e.get("publisher") for e in secondary if e.get("publisher")}) >= 2:
        return "verified"
    if secondary:
        return "reported"
    return "none"


def classify(candidate: dict, evidence: list,
             extraction_failures: list | None = None) -> dict:
    """Award a status to one candidate. `evidence` must already be
    quote-validated by the caller; unvalidated quotes never reach here."""
    assertions = candidate.get("assertions") or []
    positive = [_assertion_level(evidence, i, "supports_assertions")
                for i in range(len(assertions))]
    negative = [_assertion_level(evidence, i, "contradicts_assertions")
                for i in range(len(assertions))]
    has_pos = any(level != "none" for level in positive)
    has_neg = any(level != "none" for level in negative)
    if has_pos and has_neg:
        status = "conflicting"
    elif has_neg:
        status = "contradicted"
    elif positive and all(level == "verified" for level in positive):
        status = "verified"
    elif positive and all(level in ("verified", "reported") for level in positive):
        status = "reported"
    elif has_pos:
        status = "partial"
    elif extraction_failures:
        status = "unresolved"
    else:
        status = "rejected"
    return {"candidate": candidate, "status": status,
            "evidence": evidence,
            "extraction_failures": extraction_failures or []}


def validate_evidence(candidate: dict, raw_evidence: list) -> tuple:
    """Split raw evidence into (validated, discarded_count). A quote that is
    not verbatim in its own content is discarded, never repaired."""
    validated, discarded = [], 0
    for item in raw_evidence:
        if quote_in_content(item.get("quote", ""), item.get("content", "")):
            validated.append({k: v for k, v in item.items() if k != "content"})
        else:
            discarded += 1
    return validated, discarded


def claim_check(figure: str, source_text: str) -> bool:
    """The dataroom claim check: a figure may be proposed only if the cited
    source carries it. Whitespace-normalized substring, nothing cleverer;
    a space before a percent sign is typography, not a different figure."""
    if not figure:
        return False
    needle = re.sub(r"\s+%", "%", normalize_text(str(figure)))
    haystack = re.sub(r"\s+%", "%", normalize_text(source_text))
    return needle in haystack
