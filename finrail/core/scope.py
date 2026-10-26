"""SCOPE — what a scoped agent may do, decided by code, at every tool call.

The full agent (`full-agent`) is deliberately unconstrained. Everything narrower used to be a
briefing — "prefer these capabilities" — which a model can drift from, and which nothing
enforces. A scope is the enforced version: a small declaration on the capability class,
materialised into the engine's workspace as a `.claude/settings.json` (a deny list and a
PreToolUse hook) and a `scope.json` the hook hands back here. The engine is still offered
every tool; each call it makes is judged by `decide()` below, and an answer other than
`allow` stops it before it runs.

The declaration is the whole of the policy:

    tools         Claude Code tools it may use at all; everything else is denied twice —
                  by `permissions.deny` and again by the hook, because a settings file
                  carrying hooks and NO deny list leaves Write open (measured; see
                  `llm.rails_dir`).
    capabilities  finrail capabilities it may dispatch, as glob patterns. `finrail`
                  itself is always reachable — that is how it discovers what it can do
                  (`finrail capabilities`, `finrail -h2 <cap>`) — but a capability
                  outside the patterns is refused with the reason.
    write_roots   the only directories Write/Edit/NotebookEdit, shell redirection and
                  file-mutating commands may touch. Reading is unrestricted: "standard
                  read capabilities" means Read, Glob, Grep and the read-only shell tools
                  anywhere on the box.
    shell         which shell ruleset judges Bash — `reads` (finrail plus the read-only
                  tools, nothing else), `developer` (git, tests and the tools of a
                  working tree, under rules that keep `~/src/<repo>` on main and put
                  every change through a worktree in `dev_root`), or `site` (an agent
                  that EDITS one repository with the file tools and may only LOOK at it
                  through the shell — `_decide_site`).
    repos         for `developer`: the repositories it may work on, by checkout path.
                  For `site`: the repository it maintains.
    dev_root      for `developer`: where its worktrees live.
    mainline      for `developer`: the branch its landings go to, "main" when unset. The
                  dev agents land on `dev`, so their work is used before it is anyone's
                  main; a landing on any other branch is refused (`_land_refusal`).
    commit_roots  directories where every Write/Edit is committed and pushed for the
                  agent, by a PostToolUse hook materialised from this same declaration.
                  An agent that may not run git still gets its work versioned.
    reads         shell tools from `policy.GUARDED_READS` this scope may run as reads —
                  ones that are a read only in a particular shape (`sqlite3` only under
                  `-readonly -safe`), so they are on no global list and a scope takes one
                  alone. Not a way to grant a tool: a name whose read-only shape the gate
                  does not hold is refused when the Scope is built.

Enforcement lives HERE, in one function, consumed by `finrail policy --scope` — the same
shape as the unscoped gate (`policy.decide` behind `finrail policy`), so the hook is a
thin relay and cannot disagree with the tool. Fail closed: a payload this cannot read, a
tool it does not know, a path it cannot resolve, all deny.
"""
import fnmatch
import json
import os
import sys
import pathlib
import re
import shlex
import subprocess

from . import policy
from .errors import ContractViolation

#: Every tool Claude Code can offer, as far as this file knows. Anything on this list and
#: not in a scope's `tools` goes into `permissions.deny`; anything NOT on this list — a
#: tool invented next year, an MCP connector — is refused by the hook's default.
KNOWN_TOOLS = ("Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob",
               "Grep", "LS", "Task", "Agent", "WebFetch", "WebSearch", "TodoWrite",
               "ToolSearch", "SendUserFile", "Artifact", "AskUserQuestion",
               "EnterPlanMode", "ExitPlanMode", "Skill", "Workflow", "NotebookRead")

#: Tools that only look. Allowed by every scope that lists them, anywhere on disk.
READ_TOOLS = ("Read", "Glob", "Grep", "LS", "NotebookRead", "ToolSearch", "TodoWrite",
              "SendUserFile")

#: The engine's own question tool ("which folder?"). Not a read: it waits on a person.
#: A scope that lists it has its questions put to the person in the chat by the gate,
#: which hands the answer back as the tool's input (`clarify`). The engine offers the
#: tool only when started with a host to answer it (`llm.call(questions=True)`).
QUESTION_TOOL = "AskUserQuestion"

#: Tools that read the NETWORK rather than the disk. Their own rule rather than a line
#: added to READ_TOOLS, because "only looks, anywhere on disk" is a claim about reach and
#: these do not make it: they leave the machine. A scope that lists one is saying so
#: deliberately. Nothing they fetch can be kept except through a writable root or an
#: finrail capability, so the write boundary is unaffected by granting them.
NET_TOOLS = ("WebFetch", "WebSearch")

#: Tools that write a file the scope must place. `notebook_path` is NotebookEdit's name
#: for the same thing.
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

#: `finrail <verb>` forms that only describe the system. Reachable from every scope:
#: this is how an agent learns what it may do instead of being told a list that goes
#: stale the day a capability is added.
META_VERBS = ("capabilities", "caps", "rules", "-h2", "--help", "-h", "help", "version",
              "--version", "doctor", "status", "runs", "view", "sessions", "session")

#: The dispatching verbs, whose next word is a capability.
DISPATCH_VERBS = ("do", "get", "dispatch", "view")

ALLOW, DENY, ASK = "allow", "deny", "ask"

#: Read-only shell tools beyond `policy.SAFE_READS`, for the developer scope only: a
#: working tree is read with these all day. `sed` is here WITHOUT `-i`, checked below.
DEV_READS = ("sed", "awk", "diff", "tree", "jq", "less", "more", "column", "nl", "md5sum",
             "sha256sum", "xxd", "strings", "readlink", "du", "df", "ps", "pgrep",
             "test", "[", "true", "false", "sleep", "printf", "type", "command", "id",
             "whoami", "hostname", "uname", "git")

#: Commands that create, change or remove files by their path arguments.
MUTATORS = ("rm", "mv", "cp", "mkdir", "rmdir", "touch", "chmod", "tee", "ln", "install",
            "truncate")

#: Programs that run code from a working tree — tests, syncs, builds. Allowed in the
#: developer scope only when the shell is IN a worktree (or told to be, with `cd`).
DEV_RUNNERS = ("python", "python3", "pytest", "uv", "node", "npm", "npx", "make", "ruff",
               "black", "mypy", "pip")

#: The `site` ruleset's inspection tools, by first binary: look but do not touch. Wider
#: than `policy.SAFE_READS`, because an agent that keeps a tree reads it all day with
#: these, and narrower than `DEV_READS`, because nothing here runs code. `find` is on it
#: and is refused anyway when it carries `-delete` or `-exec`.
SITE_READS = ("ls", "find", "cat", "head", "tail", "wc", "file", "stat", "du", "tree",
              "grep", "egrep", "fgrep", "diff", "md5sum", "sha256sum", "sha1sum",
              "basename", "dirname", "realpath", "readlink", "nl", "cut", "sort",
              "uniq", "column", "jq")

#: git subcommands the `site` ruleset allows: the ones that only report. A site agent's
#: commits are made for it by the `commit_roots` hook, so nothing that writes is here.
SITE_GIT_READS = ("log", "status", "diff", "show", "blame", "ls-files", "show-ref",
                  "rev-parse", "shortlog", "branch", "remote", "describe")

#: The two above that only report when they are given nothing that acts — `git remote add`
#: and `git branch -D` are on neither list by accident. Pinned down by their first
#: argument, the way `_decide_git` pins the same two for the developer ruleset.
SITE_GIT_REPORTING = {"branch": ("-v", "-vv", "-a", "-r", "--list", "--all", "--verbose",
                                 "--remotes"),
                      "remote": ("-v", "show", "get-url")}

#: Shell punctuation refused outright inside a site repository: one command per call, so
#: an allowed first word cannot carry an unjudged tail behind `;`, `|` or `$(…)`.
SITE_META = re.compile(r"[;|&`<>]|\$\(")

#: A plain single-file delete inside a site repository: one path, no flags, no wildcards.
#: Matched against the WHOLE command, so `rm -rf <path>` (two words after `rm`) is not it.
SITE_RM = re.compile(r"rm ([A-Za-z0-9._/-]+)")

#: How many capability patterns a refusal prints before it names the count instead.
#: Eight fit on a line; fifty-five made an 821-character refusal that buried its own
#: reason in the middle of itself. See `Scope.patterns_for_refusal`.
REFUSAL_PATTERNS_MAX = 8


#: The tiers of an finrail checkout, as the paths under it that belong to each
#: (docs/LAYERS.md). `finrail_plugins` is the same code as `finrail/extensions` under
#: the name the registry imports it by, so it is the extensions tier too. A file under
#: `finrail/` that is in none of these is core's (the package root, `api.py`'s peers).
TIERS = {
    "core": ("finrail/core", "finrail/gates", "finrail/launcher", "finrail/plugin.py",
             "finrail/__init__.py"),
    "bundled": ("finrail/bundled",),
    "extensions": ("finrail/extensions", "finrail_plugins"),
}


#: The files that ARE the layer checks. They are core's whatever directory they sit in: a
#: task in another tier that could edit them could hide its own crossing in the baseline,
#: lower the floor under a layer run, or rewrite how a test file is given its tier. The
#: stress test of 2026-09-28 did all three from a bundled task.
CHECK_FILES = ("tests/conftest.py", "tests/layers_baseline.txt", "tests/layers_floor.json",
               "tests/test_layers.py", "tests/test_layer_runs.py",
               "tests/stores_baseline.txt", "tests/test_store_owners.py",
               "tests/runtime_baseline.txt")


def tier_of(path) -> str:
    """Which tier a file belongs to, by its path under a checkout: `core`, `bundled`,
    `extensions`, or "" for anything that is not finrail code (tests, docs, a vault).

    THE DEEPEST MATCH WINS. A folder named `finrail` above the checkout: `/srv/finrail`,
    `~/src/finrail`: is not the package, and reading the first one as the package made
    every file under it core: a bundled task could not touch its own code or its tests,
    and a core task could touch all of it. So each `finrail` segment is tried and the
    last one that names a tier is the answer. Compared without case: on a Mac,
    `Finrail/Core` is the same directory as `finrail/core`."""
    raw = pathlib.PurePath(str(path)).parts
    parts = [s.lower() for s in raw]
    if len(parts) >= 2 and "/".join(parts[-2:]) in CHECK_FILES:
        return "core"
    found = ""
    for i, seg in enumerate(parts):
        if seg not in ("finrail", "finrail_plugins"):
            continue
        rel = "/".join(parts[i:])
        hit = next((t for t, roots in TIERS.items()
                    if any(rel == r or rel.startswith(r + "/") for r in roots)), "")
        if hit:
            found = hit
        elif seg == "finrail" and (
                (len(parts) == i + 2 and parts[-1].endswith(".py"))
                or pathlib.Path(*raw[:i + 1], "__init__.py").is_file()):
            # A module in the package root, or a new subpackage of a real checkout: core,
            # as everything under `finrail/` that no other tier claims.
            found = "core"
    return found


def tier_refusal(scope, p) -> str:
    """Why this scope may not write `p` because of its tier, or "". One answer for every
    way a file gets written: the file tools, a redirection, `cp`/`mv`/`rm`, and the change
    set a commit, a push or a landing carries."""
    have = tier_of(p)
    if scope.tier and have and have != scope.tier:
        return (f"{scope.name} is working in the {scope.tier} tier and {p} is {have} "
                f"code: a change that needs two tiers is two tasks (docs/LAYERS.md)")
    return ""


