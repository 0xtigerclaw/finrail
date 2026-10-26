"""The failure vocabulary.

The governing rule of this system: **a failure is never returned as data that looks like
success.** Every one of these is raised, and the dispatcher records the run as `failed`
when it sees one. Nothing silently half-works.
"""


class FinrailError(Exception):
    """Base for everything this system raises deliberately."""


class OpFailed(FinrailError):
    """An operation failed. Raised, never returned — a caller cannot mistake it for a
    result. (The original sin this replaces: returning {"error": ...} that a renderer
    then printed as if the work had been done.)"""


class EngineFailed(OpFailed):
    """The model engine failed, so the model never answered: it could not be started, it
    stopped with an error of its own (a failed start, not logged in, a rate limit), or it
    ran out of time.

    Told apart from an answer the model got wrong, which stays a plain `OpFailed`, because
    the two mean different things to a caller that keeps track of what it has read. On
    4 Oct 169 of the 170 calls of the nightly learning failed as the engine started, and
    the pass marked 2,762 journal rows as read that no model had seen.

    A prompt the engine refuses for its size is NOT this: it is refused again every time,
    so it is the input's failure, and a plain `OpFailed` (the 170th call that night).
    """


class Unverified(OpFailed):
    """A side effect ran but could NOT be proven to have taken effect.

    This is deliberately a failure, not a warning. Sending mail that never arrived in
    Sent, or deleting a message that is still on the server, is a failure even though
    the call returned 200. Providers raise this when their verification step comes back
    negative or inconclusive.
    """


class ContractViolation(FinrailError):
    """Input or content broke a declared contract: an unknown parameter, a wrongly typed
    value, a page missing required frontmatter, a hallucinated id."""


class NeedsPerson(ContractViolation):
    """An approval asked for from inside an agent, refused because only a person
    approves. The run is NOT dropped: it stays pending, and the message says how the
    person approves it. `run` and `capability` let a caller (a plugin that builds and
    then approves) report "proposed, waiting for you" instead of a failure."""

    def __init__(self, run: str, capability: str, why: str):
        self.run, self.capability, self.why = run, capability, why
        super().__init__(
            f"{capability} run {run} is waiting for a person: it is {why}. An agent "
            f"cannot approve for someone, even when they asked it to, so nothing was "
            f"approved and nothing is lost: the run is still pending. To approve it, "
            f"the person sends /approve {run} in this chat, or runs `finrail approve "
            f"{run}` in a terminal on the server. To read it first: /view {run}")


class NotConfigured(FinrailError):
    """A capability was invoked before its configuration exists.

    Carries the exact command that fixes it, so a fresh operator (or a Claude Code
    session that can only run `finrail`) is never left guessing.
    """

    def __init__(self, what: str, fix: str = ""):
        self.what = what
        self.fix = fix
        msg = f"not configured: {what}"
        if fix:
            msg += f"\n  fix: {fix}"
        super().__init__(msg)
