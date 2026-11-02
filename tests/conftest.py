"""Shared test hygiene.

The console's rate limiter and daily cap are process-wide (console/ops.py). Every test
starts with them empty, so a test that posts five times does not starve the next one.
"""
import pytest


@pytest.fixture(autouse=True)
def _fresh_console_limits():
    try:
        from console import ops
    except ImportError:          # the console extra is not installed
        yield
        return
    ops.BUCKETS.reset()
    ops.DAILY.reset()
    yield
    ops.BUCKETS.reset()
    ops.DAILY.reset()
