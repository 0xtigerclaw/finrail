"""VIEW — a read capability's output, in the form the front end in use can receive.

`finrail view tasks.list` renders a read for LOOKING at. What that means
depends on who is looking, which is the one thing this module cannot infer and the
`view.front_end` setting states:

  claude-desktop  a themed, self-contained HTML page written to disk, its path printed,
                  and the call that opens it in the side panel. The panel is the front
                  end's, the CONTENT is finrail's.
  chat            markdown, returned inline. A remote chat cannot open a path on THIS
                  machine, so a page on disk is a view nobody can reach — which is what
                  every view was for anyone not sitting at the desktop app.

The setting decides delivery and content together, because they are one decision: an
HTML table and a markdown table are the same rows differently drawn, and a capability
that already writes markdown for a person (`view_markdown`) has nothing to gain from
being tabulated a second time.

This is the piece the rewrite dropped. v2 kept structured data and a per-capability text
renderer and assumed a terminal, so a table that lines up in a terminal arrived in chat as
a jumble — markdown collapses runs of spaces. Marking the output verbatim made it faithful
and still ugly. The answer was never a better ASCII table: it is to render HTML and hand
over a path.

Presentation is a CAPABILITY, deliberately, not something a caller hand-rolls with shell
redirection. That is what keeps it available inside a locked session: writing files is
denied there, running `finrail` is not. Nothing about the lock has to be loosened for the
panel to work — which is exactly why it worked before.
"""
import datetime
import html
import logging
import pathlib
import shutil
import subprocess
import sys
import tempfile

from . import markdown as md

_logger = logging.getLogger(__name__)


#: The front ends a view knows how to be received by. Not a taste setting: it is a fact
#: about where the output of this process ends up, and getting it wrong makes every view
#: unreachable rather than merely ugly.
CLAUDE_DESKTOP = "claude-desktop"      # a panel on this machine, so a path is enough
CHAT = "chat"                          # a remote chat that renders markdown, nothing more
FRONT_ENDS = (CLAUDE_DESKTOP, CHAT)


def front_end() -> str:
    """Which front end this install renders for.

    Defaults to the desktop app rather than to nothing: an unset setting has to mean the
    behaviour that existed before there was a setting, or upgrading changes what every
    view does without anyone asking for it.
    """
    try:
        from .registry import settings as _settings
        return str(_settings().get("view.front_end") or CLAUDE_DESKTOP)
    except Exception:                                            # noqa: BLE001
        # Settings unreadable (a bad hand edit, a half-built install). A view that still
        # renders the old way beats one that raises on the way to being looked at.
        return CLAUDE_DESKTOP


# columns we know how to order nicely; anything else is appended in encounter order
_PREFERRED = ["n", "sid", "id", "hash", "status", "priority", "now", "due", "assignee", "date",
              "authors", "from", "subject", "title", "attachments", "tags"]
_SKIP = {"path", "wikilink", "query_id", "thread", "body_html", "_inline",
         "matched", "total"}


