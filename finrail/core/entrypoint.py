"""THE CLI — core owns argv and nothing else.

Routing plain language to a capability is a judgment, and judgments belong in a plugin
(bundled/dispatcher). What happens here is mechanical: parse argv, find the owner, print
the result honestly.

Design rules:
  * `--json` prints the whole result, never a truncated summary
  * `-h2 <capability>` prints the REAL accepted parameters (from the code, not a doc)
  * a bare `finrail` on a fresh machine bootstraps and tells you the next command
  * an error prints its fix when it has one, and exits non-zero
"""
import json
import re
import os
import sys

from . import cron, dispatch_core, policy, provision, registry, schema, settings, tracker
from .errors import FinrailError, NotConfigured

USAGE = """finrail — a deterministic dispatcher over pluggable capabilities.

  finrail setup                       guided first-run: settings, secrets, connections
  finrail doctor [plugin]             is this machine able to do the work?
  finrail provision [plugin]          repair what finrail can repair itself

  finrail do <capability> [k=v ...]   propose an action (approval per its own rules)
  finrail do <capability> "text"      judged capabilities take free text
  finrail get <capability> [k=v ...]  run a read-only capability
  finrail ask "anything"              says which command to run (it does not run it)

  finrail runs | view <id> | status   the ledger: what was proposed and what happened
  finrail view <capability> [k=v]     render a read for reading (page or markdown,
                                       whichever view.front_end says). Always safe to
                                       try first: with nothing to draw it falls back
                                       to the capability's own output
  finrail approve <id> [<id> ...] | drop <id> [reason=...]    act on a proposal
                                       (drop --all clears pending)
  finrail kill <id>                   stop a background run

  finrail capabilities [kind]         what exists    (-h2 <cap> for real parameters)
  finrail rules [capability]          every rule, from the one place they are declared
  finrail settings show|set|unset     configuration
  finrail get plugins.list            installed plugins (bundled = always on)
  finrail do plugin.enable|disable    turn an extension on/off  name=<name>
  finrail secret set|check|delete     credentials (values are read from stdin)
  finrail get secret.needed           which credentials are required, and missing
  finrail connection ...              external links: list|types|add|set|test|remove
  finrail service ...                 managed processes: list|install|restart|status|uninstall
  finrail serve [socket=<path>]       one door per agent: a local socket, both ways
  finrail cron list|tick|reap|run <job>    the heartbeat (reap: housekeeping only, fires nothing)
  finrail sessions | reconnect <id>   chats and agent sessions
"""


#: A parameter name, and nothing else. `finrail settings set docs.max_mb=25` means dots
#: and dashes belong; a SPACE never does, and neither does a quote.
_PARAM = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*=")


def _split(argv: list) -> tuple:
    """argv -> (positionals, k=v dict, flags set).

    THE TASK IS DATA, NOT SYNTAX. An argument only becomes a parameter when the part
    before its `=` actually looks like a parameter name. Previously any argument
    containing `=` anywhere was split on the first one, so a message quoting a command
    stopped being the message: "Also if all the harness does is call `finrail do converse
    thread=<channel>`…" was parsed as a parameter whose NAME was the first two-thirds of
    the sentence. `task` came out empty, converse failed in 80ms, no run was created, and
    the person saw a placeholder that never resolved.

    That was not the first time. `ask` and `policy` each carry their own special case a
    few hundred lines below, both written after the same bug reached a user — `ask` could
    not be asked about `now=true`, `policy` could not judge a command containing `=`.
    Fixing the shape of a parameter here is what stops the third capability needing its
    own copy.

    Two deliberate escapes for when even that is not enough:
      `--`      everything after it is positional, whatever it contains.
      `task=-`  the task is read from stdin, so a programmatic caller never has to
                reason about the shape of what a person typed. `chat messages send
                --content -` already works this way, for exactly this reason.
    """
    pos, kw, flags = _parse(argv)
    if wants_stdin(kw):
        kw["task"] = task_from_stdin()
    return pos, kw, flags


def _parse(argv: list) -> tuple:
    """The reading in `_split`, and nothing else: no input is touched. The door reads a
    request's command line with this (serve._command_of, _names_a_person, forward), and
    a door that read `task=-` with `_split` read its OWN stdin, inside its event loop:
    started with that stdin an open pipe, it hung on the first turn (review, 5 Oct)."""
    pos, kw, flags = [], {}, set()
    rest_is_data = False
    for a in argv:
        if rest_is_data:
            pos.append(a)
        elif a == "--":
            rest_is_data = True
        elif a.startswith("--"):
            k, _, v = a[2:].partition("=")
            if v:
                kw[k.replace("-", "_")] = v
            else:
                flags.add(k)
        elif _PARAM.match(a):
            k, _, v = a.partition("=")
            kw[k.replace("-", "_")] = v
        else:
            pos.append(a)
    return pos, kw, flags


def wants_stdin(kw: dict) -> bool:
    """Does this command line read its task from stdin? The one rule, here, so the door's
    client (serve.forward) reads the same input for a run that the run would read itself,
    whatever the spelling (`task=-`, `--task=-`; review, 5 Oct: it matched one spelling
    and the other arrived with no task)."""
    return kw.get("task") == "-"


def task_from_stdin() -> str:
    """The task `task=-` names. At a terminal it explains itself instead of hanging."""
    if sys.stdin.isatty():
        # Reading from a terminal here would hang with no prompt and no clue why.
        raise FinrailError(
            "task=- reads the task from stdin, but stdin is a terminal.\n"
            '  usage: echo "..." | finrail do <capability> task=-')
    return sys.stdin.read()


#: The asking on a permission question, after its reason: "<reason>. Allow it?". Written
#: here once, where the question is worded for the person (`_watch_log`), and never into
#: the stored question: the question's record and event carry the bare reason, so the
#: outcome states the matter with nothing to strip back off (review, 6 Oct).
ALLOW_IT = ". Allow it?"


