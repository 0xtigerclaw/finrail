"""PLUGIN DEPLOY — what is running is a commit, and anything else is visible.

An installed plugin used to be whatever was sitting in its directory. The production plugins
were running code that existed in no commit anywhere: copied by hand from a worktree, one
plugin still from a repository nobody used any more, and the records plugin's type spec an
untracked file that nothing guaranteed stayed equal to its source. Every one of those was
invisible from inside finrail, because provenance recorded a commit and never what was
actually on disk.

So three things hold together here:

  manifest   every installed file, by sha256, recorded at install time beside the commit.
             "installed at 8f96392" now means these bytes and no others.
  drift      the directory re-hashed against its manifest: files changed, missing or
             extra. An edit made in place is reported by name, not discovered later as
             behaviour nobody can explain.
  deploy     `finrail upgrade` fetches each installed plugin's catalogue commit, runs that
             repository's tests against it, and only then swaps the directory — staged
             beside it and renamed into place, so a failure half-way leaves the old code
             running rather than a mixture.
  load       before any swap, by upgrade, plugin.update and plugin.install alike: every
             interpreter that will run the plugin loads the staged copy in a fresh
             process, and one that cannot refuses the swap (production, 6 Oct).

Deliberately not here: WHICH plugins are deployable. That is the catalogue's answer — a
plugin a remote catalogue lists is deployed from it; one only a local directory lists is
reported and left alone, because a working tree is not a commit.
"""
import hashlib
import pathlib
import shutil
import subprocess
import tempfile

from .errors import ContractViolation

#: Never part of what a plugin IS: generated on import, or version-control metadata.
IGNORED_DIRS = {"__pycache__", ".git", ".pytest_cache"}
IGNORED_SUFFIXES = (".pyc", ".pyo")

#: A repository's own suite, run against the fetched commit before anything is replaced.
TEST_TIMEOUT_S = 1800


def _ignored(rel: pathlib.PurePath) -> bool:
    return any(part in IGNORED_DIRS for part in rel.parts) or rel.name.endswith(
        IGNORED_SUFFIXES)


def manifest(root: pathlib.Path) -> dict:
    """{relative posix path: sha256} for every file a plugin directory holds."""
    root = pathlib.Path(root)
    out = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if _ignored(rel):
            continue
        out[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def drift(name: str = "") -> list:
    """Every installed plugin whose files no longer match what was installed.

    [{name, changed, missing, extra, unknown}] — `unknown` is a plugin with no manifest to
    compare against (installed before manifests existed, or never installed at all), which
    is a different fact from "modified" and is reported as one.
    """
    from . import registry
    root = registry.installed_root()
    prov = registry.provenance()
    out = []
    if not root.is_dir():
        return out
    names = [name] if name else sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and not p.name.startswith((".", "_")))
    for n in names:
        d = root / n
        if not d.is_dir():
            continue
        held = (prov.get(n) or {}).get("manifest")
        if not isinstance(held, dict):
            out.append({"name": n, "unknown": True, "changed": [], "missing": [],
                        "extra": []})
            continue
        now = manifest(d)
        changed = sorted(k for k in held if k in now and now[k] != held[k])
        missing = sorted(k for k in held if k not in now)
        extra = sorted(k for k in now if k not in held)
        if changed or missing or extra:
            out.append({"name": n, "unknown": False, "changed": changed,
                        "missing": missing, "extra": extra})
    return out


def modified(name: str = "") -> list:
    """Only the plugins edited in place — the ones that must block a deploy."""
    return [d for d in drift(name) if not d["unknown"]]


def describe(d: dict) -> str:
    if d["unknown"]:
        return f"{d['name']}: no install manifest — what is running has no recorded commit"
    parts = ([f"changed {', '.join(d['changed'])}"] if d["changed"] else []) \
        + ([f"missing {', '.join(d['missing'])}"] if d["missing"] else []) \
        + ([f"extra {', '.join(d['extra'])}"] if d["extra"] else [])
    return f"{d['name']}: modified in place — {'; '.join(parts)}"


