"""TASKS extension — one markdown note per task, in the vault.

Note what this plugin does NOT contain: any git code (the repository plugin commits its
writes through the write-guard), any learning code (the memory plugin injects lessons into
`tasks.add`), and any indexing code (the brain plugin ingests the same notes). Those all
work because of where the tasks live, not because this plugin cooperates with them.
"""
import datetime
import json
import os

from finrail.core.errors import ContractViolation
from finrail.core.requirements import Directory, Manual
from finrail.core.settings import SettingSpec
from finrail.plugin import Plugin
from . import ops

NAME = "tasks"
DESCRIPTION = "Tasks as typed markdown notes."

#: The plugin API this was written against — see finrail.API.
REQUIRES_API = 1

#: The stores this plugin owns (core/owners.py, docs/LAYERS.md rule 2).
STORES = lambda: [ops.root()]          # noqa: E731

SETTINGS = [
    SettingSpec("tasks.dir", "path", required=False,
                doc="Folder holding task notes (default: <content>/knowledge-vault/tasks)"),
]


def REQUIREMENTS():
    """No tasks folder resolvable is a gap doctor must report, not silence — an unset
    tasks.dir with no vault guess must not vanish from a fresh-install doctor run."""
    try:
        d = ops.root()
    except Exception:                                          # noqa: BLE001
        return [Manual(
            "tasks.dir",
            probe=lambda: (False, "no tasks folder configured"),
            hint="finrail settings set tasks.dir=<path to your tasks folder>")]
    return [Directory("tasks.dir", d,
                      hint="finrail settings set tasks.dir=<path>"),
            Directory("tasks.open", d / "open", after=["tasks.dir"])]


# ── markdown for a chat front end ──────────────────────────────────────────
#: What the desktop page says without words, and how to say it in a chat.
#:
#: `view._cell` colours a status pill (todo red, doing amber, done green), draws `now` as
#: ⚡, renders tags as chips and an empty value as an em dash. A chat has no colour and
#: about a third of the width, so each of those needs an equivalent rather than a copy:
#: the pill becomes a heading the rows are grouped under, ⚡ moves onto the title it
#: qualifies, and the columns the page only shows because they exist — `slug`, `created`,
#: `updated` — are dropped. They are in the page for no better reason than that
#: `_columns` appends whatever it has not been told to skip.
_NOW = "⚡"
_OVERDUE = "⚠"
_BLOCKED = "⛔"
_GATE = "🚧"
_TAG_CAP = 4          # the page stops at 6 chips; a chat column is narrower


def _md(val) -> str:
    """One value in a markdown table cell — a pipe ends the cell, a newline the row."""
    if val is None or val == "":
        return "—"
    return str(val).replace("|", "\\|").replace("\n", " ").strip() or "—"


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _late(val, status: str = "") -> bool:
    """Has this task's due date passed while it was still open?

    Both halves matter, and `ops.listing`'s own `overdue=` filter agrees on both: a date
    in the past on a task that is done is not a problem, it is a record.
    """
    if not val or status in ops.CLOSED:
        return False
    try:
        return str(val)[:10] < datetime.date.today().isoformat()
    except (TypeError, ValueError):
        return False


def _due(val, status: str = "") -> str:
    """A due date, and whether it has passed.

    The page has no idea: it prints the date and only reddens the literal word
    "overdue", which a `due` cell never contains. Here the comparison is cheap and the
    answer is the whole reason the column is on screen.
    """
    if not val:
        return ""
    day = str(val)[:10]
    return f"{_OVERDUE} {day}" if _late(val, status) else day


def _tags(vals) -> str:
    vals = [str(t) for t in (vals or [])]
    if not vals:
        return ""
    shown = ", ".join(vals[:_TAG_CAP])
    extra = len(vals) - _TAG_CAP
    return f"{shown} +{extra}" if extra > 0 else shown


