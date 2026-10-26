"""LANDING — one change reaches main at a time, tested against the main it will join.

Six agents were working this codebase at once, in six worktrees, and the thing that broke
was never the editing. It was the landing. `origin/main` moved four times under one
session in an afternoon: each agent tested its branch against the main it started from,
merged, and pushed — and what it had proved green was a tree nobody ever ran.

Two properties make that impossible, and both need a lock to hold:

  SERIALISED   one landing at a time per repository. Not because merging is slow, but
               because "fetch, rebase, test, push" is only meaningful if nothing else
               completes the same sequence while it runs.
  REBASED      the branch is replayed onto the CURRENT origin/main and the suite runs
               THERE, so the tree that passed is byte-for-byte the tree that lands. A
               suite run before the rebase proves something about a tree that is about to
               stop existing.

The merge is still `--no-ff` afterwards, so the history says a branch landed and names it.
That is a statement about the log; correctness comes from the rebase underneath it.

A conflict is not handled here. It is reported, the rebase is aborted, and the branch is
left exactly as it was — resolving it needs the person or agent who wrote the change, in
the worktree where they can see both sides.

A MAINLINE THE CHECKOUT IS NOT ON (7 Oct). The dev agents land on `dev`, not `main`, so
their work is used before it is anybody's main. The checkout under `~/src` stays on main
whatever lands: the live agents and `finrail upgrade` read it. So when the mainline is
not the branch the checkout has, the merge is made in the branch's own worktree, detached
at `origin/<mainline>`, and pushed from there. The checkout never moves.
"""
import fcntl
import os
import pathlib
import subprocess


class Busy(Exception):
    """Another landing holds this repository."""


class Rejected(Exception):
    """The branch cannot land, and the reason is the message."""


def git(repo, *args, timeout: int = 900) -> tuple:
    """(ok, output). Never raises: a step that cannot run is a step that failed."""
    try:
        p = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return (False, f"{type(e).__name__}: {e}")
    return (p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip())


def lock_path(repo) -> pathlib.Path:
    """Inside `.git`, so it is per repository and never committed. A worktree's `.git` is
    a FILE pointing at the real one, so the common directory is asked for rather than
    assumed — six worktrees of one repository must contend for one lock, not six."""
    ok, common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    base = pathlib.Path(common.strip()) if ok and common.strip() else pathlib.Path(repo) / ".git"
    return base / "finrail-landing.lock"


class held:
    """The lock, as a context manager. Non-blocking: a second landing is told to wait
    rather than queued, because a queue of six agents each holding a shell open for a
    hundred-second suite is worse than being told to come back."""

    def __init__(self, repo):
        self.path = lock_path(repo)
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.seek(0)
            who = (self.fh.read() or "").strip() or "another landing"
            self.fh.close()
            raise Busy(f"{who} is landing a change in this repository — wait for it")
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(f"pid {os.getpid()}")
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()
        return False


def worktree_of(repo, branch: str) -> str:
    """Where that branch is checked out, which is where its tests run."""
    ok, out = git(repo, "worktree", "list", "--porcelain")
    if not ok:
        return ""
    path = ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1].strip()
        elif line.strip() == f"branch refs/heads/{branch}":
            return path
    return ""


def tidy(repo, branch: str, tree: str, merged_into: str = "") -> tuple:
    """Remove the worktree and delete the branch, here and on the remote. `(ok, detail)`.

    In the same breath as the merge, because that is the only moment it is reliably true.
    This used to be a sentence in the result for a person to paste, and the five merged
    branches sitting in `~/src` with three worktrees still on disk — weeks after the work
    in them had landed — are what that sentence achieved. A merged branch that still
    exists is a question mark.

    AFTER the push and nowhere else: until the push the branch is the only copy of the
    work, and every refusal above leaves it exactly where it was.

    A failure here does NOT fail the landing. What was asked for is merged and pushed; a
    worktree that would not come away is a loose end to name, not a reason to report a
    landed change as unlanded. `branch -d`, never `-D`: a branch git still thinks is
    unmerged immediately after merging it is a fact to look at, not one to force past.

    `merged_into` is the ref the branch landed on when that is not what the checkout has
    (`origin/dev`). `-d` asks only the checkout's HEAD, which never saw the merge, so it
    would refuse every such branch. Then the ref is asked directly, and only a branch it
    already contains is deleted, with `-D`: the same guarantee, checked against the right
    branch.
    """
    notes, bad = [], []
    # NO WORKTREE IS NOT A FAILURE (6 Oct). `repo.tidy` calls this for a branch whose
    # worktree may already be gone: the agent removed it from inside its sandbox, which
    # it can, and then could not delete the branch, which it cannot (see `repo.tidy`).
    ok, out = git(repo, "worktree", "remove", tree) if tree else (True, "")
    if tree:
        notes.append(f"worktree {tree} removed") if ok else bad.append(
            f"worktree {tree} left in place: {out.strip()}")
    if ok:
        # Only once the worktree is gone: git refuses to delete a branch checked out
        # somewhere, and rightly.
        if merged_into:
            ok, out = git(repo, "merge-base", "--is-ancestor", branch, merged_into)
            ok, out = (git(repo, "branch", "-D", branch) if ok else
                       (False, f"not contained in {merged_into}, so not deleted"))
        else:
            ok, out = git(repo, "branch", "-d", branch)
        notes.append(f"branch {branch} deleted") if ok else bad.append(
            f"branch {branch} left: {out.strip()}")
    if git(repo, "rev-parse", "--verify", f"refs/remotes/origin/{branch}")[0]:
        # Only if it was ever pushed. A `--delete` of a branch the remote does not have is
        # an error, and an error here would read as a cleanup that went wrong.
        ok, out = git(repo, "push", "origin", "--delete", branch)
        notes.append(f"origin/{branch} deleted") if ok else bad.append(
            f"origin/{branch} left: {out.strip()}")
    return (not bad, "; ".join(notes + bad) or "nothing to remove")


