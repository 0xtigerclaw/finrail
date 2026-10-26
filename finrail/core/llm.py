"""THE LLM SEAM — every model call in the system goes through here.

One function means one place where:
  * lessons get injected (`before_llm`), so a capability author writes no learning code
  * JSON gets validated against the capability's schema, so a malformed answer is a
    failure and not a half-parsed proposal
  * the engine is substitutable (FINRAIL_CLAUDE_BIN), which is how the whole judged
    surface becomes testable without the network

The engine is the authenticated `claude` CLI in headless mode — no API key to manage,
same account as the interactive session.
"""
import errno
import json
import os
import pathlib
import signal
import subprocess
import uuid

from . import log as _log
from . import progress
from . import tracker
from .errors import ContractViolation, EngineFailed, OpFailed

_logger = _log.get(__name__)

def binary() -> str:
    return os.environ.get("FINRAIL_CLAUDE_BIN", "claude")


#: Whether the installed engine understands `--permission-prompts`. Asked once per
#: process, never guessed.
_SUPPORTS: dict = {}


def supports_flag(flag: str) -> bool:
    """Does the engine on this machine take this option?

    `--permission-prompts none` is how a headless call says "nobody is here, so deny
    rather than hang". Claude Code 2.1.245 does not have it, and passing an option the
    binary does not know is not a degraded call — it is `error: unknown option`, before
    the model is reached. That broke every tool-using call that is NOT the full agent:
    both scoped agents and `docs.extract`, all of them reporting a model failure for a
    reason that had nothing to do with the model.

    So the flag is offered where it exists and dropped where it does not, rather than
    pinning a version of the engine that a person is free to upgrade. Asked by running
    `--help` once and caching the answer: a version check would have to know which
    version introduced it, which is the same guess wearing a number.
    """
    if flag in _SUPPORTS:
        return _SUPPORTS[flag]
    try:
        r = subprocess.run([binary(), "--help"], capture_output=True, text=True,
                           timeout=20)
        _SUPPORTS[flag] = flag in (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        # Unreachable binary is a problem the call itself will report properly, with the
        # cause. It is not this function's to diagnose, and assuming the flag is absent
        # keeps the argv to what every version accepts.
        _SUPPORTS[flag] = False
    return _SUPPORTS[flag]


#: The engine's effort levels (`claude --help`, 2.1.289).
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def default_model() -> str:
    # Overridable for the same reason as the binary: a model that is overloaded or
    # retired must not permanently block every judged capability with no way out short
    # of editing source. `ask` hung for minutes hitting an overloaded claude-opus-5 while
    # the interactive session for the same account had already fallen back to sonnet.
    # Opus 5.5 since 15 Oct 2026: on the same questions it wrote answers about 40% faster
    # than Opus 5, which was most of the gap to the plain Claude Code agents on 5.5.
    return os.environ.get("FINRAIL_MODEL", "claude-opus-5-5")


# ── HTTP engines (the sponsor seam) ──────────────────────────────────────────
# `claude-cli` (the default) is the engine above. An HTTP engine serves the
# JUDGED surface — non-tool, sessionless calls — over an OpenAI-compatible
# chat-completions endpoint, selected by environment only (PRD E2/E5):
#
#   FINRAIL_ENGINE=nebius   Token Factory, model nvidia/nemotron-3-super-120b-a12b
#   FINRAIL_ENGINE=meta     Meta Model API (or a partner endpoint), Muse models
#
# Canonical error codes (PRD E6, adapted from DeepSeek Harness): a provider
# failure is reported under one of these codes, never as a raw provider
# exception dressed up as an answer, and never retried silently.
ERR_CONTEXT = "CONTEXT_WINDOW_EXCEEDED"
ERR_EMPTY = "EMPTY_RESPONSE"
ERR_SCHEMA = "SCHEMA_INVALID"
ERR_PROVIDER = "PROVIDER_UNAVAILABLE"
ERR_TIMEOUT = "TIMEOUT"
ERR_UNSUPPORTED = "ENGINE_UNSUPPORTED"

_ENGINE_DEFAULTS = {
    "nebius": ("https://api.tokenfactory.nebius.com/v1/",
               "nvidia/nemotron-3-super-120b-a12b", "NEBIUS_API_KEY"),
    "meta": ("", "", "FINRAIL_ENGINE_API_KEY"),
}


def engine() -> str:
    """The configured engine: `claude-cli` unless FINRAIL_ENGINE names an HTTP one."""
    return (os.environ.get("FINRAIL_ENGINE") or "claude-cli").strip().lower()


def _http_config(model: str = "") -> tuple:
    """(base_url, api_key, model_id) for the HTTP engine, from environment only."""
    eng = engine()
    base_default, model_default, key_var = _ENGINE_DEFAULTS.get(eng, ("", "", ""))
    base = (os.environ.get("FINRAIL_ENGINE_BASE_URL") or base_default).rstrip("/")
    key = os.environ.get("FINRAIL_ENGINE_API_KEY") or os.environ.get(key_var, "")
    chosen = model or os.environ.get("FINRAIL_MODEL") or model_default
    return base, key, chosen


def _http_call(system: str, prompt_text: str, expect_json: bool, schema,
               capability: str, timeout: int, record: dict, sections: dict,
               model: str = ""):
    """One judged call over HTTP. Stdlib only, a single attempt, usage recorded.

    The full assembled prompt is written to the model_call event (via
    `_log_call`) together with the engine, endpoint host, model and schema
    outcome, so a judged finance call is reconstructable from the ledger
    alone (PRD E7/DA1): what the model saw, what it returned, what it cost.
    """
    import time as _time
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    eng = engine()
    base, key, chosen = _http_config(model)
    if not base or not chosen:
        raise EngineFailed(
            f"{capability or 'llm'}: {ERR_PROVIDER}: engine '{eng}' needs "
            "FINRAIL_ENGINE_BASE_URL and a model (FINRAIL_MODEL)")
    if not key:
        raise EngineFailed(
            f"{capability or 'llm'}: {ERR_PROVIDER}: no API key in the environment "
            "for the HTTP engine (FINRAIL_ENGINE_API_KEY)")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt_text})
    body = {"model": chosen, "messages": messages, "temperature": 0,
            "max_tokens": 8192}
    if expect_json:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    started = _time.time()
    rec = {**(record or {}), "engine": eng, "engine_host": urlparse(base).netloc,
           "engine_model": chosen}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:                                        # noqa: BLE001
            pass
        code = ERR_CONTEXT if e.code in (400, 413) and "context" in detail.lower() \
            else ERR_PROVIDER
        raise EngineFailed(
            f"{capability or 'llm'}: {code}: HTTP engine returned {e.code}: "
            f"{detail}") from e
    except TimeoutError as e:
        raise EngineFailed(
            f"{capability or 'llm'}: {ERR_TIMEOUT}: HTTP engine did not answer "
            f"within {timeout}s") from e
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        code = ERR_TIMEOUT if "timed out" in reason.lower() else ERR_PROVIDER
        raise EngineFailed(
            f"{capability or 'llm'}: {code}: HTTP engine unreachable: "
            f"{reason[:200]}") from e
    choices = payload.get("choices") or []
    text = ""
    if choices:
        text = (choices[0].get("message") or {}).get("content") or ""
    usage = payload.get("usage") or {}
    _LAST["result"] = {"usage": {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0)}}
    _log_call(capability, chosen, "", False, system, prompt_text, text, 0,
              started, rec, sections)
    if not text.strip():
        raise EngineFailed(f"{capability or 'llm'}: {ERR_EMPTY}: the HTTP engine "
                           "returned no content")
    if not expect_json:
        return text.strip()
    try:
        return check_schema(_extract_json(text), schema)
    except OpFailed as e:
        raise OpFailed(f"{capability or 'llm'}: {ERR_SCHEMA}: {e}") from e