# ── deploying ───────────────────────────────────────────────────────────────
def deployable() -> tuple:
    """(plans, skipped) for every installed plugin a REMOTE catalogue lists.

    plans   [{name, entry, source, held}] in catalogue order
    skipped [{name, why}] — installed, but not something upgrade can deploy from git
    """
    from . import catalogue, registry
    root = registry.installed_root()
    if not root.is_dir():
        return [], []
    here = sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith((".", "_"))
                  and (p / "__init__.py").is_file())
    by_source = {s["name"]: s for s in catalogue.declared()}
    listed = {}
    for entry in catalogue.available()["plugins"]:
        listed.setdefault(entry["name"], []).append(entry)
    prov = registry.provenance()
    plans, skipped = [], []
    for name in here:
        entries = listed.get(name) or []
        remote = [e for e in entries
                  if not catalogue.origin(e, by_source[e["catalogue"]])["local"]]
        if not remote:
            why = ("listed only by a local catalogue — a working tree is not a commit"
                   if entries else "no catalogue lists it")
            skipped.append({"name": name, "why": why})
            continue
        entry = remote[0]
        plans.append({"name": name, "entry": entry,
                      "source": by_source[entry["catalogue"]],
                      "held": prov.get(name) or {}})
    return plans, skipped


def stage(plans: list, workdir: pathlib.Path) -> dict:
    """Fetch every planned plugin into `workdir/<name>`. {name: fetched info}."""
    from . import catalogue
    got = {}
    for p in plans:
        got[p["name"]] = catalogue.fetch(p["entry"], p["source"],
                                         pathlib.Path(workdir) / p["name"])
    return got


def test_repositories(plans: list, python: str) -> list:
    """Run each distinct plugin repository's `tests/` against the commit being deployed.

    [{repo, ref, commit, ok, detail}]. The commit is cloned whole — a plugin's tests live
    beside it, not inside it — and the suite runs from that clone, so what is tested is
    the code that is about to be installed and nothing that happens to be on disk.
    """
    from . import catalogue
    seen, out = set(), []
    for p in plans:
        where = catalogue.origin(p["entry"], p["source"])
        key = (where["repo"], where["ref"])
        if key in seen:
            continue
        seen.add(key)
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="finrail-plugin-tests-"))
        try:
            clone = tmp / "repo"
            cmd = ["git", "clone", "--depth", "1", "--quiet"]
            if where["ref"]:
                cmd += ["--branch", where["ref"]]
            r = subprocess.run(cmd + [where["repo"], str(clone)], capture_output=True,
                               text=True, timeout=catalogue.FETCH_TIMEOUT)
            if r.returncode != 0:
                out.append({"repo": where["repo"], "ref": where["ref"], "commit": "",
                            "ok": False,
                            "detail": (r.stderr or r.stdout).strip()[-400:]})
                continue
            commit = catalogue._head(clone)
            if not (clone / "tests").is_dir():
                out.append({"repo": where["repo"], "ref": where["ref"], "commit": commit,
                            "ok": True, "detail": "no tests/ in the repository"})
                continue
            try:
                t = subprocess.run([python, "-m", "pytest", "tests", "-q",
                                    "-p", "no:cacheprovider"], cwd=str(clone),
                                   capture_output=True, text=True,
                                   timeout=TEST_TIMEOUT_S)
                ok, text = t.returncode == 0, (t.stdout or "") + (t.stderr or "")
            except subprocess.TimeoutExpired:
                ok, text = False, f"tests timed out after {TEST_TIMEOUT_S}s"
            out.append({"repo": where["repo"], "ref": where["ref"], "commit": commit,
                        "ok": ok, "detail": "\n".join(text.strip().splitlines()[-15:])})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


