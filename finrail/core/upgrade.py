"""UPGRADE — everything that has to happen for new code to actually be running.

The loop was five commands in a document, and the document was right, and it still went
wrong every time — because the five are not independent and the failure of any one of them
is silent until something else breaks hours later.

WHAT MADE THIS A COMMAND. `uv tool install --force` REPLACES the installed package
directory, and the websocket harness keeps its one node dependency inside that directory.
So every upgrade deleted `node_modules` from under four running agents. Nothing failed at
the time: the processes already had the module loaded and carried on answering. The next
restart — a reboot, a redeploy, a crash — is when they stopped, hours or days later, with
an import error naming a path nobody had touched. The fix for that is not a longer
document. It is `provision` running immediately after the install, in the same breath,
because the step that breaks the dependency and the step that repairs it belong together.

The order is the argument:

    state       FIRST: the installed units name the state the live agents run on, and a
                shell on another state cannot see those agents to restart them
    pull        nothing below means anything if this did not move
    sync        the test venv, so the tests that follow are the new code's tests
    test        BEFORE anything is installed or restarted. The whole point of running
                them here is to not deploy what fails
    intact      BEFORE the pull: an installed plugin edited in place is either a hot-fix
                about to be overwritten or code that runs with no commit behind it, and
                neither may be deployed over silently
    install     the venv the CLI and the services actually execute from
    provision   IMMEDIATELY after, because install is what broke the dependencies.
                It sat after `plugins` for a while, and the promise above was broken
                for every host with a deployable plugin: a plugins step that failed
                (its suite red, 2026-10-13 23:33) stopped the upgrade with the venv
                freshly rebuilt and PyYAML, requests and the rest not yet put back —
                production could not load a plugin until somebody ran `finrail
                provision` by hand. Twice in one week, the second time by a killed
                process. The step that destroys and the step that repairs are adjacent
                now, and nothing can be scheduled between them
    plugins     each installed plugin a remote catalogue lists: its repository's tests run
                against the commit, every finrail that will run it loads it, then the
                directory is swapped and the files recorded. Plugins live outside this
                checkout, so pulling it never updated them. A deployed plugin may declare
                a requirement the old one did not, so the requirements are provisioned
                once more after a swap, inside this step
    services    last, so they come up on repaired dependencies and tested code

An upgrade whose process dies between install and provision leaves the same hole. The
next `finrail upgrade` closes it (install, then provision, again), and so does a plain
`finrail provision` — `finrail doctor` names what is missing.

Stops at the first failure. A pull that conflicts makes every later step a lie, and
"upgraded" over a repo that did not move is exactly the report that had people debugging
running code they had never actually deployed.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

from . import plugin_deploy as _plugins
from . import provision as _provision
from . import services as _services
from .api import RUNTIME
from .errors import ContractViolation
from .settings import raw_location, state

#: Long enough for a cold `uv sync` and a full suite on a laptop.
STEP_TIMEOUT_S = 1800


def _run(argv: list, cwd=None, timeout: int = STEP_TIMEOUT_S, env=None) -> tuple:
    """(ok, output). Never raises: a step that cannot run is a step that failed."""
    try:
        p = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return (False, f"{type(e).__name__}: {e}")
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    return (p.returncode == 0, out)



def _checkout_at(parent: pathlib.Path):
    """`parent` if it is the checkout — resolving a WORKTREE to the one it belongs to.

    A git worktree's `.git` is a FILE naming the real gitdir, and `.exists()` is true for
    a file, so a worktree used to answer "yes, I am the checkout". It is not: it is a
    second working tree of the same checkout, and it has no `.venv`, which is where this
    surfaced. `repo.land` rebases onto origin and runs the suite IN A WORKTREE, and the
    test that asserts a venv-less repository can still name its interpreter derives that
    interpreter from here — so it got the worktree, found no venv, and failed. Every
    landing was blocked by it, whatever the branch contained.

    The same mistake would have had `finrail upgrade` deploy from a throwaway worktree
    rather than from the checkout the install was built out of, which is worse and quiet.
    """
    if not (parent / "pyproject.toml").is_file():
        return None
    git = parent / ".git"
    if git.is_dir():
        return parent
    if not git.is_file():
        return None
    try:
        line = git.read_text().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = pathlib.Path(line.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (parent / gitdir).resolve()
    # `commondir` is git's own answer to "which gitdir do I share?" — `../..` from
    # <main>/.git/worktrees/<name>, i.e. <main>/.git. Read rather than assumed, because
    # `--separate-git-dir` puts it somewhere the path shape would not predict.
    common = gitdir / "commondir"
    if common.is_file():
        try:
            target = pathlib.Path(common.read_text().strip())
        except OSError:
            target = None
        if target is not None:
            if not target.is_absolute():
                target = (gitdir / target).resolve()
            root = target.parent if target.name == ".git" else target
            if (root / "pyproject.toml").is_file():
                return root
    for anc in gitdir.parents:
        if anc.name == ".git" and (anc.parent / "pyproject.toml").is_file():
            return anc.parent
    return None

def repo() -> pathlib.Path:
    """The checkout this install came from.

    Derived before it is configured: an editable install imports straight out of the
    working tree, so the answer is usually sitting in `__file__`. A COPIED install has no
    way to know, and guessing at one would upgrade a repository nobody named — so that
    case is a refusal that names the setting.
    """
    configured = raw_location("upgrade.repo")
    if configured:
        p = pathlib.Path(str(configured)).expanduser()
        if not (p / ".git").exists():
            raise ContractViolation(
                f"upgrade.repo is {p}, which is not a git checkout")
        return p
    # THE TREE THIS IS MADE OF, which the system already knows. `settings.source()` is
    # exactly "finrail's own source tree" and carries the `source.dir` override for
    # precisely the case where the install is a copy. Asking for a SECOND setting that
    # means the same thing is how a deploy comes to need hand-holding on every new host —
    # and the first one always fails, because the setting it demands can only be set
    # after somebody has read the refusal.
    for candidate in (_settings_source(), pathlib.Path(__file__).resolve()):
        if candidate is None:
            continue
        for parent in (candidate, *candidate.parents):
            found = _checkout_at(parent)
            if found is not None:
                return found
    raise ContractViolation(
        "there is no checkout to upgrade from: this finrail is a copy and nothing says "
        "where it was built from. Either is enough:\n"
        "  finrail settings set source.dir=/path/to/finrail     (the source tree)\n"
        "  finrail settings set upgrade.repo=/path/to/finrail   (this command only)")


def _settings_source():
    """Where the system already thinks its own source tree is, or None."""
    try:
        from .settings import source
        return pathlib.Path(source()).expanduser().resolve()
    except Exception:                                              # noqa: BLE001
        return None


def as_uv_tool() -> bool:
    """Whether the CLI runs from a uv tool install rather than a checkout's venv.

    This is the difference between `uv tool install` and `uv sync`, and doing the wrong
    one installs the new code somewhere nothing executes from — an upgrade that reports
    success and changes nothing, which is the failure this module exists to stop.
    """
    return "/uv/tools/" in str(pathlib.Path(sys.prefix).resolve()).replace(os.sep, "/")


def test_python(where: pathlib.Path) -> str:
    """The interpreter the suite runs under: the checkout's venv when it has one."""
    venv = where / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else sys.executable


