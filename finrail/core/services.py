"""SERVICES — long-running processes a plugin needs, managed by core.

A plugin declares a ServiceSpec; core renders the launchd plist, installs it, loads it,
and reports it to doctor. The mail OAuth proxy and the cron heartbeat are services; so
is anything else a plugin ever needs running.

Two lessons are baked in:
  * the plist ALWAYS carries an explicit PATH (a launchd job inherits almost nothing, and
    "mbsync: not found" at 03:00 is how you discover that)
  * install compares CONTENTS, so a drifted plist is detected as stale rather than
    assumed fine because the file exists
"""
import os
import pathlib
import plistlib
import subprocess
import sys
import time

from .requirements import path_env
from .settings import logs


def agents_dir() -> pathlib.Path:
    """Where plists live. Overridable so tests never touch the real LaunchAgents."""
    d = os.environ.get("FINRAIL_LAUNCH_AGENTS")
    return pathlib.Path(d).expanduser() if d else (
        pathlib.Path.home() / "Library/LaunchAgents")


def launchctl() -> str:
    return os.environ.get("FINRAIL_LAUNCHCTL", "launchctl")


class ServiceSpec:
    """A managed process.

    label       reverse-dns launchd label, e.g. com.finrail.emailproxy
    command     argv list, OR a callable returning one
    interval    seconds between runs (a heartbeat), or None for a daemon
    keep_alive  restart if it exits (daemons)
    env         extra environment variables

    `command` accepts a callable because a real service's argv usually depends on paths
    that are only known once settings are loaded (a rendered config, a state directory).
    Resolving it lazily, at render time, means a plugin can declare its service at import
    time without the argv being frozen empty. An earlier version mutated a module-level
    spec from a helper function instead, which worked when `provision` happened to call
    that helper and silently wrote a plist with NO ProgramArguments when `service install`
    read the spec straight from the registry.
    """

    def __init__(self, label: str, command, interval: int = None,
                 keep_alive: bool = False, run_at_load: bool = True,
                 workdir=None, env: dict = None, description: str = "",
                 switched_by: tuple = ()):
        self.label = label
        #: (switch on, switch off): the owner's commands that record the decision and
        #: then write or remove this unit. Empty for a service nothing switches. When
        #: set, `finrail service install|uninstall` refuses and names them, because
        #: writing the unit by hand leaves the owner's record saying the opposite
        #: (docs/LAYERS.md, rule 2; the two-harnesses failure of 22 Sept).
        self.switched_by = tuple(switched_by or ())
        self._command = command
        self.interval = interval
        self.keep_alive = keep_alive
        self.run_at_load = run_at_load
        self.workdir = str(workdir) if workdir else None
        self._env = env or {}
        self.description = description
        self.plugin = ""

    @property
    def command(self) -> list:
        """Resolved at access time, so a callable declared at import time picks up
        settings that were only loaded later."""
        cmd = self._command() if callable(self._command) else self._command
        return [str(c) for c in (cmd or [])]

    @command.setter
    def command(self, value) -> None:
        self._command = value

    @property
    def env(self) -> dict:
        """Also resolvable lazily, for the same reason as `command`: a launchd job
        inherits almost nothing, so the values it needs (state and content roots) have to
        be baked into the plist — and they are only known once settings are loaded."""
        e = self._env() if callable(self._env) else self._env
        return {str(k): str(v) for k, v in (e or {}).items()}

    @env.setter
    def env(self, value) -> None:
        self._env = value

    def plist_path(self) -> pathlib.Path:
        return agents_dir() / f"{self.label}.plist"

    def log_path(self) -> pathlib.Path:
        return logs() / f"{self.label}.log"

    def render(self) -> bytes:
        command = self.command
        if not command:
            # A plist with no ProgramArguments loads as an error rather than doing
            # nothing visible, so refuse to write one at all — the cause is far easier to
            # see here than in launchd's "Bootstrap failed: 5: Input/output error".
            from .errors import ContractViolation
            raise ContractViolation(
                f"service '{self.label}' has an empty command — nothing to run. "
                f"(If the argv depends on settings, pass a callable as `command`.)")
        env = {"PATH": path_env()}
        env.update(self.env)
        # THROUGH THE LAUNCHER, once it is installed. macOS remembers consent to read an
        # external volume against the first non-Apple process launchd starts for the job,
        # and a bare interpreter is remembered by a versioned path — so every upgrade was
        # a program nobody had approved, and every service blocked on an unseen dialog.
        # An app bundle at the root is remembered by bundle ID instead, which survives
        # them all. See finrail/launcher/__init__.py for what was measured.
        from . import launcher
        d = {
            "Label": self.label,
            "ProgramArguments": launcher.wrap(command),
            "EnvironmentVariables": env,
            "RunAtLoad": bool(self.run_at_load),
            "StandardOutPath": str(self.log_path()),
            "StandardErrorPath": str(self.log_path()),
        }
        if self.interval:
            d["StartInterval"] = int(self.interval)
        if self.keep_alive:
            d["KeepAlive"] = True
        if self.workdir:
            d["WorkingDirectory"] = self.workdir
        return plistlib.dumps(d, sort_keys=True)

    def __repr__(self):
        return f"<ServiceSpec {self.label}>"