class Scope:
    def __init__(self, name: str, tools=(), capabilities=("*",), write_roots=(),
                 shell: str = "reads", repos=(), dev_root: str = "", cwd: str = "",
                 commit_roots=(), tier: str = "", ask_outside: bool = False,
                 sandbox: bool = False, net=(), hide=(), reads=(), mainline: str = ""):
        if shell not in ("reads", "developer", "site"):
            raise ContractViolation(f"scope {name}: unknown shell ruleset {shell!r}")
        if tier and tier not in TIERS:
            raise ContractViolation(
                f"scope {name}: tier must be one of {', '.join(TIERS)}, not {tier!r}")
        unjudged = [t for t in reads if t not in policy.GUARDED_READS]
        if unjudged:
            # Refused at construction, not at the first call: `reads` exists to hand over
            # a tool the gate can hold to a read-only shape, and a name it has no shape
            # for would be a tool granted whole by a field that reads like a read list.
            raise ContractViolation(
                f"scope {name}: {unjudged[0]!r} is not a guarded read — `reads` names a "
                f"tool policy.read_refusal holds the read-only shape of "
                f"({', '.join(policy.GUARDED_READS)})")
        self.name = name
        #: The one tier of finrail code this scope may edit, or "" for any. A change
        #: that needs two tiers is two tasks (docs/LAYERS.md, "how it is enforced").
        self.tier = tier
        self.tools = tuple(tools)
        self.capabilities = tuple(capabilities) or ("*",)
        self.write_roots = tuple(_abs(p) for p in write_roots)
        self.shell = shell
        self.repos = tuple(_abs(p) for p in repos)
        self.dev_root = _abs(dev_root) if dev_root else ""
        self.cwd = _abs(cwd) if cwd else ""
        self.commit_roots = tuple(_abs(p) for p in commit_roots)
        #: Ask the person, instead of refusing, before a Write or Edit outside the
        #: writable roots (core/questions.py). Off unless a scope says so. A protected
        #: path or another tier is still refused outright: those are not questions.
        self.ask_outside = bool(ask_outside)
        #: Run the engine's shell inside the engine's own OS sandbox (`sandbox_settings`):
        #: what the shell and everything it starts may write is then the roots, not
        #: whatever the machine's user may. The gate judges a command by its words; a
        #: scope that may run code (the developer shell runs tests) can write anything
        #: its user can, including the answer to its own question, unless the OS says
        #: otherwise (review, 5 Oct). So a developer scope asks only when sandboxed.
        self.sandbox = bool(sandbox)
        #: The hosts the sandboxed shell may reach (git remotes). Nothing else.
        self.net = tuple(str(h) for h in net)
        #: More paths the sandboxed shell may not even see: folders holding local
        #: sockets on this host (see `sandbox_settings`).
        self.hide = tuple(_abs(h) for h in hide)
        #: Tools the gate may hand over in a read-only shape (policy.GUARDED_READS), e.g.
        #: production's sqlite3 under -readonly -safe (main, 67d5cc1).
        self.reads = tuple(reads)
        #: The branch `repo.land` may land on for this scope. "" reads as main.
        self.mainline = str(mainline or "").strip() or "main"

    def asks(self) -> bool:
        """Is a write outside the roots a question, rather than a refusal?"""
        return self.ask_outside and (self.sandbox or self.shell != "developer")

    def questions(self) -> bool:
        """May the agent ask the person a question of its own (which folder, which
        option)? Unlike `asks()`, this grants nothing: the answer is words for the
        model, and every action it then takes is judged as before."""
        return QUESTION_TOOL in self.tools

    def allows(self, capability: str) -> bool:
        """Is this capability inside the scope? The gate's own question, by name.

        Lives on the Scope because two callers now ask it: the gate below, judging an
        `finrail do <cap>` before it runs, and `converse.help`, telling a person what
        their agent may dispatch. A second glob matcher somewhere else would be free to
        disagree with the one that actually refuses things, and the answer a person is
        shown has to be the answer the gate gives.

        A pattern beginning `!` EXCLUDES, and an exclusion beats every grant. Without one
        the only way to hold "the whole surface except this" was to enumerate the surface,
        which goes stale the day a capability is added — the precise failure `META_VERBS`
        exists to avoid for discovery. It is what a coding agent's own list laments in its first
        comment: `apps.*` would take `apps.remove` with it, so nineteen app entries are
        written out one at a time.

        Deny-first, so the order of the patterns cannot change the answer. Two lists that
        disagree about a name have to resolve the same way whichever was written first,
        because the one that refuses is the one stating a boundary.
        """
        cap = str(capability)
        if any(fnmatch.fnmatchcase(cap, pat[1:])
               for pat in self.capabilities if pat.startswith("!")):
            return False
        return any(fnmatch.fnmatchcase(cap, pat)
                   for pat in self.capabilities if not pat.startswith("!"))

    def patterns_for_refusal(self) -> str:
        """The capability patterns as a REFUSAL names them, elided when there are many.

        A refusal exists to be read and acted on. Both deny branches below used to
        interpolate the whole list, which is right for a short one — `(records.*)` is the
        most useful thing on the line — and wrong for a coding agent's fifty-five, where the
        reason drowned in eight hundred characters of patterns. A long list becomes its
        count and the one command that prints it.

        Lives on the Scope because both refusals need it and a second copy of the rule
        would be free to disagree about where the line falls. `describe()` is NOT a
        refusal and still names every pattern: the briefing is where an agent is given
        the real list, up front, so it never has to be refused to learn it.
        """
        if len(self.capabilities) <= REFUSAL_PATTERNS_MAX:
            return ", ".join(self.capabilities)
        return (f"{len(self.capabilities)} patterns — "
                f"finrail get agent.scopes name={self.name} lists them")

    def to_dict(self) -> dict:
        return {"name": self.name, "tools": list(self.tools),
                "capabilities": list(self.capabilities),
                "write_roots": list(self.write_roots), "shell": self.shell,
                "repos": list(self.repos), "dev_root": self.dev_root, "cwd": self.cwd,
                "commit_roots": list(self.commit_roots), "tier": self.tier,
                "ask_outside": self.ask_outside, "sandbox": self.sandbox,
                "net": list(self.net), "hide": list(self.hide),
                "reads": list(self.reads), "mainline": self.mainline}

    def for_tier(self, tier: str) -> "Scope":
        """This scope, narrowed to one tier of finrail code. Same roots, same tools:
        only what the gate says about a file under `finrail/` changes."""
        return Scope.from_dict({**self.to_dict(), "tier": tier})

    @classmethod
    def from_dict(cls, d: dict) -> "Scope":
        if not isinstance(d, dict) or not d.get("name"):
            raise ContractViolation("a scope needs at least a name")
        return cls(d["name"], d.get("tools") or (), d.get("capabilities") or ("*",),
                   d.get("write_roots") or (), d.get("shell") or "reads",
                   d.get("repos") or (), d.get("dev_root") or "", d.get("cwd") or "",
                   d.get("commit_roots") or (), d.get("tier") or "",
                   bool(d.get("ask_outside")), bool(d.get("sandbox")),
                   d.get("net") or (), d.get("hide") or (),
                   reads=d.get("reads") or (), mainline=d.get("mainline") or "")

    # -- what the engine is told, in words -------------------------------------
    def describe(self) -> str:
        """The scope as a paragraph for the briefing. Generated from the declaration, so
        the agent is told exactly what the gate enforces and nothing else."""
        grants = [p for p in self.capabilities if not p.startswith("!")]
        blocked = [p[1:] for p in self.capabilities if p.startswith("!")]
        caps = ("any finrail capability" if "*" in grants else
                "finrail capabilities matching " + ", ".join(grants))
        lines = [f"Tools available to you: {', '.join(self.tools)}. Anything else is refused.",
                 f"Through the shell you may run {caps}, and the read-only tools "
                 f"({', '.join(policy.SAFE_READS[:8])}, …). Discover what exists with "
                 f"`finrail capabilities` and read a capability's real parameters with "
                 f"`finrail -h2 <capability>`; do not guess a name or a parameter.",]
        if self.reads:
            # The shape, not just the name: a guarded read is refused in every other form,
            # so an agent given the name alone would discover the form by being refused.
            forms = [policy.GUARDED_READ_FORMS.get(t, t) for t in self.reads]
            lines.append(f"You may also run, as reads and only in these forms: "
                         f"{'; '.join(forms)}. Any other shape of them is refused.")
        if blocked:
            # Its own sentence, and in the briefing rather than only in a refusal: an
            # agent not told what is excluded finds out by being refused, and the refusal
            # arrives mid-task, after it has planned around something it was never going
            # to get.
            lines.append(f"Excluded, whatever else matches: {', '.join(blocked)}.")
        lines.append("You may write files only under: "
                     + (", ".join(self.write_roots) if self.write_roots else "nowhere")
                     + ". Reading is unrestricted.")
        if self.tier:
            lines.append(
                f"This task is in the {self.tier} tier of finrail "
                f"({', '.join(TIERS[self.tier])}). You may edit finrail code only there, "
                f"plus tests and docs. A change that needs another tier is another task: "
                f"say so and stop, rather than reaching across.")
        if self.shell == "site":
            repo = self.repos[0] if self.repos else "<the repository>"
            lines.append(
                f"The site you keep is {', '.join(self.repos)}. You change it with the "
                f"file tools; through the shell you may only LOOK at it — one command per "
                f"call, never chained, redirected or substituted — plus `rm <one file>` "
                f"to delete a page and `git -C {repo} reset --hard HEAD~1` to undo your "
                f"last commit. Anything else there is refused with the reason.")
        if self.commit_roots:
            lines.append(
                f"Everything you write under {', '.join(self.commit_roots)} is committed "
                f"and pushed for you, as '{self.name}: update <path>', the moment you "
                f"write it. You do not run git to save your work, and there is no "
                f"uncommitted state to worry about.")
        if self.shell == "developer":
            lines.append(
                f"You may work on these repositories: {', '.join(self.repos)}. Each stays "
                f"on `main`; every change goes through a worktree under {self.dev_root} "
                f"on its own branch, and reaches `{self.mainline}` only through "
                f"`repo.land`. git, tests and the "
                f"tools of a working tree run inside a worktree; outside one they do not.")
            if self.mainline != "main":
                lines.append(
                    f"YOUR LANDINGS GO TO `{self.mainline}`, NOT `main`. Start each "
                    f"worktree from it (git -C <repo> worktree add {self.dev_root}/<topic> "
                    f"-b <topic> origin/{self.mainline} --no-track) and land with "
                    f"`finrail do repo.land repo=<repo> branch=<topic> "
                    f"mainline={self.mainline}`. Where these instructions say main for a "
                    f"landing, read `{self.mainline}`. A landing on any other branch is "
                    f"refused; a person moves `{self.mainline}` into main.")
        if self.questions():
            lines.append(
                f"When a choice is the person's to make (which folder, which of two "
                f"readings, whether to go on), ask it with {QUESTION_TOOL} rather than "
                f"guessing: it is put to them in the chat and their answer comes back to "
                f"you. If nobody answers in time you are told so; then decide yourself "
                f"only if the choice is safe to undo, otherwise stop and say what you need.")
        lines.append("Every tool call is judged by code before it runs. A refusal names its "
                     "reason; read it and do the permitted thing instead of retrying.")
        return "\n".join(lines)


