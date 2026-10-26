"""SECRETS — credentials, never in settings and never in argv.

Three rules the whole system depends on:

  1. A secret VALUE is never returned by anything that renders to the user. Capabilities
     read secrets at use-time and pass them straight to the thing that needs them.
  2. A secret is never accepted as a command-line argument (it would land in shell
     history and in the process table). `secret set` reads from stdin.
  3. Asking whether a secret EXISTS must not read it. `exists()` used to call `get()`, so
     every existence check pulled a credential into memory for nothing and the claim that
     finrail only touches a secret when injecting it was not quite true.

Connections, requirements and compose files store a REFERENCE — a name — never a value.

## References carry their backend

A reference is persisted: in `connections.json`, in a requirement, in an app's
`x-finrail` block. It is read back later, possibly on a machine whose stores are not the
same. So the store belongs in the reference:

    app/portfolio/DB_PASSWORD                     the default backend
    keychain:app/portfolio/DB_PASSWORD            the same thing, said out loud
    keychain:finrail.mail#user@example.com:proxy      another keychain service
    file:ci/deploy-token                          the 0600 file store

The rule is exactly one: **split on the first colon, and only when what precedes it names
a backend this build has.** A bare name means the default backend, so every reference
stored before this existed keeps working untouched — and a name that merely contains a
colon (`user@example.com:proxy-password` is a real one) is not mistaken for a prefix.

Within a backend, the key is that backend's own address space and core does not read it.
The keychain's is `[<service>#]<account>`, defaulting to the `finrail` service; `#` is
the separator rather than `/` because an account name may itself contain slashes —
`app/portfolio/DB_PASSWORD` is one — so `/` cannot mark where a service ends.

`FINRAIL_SECRET_BACKEND` pins every reference to one backend whatever prefix it carries.
That is what a single-store install wants, and it is what makes the test suite hermetic:
with it set to `file`, nothing in a test can reach the real keychain even by naming it.
"""
import json
import os
import pathlib
import shutil
import subprocess

from .errors import ContractViolation, NotConfigured
from .settings import ensure, state

#: The keychain service finrail's own credentials live under. Another service is named
#: in the reference (`keychain:<service>#<account>`), never threaded as a parameter.
SERVICE = "finrail"


class SecretBackend:
    """THE BACKEND CONTRACT — where a credential actually lives.

    The same shape as mail's provider slots and the apps runtime, because this codebase
    already answers this question three times: the thing that knows declares, and core
    stays general.
    """
    name = ""

    #: A read-only backend (CI environment variables, say) refuses put/delete and says so
    #: rather than appearing to succeed.
    writable = True

    def get(self, key: str) -> str:
        raise NotImplementedError

    def put(self, key: str, value: str) -> None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        """MUST NOT read the value to answer this."""
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def names(self) -> list:
        """What is stored here, where that is knowable. The keychain is not enumerable
        by design, so it returns nothing rather than pretending to be empty."""
        return []

    def test(self) -> tuple:
        """(ok, detail) — is this backend usable on this machine at all?"""
        return (True, self.name)

    def __repr__(self):
        return f"<SecretBackend {self.name}>"