def in_flight() -> list:
    """The detached runs still working, other than the one asking.

    AN UPGRADE REPLACES THE ENVIRONMENT UNDER THEM. `uv tool install --force`
    rebuilds the tool's venv, and until `provision` has run there is no
    `requests`, no Google client, no YAML parser. On 2026-10-12 an upgrade
    landed under a search fund's record build: its first step lost its model
    call, the eleven after it could not search the deal room and reported it
    empty, and the build ended `done` with nothing written.

    Only BACKGROUND runs are counted. A conversation in progress is also cut
    short by the restart that ends an upgrade, and always was; that is a turn
    somebody can ask again. A detached job is forty minutes of work nobody is
    watching.
    """
    from . import tracker
    me, out = tracker.current(), []
    for r in tracker.open_runs():
        if r.get("status") != tracker.RUNNING or not r.get("background"):
            continue
        if r.get("id") == me or r.get("capability") == "upgrade":
            continue
        if tracker.pid_alive(tracker.holder_pid(r)):
            out.append(f"{r.get('id')} {r.get('capability')}")
    return out


def layer_runs(where) -> list:
    """The layers this checkout can test alone (docs/LAYERS.md, "how it is enforced").

    Read from `tests/layers_floor.json`, which names each layer run and its floor. A
    checkout without the file predates the layer contract and is upgraded as before. A
    file that does not parse still runs both, and the suite's own conftest says why."""
    f = pathlib.Path(where) / "tests" / "layers_floor.json"
    if not f.is_file():
        return []
    try:
        named = json.loads(f.read_text())
    except ValueError:
        return list(LAYERS)
    return [layer for layer in LAYERS if layer in named]


#: The layer runs, in order: a core failure is the one to read first.
LAYERS = ("core", "bundled")