def _tasks_markdown(self, out):
    """A task list a chat can act on.

    The generic renderer draws every key `_columns` finds — eleven of them, `slug` and
    two ISO timestamps included — so the title, the one thing you read a task list FOR,
    arrived seventh and wrapped. This keeps the page's information and spends the width
    on the four things you scan: what it is, when it is due, how urgent, and the id you
    act on.

    Nothing is re-sorted. `ops.listing` already orders by status, then `now`, then
    priority, then due date, so the status headings are a rule drawn across an order that
    was already there — not a regrouping that would quietly disagree with the terminal
    and the page about which task comes first.
    """
    rows = out.get("tasks") or []
    count, shown = out.get("count", len(rows)), out.get("shown", len(rows))
    head = f"### Tasks · {count}"
    if shown != count:
        head += f" · showing {shown}"
    if not rows:
        return "### Tasks\n\n(no tasks match)"

    # A column that reads the same all the way down is width spent saying nothing, so it
    # moves into the caption instead — the same rule the mail listings use for `account`.
    statuses = [r.get("status") or "" for r in rows]
    assignees = {(r.get("assignee") or "") for r in rows}
    group = len(set(statuses)) > 1
    show_who = len(assignees) > 1
    caption = []
    if not group and statuses and statuses[0]:
        caption.append(f"all {statuses[0]}")
    if not show_who and any(assignees):
        caption.append(f"assigned to {sorted(assignees)[0]}")
    nows = sum(1 for r in rows if r.get("now"))
    if nows:
        caption.append(f"{nows} {_NOW} now")
    late = sum(1 for r in rows if _late(r.get("due"), r.get("status") or ""))
    if late:
        caption.append(f"{late} {_OVERDUE} overdue")

    cols = ["id", "pri", "due", "title", "tags"] + (["who"] if show_who else [])
    lines = [head] + ([f"_{' · '.join(caption)}_"] if caption else [])

    def _table_head():
        return ["", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]

    seen = None
    if not group:
        lines += _table_head()
    for r in rows:
        status = r.get("status") or ""
        if group and status != seen:
            seen = status
            n = sum(1 for s in statuses if s == status)
            lines += ["", f"**{status or '(no status)'}** · {n}"] + _table_head()
        title = _md(_clip(r.get("title") or "(untitled)", 72))
        # ⚡ leads the title rather than holding a column of its own, which is where the
        # page puts a whole column to carry one glyph per row.
        if r.get("now"):
            title = f"{_NOW} **{title}**"
        if r.get("gate"):
            title = f"{_GATE} {title} _({r['gate']} gate)_"
        if r.get("open_blockers"):
            # Waiting on something. The ids are what you would act on, so they are shown.
            title = f"{_BLOCKED} {title} _(after {', '.join(r['open_blockers'])})_"
        pri = r.get("priority") or ""
        # Bold is the only emphasis a chat has, so it is spent on the one value that
        # changes what you do next. The page can afford three pill colours; this cannot.
        pri = "**high**" if pri == "high" else (pri or "")
        row = [f"`{_md(r.get('id') or '------')}`", _md(pri),
               _md(_due(r.get("due"), status)), title,
               _md(_clip(_tags(r.get("tags")), 40))]
        if show_who:
            row.append(_md(_clip(r.get("assignee") or "", 14)))
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "_open one: `finrail get tasks.show term=<id>` · "
                  "close: `finrail do tasks.done term=<id>`_"]
    return "\n".join(lines)


# ── read ────────────────────────────────────────────────────────────────────
class List(Plugin):
    name = "tasks.list"
    verbatim = True   # the list, with the ids you act on
    description = "Tasks, filtered. Compact by design."
    read_only = True
    confirm = False

    def run(self, status: str = "", now: bool = False, assignee: str = "",
            tag: str = "", priority: str = "", text: str = "",
            overdue: bool = False, archived: bool = False, due_before: str = "",
            limit: int = 0, parent: str = "", blocked_by: str = ""):
        return ops.listing(status, now, assignee, tag, limit,
                           priority=priority, text=text, overdue=overdue,
                           archived=archived, due_before=due_before,
                           parent=parent, blocked_by=blocked_by)

    view_markdown = _tasks_markdown

    def render_data(self, out) -> str:
        """Compact on purpose. A full dump of every task floods the context of whoever
        reads it — which is how a list becomes useless."""
        rows = out["tasks"]
        if not rows:
            return "(no tasks match)"
        head = f"{out['count']} task(s)"
        if out["shown"] != out["count"]:
            head += f" — showing {out['shown']}"
        lines = [head,
                 f"  {'id':6}  {'N':1}  {'status':7} {'pri':4} {'due':11} title",
                 "  " + "-" * 78]
        for r in rows:
            lines.append(
                f"  {r['id'] or '------'}  {'*' if r['now'] else ' '}  "
                f"{r['status'][:7]:7} {(r['priority'] or '-')[:4]:4} "
                f"{str(r['due'] or '-')[:10]:11} {r['title'][:52]}")
        lines.append("\n(act by id: finrail do tasks.update term=<id> status=done)")
        return "\n".join(lines)


