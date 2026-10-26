"""RULE 2 OF THE LAYER CONTRACT, WATCHED AS IT HAPPENS. See docs/LAYERS.md.

A store is written only by the plugin that owns it. The import scan cannot see a write,
and the review of 25 Sept found fifteen by reading every write site by hand
(docs/LAYERS-REVIEW.md). Nothing kept the sixteenth out.

So a plugin DECLARES the stores it owns, and while one of its capabilities executes,
every file this Python process opens for writing, renames, removes or creates is
checked against those declarations by an audit hook (`sys.addaudithook`). A write into
a store another plugin owns is a CROSSING: it is written on the run record
(`crossed_stores`), logged, and collected for the test suite, which fails on a pair it
has not seen before (tests/stores_baseline.txt), the same ratchet as the import scan.

It records; it does not refuse. Refusing would break the fifteen known crossings the
day this lands. A crossing on the record is what O9 ("layers hold") counts.

THE DECLARATION. A plugin module sets `STORES`, either setting names whose values are
the roots it owns (`("tasks.dir",)`), or a callable returning paths, for a store whose
root is computed. A setting of kind `list` may hold paths, or dicts with a `path`.
Nested roots are allowed: the deepest root that contains a path owns it, so a tasks
folder inside a notes vault is the tasks plugin's.

WHAT IT CANNOT SEE: a write made by another process (git, notmuch, mbsync, the engine).
Those are declared ownership questions, and the review lists them.
"""
import contextlib
import contextvars
import os
import pathlib
import sys
import threading

from . import log

_logger = log.get(__name__)

#: (plugin, run id) of the capability executing in this context, or None.
_WRITER = contextvars.ContextVar("finrail_store_writer", default=None)
#: Crossings seen in this context's current run: list of dicts, flushed to the record.
_FOUND = contextvars.ContextVar("finrail_store_found", default=None)
#: Every (writer, owner) pair this process has seen, for the test suite's ratchet.
SEEN: dict = {}

_installed = False
_lock = threading.Lock()
_quiet = threading.local()          # set while this module writes, so it never sees itself

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _paths(value) -> list:
    out = []
    for v in value if isinstance(value, (list, tuple)) else [value]:
        if isinstance(v, dict):
            v = v.get("path") or v.get("root") or ""
        if v:
            out.append(str(v))
    return out


def _resolve(p) -> str:
    try:
        return str(pathlib.Path(os.path.expanduser(str(p))).resolve())
    except (OSError, RuntimeError, ValueError):
        return ""


def stores() -> dict:
    """{resolved root: owning plugin} for every store an active plugin declares."""
    from . import registry
    out = {}
    for name, mod in list(registry.RUNTIME.plugins.items()):
        decl = getattr(mod, "STORES", ()) or ()
        try:
            roots = decl() if callable(decl) else [
                p for key in decl for p in _paths(registry.settings().get(key))]
        except Exception:                                        # noqa: BLE001
            continue            # a store that cannot be resolved here is not in play
        for root in roots:
            r = _resolve(root)
            if r and r != "/":
                out[r] = name
    return out


def owner_of(path, table: dict = None) -> str:
    """The plugin that owns `path`, by the deepest declared root containing it."""
    p = _resolve(path)
    if not p:
        return ""
    best, owner = "", ""
    for root, plugin in (table if table is not None else stores()).items():
        if (p == root or p.startswith(root + os.sep)) and len(root) > len(best):
            best, owner = root, plugin
    return owner


@contextlib.contextmanager
def acting(plugin: str, rid: str = ""):
    """This context is executing a capability of `plugin`. Crossings are collected and
    written to run `rid` when it ends."""
    install()
    t_w = _WRITER.set((plugin or "", rid or ""))
    t_f = _FOUND.set([])
    try:
        yield
    finally:
        found = _FOUND.get() or []
        _FOUND.reset(t_f)
        _WRITER.reset(t_w)
        if found and rid:
            _flush(rid, found)


