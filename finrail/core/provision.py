"""DOCTOR and PROVISION — is this machine actually able to do the work?

`doctor` aggregates every check in the system into one dependency-ordered report:
plugin-declared requirements, required settings, and every stored connection's own
`test()`. Two contracts make it useful rather than decorative:

  * every red line carries `fix:` — the exact command that resolves it. A fresh operator
    (or a Claude Code session that can only run `finrail`) is never left guessing.
  * `provision` repairs and then RE-CHECKS. A repair that didn't take is reported as
    failed, never claimed as fixed.
"""
from .errors import ContractViolation
from . import connections
from .requirements import SettingCheck


def _declared() -> list:
    from .registry import load, settings
    from .api import RUNTIME
    load()
    reqs = list(RUNTIME.requirements())
    for m in settings().missing():
        r = SettingCheck(f"setting:{m['setting']}", m["setting"])
        r.plugin = m["plugin"]
        reqs.append(r)
    reqs.extend(connections.checks())
    return reqs


def _ordered(reqs: list) -> list:
    """Topological by `after`. A cycle is a declaration bug — say so loudly."""
    by_name = {r.name: r for r in reqs}
    out, state = [], {}

    def visit(r, trail):
        s = state.get(r.name)
        if s == "done":
            return
        if s == "open":
            raise ContractViolation(
                f"requirement cycle: {' -> '.join(trail + [r.name])}")
        state[r.name] = "open"
        for dep in r.after:
            d = by_name.get(dep)
            if d is not None:
                visit(d, trail + [r.name])
        state[r.name] = "done"
        out.append(r)

    for r in reqs:
        visit(r, [])
    return out


def requirements(plugin: str = "", install_only: bool = False) -> list:
    """Every declared requirement in dependency order, narrowed if asked.

    `install_only` keeps the ones that live inside the installed package — the ones a
    reinstall destroys. It is what `upgrade` asks for, so that putting back what it broke
    cannot turn into rewriting the host.
    """
    reqs = _ordered(_declared())
    return [r for r in reqs
            if (not plugin or r.plugin == plugin)
            and (not install_only or r.install_scoped)]


def check(plugin: str = "", install_only: bool = False) -> list:
    """Run every check. Order matters: the first failure in a chain is the real one."""
    rows, broken = [], set()
    for r in requirements(plugin, install_only):
        blocked = [d for d in r.after if d in broken]
        if blocked:
            rows.append({"name": r.name, "kind": r.kind, "plugin": r.plugin,
                         "ok": False, "detail": f"blocked by {', '.join(blocked)}",
                         "fix": f"fix {blocked[0]} first", "blocked": True,
                         "repairable": r.repairable})
            broken.add(r.name)
            continue
        try:
            ok, detail = r.check()
        except Exception as e:
            ok, detail = False, f"check raised: {str(e)[:160]}"
        if not ok:
            broken.add(r.name)
        rows.append({"name": r.name, "kind": r.kind, "plugin": r.plugin,
                     "ok": bool(ok), "detail": str(detail),
                     "fix": "" if ok else r.fix(), "blocked": False,
                     "repairable": r.repairable})
    return rows


def doctor(plugin: str = "") -> dict:
    rows = check(plugin)
    bad = [r for r in rows if not r["ok"]]
    return {"checks": rows, "ok": not bad, "total": len(rows),
            "failing": len(bad),
            "next": bad[0]["fix"] if bad else "",
            "repairable": [r["name"] for r in bad if r["repairable"]],
            "needs_you": [r["name"] for r in bad if not r["repairable"]]}


def interactive() -> bool:
    """Is there a person at a terminal to answer a prompt?

    Asked rather than assumed, because the answer decides whether a repair that needs one
    is attempted or reported. `FINRAIL_INTERACTIVE` overrides it for the cases stdin
    cannot describe — a wrapper that does have a console, a test that must pin either.
    """
    import os
    import sys
    override = (os.environ.get("FINRAIL_INTERACTIVE") or "").strip().lower()
    if override in ("1", "true", "yes"):
        return True
    if override in ("0", "false", "no"):
        return False
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, AttributeError):
        return False


