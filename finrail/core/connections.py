"""CONNECTIONS — named, testable links to things outside this machine.

The generalisation of "the OAuth proxy", "IMAP credentials", "the Postgres URL", "the
Drive account". A plugin registers a connection TYPE (how to authorise, how to refresh,
how to test); core stores INSTANCES (settings + secret references, never secret values)
and turns every instance's `test()` into a doctor check automatically.

That last part is the point: "doctor confirms the connection works" is implemented once,
here, for every connection any plugin will ever add.
"""
import json

from .errors import ContractViolation, NotConfigured
from .settings import ensure, store


class ConnectionType:
    """How to talk to a class of external thing.

    name      "gmail-oauth", "imap", "gbrain", "gdrive"
    fields    setting names this type needs, e.g. ("host", "port", "user")
    secrets   secret names this type needs, e.g. ("password",)
    """
    name = ""
    fields: tuple = ()
    secrets: tuple = ()
    description = ""

    def __init__(self):
        self.plugin = ""

    def authorize(self, config: dict) -> dict:
        """Do whatever grants access; return config to persist. Default: nothing to do."""
        return config

    def refresh(self, conn) -> dict:
        """Renew an expiring credential. Default: nothing to renew."""
        return {}

    def test(self, conn) -> tuple:
        """(ok, detail). MUST actually touch the far end — a connection that reports
        healthy without a round trip is worse than no check at all."""
        raise NotImplementedError(f"{self.name}: no test()")

    def __repr__(self):
        return f"<ConnectionType {self.name}>"


class Connection:
    """One configured instance of a type."""

    def __init__(self, name: str, type_name: str, config: dict):
        self.name = name
        self.type_name = type_name
        self.config = dict(config or {})

    @property
    def type(self) -> ConnectionType:
        from .registry import load
        from .api import RUNTIME
        load()
        ct = RUNTIME.connection_types.get(self.type_name)
        if ct is None:
            raise ContractViolation(
                f"connection '{self.name}' has unknown type '{self.type_name}'")
        return ct

    def secret_ref(self, key: str) -> str:
        """The reference this connection stores for a credential.

        A reference carries its own store — `keychain:finrail.mail#a@b.com:proxy` reads
        a credential where something else enrolled it. `<key>_service` was the older way
        of saying the same thing, and it is still read so that a connection stored before
        this change keeps working; it is folded into the reference rather than threaded
        through as a second parameter.
        """
        from . import secrets
        ref = self.config.get(f"{key}_secret") or f"{self.name}.{key}"
        service = self.config.get(f"{key}_service")
        if service and secrets.split(ref)[1] == ref:
            return f"keychain:{service}#{ref}"
        return ref

    def secret(self, key: str) -> str:
        from . import secrets
        return secrets.get(self.secret_ref(key))

    def get(self, key: str, default=None):
        return self.config.get(key, default)

    def require(self, key: str):
        if key not in self.config:
            raise NotConfigured(
                f"connection '{self.name}' field '{key}'",
                f"finrail connection set {self.name} {key}=<value>")
        return self.config[key]

    def test(self) -> tuple:
        return self.type.test(self)

    def refresh(self) -> dict:
        return self.type.refresh(self)

    def to_json(self) -> dict:
        return {"name": self.name, "type": self.type_name, "config": self.config}

    def __repr__(self):
        return f"<Connection {self.name} ({self.type_name})>"


# ── the instance store ──────────────────────────────────────────────────────
def _path():
    return store() / "connections.json"


def _read() -> dict:
    p = _path()
    if not p.is_file():
        return {}
    return json.loads(p.read_text())


def _write(data: dict) -> None:
    ensure()
    _path().write_text(json.dumps(data, indent=2, sort_keys=True))


def types() -> dict:
    from .registry import load
    from .api import RUNTIME
    load()
    return dict(RUNTIME.connection_types)


def add(name: str, type_name: str, config: dict = None) -> dict:
    ct = types().get(type_name)
    if ct is None:
        raise ContractViolation(
            f"unknown connection type '{type_name}' "
            f"(known: {', '.join(sorted(types())) or 'none'})")
    cfg = ct.authorize(dict(config or {}))
    data = _read()
    data[name] = {"type": type_name, "config": cfg}
    _write(data)
    return {"connection": name, "type": type_name, "fields": sorted(cfg)}


def set_field(name: str, key: str, value) -> dict:
    data = _read()
    if name not in data:
        raise ContractViolation(f"no connection '{name}'")
    data[name]["config"][key] = value
    _write(data)
    return {"connection": name, key: value}


def remove(name: str) -> dict:
    data = _read()
    gone = data.pop(name, None)
    _write(data)
    return {"connection": name, "removed": bool(gone)}


def names() -> list:
    return sorted(_read())


def open(name: str) -> Connection:
    data = _read()
    if name not in data:
        raise NotConfigured(f"connection '{name}'",
                            f"finrail connection add {name} type=<type>")
    rec = data[name]
    return Connection(name, rec["type"], rec.get("config", {}))


def has(name: str) -> bool:
    return name in _read()


def test(name: str) -> tuple:
    return open(name).test()


def test_all() -> list:
    out = []
    for n in names():
        try:
            ok, detail = test(n)
        except Exception as e:
            ok, detail = False, str(e)[:160]
        out.append({"connection": n, "ok": ok, "detail": detail})
    return out


def checks() -> list:
    """Every stored connection as a doctor requirement — generic, no plugin code."""
    from .requirements import ConnectionCheck
    return [ConnectionCheck(f"connection:{n}", n) for n in names()]
