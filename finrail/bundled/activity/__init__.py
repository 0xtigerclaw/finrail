"""ACTIVITY — named kinds of work a session agent has instructions for.

Something like a skill in other harnesses: a message is recognised as belonging to an
activity, and the instructions written for that activity are given to the agent while it
is in effect. Bundled, beside the session agents it serves — `a coding agent`, `full_agent`,
`dispatcher`'s `chat` — because each of them asks it at every turn.

    where they live     `store.py` — <FINRAIL_STATE>/activities/<agent>/<name>.md
    what each turn gets `turn.py`  — stored per session, checked per turn
    what a person sees  `sessions.activity_line`, in core, one rendering for every view

Creating and editing an activity needs no approval: it writes a prompt document into the
agent's own folder and changes nothing an agent may DO — the scope around an agent is code,
and no instruction widens it. Deleting one asks, because it is the only one of these that
cannot be undone from here.
"""
from ...core import sessions
from ...core.errors import ContractViolation
from ...core.settings import SettingSpec
from ...plugin import Plugin
from . import jev, judge, store, turn

NAME = "activity"
DESCRIPTION = "Named kinds of work a session agent has instructions for, chosen per turn."

SETTINGS = [
    SettingSpec("activity.default", "str", required=False, default="none",
                choices=("auto", "none"),
                doc="what a session's activity is before anybody sets one. none is the "
                    "default: a session starts with no activity and no check, and an "
                    "operator who knows which kind of work a session is says so. auto "
                    "gives the decision to the per-turn check, which is a model call in "
                    "front of every turn of every session until somebody pins one. "
                    "Either way one session is still `/activity <name>`, `/activity "
                    "none` or `/activity auto`"),
    SettingSpec("activity.check_engine", "str", required=False, default="model",
                choices=turn.ENGINES,
                doc="who answers the per-turn check. model: the model, as it always was. "
                    "shadow: the model decides and Jev is asked too, both logged "
                    "(activity.checks compares them). jev: Jev decides when it is sure "
                    "enough, the model otherwise. Jev needs the secret "
                    f"{jev.SECRET}; one message and the activity names and descriptions "
                    "go to the decision model, nothing else"),
    SettingSpec("activity.jev_min_conf", "int", required=False, default=jev.MIN_CONF,
                doc="per cent. Below this Jev's pick is not acted on and the model is "
                    "asked. Measured on a pinned Jev version; re-measure before moving it"),
    SettingSpec("activity.judge", "str", required=False, default="off",
                choices=judge.MODES,
                doc="what happens to an activity's `checks` after a turn. off: nothing. "
                    "log: each check is put to Jev and the verdicts are logged. note: as "
                    "log, and a failed check it is sure of is said under the answer. "
                    "Sends the person's message and the agent's ANSWER to the decision model, "
                    "which is more than activity.check_engine sends"),
    SettingSpec("activity.judge_engine", "str", required=False, default="cascade",
                choices=judge.ENGINES,
                doc="who judges when activity.judge is on. cascade: Jev first, and the "
                    "model for the checks Jev is less than activity.jev_min_conf sure of "
                    "— measured level with the model alone, at a third of the model "
                    "calls. jev: Jev alone; what it is unsure of is logged, never said"),
]


def _session(sid: str) -> dict:
    if not sid:
        raise ContractViolation("name the session — session=<id> (finrail sessions)")
    return sessions.load(sid)


def _capability_of(rec: dict, capability: str = "") -> str:
    """Which agent a session belongs to. Given, or read from the registry by the
    session's kind — and refused when that is not one answer, rather than guessed."""
    if capability:
        return store.check_capability(capability)
    kind = rec.get("kind") or ""
    found = store.for_kind(kind)
    if len(found) == 1:
        return found[0]
    raise ContractViolation(
        f"session {rec.get('id')} is of kind {kind or '–'}, which "
        + (f"several agents share ({', '.join(found)}) — say capability=<which>"
           if found else "no session agent on this install has"))