class Show(Plugin):
    name = "tasks.show"
    verbatim = True   # the task itself
    description = "One task in full."
    read_only = True
    confirm = False
    aliases = {"id": "term"}

    def run(self, term: str = ""):
        from finrail.core import frontmatter as fm
        path = ops.resolve(term)
        meta, body = fm.read(path)
        tid = str(meta.get("id") or "")
        # Both directions: what this task stores (blocked_by, parent, discovered_from)
        # and what the others store about it (blocks, children, discoveries).
        links = {"blocked_by": [str(x) for x in ops.as_items(meta.get("blocked_by"))],
                 "parent": str(meta.get("parent") or ""),
                 "discovered_from": str(meta.get("discovered_from") or "")}
        links.update(ops.linked_to(tid) if tid else
                     {"blocks": [], "children": [], "discoveries": []})
        return {"meta": meta, "body": body, "path": str(path), "slug": path.stem,
                "links": links}

    def render_data(self, out) -> str:
        m = out["meta"]
        head = (f"{m.get('title')}  [{m.get('status')}]  id {m.get('id')}\n"
                f"  slug: {out['slug']}   due: {m.get('due') or '-'}   "
                f"assignee: {m.get('assignee') or '-'}   "
                f"tags: {', '.join(m.get('tags') or []) or '-'}")
        links = out.get("links") or {}
        shown = [(k, v) for k, v in (("blocked by", links.get("blocked_by")),
                                     ("blocks", links.get("blocks")),
                                     ("parent", links.get("parent")),
                                     ("children", links.get("children")),
                                     ("discovered from", links.get("discovered_from")),
                                     ("discoveries", links.get("discoveries"))) if v]
        if shown:
            head += "\n  " + "   ".join(
                f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in shown)
        return f"{head}\n\n{out['body']}"


class Ready(Plugin):
    """What can be started now. The question an agent asks at the top of a turn, and the
    thing that replaces every loop that waited for a blocker to clear: when a blocker
    closes its dependents appear here by themselves."""
    name = "tasks.ready"
    verbatim = True
    description = ("Tasks that can start now: todo, nobody's, no open blocker. "
                   "owner=<name> also shows that owner's unstarted tasks.")
    read_only = True
    confirm = False
    also_known_as = ("what can I do next", "what is ready", "unblocked work")

    def run(self, owner: str = "", user: str = "", limit: int = 0):
        # `user` is the authenticated caller a chat front end passes; the owner asked
        # about defaults to them, so `/get tasks.ready` means "ready for me". Inside an
        # agent's own turn there is no user and the agent is asking for ITS work, which
        # is what a handoff assigned to it by name is: FINRAIL_AGENT is the fallback.
        import os
        who = owner or user or os.environ.get("FINRAIL_AGENT", "") or ""
        return ops.ready(who, limit=int(limit or 0))

    view_markdown = _tasks_markdown

    def render_data(self, out) -> str:
        rows = out["tasks"]
        if not rows:
            return "(nothing is ready)"
        lines = [f"{out['count']} ready" + (f" for {out['owner']}" if out.get("owner") else "")]
        for r in rows:
            lines.append(f"  {r['id'] or '------'}  {(r['priority'] or '-')[:4]:4} "
                         f"{str(r['due'] or '-')[:10]:11} {r['title'][:52]}")
        lines.append("\n(take one: finrail do tasks.claim term=<id>)")
        return "\n".join(lines)


class Claim(Plugin):
    """Take a task, atomically. Two agents cannot both win it."""
    name = "tasks.claim"
    verified_by = "the note is re-read and names the claimant as its assignee"
    description = ("Take a ready task: owner set, status doing, only if nobody has it. "
                   "owner= defaults to the caller.")
    aliases = {"id": "term"}
    autonomous = True
    confirm = False
    reason = ("sets one named task's owner and status, and only when it is free and "
              "ready; a second claimant is refused, never overwritten")

    def plan(self, term: str = "", owner: str = "", user: str = ""):
        path = ops.resolve(term)
        # AN AGENT CLAIMS AS ITSELF. The handover tells the peer "tasks.claim term=<id>",
        # and both canaries were refused for a missing owner= and had to try again
        # (2026-09-21). A person's key still wins; the agent's own name is the fallback,
        # the same one tasks.ready uses.
        who = (owner or user or os.environ.get("FINRAIL_AGENT", "") or "").strip()
        if not who:
            raise ContractViolation("who is claiming? give owner=<name or key>")
        return {"term": term, "slug": path.stem, "owner": who}

    def render(self, p) -> str:
        return f"CLAIM {p['slug']} for {p['owner']}"

    def execute(self, p) -> dict:
        return ops.claim(p["term"], p["owner"])