def _watch_log(thread: str, kw: dict) -> bool:
    """Follow a conversation's turn from its event log (core/events.py), by seq.

    Returns False, having printed nothing, when there is no log to follow: the caller
    then reads the session as before. Otherwise it streams until the turn ends.

    WHY THE LOG. The session keeps the last 40 steps of a turn and numbers them so a
    reader can page past the trim; the log keeps every step, numbered once for the whole
    conversation, so there is nothing to page and nothing to lose. A reader that dies
    mid-turn comes back with `since=<the last seq it drew>` and misses nothing.

    ONE AGENT'S STEPS. Two agents in one channel share its log; each run's first event
    names its agent, so a watcher sees only the runs of the agent it watches for.
    """
    import os as _os
    import time
    from . import events, questions
    if not events.enabled() or kw.get("source") == "session":
        return False
    # A CONVERSATION'S FIRST TURN races its first write: the watcher is started the
    # moment the turn is dispatched, a beat before converse writes the message that
    # creates the log. So a missing log is waited for, briefly; still missing, this is
    # an install from before the log, and the session feed is used exactly as before.
    grace = time.time() + min(float(kw.get("wait") or 90), float(kw.get("log_wait") or 10))
    while not events._file(thread).is_file():
        if time.time() > grace:
            return False
        time.sleep(min(float(kw.get("interval") or 1.0), 0.25) or 0.05)
    agent = kw.get("agent") or _os.environ.get("FINRAIL_AGENT", "")
    deadline = time.time() + int(kw.get("timeout") or 3600)
    every = float(kw.get("interval") or 1.0)
    appear = time.time() + float(kw.get("wait") or 90)
    given = kw.get("since")
    cursor = int(given) if str(given or "").isdigit() else None
    runs_of, announced, ever_ran = {}, "", False
    # WHERE THE LAST POLL STOPPED, in bytes. The log only grows, so each poll reads
    # what was written since and not the whole file: a watcher on a long conversation
    # parsed every event it ever had, every few seconds, to find one new line.
    offset = 0
    # The questions this feed has passed, by id, so an answer can be worded with the
    # question it answers without a second look at the record.
    asked = {}

    def agent_of(e):
        if e["kind"] == "run":
            runs_of[e["run"]] = (e.get("data") or {}).get("agent") or ""
        return runs_of.get(e["run"], "")

    def minutes(q):
        wait_s = q.get("wait_s")
        return max(1, int((questions.WAIT_S if wait_s is None else wait_s) / 60 + 0.5))

    def footer(qid, by=""):
        """The last line, on its own: the question's id, and the key of whoever
        answered when it was not the person asked. Machine markers, never in the
        sentences a person reads (review, 5 Oct)."""
        return f"\n\nquestion: {qid}" + (f"; answered by {by}" if by else "")

    def prompt(q):
        """The question as the person reads it: plain words first, the asking where it
        belongs, how to answer, how long they have, and the id last as a footer. Core
        words it here, once, so every door shows the same line and none keeps its own
        copy of how to answer (docs/LAYERS.md, rule 3). The stored question is the bare
        reason; the asking (`ALLOW_IT`) is added here, for the kind that takes yes or no."""
        text, mins, qid = q.get("text") or "", minutes(q), q.get("id") or ""
        if (q.get("kind") or questions.PERMISSION) != questions.CLARIFY:
            return (f"❓ {text}{ALLOW_IT}\nReply `/answer yes` or `/answer no`. No answer "
                    f"in {mins} min means no.{footer(qid)}")
        options = [str(o) for o in q.get("options") or []]
        listed = "".join(f"\n{i}. {o}" for i, o in enumerate(options, 1))
        how = (("Reply `/answer 1`" + (" (or several: `/answer 1, 3`)" if q.get("multi")
                                       else "") + ", or `/answer <your own words>`")
               if options else "Reply `/answer <your answer>`")
        return (f"❓ {text}{listed}\n{how}. No answer in {mins} min and the agent decides "
                f"or stops.{footer(qid)}")

    def outcome(d, q):
        """What became of the question, as the person reads it, over the question it
        closes: a transport edits this onto the question's own message. The bare
        reason is the question's text, so nothing is stripped; who answered is "you"
        or "an operator", and an operator's key goes on the footer line."""
        qid, by = d.get("id") or "", d.get("by") or ""
        text = str(q.get("text") or "")
        # A question whose record is gone has no text; its kind, and the person it was
        # put to, are on the answer event as well.
        kind = q.get("kind") or d.get("kind") or questions.PERMISSION
        person = str(q.get("person") or d.get("person") or "")
        # How long it had: on the question, and on an expiry's own answer event too
        # (questions._expire), so a swept record does not read as the default wait.
        timed = q if q.get("wait_s") is not None else d
        you = bool(by) and by.lower() == person.lower()
        whom = (" by you" if you else " by an operator") if by else ""
        tail = footer(qid, by[:8] if by and not you else "")
        closed = f"🛑 Closed, {d['why']}" if d.get("expired") and d.get("why") else ""
        if kind == questions.CLARIFY:
            head = closed or (f"⌛ No answer in {minutes(timed)} min; the agent was told "
                              f"nobody chose" if d.get("expired")
                              else f"💬 Answered{whom}: {d.get('answer') or ''}")
            return f"{head}\n❓ {text}{tail}" if text else f"{head}{tail}"
        head = closed or (f"⌛ No answer in {minutes(timed)} min, so refused" if d.get("expired")
                          else f"✅ Allowed{whom}" if d.get("answer") == "yes"
                          else f"⛔ Refused{whom}")
        return f"{head}: {text}{tail}" if text else f"{head}{tail}"

    while time.time() < deadline:
        th = registry.command("observe.thread").run(thread=thread, agent=kw.get("agent", ""),
                                                   steps="0")
        running, busy = bool(th.get("working")), th.get("run") or ""
        if cursor is None:
            if not running:
                if time.time() > appear:
                    print(json.dumps({"run": "", "kind": "idle"}), flush=True)
                    return True
                time.sleep(every)
                continue
            # The turn's start is the first event of the run holding the thread; learn
            # the agent of every run already in the log on the way. One read of the
            # whole log, and the only one: what comes after is tailed from `offset`.
            first = None
            fresh, offset = events.read_from(thread, 0)
            for e in fresh:
                agent_of(e)
                if e["run"] == busy and first is None:
                    first = e["seq"]
            if first is None:
                # The run holding the thread has not written its first event yet: start
                # after everything already there.
                first = (fresh[-1]["seq"] + 1) if fresh else 1
            cursor = first - 1
        else:
            fresh, offset = events.read_from(thread, offset)
        if busy and busy != announced:
            announced = busy
            print(json.dumps({"run": busy, "kind": "begin"}), flush=True)
        for e in fresh:
            if e["seq"] <= cursor:
                continue
            cursor = e["seq"]
            owner = agent_of(e)
            if agent and owner and owner != agent:
                continue
            if e["kind"] in ("question", "answer"):
                # A question the turn's gate is waiting on, and what became of it
                # (core/questions.py). Its own line, not a step: a transport posts a
                # question to the person; it does not draw it as progress.
                d = e.get("data") or {}
                line = {"run": e["run"], "seq": e["seq"], "kind": e["kind"],
                        "id": d.get("id") or "", "text": d.get("text") or "",
                        "person": d.get("person") or "",
                        "answer": d.get("answer") or "",
                        "by": d.get("by") or "",
                        "expired": bool(d.get("expired")),
                        # Why it closed, when it was not the clock: the run
                        # that asked was stopped, or failed. A transport that
                        # reads only `expired` says "no answer in 10 min"
                        # about a question closed seconds after it was asked.
                        "why": d.get("why") or "",
                        # A clarifying question (which folder?) is answered
                        # in words or by an option, not yes or no.
                        "qkind": d.get("kind") or "permission",
                        "options": d.get("options") or [],
                        "multi": bool(d.get("multi")),
                        "wait_s": d.get("wait_s") or 0}
                # The words a person reads, so a transport posts them as they are:
                # `prompt` with the question, `outcome` with its answer. An answer
                # whose question this feed never passed (a resume) reads the record.
                if e["kind"] == "question":
                    asked[line["id"]] = d
                    line["prompt"] = prompt(d)
                else:
                    line["outcome"] = outcome(
                        d, asked.get(line["id"]) or questions.load(line["id"]))
                print(json.dumps(line), flush=True)
                continue
            if e["kind"] != "step":
                continue
            d = e.get("data") or {}
            print(json.dumps({"run": e["run"], "seq": e["seq"], "n": e["seq"],
                              "at": d.get("at") or e["at"], "kind": d.get("kind") or "step",
                              "text": d.get("text") or ""}), flush=True)
        if running:
            ever_ran = True
        elif ever_ran or given is not None:
            print(json.dumps({"run": busy or announced, "kind": "end", "seq": cursor}),
                  flush=True)
            return True
        time.sleep(every)
    print(json.dumps({"run": announced, "kind": "timeout", "seq": cursor}), flush=True)
    return True


