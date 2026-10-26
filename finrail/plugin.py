"""THE CAPABILITY CONTRACT — the one and only place a rule is declared.

Everything a user can invoke is a Plugin subclass, and every rule about it is an
attribute on that class. There is no second declaration site: no decorators carrying
policy, no guarded dict, no register function. A bundle's `ops.py` holds plain plumbing
functions with no rules on them.

Three kinds, distinguished by what the subclass defines:

  read_only = True   a query. `run()` returns data, `render_data()` displays it.
                     No proposal, no approval.
  plan() defined     a deterministic action. `plan()` describes what it WILL do;
                     approval (per `confirm`) gates `execute()`.
  system/schema      a judged action. An LLM produces the proposal from `context()`;
                     `validate()` checks it; approval gates `execute()`.

The rules, all attributes:
  read_only   returns data; cannot cause a side effect (audited — see policy.audit)
  autonomous  execute on dispatch without a separate approval step
  confirm     approving it needs an explicit human decision
  person_call_approves
              with `confirm`: a person who runs it has made that decision, so it runs
              at once for them; from inside an agent it waits for a person
  background  run detached instead of blocking the caller
  reason      why this policy, in the author's words (shown to the human)

Two further declarations, for capabilities that need them:
  also_known_as  other words for this capability, so a router can find it
  verbatim       its rendered output is the answer; relay it unchanged
  session_kind   makes it conversational: its runs form a session and the model
                 remembers the earlier turns
"""
import json


