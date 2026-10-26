"""DISPATCHER plugin — the router, normal chat, and sessions.

Core owns argv; this plugin owns the judgment of what a request means. It uses the same
plugin API as any extension: `ask` is an ordinary judged capability whose proposal
happens to be "which capability should run".

`ask` RESOLVES; it does not act. Its answer is the exact command to run, and running it is
the caller's move — so the action passes the same gate as anything else and appears in the
ledger as itself, and the caller learns the surface instead of depending on the router
forever.

Getting there can take several steps. When a request names something the router has not
been told about, it runs read-only capabilities to find out and then decides again with
the answer in hand — the same sequence a person would follow, done once and in a place
where every step is checked. Only the exploration is multi-step: the ANSWER is always a
single command.

Answering the obvious question — *if I ask for something no plugin covers, do I just get
an error?* No: routing returns the command for `chat`, quoting what you actually asked, so
you get a normal Opus conversation recorded as a session.
"""
import json

from ...core import agents, llm, progress, registry, sessions, view
from ...core.errors import OpFailed
from ...plugin import Plugin
from ..activity import turn as activity_turn
from . import router

NAME = "dispatcher"
DESCRIPTION = "Routing, chat and sessions."


def REQUIREMENTS():
    """The engine — checked, because `ask` is the front door and everything judged runs
    through it.

    It went unchecked for a while, and `llm.available()` sat uncalled: doctor reported
    24/24 green while an expired login meant `ask` failed on every request. A system that
    passes its own health check and then cannot answer a simple question is worse than one
    with no health check, because it argues against the only correct suspicion.
    """
    from ...core.requirements import Requirement

    class Engine(Requirement):
        kind = "engine"

        def check(self):
            return llm.available()

        def fix(self):
            return "claude    (run it once, interactively, and sign in)"

    return [Engine("claude-engine")]


