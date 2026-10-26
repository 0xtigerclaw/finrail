"""POLICY — the shell-shape gate, and the read-only lint.

Two distinct things live here, and neither is capability approval (that is the `confirm`
attribute on the capability class, and nowhere else):

1. `decide(cmd)` — may this SHELL COMMAND run? Consumed by the Claude Code hook so a
   locked workspace can reach `finrail` and a short list of safe reads, and nothing
   else. Deny by default: an unrecognised command is refused, including a brand-new tool
   nobody thought to block.

2. `audit()` — a lint over the capability catalogue: does anything that claims to be
   read-only actually look like it writes? (The real proof is the filesystem-snapshot
   test; this is the cheap check that runs everywhere.)
"""
import inspect
import re
import shlex

ALLOW, ASK, DENY, ASK_RUN = "allow", "ask", "deny", "ask_run"

# Read-only shell tools a locked session may use to look around.
SAFE_READS = ("ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "which",
              "pwd", "echo", "date", "file", "stat", "sort", "uniq", "cut", "tr",
              "basename", "dirname", "realpath", "env", "printenv")

#: What turns a read tool into something else. Allowed by first word alone, `env` ran any
#: program (`env rm -rf …`, `env -u FINRAIL_CONTEXT finrail do …`) and `find` deleted and
#: executed — found on the live gate 2026-09-17. `read_refusal` is consulted wherever
#: SAFE_READS is.
FIND_ACTS = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0",
             "-fprintf", "-fls")

#: Tools that are a read only in a particular SHAPE, and so are deliberately NOT on
#: SAFE_READS: nobody gets one by default. A scope that needs one names it —
#: `Scope(reads=("sqlite3",))` — and `read_refusal` below holds the shape, so the one
#: function that answers "is this use a read" answers for these too.
GUARDED_READS = ("sqlite3",)

#: `sqlite3` reads a database, and the same binary writes it, writes files (`.output`,
#: `.backup`, `.save`), runs programs (`.shell`, `.system`) and loads extensions
#: (`.load`) — the shape of hole closed for `env` and `find` on 2026-09-17. So it is a
#: read only under sqlite's OWN restrictions: `-readonly` refuses every write to the
#: database, `-safe` refuses the rest. Those rather than a parse of the SQL, because the
#: binary applies them to what arrives on STDIN as well, which a gate reading the command
#: line cannot see.
SQLITE_READ_FLAGS = ("readonly", "safe")

#: The command line's ways back out of safe mode: `-nonce` hands over the string that
#: suspends it (and `.nonce` spends it), `-init` and `-cmd` run commands, `-A` is
#: `.archive`, `-unsafe-testing` is what it says. sqlite3 takes one dash or two for all
#: of them, so they are matched on the name alone.
SQLITE_ESCAPES = ("nonce", "init", "cmd", "A", "archive", "unsafe-testing")

#: How a guarded read must be WRITTEN, for the briefing. An agent told it has `sqlite3`
#: and not told the shape learns it by being refused, mid-task — the same reason the
#: excluded capabilities are in the briefing rather than only in a refusal.
GUARDED_READ_FORMS = {"sqlite3": 'sqlite3 -readonly -safe <database> "<SELECT …>"'}