def _render_listing(rows: list) -> str:
    out = []
    for r in rows:
        out.append(f"- **{r['name']}** — {r['description'] or '–'}")
    return "\n".join(out)


# ── reading ─────────────────────────────────────────────────────────────────

class Activities(Plugin):
    name = "activities"
    description = "The activities each session agent has, from the folders on disk."
    read_only = True
    confirm = False
    aliases = {"agent": "capability"}

    def run(self, capability: str = ""):
        if capability:
            store.check_capability(capability)
        agents = [capability] if capability else store.conversational()
        rows = [{"capability": a, "folder": str(store.folder(a)),
                 "activities": store.listing(a)} for a in agents]
        # A folder named for something that is not a session agent here is inert. It is
        # shown by its raw name so it can be noticed, not skipped as if it were not there.
        stray = [] if capability else [f for f in store.folders()
                                      if f not in store.conversational()]
        return {"root": str(store.root()), "agents": rows, "stray": stray}

    def render_data(self, out) -> str:
        lines = [f"activities under `{out['root']}`", ""]
        for a in out["agents"]:
            lines.append(f"**{a['capability']}**")
            lines.append(_render_listing(a["activities"]) if a["activities"] else "- –")
            lines.append("")
        if out.get("stray"):
            lines.append("folders that are not a session agent here (unused): "
                         + ", ".join(f"`{s}`" for s in out["stray"]))
        return "\n".join(lines).rstrip()


class Show(Plugin):
    name = "activity.show"
    description = "One activity in full: its description and the instructions it gives."
    read_only = True
    confirm = False
    aliases = {"agent": "capability"}

    def run(self, capability: str = "", name: str = ""):
        store.check_capability(capability)
        return store.load(capability, store.check_name(name))

    def render_data(self, out) -> str:
        checks = "".join(f"\n- check: {c}" for c in out.get("checks") or [])
        return (f"**{out['capability']} / {out['name']}** — {out['description'] or '–'}\n"
                f"`{out['path']}`{checks}\n\n{out['instructions'] or '–'}")


class Status(Plugin):
    name = "activity.status"
    description = "A session's activity now, whether it is pinned, and what each turn had."
    read_only = True
    confirm = False
    while_busy = "run"

    def run(self, session: str = "", turns: int = 10):
        rec = _session(session)
        cur = sessions.activity(rec)
        history = [{"at": t.get("at") or "", "activity": t["activity"]}
                   for t in (rec.get("turns") or []) if t.get("activity")]
        return {"session": rec["id"], "kind": rec.get("kind") or "",
                "current": {"name": cur.get("name") or "", "how": cur.get("how") or "",
                            "pinned": bool(cur.get("pinned"))},
                "turns": history[-int(turns or 10):]}

    def render_data(self, out) -> str:
        cur = out["current"]
        lines = [f"session {out['session']} — {sessions.activity_line(cur)}"
                 + (" — pinned" if cur["pinned"] else "")]
        for t in out["turns"]:
            lines.append(f"- {t['at'] or '–'}: {sessions.activity_line(t['activity'])}")
        if not out["turns"]:
            lines.append("- no turn has had an activity")
        return "\n".join(lines)


class Check(Plugin):
    name = "activity.check"
    description = ("Which activity a message would get, asked of the model — changes "
                   "nothing. For tuning descriptions.")
    read_only = True
    confirm = False
    aliases = {"agent": "capability", "message": "task"}

    def run(self, capability: str = "", task: str = "", current: str = ""):
        store.check_capability(capability)
        if not task:
            raise ContractViolation("give the message to check — task=<text>")
        return {"capability": capability, "task": task, "current": current,
                **turn.check(capability, task, current=current)}

    def render_data(self, out) -> str:
        named = out.get("named") or ""
        picked = out.get("activity") or ""
        line = f"{out['capability']}: {picked or '–'}"
        if named and named != picked:
            line += f" (the model named `{named}`, which is not an activity)"
        return line + (f" — {out['reason']}" if out.get("reason") else "")


