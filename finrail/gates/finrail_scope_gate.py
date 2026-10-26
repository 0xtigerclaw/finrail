#!/usr/bin/env python3
"""The scoped agent's gate — a THIN WRAPPER around `finrail policy --scope`.

Installed by `finrail.core.scope.materialise` as the PreToolUse hook of a scoped agent's
workspace, matched against every tool:

    {"matcher": ".*", "hooks": [{"type": "command",
                                  "command": "/usr/bin/python3 <this> <workdir>/scope.json"}]}

It holds no rules. The whole hook payload goes to `finrail policy --scope <file>` on
stdin and the answer comes back as `allow: …` or `deny: …`, so the one implementation of
"what may this agent do" lives in `core/scope.py` and the hook cannot disagree with it.
Same shape, same reasons, as `finrail_gate.py` for the unscoped lock.

Answers explicitly in both directions — silence would fall through to the coarse
`permissions` list — and fails CLOSED: no scope file, no finrail, no answer, nothing runs.
"""
import json
import shutil
import subprocess
import sys


def decide(kind: str, reason: str, context: str = "", updated=None) -> None:
    out = {"hookEventName": "PreToolUse", "permissionDecision": kind,
           "permissionDecisionReason": reason}
    # What the model is told alongside an allow: an allow's reason never reaches it, so
    # a decision that took a person's answer says so here (core/scope.settle).
    if context:
        out["additionalContext"] = context
    # The tool's input as policy rewrote it: a clarifying question carrying the person's
    # answers, which the engine reads as the answer (core/scope.clarify). Only on allow.
    if updated is not None and kind == "allow":
        out["updatedInput"] = updated
    print(json.dumps({"hookSpecificOutput": out}))
    sys.exit(0)


def main() -> None:
    scope_file = sys.argv[1] if len(sys.argv) > 1 else ""
    if not scope_file:
        decide("deny", "the gate was given no scope file — failing closed")
    raw = sys.stdin.read()
    try:
        json.loads(raw)
    except Exception:
        decide("deny", "unreadable hook payload — failing closed")
    binary = shutil.which("finrail")
    if not binary:
        decide("deny", "finrail is not on PATH — failing closed")
    # How long to wait for `finrail policy`: a scope that may ASK the person waits for
    # the answer inside it, and core writes the number beside the scope (`gate_timeout`,
    # core/scope.py), so the rule is not copied here. A scope file without one gets
    # the deciding gate's wait.
    try:
        wait = int(json.load(open(scope_file)).get("gate_timeout") or 25)
    except Exception:                                            # noqa: BLE001
        wait = 25
    try:
        r = subprocess.run([binary, "policy", "--scope", scope_file], input=raw,
                           capture_output=True, text=True, timeout=wait)
    except Exception as e:                                      # noqa: BLE001
        decide("deny", f"cannot reach finrail policy ({type(e).__name__}) — failing closed")
    if r.returncode != 0:
        decide("deny", f"finrail policy failed — failing closed: {r.stderr[:150]}")
    lines = (r.stdout or "").strip().splitlines() or [""]
    text = lines[0].strip()
    context = next((ln.split(":", 1)[1].strip() for ln in lines[1:]
                    if ln.startswith("context:")), "")
    updated = None
    raw_input = next((ln.split(":", 1)[1].strip() for ln in lines[1:]
                      if ln.startswith("input:")), "")
    if raw_input:
        try:
            updated = json.loads(raw_input)
        except Exception:                                        # noqa: BLE001
            decide("deny", "unreadable rewritten input from policy — failing closed")
        if not isinstance(updated, dict):
            decide("deny", "rewritten input from policy is not an object — failing closed")
    answer, _, reason = text.partition(":")
    answer = answer.strip().lower()
    if answer not in ("allow", "deny"):
        decide("deny", f"unrecognised policy answer {text[:80]!r} — failing closed")
    decide(answer, reason.strip() or "per the agent's scope", context, updated)


if __name__ == "__main__":
    main()