def read_refusal(words: list) -> str:
    """Why this use of a read tool is not a read, or "" when it is one.

    `words` is the segment already split, its first word the tool.
    """
    if not words:
        return ""
    head, args = words[0].rsplit("/", 1)[-1], words[1:]
    if head == "env" and args:
        # With no arguments it prints the environment. With any, it runs a program — or
        # changes the environment a program runs in, which is how a turn's context would
        # be taken off a command. `printenv` is the read.
        return "'env' with arguments runs a program — use printenv to read the environment"
    if head == "find":
        acts = [w for w in args if w in FIND_ACTS]
        if acts:
            return f"find {acts[0]} deletes, runs or writes — it is not a read"
    if head == "sort" and any(w == "--output" or w.startswith("--output=")
                              or (w.startswith("-") and not w.startswith("--") and "o" in w)
                              for w in args):
        # `-o` hides in a cluster (`-ro`, `-no`) the way it does in no other read tool's
        # flags: any short cluster carrying an `o` writes a file (found 19 Oct).
        return "sort -o writes a file"
    if head == "uniq" and len([w for w in args if not w.startswith("-")]) > 1:
        return "uniq with a second file writes to it"
    if head == "rg" and any(w == "--pre" or w.startswith("--pre=") for w in args):
        return "rg --pre runs a program on every file"
    if head == "file" and any(w == "-C" or w == "--compile" for w in args):
        return "file -C writes a compiled magic file"
    if head == "command" and not (args and args[0] in ("-v", "-V")):
        return "'command' runs the program it names — only command -v/-V is a lookup"
    if head == "sqlite3":
        flags = {w.lstrip("-") for w in args if w.startswith("-")}
        missing = [f for f in SQLITE_READ_FLAGS if f not in flags]
        if missing:
            return (f"sqlite3 writes the database, writes files and shells out — it is a "
                    f"read only with {' '.join('-' + f for f in SQLITE_READ_FLAGS)}, and "
                    f"{', '.join('-' + f for f in missing)} is missing")
        out = [f for f in SQLITE_ESCAPES if f in flags]
        if out:
            return f"sqlite3 -{out[0]} gets out of safe mode — it is not a read"
        if any(re.search(r"(?:^|\n)\s*\.nonce\b", w) for w in args):
            # Without `-nonce` there is no string for it to match, so this is insurance
            # rather than the guard: the one documented way to suspend safe mode reads
            # the same in a refusal as it does in sqlite's own help.
            return "sqlite3 .nonce suspends safe mode — it is not a read"
    return ""


RULES = (
    # (regex over the command string, decision, reason)
    # Matches every spelling that reaches the capability: `full-agent`, `do full-agent`,
    # `dispatch full-agent`. Anchoring on the word alone let `finrail do agent` through —
    # the one capability that must always ask was the one escaping the gate. There is
    # exactly one spelling now: the registry refuses the old name outright, so a rule
    # matching it would gate a command that cannot run.
    # `(?:\s+-\S+)*`: global flags may come first (`finrail --json approve`), and the
    # entrypoint strips them before choosing the command; so must every rule here
    # (review, 5 Oct: `finrail --json approve <id>` fell through to the plain allow).
    (r"^finrail(?:\s+-\S+)*\s+(do\s+|dispatch\s+)?(?:-\S+\s+)*full-agent(\.|\s|$)", ASK,
     "the full agent runs off the rails on purpose — it must be asked for explicitly"),
    (r"^finrail(?:\s+-\S+)*\s+approve\b", ASK_RUN,
     "approval defers to the run's own confirm flag"),
    (r"^finrail(?:\s+-\S+)*\s+kill\b.*--purge", ASK,
     "purging destroys the ledger record of what happened"),
    (r"^finrail\b", ALLOW,
     "finrail is the sanctioned surface: its own rules gate what it will do"),
    (r"^git\s+(status|log|diff|show|branch)\b", ALLOW, "read-only git"),
)


def _first_word(cmd: str) -> str:
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return ""
    return parts[0] if parts else ""


#: Output thrown away: `2>/dev/null`, `>/dev/null`, `&>/dev/null`, `>>/dev/null`. The one
#: redirection that writes nothing. In twelve test questions (13 Oct) the record keeper
#: lost a round to it seven times, `finrail get ... 2>/dev/null` refused as a write.
_DEVNULL = re.compile(r"\s*(?:[0-9]*|&)>{1,2}&?\s*/dev/null(?![^\s;|&)])")


def without_devnull(text: str) -> str:
    """`text` with every redirection to /dev/null removed: it changes nothing a command
    does to the machine, so it is judged as if it were not there."""
    return _DEVNULL.sub("", text or "")


def decide(cmd: str) -> dict:
    """{decision, reason} for a shell command. Deny by default."""
    text = without_devnull((cmd or "").strip()).strip()
    if not text:
        return {"decision": DENY, "reason": "empty command"}

    # Redirection makes a WRITE out of anything, so it is judged before the command is.
    # `cat` is on the read-only list and `cat > out.html` creates a file: the allowlist
    # describes commands, and a shell feature that ignores the command defeats it. A
    # locked session that can write files can write anything, and the one guarantee this
    # gate makes is that it cannot.
    # The one `>` that is not a write is a descriptor duplication: `>&` followed by a
    # descriptor number (`2>&1`, `>&2`) or by `-` (close it). Anything else after `>&` is
    # a file, and bash sends stdout and stderr to it: `cat a >& /etc/x` writes /etc/x.
    # A digit before the `>` does not excuse it either: `cat a 2> /etc/x` writes too
    # (both read as duplications until 6 Oct).
    if re.search(r">{1,2}(?!&(?:[0-9]+|-)(?![^\s;|&)]))", text):
        return {"decision": DENY,
                "reason": "output redirection writes a file, whatever the command is — "
                          "this workspace only runs finrail. To produce a file, use a "
                          "capability that writes one (e.g. finrail view <capability>)."}

    # A pipeline or chain is only as safe as its worst part.
    segments = [s for s in _segments(text) if s]
    worst = None
    for seg in segments:
        d = _decide_one(seg)
        rank = {ALLOW: 0, ASK_RUN: 1, ASK: 2, DENY: 3}[d["decision"]]
        if worst is None or rank > worst[0]:
            worst = (rank, d)
    return worst[1]


