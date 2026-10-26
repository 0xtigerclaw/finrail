"""RANK — combine several ranked lists into one, without a model.

Reciprocal Rank Fusion. Each leg (the vault's grep or gbrain, the document index's BM25,
one day a vector leg) returns its own ranking with its own scoring scheme; RRF merges
them using only POSITION:

    score(item) = Σ over the legs it appears in  1 / (k + rank_in_that_leg)

That it ignores the scores themselves is the entire point. A grep count of 18 and a BM25
score of -6.2 are not on the same scale and never will be — any attempt to normalise them
into a shared scale is a guess that quietly decides which source wins. Rank position is
the one thing every retrieval method genuinely agrees on the meaning of.

No model, no prompt, no network: this is arithmetic over lists, which is what keeps the
"a single query searches everything" promise compatible with "retrieval never calls an
LLM".
"""

K = 60          # the conventional constant; damps the gap between rank 1 and rank 2


def rrf(legs: list, key=None, k: int = K, limit: int = 0) -> list:
    """Fuse ranked lists. `legs` is a list of lists, best-first within each.

    `key` maps an item to its identity, so the same thing found by two legs is one result
    that scores from both. Items keep their original dict and gain `score` and `legs`.
    """
    key = key or (lambda r: r.get("path") or r.get("slug") or repr(r))
    merged: dict = {}
    for leg in legs:
        for rank, item in enumerate(leg or []):
            ident = key(item)
            slot = merged.get(ident)
            if slot is None:
                slot = merged[ident] = {**item, "score": 0.0, "legs": []}
            slot["score"] += 1.0 / (k + rank + 1)          # rank is 0-based here
            slot["legs"].append(item.get("source") or item.get("backend") or "?")
    out = sorted(merged.values(), key=lambda r: (-r["score"], str(key(r))))
    return out[:limit] if limit else out