# ── the decision ─────────────────────────────────────────────────────────────
def decide(scope: Scope, tool: str, params, cwd: str = "") -> dict:
    """{decision, reason} for one tool call. The only place a scope is interpreted."""
    tool = str(tool or "")
    params = params if isinstance(params, dict) else {}
    if not tool:
        return _deny("no tool named in the payload — failing closed")
    if tool not in scope.tools:
        return _deny(f"{scope.name} does not use the '{tool}' tool; "
                     f"it has {', '.join(scope.tools)}")
    if tool == QUESTION_TOOL:
        return {"decision": ASK, "reason": f"{scope.name} has a question for the person",
                "clarify": True}
    if tool in READ_TOOLS:
        return _allow(f"'{tool}' only reads")
    if tool in NET_TOOLS:
        return _allow(f"'{tool}' reads the network, and {scope.name} lists it")
    if tool in WRITE_TOOLS:
        target = params.get("file_path") or params.get("notebook_path") or ""
        if not target:
            return _deny(f"'{tool}' named no file — failing closed")
        p = _resolve(target, cwd or scope.cwd)
        if _protected(p):
            return _deny(_protected(p))
        if _gate_refusal(scope, p):
            return _deny(_gate_refusal(scope, p))
        if _under_any(p, scope.write_roots):
            if tier_refusal(scope, p):
                return _deny(tier_refusal(scope, p))
            return _allow(f"{p} is inside a writable root"
                          + (f" and in the {scope.tier} tier"
                             if scope.tier and tier_of(p) else ""))
        # Never a question inside a repository's own checkout: those stay on main and
        # change only through a worktree, a rule a person should not be asked to waive
        # mid-turn. Nor about another tier's code, which no answer makes one task.
        # Everywhere else outside the roots may be asked about.
        if tier_refusal(scope, p):
            return _deny(tier_refusal(scope, p))
        if _never_asked(p):
            return _deny(_never_asked(p))
        if scope.asks() and not _under_any(p, scope.repos):
            # The ASK is what the person reads in the chat, as the question's headline.
            # The file is its subject and stays; the writable roots do not: they are
            # machine detail, the agent was briefed with them (`describe`), and with
            # a coding agent's three they made one sentence of four absolute paths (review,
            # 5 Oct). The refusal below goes to the model, and keeps them.
            return _ask(f"{scope.name} wants to {('edit' if tool != 'Write' else 'write')} "
                        f"{p}, outside where it may write")
        return _deny(f"{scope.name} may only write under "
                     f"{', '.join(scope.write_roots) or 'nowhere'} — not {p}")
    if tool == "Bash":
        if params.get("run_in_background"):
            # A BACKGROUND SHELL DIES WITH THE TURN. The engine kills its background
            # shells when the turn ends, so work started this way was lost, and the agent
            # went on saying it was running (8 Oct). Detached through finrail it outlives
            # the turn and its ending is said in the thread (dispatch_core.report_back).
            return _deny("a background shell is killed when this turn ends; start long "
                         "work with `finrail do <capability> ... --detach`, which keeps "
                         "running after the turn and reports back in this conversation")
        return decide_shell(scope, params.get("command") or "", cwd or scope.cwd)
    if tool in ("Task", "Agent"):
        # A subagent inherits this session's hooks, so the same gate judges its calls.
        return _allow("a subagent runs under the same gate")
    return _deny(f"'{tool}' is listed for {scope.name} but this gate has no rule for it "
                 f"— failing closed")


def decide_shell(scope: Scope, command: str, cwd: str = "") -> dict:
    """A shell command, segment by segment: the worst part decides."""
    text = (command or "").strip()
    if not text:
        return _deny("empty command")
    cwd = _abs(cwd) if cwd else scope.cwd
    if scope.shell == "site":
        repo = next((r for r in scope.repos if r in text), "")
        if repo:
            # Judged whole, not segment by segment: inside the site the chaining IS the
            # thing refused, and a chain whose parts are each harmless is not.
            return _decide_site(scope, text, repo)
    segs = [s for s in policy._segments(text) if s]
    if scope.sandbox:
        outside = [s for s in segs if _leaves_sandbox(s)]
        if outside and (len(segs) > 1 or text.endswith("&") or _hides_a_command(text)):
            # These run outside the sandbox (`sandbox_settings`). A chain would carry the
            # rest of the line out with them, and so would a `$(…)` inside double quotes,
            # which the shell still runs. A single `&` is a
            # chain like any other and the splitter cuts there (`finrail caps & python3
            # x.py` was one segment once, and went out whole); one at the very end leaves
            # no second segment to count, so it is named here: on its own means not
            # sent to the background either.
            head = _words(outside[0]) or outside[0].split()
            return _deny(f"'{' '.join(head[:2])}' runs outside the sandbox, "
                         f"so it runs on its own: no chain, pipe or substitution, and "
                         f"not in the background (&)")
    last = None
    for seg in segs:
        # `cd` moves the rest of the chain; it is judged (a `cd` out of bounds is refused)
        # and then remembered. The first refusal decides; otherwise the last segment's
        # reason is the one worth reading, because `cd x && git merge` is about the merge.
        d, cwd = _decide_segment(scope, seg, cwd)
        if d["decision"] != ALLOW:
            return d
        last = d
    return last or _deny("nothing to run")


def _decide_segment(scope: Scope, seg: str, cwd: str) -> tuple:
    # Output thrown away is not a write (policy.without_devnull).
    seg = policy.without_devnull(seg)
    redir = _redirections(seg)
    if redir is None:
        return _deny("unreadable redirection — failing closed"), cwd
    for target in redir:
        p = _resolve(target, cwd)
        if _protected(p):
            return _deny(_protected(p)), cwd
        if _gate_refusal(scope, p):
            return _deny(_gate_refusal(scope, p)), cwd
        if not _under_any(p, scope.write_roots):
            return _deny(f"redirection writes {p}, outside the writable roots "
                         f"({', '.join(scope.write_roots) or 'none'})"), cwd
        if tier_refusal(scope, p):
            return _deny(tier_refusal(scope, p)), cwd
    words = _words(_strip_redirections(seg))
    if not words and seg.strip():
        # Since the splitter cuts at `$(` and a backtick inside "…" (review, 16 Oct), the
        # piece before one ends in an open quote. "nothing to run" told the model nothing.
        return _deny(f"unreadable quoting in `{seg.strip()[:60]}`: a quote left open, "
                     f"or a $(…) or backtick inside \"…\", which runs a command of its "
                     f"own. Run that command as its own step"), cwd
    if not words:
        return _deny("nothing to run"), cwd
    head = os.path.basename(words[0])

    if head == "cd":
        target = _resolve(words[1] if len(words) > 1 else "~", cwd)
        if scope.shell == "developer" and not _under_any(target, scope.write_roots
                                                          + scope.repos
                                                          + ((scope.dev_root,) if scope.dev_root else ())):
            return _deny(f"cd {target}: outside the places {scope.name} works in"), cwd
        return _allow(f"cd {target}"), str(target)

    if head == "finrail":
        return _decide_finrail(scope, seg, words), cwd

    # The scope's own guarded reads are judged by the same call: `read_refusal` is where
    # "is this use a read" is answered, for a tool every scope has and for one only this
    # scope has alike.
    if head in policy.SAFE_READS or head in scope.reads:
        refused = policy.read_refusal([head] + words[1:])
        if refused:
            return _deny(refused), cwd
        return _allow(f"'{head}' is a read-only tool"), cwd

    if scope.shell == "developer":
        return _decide_developer(scope, seg, words, head, cwd), cwd

    return _deny(f"'{head}' is not available to {scope.name}: it runs finrail and the "
                 f"read-only tools only"), cwd


def _decide_finrail(scope: Scope, seg: str, words: list) -> dict:
    """`finrail …` — the global policy first (the full agent always asks, and there is
    nobody here to answer), then the scope's own capability patterns."""
    g = policy.resolve(seg)
    if g["decision"] != policy.ALLOW:
        return _deny(f"finrail policy: {g['reason']}")
    rest = [w for w in words[1:] if not w.startswith("--")]
    if not rest:
        return _allow("finrail usage")
    verb = rest[0]
    if verb in META_VERBS or verb.startswith("--h2="):
        return _allow(f"finrail {verb} describes the system")
    if verb == "approve":
        # Approving is acting: only a run whose capability is in scope, and never one
        # that waits on a person; the agent is not the person (review, 5 Oct: the
        # capability was checked and the run's own `decision` was ignored).
        ids = re.findall(r"\b([0-9a-f]{6,8})\b", " ".join(rest[1:]))
        if not ids:
            return _deny("approve without a run id")
        for rid in ids:
            info = policy.run_needs_confirm(rid)
            cap = info.get("capability") or ""
            if not cap or not _in_scope(scope, cap):
                return _deny(f"run {rid} is {cap or 'unknown'}, outside {scope.name}'s scope")
            if info.get("decision") != policy.ALLOW:
                return _deny(f"run {rid} ({cap}) waits on a person's approval; "
                             f"{scope.name} cannot give it")
        return _allow(f"approving {len(ids)} run(s) that need no person")
    if verb in DISPATCH_VERBS:
        cap = rest[1] if len(rest) > 1 else ""
        if not cap:
            return _deny(f"finrail {verb} without a capability")
        if _in_scope(scope, cap):
            refused = _capability_refusal(scope, cap, words[1:])
            if refused:
                return _deny(refused)
            return _allow(f"{cap} is within {scope.name}'s scope")
        return _deny(f"{cap} is outside {scope.name}'s scope "
                     f"({scope.patterns_for_refusal()}); finrail capabilities lists "
                     f"what is in it")
    # Everything else — settings, secret, plugin, cron, kill, service — is management.
    # `finrail settings set x=y` and `finrail do settings.set name=x value=y` are the
    # same act, so they get the same answer: resolve the verb to the capability the
    # entrypoint would dispatch and judge THAT, which is the property `Scope.allows`
    # exists to guarantee.
    cap = _management_capability(verb, rest[1] if len(rest) > 1 else "")
    if cap:
        if _in_scope(scope, cap):
            # The same capability, so the same questions: `finrail repo land` reached
            # repo.land without the landing's own checks (review, 8 Oct).
            refused = _capability_refusal(scope, cap, words[1:])
            if refused:
                return _deny(refused)
            return _allow(f"finrail {verb} reaches {cap}, within {scope.name}'s scope")
        return _deny(f"finrail {verb} reaches {cap}, outside {scope.name}'s scope "
                     f"({scope.patterns_for_refusal()}); finrail capabilities lists "
                     f"what is in it")
    # A verb that reaches NO capability (cron, kill, service) is judged as the bare verb
    # against the same patterns. It used to be all-or-nothing — `*` or nothing — which
    # made `finrail kill`, stopping a runaway run, ungrantable: no entry a narrow scope
    # could carry reached it. The bare verb is the only name such a command has, so it
    # is the name the scope has to list. `*` matches it as it matches everything, so an
    # unconstrained scope answers exactly as before, and a list of dotted patterns
    # matches no bare verb, so the default is still deny.
    if _in_scope(scope, verb):
        return _allow(f"finrail {verb} reaches no capability; the verb itself is "
                      f"within {scope.name}'s scope")
    return _deny(f"finrail {verb} is management and reaches no capability; the verb "
                 f"itself is outside {scope.name}'s scope "
                 f"({scope.patterns_for_refusal()})")


#: Capabilities that run git on the repository their `repo=` names, outside the sandbox.
REPO_CAPABILITIES = ("repo.land", "repo.tidy")


def _cli_params(argv: list):
    """The parameters the CLI itself will read from this command line, or None when they
    cannot be read. `entrypoint._parse` and nothing else: the gate read `k=v` words and
    dropped every `--` word, and the CLI reads `--repo=x` as `repo=x`, the last spelling
    winning, so a second `--repo=` or `--mainline=` walked past every check below
    (review of the fixes, 8 Oct)."""
    try:
        from .entrypoint import _parse
        return _parse(list(argv))[1]
    except Exception:                                               # noqa: BLE001
        return None


def _capability_refusal(scope: Scope, cap: str, argv: list) -> str:
    """What this one capability's parameters are judged for, beyond being in scope, or
    "". One place, whichever way the command is spelled (`do`, `get`, the bare verb)."""
    if cap not in REPO_CAPABILITIES:
        return ""
    kv = _cli_params(argv)
    if kv is None:
        return f"cannot read {cap}'s parameters as the CLI will, failing closed"
    refused = _repo_refusal(scope, cap, kv)
    if refused:
        return refused
    if cap == "repo.land":
        return _land_refusal(scope, kv)
    return ""