def attribute(plugin: str, cap, meth: str):
    """A callable for `cap.<meth>` (execute or run) that attributes its writes to
    `plugin` even when it is called directly rather than dispatched: by a test, or by
    another plugin reaching round dispatch (ingress running apps' deploy in-process is
    the review's first finding). The OUTERMOST plugin keeps the pen: a direct call does
    not hand the write to the plugin it reached. A real dispatch does, through `acting`.

    The method is looked up on the class AT CALL TIME, so replacing it on the class (as
    tests do) still takes effect."""
    def attributed(*a, **kw):
        fn = getattr(type(cap), meth).__get__(cap, type(cap))
        if _WRITER.get() is not None:
            return fn(*a, **kw)
        with acting(plugin, ""):
            return fn(*a, **kw)
    # The capability's own signature, name and doc, which dispatch and converse read to
    # check a command's parameters.
    import functools
    functools.update_wrapper(attributed, getattr(type(cap), meth).__get__(cap, type(cap)))
    attributed.__finrail_attributed__ = True
    return attributed


@contextlib.contextmanager
def as_owner(plugin: str):
    """For an owner's own declared operation, called by another plugin: the write is the
    owner's, because it happens in the owner's code, by its rules. This is the other
    legitimate way across (dispatching the owner's capability by name is the first), and
    it is what `core/agents` uses for the agents store. Outside a capability it changes
    nothing: nobody was being watched."""
    w = _WRITER.get()
    if w is None:
        yield
        return
    token = _WRITER.set((plugin, w[1]))
    try:
        yield
    finally:
        _WRITER.reset(token)


def _flush(rid: str, found: list) -> None:
    from . import tracker
    _quiet.on = True
    try:
        rec = tracker.load(rid)
        have = list(rec.get("crossed_stores") or [])
        for f in found:
            line = f"{f['writer']} wrote {f['owner']}'s store: {f['path']}"
            if line not in have:
                have.append(line)
        tracker.update(rid, crossed_stores=have[:50])
    except Exception:                                            # noqa: BLE001
        _logger.debug("could not record store crossings on %s", rid, exc_info=True)
    finally:
        _quiet.on = False


def _seen_write(path) -> None:
    w = _WRITER.get()
    if not w or getattr(_quiet, "on", False) or path is None:
        return
    writer, _rid = w
    if isinstance(path, int):
        return                          # a file descriptor names no path
    try:
        path = os.fsdecode(path)
    except (TypeError, ValueError):
        return
    _quiet.on = True                    # stores() reads settings; do not watch that
    try:
        owner = owner_of(path)
    finally:
        _quiet.on = False
    if not owner or owner == writer:
        return
    key = f"{writer} -> {owner}"
    with _lock:
        SEEN.setdefault(key, _resolve(path))
    found = _FOUND.get()
    if found is not None and len(found) < 50:
        found.append({"writer": writer, "owner": owner, "path": _resolve(path)})
    _quiet.on = True
    try:
        _logger.warning("layer rule 2: %s wrote into %s's store: %s", writer, owner, path)
    finally:
        _quiet.on = False


def _hook(event: str, args) -> None:
    # Cheapest test first: almost every audit event happens outside a capability.
    if _WRITER.get() is None:
        return
    try:
        if event == "open":
            path, mode, flags = (list(args) + [None, None, None])[:3]
            writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & _WRITE_FLAGS)
            if writes:
                _seen_write(path)
        elif event == "os.rename":                       # os.rename and os.replace
            _seen_write(args[0])
            _seen_write(args[1])
        elif event in ("os.remove", "os.mkdir", "os.rmdir", "os.truncate",
                       "shutil.rmtree", "os.symlink", "os.link"):
            _seen_write(args[0] if event not in ("os.symlink", "os.link") else args[1])
        elif event in ("shutil.copyfile", "shutil.copytree", "shutil.move"):
            _seen_write(args[1])
    except Exception:                                            # noqa: BLE001
        pass                                # an audit hook must never break the write


def install() -> None:
    """Once per process. An audit hook cannot be removed, so it is cheap when idle."""
    global _installed
    if _installed:
        return
    with _lock:
        if not _installed:
            sys.addaudithook(_hook)
            _installed = True
