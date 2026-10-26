"""CATALOGUES — where plugins are listed, before any of them is trusted.

A source is a git repository (or a local directory) holding `finrail-plugins.json`. That
is the whole contract. No server, no account, no uptime: a catalogue can be cloned,
forked, diffed and read offline, which is most of why every comparable system settled on
this shape — Homebrew taps, krew-index, asdf-plugins, Obsidian's community list.

Two rules that are structure rather than policy:

**The standard catalogue is a CONSTANT, not a setting.** A system whose default catalogue
is configurable has no default; it has a value that arrived from somewhere. Settings may
ADD sources, and may override the standard one for a fork or an air-gapped mirror — and
that override is reported loudly wherever sources are shown, because it is the difference
between "the standard plugins" and "plugins from wherever this points".

**Reading a catalogue never fetches.** A read capability that quietly clones a git repo is
a read with a network dependency and a disk write, and it makes "why is this slow" a
question about a listing. `refresh` fetches; everything else reads what was fetched, and
says when that was.
"""
import json
import pathlib
import re
import subprocess
import time

from .errors import ContractViolation
from .registry import settings

#: THE standard catalogue. Changing this is a supply-chain decision — it is where every
#: install without an explicit source comes from — and it is a constant so that the diff
#: shows it. Settings may ADD sources; `plugins.standard_source` replaces this one and is
#: reported loudly wherever sources are shown.
STANDARD_SOURCE = "https://example.com/finrail/finrail-plugins.git"
STANDARD_NAME = "standard"

MANIFEST = "finrail-plugins.json"
CACHE_DIR = "plugin-catalogues"
FETCH_TIMEOUT = 120

#: A plugin name is a python identifier and a directory name. Anything else is a way to
#: write somewhere it was not invited.
SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def cache_root() -> pathlib.Path:
    from .settings import state
    return state() / CACHE_DIR


def standard_source() -> tuple:
    """(url, overridden). The override is returned rather than hidden so every caller
    can say so — a silent override is the whole risk this arrangement has."""
    declared = str(settings().get("plugins.standard_source") or "").strip()
    return (declared, True) if declared else (STANDARD_SOURCE, False)


def declared() -> list:
    """Every configured source: the standard one first, then whatever was added."""
    url, overridden = standard_source()
    out = [{"name": STANDARD_NAME, "url": url, "standard": True,
            "overridden": overridden}]
    raw = settings().get("plugins.sources") or []
    if isinstance(raw, str):
        raw = [raw]
    for i, entry in enumerate(raw):
        if isinstance(entry, dict):
            name, url = entry.get("name") or f"source{i + 1}", entry.get("url") or ""
        else:
            name, url = _name_from_url(str(entry)), str(entry)
        out.append({"name": name, "url": url, "standard": False, "overridden": False})
    return out


def _name_from_url(url: str) -> str:
    stem = url.rstrip("/").rsplit("/", 1)[-1]
    stem = stem[:-4] if stem.endswith(".git") else stem
    return re.sub(r"[^a-z0-9_-]+", "-", stem.lower()) or "source"


def is_local(url: str) -> bool:
    return bool(url) and "://" not in url and not url.startswith("git@")


def location(source: dict) -> pathlib.Path:
    """Where this source's files are read from — its own directory when local, the
    clone when remote. A local path is read in place and never copied, so a catalogue
    under development is edited and read in one step."""
    url = source["url"]
    if is_local(url):
        return pathlib.Path(url).expanduser()
    return cache_root() / source["name"]


def fetched_at(source: dict) -> float:
    p = location(source) / MANIFEST
    return p.stat().st_mtime if p.is_file() else 0.0