class Keychain(SecretBackend):
    """macOS login keychain, via `security`. Hardware-backed and the default here."""
    name = "keychain"

    def _addr(self, key: str) -> tuple:
        service, sep, account = key.partition("#")
        return (service, account) if sep else (SERVICE, key)

    def _run(self, args: list, capture: bool = True):
        try:
            return subprocess.run(["security", *args], capture_output=capture, text=True)
        except FileNotFoundError:
            # A reference can name a backend this machine does not have — a
            # `keychain:` ref carried over from a Mac, read on Linux. Say that, rather
            # than letting a FileNotFoundError about a program nobody mentioned surface
            # from underneath a credential lookup.
            raise NotConfigured(
                "this reference names the keychain backend, which needs macOS's "
                "`security` and it is not on PATH here",
                "store it in this machine's default backend instead, or set "
                "FINRAIL_SECRET_BACKEND to pin one")

    def get(self, key: str) -> str:
        service, account = self._addr(key)
        r = self._run(["find-generic-password", "-s", service, "-a", account, "-w"])
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.rstrip("\n")
        raise NotConfigured(f"secret '{key}'", f"finrail secret set {key}")

    def put(self, key: str, value: str) -> None:
        service, account = self._addr(key)
        self._run(["delete-generic-password", "-s", service, "-a", account])
        r = self._run(["add-generic-password", "-s", service, "-a", account,
                       "-w", value])
        if r.returncode != 0:
            raise NotConfigured(f"secret '{key}'",
                                f"keychain refused: {r.stderr[:120]}")

    def exists(self, key: str) -> bool:
        """Without `-w`, so the value is never decrypted, never returned by `security`
        and never in this process's memory. `security` exits 0 when the item is there and
        44 when it is not; the attributes it prints do not include the password."""
        service, account = self._addr(key)
        return self._run(["find-generic-password", "-s", service,
                          "-a", account]).returncode == 0

    def delete(self, key: str) -> None:
        service, account = self._addr(key)
        self._run(["delete-generic-password", "-s", service, "-a", account])

    def test(self) -> tuple:
        if not shutil.which("security"):
            return (False, "`security` is not on PATH — this is not a macOS keychain host")
        r = self._run(["list-keychains", "-d", "user"])
        if r.returncode != 0:
            return (False, f"keychain unreachable: {(r.stderr or '').strip()[:100]}")
        return (True, "macOS login keychain")


class FileStore(SecretBackend):
    """A 0600 JSON file under the state directory.

    What a Linux host and the test suite use. Honest about what it is: the values are
    readable by anyone who can read the file, which is you and root. It exists so that a
    machine without a platform keystore still has ONE place credentials live, rather than
    scattering them through config files.
    """
    name = "file"

    def _path(self) -> pathlib.Path:
        return state() / "secrets.json"

    def _read(self) -> dict:
        p = self._path()
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        ensure()
        p = self._path()
        p.write_text(json.dumps(data, indent=2, sort_keys=True))
        p.chmod(0o600)

    def get(self, key: str) -> str:
        v = self._read().get(key)
        if v:
            return v
        raise NotConfigured(f"secret '{key}'", f"finrail secret set {key}")

    def put(self, key: str, value: str) -> None:
        data = self._read()
        data[key] = value
        self._write(data)

    def exists(self, key: str) -> bool:
        """The one place the contract above cannot be kept literally.

        A single JSON file has no index: answering "is this key here?" means parsing the
        file, and parsing it brings every value with it. What survives is the part that
        matters — nothing is returned, nothing is decrypted (there is no encryption here),
        and the values leave memory with the dict. The keychain backend, which is the
        default wherever it exists, keeps it literally.
        """
        return bool(self._read().get(key))

    def delete(self, key: str) -> None:
        data = self._read()
        data.pop(key, None)
        self._write(data)

    def names(self) -> list:
        return sorted(self._read())

    def test(self) -> tuple:
        p = self._path()
        if p.exists() and p.stat().st_mode & 0o077:
            return (False, f"{p} is readable by others — chmod 600 it")
        return (True, str(p))


