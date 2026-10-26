"""SETTINGS plugin — configuration, the guided first run, and plugin enable/disable.

`finrail setup` is the one command a fresh operator (or a Claude Code session that can
only run `finrail`) needs to know. It reads doctor's gaps in dependency order and prints
the exact next command for each — so setup is resumable by nature: run it again and it
shows only what is still missing.

Two locations are independent, and both are configurable ON THEIR OWN — neither is tied
to a project folder. (Note: "settings.json" below is finrail's own settings store, not
Claude Code's separate `.claude/settings.json` project config.)

  state    finrail's own settings.json, the run ledger, lessons, sessions. Env
           `FINRAIL_STATE`, default `~/.finrail` — a normal, supported thing to
           relocate. What can't happen is relocating it via an `finrail settings set`
           key: reading that setting would first require finding the settings.json it
           lives in. A plain circularity, not a missing feature.
  content  mail, tasks, brains. Env `FINRAIL_HOME`, default `~` — a soft fallback used
           only when a specific setting (`tasks.dir`, `mail.accounts`, `brains`, …) is
           unset. Every one of those is independently settable; nothing requires content
           to live under one shared root, let alone a folder named "workspace".

Within state, individual pieces can be relocated further still — `runs.dir` and
`memory.lessons_dir` below.
"""
import sys

from ...core import provision, registry, settings as core_settings, view
from ...core.errors import ContractViolation
from ...core.registry import settings as declared_settings
from ...core.services import ServiceSpec
from ...core.settings import SettingSpec
from ...core.requirements import Requirement
from ...plugin import Plugin

NAME = "settings"
DESCRIPTION = "Configuration, guided setup, and plugin enable/disable."

SETTINGS = [
    SettingSpec("llm.permission_mode", "str", required=False, default="auto",
                doc="How a tool-using model call is permitted: auto|acceptEdits|"
                    "bypassPermissions|plan. `auto` still honours hooks and deny lists; "
                    "`bypassPermissions` does not"),
    SettingSpec("llm.bare", "bool", required=False, default=True,
                doc="A model call that asks for no tools is given none — no shell, no "
                    "file tools, no MCP. `false` restores the engine's full tool list "
                    "to tool-less calls, as it was before 2026-10-13"),
    SettingSpec("service.platform", "str", required=False, default="auto",
                doc="Which init system carries declared services: auto|launchd|systemd "
                    "(auto = launchd on macOS, systemd on Linux)"),
    SettingSpec("service.launcher", "str", required=False,
                doc="macOS only: the app every launchd service starts through, so one "
                    "privacy approval covers them across interpreter upgrades (default "
                    "~/Applications/Finrail.app). `none` switches it off, for diagnosis "
                    "— and brings back the per-interpreter prompts it exists to prevent"),
    SettingSpec("service.scope", "str", required=False,
                doc="user|system. Default is what the platform actually uses: a launchd "
                    "user agent on macOS, a system unit on Linux (a headless host has no "
                    "logged-in user, which is why lingering is off by default)"),
    SettingSpec("plugins.disabled", "list", required=False, default=[],
                doc="Installed extensions turned off. Bundled plugins cannot appear here."),
    SettingSpec("plugins.sources", "list", required=False,
                doc="ADDITIONAL plugin catalogues — a git URL or a local path each. The "
                    "standard one is a constant in the code and is not listed here; "
                    "adding a source is a deliberate act and reads like one."),
    SettingSpec("plugins.standard_source", "str", required=False,
                doc="Replaces the standard catalogue, for a fork or an air-gapped "
                    "mirror. Normally unset — and reported loudly wherever sources are "
                    "shown when it is not, because the difference between 'the standard "
                    "plugins' and 'plugins from wherever this points' is otherwise a "
                    "setting nobody re-reads."),
    SettingSpec("caller.schedule_units", "list", required=False,
                default=["example-inbox.service"],
                doc="Systemd units that are the schedule's though their name does not "
                    "say cron (core/caller.py). The review site's instant drain runs the "
                    "cron's drain the moment a signed click lands; a process in it may "
                    "approve in the name of the person whose click it carries."),
    SettingSpec("events.enabled", "bool", required=False, default=True,
                doc="Keep one ordered log per conversation under <state>/events "
                    "(core/events.py). On by default."),
    SettingSpec("sessions.max_context_tokens", "int", required=False, default=150000,
                doc="A conversation whose engine context has reached this many tokens "
                    "continues in a fresh engine conversation, handed the recent turns "
                    "(core/sessions.py). 0 never starts a fresh one."),
    SettingSpec("sessions.idle_hours", "int", required=False, default=12,
                doc="A conversation nobody has spoken in for this many hours continues "
                    "in a fresh engine conversation, handed the recent turns. 0 never "
                    "starts a fresh one."),
    SettingSpec("runs.dir", "path", required=False,
                doc="Where the run ledger lives (default: <state>/runs)"),
    SettingSpec("rules.dir", "path", required=False,
                doc="Hand-editable prompt/rules documents (default: <state>/rules)"),
    SettingSpec("source.dir", "path", required=False,
                doc="finrail's own source tree — the third root, alongside state and "
                    "content. Defaults to wherever this install lives. Nothing the system "
                    "produces is written here: findings live in their own repository "
                    "(memory.findings_dir)."),
    SettingSpec("view.open", "bool", required=False, default=False,
                doc="Open a rendered view in a browser window (off: a card is preferable "
                    "to a window stealing focus)"),
    SettingSpec("view.front_end", "str", required=False, default=view.CLAUDE_DESKTOP,
                choices=view.FRONT_ENDS,
                doc="Who is going to look at a rendered view — claude-desktop: an HTML "
                    "page on disk plus the call that opens it in the side panel; chat: "
                    "markdown inline. A remote chat cannot open a path on this machine, "
                    "so with the wrong value here every view is unreachable rather than "
                    "merely ugly."),
]


def _heartbeat_command() -> list:
    """`finrail cron tick`, run through the interpreter that is actually running this
    code — so the service points at this install rather than whatever happens to be on
    PATH when launchd starts it."""
    import sys
    return [sys.executable, "-P", "-m", "finrail", "cron", "tick"]   # -P: serve._child_argv


