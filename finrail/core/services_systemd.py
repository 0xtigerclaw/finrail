"""SYSTEMD — the Linux backend for a declared ServiceSpec.

The same ServiceSpec that renders a launchd plist renders a unit here. A plugin declares
what must run; which init system carries it is not the plugin's business.

Three things are baked in, and each one is a fault this machine actually had:

  KillMode=process   Type=oneshot with the default KillMode=control-group tears down the
                     whole cgroup the instant ExecStart returns. finrail hands background
                     work to a detached `finrail _job` child, and `start_new_session`
                     detaches the process GROUP, not the cgroup — so every backgrounded
                     cron run was killed the moment the tick exited. Thirty-six runs on
                     this box are `pending` with no `started` timestamp for exactly this
                     reason, and the agent's own unit already carries this line with a
                     comment explaining it.

  OnCalendar, and    A heartbeat whose consumers match an exact minute must land ON that
  no jitter          minute. The previous timer used OnUnitActiveSec=15min with
                     RandomizedDelaySec=60, so it fired at drifting minutes and reached
                     `17 4 * * *` about four days in sixty. Jitter is right for spreading
                     load across a fleet and wrong for a clock.

  Persistent=true    A window missed while the machine was off still fires.

Units are written to the SYSTEM directory. The launchd analogue (~/Library/LaunchAgents,
no root) does not transfer to a headless server: there is no logged-in user, which is why
lingering is off by default, and every other unit on this host is already system-scoped. A
user-scoped timer here would be a second mechanism with its own lifecycle sitting beside
three system ones.
"""
import os
import pathlib
import subprocess


KIND = "systemd"


def systemctl() -> str:
    return os.environ.get("FINRAIL_SYSTEMCTL", "systemctl")


def scope() -> str:
    """`system` or `user`. Overridable, but system is the default on Linux — see above."""
    from .settings import raw_location
    return str(raw_location("service.scope") or "system")


def units_dir() -> pathlib.Path:
    """Where units are written. Overridable so tests never touch the real ones."""
    d = os.environ.get("FINRAIL_SYSTEMD_DIR")
    if d:
        return pathlib.Path(d).expanduser()
    if scope() == "user":
        return pathlib.Path.home() / ".config/systemd/user"
    return pathlib.Path("/etc/systemd/system")


def _install_user() -> str:
    """Whose service this is. SUDO_USER when installed through sudo — the point of
    `sudo finrail service install` is to place a unit for the invoking user, not to
    hand their work to root."""
    return (os.environ.get("FINRAIL_SERVICE_USER")
            or os.environ.get("SUDO_USER")
            or (os.environ.get("USER") if os.geteuid() != 0 else "")
            or "root")


def _path_for(user: str) -> str:
    """The PATH that user would have, rather than the one this process happens to hold."""
    from .requirements import _own_bin
    home = pathlib.Path("/root" if user == "root" else f"/home/{user}")
    parts = [*_own_bin(), "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
             str(home / ".local/bin"),
             "/usr/local/sbin", "/usr/sbin", "/usr/bin", "/sbin", "/bin", "/snap/bin"]
    return ":".join(dict.fromkeys(p for p in parts if p))


def _argv() -> list:
    return [systemctl()] + (["--user"] if scope() == "user" else [])


def unit_name(spec) -> str:
    """`com.finrail.cron` -> `finrail-cron`. A label is reverse-dns because launchd
    wants that; a unit name is not, so it is converted rather than carried."""
    label = str(spec.label)
    for prefix in ("com.finrail.", "com.", "org."):
        if label.startswith(prefix):
            label = label[len(prefix):]
            break
    return "finrail-" + label.replace(".", "-") if not label.startswith("finrail") \
        else label.replace(".", "-")


def unit_target(spec) -> str:
    """What `systemctl` is given for this spec: the timer if it has an interval, else the
    service. Said once — it was written out at five call sites, and the one place a sixth
    would have disagreed is the only place it matters."""
    return f"{unit_name(spec)}.timer" if spec.interval else f"{unit_name(spec)}.service"


def service_path(spec) -> pathlib.Path:
    return units_dir() / f"{unit_name(spec)}.service"


