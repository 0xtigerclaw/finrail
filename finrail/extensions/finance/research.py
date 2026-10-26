"""Live public-source retrieval for finance.screen.

Tavily does discovery only: it recalls pages, and recall is not evidence.
Everything this module returns is checked downstream by the deterministic
gate in `gate.py`, which awards statuses only for quotes that occur
verbatim in the content fetched here. A page the model never saw cannot
be quoted, and a quote the page does not carry is discarded, never
repaired.

Configured by environment: TAVILY_API_KEY enables live retrieval, and
FINRAIL_TAVILY_BASE_URL overrides the endpoint (the test seam). With no
key configured the screen classifies its bundled fixture instead, and
says so in its result record; with a key configured, a failed retrieval
fails the run. There is no silent downgrade.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from finrail.core.errors import OpFailed

_TAVILY_DEFAULT = "https://api.tavily.com"

CATEGORIES = ("leadership", "regulatory", "cyber", "distress")

_QUERIES = {
    "leadership": "{c} chief executive OR chief financial officer "
                  "resignation OR departure OR appointment",
    "regulatory": "{c} lawsuit OR regulatory fine OR investigation "
                  "OR enforcement action",
    "cyber": "{c} data breach OR ransomware OR cyber attack OR leak",
    "distress": "{c} insolvency OR administration OR debt default "
                "OR going concern warning",
}

# Source governance is by host, decided in code, never by the model.
# A primary source can verify on its own; two independent credible
# secondaries can verify together; anything else can at most report.
_PRIMARY_SUFFIXES = (".gov", ".gov.uk", ".europa.eu")
_PRIMARY_HOSTS = {"sec.gov", "companieshouse.gov.uk", "fca.org.uk",
                  "ico.org.uk", "judiciary.uk", "thegazette.co.uk"}
_SECONDARY_HOSTS = {"reuters.com", "bloomberg.com", "ft.com", "bbc.com",
                    "bbc.co.uk", "wsj.com", "theguardian.com", "sky.com",
                    "cityam.com", "businesswire.com", "globenewswire.com",
                    "prnewswire.com", "techcrunch.com", "theregister.com",
                    "bleepingcomputer.com", "krebsonsecurity.com"}


def source_tier(host: str) -> str:
    host = (host or "").lower().removeprefix("www.")
    if host in _PRIMARY_HOSTS or host.endswith(_PRIMARY_SUFFIXES):
        return "primary"
    if host in _SECONDARY_HOSTS:
        return "credible_secondary"
    return "unlisted"


def _base() -> str:
    return (os.environ.get("FINRAIL_TAVILY_BASE_URL")
            or _TAVILY_DEFAULT).rstrip("/")


def configured() -> bool:
    return bool(os.environ.get("TAVILY_API_KEY"))


def _search_one(query: str, category: str) -> list:
    body = json.dumps({
        "query": query, "search_depth": "basic", "max_results": 5,
        "include_answer": False, "include_raw_content": False,
    }).encode()
    req = urllib.request.Request(
        _base() + "/search", data=body, method="POST",
        headers={"Authorization": "Bearer " + os.environ["TAVILY_API_KEY"],
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise OpFailed(
            f"PROVIDER_UNAVAILABLE: tavily search failed: {exc}") from exc
    pages = []
    for item in payload.get("results") or []:
        url = item.get("url") or ""
        host = urllib.parse.urlparse(url).netloc
        pages.append({
            "url": url, "publisher": host, "title": item.get("title") or "",
            "content": (item.get("content") or "")[:6000],
            "source_tier": source_tier(host), "category": category})
    return pages


def search(company: str) -> list:
    """One search per risk category. Returns fetched pages, deduplicated
    by URL; each page carries the tier its host is governed to."""
    pages, seen = [], set()
    for category in CATEGORIES:
        for page in _search_one(_QUERIES[category].format(c=company),
                                category):
            if page["url"] and page["url"] not in seen:
                seen.add(page["url"])
                pages.append(page)
    return pages
