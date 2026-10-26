"""ROUTING — plain language to a capability, with the model's answer checked.

Routing is a judgment, which is why it lives in a plugin rather than in core: it should be
improvable — plugins add to its prompt through `before_llm` — without touching the
machinery that executes things.

Nothing here knows about any particular capability. This module works from the catalogue,
from capabilities' own declared aliases, and from what plugins choose to inject; a domain
that wants to be found by a different word says so itself. That is what lets a new plugin
become routable the moment it is installed, with no edit here.

The critical part is `validate`: the model proposes a capability name and parameters, and
this checks them against the real catalogue and the real signatures. A hallucinated
capability or a made-up parameter dies here rather than becoming a confusing failure four
layers down.
"""
import json

from ...core import registry, schema
from ...core.errors import ContractViolation

CHAT = ("", "chat", "none", "null")

DOC = "ask"          # rules/ask.md — edit that file to take over from the default below

FALLBACK = """You route a request to exactly one capability of a local automation system.

Rules:
- Choose the single capability whose purpose matches the request.
- Fill only parameters that the capability actually declares. Never invent a parameter.
- Never invent identifiers, paths, names or addresses.
- If no capability fits (a general question, a chat, something not covered), return
  capability "chat".

A QUESTION ABOUT HOW TO DO SOMETHING IS A REQUEST TO DO IT
"How do I ...", "what is the command to ...", "X rejected my arguments, how do I make it
work": these are routing requests. Return the command that does the thing. Do not route
them to chat, and never answer with an explanation of syntax — the whole point is that the
caller gets a command they can run instead of learning the parameter rules.

Quoted errors, flags and parameter names inside a request are CONTEXT for what the caller
was trying to do. Read them that way; they are not the request.

SHOWING vs TELLING
Some capabilities are marked "can be rendered for reading". When the request is to LOOK at
something — show me, let me see, open, read, what's in my… — set "view": true and the
command becomes a rendered page instead of terminal text. When the request is a question
about the data rather than a request to see it — how many, is there one from X, when did —
leave it out and the plain read answers it.

Never set it for a capability that is not marked: it will be ignored, and the caller gets
text where they asked for a page.

RETURN THE COMMAND THAT DOES THE THING
The capability you choose must be the one that ACCOMPLISHES the request. If the request
is to delete something, route to the delete — never to a search that would merely help
someone find it. Handing back an exploratory command leaves the work undone and the
caller hunting, which is the exact outcome this system exists to prevent. Finding any
identifier you need is YOUR job, using a lookup.

LOOK BEFORE YOU DECIDE
If the request refers to something by a name you cannot map to a real value, do NOT drop
the parameter and do NOT guess it. Dropping it silently answers a different question than
the one asked, and the wrong answer looks exactly like a right one.

Instead return a `lookup`: the name of a read-only capability from the catalogue above
that would tell you, with its parameters. It will be run and you will be asked again with
the result. Then route properly, using the real value.

  {"lookup": {"capability": "<a read-only capability>",
              "reason": "what you need from it and why"}}

When you ask for a lookup, return ONLY the lookup — omit "capability" entirely; you are
not choosing a route yet. Every other reply must carry "capability".

Only read-only capabilities may be looked up — a lookup must never change anything. You
have a small budget of lookups, so look up what you actually need, not what might be
interesting. When you already know enough, route and do not look anything up."""


def system() -> str:
    """The routing instructions: `rules/ask.md` if it exists, else the default above.

    Read at call time, not import time — editing the file takes effect on the next run
    rather than the next restart, which is the whole point of it being a document.
    """
    from ...core import promptdoc
    return promptdoc.load(DOC, FALLBACK)

