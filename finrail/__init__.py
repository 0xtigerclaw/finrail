"""finrail — a deterministic dispatcher over pluggable capabilities.

Layering (see docs/ARCHITECTURE.md):
    core/         the plugin API + the managers every plugin composes with.
                  Owns nothing domain-specific.
    bundled/      plugins required for a working system.
    extensions/   everything beyond a working system.
"""
__version__ = "2.0.0"

#: THE PLUGIN API VERSION. An integer, bumped when a change would break a plugin written
#: against the previous one — a removed module, a changed Plugin contract, a renamed
#: manager. Not the same thing as `__version__`, which moves for reasons a plugin does
#: not care about.
API = 1

#: The oldest API this build still accepts. Compatible means
#: `MIN_SUPPORTED_API <= plugin.REQUIRES_API <= API`, which is the shape Chrome uses for
#: manifest_version: a new API can be introduced while the old one still works, and
#: dropping the old one is then a deliberate, visible act rather than a silent break.
MIN_SUPPORTED_API = 1

#: WHAT THIS BUILD HAS THAT A PLUGIN MAY ASK FOR BY NAME, finer than API. A plugin that
#: passes a newer keyword lists it in `REQUIRES_FEATURES`, and an finrail without it
#: refuses the plugin by name at load and before an install swaps it in, instead of a
#: TypeError on every turn (production, 6 Oct: `Scope(reads=...)` against an finrail
#: that had no `reads`). A change that adds something a plugin may come to rely on adds
#: its name here; a name is never reused for something else.
FEATURES = frozenset({
    "scope.ask_outside",            # Scope(ask_outside=...)
    "scope.sandbox",                # Scope(sandbox=...)
    "scope.net",                    # Scope(net=...)
    "scope.hide",                   # Scope(hide=...)
    "scope.reads",                  # Scope(reads=...), a guarded read such as sqlite3
    "plugin.requires_features",     # REQUIRES_FEATURES itself is checked at load
})

#: WHAT A PLUGIN MAY IMPORT. Everything here is a promise; everything else is internal
#: and may move without a bump. Written down because it was public by accident — 174
#: imports reaching into sixteen modules that had never been called an interface, which
#: is how the first refactor breaks every installed plugin at once.
PUBLIC_API = (
    "finrail.plugin",                 # the Plugin base class — every capability
    "finrail.core.errors",            # ContractViolation, NotConfigured, OpFailed, …
    "finrail.core.settings",          # SettingSpec, state(), home(), logs(), gen()
    "finrail.core.registry",          # settings(), has() — is that capability present?
    "finrail.core.requirements",      # Binary, Directory, SecretRequirement, …
    "finrail.core.secrets",           # get/put/exists — never printed
    "finrail.core.connections",       # declared connections and their checks
    "finrail.core.cron",              # CronJob
    "finrail.core.services",          # ServiceSpec
    "finrail.core.transport",         # Transport, register() — a wire an extension provides
    "finrail.core.progress",          # emit() — long runs say what they are doing
    "finrail.core.guard",             # the write-guard, so the repo plugin can version
    "finrail.core.frontmatter",       # typed markdown read/write
    "finrail.core.promptdoc",         # the prompt a judged capability is given
    "finrail.core.llm",               # asking a model, through the one place that does
    "finrail.core.tracker",           # run records
    "finrail.core.rank",              # scoring, shared so results order alike
    "finrail.core.agents",            # the agent definition: name, capability, pubkey,
                                       # operators, transport. A plugin that DEPLOYS an
                                       # agent has to read what an agent is, and core is
                                       # where that is decided — re-deriving the format
                                       # in a plugin is how two answers to "what is an
                                       # agent" come to exist.
    "finrail.core.serve",             # default_path(), ping(): the agent's one door
                                       # (a local socket). The transport that launches an
                                       # agent starts or reuses it and must find it.
    "finrail.core.atomic",            # write_text/write_json — replace a file in one
                                       # step. Needed by any plugin writing something
                                       # that is read concurrently; the case that asked
                                       # for it is an allow list a live client re-reads
                                       # on every message.
    "finrail.core.dispatch_core",     # dispatch() — CALL ANOTHER CAPABILITY BY NAME.
                                       # With registry.has(), this is the loosely coupled
                                       # way for one plugin to use another: no import, no
                                       # version coupling, and it degrades to "not
                                       # installed" instead of ImportError.
)
