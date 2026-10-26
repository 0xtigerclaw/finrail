"""THE namespace every plugin lives under — shipped and installed alike.

One name, two directories. `finrail_plugins.music` is the module whether music shipped
inside the wheel or was installed into the state directory last Tuesday, and that is the
whole point: a plugin that depends on another writes one import and it resolves either
way. Without this an installed plugin's `from finrail.extensions.apps import ops` finds
the SHIPPED apps and silently ignores the one beside it — which looks like it works, and
is the reason this exists.

Deliberately not `finrail.*`. A plugin called `settings` or `errors` must not be able to
shadow a core module, and a separate top-level namespace makes that structural rather
than a naming rule somebody has to remember.

Shipped comes first in the search path, so a name in both roots resolves to the one that
came with finrail. Discovery sees both and quarantines the loser rather than leaving the
collision silent — see registry.load().
"""
import pathlib

#: Where the shipped plugins are. Beside this package, inside the wheel.
SHIPPED = pathlib.Path(__file__).resolve().parent.parent / "finrail" / "extensions"


def roots() -> list:
    """[(kind, directory)] in precedence order. Recomputed rather than cached: the state
    directory is an environment fact and tests move it."""
    from finrail.core.settings import state
    return [("extension", SHIPPED), ("installed", state() / "plugins")]


def refresh() -> list:
    """Point `__path__` at the current roots. Called by `registry.load()`."""
    global __path__
    __path__ = [str(d) for _kind, d in roots()]
    return __path__


__path__ = [str(SHIPPED)]                 # replaced by refresh() once settings exist