def _watch(target: str, kw: dict) -> None:
    """Follow a run (or a thread's current run) until it finishes, a line at a time."""
    import time
    if kw.get("thread") and _watch_log(kw["thread"], kw):
        return
    seen = 0
    deadline = time.time() + int(kw.get("timeout") or 3600)
    every = float(kw.get("interval") or 1.0)
    # HOW LONG A TURN MAY TAKE TO APPEAR. A watcher is normally started by whoever just
    # dispatched the turn, and the dispatch has not claimed the thread yet — so the first
    # look finds nothing working. Reading that as "finished" made watch print `end` and
    # exit immediately, every single time, which is indistinguishable from a turn that
    # produced no progress at all.
    #
    # Nothing is "done" until it has first been seen RUNNING. Until then an idle thread
    # means "not yet", and this bounds how long that patience lasts.
    appear = time.time() + float(kw.get("wait") or 90)
    run_id = target
    ever_ran = False
    # THE ID, THE MOMENT IT IS KNOWN, AHEAD OF ANY STEP. This loop has always known which
    # run it is following and only ever printed it attached to a step — so a reader learned
    # the id when the engine produced its first piece of progress, and not before. The one
    # client that matters posts that id into a channel as the only thing a person can write
    # `/stop <id>` against, and it showed a bare "working…" for the opening seconds of
    # every turn and the whole of any turn that produced no steps at all. Which is exactly
    # the turn somebody wants to stop.
    announced = ""
    while time.time() < deadline:
        if kw.get("thread") or not registry.has_run(run_id):
            thread = kw.get("thread") or target
            th = registry.command("observe.thread").run(
                thread=thread, agent=kw.get("agent", ""), steps=str(seen))
            # A NEW RUN ON THE SAME THREAD NUMBERS ITS STEPS FROM 1 AGAIN. A folded turn
            # is a second run in the same conversation, and the high-water mark of the
            # first would have hidden every step of the second. So when the run changes,
            # the count starts over and the new run is read from its beginning.
            if announced and th.get("run") and th.get("run") != announced:
                seen = 0
                th = registry.command("observe.thread").run(
                    thread=thread, agent=kw.get("agent", ""), steps="0")
            run_id = th.get("run") or ""
            # converse holds the thread from the first run to the last folded one, so
            # `working` stays true across the fold and there is no gap to mistake for the
            # end of the conversation.
            rows, running = th.get("steps") or [], bool(th.get("working"))
        else:
            r = registry.command("observe.run").run(id=run_id, steps=str(seen))
            rows = r.get("steps") or []
            running = r.get("status") not in ("done", "failed")
        if run_id and run_id != announced:
            announced = run_id
            print(json.dumps({"run": run_id, "kind": "begin"}), flush=True)
        for row in rows:
            print(json.dumps({"run": run_id, **row}), flush=True)
        # THE HIGH-WATER STEP NUMBER, NOT A TALLY OF WHAT ARRIVED. Counting worked only
        # while the session's step list grew; it is trimmed to the last few dozen, so a
        # count kept walking past the end of a list that had stopped getting longer and
        # this loop went quiet for the remainder of every long turn.
        for row in rows:
            n = int(row.get("n") or 0)
            if n > seen:
                seen = n
        if rows and not any("n" in r for r in rows):
            seen += len(rows)                 # pre-numbering turn: the old reading
        if running:
            ever_ran = True
        elif ever_ran:
            print(json.dumps({"run": run_id, "kind": "end"}), flush=True)
            return
        elif time.time() > appear:
            # It never started. Say which of the two silences this is.
            print(json.dumps({"run": run_id, "kind": "idle"}), flush=True)
            return
        time.sleep(every)
    print(json.dumps({"run": run_id, "kind": "timeout"}), flush=True)


def _need(rest: list, index: int, what: str, example: str) -> str:
    """A required positional, or a clean error saying what is missing and how to get it.

    Without this, every id-taking command (`approve`, `view`, `drop`, `kill`, `cron run`)
    died on an IndexError and printed a raw Python traceback — the exact opposite of the
    rule that an error explains its own fix. `do` and `get` already did this by hand; this
    makes it uniform.
    """
    if len(rest) > index and rest[index]:
        return rest[index]
    raise FinrailError(f"missing {what}\n  usage: {example}")


def _out(value, as_json: bool, render=None) -> None:
    if as_json:
        print(json.dumps(value, indent=2, default=str))
    elif render:
        print(render(value))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, default=str))


def _verbatim(cap, body: str, plain: bool = False) -> str:
    """Mark output a capability declared as the answer itself.

    The markers are for whoever RELAYS this. A person reading their own terminal already
    sees the content; an agent passing it on is the one that needs telling, because
    summarising is its default and summarising is what loses the ids, the wording and the
    rows the reader asked for.

    This cannot be enforced from a CLI, and pretending otherwise would be the same sort of
    unverified claim the rest of the system avoids. What it buys is that a paraphrase is
    now VISIBLE as one: the markers are missing.

    `--plain` drops them, for reading it yourself.
    """
    if plain or not getattr(cap, "verbatim", False) or not body.strip():
        return body
    # A FENCED BLOCK, not bare text with markers around it. Telling a relayer "do not
    # summarise" got the content through and still ruined it: chat renders markdown, which
    # collapses runs of spaces, so an aligned table arrives as a jumble. The instruction
    # was obeyed and the output was unreadable — worse than a summary, because it looks
    # like the system produced that mess.
    #
    # Inside a fence the columns survive, which is the closest thing to the previous
    # system's side panel that a CLI can reach on its own.
    fence = "```"
    while fence in body:
        fence += "`"                       # never let content close the block early
    return (f"{cap.name} — verbatim. Relay the block below EXACTLY as it is, fences "
            f"included, so the columns line up. Do not summarise or reformat it.\n"
            f"{fence}text\n{body}\n{fence}")


