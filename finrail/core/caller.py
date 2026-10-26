"""WHO IS CALLING — the door's answer, from the machine, never from the message.

On the team's server everything runs as one Linux user: the agents, their engine, the
Chat transport, the schedule and every person who signs in. So the user a socket
connection comes from says nothing. What does is WHERE the caller sits:

    its command line      the chat transport is a node process running the harness
    its parent processes  a command started inside a running turn descends from it
    its unit (Linux)      every process belongs to one systemd unit or login session,
                          and cannot leave it, even by detaching

From those the door sorts every caller into one kind (`identify`), and every run started
for that caller records it (`tracker.save`, `caller`). A caller's own claims (a `user=`,
a turn variable it set) are not consulted: a model can type those.

    transport   the agent's own chat client. The person it names comes from a signature
                the relay checked, so it is trusted to name one. Known by WHICH process
                it is, never by what its command line says: the unit's main process (the
                launcher execs the client, so they are one pid), or, with no units to
                read, the launcher that started this door. A process that merely names
                the harness on its command line is not it (review, 5 Oct).
    turn        a process inside a running turn of this agent. The person is the turn's
                person, read from the run record.
    agent       inside this agent's unit, but neither of the above: a process an agent
                detached from its turn. The agent, for nobody.
    cron        the schedule's unit.
    terminal    a login session: a person at a shell. Which person, the machine cannot
                say while everyone signs in as the same user. With no units to read,
                a process that has a terminal.
    local       no units to read (a Mac) and no terminal: a process that is neither
                transport nor turn, and that nothing places. It names nobody and
                approves nothing.
    unknown     anything else.
"""
import os
import pathlib
import socket as _socket
import subprocess
import sys

from . import context

KINDS = ("transport", "turn", "agent", "cron", "terminal", "local", "unknown")

#: The transport's own program, as it appears on its command line.
TRANSPORT_MARK = "harness-finrail-ws.mjs"


# ── reading a process ────────────────────────────────────────────────────────
def peer_pid(sock) -> int:
    """The pid at the other end of a Unix socket, from the kernel. 0 when unknown."""
    try:
        if sys.platform.startswith("linux"):
            import struct
            raw = sock.getsockopt(_socket.SOL_SOCKET, _socket.SO_PEERCRED,
                                  struct.calcsize("3i"))
            return struct.unpack("3i", raw)[0]
        if sys.platform == "darwin":
            return int(sock.getsockopt(0, 0x002))    # SOL_LOCAL, LOCAL_PEERPID
    except (OSError, ValueError, AttributeError):
        pass
    return 0


def _ps(pid: int, field: str) -> str:
    try:
        out = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def cmdline(pid: int) -> str:
    p = pathlib.Path(f"/proc/{pid}/cmdline")
    if p.exists():
        try:
            return p.read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            return ""
    return _ps(pid, "command")


