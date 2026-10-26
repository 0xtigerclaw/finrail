#!/usr/bin/env python3
"""The scoped agent's steering hook: a message sent while the turn runs reaches it.

Installed by `finrail.core.scope.materialise` as a PostToolUse hook on every tool. After
each tool call it asks `converse.waiting` whether the turn's person has sent anything
since the turn began; only when they have does it call `converse.steered`, which takes
those messages off the thread's queue and returns what the model should be told, and
that goes back to the engine as `additionalContext` (converse/steer.py).

It holds no rules and fails OPEN, silently: steering is a courtesy, and a hook that
cannot reach finrail must never stop the turn. Outside a conversation (no thread in the
environment converse set for the turn) it does nothing at all.
"""
import json
import os
import shutil
import subprocess
import sys

#: Seconds for each finrail call. The first runs after every tool call, so it is short.
WAIT = 10


def _result(argv: list) -> dict:
    binary = shutil.which("finrail")
    if not binary:
        return {}
    try:
        r = subprocess.run([binary, *argv, "--json"], capture_output=True, text=True,
                           timeout=WAIT, stdin=subprocess.DEVNULL)
    except Exception:                                            # noqa: BLE001
        return {}
    if r.returncode != 0:
        return {}
    try:
        out = json.loads(r.stdout or "{}")
    except ValueError:
        return {}
    res = out.get("result") if isinstance(out, dict) else None
    return res if isinstance(res, dict) else {}


def main() -> None:
    sys.stdin.read()
    thread = (os.environ.get("FINRAIL_THREAD") or "").strip()
    if not thread:
        return
    if int(_result(["get", "converse.waiting", f"thread={thread}"]).get("count") or 0) < 1:
        return
    context = _result(["do", "converse.steered", f"thread={thread}"]).get("context") or ""
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": context}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:                                            # noqa: BLE001
        pass
    sys.exit(0)
