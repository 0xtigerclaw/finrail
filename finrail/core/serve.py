"""SERVE — one door into finrail per agent: a local socket, both directions.

Every door used to reach finrail by starting a process and reading what it printed when
it ended: the chat client for every call, the progress feed, every turn. Nothing could
come back up a line that was not open. This is the open line (gap map, layer 1 and 3;
agreed 2 Oct as one build with the two-way channel).

    finrail serve [socket=<path>]

A Unix socket, not a port: it cannot be reached from another machine, and who may
connect is the file's permission (0600, the agent's own user), the same way a folder is
protected. The file holds no data; it is only the address.

THE SERVICE RUNS NO TURN ITSELF. converse writes a turn's addresses into its process
environment (FINRAIL_THREAD, FINRAIL_TURN_USER), so two turns in one process would
overwrite each other. Each request is therefore its own child, `finrail <argv>`, exactly
what the client used to start, and the service is the line between them.

THE PROTOCOL, one JSON object per line, one request per connection:

    client  {"op": "run", "argv": [...], "stdin": "..." | null, "hangup": "kill" | "finish"}
    server  {"out": "<a line of stdout, newline included>"}     as it is printed
            {"err": "<stderr>"}                                  once, at the end
            {"exit": <code>, "signal": "<SIGTERM>" | null}       last
    client  {"op": "ping"}   ->   {"pong": true, "pid": <pid>, "code": "<version>"}
    client  {"op": "retire"} ->   {"retiring": true, "running": <n>}
    client  {"op": "whois", "token": "<t>"} -> {"who": {...} | null}
    client  {"op": "state"}  ->   {"state": "<the state folder this door serves>"}

WHO IS CALLING (core/caller.py). The kernel names the pid at the other end; the door
sorts it (the transport, a process inside a running turn, the agent alone, the
schedule, a terminal) and every run it starts carries the answer, with a one-off token.
`whois` lets that run check it: the door answers only the very process it gave the token
to (pid and start time), so neither a copy nor the run's own children (a turn's engine
and its shell) carry the claim.

RETIRING, NOT KILLING. A service outlives the agent's restarts on purpose (the unit is
KillMode=process, so the turns under it keep running), which also means a deploy left
the OLD service answering with old code. The launcher asks a running service to retire
and starts a fresh one: the old one gives up the socket file at once (so the new one can
take the address), takes no new request, lets the runs it is carrying finish, and exits.
No turn is lost. `code` is a digest of the package's Python files as they were when this
service started; the launcher prints it beside the digest on disk, so the journal says
whether the service it retired was out of date.

WHAT A HANG-UP MEANS is the client's to say. `"hangup": "kill"` (a progress feed) stops
the child when nobody is reading. The default, "finish", lets it run to its end: a turn
outlives the transport that started it, because the unit is KillMode=process and the next
transport adopts the turn and delivers its answer (harness recovery). Killing it on
hang-up lost that (found by the boundary suite over the socket, 5 Oct).
"""
import asyncio
import hashlib
import json
import os
import pathlib
import re
import signal
import sys

from . import log

_logger = log.get(__name__)

def _digest(root: pathlib.Path = None) -> str:
    """The package's code as a digest: what a door reports in `ping` (taken when it
    started, `_Door.code`), and what the launcher compares with the files on disk to
    say whether a door it retired was out of date.

    Every Python file in the package, by name and content, not this file alone: the
    door process holds caller, tracker, the entrypoint's splitter, the registry and
    whatever those load, so a change to any of them is a change to the door (review,
    5 Oct: a digest of serve.py and caller.py missed the rest). The whole package, a
    few tens of milliseconds: taken when a door starts, never at import, which every
    command pays for. Installed plugins (<state>/plugins) are not covered: the line
    the launcher prints from this is informational, and every door is replaced."""
    root = root or pathlib.Path(__file__).resolve().parent.parent
    h = hashlib.sha1()
    for f in sorted(root.rglob("*.py")):
        h.update(f.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def code_on_disk() -> str:
    return _digest()


#: How much of a request line is read. A turn's message rides in `stdin`, and a
#: message with an attachment's text can be long; past this the request is refused.
MAX_REQUEST = 32 * 1024 * 1024


def default_path(agent: str = "") -> pathlib.Path:
    """`<state>/sockets/<agent>.sock`. The agent is the one this process runs as."""
    from . import agents
    from .settings import state
    name = agent or agents.current() or "finrail"
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)[:60]
    return state() / "sockets" / f"{safe}.sock"


