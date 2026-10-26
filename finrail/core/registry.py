"""DISCOVERY — find the plugins, read what they declared, hand it to the runtime.

Three roots are walked. They are not symmetric:

  bundled/      required for a working system. Always absorbed — there is no state in
                which a bundled plugin's capabilities are missing. You cannot disable one.
  extensions/   everything beyond a working system. Every module under this root is
                DISCOVERED (imported, so its name/description are knowable) whether or
                not it is active, but only ABSORBED — its capabilities, settings, hooks
                registered — when it is not listed in `plugins.disabled`.

  <state>/plugins/  INSTALLED by `plugin.install` rather than shipped. Treated exactly
                as `extensions/` once found — same enable/disable, same absorption — and
                found differently: by PATH, not by package, because it must live outside
                the package. Anything inside `finrail/` is deleted by the next
                `pip install --upgrade`, which is a bad way to lose somebody's plugin.

That is the installed-vs-enabled distinction: an extension can be installed (its code is
present, `finrail plugins list` can see it) without being enabled (its capabilities do
not exist in the catalogue, its settings are not declared, its hooks never fire).

Installed plugins are imported under `finrail_plugins.<name>`, never `finrail.*`. A
plugin called `settings` or `errors` must not be able to shadow a core module, and the
separate namespace makes that structural rather than a naming convention nobody enforces.

A name found in more than one root is a CONFLICT and is reported, not resolved. Silently
preferring one would mean an installed plugin could shadow a bundled one — the same
reasoning that already refuses two plugins declaring one capability.

Discovery is lazy and happens once per process. `load(force=True)` re-reads everything
(tests use it after changing settings that determine what a bundle declares, and the
enable/disable capabilities use it to make a change take effect immediately).
"""
import importlib
import os
import json
import pathlib
import pkgutil

from ..plugin import Plugin
from .api import RUNTIME
from . import owners
from .errors import ContractViolation

BUNDLED_ROOT = "bundled"
#: Kept for the shipped plugins' directory name; discovery goes through
#: finrail_plugins.roots() now, not through this package path.
EXTENSION_ROOT = "extensions"
DISABLED_SETTING = "plugins.disabled"

#: Installed plugins hang from here, NOT from `finrail.*`. A plugin named `settings` or
#: `errors` must not be able to shadow a core module; a separate namespace makes that
#: structural rather than a convention.
INSTALLED_NAMESPACE = "finrail_plugins"

#: Plugins that are present and will not be used, with the reason. Kept APART from the
#: installed table rather than folded into it: a quarantined plugin whose name collides
#: with a shipped one was overwriting the shipped one's record, so the thing actually in
#: use reported itself as the thing that had been refused.
QUARANTINE = {}


def _instantiate(item) -> Plugin:
    cap = item() if isinstance(item, type) else item
    if not isinstance(cap, Plugin):
        raise ContractViolation(f"{item!r} is not a Plugin")
    if not cap.name:
        raise ContractViolation(f"{item!r} has no name")
    return cap


