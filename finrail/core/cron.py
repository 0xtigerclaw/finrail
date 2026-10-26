"""CRON — one heartbeat, many declared jobs.

A single OS service runs `finrail cron tick` every minute. Plugins declare jobs; the
tick fires the due ones THROUGH the normal capability path, so a scheduled action obeys
exactly the same rules (approval, verification, ledger) as one you typed. There is no
second, privileged execution path.

Schedules are standard 5-field cron: minute hour day-of-month month day-of-week.
"""
import datetime
import json

from . import atomic
from .errors import ContractViolation
from .settings import ensure, store


class CronJob:
    """A scheduled capability call, declared by a plugin."""

    def __init__(self, name: str, schedule: str, capability: str,
                 payload: dict = None, description: str = "", enabled: bool = True):
        self.name = name
        self.schedule = schedule
        self.capability = capability
        self.payload = dict(payload or {})
        self.description = description
        self.enabled = enabled
        self.plugin = ""
        parse(schedule)                       # fail at declaration, not at 03:00

    def to_json(self) -> dict:
        return {"name": self.name, "schedule": self.schedule,
                "capability": self.capability, "payload": self.payload,
                "plugin": self.plugin, "enabled": self.enabled,
                "description": self.description}

    def __repr__(self):
        return f"<CronJob {self.name} '{self.schedule}' -> {self.capability}>"


# ── schedule parsing ────────────────────────────────────────────────────────
FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("dom", 1, 31), ("month", 1, 12),
          ("dow", 0, 6))


def _field(spec: str, low: int, high: int, label: str) -> set:
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ContractViolation(f"cron {label}: empty field")
        step = 1
        if "/" in part:
            part, _, s = part.partition("/")
            if not s.isdigit() or int(s) < 1:
                raise ContractViolation(f"cron {label}: bad step '{s}'")
            step = int(s)
        if part in ("*", ""):
            lo, hi = low, high
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ContractViolation(f"cron {label}: bad range '{part}'")
            lo, hi = int(a), int(b)
        elif part.isdigit():
            lo = hi = int(part)
        else:
            raise ContractViolation(f"cron {label}: cannot parse '{part}'")
        if lo < low or hi > high or lo > hi:
            raise ContractViolation(
                f"cron {label}: {lo}-{hi} outside {low}-{high}")
        out.update(range(lo, hi + 1, step))
    return out


def parse(schedule: str) -> list:
    parts = schedule.split()
    if len(parts) != 5:
        raise ContractViolation(
            f"cron schedule needs 5 fields (min hour dom mon dow), got {len(parts)}: "
            f"'{schedule}'")
    return [_field(p, lo, hi, label)
            for p, (label, lo, hi) in zip(parts, FIELDS)]


def due(schedule: str, now: datetime.datetime = None) -> bool:
    now = now or datetime.datetime.now()
    minute, hour, dom, month, dow = parse(schedule)
    # cron's day rule: when both dom and dow are restricted, EITHER matching fires.
    dom_all = len(dom) == 31
    dow_all = len(dow) == 7
    day_ok = ((now.day in dom) or (now.isoweekday() % 7 in dow)
              if not dom_all and not dow_all
              else (now.day in dom) and (now.isoweekday() % 7 in dow))
    return (now.minute in minute and now.hour in hour and day_ok
            and now.month in month)


# ── job state (last run) ────────────────────────────────────────────────────
def _path():
    return store() / "cron.json"


def _state() -> dict:
    p = _path()
    return json.loads(p.read_text()) if p.is_file() else {}


def _save(state: dict) -> None:
    ensure()
    atomic.write_json(_path(), state, indent=2, sort_keys=True)