def _child_argv(argv: list) -> list:
    # FINRAIL_SERVE_EXEC names another `finrail` to run: the transport's own test
    # suites put a scripted stub on PATH and drive it through a real socket with this.
    override = os.environ.get("FINRAIL_SERVE_EXEC")
    if override:
        return [override, *argv]
    # -P: the caller names the folder a run works in, and `python -m` would otherwise
    # import from it first, so a worktree holding its own `finrail/` package replaced
    # the program the door runs (review, 5 Oct). The folder is for the command's paths,
    # never for its code.
    return [sys.executable, "-P", "-m", "finrail", *argv]


#: Callers that may name the person a run is for (`user=`).
NAMES_A_PERSON = ("transport", "terminal")


#: The parameters that say whom a run is for.
PERSON_PARAMS = ("user", "on_behalf_of")
_PERSON_KEYS = set()


def _person_keys() -> set:
    """PERSON_PARAMS and every alias any capability declares for them (`author=`,
    `sender=`, `owner=` on converse): an alias names the person just the same (review,
    5 Oct: the aliases walked past a check that knew only the two names)."""
    if not _PERSON_KEYS:
        keys = set(PERSON_PARAMS)
        try:
            from . import registry
            for cap in registry.commands().values():
                for alias, target in (getattr(cap, "aliases", None) or {}).items():
                    if target in PERSON_PARAMS:
                        keys.add(alias)
        except Exception:                                        # noqa: BLE001
            pass
        _PERSON_KEYS.update(keys)
    return _PERSON_KEYS


def _names_a_person(argv: list) -> str:
    """The parameter that names the person this run is for, or "". Read the way the
    entrypoint reads a parameter (`_parse`): what follows `--` is data, so a message
    that happens to start with `user=` is not a claim (review, 5 Oct: a hand parse over
    raw argv refused it)."""
    from .entrypoint import _parse
    keys = _person_keys()
    for k in _parse(argv)[1]:
        if k in keys:
            return k
    return ""


def _command_of(argv: list) -> str:
    """The command the entrypoint will run for this argv, read the way it reads it:
    leading flags (`--json`) are not the command (review, 5 Oct: `--json serve` slipped
    past a check of argv[0]). `_parse`, not `_split`: this runs inside the door, whose
    own stdin a request must never reach."""
    from .entrypoint import _parse
    pos = _parse(argv)[0]
    return pos[0] if pos else ""


#: Half of a character pair, alone. JSON can carry one; UTF-8 cannot.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