class VaultStore(SecretBackend):
    """HashiCorp Vault, KV v2, confined to one prefix.

    The only backend here that is not local to the machine, which changes what can
    go wrong: the server can be unreachable, sealed, or reachable with a token that
    is expired or too narrow. Every one of those is reported as itself, because
    "secret not found" for a sealed Vault sends you looking in the wrong place.

    ADDRESS SPACE. A key is a path under `<mount>/<prefix>/`, and the value lives in
    the field `value` of that document:

        finrail secret set mail/user      ->  secret/finrail/mail/user  {value: ...}
        vault:mail/user                    ->  the same thing, said out loud

    Slashes are the natural separator here, unlike the keychain's `#`, because a
    Vault path is already a path. Existing names with slashes in them
    (`app/portfolio/DB_PASSWORD`) map across unchanged.

    THE TOKEN. finrail is not given Vault's root token. It gets one carrying a
    policy that can reach `secret/{data,metadata}/finrail/*` and nothing else, so
    the blast radius of the agent's token is the agent's own secrets — see
    `finrail-policy.hcl` next to this repo's Vault notes. The token is read from a
    file rather than an environment variable wherever possible: a token in the
    environment is visible in `/proc/<pid>/environ` to anyone who can read it, and
    is inherited by every child process the agent spawns.
    """
    name = "vault"

    #: KV v2 keeps the value and the metadata at different paths. That separation is
    #: what lets `exists()` honour the contract literally: it reads metadata, so the
    #: value is never decrypted, never sent over the wire and never in this process.
    DATA, META = "data", "metadata"

    #: The field the value is stored under. One field, always, so a secret written by
    #: finrail is readable by anything else that knows the convention.
    FIELD = "value"

    def _conf(self, setting: str, *env: str, default: str = "") -> str:
        """Configuration, in the order a host actually wants it resolved.

        Environment first so a systemd unit can pin it and a test can isolate it;
        then the settings file, which is where an operator writes it down; then
        Vault's own conventions, so a machine already configured for the `vault` CLI
        needs nothing said twice.
        """
        for var in env:
            v = os.environ.get(var, "").strip()
            if v:
                return v
        try:                                   # lazily: registry loads plugins, and a
            from . import registry              # plugin may itself read a secret
            v = registry.settings().get(setting)
            if v:
                return str(v).strip()
        except Exception:                                          # noqa: BLE001
            pass
        return default

    def addr(self) -> str:
        return self._conf("vault.addr", "FINRAIL_VAULT_ADDR", "VAULT_ADDR",
                          default="https://127.0.0.1:8200").rstrip("/")

    def mount(self) -> str:
        return self._conf("vault.mount", "FINRAIL_VAULT_MOUNT", default="secret").strip("/")

    def prefix(self) -> str:
        return self._conf("vault.prefix", "FINRAIL_VAULT_PREFIX", default="finrail").strip("/")

    def token(self) -> str:
        """The token, from a file if we can, from the environment if we must."""
        direct = self._conf("", "FINRAIL_VAULT_TOKEN", "VAULT_TOKEN")
        path = self._conf("vault.token_file", "FINRAIL_VAULT_TOKEN_FILE")
        for candidate in (path, "~/.vault-token"):
            if not candidate:
                continue
            f = pathlib.Path(candidate).expanduser()
            try:
                if f.is_file():
                    tok = f.read_text().strip()
                    if tok:
                        return tok
            except OSError:
                pass
        if direct:
            return direct
        raise NotConfigured(
            "no Vault token",
            "write one to the file named by `finrail settings set vault.token_file=<path>`, "
            "or set FINRAIL_VAULT_TOKEN")

    def _key(self, key: str) -> str:
        """A key, or a refusal. Never a path that leaves the prefix.

        `../chat/owner` builds `secret/data/finrail/../chat/owner`, and an HTTP
        stack that normalises the URL turns that into `secret/data/chat/owner` —
        another application's credentials, reached through a secret NAME.

        The scoped token denies that today, and that is the containment that
        matters. This is the second lock: containment should not depend on a
        policy someone might one day widen, and a name that tries to climb out is
        a bug worth reporting rather than quietly failing with 403.
        """
        clean = str(key).strip("/")
        if not clean:
            raise ContractViolation("secret: the name cannot be empty")
        if any(part in ("", ".", "..") for part in clean.split("/")):
            raise ContractViolation(
                f"secret name '{key}' contains a path segment that would leave "
                f"{self.mount()}/{self.prefix()} — names are relative to it, always")
        return clean

    def _url(self, kind: str, key: str = "") -> str:
        parts = [self.addr(), "v1", self.mount(), kind, self.prefix()]
        if key:
            parts.append(self._key(key))
        return "/".join(p.strip("/") for p in parts if p != "")

    def _context(self):
        import ssl
        ca = self._conf("vault.cacert", "FINRAIL_VAULT_CACERT", "VAULT_CACERT")
        if ca and pathlib.Path(ca).expanduser().is_file():
            return ssl.create_default_context(cafile=str(pathlib.Path(ca).expanduser()))
        return ssl.create_default_context()

    def _call(self, method: str, url: str, payload: dict = None, token: str = "") -> dict:
        """One Vault request. Returns the parsed body, or {} for 404.

        Every failure mode is translated here rather than left as an HTTPError, so a
        caller reading a credential gets a sentence it can act on.
        """
        import urllib.error
        import urllib.request
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("X-Vault-Token", token or self.token())
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10, context=self._context()) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            if e.code == 403:
                raise NotConfigured(
                    f"Vault refused this path ({url.split('/v1/')[-1]})",
                    "the token's policy does not cover it — finrail's token is scoped to "
                    f"{self.mount()}/{self.prefix()}/*")
            if e.code in (429, 472, 473, 501, 503):
                raise NotConfigured(
                    "Vault is sealed or standby, so no secret can be read",
                    "unseal it (on this host: sudo vault-unseal) and try again")
            detail = ""
            try:
                detail = "; ".join(json.loads(e.read()).get("errors") or [])[:160]
            except Exception:                                      # noqa: BLE001
                pass
            raise NotConfigured(f"Vault returned {e.code}", detail or str(e)[:160])
        except urllib.error.URLError as e:
            raise NotConfigured(
                f"cannot reach Vault at {self.addr()} ({e.reason})",
                "is the address right, and is the port-forward up? "
                "kubectl -n vault port-forward svc/vault 8200:8200")

    # ── the contract ────────────────────────────────────────────────────────
    def get(self, key: str) -> str:
        key = self._key(key)
        out = self._call("GET", self._url(self.DATA, key))
        fields = (((out.get("data") or {}).get("data")) or {})
        value = self._value(key, fields)
        if value:
            return str(value)
        raise NotConfigured(f"secret '{key}'", f"finrail secret set {key}")

    def _value(self, key: str, fields: dict):
        """The value, from whichever field holds it.

        finrail WRITES `value`, always. It does not insist on reading it, because
        Vault is a shared store: a person putting a token in by hand writes
        `vault kv put secret/finrail/gitea-token token=...`, and a secret that
        `exists()` can see but `get()` cannot read is the worst of both — the check
        passes and the credential fails at the moment it is used.

        So: `value` if present, otherwise the only field there is. Two or more
        fields and none of them `value` is genuinely ambiguous, and guessing which
        one is a credential is not a thing to do quietly.
        """
        if not fields:
            return None
        if self.FIELD in fields:
            return fields[self.FIELD]
        if len(fields) == 1:
            return next(iter(fields.values()))
        raise ContractViolation(
            f"secret '{key}' has several fields ({', '.join(sorted(fields))}) and none "
            f"is '{self.FIELD}', so which one is the credential is a guess. Rewrite it "
            f"with `finrail secret set {key}`, or as {self.FIELD}=<the credential>")

    def put(self, key: str, value: str) -> None:
        key = self._key(key)
        self._call("POST", self._url(self.DATA, key), {"data": {self.FIELD: value}})

    def exists(self, key: str) -> bool:
        """Reads METADATA, never the value — the contract kept literally."""
        key = self._key(key)
        out = self._call("GET", self._url(self.META, key))
        data = out.get("data") or {}
        if not data:
            return False
        # A soft-deleted secret still has metadata. It is not there any more.
        versions = data.get("versions") or {}
        current = str(data.get("current_version", ""))
        info = versions.get(current) or {}
        return not (info.get("destroyed") or info.get("deletion_time"))

    def delete(self, key: str) -> None:
        """Removes every version and the metadata.

        Not a KV v2 soft delete, deliberately: that leaves the value recoverable and
        `exists()` answering no, which is a secret that is gone for the person asking
        and present for anyone with the API. `finrail secret delete` means gone.
        """
        key = self._key(key)
        self._call("DELETE", self._url(self.META, key))

    def names(self) -> list:
        """Every key under the prefix, walked recursively.

        The only enumerable backend here — the keychain cannot be listed by design
        and the file store lists one flat file — which is what makes
        `finrail secret.needed` able to say what is present rather than only what
        was asked for.
        """
        found, seen = [], set()

        def walk(rel: str):
            if rel in seen or len(seen) > 500:      # bounded: a cycle cannot happen, a
                return                              # very large tree should still return
            seen.add(rel)
            url = self._url(self.META, rel) + "?list=true"
            try:
                out = self._call("GET", url)
            except NotConfigured:
                return
            for entry in ((out.get("data") or {}).get("keys") or []):
                child = f"{rel}/{entry}".strip("/") if rel else entry
                if entry.endswith("/"):
                    walk(child.rstrip("/"))
                else:
                    found.append(child)

        walk("")
        return sorted(found)

    def test(self) -> tuple:
        """Reachable, unsealed, and holding a token that can actually do the job."""
        try:
            token = self.token()
        except NotConfigured as e:
            return (False, str(e))
        try:
            out = self._call("GET", f"{self.addr()}/v1/auth/token/lookup-self", token=token)
        except NotConfigured as e:
            return (False, str(e))
        data = out.get("data") or {}
        if not data:
            return (False, "Vault did not recognise the token")
        ttl = int(data.get("ttl") or 0)
        policies = ", ".join(p for p in (data.get("policies") or []) if p != "default")
        where = f"{self.addr()} {self.mount()}/{self.prefix()}"
        if ttl and ttl < 3600 and not data.get("renewable"):
            return (False, f"{where} — token expires in {ttl // 60} min and is not renewable")
        life = "does not expire" if not ttl else f"{ttl // 3600}h left"
        return (True, f"{where} — token [{policies or 'default'}], {life}")

    def renew(self) -> dict:
        """Extend the token's lease. A periodic token renewed while it is alive never
        expires; one left alone dies quietly and every secret read starts failing at
        once, which is the failure this exists to avoid."""
        out = self._call("POST", f"{self.addr()}/v1/auth/token/renew-self", {})
        auth = out.get("auth") or {}
        return {"renewed": bool(auth), "ttl": auth.get("lease_duration", 0)}