def _blocks(context: dict) -> str:
    """Context as stable, labelled blocks. Static blocks are emitted first by the caller's
    ordering, so the prompt prefix stays byte-identical between runs (cacheable)."""
    out = []
    for k, v in (context or {}).items():
        body = v if isinstance(v, str) else json.dumps(v, indent=2, default=str)
        out.append(f"<{k}>\n{body}\n</{k}>")
    return "\n\n".join(out)


def _extract_json(text: str):
    """Pull the JSON object out of a model reply. A reply we cannot parse is a failure —
    never a partially-understood proposal."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s.lstrip("`")
        s = s.split("\n", 1)[1] if s.lower().startswith(("json\n", "json\r")) else s
    start = s.find("{")
    if start == -1:
        raise OpFailed(f"model returned no JSON object: {text[:300]}")

    # THE FIRST BALANCED OBJECT, not everything between the first brace and the
    # last. A reply that carries the object and then anything else with a brace
    # in it — a second fenced block, a sentence of explanation, a worked example
    # — used to be sliced into one unparseable span and rejected as invalid
    # JSON. The object was there and perfectly good; the slice was wrong.
    # Cost a drafting run on emergeits, 2026-09-29: "Extra data: line 57".
    first = _balanced(s, start)
    for candidate in (x for x in (first, s[start:s.rfind("}") + 1]) if x):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    end = s.rfind("}")
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError as e:
        raise OpFailed(f"model returned invalid JSON ({e}): {text[:300]}")


def _balanced(s: str, start: int):
    """The first complete `{...}` from `start`, counting depth outside strings."""
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def fit_schema(obj: dict, raw_schema) -> dict:
    """Coerce values to the shapes the schema declares, before checking them.

    A parameter arrives from the CLI as a string, always — `to=ana@example.com` — while
    the schema for a recipient list says array. Rejecting that as a type error asks the
    person typing it to know the internal shape of a field, and the message they get
    ("field 'to' should be array, got str") tells them nothing about what to type instead.

    Only widening conversions: a scalar becomes a one-element list, a comma-separated
    string becomes several. Nothing is narrowed, so a genuine mismatch still fails.
    """
    if not raw_schema:
        return obj
    try:
        spec = json.loads(raw_schema) if isinstance(raw_schema, str) else dict(raw_schema)
    except (json.JSONDecodeError, TypeError):
        return obj
    for key, meta in (spec.get("properties") or {}).items():
        if key not in obj or not isinstance(meta, dict):
            continue
        want, val = meta.get("type"), obj[key]
        if not isinstance(val, str):
            continue
        text = val.strip()
        if want == "array":
            obj[key] = [part.strip() for part in text.split(",") if part.strip()]
        elif want == "boolean":
            # `now=true` is how a person writes a flag on a command line, and it is the
            # form the help text shows. Rejecting it with "field 'now' should be boolean,
            # got str" is the system describing its own internals to someone who typed
            # exactly what they were told to.
            if text.lower() in ("true", "yes", "1", "on"):
                obj[key] = True
            elif text.lower() in ("false", "no", "0", "off"):
                obj[key] = False
        elif want in ("integer", "number") and text:
            try:
                obj[key] = int(text) if want == "integer" else float(text)
            except ValueError:
                pass                       # leave it; the check below reports it properly
    return obj


def check_schema(obj: dict, raw_schema) -> dict:
    """Enforce required keys from the capability's declared schema. Stdlib only: this is
    a required-keys and type-shape check, not a full JSON-Schema implementation."""
    if not raw_schema:
        return obj
    try:
        spec = json.loads(raw_schema) if isinstance(raw_schema, str) else dict(raw_schema)
    except (json.JSONDecodeError, TypeError):
        return obj
    missing = [k for k in spec.get("required", []) if k not in obj]
    if missing:
        raise OpFailed(f"model omitted required field(s): {', '.join(missing)}")
    props = spec.get("properties") or {}
    required = set(spec.get("required") or [])
    for key, meta in props.items():
        if key not in obj or not isinstance(meta, dict):
            continue
        if obj[key] is None and key not in required:
            # `null` on an OPTIONAL field is the model saying "there isn't one", which is
            # exactly what it should say when an email carries no due date. Treating that
            # as a type error rejected a correct answer — and the capability then failed
            # with "field 'due' should be string, got NoneType", which reads like a bug in
            # the model rather than a rule that was too strict. Drop it and move on.
            obj.pop(key)
            continue
        want = meta.get("type")
        val = obj[key]
        py = {"string": str, "integer": int, "number": (int, float),
              "boolean": bool, "array": list, "object": dict}.get(want)
        if py and not isinstance(val, py):
            raise OpFailed(f"field '{key}' should be {want}, got {type(val).__name__}")
    return obj


#: Variables through which a CALLING Claude Code session describes itself to its child
#: processes. Inherited, they make finrail's own model call behave as a continuation of
#: that session instead of as its own thing. `CLAUDE_CONFIG_DIR` is deliberately NOT here:
#: that is where credentials live, and dropping it would break authentication.
_CALLER_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PROJECT_DIR",
                "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_SETTINGS")
#: What the door told this process about its caller (core/caller.py). It is this
#: process's, not the engine's: an engine a transport's turn started is not the
#: transport, and must not be able to answer a question as if it were. The check line
#: (FINRAIL_SOCKET) goes with the token it checks: in a run it is the door's private
#: whois line, and the chat client reads that same name as the door to send runs to,
#: so a client started from the engine's shell sent every run to a line that answers
#: whois only (review, 5 Oct). The engine's own commands find the door by FINRAIL_DOOR.
_DOOR_VARS = ("FINRAIL_CALLER", "FINRAIL_CALLER_TOKEN", "FINRAIL_IN_DOOR",
              "FINRAIL_SOCKET")


def permission_mode() -> str:
    """How a tool-using call is permitted. `auto` unless someone says otherwise."""
    from .settings import raw_location
    return str(raw_location("llm.permission_mode") or "auto")


def bare() -> list:
    """The options that give a call NO TOOLS AT ALL, where the engine has them.

    `rails_dir` below says "the engine is always OFFERED tools", and that was
    measured — against `--allowedTools`, which is a permission list. `--tools`
    is the exposure list, and `--tools ""` with `--strict-mcp-config` is an
    engine that holds nothing: 0 tools and 0 MCP servers in its own init event,
    against 75 and 5 without them (measured here, 2026-10-13).

    WHAT A TOOL NOBODY ASKED FOR COSTS. A caller that passes neither `tools` nor
    `rails` wants an answer from what it put in the prompt. Holding a shell
    anyway, a drafter that was (by a separate fault) shown none of its documents
    went and found some: the run ledger, other runs' drafts, the portal
    snapshots, other agents' sessions — 57 tool calls and 5.5M cached tokens on
    ONE ask of a search fund's `terms`, a build of 30 to 131 minutes, and
    figures cited to documents it had read as excerpts of a ledger file. The
    fault that sent it looking was mended in the plugin; this is what makes
    looking impossible, for every caller at once.

    AND IT IS CHEAPER FOR EVERY ONE OF THEM. The tool definitions are most of a
    small call: the same one-word answer cost about 44,000 tokens of prompt
    with them and about 10,000 without.

    `--strict-mcp-config` goes LAST, and that is load-bearing: `--tools` takes
    a list, and with nothing after it the next argument — the prompt — is read
    as a tool name and the engine waits on stdin for a prompt that never comes.

    `llm.bare = false` puts the tools back for a tool-less call, for the day a
    capability turns out to have been leaning on one without saying so.
    """
    from .settings import raw_location
    if str(raw_location("llm.bare") or "").strip().lower() in ("false", "no", "0", "off"):
        return []
    if not (supports_flag("--tools") and supports_flag("--strict-mcp-config")):
        return []
    return ["--tools", "", "--strict-mcp-config"]


def rails_dir() -> pathlib.Path:
    """A directory whose only Claude Code configuration is finrail's own gate.

    The engine is always OFFERED tools — `--allowedTools` is a permission allowlist, not
    an exposure filter, and the tool list is identical with or without it (68 either way,
    measured). So a call that must not run commands cannot be given a smaller world; what
    it can be given is a world where the commands it tries are judged by the same policy
    a locked session uses.

    Only the PreToolUse gate lives here. The caller-log hooks must NOT: they key on a
    session id and write `[user]` turns, so an engine subprocess logged through them
    records finrail's own prompts as though a person had typed them — which happened
    once already, leaving `[user] say ok` in the caller log.
    """
    from .settings import state
    d = state() / "engine" / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    # INSIDE THE PACKAGE. See `scope._gates_dir`: resolved through `source_dir()` this
    # was absent from every copied install, and the gate fails closed.
    from .scope import _gates_dir
    gate = _gates_dir() / "finrail_gate.py"
    # BOTH, because each covers what the other cannot. The gate matches Bash and mcp__.*
    # and nothing else; measured here, a settings file carrying hooks and NO deny list
    # left Write, Edit, WebFetch, WebSearch and Task fully available — asked to save a
    # file with every bash command refused, the model simply used Write, and the file was
    # created. A gate on one tool is not a gate.
    want = json.dumps({
        "permissions": {"deny": ["Write", "Edit", "NotebookEdit",
                                 "WebFetch", "WebSearch", "Task"]},
        "hooks": {"PreToolUse": [
            {"matcher": m, "hooks": [{"type": "command",
                                      "command": f"/usr/bin/python3 {gate}"}]}
            for m in ("Bash", "mcp__.*")]}}, indent=2)
    f = d / "settings.json"
    if not f.is_file() or f.read_text() != want:
        f.write_text(want)                    # regenerated, so a moved source tree heals
    return d.parent


def source_dir() -> pathlib.Path:
    from .registry import settings as declared
    try:
        v = declared().get("source.dir")
    except Exception:                                              # noqa: BLE001
        v = None
    if v:
        return pathlib.Path(str(v)).expanduser()
    return pathlib.Path(__file__).resolve().parents[2]


def _isolation(cwd: str = "", extra: dict = None) -> dict:
    """Run the engine as finrail's own subprocess, not as a child of whoever called.

    The Claude CLI discovers project settings from its working directory. Inheriting the
    caller's meant that when finrail was invoked from a LOCKED session, the engine
    started inside that session's folder, read its `.claude/settings.json` — the lock —
    and failed with "Ignoring 11 permissions.allow entries". So `finrail ask` was broken
    precisely in the sessions that most need it, and the reported cause pointed at
    permissions rather than at working directory.

    finrail is not the locked thing; the session calling it is. Running from the state
    directory (which holds no `.claude/`) keeps that true. User-level config and
    credentials still apply — only the caller's project context is dropped.
    """
    from . import settings as _settings
    from .requirements import path_env
    env = {k: v for k, v in os.environ.items()
           if k not in _CALLER_VARS and k not in _DOOR_VARS}
    env["PATH"] = path_env()
    # Layered onto the COPY. `extra` is how a caller hands one subprocess something — a
    # turn's context — without writing it into this process's own environment, which a
    # long-running harness shares with every turn after this one.
    env.update({str(k): str(v) for k, v in (extra or {}).items()})
    if cwd:
        # A capability that works IN a directory (the agent) must run there. Stripping the
        # caller vars still matters, and matters more: the engine will read that
        # directory's `.claude/settings.json`, which for a locked workspace is the lock.
        return {"env": env, "cwd": str(cwd)}
    home = _settings.state()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"env": env}
    return {"env": env, "cwd": str(home)}


#: Engine chatter that is NOT a failure. These appear on stderr while the real cause sits
#: on stdout, so reporting "stderr, or stdout if empty" reliably surfaced the noise and
#: discarded the reason — a routing failure caused by an expired login was reported as
#: "Ignoring 11 permissions.allow entries", sending the reader after the wrong problem.
_NOISE_MARKERS = ("permissions.allow entries", "hasTrustDialogAccepted",
                  "has not been trusted", "Warning:", "DeprecationWarning")


def _engine_error(stdout: str, stderr: str) -> str:
    """The engine's actual complaint — both streams, warnings dropped, cause preferred.

    A wrong diagnosis is worse than a vague one: it is what turns one failed command into
    a session of chasing the wrong thing.

    Stream events are read, not quoted. A `system` event is the engine's lifecycle (its
    start), never a cause: on 5 Oct the error was 335 characters of the start event's
    list of tools, cut at 300, and the real cause never showed. A failed `result` event
    is the engine's own account of what went wrong, so its text is the answer.
    """
    lines, said, started = [], "", False
    for ln in ((stderr or "") + "\n" + (stdout or "")).splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            event = json.loads(ln) if ln.startswith("{") else {}
        except json.JSONDecodeError:
            event = {}
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "system":
            started = True
            continue
        if kind == "result" and (event.get("is_error") or event.get("subtype") != "success"):
            said = (str(event.get("result") or "").strip()
                    or "; ".join(str(e) for e in event.get("errors") or []) or said)
            continue
        lines.append(ln)
    if said:
        return said[:300]
    real = [ln for ln in lines if not any(m in ln for m in _NOISE_MARKERS)]
    text = " | ".join(real or lines) or (
        "the engine started, then stopped without saying why" if started
        else "no output from the engine")
    return text[:300]


def new_session_id() -> str:
    """A conversation id we CHOOSE, rather than one we have to read back.

    The engine accepts `--session-id <uuid>` to name a new conversation and `--resume
    <uuid>` to continue it. Assigning the id up front means continuation needs no parsing
    of the engine's output and no extra round trip: the id exists before the first call,
    so it can be stored with the session record immediately and cannot be lost if that
    first call fails partway.
    """
    return str(uuid.uuid4())


def _describe(event: dict, continuing: bool = False) -> tuple:
    """One stream event -> (kind, text) worth showing, or None.

    Deliberately terse. This is a progress line, not a transcript: "Bash: docker ps" tells
    you the thing is alive and what it reached for, and the full record is the result.
    """
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        # "started" or "resumed" — the distinction people actually ask about.
        # Every turn spawns a new process against the SAME conversation, so this
        # event fires each time with the same id; reading it as "started" made a
        # healthy resume look like the conversation had been thrown away and
        # restarted, which is the one thing it must not look like. The event
        # cannot tell us which flag was used, so the caller says.
        verb = "resumed" if continuing else "started"
        return ("engine", f"session {str(event.get('session_id', ''))[:8]} {verb}")
    if kind == "assistant":
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "thinking":
                # Reported as its own kind so a reader can show reasoning separately
                # from what the agent SAID.
                #
                # THE TEXT IS ALWAYS EMPTY IN PRINT MODE, and this is the engine, not
                # us. Measured on this machine: a thinking block arrives as
                # {"type": "thinking", "thinking": "", "signature": "CAISyAMK…"} — the
                # reasoning is not surfaced through `-p`, only a signature attesting
                # that it happened. Chasing the missing text is chasing something that
                # is not sent, so it is written down here to stop the next person
                # looking.
                #
                # An empty block is still reported rather than dropped, because
                # "thinking happened, content withheld" is a real and useful thing to
                # show — and because whether none arrive or they arrive empty is the
                # first thing anyone debugging this needs to tell apart.
                # REPORTED EMPTY, not explained. Saying "(reasoning not exposed by the
                # engine)" was accurate and unreadable: the engine emits one of these per
                # assistant block, so a working turn filled with the same sentence over
                # and over, which looks far more like a loop than blankness ever did.
                #
                # An empty thinking step is still a step. Whoever renders shows that it
                # happened — a beat, an icon — and the reason it carries no words is
                # written down here, once, where someone looking for the missing text
                # will find it.
                return ("thinking", (block.get("thinking") or "").strip())
            if block.get("type") == "tool_use":
                arg = block.get("input") or {}
                detail = (arg.get("command") or arg.get("file_path") or arg.get("pattern")
                          or arg.get("query") or "")
                return ("tool", f"{block.get('name', 'tool')}: {str(detail)[:120]}")
            if block.get("type") == "text" and (block.get("text") or "").strip():
                return ("says", block["text"].strip())
    if kind == "user":
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_result":
                body = block.get("content")
                if isinstance(body, list):
                    body = " ".join(b.get("text", "") for b in body
                                    if isinstance(b, dict))
                return ("result", str(body or "")[:120])
    return None


#: The last `result` event the engine reported in this process, for the event log, and
#: the meter that timed the call's rounds.
_LAST: dict = {}


def _log_thread() -> tuple:
    """(thread, run) the current model call is logged under."""
    import os as _os
    from . import events, tracker
    rid = tracker.current()
    thread = ""
    if rid:
        try:
            thread = events.thread_of_run(tracker.load(rid))
        except Exception:                                        # noqa: BLE001
            thread = ""
    return thread or _os.environ.get("FINRAIL_THREAD") or events.LEDGER, rid


def _context_tokens(usage: dict) -> int:
    """How much the model was shown for one request: fresh input plus the cached part."""
    u = usage or {}
    return sum(int(u.get(k) or 0) for k in
               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


#: How much of one tool call's input the event log keeps.
INPUT_MAX = 8000


class _Meter:
    """Times one engine call ROUND BY ROUND, from the stream it is already reading.

    A round is one message from the model (its id), which may hold several tool calls;
    the engine runs a tool as soon as its block is complete, so model and tool time
    overlap inside a round. So each round is measured as wall time and tool time, and
    the model's share is the difference. Per round the stream carries the input side of
    usage (what the model was shown); output tokens arrive only in the final result,
    which is why they are on the `turn` event and not here (measured 2026-10-07: the
    per-message output count in the stream is a placeholder, 1 to 6 tokens).

    Never raises into the stream: a meter that fails just stops measuring."""

    def __init__(self, started: float):
        self.started = started
        self.mark = started
        self.rounds = []
        self.cur = None
        self.pending = {}
        self.tool_calls = self.errors = self.refused = 0
        self.tool_s = 0.0
        self.peak_context = 0

    def see(self, event: dict, now: float) -> dict:
        """Account for one stream event; return the fields its progress step carries."""
        try:
            return self._see(event, now)
        except Exception:                                        # noqa: BLE001
            return {}

    def _see(self, event: dict, now: float) -> dict:
        kind = event.get("type")
        if kind == "assistant":
            msg = event.get("message") or {}
            mid = msg.get("id") or ""
            if self.cur is None or mid != self.cur["id"]:
                self.close(now)
                self.cur = {"id": mid, "start": self.mark, "end": now, "tools": [],
                            "tool_s": 0.0, "usage": {}}
            self.cur["end"] = now
            if msg.get("usage"):
                self.cur["usage"] = msg["usage"]
            out = {}
            for block in msg.get("content") or []:
                if block.get("type") == "tool_use":
                    name = block.get("name") or "tool"
                    self.pending[block.get("id")] = (name, now)
                    self.cur["tools"].append(name)
                    self.tool_calls += 1
                    # The WHOLE call, for the log: the progress line keeps 120 characters,
                    # which cut every `example.engine.document uri=...` before its find= and
                    # left "did it read a window or the whole document" unanswerable (7 Oct).
                    arg = block.get("input") or {}
                    whole = (arg.get("command") or arg.get("file_path") or arg.get("pattern")
                             or arg.get("query") or "")
                    out = out or {"tool": name, "input": str(whole)[:INPUT_MAX]}
            return out
        if kind == "user":
            out = {}
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                body = block.get("content")
                if isinstance(body, list):
                    body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
                body = str(body or "")
                name, at = self.pending.pop(block.get("tool_use_id"), (None, None))
                secs = max(0.0, now - at) if at is not None else 0.0
                self.tool_s += secs
                if self.cur is not None:
                    self.cur["tool_s"] += secs
                    self.cur["end"] = now
                fields = {"tool": name, "bytes": len(body.encode("utf-8", "replace"))}
                if at is not None:
                    fields["ms"] = int(secs * 1000)
                if block.get("is_error"):
                    self.errors += 1
                    fields["error"] = True
                    # A gate's refusal reaches the model as a failed tool call whose text
                    # names the hook; counted apart, because it is the rails saying no,
                    # not the tool breaking.
                    if "hook error" in body or "hook denied" in body.lower():
                        self.refused += 1
                        fields["refused"] = True
                out = out or {k: v for k, v in fields.items() if v is not None}
            return out
        if kind == "result":
            self.close(now)
        return {}

    def close(self, now: float) -> None:
        """Finish the round in progress and log it."""
        cur, self.cur = self.cur, None
        if cur is None:
            return
        wall = max(0.0, cur["end"] - cur["start"])
        context = _context_tokens(cur["usage"])
        self.peak_context = max(self.peak_context, context)
        row = {"n": len(self.rounds) + 1, "seconds": round(wall, 2),
               "model_s": round(max(0.0, wall - cur["tool_s"]), 2),
               "tool_s": round(cur["tool_s"], 2), "tools": cur["tools"],
               "context_tokens": context,
               "cache_read": int(cur["usage"].get("cache_read_input_tokens") or 0),
               "cache_write": int(cur["usage"].get("cache_creation_input_tokens") or 0)}
        self.rounds.append(row)
        self.mark = cur["end"]
        try:
            from . import events
            thread, rid = _log_thread()
            events.append(thread, "model_round", row, run=rid)
        except Exception:                                        # noqa: BLE001
            pass


def _log_call(capability: str, model: str, session: str, resume: bool, system: str,
              prompt: str, answer: str, code, started: float, record: dict = None,
              sections: dict = None) -> None:
    """One `model_call` event: what the model was shown, in full, and what it reported;
    then one `turn` event that sums the call up. The rule is DeepSeek Harness's: if the
    model saw it, it is a record. Never raises.

    The system prompt is stored ONCE under its digest (events.put_blob) and the event
    names it: it is the same text on nearly every call, and its copies were most of the
    log. The cost is THIS call's, not the engine session's running total
    (events.call_cost). `record` is what a `before_llm` plugin asked to have said about the
    call; its fields go on the `model_call` event and never replace the call's own."""
    try:
        import time as _time
        from . import events
        thread, rid = _log_thread()
        res = _LAST.pop("result", None) or {}
        meter = _LAST.pop("meter", None)
        engine_session = res.get("session_id") or ""
        cost, session_cost = events.call_cost(engine_session, res.get("total_cost_usd"))
        sha = events.put_blob(system or "") if system else ""
        wall_ms = int((_time.time() - started) * 1000)
        data = {"capability": capability, "model": model or "", "session": session or "",
                "resume": bool(resume), "prompt": prompt or "",
                "answer": answer or "", "exit": code,
                "engine_session": engine_session,
                "usage": res.get("usage") or {}, "cost_usd": cost,
                "session_cost_usd": session_cost,
                "duration_ms": res.get("duration_ms") or wall_ms}
        data = {**(record if isinstance(record, dict) else {}), **data}
        if sections:
            # The context object by section, as sizes: what the call was assembled from,
            # readable without the prompt text (`finrail get memory.context`).
            data["sections"] = dict(sections)
        if sha:
            data.update(system_sha=sha, system_chars=len(system))
        else:
            data["system"] = system or ""
        events.append(thread, "model_call", data, run=rid)
        usage = res.get("usage") or {}
        rounds = meter.rounds if meter else []
        tool_s = round(meter.tool_s, 2) if meter else 0.0
        wall_s = round(wall_ms / 1000, 2)
        events.append(thread, "turn", {
            "capability": capability, "model": model or "",
            "seconds": wall_s, "model_s": round(max(0.0, wall_s - tool_s), 2),
            "tool_s": tool_s, "rounds": len(rounds),
            "tool_calls": meter.tool_calls if meter else 0,
            "tool_errors": meter.errors if meter else 0,
            "refused": meter.refused if meter else 0,
            "slowest_round_s": max((r["seconds"] for r in rounds), default=0),
            "peak_context_tokens": meter.peak_context if meter else 0,
            "output_tokens": int(usage.get("output_tokens") or 0),
            "thinking_tokens": int((usage.get("output_tokens_details") or {})
                                   .get("thinking_tokens") or 0),
            "cost_usd": cost, "exit": code}, run=rid)
    except Exception:                                            # noqa: BLE001
        pass


