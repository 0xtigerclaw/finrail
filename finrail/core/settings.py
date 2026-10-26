"""SETTINGS — where state lives, and the typed registry of what plugins need to know.

NOTE ON NAMING: "settings.json" anywhere in this file means finrail's OWN internal
settings store (`<FINRAIL_STATE>/settings.json` — the file backing `tasks.dir`,
`mail.accounts`, `brains`, and everything else in this typed registry). It has nothing to
do with `.claude/settings.json`, which is Claude Code's own project config (the PreToolUse
hook lock) and lives in a completely separate location this module never touches. Two
unrelated files that happen to share a basename.

Two roots, deliberately separate, and neither tied to any particular project folder:

  FINRAIL_STATE   the system's own state: finrail's settings.json, plus runs, lessons,
                   sessions, stores. Default ~/.finrail. This env var IS how you relocate
                   it — moving it is a normal, supported thing to do. What can't happen is
                   relocating it from WITHIN finrail's own settings system (there is no
                   `finrail settings set` key for it): reading such a setting would first
                   require finding the very file that setting lives in. That's a plain
                   circularity, not a missing feature — FINRAIL_STATE already solves the
                   real problem, an env var just structurally has to be the mechanism.
  FINRAIL_HOME    a soft CONTENT fallback (mail, tasks, brains — your work, not ours).
                   Default ~ (your actual home directory). It is used only as a guess
                   when a specific location setting (`tasks.dir`, `mail.accounts`,
                   `brains`, `runs.dir`, `memory.lessons_dir`, …) is unset; every one of
                   those is independently configurable, so content never has to live
                   under one shared root — let alone a folder that has to be called
                   "workspace". The only thing that genuinely needs a fixed directory is
                   a Claude Code project (its OWN, separate `.claude/settings.json` hook
                   lock and `CLAUDE.md`) — that directory can be small and disposable, and
                   does not need to contain any finrail content at all.

Individual pieces of state can be relocated further still, past FINRAIL_STATE: `runs()`
and `lessons()` below consult finrail's settings.json for an override before falling back
to their default subdirectory, which is what makes `runs.dir` / `memory.lessons_dir` real
settings rather than just aliases for a fixed layout.

Paths are resolved per call (never cached at import) so that a test — or a `setup` run
that changes the roots — takes effect immediately.
"""
import json
import os
import pathlib

from . import atomic
from .errors import ContractViolation, NotConfigured

DEFAULT_STATE = "~/.finrail"
DEFAULT_HOME = "~"


# ── roots ───────────────────────────────────────────────────────────────────
def state() -> pathlib.Path:
    return pathlib.Path(os.environ.get("FINRAIL_STATE", DEFAULT_STATE)).expanduser()


def home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("FINRAIL_HOME", DEFAULT_HOME)).expanduser()


def source() -> pathlib.Path:
    """finrail's own source tree — the THIRD root, alongside state and content.

    Distinct from both: state is what finrail remembers, content is what it acts on, and
    this is what it is made of. It exists because some things the system produces are
    addressed to whoever edits the code — engineering findings today, and whatever coding
    capabilities come later — and those belong in the tree, not in a state directory
    nobody looks at.

    Defaults to wherever this file is installed, which is right for a `pip install -e`
    checkout and wrong for a packaged install; hence the setting.
    """
    v = raw_location("source.dir")
    if v:
        return pathlib.Path(str(v)).expanduser()
    return pathlib.Path(__file__).resolve().parents[2]


def raw_location(key: str):
    """Peek at finrail's own settings.json directly (state()/"settings.json"; not
    .claude/settings.json) for a location override.

    Used by the location resolvers that must work before plugins have necessarily been
    loaded (ensure() runs on every invocation, ahead of registry.load()) — and by
    promptdoc.rules_dir() for the same reason. Bypasses the declared-Settings/registry
    machinery on purpose; see registry._disabled_from_disk() for the same pattern and the
    same reason. The matching SettingSpec is still declared so the location shows up in
    `settings show` and in doctor.
    """
    p = state() / "settings.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text()).get(key) or None
    except (OSError, json.JSONDecodeError):
        return None


def runs() -> pathlib.Path:
    v = raw_location("runs.dir")
    return pathlib.Path(str(v)).expanduser() if v else state() / "runs"


def store() -> pathlib.Path:
    return state() / "store"


def sessions() -> pathlib.Path:
    return state() / "sessions"


def lessons() -> pathlib.Path:
    v = raw_location("memory.lessons_dir")
    return pathlib.Path(str(v)).expanduser() if v else state() / "lessons"


def rules() -> pathlib.Path:
    """Where hand-editable prompt/rules documents live. Default is under state, NOT under
    any vault: rules are finrail's operating content, and a machine with no vault at all
    must still find them."""
    v = raw_location("rules.dir")
    return pathlib.Path(str(v)).expanduser() if v else state() / "rules"


def gen() -> pathlib.Path:
    return state() / "gen"


def logs() -> pathlib.Path:
    return state() / "logs"


DIRS = (state, runs, store, sessions, lessons, rules, gen, logs)


def ensure() -> pathlib.Path:
    """Create the state tree. Idempotent; safe to call on every command."""
    for d in DIRS:
        d().mkdir(parents=True, exist_ok=True)
    return state()


def is_first_run() -> bool:
    return not (state() / "settings.json").is_file()