class Release(Plugin):
    name = "tasks.release"
    verified_by = "the note is re-read and has no assignee"
    description = "Give a claimed task back: owner cleared, status todo. The holder's act."
    aliases = {"id": "term"}
    autonomous = True
    confirm = False
    reason = "clears one named task's owner, as that owner or by force; reversible by a claim"

    def plan(self, term: str = "", owner: str = "", user: str = "", force: bool = False):
        path = ops.resolve(term)
        who = (owner or user or os.environ.get("FINRAIL_AGENT", "") or "").strip()
        return {"term": term, "slug": path.stem, "owner": who, "force": bool(force)}

    def render(self, p) -> str:
        return f"RELEASE {p['slug']}" + (" (forced)" if p.get("force") else "")

    def execute(self, p) -> dict:
        return ops.release(p["term"], p["owner"], force=p.get("force", False))


class Gates(Plugin):
    name = "tasks.gates"
    verbatim = True
    description = "Open gates, each with what waits behind it. all=true includes closed ones."
    read_only = True
    confirm = False
    also_known_as = ("what is waiting on a person", "open gates", "what is gated")

    def run(self, all: bool = False):
        return ops.gates(open_only=not all)

    def render_data(self, out) -> str:
        rows = out.get("gates") or []
        if not rows:
            return "(no open gates)"
        lines = [f"{out['count']} gate(s)"]
        for g in rows:
            cond = {"human": "a person decides",
                    "timer": f"until {g.get('until')}",
                    "task": f"waits for {g.get('waits_for')}"}.get(g["gate"], "")
            state = ("open" if g["status"] not in ops.CLOSED else g.get("resolution") or g["status"])
            if g.get("satisfied") and g["status"] not in ops.CLOSED:
                state = "satisfied, run tasks.check"
            lines.append(f"  {g['id']}  {g['gate']:5} {state:12} {g['title'][:40]:40} "
                         f"{cond}" + (f"  blocks {', '.join(g['blocks'])}" if g["blocks"] else "")
                         + (f"  run {g['run']}" if g.get("run") else ""))
        lines.append("\n(decide one: finrail do tasks.resolve term=<id> decision=approve|reject)")
        return "\n".join(lines)


class Gate(Plugin):
    """A task that only blocks. human: a person decides. timer: opens at a time.
    task: opens when another task closes. Attached in front of the tasks it blocks."""
    name = "tasks.gate"
    verified_by = "the gate note exists and each blocked task names it in blocked_by"
    description = ("Create a gate in front of tasks: kind=human|timer|task, blocks=<ids>, "
                   "until=<ISO> for a timer, waits_for=<id> for a task gate.")
    autonomous = True
    confirm = False
    reason = ("creates a note that only blocks; it grants nothing, and a human gate can "
              "only ever be closed by a person through tasks.resolve")

    def plan(self, kind: str = "human", title: str = "", blocks: list = None,
             until: str = "", waits_for: str = "", parent: str = "", note: str = "",
             run: str = ""):
        if kind not in ops.GATE_KINDS:
            raise ContractViolation(f"kind must be one of {', '.join(ops.GATE_KINDS)}")
        blocks = [ops.task_id(str(b)) for b in ops.as_items(blocks)]
        if waits_for:
            waits_for = ops.task_id(waits_for)
        return {"kind": kind, "title": title, "blocks": blocks, "until": until,
                "waits_for": waits_for, "parent": parent, "note": note, "run": run}

    def render(self, p) -> str:
        cond = {"human": "a person decides", "timer": f"until {p.get('until')}",
                "task": f"waits for {p.get('waits_for')}"}[p["kind"]]
        return (f"GATE ({p['kind']}): {p.get('title') or cond}\n  {cond}"
                + (f"\n  blocks: {', '.join(p['blocks'])}" if p["blocks"] else ""))

    def execute(self, p) -> dict:
        return ops.gate(p.get("title", ""), p["kind"], blocks=p.get("blocks") or [],
                        until=p.get("until", ""), waits_for=p.get("waits_for", ""),
                        parent=p.get("parent", ""), note=p.get("note", ""),
                        run=p.get("run", ""))