def _absorb(mod, name: str) -> None:
    """Read one plugin module's declarations into the runtime."""
    RUNTIME.plugins[name] = mod

    for spec in getattr(mod, "SETTINGS", ()) or ():
        spec.plugin = name
        RUNTIME.settings.declare(spec)

    for item in getattr(mod, "COMMANDS", ()) or ():
        cap = _instantiate(item)
        cap.bundle = name
        # Every write the capability's own code makes is its plugin's, however it is
        # called (core/owners.py, docs/LAYERS.md rule 2).
        for meth in ("execute", "run"):
            if callable(getattr(type(cap), meth, None)) and not getattr(
                    cap.__dict__.get(meth), "__finrail_attributed__", False):
                setattr(cap, meth, owners.attribute(name, cap, meth))
        if cap.name in RUNTIME.commands:
            raise ContractViolation(
                f"two plugins declare capability '{cap.name}' "
                f"({RUNTIME.commands[cap.name].bundle} and {name})")
        RUNTIME.commands[cap.name] = cap

    for ct in getattr(mod, "CONNECTION_TYPES", ()) or ():
        ct.plugin = name
        RUNTIME.connection_types[ct.name] = ct

    # A CALLABLE is allowed, exactly as `REQUIREMENTS` is and for the same reason: a
    # plugin whose services depend on what is on disk cannot answer at import time. The
    # a chat plugin runs one harness per agent DEFINED, and a list built when the module
    # loaded would be empty on the run that defines the first one — so `service list`
    # would show nothing for an agent that had just been created.
    svcs = getattr(mod, "SERVICES", ()) or ()
    if callable(svcs):
        try:
            svcs = svcs() or ()
        except Exception:                                        # noqa: BLE001
            # A plugin that cannot enumerate its services must not stop the registry
            # loading: every other capability it declares still works, and `doctor` is
            # where a broken plugin is reported.
            svcs = ()
    for svc in svcs:
        svc.plugin = name
        RUNTIME.services[svc.label] = svc

    for job in getattr(mod, "CRONS", ()) or ():
        job.plugin = name
        RUNTIME.crons.append(job)

    reqs = getattr(mod, "REQUIREMENTS", None)
    if reqs is not None:
        RUNTIME.add_requirements(name, reqs)

    for event, fn in (getattr(mod, "HOOKS", {}) or {}).items():
        RUNTIME.hooks.on(event, fn)

    # What this plugin calls in other plugins, so doctor can say "X needs Y and Y is
    # off" before the call fails in a message about the wrong thing. One declaration,
    # on the module, beside REQUIRES_API; the requirement is derived from it here.
    needs = tuple(str(x) for x in (getattr(mod, "REQUIRES", ()) or ()))
    if needs:
        from .requirements import PluginRequirement
        RUNTIME.plugin_requires[name] = list(needs)
        RUNTIME.add_requirements(name, [PluginRequirement(name, n) for n in needs])


def _discover(pkg_root: str, root: str) -> list:
    """Import every module under a package root — BUNDLED only, now.

    Extensions moved to `finrail_plugins`, where installed plugins already were, so that
    one import resolves whichever root a plugin came from. Bundled plugins stay inside
    the package because they are not optional and never move.

    Returns [(name, mod), ...]. Importing has
    no side effects — a plugin module only defines classes/constants at import time, so
    this is safe to do for a module that turns out to be disabled."""
    out = []
    try:
        parent = importlib.import_module(f"{pkg_root}.{root}")
    except ModuleNotFoundError:
        return out
    for info in pkgutil.iter_modules(parent.__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{pkg_root}.{root}.{info.name}")
        out.append((getattr(mod, "NAME", info.name), mod))
    return out


def installed_root():
    """Where `plugin.install` puts code. Outside the package, deliberately."""
    from .settings import state
    return state() / "plugins"


def _namespace():
    """`finrail_plugins`, with its search path pointed at the current roots.

    Refreshed on every load rather than cached: the state directory is an environment
    fact and tests move it, so a path computed once at import time would be stale for
    everything after the first test that changes it.
    """
    import finrail_plugins
    finrail_plugins.refresh()
    return finrail_plugins


def api_fault(mod, name: str) -> str:
    """Why this plugin cannot run against this build, or "".

    Checked BEFORE anything is absorbed, because the alternative is discovering the
    incompatibility as an AttributeError somewhere inside a capability, six steps from
    the cause.

    An installed plugin must SAY what it was written for. One that does not cannot be
    checked, and unchecked is exactly how a core refactor turns into an import-time
    explosion for a user who changed nothing.
    """
    import finrail
    wants = getattr(mod, "REQUIRES_API", None)
    if wants is None:
        return (f"declares no REQUIRES_API, so nothing can tell whether it works with "
                f"this build (this one provides API {finrail.API})")
    if not isinstance(wants, int):
        return f"REQUIRES_API is {wants!r}; it must be an integer"
    if wants > finrail.API:
        return (f"needs API {wants} and this build provides {finrail.API} — the plugin "
                f"is newer than finrail; update finrail")
    if wants < finrail.MIN_SUPPORTED_API:
        return (f"was written for API {wants} and this build no longer supports "
                f"anything below {finrail.MIN_SUPPORTED_API} — the plugin needs "
                f"updating")
    # Finer than the API number: named things a plugin needs that this build may lack.
    want = getattr(mod, "REQUIRES_FEATURES", ()) or ()
    if isinstance(want, str):
        return "REQUIRES_FEATURES must be a list of names, not one string"
    missing = sorted(str(f) for f in want if f not in finrail.FEATURES)
    if missing:
        return (f"needs {', '.join(missing)}, which this finrail does not provide, so the "
                f"plugin is newer than finrail; update finrail")
    return ""


def _discover_path(root) -> list:
    """Import every plugin package under a DIRECTORY, as `finrail_plugins.<name>`.

    Returns `(found, faults)`. A plugin that will not import becomes a FAULT rather than
    an exception: it is reported and disabled, and the rest of the system starts. Two
    reasons, and the second is the one that matters. An installed plugin that silently is
    not there looks exactly like one that was never installed — and if a bad plugin were
    fatal, the one command that would remove it is a command this system could no longer
    run.
    """
    import importlib
    import sys
    out, faults = [], []
    if not root.is_dir():
        return out, faults
    _namespace()
    for entry in sorted(root.iterdir()):
        if entry.name.startswith((".", "_")) or not (entry / "__init__.py").is_file():
            continue
        dotted = f"{INSTALLED_NAMESPACE}.{entry.name}"
        try:
            mod = sys.modules.get(dotted) or importlib.import_module(dotted)
        except Exception as e:                                       # noqa: BLE001
            sys.modules.pop(dotted, None)
            faults.append((entry.name, f"will not import: {type(e).__name__}: "
                                       f"{str(e)[:150]}"))
            continue
        # Which ROOT it actually came from. `finrail_plugins` searches shipped first, so
        # a name in both resolves to the shipped one — and the caller needs to know that
        # happened rather than assume the directory it was walking is what got loaded.
        came_from = pathlib.Path(getattr(mod, "__file__", "") or "").resolve().parent
        if came_from != entry.resolve():
            faults.append((entry.name,
                           f"a plugin of the same name is already loaded from "
                           f"{came_from}, and the one that ships with finrail wins"))
            continue
        out.append((getattr(mod, "NAME", entry.name), mod))
    return out, faults


def _disabled_from_disk() -> set:
    """The disabled list, read directly rather than through Settings.get().

    This runs during load() itself, so the declared-settings registry a normal `get()`
    would consult is still being built. Settings storage has no such bootstrapping
    problem (its location is env/default only — see core/settings.py), so reading the
    raw file is safe and avoids a chicken-and-egg dependency on load() having finished.
    """
    from .settings import state
    p = state() / "settings.json"
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data.get(DISABLED_SETTING) or [])


