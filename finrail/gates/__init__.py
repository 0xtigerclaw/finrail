"""The hook scripts the engine runs, INSIDE the package because they are runtime.

These three are invoked by Claude Code as PreToolUse and PostToolUse hooks, by absolute
path, as standalone programs — `/usr/bin/python3 <this file> [args]`. They import nothing
from finrail; they shell out to the `finrail` CLI, so the one implementation of "what
may this agent do" stays in `core/scope.py` and a hook cannot disagree with it.

THEY LIVED IN `tools/` AND THAT COST EVERY AGENT ON THE HOST. `tools/` is beside the
package, not in it, and the wheel ships `finrail` and `finrail_plugins` — so the day
the deploy stopped being an editable install and became a COPY of the commit, all three
were simply absent. Every scoped agent's PreToolUse hook pointed at a file that did not
exist, the hook failed, and the gate fails closed: not one tool call could run, in any
agent, until the path was pointed back at a working tree.

Nothing said so. `finrail` itself ran perfectly, because what broke was not finrail —
it was the three programs finrail tells somebody else to run. That is the whole lesson:
a change to HOW the code is installed has to be verified against everything the runtime
shells out to, not only against the code that does the shelling.

`settings.source()` still points at the checkout and should: it is for content addressed
to whoever edits the code. Machinery is not content, and machinery ships.
"""