def _full_preview(cap, proposal: dict) -> str:
    """The capability's own preview, plus anything it left out.

    Approving is a decision about what will be WRITTEN, so the preview has to account for
    the whole proposal. A hand-written `render()` naturally shows the fields its author
    thought interesting, and a field added later is simply missing — which is how a task
    could be approved carrying a `refs` value the preview never mentioned. From the
    reader's side that is indistinguishable from the parameter having been dropped, and
    neither guess can be checked before saying yes.

    So core adds the remainder. Capabilities keep their readable summary; nothing they
    forget stays invisible.
    """
    body = cap.render(proposal) or ""
    shown = body.lower()
    extra = []
    for key, value in (proposal or {}).items():
        if key.startswith("_") or value in (None, "", [], {}):
            continue
        text = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        # Judge by the VALUE, not the field name: what matters is whether this content is
        # already in front of the reader, and a good `render()` shows values without
        # labelling them ("NEW TASK: <title>").
        if text.strip() and text.strip()[:60].lower() in shown:
            continue
        # A STRUCTURED value never matches that way — its repr starts `[{'name': ...`,
        # which no render ever prints — so a fully-rendered list of playlists was shown
        # again as a raw dict underneath its own tidy summary. Ask instead whether the
        # thing it names is on screen: the first real string inside it. A field the
        # render genuinely forgot still fails this and is still surfaced.
        named = _first_leaf(value)
        if named and named.lower() in shown:
            continue
        extra.append(f"    {key}: {text[:120]}")
    if extra:
        body += "\n  also being written:\n" + "\n".join(extra)
    return body


def _first_leaf(value, depth: int = 0):
    """The first substantial string inside a nested value — what it would be called.

    Used to decide whether a structured field is already in front of the reader. Prefers
    an identifying key where there is one, because that is what a render prints.
    """
    if depth > 3:
        return ""
    if isinstance(value, str):
        return value.strip() if len(value.strip()) > 3 else ""
    if isinstance(value, dict):
        for key in ("name", "title", "label", "id"):
            if isinstance(value.get(key), str) and len(value[key].strip()) > 3:
                return value[key].strip()
        for v in value.values():
            got = _first_leaf(v, depth + 1)
            if got:
                return got
        return ""
    if isinstance(value, (list, tuple)):
        for v in value:
            got = _first_leaf(v, depth + 1)
            if got:
                return got
    return ""


def _panel(name: str, kw: dict, title: str = "") -> str:
    """Render a read capability for whoever is going to look at it, and return that.

    A page and a path for the desktop panel, markdown inline for a chat — `view.front_end`
    decides, and `view.deliver` is where that decision lives. This function's job is the
    same either way: refuse anything that is not a read, and hand the capability's own
    result over.

    Only reads: a view is for LOOKING at something, and rendering must not be a way to
    make something happen. The lock needs no exception for this, because it is an finrail
    command like any other — which is the whole reason presentation belongs here rather
    than in whatever is relaying the output.
    """
    from . import view as view_mod
    cap = registry.command(name)
    if not cap.read_only:
        raise FinrailError(
            f"{name} is not a read capability — `view` renders reads.\n"
            f"  what exists: finrail capabilities read")
    # `title`, `out` and `share` are instructions to the RENDERER, not parameters of the
    # capability being rendered — passing them on made `view … share=true` die with
    # "run() got an unexpected keyword argument 'share'". `share` is otherwise unused now
    # (there is no publish path); still stripped so a stray one cannot crash the call.
    params = {k: v for k, v in (kw or {}).items()
              if k not in ("title", "out", "share")}
    result = cap.run(**schema.coerce(cap, dict(params)))
    shown = ", ".join(f"{k}={v}" for k, v in params.items())
    stem = title or (f"{name} {' '.join(str(v) for v in params.values())}".strip()
                     if params else name)

    def _dest() -> str:
        # Called only by a front end that actually writes a file — which is why it is a
        # function. A markdown view leaves nothing behind, so it must not record a run
        # and reserve an artifact directory for a page that never gets written.
        if kw.get("out"):
            return kw["out"]
        # Under the run that produced it. A temp directory was a mistake made while
        # trying to avoid clutter: it is not somewhere a person can find a page again,
        # and it put the file outside the tree everything else lives in.
        rec = tracker.save(name, "", dict(params), status=tracker.DONE,
                           kind="read", source="view")
        return str(tracker.artifacts(rec["id"]) / "view.html")

    return view_mod.deliver(cap, result, title or name, subtitle=shown, slug=stem,
                            path_for=_dest)


def _render_run(rec: dict) -> str:
    """A proposal or a result, as a human reads it."""
    if rec.get("id") is None and rec.get("kind") == "read":
        cap = registry.command(rec["capability"])
        return _verbatim(cap, cap.render_data(rec["result"]), rec.get("_plain"))
    cap = registry.command(rec["capability"])
    head = f"[{rec['status']}] {rec['capability']}  run {rec['id']}"
    if rec.get("already"):
        # Approving something already handled reprinted the head and the RESULT, which
        # reads exactly like it just ran again. What the caller needs to know is that
        # nothing happened this time, and why.
        return (f"· {rec['capability']}  run {rec['id']}: {rec.get('note', 'already handled')}"
                f"\n  what it did: finrail view {rec['id']}"
                f"\n  what is actually outstanding: finrail status")
    body = ""
    steps = rec.get("progress") or []
    if rec["status"] == tracker.RUNNING and steps:
        # What someone actually wants when a command is taking a long time: not the
        # state machine's word for it, but what the thing is doing.
        recent = "\n".join(f"  {s.get('at', '')[11:]} {s.get('kind', '')}: "
                            f"{s.get('text', '')}" for s in steps[-8:])
        return (f"[running] {rec['capability']}  run {rec['id']}\n{recent}"
                + (f"\nsession {rec['session']}" if rec.get("session") else "")
                + f"\ncheck: finrail view {rec['id']}")
    if rec.get("worker_pid") and rec["status"] == tracker.PENDING:
        # Handed to a detached worker, which flips the status when it starts. Checking
        # PENDING first meant a run that was ALREADY RUNNING was presented as waiting for
        # approval — for a capability that declares it needs none. The declaration was
        # right and the display was wrong, which is the worse way round: it teaches you to
        # distrust the declarations.
        body = (f"handed to a background worker (pid {rec['worker_pid']}) — "
                f"it needs no approval")
        tail = f"\ncheck: finrail view {rec['id']}"
    elif rec["status"] == tracker.PENDING:
        body = _full_preview(cap, rec["proposal"])
        tail = (f"\n\napprove: finrail approve {rec['id']}"
                f"\ndiscard: finrail drop {rec['id']}")
        if cap.reason:
            tail += f"\n(why approval: {cap.reason})"
    elif rec["status"] == tracker.DONE:
        body = (_verbatim(cap, cap.render_data(rec["result"]), rec.get("_plain"))
                if rec.get("result") is not None else "")
        tail = ""
    elif rec.get("background") and rec["status"] == tracker.RUNNING:
        # Only while it is ACTUALLY running. Testing `background` alone meant a
        # background run that had FAILED was reported as still running, and its errors —
        # the only place the failure was recorded — were never printed by the command
        # whose whole job is to show what happened. A deploy that rolled back looked
        # identical to one still pulling.
        body = f"running in the background (pid {rec.get('worker_pid')})"
        tail = f"\ncheck: finrail view {rec['id']}"
    else:
        body = "\n".join(str(e) for e in rec.get("errors") or []) or "(no detail)"
        tail = ""
    return f"{head}\n{body}{tail}"