def in_flight() -> list:
    """The detached runs still working, other than the one asking.

    AN UPGRADE REPLACES THE ENVIRONMENT UNDER THEM. `uv tool install --force`
    rebuilds the tool's venv, and until `provision` has run there is no
    `requests`, no Google client, no YAML parser. On 2026-10-12 an upgrade
    landed under a search fund's record build: its first step lost its model
    call, the eleven after it could not search the deal room and reported it
    empty, and the build ended `done` with nothing written.

    Only BACKGROUND runs are counted. A conversation in progress is also cut
    short by the restart that ends an upgrade, and always was; that is a turn
    somebody can ask again. A detached job is forty minutes of work nobody is
    watching.
    """
    from . import tracker
    me, out = tracker.current(), []
    for r in tracker.open_runs():
        if r.get("status") != tracker.RUNNING or not r.get("background"):
            continue
        if r.get("id") == me or r.get("capability") == "upgrade":
            continue
        if tracker.pid_alive(tracker.holder_pid(r)):
            out.append(f"{r.get('id')} {r.get('capability')}")
    return out


#: How long an upgrade waits for detached work to finish before it gives up, in minutes,
#: and how often it looks. It WAITS rather than refusing at once: on 14 Oct an agent asked
#: for an upgrade while another agent's record build ran, the idle check refused, and the
#: agent asked again and again until a person told it to stop. The upgrade was right to
#: hold; it was wrong to make somebody else do the holding.
IDLE_WAIT_MIN = 30
IDLE_POLL_S = 20

PLUGINS_INSTEAD = ("a change that is only in a plugin can go now with `finrail do "
                   "plugin.update name=<plugin>`, which swaps the plugin and leaves the "
                   "environment alone")


def wait_for_idle(minutes: float, poll_s: float = IDLE_POLL_S, sleep=None, clock=None) -> list:
    """Wait up to `minutes` for `in_flight()` to empty; return what is still running."""
    import time
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + max(0.0, float(minutes)) * 60
    busy = in_flight()
    while busy and clock() < deadline:
        try:
            from . import progress
            progress.emit("engine", f"waiting for detached work before installing: "
                                    f"{'; '.join(busy[:3])}")
        except Exception:                                          # noqa: BLE001
            pass
        sleep(poll_s)
        busy = in_flight()
    return busy


def installed_package() -> pathlib.Path:
    """The package directory this process runs from, which the install replaces."""
    return pathlib.Path(__file__).resolve().parents[1]


def node_dirs(pkg: pathlib.Path) -> list:
    """Every directory in the installed package with a `package.json` AND its modules.

    Asked BEFORE the install, of the disk and nothing else. The chat plugin declares its
    node package only when it can see a deployed agent, and an upgrade run from a login
    shell without the units' FINRAIL_STATE sees none — so on kubernetes01 on 18 Oct
    2026 provision had nothing to put back, said "everything already in place", and four
    agents crash-looped on `nostr-tools` at the next restart. Whether the modules were
    there is a fact about this directory, not about which shell is asking.
    """
    if not pkg.is_dir():
        return []
    return sorted(d.parent for d in pkg.rglob("package.json")
                  if "node_modules" not in d.relative_to(pkg).parts
                  and (d.parent / "node_modules").is_dir())


def restore_node(had: list) -> tuple:
    """(ok, notes): `npm install` wherever the install took away modules that were there."""
    notes, ok = [], True
    for d in had:
        if (d / "node_modules").is_dir():
            continue
        good, out = _run(["npm", "install", "--prefix", str(d)], timeout=600)
        if good and (d / "node_modules").is_dir():
            notes.append(f"restored {d / 'node_modules'}")
        else:
            ok = False
            notes.append(f"FAILED to restore {d / 'node_modules'}: "
                         f"{(out or 'npm ran but it is still missing')[-400:]}")
    return ok, notes


def unit_states() -> dict:
    """{unit file name: the FINRAIL_STATE it carries}, for every installed finrail unit.

    Every unit this system writes bakes the state in (`service_env`, the settings plugin's
    `ENV`), because the state is the one location nothing else can tell a process. So the
    unit files are the authority on where the LIVE system keeps its state, and the shell
    running the upgrade is not.

    ONLY THIS INSTALL'S UNITS: one whose ExecStart runs another environment's python is
    another install sharing the host (the dev agents on kubernetes01 run from
    ~/finrail-dev/.venv on their own state). This upgrade neither installs nor restarts
    those, and counting them refused every live upgrade on that host (19 Oct).
    """
    out = {}
    if _services.platform() == "systemd":
        from . import services_systemd
        for p in sorted(services_systemd.units_dir().glob("finrail*.service")):
            try:
                text = p.read_text()
            except OSError:
                continue
            lines = [ln.strip() for ln in text.splitlines()]
            if not any(_ours(ln[len("ExecStart="):]) for ln in lines
                       if ln.startswith("ExecStart=")):
                continue
            for line in lines:
                if line.startswith("Environment=") and "FINRAIL_STATE=" in line:
                    out[p.stem] = line.split("FINRAIL_STATE=", 1)[1].rstrip('"')
    else:
        import plistlib
        for p in sorted(_services.agents_dir().glob("*.plist")):
            try:
                env = plistlib.loads(p.read_bytes()).get("EnvironmentVariables") or {}
            except Exception:                                      # noqa: BLE001
                continue
            if env.get("FINRAIL_STATE"):
                out[p.stem] = str(env["FINRAIL_STATE"])
    return out