BACKENDS = {"keychain": Keychain, "file": FileStore, "vault": VaultStore}


# ── references ──────────────────────────────────────────────────────────────
def pinned() -> str:
    """The backend this process is pinned to, if any."""
    return os.environ.get("FINRAIL_SECRET_BACKEND", "").strip()


def default_name() -> str:
    return pinned() or ("keychain" if shutil.which("security") else "file")


def split(ref: str) -> tuple:
    """(backend name, key). A bare name is the default backend."""
    head, sep, rest = str(ref).partition(":")
    if sep and head in BACKENDS:
        return (head, rest)
    return (default_name(), str(ref))


def backend(name: str = "") -> SecretBackend:
    name = pinned() or name or default_name()
    cls = BACKENDS.get(name)
    if cls is None:
        raise ContractViolation(
            f"unknown secret backend '{name}' (this build has: "
            f"{', '.join(sorted(BACKENDS))})")
    return cls()


def resolve(ref: str) -> tuple:
    """(backend, key) for a reference."""
    name, key = split(ref)
    return (backend(name), key)


class SecretRef:
    """What a connection persists instead of a credential."""

    def __init__(self, name: str):
        self.name = name

    def resolve(self) -> str:
        return get(self.name)

    def to_json(self) -> dict:
        return {"secret": self.name}

    def __repr__(self):
        return f"<SecretRef {self.name}>"


