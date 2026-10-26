"""AGENTS — an agent is a session-capable capability, plus an identity to answer as.

Not a harness, not a directory, not a settings file. Everything that makes two agents
different from each other is in one small document:

    name        what it is called, and the key it answers as. Same word both places.
    capability  the session-capable capability that IS the agent. `full-agent` is the full
                agent; another might be a narrower one. This is the whole of what the
                agent can do, because converse dispatches to it and to nothing else.
    pubkey      the identity the transport verifies signatures against.
    secret      a VAULT PATH, never the key itself. The harness deliberately drops its
                root token before handing off, so that a compromised agent cannot read
                another's identity; a private key sitting in this file would undo that.
    operators   who may drive it. Defaults to `<name>.operators` beside this file.
    enabled     whether it should be running. THE ONE RECORD OF THAT DECISION. The plugin
                that deploys it writes true; the one that undeploys it writes false; an
                upgrade restarts only what says true; a unit that disagrees is a doctor
                finding, never an action. Absent means nobody has said, and nothing
                starts or restarts it until somebody does.

BESIDE THE STATE, NOT IN IT. State is derived and mutable — runs, sessions, lessons, the
journal. A definition is hand-written configuration that wants version control, review and
a different backup story. Mixed together, a state relocation or a clear-down takes the
agent definitions with it.

And every field here is a security decision: `capability` is what the agent may do,
`operators` is who may ask, `pubkey`/`secret` is who it claims to be. So in a real
deployment this directory is root-owned and the agent cannot write it — the agent must not
be able to widen its own operator list or swap its own capability. The default below is a
sibling of the state directory so that a fresh install and the test suite work with no
root at all; deployment points `agents.dir` at /etc.

JSON, not YAML: finrail core has no third-party dependencies and is not about to take one
to read a five-line document.
"""
import json
import os
import pathlib

from .errors import ContractViolation

#: The agent this process is acting as. Set by the unit, exactly like FINRAIL_STATE, and
#: CARRIED rather than derived — the same rule `user` follows, and for the same reason: a
#: message is precisely the thing an identity must not come from.
ENV = "FINRAIL_AGENT"

FIELDS = ("name", "capability", "pubkey", "secret", "operators", "transport", "relay",
          "enabled")


def directory() -> pathlib.Path:
    """Where definitions live. Beside the state directory, never inside it."""
    from .settings import raw_location, state
    v = raw_location("agents.dir")
    return pathlib.Path(str(v)).expanduser() if v else state().parent / "agents"


def _file(name: str) -> pathlib.Path:
    safe = "".join(c for c in str(name) if c.isalnum() or c in "-_.")
    if not safe or safe != str(name):
        raise ContractViolation(
            f"{name!r} is not a usable agent name — letters, digits, '-', '_' and '.' only")
    return directory() / f"{safe}.json"


def names() -> list:
    d = directory()
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def load(name: str) -> dict:
    """One definition, with its operators resolved. Raises if it is not usable."""
    p = _file(name)
    if not p.is_file():
        raise ContractViolation(
            f"no agent {name!r} in {directory()} (see: finrail get agents.list)")
    try:
        rec = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ContractViolation(f"agent {name}: {p} is not readable JSON — {e}")
    if not isinstance(rec, dict):
        raise ContractViolation(f"agent {name}: {p} must contain an object")
    rec.setdefault("name", name)
    if rec["name"] != name:
        # The filename is the name people type and the unit references. A document that
        # disagrees with its own filename is one of them being wrong, and guessing which
        # is how an agent ends up answering as the wrong identity.
        raise ContractViolation(
            f"agent {name}: the file says name={rec['name']!r}; they must match")
    rec["path"] = str(p)
    rec.setdefault("operators", str(p.with_suffix(".operators")))
    return rec


def switched(rec: dict):
    """True or False when the definition says whether the agent should run; None when it
    does not say. Three values on purpose: the operator who switched an agent off by
    hand, and every definition written before this field existed, are both "not said",
    and treating either as "on" is how one agent came to run on two harnesses."""
    v = rec.get("enabled")
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def switch(name: str, on: bool) -> pathlib.Path:
    """Record whether `name` should run. Every other field is kept as it is."""
    rec = load(name)
    rec["enabled"] = bool(on)
    return save(rec)


def operators(name: str) -> set:
    """Pubkeys allowed to drive this agent.

    One per line, rest of the line is a display name. Read on every call rather than
    cached, because adding someone should not need a restart — the ws harness already
    relies on exactly that.
    """
    rec = load(name) if isinstance(name, str) else name
    p = pathlib.Path(str(rec.get("operators") or ""))
    if not p.is_file():
        return set()
    out = set()
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.split()[0])
    return out