class Checks(Plugin):
    name = "activity.checks"
    description = ("How Jev's answers compare with the model's on the checks logged so "
                   "far (activity.check_engine=shadow or jev) — changes nothing.")
    read_only = True
    confirm = False

    def run(self, capability: str = "", min_conf: str = ""):
        import json
        from ...core.settings import logs
        path = logs() / turn.CHECK_LOG
        rows = []
        if path.is_file():
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not capability or row.get("capability") == capability:
                    rows.append(row)
        try:
            bar = int(min_conf)
        except (TypeError, ValueError):
            bar = int(turn._setting("activity.jev_min_conf", jev.MIN_CONF))
        return {"log": str(path), **jev.score(rows, bar)}

    def render_data(self, out) -> str:
        return jev.render(out)


# ── writing ─────────────────────────────────────────────────────────────────

class Create(Plugin):
    name = "activity.create"
    description = "Write a new activity into an agent's folder. Available on its next turn."
    confirm = False
    autonomous = True
    reason = ("writes a prompt document into the agent's own activity folder; what any "
              "agent may do is its scope, which no instruction changes")
    aliases = {"agent": "capability", "task": "instructions", "text": "instructions"}

    def plan(self, capability: str = "", name: str = "", description: str = "",
             instructions: str = ""):
        store.check_capability(capability)
        name = store.check_name(name)
        if store.exists(capability, name):
            raise ContractViolation(f"{capability} already has {name!r} — "
                                    f"activity.edit changes it")
        if not (description or "").strip():
            raise ContractViolation("give it a one-line description — the check chooses "
                                    "by it")
        if not (instructions or "").strip():
            raise ContractViolation("give it instructions — instructions=<text>")
        return {"capability": capability, "name": name,
                "description": description.strip(), "instructions": instructions.strip(),
                "path": str(store.path(capability, name))}

    def render(self, p) -> str:
        return (f"CREATE activity {p['capability']} / {p['name']}\n"
                f"  description: {p['description']}\n  file: {p['path']}")

    def execute(self, p) -> dict:
        path = store.write(p["capability"], p["name"], p["description"], p["instructions"])
        return {"capability": p["capability"], "name": p["name"], "path": path}

    def render_data(self, out) -> str:
        return f"created **{out['capability']} / {out['name']}** at `{out['path']}`"


class Edit(Plugin):
    name = "activity.edit"
    description = "Change an activity's description or instructions. Takes effect next turn."
    confirm = False
    autonomous = True
    reason = Create.reason
    aliases = {"agent": "capability", "task": "instructions", "text": "instructions"}

    def plan(self, capability: str = "", name: str = "", description: str = "",
             instructions: str = ""):
        store.check_capability(capability)
        was = store.load(capability, store.check_name(name))
        if not (description or "").strip() and not (instructions or "").strip():
            raise ContractViolation("say what changes — description=<text> and/or "
                                    "instructions=<text>")
        return {"capability": capability, "name": was["name"], "path": was["path"],
                "description": (description or "").strip() or was["description"],
                "instructions": (instructions or "").strip() or was["instructions"],
                "changes": [k for k, v in (("description", description),
                                           ("instructions", instructions))
                            if (v or "").strip()]}

    def render(self, p) -> str:
        return (f"EDIT activity {p['capability']} / {p['name']}\n"
                f"  changes: {', '.join(p['changes'])}\n  file: {p['path']}")

    def execute(self, p) -> dict:
        path = store.write(p["capability"], p["name"], p["description"], p["instructions"])
        return {"capability": p["capability"], "name": p["name"], "path": path,
                "changes": p["changes"]}

    def render_data(self, out) -> str:
        return (f"edited **{out['capability']} / {out['name']}** "
                f"({', '.join(out['changes'])})")