def provision(plugin: str = "", dry_run: bool = False,
              install_only: bool = False) -> dict:
    """Repair everything repairable, in dependency order, then VERIFY each repair.

    The deterministic half of the pair described in
    dev/proposed/verification-contract.md: one pass, one attempt per requirement, and the
    repair is written in code because the code knows the fix. Its judged twin is
    `apps.add`, which spends real deploys and asks a model to amend from evidence. What
    both refuse to do is the same thing, and it is the only thing that matters: **a repair
    that did not take is reported as failed, never claimed as fixed.**

    What does NOT transfer between them, said here so nobody looks for it: this has no
    attempt bound because it makes exactly one attempt per requirement, and it has no
    cleanup on failure because a requirement is not a thing that can be un-created.
    """
    done, failed, skipped = [], [], []
    broken = set()
    attended = interactive()
    for r in requirements(plugin, install_only):
        # Dependency order alone is not enough. `doctor` already refuses to run a check
        # whose prerequisite failed, and reports "blocked by" instead; this did not, so a
        # missing docker produced a repair attempt for the network that depends on it and
        # a raw error from a program that is not installed. The first failure in a chain
        # is the real one — in both reports, or in neither.
        blocked = [d for d in r.after if d in broken]
        if blocked:
            skipped.append({"name": r.name, "detail": f"blocked by {', '.join(blocked)}",
                            "fix": f"fix {blocked[0]} first"})
            broken.add(r.name)
            continue
        try:
            ok, detail = r.check()
        except Exception as e:
            ok, detail = False, str(e)[:160]
        if ok:
            continue
        if not r.repairable:
            broken.add(r.name)
            skipped.append({"name": r.name, "detail": detail, "fix": r.fix()})
            continue
        if r.needs_human and not attended:
            # NOT ATTEMPTED, because attempting it is what did the damage. A repair that
            # reads a credential cannot get one from a store with nobody to unlock it, and
            # the read comes back looking like an absence rather than a refusal — so the
            # repair proceeds on half the truth and writes it down. Reporting beats
            # guessing, and the person who runs it from a terminal gets the real thing.
            broken.add(r.name)
            skipped.append({"name": r.name, "detail": detail,
                            "fix": f"needs a terminal — run `finrail provision "
                                   f"{r.plugin}`".strip() + " yourself"})
            continue
        if dry_run:
            done.append({"name": r.name, "would": r.fix()})
            continue
        try:
            note = r.repair()
        except Exception as e:
            broken.add(r.name)
            failed.append({"name": r.name, "error": str(e)[:200]})
            continue
        ok2, detail2 = r.check()                      # the verification that matters
        if not ok2:
            broken.add(r.name)
        (done if ok2 else failed).append(
            {"name": r.name, "note": note, "detail": detail2,
             **({} if ok2 else {"error": "repair ran but the check still fails"})})
    return {"repaired": done, "failed": failed, "needs_you": skipped,
            "ok": not failed and not skipped, "dry_run": dry_run}


def render(out: dict) -> str:
    """Doctor as a human reads it: worst first, every red line with its fix."""
    if "checks" not in out:
        return _render_provision(out)
    rows = out["checks"]
    if not rows:
        return "(nothing declared yet — install or configure a plugin)"
    lines = []
    bad = [r for r in rows if not r["ok"]]
    good = [r for r in rows if r["ok"]]
    lines.append(f"{len(good)}/{len(rows)} checks pass"
                 + ("" if not bad else f" — {len(bad)} need attention"))
    for r in bad:
        lines.append(f"\n  ✗ {r['name']}  [{r['kind']}"
                     + (f"/{r['plugin']}" if r["plugin"] else "") + "]")
        lines.append(f"      {r['detail']}")
        if r["fix"]:
            lines.append(f"      fix: {r['fix']}")
    if good:
        lines.append("\n  ok: " + ", ".join(r["name"] for r in good))
    if bad:
        rep = [r["name"] for r in bad if r["repairable"]]
        if rep:
            lines.append(f"\n{len(rep)} of these finrail can fix itself: "
                         f"finrail provision")
    return "\n".join(lines)


def _render_provision(out: dict) -> str:
    lines = []
    for d in out.get("repaired", ()):
        lines.append(f"  ✓ {d['name']}  {d.get('note') or d.get('would','')}")
    for d in out.get("failed", ()):
        lines.append(f"  ✗ {d['name']}  {d.get('error','')} {d.get('detail','')}")
    for d in out.get("needs_you", ()):
        lines.append(f"  … {d['name']} needs you: {d['fix']}")
    if not lines:
        return "nothing to do — everything already in place"
    head = "would repair:" if out.get("dry_run") else "provision:"
    return head + "\n" + "\n".join(lines)