def read(source: dict) -> dict:
    """The manifest, validated. Raises rather than returning something half-trusted.

    Everything in here came from somewhere else, so it is checked before it is believed:
    a name that is not a plain identifier, or a path that climbs out of the repository,
    is a way to write somewhere nobody invited it.
    """
    path = location(source) / MANIFEST
    if not path.is_file():
        raise ContractViolation(
            f"{source['name']}: no {MANIFEST} at {location(source)}"
            + ("" if is_local(source["url"]) else
               f" — it may never have been fetched (finrail do plugins.refresh)"))
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ContractViolation(f"{source['name']}: {MANIFEST} is not readable: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise ContractViolation(
            f"{source['name']}: {MANIFEST} must be an object with a 'plugins' list")
    seen, plugins = set(), []
    for entry in data["plugins"]:
        if not isinstance(entry, dict):
            raise ContractViolation(f"{source['name']}: a plugin entry is not an object")
        name = str(entry.get("name") or "")
        if not SAFE_NAME.match(name):
            raise ContractViolation(
                f"{source['name']}: {name!r} is not a usable plugin name — lowercase "
                f"letters, digits and underscores, starting with a letter")
        if name in seen:
            raise ContractViolation(
                f"{source['name']}: lists {name!r} twice; nothing here decides which")
        seen.add(name)
        rel = str(entry.get("path") or "")
        if rel and (pathlib.PurePosixPath(rel).is_absolute() or ".." in rel.split("/")):
            raise ContractViolation(
                f"{source['name']}/{name}: path {rel!r} leaves the repository")
        repo = str(entry.get("source") or "")
        if not rel and not repo:
            # At least one is required. Defaulting BOTH would make a typo'd entry
            # `{"name": "weather"}` mean "the catalogue repository itself is the weather
            # plugin", which installs the whole catalogue and is a poor way to find out.
            raise ContractViolation(
                f"{source['name']}/{name}: no source and no path — nothing says where "
                f"the code is")
        plugins.append({
            "name": name,
            "description": str(entry.get("description") or ""),
            # WHICH repository, and WHERE IN IT. Independent facts, and composable:
            #   path only    -> this catalogue's repository, at that path
            #   source only  -> that repository, at its root
            #   both         -> that repository, at that path
            # Terraform (`git::https://…//subdir`), Go modules and Nix flakes all address
            # code this way, and it is what lets a plugin live in a shared repository
            # today and move to its own later by changing one field.
            "source": repo,
            "path": rel,
            "ref": str(entry.get("ref") or ""),
            "requires": [str(x) for x in (entry.get("requires") or [])],
            "requires_api": entry.get("requires_api"),
            "catalogue": source["name"],
            "standard": source["standard"]})
    return {"name": str(data.get("name") or source["name"]),
            "description": str(data.get("description") or ""),
            "api": data.get("api"), "plugins": plugins}


def refresh(source: dict) -> dict:
    """Clone or update one remote catalogue. Local sources are read in place."""
    url = source["url"]
    if not url:
        return {"name": source["name"], "fetched": False,
                "detail": "no URL configured"}
    if is_local(url):
        return {"name": source["name"], "fetched": False,
                "detail": f"local path, read in place from {url}"}
    dest = location(source)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (dest / ".git").is_dir():
        cmd = ["git", "-C", str(dest), "pull", "--ff-only", "--quiet"]
    else:
        cmd = ["git", "clone", "--depth", "1", "--quiet", url, str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"name": source["name"], "fetched": False,
                "detail": f"git timed out after {FETCH_TIMEOUT}s"}
    if r.returncode != 0:
        return {"name": source["name"], "fetched": False,
                "detail": (r.stderr or r.stdout).strip()[-200:] or "git failed"}
    return {"name": source["name"], "fetched": True, "at": time.time(),
            "commit": _head(dest)}