def swap(name: str, staged: pathlib.Path) -> None:
    """Replace one installed plugin with a staged copy, as close to atomically as a
    directory allows: the new copy is moved in beside the old, then renamed over it."""
    from . import registry
    root = registry.installed_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    incoming = root / f".{name}.incoming"
    outgoing = root / f".{name}.outgoing"
    for leftover in (incoming, outgoing):
        if leftover.exists():
            shutil.rmtree(leftover)
    shutil.copytree(staged, incoming)
    if target.exists():
        target.rename(outgoing)
    try:
        incoming.rename(target)
    except OSError:
        if outgoing.exists() and not target.exists():
            outgoing.rename(target)          # put the old code back before failing
        raise
    shutil.rmtree(outgoing, ignore_errors=True)


def changes(held: dict, staged_manifest: dict) -> list:
    """Which files a deploy changes, against what was recorded — or everything, when
    nothing was."""
    old = held.get("manifest") if isinstance(held.get("manifest"), dict) else None
    if old is None:
        return sorted(staged_manifest)
    return sorted({k for k in set(old) | set(staged_manifest)
                   if old.get(k) != staged_manifest.get(k)})


def refuse_if_modified() -> None:
    """Raise when any installed plugin was edited in place. Named, file by file."""
    bad = modified()
    if bad:
        raise ContractViolation(
            "installed plugins were modified in place, so deploying would silently "
            "overwrite those edits (or keep running them unrecorded):\n  "
            + "\n  ".join(describe(d) for d in bad)
            + "\n  commit the change to the plugin's repository, or put the installed "
              "copy back, then upgrade")


# ── can the finrail that runs it load it? ──────────────────────────────────
# Two outages on one day (production, 6 Oct) with one cause. `finrail upgrade` ran from
# the installed tool against the dev state, its plugin step fetched a production that builds
# `Scope(..., reads=...)`, the plugin's own suite passed against the tool's newer code,
# and the dev agents, which run ~/finrail-dev and had no `reads`, failed every turn with
# a TypeError for 2h20m. Two hours later `plugin.update` did the same thing from chat.
# Tests prove the plugin works with SOME finrail. What a deploy has to prove is that it
# loads in the one that will run it, so that is asked of each such interpreter, in a
# fresh process, before anything is swapped in.

#: Per interpreter. A load is an import and a few constructors, not a suite.
LOAD_CHECK_TIMEOUT_S = 120

#: The line the check's answer is on. Marked, because a plugin may print while it loads.
_LOAD_CHECK_MARK = "FINRAIL-PLUGIN-LOAD-CHECK "