def _ours(exec_line: str) -> bool:
    """Does this ExecStart run in this process's own environment? Compared as written,
    not resolved: every venv's python is a link to the same interpreter."""
    import shlex
    try:
        argv = shlex.split(exec_line)
    except ValueError:
        return True                                  # unreadable: counted, never skipped
    if not argv:
        return True
    prog = pathlib.Path(argv[0])
    return any(prog.parent == pathlib.Path(env) / "bin"
               for env in {sys.prefix, os.path.dirname(os.path.dirname(sys.executable))})


def _same(a, b) -> bool:
    return pathlib.Path(str(a)).expanduser().resolve() == \
        pathlib.Path(str(b)).expanduser().resolve()


def state_mismatch() -> str:
    """Why this shell must not run the upgrade, or "" when it may.

    THE 18 OCT OUTAGE, second half. Agent definitions live beside the state
    (`agents.directory()`), so a shell without the units' FINRAIL_STATE looks for agents
    in the default place, finds none, and RUNTIME.services has no chat specs in it. The
    upgrade then restarted the cron unit, skipped every agent, and reported success — the
    agents kept running the old code with the new code installed under them. Worse, the
    restart REWRITES each unit it touches from this shell's state, so a unit it did reach
    would have been pointed at the wrong ledger.

    Refused rather than adopted. Quietly switching to the units' state would load other
    settings and other plugins than the ones the person asked about, behind their back;
    one line naming the state and the command is cheaper than that surprise.
    """
    units = unit_states()
    here = state()
    away = {u: s for u, s in units.items() if not _same(s, here)}
    if not away:
        return ""
    distinct = sorted({str(s) for s in units.values()})
    listing = "\n".join(f"  {u}: FINRAIL_STATE={s}" for u, s in sorted(units.items()))
    if len(distinct) > 1:
        return (f"the installed finrail units do not agree on a state, and this shell "
                f"uses {here}:\n{listing}\n"
                f"an upgrade would restart some of them onto the wrong one. Reinstall the "
                f"odd ones out so every unit names the same state, then upgrade.")
    return (f"the installed finrail units run on {distinct[0]}, but this shell uses "
            f"{here}:\n{listing}\n"
            f"from here the upgrade cannot see those agents and would not restart them. "
            f"Run it with their state:\n"
            f"  FINRAIL_STATE={distinct[0]} finrail upgrade")


def plan(test: bool = True, restart: bool = True, wait: float = IDLE_WAIT_MIN) -> dict:
    wrong = state_mismatch()
    if wrong:
        raise ContractViolation(wrong)
    where = repo()
    uv = shutil.which("uv")
    modified = [_plugins.describe(d) for d in _plugins.modified()]
    dirty = dirty_files(where)
    busy = in_flight()
    steps = [{"name": "idle",
              "what": "no detached run is in flight: an install replaces the environment "
                      "under it"
              + (f". WAITS up to {wait:g} min for: {'; '.join(busy[:5])} ({PLUGINS_INSTEAD})"
                 if busy else "")},
             {"name": "intact",
              "what": "nothing was edited in place — not an installed plugin, not the "
                      "checkout this deploys from"
              + (f" — REFUSES NOW: {'; '.join(modified + dirty[:5])}"
                 if (modified or dirty) else "")},
             {"name": "pull", "what": f"git -C {where} pull --ff-only"}]
    if uv:
        steps.append({"name": "sync", "what": "uv sync --all-extras (the test venv)"})
    if test:
        steps.append({"name": "test", "what": f"{test_python(where)} -m pytest tests -q"})
        if layer_runs(where):
            steps.append({"name": "layers",
                          "what": "the same suite with each layer alone: "
                          + ", ".join(f"FINRAIL_LAYER={x}" for x in layer_runs(where))})
    if uv and as_uv_tool():
        steps.append({"name": "install",
                      "what": f"uv tool install --force {where}  (a COPY of the commit, "
                              f"not a pointer at the checkout)"})
    steps.append({"name": "provision",
                  "what": "put back node_modules the install deletes, then install "
                          "declared requirements"})
    deploy, skipped = _plugins.deployable()
    if deploy:
        steps.append({"name": "plugins", "what": "test, load in each finrail that runs them, then "
                      "deploy from their catalogues: "
                      + ", ".join(f"{d['name']} @{(d['held'].get('commit') or '?')[:8]}"
                                  for d in deploy)
                      + "; then provision again, for a requirement a new plugin declares"})
    services = sorted(RUNTIME.services)
    if restart and services:
        steps.append({"name": "services",
                      "what": f"restart {len(services)}: {', '.join(services)}"})
    return {"repo": str(where), "uv_tool": as_uv_tool(), "steps": steps,
            "services": services, "test": test, "restart": restart,
            "plugins": [d["name"] for d in deploy],
            "plugins_skipped": skipped, "modified": modified, "wait": wait}