def _heartbeat_env() -> dict:
    """A launchd job inherits almost nothing, so the state root must be explicit — without
    it the tick would read a DIFFERENT settings.json and silently operate on the wrong
    vault.

    Only FINRAIL_STATE is baked in, deliberately. It is the one location that cannot come
    from settings (it is where settings live); everything else — tasks, mail, brains — is
    an explicit setting read from there. Baking FINRAIL_HOME too made the rendered plist
    depend on whatever the installer happened to have exported, so `doctor` reported the
    plist as "stale" purely because it was run from a different shell.
    """
    return {"FINRAIL_STATE": str(core_settings.state())}


# Every plugin's cron jobs are inert without something to fire them. This is that
# something: one OS-level heartbeat calling `cron tick` each minute, which then dispatches
# whatever is due THROUGH the normal capability path (so a scheduled action obeys the same
# approval and verification rules as one typed by hand). Declared by the settings plugin
# because it is system-wide infrastructure, not any one domain's.
HEARTBEAT = ServiceSpec(
    label="com.finrail.cron",
    command=_heartbeat_command,
    env=_heartbeat_env,
    interval=60,
    run_at_load=True,
    description="Heartbeat: fires due cron jobs every minute")

SERVICES = [HEARTBEAT]


def REQUIREMENTS():
    """The rules directory and the cron heartbeat, surfaced to doctor.

    rules.dir is declared here rather than in any one plugin because rules span several —
    mail's voice and composing rules, the router's, tasks'. Being a real check matters: a
    wrong rules location otherwise fails silently, since every caller has an in-code
    fallback.

    The heartbeat is checked only when something actually schedules work, so a minimal
    install is not nagged about a scheduler it has no use for.
    """
    from ...core import promptdoc
    from ...core.requirements import Directory, ServiceRequirement
    from ...core.api import RUNTIME
    reqs = [Directory("rules.dir", promptdoc.rules_dir(),
                      hint="finrail settings set rules.dir=<path>")]
    if RUNTIME.crons:
        reqs.append(ServiceRequirement("cron-heartbeat", HEARTBEAT))
    reqs += _launcher_requirements(RUNTIME)
    return reqs


class LauncherApp(Requirement):
    """The app every launchd service starts through — built, signed, current.

    NOT `install_scoped`, deliberately, so `finrail upgrade` never rebuilds it. A rebuild
    changes the binary, an ad-hoc signature binds macOS's consent to the exact binary, and
    so a rebuild during an unattended upgrade would raise a fresh dialog on a screen
    nobody is watching — the outage the launcher exists to prevent, caused by the thing
    that prevents it. It is rebuilt only by an explicit `finrail provision`, which a
    person runs at the Mac, where the one dialog it raises can be answered.
    """
    kind = "launcher"

    def check(self) -> tuple:
        from ...core import launcher
        if not launcher.enabled():
            return (True, "switched off (service.launcher=none) — services run their "
                          "interpreter directly and inherit its versioned-path prompts")
        if not launcher.installed():
            return (False, f"not built: {launcher.app_path()}")
        want, have = launcher.fingerprint(), launcher.installed_fingerprint()
        if want != have:
            return (False, "built from older source — rebuilding changes the binary, so "
                           "macOS will ask once more; do it at the Mac")
        ident = launcher.signed_identifier()
        if ident != launcher.BUNDLE_ID:
            return (False, f"signed as {ident or 'nothing'}, not {launcher.BUNDLE_ID} — "
                           f"macOS would treat it as a different program")
        return (True, f"{launcher.app_path()} ({launcher.BUNDLE_ID})")

    def repair(self):
        from ...core import launcher
        return f"built {launcher.build()} — approve Finrail when macOS asks, at the Mac"

    def fix(self) -> str:
        return ("finrail provision settings   (at the Mac: macOS asks once to let "
                "Finrail read the external volume)")


class LauncherCanRead(Requirement):
    """Can a service started through the launcher actually read the external volumes?

    The check that would have named the 2026-09-17 outage in the first minute instead of
    two hours later. It goes through launchd because nothing else asks the right question
    — see `core.launcher.probe` — and it distinguishes REFUSED from WAITING, because a
    read that is waiting on an unseen dialog is not a denial and must never look like one.
    """
    kind = "launcher-access"

    def check(self) -> tuple:
        from ...core import launcher
        roots = launcher.protected_roots()
        ok, detail = launcher.probe(roots)
        return (bool(ok), detail)

    def fix(self) -> str:
        return ("at the Mac: approve Finrail in the macOS dialog, or System Settings → "
                "Privacy & Security → Files and Folders → Finrail")


def _launcher_requirements(runtime) -> list:
    """Only where it means something: launchd, with services declared. The access probe
    only once the app exists and only when an external volume is actually in use —
    probing starts a launchd job, and a doctor that did that on every machine for no
    reason would be a doctor people stop running."""
    from ...core import launcher, services
    if services.platform() != "launchd" or not runtime.services:
        return []
    reqs = [LauncherApp("service-launcher")]
    if launcher.installed() and launcher.protected_roots():
        reqs.append(LauncherCanRead("service-launcher-access", after=["service-launcher"]))
    return reqs


# ── reads ───────────────────────────────────────────────────────────────────
class Show(Plugin):
    name = "settings.show"
    description = "Every declared setting, its value and who declared it."
    read_only = True
    confirm = False

    def run(self, name: str = ""):
        s = declared_settings()
        have = s.all()
        rows = []
        for key, spec in sorted(s.declared().items()):
            if name and name not in key:
                continue
            rows.append({"setting": key, "value": have.get(key), "kind": spec.kind,
                         "required": spec.required, "plugin": spec.plugin,
                         "choices": list(spec.choices), "doc": spec.doc})
        return {"settings": rows, "state_dir": str(core_settings.state()),
                "content_dir": str(core_settings.home())}

    def render_data(self, out) -> str:
        rows = out["settings"]
        if not rows:
            return "(no settings declared)"
        lines = [f"state:   {out['state_dir']}  (settings.json, runs, lessons, sessions)",
                 f"content: {out['content_dir']}  (fallback only — every location below "
                 f"can point elsewhere)", ""]
        for r in rows:
            flag = "*" if r["required"] else " "
            val = "(unset)" if r["value"] in (None, "", [], {}) else str(r["value"])
            lines.append(f" {flag}{r['setting']:28} {val[:44]:45} [{r['plugin']}]")
            # A fixed set of values, said where the value is — otherwise the only way to
            # learn one exists is to guess wrong and read the refusal.
            if r.get("choices"):
                lines.append(f"  {'':28} one of: {', '.join(r['choices'])}")
        lines.append("\n  * required     set one: finrail settings set <name>=<value>")
        return "\n".join(lines)