def parent(pid: int) -> int:
    p = pathlib.Path(f"/proc/{pid}/stat")
    if p.exists():
        try:
            # The name in field 2 may hold spaces and parentheses: read after the last ')'.
            return int(p.read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return 0
    try:
        return int(_ps(pid, "ppid") or 0)
    except ValueError:
        return 0


def started(pid: int) -> str:
    """When the process began, as the kernel tells it: with the pid, one process's
    identity. A pid alone can be reused by a later process."""
    p = pathlib.Path(f"/proc/{pid}/stat")
    if p.exists():
        try:
            return p.read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return ""
    return _ps(pid, "lstart") if pid else ""


def main_pid(unit_name: str) -> int:
    """The main process of a systemd service, 0 when it is not one or has none."""
    if not unit_name.endswith(".service"):
        return 0
    # ASKED OF THE MANAGER THE UNIT WAS INSTALLED UNDER, the way the services code asks
    # (services_systemd._argv: `--user` when service.scope is user). The system manager
    # does not know a user unit and answers 0 for it, so after the client's first restart
    # nothing was the transport and every turn naming a person was refused (review, 5 Oct).
    from . import services_systemd
    try:
        out = subprocess.run(services_systemd._argv()
                             + ["show", "-p", "MainPID", "--value", unit_name],
                             capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def transport_pid(own_unit: str, launcher: tuple = ()) -> int:
    """Which pid is this agent's transport. The unit's main process where there is a
    unit; otherwise the launcher that started the door, if it is still that process.
    0 when it cannot be known, and then nothing is the transport."""
    pid = main_pid(own_unit) if own_unit else 0
    if pid:
        return pid
    if launcher and launcher[0] and launcher[1] and started(launcher[0]) == launcher[1]:
        return launcher[0]
    return 0


def ancestors(pid: int, limit: int = 40) -> list:
    """pid and every parent up to init, nearest first."""
    out, seen = [], set()
    while pid and pid > 1 and pid not in seen and len(out) < limit:
        out.append(pid)
        seen.add(pid)
        pid = parent(pid)
    return out


def unit(pid) -> str:
    """The systemd unit or login scope a process belongs to, "" where there is none."""
    p = pathlib.Path(f"/proc/{pid}/cgroup")
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return ""
    for ln in lines:
        path = ln.split(":", 2)[-1]
        if path:
            return path.rstrip("/").rsplit("/", 1)[-1]
    return ""


# ── sorting a caller ─────────────────────────────────────────────────────────
def runs_in(rec: dict, pids) -> bool:
    """Does this run execute in one of `pids`: the process executing it, its worker, or
    the engine it started. The one match for a run and a set of processes, for the door
    and for converse's own lookup (converse/threads.turn_of_process).

    A pid is matched with its start time where the record has one: a stale `running`
    record whose pid the kernel has since given to another process must not make that
    process the old turn's person (review, 5 Oct)."""
    for k in ("process", "worker", "engine"):
        pid = rec.get(f"{k}_pid")
        if not pid or pid not in pids:
            continue
        when = rec.get(f"{k}_started")
        if when and started(pid) != when:
            continue                         # the pid now belongs to someone else
        return True
    return False


def _turn_of(chain: list, agent: str) -> dict:
    """The running run of this agent whose process the caller descends from. Read from
    the ledger's index of runs in flight (`tracker.running`): every command an agent
    runs asks this, and it read the newest thousand records to answer (review, 5 Oct)."""
    from . import tracker
    pids = set(chain)
    for rec in tracker.running():
        if agent and rec.get("agent") and rec.get("agent") != agent:
            continue
        if runs_in(rec, pids):
            return rec
    return {}


def identify(pid: int, agent: str = "", own_unit: str = None, transport: int = 0) -> dict:
    """{"kind", "actor", "pid", "unit", and for a turn: "person", "run", "thread" and
    "context", what the turn's children inherit (`context.of_record`): the same person
    and run in the context's own words, with the turn's request and stage.
    `transport` is the pid the door knows as the transport (`transport_pid`)."""
    from . import agents as _agents
    agent = agent or _agents.current() or ""
    own = unit(os.getpid()) if own_unit is None else own_unit
    out = {"pid": pid, "unit": "", "kind": "unknown", "actor": f"unknown:{pid}"}
    if not pid:
        return out
    where = unit(pid)
    out["unit"] = where
    if transport and pid == transport and TRANSPORT_MARK in cmdline(pid):
        out.update(kind="transport", actor=f"transport:{agent or 'transport'}")
        return out
    chain = ancestors(pid)
    turn = _turn_of(chain, agent)
    if turn:
        out.update(kind="turn", actor=f"agent:{agent}" if agent else "agent",
                   person=turn.get("on_behalf_of") or "", run=turn.get("id") or "",
                   thread=turn.get("thread") or "", context=context.of_record(turn))
        return out
    out.update(placed(pid, where, own, agent))
    return out


def placed(pid: int, where: str, own: str, agent: str = "") -> dict:
    """Where a process sits, from its unit or its terminal alone: {"kind", "actor"} for
    what is left once the transport and a turn have been ruled out. `where` is the
    process's unit (`unit`), `own` the unit this agent's processes share.

    THE ORDER IS THE RULE. A login session is a person's. The schedule's unit is the
    schedule's. Any other unit the process shares with the agent is the agent's, however
    the process got there: a turn's child that double-forked is still in the unit, since
    nothing leaves one. A process asking about ITSELF (`approver`) passes its own unit as
    both `where` and `own`, and gets the same answer the door would give it (review, 8 Oct
    evening: such a process had no door word and no turn above it, `approver` answered
    "unverified", and unverified was let through as a person)."""
    out = {}
    if not where:
        # No units to read (a Mac). A terminal is the one thing still to be seen: a
        # person's shell, and whatever it starts, keeps the terminal it was opened in.
        # A process with none (one detached from a turn, a launchd job) is placed by
        # nothing, so it is `local` and names nobody (review, 8 Oct: an orphan named a
        # person and approved runs through the door).
        user = _ps(pid, "user") or os.environ.get("USER") or "?"
        tty = _ps(pid, "tty")
        if tty and tty.strip("?-") and _opened_by_a_door(pid):
            out.update(kind="terminal",
                       actor=f"terminal:{user}@{_socket.gethostname()}:{tty}")
            return out
        out.update(kind="local", actor=f"os:{user}")
        return out
    if where.startswith("session-") and where.endswith(".scope"):
        user = _ps(pid, "user") or "?"
        out.update(kind="terminal",
                   actor=f"terminal:{user}@{_socket.gethostname()}:{where}")
    elif "cron" in where or where.endswith("-reap.service") or where in _schedule_units():
        out.update(kind="cron", actor=f"cron:{where.removesuffix('.service')}")
    elif own and where == own:
        out.update(kind="agent", actor=f"agent:{agent}" if agent else "agent")
    return out


#: What a person's terminal on a host with no units hangs from, at the top of its
#: process tree: a terminal application, or a remote login.
_DOORS = ("sshd", "sshd-session", "login")


def _opened_by_a_door(pid: int) -> bool:
    """Does this process hang from a terminal application or a remote login? Asked on a
    host with no units, where a terminal alone used to place a person: a turn's child
    that double-forked and then opened a pty of its own (`script`) had a terminal too,
    and was named a person (review, 19 Oct). An orphan's tree tops out at itself under
    launchd; a person's at Terminal, iTerm or sshd. A daemon such as tmux tops out at
    itself as well, so a shell inside one is `local` here. This is a floor, not a wall:
    on such a host the agent runs as the person's own user, and that user can open a
    terminal application."""
    chain = [p for p in ancestors(pid) if p > 1] or [pid]
    exe = _ps(chain[-1], "comm")
    return ".app/Contents/" in exe or exe.rsplit("/", 1)[-1] in _DOORS


#: Who may approve a run that waits on a person. A turn (an agent's engine or anything it
#: started), an agent's detached process, or a caller nobody can place, may not: the
#: agent is not the person (review, 5 Oct: `converse … approve=true` and
#: `finrail --json approve` let a coding agent approve its own runs). The transport and a
#: terminal are a person's. The schedule's unit is kept: the review site's inbox drain
#: runs there and approves in the name of the person whose signed click it carries, and
#: no turn can put a process into that unit.
NOT_A_PERSON = ("turn", "agent", "local", "unknown")


def _schedule_units() -> tuple:
    """Units that are the schedule's though their name does not say `cron`: declared in
    `caller.schedule_units`. The review site's instant drain is one — a systemd path
    unit that runs the same drain the cron runs, the moment a signed click lands, and
    no turn can put a process into it. Undeclared, it was `unknown` and every approval
    a person clicked was refused as coming "from inside the agent" (2026-10-14)."""
    try:
        from .registry import settings
        got = settings().get("caller.schedule_units")
    except Exception:                                              # noqa: BLE001
        got = None
    return tuple(str(u) for u in (got or ()) if str(u).endswith(".service"))


def approver() -> dict:
    """Who is approving, from where this process sits. The door's checked word when this
    process came by it; otherwise the process's own parents, then its own place: a
    process started inside a running turn is that turn (its own run does not count:
    converse approving at the chat's request is the chat, not a turn of itself), and
    one with no turn above it is what its unit or its terminal makes it (`placed`).

    NOTHING IS "UNVERIFIED". That used to be the answer for a process with no door word
    and no turn above it, and it was let through as a person, so a turn that
    double-forked a python and called `dispatch` from it approved anything pending
    (review, 8 Oct evening). The machine can always place a process: in the agent's
    unit it is the agent, on a host with no units and no terminal it is `local`, and
    neither approves."""
    who = verified()
    if who:
        return who
    from . import agents as _agents
    me = os.getpid()
    agent = _agents.current() or ""
    turn = _turn_of(ancestors(me)[1:], agent)
    if turn:
        return {"kind": "turn", "run": turn.get("id")}
    own = unit(me)
    return {"pid": me, "unit": own, "kind": "unknown", "actor": f"unknown:{me}",
            **placed(me, own, own, agent)}


def approval_refusal() -> str:
    """Why this process may not approve a run that waits on a person, or ""."""
    who = approver()
    if who.get("kind") in NOT_A_PERSON:
        return (f"a run that waits on a person is approved by a person, not by a "
                f"{who['kind']} (this came from inside the agent)")
    return ""


# ── what a child is handed ───────────────────────────────────────────────────
ENV = "FINRAIL_CALLER"
#: A one-off token the door gives each run it starts. The claim in ENV is only a claim:
#: anything can set an environment variable. `verified()` asks the door whose token it
#: is, and the door answers only the process it issued it to (pid and start time).
TOKEN = "FINRAIL_CALLER_TOKEN"
SOCKET = "FINRAIL_SOCKET"
#: The agent's door itself, the public address (`FINRAIL_SOCKET` in a run is the door's
#: private check line, which outlives a hand-over until the run is done).
DOOR = "FINRAIL_DOOR"


def child_env(base: dict, who: dict, token: str = "", socket_path: str = "") -> dict:
    """The environment for a run started on this caller's behalf."""
    import json
    env = {k: v for k, v in base.items() if k not in (ENV, TOKEN, SOCKET, DOOR)}
    if token:
        env[TOKEN] = token
    if socket_path:
        env[SOCKET] = str(socket_path)
    env[ENV] = json.dumps({k: who.get(k) for k in ("kind", "actor", "pid", "unit")},
                          sort_keys=True)
    # THE TURN'S VARIABLES (context.TURN_VARS), from the run record alone (`identify`),
    # and none at all when the caller is not inside a turn: whatever the caller had set
    # is not handed on. Merged under nothing rather than under this process's own
    # context: the door is not inside the turn, and its own context, if it had one,
    # would otherwise override every turn's person.
    turn = who if who.get("kind") == "turn" else {}
    return context.turn_env(turn.get("context") or {}, turn.get("thread"),
                            inherited={}, base=env)


def current() -> dict:
    """What the door said about this process's caller, or {} when it came another way."""
    import json
    raw = os.environ.get(ENV)
    if not raw:
        return {}
    try:
        got = json.loads(raw)
        return got if isinstance(got, dict) else {}
    except ValueError:
        return {}


def verified() -> dict:
    """The door's answer for this process's caller, CHECKED: {} when nothing came by the
    door, or when the door does not recognise the token for this process.

    Both ends are checked. The door answers only the very process it gave the token to
    (not its children: an engine a turn started must not carry the transport's word),
    and this process believes only a door that is its own parent. Anything can set an
    environment variable, run a socket, or copy a token; nothing can make itself the
    parent of a process that is already running (review, 5 Oct)."""
    import json
    token, path = os.environ.get(TOKEN), os.environ.get(SOCKET)
    if not token or not path:
        return {}
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(path)
        server = peer_pid(s)
        if not server or server <= 1 or server != os.getppid():
            s.close()
            return {}
        s.sendall((json.dumps({"op": "whois", "token": token}) + "\n").encode())
        got = b""
        while not got.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            got += chunk
        s.close()
        who = (json.loads(got or b"{}") or {}).get("who") or {}
        return who if isinstance(who, dict) else {}
    except (OSError, ValueError):
        return {}
