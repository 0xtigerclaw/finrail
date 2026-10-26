"""core — the plugin API and the managers every plugin composes with.

Owns nothing domain-specific. If something here knows about mail, tasks or brains, it is
in the wrong place.
"""
from .api import RUNTIME, PluginAPI, api                       # noqa: F401
from .errors import (FinrailError, ContractViolation,         # noqa: F401
                     NotConfigured, OpFailed, Unverified)