def jobs() -> list:
    """Every declared job, with its persisted state and any local override."""
    from .registry import load
    from .api import RUNTIME
    load()
    st = _state()
    out = []
    for job in RUNTIME.crons:
        d = job.to_json()
        s = st.get(job.name, {})
        d["enabled"] = s.get("enabled", job.enabled)
        d["last_run"] = s.get("last_run")
        d["last_status"] = s.get("last_status")
        d["last_detail"] = s.get("last_detail")
        d["last_run_id"] = s.get("last_run_id")
        # Prefer the LIVE status from the ledger over the snapshot taken at dispatch. A
        # background job (mail.sync, brain.rebuild) hands off to a detached worker and
        # returns immediately, so the snapshot is "pending" even once the work is long
        # done — which reads as permanently stuck. The ledger knows what really happened.
        if d["last_run_id"]:
            try:
                from . import tracker
                d["last_status"] = tracker.load(d["last_run_id"])["status"]
            except Exception:                                   # noqa: BLE001
                pass                    # record dropped or unreadable; keep the snapshot
        out.append(d)
    return sorted(out, key=lambda d: d["name"])


def job(name: str) -> dict:
    for j in jobs():
        if j["name"] == name:
            return j
    raise ContractViolation(f"no cron job '{name}'")


def enable(name: str, on: bool = True) -> dict:
    job(name)
    st = _state()
    st.setdefault(name, {})["enabled"] = bool(on)
    _save(st)
    return {"job": name, "enabled": bool(on)}


def record(name: str, status: str, detail: str = "", run_id: str = None) -> None:
    """Locked, because two agents' ticks can land in the same second and the loser's
    record would otherwise vanish with no error anywhere."""
    ensure()

    def change(st):
        st.setdefault(name, {}).update(
            {"last_run": datetime.datetime.now().isoformat(timespec="seconds"),
             "last_status": status, "last_detail": detail[:400], "last_run_id": run_id})
        return st

    atomic.update_json(_path(), change, indent=2, sort_keys=True)


def run_job(name: str) -> dict:
    """Fire one job through the normal capability path."""
    from . import dispatch_core
    j = job(name)
    # ── A JOB WHOSE LAST RUN STILL WAITS FOR A PERSON IS NOT FIRED AGAIN ────────
    #
    # A scheduled capability that asks for approval does not run: it records a pending
    # run and waits. Fired again a minute later it records another, and another (14 Oct:
    # the fund drain, missing `autonomous = True`, had six waiting within six minutes, and
    # the first of them blocked a deploy). The missing flag was that job's bug; the pile
    # is this loop's, for every job that will ever have it. So while the last run waits,
    # the job is held, and says why and which run, in `cron list` where it is looked at.
    held = _waiting(j.get("last_run_id"))
    if held:
        detail = (f"held: run {held} still waits for approval, so this job is not fired "
                  f"again. A scheduled job that needs a person every time is missing "
                  f"`autonomous = True`, or should not be scheduled. "
                  f"`finrail approve {held}` or `finrail drop {held}`.")
        record(name, "held", detail, run_id=held)
        return {"job": name, "status": "held", "run": held, "detail": detail}
    try:
        rec = dispatch_core.dispatch(j["capability"], dict(j["payload"]),
                                     source=f"cron:{name}", actor=f"cron:{name}")
        # The run id matters more than the status captured here: a BACKGROUND capability
        # returns while its detached worker is still going, so this snapshot would say
        # "pending" forever even after the work finished. jobs() resolves the live status
        # from the ledger instead — see there.
        record(name, rec.get("status", "?"), str(rec.get("result"))[:200],
               run_id=rec.get("id"))
        return {"job": name, "run": rec.get("id"), "status": rec.get("status")}
    except Exception as e:
        record(name, "failed", str(e))
        return {"job": name, "status": "failed", "error": str(e)}


def _waiting(run_id) -> str:
    """`run_id` when that run still waits for a person's decision, else ''."""
    if not run_id:
        return ""
    try:
        from . import tracker
        # `awaiting`, not the bare status: an approved run handed to a worker also reads
        # `pending` until the worker starts, and that one is not waiting for anybody.
        return run_id if tracker.awaiting(tracker.load(run_id)) else ""
    except Exception:                                              # noqa: BLE001
        return ""                 # dropped or unreadable: nothing is waiting on it