def _render_approvals(out) -> str:
    """One line per run, so a batch cannot report less than it did.

    Including the ones that did NOT go through: a failure in the middle of a list is one
    failure, and printing only its error line is how nineteen other outcomes — approved,
    already done, still pending — went unreported.
    """
    rows = out["approved"]
    lines = []
    for rec in rows:
        if rec.get("approved") is False:
            mark = "✗"
            what = rec.get("note") or "; ".join(str(e) for e in rec.get("errors") or [])
        else:
            mark = "·" if rec.get("already") else "✓"
            what = rec.get("note") or f"approved — {rec.get('status', '?')}"
        lines.append(f"  {mark} {rec['id']}  {rec.get('capability', '')}: {what}")
    bad = [r for r in rows if r.get("approved") is False]
    head = f"{len(rows)} run(s)" + (f" — {len(bad)} did not go through" if bad else "")
    return f"{head}\n" + "\n".join(lines)


def _render_runs(rows: list) -> str:
    """Compact on purpose: a long dump of every run floods the context of whoever is
    reading it, which is the opposite of useful."""
    if not rows:
        return "(no runs yet)"
    out = [f"{len(rows)} run(s)", "  id        status   capability            when"]
    for r in rows:
        # "pending" meant two things: waiting for YOU, and approved but waiting for a
        # worker. Only the first is yours to act on, and printing one word for both is
        # what made an operator approve the same run twice and doubt the ledger.
        state = r["status"]
        if state == "pending" and not tracker.awaiting(r):
            state = "queued"
        mark = {"pending": "…", "queued": "»", "running": "▶", "done": "✓",
                "failed": "✗"}.get(state, "?")
        out.append(f"  {r['id']}  {mark} {state:7} {r['capability'][:20]:21} "
                   f"{(r.get('updated') or r['created'])[5:16]}")
    pend = [r["id"] for r in rows if tracker.awaiting(r)]
    queued = [r for r in rows if r["status"] == "pending" and not tracker.awaiting(r)]
    if pend:
        out.append(f"\nawaiting you: {' '.join(pend)}   "
                   f"(finrail approve {' '.join(pend)})")
    if queued:
        out.append(f"queued, nothing to do: {' '.join(r['id'] for r in queued)}")
    return "\n".join(out)


def _render_services(rows: list) -> str:
    """Declared vs actually running, plus HOW each was deployed — the plist that defines
    it and the command it runs. Without that, "is it running?" and "what is it running?"
    need two more commands to answer."""
    if not rows:
        return "(no services declared)"
    out = []
    for r in rows:
        if not r["loaded"]:
            state = "NOT LOADED"
        elif r["interval"]:
            state = f"loaded, every {r['interval']}s (idle between runs)"
        elif r["running"]:
            state = f"running (pid {r['pid']})"
        else:
            state = "loaded but NOT running"
        mark = "✓" if r["loaded"] and (r["running"] or r["interval"]) else "✗"
        out.append(f"  {mark} {r['label']}   [{r['plugin']}]")
        out.append(f"      {r['description']}")
        out.append(f"      state:  {state}")
        if r["loaded"] and not r["plist_current"]:
            out.append("      ⚠ the loaded plist differs from what finrail would "
                       "write now — reinstall to pick up config changes")
        out.append(f"      via:    launchd, {r['plist']}")
        out.append(f"      runs:   {' '.join(r['command'])[:150]}")
    out.append("\ninstall/reload: finrail service install <label>"
               "     restart as installed: finrail service restart <label>"
               "     stop: finrail service uninstall <label>")
    return "\n".join(out)


def _capabilities(kind: str = "") -> str:
    rows = registry.catalogue()
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    if not rows:
        return "(no capabilities — is anything installed?)"
    by_bundle = {}
    for r in rows:
        by_bundle.setdefault(r["bundle"], []).append(r)
    out = [f"{len(rows)} capabilities"]
    for bundle in sorted(by_bundle):
        out.append(f"\n{bundle}")
        for r in sorted(by_bundle[bundle], key=lambda x: x["name"]):
            gate = ("read" if r["read_only"] else
                    "asks" if r["confirm"] else
                    "auto" + ("+bg" if r["background"] else ""))
            out.append(f"  {r['name']:24} {gate:8} {r['description'][:44]}")
    out.append("\nparameters: finrail -h2 <capability>")
    return "\n".join(out)


def _rules(name: str = "") -> str:
    rows = [schema.describe(registry.command(name))] if name else [
        schema.describe(registry.command(r["name"])) for r in registry.catalogue()]
    out = []
    for d in rows:
        out.append(f"{d['name']}  [{d['kind']}]  {d['description']}")
        out.append(f"    {d['signature']}")
        out.append(f"    read_only={d['read_only']} autonomous={d['autonomous']} "
                   f"confirm={d['confirm']} background={d['background']}")
        if d.get("verbatim"):
            out.append("    verbatim=True  (its output IS the answer — relay it "
                       "unchanged; do not summarise)")
        if d.get("session_kind"):
            # Conversational capabilities behave differently across runs — the model
            # remembers the previous turn — so a reader deciding how to use one has to be
            # told, not left to infer it from a `session=` parameter.
            out.append(f"    session_kind={d['session_kind']}  "
                       f"(runs form a session; the model remembers earlier turns — "
                       f"continue with session=<id>)")
        if d["reason"]:
            out.append(f"    why: {d['reason']}")
        if not d["read_only"]:
            # "What would make you believe this worked?" is a rule with consequences, so
            # it is printed beside the other rules rather than being something you find
            # out by reading the executor.
            out.append(f"    verified by: {d['verified_by']}" if d.get("verified_by")
                       else "    verified by: whatever the result reports (ok / verified "
                            "/ success) — this capability does not say more")
            if d.get("verifies"):
                # A real distinction, and not the same as declaring how: this capability
                # goes and LOOKS again after the fact, rather than trusting what its own
                # executor put in the result.
                out.append("                 and it re-checks after the run, rather than "
                           "trusting its own result")
    lessons = _standing_lessons()
    if lessons:
        out.append(lessons)
    return "\n".join(out)


