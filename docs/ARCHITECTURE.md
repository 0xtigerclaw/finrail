# Architecture

How finrail is put together. ~14,000 lines of Python, ~5,300 lines of tests, no runtime
dependencies in core.

For *why* any of this is shaped as it is, see [PHILOSOPHY.md](PHILOSOPHY.md).

---

## The shape in one paragraph

A **dispatcher** turns a command into a **run**. Capabilities are declared by **plugins**,
which also declare their own settings, infrastructure requirements, cron jobs and
connections. Every model call goes through **one seam**, which is where lessons get
injected and schemas enforced. Everything a run does is recorded in a **ledger**. Content
lives in directories the operator chooses; finrail's own state lives somewhere else
entirely; and its source tree is a third root, for output addressed to whoever edits the
code. All three are independent.

```
finrail <verb> <capability> k=v
    │
    ├─ policy.py ........ may this run at all? (the locked-session gate)
    ├─ registry.py ...... which plugin declares it, and with what rules
    ├─ schema.py ........ coerce "true" → True against the declared type
    ├─ dispatch_core.py . plan() → validate() → [approval] → execute()
    │      │
    │      └─ llm.py .... the ONE model seam: lessons in, JSON out
    │
    └─ tracker.py ....... the run ledger: pending → running → done|failed
```

## Three roots, deliberately independent

| | env var | default | holds |
|---|---|---|---|
| **state** | `FINRAIL_STATE` | `~/.finrail` | settings.json, run ledger, lessons, sessions, rules, caches |
| **source** | `source.dir` setting | this install | finrail's own tree — where output for whoever edits the code lands |
| **content** | `FINRAIL_HOME` | `~` | *fallback only* — every content location is its own setting |

A machine with no vault still works. `tasks.dir`, `mail.dir`, `brains`, `docs.roots` are
each set independently; nothing requires them to share a parent, and nothing requires a
folder called "workspace".

State cannot be relocated *by a setting* — reading that setting would first require
finding the settings.json it lives in. That is a plain circularity, not a missing feature,
which is why it is an environment variable.

## The plugin contract

A plugin is a module under `finrail/bundled/` (always on) or `finrail/extensions/`
(can be disabled). It declares, by name:

| declaration | what it does |
|---|---|
| `COMMANDS` | the capability classes |
| `SETTINGS` | `SettingSpec`s — required ones become doctor checks |
| `REQUIREMENTS()` | binaries, directories, config files, services, connections, Python packages |
| `CRONS` | scheduled capability calls |
| `CONNECTION_TYPES` | credentialed endpoints it knows how to test |
| `SERVICES` | long-running processes it needs (launchd) |
| `HOOKS` | functions bound to `before_llm`, `after_run`, `before_write`, `after_write` |

Capabilities declare their own rules as class attributes — `read_only`, `confirm`,
`autonomous`, `background`, `reason`, `session_kind`, `verbatim`, `also_known_as`,
`verified_by`. One declaration site, and `finrail rules` prints from it, so the
documented behaviour cannot drift from the real behaviour.

### Verification is one of those rules

`Plugin.verify(proposal, result) -> (ok, evidence)` is asked by core after `execute()`, at
the one point where side effects are recorded. The default is the three keys core used to
inspect directly — `ok`, `verified`, `success` — so nothing changes for a capability that
does not override it. What overriding buys is that the question is asked of the thing that
knows the answer: `apps.deploy` re-runs the app's own declared checks rather than trusting
the `healthy: true` it just wrote, because a container that came up and fell over a second
later is exactly what a deploy would otherwise call a success.

`verified_by` says how, in the author's words, and `finrail rules` prints it beside the
policy — "what would make you believe this worked?" is a rule with consequences and belongs
where the other rules are. A verification that raises fails the run: a check that could not
run has not verified anything.

## The three kinds of capability

| kind | shape | example |
|---|---|---|
| **read** | `run()` returns data; `render_data()` presents it | `mail.inbox`, `brain.search` |
| **planned** | `plan()` builds a proposal, `execute()` performs it | `mail.delete`, `tasks.add` |
| **judged** | a model fills the proposal against a JSON schema, then the same `validate()`/`execute()` path | `mail.compose`, `ask` |

Judged capabilities are not a separate execution path — they are a planned capability
whose `plan()` happens to consult a model. The approval, validation, verification and
ledger rules are identical.

## The run ledger

`tracker.py`. One JSON record per run under `runs.dir`, plus a directory
`runs/<id>/` for anything the run produces (extracted attachments, rendered pages).

```
pending ──approve──> running ──> done
   │                              └─> failed
   └──drop──> (verdict recorded, then removed)
```

`approve` and `drop` are the only explicit human judgements the system collects, so both
are journalled — a dropped proposal is the operator rejecting a decision, and it is
evidence.

**`execute()` runs in a different process from `plan()`.** Anything stashed on `self`
during planning is gone by the time approval happens. The proposal is the only thing that
survives, and a test enforces it.

## The model seam