def plugin_deploy_describe(d: dict) -> str:
    from ...core import plugin_deploy
    return plugin_deploy.describe(d)


class Plugins(Plugin):
    """Every discovered plugin, whether or not it is active — the installed-vs-enabled
    distinction made visible. `finrail capabilities` only ever shows what an enabled
    plugin contributes; this shows what EXISTS, on disk, regardless."""
    name = "plugins.list"
    description = ("Installed plugins: bundled, shipped extensions, and installed ones.")
    read_only = True
    confirm = False

    def run(self):
        from ...core import plugin_deploy
        return {"plugins": registry.installed(),
                "quarantined": registry.quarantined(),
                "installed_dir": str(registry.installed_root()),
                # Installed code that is not the code that was installed. Listed here
                # because this is where somebody looks to learn what is running.
                "drift": plugin_deploy.drift()}

    def render_data(self, out) -> str:
        rows = out["plugins"]
        lines = []
        # Rendered by KIND, and a kind this does not know about must still appear.
        # An installed plugin that loads, whose capabilities work, and which nothing
        # lists is the exact failure discovery claims to refuse — and it was this
        # renderer, not discovery, that produced it.
        groups = [("bundled", "bundled ({n}, always enabled):"),
                  ("extension", "shipped extensions ({n}):"),
                  ("installed", "installed ({n}) — from " + out.get("installed_dir", "")
                   + ":")]
        seen = set()
        for kind, heading in groups:
            members = [p for p in rows if p["kind"] == kind]
            seen.add(kind)
            if not members and kind == "installed":
                continue                    # nothing installed yet is not worth a heading
            lines.append(("" if not lines else "\n") + heading.format(n=len(members)))
            for p in members:
                mark = "    " if kind == "bundled" else \
                       ("[on ] " if p["enabled"] else "[OFF] ")
                lines.append(f"  {mark}{p['name']:12} {p['description'][:56]}")
        stray = [p for p in rows if p["kind"] not in seen]
        if stray:
            lines.append(f"\nunrecognised kind ({len(stray)}):")
            for p in stray:
                lines.append(f"  [{p['kind']}] {p['name']:12} {p['description'][:50]}")
        # Present, refused, and NAMED. A plugin that is broken and invisible is the
        # same failure as one that works and is invisible: the listing is where somebody
        # looks to find out what is going on, and silence there sends them to the logs.
        broken = out.get("quarantined") or []
        if broken:
            lines.append(f"\nQUARANTINED ({len(broken)}) — installed, not running:")
            for p in broken:
                lines.append(f"  [!!!] {p['name']:12} {p['fault'][:70]}")
        drift = out.get("drift") or []
        edited = [d for d in drift if not d["unknown"]]
        if edited:
            lines.append(f"\nMODIFIED IN PLACE ({len(edited)}) — not the code that was "
                         f"installed, and `finrail upgrade` will refuse until resolved:")
            lines += [f"  [!!!] {plugin_deploy_describe(d)}" for d in edited]
        unrecorded = [d["name"] for d in drift if d["unknown"]]
        if unrecorded:
            lines.append(f"\nno install manifest ({len(unrecorded)}): "
                         f"{', '.join(unrecorded)} — no recorded commit for what is running")
        if any(not p["enabled"] for p in rows if p["kind"] != "bundled"):
            lines.append("\nenable:  finrail do plugin.enable name=<name>")
        lines.append("disable: finrail do plugin.disable name=<name>")
        return "\n".join(lines)


class PluginSources(Plugin):
    """Where plugins are listed, and whether each catalogue can actually be read."""
    name = "plugins.sources"
    description = "Plugin catalogues: where they are, and when each was last fetched."
    read_only = True
    confirm = False
    also_known_as = ("where do plugins come from", "list plugin catalogues")

    def run(self):
        import time
        from ...core import catalogue
        rows = []
        for source in catalogue.declared():
            at = catalogue.fetched_at(source)
            row = dict(source, fetched_at=at,
                       age_hours=round((time.time() - at) / 3600, 1) if at else None,
                       local=catalogue.is_local(source["url"]),
                       where=str(catalogue.location(source)))
            if not source["url"]:
                # Not a broken catalogue — an absent one. Reporting a missing file for
                # something nobody configured sends people looking for the file.
                row.update(readable=False, plugins=0,
                           why=("no standard catalogue is published yet"
                                if source["standard"] else "no URL configured"))
                rows.append(row)
                continue
            try:
                manifest = catalogue.read(source)
                row.update(readable=True, plugins=len(manifest["plugins"]),
                           title=manifest["name"])
            except Exception as e:                                   # noqa: BLE001
                row.update(readable=False, plugins=0, why=str(e)[:200])
            rows.append(row)
        return {"sources": rows}

    def render_data(self, out) -> str:
        lines = []
        for s in out["sources"]:
            tag = "standard" if s["standard"] else "added"
            lines.append(f"  {s['name']:14} [{tag}]  {s['url'] or '(none configured)'}")
            if s["overridden"]:
                # The one thing worth shouting about: the difference between "the
                # standard plugins" and "plugins from wherever this points" is a setting
                # nobody re-reads, so it is said every time rather than once.
                lines.append("      OVERRIDDEN by plugins.standard_source — this is NOT "
                             "the catalogue that ships with finrail")
            if s["readable"]:
                when = ("read in place (local)" if s["local"] else
                        f"fetched {s['age_hours']}h ago" if s["age_hours"] is not None
                        else "never fetched")
                lines.append(f"      {s['plugins']} plugin(s) · {when}")
            else:
                lines.append(f"      cannot read it: {s.get('why', '')[:74]}")
        lines.append("")
        lines.append("  add one:  finrail settings set plugins.sources='[\"<url>\"]'")
        lines.append("  fetch:    finrail do plugins.refresh")
        return "\n".join(lines)


