"""Small statistics, stdlib only: the Wilson interval, percentiles and list-price cost."""
import math

#: List prices per million tokens (input, output), USD, as `docs/model-comparison.md`
#: records them on 30 October 2026. A stub run costs nothing, which is the truth and
#: useless, so `--prices <model>` prices a stub's recorded token counts at a real model.
PRICES = {
    "nvidia/nemotron-3-super-120b-a12b": (0.30, 0.90),
    "nvidia/Nemotron-3.5-Super-VL": (0.30, 0.90),
    "claude-opus-5-5": (4.00, 20.00),
    "stub": (0.0, 0.0),
}


def wilson(passed: int, calls: int, z: float = 1.959964) -> tuple:
    """The 95% Wilson score interval for `passed` out of `calls`, as (low, high) rates.

    Preferred over the normal approximation because the pass rate sits near 100%, where
    the normal interval runs past 1 and says nothing."""
    if calls <= 0:
        return (0.0, 0.0)
    p = passed / calls
    denom = 1 + z * z / calls
    centre = (p + z * z / (2 * calls)) / denom
    half = z * math.sqrt(p * (1 - p) / calls + z * z / (4 * calls * calls)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def percentile(values: list, q: float) -> float:
    """The q-th percentile (0..1) by linear interpolation, or 0.0 for no values."""
    xs = sorted(float(v) for v in values if v is not None)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def median(values: list) -> float:
    return percentile(values, 0.5)


def mean(values: list) -> float:
    xs = [float(v) for v in values if v is not None]
    return sum(xs) / len(xs) if xs else 0.0


def list_cost(model: str, input_tokens: int, output_tokens: int):
    """What the call costs at list price with no caching, or None for an unpriced model."""
    price = PRICES.get(model or "")
    if price is None:
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 8)