def _walk(text: str) -> tuple:
    """The command as the shell reads its quoting: a list of `(i, piece, quote)` and
    the quote still open at the end ("" when none).

    A piece is one character, or a backslash with the character it takes. `quote` is
    the quote the piece is read in: "" outside any, "'" or '"' inside those, "$'"
    inside a dollar-quote; a quote mark itself carries the quote it opens or closes.
    What is read inside a quote, or taken by a backslash, is data to every reader of
    this: `_segments` does not split there, and the scoped gate's sandbox check does
    not see a second command there (core/scope.py). One walker, so the two cannot
    disagree about where a quote ends.

    A BACKSLASH TAKES THE NEXT CHARACTER AS ITSELF, as the shell reads it everywhere but
    inside '…'. Without that the splitter and the shell disagreed in both directions
    (second review, 6 Oct): `\\>` is a word, so the `&` after it does separate, and was
    kept as a redirection; `R\\&D` is one word, and was cut in two and refused; and `\\"`
    inside "…" does not end the quote, so a separator after it was read as quoted, or a
    quoted one as bare.

    A DOLLAR-QUOTE ($'…') IS A SINGLE QUOTE IN WHICH A BACKSLASH DOES ESCAPE. Read as a
    plain '…', `$'\\''` looked like a closed quote followed by an open one, so everything
    after it was taken as quoted and `finrail caps $'\\'' & python3 evil.py \\'` was one
    command to the splitter and two to the shell (second review, 6 Oct). It is its own
    kind of quote here, ended by the first unescaped single quote.
    """
    pieces, quote = [], ""
    i, esc = 0, -1                      # esc: where the last character a backslash took is
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote != "'" and i + 1 < len(text):
            pieces.append((i, text[i:i + 2], quote))
            i += 1
            esc = i
        elif quote:
            pieces.append((i, ch, quote))
            if ch == quote[-1]:
                quote = ""
        elif ch in "'\"":
            # $'…' only when the dollar is itself unescaped: `\$'a'` is a plain quote.
            quote = "$'" if ch == "'" and text[i - 1:i] == "$" and esc != i - 1 else ch
            pieces.append((i, ch, quote))
        else:
            pieces.append((i, ch, ""))
        i += 1
    return pieces, quote