#: Which tiers a process loads. Unset: everything. `core`: no plugin at all, so a core
#: test that quietly needs a bundled plugin fails here instead of passing by accident.
#: `bundled`: bundled only, every extension present but off, for the same reason one
#: tier up. Set by the layer test runs (tests/conftest.py, docs/LAYERS.md).
LAYER_ENV = "FINRAIL_LAYER"


def layer() -> str:
    v = (os.environ.get(LAYER_ENV) or "").strip().lower()
    if v and v not in ("core", "bundled"):
        raise ContractViolation(f"{LAYER_ENV}={v!r}: core, bundled, or unset")
    return v


def load(force: bool = False) -> None:
    if RUNTIME.loaded and not force:
        return
    if force:
        RUNTIME.reset()
        QUARANTINE.clear()
        _drop_stale_modules()
    pkg_root = __name__.rsplit(".", 2)[0]              # "finrail"

    tier = layer()
    for name, mod in ([] if tier == "core" else _discover(pkg_root, BUNDLED_ROOT)):
        RUNTIME.installed[name] = {"kind": "bundled", "enabled": True,
                                   "description": getattr(mod, "DESCRIPTION", "")}
        _absorb(mod, name)

    disabled = _disabled_from_disk()
    if tier == "core":
        RUNTIME.loaded = True
        return

    import finrail_plugins
    for kind, root in finrail_plugins.roots():
        found, faults = _discover_path(root)
        for name, reason in faults:
            if kind == "extension":
                # Between things that SHIP together this is a build defect and must be
                # loud: it can be fixed before release, and hiding it in CI is how it
                # ships.
                raise ContractViolation(f"the shipped plugin '{name}' {reason}")
            _quarantine(name, reason)
        for name, mod in found:
            if name in RUNTIME.installed:
                raise ContractViolation(
                    f"two plugins are called '{name}': one "
                    f"{RUNTIME.installed[name]['kind']}, one {kind}.")
            fault = api_fault(mod, name) if kind == "installed" else ""
            if fault:
                _quarantine(name, fault, getattr(mod, "DESCRIPTION", ""))
                continue
            enabled = name not in disabled and tier != "bundled"
            RUNTIME.installed[name] = {"kind": kind, "enabled": enabled,
                                       "description": getattr(mod, "DESCRIPTION", "")}
            if enabled:
                try:
                    _absorb(mod, name)
                except Exception as e:                               # noqa: BLE001
                    if kind == "extension":
                        raise
                    # It imported and then would not declare itself. Undo whatever it got
                    # halfway through, or the catalogue holds half a plugin.
                    _forget(name)
                    _quarantine(name, f"failed while declaring itself: "
                                      f"{type(e).__name__}: {str(e)[:150]}",
                                getattr(mod, "DESCRIPTION", ""))

    RUNTIME.loaded = True
    _warn_if_modified()