def _repo_refusal(scope: Scope, cap: str, kv: dict) -> str:
    """A repository this scope does not list, named to a capability that acts on it.

    `repo.tidy` runs autonomously, so no person reads the repository it is given; it
    took any checkout on the machine (review, 8 Oct). A scope that lists its
    repositories is held to them, by absolute path: a name or a relative path is
    resolved by the capability against the install's settings and its own folder, which
    this cannot see, so the two could mean different repositories. `repo.tidy` must name
    one; a landing that names none is left to the person who approves it, who reads the
    repository in the proposal."""
    if not scope.repos:
        return ""
    given = str(kv.get("repo") or "").strip()
    listed = ", ".join(scope.repos)
    if not given:
        return ("" if cap == "repo.land" else
                f"name the repository: {cap} repo=<one of {listed}>")
    if not os.path.isabs(os.path.expanduser(given)):
        return f"{cap} takes the repository's full path (one of {listed}), not {given}"
    if _abs(given) in scope.repos:
        return ""
    return f"{cap} acts on {given}, which is not one of {scope.name}'s repositories ({listed})"


def _land_refusal(scope: Scope, kv: dict) -> str:
    """`repo.land` is the only way a branch reaches main, so it is where the change set is
    judged last: a commit made by a script the worktree ran still has to land. `kv` is
    the CLI's own reading of the parameters (`_cli_params`)."""
    repo, branch = str(kv.get("repo") or ""), str(kv.get("branch") or "")
    onto = str(kv.get("mainline") or "main")
    if onto != scope.mainline:
        # A scope lands on one branch. The dev agents' is `dev`: their work is used there
        # before it is anybody's main, and a person moves it on. Judged here, before the
        # capability runs, because the capability itself lands wherever it is told.
        return (f"{scope.name} lands on {scope.mainline}, not {onto}: finrail do "
                f"repo.land repo={repo or '<repo>'} branch={branch or '<topic>'}"
                + ("" if scope.mainline == "main" else f" mainline={scope.mainline}"))
    if not repo or not branch:
        return ("name repo= and branch= so the landing can be judged, failing closed"
                if scope.tier else "")
    return change_refusal(scope, _abs(repo), "land", branch, onto)


def _management_capability(verb: str, sub: str) -> str:
    """The capability `finrail <verb> <sub>` dispatches, or "" if it reaches none.

    Mirrors the entrypoint's own resolution — `<verb>.<sub>` first, then `<verb>` — so
    the gate judges the capability that would actually run rather than a hand-kept list
    that is free to fall out of step with it. The registry is imported here rather than
    at module scope because the gate runs as a hook subprocess: a scope decision must
    not depend on the catalogue loading, and an install that cannot answer fails closed.
    """
    try:
        from . import registry
        for name in ((f"{verb}.{sub}" if sub else ""), verb):
            if name and registry.has(name):
                return name
    except Exception:                                            # noqa: BLE001
        return ""
    return ""


def _in_scope(scope: Scope, cap: str) -> bool:
    return scope.allows(cap)


# ── the site ruleset ────────────────────────────────────────────────────────
def rollback_command(repo: str) -> str:
    """The one command a site agent runs that changes the repository: undo its own last
    commit. Named here so the gate, the refusal and the briefing all spell it the same."""
    return f"git -C {repo} reset --hard HEAD~1"


def _decide_site(scope: Scope, text: str, repo: str) -> dict:
    """A command that names the site repository, judged as one whole command.

    The agent EDITS this tree with Write/Edit — that is what `write_roots` is for — and
    its commits are made for it by the `commit_roots` hook. What is left for the shell is
    looking: how the tree is laid out, what a page says, what changed. So the shell rules
    here are a whitelist of read-only first words, plus the two housekeeping commands
    that have no file-tool spelling — deleting a page and undoing the last commit.

    Everything else is refused, and the refusal names what IS allowed, because an agent
    that cannot learn the boundary from a refusal will spend the turn guessing at it.
    """
    if text == rollback_command(repo):
        return _allow("the rollback: undoing the last commit in the site")
    if SITE_META.search(text):
        return _deny(f"Bash inside {repo} may not chain, redirect, or substitute "
                     f"commands — one command per call")
    m = SITE_RM.fullmatch(text)
    if m and m.group(1).startswith(f"{repo}/"):
        return _allow("removing a single file from the site")
    words = _words(text)
    if not words:
        return _deny("unreadable command — failing closed")
    head = os.path.basename(words[0])
    if head in SITE_READS:
        if head == "find" and any(w in ("-delete", "-exec") for w in words[1:]):
            return _deny(f"find -delete/-exec is not read-only and is blocked inside "
                         f"{repo}")
        return _allow(f"'{head}' only looks at the site")
    if head == "git":
        # `git -C <path> <subcommand>`: the subcommand is the fourth word, and -C is on
        # every one of these, because the shell's cwd is the agent's own directory.
        rest = words[4:] if len(words) > 1 and words[1] == "-C" else words[2:]
        sub = (words[3] if len(words) > 3 and words[1] == "-C"
               else (words[1] if len(words) > 1 else ""))
        if sub in SITE_GIT_READS:
            reporting = SITE_GIT_REPORTING.get(sub)
            if reporting is not None and rest and rest[0] not in reporting:
                return _deny(f"git {sub} {rest[0]} changes the repository; branches and "
                             f"remotes are a person's to set")
            return _allow(f"git {sub} only reports")
    return _deny(f"Bash inside {repo} is limited to read-only inspection "
                 f"({', '.join(SITE_READS[:8])}, …, git "
                 f"{'/'.join(SITE_GIT_READS[:4])}/…), a plain single-file rm, and the "
                 f"rollback ('{rollback_command(repo)}'). Edit the site with Write/Edit; "
                 f"your changes are committed for you.")


# ── the developer ruleset ───────────────────────────────────────────────────
def _decide_developer(scope: Scope, seg: str, words: list, head: str, cwd: str) -> dict:
    if head == "git":
        return _decide_git(scope, words, cwd)
    if head == "sed" and any(w == "-i" or w.startswith("-i") and not w.startswith("--")
                             for w in words[1:]):
        return _deny("sed -i edits in place; use the Edit tool inside a writable root")
    if head in DEV_READS:
        refused = policy.read_refusal([head] + words[1:])
        if refused:
            return _deny(refused)
        return _allow(f"'{head}' is a read-only tool")
    if head in MUTATORS:
        paths = [w for w in words[1:] if not w.startswith("-")]
        if head == "cp" and paths:
            paths = paths[-1:]                       # the source may be anywhere
        if not paths:
            return _deny(f"{head} with no path")
        for w in paths:
            p = _resolve(w, cwd)
            if _protected(p):
                return _deny(_protected(p))
            if _gate_refusal(scope, p):
                return _deny(_gate_refusal(scope, p))
            if not _under_any(p, scope.write_roots):
                return _deny(f"{head} {p}: outside the writable roots "
                             f"({', '.join(scope.write_roots)})")
            if tier_refusal(scope, p):
                return _deny(tier_refusal(scope, p))
        return _allow(f"{head} inside a writable root")
    if head in DEV_RUNNERS or head.endswith(("/python", "/python3", "/pytest", "/finrail")):
        if head.endswith("/finrail"):
            # A worktree's own build of the CLI: the same rules as the installed one.
            return _decide_finrail(scope, seg, words)
        if _is_tool_install(words):
            return _deny("the live install is deployed from a commit by "
                         "`finrail do upgrade`, never installed by hand")
        if scope.dev_root and _under_any(pathlib.Path(cwd or "/"), (scope.dev_root,)):
            return _allow(f"'{head}' inside a worktree under {scope.dev_root}")
        return _deny(f"'{head}' runs code; {scope.name} runs it inside a worktree under "
                     f"{scope.dev_root} (cd there first)")
    if head in ("sudo", "su", "systemctl", "kubectl", "vault", "docker", "curl", "wget",
                "ssh", "scp", "rsync", "bash", "sh", "zsh", "eval", "xargs", "nohup"):
        return _deny(f"'{head}' is not available to {scope.name}")
    return _deny(f"'{head}' is not on {scope.name}'s allowlist (default deny)")


def _is_tool_install(words: list) -> bool:
    """`uv tool install …` — what replaces the live CLI. It used to be allowed for an
    allowed repo, as `--editable`, when merging was deploying; that made the running
    system a pointer at a working tree. `finrail upgrade` installs a copy of a commit
    and restarts what depends on it, so a hand install is refused wherever it is run,
    a worktree included."""
    return os.path.basename(words[0]) == "uv" and words[1:3] == ["tool", "install"]


#: git subcommands that only read. Allowed in any directory.
GIT_READS = ("status", "log", "diff", "show", "branch", "remote", "ls-files", "ls-tree",
             "rev-parse", "describe", "blame", "shortlog", "show-ref", "fetch",
             "cat-file", "name-rev", "reflog", "config", "stash", "tag", "grep",
             "worktree", "merge-base", "cherry", "count-objects", "check-ignore")


#: Options that turn a git read into running a program or writing a file: the program
#: that answers a fetch, a pager for `grep`, a file for `diff`/`log`/`show` output.
#: `-c` is refused for the same reason (`_decide_git`). Inside the sandbox these could
#: do no more than the sandbox allows; on an install without one, a "read" ran any
#: program as the agent's user (review, 8 Oct: `git fetch --upload-pack=<prog>`).
GIT_READ_RUNS = ("--upload-pack", "--receive-pack", "--exec", "--open-files-in-pager",
                 "--output")
#: `git config` options and verbs that write it. A written config is a program run
#: later by every git that reads it (core.fsmonitor, core.sshCommand, core.hooksPath).
_CONFIG_WRITES = ("--add", "--unset", "--unset-all", "--replace-all", "--rename-section",
                  "--remove-section", "--edit")
_CONFIG_VERBS_WRITE = ("set", "unset", "rename-section", "remove-section", "edit")
_CONFIG_READS = ("--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list")


#: `git config` options that take the next word as their value.
_CONFIG_VALUES = ("-f", "--file", "--blob", "--type", "--default", "--comment")


def _names_option(word: str, options) -> bool:
    """Does this word spell one of these long options? git takes any unambiguous start
    of one (`--upl` is `--upload-pack`, `--op` is `--open-files-in-pager` to grep), so
    any word that begins one counts (review of the fixes, 8 Oct). `--` alone is the
    pathspec separator, not an option."""
    name = word.split("=", 1)[0]
    return name.startswith("--") and len(name) > 2 and any(o.startswith(name)
                                                            for o in options)


def _git_read_refusal(sub: str, tail: list) -> str:
    """Why a git subcommand on the read list is not a read as written, or ""."""
    for t in tail:
        if (_names_option(t, GIT_READ_RUNS)
                or (sub == "grep" and t.startswith("-") and not t.startswith("--")
                    and "O" in t)):
            return (f"git {sub} {t.split('=', 1)[0]} runs a program or writes a file; "
                    f"it is not a read")
    if sub != "config":
        return ""
    plain, skip = [], False
    for t in tail:
        if skip:
            skip = False
        elif t in _CONFIG_VALUES:
            skip = True
        elif not t.startswith("-"):
            plain.append(t)
    shorts = "".join(t[1:] for t in tail if t.startswith("-") and not t.startswith("--"))
    if (any(_names_option(t, _CONFIG_WRITES) for t in tail) or "e" in shorts
            or (plain and plain[0] in _CONFIG_VERBS_WRITE)):
        return "git config only reads here: a person sets git's configuration"
    reads = (any(_names_option(t, _CONFIG_READS) for t in tail) or "l" in shorts
             or (plain and plain[0] in ("get", "list")))
    if not reads and len(plain) > 1:
        return ("git config <key> <value> writes git's configuration, which a person "
                "sets; read one with git config --get <key>")
    return ""


