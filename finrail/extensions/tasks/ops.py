"""Task plumbing — plain functions, no rules.

A task is one markdown note with YAML frontmatter, in the vault's tasks folder. That is
the whole storage model: no database, no index to fall out of date, greppable by hand and
editable in Obsidian while this system is running.
"""
import pathlib
import re

from finrail.core import frontmatter as fm
from finrail.core.errors import ContractViolation, NotConfigured
from finrail.core.registry import settings
from finrail.core.settings import home

STATUSES = ("todo", "doing", "parked", "done", "cancelled")
CLOSED = ("done", "cancelled")
PRIORITIES = ("low", "medium", "high")
_ORDER = {"todo": 0, "doing": 1, "parked": 2, "done": 3, "cancelled": 4}


def root() -> pathlib.Path:
    """The tasks folder. Defaults inside the vault, overridable by setting."""
    v = settings().get("tasks.dir")
    if v:
        return pathlib.Path(str(v)).expanduser()
    guess = home() / "knowledge-vault" / "tasks"
    if guess.is_dir():
        return guess
    raise NotConfigured("tasks.dir",
                        "finrail settings set tasks.dir=<path to your tasks folder>")


def open_dir() -> pathlib.Path:
    return root() / "open"


def archive_dir() -> pathlib.Path:
    return root() / "archived"


def notes() -> list:
    d = open_dir()
    return sorted(d.glob("*.md")) if d.is_dir() else []


def _record(path: pathlib.Path) -> dict:
    meta, body = fm.read(path)
    return {"id": meta.get("id") or "", "slug": path.stem,
            "title": meta.get("title") or path.stem,
            "status": meta.get("status") or "todo",
            "now": bool(meta.get("now")),
            "priority": meta.get("priority"),
            "assignee": meta.get("assignee"),
            "due": meta.get("due"), "tags": meta.get("tags") or [],
            "created": meta.get("created"), "updated": meta.get("updated"),
            # THE LINKS. Stored on the task they describe, by id, never by slug or
            # title: a title is edited, a slug is a filename, an id is the one thing that
            # survives both. `blocked_by` is what must close before this can start;
            # `parent` is the epic it belongs to; `discovered_from` is the task whose
            # work turned this one up (an agent that finds a code change is needed makes
            # a task that says so, and another agent picks it up). Absent means none.
            "blocked_by": [str(x) for x in as_items(meta.get("blocked_by"))],
            "parent": str(meta.get("parent") or ""),
            "discovered_from": str(meta.get("discovered_from") or ""),
            # A GATE is a task that only blocks. `gate` names its kind (human, timer,
            # task); `until` is when a timer opens; `waits_for` is the task a task-gate
            # follows; `resolved_by` and `resolution` are who closed a human gate and how.
            "gate": str(meta.get("gate") or ""),
            "until": str(meta.get("until") or ""),
            "waits_for": str(meta.get("waits_for") or ""),
            "resolved_by": str(meta.get("resolved_by") or ""),
            "resolution": str(meta.get("resolution") or ""),
            # The ledger run a gate stands in for, when it does: a pending proposal and
            # the decision on it are then one record, reachable from either side.
            "run": str(meta.get("run") or ""),
            "path": str(path), "wikilink": f"[[{path.stem}]]"}


def listing(status: str = "", now: bool = False, assignee: str = "",
            tag: str = "", limit: int = 0, priority: str = "", text: str = "",
            overdue: bool = False, archived: bool = False,
            due_before: str = "", parent: str = "", blocked_by: str = "") -> dict:
    source = (archive_dir().glob("*.md") if archived else notes())
    rows = [_record(p) for p in sorted(source)]
    # What still stands in front of each task, computed once for the whole list so a
    # reader (or the chat view) can mark a blocked row without a second pass.
    by_id = {r["id"]: r for r in rows if r["id"]}
    for r in rows:
        r["open_blockers"] = open_blockers(r, by_id)
    if status:
        want = [s.strip() for s in status.split(",")]
        rows = [r for r in rows if r["status"] in want]
    if now:
        rows = [r for r in rows if r["now"]]
    if assignee:
        rows = [r for r in rows if (r["assignee"] or "").lower() == assignee.lower()]
    if tag:
        rows = [r for r in rows if tag in (r["tags"] or [])]
    if priority:
        rows = [r for r in rows if (r["priority"] or "") == priority]
    if text:
        low = text.lower()
        rows = [r for r in rows if low in (r["title"] or "").lower()]
    if due_before:
        rows = [r for r in rows if r["due"] and str(r["due"]) <= due_before]
    if parent:
        pid = task_id(parent)
        rows = [r for r in rows if r["parent"] == pid]
    if blocked_by:
        bid = task_id(blocked_by)
        rows = [r for r in rows if bid in r["blocked_by"]]
    if overdue:
        import datetime
        today = datetime.date.today().isoformat()
        rows = [r for r in rows
                if r["due"] and str(r["due"]) < today and r["status"] not in CLOSED]
    rows.sort(key=lambda r: (_ORDER.get(r["status"], 9), not r["now"],
                             {"high": 0, "medium": 1, "low": 2}.get(r["priority"], 3),
                             r["due"] or "9999", r["title"].lower()))
    total = len(rows)
    if limit:
        rows = rows[:limit]
    return {"tasks": rows, "count": total, "shown": len(rows)}