async def _run(req: dict, writer: asyncio.StreamWriter, gone: asyncio.Event,
               door: "_Door", who: dict) -> None:
    argv = req.get("argv")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv) or not argv:
        await _send(writer, {"error": "run needs argv: a non-empty list of strings"})
        return
    if _command_of(argv) == "serve":
        await _send(writer, {"error": "a service does not start another service"})
        return
    named = _names_a_person(argv)
    if named and who.get("kind") not in NAMES_A_PERSON:
        # Only the chat (whose author the relay checked) or a person at a terminal may
        # say whom a run is for. A turn's person comes from its record, not its words.
        await _send(writer, {"error": f"{named}= is not taken from a "
                             f"{who.get('kind') or 'caller'}: only the chat or a "
                             f"terminal may name the person a run is for"})
        return
    stdin = req.get("stdin")
    data = None
    if stdin is not None:
        # Encoded BEFORE the child exists, so text that cannot be written refuses the
        # request instead of starting a run on no input. Half an emoji (a client cut the
        # text mid-character) arrives from JSON as a lone surrogate, which UTF-8 cannot
        # carry. It goes in as U+FFFD, the way a pipe carried it when the client started
        # the process itself (review, 5 Oct: it emptied a turn's message, and the run
        # still said exit 0).
        data = _LONE_SURROGATE.sub("\ufffd", str(stdin)).encode()
    import secrets
    from . import caller
    token = secrets.token_hex(16)
    env = caller.child_env(dict(os.environ), who, token, str(door.whois_path))
    env[caller.DOOR] = str(door.path)
    env[INSIDE] = "1"
    if req.get("cwd") and os.path.isdir(str(req["cwd"])):
        cwd = str(req["cwd"])
    else:
        cwd = None
    proc = await asyncio.create_subprocess_exec(
        *_child_argv(argv),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=MAX_REQUEST, env=env, cwd=cwd)
    door.tokens[token] = {**who, "child": proc.pid, "child_start": caller.started(proc.pid)}

    async def feed_in():
        # Its own task, beside the readers: a child that does not read its input would
        # otherwise hold this write for ever, with nothing watching for the hang-up
        # (review, 5 Oct).
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:                                    # noqa: BLE001
                pass

    async def pump_out():
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            if gone.is_set():
                continue                 # read to the end, say nothing: nobody is there
            await _send(writer, {"out": line.decode(errors="replace")})

    in_task = asyncio.ensure_future(feed_in()) if stdin is not None else None
    err_task = asyncio.ensure_future(proc.stderr.read())
    out_task = asyncio.ensure_future(pump_out())
    wait_gone = asyncio.ensure_future(gone.wait())
    try:
        done, _ = await asyncio.wait({out_task, wait_gone},
                                     return_when=asyncio.FIRST_COMPLETED)
        if wait_gone in done and not out_task.done():
            if req.get("hangup") == "kill":
                # A feed nobody reads any more: the work stops.
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                out_task.cancel()
            else:
                # A turn: it runs to its end with nobody listening, and its outcome is
                # read from finrail's records by whoever comes next.
                await out_task
        elif req.get("hangup") == "kill":
            # Its output ended but the child may not have: one that closes stdout and
            # carries on must still die when the client goes (review, 5 Oct).
            ended = asyncio.ensure_future(proc.wait())
            done, _ = await asyncio.wait({ended, wait_gone},
                                         return_when=asyncio.FIRST_COMPLETED)
            if ended not in done:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        code = await proc.wait()
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    finally:
        wait_gone.cancel()
        if in_task is not None and not in_task.done():
            in_task.cancel()
        door.tokens.pop(token, None)
    err = (await err_task).decode(errors="replace")
    if gone.is_set():
        return
    if err:
        await _send(writer, {"err": err})
    if in_task is not None and in_task.done() and not in_task.cancelled():
        lost = in_task.exception()
        if lost is not None:
            # The input did not reach the run whole, so its exit code is no answer to
            # what was asked. Said as an error: a failed write must never again pass
            # for an empty message that ran fine.
            await _send(writer, {"error": f"the run did not get its input whole "
                                          f"({type(lost).__name__}: {lost})"})
            return
    sig = None
    if code < 0:
        try:
            sig = signal.Signals(-code).name
        except ValueError:
            sig = str(-code)
    await _send(writer, {"exit": code if code >= 0 else None, "signal": sig})


async def _send(writer: asyncio.StreamWriter, frame: dict) -> None:
    # A client that has gone leaves a transport asyncio warns about on every write
    # ("socket.send() raised exception"), a line per line of a turn that outlived its
    # client: the journal filled with them (dev agents, 5 Oct). Nothing is sent to it.
    if writer.is_closing() or writer.transport is None or writer.transport.is_closing():
        return
    try:
        writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass


