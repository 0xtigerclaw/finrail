#!/usr/bin/env python3
"""The deploy gate — what the FULL agent may not do from its own shell.

The full agent is the one capability that runs unconstrained (`full_agent`), and that is
deliberate. What it showed on 12 to 14 Oct is the one thing that must not ride on it: it
changed the code every agent on the host runs and put it live, in the same turn, with no
person in between. It loosened the shared approval rule twice in a morning, live within
minutes each time, operating companies included. It restarted its own service mid-turn
and the person's replies were lost. It ran `finrail upgrade` over another agent's
two-hour record build and retried until told to stop. Each step was reasonable on its
own; nobody had decided any of them.

So four things are refused here, and each refusal says the road that is open:

  * restarting, stopping or killing an finrail service: cuts off every turn on it,
    this one included. `finrail do upgrade` or `finrail do plugin.update` restart
    at the end, after a person approves.
  * pushing a guarded repository's main (the finrail checkout this host deploys from,
    the plugin repositories): `finrail do repo.land` lands it, after a person approves.
  * installing finrail over itself (`uv tool install`, `pip install` into it): that is
    what `upgrade` is for, with its suite, its idle check and its restart order.
  * writing under the installed plugins: they are a deploy's output, and an edit there
    is code nobody reviewed, running for everyone.

Everything else is left alone, on purpose. A feature branch pushes; tests run; the agent
reads anything. This is not a sandbox. A shell that wants round it can get round it
(`bash -c` in a file, python); the gate is for the habit, which is what the record shows,
and the refusal teaches the right road in the same breath.

Self-contained, standard library only: it runs before every tool call and must not
import finrail to do it. The guarded paths arrive on the command line (`deploy_settings`
in core/scope.py writes them there), so the script holds no copy of where things live.

Silent on everything it does not refuse. The full agent runs with permissions skipped,
so silence is "go ahead", and an explicit allow here would only be a second opinion.
"""
import json
import os
import re
import shlex
import sys

HOW = ("Deploys go through finrail so a person approves them: `finrail do upgrade` "
       "(core), `finrail do plugin.update name=<plugin>` (one plugin), `finrail do "
       "repo.land` (a branch onto main). Push your branch, propose it, and tell the "
       "person what is waiting.")

SERVICE_VERBS = r"(?:restart|stop|kill|try-restart|reload-or-restart|condrestart|disable|mask)"
SERVICE_RE = re.compile(
    rf"\bsystemctl\b[^;&|\n]*\b{SERVICE_VERBS}\b[^;&|\n]*finrail"
    r"|\blaunchctl\b[^;&|\n]*\b(?:kickstart|bootout|unload|stop|kill)\b[^;&|\n]*finrail"
    r"|\bpkill\b[^;&|\n]*(?:finrail|harness-finrail)"
    r"|\bkill(?:all)?\b[^;&|\n]*harness-finrail")
INSTALL_RE = re.compile(r"\buv\s+tool\s+install\b[^;&|\n]*finrail"
                        r"|\buv\s+tool\s+upgrade\b[^;&|\n]*finrail")
MAIN_RE = re.compile(r"(?:^|[\s:+])(?:refs/heads/)?(?:main|master)(?:\s|$)")
#: Commands whose LAST path argument is where they write.
TO_LAST = ("cp", "mv", "install", "rsync", "ln")
#: Commands that write every path they are given.
TO_ALL = ("rm", "touch", "tee", "truncate", "patch")


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"deploy gate: {reason} {HOW}"}}))
    sys.exit(0)


def under(path: str, root: str) -> bool:
    try:
        p = os.path.realpath(os.path.expanduser(path))
        r = os.path.realpath(os.path.expanduser(root))
    except Exception:                                               # noqa: BLE001
        return False
    return p == r or p.startswith(r.rstrip("/") + "/")


def segments(command: str) -> list:
    """The command split at `;`, `&&`, `||`, `|` and newlines, each with the directory it
    runs in as far as the text says (`cd X && ...`, `git -C X ...`)."""
    return [s.strip() for s in re.split(r";|&&|\|\||\||\n", command) if s.strip()]


def words(seg: str) -> list:
    try:
        return shlex.split(seg)
    except ValueError:
        return seg.split()