class Resolve(Plugin):
    """A person's decision on a human gate. The one way such a gate closes."""
    name = "tasks.resolve"
    verified_by = "the gate note is re-read and names who resolved it and how"
    description = ("Decide a human gate as a person: decision=approve|reject. "
                   "by= defaults to the authenticated caller.")
    aliases = {"id": "term"}
    autonomous = True
    confirm = False
    reason = ("closes one named human gate on the say-so of the person named; asking "
              "for approval to record an approval is the same question twice, and "
              "resolved_by records which person it was")
    #: A decision about waiting work must not queue behind a turn.
    while_busy = "run"

    def plan(self, term: str = "", decision: str = "approve", by: str = "",
             user: str = "", note: str = ""):
        path = ops.resolve(term)
        who = (by or user or "").strip()
        if not who:
            raise ContractViolation(
                "no person on this decision; a human gate closes only as somebody")
        if decision not in ("approve", "reject"):
            raise ContractViolation("decision must be approve or reject")
        return {"term": term, "slug": path.stem, "decision": decision, "by": who,
                "note": note}

    def render(self, p) -> str:
        return f"{p['decision'].upper()} gate {p['slug']} as {p['by']}"

    def execute(self, p) -> dict:
        return ops.resolve_gate(p["term"], p["by"], p["decision"], p.get("note", ""))


class Check(Plugin):
    name = "tasks.check"
    verified_by = "every gate it opened is re-read as done with the reason logged"
    description = "Write down which timer and task gates have opened. ready already knows."
    autonomous = True
    confirm = False
    reason = "closes only gates whose own condition is met; a human gate is never touched"

    def plan(self):
        return {"at": __import__("finrail.core.frontmatter", fromlist=["now"]).now()}

    def render(self, p) -> str:
        return f"CHECK gates at {p['at']}"

    def execute(self, p) -> dict:
        return ops.check_gates()


class Handover(Plugin):
    """One task for a named peer, from explicit fields: what a handoff writes.

    A capability rather than a call into this plugin's internals, so another plugin
    reaches it through the ledger like anything else and the handover is a run of its
    own. Deterministic: every field is given, no model is asked, and the assignee is
    who the caller named.
    """
    name = "tasks.handover"
    verified_by = "the note exists, assigned to the peer, with the provenance line in its body"
    description = ("Create a task assigned to a peer, with provenance: title=, to=, "
                   "[body=] [from_task=<id>] [by=<agent>] [run=<id>] [priority=] [due=].")
    autonomous = True
    confirm = False
    reason = ("creates one task assigned to a named peer from explicit fields, linked to "
              "where it came from; the peer decides whether to claim it")

    def plan(self, title: str = "", to: str = "", body: str = "", from_task: str = "",
             by: str = "", run: str = "", priority: str = "", due: str = ""):
        ops.root()                                   # NotConfigured, with the fix, if none
        if not (title or "").strip():
            raise ContractViolation("a handover needs a title")
        if not (to or "").strip():
            raise ContractViolation("a handover needs to=<peer name>")
        return {"title": title.strip()[:90], "to": to.strip(), "body": (body or "").strip(),
                "from_task": ops.task_id(from_task) if from_task else "",
                "by": by, "run": run, "priority": priority, "due": due}

    def render(self, p) -> str:
        return (f"HANDOVER to {p['to']}: {p['title']}"
                + (f"\n  from: {p['from_task']}" if p["from_task"] else ""))

    def execute(self, p) -> dict:
        provenance = (f"Handed over"
                      + (f" by {p['by']}" if p.get("by") else "")
                      + f" to {p['to']}"
                      + (f", from task {p['from_task']}" if p.get("from_task") else "")
                      + (f", during run {p['run']}" if p.get("run") else "") + ".")
        description = (p["body"] + "\n\n" if p.get("body") else "") + provenance
        return ops.create(p["title"], description=description, assignee=p["to"],
                          discovered_from=p.get("from_task") or "",
                          priority=p.get("priority") or "", due=p.get("due") or "")


