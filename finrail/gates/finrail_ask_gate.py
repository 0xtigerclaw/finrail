#!/usr/bin/env python3
"""The full agent's PermissionRequest hook — a THIN WRAPPER around `finrail policy --permission`.

Used only when the full agent runs ASKING rather than bypassing (`full_agent.ask_person`).
The engine runs in its own permission mode; when that mode would show a person a
permission dialog, this hook runs instead (measured 5 Oct, CLI 2.1.281: it fires in a
headless `-p` turn under `--permission-prompts none`, the engine waits for it, and an
allow runs the tool while a deny's message reaches the model). The question goes to the
person in the chat through `core/questions.py`, like a scoped agent's.

Fails CLOSED: no finrail, no answer, a broken reply, all deny.
"""
import json
import shutil
import subprocess
import sys


def decide(allow: bool, message: str = "") -> None:
    d = {"behavior": "allow"} if allow else {"behavior": "deny", "message": message}
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                             "decision": d}}))
    sys.exit(0)


def main() -> None:
    raw = sys.stdin.read()
    try:
        json.loads(raw)
    except Exception:                                            # noqa: BLE001
        decide(False, "unreadable permission request — failing closed")
    binary = shutil.which("finrail")
    if not binary:
        decide(False, "finrail is not on PATH — failing closed")
    # How long to wait for `finrail policy`: the person's answer is waited for inside
    # it, and core puts the number on this hook's command line (`scope.ask_settings`,
    # beside the question's own wait), so the rule is not copied here. Without one, the
    # deciding gate's wait.
    try:
        wait = int(sys.argv[1])
    except (IndexError, ValueError):
        wait = 25
    try:
        r = subprocess.run([binary, "policy", "--permission"], input=raw,
                           capture_output=True, text=True, timeout=wait)
    except Exception as e:                                      # noqa: BLE001
        decide(False, f"cannot reach finrail policy ({type(e).__name__}) — failing closed")
    if r.returncode != 0:
        decide(False, f"finrail policy failed — failing closed: {r.stderr[:150]}")
    text = ((r.stdout or "").strip().splitlines() or [""])[0].strip()
    answer, _, reason = text.partition(":")
    answer = answer.strip().lower()
    if answer == "allow":
        decide(True)
    if answer == "deny":
        decide(False, reason.strip() or "refused")
    decide(False, f"unrecognised policy answer {text[:80]!r} — failing closed")


if __name__ == "__main__":
    main()