class _Door:
    """One running service: its listening server, the address it holds, and the runs in
    flight, so a retirement can wait for them."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        #: This door's own line for `whois`, named for its process. The public address
        #: is handed to a newer door on retirement; the runs this one started still
        #: check their token here, with the door that issued it (review, 5 Oct).
        self.whois_path = path.with_name(f"{path.stem}.{os.getpid()}.whois")
        self.whois_server = None
        self.server = None
        self.inode = None
        self.runs = 0
        self.retired = asyncio.Event()
        #: The code this door runs, as it was on disk when the door started.
        self.code = _digest()
        #: token -> who the caller was, and the pid of the run it was given to
        self.tokens = {}
        #: The process that started this door, as (pid, start time): with no systemd
        #: unit to ask, it is how the transport is known (core/caller.transport_pid).
        from . import caller
        ppid = os.getppid()
        self.launcher = (ppid, caller.started(ppid)) if ppid > 1 else ()
        self.unit = caller.unit(os.getpid())
        #: The transport as last found, (pid, start time), so that finding it does not
        #: start a `systemctl` for every request (on a Mac it is one `ps` either way).
        self.transport = (0, "")
        #: The install this door serves: the state folder its runs work on. A caller
        #: that was pointed at another install asks before it sends a command here
        #: (`_own_door`).
        from .settings import state
        self.state = str(state().resolve())

    def transport_pid(self) -> int:
        """Which pid is the transport now (core/caller.transport_pid), asked of the
        system only when the one found last time is no longer that process. The start
        time is the check: a pid alone can be handed to a later process, and the door
        outlives the client (KillMode=process), so the client it knew does go away.
        Nothing is kept when nothing was found, so a unit caught between a stop and a
        start is asked again on the next request."""
        from . import caller
        pid, when = self.transport
        if pid and caller.started(pid) == when:
            return pid
        pid = caller.transport_pid(self.unit, self.launcher)
        when = caller.started(pid) if pid else ""
        self.transport = (pid, when) if pid and when else (0, "")
        return pid

    def owns_address(self) -> bool:
        try:
            return os.stat(self.path).st_ino == self.inode
        except OSError:
            return False

    def give_up_address(self) -> None:
        if self.owns_address():
            try:
                self.path.unlink()
            except OSError:
                pass
        # Given up is given up: this is called again on the way out, after the runs a
        # retired door carried have finished, and by then a newer door holds the
        # address. A file system that hands out a freed inode number again (ext4) could
        # give its socket this one's number, and the exit path would unlink the new
        # door's address (review, 5 Oct). With nothing recorded, nothing can match.
        self.inode = None


async def _handle(door: "_Door", reader: asyncio.StreamReader,
                  writer: asyncio.StreamWriter, whois_only: bool = False) -> None:
    gone = asyncio.Event()
    try:
        line = await reader.readline()
        try:
            req = json.loads(line or b"{}")
        except ValueError:
            await _send(writer, {"error": "a request is one JSON object on one line"})
            return
        op = req.get("op") if isinstance(req, dict) else None
        if op == "ping":
            await _send(writer, {"pong": True, "pid": os.getpid(), "code": door.code,
                                 "unit": door.unit,
                                 "launcher": door.launcher[0] if door.launcher else 0})
            return
        from . import caller
        peer = caller.peer_pid(writer.get_extra_info("socket"))
        if whois_only and op != "whois":
            await _send(writer, {"error": "this line answers whois only"})
            return
        if op == "whois":
            rec = door.tokens.get(str(req.get("token") or ""))
            ok = (bool(rec) and peer and rec.get("child") == peer
                  and rec.get("child_start") == caller.started(peer))
            await _send(writer, {"who": {k: v for k, v in rec.items()
                                         if k not in ("child", "child_start")}
                                 if ok else None})
            return
        if op == "retire":
            # The address first, so a new service can bind it the moment we answer.
            door.give_up_address()
            door.server.close()
            door.retired.set()
            await _send(writer, {"retiring": True, "running": door.runs})
            return
        if op == "state":
            # Which install this is the door of, for a caller that must not send
            # another install's command here (`_own_door`).
            await _send(writer, {"state": door.state})
            return
        if op != "run":
            await _send(writer, {"error": f"unknown op {op!r} "
                                          f"(run, ping, retire, whois, state)"})
            return
        if door.retired.is_set():
            await _send(writer, {"error": "this service is retiring; ask the new one"})
            return
        if peer and any(rec.get("child") == peer for rec in door.tokens.values()):
            # One of our own runs asking us to run it again: it is already inside, and
            # answering would start a loop of runs each sending itself back.
            await _send(writer, {"error": "a run of this door is already inside it"})
            return

        async def watch_hangup():
            # Anything after the request line, or EOF, from a client means it is done
            # listening: a one-request connection never sends a second line.
            try:
                await reader.read()
            except OSError:
                # A reset, or our own write meeting the closed line, is a hang-up too.
                # Unheard, a run that asked to be killed on hang-up ran on with nobody
                # reading (review, 5 Oct). A cancel is not caught: it is ours.
                pass
            gone.set()
        hangup = asyncio.ensure_future(watch_hangup())
        door.runs += 1
        try:
            def sort():
                return caller.identify(peer, transport=door.transport_pid())
            who = await asyncio.get_running_loop().run_in_executor(None, sort)
            await _run(req, writer, gone, door, who)
        finally:
            door.runs -= 1
            hangup.cancel()
    except Exception as e:                                       # noqa: BLE001
        _logger.warning("serve: request failed: %s", e)
        await _send(writer, {"error": f"{type(e).__name__}: {e}"})
    finally:
        try:
            writer.close()
        except Exception:                                        # noqa: BLE001
            pass


class Occupied(RuntimeError):
    """Another door is answering at this address."""


async def _serve(path: pathlib.Path, ready=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    import functools
    from . import atomic
    door = _Door(path)
    old = os.umask(0o177)
    try:
        # One door takes an address at a time, and only an address nobody answers on: a
        # socket file that still answers belongs to a live door, which removing it would
        # strand, serving nobody for ever (review, 5 Oct: two launchers at once).
        with atomic.locked(path):
            if path.exists() or path.is_symlink():
                if ping(path, timeout=0.5):
                    raise Occupied(f"a door is already serving on {path}")
                path.unlink()            # a stale socket from a process that died
            door.server = await asyncio.start_unix_server(
                functools.partial(_handle, door), path=str(path), limit=MAX_REQUEST)
            os.chmod(path, 0o600)
            door.inode = os.stat(path).st_ino
        if door.whois_path.exists():
            door.whois_path.unlink()
        door.whois_server = await asyncio.start_unix_server(
            functools.partial(_handle, door, whois_only=True), path=str(door.whois_path))
        os.chmod(door.whois_path, 0o600)
    finally:
        os.umask(old)
    print(f"serving on {path} (code {door.code})", flush=True)
    if ready:
        ready()
    try:
        await door.retired.wait()
        # Retired: no new requests; the runs already carried finish, then we go.
        while door.runs:
            await asyncio.sleep(0.5)
        print(f"retired from {path}", flush=True)
    finally:
        door.give_up_address()           # only if the address is still ours
        try:
            door.whois_path.unlink()
        except OSError:
            pass


def serve(path=None, ready=None) -> None:
    """Serve until killed. Removes the socket file on the way out."""
    p = pathlib.Path(path) if path else default_path()

    def _stop(*_):
        raise KeyboardInterrupt          # a unit stops us with SIGTERM: leave tidily

    try:
        signal.signal(signal.SIGTERM, _stop)
    except ValueError:
        pass                             # not the main thread (a test): no handler
    try:
        asyncio.run(_serve(p, ready))
    except KeyboardInterrupt:
        pass
    except Occupied as e:
        print(str(e), flush=True)
        raise SystemExit(3)


# ── a client, for tests and for Python callers ──────────────────────────────
def _frames(s):
    """The frames a door sends on one connection, one JSON object per line, as they
    arrive, until the door closes the line. The one reader for `call` and `forward`."""
    buf = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                frame = json.loads(line)
            except ValueError:
                frame = None
            if not isinstance(frame, dict):
                # A door that sends something else is broken, and saying so beats a
                # traceback (review, 8 Oct). Both readers end on an `error` frame.
                yield {"error": f"the door sent a line that is not a frame: "
                                f"{line[:80].decode(errors='replace')!r}"}
                return
            yield frame


def call(path, argv: list, stdin: str = None, timeout: float = 600) -> dict:
    """{"stdout", "stderr", "exit", "signal"} for one run through the socket."""
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(path))
    s.sendall((json.dumps({"op": "run", "argv": argv, "stdin": stdin}) + "\n").encode())
    out, err, end = [], "", {}
    for frame in _frames(s):
        if "out" in frame:
            out.append(frame["out"])
        elif "err" in frame:
            err += frame["err"]
        elif "exit" in frame or "error" in frame:
            end = frame
    s.close()
    return {"stdout": "".join(out), "stderr": err, "exit": end.get("exit"),
            "signal": end.get("signal"), "error": end.get("error")}


def _ask(path, req: dict, timeout: float) -> dict:
    """One question to a door (ping, retire, state): connect, send the one line, read
    the one-line answer. {} when nothing answers, or the answer is not a JSON object.
    The one client for every one-shot question, so a longer reply or a new field is
    read the same way everywhere (review, 5 Oct: three copies, each trusting one recv,
    and `ping` matched the bytes "pong" while `ping_info` read the field)."""
    import socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall((json.dumps(req) + "\n").encode())
            got = b""
            while b"\n" not in got:
                chunk = s.recv(4096)
                if not chunk:
                    break
                got += chunk
        out = json.loads(got.split(b"\n", 1)[0] or b"{}")
    except (OSError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def ping_info(path, timeout: float = 2) -> dict:
    """The running service's answer to ping, or {} when nothing answers."""
    out = _ask(path, {"op": "ping"}, timeout)
    return out if out.get("pong") else {}