def _head(repo: pathlib.Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:                                                # noqa: BLE001
        return ""


def available() -> dict:
    """Every plugin in every readable catalogue, and what is wrong with the rest.

    A source that cannot be read is REPORTED, never dropped. A short list beside a
    catalogue that silently failed reads as "this is everything there is", which is the
    same mistake the media search refuses for indexers.
    """
    from . import registry
    here = {p["name"]: p for p in registry.installed()}
    quarantined = {q["name"] for q in registry.quarantined()}
    rows, failed = [], []
    for source in declared():
        if not source["url"]:
            failed.append({"name": source["name"],
                           "why": "no standard catalogue is published yet"
                                  if source["standard"] else "no URL configured"})
            continue
        try:
            manifest = read(source)
        except ContractViolation as e:
            failed.append({"name": source["name"], "why": str(e)[:200]})
            continue
        for entry in manifest["plugins"]:
            installed_here = here.get(entry["name"])
            rows.append(dict(
                entry,
                installed=bool(installed_here),
                kind=(installed_here or {}).get("kind", ""),
                quarantined=entry["name"] in quarantined))
    return {"plugins": rows, "failed": failed, "sources": declared()}


def origin(entry: dict, source: dict) -> dict:
    """Where one plugin's code actually is: {repo, ref, subdir, same_repo}.

    Resolved in one place so the installer never has to reason about which of `source`
    and `path` was given. A plugin with no `source` lives in the catalogue's own
    repository, which is what lets the ten shipped plugins be catalogued from a separate
    repository before any of them is moved — the catalogue is genuinely just another
    source from the first day, while core and plugin changes stay in one commit.
    """
    repo = entry.get("source") or source["url"]
    return {"repo": repo,
            "ref": entry.get("ref") or "",
            "subdir": entry.get("path") or "",
            # Worth knowing at install time: fetching a subdirectory of a large shared
            # repository is a different (and slower) act than cloning a small dedicated
            # one, and the person approving it should see which they are getting.
            "same_repo": not entry.get("source"),
            "local": is_local(repo)}


def fetch(entry: dict, source: dict, dest: pathlib.Path) -> dict:
    """Put one plugin's code at `dest`. Returns what was actually fetched.

    The commit is RECORDED, not just the ref. A tag can be moved and a branch moves by
    definition, so "installed at v1.2.0" answers a different question from "installed
    this code" — and the second is the one that matters when something starts behaving
    differently and nothing was updated.

    Nothing is executed. A clone is a copy: no build step, no post-install hook, not ever.
    Code from here runs when a capability runs, behind the gate that already exists.
    """
    import shutil
    import subprocess
    import tempfile
    where = origin(entry, source)
    if not where["repo"]:
        raise ContractViolation(f"{entry['name']}: its catalogue has no repository URL")
    staging = pathlib.Path(tempfile.mkdtemp(prefix="finrail-plugin-"))
    try:
        root = staging / "repo"
        if where["local"]:
            src = pathlib.Path(where["repo"]).expanduser()
            if not src.is_dir():
                raise ContractViolation(f"{entry['name']}: {src} is not a directory")
            commit = _export_commit(entry["name"], src, where["subdir"], root)
        else:
            cmd = ["git", "clone", "--depth", "1", "--quiet"]
            if where["ref"]:
                cmd += ["--branch", where["ref"]]
            cmd += [where["repo"], str(root)]
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=FETCH_TIMEOUT)
            if r.returncode != 0:
                raise ContractViolation(
                    f"{entry['name']}: git could not fetch {where['repo']}"
                    + (f" at {where['ref']}" if where["ref"] else "")
                    + f": {(r.stderr or r.stdout).strip()[-200:]}")
            commit = _head(root)
        code = root / where["subdir"] if where["subdir"] else root
        if not (code / "__init__.py").is_file():
            raise ContractViolation(
                f"{entry['name']}: no __init__.py at "
                f"{where['subdir'] or '<repository root>'} — the catalogue points at "
                f"something that is not a python package")
        # Into place only after it is known to be a package. A half-written plugin
        # directory is a plugin that quarantines on the next start for a reason nobody
        # can act on.
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(code, dest, ignore=shutil.ignore_patterns(
            "__pycache__", ".git", "*.pyc"))
        from . import plugin_deploy
        return {"repo": where["repo"], "ref": where["ref"], "subdir": where["subdir"],
                "commit": commit, "local": where["local"],
                # WHAT was installed, not only which commit it came from. See
                # plugin_deploy: a commit names code, and this proves it is the code.
                "manifest": plugin_deploy.manifest(dest)}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _export_commit(name: str, src: pathlib.Path, subdir: str,
                   root: pathlib.Path) -> str:
    """Put HEAD of a local repository at `root` — the commit, never the working tree.

    A local catalogue used to be installed with a copy of the directory, uncommitted edits
    and all, while provenance recorded `HEAD`. The recorded commit then described code
    other than what ran: the production plugins went into production that way, and for a day
    nobody could say which commit, if any, was live. So a local source must be a git
    repository whose plugin directory is clean, and the files come from `git archive`,
    which cannot see anything that was not committed.
    """
    git = ["git", "-C", str(src)]
    top = subprocess.run(git + ["rev-parse", "--show-toplevel"], capture_output=True,
                         text=True, timeout=30)
    if top.returncode != 0:
        raise ContractViolation(
            f"{name}: {src} is not a git repository, so there is no commit to install — "
            f"a working directory is not a release. Commit it, or install from the "
            f"repository's remote.")
    where = subdir or "."
    dirty = subprocess.run(git + ["status", "--porcelain", "--", where],
                           capture_output=True, text=True, timeout=60)
    if dirty.stdout.strip():
        files = ", ".join(line[3:] for line in dirty.stdout.splitlines()[:6])
        raise ContractViolation(
            f"{name}: {src}/{subdir} has uncommitted changes ({files}) — installing would "
            f"put code into production that no commit holds. Commit or discard them first.")
    root.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(git + ["archive", "--format=tar", "HEAD"],
                             capture_output=True, timeout=FETCH_TIMEOUT)
    if archive.returncode != 0:
        raise ContractViolation(
            f"{name}: git archive failed in {src}: "
            f"{archive.stderr.decode(errors='replace').strip()[-200:]}")
    untar = subprocess.run(["tar", "-x", "-C", str(root)], input=archive.stdout,
                           capture_output=True, timeout=FETCH_TIMEOUT)
    if untar.returncode != 0:
        raise ContractViolation(
            f"{name}: could not unpack the commit: "
            f"{untar.stderr.decode(errors='replace').strip()[-200:]}")
    return _head(src)


def resolve(names: list) -> tuple:
    """(entries in install order, missing names).

    `requires` is followed transitively so one approval covers the whole set — being
    asked five times for one decision is how people stop reading the question.
    """
    index = {p["name"]: p for p in available()["plugins"]}
    order, seen, missing = [], set(), []

    def walk(name, chain):
        if name in seen:
            return
        entry = index.get(name)
        if entry is None:
            missing.append(name if not chain else f"{name} (needed by {chain[-1]})")
            return
        seen.add(name)
        for dep in entry["requires"]:
            if dep in chain:
                raise ContractViolation(
                    f"{' -> '.join(chain + [dep])} is a dependency cycle")
            walk(dep, chain + [name])
        order.append(entry)                     # dependencies first

    for n in names:
        walk(n, [])
    return (order, missing)