def _segments(text: str) -> list:
    """Split a command on shell separators, but not on ones inside quotes.

    A NEWLINE OUTSIDE QUOTES IS A SEPARATOR. It was not one, so `ls<newline>rm -rf x`
    was a single segment, read by shlex as `ls` with three more arguments, and allowed
    by its first word; bash ran both lines (found 6 Oct). Three things that look like a
    second line are not: a backslash before the newline joins the two lines into one
    command; a newline inside quotes is text (`echo 'a<newline>b'`); and the lines of a
    heredoc body are input, skipped by `_heredoc_bodies`, which says what that means for
    a body that would not pass as commands — and hands back the ones an unquoted
    delimiter leaves expandable, for `_heredoc_cuts` to judge the commands the shell
    runs in them.

    `grep -E 'link|url|gmail'` was split at the alternation INSIDE the pattern, producing
    fragments like `grep -i -E 'link` and `url`, none of which is an allowed command. A
    legitimate read was refused, and the reason quoted a command nobody typed. Quoting is
    the normal way to write a regex; a gate that cannot read quotes will keep refusing
    correct commands, and being wrong in that direction teaches people the gate is noise.
    How a quote is read, with backslashes and dollar-quotes, is `_walk`'s.

    A SINGLE `&` SEPARATES TOO. Only `&&` was split, so in `ls & rm -rf x` the second
    command was never judged by any ruleset, and `finrail caps & python3 x.py` left the
    sandbox whole on finrail's allow (review, 5 Oct). Next to a `>`, or right after a
    `<`, the `&` is part of a redirection (`2>&1`, `>&2`, `&>file`, `<&3`) and stays in
    its command: splitting there would cut one command in two and refuse it.
    """
    pieces, _ = _walk(text)
    out, cur, prev, skip, upto = [], [], "", -1, -1
    heredocs = []                     # (delimiter, strip tabs) of bodies after this line
    for i, piece, quote in pieces:
        if i == skip or i < upto:
            pass                        # the second half of `&&` or `$(`, or a heredoc word
        elif piece == "\\\n" and not quote:
            pass                        # a backslash-newline joins two lines: it is not text
        elif quote == '"' and (piece == "`" or (text.startswith("$(", i)
                                                and not _arithmetic(text, i))):
            # Inside "…" the shell still runs `…` and $(…), so they cut here as they do
            # bare. `echo "$(python3 evil.py)"` was one segment, judged as `echo` and
            # allowed (review, 16 Oct). Inside '…' they are text and stay in the word.
            # "$((2+3))" is arithmetic and runs nothing, so it stays: it is how production
            # is told to count. A `$(` or backtick inside it still cuts.
            out.append("".join(cur).strip())
            cur = []
            if piece != "`":
                skip = i + 1
        elif quote or len(piece) == 2:
            cur.append(piece)
        elif piece == "\n":
            out.append("".join(cur).strip())
            cur = []
            if heredocs:
                # The bodies are input, not commands, and they are skipped BEFORE their
                # text is read for quotes: an apostrophe in a body ("it's") would open a
                # quote that hides every separator after it. So what follows the bodies
                # is read afresh, as its own text.
                rest, bodies = _heredoc_bodies(text, i + 1, heredocs)
                # An unquoted delimiter leaves the body a double-quoted context: bash
                # runs `$(…)` and `…` in it while it gathers the body, so each is judged
                # as the command it runs (review, 19 Oct), exactly as inside "…".
                for body in bodies:
                    out += _heredoc_cuts(body)
                return [s for s in out if s] + _segments(text[rest:])
        elif _heredoc_starts(text, i):
            word, tabs, quoted, end = _heredoc_word(text, i + 2)
            cur.append(text[i:end])
            if word:
                heredocs.append((word, tabs, quoted))
            upto = end
        elif piece in "|;`":
            out.append("".join(cur).strip())
            cur = []
        elif piece == "&" and (prev in (">", "<") or text[i + 1:i + 2] == ">"):
            cur.append(piece)
        elif piece == "&":
            out.append("".join(cur).strip())
            cur = []
            if text[i:i + 2] == "&&":
                skip = i + 1
        elif text[i:i + 2] in ("$(", "<("):
            # `<(…)` runs its contents as a command, as `$(…)` does: `cat <(curl x | sh)`
            # was judged as `cat` alone and allowed (review, 14 Oct). `>(…)` never gets
            # here: the redirection rule refuses any `>` first.
            out.append("".join(cur).strip())
            cur = []
            skip = i + 1
        else:
            cur.append(piece)
        prev = piece
    out.append("".join(cur).strip())
    return [s for s in out if s]


def _arithmetic(text: str, i: int) -> bool:
    """Is the `$(` at `text[i]` an arithmetic expansion, `$((…))`? Only when its
    parentheses close as `))`: bash reads `$((echo hi) )` as a command substitution of a
    subshell, and runs it. Anything inside is still read by the caller, so a `$(` or a
    backtick in the expression is cut there as a command of its own."""
    if not text.startswith("$((", i):
        return False
    depth, j = 2, i + 3
    while j < len(text):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 1:
                return text[j + 1:j + 2] == ")"
        j += 1
    return False


def _heredoc_starts(text: str, i: int) -> bool:
    """Is `text[i]` the first `<` of a heredoc operator? `<<<` is a here-string, which
    has no body; the check on the character before is for the second `<` of one."""
    return (text[i:i + 2] == "<<" and text[i + 2:i + 3] != "<"
            and (i == 0 or text[i - 1] != "<"))