def retire(path, timeout: float = 5) -> dict:
    """Ask the running service to give up its address and leave once its runs finish."""
    return _ask(path, {"op": "retire"}, timeout)


def ping(path, timeout: float = 2) -> bool:
    return bool(ping_info(path, timeout))


# ── 2b: every `finrail` an agent runs comes in by its door ─────────────────
#: Set in every run the door starts: that process is already inside, and runs locally.
#: The engine a run starts drops it (llm._DOOR_VARS), so the engine's own commands come
#: back in through the door and are sorted like any other caller.
INSIDE = "FINRAIL_IN_DOOR"

#: Commands that never go by the door: the door itself, the gate's own judgement (every
#: tool call of a scoped engine; pure computation, and the door would only add a process),
#: a background worker the door's own run already started, and the usage text. Command
#: words only, as the entrypoint reads them; `--help` is a flag and is checked as one.
LOCAL_ONLY = ("serve", "policy", "_job", "help", "-h")


def _inside_for_real() -> bool:
    """Is this process really a run its door started? The marker is only a claim: a
    process could set it to skip the door and keep its own environment. A real run's
    door is its parent and knows its token (`caller.verified`)."""
    from . import caller
    return bool(caller.verified())


def _own_door(path: pathlib.Path) -> bool:
    """Is the door at `path` this install's own? A door's name is inherited: the engine
    keeps it, and so does everything its shell starts, including a process pointed at
    ANOTHER install by its own FINRAIL_STATE (the test suite, an agent trying a change
    on a scratch state). The door ran that command on its own, live state instead
    (review, 5 Oct).

    A door kept where this install keeps its doors is its own. One served somewhere else
    (`serve socket=<path>`) is asked which install it serves, and one that does not say
    is not believed."""
    from .settings import state
    try:
        mine = state().resolve()
        if path.resolve().parent == (mine / "sockets").resolve():
            return True
    except OSError:
        return False
    return _ask(path, {"op": "state"}, 2).get("state") == str(mine)