class Ask(Plugin):
    """The front door. It RESOLVES a request into an exact command and returns it. It
    does not run it.

    That last part is the whole design. Routing that executes is a crutch: the caller
    never learns the surface, so it stays dependent on the router and is back to guessing
    the moment the router is unavailable. Routing that ANSWERS teaches the surface on
    every use — the next identical request skips the router entirely.

    It also keeps one enforcement point. The returned command is run by the caller, so it
    passes the same gate as anything else and appears in the ledger as itself. Executing
    here buried the real action one level down inside `ask`, where the gate never saw it.

    And it makes the declared rules true rather than nearly true: `autonomous` with
    "routing changes nothing by itself" was a polite fiction while this dispatched. The
    only things it runs now are read-only lookups, which cannot change anything.
    """
    name = "ask"
    description = ("Say what you want in plain language; returns the exact command to "
                   "run. It resolves — it does not run the command for you.")
    autonomous = True
    confirm = False
    reason = "routing changes nothing by itself — the routed capability applies its own rules"
    schema = router.SCHEMA

    @property
    def system(self) -> str:
        """Loaded per call from `rules/ask.md`, falling back to the in-code default —
        so routing instructions are editable without a code change, and editing them
        takes effect on the next run."""
        return router.system()

    def static_context(self) -> dict:
        ctx = {"capabilities": router.catalogue_block()}
        live = router.live_values_block()
        if live:
            ctx["real values you may use"] = live
        return ctx

    def validate(self, proposal) -> list:
        return router.validate(proposal)

    def render(self, p) -> str:
        name, params = router.target(p)
        if not name:
            return "no capability fits — this will be answered as a chat"
        return f"route to {name} {params or ''}".strip()

    def resolve(self, task: str, context: dict):
        """Route by looking things up first, when one shot would only be a guess.

        Injecting known values up front (mail hands over its accounts) is the better
        answer and handles the common cases outright. But it cannot cover everything: a
        request can name a folder, a task or a brain the router was never told about, and
        the honest move then is the one a person makes — go and read, then decide.

        This is the same sequence anyone would follow without a router: see what exists,
        run the read that resolves the name, then run the real command. Doing it here is
        better than leaving it to the caller, because every step is checked against the
        real catalogue, lookups are restricted to read-only capabilities so no iteration
        can act by accident, and the budget is fixed instead of open-ended.

        Running out of lookups is reported as a failure to route. It is never quietly
        turned into a route with the unresolved parameter dropped — that would reintroduce
        the exact bug this exists to prevent.
        """
        ctx = dict(context or {})
        trail = []
        for attempt in range(router.MAX_LOOKUPS + 1):
            proposal = llm.call(system=self.system, prompt=task, context=ctx,
                                schema=self.schema, capability=self.name)
            if not isinstance(proposal, dict):
                return proposal
            want = proposal.get("lookup") or {}
            name = (want.get("capability") or "").strip() if isinstance(want, dict) else ""
            if not name:
                if not (proposal.get("capability") or "").strip():
                    # Neither a route nor a lookup. Say so and let it answer again rather
                    # than failing on a reply the model can obviously repair.
                    ctx = {**ctx, "correction": "Your last reply chose no capability and "
                                                "asked for no lookup. Return one or the "
                                                "other."}
                    continue
                # The model is asked to route; it is never handed back the words the
                # person used. Carrying them explicitly is what lets the chat fallback
                # quote the actual request — it previously fell back to the model's
                # `reason`, so "no capability covers this" became the thing chat was
                # asked about.
                proposal["_task"] = task
                if trail:
                    proposal["_looked_up"] = trail
                return proposal
            if attempt >= router.MAX_LOOKUPS:
                raise OpFailed(
                    f"could not resolve this after {router.MAX_LOOKUPS} lookups "
                    f"(last wanted '{name}'). Name the value directly, or run the "
                    f"command yourself — see: finrail capabilities")
            try:
                result = router.run_lookup(name, want.get("params") or {})
                note = f"result of {name}"
            except router.LookupRefused as e:
                # Tell the model why, so it corrects instead of repeating itself.
                result, note = str(e), f"{name} was refused"
            trail.append({"capability": name, "reason": want.get("reason", "")})
            ctx = {**ctx, note: json.dumps(result, default=str)[:4000]}
        return None                                  # unreachable; the loop always exits

    def degrade(self, task: str, error: Exception):
        """With no model, name the capabilities that fit — never a dead end.

        The model does the routing and has to: "show my inbox from example.com" is only a
        useful route if it comes back as `mail.inbox account=you@example.com`,
        and picking parameters out of prose is not something string matching can do. A
        capability chosen with its parameters silently dropped is not a cheaper answer,
        it is a wrong one.

        So this never competes with a model that answered. It exists for when there is no
        answer at all, where a ranked list of real commands beats a bare error.
        """
        return {"capability": "", "params": {}, "_task": task,
                "_candidates": router.match(task),
                "_model_error": str(error)[:300],
                "reason": "the model was unavailable; these capabilities fit the request"}

    def execute(self, p) -> dict:
        """Resolve to a command. Running it is the caller's move, deliberately."""
        if p.get("_candidates") is not None:
            return {"routed": "choose", "candidates": p["_candidates"],
                    "model_error": p.get("_model_error"), "task": p.get("_task", "")}
        task = p.get("_task", "")
        name, params = router.target(p)
        if not name:
            return {"routed": "chat", "task": task,
                    "command": f'finrail do chat {json.dumps(task, ensure_ascii=False)}',
                    "why": p.get("reason", "no capability covers this")}
        wants_view = bool(p.get("view"))
        return {"routed": name, "capability": name, "params": params,
                "command": router.invocation(name, params, view=wants_view),
                "view": wants_view and router.viewable(registry.command(name)),
                "why": p.get("reason", ""),
                "looked_up": p.get("_looked_up") or []}

    def render_data(self, out) -> str:
        if out.get("routed") == "choose":
            lines = [f"Could not route automatically: {out.get('model_error','')}", ""]
            cands = out.get("candidates") or []
            if not cands:
                lines.append("No capability matched. See everything: finrail capabilities")
            else:
                lines.append("These fit — run one directly:")
                for c in cands:
                    lines.append(f"  {c['command']}")
                    lines.append(f"      {c['description']}  ({c['why']})")
            return "\n".join(lines)
        # The output is an INSTRUCTION, not a note. Whoever asked — a person or a model
        # reading this as a tool result — should be in no doubt that the next move is to
        # run this exact line, and should not have to reconstruct it from a description.
        lines = ["Run this command now:", "", f"    {out['command']}", ""]
        if out.get("view"):
            # What to do with a view depends on what a view IS here. The desktop app gets
            # a page and the call that opens it; a chat front end gets the markdown
            # itself, and telling a model to "show the page with the call it prints" when
            # no call is printed sends it looking for one.
            if view.front_end() == view.CHAT:
                lines.append("  Run it, then relay what it prints, unchanged.")
            else:
                lines.append("  Run it, then show the page with the call it prints.")
            lines.append("  Say nothing else about it — whoever asked is looking at it.")
        if out.get("routed") == "chat":
            lines.insert(0, "No capability covers this; it is a conversation.")
        for step in out.get("looked_up") or []:
            lines.append(f"  looked up {step['capability']}"
                         + (f" — {step['reason']}" if step.get("reason") else ""))
        if out.get("why"):
            lines.append(f"  why: {out['why']}")
        return "\n".join(lines)


