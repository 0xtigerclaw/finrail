"""CONSOLE extension: the web console, declared as a plugin like any other.

The console holds no rules of its own. It reads capabilities, their steps and their rules
from the registry, and it dispatches through `dispatch_core`, the same path as the CLI.
Declaring it here makes that structural rather than a promise:

  * it appears in `finrail get plugins.list` beside every other plugin;
  * the libraries it needs are its own requirements, so `doctor` names a missing one and
    an install without the console carries no web server;
  * it can be turned off (`finrail do plugin.disable name=console`), and `finrail-console`
    then refuses to start, because a disabled plugin has no surface.

It declares no capabilities: a presentation layer that could act on its own would be a
second, privileged path, and the harness has exactly one.
"""
from finrail.core.requirements import PythonPackage

NAME = "console"
DESCRIPTION = "The web console: a presentation plugin over the same dispatcher as the CLI."

#: The plugin API this was written against (see finrail.API).
REQUIRES_API = 1

COMMANDS: list = []


def REQUIREMENTS():
    why = "serves the web console"
    return [PythonPackage("fastapi", why=why),
            PythonPackage("uvicorn", why=why),
            PythonPackage("multipart", install="python-multipart", why="reads the console's forms")]


def enabled() -> bool:
    """Is the console plugin installed and turned on in this state directory?"""
    from finrail.core import registry
    return any(p["name"] == NAME and p["enabled"] for p in registry.installed())