# ── how big an engine conversation has grown ────────────────────────────────
def _sizes_dir():
    from .settings import state
    return state() / "engine" / "sizes"


def _safe(session: str) -> str:
    import re as _re
    return _re.sub(r"[^A-Za-z0-9._-]", "_", str(session or ""))[:120]


def _remember_size(session: str, meter) -> None:
    """The context the engine re-reads for this conversation, as of its last round.

    A resumed conversation is re-read whole on every model round, so its size is what a
    turn costs and how long it takes. Kept per engine conversation, outside the event log
    (which an install may switch off), for `sessions.open_or_resume` to decide when a
    conversation continues in a fresh one. Never raises."""
    try:
        safe = _safe(session)
        rounds = getattr(meter, "rounds", None) or []
        tokens = int(rounds[-1].get("context_tokens") or 0) if rounds else 0
        if not safe or not tokens:
            return
        d = _sizes_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{safe}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"tokens": tokens}))
        os.replace(tmp, d / f"{safe}.json")
    except Exception:                                            # noqa: BLE001
        pass


def context_size(session: str) -> int:
    """Tokens the engine re-read on this conversation's last round, or 0 if unknown."""
    try:
        return int(json.loads((_sizes_dir() / f"{_safe(session)}.json").read_text())
                   .get("tokens") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


#: When to stop putting the prompt in argv. Linux's MAX_ARG_STRLEN is 128 KiB
#: for one element; this leaves room rather than sitting on the edge of it.
ARG_LIMIT = 96 * 1024

#: The most the engine reads from stdin. Measured in the CLI (2.1.281): past 10,485,760
#: characters it stops with "piped stdin input exceeds 10MB", whatever the prompt says.
#: Counted here in bytes, which is never less, so a prompt that passes is one it reads.
STDIN_LIMIT = 10 * 1024 * 1024


def _stream(argv: list, prompt: str, timeout: int, isolation: dict,
            quiet_text: bool = False) -> tuple:
    """Run the engine and report as it goes. Returns (text, stdout, stderr, code).

    Reads line-delimited JSON events so a step can be emitted WHILE the call runs — the
    whole point. If the output is not stream-json (a fake engine in the tests, an older
    CLI), every line simply fails to parse and the accumulated stdout is returned as
    before: the streaming path degrades to the previous behaviour rather than breaking
    the callers that depend on it.
    """
    import threading
    # Read from argv rather than taking it as an argument: this is what was
    # ACTUALLY passed to the engine, so the progress line cannot disagree with
    # the invocation it is describing.
    continuing = "--resume" in argv
    # ITS OWN SESSION, so the engine leads a process group. Two things depend on that.
    # A stop has to reach the engine AND everything the engine started — the agent's own
    # shell commands are its children, and signalling one pid left every one of them
    # running; and the timeout below had the same hole, killing the engine process while a
    # build or a sleep it had spawned carried on holding the pipe open.
    # THE PROMPT IS DATA, AND ABOVE A CERTAIN SIZE IT CANNOT BE AN ARGUMENT.
    # Linux caps a SINGLE argv element at MAX_ARG_STRLEN — 128 KiB, and unlike
    # ARG_MAX it is not configurable. A quarterly pass that hands the model two
    # investor reports in full reaches that on an ordinary company, and the
    # failure is `[Errno 7] Argument list too long: 'claude'` at spawn, which
    # names neither the prompt nor its size. Over the threshold the prompt goes
    # down stdin, which the CLI reads when no positional prompt is given.
    # Under it nothing changes, so every call that works today still takes the
    # same path.
    size = len(prompt.encode("utf-8", "replace"))
    piped = size > ARG_LIMIT
    # TOO BIG IS THE INPUT'S FAULT, NOT THE ENGINE'S. Refused here, before anything starts,
    # as a plain failure: sent, the engine refuses it at once and exits non-zero, which
    # reads as an engine failure, and a caller that retries engine failures sends the same
    # prompt every night (4 Oct: one batch of 10,751,936 characters).
    if size > STDIN_LIMIT:
        mb = 1024 * 1024
        raise OpFailed(f"the prompt is {size / mb:.1f} MB; the engine takes at most "
                       f"{STDIN_LIMIT // mb} MB")
    # An engine with a host to prompt (`questions`) reads its answers from stdin. Nobody
    # writes them, so stdin must be CLOSED: an inherited pipe nobody closes would leave a
    # prompt waiting until the turn's timeout instead of failing at once.
    hosted = "--permission-prompt-tool" in argv
    proc = subprocess.Popen(argv if piped else argv + [prompt],
                            stdin=(subprocess.PIPE if piped
                                   else subprocess.DEVNULL if hosted else None),
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            start_new_session=True, **isolation)
    if piped:
        try:
            proc.stdin.write(prompt)
        except BrokenPipeError:            # the engine died before reading it
            pass
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
    # RECORDED AGAINST THE RUN, which is the only reason `kill` can find this process at
    # all. Best effort on purpose: a call made outside a dispatched run has no run to
    # write to, and that is the ordinary case for a direct `llm.call`.
    rid = tracker.current()
    if rid:
        try:
            from . import caller as _caller
            tracker.update(rid, engine_pid=proc.pid, engine_started=_caller.started(proc.pid))
        except Exception:                                          # noqa: BLE001
            _logger.debug("could not record engine pid for run %s", rid)

    def _expire():
        # The GROUP, not the process, for the reason above.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
    killer = threading.Timer(timeout, _expire)
    killer.start()
    final, saw_events, out = "", False, []
    import time as _time
    meter = _LAST["meter"] = _Meter(_time.time())
    try:
        for line in proc.stdout:
            out.append(line)
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # Expected for the final partial line while the engine is still
                # writing, and for a non-stream-json engine — but if EVERY line
                # fails, the caller sees a turn with no progress and no reason.
                _logger.debug("unparseable engine line: %.120s", line)
                continue
            # A stream EVENT, not merely a JSON object. A reply that is itself one
            # object — which is what every judged capability returns, and what the test
            # engine prints — parses perfectly and is not an event; treating it as one
            # swallowed the reply and returned nothing.
            if event.get("type") not in ("system", "assistant", "user", "result"):
                continue
            saw_events = True
            if event.get("type") == "result":
                final = event.get("result") or final
                # The engine's own account of the call (its session, usage, cost), kept
                # for the event log; the text above is still what the caller gets.
                _LAST["result"] = event
            fields = meter.see(event, _time.time())
            step = _describe(event, continuing=continuing)
            # For a judged call the model's text IS the answer, and repeating it as a
            # progress line duplicates the result in the ledger without adding anything.
            # Tool use and lifecycle still report: those are what "still alive" looks like.
            if step and quiet_text and step[0] == "says":
                step = None
            if step:
                progress.emit(*step, **(fields if step[0] in ("tool", "result") else {}))
        code = proc.wait()
    finally:
        killer.cancel()
    stderr = proc.stderr.read() if proc.stderr else ""
    stdout = "".join(out)
    if not killer.is_alive() and code is not None and code < 0:
        raise EngineFailed(f"model call timed out after {timeout}s")
    return (final if saw_events else stdout), stdout, stderr, code


def call(system: str = "", prompt: str = "", context: dict = None,
         schema=None, capability: str = "", session: str = "", resume: bool = True,
         expect_json: bool = True, timeout: int = 300, cwd: str = "",
         tools: bool = False, model: str = "", wants=None, rails: bool = False,
         dangerous: bool = False, env: dict = None, person: str = "", activity: str = "",
         questions: bool = False, ask_person: bool = False, effort: str = ""):
    """Run one model call. Returns a dict when expect_json, else text.

    `person` and `activity` say who the turn is for and what kind of work is in effect, and
    are handed to `before_llm` with the capability, so what is added to the prompt can be
    what applies to this turn rather than to every turn of the capability.

    `session` is an engine conversation id. `resume=False` STARTS that conversation under
    the given id; `resume=True` continues it. Getting this wrong is not cosmetic —
    resuming an id the engine has never seen fails, and starting one twice discards the
    first turn — so the caller tracks which it is (see `sessions.open_or_resume`).

    `dangerous` skips the engine's permission system entirely. Exactly one caller may
    pass it — the full agent, which is unconstrained by its own explicit, confirmed
    declaration. Nothing else may, and a test says so: every other tool-using call is
    gated, and a SCOPED agent's whole boundary is the hooks and deny list that
    `--dangerously-skip-permissions` ignores.

    `questions` offers the engine its own question tool (AskUserQuestion), for a scoped
    agent whose gate puts the question to the person (`scope.clarify`). The engine
    offers that tool in `-p` only when it has a host to answer it, which is what
    `--permission-prompt-tool stdio` says; the gate answers before the host is reached.
    Measured 5 Oct (2.1.281): with stdin closed, anything else that would prompt the
    host fails at once ("Stream closed"), as `--permission-prompts none` denies it.

    `ask_person` runs a tool-using call in the engine's permission mode with its
    permission dialogs put to the person in the chat (`scope.ask_settings`), instead of
    denied unseen. For the full agent's trial of asking instead of bypassing.

    `env` is variables to set for the engine and everything it starts, over the isolated
    environment — how a scoped agent's turn hands its context (`core/context.py`) to the
    commands the agent runs. It never touches `os.environ`.

    `cwd`, `tools` and `model` are the only other things that legitimately vary between
    callers.
    The full agent used to run its OWN subprocess to get them, and in doing so quietly
    lost everything this function does: the substitutable binary (so it ignored
    FINRAIL_CLAUDE_BIN and could not be tested against the fake), the pinned model, the
    caller-variable stripping, and the error handling that reports the cause rather than
    the first warning on stderr. Same mechanism, four silent differences — which is what a
    second copy of a subprocess call buys you.
    """
    from . import registry
    ctx = dict(context or {})

    # the injection point: a plugin's mere presence can add to any prompt
    payload = registry.hooks().emit(
        "before_llm", {"capability": capability, "system": system, "context": ctx,
                       "person": person or "", "activity": activity or "",
                       # Whether the engine already holds this conversation: what was
                       # added to an earlier turn of it is still in its history.
                       "resume": bool(session and resume), "session": session or ""})
    system = payload.get("system", system)
    ctx = payload.get("context", ctx)
    # What a plugin wants said about this call on its log entry: memory's "lessons": "off"
    # when it gave none on purpose. Added beside the call's own fields, never over them.
    record = payload.get("record") or {}
    # The context object by section, for the call's log entry: name and size of each
    # block, in the order the model sees them.
    sections = {k: len(v if isinstance(v, str) else json.dumps(v, default=str))
                for k, v in ctx.items()}

    parts = [p for p in (_blocks(ctx), prompt) if p]
    if expect_json:
        ask = "Reply with a single JSON object and nothing else."
        if wants:
            # Name the fields. A capability's system prompt describes the JOB — "write a
            # brief covering note" — and leaves the model to infer the field names from
            # it. `mail.forward` required `body` and never said so, so the model returned
            # a note under some other key and the call failed every time.
            ask += (" It must contain these fields: "
                    + ", ".join(sorted(wants)) + ".")
        parts.append(ask)
    if engine() != "claude-cli":
        # The HTTP engines serve the judged surface only: no tools, no engine
        # sessions. A tool-using or session call on an HTTP engine is refused
        # under a canonical code rather than silently rerouted to another
        # provider (PRD E5: no silent fallback in a judged deployment).
        if tools or session:
            raise EngineFailed(
                f"{capability or 'llm'}: {ERR_UNSUPPORTED}: engine "
                f"'{engine()}' serves judged (non-tool, sessionless) calls "
                "only; tool-using agent sessions stay on the CLI engine")
        return _http_call(system, "\n\n".join(parts), expect_json, schema,
                          capability, timeout, record, sections, model)
    argv = [binary(), "-p", "--model", model or default_model(),
            # Line-delimited events instead of one blob at the end, so a call that takes
            # minutes can say what it is doing while it takes them. `--verbose` is what
            # the CLI requires alongside stream-json in print mode.
            "--output-format", "stream-json", "--verbose"]
    # HOW HARD THE MODEL THINKS, when the caller says. Most of a lookup turn is the model
    # thinking between tool calls (measured 7 Oct: 84% of a 401 s production turn), so this
    # is the first lever on a slow agent. Offered only where the engine takes it, for
    # the reason `supports_flag` gives; an unknown level is dropped, never passed.
    if effort in EFFORTS and supports_flag("--effort"):
        argv += ["--effort", effort]
    if tools and dangerous:
        # THE ONE CALLER THAT IS NOT GATED, and it is the full agent.
        #
        # `auto` does not mean "allowed": it still decides some commands need a human,
        # and with nobody at the end of a `-p` call every one of those is a silent
        # denial — the question asked into an empty room and answered no. Routing those
        # questions to a person through a permission tool was tried and reverted; for a
        # capability that exists precisely to be unconstrained, the honest answer is to
        # ask nothing. Which is what `agent` already declares: explicit, confirmed,
        # logged, off the rails on purpose.
        #
        # It stays a parameter rather than a setting, because it is not a preference.
        # A SCOPED agent's entire boundary is the hooks and deny list this flag ignores,
        # so passing it there would leave a gate that reads like a gate and is not one.
        argv += ["--dangerously-skip-permissions"]
        # EXCEPT DEPLOYS. Unconstrained is for the work, not for putting code live for every
        # agent on the host with nobody having seen it, which is what it did on 12 to 14 Oct
        # (scope.deploy_settings, gates/finrail_deploy_gate.py). A PreToolUse hook still
        # runs with permissions skipped; `full_agent.deploy_gate = false` takes it off.
        from .scope import deploy_gate_on, deploy_settings
        if deploy_gate_on():
            argv += ["--settings", str(deploy_settings())]
    elif tools:
        # Everything else: full tool access, but `auto`, not `bypassPermissions`.
        #
        # Measured on this machine: under `auto` with prompts answered by nobody, bash,
        # file writes and passwordless sudo all still succeed, so a capability loses
        # nothing it was actually using. What it gains is that `auto` still honours hooks
        # and permissions.deny, which bypassing does not — so a restricted agent remains
        # possible without changing this line again, and the gate a workspace declares is
        # a gate rather than a decoration.
        #
        # `--permission-prompts none`: there is no human at this end of a `-p` call, so
        # anything that would prompt is denied rather than hanging until the timeout.
        argv += ["--permission-mode", permission_mode()]
        if ask_person:
            # Its own settings layer, added to the workspace's: one PermissionRequest
            # hook. It fires only with prompts going to nobody (`none`): with a host
            # (`questions`) the request goes to the host first and fails before the
            # hook's answer lands (measured 5 Oct), so the two are not combined.
            from .scope import ask_settings
            argv += ["--settings", str(ask_settings())]
            questions = False
        if questions:
            # The gate answers the question tool; nothing else reaches the host, and
            # with stdin closed below anything that tries fails rather than waits.
            argv += ["--permission-prompt-tool", "stdio"]
        elif supports_flag("--permission-prompts"):
            argv += ["--permission-prompts", "none"]
    elif not rails:
        argv += bare()
    if session:
        # Continue the conversation, or open it under this id. This is the whole of
        # context retention: the engine keeps the thread, including tool use and
        # thinking, which replaying a stored transcript into the prompt could not.
        argv += ["--resume", session] if resume else ["--session-id", session]
    if system:
        argv += ["--append-system-prompt", system]

    try:
        # `rails` runs the call inside finrail's own gate: tool use is judged by
        # `finrail policy`, the same deny-by-default rule a locked session obeys. That
        # turns "I tried sqlite3 and was refused" into "I ran finrail and answered".
        where = cwd or (str(rails_dir()) if rails else "")
        import time as _time
        started = _time.time()
        _LAST.pop("result", None)
        iso = _isolation(where, env)
        # The engine keeps an auto memory of its own per working folder and loads it into
        # every session it starts there: a second behavioural memory, written by the engine
        # itself and reviewed by nobody. Whether this call ran with it off is on the
        # record, so a replay arm or a check can tell what else the model was given.
        record = {**record, "engine_auto_memory":
                  "off" if str(iso["env"].get("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "")) == "1"
                  else "on"}
        try:
            text, stdout, stderr, code = _stream(argv, "\n\n".join(parts), timeout,
                                                 iso, quiet_text=expect_json)
        except EngineFailed:
            # OUT OF TIME. The model saw the prompt, so it is a record like any other:
            # logged with no answer and no cost (the engine never reported one), and
            # `exit` "timeout". Without it the call left no trace at all, and a reader
            # adding up a thread's calls (the memory replay) counted the dearest turns
            # as free and could not see which lessons they were given.
            _log_call(capability, model, session, resume, system, "\n\n".join(parts),
                      "", "timeout", started, record, sections)
            raise
        _remember_size(session, _LAST.get("meter"))
        _log_call(capability, model, session, resume, system, "\n\n".join(parts),
                  text, code, started, record, sections)
    except FileNotFoundError:
        raise EngineFailed(
            f"cannot run '{binary()}' — the judged capabilities need the Claude CLI "
            f"(set FINRAIL_CLAUDE_BIN to point at it)")
    except EngineFailed as e:
        raise EngineFailed(f"{capability or 'llm'}: {e}")
    except OpFailed as e:
        raise OpFailed(f"{capability or 'llm'}: {e}")
    except OSError as e:
        # The engine could not be started: not executable, or no memory to fork it. That
        # is the engine failing, like a missing binary. E2BIG alone is the input's doing
        # (the arguments are too long), so it stays a plain failure.
        if e.errno == errno.E2BIG:
            raise OpFailed(f"{capability or 'llm'}: the call is too big to start the "
                           f"engine with: {e}")
        raise EngineFailed(f"{capability or 'llm'}: cannot start '{binary()}': {e}")
    if code != 0:
        # The ENGINE failed, not the answer: raised as its own kind, so a caller that
        # records what it has read can tell "the model never ran" from "the model ran and
        # answered badly" without reading the message (see `EngineFailed`).
        raise EngineFailed(f"{capability or 'llm'}: model call failed: "
                           f"{_engine_error(stdout, stderr)}")
    if not expect_json:
        return text.strip()
    return check_schema(_extract_json(text), schema)


CHAT_RAILS = """You are answering inside finrail, on its own rails. You have a shell,
and it is governed by finrail's policy: `finrail` commands and a few read-only tools are
permitted, everything else is refused with a reason. Use `finrail capabilities` to see
what exists and `finrail get <capability>` to read real data — that is how you answer
questions about this person's mail, tasks, documents and vault. Do not reach for sqlite3,
python or curl; they are denied, and the capability you actually want almost certainly
exists."""


def chat(prompt: str, session: str = "", resume: bool = True, system: str = "",
         timeout: int = 600, context: dict = None, person: str = "",
         activity: str = "") -> str:
    """Free-form conversation — no schema, no proposal. The fallback when no capability
    fits.

    Runs on the rails. Asked what was in the document corpus, it used to try `sqlite3`,
    be refused because nothing can approve a tool call in a headless run, and return the
    refusal AS its answer — with `docs.search` one capability away holding 2,125
    documents. The engine cannot be given fewer tools, so it is given a policy instead.

    `context` is passed through as `call` takes it — the activity in effect arrives this
    way, as it does for the agents that call `call` themselves.
    """
    return call(system=(system + "\n\n" + CHAT_RAILS).strip(), prompt=prompt,
                context=context, session=session, resume=resume, expect_json=False,
                timeout=timeout, capability="chat", rails=True, person=person,
                activity=activity)


def available() -> tuple:
    """(ok, detail) — a doctor check for the engine itself.

    This asks the engine to actually ANSWER, rather than only to exist. `--version`
    succeeds perfectly well while logged out, so the presence check reported the engine as
    healthy while every judged capability — `ask` above all — was failing on an expired
    login. Doctor being green while the front door is broken is the worst possible
    combination: it sends you looking anywhere but at the real fault.

    One trivial generation per doctor run is the honest price for the claim.
    """
    import shutil
    p = shutil.which(binary())
    if not p:
        return (False, f"'{binary()}' not on PATH — judged capabilities cannot run. "
                       f"Set FINRAIL_CLAUDE_BIN if it lives elsewhere.")
    try:
        r = subprocess.run([binary(), "--version"], capture_output=True, text=True,
                           timeout=30, **_isolation())
        if r.returncode != 0:
            return (False, _engine_error(r.stdout, r.stderr))
        version = (r.stdout or "").strip()[:40]
    except Exception as e:                                     # noqa: BLE001
        return (False, str(e)[:120])

    try:
        r = subprocess.run([binary(), "-p", "--model", default_model(), "say ok"],
                           capture_output=True, text=True, timeout=90, **_isolation())
    except subprocess.TimeoutExpired:
        return (False, f"{version}: engine did not respond within 90s")
    except Exception as e:                                     # noqa: BLE001
        return (False, str(e)[:120])
    if r.returncode != 0 or not (r.stdout or "").strip():
        detail = _engine_error(r.stdout, r.stderr)
        if "authenticat" in detail.lower() or "oauth" in detail.lower():
            detail += "  —  run:  claude   (log in once, interactively)"
        return (False, detail)
    return (True, f"{version} — signed in and answering")