def _rows(data) -> list:
    """Find the first list-of-dicts in an op result (tasks, results, events, messages…)."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("tasks", "results", "events", "messages", "hits", "items"):
            v = data.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        return [data]                                   # single record → one row
    return []


def _columns(rows: list) -> list:
    skip = set(_SKIP)
    if rows and "sid" in rows[0]:
        skip |= {"id", "email"}               # keep n + sid; hide the raw Message-ID
    seen = []
    for r in rows:
        for k in r:
            if k not in seen and k not in skip:
                seen.append(k)
    return ([c for c in _PREFERRED if c in seen] +
            [c for c in seen if c not in _PREFERRED])


def _cell(val) -> str:
    if val is None or val == "":
        return '<span class="none">—</span>'
    if isinstance(val, bool):
        return "⚡" if val else '<span class="none">—</span>'
    if isinstance(val, list):
        return ('<div class="tags">' +
                "".join(f'<span class="tag">{html.escape(str(t))}</span>' for t in val[:6])
                + "</div>")
    s = html.escape(str(val))
    low = str(val).lower()
    for cls, words in (("p-high", ("high", "todo", "overdue")),
                       ("p-med", ("medium", "doing", "pending")),
                       ("p-done", ("done", "sent", "closed")),
                       ("p-low", ("low", "parked", "cancelled"))):
        if low in words:
            return f'<span class="pill {cls}">{s}</span>'
    return s


_CSS = """
:root{--bg:#fff;--fg:#16181d;--muted:#6b7280;--line:#e5e7eb;--head:#f7f8fa;--zebra:#fbfbfc;
--chip:#f1f3f5;--chip-fg:#414852;--high:#b42318;--high-bg:#fef3f2;--med:#b54708;
--med-bg:#fffaeb;--low:#3538cd;--low-bg:#eef4ff;--done:#067647;--done-bg:#ecfdf3;--accent:#2f5d99}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#16181d;--fg:#e6e8eb;--muted:#9aa1ab;
--line:#2b2f36;--head:#1c1f25;--zebra:#191c21;--chip:#23272e;--chip-fg:#b6bdc7;
--high:#ff9d95;--high-bg:#3a1a17;--med:#f5be6a;--med-bg:#382512;--low:#a9b4ff;
--low-bg:#1f2340;--done:#6fe0a6;--done-bg:#10301f;--accent:#8fb6e8}}
:root[data-theme=light]{--bg:#fff;--fg:#16181d;--muted:#6b7280;--line:#e5e7eb;
--head:#f7f8fa;--zebra:#fbfbfc;--chip:#f1f3f5;--chip-fg:#414852}
:root[data-theme=dark]{--bg:#16181d;--fg:#e6e8eb;--muted:#9aa1ab;--line:#2b2f36;
--head:#1c1f25;--zebra:#191c21;--chip:#23272e;--chip-fg:#b6bdc7}
*{box-sizing:border-box}
body,.ak{margin:0;background:var(--bg);color:var(--fg);padding:26px 22px 36px;
font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
h1,.ak h1{font-size:19px;font-weight:620;margin:0 0 4px;letter-spacing:-.01em}
.sub,.ak .sub{color:var(--muted);font-size:13.5px;margin:0 0 18px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%}
th{text-align:left;font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;
color:var(--muted);background:var(--head);padding:10px 13px;border-bottom:1px solid var(--line);
white-space:nowrap}
td{padding:11px 13px;border-bottom:1px solid var(--line);vertical-align:top;max-width:520px}
tbody tr:nth-child(even){background:var(--zebra)}
tbody tr:last-child td{border-bottom:none}
td:first-child{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;
color:var(--muted);white-space:nowrap}
.tags{display:flex;flex-wrap:wrap;gap:4px}
.tag{background:var(--chip);color:var(--chip-fg);font-size:11px;padding:2px 7px;border-radius:4px}
.pill{display:inline-block;font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:999px}
.p-high{color:var(--high);background:var(--high-bg)}.p-med{color:var(--med);background:var(--med-bg)}
.p-low{color:var(--low);background:var(--low-bg)}.p-done{color:var(--done);background:var(--done-bg)}
.none{color:var(--muted)}
footer{margin-top:14px;color:var(--muted);font-size:12.5px}
"""


_DOC_CSS = """
.msg{border:1px solid var(--line);border-radius:10px;margin-bottom:18px;overflow:hidden}
.msg>h2{font-size:15.5px;font-weight:620;margin:0;padding:13px 16px;background:var(--head);
border-bottom:1px solid var(--line)}
.meta{padding:11px 16px;border-bottom:1px solid var(--line);font-size:13px;
display:grid;grid-template-columns:auto 1fr;gap:3px 12px}
.meta dt{color:var(--muted);white-space:nowrap}
.meta dd{margin:0;overflow-wrap:anywhere}
.body{padding:16px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.62}
.quote{color:var(--muted)}
.att{padding:10px 16px;border-bottom:1px solid var(--line)}
.page{padding:6px 20px 18px;font-size:14.5px;line-height:1.65}
.page h1,.page h2,.page h3,.page h4,.page h5,.page h6{background:none;padding:0;
border:0;line-height:1.3}
.page h1{font-size:21px;font-weight:660;margin:20px 0 9px;letter-spacing:-.01em}
.page h2{font-size:17px;font-weight:640;margin:22px 0 9px;padding-bottom:5px;
border-bottom:1px solid var(--line)}
/* A page whose paragraphs open with **bold lead-ins** puts runs of bold text right under
   the sub-heading they belong to. Size and weight alone cannot separate them — h3 was
   LIGHTER than the bold beneath it — so h3 and h5 carry the accent colour, which nothing
   in body text uses. */
.page h3{font-size:15.5px;font-weight:700;margin:20px 0 7px;color:var(--accent)}
.page h4{font-size:13px;font-weight:700;margin:16px 0 5px;text-transform:uppercase;
letter-spacing:.05em}
.page h5{font-size:12.5px;font-weight:680;margin:14px 0 4px;text-transform:uppercase;
letter-spacing:.06em;color:var(--accent)}
.page h6{font-size:12.5px;font-weight:600;margin:13px 0 4px;font-style:italic;
color:var(--muted)}
.page>:first-child{margin-top:0}
.page section{margin:0}
/* A heading owns what follows it, and the indent is what says so — an agenda item's
   paragraphs sit INSIDE the item rather than merely after it. Only from the third level
   down: indenting every section would push a whole page to the right for nothing. */
.page section.lvl3,.page section.lvl4,.page section.lvl5,.page section.lvl6{
padding-left:16px}
.page p{margin:9px 0}
.page ul,.page ol{margin:8px 0;padding-left:22px}
.page li{margin:3px 0}
.page li.ak-done{color:var(--muted);text-decoration:line-through}
.page blockquote{margin:10px 0;padding:2px 14px;border-left:3px solid var(--line);
color:var(--muted)}
.page img{max-width:100%;height:auto;border-radius:6px;margin:8px 0;display:block}
.page pre{background:var(--head);border:1px solid var(--line);border-radius:8px;
padding:12px 14px;overflow-x:auto;margin:0}
.page code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.page hr{border:0;border-top:1px solid var(--line);margin:18px 0}
.ak-code{margin:12px 0}
.ak-code-lang{display:inline-block;font-size:11px;letter-spacing:.04em;
text-transform:uppercase;color:var(--muted);padding:0 0 4px 2px}
.ak-wiki{color:var(--accent,#4a7dbd);border-bottom:1px dotted currentColor}
.ak-img-missing{display:inline-block;padding:6px 10px;margin:6px 0;border-radius:6px;
border:1px dashed var(--line);color:var(--muted);font-size:13px}
.ak-scroll{overflow-x:auto}
.mailbody{padding:4px 16px 12px;background:#fff;color:#111;border-radius:0 0 10px 10px;overflow-x:auto}
.page td:first-child{font-family:inherit;font-size:inherit;color:inherit}
"""


def _body_html(text: str) -> str:
    """Readable body: quoted history dimmed rather than deleted."""
    out = []
    for line in (text or "").splitlines():
        esc = html.escape(line)
        out.append(f'<span class="quote">{esc}</span>' if line.lstrip().startswith(">")
                   else esc)
    return "\n".join(out)


#: Anything that executes, navigates, or fetches. A mail body is markup written by
#: someone else and rendered in YOUR browser; the layout is the part worth keeping and
#: none of this is.
_STRIP_TAGS = ("script", "style", "iframe", "object", "embed", "link", "meta", "form",
               "base", "applet")
_EVENT_ATTR = __import__("re").compile(r"""\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""",
                                       __import__("re").I)
_REMOTE_SRC = __import__("re").compile(
    r"""\s(src|background)\s*=\s*("https?://[^"]*"|'https?://[^']*')""",
    __import__("re").I)
_CID_SRC = __import__("re").compile(r"""\ssrc\s*=\s*["']cid:([^"']+)["']""",
                                    __import__("re").I)


def _safe_mail_html(markup: str, inline: dict) -> str:
    """A message body as it was sent, minus anything that executes or phones home.

    Three classes, and the third is the one that matters most. Scripts and frames are
    third-party CODE running in the reader's browser. Remote images are a REQUEST: a
    tracking pixel reports that this message was opened, when, and from where — so
    rendering mail "unchanged" would turn every read into a receipt the sender collects.
    What is kept is the markup, the layout and the images the message already carries.
    """
    import base64
    import re as _re
    body = markup or ""
    for tag in _STRIP_TAGS:
        body = _re.sub(rf"<{tag}\b.*?</{tag}\s*>", "", body, flags=_re.I | _re.S)
        body = _re.sub(rf"<{tag}\b[^>]*/?>", "", body, flags=_re.I)
    body = _EVENT_ATTR.sub("", body)

    def _inline(m):
        part = (inline or {}).get(m.group(1)) or {}
        if not part.get("data"):
            return ' data-cid-missing="1"'
        uri = base64.b64encode(part["data"]).decode("ascii")
        return f' src="data:{part.get("mime", "image/png")};base64,{uri}"'

    body = _CID_SRC.sub(_inline, body)
    # A remote reference is neutralised rather than removed, so the alt text and the
    # layout survive and the reader can see that something was withheld.
    body = _REMOTE_SRC.sub(' data-remote=\2 data-blocked="1"', body)
    return body


def _is_document(rows: list) -> bool:
    return bool(rows) and all("body" in r for r in rows)


def _is_markdown(record: dict) -> bool:
    """Whether this record's body IS markdown, asked of the record rather than guessed.

    A mail body is plain text: run a markdown renderer over it and a line beginning
    "#1 priority" becomes a heading, a signature's asterisks become emphasis, and a
    quoted `>` reply turns into a blockquote inside a blockquote. The file extension on
    the record's own path is the honest signal — a vault page is a .md file and says so.
    """
    return str(record.get("path") or "").lower().endswith(".md")


def _page_html(record: dict) -> str:
    """A vault page rendered from its source, with its images inlined."""
    note = pathlib.Path(str(record["path"]))
    root = _vault_root(note)
    return md.render(record.get("body") or "", note=note, root=root)


def _vault_root(note: pathlib.Path):
    """Which vault this note belongs to — for resolving `![[an-image.png]]`, which
    Obsidian looks up across the whole vault rather than beside the note.

    Asked of the registered brains, so an embed resolves the way it does in the editor
    the page was written in. Falls back to the note's own folder, which is correct for a
    page that belongs to no registered brain.
    """
    try:
        from .registry import settings as declared
        for rec in declared().get("brains") or []:
            path = rec.get("path") if isinstance(rec, dict) else rec
            if not path:
                continue
            root = pathlib.Path(str(path)).expanduser().resolve()
            if root in note.resolve().parents:
                return root
    except Exception:                                            # noqa: BLE001
        pass
    # Not in a registered brain — a scratch vault, a repo checked out to look at, a
    # tasks folder pointed at directly. Walk up for the marker a vault carries instead
    # of giving up at the note's own folder, or `![[an-image.png]]` beside the vault
    # root resolves to nothing for anyone who has not registered it.
    for parent in [note.parent, *note.parents]:
        if (parent / "brain.yml").is_file() or (parent / ".obsidian").is_dir():
            return parent
    return note.parent


def render_document(rows: list, title: str, subtitle: str) -> str:
    """Reading layout for message-shaped records (one card per message)."""
    cards = []
    for m in rows:
        meta = []
        for label, key in (("From", "from"), ("To", "to"), ("Cc", "cc"), ("Date", "date")):
            if m.get(key):
                meta.append(f"<dt>{label}</dt><dd>{html.escape(str(m[key]))}</dd>")
        atts = m.get("attachments") or []
        att_html = ""
        if atts:
            names = [a.get("filename", "?") if isinstance(a, dict) else str(a) for a in atts]
            att_html = ('<div class="att"><div class="tags">' +
                        "".join(f'<span class="tag">📎 {html.escape(n)}</span>' for n in names)
                        + "</div></div>")
        heading = (m.get("subject") or (m.get("meta") or {}).get("title")
                   or m.get("slug") or "(no subject)")
        if m.get("body_html"):
            # The message's own markup, in the page, because the page IS the message.
            # It was in a sandboxed iframe, which needed srcdoc escaping and a guessed
            # height because a sandboxed frame cannot size itself — all of it the cost of
            # embedding the mail inside finrail's chrome rather than letting it be the
            # document. A stylesheet from the sender may now restyle these few headers.
            # That is the correct trade: you are looking at their email.
            inner = (f'<div class="mailbody">'
                     f'{_safe_mail_html(m["body_html"], m.get("_inline") or {})}</div>'
                     f'<div class="att"><span class="tag">shown as sent · remote images '
                     f'not fetched</span></div>')
        elif _is_markdown(m):
            inner = f'<div class="page">{_page_html(m)}</div>'
        else:
            inner = f'<div class="body">{_body_html(m.get("body") or "")}</div>'
        meta_html = f'<dl class="meta">{"".join(meta)}</dl>' if meta else ""
        cards.append(f'<div class="msg"><h2>{html.escape(str(heading))}'
                     f'</h2>{meta_html}{att_html}{inner}</div>')
    stamp = datetime.datetime.now().strftime("%d %b %Y, %H:%M")
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{_CSS}{_DOC_CSS}</style></head>"
            '<body class="ak">'
            f"<h1>{html.escape(title)}</h1>"
            f"<p class=sub>{html.escape(subtitle or f'{len(rows)} message(s)')} · {stamp}</p>"
            f"{''.join(cards)}"
            f"<footer>Generated by finrail view</footer></body></html>")


def render(data, title: str, subtitle: str = "") -> str:
    rows, out = _rows(data), []
    if _is_document(rows):                       # emails/notes read, they don't tabulate
        return render_document(rows, title, subtitle)
    cols = _columns(rows)
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{_cell(r.get(c))}</td>" for c in cols) + "</tr>")
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    stamp = datetime.datetime.now().strftime("%d %b %Y, %H:%M")
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
            '<body class="ak">'
            f"<h1>{html.escape(title)}</h1>"
            f"<p class=sub>{html.escape(subtitle or f'{len(rows)} row(s)')} · {stamp}</p>"
            f"<div class=scroll><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(out)}</tbody></table></div>"
            f"<footer>Generated by finrail view</footer></body></html>")


def write(data, title: str, subtitle: str = "", out: str = None,
          slug: str = "") -> pathlib.Path:
    """Render, and hand back somewhere to open it from.

    Transient by default. A page is something you LOOK at once; keeping every one of them
    made a tenth directory under the state root, full of stale snapshots nobody would ever
    open again and which say nothing about what the mailbox contains now. The original
    system kept the content inline in its result and let the front end draw it — nothing
    was saved — and a temporary file is the nearest thing to that when the front end is
    whatever opens a path.

    `out` writes to a real location, for when you actually want to keep one.
    """
    base = (slug or title).lower()
    slug = "".join(c if c.isalnum() else "-" for c in base)[:48].strip("-") or "view"
    if out:
        path = pathlib.Path(out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        # One directory, reused: rendering the inbox twice replaces the page rather than
        # accumulating another copy of a view that is already out of date.
        tmp = pathlib.Path(tempfile.gettempdir()) / "finrail-views"
        tmp.mkdir(parents=True, exist_ok=True)
        path = tmp / f"{slug}.html"
    path.write_text(render(data, title, subtitle))
    return path


def show(path: pathlib.Path) -> str:
    """Open it. Returns what happened, so the caller can say so honestly.

    Asking someone to go and open a file is a step that exists only because the software
    stopped short. It is skippable — `view.open=false` — because a machine rendering on a
    schedule should not have windows appearing on it.
    """
    from .registry import settings as _settings
    # Off unless explicitly asked for. A window appearing over whatever you were doing is
    # worse than a file you click when you want it — opening things at people is not a
    # feature, and a card at least waits.
    try:
        if not _settings().get("view.open"):
            return "not opened"
    except Exception:                                            # noqa: BLE001
        return "not opened"
    opener = {"darwin": "open", "linux": "xdg-open"}.get(sys.platform)
    if not opener or not shutil.which(opener):
        return f"no opener on this platform — open it yourself: {path}"
    try:
        subprocess.Popen([opener, str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "opened"
    except OSError as e:
        return f"could not open it ({e}) — open it yourself: {path}"


# ── markdown, for a front end that is a chat ────────────────────────────────
def _md_cell(val) -> str:
    """One value in a markdown table cell.

    A pipe ends a cell and a newline ends the ROW, so an unescaped one does not merely
    look wrong — it silently truncates the table around whatever the data happened to
    contain. The same reason `_render_sessions` escapes a title's pipes.
    """
    if val is None or val == "":
        return "—"
    if isinstance(val, bool):
        return "⚡" if val else "—"
    if isinstance(val, list):
        val = ", ".join(str(t) for t in val[:6])
    return str(val).replace("|", "\\|").replace("\n", " ").strip() or "—"


def _fence(body: str) -> str:
    """A fence longer than any run of backticks inside, so the block closes where meant."""
    fence = "```"
    while fence in body:
        fence += "`"
    return fence


def _heading(title: str, subtitle: str) -> list:
    # No timestamp. The HTML page carries one because it is a file that outlives the
    # asking; a chat message is already stamped with when it arrived.
    return [f"### {title}"] + ([f"_{subtitle}_"] if subtitle else []) + [""]


def render_document_markdown(rows: list, title: str, subtitle: str = "") -> str:
    """Message-shaped records as markdown: one section per message, bodies intact."""
    out = _heading(title, subtitle or f"{len(rows)} message(s)")
    for m in rows:
        heading = (m.get("subject") or (m.get("meta") or {}).get("title")
                   or m.get("slug") or "(no subject)")
        head = [f"**{_md_cell(heading)}**"]
        head += [f"{label}: {_md_cell(m[key])}"
                 for label, key in (("From", "from"), ("To", "to"), ("Cc", "cc"),
                                    ("Date", "date")) if m.get(key)]
        atts = m.get("attachments") or []
        if atts:
            names = [a.get("filename", "?") if isinstance(a, dict) else str(a) for a in atts]
            head.append(" ".join(f"📎 {_md_cell(n)}" for n in names))
        # Two trailing spaces, not blank lines: markdown otherwise runs the subject, the
        # sender and the date together into one paragraph — a header that reads as prose.
        out.append("  \n".join(head))
        out.append("")
        body = m.get("body") or ""
        if _is_markdown(m):
            # A vault page IS markdown; fencing it would show the reader its source.
            out.append(body)
        elif body:
            # Everything else is plain text that only looks like markdown by accident —
            # a "#1 priority" line is not a heading and a quoted reply is not a
            # blockquote. Fenced, it arrives as it was written.
            f = _fence(body)
            out.append(f"{f}\n{body}\n{f}")
        elif m.get("body_html"):
            # The markup is the desktop view's business: pasting a mail's HTML into a
            # chat renders as tag soup, and stripping it here would be a second, worse
            # mail renderer.
            out.append("_(HTML message — open it with the desktop view to read it)_")
        out.append("")
    return "\n".join(out).strip()


def render_markdown(data, title: str, subtitle: str = "") -> str:
    """The same rows the page draws, as markdown a chat can render.

    Deliberately the same shape as `render`: one renderer per front end over one set of
    rows, so a capability's `view_data` is the only place that decides what a view
    contains and neither renderer learns anything about mail or tasks.
    """
    rows = _rows(data)
    if not rows:
        return "\n".join(_heading(title, subtitle)).strip() + "\n\n(nothing to show)"
    if _is_document(rows):                       # emails/notes read, they don't tabulate
        return render_document_markdown(rows, title, subtitle)
    cols = _columns(rows)
    lines = _heading(title, subtitle or f"{len(rows)} row(s)")
    lines.append("| " + " | ".join(_md_cell(c) for c in cols) + " |")
    lines.append("|" + "---|" * len(cols))
    for r in rows:
        lines.append("| " + " | ".join(_md_cell(r.get(c)) for c in cols) + " |")
    return "\n".join(lines)


def substantive(rows: list) -> bool:
    """Does this view show the THING, or only the fact that it exists?

    The question a fallback needs answered, asked of the rows rather than of the text
    they render into — string-sniffing an output for "(nothing to show)" is a second
    renderer's worth of guesswork.

    A document with no body is the case that prompted this: "show me this message"
    answered with a subject, a sender and a date is not showing anybody the message. An
    HTML-only mail renders exactly that way — the markdown renderer has nothing to put
    in the body and says to open the desktop view, which is advice a chat window cannot
    take.
    """
    if not rows:
        return False
    if _is_document(rows):
        return any((r.get("body") or "").strip() for r in rows)
    return True


def _fallback(cap, result, title: str, subtitle: str) -> str:
    """What to show when the view itself has nothing. The capability's OWN text.

    `render_data` targets a terminal, so for a chat it is fenced: an aligned table is
    precisely what markdown collapses into a jumble, and this is reached for exactly
    when the row renderer could not do the job.
    """
    try:
        text = str(cap.render_data(result) or "").strip()
    except Exception:                                            # noqa: BLE001
        _logger.warning("%s: render_data failed as a fallback for view", cap.name)
        text = ""
    head = "\n".join(_heading(title, subtitle)).strip()
    if not text:
        # Say which read came back empty. A blank answer is indistinguishable from a
        # broken one, and the reader cannot tell whether to look again or elsewhere.
        return f"{head}\n\n(nothing to show)"
    _logger.info("%s: the view had no content; fell back to its own rendering", cap.name)
    if front_end() == CHAT:
        fence = _fence(text)
        return f"{head}\n\n{fence}\n{text}\n{fence}"
    return text


def deliver(cap, result, title: str, subtitle: str = "", slug: str = "",
            path_for=None) -> str:
    """A capability's result, rendered and handed over the way this front end takes it.

    `path_for` is a callable rather than a path because a markdown front end writes
    NOTHING: asking for the destination up front would record a run and reserve an
    artifact directory for a file that is never going to exist.

    THE VIEW IS TRIED FIRST AND ALWAYS, AND NEVER HANDS BACK AN EMPTY FRAME. Every
    caller used to carry the same judgement — view it, or print it? — and the judgement
    was made per call site by whoever was writing one. Made wrongly it costs the whole
    answer: a request to SEE something came back as a heading with nothing under it, or
    as a path to a page with no rows in it, and the person was left to ask again. The
    ladder is here instead, in the one place a view is produced:

        the capability's own markdown  →  the rendered rows  →  its own text  →  "nothing"

    Each rung is taken only when the one above it has no content, so a caller gets the
    best available rendering of the thing itself, or a sentence saying there is none.
    """
    rows = []
    try:
        rows = _rows(cap.view_data(result))
    except Exception:                                            # noqa: BLE001
        _logger.warning("%s: view_data failed; falling back to its own rendering",
                        cap.name)
    if front_end() == CHAT:
        # A capability that already renders markdown for a person has said so, and its
        # own table beats a generic one built from the same rows — it knows which session
        # is the current one, that a listing is scoped to you, and what to call things.
        own = cap.view_markdown(result)
        if own and str(own).strip():
            return own
        if not substantive(rows):
            return _fallback(cap, result, title, subtitle)
        return render_markdown(rows, title, subtitle)
    if not substantive(rows):
        # No page, and no run recorded for one: `path_for` reserves an artifact
        # directory, and a directory reserved for a page nobody can read is the same
        # lie in a different place.
        return _fallback(cap, result, title, subtitle)
    path = write(rows, title, subtitle,
                 out=path_for() if path_for else None, slug=slug)
    show(path)
    # Two lines: the file, and the call that shows it. Everything else this printed got
    # relayed into the chat alongside the page, so a request to SEE something produced a
    # paragraph about seeing it. The output is an instruction to a tool, not a briefing
    # for the reader. The preview pane, not a file card: it opens the page in the side
    # panel with nothing to click and nothing sent anywhere.
    return (f"{path}\n"
            f'mcp__Claude_Browser__preview_start({{"url": "file://{path}"}})')