def _standing_lessons() -> str:
    """Lessons whose reader is whoever DRIVES finrail, printed where they will be seen.

    A lesson nothing reads is a diary. These are recorded by `memory.learn` from inside a
    session — the only way to write one there, since a locked workspace has no `Write` —
    and they are worth nothing unless the NEXT session sees them. So they go on the two
    outputs a session actually reads before doing anything: the bare invocation and
    `rules`.

    Deliberately not a document to maintain. `CLAUDE.md` says to run `finrail` first;
    what that prints comes from a file the system updates itself, so catching something
    once is enough and nobody has to remember to edit a guide.

    CORE DOES NOT KNOW WHO KEEPS THEM. This used to import the memory plugin by name,
    which is a tier above this one (docs/LAYERS.md, rule 1): core must work with nothing
    above it loaded. So it asks the `standing_lessons` hook, and whichever plugin keeps
    lessons answers. No subscriber, no lessons, and nothing here has to know why.
    """
    try:
        from . import registry
        registry.load()
        text = (registry.RUNTIME.hooks.emit("standing_lessons", "") or "").strip()
    except Exception:                                            # noqa: BLE001
        return ""
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return ("\nLearned here — these apply to you, not to a capability:\n"
            + "\n".join(f"  {ln}" for ln in lines))


def _first_run() -> str:
    settings.ensure()
    d = provision.doctor()
    lines = ["finrail is installed and its state directory is ready.",
             f"  state:   {settings.state()}",
             f"  content: {settings.home()}", ""]
    if d["ok"]:
        lines.append("doctor: everything passes. Try:  finrail capabilities")
        lessons = _standing_lessons()
        if lessons:
            lines.append(lessons)
    else:
        lines.append(f"doctor: {d['failing']} of {d['total']} checks need attention.")
        lines.append(f"  next: {d['next']}")
        lines.append("")
        lines.append("The guided path sets all of it up in order:")
        lines.append("  finrail setup")
    lessons = _standing_lessons()
    if lessons and d["ok"] is False:
        lines.append(lessons)
    return "\n".join(lines)


def _from_the_command_line() -> bool:
    """Only a real `finrail` process is forwarded, never an in-process call to main()
    (tests, and the door's own child, which is marked INSIDE anyway)."""
    return os.path.basename(sys.argv[0] or "") in ("finrail", "__main__.py", "-m")