# ── which init system ───────────────────────────────────────────────────────
#
# A plugin declares a ServiceSpec and never learns which of these carries it. The
# default is what the platform actually uses, not what the other platform does: launchd
# on macOS, systemd on Linux. `service.platform` overrides it when detection is wrong.
#
# There is no single right SCOPE either. On macOS ~/Library/LaunchAgents is the normal
# home for a per-user daemon and there is a logged-in user to own it. On a headless
# server there is not — which is why lingering is off by default — so a system unit is
# the normal thing there, and is what every other unit on such a host already is.

def platform() -> str:
    """`launchd` or `systemd`.

    The env var is the test seam, exactly as FINRAIL_LAUNCHCTL and FINRAIL_SYSTEMD_DIR
    are: the launchd tests must keep exercising launchd when the suite runs on Linux.
    """
    v = (os.environ.get("FINRAIL_SERVICE_PLATFORM") or "").strip().lower()
    if v in ("launchd", "systemd"):
        return v
    from .settings import raw_location
    v = str(raw_location("service.platform") or "auto").strip().lower()
    if v in ("launchd", "systemd"):
        return v
    return "launchd" if sys.platform == "darwin" else "systemd"


def _backend():
    if platform() == "systemd":
        from . import services_systemd
        return services_systemd
    return None                      # launchd is implemented in this module


def installed_matches(spec: ServiceSpec) -> bool:
    """False when the on-disk plist differs from what we would write now (staleness is a
    content question, not an existence question)."""
    b = _backend()
    if b:
        return b.installed_matches(spec)
    p = spec.plist_path()
    return p.is_file() and p.read_bytes() == spec.render()


def installed_command(spec: ServiceSpec):
    """The argv the INSTALLED unit runs, read from the file on disk, or None.

    Not `spec.command`, which is what this process would write now and so names this
    process's interpreter. The unit names the interpreter the service really runs, and
    those differ whenever two checkouts serve one state: the dev agents run
    ~/finrail-dev while `finrail upgrade` ran from the installed tool, so every check
    made with this process's code passed for code the agents did not have
    (production, 6 Oct). A plugin deploy asks this before it swaps anything in."""
    b = _backend()
    if b:
        return b.installed_command(spec)
    p = spec.plist_path()
    try:
        argv = plistlib.loads(p.read_bytes()).get("ProgramArguments")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return [str(a) for a in argv] if isinstance(argv, list) and argv else None


def write_plist(spec: ServiceSpec) -> pathlib.Path:
    p = spec.plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    logs().mkdir(parents=True, exist_ok=True)
    p.write_bytes(spec.render())
    return p


def _domain() -> str:
    return f"gui/{os.getuid()}"


def is_registered(label: str) -> bool:
    """Whether launchd still knows this label at all."""
    r = subprocess.run([launchctl(), "print", f"{_domain()}/{label}"],
                       capture_output=True, text=True)
    return r.returncode == 0