SCHEMA = json.dumps({
    "type": "object",
    # `capability` is NOT required, because a reply that asks to look something up
    # legitimately has no capability yet. Requiring it rejected the model's correct move
    # ("I need to see what exists first") as a malformed answer, so the one path built to
    # avoid guessing was the one path that could not be taken. A reply carrying neither a
    # capability nor a lookup is caught in the loop, where the model can be told why.
    "required": [],
    "properties": {
        "capability": {"type": "string"},
        "params": {"type": "object"},
        "reason": {"type": "string"},
        # Whether the ANSWER is something to look at. Presentation is a routing decision:
        # "show me my inbox" and "how many unread do I have" reach the same capability and
        # want different things back, and only the request says which.
        "view": {"type": "boolean"},
        # Routing in one shot is a guess whenever the right parameters depend on values
        # the router has not been told. Rather than guess, it may name a read-only
        # capability to run first and then decide again with the answer in hand.
        "lookup": {
            "type": "object",
            "properties": {"capability": {"type": "string"},
                           "params": {"type": "object"},
                           "reason": {"type": "string"}},
        },
    },
})

#: How many times routing may look something up before it must commit. Each round is a
#: real model call and a real read, so this is a budget, not a limit to be reached: two
#: lookups covers "what exists, and then what is inside the one I meant". Running
#: out is reported, never silently treated as an answer.
MAX_LOOKUPS = 2


class LookupRefused(Exception):
    """A lookup that is not a plain read. Never silently downgraded to a route."""


def run_lookup(name: str, params: dict) -> dict:
    """Run one READ-ONLY capability so routing can decide with facts instead of guesses.

    Read-only is the whole safety boundary, and it is enforced here against the
    capability's own declared rules rather than against a list kept in the router — a
    list would drift the moment someone added a capability. Because a lookup cannot
    change anything, iterating costs time and nothing else: there is no half-finished
    action to undo if routing then picks differently.
    """
    if not registry.has(name):
        raise LookupRefused(f"no capability '{name}'")
    cap = registry.command(name)
    if not getattr(cap, "read_only", False):
        raise LookupRefused(
            f"'{name}' is not read-only — routing may look things up, never act. "
            f"Route to it instead, so its own approval rules apply.")
    from ...core import dispatch_core
    rec = dispatch_core.dispatch(name, dict(params or {}), source="ask:lookup")
    if rec.get("status") != "done":
        raise LookupRefused(f"{name}: {rec.get('status')} — "
                            f"{'; '.join(rec.get('errors') or []) or 'no result'}")
    return rec.get("result")


def catalogue_block() -> str:
    """Every capability, its purpose and its REAL parameters — one source, so the router
    can never be told about a capability that does not exist or a parameter that is not
    accepted."""
    lines = []
    for row in registry.catalogue():
        if row["name"] == "ask":
            continue                       # the router does not route to itself
        cap = registry.command(row["name"])
        d = schema.describe(cap)
        params = ", ".join(
            f"{p['name']}:{p['kind']}" + ("" if p["required"] else "?")
            for p in d["params"]) or "(none)"
        entry = f"- {row['name']}: {row['description']}\n    params: {params}"
        # Each capability's own words for itself, so a request phrased in the caller's
        # vocabulary reaches it without the router holding a table of every domain.
        aka = tuple(getattr(cap, "also_known_as", ()) or ())
        if aka:
            entry += f"\n    also called: {', '.join(aka)}"
        if viewable(cap):
            entry += "\n    can be rendered for reading (set \"view\": true)"
        lines.append(entry)
    return "\n".join(lines)


def viewable(cap) -> bool:
    """Can this capability's output be rendered as a page to look at?

    `verbatim` already declares "this output IS the answer, not a report about it" — which
    is the same set of things someone means when they say *show me*. Reusing it keeps one
    declaration on the capability rather than a second list that has to be kept in step,
    and read-only is required because looking at something must never be a way to make
    something happen.
    """
    return bool(getattr(cap, "verbatim", False) and getattr(cap, "read_only", False))


