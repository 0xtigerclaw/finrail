"""REQUIREMENTS — what a plugin needs in order to work, declared by the plugin.

A capability is useless without its infrastructure: binaries on PATH, generated config
files, a directory, a running service, a credential, a reachable connection. That
infrastructure belongs to the plugin, not to some external server — so the plugin
declares it and core provides the machinery to check and repair it.

Each requirement answers three questions:
    check()   is it satisfied right now?          -> (ok, detail)
    repair()  can finrail fix it itself?         -> str, or None if only a human can
    hint      what must a human do if it cannot?

`after` expresses ordering, so `doctor` reports the FIRST broken link rather than a
cascade, and `provision` repairs in dependency order.
"""
import importlib.util
import os
import pathlib
import re
import shutil
import subprocess
import sys

from .errors import OpFailed


class Requirement:
    kind = "requirement"

    #: Does this live INSIDE the installed package, so that reinstalling destroys it?
    #:
    #: `uv tool install --force` replaces the package directory. The libraries in that
    #: venv and the node modules under it go with it; a config file, a service, a
    #: credential and a directory on the host do not. That line is the whole of what an
    #: upgrade is responsible for putting back, and getting it wrong cost a working mail
    #: setup: `upgrade` called plain `provision()`, which repairs EVERYTHING declared,
    #: so a step whose blast radius should have been one directory was given the run of
    #: the machine — and rewrote a generated config from secrets it could not read.
    #:
    #: Declared by the requirement because the requirement is the only thing that knows
    #: where it lives. A hard-coded list of kinds in core goes stale the day a plugin
    #: ships a new one, which is exactly how the node package would have been missed.
    install_scoped = False

    #: Does repairing this need a person at a terminal?
    #:
    #: A keychain cannot prompt a process three levels deep in a service, and a read that
    #: is DENIED comes back looking identical to one that found nothing. Anything that
    #: reads or writes a credential says so here, and an unattended `provision` reports it
    #: rather than attempting it and producing something half-built.
    needs_human = False

    def __init__(self, name: str, after: list = None, hint: str = "",
                 needs_human: bool = None):
        self.name = name
        self.after = after or []
        self.hint = hint
        # Per instance when given, so a plugin can say that ITS config file is rendered
        # from secrets while config files in general are not.
        if needs_human is not None:
            self.needs_human = bool(needs_human)
        self.plugin = ""                 # set by the runtime when absorbed

    def check(self) -> tuple:
        raise NotImplementedError

    def repair(self):
        return None

    @property
    def repairable(self) -> bool:
        return type(self).repair is not Requirement.repair

    def fix(self) -> str:
        """The exact command (or instruction) that resolves this. Every red doctor line
        carries one — a fresh operator should never have to guess the next move."""
        if self.hint:
            return self.hint
        if self.repairable:
            return f"finrail provision {self.plugin}".strip()
        return "no automatic fix available"

    def __repr__(self):
        return f"<{self.kind} {self.name}>"


class Binary(Requirement):
    """An executable that must be on PATH."""
    kind = "binary"

    def __init__(self, name: str, install: str = "", **kw):
        super().__init__(name, **kw)
        self.install = install                        # e.g. "brew install isync"

    def check(self) -> tuple:
        p = shutil.which(self.name)
        return (bool(p), p or "not on PATH")

    def repair(self):
        if not self.install:
            return None
        subprocess.run(self.install.split(), check=True, capture_output=True)
        return f"ran: {self.install}"

    @property
    def repairable(self) -> bool:
        return bool(self.install)

    def fix(self) -> str:
        return self.install or f"install '{self.name}' and put it on PATH"