# ── planned writes ──────────────────────────────────────────────────────────
class Update(Plugin):
    """Change a task.

    `notes` and `note` are one character apart and do different things, deliberately:
    `## Notes` is what you currently know about the task and is meant to be rewritten,
    `## Log` is what happened to it and is append-only. Keeping them separate is what
    lets the second stay trustworthy — a history you can edit is not a history. The
    preview names which one is being touched, because a parameter that quietly did the
    other thing would be found out one lost note at a time.
    """
    name = "tasks.update"
    description = ("Change a task: status, priority, due, assignee, tags, refs, title, "
                   "links (add_blocked_by=, remove_blocked_by=, parent=, clear_parent=, "
                   "discovered_from=, each an id), and the notes section (notes= "
                   "replaces, add_notes= appends, clear_notes= removes; note= is a log "
                   "line instead).")
    aliases = {"id": "term"}
    autonomous = True
    confirm = False
    reason = "edits one task note you named; the change is shown and logged in the note"

    def plan(self, term: str = "", title: str = "", status: str = "",
             priority: str = "", due: str = "", assignee: str = "",
             now: bool = None, add_tags: list = None, remove_tags: list = None,
             add_refs: list = None, remove_refs: list = None,
             note: str = "", notes: str = "", add_notes: str = "",
             clear_notes: bool = False, add_blocked_by: list = None,
             remove_blocked_by: list = None, parent: str = "",
             clear_parent: bool = False, discovered_from: str = ""):
        path = ops.resolve(term)                  # resolve in code, never ask the model
        # Links are resolved here too, so the preview names ids and a link to a task
        # that does not exist is refused before anyone is asked to approve it.
        add_blocked_by = [ops.task_id(str(b)) for b in ops.as_items(add_blocked_by)]
        parent = ops.task_id(parent) if parent else ""
        discovered_from = ops.task_id(discovered_from) if discovered_from else ""
        # The CURRENT notes travel in the proposal, so the preview can show what is about
        # to be overwritten. `approve` runs in another process, so reading the file at
        # execute time to say "was: …" would describe a different moment than the one the
        # human agreed to.
        was = ops.current_notes(path) if (notes or clear_notes) else ""
        # The preview shows the notes as they will be WRITTEN — headings pushed inside
        # the Notes section (ops.contain_headings) — not as they were typed.
        notes, add_notes = ops.contain_headings(notes), ops.contain_headings(add_notes)
        return {"term": term, "path": str(path), "slug": path.stem,
                "title": title, "status": status, "priority": priority, "due": due,
                "assignee": assignee, "now": now, "add_tags": add_tags or [],
                "remove_tags": remove_tags or [], "add_refs": add_refs or [],
                "remove_refs": remove_refs or [],
                "note": note, "notes": notes, "add_notes": add_notes,
                "clear_notes": bool(clear_notes), "notes_was": was,
                "add_blocked_by": add_blocked_by,
                "remove_blocked_by": [str(b) for b in ops.as_items(remove_blocked_by)],
                "parent": parent, "clear_parent": bool(clear_parent),
                "discovered_from": discovered_from}

    def render(self, p) -> str:
        bits = [f"{k}={v!r}" for k, v in p.items()
                if k not in ("term", "path", "slug", "notes", "add_notes",
                             "clear_notes", "notes_was")
                and v not in (None, "", [], {})]
        # Notes are shown in full rather than repr'd into one long line: this is prose a
        # person is being asked to approve, and an overwrite says what it replaces.
        if p.get("clear_notes"):
            bits.append(f"notes: REMOVED ({len(p['notes_was'].splitlines())} line(s))")
        elif p.get("notes"):
            bits.append("notes: REPLACED"
                        + (f", was {len(p['notes_was'])} character(s)"
                           if p["notes_was"] else " (none before)")
                        + f"\n    {chr(10).join(p['notes'].splitlines())}")
        elif p.get("add_notes"):
            bits.append("notes: APPENDED\n    "
                        + chr(10).join(p["add_notes"].splitlines()))
        return f"UPDATE {p['slug']}\n  " + "\n  ".join(bits or ["(nothing)"])

    def execute(self, p) -> dict:
        return ops.update(p["term"], title=p["title"], status=p["status"],
                          priority=p["priority"], due=p["due"],
                          assignee=p["assignee"], now_flag=p["now"],
                          add_tags=p["add_tags"], remove_tags=p["remove_tags"],
                          add_refs=p["add_refs"],
                          remove_refs=p.get("remove_refs") or [], note=p["note"],
                          notes=p.get("notes", ""),
                          add_notes=p.get("add_notes", ""),
                          clear_notes=p.get("clear_notes", False),
                          add_blocked_by=p.get("add_blocked_by") or [],
                          remove_blocked_by=p.get("remove_blocked_by") or [],
                          parent=p.get("parent") or "",
                          clear_parent=p.get("clear_parent", False),
                          discovered_from=p.get("discovered_from") or "")


class Done(Plugin):
    name = "tasks.done"
    verified_by = "the page is re-read and its status is checked"
    description = "Mark a task done."
    aliases = {"id": "term"}
    autonomous = True
    confirm = False
    reason = "sets one named task's status; reversible by another update"

    def plan(self, term: str = ""):
        path = ops.resolve(term)
        from finrail.core import frontmatter as fm
        kind = fm.read(path)[0].get("gate")
        if kind:
            raise ContractViolation(
                f"{path.stem} is a {kind} gate; "
                + ("a person closes it with tasks.resolve" if kind == "human"
                   else "tasks.check closes it when its condition is met"))
        return {"term": term, "slug": path.stem}

    def render(self, p) -> str:
        return f"DONE {p['slug']}"

    def execute(self, p) -> dict:
        return ops.update(p["term"], status="done")


