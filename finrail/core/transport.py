"""The seam between a conversation and the wire it travels on.

converse knows two addresses, a thread and a run, and one person, the user the
transport verified. It does not know relays, channels or public keys, and it must not:
the day it did, a handoff was a chat capability reached through `/do`, the person and
the channel rode in environment variables the harness happened to set, and the list of
other agents was frozen at harness launch (canary, 2026-09-17 to 09-22).

A transport implements two verbs. `deliver` puts a message in front of another agent,
addressed to it and to the person it is for, in the thread the conversation is in.
`reply` says something back into a thread. Each returns {"id", "thread"}: the
transport's own id for what it sent, and the thread it actually used, which may differ
from the one asked for when the peer cannot be reached there and a shared place exists. Everything else a wire needs (which relay, which key
signs) is the transport's business, read from the agent definition it is given.

`local` is the implementation for a box with no relay: it writes the message into the
peer's thread record here and returns, which is enough for the tests that used to need
a relay and a second harness, and is what a sandboxed executor will use.
"""
import json
import time
import uuid

from .errors import ContractViolation


class Transport:
    """What a transport must provide. The name is what an agent definition's
    `transport` field says."""
    name = ""

    def deliver(self, agent: dict, peer: dict, thread: str, text: str,
                person: str = "") -> dict:
        raise NotImplementedError

    def reply(self, agent: dict, thread: str, text: str, person: str = "") -> dict:
        raise NotImplementedError


_REGISTRY = {}


def register(transport: Transport) -> Transport:
    _REGISTRY[transport.name] = transport
    return transport


def names() -> list:
    return sorted(_REGISTRY)


def for_agent(agent: dict) -> Transport:
    """The transport an agent definition names, or a refusal that says which exist."""
    want = str((agent or {}).get("transport") or "local")
    if want not in _REGISTRY:
        # Extensions register theirs on import; make sure they have been imported.
        from . import registry
        registry.load()
    if want not in _REGISTRY:
        raise ContractViolation(
            f"transport: {want!r} is not installed for agent "
            f"{(agent or {}).get('name', '?')} (installed: {', '.join(names()) or 'none'})")
    return _REGISTRY[want]


class Local(Transport):
    """No wire. A delivery is appended to <state>/transport/<peer>.jsonl, where a local
    harness or a test reads it. The same shape a relay would carry, minus the relay."""
    name = "local"

    def _box(self, peer_name: str):
        from .settings import state
        d = state() / "transport"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{peer_name}.jsonl"

    def _write(self, peer_name: str, rec: dict) -> dict:
        rec = {"id": uuid.uuid4().hex[:12], "at": int(time.time()), **rec}
        with self._box(peer_name).open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        return {"id": rec["id"], "thread": rec.get("thread", "")}

    def deliver(self, agent, peer, thread, text, person=""):
        return self._write(peer["name"], {
            "kind": "deliver", "from": agent.get("name", ""), "to": peer["name"],
            "thread": thread, "text": text, "person": person})

    def reply(self, agent, thread, text, person=""):
        return self._write(agent.get("name", "") or "-", {
            "kind": "reply", "from": agent.get("name", ""), "thread": thread,
            "text": text, "person": person})

    def read(self, peer_name: str) -> list:
        p = self._box(peer_name)
        if not p.is_file():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


register(Local())