Every model call in the system goes through `llm.call()`. That is what makes four separate
things possible without any capability writing code for them:

1. **Lessons injected** — the `before_llm` hook prepends `<state>/lessons/<capability>.md`
2. **Schema enforced** — a malformed answer is a failure, not a half-parsed proposal
3. **The engine is substitutable** — `FINRAIL_CLAUDE_BIN` points at a fake in tests, so
   the entire judged surface is testable with no network
4. **The model is configurable** — `FINRAIL_MODEL`, because an overloaded model must not
   permanently block every judged capability

The engine is the authenticated `claude` CLI in headless mode — no API key to manage.

`full_agent` used to run its own subprocess and silently lost all four; unifying it was a
real fix, not a tidy-up.

## Sessions

Most capabilities are stateless: a command runs start to finish and dies. A capability
that declares `session_kind` is conversational instead — its runs form a session and the
model remembers earlier turns.

**Continuation is delegated to the engine.** A conversation id is assigned when the
session opens (`llm.new_session_id()`); the first turn opens that thread with
`--session-id`, later turns resume it with `--resume`. The engine keeps the real context —
tool use and reasoning included — which replaying a stored transcript could not reproduce.

The transcript in `<state>/sessions/<id>.json` is a **readable record and what the learning
plugin reads. It is not the mechanism.** A session file containing earlier turns is
therefore not by itself evidence that the model remembers them.

`chat`, `full-agent`, `a coding agent` and `full-finrail` declare `session_kind` today — the
full agent's sessions are still filed under the kind `agent`, the name the capability had
before it was renamed. `dns.authorize` declares one too, which is worth knowing: the
attribute makes a capability's runs a conversation, and an agent is the further step of
pairing one with an identity to answer as ([capabilities/agents.md](capabilities/agents.md)).

## Policy, and the locked session

`core/policy.py` decides whether a command may run at all. Its rules are hardcoded on
purpose — it is a security gate, and friction is a feature there.

The lock itself is not finrail's problem: finrail is not locked, the *session calling
it* is. A hook asks `finrail policy <command>` before every Bash command, deny-by-default.
If a lock appears to break finrail internally, the bug is inheritance — finrail standing
in the caller's directory or environment — not the lock.

## Learning

Four stores, four different readers, because they are four different things:

| kind | lands in | read by |
|---|---|---|
| **lesson** | `<state>/lessons/<capability>.md` | injected into that capability's prompt |
| **hint** | `<state>/lessons/caller.md` → managed block in each `memory.guides` file | the driving session, at startup |
| **finding** | its own file in the findings repository (`memory.findings_dir`), outside the codebase | a coding session, which is the only thing that can fix code |
| **journal** | `<state>/journal.jsonl` | `memory.extract`, nightly |

The distinction that made this work: a **deterministic** capability makes no judgement, so
it has no prompt to inject a lesson into. Everything learned about `mail.sync` is an
engineering finding and belongs in the source tree. Before this split, those were written
into lesson files that nothing ever read.

**How it actually works — writing, reading, the cron and the hooks — is its own page:
[LEARNING.md](LEARNING.md).** The unbuilt remainder is in
[dev/proposed/learning.md](../dev/proposed/learning.md).

## Content stores

| store | what it is | source of truth | derived |
|---|---|---|---|
| **vault** (`brain`) | typed markdown pages + a graph | `people/`, `companies/`, `tasks/`, `email/` | `_graph/` |
| **mail** | a local maildir mirror | the server | the notmuch index |
| **documents** (`docs`) | extracted text of a raw file tree | the sidecar repo | `docs.db` |

Each pairs a durable artefact with a disposable index. The mail mirror is **inbox-only and
pull-direction**, so moves and deletes are server-side operations, not local ones.

`brain.search` queries the vault and the document index together and fuses the rankings
with Reciprocal Rank Fusion (`core/rank.py`) — arithmetic over rank positions, no model,
so retrieval stays free of an LLM as legs are added.

## Infrastructure the plugins declare

- **doctor** checks every requirement and verifies things *work* rather than merely exist —
  a green engine check means the engine answered, not that a binary is on PATH.
- **provision** repairs what is mechanically repairable; **setup** walks the rest in
  dependency order and is resumable by nature.
- **cron** is one launchd heartbeat calling `finrail cron tick` every minute, which
  dispatches due jobs **through the normal capability path** — a scheduled action obeys
  the same approval and verification rules as one typed by hand. There is no second,
  privileged execution path.

## Where to look in the code

| you want | look at |
|---|---|
| the capability contract | `finrail/plugin.py` |
| dispatch, approval, verdicts | `finrail/core/dispatch_core.py` |
| the run ledger and artifacts | `finrail/core/tracker.py` |
| the model seam | `finrail/core/llm.py` |
| plugin loading and declarations | `finrail/core/registry.py` |
| the security gate | `finrail/core/policy.py` |
| requirements and doctor | `finrail/core/requirements.py` |
| CLI surface and rendering | `finrail/core/entrypoint.py` |