def render(p: dict) -> str:
    lines = [f"UPGRADE {p['repo']}",
             f"  installed as: {'uv tool' if p['uv_tool'] else 'a checkout venv'}"]
    lines += [f"  {i + 1}. {s['what']}" for i, s in enumerate(p["steps"])]
    for sk in p.get("plugins_skipped") or []:
        lines.append(f"     not deployed: {sk['name']} — {sk['why']}")
    if not p["test"]:
        lines.append("  (tests skipped — nothing will check the code before it is installed)")
    return "\n".join(lines)


def dirty_files(where: pathlib.Path) -> list:
    """Tracked files with uncommitted changes, plus untracked ones.

    Asked of the DEPLOY's own repository, because that directory is what the install is
    built from. Plugins ask the same question of the plugin's directory before fetching a
    commit out of it; this is the same question one level up.
    """
    ok, out = _run(["git", "-C", str(where), "status", "--porcelain"], timeout=60)
    if not ok:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _head(where: pathlib.Path) -> str:
    ok, out = _run(["git", "-C", str(where), "rev-parse", "--short", "HEAD"], timeout=30)
    return out.strip() if ok else "?"


def run(test: bool = True, restart: bool = True, wait: float = IDLE_WAIT_MIN) -> dict:
    """Do it, in order, stopping at the first failure."""
    where = repo()
    done, before = [], _head(where)

    def record(name: str, ok: bool, detail: str, skipped: bool = False) -> bool:
        done.append({"step": name, "ok": ok, "skipped": skipped,
                     "detail": (detail or "")[-1200:]})
        return ok

    wrong = state_mismatch()
    if wrong:
        record("state", False, wrong)
        return _result(done, before, before, where)

    busy = wait_for_idle(wait)
    if not record("idle", not busy,
                  (f"after {wait:g} min, detached work is still running, and installing "
                   "would replace the environment under it: " + "; ".join(busy[:10])
                   + f". Nothing was changed. Run the upgrade again when it is done "
                   f"(`finrail view <id>`); {PLUGINS_INSTEAD}")
                  if busy else "no detached run is in flight"):
        return _result(done, before, before, where)

    bad = _plugins.modified()
    unclean = dirty_files(where)
    if unclean:
        # What gets installed below is built from this directory. Uncommitted work in it
        # is somebody's half-finished change, and deploying it makes them the author of
        # an outage they did not know they had shipped.
        record("intact", False,
               "the checkout has uncommitted changes, and what would be installed is "
               "built from it:\n  " + "\n  ".join(unclean[:20])
               + "\ncommit them on a branch and land it, or stash them — a deploy is a "
                 "commit, never a working tree")
        return _result(done, before, before, where)
    if not record("intact", not bad,
                  "; ".join(_plugins.describe(d) for d in bad)
                  or "every installed plugin matches its install manifest"):
        return _result(done, before, before, where)

    ok, out = _run(["git", "-C", str(where), "pull", "--ff-only"])
    if not record("pull", ok, out):
        return _result(done, before, before, where)
    after = _head(where)

    uv = shutil.which("uv")
    if uv:
        ok, out = _run([uv, "sync", "--all-extras"], cwd=where)
        if not record("sync", ok, out):
            return _result(done, before, after, where)
    else:
        record("sync", True, "uv is not on PATH — skipped", skipped=True)

    if test:
        ok, out = _run([test_python(where), "-m", "pytest", "tests", "-q"], cwd=where)
        # The last lines are the summary; the rest is a wall of dots.
        if not record("test", ok, "\n".join(out.splitlines()[-25:])):
            return _result(done, before, after, where)
        # EACH LAYER ALONE, before anything is installed. The full suite passing says
        # nothing about a core test that quietly needs a bundled plugin: it passes with
        # the plugin loaded and fails the day the plugin is off. The layer runs are what
        # catch that, and a promotion is when it matters (docs/LAYERS.md).
        layers = layer_runs(where)
        if layers:
            notes, all_ok = [], True
            for layer in layers:
                ok, out = _run([test_python(where), "-m", "pytest", "tests", "-q"],
                               cwd=where, env=dict(os.environ, FINRAIL_LAYER=layer))
                lines = out.splitlines()
                notes.append(f"{layer}: {lines[-1] if lines else 'no output'}")
                if not ok:
                    all_ok = False
                    notes += lines[-15:]
                    break
            if not record("layers", all_ok, "\n".join(notes)):
                return _result(done, before, after, where)
    else:
        record("test", True, "asked not to", skipped=True)

    had = node_dirs(installed_package()) if uv and as_uv_tool() else []
    if uv and as_uv_tool():
        # NO `--editable`. That installs a POINTER at the working tree, so the live
        # system was never a commit — it was "whatever that directory says, from now
        # on". With six agents editing six worktrees of one checkout, every uncommitted
        # line any of them wrote was live for every agent on the box the moment it was
        # saved, and a deploy from a dirty checkout shipped somebody's half-finished
        # work. Plugins already install a COMMIT and refuse a tree edited in place
        # (`catalogue.fetch`); this is core joining them.
        ok, out = _run([uv, "tool", "install", "--force", str(where)])
        if not record("install", ok, out):
            return _result(done, before, after, where)
    else:
        record("install", True,
               "the CLI runs from the checkout's venv, which sync already refreshed",
               skipped=True)

    # IMMEDIATELY AFTER INSTALL. See the module docstring: the install is what breaks
    # these, and a gap between the two is measured in days of an agent that will not start.
    # ONLY WHAT THE INSTALL DESTROYED. `uv tool install --force` replaces the package
    # directory: the libraries in that venv and the node modules under it go with it.
    # A config file, a service, a credential and a directory on the host do not, and an
    # upgrade has no business rewriting them — this called plain `provision()`, which
    # repairs everything declared, and a step whose blast radius should have been one
    # directory was given the run of the machine. It rewrote a mail proxy's config from a
    # keychain it could not read, and IMAP stopped on both accounts.
    #
    # NODE MODULES FIRST, and from the disk, not from what any plugin declares: see
    # `node_dirs`. This half IS a gate, unlike the rest. Restarting an agent onto a module
    # the install just deleted is not a standing gap, it is the outage.
    node_ok, node_notes = restore_node(had)
    if not node_ok:
        record("provision", False, "; ".join(node_notes))
        return _result(done, before, after, where)
    prov = _provision.provision(install_only=True)
    detail = "; ".join(node_notes + [_provisioned(prov)])
    # REPORTED, NEVER FATAL — and this is the one rung of the ladder that is not a gate.
    #
    # It was a gate, and the first real upgrade proved it wrong. `provision` repaired the
    # five libraries the install had just removed, exactly as intended, and then returned
    # ok=False because of two things that had nothing to do with this upgrade: a `docker`
    # requirement whose repair is `brew install docker` on a host with no brew, and three
    # OVH keys nobody has ever set here. Both were true before the upgrade and will be
    # true after it. Treating them as a reason not to restart meant a standing gap
    # anywhere on the machine could block every future deploy — and it did, on the first
    # one, leaving the services on the old code with the new code installed under them.
    #
    # What actually needs proving is that the services come back, and the step below
    # proves that per service by asking whether each one is RUNNING. So provision says
    # what it repaired and what still wants a person, and the restart is the gate.
    record("provision", True, detail, skipped=not prov.get("ok"))
    plugin_files = []
    if not deploy_plugins(record, test=test, touched=plugin_files):
        return _result(done, before, after, where)
    if plugin_files:
        # A PLUGIN JUST DEPLOYED MAY DECLARE A REQUIREMENT THE OLD ONE DID NOT. The
        # provision above ran against the plugins as they were; once more, for what
        # the swap brought in — said on this step, since it is this step's doing.
        again = _provision.provision(install_only=True)
        done[-1]["detail"] = (done[-1]["detail"] + "\nrequirements after the swap: "
                              + _provisioned(again))[-1200:]


    if restart:
        needed, why = restart_needed(changed_paths(where, before, after), plugin_files)
        if not needed:
            record("services", True, why, skipped=True)
            return _result(done, before, after, where)
        notes, failed = [], []
        mine = _services.own_unit()
        # The service carrying the conversation that asked for this goes LAST, and is
        # deferred: see `services.restart_detached`. Everything else is back, and this run
        # has written its result, before the process running it is replaced.
        for label in sorted(RUNTIME.services,
                            key=lambda l: (_services.unit_of(RUNTIME.services[l]) == mine
                                           and bool(mine), l)):
            spec = RUNTIME.services[label]
            if mine and _services.unit_of(spec) == mine:
                st = _services.restart_detached(spec)
                notes.append(f"{label}: {st.get('detail') or 'deferred'}") \
                    if st.get("deferred") else failed.append(
                        f"{label}: {st.get('error') or 'could not be scheduled'}")
                continue
            try:
                # restart(), NOT install(). install() rewrites the unit and enables it,
                # and using it here re-enabled every unit an operator had switched off
                # by hand: one agent answered on two harnesses. It also made every deploy
                # on a system-scoped host need root over /etc/systemd/system. A unit is
                # written by the plugin that deploys it (docs/LAYERS.md, rule 2); an
                # upgrade restarts what is installed and says when a unit is stale or not
                # there. A service left running the old code is still an upgrade that did
                # not happen, so a unit that does not come back still fails the step.
                # Privileged only where needed: the restart is `sudo -n systemctl restart`,
                # so an unattended deploy needs one narrow rule, not root (main, 865fccf).
                try:
                    was = _services.status(spec)
                except Exception:                                  # noqa: BLE001
                    was = None             # not known: nothing is excused on a guess
                st = _services.restart(spec)
                # `loaded`, NOT `running` — because `running` is a key `install` has
                # never returned. Both backends return {service, loaded, detail, …};
                # only `status` reports `running`. So `st.get("running")` was None for
                # every service on every upgrade, and each one was recorded as "did NOT
                # come back" whether it came back or not — the gate failed closed on a
                # truth it never asked for, and the restart it was guarding had in fact
                # worked every time.
                #
                # `loaded` is also the RIGHT question. It is `test(spec)`'s verdict, read
                # after the settling pause, and it already knows that an interval job
                # idle between ticks is healthy — which `running` does not:
                # `com.finrail.cron` is legitimately pid-less most of the time.
                ok = bool(st.get("loaded"))
                if not ok and was is not None and was.get("loaded") is False:
                    # SWITCHED OFF BEFORE THIS UPGRADE, AND LEFT THAT WAY. A unit
                    # somebody disabled is a decision, not a casualty: the restart
                    # did not disable it and is not here to enable it. Reported as
                    # a failure, it made every upgrade on this host end "stopped at
                    # services" with every service back — five in a row on
                    # 2026-10-12 — which is how a red result stops being read.
                    now = _services.status(spec)
                    notes.append(f"{label}: not enabled before this upgrade and left "
                                 f"that way ({'running' if now.get('running') else 'not running'})")
                    continue
                if ok and st.get("stale"):
                    notes.append(f"{label} (unit is stale — redeploy it from its plugin)")
                elif ok:
                    notes.append(label)
                else:
                    failed.append(f"{label} did NOT come back: "
                                  f"{st.get('error') or st.get('detail') or 'no detail'}")
            except Exception as e:                                 # noqa: BLE001
                failed.append(f"{label}: {type(e).__name__}: {e}")
        # The REASON travels with the restart, not only with the decision not to restart.
        # Whoever reads this later is asking why every agent on the host was bounced at
        # 03:00, and "services: com.finrail.cron" does not answer it.
        record("services", not failed,
               f"{why} — " + ("; ".join(notes + failed) or "none declared"))
    else:
        record("services", True, "asked not to", skipped=True)

    return _result(done, before, after, where)


