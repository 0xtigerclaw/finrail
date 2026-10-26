"""SECRETS plugin — credentials, without ever putting one in argv.

`secret set` reads the value from stdin. A password on a command line lands in shell
history and in the process table, which is why the capability refuses a value passed as
a parameter.
"""
import sys

from ...core import secrets
from ...core.errors import ContractViolation
from ...core.requirements import Manual
from ...core.settings import SettingSpec
from ...plugin import Plugin

NAME = "secrets"
DESCRIPTION = "Credential storage, one interface over several backends."

#: Where the `vault` backend looks. Declared rather than hardcoded because these are
#: facts about a HOST, not about finrail: the address changes between a port-forward
#: and a real endpoint, and the mount and prefix are decisions someone made when they
#: laid out their Vault.
#:
#: None of these is a secret, which is why they belong in settings.json at all. The
#: token does not appear here — `vault.token_file` names a file, so the token itself
#: never lands in a config file, in argv, or in the environment of every child process
#: the agent spawns.
SETTINGS = [
    SettingSpec("vault.addr", "str", plugin=NAME,
                doc="Vault API address, e.g. https://127.0.0.1:8200"),
    SettingSpec("vault.mount", "str", plugin=NAME, default="secret",
                doc="KV v2 mount point"),
    SettingSpec("vault.prefix", "str", plugin=NAME, default="finrail",
                doc="Path under the mount that finrail owns; its token reaches nothing else"),
    SettingSpec("vault.token_file", "path", plugin=NAME,
                doc="File holding the Vault token (0600). Preferred over an env var, "
                    "which is readable in /proc and inherited by every child process"),
    SettingSpec("vault.cacert", "path", plugin=NAME,
                doc="CA certificate for Vault's TLS, when it is not publicly trusted"),
]


def REQUIREMENTS():
    """The store credentials live in has to be usable, and that is not free of failure
    modes: a `keychain:` reference read on a host with no `security`, or a file store
    left world-readable. Both were previously discovered at the moment something needed
    a credential — which is the worst moment to discover them."""
    return [Manual("secret-backend", probe=_backend_state,
                   hint="set FINRAIL_SECRET_BACKEND to a backend this machine has, "
                        "or fix what the check reports")]


def _backend_state() -> tuple:
    ok, detail = secrets.backend().test()
    return (ok, f"{secrets.default_name()}: {detail}")


class Check(Plugin):
    name = "secret.check"
    description = "Whether a secret is present. Never shows the value."
    read_only = True
    confirm = False

    def run(self, name: str = ""):
        backends = secrets.report()
        if name:
            store, key = secrets.resolve(name)
            return {"secrets": [{"name": name, "present": secrets.exists(name),
                                 "backend": store.name, "key": key}],
                    "backends": backends}
        return {"secrets": [{"name": n, "present": True,
                             "backend": secrets.default_name(), "key": n}
                            for n in secrets.names()],
                "backends": backends}

    def render_data(self, out) -> str:
        lines = []
        for b in out["backends"]:
            lines.append(f"  {'✓' if b['ok'] else '✗'} backend {b['backend']}"
                         + ("  (default)" if b["default"] else "")
                         + f" — {b['detail']}")
        rows = out["secrets"]
        if not rows:
            default = next((b["backend"] for b in out["backends"] if b["default"]), "?")
            lines.append(f"\n  (nothing to list in the {default} backend — the keychain "
                         f"is not enumerable by design; check one by name)")
        else:
            lines.append("")
            lines += [f"  {'✓' if r['present'] else '✗'} {r['name']}   "
                      f"[{r['backend']}: {r['key']}]" for r in rows]
        return "\n".join(lines)