# ── the API every caller uses ───────────────────────────────────────────────
def exists(name: str) -> bool:
    store, key = resolve(name)
    return store.exists(key)


def get(name: str) -> str:
    """The value, or NotConfigured with the exact fix. Never returns "" for missing — an
    empty string that looks like a credential is how silent auth failures happen."""
    store, key = resolve(name)
    return store.get(key)


def put(name: str, value: str) -> dict:
    """Store a secret. Callers must have read the value from stdin, not argv."""
    if not value:
        raise NotConfigured(f"secret '{name}'", "a secret cannot be empty")
    store, key = resolve(name)
    if not store.writable:
        raise ContractViolation(
            f"the '{store.name}' backend is read-only, so '{name}' cannot be stored "
            f"through finrail — it has to be provided where that backend reads from")
    store.put(key, value)
    return {"secret": name, "backend": store.name, "stored": True}   # never the value


def delete(name: str) -> dict:
    store, key = resolve(name)
    if not store.writable:
        raise ContractViolation(
            f"the '{store.name}' backend is read-only, so '{name}' cannot be deleted "
            f"through finrail")
    store.delete(key)
    return {"secret": name, "backend": store.name, "deleted": True}


def names() -> list:
    """Known secret names, where the backend can enumerate them at all."""
    return backend().names()


def report() -> list:
    """Every backend and whether it is usable here — for doctor and `secret.check`."""
    out = []
    for name in sorted(BACKENDS):
        ok, detail = BACKENDS[name]().test()
        out.append({"backend": name, "ok": ok, "detail": detail,
                    "default": name == default_name()})
    return out