class Plugin:
    name = ""
    description = ""
    bundle = ""               # set by the loader; which plugin package it came from

    # -- the rules: declared here, nowhere else ------------------------------
    read_only = False
    autonomous = False
    confirm = True
    # A person's own call is the approval `confirm` asks for. The check of WHO is calling
    # is the one every approval makes (`caller.approval_refusal`), so an agent's call
    # still waits, and still cannot be approved by the agent.
    person_call_approves = False
    background = False
    reason = ""

    # -- judged capabilities -------------------------------------------------
    system = ""
    schema = "{}"

    # Extra explicit parameters, beyond those inferred from plan()/run(). A judged
    # capability has no signature to inspect, so this is how it declares that it accepts
    # (say) an id alongside the free text — which keeps identifier resolution in code
    # instead of parsing it back out of prose.
    takes: tuple = ()

    # Accepted older spellings, {alias: real}. Renaming a parameter is a breaking change
    # for every habit and script that used the old name; an alias makes the rename free
    # while keeping ONE canonical name in the signature.
    aliases: dict = {}

    def static_context(self) -> dict:
        """Blocks identical on every dispatch (voice, rules, catalogues) — emitted first
        so the prompt prefix stays byte-stable and cacheable."""
        return {}

    def context(self, task: str, params: dict = None) -> dict:
        """Task-specific blocks, after the static prefix.

        `params` are the explicit arguments the caller supplied. A judged capability needs
        them: "turn THIS email into a task" is `id=e3ff2a` plus a free-text instruction,
        and the capability has to fetch that message before it can brief the model.

        They were not passed for a while, and the three capabilities that needed them
        worked around it by reading a class attribute nobody ever assigned — so the
        selector was always empty and the INSTRUCTION was used to find the message
        instead. `mail.to_task id=e3ff2a task="Create a task ..."` failed with "no message
        matches 'Create a task ...'": the documented parameter was accepted, validated,
        and then ignored.
        """
        return {}

    # -- planned capabilities ------------------------------------------------
    def plan(self, **kwargs):
        """Describe what this action WILL do, deterministically. Returning None means
        this capability is judged by an LLM instead."""
        return None

    # -- read-only capabilities ----------------------------------------------
    def run(self, **kwargs):
        raise NotImplementedError(f"{self.name}: not a read-only capability")

    def render_data(self, out) -> str:
        return json.dumps(out, indent=2, default=str)

    #: The output of `render_data()` IS the answer, and must reach the reader unchanged.
    #:
    #: When someone asks to see their inbox, a message, or a page, they asked for the
    #: content — not for an account of it. An agent relaying that content is free to
    #: compress it, and compressing it destroys exactly what was requested: the ids you
    #: act on, the wording of a message, the rows you were going to scan. The previous
    #: system routed this to a separate display channel for the same reason.
    #:
    #: Declaring it here rather than trusting each reader to guess is the point: the CLI
    #: marks the block, `finrail rules` reports it, and the session guides carry one rule
    #: instead of a judgement call per capability.
    #:
    #: An honest limit: from a CLI this can be declared and marked, not enforced. What it
    #: removes is the ambiguity — a paraphrased verbatim block is now visibly a paraphrase,
    #: because the markers around it are missing.
    verbatim = False

    def view_data(self, out):
        """This capability's output, shaped for rendering. Default: unchanged.

        The renderer is domain-agnostic on purpose — it finds rows and columns in whatever
        it is handed, and it must not learn that mail keeps its body under `text` while
        tasks keep theirs in the page. A capability whose natural result shape reads badly
        as a table says so here, which is the same rule as `also_known_as` and
        `routing_context()`: the thing that knows declares, and core stays general.
        """
        return out

    def view_markdown(self, out):
        """This capability's own markdown for a chat front end, or None for the default.

        The generic renderer draws rows; some capabilities already write the thing a
        person reads — a table with the current session in bold, a note saying the list
        is scoped to you — and rebuilding that from columns would be a second renderer
        of the same data, kept in step by hand. None means "tabulate my `view_data`",
        which is right for almost everything.

        Distinct from `render_data`, which targets a TERMINAL: an aligned ASCII table is
        exactly what markdown collapses into a jumble, so it cannot be assumed to be
        markdown just because it is text.
        """
        return None

    #: Other words for this capability, for whoever has to FIND it. The catalogue is
    #: written in the system's vocabulary; people ask in their own ("email" for mail,
    #: "todo" for a task). Declared here because the capability is the only thing that
    #: knows what it is also called — a table of synonyms kept in the router has to be
    #: edited every time a plugin is added, and until someone remembers, the domains it
    #: happens to know about rank above the ones it does not.
    also_known_as = ()

    def routing_context(self):
        """Live values a router needs in order to reach this capability correctly, or None.

        The catalogue can say that this takes an `account`; only the plugin can say which
        accounts exist. Without that, a request naming one informally leaves the router
        two bad options — invent an identifier, or drop the parameter and quietly act on
        some default. Both look like success.

        Return anything JSON-serialisable, or a string. Blocks identical across several
        capabilities are collapsed, so capabilities in one domain can share a helper
        without paying for it in the prompt. Called only when routing, so the cost lands
        on the one path that needs it.
        """
        return None

    #: Declaring this makes the capability CONVERSATIONAL: its runs belong to a session,
    #: and the model remembers the earlier turns. The value is the session's kind, used to
    #: group them (`finrail sessions kind=<this>`).
    #:
    #: It is a declaration, not a wiring job. Continuation has two traps that are easy to
    #: get subtly wrong in each re-implementation — resume is decided by whether the record
    #: has TURNS rather than whether an id exists, and a record predating engine ids must
    #: open a fresh thread rather than resume one the engine never saw. Both were already
    #: hand-rolled twice, in `chat` and in the full agent, before this existed. A third and
    #: fourth copy for the domain agents would have drifted.
    #:
    #: Empty means every run stands alone, which is the right default: a capability that
    #: rebuilds its context from scratch each time behaves the same way for the same
    #: request, and that determinism is a feature, not an omission.
    session_kind = ""

    #: What this command does when it arrives while a turn is already running.
    #:
    #:   queue   hold it; the running turn folds it in. The default, and right for
    #:           anything that is a message rather than an instruction about the
    #:           conversation itself.
    #:   run     answer now, out of band. For reads that are ABOUT the running turn
    #:           (status, check) and for the two commands whose whole purpose is to
    #:           control it (stop, background). A stop that waits for the thing it is
    #:           stopping is not a stop.
    #:   refuse  say no, now, with a reason. For commands that re-point or destroy what
    #:           the running turn depends on — a `/new` held for four minutes fires when
    #:           the person has forgotten they asked and silently re-points their thread.
    #:
    #: Declared here rather than listed in the router, for the same reason `read_only`
    #: and `confirm` are: the capability knows what it is, and a list somewhere else
    #: drifts. Eligibility is separate and is not a matter of opinion — only a command
    #: the router resolves WITHOUT a model call can jump, because a model call is slow
    #: exactly when someone is waiting and unavailable exactly when it is wedged.
    while_busy = "queue"

    def is_empty_result(self, result) -> bool:
        """Whether a finished run of this command did nothing worth learning from.

        A poll that runs every two minutes and nearly always finds nothing filled the journal
        — 1,103 of its rows on 16–17 Sep were `example.company.inbox` checks with nothing
        applied — and each one went to the model. The command knows what "nothing" looks
        like for it; a list kept in the extractor would not. Such rows are skipped by the
        learning, and kept in the history. False unless a command says otherwise."""
        return False

    def while_busy_with(self, args: dict) -> str:
        """`while_busy`, refined by what the command was actually given.

        Almost every command is one thing whatever its arguments. `converse.background`
        is two: parking the turn that is running is the whole point of asking mid-turn,
        while `/background <task>` asks for a SEPARATE run and so waits its turn like any
        other request. A declaration cannot express that; the capability can.
        """
        return self.while_busy

    @property
    def conversational(self) -> bool:
        return bool(self.session_kind)

    def conversation(self, session: str = "", title: str = "", meta: dict = None,
                     owner: str = "") -> tuple:
        """(record, engine_session, resume) for this capability's session.

        `owner` is the identity the session belongs to, passed down from whatever
        authenticated the caller. A capability that has one declares a `user`
        parameter and hands it here; one that does not opens unowned sessions, which
        is the old behaviour exactly.

        Extension point for the agents still to come: a capability that needs a different
        rule for when a thread starts — one session per account, say, or a thread that
        expires — overrides this and still gets the storage, the ledger and the
        transcript unchanged.
        """
        if not self.conversational:
            raise NotImplementedError(
                f"{self.name}: not a conversational capability — declare `session_kind` "
                f"to give it sessions")
        from .core import sessions
        return sessions.open_or_resume(self.session_kind, session, title, meta, owner)

    def resolve(self, task: str, context: dict):
        """Build the proposal yourself, or None for the ordinary single model call.

        This exists for capabilities that cannot honestly answer in one shot because the
        answer depends on values they must first go and look up. Overriding it buys a
        capability the right to LOOK before it decides — see `ask`, which uses it to run
        read-only capabilities and re-decide with the results.

        Whatever a resolver does must still come back as a proposal and still face
        `validate()`: looking things up changes how the answer is reached, never whether
        it is checked.
        """
        return None

    def degrade(self, task: str, error: Exception):
        """A usable answer when the model cannot be reached, or None to let it fail.

        Failing is the right default: for most judged capabilities there is no honest
        substitute for the judgment, and inventing one would be guessing. `ask` overrides
        this because it does have something true to say without a model — the capabilities
        that match the request — and because it is the front door: leaving the caller with
        nothing but "model call failed" is what turns one broken command into a session
        spent hunting for a surface that was there all along.
        """
        return None

    # -- shared --------------------------------------------------------------
    def verify(self, proposal: dict, result) -> tuple:
        """(ok, evidence) — what proves this actually happened.

        Verification was real, load-bearing and entirely by convention: `settings.set`
        re-reads the setting, `secret.set` re-checks existence, `memory.forget` checks the
        file is gone, `tasks.archive` checks the destination exists and the source does
        not, mail's providers go through `apply_guarded`, `apps.deploy` runs the app's own
        declared checks. All correct, all private, and none of them declarable, printable
        or testable as a class of thing — the contract existed only as three magic key
        names that core happened to read.

        The default is those same three keys, so nothing changes for a capability that
        does not override this. What overriding buys is that the question is asked of the
        thing that knows the answer: `apps.deploy` re-runs the app's own declared checks
        instead of trusting the `healthy: true` it just wrote, because a container that
        came up and fell over a second later is exactly what a deploy would otherwise call
        a success. `finrail rules` prints both — how a capability says it knows, and
        whether it goes and looks again.

        What the invariant tests enforce is narrower than "every capability must answer",
        and deliberately so — see dev/proposed/verification-contract.md. They enforce that
        an override may not WEAKEN the default, which is the one regression this could
        introduce, and that the capabilities where the claim is load-bearing declare
        `verified_by`.

        It runs AFTER `execute()`, in core, at the one point where side effects happen —
        rather than inside each executor, which is where it used to live and why it could
        not be seen from outside.
        """
        if not isinstance(result, dict):
            return (True, "")
        if result.get("error"):
            return (False, str(result["error"]))
        for key in ("ok", "verified", "success"):
            if key in result and result[key] is False:
                return (False, f"{key} is False: "
                               f"{json.dumps(result, default=str)[:200]}")
        claimed = [k for k in ("ok", "verified", "success") if k in result]
        return (True, f"{', '.join(claimed)} reported by the capability" if claimed
                else "nothing was checked — this capability does not say how it would "
                     "know it worked")

    #: How this capability's claim is proven, in the author's words. Printed by
    #: `finrail rules` beside the policy, because "what would make you believe this
    #: worked?" is a rule with consequences and belongs where the other rules are.
    verified_by = ""

    def validate(self, proposal: dict) -> list:
        """Return a list of problems. A non-empty list blocks execution — this is where
        hallucinated ids and invented paths die."""
        return []

    def render(self, proposal: dict) -> str:
        return json.dumps(proposal, indent=2, default=str)

    def execute(self, proposal: dict) -> dict:
        raise NotImplementedError(f"{self.name}: no executor")

    # -- kind, derived from what the subclass defined ------------------------
    @property
    def kind(self) -> str:
        if self.read_only:
            return "read"
        if type(self).plan is not Plugin.plan:
            return "planned"
        return "judged"

    # -- how the rules are reported (one source, so nothing can drift) ------
    def rules(self) -> dict:
        return {"name": self.name, "kind": self.kind, "bundle": self.bundle,
                "read_only": self.read_only, "autonomous": self.autonomous,
                "confirm": self.confirm,
                "person_call_approves": self.person_call_approves,
                "background": self.background,
                "session_kind": self.session_kind, "verbatim": self.verbatim,
                "reason": self.reason, "description": self.description,
                "verified_by": self.verified_by,
                "verifies": type(self).verify is not Plugin.verify}

    def __repr__(self):
        return f"<{self.kind} {self.name}>"


def read_only(name: str, fn, render=None, description: str = "",
              background: bool = False):
    """One-line read capability — keeps a trivial query from needing a class body.

    The returned object is a real Plugin instance, so it is audited and rendered exactly
    like a hand-written one.
    """
    return type("Read_" + name.replace(".", "_"), (Plugin,), {
        "name": name,
        "description": description or ("Read " + name + "."),
        "read_only": True,
        "background": background,
        "run": staticmethod(fn),
        "render_data": staticmethod(render) if render else Plugin.render_data,
    })()