class PluginsRefresh(Plugin):
    """Fetch the catalogues. Only the catalogues — no plugin code is downloaded here."""
    name = "plugins.refresh"
    description = "Fetch or update the plugin catalogues.  [name=<source>]"
    # No approval: this clones a list, runs nothing, and is undone by deleting a cache
    # directory. Installing a plugin is the act that deserves a gate, and it has one.
    autonomous = True
    confirm = False
    background = True
    reason = "clones or updates the catalogue repositories; it downloads no plugin code"
    verified_by = "each catalogue is read back and its plugin count reported"

    def plan(self, name: str = ""):
        from ...core import catalogue
        sources = [s for s in catalogue.declared()
                   if not name or s["name"] == name.strip()]
        if name and not sources:
            raise ContractViolation(
                f"no catalogue called {name!r} (finrail get plugins.sources)")
        remote = [s for s in sources if s["url"] and not catalogue.is_local(s["url"])]
        if not remote:
            raise ContractViolation(
                "nothing to fetch — every configured catalogue is a local path, or none "
                "is configured (finrail get plugins.sources)")
        return {"sources": remote}

    def render(self, p) -> str:
        return ("FETCH  " + ", ".join(s["name"] for s in p["sources"]) + "\n"
                + "\n".join(f"    {s['url']}" for s in p["sources"])
                + "\n  this downloads the LIST of plugins, not any plugin")

    def execute(self, p) -> dict:
        from ...core import catalogue
        return {"results": [catalogue.refresh(s) for s in p["sources"]], "ok": True}

    def render_data(self, out) -> str:
        lines = []
        for r in out["results"]:
            lines.append(f"  {r['name']}: "
                         + (f"fetched at {r.get('commit') or 'unknown commit'}"
                            if r["fetched"] else f"NOT fetched — {r.get('detail', '')}"))
        return "\n".join(lines)


class PluginsAvailable(Plugin):
    """Everything the catalogues list, and what is already here."""
    name = "plugins.available"
    description = "Plugins the catalogues offer.  [installed=false to hide what you have]"
    read_only = True
    confirm = False
    also_known_as = ("what plugins can I install", "list available plugins")

    def run(self, installed: bool = True):
        from ...core import catalogue
        out = catalogue.available()
        if not installed:
            out["plugins"] = [p for p in out["plugins"] if not p["installed"]]
        return out

    def render_data(self, out) -> str:
        rows = out["plugins"]
        lines = [f"{len(rows)} plugin(s) across "
                 f"{len(out['sources'])} catalogue(s)"] if rows else []
        for p in sorted(rows, key=lambda x: (not x["standard"], x["name"])):
            state = ("installed" if p["installed"] and not p["quarantined"] else
                     "QUARANTINED" if p["quarantined"] else "")
            where = "" if p["standard"] else f"  [{p['catalogue']}]"
            lines.append(f"  {p['name']:14} {p['description'][:44]:46}"
                         f"{state}{where}")
            if p["requires"]:
                lines.append(f"      needs: {', '.join(p['requires'])}")
        # A catalogue that could not be read is REPORTED, never dropped: a short list
        # beside a silent failure reads as "this is everything there is".
        for f in out["failed"]:
            lines.append(f"  catalogue {f['name']} could not be read: {f['why'][:70]}")
        if not rows and not out["failed"]:
            lines.append("no catalogues are configured")
        lines.append("")
        lines.append("  install:  finrail do plugin.install name=<name>   (not built yet)")
        return "\n".join(lines)


