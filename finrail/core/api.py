"""THE PLUGIN API — the only door a plugin has to the world.

A plugin declares what it needs and what it offers; core wires it up. Nothing in a
plugin reaches around this to touch launchd, the keychain, or the ledger directly.

Declaration is DECLARATIVE, by module attributes the loader reads (see registry.py):

    NAME, DESCRIPTION
    COMMANDS           [Capability, ...]         capability classes or instances
    SETTINGS           [SettingSpec, ...]
    CONNECTION_TYPES   [ConnectionType, ...]
    SERVICES           [ServiceSpec, ...]
    CRONS              [CronJob, ...]
    REQUIREMENTS       [Requirement, ...] or a callable returning that
    HOOKS              {"before_llm": fn, ...}

There is deliberately no `register()` function: a plugin states facts, it does not run
code to install itself, and it certainly does not restate a capability's rules (those
live on the capability class and nowhere else).

HOOKS are how the mere PRESENCE of a plugin changes how every other plugin behaves:
    before_llm(capability, ctx)  -> ctx    memory injects lessons here (and puts
                                           fields for the call's log under `record`)
    after_run(record)                      memory journals every run here
    before_write(path) / after_write(path) repository pulls/commits here
    extend_capability(capability)-> cap    a connection swaps a backend here
"""
from .errors import ContractViolation
from .settings import Settings

EVENTS = ("before_llm", "after_run", "before_write", "after_write", "extend_capability",
          "standing_lessons")

#: The events whose subscribers RETURN the next value. The rest are notifications.
TRANSFORMING = ("before_llm", "extend_capability", "standing_lessons")


class HookBus:
    """Subscribers per event, in registration order.

    A hook that raises SURFACES the error. Swallowing it would mean a lesson silently
    stopped being injected, or a commit silently stopped happening — exactly the class of
    invisible failure this system exists to prevent.
    """

    def __init__(self):
        self._subs: dict[str, list] = {e: [] for e in EVENTS}

    def on(self, event: str, fn) -> None:
        if event not in EVENTS:
            raise ContractViolation(
                f"unknown hook event '{event}' (known: {', '.join(EVENTS)})")
        self._subs[event].append(fn)

    def subscribers(self, event: str) -> list:
        return list(self._subs.get(event, ()))

    def emit(self, event: str, payload=None, *args, **kw):
        """Call every subscriber. For transforming events (before_llm,
        extend_capability) each subscriber receives the running value and returns the
        next one; for notifications the payload is passed through unchanged."""
        value = payload
        for fn in self._subs.get(event, ()):
            out = fn(value, *args, **kw)
            if event in TRANSFORMING and out is not None:
                value = out
        return value


class Runtime:
    """Everything the plugins collectively declared. One instance per process."""

    def __init__(self):
        self.settings = Settings()
        self.hooks = HookBus()
        self.commands: dict = {}            # name -> capability instance
        self.connection_types: dict = {}    # name -> ConnectionType
        self.services: dict = {}            # label -> ServiceSpec
        self.crons: list = []               # CronJob
        self.plugins: dict = {}             # ACTIVE plugin name -> module (bundled + enabled extensions)
        self.plugin_requires: dict = {}     # plugin name -> the plugins it declares it calls
        self.installed: dict = {}           # EVERY discovered plugin name -> {kind, enabled, description}
        self._requirements: list = []       # (plugin, list|callable)
        self.loaded = False

    def add_requirements(self, plugin: str, reqs) -> None:
        self._requirements.append((plugin, reqs))

    def requirements(self) -> list:
        """Resolved requirements. Callables are evaluated late, because a bundle's
        requirements usually depend on its settings (which accounts exist, which
        providers they use)."""
        out = []
        for plugin, reqs in self._requirements:
            items = reqs() if callable(reqs) else reqs
            for r in (items or ()):
                r.plugin = plugin
                out.append(r)
        return out

    def reset(self) -> None:
        """Drop all declarations. Tests use this between cases."""
        self.__init__()


RUNTIME = Runtime()


class PluginAPI:
    """The per-plugin facade. The loader hands one to each plugin; anything a plugin
    needs at RUN time (read a setting, open a connection, get a secret) goes through it."""

    def __init__(self, plugin: str, runtime: Runtime = None):
        self.plugin = plugin
        self._rt = runtime or RUNTIME

    # -- reads at run time ---------------------------------------------------
    def setting(self, name: str, default=None):
        return self._rt.settings.get(name, default)

    def require_setting(self, name: str):
        return self._rt.settings.require(name)

    def secret(self, name: str) -> str:
        from . import secrets
        return secrets.get(name)

    def connection(self, name: str):
        from . import connections
        return connections.open(name)

    def hooks(self) -> HookBus:
        return self._rt.hooks

    def emit(self, event: str, payload=None, *a, **kw):
        return self._rt.hooks.emit(event, payload, *a, **kw)

    def __repr__(self):
        return f"<PluginAPI {self.plugin}>"


def api(plugin: str) -> PluginAPI:
    return PluginAPI(plugin)