class Delete(Plugin):
    name = "activity.delete"
    description = "Delete an activity's file. Sessions using it drop it on their next turn."
    confirm = True
    reason = ("removes the file, and nothing here restores it — sessions that had it "
              "carry on with none")
    aliases = {"agent": "capability"}

    def plan(self, capability: str = "", name: str = ""):
        store.check_capability(capability)
        was = store.load(capability, store.check_name(name))
        return {"capability": capability, "name": was["name"], "path": was["path"],
                "description": was["description"]}

    def render(self, p) -> str:
        return (f"DELETE activity {p['capability']} / {p['name']}\n"
                f"  {p['description'] or '–'}\n  file: {p['path']}\n"
                f"  sessions using it carry on with no activity from their next turn")

    def execute(self, p) -> dict:
        return {"capability": p["capability"], "name": p["name"],
                "path": store.remove(p["capability"], p["name"])}

    def render_data(self, out) -> str:
        return f"deleted **{out['capability']} / {out['name']}** (`{out['path']}`)"


# ── the session's activity ──────────────────────────────────────────────────

def set_for_session(rec: dict, name: str, capability: str = "", pin: bool = True,
                    by: str = "by hand") -> dict:
    """The one implementation behind `activity.set` and `converse.activity`."""
    agent = _capability_of(rec, capability)
    name = (name or "").strip()
    if not name:
        raise ContractViolation(
            f"say which — name=<activity>, none, or auto. {agent} has: "
            f"{', '.join(store.names(agent)) or 'none'}")
    if name == "auto":
        fields = {"pinned": False, "how": "unpinned — the check decides", "fresh": True}
    elif name == "none":
        fields = {"name": "", "pinned": bool(pin),
                  "how": f"turned off {by}" + (", pinned" if pin else ""), "fresh": True}
    else:
        store.check_name(name)
        if not store.exists(agent, name):
            store.load(agent, name)                                # raises with the list
        fields = {"name": name, "pinned": bool(pin),
                  "how": (f"set {by}, pinned" if pin else "chosen by the agent"),
                  "fresh": True}
    sessions.set_activity(rec["id"], **fields)
    return {"session": rec["id"], "capability": agent, "name": fields.get("name",
            sessions.activity(rec).get("name") or ""), **fields}


def _flag(value) -> bool:
    return str(value).strip().lower() not in ("false", "0", "no", "off")


class Set(Plugin):
    name = "activity.set"
    description = ("Set a session's activity: name=<activity> pins it, none turns it off, "
                   "auto hands it back to the per-turn check.")
    confirm = False
    autonomous = True
    while_busy = "run"
    reason = "changes which instructions the session's next turn is given, nothing else"
    aliases = {"agent": "capability", "activity": "name"}

    def plan(self, session: str = "", name: str = "", pin: str = "true",
             capability: str = "", user: str = ""):
        rec = _session(session)
        if not sessions.owns(rec, user):
            raise ContractViolation(f"session {session} belongs to someone else")
        agent = _capability_of(rec, capability)
        return {"session": rec["id"], "name": (name or "").strip(), "pin": _flag(pin),
                "capability": agent, "was": sessions.activity(rec).get("name") or ""}

    def render(self, p) -> str:
        return (f"ACTIVITY for session {p['session']} ({p['capability']})\n"
                f"  from: {p['was'] or '–'}\n  to:   {p['name'] or '–'}"
                + ("  (pinned)" if p["pin"] and p["name"] not in ("auto",) else ""))

    def execute(self, p) -> dict:
        rec = sessions.load(p["session"])
        return set_for_session(rec, p["name"], p["capability"], pin=p["pin"])

    def render_data(self, out) -> str:
        return (f"session {out['session']}: {sessions.activity_line(out)} — "
                f"from its next turn")


COMMANDS = [Activities, Show, Status, Check, Checks, Create, Edit, Delete, Set]