def land(repo, branch: str, mainline: str = "main", test=None) -> dict:
    """Rebase onto the current origin/main, prove it there, then merge and push.

    `test` is a callable taking the worktree path and returning (ok, detail), so the
    caller decides what "proved" means — the repository under change knows its own suite,
    and core does not.
    """
    repo = str(pathlib.Path(repo).expanduser())
    steps = []

    def step(name, ok, detail):
        steps.append({"step": name, "ok": bool(ok), "detail": (detail or "")[-2000:]})
        return ok

    with held(repo):
        ok, out = git(repo, "fetch", "origin")
        if not step("fetch", ok, out):
            raise Rejected(f"could not fetch: {out}")
        if not git(repo, "rev-parse", "--verify", "--quiet",
                   f"refs/remotes/origin/{mainline}")[0]:
            raise Rejected(f"origin has no branch {mainline} to land on. A person makes it "
                           f"once: git -C {repo} push origin origin/main:refs/heads/"
                           f"{mainline}")

        tree = worktree_of(repo, branch)
        if not tree:
            raise Rejected(f"{branch} is not checked out in any worktree — the suite has "
                           f"nowhere to run. Open one: git -C {repo} worktree add "
                           f"<dev>/{branch} {branch}")

        ok, out = git(tree, "status", "--porcelain")
        if out.strip():
            raise Rejected(f"{tree} has uncommitted changes:\n{out}\nCommit them; what "
                           f"lands is a commit, never a working tree.")

        # REBASED BEFORE TESTED. The other order proves something about a tree that is
        # about to stop existing.
        ok, out = git(tree, "rebase", f"origin/{mainline}")
        if not ok:
            git(tree, "rebase", "--abort")
            step("rebase", False, out)
            raise Rejected(
                f"{branch} does not replay onto origin/{mainline}:\n{out}\n"
                f"The rebase was aborted and the branch is as it was. Resolve it in "
                f"{tree}, where both sides are visible, then land again.")
        step("rebase", True, out)

        ok, detail = test(tree) if test else (True, "no suite was given")
        if not step("test", ok, detail):
            raise Rejected(f"the suite does not pass on the rebased tree:\n{detail}\n"
                           f"Nothing was merged.")

        ok, here = git(repo, "branch", "--show-current")
        if not ok or here.strip() != mainline:
            return _land_in_worktree(repo, branch, mainline, tree, step, steps)

        ok, out = git(repo, "checkout", mainline)
        if not step("checkout", ok, out):
            raise Rejected(f"could not check out {mainline}: {out}")
        ok, out = git(repo, "merge", "--ff-only", f"origin/{mainline}")
        if not step("update", ok, out):
            raise Rejected(f"{mainline} here is not a fast-forward of origin: {out}")

        # --no-ff for the LOG. The tree is already identical to the one just proved; the
        # merge commit is there so the history says which branch landed.
        ok, out = git(repo, "merge", "--no-ff", "-m", f"Merge branch '{branch}'", branch)
        if not step("merge", ok, out):
            git(repo, "merge", "--abort")
            raise Rejected(f"the merge failed after a clean rebase, which should not "
                           f"happen: {out}")

        ok, out = git(repo, "push", "origin", mainline)
        if not step("push", ok, out):
            raise Rejected(f"merged locally but NOT pushed: {out}")

        ok, head = git(repo, "rev-parse", "--short", "HEAD")
        cleaned = step("cleanup", *tidy(repo, branch, tree))
        return {"repo": repo, "branch": branch, "mainline": mainline,
                "head": head.strip(), "worktree": tree, "cleaned": cleaned,
                "steps": steps}


def _land_in_worktree(repo: str, branch: str, mainline: str, tree: str, step,
                      steps: list) -> dict:
    """Merge and push from the branch's own worktree, for a mainline the checkout is not
    on. Called with the lock held, after the rebase and the suite.

    The worktree is detached at `origin/<mainline>` and the branch merged into it there,
    `--no-ff` as on main, so the history names what landed. The push names the remote
    branch whole (`HEAD:refs/heads/<mainline>`), so nothing local called <mainline> is
    made or needed. A push refused because somebody else pushed first fails the landing,
    as on main: the branch is still there, rebased, and landing again replays it."""
    ok, out = git(tree, "checkout", "--detach", f"origin/{mainline}")
    if not step("checkout", ok, out):
        raise Rejected(f"could not detach {tree} at origin/{mainline}: {out}")
    ok, out = git(tree, "merge", "--no-ff", "-m", f"Merge branch '{branch}' into {mainline}",
                  branch)
    if not step("merge", ok, out):
        git(tree, "merge", "--abort")
        git(tree, "checkout", branch)
        raise Rejected(f"the merge failed after a clean rebase, which should not happen: "
                       f"{out}")
    ok, out = git(tree, "push", "origin", f"HEAD:refs/heads/{mainline}")
    if not step("push", ok, out):
        git(tree, "checkout", branch)
        raise Rejected(f"merged in {tree} but NOT pushed to {mainline}: {out}\n"
                       f"{branch} is back in its worktree, rebased; land it again.")
    ok, head = git(tree, "rev-parse", "--short", "HEAD")
    # The push moved the remote-tracking ref, so the branch is checked against it.
    cleaned = step("cleanup", *tidy(repo, branch, tree, merged_into=f"origin/{mainline}"))
    return {"repo": repo, "branch": branch, "mainline": mainline,
            "head": head.strip(), "worktree": tree, "cleaned": cleaned,
            "steps": steps}