def _decide_git(scope: Scope, words: list, cwd: str) -> dict:
    """The DEVELOPMENT.md rules, as code.

    `~/src/<repo>` is always on main and never branched: the only writes allowed there
    are the ones the document names — open or remove a worktree, delete the merged
    branch. Main moves only through `repo.land`, which merges and pushes behind its own
    lock. Inside a worktree under `dev_root` git is free, short of rewriting history that
    has been shared."""
    args = words[1:]
    where = pathlib.Path(cwd or "/")
    if len(args) >= 2 and args[0] == "-C":
        where = _resolve(args[1], cwd)
        args = args[2:]
    if not args:
        return _allow("git usage")
    sub = args[0]
    tail = args[1:]
    if sub in ("-c", "--git-dir", "--work-tree"):
        return _deny("git with a redirected tree is not judged — run it plainly")

    in_worktree = bool(scope.dev_root) and _under_any(where, (scope.dev_root,))
    repo = next((r for r in scope.repos if _under_any(where, (r,))), "")

    # reads: anywhere, with the two that are only half read-only pinned down
    if sub in GIT_READS:
        refused = _git_read_refusal(sub, tail)
        if refused:
            return _deny(refused)
        if sub == "worktree":
            return _decide_worktree(scope, tail, repo, in_worktree, where)
        if sub == "branch" and any(t in ("-d", "-D", "--delete", "-m", "-M") for t in tail):
            return _decide_branch_delete(scope, tail, repo, in_worktree)
        if sub == "stash" and tail and tail[0] not in ("list", "show"):
            return (_allow("stash in a worktree") if in_worktree
                    else _deny("git stash changes the tree; only inside a worktree"))
        if sub == "config" and any(t in ("--global", "--system") for t in tail):
            return _deny("git config --global/--system changes the machine's identity")
        if sub == "tag" and tail and not tail[0].startswith("-l"):
            return _deny("tags are cut by a person")
        if sub == "remote" and tail and tail[0] not in ("-v", "show", "get-url"):
            return _deny("remotes are configured by a person")
        return _allow(f"git {sub} reads")

    if "--force" in tail or "-f" in tail or "--force-with-lease" in tail:
        return _deny("force is never allowed: it rewrites history someone else may hold")

    if in_worktree:
        if sub in ("commit", "push"):
            refused = change_refusal(scope, where, sub)
            if refused:
                return _deny(refused)
        if sub in ("mv", "rm"):
            # These two name their paths, so they are judged as they run, like `mv` and
            # `rm`; `apply`, `stash` and `checkout -- <path>` do not, and the commit that
            # has to follow them is where their change set is judged.
            for t in (t for t in tail if not t.startswith("-")):
                refused = tier_refusal(scope, _resolve(t, str(where)))
                if refused:
                    return _deny(refused)
        if sub == "push":
            targets = [t for t in tail if not t.startswith("-")]
            lines_ = ("main", "master", scope.mainline)
            if any(t in lines_ or t.endswith(tuple(f":{m}" for m in lines_)) or
                   t.endswith(f"refs/heads/{scope.mainline}") for t in targets):
                return _deny("a worktree pushes its own branch; main is pushed from the "
                             "checkout after the merge")
            return _allow("push the topic branch from its worktree")
        if sub in ("checkout", "switch") and any(
                t in ("main", "master", scope.mainline) for t in tail):
            return _deny("a worktree stays on its topic branch; main lives in the checkout")
        return _allow(f"git {sub} inside a worktree")

    if repo:
        return _decide_mainline(scope, sub, tail, repo)

    return _deny(f"git {sub} in {where}: neither a worktree under {scope.dev_root} nor "
                 f"an allowed repository")


#: `git worktree add` options that take the next word as their value.
_WORKTREE_ADD_VALUES = ("-b", "-B", "--reason")


def _decide_worktree(scope: Scope, tail: list, repo: str, in_worktree: bool,
                     where=None) -> dict:
    if not tail or tail[0] in ("list", "prune"):
        return _allow("git worktree list/prune")
    op = tail[0]
    paths = [t for t in tail[1:] if not t.startswith("-")]
    if op == "add":
        if not repo and not in_worktree:
            return _deny("git worktree add from outside an allowed repository")
        if not paths or not _under_any(_abs(paths[0]), (scope.dev_root,)):
            return _deny(f"worktrees live under {scope.dev_root}: "
                         f"git worktree add {scope.dev_root}/<topic> -b <topic>")
        if "-b" not in tail and "-B" not in tail:
            return _deny("a worktree is a branch: git worktree add <path> -b <topic>")
        if scope.sandbox and _tracks_upstream(tail, where):
            # A new branch that tracks a remote one is written into .git/config
            # (`branch.<topic>.remote` and `.merge`), which the sandbox keeps shut. git
            # makes the branch anyway and prints the failure twice (measured on the
            # team server, 6 Oct), so this says it plainly instead.
            return _deny("a branch started from a remote branch would track it, and that "
                         "is written into .git/config, which the sandbox keeps shut: add "
                         "--no-track, or start from main")
        return _allow("a new worktree on its own branch")
    if op == "remove":
        if not paths or not _under_any(_abs(paths[0]), (scope.dev_root,)):
            return _deny(f"only worktrees under {scope.dev_root} are removed here")
        return _allow("removing a worktree after its merge")
    if op in ("move", "lock", "unlock", "repair"):
        return _deny(f"git worktree {op} is not part of the loop")
    return _deny(f"git worktree {op}: unknown operation")


def _decide_branch_delete(scope: Scope, tail: list, repo: str, in_worktree: bool) -> dict:
    names = [t for t in tail if not t.startswith("-")]
    if any(n in ("main", "master") for n in names):
        return _deny("main is never deleted or renamed")
    if "-D" in tail:
        return _deny("git branch -D discards unmerged work; -d refuses to, which is the "
                     "point")
    if "-m" in tail or "-M" in tail:
        return _deny("branches are not renamed mid-loop")
    if scope.sandbox and (repo or in_worktree):
        # `git branch -d` rewrites .git/config on every deletion, to drop the branch's
        # section whether it has one or not, and the sandbox keeps that file shut. The ref
        # went and git said "could not write config file .git/config: Device or resource
        # busy" (a coding agent-dev, 6 Oct). The deletion runs outside the sandbox instead.
        where = repo or "<repo>"
        return _deny(f"git branch -d rewrites .git/config, which your sandbox may not "
                     f"write. Delete a merged branch with: finrail do repo.tidy "
                     f"repo={where} branch={names[0] if names else '<topic>'}")
    if repo or in_worktree:
        return _allow("deleting a merged topic branch")
    return _deny("git branch -d outside an allowed repository")


def _tracks_upstream(tail: list, where) -> bool:
    """Would `git worktree add <tail>` record an upstream for its new branch? It does when
    asked to (`--track`) and, by git's default (`branch.autoSetupMerge`), when the start
    point is a remote-tracking branch such as `origin/main`. Unreadable counts as yes:
    the answer only decides whether to ask for `--no-track`, which is always safe."""
    if "--no-track" in tail:
        return False
    if "--track" in tail:
        return True
    words, skip = [], False
    for t in tail[1:]:
        if skip:
            skip = False
        elif t in _WORKTREE_ADD_VALUES:
            skip = True
        elif not t.startswith("-"):
            words.append(t)
    if len(words) < 2:
        return False                    # no start point: the new branch starts at HEAD
    ok, full = _git_out(where or ".", "rev-parse", "--symbolic-full-name", words[1])
    return (not ok) or full.strip().startswith("refs/remotes/")


def _decide_mainline(scope: Scope, sub: str, tail: list, repo: str) -> dict:
    """In `~/src/<repo>`: only the cleanup of DEVELOPMENT.md step 6.

    The merge and the push of main used to be allowed here, in the `--no-ff` shape.
    Several agents land on this codebase at once, and a merge by hand tests a main that
    may already be gone; `repo.land` holds the lock, rebases, runs the suite on the tree
    that lands and only then merges and pushes. So both are refused with that name."""
    targets = [t for t in tail if not t.startswith("-")]
    land = f"finrail do repo.land repo={repo} branch=<topic>" + (
        "" if scope.mainline == "main" else f" mainline={scope.mainline}")
    if sub == "merge":
        return _deny(f"main moves only through repo.land: {land}")
    if sub == "push":
        if "--delete" in tail or "-d" in tail:
            if any(t in ("main", "master") for t in targets):
                return _deny("main is never deleted")
            return _allow("deleting a merged topic branch on the remote")
        return _deny(f"main is pushed by repo.land, not from the checkout: {land}")
    if sub == "pull":
        return (_allow("fast-forward pull of main") if "--ff-only" in tail
                else _deny("pull with --ff-only; main is never rebased or merged into"))
    if sub in ("checkout", "switch", "reset", "rebase", "commit", "add", "rm", "mv",
               "restore", "revert", "cherry-pick", "am", "apply", "clean", "filter-branch"):
        return _deny(f"git {sub} in {repo}: the checkout stays on main and is never "
                     f"edited directly — work in a worktree under {scope.dev_root}")
    return _deny(f"git {sub} in {repo}: not part of the land-and-clean loop")


# ── the change a commit, a push or a landing carries ─────────────────────────
#: Where main is, in the order a worktree is likely to have it.
MAINLINES = ("origin/main", "main", "origin/master", "master")

#: The two files the layer checks read their allowances from. Both only tighten: the
#: baseline lists the crossings that exist and loses a line when one is fixed; the floor
#: is the least a layer run may collect and rises when tests are added.
BASELINE_FILE = "tests/layers_baseline.txt"
STORES_BASELINE_FILE = "tests/stores_baseline.txt"
RUNTIME_BASELINE_FILE = "tests/runtime_baseline.txt"
FLOOR_FILE = "tests/layers_floor.json"