def live_values_block() -> str:
    """What the capabilities say about the real values they can be reached with.

    The catalogue is a set of signatures; this is the state those signatures refer to.
    Identical contributions are collapsed — capabilities in one domain usually share a
    helper, and repeating the same list once per capability would crowd out the catalogue
    itself.
    """
    seen, blocks = set(), []
    for row in registry.catalogue():
        if row["name"] == "ask":
            continue
        try:
            value = registry.command(row["name"]).routing_context()
        except Exception:                                        # noqa: BLE001
            continue                       # a broken contributor must not break routing
        if value in (None, "", [], {}):
            continue
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if text in seen:
            continue
        seen.add(text)
        blocks.append(text)
    return "\n\n".join(blocks)


# ── the last-resort matcher ─────────────────────────────────────────────────
# The MODEL routes; this does not compete with it. Turning a request that names something
# informally into a command carrying that thing's REAL identifier means reading a
# parameter value out of prose, which string matching cannot do — and a capability chosen
# with its parameters dropped is not a cheaper answer, it is a wrong one that quietly
# reports on some default instead.
#
# What this exists for is the case where there is no model at all: expired login, no
# network. Routing is the front door, so "model call failed" and nothing else leaves the
# caller with no surface and no next move — which is precisely how one broken command
# becomes a session of guessing. A ranked list of REAL commands is a worse answer than
# the model's and a far better one than none.

# This module knows NOTHING about any particular capability. It works from the catalogue
# — names, purposes, declared parameters and each capability's own `aliases` — and from
# whatever plugins choose to inject. There is deliberately no vocabulary table here
# mapping informal words onto particular domains: the moment the router names one, it has
# to be edited every time a plugin is added, and it silently ranks the domains it happens
# to know about above the ones it does not. A plugin that wants to be found by another
# word declares it as an alias, which is the same one-declaration-site rule the rest of
# the system follows.
#
# Ordinary English stopwords are a language concern, not a domain one, so they do live
# here. Verbs are included: nearly every request contains one and nearly all of them mean
# "read", so they separate nothing. Leaving them in ranked any capability whose NAME
# contains a common verb above genuinely relevant ones, purely because that verb happened
# to be rare in the catalogue.
_NOISE = {"the", "a", "an", "my", "me", "i", "is", "are", "in", "on", "of", "to", "from",
          "for", "please", "can", "you", "do", "does", "any", "there", "s", "it", "that",
          "with", "and", "or", "at", "by", "about", "all",
          "show", "see", "view", "what", "which", "get", "display", "read", "list",
          "give", "tell", "find", "look", "want", "need", "have", "has"}


def _tokens(text: str) -> list:
    return [w for w in "".join(c if c.isalnum() else " "
                               for c in (text or "").lower()).split()
            if w not in _NOISE]


def _capability_tokens(name: str, description: str, params: list) -> tuple:
    """Name parts weigh more than prose, so a capability NAMED for the thing beats one
    that merely mentions it in passing."""
    name_toks = set(_tokens(name.replace(".", " ")))
    desc_toks = set(_tokens(description)) - name_toks
    param_toks = {p["name"].lower() for p in params}
    return name_toks, desc_toks, param_toks