class PythonPackage(Requirement):
    """A third-party library a plugin needs, declared by that plugin.

    Extraction libraries (pypdf, openpyxl, …) belong to the extension that reads
    documents, not to every install of finrail: a machine that only does mail should not
    have to carry a PDF parser. Declaring them here means the same doctor line that
    reports a missing binary reports a missing library, with the exact command — and
    `provision` can install it, because the fix is mechanical.

    `module` is the IMPORT name, which is often not the install name
    (`python-docx` installs `docx`), so both are recorded and the check imports.
    """
    kind = "python"
    #: In the venv that `uv tool install --force` replaces, so an upgrade must put it back.
    install_scoped = True

    def __init__(self, module: str, install: str = "", why: str = "", **kw):
        super().__init__(module, **kw)
        self.module = module
        self.install = install or module
        self.why = why

    def check(self) -> tuple:
        try:
            found = importlib.util.find_spec(self.module) is not None
        except (ImportError, ValueError):
            found = False
        if found:
            return (True, self.module)
        return (False, f"{self.module} not installed" + (f" — {self.why}" if self.why else ""))

    def _installer(self) -> list:
        """The command that installs into THIS interpreter's environment.

        `python -m pip` is not a given. A venv created by `uv venv` has no pip in
        it at all — uv installs into the environment from outside rather than
        bootstrapping a package manager inside it — so on every uv-made install
        `provision` failed with "No module named pip" for each library in turn,
        having told the operator one line earlier that it could fix them itself.
        Found on a real install 2026-09-09, where it blocked a plugin whose only
        missing pieces were two libraries.

        So the interpreter is asked whether it has pip, and uv is used when it
        does not. `--python` is what points uv at this environment instead of
        whatever it would otherwise choose, and getting that wrong would install
        the library somewhere finrail cannot import it from — which is the same
        failure, one step later and much harder to see.
        """
        if importlib.util.find_spec("pip") is not None:
            return [sys.executable, "-m", "pip", "install", self.install]
        uv = shutil.which("uv")
        if uv:
            return [uv, "pip", "install", "--python", sys.executable, self.install]
        return []

    def repair(self):
        command = self._installer()
        if not command:
            # Neither installer exists. Saying so beats a CalledProcessError whose
            # message is about the wrong thing entirely.
            raise OpFailed(
                f"cannot install {self.install}: this interpreter has no pip and "
                f"uv is not on PATH. Install one of them, or install the library "
                f"yourself into {sys.executable}")
        subprocess.run(command, check=True, capture_output=True)
        return f"installed {self.install}"

    def fix(self) -> str:
        command = self._installer()
        if not command:
            return (f"install {self.install} into {sys.executable} — that "
                    f"interpreter has no pip and uv is not on PATH")
        return " ".join(command)


class Directory(Requirement):
    kind = "directory"

    def __init__(self, name: str, path, **kw):
        super().__init__(name, **kw)
        self.path = pathlib.Path(path).expanduser()

    def check(self) -> tuple:
        return (self.path.is_dir(), str(self.path))

    def repair(self):
        self.path.mkdir(parents=True, exist_ok=True)
        return f"created {self.path}"


#: `key = value` at the start of a line, in the config formats these files use — ini,
#: mbsyncrc, notmuch-config. Anything this does not match is not compared, so a format it
#: does not understand is simply never protected rather than wrongly refused.
_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][\w.\-]*)\s*=", re.MULTILINE)


def _lost_keys(old: str, new: str) -> set:
    """Settings present in the file that the new rendering does not have."""
    return set(_ASSIGNMENT.findall(old or "")) - set(_ASSIGNMENT.findall(new or ""))


class ConfigFile(Requirement):
    """A generated config file. `render()` produces the exact intended contents, so the
    check is not merely 'does it exist' but 'is it what it should be' — a stale config is
    the failure mode that actually bites."""
    kind = "config"

    def __init__(self, name: str, path, render, allow_loss: bool = False, **kw):
        super().__init__(name, **kw)
        self.path = pathlib.Path(path).expanduser()
        self.render = render
        self.allow_loss = allow_loss

    def check(self) -> tuple:
        if not self.path.is_file():
            return (False, f"missing: {self.path}")
        try:
            want = self.render()
        except Exception as e:
            return (False, f"cannot render intended contents: {e}")
        if self.path.read_text() != want:
            return (False, f"stale (contents differ): {self.path}")
        return (True, str(self.path))

    def repair(self):
        """Write the intended contents — but never LESS than what is already there.

        THIS IS THE ONE THAT COST A LIVE MAILBOX. `emailproxy.config` is rendered from
        credentials in a keychain. Run unattended, the read was denied, the renderer
        omitted the two credential lines without complaining, and this method wrote the
        result over a file that worked. IMAP stopped on both accounts. And because
        `check()` compares the file to what `render()` produces, the damaged file then
        AGREED WITH ITSELF and doctor went green over it.

        A repair may add and it may change. Losing a setting that was there is not a
        repair, it is damage, and no renderer can be trusted to have read everything it
        needed. So the keys are compared and a repair that drops one refuses — the
        existing file survives, and provision reports a failure somebody can act on.

        `allow_loss=True` is for the rare generator that legitimately removes settings;
        it has to be asked for, in the declaration, where the reasoning is visible.
        """
        new = self.render()
        if self.path.is_file():
            lost = _lost_keys(self.path.read_text(), new)
            if lost and not self.allow_loss:
                raise OpFailed(
                    f"refusing to rewrite {self.path}: it would drop "
                    f"{', '.join(sorted(lost))}. Something the renderer needs could not "
                    f"be read — a credential store that cannot prompt is the usual "
                    f"cause. Run this from a terminal, or pass allow_loss if the setting "
                    f"is genuinely gone.")
            # KEPT BEFORE IT IS REPLACED. Recovery should be a copy, not an investigation.
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            try:
                backup.write_text(self.path.read_text())
            except OSError:
                pass                     # a backup that cannot be written is not a reason
                                         # to refuse the repair it was only insuring
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(new)
        return f"wrote {self.path}"