def _door_for_caller() -> "pathlib.Path | None":
    from . import caller
    if os.environ.get(INSIDE):
        if _inside_for_real():
            return None
        os.environ.pop(INSIDE, None)     # a claim, not a fact: come in by the door
    named = os.environ.get(caller.DOOR)
    if named and _own_door(pathlib.Path(named)):
        return pathlib.Path(named)
    if os.environ.get("FINRAIL_AGENT"):
        try:
            return default_path()
        except Exception:                                        # noqa: BLE001
            return None
    return None


def _at_a_terminal() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, OSError):
        return False


def _input_for(pos: list, kw: dict):
    """The input a forwarded command needs, or None.

    `task=-` reads it exactly as the run would (entrypoint.wants_stdin, task_from_stdin),
    so the spelling is never matched here again and a terminal gets the usage error
    instead of a run on an empty task (review, 5 Oct: `--task=-` arrived with no task,
    and `task=-` typed at a terminal ran on nothing). The approval of a run that takes
    its value from input (a secret: `printf … | finrail approve <id>`; forwarding
    dropped it) reads input only if some is waiting, so an idle inherited pipe never
    holds the command."""
    from .entrypoint import task_from_stdin, wants_stdin
    if wants_stdin(kw):
        return task_from_stdin()
    if pos[:1] == ["approve"] and not _at_a_terminal() and sys.stdin is not None:
        import select
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
        except (ValueError, OSError):
            return None
        if ready:
            return sys.stdin.read()
    return None