def match(query: str, limit: int = 5) -> list:
    """Rank capabilities against a plain-language request. Pure string work: no model,
    no network, no auth — so this answers inside a locked session and when offline.

    Returns rows carrying the exact command to run, because a router that names a
    capability but leaves the caller to work out the invocation has not finished the job.
    """
    qset = set(_tokens(query))
    if not qset:
        return []

    # Build the catalogue once, then weight each token by how RARE it is across it. A word
    # shared by many capabilities narrows the field a little; one that belongs to a single
    # capability nearly settles it. Without this, matching
    # two common words beat matching one decisive one.
    entries, doc_freq = [], {}
    for row in registry.catalogue():
        name = row["name"]
        if name == "ask":
            continue                        # the router does not route to itself
        cap = registry.command(name)
        toks = _capability_tokens(name, row["description"], schema.describe(cap)["params"])
        entries.append((row, cap, toks))
        for tok in set().union(*toks):
            doc_freq[tok] = doc_freq.get(tok, 0) + 1

    total = max(len(entries), 1)

    def weight(tok: str) -> float:
        return total / (1.0 + doc_freq.get(tok, 0))

    rows = []
    for row, cap, (name_toks, desc_toks, param_toks) in entries:
        name = row["name"]
        score, why = 0.0, []
        # A capability named outright in the request is not really a guess.
        if name.lower() in (query or "").lower():
            score += 100
            why.append("named directly")
        hit_name = qset & name_toks
        if hit_name:
            score += 6 * sum(weight(t) for t in hit_name)
            why.append("name: " + ", ".join(sorted(hit_name)))
        hit_alias = qset & {a.lower() for a in (getattr(cap, "aliases", {}) or {})}
        if hit_alias:
            score += 4 * sum(weight(t) for t in hit_alias)
            why.append("alias: " + ", ".join(sorted(hit_alias)))
        hit_desc = qset & desc_toks
        if hit_desc:
            score += 1.5 * sum(weight(t) for t in hit_desc)
            why.append("purpose: " + ", ".join(sorted(hit_desc)))
        score += 0.5 * sum(weight(t) for t in qset & param_toks)

        if not score:
            continue
        # A read is the safer reading of an ambiguous request, so it wins ties. Nothing
        # here can pick a destructive capability over a harmless one on equal evidence.
        if getattr(cap, "read_only", False):
            score += 0.5
        rows.append({"name": name, "score": round(score, 2),
                     "description": row["description"],
                     "why": "; ".join(why),
                     "command": command_for(name, cap),
                     "read_only": bool(getattr(cap, "read_only", False))})
    rows.sort(key=lambda r: (-r["score"], r["name"]))
    return rows[:limit]


def invocation(name: str, params: dict, view: bool = False) -> str:
    """The exact, runnable command for a resolved route — real values, not placeholders.

    This is the router's actual product, so it has to be copy-pasteable as-is. Values are
    quoted only when they need it, because a line cluttered with quotes is one a person
    reads past instead of learning.
    """
    cap = registry.command(name)
    if view and viewable(cap):
        verb = "view"
    else:
        verb = "get" if getattr(cap, "read_only", False) else "do"
    parts = [f"finrail {verb} {name}"]
    for key, value in (params or {}).items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"        # not Python's True/False
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, default=str)
        else:
            text = str(value)
        parts.append(f"{key}={text}" if _bare(text) else f"{key}={json.dumps(text)}")
    return " ".join(parts)


def _bare(text: str) -> bool:
    """Safe to write without quotes in a shell."""
    return bool(text) and all(c.isalnum() or c in "-_.@:/+" for c in text)


def command_for(name: str, cap=None) -> str:
    """The exact invocation, with required parameters shown as placeholders."""
    cap = cap or registry.command(name)
    verb = "get" if getattr(cap, "read_only", False) else "do"
    d = schema.describe(cap)
    required = " ".join(f"{p['name']}=<{p['kind']}>" for p in d["params"] if p["required"])
    return f"finrail {verb} {name}" + (f" {required}" if required else "")


def validate(proposal: dict) -> list:
    """Block a hallucinated route before it becomes someone else's confusing error."""
    problems = []
    name = (proposal.get("capability") or "").strip()
    if name.lower() in CHAT:
        return []
    if not registry.has(name):
        return [f"no such capability '{name}' — routing must choose from the catalogue"]
    params = proposal.get("params") or {}
    if not isinstance(params, dict):
        return [f"params must be an object, got {type(params).__name__}"]
    cap = registry.command(name)
    try:
        schema.validate_params(cap, schema.coerce(cap, params))
    except ContractViolation as e:
        problems.append(str(e))
    return problems


def target(proposal: dict) -> tuple:
    """(capability_name or "", params) — "" means chat."""
    name = (proposal.get("capability") or "").strip()
    if name.lower() in CHAT:
        return "", {}
    return name, dict(proposal.get("params") or {})