_WARNED_DRIFT: set = set()


def _warn_if_modified() -> None:
    """An installed plugin edited in place is said out loud when finrail starts.

    Once per process per plugin, and only for a real edit — a plugin with no manifest yet
    is a provenance gap `plugins.list` reports, not something to shout about on every
    command. Never fatal here: refusing to start would take down every agent over a file
    somebody can fix, and `finrail upgrade` is the place that refuses.
    """
    import logging
    try:
        from . import plugin_deploy
        for d in plugin_deploy.modified():
            if d["name"] not in _WARNED_DRIFT:
                _WARNED_DRIFT.add(d["name"])
                logging.getLogger("finrail.plugins").warning(
                    "%s — running code no commit holds", plugin_deploy.describe(d))
    except Exception:                                               # noqa: BLE001
        pass


#: Where an installed plugin came from. One file for all of them rather than a marker
#: inside each: an update replaces the plugin's directory wholesale, and provenance that
#: lives inside it would be replaced by whatever the new copy happened to contain.
PROVENANCE = ".installed.json"


def provenance() -> dict:
    """{name: {repo, ref, commit, catalogue, at}} — where each installed plugin came from.

    Kept because "what is installed" and "what did I agree to install" are different
    questions, and the second is the one asked after something starts behaving oddly.
    """
    p = installed_root() / PROVENANCE
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_install(name: str, info: dict) -> None:
    root = installed_root()
    root.mkdir(parents=True, exist_ok=True)
    data = provenance()
    data[name] = info
    (root / PROVENANCE).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def forget_install(name: str) -> None:
    data = provenance()
    if data.pop(name, None) is not None:
        (installed_root() / PROVENANCE).write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n")


#: What the plugin directories looked like at the last load: the roots, and the state of
#: the provenance file. Compared rather than assumed, because both answers matter and
#: they are different questions.
_SEEN = {"roots": None, "stamp": None}


def _plugin_stamp() -> tuple:
    """A value that changes when the INSTALLED plugins change, and not otherwise."""
    p = installed_root() / PROVENANCE
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def _drop_stale_modules() -> bool:
    """Forget cached plugin modules, but only when what is on disk has actually moved.

    Both halves of that are load-bearing. Never dropping them means `plugin.update`
    replaces a directory and the running process keeps the old code — an update that
    reports success and changes nothing. Dropping them on every reload means any
    reference held across one silently points at a module the registry no longer uses,
    so a monkeypatch or a cached `ops` handle applies to a copy nobody runs. That second
    failure is the quieter one, and it is how a test can pass alone and fail in a suite.

    So: the roots moving, or an install/update/uninstall touching the provenance file.
    A plain `load(force=True)` — which is most of them — changes neither and keeps the
    modules it already has.
    """
    import finrail_plugins
    import sys
    roots = tuple(str(d) for _k, d in finrail_plugins.roots())
    stamp = _plugin_stamp()
    changed = (_SEEN["roots"] is not None
               and (roots != _SEEN["roots"] or stamp != _SEEN["stamp"]))
    _SEEN["roots"], _SEEN["stamp"] = roots, stamp
    if not changed:
        return False
    for name in [m for m in sys.modules
                 if m == INSTALLED_NAMESPACE or m.startswith(INSTALLED_NAMESPACE + ".")]:
        del sys.modules[name]
    return True


def _quarantine(name: str, reason: str, description: str = "") -> None:
    """Record a plugin that is present and will not be used, and why."""
    QUARANTINE[name] = {"name": name, "kind": "installed", "enabled": False,
                        "description": description, "fault": reason}