#: Paths an upgrade can move without any running process caring, because nothing imports
#: them. Everything else counts: a file this list does not recognise is treated as code,
#: which is the safe direction to be wrong in.
NOT_IMPORTED = ("tests/", "docs/", ".github/")


def _provisioned(prov: dict) -> str:
    """One line on what a provision did, for a step's detail."""
    return "; ".join(
        [f"repaired {r['name']}" for r in prov.get("repaired") or []]
        + [f"FAILED {r['name']}: {r.get('error', '')}" for r in prov.get("failed") or []]
        + [f"{r['name']} needs you: {r.get('fix', '')}" for r in prov.get("needs_you") or []]
    ) or "everything already in place"


def changed_paths(where, before: str, after: str) -> list:
    """The files this upgrade moved in the checkout. Empty when it did not move."""
    if not before or before == after:
        return []
    ok, out = _run(["git", "-C", str(where), "diff", "--name-only", f"{before}..{after}"])
    return [line.strip() for line in out.splitlines() if line.strip()] if ok else []


def imported(path: str) -> bool:
    """Could a long-lived process be holding this file in memory?"""
    return not (path.startswith(NOT_IMPORTED) or path.endswith((".md", ".txt")))


def restart_needed(core: list, plugins: list) -> tuple:
    """Whether the services must be bounced, and why. `(needed, reason)`.

    A service here is a process that imported finrail and its installed plugins when it
    started and has held them in memory ever since, so the question is whether this upgrade
    replaced any of that code. The test is deliberately blunt — once a process has imported
    a package, nothing finer than "the package changed" is true of it.

    What it buys is the common case. An upgrade that moved only tests, documentation or a
    workflow file has changed nothing any running service imported, and bouncing every
    agent on the host for it is a small outage that nobody asked for and that nothing
    required. An upgrade that moved one line of `core/policy.py` bounces everything,
    because every one of them is running the old copy of it.

    A checkout that did not move at all and no plugin that changed means there is nothing
    to restart FOR, which is different from "restart was not asked for" and says so.
    """
    code = [p for p in core if imported(p)]
    if code or plugins:
        parts = []
        if code:
            parts.append(f"{len(code)} file(s) the services import changed: "
                         + ", ".join(code[:4]) + ("…" if len(code) > 4 else ""))
        if plugins:
            parts.append(f"{len(plugins)} plugin file(s) changed: "
                         + ", ".join(plugins[:4]) + ("…" if len(plugins) > 4 else ""))
        return (True, "; ".join(parts))
    if core:
        return (False, f"nothing any running service imported changed — only "
                       + ", ".join(core[:6]) + ("…" if len(core) > 6 else ""))
    return (False, "the checkout did not move and no plugin changed, so no service is "
                   "running anything older than it was")


