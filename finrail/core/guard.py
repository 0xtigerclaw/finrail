"""The write guard — how a plugin lets other plugins act on its writes.

Any capability that writes durable content wraps it:

    with guard.writing(path):
        path.write_text(...)

That emits `before_write` / `after_write`, which is how the repository plugin can pull
before and commit after a write into a managed directory — with no git code in the
capability, and no knowledge of git in core.
"""
import contextlib


@contextlib.contextmanager
def writing(path, what: str = ""):
    from . import registry
    hooks = registry.hooks()
    hooks.emit("before_write", str(path))
    try:
        yield
    finally:
        hooks.emit("after_write", str(path))


def wrote(path, what: str = "") -> None:
    """For a write that already happened (an external tool produced the file)."""
    from . import registry
    registry.hooks().emit("after_write", str(path))
