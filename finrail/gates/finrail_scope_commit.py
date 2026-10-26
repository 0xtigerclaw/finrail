#!/usr/bin/env python3
"""The scoped agent's version control — a PostToolUse hook over `commit_roots`.

Installed by `finrail.core.scope.materialise` beside the PreToolUse gate whenever a
scope declares `commit_roots`:

    {"matcher": "Write|Edit|MultiEdit|NotebookEdit",
     "hooks": [{"type": "command",
                "command": "/usr/bin/python3 <this> <workdir>/scope.json"}]}

Every file the agent writes under one of those roots is added, committed as
`<scope name>: update <path relative to the root>`, and pushed. That is the whole of it:
an agent whose shell may not run git still has its work versioned, and it never has to
remember to save.

Unlike the gate, this holds its own git calls rather than relaying to finrail. The gate
is thin because a RULE in two places is free to disagree with itself; this is an action,
and its whole policy — which roots, which message, that it pushes — is read from
scope.json, which the same `materialise` regenerates every turn.

It fails SOFT, which is the opposite of the gate and deliberate: the write has already
happened. A failed push warns into the transcript and leaves the commit on disk; nothing
here refuses a turn that already did its work.
"""
import json
import pathlib
import subprocess
import sys


def warn(message: str) -> None:
    print(json.dumps({"systemMessage": message}))


def git(repo: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          timeout=20)


def main() -> None:
    scope_file = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        scope = json.loads(pathlib.Path(scope_file).read_text())
        payload = json.loads(sys.stdin.read())
    except Exception:                                           # noqa: BLE001
        return                                                  # nothing to commit into
    written = (payload.get("tool_input") or {}).get("file_path") \
        or (payload.get("tool_input") or {}).get("notebook_path") \
        or (payload.get("tool_response") or {}).get("filePath") or ""
    if not written:
        return
    try:
        path = pathlib.Path(str(written)).resolve()
    except OSError:
        return
    roots = [pathlib.Path(r) for r in (scope.get("commit_roots") or [])]
    root = next((r for r in roots if r == path or r in path.parents), None)
    if root is None:
        return                              # written outside the versioned roots
    repo = str(root)
    name = scope.get("name") or "finrail"
    rel = str(path.relative_to(root))
    try:
        git(repo, "add", "-A")
        if not git(repo, "diff", "--cached", "--quiet").returncode:
            return                                              # nothing staged
        r = git(repo, "commit", "-q", "-m", f"{name}: update {rel}")
        if r.returncode:
            warn(f"{name}: could not commit {rel} — {(r.stderr or r.stdout).strip()[:200]}")
            return
        p = git(repo, "push", "-q", "origin", "HEAD")
        if p.returncode:
            warn(f"{name}: {rel} is committed locally but the push to origin failed — "
                 f"check the remote or the credentials.")
    except Exception as e:                                      # noqa: BLE001
        warn(f"{name}: version control for {rel} failed ({type(e).__name__}); the file "
             f"itself is written.")


if __name__ == "__main__":
    main()