class Chat(Plugin):
    """A normal Opus conversation. Deterministic plan (what will be said, in which
    session); the writing it does is its own transcript, which is why it needs no
    approval."""
    name = "chat"
    description = "Talk to Opus. session=<id> continues an existing conversation."
    session_kind = "chat"
    aliases = {"message": "task", "prompt": "task"}
    autonomous = True
    confirm = False
    reason = "a conversation writes only its own transcript"

    def plan(self, task: str = "", session: str = "", title: str = "", user: str = ""):
        return {"prompt": task, "session": session, "title": title or task[:60],
                "user": user}

    def render(self, p) -> str:
        where = f"session {p['session']}" if p["session"] else "a new session"
        return f"CHAT in {where}:\n  {p['prompt'][:300]}"

    def execute(self, p) -> dict:
        """One turn, in a conversation the engine remembers.

        The session bookkeeping is the contract's, not this capability's: declaring
        `session_kind` is what buys it. All that is left here is the turn itself.
        """
        rec, engine, resume = self.conversation(
            session=p.get("session") or "", title=p.get("title") or "chat",
            owner=p.get("user") or "")
        sid = rec["id"]
        sessions.append(sid, "user", p["prompt"])
        rid = p.get("_run") or ""
        sessions.start_turn(sid, run=rid)
        if rid:
            from ...core import tracker
            tracker.update(rid, session=sid)
        try:
            with progress.sink(lambda e: sessions.step(sid, e)):
                ctx = activity_turn.for_turn(self.name, sid, p["prompt"])
                text = llm.chat(sessions.take_handover(sid, p["prompt"]), session=engine, resume=resume, context=ctx,
                                person=p.get("user") or "",
                                activity=activity_turn.current_name(sid))
        finally:
            sessions.end_turn(sid)
        text = activity_turn.judged(self.name, sid, p["prompt"], text)
        sessions.append(sid, "assistant", text, engine_session=engine)
        return {"session": sid, "text": text, "turns": len(rec["turns"]) + 2,
                "continued": resume}

    def render_data(self, out) -> str:
        return (f"{out.get('text','')}\n\n"
                f"— session {out.get('session')} "
                f"(continue: finrail do chat session={out.get('session')} \"...\")")


