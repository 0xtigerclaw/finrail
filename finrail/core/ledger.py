"""THE LEDGER'S OWN CHECK: `finrail ledger verify` and `finrail ledger migrate`.

What the chain in core/events.py promises is only worth something if somebody walks it.
`verify` walks every event log and every run history, reports the first break in each
with its line number, says which guarantee applied (chain alone, or chain plus HMAC),
and exits non-zero on any break. It also runs `events.compare`, the older check that the
log and the run files tell the same story.

`migrate` brings a state directory written before schema 2 up to the chain, once: the
originals go to `<state>/ledger-v1-backup/`, every log is rewritten line for line with
chain fields (and signed, when the install has a key), and every run record gets its
schema number. It refuses to run twice, so the backup is always the pre-chain state.
"""
import datetime
import json
import pathlib
import shutil

from . import atomic, events, tracker
from .errors import ContractViolation
from .settings import state

BACKUP = "ledger-v1-backup"


# ── verify ──────────────────────────────────────────────────────────────────
def verify() -> dict:
    """Every log and every run history checked; `ok` is False on any break."""
    files = events.verify_all() + [events.verify_file(p) for p in tracker.histories()]
    disagreements = []
    for thread in events.threads():
        try:
            disagreements.extend(events.compare(thread))
        except Exception as e:                                   # noqa: BLE001
            disagreements.append(f"thread {thread}: compare failed ({e})")
    broken = [f for f in files if not f["ok"]]
    return {"ok": not broken, "guarantee": events.guarantee(), "key": bool(events.key()),
            "files": files, "broken": len(broken),
            "chained": sum(f["chained"] for f in files),
            "unchained": sum(f["unchained"] for f in files),
            "disagreements": disagreements}


def render(report: dict) -> str:
    lines = [f"guarantee: {report['guarantee']}"]
    root = state()
    for f in report["files"]:
        name = _short(f["file"], root)
        if f["break"]:
            b = f["break"]
            lines.append(f"  ✗ {name}: line {b['line']}: {b['reason']}")
        else:
            said = f"{f['chained']} chained"
            if f["unchained"]:
                said += f", {f['unchained']} unchained, pre-v2"
            if f["torn"]:
                said += f", {len(f['torn'])} torn line(s) skipped at " + \
                        ", ".join(str(n) for n in f["torn"][:5])
            lines.append(f"  ✓ {name}: {said}")
    if not report["files"]:
        lines.append("  (no logs yet)")
    if report["disagreements"]:
        lines.append(f"log and run files disagree ({len(report['disagreements'])}):")
        lines.extend(f"  ! {d}" for d in report["disagreements"][:50])
    lines.append(f"{report['chained']} chained line(s), {report['unchained']} unchained "
                 f"pre-v2 line(s), {report['broken']} broken file(s)"
                 + ("" if report["unchained"] == 0 else
                    "   (finrail ledger migrate chains the pre-v2 lines)"))
    return "\n".join(lines)


def _short(path: str, root: pathlib.Path) -> str:
    try:
        return str(pathlib.Path(path).relative_to(root))
    except ValueError:
        return path


# ── migrate ─────────────────────────────────────────────────────────────────
def migrate() -> dict:
    """Rewrite a v1 state directory into chained files, once, keeping the originals."""
    root = state()
    backup = root / BACKUP
    if backup.exists():
        raise ContractViolation(
            f"this state directory was already migrated: the pre-chain originals are in "
            f"{backup}. A second migration would overwrite that backup with the chained "
            f"files, so it is refused. Move the backup away to migrate again.")
    logs = sorted(events.directory().glob("*.jsonl")) if events.directory().is_dir() else []
    runs_dir = tracker.runs()
    records = sorted(runs_dir.glob("*.json")) if runs_dir.is_dir() else []
    hists = tracker.histories()
    # The originals first, every one of them, before anything is rewritten.
    backup.mkdir(parents=True)
    for p in logs + [events.head_path(p) for p in logs] + records + hists \
            + [events.head_path(p) for p in hists]:
        if p.is_file():
            rel = p.relative_to(root) if _under(p, root) else pathlib.Path(p.name)
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
    k = events.key()
    out = {"backup": str(backup), "logs": 0, "lines": 0, "torn": 0, "histories": 0,
           "records": 0, "signed": bool(k)}
    for p in logs + hists:
        n, torn = _rechain(p, k)
        out["lines"] += n
        out["torn"] += torn
        out["logs" if p in logs else "histories"] += 1
    for p in records:
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("schema") == tracker.SCHEMA:
            continue
        rec = {"schema": tracker.SCHEMA, **rec}
        atomic.write_text(p, json.dumps(rec, indent=2, default=str))
        out["records"] += 1
    (backup / "MIGRATED.json").write_text(json.dumps(
        {"at": datetime.datetime.now().isoformat(timespec="seconds"), **out}, indent=2))
    return out


def _under(p: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _rechain(path: pathlib.Path, k) -> tuple:
    """Rewrite one log with every line chained from genesis. Each line keeps its own
    fields (seq, epoch, time, content); only the chain fields are set. A line that does
    not parse is left out and counted: the original is in the backup."""
    out, torn, prev_raw = [], 0, None
    with events._locked(path) as fh:
        fh.seek(0)
        for raw in fh.read().split(b"\n"):
            if not raw.strip():
                continue
            try:
                ev = json.loads(raw)
                if not isinstance(ev, dict):
                    raise ValueError
            except ValueError:
                torn += 1
                continue
            ev = events.chain(ev, prev_raw, k)
            line = events.canonical(ev)
            out.append(line)
            prev_raw = line
        atomic.write_text(path, "".join(ln.decode("utf-8") + "\n" for ln in out))
        if out:
            events._write_head(path, json.loads(out[-1]), k)
    return len(out), torn


# ── the command ─────────────────────────────────────────────────────────────
def cli(rest: list, as_json: bool) -> int:
    sub = rest[0] if rest else "verify"
    if sub == "verify":
        report = verify()
        print(json.dumps(report, indent=2) if as_json else render(report))
        return 0 if report["ok"] else 1
    if sub == "migrate":
        done = migrate()
        print(json.dumps(done, indent=2) if as_json else
              f"migrated: {done['logs']} log(s) and {done['histories']} history file(s), "
              f"{done['lines']} line(s) chained"
              + (" and signed" if done["signed"] else " (no ledger key: unsigned)")
              + f", {done['records']} run record(s) marked schema 2"
              + (f", {done['torn']} unparseable line(s) left out" if done["torn"] else "")
              + f"\n  originals: {done['backup']}")
        return 0
    raise ContractViolation(f"finrail ledger {sub}: one of verify, migrate")
