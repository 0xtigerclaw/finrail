"""LOGGING — the things worth saying that are not results.

finrail's OUTPUT is its interface: `--json` is parsed by callers, and `ask`,
`doctor` and the renderers are read by people. Log lines must never appear in
that stream, which is why this exists rather than a `print`. Two rules follow
and neither is negotiable:

  * nothing is ever written to stdout — stdout belongs to the result
  * WARNING and above reach stderr; everything below only reaches the file

The file lives at `<state>/logs/finrail.log`, so it follows FINRAIL_STATE like
every other piece of state: two agents on one machine keep separate logs without
being told to, and a log is findable from the state directory you already know.

Default level is WARNING. A quiet CLI is the point — an operator running
`finrail get mail.inbox` should see the inbox, not a debug trace. Turn it up
with FINRAIL_LOG_LEVEL=DEBUG when you are asking "why did nothing happen",
which is the question this is for: the silent skip, the swallowed exception, the
branch that decided there was nothing to do.

Logging must never break the work it reports on — the same rule `progress.emit`
follows. If the log file cannot be opened (read-only state, a directory that
does not exist yet, a permissions mistake) it degrades to stderr rather than
raising, because failing a run over a log line would be absurd.
"""

import logging
import os
import sys

ROOT = "finrail"
DEFAULT_LEVEL = "WARNING"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_configured = False


def _level() -> int:
    raw = (os.environ.get("FINRAIL_LOG_LEVEL") or DEFAULT_LEVEL).strip().upper()
    return getattr(logging, raw, logging.WARNING)


def _configure() -> None:
    """Attach handlers once. Safe to call from every get()."""
    global _configured
    if _configured:
        return
    _configured = True                      # set first: a failure below must not retry forever

    logger = logging.getLogger(ROOT)
    logger.setLevel(_level())
    # Never hand these to the root logger: a caller that embeds finrail as a
    # library configures its own logging, and duplicating into it is rude.
    logger.propagate = False

    formatter = logging.Formatter(FORMAT)

    # stderr, WARNING+ only. stdout is the result and must stay parseable.
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(max(_level(), logging.WARNING))
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    try:
        from .settings import logs as logs_dir
        path = logs_dir()
        path.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path / "finrail.log", encoding="utf-8")
        handler.setLevel(_level())
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except Exception:                                              # noqa: BLE001
        # Degrade to stderr. A log that cannot be written is not a reason to
        # fail the command that was being logged.
        logger.debug("no log file; stderr only", exc_info=True)


def get(name: str) -> logging.Logger:
    """`log = log.get(__name__)` at the top of a module, then log.debug(...)."""
    _configure()
    short = name.split(".")[-1] if name else ROOT
    return logging.getLogger(f"{ROOT}.{short}")
