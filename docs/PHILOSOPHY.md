# Philosophy

Why finrail is shaped the way it is. These are not style preferences — each one was paid
for by a bug, and the code enforces most of them with tests.

---

## 1. Determinism is the whole product

An assistant with a general shell can already read mail and edit files. finrail exists so
that the same request produces the same action every time, and so that what happened is
recorded as itself.

The standard, in the words that set it: *"If Claude Code using finrail sits there having
to guess and run around in circles for a simple request, we aren't deterministic. We are
much worse than without this harness."*

That has a sharp consequence: **an error with no next move is the failure mode to design
out.** Every refusal names its fix, every failing doctor check carries its own command,
and `ask` returns runnable commands even with no model reachable. Judge a change by
whether it removes a reason to guess — not by whether the code is tidier.

## 2. On rails, whatever is driving

Claude Code is one front-end. Tomorrow it might be a different one, a cron, or nothing at
all. So a capability must behave identically no matter what calls it, with any model step
**internal and bounded**.

Concretely: never design a feature that only works because a clever agent is attached and
willing to drive a multi-step loop. When reasoning across several steps is genuinely
needed, build the loop *inside* the capability with a fixed budget — `router.py`'s
`MAX_LOOKUPS` is the template: the model asks for one read, code performs it, the result
comes back, capped at a small number of rounds.

`full-agent` is the deliberate exception, and says so in its own description: *"the one
capability that is not constrained, so it is always asked for explicitly."* That sentence
only makes sense because everything else is not like that.

## 3. Enforcement beats instruction

A rule in a prompt is a request the model can drop — and it drops it exactly when it is
least sure, which is when the rule matters most. A rule in code is a guarantee.

This is why `validate()` exists, why parameters are coerced against a real JSON schema
rather than hoped for, and why lessons **retire when the behaviour they teach becomes
enforced**: anything that can be made structural should become a finding, then a fix, then
a test — and the lesson beside it is then pure prompt weight.

Applied to answers: citations are not requested in a prompt, they are a schema field, and
code checks that each cited document exists, was actually retrieved in that run, and
contains the quoted text.

## 4. Verify the thing you claim, not something next to it

The recurring bug family in this codebase, and the first thing to look for in any new
work. Every instance had every individual component behaving correctly and the overall
report false:

- `mail.delete` verified the message left **the server** while `mail.inbox` reads the
  **local mirror**. Runs said done; the messages were still listed.
- `verify_sent` counted the message in the local index, but the mirror is inbox-only, so
  Sent is never there — every successful send reported unverified.
- `mail.to_vault` verified a non-empty **file** existed; HTML-only mail rendered as
  headers, so the vault filled with husks that looked filed.
- `notmuch show` returns the whole **thread**; `get()` took `out[0]`, so a bounce's id
  resolved to the reply it was bouncing — and a delete proposed destroying the wrong
  message with a receipt that would have verified correctly.
- doctor reported 24/24 green while `ask` failed on every request, because the engine
  check ran `--version`, which succeeds while logged out.
- A cron-fired `docs.scan` deleted all 2,474 indexed documents and **reported success**:
  the root was unreadable, `os.walk` swallows permission errors, and an unreadable tree
  is indistinguishable from an emptied one.
- Extraction "succeeded" with thousands of characters of glyph soup — a PDF font with no
  unicode map — so the document was indexed as noise nobody could search, and never
  queued for a second look.

**The rule:** verification must land on the surface the user actually reads, and must ask
"did I get back what I asked for", not "did the call succeed". Per-component correctness
cannot catch a composition error.

## 5. A wrong answer that looks right is the worst outcome

Worse than an error, worse than nothing. So:

- A failing retrieval leg reports itself as failing rather than quietly returning the
  other leg's results as if they were the whole answer.
- A scan that cannot read its root **fails** instead of pruning.
- Destructive steps run only on evidence they can trust, and refuse to remove most of an
  index in one pass.
- When an answer cannot be evidenced, say so — never hand over something confident and
  unsourced.

## 6. Say what is true, including about your own work

Reports quote what the output actually said. A step that was skipped is named. A run still
pending is called pending. This applies to the system's own messages: `approve` on a
finished run answers *"already done at 14:32 — nothing to approve"*, because the operator's
real question is "is this outstanding", not a quibble about state machines.

## 7. The plugin owns its rules, its infrastructure, and its dependencies

A capability declares `read_only`, `confirm`, `autonomous`, `background`, `reason`,
`session_kind` on itself — one declaration site, and `finrail rules` prints them from
there, so documentation cannot drift from behaviour.

The same holds for what a plugin *needs*: binaries, directories, services, connections,
and Python libraries are declared as `REQUIREMENTS()` by the plugin that needs them.
An install that only does mail carries no PDF parser. Doctor names what is missing and
`provision` installs what it mechanically can.

## 8. Do the smallest thing that works, and let evidence pull you forward

Deliberately not built until something concrete demands it: a vector store, a Postgres
server, a reranking API. The corpus is thousands of documents, not millions, and SQLite
FTS5 answers in milliseconds. The trigger for change is a real failure — conceptual
queries that lexical search misses — not an anticipated one.

Borrowed from the sibling project this idea came from: *"the failure mode of this system
is staleness, not retrieval. Before adding structure, ask what breaks without it."*

## 9. Derived data is disposable; the artefact is not

Whatever cost real money or real judgement is the source of truth and lives in git: vault
pages, extracted document text, lessons. Whatever can be recomputed is disposable and
gitignored: the graph, the search index.

This is a load-bearing distinction, not tidiness — losing `docs.db` costs a reindex
(seconds); losing the sidecars costs re-running a vision model over every scan. It also
means the rules that protect paid work are real code: a rescan must never overwrite a
transcription or a translation with an empty parse.

## 10. Approving is the human's call

Anything consequential stops as a pending run with a full preview of what it will do — not
a summary, the whole proposal, because a preview that shows only the fields its author
remembered to render is how a task got approved carrying a value nobody saw.

`execute()` runs in a **different process** from the one that built the proposal, so the
proposal must be sufficient on its own. State stashed on `self` is gone by then — a test
enforces this, because it silently produced a task with no reference to the email that
caused it.

---

## Reading these as a coding agent

Two habits follow directly:

- **Look at the code before asserting.** Claims about this system are cheap to check and
  expensive to get wrong. One tool call beats a confident sentence.
- **When a bug appears, ask which of these it violates.** It is almost always #4, and the
  fix is almost always to move a check onto the surface being claimed about.