class PluginInstall(Plugin):
    """Fetch a plugin's code and put it where discovery will find it.

    This is the act that deserves the gate, and it is worth being plain about why:
    installing runs somebody else's code inside a process that reaches your keychain,
    your mail, your DNS credentials and your docker socket. So the approval names the
    repository, the ref and the commit, and a plugin from an added catalogue is rendered
    differently from one in the standard set — the second is the one somebody vouched for.

    Nothing OF THE PLUGIN'S is executed by installing. A clone is a copy: no build step,
    no post-install hook, not ever. Code from here runs when a capability runs, behind the
    gate that already exists for that.

    What DOES happen is provisioning: the libraries the plugin declares are installed
    into the interpreter finrail is running in, as part of this act rather than as a
    step somebody has to remember afterwards. A plugin whose packages are missing
    installs cleanly, loads cleanly, and then fails at the moment somebody first needs
    it — which is the worst time to discover it and the hardest place to connect to the
    cause. `finrail provision` still exists and does the same thing; it is no longer the
    only thing that does.
    """
    name = "plugin.install"
    description = "Install a plugin from a catalogue.  name=<plugin>"
    confirm = True
    background = True
    aliases = {"plugin": "name"}
    also_known_as = ("install a plugin", "add a plugin")
    reason = ("downloads code that will run inside finrail, with everything finrail "
              "can reach")
    verified_by = ("the plugin imports, its capabilities appear in the catalogue, and "
                   "what it declared is reported back")

    def plan(self, name: str = ""):
        from ...core import catalogue, registry
        wanted = (name or "").strip()
        if not wanted:
            raise ContractViolation(
                "which plugin? (finrail get plugins.available)")
        here = {p["name"]: p for p in registry.installed()}
        if wanted in here and here[wanted]["kind"] != "installed":
            raise ContractViolation(
                f"'{wanted}' already ships with finrail as a {here[wanted]['kind']} "
                f"plugin — installing over it is refused, and it would be shadowed "
                f"rather than replaced")
        order, missing = catalogue.resolve([wanted])
        if missing:
            sources = ", ".join(s["name"] for s in catalogue.declared())
            raise ContractViolation(
                f"not in any catalogue: {', '.join(missing)} (searched: {sources}) — "
                f"finrail do plugins.refresh, or add a source")
        by_name = {s["name"]: s for s in catalogue.declared()}
        steps = []
        for entry in order:
            if entry["name"] in here and here[entry["name"]]["kind"] != "installed":
                continue                      # a dependency that already ships: fine
            where = catalogue.origin(entry, by_name[entry["catalogue"]])
            steps.append(dict(entry, **{"origin": where,
                                        "already": entry["name"] in here}))
        if not steps:
            raise ContractViolation(f"'{wanted}' is already installed "
                                    f"(finrail do plugin.update name={wanted})")
        return {"name": wanted, "steps": steps,
                "into": str(registry.installed_root())}

    def render(self, p) -> str:
        lines = [f"INSTALL  {p['name']}"
                 + (f"  and {len(p['steps']) - 1} it depends on"
                    if len(p["steps"]) > 1 else "")]
        for s in p["steps"]:
            o = s["origin"]
            vouched = ("standard catalogue" if s["standard"]
                       else f"ADDED CATALOGUE '{s['catalogue']}' — nobody vouched for "
                            f"this")
            lines.append(f"  {s['name']}{'  (reinstall)' if s['already'] else ''}"
                         f"   {s['description'][:44]}")
            lines.append(f"      {o['repo']}"
                         + (f"  //{o['subdir']}" if o["subdir"] else "")
                         + (f"  @{o['ref']}" if o["ref"] else "  @default branch"))
            lines.append(f"      {vouched}")
        lines += [f"  into {p['into']}, where discovery will find it",
                  "  this DOWNLOADS code that will run inside finrail, with everything "
                  "finrail can reach — keychain, mail, docker",
                  "  nothing is executed by installing: no build step and no "
                  "post-install hook, ever"]
        return "\n".join(lines)

    def execute(self, p) -> dict:
        import pathlib as _p
        import time
        from ...core import catalogue, progress, registry
        import shutil
        import tempfile
        from ...core import plugin_deploy
        by_name = {s["name"]: s for s in catalogue.declared()}
        done = []
        # STAGED, LOADED, THEN PLACED. Fetched straight into the plugins directory, a
        # plugin the running finrail cannot load was live before anything had asked
        # (plugin_deploy, "can the finrail that runs it load it?"; production, 6 Oct).
        # Importing it is not new: the load just below always did. This does it first,
        # in a separate process, so a plugin that will not load never lands.
        work = _p.Path(tempfile.mkdtemp(prefix="finrail-plugin-install-"))
        try:
            fetched = []
            for s in p["steps"]:
                progress.emit("install", f"fetching {s['name']}")
                fetched.append((s, catalogue.fetch(s, by_name[s["catalogue"]],
                                                   work / s["name"])))
            progress.emit("install", "loading it in the finrail that will run it")
            plugin_deploy.refuse_if_incompatible(work, [s["name"] for s in p["steps"]])
            for s, got in fetched:
                plugin_deploy.swap(s["name"], work / s["name"])
                registry.record_install(s["name"], dict(
                    got, catalogue=s["catalogue"], standard=s["standard"],
                    at=time.time()))
                done.append(dict(got, name=s["name"]))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        registry.load(force=True)
        # THE LIBRARIES IT DECLARED, into this interpreter. `provision` resolves the
        # installer against `sys.executable`, so what lands is importable by the process
        # that will run the capability — and on a uv-made environment, which has no pip
        # inside it, it uses uv with `--python` rather than failing.
        provisioned = {}
        for d in done:
            progress.emit("install", f"provisioning {d['name']}")
            try:
                provisioned[d["name"]] = provision.provision(d["name"])
            except Exception as e:                                 # noqa: BLE001
                provisioned[d["name"]] = {"ok": False, "repaired": [], "needs_you": [],
                                          "failed": [{"name": d["name"],
                                                      "error": str(e)[:200]}]}
        # What it DECLARED, read from the code rather than from the catalogue's
        # description of it. That is the answer to "what did I just install".
        declared = {}
        for d in done:
            mod = registry.RUNTIME.plugins.get(d["name"])
            declared[d["name"]] = {
                "capabilities": sorted(c.name for c in registry.RUNTIME.commands.values()
                                       if getattr(c, "bundle", "") == d["name"]),
                "settings": sorted(s.name for s in registry.RUNTIME.settings.declared()
                                   if getattr(s, "plugin", "") == d["name"])
                if hasattr(registry.RUNTIME.settings, "declared") else [],
                "quarantined": next((q["fault"] for q in registry.quarantined()
                                     if q["name"] == d["name"]), "")}
        return {"installed": done, "declared": declared, "provisioned": provisioned,
                "ok": not any(v["quarantined"] for v in declared.values())}

    def render_data(self, out) -> str:
        lines = []
        for d in out["installed"]:
            info = out["declared"].get(d["name"], {})
            lines.append(f"  {d['name']} at {d['commit'][:12] or 'unknown commit'}")
            if info.get("quarantined"):
                lines.append(f"      INSTALLED BUT NOT RUNNING: {info['quarantined']}")
                continue
            caps = info.get("capabilities") or []
            lines.append(f"      {len(caps)} capability(ies): "
                         f"{', '.join(caps[:5])}" if caps
                         else "      it declared no capabilities")
            if info.get("settings"):
                lines.append(f"      settings: {', '.join(info['settings'][:5])}")
            lines += _provision_lines(out.get("provisioned", {}).get(d["name"]))
        return "\n".join(lines) or "  nothing installed"

    def verify(self, proposal, result):
        """It imports, its capabilities are really in the catalogue, and what it needs is
        actually there. Not "the files are on disk" — that is what was just done, not
        evidence that it worked.

        The requirement half matters most: a plugin that installed but cannot import its
        library is exactly the failure this capability used to hand back as a success,
        and the report of it would arrive days later from whoever first ran a capability.
        """
        ok, evidence = Plugin.verify(self, proposal, result)
        if not ok:
            return (ok, evidence)
        from ...core import registry
        bad = [name for name, info in (result.get("declared") or {}).items()
               if info.get("quarantined")]
        if bad:
            return (False, f"installed but quarantined: {', '.join(bad)}")
        missing = [d["name"] for d in result.get("installed") or []
                   if d["name"] not in registry.RUNTIME.plugins]
        if missing:
            return (False, f"installed and not loaded: {', '.join(missing)}")
        unmet = [f"{name}: {f['name']}"
                 for name, r in (result.get("provisioned") or {}).items()
                 for f in (r.get("failed") or [])]
        if unmet:
            return (False, "installed, but what it needs is not there: "
                           + ", ".join(unmet[:4]))
        n = sum(len(v.get("capabilities") or [])
                for v in (result.get("declared") or {}).values())
        fixed = sum(len(r.get("repaired") or [])
                    for r in (result.get("provisioned") or {}).values())
        return (True, f"{len(result.get('installed') or [])} plugin(s) loaded, "
                      f"{n} capability(ies) in the catalogue"
                      + (f", {fixed} requirement(s) provisioned" if fixed else ""))


