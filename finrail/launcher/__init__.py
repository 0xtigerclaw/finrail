"""THE LAUNCHER — why every finrail service on a Mac starts through a tiny app.

macOS guards removable and external volumes behind a privacy consent, and remembers
each consent against the program that asked. For an app bundle that is its bundle ID,
which survives every update. For a bare binary it is the binary's PATH — and the
interpreters finrail runs are bare binaries at paths with a version in them:
`Cellar/python@3.14/3.14.6`, `Cellar/node/25.8.1_1`, `uv/python/cpython-3.14.3`.

So every `brew upgrade` or `uv tool install` produced a program macOS had never been
asked about. The first time it touched `/Volumes/storage` a consent dialog appeared on
a screen nobody was watching, and the process asking simply WAITED — no error, no
timeout. On 2026-09-17 a switch to a uv tool install moved cron, both chat agents and
the CLI onto a new interpreter at once; tccd logged the unanswered request every minute
for hours, the agents sat silent, and one turn ran its full hour before timing out.

The fix is measured, not assumed. macOS attributes a process's file access to the
first non-Apple process in its tree that launchd started. A bare `/bin/zsh` at the root
did NOT take that role — the Python child was asked about anyway. A compiled program
inside an app bundle DID: its child's reads were attributed to the bundle ID, one
approval covered them, and re-running under a copy of the interpreter at a path macOS
had never seen was allowed in 0.03s with no request naming the new path at all.

So this directory holds the source of that app — a C program and its Info.plist —
and `finrail.core.launcher` builds, signs, installs and probes it. The BUILT app does
not live here: it must sit at one fixed place on the internal disk, outside anything a
package manager rewrites, and it must not be rebuilt casually, because an ad-hoc
signature binds the consent to that exact binary. Rebuilding means one more dialog.
"""