def pushes_main(command: str, cwd: str, guarded: list) -> str:
    """The guarded repository whose main this command pushes, or ''."""
    here = cwd
    for seg in segments(command):
        w = words(seg)
        if len(w) >= 2 and w[0] == "cd":
            here = w[1] if os.path.isabs(os.path.expanduser(w[1])) else os.path.join(here, w[1])
            continue
        if "git" not in w or "push" not in w:
            continue
        where = here
        if "-C" in w and w.index("-C") + 1 < len(w):
            c = w[w.index("-C") + 1]
            where = c if os.path.isabs(os.path.expanduser(c)) else os.path.join(here, c)
        repo = next((g for g in guarded if under(where, g)), "")
        if not repo:
            continue
        after = w[w.index("push") + 1:]
        refs = [a for a in after if not a.startswith("-")]
        # `git push` and `git push origin` push the current branch, which on a deploy
        # checkout is main; a named branch other than main is a branch, and goes.
        if len(refs) <= 1 or any(MAIN_RE.search(f" {r} ") for r in refs[1:]) \
                or any(r in ("HEAD", "HEAD:main", "HEAD:master") for r in refs[1:]):
            return repo
    return ""


def writes_into(command: str, cwd: str, roots: list) -> str:
    """The path under one of `roots` that this command writes, or ''. Reads are free: a
    `cat` of plugin code into a file elsewhere writes elsewhere."""
    here = cwd

    def hit(arg):
        if not arg or arg.startswith("-"):
            return ""
        full = arg if os.path.isabs(os.path.expanduser(arg)) else os.path.join(here, arg)
        return full if any(under(full, r) for r in roots) else ""

    for seg in segments(command):
        for target in re.findall(r">>?\s*([^\s;&|<>]+)", seg):
            if hit(target):
                return target
        w = words(re.sub(r"\d?>>?\s*[^\s;&|<>]+", " ", seg))
        if len(w) >= 2 and w[0] == "cd":
            here = w[1] if os.path.isabs(os.path.expanduser(w[1])) else os.path.join(here, w[1])
            continue
        if w and w[0] == "sudo":
            w = w[1:]
        if not w:
            continue
        if w[0] in TO_LAST:
            args = [a for a in w[1:] if not a.startswith("-")]
            if args and hit(args[-1]):
                return args[-1]
        elif w[0] in TO_ALL or (w[0] == "sed" and any(a.startswith("-i") for a in w[1:])):
            for a in w[1:]:
                if hit(a):
                    return a
        elif w[:2] == ["git", "apply"] and any(under(here, r) for r in roots):
            return here
    return ""


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:                                               # noqa: BLE001
        sys.exit(0)            # nothing to judge; the engine's own handling stands
    guarded = [a for a in sys.argv[1:] if a.startswith("--repo=")]
    plugins = [a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--plugins=")]
    guarded = [a.split("=", 1)[1] for a in guarded]
    tool = data.get("tool_name") or ""
    params = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()

    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        target = params.get("file_path") or params.get("notebook_path") or ""
        if target and any(under(target, p) for p in plugins):
            deny(f"{target} is installed plugin code, the output of a deploy; change the "
                 "plugin's repository instead.")
        sys.exit(0)
    if tool != "Bash":
        sys.exit(0)
    command = str(params.get("command") or "")
    if SERVICE_RE.search(command):
        deny("restarting or stopping an finrail service cuts off every turn on it, "
             "this one included, and the person's replies with it.")
    if INSTALL_RE.search(command) or any(
            re.search(r"\bpip3?\s+install\b", s) and any(g and g in s for g in guarded)
            for s in segments(command)):
        deny("installing finrail over itself skips the suite, the idle check and the "
             "restart order `upgrade` runs.")
    repo = pushes_main(command, cwd, guarded)
    if repo:
        deny(f"pushing main in {repo} puts code live for every agent with no person "
             "having seen it.")
    wrote = writes_into(command, cwd, [p for p in plugins if p])
    if wrote:
        deny(f"{wrote} is installed plugin code, the output of a deploy; change the "
             "plugin's repository instead.")
    sys.exit(0)


if __name__ == "__main__":
    main()