class SweepRunGates(Plugin):
    """Clean out the gate notes a conversation minted for its own pending runs.

    One per pending run, at the end of every turn, into a person's vault: 58 of 152 open
    notes were these before converse stopped (afc5694). A run's openness now comes from
    the ledger, so each of these is dead weight. This is the tasks plugin removing them
    because the vault is the tasks plugin's store and nobody else writes there
    (docs/LAYERS.md, rule 2): the fix for a cross-owner write is not a second one.
    """
    name = "tasks.gates.sweep"
    description = ("Archive (or delete) the gate notes converse minted for its own "
                   "pending runs, and list every one. [mode=archive|delete]")
    confirm = True
    takes = ("mode",)
    reason = ("moves or deletes notes out of a person's vault; a person sees the list "
              "and decides")
    verified_by = "none of the swept notes is still in the open folder"

    def plan(self, mode: str = "archive"):
        mode = (mode or "archive").strip().lower()
        if mode not in ("archive", "delete"):
            raise ContractViolation("mode=archive (move to archived/) or mode=delete")
        found = ops.run_gates()
        return {"mode": mode,
                "sweep": [g for g in found if not g["awaiting"]],
                "keep": [g for g in found if g["awaiting"]],
                "open_dir": str(ops.open_dir())}

    def render(self, p) -> str:
        verb = "ARCHIVE" if p["mode"] == "archive" else "DELETE"
        lines = [f"{verb} {len(p['sweep'])} run gate note(s) from {p['open_dir']}"]
        for g in p["sweep"]:
            lines.append(f"  {g['slug']}  run {g['run']} ({g['run_status']})")
        if p["keep"]:
            lines.append(f"keep {len(p['keep'])}, their runs still wait on a person:")
            lines += [f"  {g['slug']}  run {g['run']}" for g in p["keep"]]
        if not p["sweep"] and not p["keep"]:
            lines.append("  nothing to sweep")
        return "\n".join(lines)

    def execute(self, p) -> dict:
        return ops.sweep_run_gates(p["mode"])

    def verify(self, proposal: dict, out) -> tuple:
        out = out or {}
        if out.get("verified", False):
            return True, f"{out.get('count', 0)} note(s) gone from the open folder"
        return False, "a swept note is still in the open folder"

    def render_data(self, out) -> str:
        verb = "archived" if out["mode"] == "archive" else "deleted"
        lines = [f"{verb} {out['count']} run gate note(s):"]
        for r in out["removed"]:
            lines.append(f"  {r['slug']}  run {r['run']} ({r['run_status']})"
                         + (f"  -> {r['to']}" if r["to"] else ""))
        for k in out["kept"]:
            lines.append(f"  kept {k['slug']}: {k['why']}")
        return "\n".join(lines)


class Archive(Plugin):
    name = "tasks.archive"
    verified_by = "the destination exists and the source does not"
    description = "Move a task out of the open folder."
    aliases = {"id": "term"}
    autonomous = True
    confirm = False
    reason = "moves a note between folders in the same vault; nothing is destroyed"

    def plan(self, term: str = ""):
        path = ops.resolve(term)
        return {"term": term, "slug": path.stem, "from": str(path)}

    def render(self, p) -> str:
        return f"ARCHIVE {p['slug']}"

    def execute(self, p) -> dict:
        return ops.archive(p["term"])