#: Run by the TARGET interpreter, which may be older than this file. So it is a string
#: handed to `python -P -c` and uses nothing a plugin-loading finrail has ever lacked:
#: an entry point added here would not exist in the very finrail it has to test.
#: It imports each staged plugin as `finrail_plugins.<name>` from the staging directory,
#: constructs what `registry._absorb` constructs (every COMMANDS entry, the SERVICES
#: list), applies that finrail's own API check, and calls each capability's `scope()`,
#: because a scoped agent builds its Scope per turn, not at import: the 6 Oct TypeError
#: was raised there, after a load that looked clean. Only errors that mean "this plugin
#: and this finrail disagree about the code" (INCOMPAT) count from those calls; one
#: that means "this host is not configured" is the plugin's business at run time.
_LOAD_CHECK = r'''
import functools, importlib, json, pathlib, sys, traceback
MARK = sys.argv[1]
root, names = pathlib.Path(sys.argv[2]), json.loads(sys.argv[3])
INCOMPAT = (TypeError, AttributeError, ImportError, NameError, SyntaxError)
def say(d):
    sys.stdout.write("\n" + MARK + json.dumps(d) + "\n"); sys.stdout.flush()
try:
    import finrail, finrail_plugins
    base = finrail_plugins.refresh() if hasattr(finrail_plugins, "refresh") \
        else list(finrail_plugins.__path__)
    finrail_plugins.__path__ = [str(root)] + [p for p in base if p != str(root)]
    from finrail.plugin import Plugin
except BaseException as e:
    say({"fatal": f"{type(e).__name__}: {e}"}); sys.exit(0)
info = {"version": getattr(finrail, "__version__", "?"), "api": getattr(finrail, "API", None),
        "file": str(getattr(finrail, "__file__", ""))}
out = []
def where(e):
    tb = traceback.extract_tb(e.__traceback__)
    return f"{tb[-1].filename}:{tb[-1].lineno}" if tb else ""
def _fail(name, stage, e=None, text=""):
    out.append({"name": name, "stage": stage,
                "error": text or f"{type(e).__name__}: {e}"[:400],
                "at": where(e) if e is not None else ""})
# Every import before any declaration is called: a scope() reaches the registry, whose
# load points the namespace back at the installed roots, and a staged plugin imported
# after that would be looked for where it is not.
mods = {}
for name in names:
    finrail_plugins.__path__ = [str(root)] + [p for p in base if p != str(root)]
    try:
        mods[name] = importlib.import_module("finrail_plugins." + name)
    except BaseException as e:
        _fail(name, "import", e)
for name, mod in mods.items():
    fail = functools.partial(_fail, name)
    came = pathlib.Path(getattr(mod, "__file__", "") or "").resolve().parent
    if came != (root / name).resolve():
        fail("import", text=f"a plugin of the same name ships with this finrail "
                            f"({came}) and would be loaded instead"); continue
    try:
        from finrail.core import registry
        fault = registry.api_fault(mod, name) if hasattr(registry, "api_fault") else ""
    except BaseException as e:
        fault = f"{type(e).__name__}: {e}"
    if fault:
        fail("api", text=fault); continue
    want = getattr(mod, "REQUIRES_FEATURES", ()) or ()
    have = getattr(finrail, "FEATURES", frozenset())
    missing = sorted(str(f) for f in want if f not in have)
    if missing:
        fail("features", text="needs " + ", ".join(missing)
             + " and this finrail does not provide " + ("it" if len(missing) == 1 else "them"))
        continue
    caps, bad = [], False
    for item in getattr(mod, "COMMANDS", ()) or ():
        try:
            cap = item() if isinstance(item, type) else item
            if not isinstance(cap, Plugin):
                raise TypeError(f"{item!r} is not a Plugin")
            caps.append(cap)
        except BaseException as e:
            fail(f"constructing {getattr(item, 'name', '') or item!r}", e); bad = True
    if bad:
        continue
    svcs = getattr(mod, "SERVICES", ()) or ()
    if callable(svcs):
        try:
            svcs()
        except INCOMPAT as e:
            fail("SERVICES()", e); continue
        except BaseException:
            pass
    for cap in caps:
        fn = getattr(cap, "scope", None)
        if not callable(fn):
            continue
        try:
            fn()
        except INCOMPAT as e:
            fail(f"{cap.name}.scope()", e); break
        except BaseException:
            pass
say({"finrail": info, "faults": out})
'''


#: How `interpreters` names the process doing the install.
SELF = "this process"


def interpreters() -> list:
    """[(python, who)]: every interpreter that will run installed plugins from this state.

    This process, and the interpreter named by each installed service unit that runs
    finrail (`... -m finrail...`), read from the unit file (services.installed_command
    says why the file and not the spec). One per environment: a venv is its bin
    directory, so `python` and `python3` beside each other are the same answer, while two
    venvs over one system python are not, and resolving symlinks would merge them.
    """
    import os
    import sys
    from . import registry, services
    from .api import RUNTIME
    registry.load()
    found = [(sys.executable, SELF)]
    for label in sorted(RUNTIME.services):
        try:
            argv = services.installed_command(RUNTIME.services[label]) or []
        except Exception:                                           # noqa: BLE001
            continue
        if "-m" not in argv:
            continue
        i = argv.index("-m")
        if i + 1 >= len(argv) or not argv[i + 1].startswith("finrail"):
            continue
        j = i - 1
        while j >= 0 and argv[j].startswith("-"):
            j -= 1
        if j >= 0:
            found.append((argv[j], f"service {label}"))
    out, seen = [], set()
    for python, who in found:
        key = os.path.dirname(os.path.abspath(python))
        if key in seen or not os.path.isfile(python):
            continue
        seen.add(key)
        out.append((python, who))
    return out


