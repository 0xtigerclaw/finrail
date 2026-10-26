"""YAML frontmatter — a deliberately small subset, byte-compatible with the vault.

The knowledge vault is typed markdown with YAML frontmatter, written and read by Obsidian
and by hand as well as by this system. So the parser has to accept what those write, and
the writer has to produce the same shape it found — a reformatting writer would show up as
a diff on every file it touched.

Supported (which is everything the vault actually uses):
    key: scalar
    key: 'quoted scalar'
    key:              -> list, items as unindented or indented "- item" lines
    key: [a, b]       -> inline list
    key:              -> None (empty)

Anything more exotic is left as a raw string rather than guessed at.
"""
import datetime
import re

DELIM = "---"


def _scalar(text: str):
    s = text.strip()
    if not s:
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    if s.lower() in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    return s


def parse(text: str) -> tuple:
    """(meta, body). A document with no frontmatter yields ({}, text)."""
    if not text.startswith(DELIM):
        return {}, text
    lines = text.splitlines()
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == DELIM:
            end = i
            break
    if end is None:
        return {}, text

    meta, key = {}, None
    for line in lines[1:end]:
        if not line.strip():
            continue
        item = re.match(r"\s*-\s+(.*)$", line)
        if item and key is not None:
            # `key:` with an empty value parses as None until its first list item
            # arrives — at which point it was always a list. (Getting this wrong
            # silently dropped every tags: and aliases: block.)
            if meta.get(key) is None:
                meta[key] = []
            if isinstance(meta[key], list):
                meta[key].append(_scalar(item.group(1)))
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        meta[key] = _scalar(raw) if raw.strip() else None
    body = "\n".join(lines[end + 1:])
    return meta, body.lstrip("\n")


NEEDS_QUOTE = re.compile(r"^[\d\-]|[:#]|^\s|\s$")


def _emit(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return f"'{value.isoformat()}'"
    s = str(value)
    return f"'{s}'" if NEEDS_QUOTE.search(s) else s


def dump(meta: dict, body: str = "") -> str:
    """Frontmatter + body, in the vault's own style (unindented list items)."""
    out = [DELIM]
    for key, value in meta.items():
        if isinstance(value, (list, tuple)):
            out.append(f"{key}:")
            out.extend(f"- {_emit(v)}" for v in value)
        elif value is None:
            out.append(f"{key}:")
        else:
            out.append(f"{key}: {_emit(value)}")
    out.append(DELIM)
    text = "\n".join(out) + "\n"
    if body:
        text += "\n" + body.lstrip("\n")
        if not text.endswith("\n"):
            text += "\n"
    return text


def read(path) -> tuple:
    import pathlib
    return parse(pathlib.Path(path).read_text())


def write(path, meta: dict, body: str = "") -> None:
    """Write through the write-guard, so the repository plugin can version it."""
    import pathlib
    from . import guard
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with guard.writing(p):
        p.write_text(dump(meta, body))


def slugify(text: str, limit: int = 60) -> str:
    import unicodedata
    folded = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return (s[:limit].rstrip("-") or "untitled")


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return datetime.date.today().isoformat()