def timer_path(spec) -> pathlib.Path:
    return units_dir() / f"{unit_name(spec)}.timer"


def _quote(argv: list) -> str:
    out = []
    for a in argv:
        a = str(a)
        out.append('"' + a.replace('\\', '\\\\').replace('"', '\\"') + '"'
                   if (" " in a or '"' in a) else a)
    return " ".join(out)


def render(spec) -> bytes:
    """The .service unit."""
    command = spec.command
    if not command:
        from .errors import ContractViolation
        raise ContractViolation(
            f"service '{spec.label}' has an empty command — nothing to run. "
            f"(If the argv depends on settings, pass a callable as `command`.)")
    # PATH FOR THE USER THE UNIT RUNS AS, not the one installing it. `path_env` builds
    # from the CURRENT process, so `sudo finrail service install` baked root's PATH into
    # a unit that runs as ubuntu: no /home/ubuntu/.local/bin, so the tick could not find
    # `finrail` if a job shelled out — and doctor reported only "the unit file is stale",
    # which is true and says nothing about why it matters.
    env = {"PATH": _path_for(_install_user())}
    env.update(spec.env)
    lines = ["[Unit]",
             f"Description={spec.description or spec.label}",
             "",
             "[Service]",
             # A tick runs and exits; a daemon stays. Both are `simple` here rather than
             # `oneshot`, because oneshot is what invites the control-group teardown that
             # killed every backgrounded run on this machine.
             "Type=simple" if spec.interval else "Type=simple",
             # THE LINE. See the module docstring: without it, work handed to a detached
             # child dies the moment the parent returns.
             "KillMode=process",
             f"ExecStart={_quote(command)}"]
    # WHO IT RUNS AS. A launchd user agent runs as the user by definition — it lives in
    # ~/Library/LaunchAgents and there is nobody else it could be. A systemd SYSTEM unit
    # runs as root unless told otherwise, and the difference is not cosmetic: the first
    # heartbeat installed without this wrote cron.json into the agent's state as
    # root-owned mode 0600, so the agent could no longer read its own schedule, and every
    # run record and job log the tick produced was owned by root too.
    #
    # The service does the same work as the person who installed it, so it runs as them.
    if scope() != "user":
        owner = _install_user()
        lines += [f"User={owner}", f"Group={owner}"]
    if spec.workdir:
        lines.append(f"WorkingDirectory={spec.workdir}")
    for k, v in sorted(env.items()):
        lines.append(f'Environment="{k}={v}"')
    if spec.keep_alive:
        lines += ["Restart=always", "RestartSec=30"]
    lines += [f"SyslogIdentifier={unit_name(spec)}", ""]
    if not spec.interval:
        lines += ["[Install]", "WantedBy=multi-user.target", ""]
    return "\n".join(lines).encode()


def render_timer(spec) -> bytes:
    """The .timer, for an interval spec. Nothing here is jittered; see the docstring."""
    every = int(spec.interval or 60)
    on_calendar = "*:*:00" if every <= 60 else f"*:0/{max(1, every // 60)}"
    return "\n".join([
        "[Unit]",
        f"Description=Heartbeat for {unit_name(spec)}",
        "",
        "[Timer]",
        f"OnCalendar={on_calendar}",
        "AccuracySec=1s",
        # No RandomizedDelaySec. A consumer that matches an exact minute cannot be
        # reached by a timer that drifts.
        "Persistent=true",
        f"Unit={unit_name(spec)}.service",
        "",
        "[Install]",
        "WantedBy=timers.target",
        ""]).encode()


def _files(spec) -> list:
    out = [(service_path(spec), render(spec))]
    if spec.interval:
        out.append((timer_path(spec), render_timer(spec)))
    return out


def installed_matches(spec) -> bool:
    """Staleness is a CONTENT question, not an existence question — the same rule the
    launchd backend follows, and the reason a drifted unit is detected at all."""
    return all(p.is_file() and p.read_bytes() == body for p, body in _files(spec))


