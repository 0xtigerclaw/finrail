#!/usr/bin/env python3
"""The workspace lock — a THIN WRAPPER around finrail's own policy.

It contains no rules, no patterns and no risk judgements of its own. It asks
`finrail policy <command>` and relays the answer, so there is exactly ONE implementation
of "what may run here" and no way for the hook and the tool to disagree.

Two things about the OUTPUT, both learned by getting them wrong:

  * The schema Claude Code reads is `hookSpecificOutput.permissionDecision`. An earlier
    version printed `{"decision": ...}`, which Claude Code ignored entirely — so every
    call silently fell through to the coarse `permissions.allow` list, where
    `Bash(finrail:*)` failed to match any command containing `=`. The result was a
    session where `finrail get mail.inbox` worked but
    `finrail get mail.inbox account=x` was refused, with no stated reason.

  * It emits an explicit "allow" rather than staying silent on the happy path. Silence
    means "fall through to the permission list", which re-introduces exactly that
    second, dumber matcher. Being authoritative in both directions is the point of
    having one source of truth.

Fails CLOSED: if finrail cannot answer, nothing runs.

Install as a PreToolUse hook (see ~/.finrail/claude-lock.json):
    {"matcher": "Bash",    "hooks": [{"type": "command", "command": "python3 <this>"}]}
    {"matcher": "mcp__.*", "hooks": [{"type": "command", "command": "python3 <this>"}]}
"""
import json
import shutil
import subprocess
import sys


#: Opening a local page in the in-app preview pane. Named individually, not by prefix: the
#: point of the rule below is that a whole MCP namespace is not a safe unit to allow, and
#: that argument applies to this one too. Nothing here fetches, sends or writes.
PREVIEW_TOOLS = (
    "mcp__Claude_Browser__preview_start",
    "mcp__Claude_Browser__navigate",
)


def decide(kind: str, reason: str) -> None:
    """Answer in the schema Claude Code actually reads, then stop."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": kind,
        "permissionDecisionReason": reason}}))
    sys.exit(0)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        decide("deny", "unreadable hook payload — failing closed")

    tool = data.get("tool_name") or data.get("tool") or ""
    params = data.get("tool_input") or data.get("parameters") or {}

    # Anything that is not a shell command is not this session's business to allow: the
    # whole point is that finrail is the only surface.
    # Showing the user something finrail already produced is not an escape from the
    # lock: these surface an existing file, they cannot create one, because Write stays
    # denied. Presentation was the single thing this workspace had no way to do, so a
    # session could only ever offer to describe output instead of showing it.
    #
    # The preview pane is the best of them — it opens a local file in the panel, with no
    # card to click and nothing sent anywhere. It is reached through an MCP tool, and the
    # blanket "deny every mcp__*" rule below is what hid it: the previous system's hook
    # denied the external CONNECTORS (mail, calendar) rather than the whole namespace, and
    # denying a namespace to block a category takes the useful things with it.
    if tool in ("SendUserFile", "Artifact", *PREVIEW_TOOLS):
        decide("allow", "showing the user a file finrail rendered")

    if tool != "Bash":
        decide("deny",
               f"this session runs finrail only; '{tool}' is not available here. "
               f"Use `finrail capabilities` to see what exists.")

    command = (params.get("command") or "").strip() if isinstance(params, dict) else ""
    if not command:
        decide("deny", "empty command")

    binary = shutil.which("finrail")
    if not binary:
        decide("deny", "finrail is not on PATH — failing closed")

    try:
        r = subprocess.run([binary, "policy", command],
                           capture_output=True, text=True, timeout=20)
    except Exception as e:                                      # noqa: BLE001
        decide("deny", f"cannot reach finrail policy ({type(e).__name__}) — "
                       f"failing closed")
    if r.returncode != 0:
        decide("deny", f"finrail policy failed — failing closed: {r.stderr[:150]}")

    text = (r.stdout or "").strip()
    answer, _, reason = text.partition(":")
    answer = answer.strip().lower()
    if answer not in ("allow", "ask", "deny"):
        decide("deny", f"unrecognised policy answer {text[:80]!r} — failing closed")
    decide(answer, reason.strip() or "per finrail policy")


if __name__ == "__main__":
    main()