def operator_names(name=None) -> dict:
    """Pubkey -> display name, from the same files `operators` reads.

    The name after the key is written there "for the logs and for finrail's session
    ownership"; this is the half that reads it back. `operators` throws it away on
    purpose — its callers ask "may this key drive me", which a name cannot answer — but
    a session listing asks the other question, "who is on the other side", and a
    64-character key is not an answer to that one.

    With no agent named, every defined agent's list is merged, because a session's owner
    is a pubkey and nothing else: there is no agent to start from. When two files give
    one key different names, THE FIRST IN AGENT-NAME ORDER WINS — `names()` is sorted, so
    the winner is the same on every host holding the same files, rather than whatever the
    directory happened to list first. Deterministic beats clever here; a name that moves
    between runs is worse than a name that is merely one file's opinion.

    Read on every call and never cached, exactly as `operators` is: adding someone must
    not need a restart.
    """
    if name is not None:
        recs = [load(name) if isinstance(name, str) else name]
    else:
        recs = []
        for agent in names():
            try:
                recs.append(load(agent))
            except ContractViolation:
                continue      # a broken definition is doctor's to report, not a listing's
    out = {}
    for rec in recs:
        p = pathlib.Path(str(rec.get("operators") or ""))
        if not p.is_file():
            continue
        try:
            text = p.read_text()
        except OSError:
            continue          # root-owned and unreadable is a missing name, not a crash
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            label = parts[1].strip() if len(parts) > 1 else ""
            if label:
                out.setdefault(parts[0], label)
    return out


def short_key(key: str) -> str:
    """A pubkey is 64 hex characters and a column is not.

    Shorten only what is long enough to need it, so a readable handle passed as `user=`
    is left alone. Lives here because two session views print keys and they have to
    shorten them the same way to be talking about the same session.
    """
    key = key or ""
    return key if len(key) <= 12 else key[:10] + "…"


def owner_label(pubkey: str, known: dict = None, unowned: str = "—") -> str:
    """How to show a session's owner. Three cases, and all three are honest:

    no pubkey       a session started from the CLI has no owner at all, so `unowned` —
                    a dash, never the name of whoever happens to be listed
    a known key     the display name, which is the whole point
    anything else   the SHORTENED KEY, so an operator no file lists reads as unknown
                    rather than disappearing behind a plausible name

    `known` is a map built once by a caller rendering a whole table; without it the
    files are read here, per call.
    """
    key = (pubkey or "").strip()
    if not key:
        return unowned
    return (operator_names() if known is None else known).get(key) or short_key(key)


def current() -> str:
    """The agent this process is acting as, or "" when it is a plain CLI invocation."""
    return (os.environ.get(ENV) or "").strip()


def problems(name: str) -> list:
    """Everything wrong with a definition, in the words of whoever has to fix it.

    Separate from `load` because doctor wants ALL of them at once, while a dispatch only
    needs to know it cannot proceed.
    """
    from . import registry
    out = []
    try:
        rec = load(name)
    except ContractViolation as e:
        return [str(e)]
    for field in ("capability", "pubkey"):
        if not rec.get(field):
            out.append(f"{name}: no {field}")
    cap_name = rec.get("capability") or ""
    if cap_name:
        try:
            cap = registry.command(cap_name)
        except Exception:                                        # noqa: BLE001
            out.append(f"{name}: capability {cap_name!r} does not exist")
        else:
            if not getattr(cap, "session_kind", ""):
                # converse refuses it anyway; saying so here means finding out when the
                # agent is defined rather than when someone first speaks to it.
                out.append(f"{name}: capability {cap_name!r} is not conversational "
                           f"(it declares no session_kind) — converse cannot drive it")
    if not pathlib.Path(str(rec.get("operators") or "")).is_file():
        out.append(f"{name}: no operators file at {rec.get('operators')} — "
                   f"nobody would be allowed to drive it")
    elif not operators(rec):
        out.append(f"{name}: the operators file is empty — nobody may drive it")
    return out


#: The plugin that owns the agents store (core/owners.py). The functions below are its
#: declared operations: a plugin that defines agents calls them rather than
#: writing into the directory itself (docs/LAYERS.md, rule 2).
OWNER = "agents"


def save(rec: dict) -> pathlib.Path:
    """Write a definition. Used by `agents.add`; a deployment writes these by hand."""
    from . import atomic, owners
    name = rec.get("name") or ""
    if not name:
        raise ContractViolation("an agent needs a name")
    p = _file(name)
    body = {k: rec[k] for k in FIELDS if rec.get(k) is not None and k in rec}
    body["name"] = name
    with owners.as_owner(OWNER):
        atomic.write_json(p, body, indent=2, sort_keys=True)
    return p


def operators_path(name: str) -> pathlib.Path:
    """Where an agent's allow list lives: beside its definition."""
    return _file(name).with_suffix(".operators")


def save_operators(name: str, body: str) -> pathlib.Path:
    """Write an agent's allow list, whole."""
    from . import atomic, owners
    p = operators_path(name)
    with owners.as_owner(OWNER):
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(p, body)
    return p


def remove(name: str) -> list:
    """Delete an agent's definition and its allow list. Its keys are not touched."""
    from . import owners
    gone = []
    with owners.as_owner(OWNER):
        for p in (_file(name), operators_path(name)):
            if p.is_file():
                p.unlink()
                gone.append(str(p))
    return gone
