"""MARKDOWN — the vault's own dialect, rendered to self-contained HTML.

A task or a brain page IS markdown, and the panel showed it as escaped, pre-wrapped
text: `## Notes` as two hashes, a table as pipes, an embedded diagram as the literal
characters `![[flow.png]]`. Reading a page meant reading its source.

Scope is deliberately the subset a vault actually contains, not CommonMark. Core carries
no runtime dependencies — that is what lets finrail run on a machine with nothing but
Python — so this is stdlib, and it is small enough to reason about rather than large
enough to be right about everything.

Two rules it does not break:

  * **Self-contained.** Images are read off disk and inlined as `data:` URIs. Nothing in
    the produced page causes a fetch: a remote image is NOT downloaded, it is replaced by
    a placeholder naming it. A page that fetches is a page that leaks where and when you
    read, and one that renders differently offline.

  * **Markdown only where the body IS markdown.** Mail bodies are plain text, and running
    this over them would turn a line beginning "#1 priority" into a heading and a
    signature's `*` bullets into emphasis. The caller decides, from the record; this
    module never guesses.
"""
import base64
import html
import mimetypes
import pathlib
import re

#: Inlined images stop here. A page is something you glance at in a side panel; a 40MB
#: one is a spinner. The placeholder names the file, so a skipped image is visible as a
#: decision rather than as a gap.
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}


class Assets:
    """Resolves an embed against the vault and inlines it, once, within a budget.

    Obsidian resolves `![[flow.png]]` by NAME, anywhere in the vault, not by path. That
    is the behaviour a vault author expects, so it is the behaviour implemented here:
    look beside the note first, then search the vault root.
    """

    def __init__(self, note: pathlib.Path = None, root: pathlib.Path = None):
        self.note = pathlib.Path(note) if note else None
        self.root = pathlib.Path(root) if root else (self.note.parent if self.note
                                                     else None)
        self.spent = 0
        self._cache: dict = {}
        self._index: dict = None

    def _by_name(self, name: str):
        if self._index is None:
            self._index = {}
            if self.root and self.root.is_dir():
                for p in self.root.rglob("*"):
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
                        self._index.setdefault(p.name.lower(), p)
        return self._index.get(name.lower())

    def resolve(self, target: str):
        """A referenced file -> a real path, or None. Never leaves the vault."""
        target = (target or "").split("|")[0].strip()          # `![[img.png|300]]`
        if not target or target.startswith(("http://", "https://", "data:")):
            return None
        candidates = []
        if self.note:
            candidates.append((self.note.parent / target))
        if self.root:
            candidates.append(self.root / target)
        for cand in candidates:
            try:
                resolved = cand.resolve()
            except OSError:
                continue
            if resolved.is_file() and self._inside_root(resolved):
                return resolved
        return self._by_name(pathlib.Path(target).name)

    def _inside_root(self, path: pathlib.Path) -> bool:
        """No escaping the vault via `../../../etc/passwd`. A page is rendered from
        content someone else may have written into your vault (mail filed as notes, a
        shared repo), so the traversal guard is not theatre."""
        if not self.root:
            return True
        try:
            path.relative_to(self.root.resolve())
            return True
        except ValueError:
            return False

    def data_uri(self, path: pathlib.Path) -> str:
        """`data:` URI for an image, or "" when it is too big or unreadable."""
        key = str(path)
        if key in self._cache:
            return self._cache[key]
        uri = ""
        try:
            size = path.stat().st_size
            if size <= MAX_IMAGE_BYTES and self.spent + size <= MAX_TOTAL_IMAGE_BYTES:
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                uri = (f"data:{mime};base64,"
                       + base64.b64encode(path.read_bytes()).decode("ascii"))
                self.spent += size
        except OSError:
            uri = ""
        self._cache[key] = uri
        return uri


# ── inline ──────────────────────────────────────────────────────────────────
_CODE = re.compile(r"`([^`]+)`")
_EMBED = re.compile(r"!\[\[([^\]]+)\]\]")
_WIKI = re.compile(r"\[\[([^\]]+)\]\]")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^>]+)>")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_UNDER = re.compile(r"(?<![_\w])_([^_\n]+)_(?![_\w])")
_STRIKE = re.compile(r"~~([^~]+)~~")