def forward(argv: list) -> "int | None":
    """Run this command through the agent's door, if it has one: the door sees which
    process asks and decides what that process may say (2b). None when there is no door
    to use, and the caller runs the command itself, exactly as before.

    Only for a process that belongs to an agent (FINRAIL_AGENT or FINRAIL_DOOR); a
    terminal or the schedule with no agent named runs locally."""
    import socket
    from .entrypoint import _parse
    pos, kw, flags = _parse(argv)
    cmd = pos[0] if pos else ""
    if not argv or cmd in LOCAL_ONLY or "help" in flags:
        return None
    if cmd == "approve" and _at_a_terminal():
        # An approval may prompt on the terminal for the value it stores (secret.set
        # prompt=true, bundled/secrets), and the door's run has no terminal. The person
        # is here, at theirs: the approval is their own, and it runs here.
        return None
    path = _door_for_caller()
    if path is None or not path.exists():
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(path))
    except OSError:
        return None
    try:
        # Read after the door is known to answer, so a command that runs locally after
        # all still has its input (a terminal's task=- is refused here, as the run
        # would refuse it).
        stdin = _input_for(pos, kw)
    except Exception:
        s.close()
        raise
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    s.sendall((json.dumps({"op": "run", "argv": argv, "stdin": stdin, "cwd": cwd,
                           "hangup": "kill"}) + "\n").encode())
    end = {}
    try:
        for frame in _frames(s):
            if "out" in frame:
                sys.stdout.write(frame["out"])
                sys.stdout.flush()
            elif "err" in frame:
                sys.stderr.write(frame["err"])
                sys.stderr.flush()
            elif "exit" in frame or "error" in frame:
                end = frame
                break
    except KeyboardInterrupt:
        s.close()                         # the door stops the run when we hang up
        return 130
    except BrokenPipeError:
        # Whoever read our output stopped (`| head`): stop the run, quietly.
        s.close()
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 141
    finally:
        try:
            s.close()
        except OSError:
            pass
    if end.get("error"):
        sys.stderr.write(f"finrail: {end['error']}\n")
        return 2
    if end.get("exit") is not None:
        return int(end["exit"])
    if end.get("signal"):
        try:
            return 128 + int(getattr(signal.Signals, end["signal"]))
        except (AttributeError, ValueError):
            return 1
    sys.stderr.write("finrail: the door closed the line before the run ended\n")
    return 1
