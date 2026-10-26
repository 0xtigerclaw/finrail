"""LAUNCHER — build, install and probe the app every launchd service starts through.

Why it exists is written down beside its source, in `finrail/launcher/__init__.py`. In
one line: macOS remembers consent to read an external volume against the program that
asked, a bare interpreter is remembered by a versioned path, and every upgrade therefore
produced a program nobody had approved — which blocked every service in silence.

What this module owns:

    app_path()        where the built app lives — a fixed place on the internal disk
    wrap(command)     the argv launchd is given: [launcher, *command] once it is installed
    build()           compile, write the bundle, sign it, and swap it into place
    fingerprint()     which source the installed app was built from
    protected_roots() the external volumes this install actually reads
    probe(roots)      does a launchd job started through the launcher read them — now?

Everything here is launchd-only. `ServiceSpec.render` is the launchd backend's renderer
and the only caller of `wrap`; the systemd backend writes its own units and never sees
this, because Linux has no equivalent consent to survive.
"""
import hashlib
import os
import pathlib
import plistlib
import shutil
import subprocess
import tempfile
import time

from .errors import OpFailed

#: The identity macOS files the consent under. CHANGING THIS REVOKES IT: a new bundle ID
#: is a new program, and every service would block on a fresh dialog. It is a constant
#: for exactly that reason.
BUNDLE_ID = "com.finrail.launcher"
APP_NAME = "Finrail.app"
EXECUTABLE = "finrail-launcher"
FINGERPRINT_FILE = "finrail-launcher.sha256"

#: `service.launcher` values that switch the launcher off, for diagnosis.
DISABLED = ("none", "off", "false", "0", "")


def source_dir() -> pathlib.Path:
    """The C source and Info.plist, shipped inside the package like the engine's gates."""
    return pathlib.Path(__file__).resolve().parent.parent / "launcher"


def app_path() -> pathlib.Path:
    """Where the built app lives, or an empty path when the launcher is switched off.

    `~/Applications`, on the internal disk, deliberately. Not the state directory, which
    can be moved onto the very volume this protects — at which point launchd would need
    consent to read the program that carries the consent. Not the package, which every
    `uv tool install` rewrites at a new path. The env var is the test seam, exactly as
    FINRAIL_LAUNCH_AGENTS is for the plists themselves.
    """
    v = os.environ.get("FINRAIL_LAUNCHER")
    if v is None:
        from .settings import raw_location
        v = raw_location("service.launcher")
    if v is None:
        return pathlib.Path.home() / "Applications" / APP_NAME
    if str(v).strip().lower() in DISABLED:
        return pathlib.Path("")
    return pathlib.Path(str(v)).expanduser()


def executable(app: pathlib.Path = None) -> pathlib.Path:
    app = app_path() if app is None else app
    return app / "Contents" / "MacOS" / EXECUTABLE


def enabled() -> bool:
    return str(app_path()) not in ("", ".")


def installed() -> bool:
    return enabled() and executable().is_file() and os.access(executable(), os.X_OK)


def wrap(command: list) -> list:
    """The argv a launchd job runs: through the launcher once it exists, as-is before.

    A property of the FILESYSTEM, never of the caller. The plist this feeds is compared
    byte for byte against a fresh render to decide whether a service is stale, and a
    render that depended on who was asking — which is what `path_env` once did with the
    caller's PATH — made every service read stale to every other shell, permanently.
    Whether the launcher is installed is the same answer for everyone.
    """
    if not command or not installed():
        return list(command)
    exe = str(executable())
    if command[0] == exe:
        return list(command)                 # never nested, however many times it is asked
    return [exe] + list(command)


# ── what the installed app was built from ────────────────────────────────────
def fingerprint(src: pathlib.Path = None) -> str:
    """A hash of everything that decides the binary: the C source, the Info.plist, and
    the bundle ID. If it matches the installed app's record, a rebuild would change
    nothing except the signature — and a changed signature costs a dialog."""
    src = source_dir() if src is None else src
    h = hashlib.sha256()
    for name in ("launcher.c", "Info.plist"):
        h.update(name.encode())
        h.update((src / name).read_bytes())
    h.update(BUNDLE_ID.encode())
    return h.hexdigest()


def installed_fingerprint(app: pathlib.Path = None) -> str:
    app = app_path() if app is None else app
    p = app / "Contents" / "Resources" / FINGERPRINT_FILE
    return p.read_text().strip() if p.is_file() else ""


def signed_identifier(app: pathlib.Path = None) -> str:
    app = app_path() if app is None else app
    r = subprocess.run(["codesign", "-dv", str(app)], capture_output=True, text=True)
    for line in (r.stderr or "").splitlines():
        if line.startswith("Identifier="):
            return line.split("=", 1)[1].strip()
    return ""