class PluginUpdate(Plugin):
    """Re-fetch an installed plugin at whatever its catalogue now points to."""
    name = "plugin.update"
    description = "Update an installed plugin.  name=<plugin>"
    confirm = True
    background = True
    aliases = {"plugin": "name"}
    reason = "replaces installed code with a newer copy of it"
    verified_by = "the plugin still imports and its capabilities are still there"

    def plan(self, name: str = ""):
        from ...core import catalogue, registry
        wanted = (name or "").strip()
        held = registry.provenance().get(wanted)
        if not held:
            raise ContractViolation(
                f"'{wanted}' was not installed by finrail, so there is nothing to "
                f"update it from (finrail get plugins.list)")
        order, missing = catalogue.resolve([wanted])
        if missing:
            raise ContractViolation(
                f"'{wanted}' is installed but no catalogue lists it any more — it "
                f"cannot be updated, only removed")
        entry = next(e for e in order if e["name"] == wanted)
        by_name = {s["name"]: s for s in catalogue.declared()}
        return {"name": wanted, "entry": entry,
                "origin": catalogue.origin(entry, by_name[entry["catalogue"]]),
                "held": held, "into": str(registry.installed_root())}

    def render(self, p) -> str:
        o, held = p["origin"], p["held"]
        return (f"UPDATE  {p['name']}\n"
                f"  from {held.get('commit', '?')[:12]} "
                f"(installed {_ago(held.get('at'))})\n"
                f"  to whatever {o['repo']}"
                + (f" @{o['ref']}" if o["ref"] else " @default branch")
                + " now points at\n"
                f"  the new code is fetched and checked before the old is replaced")

    def execute(self, p) -> dict:
        import pathlib as _p
        import time
        from ...core import catalogue, registry
        import shutil
        import tempfile
        from ...core import plugin_deploy
        by_name = {s["name"]: s for s in catalogue.declared()}
        # STAGED AND LOADED BEFORE THE OLD COPY IS TOUCHED. This fetched straight over
        # the installed directory, and on 6 Oct an update approved in chat put a production
        # in place that the running finrail could not load; the next turn failed in
        # dispatch. A copy that will not load now refuses here and the old one stays.
        work = _p.Path(tempfile.mkdtemp(prefix="finrail-plugin-update-"))
        try:
            got = catalogue.fetch(p["entry"], by_name[p["entry"]["catalogue"]],
                                  work / p["name"])
            plugin_deploy.refuse_if_incompatible(work, [p["name"]])
            plugin_deploy.swap(p["name"], work / p["name"])
        finally:
            shutil.rmtree(work, ignore_errors=True)
        registry.record_install(p["name"], dict(
            got, catalogue=p["entry"]["catalogue"], standard=p["entry"]["standard"],
            at=time.time()))
        registry.load(force=True)
        fault = next((q["fault"] for q in registry.quarantined()
                      if q["name"] == p["name"]), "")
        return {"name": p["name"], "was": p["held"].get("commit", ""),
                "now": got["commit"], "changed": got["commit"] != p["held"].get("commit"),
                "quarantined": fault, "ok": not fault}

    def render_data(self, out) -> str:
        if out["quarantined"]:
            return (f"  {out['name']} updated to {out['now'][:12]} and is NOT running: "
                    f"{out['quarantined']}")
        if not out["changed"]:
            return f"  {out['name']} was already at {out['now'][:12]} — nothing changed"
        return f"  {out['name']}: {out['was'][:12]} -> {out['now'][:12]}"


class PluginUninstall(Plugin):
    """Remove an installed plugin's code."""
    name = "plugin.uninstall"
    description = "Remove an installed plugin.  name=<plugin>"
    confirm = True
    aliases = {"plugin": "name"}
    reason = "deletes the plugin's code; anything relying on its capabilities stops"
    verified_by = "the code is gone and its capabilities are out of the catalogue"

    def plan(self, name: str = ""):
        from ...core import catalogue, registry
        wanted = (name or "").strip()
        here = {p["name"]: p for p in registry.installed()}
        quarantined = {q["name"] for q in registry.quarantined()}
        if wanted not in here and wanted not in quarantined:
            raise ContractViolation(f"'{wanted}' is not installed")
        if wanted in here and here[wanted]["kind"] != "installed":
            raise ContractViolation(
                f"'{wanted}' is a {here[wanted]['kind']} plugin that ships with "
                f"finrail — disable it instead (finrail do plugin.disable "
                f"name={wanted})")
        # Anything else that NEEDS it. Removing a dependency out from under its
        # dependents leaves them importing something that is gone, which surfaces as the
        # dependent being broken rather than as this having been a bad idea.
        index = {p["name"]: p for p in catalogue.available()["plugins"]}
        dependents = sorted(n for n, e in index.items()
                            if wanted in e["requires"] and n in here)
        caps = sorted(c.name for c in registry.RUNTIME.commands.values()
                      if getattr(c, "bundle", "") == wanted)
        return {"name": wanted, "dependents": dependents, "capabilities": caps,
                "where": str(registry.installed_root() / wanted),
                "provenance": registry.provenance().get(wanted, {})}

    def render(self, p) -> str:
        lines = [f"REMOVE  {p['name']}", f"  deletes {p['where']}"]
        if p["capabilities"]:
            lines.append(f"  losing: {', '.join(p['capabilities'][:6])}")
        if p["dependents"]:
            lines.append(f"  {', '.join(p['dependents'])} declare(s) a dependency on it "
                         f"and will stop working")
        lines.append("  reinstalling fetches it again; nothing here is kept")
        return "\n".join(lines)

    def execute(self, p) -> dict:
        import pathlib as _p
        import shutil
        from ...core import registry
        target = _p.Path(p["where"])
        if target.is_dir():
            shutil.rmtree(target)
        registry.forget_install(p["name"])
        registry.load(force=True)
        return {"name": p["name"], "gone": not target.exists(),
                "lost": p["capabilities"], "ok": not target.exists()}

    def render_data(self, out) -> str:
        return (f"  {out['name']} removed"
                + (f" — lost {', '.join(out['lost'][:6])}" if out["lost"] else ""))

    def verify(self, proposal, result):
        ok, evidence = Plugin.verify(self, proposal, result)
        if not ok:
            return (ok, evidence)
        from ...core import registry
        still = [c for c in proposal["capabilities"]
                 if c in registry.RUNTIME.commands]
        if still:
            return (False, f"still in the catalogue: {', '.join(still[:3])}")
        return (True, f"the code is gone and {len(proposal['capabilities'])} "
                      f"capability(ies) with it")