#: How far back a tick will look for a window it missed. One hour: long enough that a
#: restart, a slow boot or a minute of clock skew does not cost a nightly job, short
#: enough that a machine off for a week does not come back and fire everything at once.
CATCH_UP_MINUTES = 60

LAST_TICK = "last_tick"


def _missed(schedule: str, now: datetime.datetime, last: str) -> bool:
    """Was this schedule due at any minute between the last tick and this one?

    `due` is a point-in-time predicate, so a job wanting 04:17 needs a tick DURING 04:17.
    That made correctness depend on the heartbeat being punctual: one missed minute cost
    a whole night of learning, silently, and nothing anywhere reported it. The catch-up
    is what decouples the two — the schedule says when the work should happen, and the
    heartbeat only has to be roughly regular.
    """
    if not last:
        return False
    try:
        prev = datetime.datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return False
    minute = now.replace(second=0, microsecond=0)
    prev = max(prev.replace(second=0, microsecond=0),
               minute - datetime.timedelta(minutes=CATCH_UP_MINUTES))
    while prev < minute:
        prev += datetime.timedelta(minutes=1)
        if prev < minute and due(schedule, prev):
            return True
    return False


def tick(now: datetime.datetime = None) -> dict:
    """Fire every due job, and anything whose window was missed since the last tick.

    One job's failure never stops the others.
    """
    now = now or datetime.datetime.now()
    last = _state().get(LAST_TICK, {}).get("at") if isinstance(
        _state().get(LAST_TICK), dict) else _state().get(LAST_TICK)
    fired, skipped, failed, caught, held = [], [], [], [], []
    for j in jobs():
        if not j["enabled"]:
            skipped.append(j["name"])
            continue
        try:
            is_due = due(j["schedule"], now)
            late = False if is_due else _missed(j["schedule"], now, last)
            if not (is_due or late):
                continue
            if late:
                caught.append(j["name"])
        except ContractViolation as e:
            failed.append({"job": j["name"], "error": str(e)})
            continue
        out = run_job(j["name"])
        {"failed": failed, "held": held}.get(out.get("status"), fired).append(out)
    closed = reap()
    st = _state()
    st[LAST_TICK] = now.isoformat(timespec="seconds")
    _save(st)
    return {"at": now.isoformat(timespec="seconds"), "fired": fired,
            "failed": failed, "held": held, "disabled": skipped, "caught_up": caught,
            **closed}


def reap() -> dict:
    """The tick's housekeeping alone: close runs and sessions nothing is running any more.

    Separate from `tick` for an install that must not fire jobs: a second install on a
    box (the dev agents beside the live ones) shares some of the live install's data, so
    a heartbeat of its own would run the same jobs twice against it. It still needs its
    dead runs closed, and this fires nothing. `finrail cron reap`.
    """
    # Close what nothing is running any more. The heartbeat is the only thing that
    # reliably comes back after a kill, so it is where a record that cannot write its
    # own ending gets one.
    try:
        from . import tracker
        reaped = [r["id"] for r in tracker.reap()]
    except Exception:                                            # noqa: BLE001
        reaped = []
    # The SESSION side of the same kill. Runs and sessions each record that a turn is in
    # flight, and only the run was ever reconciled — so `finrail get sessions` kept
    # showing `▶ a turn is in flight` for runs this same tick had already closed. Separate
    # try: a failure reaping sessions must not cost us the run reaping, and vice versa.
    try:
        from . import sessions
        unstuck = [r["session"] for r in sessions.reap()]
    except Exception:                                            # noqa: BLE001
        unstuck = []
    return {"reaped": reaped, "unstuck": unstuck}