def _compiler() -> str:
    found = shutil.which("clang")
    if found:
        return found
    r = subprocess.run(["xcrun", "--find", "clang"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def build(app: pathlib.Path = None) -> pathlib.Path:
    """Compile, assemble, sign, and swap the app into place in one step.

    Built beside the destination and renamed over it, so there is never a moment when
    launchd can start a service and find half a launcher. The fingerprint is written
    BEFORE signing, because the signature seals Resources — a record added afterwards
    would break the very signature the consent is bound to.
    """
    app = app_path() if app is None else app
    if not str(app) or str(app) == ".":
        raise OpFailed("the launcher is switched off (service.launcher) — nothing to build")
    cc = _compiler()
    if not cc:
        raise OpFailed("building the launcher needs a C compiler — install the Xcode "
                       "command line tools: xcode-select --install")
    src = source_dir()
    app.parent.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix=".finrail-launcher-", dir=app.parent))
    try:
        new = work / APP_NAME
        (new / "Contents" / "MacOS").mkdir(parents=True)
        (new / "Contents" / "Resources").mkdir(parents=True)
        r = subprocess.run([cc, "-O2", "-Wall", "-Werror", "-mmacosx-version-min=13.0",
                            "-o", str(executable(new)), str(src / "launcher.c")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise OpFailed(f"compiling the launcher failed: {(r.stderr or r.stdout)[-600:]}")
        shutil.copyfile(src / "Info.plist", new / "Contents" / "Info.plist")
        (new / "Contents" / "Resources" / FINGERPRINT_FILE).write_text(fingerprint(src) + "\n")
        r = subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID,
                            str(new)], capture_output=True, text=True)
        if r.returncode != 0:
            raise OpFailed(f"signing the launcher failed: {(r.stderr or r.stdout)[-400:]}")
        r = subprocess.run(["codesign", "--verify", "--strict", str(new)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise OpFailed(f"the signed launcher does not verify: {(r.stderr or r.stdout)[-400:]}")
        old = work / "previous.app"
        if app.exists():
            app.rename(old)
        new.rename(app)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return app


# ── what it has to be able to read ───────────────────────────────────────────
def _external_root(path: str) -> str:
    """`/Volumes/<name>` for a path on a mounted external volume, else "".

    Resolved first, because `~/src` here is a symlink to `/Volumes/storage/src`: a path
    that does not LOOK external can still be one, and it was exactly that path that every
    finrail command read to load its own code."""
    try:
        real = os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError):
        return ""
    parts = pathlib.PurePosixPath(real).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        root = f"/Volumes/{parts[2]}"
        return root if os.path.ismount(root) else ""
    return ""


def protected_roots() -> list:
    """The external volumes this install reads, derived rather than configured: every
    path-kind setting, plus the state, content and source roots."""
    from . import registry
    from .settings import home, source, state
    candidates = [state(), home(), source()]
    try:
        declared = registry.settings().declared()
        values = registry.settings().all()
    except Exception:                                              # noqa: BLE001
        declared, values = {}, {}
    for name, spec in declared.items():
        if getattr(spec, "kind", "") == "path" and values.get(name):
            candidates.append(values[name])
    return sorted({r for r in (_external_root(c) for c in candidates) if r})


def probe(roots: list, timeout: float = 10.0) -> tuple:
    """(ok, detail) for "can a service started through the launcher read these, now?"

    It has to go THROUGH LAUNCHD. Run the launcher as a child of this process and macOS
    attributes its reads to whatever started this process — Terminal, the Claude app —
    and the answer is about their consent, not the launcher's. A one-shot job is the only
    honest test, which is what the two experiments that justified the launcher were.

    ok is True when every root is readable, False when access was refused outright, and
    None when nothing came back in time: the read is WAITING, which means a consent
    dialog is open on the Mac's screen. That third state is the outage this exists to
    name, and it must never be reported as a denial or as success.
    """
    from .services import launchctl
    if not roots:
        return (True, "no external volume is in use")
    if not installed():
        return (False, "the launcher is not installed")
    work = pathlib.Path(tempfile.mkdtemp(prefix="finrail-launcher-probe-"))
    label = f"{BUNDLE_ID}.probe.{os.getpid()}"
    result = work / "result"
    script = ('for d do /bin/ls "$d" >/dev/null 2>&1 || { echo "refused $d" > "$0"; exit 1; }; '
              'done; echo ok > "$0"')
    plist = work / f"{label}.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": [str(executable()), "/bin/sh", "-c", script, str(result), *roots],
        "RunAtLoad": True,
    }))
    domain = f"gui/{os.getuid()}"
    try:
        subprocess.run([launchctl(), "bootstrap", domain, str(plist)],
                       capture_output=True, text=True, timeout=15)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result.is_file() and result.read_text().strip():
                break
            time.sleep(0.2)
        text = result.read_text().strip() if result.is_file() else ""
    finally:
        subprocess.run([launchctl(), "bootout", f"{domain}/{label}"],
                       capture_output=True, text=True, timeout=15)
        shutil.rmtree(work, ignore_errors=True)
    return interpret(text, roots, timeout)


def interpret(text: str, roots: list, timeout: float) -> tuple:
    """Separate from `probe` so the three outcomes are testable without launchd."""
    if text == "ok":
        return (True, f"services started through {APP_NAME} can read {', '.join(roots)}")
    if text.startswith("refused"):
        where = text.split(" ", 1)[1] if " " in text else ", ".join(roots)
        return (False, f"macOS REFUSED {APP_NAME} access to {where} — allow it in System "
                       f"Settings → Privacy & Security → Files and Folders → Finrail")
    return (None, f"no answer from {', '.join(roots)} within {timeout:g}s — the read is "
                  f"WAITING, which means macOS is asking for permission on this Mac's "
                  f"screen. Approve Finrail there; until then every service that touches "
                  f"the volume is blocked")