def _git_out(where, *args) -> tuple:
    """(ok, stdout) of one git read in `where`. Never raises."""
    try:
        r = subprocess.run(["git", "-C", str(where), *args], capture_output=True,
                           text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return r.returncode == 0, r.stdout


def _mainline(where, prefer: str = "") -> str:
    for ref in ((prefer,) if prefer else ()) + MAINLINES:
        if _git_out(where, "rev-parse", "--verify", "--quiet", ref)[0]:
            return ref
    return ""


def change_refusal(scope: Scope, where, what: str, branch: str = "",
                   mainline: str = "") -> str:
    """Why this commit, push or landing may not go further, or "".

    The file tools, redirections and `cp`/`mv`/`rm` are judged as they write. A file can
    also be written by a script the worktree runs, by `git apply` or by `git stash pop`,
    none of which name their paths; the stress test of 2026-09-28 crossed tiers all three
    ways. What they write cannot reach main without a commit and a landing, so the CHANGE
    SET is judged there, whatever wrote it: every file must be in the task's tier, and the
    baseline and the floor may only tighten. A repository this cannot read is refused for
    a tiered task (failing closed) and left to git for any other."""
    ok, top = _git_out(where, "rev-parse", "--show-toplevel")
    if not ok:
        return (f"cannot read the repository at {where} to judge this {what}: failing "
                f"closed" if scope.tier else "")
    top = pathlib.Path(top.strip())
    names = set()
    if what == "commit":
        # The index, the working tree and untracked files together: `commit -a`, a
        # pathspec and a later `add` all draw from them, and a tiered task should have
        # nothing outside its tier in any of the three.
        for args in (("diff", "--cached", "--name-only"), ("diff", "--name-only"),
                     ("ls-files", "--others", "--exclude-standard")):
            ok, out = _git_out(top, *args)
            if not ok:
                return f"cannot list what this commit carries, failing closed"
            names |= {ln.strip() for ln in out.splitlines() if ln.strip()}
        before, after = "HEAD", ""
    else:
        base = _mainline(top, f"origin/{mainline}" if mainline else "")
        head = branch or "HEAD"
        ok, fork = _git_out(top, "merge-base", base, head) if base else (False, "")
        if not ok:
            return (f"no main branch to compare this {what} with, failing closed"
                    if scope.tier else "")
        fork = fork.strip()
        ok, out = _git_out(top, "diff", "--name-only", fork, head)
        if not ok:
            return f"cannot list what this {what} carries, failing closed"
        names = {ln.strip() for ln in out.splitlines() if ln.strip()}
        before, after = fork, head
    for name in sorted(names):
        refused = tier_refusal(scope, top / name)
        if refused:
            return f"this {what} carries {name}: {refused}"
    return _ratchet_refusal(top, before, after, what)


def _ratchet_refusal(top: pathlib.Path, before: str, after: str, what: str) -> str:
    """The baseline gains no line and the floor loses no count, between two versions.
    `after` of "" is the working tree."""
    def read(rev, rel):
        if not rev:
            f = top / rel
            return f.read_text() if f.is_file() else None
        ok, out = _git_out(top, "show", f"{rev}:{rel}")
        return out if ok else None

    def lines(text):
        return {ln.strip() for ln in text.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")}

    for rel in (BASELINE_FILE, STORES_BASELINE_FILE, RUNTIME_BASELINE_FILE):
        was, now = read(before, rel), read(after, rel)
        if was is not None and now is not None:
            added = sorted(lines(now) - lines(was))
            if added:
                return (f"this {what} adds {added[0]!r} to {rel}: the baseline only "
                        f"shrinks: fix the crossing, or dispatch the other plugin by name")
    was, now = read(before, FLOOR_FILE), read(after, FLOOR_FILE)
    if was is not None and now is not None:
        try:
            was, now = json.loads(was), json.loads(now)
        except ValueError:
            return f"{FLOOR_FILE} does not parse after this {what}, failing closed"
        for layer, least in was.items():
            if int(now.get(layer, 0) or 0) < int(least):
                return (f"this {what} lowers the {layer} floor in {FLOOR_FILE} from {least} "
                        f"to {now.get(layer, 0)}: a layer run that got smaller has lost "
                        f"coverage, and lowering the floor is a person's decision")
    return ""


# ── shell text helpers ───────────────────────────────────────────────────────
_REDIR = re.compile(r"(?<![0-9<])(?:[0-9]?>{1,2})(&?)\s*(\S+)")
#: What `>&` may point at and still be a descriptor duplication rather than a file: a
#: descriptor number, or `-` to close the descriptor.
_DUP_TARGET = re.compile(r"[0-9]+|-")


def _redirections(seg: str):
    """Targets of every `>` / `>>` in a segment, or None if one cannot be read.
    `2>&1`, `>&2` and `>&-` are descriptor duplications, not files. Anything else after
    `>&` is a file, and bash sends stdout and stderr to it: `cat a >& /etc/x` writes
    /etc/x exactly as `cat a &> /etc/x` does. Every `&` was read as a duplication, so
    the first spelling was never judged (found 6 Oct)."""
    out = []
    for m in _REDIR.finditer(seg):
        amp, t = m.group(1), m.group(2)
        if amp and _DUP_TARGET.fullmatch(t):
            continue
        if t.startswith("("):
            # `>(…)` is process substitution, not a file: bash runs `…` and wires its
            # output into the command — a redirection that is itself a command. Read as
            # a file target, `(touch` was allowed whenever the pseudo-path resolved
            # under a writable root (review, 19 Oct: `cat >(evil.sh)`, with evil.sh
            # written under the root, ran it). Unreadable, and the caller fails closed.
            return None
        try:
            t = shlex.split(t)[0]
        except ValueError:
            return None
        out.append(t)
    return out


def _strip_redirections(seg: str) -> str:
    return _REDIR.sub("", seg).replace("2>&1", "").strip()


def _words(seg: str) -> list:
    try:
        return shlex.split(seg)
    except ValueError:
        return []


def _abs(p) -> str:
    return str(pathlib.Path(os.path.expanduser(str(p))).resolve())


def _resolve(target: str, cwd: str) -> pathlib.Path:
    t = os.path.expanduser(str(target))
    base = pathlib.Path(cwd or "/")
    return (base / t).resolve() if not os.path.isabs(t) else pathlib.Path(t).resolve()


def _protected(p) -> str:
    """The reason a path may never be written by an agent, or "".

    INSTALLED PLUGIN CODE is deployed from a commit by `finrail upgrade`, and nothing
    else. An agent that edits it in place makes production run code no commit holds —
    which is how the production plugins came to be running a hand copy of a worktree. Refused
    here whatever a scope's `write_roots` say, so no scope can be written that allows it.
    """
    try:
        from .registry import installed_root
        root = installed_root().resolve()
    except Exception:                                               # noqa: BLE001
        return ""
    if _under_any(pathlib.Path(str(p)), (root,)):
        return (f"{p} is installed plugin code under {root}; it is deployed from its "
                f"repository by `finrail upgrade` and never edited in place — change the "
                f"plugin's source instead")
    return ""


def _gate_files(scope: Scope) -> tuple:
    """What the gate judging this scope is made of, and what runs it: the scope file and
    the engine's settings beside it (`materialise`), the engine's own settings and
    servers (`CLAUDE_CONFIG_DIR`, else under the home folder), the hook programs
    (`_gates_dir`), the finrail package whose code decides (`scope.py`, `policy.py`)
    and the environment it runs in, and the `finrail` and `claude` programs on PATH.
    Each is a place where a written file runs outside every rule here. Resolved, as
    `_resolve` resolves what it is compared with, so a link into one is the file."""
    import shutil
    home = pathlib.Path.home()
    out = [home / ".claude", home / ".claude.json", pathlib.Path(sys.prefix),
           pathlib.Path(__file__).resolve().parent.parent]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        out.append(pathlib.Path(os.path.expanduser(os.environ["CLAUDE_CONFIG_DIR"])))
    if scope.cwd:
        work = pathlib.Path(scope.cwd)
        out += [work / "scope.json", work / ".claude", work / ".mcp.json"]
    for prog in ("finrail", "claude"):
        found = shutil.which(prog)
        if found:
            out.append(pathlib.Path(found))
    try:
        out.append(_gates_dir())
    except Exception:                                               # noqa: BLE001
        pass
    return tuple(p.resolve() for p in out)


def _folded(p) -> pathlib.PurePath:
    """A path as this host's file system compares it. A Mac's is blind to letter case,
    so `SCOPE.json` IS `scope.json` there, and writing one replaced the other (review of
    the fixes, 8 Oct; `tier_of` already folds)."""
    s = str(p)
    return pathlib.PurePosixPath(s.lower() if sys.platform == "darwin" else s)


def _inside(p, roots) -> bool:
    """`_under_any`, as the file system compares names."""
    return _under_any(_folded(p), [_folded(r) for r in roots if r])


def _gate_refusal(scope: Scope, p) -> str:
    """The reason a path may never be written because the gate itself is there, or "".

    It used to be a question like any other write outside the roots: "a coding agent wants to
    write …/scope.json, outside where it may write. Allow it?" Nothing said the file was
    the gate, the gate reads that file on every call, and the file tools are not under
    the OS sandbox, so one yes turned the gate off for the rest of the turn (review,
    8 Oct). Refused whatever a scope's roots say, like `_protected`: no answer opens it,
    and `materialise` writing it fresh each turn is not a reason to let it be written."""
    if _inside(p, _gate_files(scope)):
        return (f"{p} is part of the gate that judges {scope.name}'s calls, or of what "
                f"runs it; the agent never writes it, whatever a person answers")
    return ""


def _never_asked(p) -> str:
    """Why a write outside the roots is refused rather than put to a person, or "".

    finrail's own records: the settings that set this scope, the questions and their
    answers; the sandbox already shuts them to the shell (`sandbox_settings`). And every
    hidden file or folder in the home folder: that is where programs keep the settings
    and code they run at start (a shell's rc file, `~/.local`, `~/.config`, `~/.ssh`),
    and a one-line question cannot show a person what writing one would start."""
    from .settings import state
    if _inside(p, (state().resolve(),)):
        return f"{p} is finrail's own records; they change through finrail, never by a file write"
    home, q = _folded(pathlib.Path.home().resolve()), _folded(p)
    if home in q.parents and q.relative_to(home).parts[0].startswith("."):
        return (f"{p} is a program's own settings in the home folder; it is never a "
                f"question, because what it would start cannot be shown in one")
    return ""


def _under_any(p, roots) -> bool:
    p = pathlib.Path(str(p))
    for r in roots:
        if not r:
            continue
        r = pathlib.Path(str(r))
        if p == r or r in p.parents:
            return True
    return False


def _allow(reason: str) -> dict:
    return {"decision": ALLOW, "reason": reason}


def _deny(reason: str) -> dict:
    return {"decision": DENY, "reason": reason}


def _ask(reason: str) -> dict:
    return {"decision": ASK, "reason": reason}


# ── the workspace ───────────────────────────────────────────────────────────
def _gates_dir() -> pathlib.Path:
    """Where the hook programs live: INSIDE the package, so they ship with it.

    They were beside it, under `tools/`, resolved through `source_dir()`. That worked for
    as long as the deploy was an editable install — `source_dir()` landed in the checkout
    and the checkout has `tools/`. The day the deploy became a COPY of the commit, the
    wheel brought `finrail` and `finrail_plugins` and nothing else, all three hook
    programs were absent, and every gate failed closed: not one tool call ran, in any
    agent, scoped or not. `finrail` itself was fine. What broke was the three programs
    it tells the engine to run.

    Resolved from this module's own location, so the answer is right in a checkout, in a
    copied install, and in a wheel, without a setting to remember.
    """
    from .. import gates as _g                                    # noqa: PLC0415
    return pathlib.Path(_g.__file__).resolve().parent


def gate_script() -> pathlib.Path:
    return _gates_dir() / "finrail_scope_gate.py"


def commit_script() -> pathlib.Path:
    return _gates_dir() / "finrail_scope_commit.py"


def steer_script() -> pathlib.Path:
    return _gates_dir() / "finrail_steer.py"


def _timeouts(scope=None) -> tuple:
    """(seconds the gate gives `finrail policy`, seconds the engine gives the gate).

    One place for both, because they only work in order: a question's wait
    (questions.WAIT_S) inside the gate's, inside the engine's. A gate that only decides
    keeps its 25 and 30. One that may ask a person waits past the question's time, or
    it would kill the policy child mid-wait and leave the question open with nobody
    waiting on it; and the engine waits past the gate, or the model reads "hook timed
    out" instead of the gate's own deny. The gate's number is written next to the scope
    (`materialise`), or on the full agent's hook command (`ask_settings`, no scope: it
    always asks), so neither gate script holds the rule or a constant: each once
    carried a literal 640 that had to sit between the two numbers here, and a change
    to either would have left it behind.

    A question's whole wait is `questions.WAIT_S` and then `questions.LATE_S` (a late
    answer still counts, 6 Oct). A permission is one question per call. A scope whose
    agent may ask its own questions waits for up to MAX_QUESTIONS of them in one call,
    each with its own full wait (`clarify`), so its gate waits for all of them. Both stay
    under a scoped turn's own hour (scoped_agent `timeout`)."""
    from . import questions
    one = questions.WAIT_S + questions.LATE_S
    if scope is not None and scope.questions():
        return MAX_QUESTIONS * one + 40, MAX_QUESTIONS * one + 60
    if scope is None or scope.asks():
        return one + 40, one + 60
    return 25, 30


def gate_timeout(scope) -> int:
    """Seconds the gate script gives `finrail policy` before it answers deny on its own."""
    return _timeouts(scope)[0]


def hook_timeout(scope) -> int:
    """Seconds the engine waits for the PreToolUse gate. 30 for a gate that only decides;
    longer than a question's wait for one that may ask a person, or the engine gives up
    on the hook while the person is still reading the question."""
    return _timeouts(scope)[1]


def materialise(scope: Scope, workdir) -> pathlib.Path:
    """Write the engine's workspace: `<workdir>/.claude/settings.json` and
    `<workdir>/scope.json`. Regenerated on every turn from the declaration, so a file the
    agent somehow altered heals before the next call — and so a moved source tree does.

    Returns the workdir, which is what `llm.call(cwd=…)` is given."""
    workdir = pathlib.Path(os.path.expanduser(str(workdir)))
    (workdir / ".claude").mkdir(parents=True, exist_ok=True)
    for r in scope.write_roots:
        pathlib.Path(r).mkdir(parents=True, exist_ok=True)
    scope_file = workdir / "scope.json"
    # `gate_timeout` is for the gate script alone (`_timeouts`); `from_dict` ignores it.
    want_scope = json.dumps({**scope.to_dict(), "cwd": str(workdir),
                             "gate_timeout": gate_timeout(scope)}, indent=2, sort_keys=True)
    if not scope_file.is_file() or scope_file.read_text() != want_scope:
        scope_file.write_text(want_scope)
    gate = gate_script()
    denied = [t for t in KNOWN_TOOLS if t not in scope.tools]
    hooks = {"PreToolUse": [
        {"matcher": ".*", "hooks": [{"type": "command",
                                     "command": f"/usr/bin/python3 {gate} {scope_file}",
                                     "timeout": hook_timeout(scope)}]}]}
    # A message the person sends while the turn runs reaches the turn after its next
    # tool call (converse/steer.py), instead of waiting for the turn to end.
    hooks["PostToolUse"] = [
        {"matcher": ".*", "hooks": [{"type": "command",
                                     "command": f"/usr/bin/python3 {steer_script()}",
                                     "timeout": 30}]}]
    if scope.commit_roots:
        # Declared, therefore generated — the same reason the deny list is. A hook a
        # person wrote into this file by hand would be erased by the next turn's
        # regeneration, silently taking the agent's version control with it.
        hooks["PostToolUse"].append(
            {"matcher": "|".join(WRITE_TOOLS),
             "hooks": [{"type": "command",
                        "command": f"/usr/bin/python3 {commit_script()} {scope_file}",
                        "timeout": 30}]})
    settings = {"permissions": {"deny": denied}, "hooks": hooks}
    if scope.sandbox:
        settings["sandbox"] = sandbox_settings(scope, workdir)
    want = json.dumps(settings, indent=2)
    f = workdir / ".claude" / "settings.json"
    if not f.is_file() or f.read_text() != want:
        f.write_text(want)
    return workdir


#: Commands that run OUTSIDE the engine's sandbox: finrail alone, which writes its own
#: records. What it may do is still judged by the gate, and it must run alone
#: (`decide_shell`). git was here (push and fetch over ssh) and came out: `git fetch
#: --upload-pack=<program>` runs any program, and git has many such doors (review,
#: 5 Oct). Landing goes through `finrail do repo.land`, which fetches and pushes itself.
SANDBOX_EXITS = ("finrail",)


def _hides_a_command(text: str) -> bool:
    """Does this command hold a second one the splitter does not cut?

    Read as the shell reads it (`policy._walk`), so quoted text is data: a newline, a
    backtick or a `$(…)` inside '…' is what the command is given, and a newline inside
    "…" is too. The raw text was searched for these, so a sandboxed agent could not
    hand finrail an instruction with two lines, or one naming `git status` in a code
    span (review, 5 Oct). Still a second command: a newline, a backtick, `$(`, `<(` or
    `>(` outside quotes, and a backtick or `$(` inside "…", where the shell runs them.
    A backslash is followed, or `id=\\' <newline> python3 evil.py \\'` would read as one
    quoted argument; and a quote left open is read as more, because what follows it
    cannot be told."""
    pieces, open_quote = policy._walk(text)
    for i, piece, quote in pieces:
        if len(piece) == 2 or quote in ("'", "$'"):
            continue
        if piece == "`" or text.startswith("$(", i):
            return True
        if not quote and (piece == "\n" or text.startswith(("<(", ">("), i)):
            return True
    return bool(open_quote)


def _leaves_sandbox(seg: str) -> bool:
    # A segment whose quoting cannot be read is read by its blanks instead. Since the
    # splitter cuts at `$(` inside "…" (review, 16 Oct), `finrail x id="$(evil)"` starts
    # with `finrail x id="`, which shlex cannot read; with no words it was not an exit,
    # and the refusal said "nothing to run" instead of why. Indexing an empty head here
    # raised once, and the model read the start of a traceback (second review, 6 Oct).
    words = _words(_strip_redirections(seg)) or seg.split() or [""]
    head = " ".join([os.path.basename(words[0])] + words[1:2])
    return any(head == e or head.split()[:1] == [e] for e in SANDBOX_EXITS)


#: Folders under /tmp where a program a person leaves running keeps its local socket. On
#: Linux the sandboxed shell may connect to any socket it can see (`sandbox_settings`),
#: and through one of these the code an agent runs could type a command into a person's
#: own session, outside the sandbox, or sign in somewhere as them (review, 5 Oct). The
#: ones that exist are hidden, as /run is. The list is short on purpose: each is a folder
#: only that one program uses, so hiding it takes nothing from a build or a test run.
#: /tmp itself stays visible, because the engine's sandbox and the tests keep their own
#: files there. Whatever else a host has goes in the scope's `hide`.
SOCKET_FOLDERS = (
    # tmux, one folder per user (tmux-<uid>). `tmux send-keys` types into an open pane,
    # and the command runs in that person's login shell.
    "/tmp/tmux-*",
    # ssh-agent, and the agent sshd forwards for someone signed in (ssh-<random>). Code
    # that reaches it signs in to other machines as that person. The same sockets under
    # /run are hidden already.
    "/tmp/ssh-*",
    # GNU screen, where it does not keep its sessions under /run/screen. `screen -X
    # stuff` types into a session the way tmux does. The second is where a screen
    # installed without setuid keeps them.
    "/tmp/screens",
    "/tmp/uscreens",
    # The Emacs server, one folder per user (emacs<uid>): `emacsclient --eval` runs code
    # in the editor a person has open.
    "/tmp/emacs[0-9]*",
    # Neovim's server, one folder per user (nvim.<user>): `nvim --server … --remote-send`
    # runs commands in theirs.
    "/tmp/nvim.*",
)

#: The same, under the person's home. From OpenSSH 10.1 ssh-agent, and the agent sshd
#: forwards for someone signed in, keep their socket here and not under /tmp (second
#: review, 6 Oct), so on a host with a current OpenSSH `/tmp/ssh-*` hides nothing. The
#: folder holds sockets only: ~/.ssh itself stays visible, because git reads its config
#: and keys from there.
HOME_SOCKET_FOLDERS = (
    "~/.ssh/agent",
)


def _socket_folders() -> list:
    """Which of `SOCKET_FOLDERS` and `HOME_SOCKET_FOLDERS` are on this host now. Read on
    every turn (`materialise` writes the settings each time), so a session opened since
    the last one is covered.

    Real folders only. Each of these programs makes its own folder, so a link under one
    of these names is not theirs, and following it would hide whatever it points at,
    which could be a folder the agent's work needs."""
    import glob
    found = [p for pattern in SOCKET_FOLDERS for p in glob.glob(pattern)]
    found += [os.path.expanduser(p) for p in HOME_SOCKET_FOLDERS]
    return sorted(p for p in found if os.path.isdir(p) and not os.path.islink(p))


def sandbox_settings(scope: Scope, workdir) -> dict:
    """The engine's own sandbox for its shell (Claude Code `sandbox`): every command and
    every process it starts may write only the roots. The gate still judges each command
    first; this is what holds when a command allowed by its words runs code.

    - It fails closed: with the OS pieces missing (Linux: bubblewrap and socat) the
      engine would otherwise run the shell UNSANDBOXED without a word. It refuses to start.
    - No command may retry outside the sandbox.
    - `SANDBOX_EXITS` run outside it (finrail writes its own records; git push and
      fetch go over ssh). What they may do is the gate's to judge, and each is refused
      chained with anything else (`decide_shell`), so nothing rides out on its back.
    - A repository's `.git` is writable (a worktree commits into it) except its hooks and
      config, which unsandboxed git would run later.
    - The engine's own settings, the scope file and finrail's state are never writable:
      those are where an answer, a rule or a gate lives.
    - LOCAL SOCKETS. On Linux the engine blocks them with a seccomp filter it installs
      from a nested user namespace, which Ubuntu's AppArmor refuses (measured on the
      team server, 5 Oct: every command failed). Changing that is a host security
      setting. So sockets are allowed and the places they live are hidden instead:
      /run (the user's systemd and D-Bus, through which code could start a process
      outside the sandbox), finrail's own state, the folders under /tmp where a
      person's tmux, ssh-agent, screen or editor keeps its socket (`SOCKET_FOLDERS`),
      where a current OpenSSH keeps the agent's under their home
      (`HOME_SOCKET_FOLDERS`), and `scope.hide` for anything else on the host. The
      network namespace is the engine's own, so abstract sockets are not reachable
      either."""
    from .settings import state
    workdir = pathlib.Path(os.path.expanduser(str(workdir)))
    # Linux is where sockets are allowed (below), so it is where their folders are
    # hidden. Anywhere else nothing here allows them, and there is nothing to hide.
    linux = sys.platform.startswith("linux")
    allow = list(scope.write_roots) + ([scope.dev_root] if scope.dev_root else [])
    deny = [str(workdir / ".claude"), str(workdir / "scope.json"), str(state())]
    hidden = (["/run", "/var/run", str(state())] + (_socket_folders() if linux else [])
              + list(scope.hide))
    for repo in scope.repos:
        allow.append(f"{repo}/.git")
        deny += [f"{repo}/.git/hooks", f"{repo}/.git/config"]
    return {"enabled": True, "failIfUnavailable": True,
            "allowUnsandboxedCommands": False, "autoAllowBashIfSandboxed": False,
            "excludedCommands": [p for e in SANDBOX_EXITS for p in (e, f"{e} *")],
            "filesystem": {"allowWrite": sorted(set(allow)), "denyWrite": sorted(set(deny)),
                           "denyRead": sorted(set(hidden))},
            "network": {"allowedDomains": list(scope.net),
                        **({"allowAllUnixSockets": True} if linux else {})}}


def load(path) -> Scope:
    p = pathlib.Path(str(path))
    try:
        return Scope.from_dict(json.loads(p.read_text()))
    except (OSError, ValueError) as e:
        raise ContractViolation(f"scope file {p} is not readable: {e}")


def judge(path, payload: dict) -> dict:
    """What `finrail policy --scope <file>` does with a hook payload."""
    scope = load(path)
    if not isinstance(payload, dict):
        return _deny("unreadable hook payload — failing closed")
    tool = payload.get("tool_name") or payload.get("tool") or ""
    params = payload.get("tool_input") or payload.get("parameters") or {}
    d = decide(scope, tool, params, cwd=str(payload.get("cwd") or ""))
    if d.get("clarify"):
        return clarify(params)
    return settle(d) if d["decision"] == ASK else d


#: Seconds the gate gives the agent's door to say it is there before a question is asked.
#: A door that is up answers a ping without doing any work; two seconds is so that a busy
#: machine is not read as a door that is down.
DOOR_PING_S = 2


def _door_answers() -> bool:
    """Can the person's answer get back to a question asked now?

    An answer is taken only from a run the agent's door started and vouches for
    (`converse.answer`, core/caller.py). With the door down the transport starts plain
    processes instead, the door vouches for none of them, and every `/answer` is
    refused: the question went up, the person replied, and the gate still waited its ten
    minutes and then said no (review, 5 Oct). So a question is asked only when this turn
    came in by the door and the door still answers. The door names itself in every run
    it starts (`caller.DOOR`), and the engine and this hook inherit that; a turn started
    as a plain process does not have it. The ping is for a door that died after the turn
    began."""
    from . import caller, serve
    door = (os.environ.get(caller.DOOR) or "").strip()
    return bool(door) and serve.ping(door, timeout=DOOR_PING_S)


def settle(d: dict, wait_s: int = None) -> dict:
    """Turn an ASK into the person's answer: the question goes into the conversation
    this turn belongs to, and the gate waits for it (core/questions.py).

    The turn's addresses come from the environment converse set for this turn, which
    the engine and this hook inherit; nothing the model typed can set them. No
    conversation means nobody to ask (a terminal or a schedule), and that is a refusal
    saying so, never an allow. So is a conversation the answer cannot come back from
    (`_door_answers`): refused at once, with nothing posted and no wait."""
    from . import questions
    thread = (os.environ.get("FINRAIL_THREAD") or "").strip()
    person = (os.environ.get("FINRAIL_TURN_USER") or "").strip()
    if not thread or not person:
        return _deny(d["reason"] + ": there is nobody to ask from here, so no")
    if not _door_answers():
        return _deny(d["reason"] + ". The person was not asked: their answer could only "
                     "come back through the agent's door, and the door is not up. So no")
    # The bare reason. The question's kind says it is answered yes or no, and the words
    # a person reads (the asking, how to answer, how long they have) are composed once
    # where the feed is worded (core/entrypoint.py, `finrail watch`); a suffix here had
    # to be stripped back off before the outcome could state the matter (review, 6 Oct).
    got = questions.ask(thread, os.environ.get("FINRAIL_RUN") or "", person, d["reason"],
                        wait_s=questions.WAIT_S if wait_s is None else wait_s)
    if got.get("unposted"):
        # The question never reached the log the transport reads, so nobody saw it
        # (questions.post). Said as that, not as the person's silence.
        return _deny(d["reason"] + ": the question could not be put to the person "
                     "from here, so no")
    waited = _waited(got)
    if got.get("answer") == "yes":
        # The engine does not see the gate wait, and an allow's reason never reaches the
        # model, so without this the agent told the person there had been "no permission
        # prompt and no wait" (a coding agent-dev, 2026-10-05). `context` is passed to the model
        # as the hook's additionalContext, which it does read.
        return {**_allow(d["reason"] + ": allowed by the person"),
                "context": (f"This action was outside your normal permissions. The person "
                            f"was asked in the chat (question {got.get('id')}) and allowed "
                            f"it after {waited}. Say so if you report on it.")}
    if got.get("status") == questions.EXPIRED:
        if got.get("why"):
            # Closed before an answer came, and not by the clock: the run that asked was
            # stopped, or failed, and its gate went with it (questions.expire_run keeps
            # the reason on the record). Said as that: "did not answer within" blamed
            # the person's silence for a question taken down seconds after it was put.
            return _deny(d["reason"] + f": the person was asked in the chat (question "
                         f"{got.get('id')}) and the question was closed first: "
                         f"{got['why']}, so no")
        return _deny(d["reason"] + f": the person was asked in the chat (question "
                     f"{got.get('id')}) and did not answer within {waited}, so no")
    return _deny(d["reason"] + f": the person was asked in the chat (question "
                 f"{got.get('id')}) and refused it after {waited}")


def _waited(q: dict) -> str:
    """How long a question was open, said the way a person would."""
    import datetime
    try:
        s = int((datetime.datetime.fromisoformat(q.get("closed"))
                 - datetime.datetime.fromisoformat(q.get("asked"))).total_seconds())
    except (TypeError, ValueError):
        return "a wait"
    return f"{s} seconds" if s < 120 else f"{round(s / 60)} minutes"


#: How many questions one AskUserQuestion call may carry (the engine's own limit).
MAX_QUESTIONS = 4


def clarify(params, wait_s: int = None) -> dict:
    """The engine's own question tool, put to the person in the chat.

    Measured with the real CLI (2.1.281, 5 Oct): a PreToolUse hook that ALLOWS the call
    and fills in the tool's `answers` is read by the model as "The user answered: ...".
    An allow with only `additionalContext` is not enough (the tool then asks the engine's
    host, finds nobody, and fails); a deny's reason reaches the model as a hook error.
    So an answer comes back as the tool's input, and no answer as a deny that says so.

    Several questions in one call are asked one after another, each with its own full
    wait. They used to share one deadline, so on 6 Oct the second question of a call had
    seven minutes and the third four and a half, though each was put to the person as
    ten. The gate's timeout is sized for MAX_QUESTIONS full waits (`_timeouts`), and
    a question is still never given more than what is left of that, so the gate never
    outlives its hook. Same rules as a permission question: asked
    of the person whose turn it is, answered by them or an operator, from the chat, and
    not asked at all when the answer could not come back (`_door_answers`)."""
    from . import questions
    wait_s = questions.WAIT_S if wait_s is None else int(wait_s)
    asked = params.get("questions") if isinstance(params, dict) else None
    if (not isinstance(asked, list) or not asked or len(asked) > MAX_QUESTIONS
            or not all(isinstance(q, dict) and str(q.get("question") or "").strip()
                       for q in asked)):
        return _deny("the question could not be read, so it was not asked; decide "
                     "yourself if the choice is safe to undo, or stop and say what you need")
    thread = (os.environ.get("FINRAIL_THREAD") or "").strip()
    person = (os.environ.get("FINRAIL_TURN_USER") or "").strip()
    if not thread or not person:
        return _deny("there is nobody to ask from here (no conversation), so the question "
                     "was not asked. Nobody chose for you: decide yourself if the choice is "
                     "safe to undo, or stop and say what you need")
    if not _door_answers():
        return _deny("The question was not asked: the person's answer could only come "
                     "back through the agent's door, and the door is not up. Nobody chose "
                     "for you: decide yourself if the choice is safe to undo, or stop and "
                     "say what you need")
    import time
    # The whole call's bound: MAX_QUESTIONS full waits, each with its late window, as
    # `_timeouts` sizes the gate for. Reached only if a wait overruns.
    deadline = time.monotonic() + MAX_QUESTIONS * (max(0, wait_s) + questions.LATE_S)
    answers, ids, by = {}, [], set()
    for q in asked:
        text = " ".join(str(q["question"]).split())
        options = [str(o.get("label") if isinstance(o, dict) else o).strip()
                   for o in (q.get("options") or [])]
        options = [o for o in options if o]
        left = max(0, int(deadline - time.monotonic()) - questions.LATE_S)
        got = questions.ask(thread, os.environ.get("FINRAIL_RUN") or "", person, text,
                            kind=questions.CLARIFY, wait_s=min(wait_s, left),
                            options=options,
                            multi=bool(q.get("multiSelect")))
        ids.append(got.get("id"))
        if got.get("unposted"):
            return _deny("The question could not be put to the person from here, so "
                         "it was not asked. Nobody chose for you: decide yourself if "
                         "the choice is safe to undo, or stop and say what you need")
        if got.get("status") != questions.ANSWERED:
            so_far = "".join(f" They had answered: {k} = {v}." for k, v in answers.items())
            return _deny(f"The person was asked in the chat (question {got.get('id')}) and "
                         f"did not answer within {_waited(got)}.{so_far} Nobody chose for "
                         f"you: decide yourself if the choice is safe to undo, or stop and "
                         f"say what you need")
        answers[str(q["question"])] = got.get("answer") or ""
        by.add(got.get("by") or "")
    who = ("the person" if by == {person.lower()}
           else "an operator of this agent, for the person")
    return {**_allow(f"answered in the chat by {who}"),
            "input": {**params, "answers": answers},
            "context": (f"Your question was put to the person in the chat (question "
                        f"{', '.join(ids)}) and answered by {who}.")}


# ── the full agent, asking instead of bypassing ─────────────────────────────
def ask_script() -> pathlib.Path:
    return _gates_dir() / "finrail_ask_gate.py"


def ask_settings() -> pathlib.Path:
    """A settings file holding one hook: the engine's own permission dialog, put to the
    person in the chat (`finrail_ask_gate.py` -> `permission`). Passed to the engine
    with `--settings`, so it adds to whatever the working directory already declares
    and writes nothing there. Regenerated on every call, so a moved tree heals. The
    gate's own wait rides on its command line (`_timeouts`): the script holds no copy."""
    from .settings import state
    d = state() / "engine" / "ask"
    d.mkdir(parents=True, exist_ok=True)
    gate_wait, hook_wait = _timeouts()
    want = json.dumps({"hooks": {"PermissionRequest": [
        {"matcher": ".*", "hooks": [{"type": "command",
                                     "command": f"/usr/bin/python3 {ask_script()} {gate_wait}",
                                     "timeout": hook_wait}]}]}}, indent=2)
    f = d / "settings.json"
    if not f.is_file() or f.read_text() != want:
        f.write_text(want)
    return f


def permission(payload) -> dict:
    """What `finrail policy --permission` does with a PermissionRequest hook payload:
    the engine wanted to show a permission dialog, so the person is asked instead."""
    from . import agents
    if not isinstance(payload, dict) or not payload.get("tool_name"):
        return _deny("unreadable permission request — failing closed")
    tool = str(payload.get("tool_name"))
    params = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    what = (params.get("command") or params.get("file_path") or params.get("url")
            or params.get("notebook_path") or json.dumps(params)[:300])
    what = " ".join(str(what).split())
    if len(what) > 400:
        what = what[:400] + "…"
    who = agents.current() or "the agent"
    return settle(_ask(f"{who} wants to use {tool}: {what}"))


# ── THE FULL AGENT'S DEPLOY GATE ─────────────────────────────────────────────

def deploy_script() -> pathlib.Path:
    return _gates_dir() / "finrail_deploy_gate.py"


def deploy_gate_on() -> bool:
    """`full_agent.deploy_gate`, on unless set off. Read off the settings file, like
    `llm.permission_mode`, because the engine's argv is built before plugins load."""
    # An engine that brings its own gate on every call (the memory replay's read-only
    # wrapper, which refuses a second settings layer) turns it off for its turns.
    import os
    if str(os.environ.get("FINRAIL_DEPLOY_GATE", "")).strip().lower() in ("off", "false", "0", "no"):
        return False
    # NOT `raw_location`: it reads `.get(key) or None`, so a stored `false` comes back as
    # "not set" and the gate could never be switched off.
    from .settings import state
    try:
        v = json.loads((state() / "settings.json").read_text()).get("full_agent.deploy_gate")
    except Exception:                                              # noqa: BLE001
        v = None
    return v is None or str(v).strip().lower() not in ("false", "no", "0", "off")


def guarded_repos() -> list:
    """The checkouts whose main the full agent may not push: the finrail tree this host
    deploys from, the plugin repositories beside it, and `full_agent.guarded_repos`."""
    from .settings import raw_location
    out = []
    try:
        from .upgrade import repo
        out.append(repo())
    except Exception:                                              # noqa: BLE001
        pass
    # The package's own tree, read here rather than through `llm.source_dir`: scope is
    # platform and may not import the runtime (docs/LAYERS.md, rule 4).
    src = raw_location("source.dir")
    tree = pathlib.Path(str(src)).expanduser() if src else pathlib.Path(__file__).resolve().parents[2]
    for base in list(out) + [tree]:
        sib = pathlib.Path(base).expanduser().resolve().parent / "finrail-plugins"
        if (sib / ".git").exists():
            out.append(sib)
    extra = raw_location("full_agent.guarded_repos") or []
    if isinstance(extra, str):
        extra = [e for e in extra.replace(",", " ").split() if e]
    out += [pathlib.Path(str(e)).expanduser() for e in extra]
    seen, keep = set(), []
    for p in out:
        k = str(pathlib.Path(p).expanduser().resolve())
        if k not in seen:
            seen.add(k)
            keep.append(k)
    return keep


def deploy_settings() -> pathlib.Path:
    """A settings file holding the deploy gate for the full agent (`finrail_deploy_gate.py`).
    Passed with `--settings`, so it adds to the working directory's own and writes nothing
    there; regenerated on every call, so a moved tree or a new guarded repo heals. The
    guarded paths ride on the gate's command line: the script holds no copy."""
    import shlex
    from .registry import installed_root
    from .settings import state
    d = state() / "engine" / "deploy"
    d.mkdir(parents=True, exist_ok=True)
    args = [f"--repo={r}" for r in guarded_repos()] + [f"--plugins={installed_root()}"]
    cmd = " ".join(["/usr/bin/python3", shlex.quote(str(deploy_script()))]
                   + [shlex.quote(a) for a in args])
    want = json.dumps({"hooks": {"PreToolUse": [
        {"matcher": m, "hooks": [{"type": "command", "command": cmd}]}
        for m in ("Bash", "Write|Edit|MultiEdit|NotebookEdit")]}}, indent=2)
    f = d / "settings.json"
    if not f.is_file() or f.read_text() != want:
        f.write_text(want)
    return f