def _img_tag(alt: str, target: str, assets: Assets) -> str:
    """An image, inlined — or a placeholder saying why not.

    A missing or remote image renders as a labelled box rather than a broken icon or
    silence: "the picture is not here" and "the picture would have to be fetched from
    someone's server" are different facts, and both matter more than a blank space.
    """
    label = html.escape(alt or pathlib.Path(target).name)
    if target.startswith(("http://", "https://")):
        return (f'<span class="ak-img-missing" title="not fetched">🌐 {label} '
                f'<em>(remote image, not fetched)</em></span>')
    path = assets.resolve(target)
    if path is None:
        return f'<span class="ak-img-missing">🖼 {label} <em>(not found)</em></span>'
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return f'<span class="ak-img-missing">📎 {html.escape(path.name)}</span>'
    uri = assets.data_uri(path)
    if not uri:
        return (f'<span class="ak-img-missing">🖼 {html.escape(path.name)} '
                f'<em>(too large to inline)</em></span>')
    return f'<img src="{uri}" alt="{label}" loading="lazy">'


def inline(text: str, assets: Assets) -> str:
    """One line of markdown -> HTML. Escaped FIRST, so nothing in the source becomes
    markup by accident; the constructs below are then put back deliberately."""
    holes: list = []

    def stash(fragment: str) -> str:
        holes.append(fragment)
        return f"\x00{len(holes) - 1}\x00"

    # code spans first: their contents are not markdown and must not be interpreted
    text = _CODE.sub(lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _EMBED.sub(lambda m: stash(_img_tag("", m.group(1), assets)), text)
    text = _IMAGE.sub(lambda m: stash(_img_tag(m.group(1), m.group(2), assets)), text)
    text = html.escape(text)
    text = _LINK.sub(lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">'
                               f"{m.group(1)}</a>", text)
    text = _AUTOLINK.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', text)
    # a wikilink points inside the vault, which the reader of a page is not browsing —
    # so it is shown as what it is, a named link, not as a dead href
    text = _WIKI.sub(lambda m: f'<span class="ak-wiki">{m.group(1).split("|")[-1]}</span>',
                     text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _UNDER.sub(r"<em>\1</em>", text)
    text = _STRIKE.sub(r"<del>\1</del>", text)
    for i, fragment in enumerate(holes):
        text = text.replace(f"\x00{i}\x00", fragment)
    return text


# ── blocks ──────────────────────────────────────────────────────────────────
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_ULI = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OLI = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_TASK = re.compile(r"^\[([ xX])\]\s+(.*)$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s*```\s*(\S*)")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _cells(line: str) -> list:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def render(body: str, note=None, root=None) -> str:
    """A markdown body -> an HTML fragment. Block structure by line, inline per line."""
    assets = Assets(note, root)
    lines = (body or "").replace("\r\n", "\n").split("\n")
    out, i = [], 0
    list_stack: list = []          # open <ul>/<ol> tags, innermost last
    # Open heading levels, outermost first. A heading OWNS what follows it until a
    # heading of the same or higher rank, and wrapping that span in a section is what
    # lets the page show it — an agenda item's paragraphs sitting inside the agenda item
    # rather than merely after it. Typography alone cannot carry this: a page whose
    # paragraphs open with **bold lead-ins** has runs of bold text that compete with the
    # sub-headings above them, and a reader cannot tell which owns which.
    section_stack: list = []

    def close_lists(to: int = 0):
        while len(list_stack) > to:
            out.append(f"</{list_stack.pop()}>")

    def close_sections(to_level: int):
        while section_stack and section_stack[-1] >= to_level:
            section_stack.pop()
            out.append("</section>")

    while i < len(lines):
        line = lines[i]

        fence = _FENCE.match(line)
        if fence:
            lang = fence.group(1).lower()
            i += 1
            block = []
            while i < len(lines) and not _FENCE.match(lines[i]):
                block.append(lines[i])
                i += 1
            i += 1
            close_lists()
            code = html.escape("\n".join(block))
            if lang:
                # A diagram's SOURCE, labelled as one. Drawing it needs a JavaScript
                # renderer, and a page that pulls one in from a CDN is neither
                # self-contained nor private; vendoring megabytes of it into a
                # dependency-free core buys one block type. Shown, not drawn, and
                # honestly labelled so nobody mistakes it for a failed render.
                label = ("flowchart · mermaid source" if lang == "mermaid" else lang)
                out.append(f'<div class="ak-code"><span class="ak-code-lang">'
                           f'{html.escape(label)}</span><pre><code>{code}</code></pre></div>')
            else:
                out.append(f"<pre><code>{code}</code></pre>")
            continue

        if not line.strip():
            close_lists()
            i += 1
            continue

        if _RULE.match(line):
            close_lists()
            out.append("<hr>")
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            close_lists()
            level = len(heading.group(1))
            close_sections(level)
            out.append(f'<section class="lvl{level}">')
            section_stack.append(level)
            out.append(f"<h{level}>{inline(heading.group(2), assets)}</h{level}>")
            i += 1
            continue

        # table: a header row followed by a |---|---| separator
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            close_lists()
            head = _cells(line)
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            head_html = "".join(f"<th>{inline(c, assets)}</th>" for c in head)
            body_html = "".join(
                "<tr>" + "".join(f"<td>{inline(c, assets)}</td>" for c in r) + "</tr>"
                for r in rows)
            out.append(f'<div class="ak-scroll"><table><thead><tr>{head_html}</tr>'
                       f"</thead><tbody>{body_html}</tbody></table></div>")
            continue

        if line.lstrip().startswith(">"):
            close_lists()
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(lines[i].lstrip()[1:].lstrip())
                i += 1
            out.append("<blockquote>"
                       + "".join(f"<p>{inline(q, assets)}</p>" for q in quote if q)
                       + "</blockquote>")
            continue

        item = _ULI.match(line) or _OLI.match(line)
        if item:
            ordered = _OLI.match(line) is not None
            depth = len(item.group(1).expandtabs(4)) // 2
            tag = "ol" if ordered else "ul"
            while len(list_stack) > depth + 1:
                out.append(f"</{list_stack.pop()}>")
            if len(list_stack) < depth + 1:
                out.append(f"<{tag}>")
                list_stack.append(tag)
            content = item.group(2)
            task = _TASK.match(content)
            if task:
                done = task.group(1).lower() == "x"
                box = ('<input type="checkbox" disabled'
                       + (" checked" if done else "") + ">")
                cls = ' class="ak-done"' if done else ""
                out.append(f"<li{cls}>{box} {inline(task.group(2), assets)}</li>")
            else:
                out.append(f"<li>{inline(content, assets)}</li>")
            i += 1
            continue

        # paragraph: consecutive non-blank lines that start nothing else
        close_lists()
        para = []
        while i < len(lines) and lines[i].strip() and not _HEADING.match(lines[i]) \
                and not _ULI.match(lines[i]) and not _OLI.match(lines[i]) \
                and not _FENCE.match(lines[i]) and not _RULE.match(lines[i]) \
                and not lines[i].lstrip().startswith(">"):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + "<br>".join(inline(p, assets) for p in para) + "</p>")

    close_lists()
    close_sections(0)
    return "\n".join(out)


# ── emphasis around a leading command ────────────────────────────────────────
#: A run of bold and italic markers directly before the slash: `**`, `_`, `***`, `**_`.
#: Bold and italic only — code, quotes, lists and headings around a slash are a person
#: quoting or showing something, not giving an order.
_OPENING = re.compile(r"^([*_]{1,3})/")


def _closes(line: str, i: int, marker: str) -> bool:
    """Whether `marker` at `line[i]` can close the emphasis it opened: it follows text, not
    space, is not part of a longer run, and an underscore is not inside a word — `fix_bug`
    is a name, not italics (CommonMark)."""
    if i == 0 or line[i - 1].isspace() or line[i - 1] in "*_":
        return False
    after = line[i + len(marker):i + len(marker) + 1]
    return not (after in ("*", "_") or (marker[-1] == "_" and after.isalnum()))


def unemphasise_command(text: str) -> str:
    """The message with bold or italic removed from around a command that begins it.

    `**/approve 81f7d47e**` is the command `/approve 81f7d47e`, typed in bold. The router
    decides what a message is by whether it begins with `/`, so the bold made the approval
    ordinary prose: it reached the agent, nothing ran, and nothing said so.

    Only the opening marker immediately before the `/`, and its partner on the same line.
    A marker with no partner is left as typed, as is everything on the lines below — this
    decides what the command is and never rewrites what the agent is sent. A message that
    does not open with an emphasised slash is returned unchanged.
    """
    out = (text or "").strip()
    m = _OPENING.match(out)
    if not m:
        return out
    opening = m.group(1)
    closing = opening[::-1]                         # `**_` closes as `_**`
    line, nl, rest = out.partition("\n")
    i = len(opening) + 1
    while True:
        i = line.find(closing, i)
        if i < 0 or _closes(line, i, closing):
            break
        i += 1
    if i < 0:
        return out                                  # unpaired: leave it as typed
    line = line[len(opening):i] + line[i + len(closing):]
    return (line.strip() + nl + rest).strip()