def resolve(term: str) -> pathlib.Path:
    """id, slug or unique title fragment -> the note. Resolution happens in CODE; the
    model is never asked to supply a path."""
    term = (term or "").strip()
    if not term:
        raise ContractViolation("which task? (id, slug, or part of the title)")
    cands = notes()
    for p in cands:                                   # exact slug
        if p.stem == term:
            return p
    for p in cands:                                   # id
        meta, _ = fm.read(p)
        if str(meta.get("id") or "") == term:
            return p
    hits = [p for p in cands if term.lower() in p.stem.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        hits = [p for p in cands
                if term.lower() in (fm.read(p)[0].get("title") or "").lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ContractViolation(f"no task matches '{term}'")
    raise ContractViolation(
        f"'{term}' matches {len(hits)} tasks: "
        + ", ".join(p.stem for p in hits[:6]) + " — be more specific")


def new_id() -> str:
    import uuid
    return uuid.uuid4().hex[:6]


# ── links between tasks ──────────────────────────────────────────────
LINK_FIELDS = ("blocked_by", "parent", "discovered_from")


def task_id(term: str) -> str:
    """The id of the task `term` names, resolved in code, so a link can only ever point
    at a task that exists. A link written by title would follow the title into a rename
    and a link written by slug would follow the file; an id follows nothing."""
    path = resolve(term)
    meta, _ = fm.read(path)
    tid = str(meta.get("id") or "")
    if not tid:
        raise ContractViolation(
            f"{path.stem} has no id, so nothing can link to it; give it one with "
            f"`finrail do tasks.update term={path.stem} note=...` (any update assigns an id)")
    return tid


def _by_id() -> dict:
    """id -> record, for every open task."""
    out = {}
    for p in notes():
        r = _record(p)
        if r["id"]:
            out[r["id"]] = r
    return out


def linked_to(tid: str) -> dict:
    """The links that point AT a task: what it blocks, its children, what its work
    turned up. The reverse of what the task itself stores, read from the others."""
    rows = _by_id()
    return {"blocks": sorted(r["id"] for r in rows.values() if tid in r["blocked_by"]),
            "children": sorted(r["id"] for r in rows.values() if r["parent"] == tid),
            "discoveries": sorted(r["id"] for r in rows.values()
                                  if r["discovered_from"] == tid)}


def _would_cycle(tid: str, blockers: list) -> str:
    """The blocker that, followed through its own blockers, arrives back at `tid`.

    A blocks B blocks A is two tasks that can never be ready, and the ready query would
    simply never show either: the shape is refused where it is written instead.
    """
    rows = _by_id()
    seen, stack = set(), list(blockers)
    while stack:
        b = stack.pop()
        if b == tid:
            return b
        if b in seen:
            continue
        seen.add(b)
        stack.extend(rows.get(b, {}).get("blocked_by") or [])
    return ""


def _link_ids(tid: str, terms, what: str) -> list:
    """Terms -> ids, each a real task and never this one."""
    out = []
    for term in as_items(terms):
        other = task_id(str(term))
        if other == tid:
            raise ContractViolation(f"a task cannot be {what} itself")
        if other not in out:
            out.append(other)
    return out


def _as_ref(value: str) -> str:
    """A reference line: a wikilink for a vault page, a plain link for a URL.

    `[[…]]` means "a page in this vault", and wrapping a URL in it produces a link that
    resolves to nothing — `[[https://mail.google.com/…]]` is a dead wikilink, not a link
    to the mail. Every value was wrapped regardless, so a perfectly good Gmail permalink
    became unclickable and could only be repaired by editing the file by hand, which is
    exactly what a locked session cannot do.

    Already-bracketed values are left alone so re-adding one does not nest the brackets.
    """
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith("[[") and text.endswith("]]"):
        return text
    if text.startswith(("http://", "https://", "mailto:", "file://")):
        return f"<{text}>"                 # a link, kept intact and clickable
    return f"[[{text}]]"


# The body's sections, in the order they are kept. A task reads top-down: what it is,
# what you know about it, what it points at, what has happened to it. A new section is
# inserted at its place in this order rather than appended, so a note added today does
# not land under the log of everything that came before it.
SECTIONS = ("Notes", "References", "Log")


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^( {0,3})(#{1,6})(?=[ \t]|$)")

#: The shallowest heading anything WE write into a section may carry. `##` is what
#: starts a section, so anything shallower than `###` in text a caller supplied is a
#: section the caller never asked for.
INNER_LEVEL = 3


def _outside_fences(lines: list):
    """(line, is_code) for each line — a `## ` inside a fenced block is code, not a
    heading, and must neither cut a section nor be rewritten."""
    fence = ""
    for line in lines:
        m = _FENCE.match(line)
        if m and not fence:
            fence = m.group(1)[0] * 3
            yield line, True
        elif m and fence and m.group(1).startswith(fence):
            fence = ""
            yield line, True
        else:
            yield line, bool(fence)


def contain_headings(text: str) -> str:
    """Text a caller supplies for INSIDE a section, with its headings pushed down so none
    of them can start a section of its own.

    2026-09-26, task 18131d: an `add_notes` whose text held "## Shopping list" wrote that
    heading as a section of its own. The Notes section only runs to the next `## `, so
    the list was no longer in the notes at all — and the next log line, which puts the
    Log above any section it does not know, left it standing AFTER the Log. A later
    `notes=` with its own "## Shopping list" replaced only the paragraph under `## Notes`,
    appended a second Shopping list after the Log, and left the first — still listing a
    battery charger just split into its own task — where it was. Two lists, one wrong,
    fixed by hand.

    The whole text shifts together, so its shallowest heading lands at `###` and the
    headings under it keep their relative depth: a subsection of Notes, which a
    replace replaces and a clear removes with everything else. Idempotent — text already
    at `###` or deeper comes back unchanged.
    """
    lines = (text or "").splitlines()
    levels = [len(m.group(2)) for line, code in _outside_fences(lines)
              if not code and (m := _HEADING.match(line))]
    shift = max(0, INNER_LEVEL - min(levels)) if levels else 0
    if not shift:
        return text or ""
    out = []
    for line, code in _outside_fences(lines):
        m = None if code else _HEADING.match(line)
        if m:
            level = min(6, len(m.group(2)) + shift)
            line = f"{m.group(1)}{'#' * level}{line[m.end():]}"
        out.append(line)
    return "\n".join(out)


def stray_sections(body: str) -> list:
    """Sections that are none of Notes, References or Log.

    Before `contain_headings`, a heading in notes (or a description) became one of these,
    and every later log write moved it below the Log. They cannot be told apart from a
    section a person wrote by hand — the Log records that notes changed, never their text
    — so they are NAMED to whoever writes the notes next, never moved or removed.
    """
    known = {s.lower() for s in SECTIONS}
    return [name for name, _lines in split_sections(body)[1] if name.lower() not in known]


def split_sections(body: str) -> tuple:
    """(preamble, [(name, lines)]) — the body cut at its `## ` headings.

    The preamble is the title and description: everything before the first section. A
    `## ` inside a fenced code block is code and does not cut.
    """
    preamble, sections, current = [], [], None
    for line, code in _outside_fences((body or "").splitlines()):
        if line.startswith("## ") and not code:
            current = (line[3:].strip(), [])
            sections.append(current)
        elif current is None:
            preamble.append(line)
        else:
            current[1].append(line)
    return "\n".join(preamble), sections


def join_sections(preamble: str, sections: list) -> str:
    out = preamble.rstrip()
    for name, lines in sections:
        text = "\n".join(lines).strip("\n")
        out = f"{out.rstrip()}\n\n## {name}\n{text}".rstrip()
    return out + "\n"


def read_section(body: str, name: str) -> str:
    for section, lines in split_sections(body)[1]:
        if section.lower() == name.lower():
            return "\n".join(lines).strip("\n")
    return ""


def set_section(body: str, name: str, text: str) -> str:
    """Replace a section's contents, create it in its proper place, or drop it entirely.

    Empty text removes the section rather than leaving a heading with nothing under it:
    a `## Notes` followed by blank space reads as "there are notes" to everything that
    scans this file, including a person.
    """
    preamble, sections = split_sections(body)
    text = (text or "").strip("\n")
    kept = [(s, lines) for s, lines in sections if s.lower() != name.lower()]
    if not text:
        return join_sections(preamble, kept)
    block = (name, text.splitlines())
    order = {s.lower(): i for i, s in enumerate(SECTIONS)}
    mine = order.get(name.lower(), len(SECTIONS))
    for i, (s, _lines) in enumerate(kept):
        if order.get(s.lower(), len(SECTIONS)) > mine:
            return join_sections(preamble, kept[:i] + [block] + kept[i:])
    return join_sections(preamble, kept + [block])


def current_notes(path) -> str:
    """The Notes section as it stands on disk — so a preview can say what an overwrite
    is about to replace, rather than announcing the new text alone."""
    return read_section(fm.read(pathlib.Path(path))[1], "Notes")


def _ref_key(line: str) -> str:
    """The value inside a reference line, however it is written — for matching removals
    against what is actually on the page."""
    t = line.strip().lstrip("-").strip()
    for opener, closer in (("[[", "]]"), ("<", ">")):
        if t.startswith(opener) and t.endswith(closer):
            return t[len(opener):-len(closer)].strip()
    return t


def as_items(value) -> list:
    """A list of values, whatever shape the caller had.

    THE STRING TRAP. `refs` and `tags` are declared `list`, and every list-shaped
    parameter here is one `for` loop away from disaster if a string arrives instead:
    Python iterates it happily, one CHARACTER at a time. One task was written with 232
    references — `[[a]]`, `[[2]]`, `[[9]]`, `[[-]]` — one per character of the string it
    was handed, every one of them a dead link that offers to create a new page.

    The schema layer now coerces a comma-separated string on the way in, which fixes the
    path that caused it. This is the second line: the function that WRITES the links
    refuses to iterate a string, so no future caller — a new capability, a direct call,
    a provider — can reproduce it by bypassing that layer.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(as_items(item) if isinstance(item, str) else [item])
        return out
    return [value]


def create(title: str, description: str = "", status: str = "todo",
           priority: str = "", due: str = "", tags: list = None,
           assignee: str = "", refs: list = None, now_flag: bool = False,
           parent: str = "", discovered_from: str = "", blocked_by=None) -> dict:
    if not title:
        raise ContractViolation("a task needs a title")
    # Links are resolved BEFORE the id is minted and the file written, so a link to a
    # task that does not exist refuses the whole create rather than leaving a note with
    # a dangling edge.
    parent_id = task_id(parent) if parent else ""
    from_id = task_id(discovered_from) if discovered_from else ""
    blockers = [task_id(str(b)) for b in as_items(blocked_by)]
    if status not in STATUSES:
        raise ContractViolation(f"status must be one of {', '.join(STATUSES)}")
    if priority and priority not in PRIORITIES:
        raise ContractViolation(f"priority must be one of {', '.join(PRIORITIES)}")
    slug = fm.slugify(title)
    path = open_dir() / f"{slug}.md"
    n = 2
    while path.exists():
        path = open_dir() / f"{slug}-{n}.md"
        n += 1
    stamp = fm.now()
    meta = {"id": new_id(), "type": "task", "title": title, "status": status,
            "now": bool(now_flag), "created": stamp, "updated": stamp}
    if due:
        meta["due"] = due
    if tags:
        meta["tags"] = as_items(tags)
    if assignee:
        meta["assignee"] = assignee
    if priority:
        meta["priority"] = priority
    if parent_id:
        meta["parent"] = parent_id
    if from_id:
        meta["discovered_from"] = from_id
    if blockers:
        meta["blocked_by"] = blockers
    body = [f"# {title}", ""]
    if description.strip():
        # Under the title, not a section: a `## ` in it would be cut off as one.
        body += [contain_headings(description.strip()), ""]
    if refs:
        body += ["## References"] + [f"- {_as_ref(r)}" for r in as_items(refs)
                                     if _as_ref(r)] + [""]
    log = [f"- {stamp} created"]
    if from_id:
        log.append(f"- {stamp} discovered from {from_id}")
    if parent_id:
        log.append(f"- {stamp} parent {parent_id}")
    if blockers:
        log.append(f"- {stamp} blocked by {', '.join(blockers)}")
    body += ["## Log"] + log + [""]
    fm.write(path, meta, "\n".join(body))
    return {**_record(path), "created_now": True}


def _log(body: str, line: str) -> str:
    """Append one line to `## Log`, INSIDE that section.

    It used to append to the end of the BODY on the assumption that the log is last.
    `add_refs` breaks that assumption — it appended `## References` at the end, after the
    log — and every subsequent log line then landed inside the references, where a reader
    sees "- 2026-08-25T14:58:41 refs +2" listed as a reference to a page that does not
    exist. Writing to the section by name cannot drift that way.
    """
    existing = read_section(body, "Log")
    return set_section(body, "Log", f"{existing}\n- {line}".strip() if existing
                       else f"- {line}")


#: Set by resolve() around the one update that may close a human gate.
_resolving = False


def update(term: str, title: str = "", status: str = "", priority: str = "",
           due: str = "", assignee: str = "", now_flag=None,
           add_tags: list = None, remove_tags: list = None,
           add_refs: list = None, remove_refs: list = None,
           note: str = "", notes: str = "", add_notes: str = "",
           clear_notes: bool = False, add_blocked_by=None, remove_blocked_by=None,
           parent: str = "", clear_parent: bool = False,
           discovered_from: str = "") -> dict:
    """Change one task.

    NOTE vs NOTES, because one character apart with different effects is a trap worth
    naming rather than discovering:

      note=        appends ONE timestamped line to `## Log`. Append-only history — why
                   the status changed, what was tried. Nothing rewrites it.
      notes=       REPLACES the `## Notes` section. Working notes about the task itself,
                   meant to be edited as understanding changes.
      add_notes=   appends a paragraph to `## Notes`, keeping what is there.
      clear_notes= removes the section.
    """
    path = resolve(term)
    meta, body = fm.read(path)
    if not meta.get("created"):
        # Hand-made notes predate finrail and have no `created`. Emitting null forever
        # is a worse answer than the file's own age, which is real evidence.
        try:
            import datetime
            meta["created"] = datetime.datetime.fromtimestamp(
                path.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            pass
    stamp = fm.now()
    changes = []

    if title and title != meta.get("title"):
        # The title changes; the SLUG does not. Renaming the file would break every
        # [[wikilink]] pointing at it — the note keeps its identity.
        meta["title"] = title
        body = re.sub(r"^#\s+.*$", f"# {title}", body, count=1, flags=re.M) \
            if body.lstrip().startswith("#") else f"# {title}\n\n{body}"
        changes.append("title")
    if status:
        if status not in STATUSES:
            raise ContractViolation(f"status must be one of {', '.join(STATUSES)}")
        if meta.get("gate") == "human" and status in CLOSED and not _resolving:
            # The whole point of a human gate: it closes on a person's signed decision
            # and by no other path, so an agent editing its status is refused.
            raise ContractViolation(
                f"{path.stem} is a human gate; it closes through tasks.resolve, as a "
                f"person, not by setting its status")
        meta["status"] = status
        changes.append(f"status → {status}")
        if status in CLOSED:
            meta["completed"] = fm.today()
            # A finished task that keeps `now: true` goes on claiming the attention the
            # flag exists to direct — it stays in the now list after the work is done.
            if meta.get("now"):
                meta["now"] = False
                changes.append("now → False (completed)")
    if priority:
        if priority not in PRIORITIES:
            raise ContractViolation(f"priority must be one of {', '.join(PRIORITIES)}")
        meta["priority"] = priority
        changes.append(f"priority → {priority}")
    if due:
        meta["due"] = due
        changes.append(f"due → {due}")
    if assignee:
        meta["assignee"] = assignee
        changes.append(f"assignee → {assignee}")
    if now_flag is not None:
        meta["now"] = bool(now_flag)
        changes.append(f"now → {bool(now_flag)}")
    if add_tags or remove_tags:
        tags = list(meta.get("tags") or [])
        for t in add_tags or []:
            if t not in tags:
                tags.append(t)
        for t in remove_tags or []:
            if t in tags:
                tags.remove(t)
        meta["tags"] = tags
        changes.append("tags")
    if add_refs:
        add_refs = as_items(add_refs)
        links = "\n".join(f"- {_as_ref(r)}" for r in add_refs if _as_ref(r))
        # Through set_section, so the section lands in its place (above the log) rather
        # than at the end of whatever the body happens to be — which is how log lines
        # ended up filed as references.
        existing = read_section(body, "References")
        body = set_section(body, "References",
                           f"{existing}\n{links}".strip() if existing else links)
        changes.append(f"refs +{len(add_refs)}")
    if remove_refs:
        # The counterpart `remove_tags` always had. Without it a wrong reference could
        # only be taken out by editing the file, which a locked session cannot do — so
        # the only route left was the unconstrained agent, for a one-line change.
        #
        # Scoped to the References SECTION. It used to sweep every `- ` line in the body,
        # so a bullet in the log — or now in the notes — that happened to match was
        # deleted along with the reference, silently and with the same success message.
        wanted = {_ref_key(str(r)) for r in as_items(remove_refs)}
        kept, dropped = [], 0
        for line in read_section(body, "References").splitlines():
            if line.strip().startswith("- ") and _ref_key(line) in wanted:
                dropped += 1
                continue
            kept.append(line)
        if not dropped:
            raise ContractViolation(
                f"{path.stem} has no reference matching "
                f"{', '.join(sorted(wanted))!r} — nothing removed")
        body = set_section(body, "References", "\n".join(kept))
        changes.append(f"refs -{dropped}")
    tid = str(meta.get("id") or "")
    if (add_blocked_by or remove_blocked_by or parent or clear_parent
            or discovered_from) and not tid:
        # A note without an id cannot be linked to and cannot safely link: give it one
        # first, in the same write, so the links below are between ids.
        tid = new_id()
        meta["id"] = tid
        changes.append(f"id {tid}")
    if add_blocked_by:
        have = [str(x) for x in as_items(meta.get("blocked_by"))]
        new = [b for b in _link_ids(tid, add_blocked_by, "blocked by") if b not in have]
        loop = _would_cycle(tid, have + new)
        if loop:
            raise ContractViolation(
                f"{path.stem} cannot be blocked by {loop}: following its blockers leads "
                f"back to {path.stem}, and two tasks blocking each other can never be ready")
        if new:
            meta["blocked_by"] = have + new
            changes.append(f"blocked by +{', '.join(new)}")
    if remove_blocked_by:
        have = [str(x) for x in as_items(meta.get("blocked_by"))]
        drop = [str(x) for x in as_items(remove_blocked_by)]
        # By id or by any term that resolves; a blocker that is not there is refused,
        # for the same reason a missing reference is.
        drop_ids = []
        for d in drop:
            drop_ids.append(d if d in have else task_id(d))
        missing = [d for d in drop_ids if d not in have]
        if missing:
            raise ContractViolation(
                f"{path.stem} is not blocked by {', '.join(missing)}; nothing removed")
        kept = [b for b in have if b not in drop_ids]
        if kept:
            meta["blocked_by"] = kept
        else:
            meta.pop("blocked_by", None)
        changes.append(f"blocked by -{', '.join(drop_ids)}")
    if parent and clear_parent:
        raise ContractViolation("parent= and clear_parent= contradict; name one")
    if parent:
        pid = _link_ids(tid, [parent], "the parent of")[0]
        if _would_cycle(tid, [pid]) or _is_descendant(pid, tid):
            raise ContractViolation(
                f"{pid} is under {path.stem} already; a task cannot be its own ancestor")
        if meta.get("parent") != pid:
            meta["parent"] = pid
            changes.append(f"parent → {pid}")
    if clear_parent:
        if not meta.get("parent"):
            raise ContractViolation(f"{path.stem} has no parent to clear")
        meta.pop("parent", None)
        changes.append("parent cleared")
    if discovered_from:
        fid = _link_ids(tid, [discovered_from], "discovered from")[0]
        if meta.get("discovered_from") != fid:
            meta["discovered_from"] = fid
            changes.append(f"discovered from {fid}")
    if sum(1 for v in (notes, add_notes, clear_notes) if v) > 1:
        raise ContractViolation(
            "notes, add_notes and clear_notes do different things to the same section — "
            "name one: notes= replaces it, add_notes= appends to it, clear_notes=true "
            "removes it")
    if clear_notes:
        if not read_section(body, "Notes"):
            raise ContractViolation(f"{path.stem} has no notes to clear")
        body = set_section(body, "Notes", "")
        changes.append("notes cleared")
    elif notes:
        body = set_section(body, "Notes", contain_headings(notes))
        changes.append("notes → replaced")
    elif add_notes:
        existing = read_section(body, "Notes")
        add_notes = contain_headings(add_notes)
        body = set_section(body, "Notes",
                           f"{existing}\n\n{add_notes}".strip() if existing else add_notes)
        changes.append("notes → appended")
    # Named, not repaired: see stray_sections. The person writing the notes is the one
    # who can say whether "Shopping list" below the Log is part of them.
    strays = stray_sections(body) if (notes or add_notes or clear_notes) else []

    if note:
        body = _log(body, f"{stamp} {contain_headings(note)}")
        changes.append("note")

    if not changes:
        raise ContractViolation(f"nothing to change on {path.stem}")
    meta["updated"] = stamp
    for c in changes:
        if c not in ("note",):
            body = _log(body, f"{stamp} {c}")
    fm.write(path, meta, body)
    out = {**_record(path), "changed": changes}
    if strays:
        out["warning"] = (
            f"{path.stem} has section(s) outside its notes: "
            f"{', '.join(repr(s) for s in strays)}. Before 2026-09-26 a heading in notes "
            f"became a section of its own below the Log, where notes=, add_notes= and "
            f"clear_notes= do not reach. If it belongs to the notes, move it under "
            f"## Notes by hand ({path}); if it is stale, delete it there.")
    return out


def _is_descendant(candidate: str, ancestor: str) -> bool:
    """Is `candidate` somewhere under `ancestor` by parent links?"""
    rows = _by_id()
    seen, cur = set(), candidate
    while cur and cur not in seen:
        seen.add(cur)
        cur = rows.get(cur, {}).get("parent") or ""
        if cur == ancestor:
            return True
    return False


# ── ready and claim ──────────────────────────────────────────────────
def open_blockers(rec: dict, rows: dict = None) -> list:
    """The ids in `blocked_by` that are still open.

    A blocker that is not among the open notes is not blocking: a task leaves the open
    folder by being archived, which happens after it is done or cancelled. Treating an
    absent id as an eternal block would freeze every dependent of an archived task.
    """
    rows = _by_id() if rows is None else rows
    return [b for b in (rec.get("blocked_by") or [])
            if b in rows and _still_blocks(rows[b], rows)]


def _still_blocks(b: dict, rows: dict) -> bool:
    """Does this blocker still stand in the way?

    An ordinary task stops blocking when it closes, done or cancelled: cancelled is no
    longer in the way. A GATE stops blocking only when it is DONE, so a rejected human
    gate keeps its dependents where they are. A timer or task gate that is satisfied
    stops blocking the moment it is, whether or not `tasks.check` has written that down
    yet: ready reads the world, check records it.
    """
    if not b.get("gate"):
        return b["status"] not in CLOSED
    if b["status"] == "done":
        return False
    return not gate_satisfied(b, rows)


def gate_satisfied(g: dict, rows: dict = None) -> bool:
    """Whether a timer or task gate's condition has been met. A human gate is never
    satisfied by the world; only a person closes it."""
    kind = g.get("gate") or ""
    if kind == "timer":
        until = str(g.get("until") or "")
        return bool(until) and until <= fm.now()
    if kind == "task":
        rows = _by_id() if rows is None else rows
        target = rows.get(g.get("waits_for") or "")
        # The target has left the open folder (archived after done) or is closed.
        return target is None or target["status"] in CLOSED
    return False


def ready(owner: str = "", limit: int = 0) -> dict:
    """What can be started now: open, nobody's, nothing open in front of it.

    Beads' rule, because it is the right one: "open beads with no open blockers,
    excluding anything in progress, blocked, deferred". Here that is status todo (doing
    is in progress, parked is deferred), no assignee, and every blocker closed. With
    `owner`, tasks already assigned to that owner but not started count too: they are
    that owner's to start, and hiding them would make a claim look like it vanished.
    """
    rows = _by_id()
    out = []
    for r in rows.values():
        if r["gate"]:
            continue                      # a gate is a wait, not work to start
        if r["status"] != "todo":
            continue
        if r["assignee"] and not (owner and r["assignee"] == owner):
            continue
        if open_blockers(r, rows):
            continue
        out.append(r)
    out.sort(key=lambda r: (not r["now"],
                            {"high": 0, "medium": 1, "low": 2}.get(r["priority"], 3),
                            r["due"] or "9999", r["title"].lower()))
    total = len(out)
    if limit:
        out = out[:limit]
    return {"tasks": out, "count": total, "shown": len(out), "owner": owner}


def lock_path(path: pathlib.Path) -> pathlib.Path:
    """Where the claim lock for one note lives: under state, keyed on the note's path.

    It sat beside the note (`<task>.md.claim`), in the person's vault, where the
    repository plugin's commit swept it into the vault's history and its removal left the
    tree dirty (LAYERS-REVIEW finding 11). A lock is machine bookkeeping; the vault is
    the person's content. The key is the resolved path, so every process claiming the
    same note meets the same lock file."""
    import hashlib
    from finrail.core.settings import state
    key = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:16]
    d = state() / "locks" / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.claim"


def _lock(path: pathlib.Path):
    """One claim at a time per note, across processes.

    Two agents reading the same note in the same instant would both see it unowned and
    both write their name; whoever wrote last would hold a task the other believed was
    theirs. O_EXCL on a sidecar file is the one atomic primitive every filesystem here
    has, so the second arrival is refused rather than raced.
    """
    import contextlib, os

    @contextlib.contextmanager
    def held():
        lock = lock_path(path)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ContractViolation(
                f"{path.stem} is being claimed by someone else right now; try again")
        try:
            os.close(fd)
            yield
        finally:
            try:
                os.unlink(lock)
            except OSError:
                pass
    return held()


def claim(term: str, owner: str) -> dict:
    """Take a task: owner set, status doing, only if it is ready and nobody's.

    Idempotent for the owner who already has it, so a retried claim is not a refusal.
    Refused for anyone else with the holder's name, for a task that is not ready with
    the reason (closed, parked, in progress, or the blockers still open), never
    silently.
    """
    owner = (owner or "").strip()
    if not owner:
        raise ContractViolation("who is claiming? give owner=<name or key>")
    path = resolve(term)
    with _lock(path):
        meta, body = fm.read(path)
        rec = _record(path)
        holder = (meta.get("assignee") or "").strip()
        if holder == owner and meta.get("status") == "doing":
            return {**rec, "claimed": False, "already": True,
                    "note": f"{path.stem} is already yours"}
        if holder and holder != owner:
            raise ContractViolation(
                f"{path.stem} is {holder}'s; it is not free to claim")
        if meta.get("gate"):
            raise ContractViolation(
                f"{path.stem} is a {meta['gate']} gate, not work; "
                + ("a person closes it with tasks.resolve" if meta["gate"] == "human"
                   else "tasks.check closes it when its condition is met"))
        status = meta.get("status") or "todo"
        if status in CLOSED:
            raise ContractViolation(f"{path.stem} is {status}; nothing to claim")
        if status == "parked":
            raise ContractViolation(
                f"{path.stem} is parked; unpark it first "
                f"(finrail do tasks.update term={path.stem} status=todo)")
        if status == "doing" and not holder:
            # In progress with no owner is a note edited by hand. Taking it is fine;
            # say so in the log rather than pretending it was todo.
            pass
        blockers = open_blockers(rec)
        if blockers:
            raise ContractViolation(
                f"{path.stem} is not ready: blocked by {', '.join(blockers)}")
        stamp = fm.now()
        if not meta.get("id"):
            meta["id"] = new_id()
        meta["assignee"] = owner
        meta["status"] = "doing"
        meta["updated"] = stamp
        body = _log(body, f"{stamp} claimed by {owner}")
        fm.write(path, meta, body)
    return {**_record(path), "claimed": True, "already": False,
            "note": f"{path.stem} is now {owner}'s"}


# ── gates: tasks that only block ─────────────────────────────────────
GATE_KINDS = ("human", "timer", "task")


def gate(title: str, kind: str, blocks=None, until: str = "", waits_for: str = "",
         parent: str = "", note: str = "", run: str = "") -> dict:
    """Create a gate and attach it in front of the tasks it blocks.

    human   closes only through resolve(), by a person, with their name recorded.
    timer   opens at `until` (ISO); check() writes that down when it happens.
    task    opens when `waits_for` closes; check() writes that down.

    `blocks` are the tasks that wait on this gate: the gate's id is added to each of
    their blocked_by, so they leave `ready` until it opens. `run` ties a gate to the
    ledger run it stands in for (a pending proposal), for whoever resolves it.
    """
    if kind not in GATE_KINDS:
        raise ContractViolation(f"gate kind must be one of {', '.join(GATE_KINDS)}")
    if kind == "timer":
        if not until:
            raise ContractViolation("a timer gate needs until=<ISO date or datetime>")
        if len(until) == 10:
            until = until + "T00:00:00"
    if kind == "task":
        if not waits_for:
            raise ContractViolation("a task gate needs waits_for=<task id>")
        waits_for = task_id(waits_for)
    if not title:
        title = {"human": "Waiting for a decision", "timer": f"Waiting until {until}",
                 "task": f"Waiting for {waits_for}"}[kind]
    dependents = [task_id(str(b)) for b in as_items(blocks)]
    rec = create(title, description=note, status="todo", parent=parent)
    path = pathlib.Path(rec["path"])
    meta, body = fm.read(path)
    meta["gate"] = kind
    if until:
        meta["until"] = until
    if waits_for:
        meta["waits_for"] = waits_for
    if run:
        meta["run"] = run
    stamp = fm.now()
    body = _log(body, f"{stamp} gate: {kind}"
                + (f" until {until}" if until else "")
                + (f" waits for {waits_for}" if waits_for else "")
                + (f" for run {run}" if run else ""))
    fm.write(path, meta, body)
    gid = meta["id"]
    for d in dependents:
        update(d, add_blocked_by=[gid])
    return {**_record(path), "blocks": dependents, "created_now": True}


def resolve_gate(term: str, by: str, decision: str = "approve", note: str = "") -> dict:
    """Close a human gate, as a person. approve -> done (dependents become ready);
    reject -> cancelled (dependents stay where they are, and say why)."""
    global _resolving
    by = (by or "").strip()
    if not by:
        raise ContractViolation(
            "no person on this decision; a human gate closes only as somebody, "
            "give by=<name or key>")
    if decision not in ("approve", "reject"):
        raise ContractViolation("decision must be approve or reject")
    path = resolve(term)
    meta, body = fm.read(path)
    if meta.get("gate") != "human":
        raise ContractViolation(
            f"{path.stem} is not a human gate"
            + (f" (it is a {meta['gate']} gate; tasks.check closes it)" if meta.get("gate")
               else "; tasks.done closes a task"))
    if meta.get("status") in CLOSED:
        raise ContractViolation(
            f"{path.stem} was already {meta.get('resolution') or meta['status']}"
            + (f" by {meta['resolved_by']}" if meta.get("resolved_by") else ""))
    meta["resolved_by"] = by
    meta["resolution"] = "approved" if decision == "approve" else "rejected"
    fm.write(path, meta, body)
    _resolving = True
    try:
        out = update(path.stem, status="done" if decision == "approve" else "cancelled",
                     note=f"{meta['resolution']} by {by}" + (f": {note}" if note else ""))
    finally:
        _resolving = False
    result = {**out, "resolved": True, "by": by, "decision": meta["resolution"],
              "unblocked": linked_to(meta["id"])["blocks"] if decision == "approve" else []}
    run = str(meta.get("run") or "")
    if run:
        # THE RUN FOLLOWS THE GATE. A gate that stands in for a pending proposal is the
        # decision on that proposal: approving it approves the run, rejecting it drops
        # the run, as the same person. A run already settled from the other side (the
        # operator typed /approve, and converse resolved this gate afterwards) is left
        # alone, which is what makes the two directions safe to meet in the middle.
        result["run"] = settle_run(run, by, decision)
    return result


def settle_run(run: str, by: str, decision: str) -> dict:
    """Approve or drop the ledger run a gate names, if it is still waiting on a person."""
    from finrail.core import dispatch_core, tracker
    try:
        rec = tracker.load(run)
    except Exception as exc:                                     # noqa: BLE001
        return {"id": run, "settled": False, "note": f"no such run: {exc}"}
    if not tracker.awaiting(rec):
        return {"id": run, "settled": False, "status": rec.get("status"),
                "note": "already settled"}
    if decision == "approve":
        out = dispatch_core.approve_safely(run, actor=by)
        return {"id": run, "settled": bool(out.get("approved")),
                "status": out.get("status"), "note": out.get("note") or "approved"}
    try:
        tracker.drop(run)
        return {"id": run, "settled": True, "status": "dropped", "note": "dropped"}
    except Exception as exc:                                     # noqa: BLE001
        return {"id": run, "settled": False, "note": str(exc)}


def check_gates() -> dict:
    """Write down which timer and task gates have opened. `ready` already sees them as
    open; this makes the record say so too, and logs when and why."""
    rows = _by_id()
    opened = []
    for g in list(rows.values()):
        if not g["gate"] or g["gate"] == "human" or g["status"] in CLOSED:
            continue
        if gate_satisfied(g, rows):
            why = (f"timer reached {g['until']}" if g["gate"] == "timer"
                   else f"{g['waits_for']} closed")
            update(g["id"], status="done", note=f"opened: {why}")
            opened.append({"id": g["id"], "title": g["title"], "why": why,
                           "unblocked": linked_to(g["id"])["blocks"]})
    return {"opened": opened, "count": len(opened)}


def gates(open_only: bool = True) -> dict:
    """The gates, each with what waits behind it."""
    rows = _by_id()
    out = []
    for g in rows.values():
        if not g["gate"]:
            continue
        if open_only and (g["status"] in CLOSED):
            continue
        out.append({**g, "blocks": linked_to(g["id"])["blocks"],
                    "satisfied": gate_satisfied(g, rows)})
    out.sort(key=lambda g: (g["gate"] != "human", g["created"] or ""))
    return {"gates": out, "count": len(out)}


# ── the run gates converse used to mint ───────────────────────────────────
def run_gates() -> list:
    """The gate notes a conversation minted for its own pending runs, with the state of
    the run each stands in for.

    Converse used to write one `tasks.gate` note per pending run into the open folder,
    titled "Approve CAPABILITY (RUNID)", at the end of every turn; 58 of 152 open notes
    were these before it stopped (afc5694). A run's openness is now read from the
    ledger, so every one of these notes is dead weight in a person's vault. They are
    told apart by the marks converse left and nothing else leaves together: a human
    gate, a `run`, and a title that starts "Approve " and ends with that run in
    brackets. A gate a person made through `tasks.gate` that happens to name a run and
    say "Approve" in its title does not carry the run id at its end.
    """
    from finrail.core import tracker
    out = []
    for g in gates(open_only=False)["gates"]:
        title = g["title"]
        if (g["gate"] != "human" or not g["run"] or not title.startswith("Approve ")
                or not title.endswith(f"({g['run']})")):
            continue
        try:
            rec = tracker.load(g["run"])
            run_status, awaiting = rec.get("status") or "?", tracker.awaiting(rec)
        except Exception:                                        # noqa: BLE001
            run_status, awaiting = "gone", False
        out.append({"id": g["id"], "slug": g["slug"], "title": g["title"],
                    "run": g["run"], "run_status": run_status, "awaiting": awaiting,
                    "note_status": g["status"], "blocks": g["blocks"], "path": g["path"]})
    return out


def sweep_run_gates(mode: str = "archive") -> dict:
    """Move (or delete) every run gate whose run is no longer waiting on a person, and
    say exactly what went where. A gate whose run still waits is left and listed.

    `archive` moves the note to the archived folder, which destroys nothing; `delete`
    removes the file, for a vault whose owner wants the machine notes gone rather than
    filed. Either way a task that named one of these gates in `blocked_by` is unlinked
    first, so nothing stays blocked on a note that is gone.
    """
    if mode not in ("archive", "delete"):
        raise ContractViolation("mode must be archive or delete")
    removed, kept = [], []
    for g in run_gates():
        if g["awaiting"]:
            kept.append({**g, "why": "its run still waits on a person"})
            continue
        for dep in g["blocks"]:
            try:
                update(dep, remove_blocked_by=[g["id"]])
            except Exception as exc:                             # noqa: BLE001
                kept.append({**g, "why": f"could not unlink {dep}: {exc}"})
                break
        else:
            path = pathlib.Path(g["path"])
            if mode == "archive":
                dest = archive_dir() / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                from finrail.core import guard
                with guard.writing(dest):
                    path.replace(dest)
                where = str(dest)
            else:
                path.unlink()
                where = ""
            removed.append({**g, "to": where})
    return {"mode": mode, "removed": removed, "kept": kept,
            "count": len(removed), "verified": not any(
                pathlib.Path(r["path"]).exists() for r in removed)}


def release(term: str, owner: str = "", force: bool = False) -> dict:
    """Give a task back: owner cleared, status todo. The holder's act, or forced."""
    path = resolve(term)
    with _lock(path):
        meta, body = fm.read(path)
        holder = (meta.get("assignee") or "").strip()
        if not holder:
            raise ContractViolation(f"{path.stem} is nobody's; nothing to release")
        if holder != (owner or "").strip() and not force:
            raise ContractViolation(
                f"{path.stem} is {holder}'s; release it as {holder}, or force=true")
        stamp = fm.now()
        meta.pop("assignee", None)
        if meta.get("status") == "doing":
            meta["status"] = "todo"
        meta["updated"] = stamp
        body = _log(body, f"{stamp} released by {owner or 'force'} (was {holder}'s)")
        fm.write(path, meta, body)
    return {**_record(path), "released": True, "was": holder,
            "note": f"{path.stem} is free again"}


def archive(term: str) -> dict:
    path = resolve(term)
    dest = archive_dir() / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    from finrail.core import guard
    with guard.writing(dest):
        path.replace(dest)
    return {"slug": dest.stem, "archived_to": str(dest),
            "verified": dest.is_file() and not path.exists()}