def load_check(staged_root: pathlib.Path, names: list, python: str,
               who: str = "") -> dict:
    """Ask one interpreter to load the staged plugins. {finrail, faults} or {fatal}.

    For this process the child is pointed at the finrail this process imported, because
    that is the code this process runs, and an interpreter's default may be another tree
    (an editable install of a sibling worktree). For a service it is left to find its
    own, as the unit does, with nothing of this shell's PYTHONPATH."""
    import json
    import os
    import finrail
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    if who == SELF:
        env["PYTHONPATH"] = str(pathlib.Path(finrail.__file__).resolve().parent.parent)
    try:
        r = subprocess.run(
            [python, "-P", "-c", _LOAD_CHECK, _LOAD_CHECK_MARK, str(staged_root),
             json.dumps(list(names))],
            capture_output=True, text=True, timeout=LOAD_CHECK_TIMEOUT_S,
            # Never from the staging directory or wherever this was started: `-P` keeps
            # the folder off sys.path and cwd makes sure (serve._child_argv, 5 Oct).
            # No bytecode in the staging directory (env): it is copied into place as is.
            cwd=str(pathlib.Path(staged_root).parent), env=env)
    except subprocess.TimeoutExpired:
        return {"fatal": f"loading took longer than {LOAD_CHECK_TIMEOUT_S}s"}
    except OSError as e:
        return {"fatal": f"could not start it: {e}"}
    for line in reversed((r.stdout or "").splitlines()):
        if line.startswith(_LOAD_CHECK_MARK):
            try:
                return json.loads(line[len(_LOAD_CHECK_MARK):])
            except ValueError:
                break
    tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()[-3:]
    return {"fatal": "it exited without an answer"
            + (f": {' / '.join(tail)}" if tail else f" (exit {r.returncode})")}


def incompatible(staged_root: pathlib.Path, names: list) -> list:
    """Every way a staged plugin fails to load in an finrail that would run it.

    [{name, python, who, finrail, stage, error, at}], empty when all of them load. An
    interpreter that cannot answer at all is a refusal too: nothing was proven."""
    problems = []
    for python, who in interpreters():
        got = load_check(staged_root, names, python, who)
        if "fatal" in got:
            problems.append({"name": ", ".join(names), "python": python, "who": who,
                             "finrail": {}, "stage": "check",
                             "error": got["fatal"], "at": ""})
            continue
        for f in got.get("faults") or []:
            problems.append(dict(f, python=python, who=who,
                                 finrail=got.get("finrail") or {}))
    return problems


def describe_incompatible(problems: list) -> str:
    """The refusal, in words: which plugin, what failed, and which finrail it was."""
    lines = []
    for p in problems:
        ak = p.get("finrail") or {}
        which = (f"finrail {ak.get('version', '?')} (API {ak.get('api')}) at "
                 f"{pathlib.Path(ak['file']).parent.parent}" if ak.get("file")
                 else "the finrail")
        lines.append(f"{p['name']} cannot be loaded by {which}, which runs it for "
                     f"{p['who']} ({p['python']}):")
        lines.append(f"  {p['stage']}: {p['error']}" + (f"  [{p['at']}]" if p["at"] else ""))
    lines.append("That finrail is older or newer than the plugin needs, so the plugin was "
                 "NOT replaced and the installed copy keeps running. Bring that finrail "
                 "to the commit the plugin was written against, or point the catalogue at "
                 "a plugin commit it can load, then try again.")
    return "\n".join(lines)


def refuse_if_incompatible(staged_root: pathlib.Path, names: list) -> None:
    """Raise, naming the plugin and the error, unless every interpreter loads them all."""
    problems = incompatible(staged_root, names)
    if problems:
        raise ContractViolation(describe_incompatible(problems))