def _forget(name: str) -> None:
    """Undo a partial absorption. A plugin that failed halfway through declaring itself
    must not leave half its capabilities in the catalogue — that is worse than not being
    there, because the half that works looks like the whole thing."""
    RUNTIME.plugins.pop(name, None)
    for table in (RUNTIME.commands, RUNTIME.connection_types, RUNTIME.services):
        for key in [k for k, v in table.items() if getattr(v, "bundle", None) == name
                    or getattr(v, "plugin", None) == name]:
            del table[key]
    RUNTIME.crons[:] = [c for c in RUNTIME.crons if getattr(c, "plugin", None) != name]


def quarantined() -> list:
    """Plugins that are installed, will not run, and said why."""
    load()
    return [dict(v) for v in QUARANTINE.values()]


def installed() -> list:
    """Every discovered plugin, bundled and extension, enabled or not."""
    load()
    return sorted(({"name": n, **info} for n, info in RUNTIME.installed.items()),
                  key=lambda p: (p["kind"], p["name"]))


def is_extension(name: str) -> bool:
    load()
    return RUNTIME.installed.get(name, {}).get("kind") == "extension"


def enable(name: str) -> dict:
    """Turn an installed extension on. Refuses anything that isn't a known, disabled
    extension — enabling a name that was never installed would silently do nothing, and
    enabling a bundled plugin is a no-op dressed up as an action."""
    load()
    info = RUNTIME.installed.get(name)
    if info is None:
        raise ContractViolation(f"no installed plugin '{name}'")
    if info["kind"] == "bundled":
        raise ContractViolation(f"'{name}' is bundled — it is always enabled")
    disabled = _disabled_from_disk()
    disabled.discard(name)
    _write_disabled(disabled)
    load(force=True)
    return {"plugin": name, "enabled": True,
            "gained": sorted(c for c, cap in RUNTIME.commands.items()
                             if cap.bundle == name)}


def disable(name: str) -> dict:
    """Turn an installed extension off. Bundled plugins cannot be disabled — the
    architecture's own promise ('bundled = required for a working system') would be a
    lie if this let one through."""
    load()
    info = RUNTIME.installed.get(name)
    if info is None:
        raise ContractViolation(f"no installed plugin '{name}'")
    if info["kind"] == "bundled":
        raise ContractViolation(
            f"'{name}' is bundled — required for a working system, cannot be disabled")
    lost = sorted(c for c, cap in RUNTIME.commands.items() if cap.bundle == name)
    disabled = _disabled_from_disk()
    disabled.add(name)
    _write_disabled(disabled)
    load(force=True)
    return {"plugin": name, "enabled": False, "lost": lost}


def _write_disabled(names: set) -> None:
    from .settings import ensure, state
    ensure()
    p = state() / "settings.json"
    data = json.loads(p.read_text()) if p.is_file() else {}
    data[DISABLED_SETTING] = sorted(names)
    p.write_text(json.dumps(data, indent=2, sort_keys=True))


# ── accessors ───────────────────────────────────────────────────────────────
def commands() -> dict:
    load()
    return dict(RUNTIME.commands)


def command(name: str) -> Plugin:
    load()
    cap = RUNTIME.commands.get(name)
    if cap is None:
        near = [n for n in RUNTIME.commands if name in n or n.startswith(name.split(".")[0])]
        hint = f" — did you mean: {', '.join(sorted(near)[:5])}" if near else ""
        raise ContractViolation(f"no capability '{name}'{hint}")
    # a plugin may swap a capability's backend simply by being installed
    return RUNTIME.hooks.emit("extend_capability", cap) or cap


def has_run(rid: str) -> bool:
    """Whether this names an existing run. Asked so `watch` can take either a run id or a
    thread name without the caller having to say which."""
    from . import tracker
    try:
        tracker.load(rid)
        return True
    except Exception:                                            # noqa: BLE001
        return False


def has(name: str) -> bool:
    load()
    return name in RUNTIME.commands


def of_kind(kind: str) -> list:
    return [c for c in commands().values() if c.kind == kind]


def bundles() -> dict:
    load()
    return dict(RUNTIME.plugins)


def settings():
    load()
    return RUNTIME.settings


def hooks():
    load()
    return RUNTIME.hooks


def catalogue() -> list:
    """Every capability's rules — the single source the router, `finrail rules` and the
    full agent all read. A capability added anywhere shows up here with no other edit."""
    return sorted((c.rules() for c in commands().values()), key=lambda r: r["name"])