def main(argv: list = None) -> int:
    # Asked BEFORE argv becomes a list: both real ways in (`python -m finrail`, the
    # `finrail` script) call main() with nothing, and a caller inside this process
    # hands its own list. Asked after, it was always true, and under `python -m pytest`
    # (whose argv[0] is a `__main__.py` too) a test's main([...]) went to the live door
    # (review, 5 Oct).
    bare = argv is None
    argv = list(sys.argv[1:] if bare else argv)
    if bare and _from_the_command_line():
        # 2b: an agent's own process comes in by its door, which sees who is asking.
        from . import serve as _serve
        code = _serve.forward(argv)
        if code is not None:
            return code
    # `-h2 <cap>` is the short form of `--h2=<cap>`; normalise before parsing so the
    # flag cannot be mistaken for a positional command.
    if "-h2" in argv:
        i = argv.index("-h2")
        name = argv[i + 1] if len(argv) > i + 1 else ""
        argv = argv[:i] + [f"--h2={name}"] + argv[i + 2:]
    # `policy` takes a raw shell command, which routinely contains `=` — and _split()
    # treats anything with `=` as a k=v pair. That silently ATE the argument, so the gate
    # asked about an empty command and denied it. From the caller's side that looked like
    # "finrail get mail.inbox works but adding account=… is forbidden", with no stated
    # reason — which is exactly the guessing this system exists to prevent. Handled before
    # parsing, because the argument is not a parsed thing.
    # `ask` takes one thing: what you want, in your words. Those words routinely contain
    # `=` — quoting an error, naming a flag — and the argv parser turns anything with an
    # `=` into a k=v pair, so asking about `now=true` produced
    # "unknown parameter(s) tasks.add rejected now". The front door refused the question
    # BECAUSE of what the question was about.
    if argv and argv[0] == "ask":
        question = " ".join(argv[1:]).strip()
        if not question:
            raise FinrailError('ask what?\n  usage: finrail ask "show me my inbox"')
        _out(dispatch_core.dispatch("ask", {}, task=question),
             "--json" in argv, _render_run)
        return 0

    if argv and argv[0] == "policy":
        # `policy --scope <file>`: a scoped agent's hook, judging ONE tool call. The
        # payload is Claude Code's hook JSON on stdin — tool name, its input and the
        # shell's cwd — because a Write's path or a git's directory is not a command
        # string, and the scope needs all three to answer.
        if len(argv) >= 3 and argv[1] == "--scope":
            from . import scope as _scope
            try:
                payload = json.load(sys.stdin)
            except Exception:                                      # noqa: BLE001
                payload = None
            d = _scope.judge(argv[2], payload)
            print(f"{d['decision']}: {d['reason']}")
            # A second line the gate passes to the model as additionalContext: what an
            # allow after a question meant (core/scope.settle). Absent otherwise.
            if d.get("context"):
                print("context: " + " ".join(str(d["context"]).split()))
            # A third: the tool's input as the gate rewrote it. Only a clarifying
            # question has one, carrying the person's answers (core/scope.clarify).
            if d.get("input") is not None:
                print("input: " + json.dumps(d["input"]))
            return 0
        if len(argv) >= 2 and argv[1] == "--permission":
            # The full agent's PermissionRequest hook, when it runs asking instead of
            # bypassing (`full_agent.ask_person`): the person decides in the chat.
            from . import scope as _scope
            try:
                payload = json.load(sys.stdin)
            except Exception:                                      # noqa: BLE001
                payload = None
            d = _scope.permission(payload)
            print(f"{d['decision']}: {' '.join(str(d['reason']).split())}")
            return 0
        raw = " ".join(argv[1:]).strip()
        if not raw:
            print("deny: no command given to policy")
            return 0
        d = policy.resolve(raw)
        print(f"{d['decision']}: {d['reason']}")
        return 0

    pos, kw, flags = _split(argv)
    as_json = "json" in flags
    # `--plain` drops the verbatim markers. They exist for whoever RELAYS the output; when
    # you are reading it yourself they are just noise.
    plain = "plain" in flags

    def _run_render(rec):
        return _render_run({**rec, "_plain": plain} if isinstance(rec, dict) else rec)

    try:
        if "help" in flags or (pos and pos[0] in ("help", "-h", "--help")):
            print(USAGE)
            return 0
        if kw.get("h2"):
            _out(schema.describe(registry.command(kw["h2"])), as_json,
                 lambda d: _rules(d["name"]))
            return 0
        if not pos:
            print(_first_run())
            return 0

        cmd, rest = pos[0], pos[1:]
        settings.ensure()

        # ── the ledger ───────────────────────────────────────────────────────
        if cmd == "runs":
            _out(tracker.recent(int(kw.get("n", 25))), as_json, _render_runs)
        elif cmd == "status":
            # `status` is "what is in flight", not "what has ever run" — saying
            # "(no runs yet)" when 25 are recorded reads as a broken ledger.
            _out(tracker.open_runs(), as_json, lambda rows: _render_runs(rows)
                 if rows else "nothing in flight (finrail runs — the full ledger)")
        elif cmd == "view":
            target = _need(rest, 0, "a run id or a read capability",
                           "finrail view <run-id>   |   finrail view mail.inbox")
            # Which one is decided by ASKING THE REGISTRY, not by guessing from the shape
            # of the string. A capability name is a capability name; anything else is a
            # run id and fails as one, with a run id's error.
            if registry.has(target):
                _out(_panel(target, kw, kw.get("title", "")), as_json, lambda s: s)
            else:
                _out(tracker.load(target), as_json, _run_render)
        elif cmd == "approve":
            # SEVERAL ids in one call. Chaining with `&&` was the only way to approve a
            # batch, and one already-done id in the chain aborted the rest silently — so
            # "I approved three" and "one was approved" both looked the same from here.
            ids = rest or [_need(rest, 0, "a run id",
                                 "finrail approve <run-id> [<run-id> ...]"
                                 "   (see: finrail status)")]
            if len(ids) == 1:
                # One id, one answer: a run that cannot be approved is the whole outcome,
                # and it still raises with its own explanation and a non-zero exit.
                _out(dispatch_core.approve(ids[0]), as_json, _run_render)
            else:
                # EVERY id IS ATTEMPTED. A raise partway down the list left the ids after
                # it pending and unmentioned — five approved, one error line printed,
                # fourteen never tried and no output saying so.
                done = [dispatch_core.approve_safely(rid) for rid in ids]
                _out({"approved": done}, as_json, _render_approvals)
                if any(r.get("approved") is False for r in done):
                    # Part of what was asked for did not happen, and an exit code of 0
                    # tells a script — or an assistant reading it — that all of it did.
                    return 1
        elif cmd == "drop":
            if "all" in flags:
                gone = tracker.drop_pending()
                _out({"dropped": [g["id"] for g in gone]}, as_json,
                     lambda d: f"dropped {len(d['dropped'])} run(s)")
            else:
                # `reason=` is what was wrong with it, in the person's words. It rides on
                # the verdict the drop emits; whoever subscribes decides what to keep.
                # Words after the id are refused, not ignored: typed the way `/drop` takes
                # them in a chat, the run was dropped and the reason lost without a word.
                if len(rest) > 1:
                    raise FinrailError(
                        f"finrail drop takes one run id, and nothing was dropped: "
                        f"{' '.join(rest[1:])!r} would have been lost.\n"
                        f"  to say why: finrail drop {rest[0]} reason=\"...\"")
                _out(tracker.drop(_need(rest, 0, "a run id",
                                        "finrail drop <run-id> [reason=\"...\"]   "
                                        "(or: finrail drop --all)"),
                                  reason=kw.get("reason") or ""),
                     as_json,
                     lambda r: f"dropped {r['id']} ({r['capability']})"
                               + (f": {r['drop_reason']}" if r.get("drop_reason") else ""))
        elif cmd == "watch":
            # STREAMS, rather than answering once. A front end wants to know what a turn
            # is doing WHILE it does it, and the only way to learn that before this was
            # to poll <state>/sessions/<id>.json directly — which is how a client ends up
            # owning a copy of finrail's on-disk layout, and how progress silently
            # stopped the day thread records moved.
            #
            # One JSON object per line, each carrying only the steps not yet emitted, so
            # a reader appends rather than diffing. It ends when the run does.
            # Either form. The usage line has always offered `thread=<name>`, which
            # `_need` could never accept because it only looks at positionals — so the
            # documented form failed with the very message that documents it.
            target = kw.get("thread") or (rest[0] if rest else "")
            if not target:
                raise FinrailError(
                    "watch what?\n  usage: finrail watch <run-id>"
                    "   |   finrail watch thread=<name>")
            _watch(target, kw)
            return 0
        elif cmd == "kill":
            _out(dispatch_core.kill(_need(
                rest, 0, "a run id",
                "finrail kill <run-id>   (see: finrail status)")), as_json)
        elif cmd == "_job":
            dispatch_core.run_job(_need(rest, 0, "a run id", "finrail _job <run-id>"))

        # ── acting ───────────────────────────────────────────────────────────
        elif cmd in ("do", "dispatch", "get"):
            # `--detach`: the work runs in a worker of its own and outlives the turn or the
            # terminal that asked; an agent's turn is told in its thread how it ended.
            if not rest:
                raise FinrailError(f"{cmd}: which capability? (finrail capabilities)")
            name, free = rest[0], " ".join(rest[1:])
            _out(dispatch_core.dispatch(name, kw, task=free, detach="detach" in flags), as_json, _run_render)
        elif cmd == "ask":
            _out(dispatch_core.dispatch("ask", kw, task=" ".join(rest)), as_json,
                 _run_render)

        # ── discovery ────────────────────────────────────────────────────────
        elif cmd in ("capabilities", "caps"):
            _out(registry.catalogue(), as_json,
                 lambda _: _capabilities(rest[0] if rest else ""))
        elif cmd == "rules":
            _out({"catalogue": registry.catalogue()}, as_json,
                 lambda _: _rules(rest[0] if rest else ""))

        # ── health ───────────────────────────────────────────────────────────
        elif cmd == "doctor":
            d = provision.doctor(rest[0] if rest else "")
            _out(d, as_json, provision.render)
            return 0 if d["ok"] else 1
        elif cmd == "provision":
            r = provision.provision(rest[0] if rest else "",
                                    dry_run="dry_run" in flags or "dry-run" in flags)
            _out(r, as_json, provision.render)
            return 0 if r["ok"] else 1
        elif cmd == "audit":
            _out(policy.audit(), as_json,
                 lambda f: "no findings" if not f else "\n".join(
                     f"  ✗ {x['capability']}: {x['problem']}" for x in f))
        elif cmd == "policy":
            _out(policy.resolve(" ".join(rest)), as_json,
                 lambda d: f"{d['decision']}: {d['reason']}")

        # ── managers ─────────────────────────────────────────────────────────
        elif cmd == "serve":
            # One door per agent: a local socket both ways (core/serve.py). Runs until
            # killed; the agent's own unit starts it beside the chat client.
            from . import serve as _serve
            _serve.serve(kw.get("socket") or None)
        elif cmd == "cron":
            sub = rest[0] if rest else "list"
            if sub == "tick":
                _out(cron.tick(), as_json)
            elif sub == "reap":
                _out(cron.reap(), as_json)
            elif sub == "run":
                _out(cron.run_job(_need(rest, 1, "a job name",
                                        "finrail cron run <job>   (see: finrail cron list)")),
                     as_json)
            elif sub in ("enable", "disable"):
                _out(cron.enable(_need(rest, 1, "a job name",
                                       f"finrail cron {sub} <job>   (see: finrail cron list)"),
                                 sub == "enable"), as_json)
            else:
                _out(cron.jobs(), as_json, lambda js: "\n".join(
                    f"  {'on ' if j['enabled'] else 'off'} {j['name']:22} "
                    f"{j['schedule']:12} -> {j['capability']:20} "
                    f"last: {j['last_run'] or 'never'} {j['last_status'] or ''}"
                    for j in js) or "(no cron jobs declared)")
        elif cmd == "connection":
            from . import connections
            sub = rest[0] if rest else "list"
            if sub == "types":
                _out({n: ct.description for n, ct in connections.types().items()}, as_json,
                     lambda d: "\n".join(f"  {k:16} {v}" for k, v in sorted(d.items()))
                     or "(no connection types)")
            elif sub == "add":
                _out(connections.add(rest[1], kw.pop("type", rest[2] if len(rest) > 2
                                                     else ""), kw), as_json)
            elif sub == "set":
                for k, v in kw.items():
                    connections.set_field(rest[1], k, v)
                _out({"connection": rest[1], "set": sorted(kw)}, as_json)
            elif sub == "test":
                if len(rest) > 1:
                    ok, detail = connections.test(rest[1])
                    _out({"connection": rest[1], "ok": ok, "detail": detail}, as_json,
                         lambda d: f"{'✓' if d['ok'] else '✗'} {d['connection']}: "
                                   f"{d['detail']}")
                    return 0 if ok else 1
                _out(connections.test_all(), as_json)
            elif sub == "remove":
                _out(connections.remove(rest[1]), as_json)
            else:
                _out(connections.names(), as_json,
                     lambda ns: "\n".join(f"  {n}" for n in ns) or "(no connections)")
        elif cmd == "service":
            from . import services
            from .api import RUNTIME
            registry.load()
            sub = rest[0] if rest else "list"
            if sub == "list":
                # Declared AND live state together: "what should run" and "what is
                # actually running" are different questions, and showing only the first
                # makes a dead service look fine.
                rows = []
                for s in RUNTIME.services.values():
                    st = services.status(s)
                    rows.append({"label": s.label, "plugin": s.plugin,
                                 "description": s.description,
                                 "loaded": st["loaded"], "running": st["running"],
                                 "pid": st["pid"],
                                 "plist_current": st["plist_current"],
                                 "interval": s.interval,
                                 "plist": str(s.plist_path()),
                                 "command": s.command})
                _out(rows, as_json, _render_services)
            else:
                spec = RUNTIME.services.get(rest[1]) if len(rest) > 1 else None
                if spec is None:
                    raise FinrailError(f"no service '{rest[1] if len(rest) > 1 else ''}'")
                if sub in ("install", "uninstall") and getattr(spec, "switched_by", ()):
                    # THE OWNER'S RECORD FIRST. Written by hand, the unit and the record
                    # of whether the agent is on can disagree again, which is how one
                    # agent came to answer on two harnesses (docs/LAYERS.md, rule 2).
                    on, off = spec.switched_by
                    raise FinrailError(
                        f"{spec.label} is switched by its owner, which records the "
                        f"decision and then writes the unit: "
                        f"{on if sub == 'install' else off}")
                if sub == "install":
                    _out(services.install(spec), as_json)
                elif sub == "restart":
                    _out(services.restart(spec), as_json)
                elif sub == "uninstall":
                    _out({"service": spec.label, "detail": services.unload(spec)}, as_json)
                else:
                    _out(services.status(spec), as_json)

        # ── plugin-owned surfaces ────────────────────────────────────────────
        # Ergonomic shim: `settings set foo=bar` and `secret set NAME` read better than
        # the strict `name=`/`value=` form, so translate here rather than loosening the
        # capability's signature (which would let real typos through).
        elif cmd == "settings" and rest and rest[0] in ("set", "unset"):
            pairs = [(k, v) for k, v in kw.items() if k not in ("name", "value")]
            if pairs and "name" not in kw:
                if len(pairs) > 1:
                    raise FinrailError(
                        "set one setting at a time: " + ", ".join(k for k, _ in pairs))
                kw = {"name": pairs[0][0], "value": pairs[0][1]}
            if rest[0] == "unset":
                kw.pop("value", None)
            _out(dispatch_core.dispatch(f"settings.{rest[0]}", kw), as_json, _run_render)
        elif cmd == "secret" and rest and rest[0] in ("set", "check", "delete", "needed"):
            if len(rest) > 1 and "name" not in kw:
                kw["name"] = rest[1]
            _out(dispatch_core.dispatch(f"secret.{rest[0]}", kw), as_json, _run_render)
        elif registry.has(cmd):
            name, free = cmd, " ".join(rest)
            _out(dispatch_core.dispatch(name, kw, task=free, detach="detach" in flags), as_json, _run_render)
        elif registry.has(f"{cmd}.{rest[0]}" if rest else ""):
            _out(dispatch_core.dispatch(f"{cmd}.{rest[0]}", kw,
                                        task=" ".join(rest[1:]),
                                        detach="detach" in flags), as_json, _run_render)
        else:
            print(f"unknown command '{cmd}'\n")
            print(USAGE)
            return 2

    except NotConfigured as e:
        print(f"✗ {e}", file=sys.stderr)
        return 3
    except FinrailError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
