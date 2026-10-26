"""PROGRESS — what a long call is doing, while it is still doing it.

A judged capability can run for minutes: the agent with tools for up to an hour, a
document enrichment through a whole batch. Until now the only record was written when the
call RETURNED, so anyone watching saw an instruction and then nothing — indistinguishable
from a stalled run, and the reason an operator asked "what is it doing?" and had no way to
find out.

The sink is a context variable rather than a parameter threaded through every capability:
`llm.call` does not know which run or session it serves, and should not have to. Whoever
dispatches sets the sink; whoever streams emits into it.
"""
import contextlib
import contextvars
import datetime

from . import log as _log

_logger = _log.get(__name__)

#: Keep the last N steps. This is a live view, not an archive — the transcript and the
#: run result are the record. Unbounded, a chatty agent would rewrite a growing file on
#: every tool call.
KEEP = 40

_SINK = contextvars.ContextVar("finrail_progress_sink", default=None)


def emit(kind: str, text: str, **fields) -> None:
    """Report one step. Never raises: progress reporting must not be able to fail the
    work it is reporting on.

    `fields` are measurements a reader can query without parsing the text: which tool,
    how long it took, how many bytes came back. Only plain values; `at`, `kind` and
    `text` cannot be overridden."""
    fn = _SINK.get()
    if fn is None:
        return
    try:
        body = text or ""
        entry = {"at": datetime.datetime.now().isoformat(timespec="seconds"),
                 "kind": kind, "text": body[:300]}
        for k, v in fields.items():
            if k not in entry and v is not None and isinstance(v, (str, int, float, bool)):
                entry[k] = v
        # The 300 characters are for the live view; the rest is not junk, it is just too
        # long to redraw. When the archive is on, the untrimmed text rides along under
        # `full` for whoever persists the entry to hand on — see `sessions.step`, which
        # strips it back out before the session record is written. Sinks that do not know
        # about it are unaffected: it is one extra key in a dict they already copy.
        from . import retention
        if len(body) > 300 and retention.active():
            entry["full"] = body
        fn(entry)
    except Exception:                                              # noqa: BLE001
        # Never raise: progress reporting must not fail the work it reports on.
        # But a sink that throws every time means nobody is seeing progress at
        # all, and silence made that indistinguishable from a quiet run.
        _logger.debug("progress sink raised; step dropped", exc_info=True)


@contextlib.contextmanager
def sink(fn):
    """Route steps somewhere for the duration of a call."""
    token = _SINK.set(fn)
    try:
        yield
    finally:
        _SINK.reset(token)


def trim(steps: list) -> list:
    return list(steps or [])[-KEEP:]