class PluginRequirement(Requirement):
    """Another plugin this one calls. Declared as `REQUIRES = ("apps", "dns")` on the
    plugin module, absorbed by the registry, checked here: the plugin has to be
    installed and on, or the call fails at the moment somebody makes it, in a message
    about the wrong thing. Not repairable by code: switching a plugin on is a decision.
    """
    kind = "plugin"

    def __init__(self, plugin: str, needs: str):
        super().__init__(f"{plugin}-needs-{needs}")
        self.needs = needs

    def check(self) -> tuple:
        from .api import RUNTIME
        if self.needs in RUNTIME.plugins:
            return (True, f"{self.needs} is on")
        if self.needs in RUNTIME.installed:
            return (False, f"needs the {self.needs} plugin, which is switched off")
        return (False, f"needs the {self.needs} plugin, which is not installed")

    def fix(self) -> str:
        from .api import RUNTIME
        if self.needs in RUNTIME.installed:
            return f"finrail plugin enable {self.needs}"
        return f"finrail plugin install {self.needs}"


class ServiceRequirement(Requirement):
    """A managed service that must be loaded and running."""
    kind = "service"

    def __init__(self, name: str, spec, **kw):
        super().__init__(name, **kw)
        self.spec = spec

    def check(self) -> tuple:
        from . import services
        return services.test(self.spec)

    def repair(self):
        from . import services
        services.install(self.spec)
        return f"installed+loaded {self.spec.label}"

    def fix(self) -> str:
        return f"finrail service install {self.spec.label}"


class ConnectionCheck(Requirement):
    """Every connection's own `test()`, surfaced as a doctor check — generically, for
    every connection any plugin ever registers."""
    kind = "connection"

    def __init__(self, name: str, connection_name: str = "", **kw):
        super().__init__(name, **kw)
        self.connection_name = connection_name or name

    def check(self) -> tuple:
        from . import connections
        try:
            return connections.test(self.connection_name)
        except Exception as e:
            return (False, str(e)[:160])

    def fix(self) -> str:
        return self.hint or f"finrail connection test {self.connection_name}"


class SettingCheck(Requirement):
    """A required setting that must have a value."""
    kind = "setting"

    def __init__(self, name: str, setting: str, **kw):
        super().__init__(name, **kw)
        self.setting = setting

    def check(self) -> tuple:
        from .registry import settings
        v = settings().get(self.setting)
        ok = v not in (None, "", [], {})
        return (ok, f"{self.setting}={v!r}" if ok else f"{self.setting} is unset")

    def fix(self) -> str:
        return f"finrail settings set {self.setting}=<value>"


class Manual(Requirement):
    """Something only a human can do — a browser OAuth flow, a keychain secret. It still
    belongs in the dependency graph: doctor must be able to say it is the missing link,
    even though it cannot fix it."""
    kind = "manual"

    def __init__(self, name: str, probe, hint: str, **kw):
        super().__init__(name, hint=hint, **kw)
        self.probe = probe

    def check(self) -> tuple:
        try:
            return self.probe()
        except Exception as e:
            return (False, str(e)[:160])


