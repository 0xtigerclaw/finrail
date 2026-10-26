"""Prompt documents — the rules the judged capabilities are written against.

Voice, composing rules, routing rules, filing rules: these are content, not code. They
live as markdown so they can be edited without touching the system, and so there is
exactly ONE copy of each — the same file the human reads is the one the model is given.

WHERE they live is a real setting (`rules.dir`, default `<state>/rules`), NOT a guess.
An earlier version searched a hardcoded list of vault-shaped candidates
(`knowledge-vault/rules`, `vault/rules`, `rules`) under the content root. That was wrong
twice over: rules are finrail's own operating content rather than vault knowledge, so a
machine with no vault must still find them; and a wrong guess failed SILENTLY — `load()`
returns the caller's fallback, so a capability just quietly ran without the user's real
rules. (That is exactly how `mail.triage` came to be running without its 9,000-character
rules document.) Now it is declared, settable, and visible to doctor.

A missing rule document is still not an error: every caller passes an in-code fallback so
the system works out of the box. But `has()` lets a caller say so plainly, and `doctor`
reports a missing rules directory rather than letting it pass unnoticed.
"""
import os
import pathlib

from .settings import rules as rules_location


def rules_dir() -> pathlib.Path:
    """The rules directory: env override, then the `rules.dir` setting, then
    `<state>/rules`. The env var stays first so a test or a one-off run can redirect it
    without touching stored settings."""
    override = os.environ.get("FINRAIL_RULES")
    if override:
        return pathlib.Path(override).expanduser()
    return rules_location()


def path(name: str) -> pathlib.Path:
    return rules_dir() / f"{name}.md"


def has(name: str) -> bool:
    return path(name).is_file()


def load(name: str, default: str = "") -> str:
    """The document's content, or `default` if it does not exist.

    Callers pass a real working prompt as `default`, so a fresh install behaves correctly
    with no files present; writing the file is how you take over from the default.
    """
    p = path(name)
    return p.read_text().strip() if p.is_file() else default


def names() -> list:
    d = rules_dir()
    return sorted(p.stem for p in d.glob("*.md")) if d.is_dir() else []