def deploy_plugins(record, test: bool = True, touched: list = None) -> bool:
    """The `plugins` step. Fetch, test, swap, record — or change nothing.

    Everything is staged and every repository's suite is run BEFORE the first directory
    is replaced, so a red suite in one repository cannot leave some plugins upgraded and
    others not. Returns whether the step passed; `record` is `run`'s ledger.
    """
    import tempfile as _tempfile
    import time as _time
    from . import registry as _registry
    try:
        plans, skipped = _plugins.deployable()
    except Exception as e:                                          # noqa: BLE001
        return record("plugins", False, f"could not read the catalogues: {e}")
    notes = [f"not deployed: {sk['name']} — {sk['why']}" for sk in skipped]
    if not plans:
        return record("plugins", True, "; ".join(notes) or "no installed plugins",
                      skipped=True)
    work = pathlib.Path(_tempfile.mkdtemp(prefix="finrail-upgrade-plugins-"))
    try:
        try:
            staged = _plugins.stage(plans, work)
        except Exception as e:                                      # noqa: BLE001
            return record("plugins", False, f"fetching failed, nothing replaced: {e}")
        if test:
            results = _plugins.test_repositories(plans, test_python(repo()))
            failed = [r for r in results if not r["ok"]]
            if failed:
                return record("plugins", False, "tests failed, nothing replaced:\n" + "\n".join(
                    f"{r['repo']} @{r['commit'][:8]}:\n{r['detail']}" for r in failed))
            notes += [f"tests passed: {r['repo']} @{r['commit'][:8]}" for r in results]
        # LOADED BY WHAT WILL RUN IT, before the first swap, whether or not tests ran.
        # The suite above runs under this checkout's venv, and on 6 Oct that was the
        # wrong finrail: the dev agents run another checkout, the plugin passed here and
        # then failed every one of their turns (plugin_deploy, "can the finrail that
        # runs it load it?").
        problems = _plugins.incompatible(work, [p["name"] for p in plans])
        if problems:
            return record("plugins", False, "nothing replaced:\n"
                          + _plugins.describe_incompatible(problems))
        for p in plans:
            got = staged[p["name"]]
            changed = _plugins.changes(p["held"], got["manifest"])
            if touched is not None:
                # What the RESTART decision is made from: a plugin whose files did not move
                # is not a reason to bounce every agent on the host.
                touched += [f"{p['name']}/{f}" for f in changed]
            _plugins.swap(p["name"], work / p["name"])
            _registry.record_install(p["name"], dict(
                got, catalogue=p["entry"]["catalogue"], standard=p["entry"]["standard"],
                at=_time.time()))
            was = (p["held"].get("commit") or "unrecorded")[:8]
            notes.append(f"{p['name']}: {was} → {got['commit'][:8]}, "
                         f"{len(changed)} file(s) changed"
                         + (f" ({', '.join(changed[:6])}"
                            + ("…" if len(changed) > 6 else "") + ")" if changed else ""))
        _registry.load(force=True)
        still = _plugins.modified()
        if still:
            return record("plugins", False, "deployed, but the installed files do not "
                          "match their manifests: " + "; ".join(
                              _plugins.describe(d) for d in still))
        return record("plugins", True, "; ".join(notes))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _result(done: list, before: str, after: str, where: pathlib.Path) -> dict:
    return {"repo": str(where), "from": before, "to": after,
            "moved": before != after, "steps": done,
            "ok": all(s["ok"] for s in done),
            "stopped_at": next((s["step"] for s in done if not s["ok"]), "")}


def render_result(out: dict) -> str:
    lines = [f"upgrade {out['repo']}: {out['from']} → {out['to']}"
             + ("" if out["moved"] else "  (nothing new to pull)")]
    for s in out["steps"]:
        # A dash is "not everything here is done" — skipped, or reported-but-not-fatal.
        mark = "–" if s["skipped"] else ("✓" if s["ok"] else "✗")
        lines.append(f"  {mark} {s['step']}")
        # The plugins step's detail IS its result — which commit each plugin moved to and
        # which files that changed — so it is shown when it succeeds, not only when not.
        if s["detail"] and (not s["ok"] or s["skipped"] or s["step"] == "plugins"):
            lines += [f"      {line}" for line in s["detail"].splitlines()[-12:]]
    if out["stopped_at"]:
        lines.append(f"\nstopped at {out['stopped_at']} — nothing after it ran, and "
                     f"nothing was restarted.")
    return "\n".join(lines)