class SecretRequirement(Manual):
    """A credential that must exist before something can work.

    Declaring one is how a plugin says "I need this credential" in a way that can be
    ENUMERATED — `finrail secret needed` lists every one and whether it is set, so you
    can see what is missing without discovering it as a runtime failure.

    `secret_name` is a REFERENCE, and a reference carries its own store:
    `keychain:finrail.mail#user@example.com:proxy-password` reads a credential where something
    else enrolled it. That used to be a `service=` parameter threaded through here,
    `connections` and `account.password()` — a second backend namespace wearing the wrong
    name. One concept replaced three copies of a parameter.

    `generate=True` says the value is a random string finrail may invent, so `provision`
    creates it rather than waiting for a human. That distinction is not about containers:
    every credential in the system is either generated or provided, and the mail proxy
    password is the proof — it is a random local string, and enrolling it needed a
    hand-written script during this install.
    """
    kind = "secret"

    def __init__(self, name: str, secret_name: str, hint: str = "",
                 why: str = "", generate: bool = False, length: int = 32, **kw):
        self.secret_name = secret_name
        self.why = why
        self.generate = bool(generate)
        self.length = length
        # WHICH OF THE TWO KINDS THIS IS. A credential somebody must PROVIDE cannot be got
        # without them — a keychain has no way to prompt an unattended child, and a denied
        # read comes back indistinguishable from an absence. One finrail GENERATES is a
        # random string it invents on the spot, and making that wait for a terminal would
        # break the automatic enrolment the `generate` flag exists to do.
        self.needs_human = not self.generate
        super().__init__(name, probe=self._probe,
                         hint=hint or f"finrail secret set {secret_name}", **kw)

    @property
    def where(self) -> str:
        from . import secrets
        backend, key = secrets.split(self.secret_name)
        return f"{backend}: {key}"

    def _probe(self) -> tuple:
        from . import secrets
        ok = secrets.exists(self.secret_name)
        return (ok, f"present ({self.where})" if ok
                else f"missing secret '{self.secret_name}' ({self.where})"
                     + ("" if not self.generate else
                        " — finrail can create this one: finrail provision"))

    def repair(self):
        """Only for a GENERATED secret. A provided one has an issuer, and inventing a
        value for it would produce a credential that exists and does not work."""
        if not self.generate:
            return None
        import secrets as stdlib_secrets
        from . import secrets
        secrets.put(self.secret_name, stdlib_secrets.token_urlsafe(self.length))
        return f"generated {self.secret_name} ({self.where})"

    @property
    def repairable(self) -> bool:
        return self.generate

    def fix(self) -> str:
        if self.generate:
            return "finrail provision"
        return self.hint or (f"printf '%s' '<value>' | finrail secret set "
                             f"{self.secret_name}")


#: Where a detached process is expected to find things, in order. DECLARED, not
#: inherited — see `path_env`.
PATH_DIRS = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
             "~/.local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def path_env() -> str:
    """The PATH a detached/launchd process needs. THE SAME ONE EVERY TIME.

    Learned the hard way: a launchd job inherits almost nothing, so mbsync / notmuch /
    msmtp / claude are all "not found" unless PATH is set explicitly. Every service and
    every detached worker gets this.

    Learned the harder way: it used to append `os.environ["PATH"]`, which made the value
    a property of WHOEVER HAPPENED TO BE ASKING. A plist carries this string, and
    `installed_matches` compares the file on disk against a fresh render — so a service
    written by one shell read as STALE to every other shell, permanently and for no
    reason. `finrail upgrade` rewrote all four services and then reported all four
    stale, because the PATH it wrote was its own and the PATH it compared against was
    the next caller's. Agents told their operator the harness needed redeploying; it did
    not, and redeploying did not help.

    Inheriting was also wrong on its own terms. The live PATH here carries per-session
    directories from whatever launched the shell — an editor, a session manager — which
    have no business being baked into a launchd job that outlives them.

    So the list is declared. Anything a capability needs that is not on it is a
    `Binary` requirement, which doctor reports and provision repairs — the mechanism
    that already exists for exactly this question.
    """
    return ":".join(dict.fromkeys(
        [*_own_bin()] + [str(pathlib.Path(p).expanduser()) for p in PATH_DIRS]))


def _own_bin() -> list:
    """The directory of THIS install's `finrail`, first, when it has one.

    A second install on the same host (a dev checkout beside the live one) otherwise gets
    `~/.local/bin` from the list above, and every `finrail` its harness and its workers
    run is the OTHER install's code against this install's state. Still the same string
    every time: it is a property of the interpreter, not of the caller's PATH."""
    d = pathlib.Path(sys.executable).parent
    return [str(d)] if (d / "finrail").is_file() else []