def wait_unregistered(label: str, timeout: float = 15.0) -> bool:
    """Wait until launchd has really let the label go.

    `bootout` returns before teardown finishes. Bootstrapping into that window fails with
    `Bootstrap failed: 5: Input/output error` — an error that says nothing about the real
    cause, which is simply that the previous instance is still going away.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_registered(label):
            return True
        time.sleep(0.4)
    return not is_registered(label)


def load(spec: ServiceSpec) -> str:
    """Boot the service out, WAIT for launchd to release the label, then bootstrap.

    The wait is the whole point: without it a reinstall of a running service races its own
    teardown and fails opaquely, which then gets reported as "loaded" because
    `launchctl list` still shows the dying instance.
    """
    subprocess.run([launchctl(), "bootout", f"{_domain()}/{spec.label}"],
                   capture_output=True)
    wait_unregistered(spec.label)
    r = subprocess.run([launchctl(), "bootstrap", _domain(), str(spec.plist_path())],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # One retry: teardown occasionally outlasts the wait on a busy machine.
        time.sleep(2)
        wait_unregistered(spec.label, timeout=10)
        r = subprocess.run([launchctl(), "bootstrap", _domain(),
                            str(spec.plist_path())], capture_output=True, text=True)
    return (r.stderr.strip() or "loaded") if r.returncode != 0 else "loaded"


def unload(spec: ServiceSpec) -> str:
    b = _backend()
    if b:
        return b.unload(spec)
    r = subprocess.run([launchctl(), "bootout", f"{_domain()}/{spec.label}"],
                       capture_output=True, text=True)
    wait_unregistered(spec.label)
    return r.stderr.strip() or "unloaded"


def install(spec: ServiceSpec) -> dict:
    """Write the plist and (re)load it. Idempotent.

    Verified after a settling pause, not immediately: `launchctl list` keeps showing a
    dying instance for a moment, so checking straight away reported the OLD process as
    proof the NEW one had started — success claimed from a stale read.
    """
    b = _backend()
    if b:
        return b.install(spec)
    write_plist(spec)
    msg = load(spec)
    time.sleep(1.5)
    ok, detail = test(spec)
    out = {"service": spec.label, "loaded": ok, "detail": detail, "launchctl": msg}
    if not ok:
        out["error"] = (f"{spec.label} did not come up: {detail}. "
                        f"Its log is {spec.log_path()}")
    return out


def unit_of(spec: ServiceSpec) -> str:
    """The init system's own name for this service, or "" where it has none.

    Exists so a caller can ask "is this the service I am running inside?" without knowing
    which init system it is talking to.
    """
    b = _backend()
    if b and hasattr(b, "unit_target"):
        return b.unit_target(spec)
    return ""


def own_unit() -> str:
    """The unit THIS process runs under, or "" — read from the cgroup, not guessed.

    The service that carries a conversation asking for a deploy is one of the services the
    deploy restarts. Which one it is has to be a fact rather than a name match: an agent's
    label, its capability and its unit are three different strings, and the consequence of
    getting it wrong is bouncing the wrong service or killing the run mid-sentence.
    """
    try:
        text = pathlib.Path("/proc/self/cgroup").read_text()
    except OSError:
        return ""
    for part in reversed(text.strip().replace("\n", "/").split("/")):
        if part.endswith((".service", ".timer")):
            return part
    return ""


def restart_detached(spec: ServiceSpec, delay: int = 5) -> dict:
    """Schedule a bounce for after this process is done. See the systemd backend."""
    b = _backend()
    if b and hasattr(b, "restart_detached"):
        return b.restart_detached(spec, delay)
    return restart(spec)


def restart(spec: ServiceSpec) -> dict:
    """Stop and start what is INSTALLED. Writes nothing.

    `install` rewrites the unit and starts it, and an upgrade used it as its restart,
    so an upgrade re-enabled every unit an operator had switched off by hand: one agent
    then answered on two harnesses. A unit file is written by the plugin that deploys it
    and by nothing else (docs/LAYERS.md, rule 2); a restart takes the unit as it finds
    it. A unit that is not there is reported, not created, and a stale one is restarted
    as it is and said to be stale, with the deployer's command as the fix.
    """
    b = _backend()
    if b:
        return b.restart(spec)
    p = spec.plist_path()
    if not p.is_file():
        return {"service": spec.label, "loaded": False, "installed": False,
                "detail": f"not installed ({p} is not there) — nothing to restart; "
                          f"the plugin that owns it deploys it"}
    msg = load(spec)
    time.sleep(1.5)
    # `status`, not `test`: test() fails a stale unit, and a stale unit restarted as it
    # is has come back. Stale is reported beside `loaded`, not instead of it.
    s = status(spec)
    ok = bool(s["loaded"]) and (bool(spec.interval) or bool(s["running"]))
    stale = not s["plist_current"]
    detail = (f"loaded (pid {s['pid']})" if ok else "loaded but not running" if s["loaded"]
              else "not loaded") + (" — unit is stale, redeploy it from its plugin" if stale else "")
    out = {"service": spec.label, "loaded": ok, "installed": True, "detail": detail,
           "stale": stale, "launchctl": msg}
    if not ok:
        out["error"] = (f"{spec.label} did not come back: {detail}. "
                        f"Its log is {spec.log_path()}")
    return out


def unowned(prefix: str, specs: list) -> list:
    """Unit files whose label starts with `prefix` and which none of `specs` renders.

    A unit nobody defines is a process nobody can switch off through the definition,
    and it is reported by whoever owns the prefix, never started and never removed here.
    """
    b = _backend()
    if b:
        return b.unowned(prefix, specs)
    known = {s.plist_path().resolve() for s in specs}
    d = agents_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob(f"{prefix}*.plist") if p.resolve() not in known)


def status(spec: ServiceSpec) -> dict:
    b = _backend()
    if b:
        return b.status(spec)
    r = subprocess.run([launchctl(), "list"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        parts = line.split()
        if parts and parts[-1] == spec.label:
            pid = parts[0]
            return {"label": spec.label, "loaded": True,
                    "running": pid not in ("-", "0"), "pid": pid,
                    "plist_current": installed_matches(spec)}
    return {"label": spec.label, "loaded": False, "running": False, "pid": None,
            "plist_current": installed_matches(spec)}


def test(spec: ServiceSpec) -> tuple:
    """(ok, detail) — what doctor reports."""
    b = _backend()
    if b:
        return b.test(spec)
    s = status(spec)
    if not s["loaded"]:
        return (False, "not loaded")
    if not s["plist_current"]:
        return (False, f"loaded (pid {s['pid']}) but plist is stale — needs reinstall")
    # An interval job is normally idle between runs; that is healthy.
    if spec.interval:
        return (True, f"loaded (interval {spec.interval}s, pid {s['pid']})")
    return (s["running"], f"loaded (pid {s['pid']})" if s["running"]
            else "loaded but not running")