# ── the typed registry ──────────────────────────────────────────────────────
class SettingSpec:
    """A setting a plugin declares. `required` ones become doctor checks."""

    KINDS = ("str", "int", "bool", "path", "list", "dict")

    def __init__(self, name: str, kind: str = "str", doc: str = "",
                 required: bool = False, default=None, plugin: str = "",
                 choices=()):
        if kind not in self.KINDS:
            raise ContractViolation(f"setting {name}: unknown kind '{kind}'")
        self.name = name
        self.kind = kind
        self.doc = doc
        self.required = required
        self.default = default
        self.plugin = plugin
        #: The whole set of values this setting can hold, when there is one. A typo in a
        #: free-string setting is otherwise SILENT: the store keeps it, every reader falls
        #: back to its default, and the fallback looks exactly like the setting not
        #: working. Refused where it is written instead.
        self.choices = tuple(choices or ())

    def coerce(self, value):
        """Turn a CLI string into the declared type. Wrong shape → ContractViolation."""
        if value is None:
            return value
        return self._allowed(self._typed(value))

    def _allowed(self, value):
        if self.choices and value not in self.choices:
            raise ContractViolation(
                f"setting {self.name}: '{value}' is not one of "
                f"{', '.join(str(c) for c in self.choices)}")
        return value

    def _typed(self, value):
        if self.kind == "str":
            return value
        try:
            if self.kind == "int":
                return int(value)
            if self.kind == "bool":
                if isinstance(value, bool):
                    return value
                return str(value).strip().lower() in ("1", "true", "yes", "on")
            if self.kind == "path":
                return str(pathlib.Path(str(value)).expanduser())
            if self.kind in ("list", "dict"):
                if isinstance(value, (list, dict)):
                    return value
                return json.loads(value)
        except (ValueError, json.JSONDecodeError) as e:
            raise ContractViolation(f"setting {self.name}: expected {self.kind} — {e}")
        return value

    def __repr__(self):
        return f"<SettingSpec {self.name} ({self.kind}){' required' if self.required else ''}>"


class Settings:
    """The settings store: declared specs + persisted values."""

    def __init__(self):
        self._specs: dict[str, SettingSpec] = {}

    # -- declaration (plugins, via the API) ---------------------------------
    def declare(self, spec: SettingSpec) -> SettingSpec:
        prior = self._specs.get(spec.name)
        if prior and prior.plugin != spec.plugin:
            raise ContractViolation(
                f"setting '{spec.name}' declared by both {prior.plugin} and {spec.plugin}")
        self._specs[spec.name] = spec
        return spec

    def declared(self) -> dict:
        return dict(self._specs)

    def spec(self, name: str) -> SettingSpec:
        if name not in self._specs:
            raise ContractViolation(
                f"unknown setting '{name}' (see: finrail settings show)")
        return self._specs[name]

    # -- values --------------------------------------------------------------
    def _path(self) -> pathlib.Path:
        return state() / "settings.json"

    def _read(self) -> dict:
        p = self._path()
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError as e:
            raise ContractViolation(f"settings.json is not valid JSON: {e}")

    def all(self) -> dict:
        """Persisted values merged over declared defaults."""
        out = {n: s.default for n, s in self._specs.items() if s.default is not None}
        out.update(self._read())
        return out

    def stored(self, name: str):
        """The PERSISTED value, with no default behind it — `None` means nothing is set.

        `get()` cannot answer this, and should not: a caller asking for a setting wants the
        value in force, default included. A caller asking whether a WRITE happened needs
        the store, and reading `get()` for it takes the declared default as proof of the
        write. `settings.unset` did that and reported failure after succeeding, on every
        one of the 54 settings that declare a default — the value it read back to check
        could never be `None`.
        """
        return self._read().get(name)

    def get(self, name: str, default=None):
        spec = self._specs.get(name)
        raw = self._read().get(name)
        if raw is None:
            if spec is not None and spec.default is not None:
                return spec.default
            return default
        return spec.coerce(raw) if spec else raw

    def require(self, name: str):
        """Read a setting that must be there. Missing → NotConfigured WITH the fix."""
        v = self.get(name)
        if v in (None, "", [], {}):
            raise NotConfigured(name, f"finrail settings set {name}=<value>")
        return v

    def set(self, name: str, value) -> dict:
        """Locked read-modify-write, replaced atomically.

        Three separate steps, and with several agents sharing one state directory each
        one is a hazard: interleaved writers lose an update silently, and a reader that
        arrives mid-write sees a truncated file and stops finrail with "settings.json is
        not valid JSON". Both were reproduced here with six concurrent writers.
        """
        spec = self.spec(name)                 # unknown name → refuse (no silent junk)
        coerced = spec.coerce(value)
        ensure()

        def change(data):
            data[name] = coerced
            return data

        atomic.update_json(self._path(), change, indent=2, sort_keys=True)
        return {"setting": name, "value": coerced}

    def unset(self, name: str) -> dict:
        gone = []

        def change(data):
            gone.append(data.pop(name, None))
            return data

        atomic.update_json(self._path(), change, indent=2, sort_keys=True)
        return {"setting": name, "was": gone[0] if gone else None}

    # -- doctor --------------------------------------------------------------
    def missing(self) -> list:
        """Required settings with no value — each with the command that fixes it."""
        have = self.all()
        out = []
        for name, spec in sorted(self._specs.items()):
            if spec.required and have.get(name) in (None, "", [], {}):
                out.append({"setting": name, "plugin": spec.plugin, "doc": spec.doc,
                            "fix": f"finrail settings set {name}=<value>"})
        return out