def installed_command(spec):
    """The argv on the installed unit's ExecStart line, or None (services.py says why)."""
    import shlex
    try:
        text = service_path(spec).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            try:
                return shlex.split(line[len("ExecStart="):]) or None
            except ValueError:
                return None
    return None


def write_units(spec) -> list:
    written = []
    for p, body in _files(spec):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        written.append(p)
    return written


def _run(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(_argv() + args, capture_output=True, text=True)


def sudo() -> str:
    """Overridable so tests never need a real one."""
    return os.environ.get("FINRAIL_SUDO", "sudo")


def _privileged(args: list) -> list:
    """`systemctl` for a verb that CHANGES something, with privilege when this process has
    none and the units are the system's.

    The reads — `is-active`, `is-enabled` — need nothing and are left alone. Writes do, and
    the agent that deploys on this host is not root: that is the whole reason the restart
    at the end of every upgrade was a line handed to a person to paste. `sudo -n`, so a
    host without the rule refuses here, immediately and legibly, instead of blocking on a
    password prompt with nobody sitting in front of it.
    """
    if scope() == "user" or os.geteuid() == 0:
        return _argv() + args
    return [sudo(), "-n"] + _argv() + args


def grant(target: str = "") -> str:
    """The rule that lets this host restart a service unattended.

    Carried in the refusal rather than only in a document, because the refusal is where
    somebody is standing when they need it. It names the exact unit: sudo allows a
    wildcard in a command's path but not in its arguments, so `restart finrail-*.service`
    is a rule visudo rejects (a coding agent-dev handed that line to a person on 6 Oct, and
    visudo refused it). `restart` is the only verb granted.
    """
    import shutil
    which = shutil.which(systemctl()) or systemctl()
    unit = target or "<unit>.service"
    return (f"to let it restart {unit} unattended, put this in "
            f"/etc/sudoers.d/finrail-restart (one line per unit, no wildcards):\n"
            f"  {_install_user()} ALL=(root) NOPASSWD: {which} restart {unit}")


def restart_detached(spec, delay: int = 5) -> dict:
    """Bounce a service from OUTSIDE its own lifetime, and say so rather than claim it came
    back.

    The service carrying the conversation that asked for an upgrade is one of the services
    being upgraded. Restarting it from inside kills the run before it can write its own
    result: the code is deployed, the ledger shows a run that died, and the person who
    asked is told nothing. So the bounce is handed to a detached child and this returns at
    once — the run finishes, writes its record and answers, and the restart lands after
    it. The unit carries `KillMode=process` (see the module docstring), so the child
    outlives the main process it was started from.

    `deferred`, never `loaded`: nothing here has watched the service come back, and a
    report that says it did would be a guess dressed as a verification.
    """
    argv = _privileged(["restart", unit_target(spec)])
    try:
        subprocess.Popen(["/bin/sh", "-c", 'sleep "$1"; shift; exec "$@"', "sh",
                          str(int(delay))] + argv,
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return {"service": spec.label, "unit": unit_target(spec), "deferred": False,
                "loaded": False, "detail": str(e),
                "error": f"could not schedule a restart of {unit_target(spec)}: {e}"}
    return {"service": spec.label, "unit": unit_target(spec), "deferred": True,
            "detail": f"{unit_target(spec)} restarts in {int(delay)}s, after this run ends"}


def install(spec) -> dict:
    """Write the units and start them. Returns what happened, never claims more."""
    try:
        write_units(spec)
    except OSError as e:
        # /etc/systemd/system needs root. Say the two commands rather than half-failing.
        return {"service": spec.label, "loaded": False,
                "detail": f"cannot write {units_dir()}: {e}",
                "error": f"needs root — {fix(spec)}"}
    _run(["daemon-reload"])
    target = unit_target(spec)
    r = _run(["enable", "--now", target])
    ok, detail = test(spec)
    out = {"service": spec.label, "unit": target, "loaded": ok, "detail": detail,
           "systemctl": (r.stderr or r.stdout or "").strip() or "enabled"}
    if not ok:
        out["error"] = (f"{target} did not come up: {detail}. "
                        f"Its log is: journalctl -u {target}")
    return out


def restart(spec) -> dict:
    """Restart the unit that is on disk. Writes nothing; see services.restart."""
    target = f"{unit_name(spec)}.timer" if spec.interval else f"{unit_name(spec)}.service"
    if not service_path(spec).is_file():
        return {"service": spec.label, "unit": target, "loaded": False, "installed": False,
                "detail": f"not installed ({service_path(spec)} is not there) — nothing "
                          f"to restart; the plugin that owns it deploys it"}
    # Privileged only where it must be (`_privileged`): an agent that deploys on this host
    # is not root, and `sudo -n` refuses at once, with the rule to install, instead of
    # waiting on a password nobody will type (main, 3871bc6). It never writes the unit:
    # a restart that rewrote and enabled units re-enabled one an operator had switched off.
    r = subprocess.run(_privileged(["restart", target]), capture_output=True, text=True)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip() or "no detail"
        return {"service": spec.label, "unit": target, "loaded": False, "installed": True,
                "rewrote_unit": False, "detail": detail,
                "error": f"could not restart {target}: {detail}"
                         + ("" if os.geteuid() == 0 else f"\n{grant(target)}")}
    # A STALE UNIT IS RESTARTED AS IT IS AND SAID TO BE STALE, never rewritten: a unit is
    # written by the plugin that deploys it (docs/LAYERS.md, rule 2), and a restart that
    # rewrote and enabled units is how an agent an operator had switched off came back.
    # main (3871bc6) reinstalled a moved declaration here; the fork keeps the rule, and
    # the deployer's command is the fix the report names.
    stale = not installed_matches(spec)
    if stale:
        st = status(spec)
        ok = bool(st["loaded"]) and bool(st["running"])
        detail = (f"{target} running" if ok else f"{target} not running") \
            + " — unit is stale, redeploy it from its plugin"
    else:
        ok, detail = test(spec)
    out = {"service": spec.label, "unit": target, "loaded": ok, "installed": True,
           "detail": detail, "stale": stale, "rewrote_unit": False,
           "systemctl": (r.stderr or r.stdout or "").strip() or "restarted"}
    if not ok:
        out["error"] = (f"{target} did not come back: {detail}. "
                        f"Its log is: journalctl -u {target}")
    return out


def unowned(prefix: str, specs: list) -> list:
    """Unit files under the prefix that no spec renders. See services.unowned."""
    class _P:                              # the prefix, rendered the way a label is
        label = prefix
        interval = None
    stem = unit_name(_P)
    known = {service_path(s).resolve() for s in specs} | {timer_path(s).resolve() for s in specs}
    d = units_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in list(d.glob(f"{stem}*.service")) + list(d.glob(f"{stem}*.timer"))
                  if p.resolve() not in known)


def unload(spec) -> str:
    target = unit_target(spec)
    r = _run(["disable", "--now", target])
    return (r.stderr or r.stdout or "").strip() or "disabled"


def status(spec) -> dict:
    target = unit_target(spec)
    active = _run(["is-active", target]).stdout.strip()
    enabled = _run(["is-enabled", target]).stdout.strip()
    return {"label": spec.label, "unit": target,
            "loaded": enabled in ("enabled", "static", "enabled-runtime"),
            "running": active in ("active", "activating", "waiting"),
            "pid": None, "plist_current": installed_matches(spec)}


def test(spec) -> tuple:
    s = status(spec)
    if not s["loaded"]:
        return (False, f"{s['unit']} is not enabled")
    if not s["plist_current"]:
        return (False, f"{s['unit']} is enabled but its unit file is stale — reinstall")
    if spec.interval:
        # A timer between firings is `waiting`, which is healthy.
        return (s["running"], f"{s['unit']} armed (every {spec.interval}s)"
                if s["running"] else f"{s['unit']} is enabled but not armed")
    return (s["running"], f"{s['unit']} running" if s["running"]
            else f"{s['unit']} enabled but not running")


def fix(spec) -> str:
    target = unit_target(spec)
    sudo = "" if scope() == "user" else "sudo "
    return (f"{sudo}finrail service install {spec.label}"
            f"   (writes {units_dir()}/{target} and enables it)")