# ── judged write ────────────────────────────────────────────────────────────
class Add(Plugin):
    """Creating a task is judged: the model turns "remind me to chase the lawyer" into a
    proper note.

    IT USED TO ASK, on the argument that a task appearing in the list without the human
    having seen it is a quiet side effect. What that missed is that it was the only verb
    here that asked. `tasks.update` rewrites a note, `tasks.done` closes one and
    `tasks.archive` moves one out of the folder — all unasked, and every one of them
    further from reversible than adding a page that was not there before. Asking only on
    the way in put the gate on the cheapest step of the set, and the cost was paid every
    time: a task dictated in passing became a pending run to go and approve, which is how
    a thing meant to catch a thought stops catching them.

    What the approval really bought was a look at the model's reading before it landed.
    That is still on the record — the run keeps the proposal it executed — and a title
    the model got wrong is one `tasks.update` away, which nobody is asked about either.
    """
    name = "tasks.add"
    description = "Create a task from plain language, or from explicit fields."
    autonomous = True
    confirm = False
    # Explicit fields, so a caller who already knows the answer can skip the model
    # entirely (see dispatch_core._judge). Judged when you describe it; deterministic
    # when you state it.
    takes = ("title", "description", "status", "priority", "due", "tags",
             "assignee", "refs", "now", "parent", "discovered_from", "blocked_by")
    # A reason for RUNNING, not for asking — `policy.audit` requires one of every
    # capability that writes without approval, and it is read by whoever wonders why
    # this one did not stop.
    reason = "writes one new note in your vault; nothing is overwritten, and tasks.archive undoes it"
    DOC = "tasks_add"        # rules/tasks_add.md — edit to take over from the default
    FALLBACK = """You turn a request into ONE task note for a personal task system.

Rules:
- title: imperative, specific, under 90 characters. No trailing period.
- status: todo unless the text says it is already in progress (doing) or waiting (parked).
- priority: only if the text justifies it. high = explicit urgency or a hard deadline.
- due: only an explicit date. Use ISO (YYYY-MM-DD). Never invent one.
- tags: 1-4 lowercase-hyphenated topic tags. No generic tags like "task" or "todo".
- assignee: only if a person is named as the owner.
- description: one or two sentences of context, or empty. Do not restate the title.
- refs: only wikilink names that appear in the request. Never invent one.
"""

    @property
    def system(self) -> str:
        from finrail.core import promptdoc
        return promptdoc.load(self.DOC, self.FALLBACK)
    schema = json.dumps({
        "type": "object",
        "required": ["title"],
        "properties": {
            "title": {"type": "string"}, "description": {"type": "string"},
            "status": {"type": "string"}, "priority": {"type": "string"},
            "due": {"type": "string"}, "tags": {"type": "array"},
            "assignee": {"type": "string"}, "refs": {"type": "array"},
            "now": {"type": "boolean"},
            # Links, by task id. The model is never asked to invent one: they arrive
            # from a caller who already holds the id (an agent handing work over), and
            # validate() refuses any that does not resolve.
            "parent": {"type": "string"}, "discovered_from": {"type": "string"},
            "blocked_by": {"type": "array"},
        },
    })

    def static_context(self) -> dict:
        return {"vocabulary": {"status": list(ops.STATUSES),
                               "priority": list(ops.PRIORITIES)}}

    def validate(self, p) -> list:
        bad = []
        if not (p.get("title") or "").strip():
            bad.append("no title")
        if p.get("status") and p["status"] not in ops.STATUSES:
            bad.append(f"status '{p['status']}' is not one of {', '.join(ops.STATUSES)}")
        if p.get("priority") and p["priority"] not in ops.PRIORITIES:
            bad.append(f"priority '{p['priority']}' is not one of "
                       f"{', '.join(ops.PRIORITIES)}")
        due = p.get("due") or ""
        if due and not __import__("re").fullmatch(r"\d{4}-\d{2}-\d{2}", str(due)):
            bad.append(f"due '{due}' is not an ISO date")
        for key in ("parent", "discovered_from"):
            if p.get(key):
                try:
                    ops.task_id(str(p[key]))
                except ContractViolation as e:
                    bad.append(f"{key}: {e}")
        for b in ops.as_items(p.get("blocked_by")):
            try:
                ops.task_id(str(b))
            except ContractViolation as e:
                bad.append(f"blocked_by: {e}")
        return bad

    def render(self, p) -> str:
        lines = [f"NEW TASK: {p.get('title')}"]
        for key in ("status", "priority", "due", "assignee"):
            if p.get(key):
                lines.append(f"  {key}: {p[key]}")
        if p.get("tags"):
            lines.append(f"  tags: {', '.join(p['tags'])}")
        for key in ("parent", "discovered_from"):
            if p.get(key):
                lines.append(f"  {key}: {p[key]}")
        if p.get("blocked_by"):
            lines.append(f"  blocked_by: {', '.join(str(b) for b in ops.as_items(p['blocked_by']))}")
        if p.get("description"):
            lines.append(f"\n  {p['description']}")
        return "\n".join(lines)

    def execute(self, p) -> dict:
        return ops.create(title=p["title"], description=p.get("description", ""),
                          status=p.get("status") or "todo",
                          priority=p.get("priority") or "",
                          due=p.get("due") or "", tags=p.get("tags") or [],
                          assignee=p.get("assignee") or "",
                          refs=p.get("refs") or [], now_flag=bool(p.get("now")),
                          parent=p.get("parent") or "",
                          discovered_from=p.get("discovered_from") or "",
                          blocked_by=p.get("blocked_by") or [])


COMMANDS = [List, Show, Ready, Claim, Release, Gates, Gate, Resolve, Check,
            Handover, Update, Done, Archive, SweepRunGates, Add]