def _ago(when) -> str:
    import time
    if not when:
        return "at an unknown time"
    hours = (time.time() - float(when)) / 3600
    return f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f} days ago"


def _provision_lines(report) -> list:
    """What provisioning did, for a person reading an install or an enable."""
    if not report:
        return []
    out = []
    for d in report.get("repaired") or []:
        out.append(f"      provided {d['name']}"
                   + (f" — {d['note']}" if d.get("note") else ""))
    for f in report.get("failed") or []:
        out.append(f"      COULD NOT PROVIDE {f['name']}: {f.get('error', '')[:90]}")
    for sk in report.get("needs_you") or []:
        out.append(f"      needs a person: {sk['name']} — {sk.get('fix', '')[:80]}")
    return out


class PluginEnable(Plugin):
    """Turn a plugin on, and give it what it needs to actually run.

    Enabling used to be a line in a settings file. A plugin switched on without its
    libraries is on in the catalogue and broken at the first call, and the two facts sit
    far enough apart that nobody connects them — so enabling provisions, in the same
    interpreter, as part of the same act.
    """
    name = "plugin.enable"
    description = "Turn on an installed extension, and provide what it needs."
    confirm = True
    reason = ("changes which capabilities, settings and hooks exist, and installs the "
              "libraries the plugin declares into finrail's own interpreter")

    def plan(self, name: str = ""):
        info = dict(registry.RUNTIME.installed.get(name) or {})
        if not info:
            from ...core.errors import ContractViolation
            raise ContractViolation(f"no installed plugin '{name}'")
        return {"name": name, **info}

    def render(self, p) -> str:
        return (f"ENABLE {p['name']}  — {p['description']}\n"
                f"  anything it declares and this interpreter does not have is installed "
                f"into {sys.executable}")

    def execute(self, p) -> dict:
        out = registry.enable(p["name"])
        # AFTER the enable, not before: a disabled plugin contributes no requirements,
        # so asking for them first asks about nothing.
        out["provisioned"] = provision.provision(p["name"])
        return out

    def render_data(self, out) -> str:
        gained = out.get("gained") or []
        lines = [f"{out['plugin']} enabled" +
                 (f" — gained: {', '.join(gained)}" if gained else "")]
        lines += _provision_lines(out.get("provisioned"))
        return "\n".join(lines)

    def verify(self, proposal, result):
        """On, and able to run. A plugin enabled without its libraries is the same
        failure as one installed without them, one step later."""
        ok, evidence = Plugin.verify(self, proposal, result)
        if not ok:
            return (ok, evidence)
        failed = (result.get("provisioned") or {}).get("failed") or []
        if failed:
            return (False, "enabled, but what it needs is not there: "
                           + ", ".join(f["name"] for f in failed[:4]))
        return (True, f"{result['plugin']} is loaded and provisioned")


class PluginDisable(Plugin):
    name = "plugin.disable"
    description = "Turn off an installed extension. Bundled plugins cannot be disabled."
    confirm = True
    reason = "removes capabilities, settings and hooks from the running system"

    def plan(self, name: str = ""):
        info = dict(registry.RUNTIME.installed.get(name) or {})
        if not info:
            from ...core.errors import ContractViolation
            raise ContractViolation(f"no installed plugin '{name}'")
        if info.get("kind") == "bundled":
            from ...core.errors import ContractViolation
            raise ContractViolation(
                f"'{name}' is bundled — required for a working system, cannot be disabled")
        return {"name": name, **info}

    def render(self, p) -> str:
        return f"DISABLE {p['name']}  — {p['description']}"

    def execute(self, p) -> dict:
        return registry.disable(p["name"])

    def render_data(self, out) -> str:
        lost = out.get("lost") or []
        return (f"{out['plugin']} disabled" +
                (f" — lost: {', '.join(lost)}" if lost else ""))