class Set(Plugin):
    """Store a secret. The value is read from stdin or typed with no echo — never argv.

    `prompt=true` is the better of the two and the reason it exists: a pipeline puts the
    value in your shell's history, and `printf '%s' '...' | finrail ...` puts it in the
    process table of every process that looks while it runs. A no-echo prompt puts it
    nowhere except this process's memory.
    """
    name = "secret.set"
    verified_by = "the backend is asked whether the secret exists — never for its value"
    description = "Store a secret. The value is read from stdin, or typed with no " \
                  "echo (prompt=true). Never from argv."
    confirm = True
    reason = "writes a credential to the secret store"

    def plan(self, name: str = "", value: str = "", prompt: bool = False):
        if value:
            raise ContractViolation(
                "refusing a secret passed as a parameter — it would land in shell "
                f"history and the process table. Use:\n"
                f"  finrail do secret.set name={name} prompt=true      (typed, no echo)\n"
                f"  printf '%s' '<secret>' | finrail secret set {name}")
        if not name:
            raise ContractViolation("secret.set: which secret? (name=<name>)")
        store, key = secrets.resolve(name)
        return {"secret": name, "_replacing": secrets.exists(name),
                "backend": store.name, "stored_as": key,
                "how": "typed at a prompt, with no echo" if prompt else "read from stdin"}

    def render(self, p) -> str:
        verb = "REPLACE" if p["_replacing"] else "STORE"
        # Where the value is actually read matters, and it is not obvious: this is a
        # capability that needs approval, so `execute()` runs in the APPROVE command, not
        # in this one. `printf ... | finrail secret set NAME` therefore only proposes —
        # the value has to reach the approval. Said here because the alternative is
        # someone piping a credential twice and wondering why.
        return (f"{verb} secret '{p['secret']}'  ({p['how']})\n"
                f"  into the {p['backend']} backend, as {p['stored_as']}\n"
                f"  the value is not shown here, in the run record, or anywhere else\n"
                f"  the value is read when you APPROVE this, not now — "
                + ("approve it from a terminal and it will prompt with no echo"
                   if p["how"].startswith("typed") else
                   "pipe it to the approve command"))

    def execute(self, p) -> dict:
        value = (self._typed(p["secret"]) if p["how"].startswith("typed")
                 else (sys.stdin.read().strip() if not sys.stdin.isatty() else ""))
        if not value:
            raise ContractViolation(
                f"no value given. Use:\n"
                f"  finrail do secret.set name={p['secret']} prompt=true\n"
                f"  printf '%s' '<secret>' | finrail secret set {p['secret']}")
        out = secrets.put(p["secret"], value)
        return {**out, "verified": secrets.exists(p["secret"])}

    @staticmethod
    def _typed(name: str) -> str:
        """Read from the controlling terminal with echo off.

        The terminal is checked FIRST rather than left to `getpass`. Without one,
        `getpass` emits a warning and silently falls back to reading stdin — with echo
        on — so a `prompt=true` in a cron job or a pipeline would look like a no-echo
        prompt and behave like `input()`. Verified: it does exactly that.
        """
        import getpass
        try:
            tty = open("/dev/tty")
            tty.close()
        except OSError:
            raise ContractViolation(
                f"prompt=true needs a terminal and there is none here, and a prompt that "
                f"quietly reads stdin with echo on is worse than no prompt. Pipe it "
                f"instead: printf '%s' '<secret>' | finrail secret set {name}")
        return getpass.getpass(f"value for {name} (not echoed): ").strip()


class Delete(Plugin):
    name = "secret.delete"
    verified_by = "the backend is asked whether the secret is gone"
    description = "Remove a secret."
    confirm = True
    reason = "removes a credential other capabilities may need"

    def plan(self, name: str = ""):
        return {"secret": name, "present": secrets.exists(name)}

    def render(self, p) -> str:
        return f"DELETE secret '{p['secret']}'" + ("" if p["present"] else " (not set)")

    def execute(self, p) -> dict:
        out = secrets.delete(p["secret"])
        return {**out, "verified": not secrets.exists(p["secret"])}


COMMANDS = [Check, Set, Delete]


class Needed(Plugin):
    """Every credential the installed plugins declare, and whether it is set.

    Answers "what do I still have to provide?" without discovering it as a runtime
    failure. Reads the SAME declarations doctor walks, so the two cannot disagree — a
    plugin that declares a SecretRequirement shows up here automatically.
    """
    name = "secret.needed"
    description = "Which secrets the installed plugins require, and which are missing."
    read_only = True
    confirm = False

    def run(self, missing_only: bool = False):
        from ...core import provision
        rows = []
        for req in provision.requirements():
            if getattr(req, "kind", "") != "secret":
                continue
            try:
                ok, detail = req.check()
            except Exception as e:                                  # noqa: BLE001
                ok, detail = False, str(e)[:120]
            if missing_only and ok:
                continue
            rows.append({"name": getattr(req, "secret_name", req.name),
                         "set": bool(ok), "plugin": req.plugin,
                         "where": getattr(req, "where", ""),
                         "generated": bool(getattr(req, "generate", False)),
                         "why": getattr(req, "why", "") or req.name,
                         "detail": detail, "fix": "" if ok else req.fix()})
        return {"secrets": rows,
                "missing": sum(1 for r in rows if not r["set"]),
                "total": len(rows)}

    def render_data(self, out) -> str:
        rows = out["secrets"]
        if not rows:
            return ("(no plugin declares a required secret)\n"
                    "  Nothing is waiting on a credential.")
        lines = [f"{out['total'] - out['missing']}/{out['total']} required secrets are set"]
        for r in rows:
            mark = "✓" if r["set"] else "✗"
            where = f"  [{r['where']}]" if r.get("where") else ""
            lines.append(f"\n  {mark} {r['name']}{where}")
            if r["why"]:
                lines.append(f"      {r['why']}")
            if not r["set"]:
                lines.append(f"      fix: {r['fix']}")
        if any(r["generated"] and not r["set"] for r in rows):
            lines.append("\n(the ones finrail can create for itself: finrail provision)")
        if any(not r["generated"] and not r["set"] for r in rows):
            lines.append("\n(a value only its issuer can create is read from stdin, "
                         "never from the command line:"
                         "\n   printf '%s' '<secret>' | finrail secret set <name>"
                         "\n or, with no echo:  finrail do secret.set name=<name> "
                         "prompt=true)")
        return "\n".join(lines)


COMMANDS = COMMANDS + [Needed]
