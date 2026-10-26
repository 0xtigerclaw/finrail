"""ACTIVITIES ON DISK — one markdown file per activity, in the folder of the agent it is for.

    <FINRAIL_STATE>/activities/<capability>/<name>.md

The folder IS the link. An activity under `a coding agent/` is a coding agent's, and nothing else has
to say so: no setting to update, no second list to keep in step, nothing to restart —
the folder is read on every turn. Sharing one activity between two agents is a copy of
the file, deliberately; most activities belong to exactly one agent, and a linking layer
paid for on every activity to serve the rare shared one is the mess this avoids.

Rooted in `state()`, which is FINRAIL_STATE when it is set, never a path written here.
The files are content rather than code, so they live with the rest of the system's
state and not in the package: the installed copy is replaced on every upgrade.

A file is frontmatter with a one-line `description`, then the instructions:

    ---
    description: Fix a reported defect with a failing test first
    checks:
      - The answer says a failing test was written before the fix
      - The answer reports the result of running the suite
    ---
    <what the agent is told when this activity is in effect>

`checks` is optional: yes/no statements about the agent's ANSWER that should hold when the
activity was done properly. They are what `judge.py` asks after the turn. An activity
with none is not judged.
"""
import pathlib
import re

from ...core import frontmatter, registry
from ...core.errors import ContractViolation
from ...core.settings import state

DIR = "activities"

#: Words `activity.set` gives a meaning of their own, so no activity may be called them.
RESERVED = ("none", "auto")

_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")


def root() -> pathlib.Path:
    return state() / DIR


def folder(capability: str) -> pathlib.Path:
    return root() / capability


def path(capability: str, name: str) -> pathlib.Path:
    return folder(capability) / f"{name}.md"


def conversational() -> list:
    """The capabilities an activity can be for: every one declaring `session_kind`, read
    from the live registry, so a new session agent needs no edit here."""
    out = []
    for row in registry.catalogue():
        try:
            cap = registry.command(row["name"])
        except Exception:                                          # noqa: BLE001
            continue
        if getattr(cap, "session_kind", ""):
            out.append(row["name"])
    return sorted(out)


def for_kind(kind: str) -> list:
    """The capabilities whose sessions are of this kind. A session records its kind, not
    the command that opened it, and those are one-to-one on this install — but that is
    a fact about the registry, so it is asked of the registry."""
    out = []
    for name in conversational():
        if getattr(registry.command(name), "session_kind", "") == kind:
            out.append(name)
    return out


def check_capability(capability: str) -> str:
    if not capability:
        raise ContractViolation(
            f"name the agent — capability=<one of {', '.join(conversational())}>")
    if capability not in conversational():
        raise ContractViolation(
            f"{capability!r} is not a session agent; activities belong to one of "
            f"{', '.join(conversational()) or 'none'}")
    return capability


def check_name(name: str) -> str:
    name = (name or "").strip()
    if name in RESERVED:
        raise ContractViolation(f"{name!r} means something to activity.set; choose "
                                f"another name")
    if not _NAME.fullmatch(name):
        raise ContractViolation(
            f"activity name {name!r}: lowercase letters, digits and hyphens, starting "
            f"with a letter or digit, at most 60 characters")
    return name


def names(capability: str) -> list:
    d = folder(capability)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.md") if _NAME.fullmatch(p.stem))


def exists(capability: str, name: str) -> bool:
    return bool(name) and path(capability, name).is_file()


def load(capability: str, name: str) -> dict:
    p = path(capability, name)
    if not p.is_file():
        have = names(capability)
        raise ContractViolation(
            f"{capability} has no activity {name!r}"
            + (f" — it has: {', '.join(have)}" if have else " — it has none yet"))
    meta, body = frontmatter.read(p)
    return {"capability": capability, "name": name,
            "description": str(meta.get("description") or ""),
            "checks": _checks(meta.get("checks")),
            "instructions": body.strip(), "path": str(p)}


def _checks(raw) -> list:
    if isinstance(raw, str):
        raw = [raw]
    return [str(c).strip() for c in (raw or []) if str(c or "").strip()]


def listing(capability: str) -> list:
    """Every activity for one agent. A file that cannot be read is listed as it is, with
    no description, rather than hiding the rest behind it."""
    out = []
    for name in names(capability):
        try:
            out.append(load(capability, name))
        except Exception:                                          # noqa: BLE001
            out.append({"capability": capability, "name": name, "description": "",
                        "instructions": "", "path": str(path(capability, name))})
    return out


def folders() -> list:
    """Folder names under the root, whether or not they name a session agent — so a
    folder for an agent that no longer exists stays visible instead of silently inert."""
    if not root().is_dir():
        return []
    return sorted(p.name for p in root().iterdir() if p.is_dir())


def write(capability: str, name: str, description: str, instructions: str,
          checks=None) -> str:
    """`checks=None` keeps whatever checks the file already has — an edit to the
    description must not quietly stop the activity being judged."""
    p = path(capability, name)
    if checks is None and p.is_file():
        try:
            checks = _checks(frontmatter.read(p)[0].get("checks"))
        except Exception:                                          # noqa: BLE001
            checks = None
    meta = {"description": (description or "").strip()}
    if checks:
        meta["checks"] = _checks(checks)
    frontmatter.write(p, meta, (instructions or "").strip() + "\n")
    return str(p)


def remove(capability: str, name: str) -> str:
    p = path(capability, name)
    p.unlink()
    return str(p)