class Tidy(Plugin):
    """Make the enabled set match what this machine can actually run."""
    name = "plugins.tidy"
    description = ("Disable installed extensions this host cannot run — the ones whose "
                   "unmet requirements need a person who is never going to come.")
    confirm = True
    reason = "removes capabilities, settings and hooks for every plugin it turns off"
    also_known_as = ("tidy the plugins", "disable what is not configured")

    def plan(self):
        """Which plugins are enabled here and cannot work.

        EXTENSIONS ARE OPT-OUT: every one shipped in the tree is live unless it is listed
        as disabled. So a host that does mail and chat also carries dns, ingress, tasks,
        music and the rest, each declaring requirements for a feature nobody here uses.
        That has two costs and the second is the expensive one. Doctor fills with red
        that is always there, which teaches everyone to skim past the line that matters.
        And a deploy has to reason about surfaces this machine does not have — which is
        how an upgrade came to rewrite a mail proxy's config on a host, from credentials
        it could not read, and stopped the mailbox.

        The signal is a requirement NOBODY CAN REPAIR: a credential to be provided, a
        setting to be chosen, a folder to be named. A missing library is not that — it
        gets installed. An unmet thing that needs a person, on a host where no person
        ever intends to supply it, means the plugin is not in use here.
        """
        from ...core import provision
        rows = provision.check()
        unusable = {}
        for r in rows:
            name = r.get("plugin") or ""
            info = registry.RUNTIME.installed.get(name) or {}
            if not name or info.get("kind") == "bundled" or not info.get("enabled", True):
                continue
            if not r["ok"] and not r["repairable"]:
                unusable.setdefault(name, []).append(f"{r['name']}: {r['detail']}")
        return {"disable": sorted(unusable), "why": unusable}

    def render(self, p) -> str:
        if not p["disable"]:
            return "every enabled plugin is usable on this host — nothing to tidy"
        lines = ["DISABLE, because nothing here can satisfy them:"]
        for name in p["disable"]:
            lines.append(f"  {name}")
            lines += [f"      {w}" for w in p["why"][name][:3]]
        lines.append("  their code stays installed; `finrail do plugin.enable name=<x>` "
                     "brings one back the moment it is configured")
        return "\n".join(lines)

    def execute(self, p) -> dict:
        off = []
        for name in p["disable"]:
            try:
                off.append(registry.disable(name))
            except Exception as e:                                 # noqa: BLE001
                off.append({"plugin": name, "error": str(e)[:160]})
        return {"disabled": off}

    def render_data(self, out) -> str:
        rows = out.get("disabled") or []
        if not rows:
            return "nothing to disable"
        return "\n".join(
            f"  {r.get('plugin')}: " + (r["error"] if r.get("error")
                                        else f"off — lost {len(r.get('lost') or [])} capability(ies)")
            for r in rows)


class Setup(Plugin):
    name = "setup"
    description = "Guided first run: what to configure, in order, with the commands."
    read_only = True
    confirm = False

    def run(self):
        d = provision.doctor()
        steps, auto = [], []
        for c in d["checks"]:
            if c["ok"]:
                continue
            (auto if c["repairable"] else steps).append(c)
        return {"ok": d["ok"], "total": d["total"], "failing": d["failing"],
                "you": steps, "finrail_can_fix": auto,
                "state_dir": str(core_settings.state()),
                "content_dir": str(core_settings.home())}

    def render_data(self, out) -> str:
        if out["ok"]:
            return ("Everything is configured and doctor is green.\n"
                    "  what exists:  finrail capabilities\n"
                    "  try a read:   finrail get tasks.list")
        lines = [f"Setup — {out['failing']} of {out['total']} checks still need work.",
                 f"  state:   {out['state_dir']}",
                 f"  content: {out['content_dir']}", ""]
        n = 0
        if out["you"]:
            lines.append("You need to do these (in this order):")
            for c in out["you"]:
                n += 1
                lines.append(f"  {n}. {c['name']}  —  {c['detail']}")
                lines.append(f"       {c['fix']}")
            lines.append("")
        if out["finrail_can_fix"]:
            lines.append("finrail can do these itself, once the above are in place:")
            for c in out["finrail_can_fix"]:
                lines.append(f"  •  {c['name']}  ({c['detail']})")
            lines.append("       finrail provision")
            lines.append("")
        lines.append("Then re-run:  finrail setup    (it shows only what is left)")
        return "\n".join(lines)


# ── writes ──────────────────────────────────────────────────────────────────
class Set(Plugin):
    name = "settings.set"
    verified_by = "the setting is read back from the store afterwards"
    description = "Set a setting.  finrail settings set <name>=<value>"
    confirm = True
    reason = "changes configuration every other capability reads"

    def plan(self, name: str = "", value: str = ""):
        spec = declared_settings().spec(name)
        return {"setting": name, "value": spec.coerce(value), "kind": spec.kind,
                "was": declared_settings().get(name), "plugin": spec.plugin}

    def render(self, p) -> str:
        return (f"SET {p['setting']} = {p['value']!r}\n"
                f"  was: {p['was']!r}\n  declared by: {p['plugin']}")

    def execute(self, p) -> dict:
        out = declared_settings().set(p["setting"], p["value"])
        # The STORE, not the value in force. Reading the latter verifies a write that
        # never happened whenever the value asked for is the declared default — the mirror
        # of the false FAILURE `Unset` used to report. `now` stays the effective value,
        # because that is the question the operator asked.
        after = declared_settings().stored(p["setting"])
        return {**out, "verified": after == p["value"],
                "now": declared_settings().get(p["setting"])}


class Unset(Plugin):
    name = "settings.unset"
    verified_by = "the setting is confirmed gone from the store"
    description = "Remove a setting's value."
    confirm = True
    reason = "removes configuration other capabilities may require"

    def plan(self, name: str = ""):
        declared_settings().spec(name)
        return {"setting": name, "was": declared_settings().get(name)}

    def render(self, p) -> str:
        return f"UNSET {p['setting']}  (was {p['was']!r})"

    def execute(self, p) -> dict:
        out = declared_settings().unset(p["setting"])
        # GONE FROM THE STORE, which is what this capability says it verifies. It asked
        # `get()` instead, which falls back to the declared default — so unsetting any of
        # the 54 settings that declare one was recorded as a failure while the value had
        # in fact been removed, and the operator was told their change had not happened.
        # `falls_back_to` is the other half of the answer: unsetting does not leave
        # nothing, it leaves the default, and that is what the next reader will see.
        return {**out, "verified": declared_settings().stored(p["setting"]) is None,
                "falls_back_to": declared_settings().get(p["setting"])}


COMMANDS = [Tidy, PluginSources, PluginsRefresh, PluginsAvailable,
            PluginInstall, PluginUpdate, PluginUninstall,
            Show, Plugins, PluginEnable, PluginDisable, Setup, Set, Unset]