def _heredoc_word(text: str, i: int) -> tuple:
    """The delimiter of the heredoc whose `<<` ends at `text[i]`, as bash will look for
    it: quotes and backslashes removed (`<<'EOF'`, `<<"EOF"`, `<<\\EOF`), after an
    optional `-` (strip leading tabs from the body) and optional blanks. Returns
    (delimiter, strip tabs, quoted, index after the word). `quoted` is whether ANY part
    of the delimiter was quoted (`<<'EOF'`, `<<"EOF"`, `<<\\E\\F`): bash then writes the
    body literally, substitutions and all, so its text is never judged (review, 19
    Oct). An empty delimiter is a bash syntax error, and the caller records no body
    for it."""
    tabs = text[i:i + 1] == "-"
    if tabs:
        i += 1
    while text[i:i + 1] in (" ", "\t"):
        i += 1
    word, quote, quoted = [], "", False
    while i < len(text):
        ch = text[i]
        if ch == "\n":
            break
        if quote:
            if ch == quote:
                quote = ""
            else:
                word.append(ch)
        elif ch in "'\"":
            quote = ch
            quoted = True
        elif ch == "\\" and i + 1 < len(text) and text[i + 1] != "\n":
            i += 1
            word.append(text[i])
            quoted = True
        elif ch in " \t|;&<>()":
            break
        else:
            word.append(ch)
        i += 1
    return "".join(word), tabs, quoted, i


def _heredoc_bodies(text: str, i: int, heredocs: list) -> tuple:
    """Skip the heredoc bodies that begin at `text[i]`, the first character after the
    newline that ended a command with `<<` in it, and return where the next command
    starts, with the bodies whose delimiter carried no quote. Each body runs to the
    line that is exactly its delimiter (leading tabs aside, for `<<-`); the bodies come
    in the order their operators did, as bash reads them; a body whose delimiter never
    comes runs to the end, so nothing follows it.

    A HEREDOC BODY IS INPUT, NOT A COMMAND, AND IS NOT JUDGED AS ONE. Its lines go to the
    command's stdin, so what they mean is decided by the command that reads them, and
    that command is judged. `cat <<EOF` with `rm -rf /` in the body prints three words.
    `bash <<EOF` would run them, and bash, sh and their kind are refused in every
    ruleset: by name in the developer one, by default everywhere else. `python3 - <<PY`
    runs its body, which is what a developer scope
    allows a runner to do inside a worktree, with the sandbox holding its writes. Judging
    the body as commands instead would refuse every heredoc an agent writes a file or a
    script with, for text that is never executed by the shell.

    THE ONE THING THE SHELL STILL RUNS IN A BODY IS A COMMAND SUBSTITUTION. An
    UNQUOTED delimiter leaves the body a double-quoted context, and bash executes
    `$(…)` and `…` in it while it gathers the body, whatever command reads it:
    `cat <<EOF` with `$(curl … | sh)` in the body was judged as `cat` alone and allowed
    (review, 19 Oct). Those bodies are handed back for `_heredoc_cuts` to judge their
    substitutions; a body whose delimiter was quoted in any part is literal — it runs
    nothing, and is not handed back."""
    bodies, held = [], []
    while heredocs and i < len(text):
        end = text.find("\n", i)
        if end < 0:
            end = len(text)
        line = text[i:end]
        word, tabs, quoted = heredocs[0]
        if (line.lstrip("\t") if tabs else line) == word:
            heredocs.pop(0)
            if held:
                bodies.append("".join(held))
                held = []
        elif not quoted:
            held.append(line + "\n")
        i = end + 1
    if held:
        bodies.append("".join(held))
    return min(i, len(text)), bodies


def _heredoc_cuts(body: str) -> list:
    """The commands an unquoted heredoc body runs, as segments to judge. Bash gathers
    such a body as if it were inside "…": every `$(…)` and `…` in it executes — the
    command reading the body never sees them — while a quoted delimiter writes the
    body literally and runs nothing. Each substitution's text is judged by the same
    `_segments` that judges it inside "…". Escapes are bash's (`\\$` and `` \\` `` are
    literal); arithmetic `$((…))` runs nothing of its own, but a substitution inside
    it still does and is cut here as it is inside "…". A `$(` or backtick that never
    closes is returned whole: no head word of it is on any allowlist, so the body is
    refused."""
    out, i, n = [], 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "\\" and i + 1 < n:
            i += 2                                   # the escaped character runs nothing
        elif ch == "`":
            j = body.find("`", i + 1)
            if j < 0:
                out.append(body[i + 1:])
                return out
            out += _segments(body[i + 1:j])
            out += _heredoc_cuts(body[i + 1:j])
            i = j + 1
        elif body.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if body[j] == "(":
                    depth += 1
                elif body[j] == ")":
                    depth -= 1
                j += 1
            if depth:
                out.append(body[i + 2:])
                return out
            if _arithmetic(body, i):
                # runs nothing of its own; a `$( or backtick inside it still does
                out += _heredoc_cuts(body[i + 3:j - 2])
            else:
                out += _segments(body[i + 2:j - 1])
                out += _heredoc_cuts(body[i + 2:j - 1])
            i = j
        else:
            i += 1
    return out