class Sessions(Plugin):
    name = "sessions"
    description = "Open chats and agent sessions. user=<id> narrows to one person's."
    read_only = True
    confirm = False
    aliases = {"owner": "user"}

    def run(self, kind: str = "", limit: int = 25, user: str = ""):
        return {"sessions": sessions.listing(kind, limit, owner=user),
                "user": user}

    def render_data(self, out) -> str:
        rows = out["sessions"]
        scoped = out.get("user") or ""
        if not rows:
            return ("(no sessions for " + agents.owner_label(scoped) + ")" if scoped
                    else "(no sessions yet — finrail ask \"...\" starts one)")
        # STATUS is the column people came for: the listing shows closed sessions
        # too, so without it a reader counts every row as open and ends the wrong
        # things (or ends nothing, believing them already closed).
        open_n = sum(1 for r in rows if not sessions.is_closed(r))
        # The owner is a pubkey in the record and a PERSON in the column: the operators
        # files carry the names, so this view and `/sessions` name the same people rather
        # than one of them showing `027b4398bb…`. Built once for the table.
        known = agents.operator_names()
        # A SCOPED listing says so. A filtered list that looks like a complete one is
        # how someone concludes a session is gone when it is merely someone else's.
        head = f"{len(rows)} session(s), {open_n} open"
        if scoped:
            head += f" — scoped to {agents.owner_label(scoped, known)} (and unowned)"
        # The owner column appears only when there is ownership to show, so a
        # single-operator install looks exactly as it did.
        show_owner = any(r.get("owner") for r in rows) and not scoped
        lines = [head, "  id        kind    status   turns  updated     "
                 + (" owner     " if show_owner else "") + " title"]
        for r in rows:
            live = "▶ " if r.get("working") else "  "
            # A dash where there is no owner: a CLI session belongs to nobody, and a
            # blank cell in a column of names invites the reader to fill it in.
            who = f" {agents.owner_label(r.get('owner'), known):10}" if show_owner else ""
            lines.append(f"{live}{r['id']}  {r['kind'][:7]:7} "
                         f"{(r.get('status') or '?')[:8]:8} {r['turns']:5}  "
                         f"{r['updated'][5:16]} {who} {r['title'][:40]}")
        if any(r.get("working") for r in rows):
            lines.append("\n▶ = a turn is in flight; its reply lands in the transcript "
                         "when the engine returns")
        lines.append("\nreconnect: finrail get session id=<id>")
        return "\n".join(lines)


class Session(Plugin):
    name = "session"
    verbatim = True   # the transcript
    description = "One session's transcript, and how to continue it."
    read_only = True
    confirm = False

    def run(self, id: str = "", tail: int = 6, user: str = ""):
        rec = sessions.load(id)
        # Scoping the listing and not the transcript would be theatre: the ids are
        # eight hex characters and the listing prints them.
        if not sessions.owns(rec, user):
            from ...core.errors import ContractViolation
            raise ContractViolation(
                f"session {id} belongs to someone else "
                f"(finrail get sessions user={user} lists yours)")
        return {"id": rec["id"], "kind": rec["kind"], "title": rec["title"],
                "status": rec["status"], "owner": rec.get("owner") or "",
                "working": sessions.working(rec),
                "turns": rec["turns"][-tail:],
                "total_turns": len(rec["turns"])}

    def render_data(self, out) -> str:
        head = (f"session {out['id']} [{out['kind']}] {out['title']}  "
                f"({out['total_turns']} turns)"
                + (f"  owner {agents.owner_label(out.get('owner'))}"
                   if out.get("owner") else ""))
        body = "\n\n".join(f"[{t['role']}] {t['text'][:1200]}" for t in out["turns"])
        live = out.get("working") or {}
        if live:
            # At the END, where someone looking for the reply actually stops reading.
            body += (f"\n\n[working since {live.get('since', '?')}"
                     + (f", run {live['run']}" if live.get("run") else "") + "]"
                     "\nA turn is in flight. Its reply lands here when the engine "
                     "returns; nothing is lost while you wait.")
        # The session kind IS the capability name for every conversational capability
        # but the full agent, whose sessions are still filed under `agent` — the name it
        # had before the rename. Hard-coding `agent` here sent anyone continuing any
        # other agent's session — `a coding agent`, or one an installed plugin brings — to the
        # wrong capability.
        cap = "full-agent" if out["kind"] == "agent" else out["kind"]
        cont = f"\n\ncontinue: finrail do {cap} session={out['id']} \"...\""

        return f"{head}\n\n{body}{cont}"


class Runs(Plugin):
    name = "runs"
    description = "Recent runs from the ledger (compact)."
    read_only = True
    confirm = False

    def run(self, n: int = 25):
        from ...core import tracker
        return {"runs": tracker.recent(n)}

    def render_data(self, out) -> str:
        from ...core.entrypoint import _render_runs
        return _render_runs(out["runs"])


COMMANDS = [Ask, Chat, Sessions, Session, Runs]