def _decide_one(seg: str) -> dict:
    for pattern, decision, reason in RULES:
        if re.search(pattern, seg):
            return {"decision": decision, "reason": reason, "matched": pattern}
    head = _first_word(seg)
    if head in SAFE_READS:
        try:
            refused = read_refusal(shlex.split(seg))
        except ValueError:
            refused = "unreadable quoting — failing closed"
        if refused:
            return {"decision": DENY, "reason": refused}
        return {"decision": ALLOW, "reason": f"'{head}' is a read-only tool"}
    return {"decision": DENY,
            "reason": f"'{head or seg[:30]}' is not on the allowlist — this workspace "
                      f"only runs finrail (default deny)"}


def run_needs_confirm(rid: str) -> dict:
    """Whether approving THIS run needs a human decision — read from the capability's own
    `confirm` attribute, so the gate and the capability can never disagree."""
    from . import registry, tracker
    try:
        rec = tracker.load(rid)
        cap = registry.command(rec["capability"])
    except Exception as e:                                     # noqa: BLE001
        return {"decision": ASK, "reason": f"cannot inspect run {rid}: {e}"}
    if cap.confirm:
        return {"decision": ASK, "capability": cap.name,
                "reason": cap.reason or f"{cap.name} requires explicit approval"}
    return {"decision": ALLOW, "capability": cap.name,
            "reason": f"{cap.name} does not require approval"}


def resolve(cmd: str) -> dict:
    """What the hook actually calls: like decide(), but if the answer depends on a run,
    look the run up rather than asking the human about an id they cannot judge."""
    d = decide(cmd)
    if d["decision"] != ASK_RUN:
        return d
    ids = re.findall(r"\b([0-9a-f]{6,8})\b", cmd.split("approve", 1)[-1])
    if not ids:
        return {"decision": ASK, "reason": "approve without a run id"}
    # EVERY id: a batch is approved as a whole, so the strictest run decides.
    for rid in ids:
        d = run_needs_confirm(rid)
        if d["decision"] != ALLOW:
            return d
    return d


# ── the read-only lint ──────────────────────────────────────────────────────
WRITEY = (r"\.write_text\(", r"\.write_bytes\(", r"\bopen\([^)]*['\"][wax]",
          r"\.mkdir\(", r"\.unlink\(", r"\.rmdir\(", r"shutil\.(copy|move|rmtree)",
          r"os\.remove\(", r"os\.rename\(", r"\.replace\(.*Path")


def audit() -> list:
    """Findings, empty when clean."""
    from . import registry
    out = []
    for cap in registry.commands().values():
        r = cap.rules()
        if cap.read_only:
            if type(cap).execute is not type(cap).__mro__[-2].execute:
                pass                      # having an executor is not itself proof
            try:
                src = inspect.getsource(cap.run)
            except (OSError, TypeError):
                src = ""
            hits = [p for p in WRITEY if re.search(p, src)]
            if hits:
                out.append({"capability": cap.name, "problem":
                            "declared read_only but its run() writes",
                            "evidence": hits})
            if cap.confirm and cap.read_only:
                out.append({"capability": cap.name, "problem":
                            "read_only with confirm=True — a query cannot need approval"})
        if cap.autonomous and cap.confirm:
            out.append({"capability": cap.name, "problem":
                        "autonomous=True with confirm=True is contradictory: it would "
                        "both skip and require approval"})
        if not r["reason"] and not cap.read_only and not cap.confirm:
            out.append({"capability": cap.name, "problem":
                        "runs without approval but gives no reason"})
    return out


def export() -> dict:
    """What the hook needs, as data — one source for the gate's behaviour."""
    return {"rules": [{"pattern": p, "decision": d, "reason": r} for p, d, r in RULES],
            "safe_reads": list(SAFE_READS), "default": DENY}
