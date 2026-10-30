"""OPERATIONS: what makes the console runnable as a service rather than a demo.

One pure ASGI middleware (`ConsoleOps`) wraps the FastAPI app and does, in this order,
outermost first:

  1. request log: one JSON line per request to stdout, which is where Cloud Run reads;
  2. response headers: CSP, nosniff, frame denial, no referrer;
  3. body limit: a POST over 64 KB is refused with 413 before it is read;
  4. rate limit: a per-client token bucket plus a daily cap, on every route that can
     trigger a paid provider call;
  5. timeout: a provider-bound route that is still running after
     FINRAIL_ROUTE_TIMEOUT_S answers 504 and frees the worker.

The health endpoints live in app.py beside the other routes; this module holds their
checks so they stay testable without a server.

Why stdout, when finrail.core.log says stdout belongs to the result: that rule is the
CLI's, where stdout is parsed. The console is a server, its stdout is the log stream the
platform collects, and nothing else is written there.

Everything here is per process. Two Cloud Run instances keep two token-bucket tables and
two daily counters (each persisted to its own state dir); a shared store is the next step
once there is a second instance. docs/operations.md says so.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.exceptions import HTTPException as StarletteHTTPException

BODY_LIMIT = 64 * 1024
DEFAULT_RATE_PER_MINUTE = 10
DEFAULT_RATE_BURST = 5
DEFAULT_ROUTE_TIMEOUT_S = 180.0

#: POST routes that can end in a paid call (Nebius, Vertex, Tavily or TypeSafe).
#: `/approvals/*` is not here: dropping a proposal costs nothing.
PAID_PREFIXES = ("/ask", "/reports/", "/proof/", "/screen/", "/dataroom/propose", "/memo/")

HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}

#: Per-request notes shared between the middleware and the route: the capability that
#: is running (`step`) and the run the route created (`run`). A contextvar holding ONE
#: dict, so the thread a sync handler runs in mutates the same object the middleware
#: reads afterwards.
REQUEST: contextvars.ContextVar = contextvars.ContextVar("finrail_console_request", default=None)


def note(**fields) -> None:
    """Record something about the current request for its log line or 504 page."""
    ctx = REQUEST.get()
    if ctx is not None:
        ctx.update(fields)


# ── settings ─────────────────────────────────────────────────────────────────
def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if re.fullmatch(r"\d+", raw) else default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def rate_per_minute() -> int:
    return _env_int("FINRAIL_RATE_PER_MINUTE", DEFAULT_RATE_PER_MINUTE)


def rate_burst() -> int:
    return max(1, _env_int("FINRAIL_RATE_BURST", DEFAULT_RATE_BURST))


def daily_cap() -> int:
    """0 means no daily cap."""
    return _env_int("FINRAIL_DAILY_POST_CAP", 0)


def route_timeout_s() -> float:
    return _env_float("FINRAIL_ROUTE_TIMEOUT_S", DEFAULT_ROUTE_TIMEOUT_S)


def show_prompts() -> bool:
    return truthy("FINRAIL_CONSOLE_SHOW_PROMPTS")


# ── client identity ──────────────────────────────────────────────────────────
def client_ip(scope: dict) -> str:
    """The first hop of X-Forwarded-For, else the socket peer.

    Behind Cloud Run the platform appends the real peer to whatever the client sent, so
    the first hop is the client's own claim. That is what the task asked for; a stricter
    deployment trims the list to the last untrusted hop (docs/operations.md).
    """
    for name, value in scope.get("headers") or ():
        if name == b"x-forwarded-for":
            first = value.decode("latin-1").split(",")[0].strip()
            if first:
                return first
    client = scope.get("client")
    return client[0] if client else "unknown"


def provider_bound(scope: dict) -> bool:
    return scope.get("method") == "POST" and str(scope.get("path") or "").startswith(PAID_PREFIXES)


# ── rate limiting ────────────────────────────────────────────────────────────
class TokenBuckets:
    """One bucket per key. `take` returns 0 when a token was spent, else the seconds
    until one is available. In memory, per process."""

    def __init__(self):
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str, per_minute: int, burst: int, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        refill = per_minute / 60.0
        with self._lock:
            tokens, last = self._buckets.get(key, (float(burst), now))
            tokens = min(float(burst), tokens + (now - last) * refill)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return 0.0
            self._buckets[key] = (tokens, now)
            if len(self._buckets) > 10000:
                self._prune(burst, now, refill)
            return (1.0 - tokens) / refill if refill > 0 else 3600.0

    def _prune(self, burst: int, now: float, refill: float) -> None:
        full = [k for k, (t, last) in self._buckets.items()
                if t + (now - last) * refill >= burst]
        for key in full:
            del self._buckets[key]

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


class DailyCap:
    """The demo budget: N provider-bound requests per UTC day across all clients.

    The counter is written to `<state>/console/daily_cap.json` on every increment, so a
    restart continues the day's count instead of starting from zero. One file per
    instance; two instances keep two counts.
    """

    def __init__(self, path_of):
        self._path_of = path_of
        self._lock = threading.Lock()
        self._day = ""
        self._count = 0
        self._loaded_from: Path | None = None

    @staticmethod
    def _today(now: datetime | None = None) -> str:
        return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")

    @staticmethod
    def _until_midnight(now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1.0, (tomorrow - now).total_seconds())

    def _load(self, path: Path, today: str) -> None:
        if self._loaded_from == path and self._day == today:
            return
        self._day, self._count, self._loaded_from = today, 0, path
        try:
            saved = json.loads(path.read_text())
            if isinstance(saved, dict) and saved.get("day") == today:
                self._count = int(saved.get("count") or 0)
        except (OSError, ValueError, TypeError):
            pass

    def _save(self, path: Path) -> None:
        try:
            from finrail.core import atomic
            atomic.write_json(path, {"day": self._day, "count": self._count})
        except Exception:  # noqa: BLE001 - a counter that cannot be saved must not fail the request
            pass

    def status(self, cap: int) -> tuple[int, int]:
        """(used, cap) for today."""
        with self._lock:
            self._load(self._path_of(), self._today())
            return self._count, cap

    def take(self, cap: int, now: datetime | None = None) -> float:
        """0 when the request fits today's budget, else seconds until the UTC day turns."""
        if cap <= 0:
            return 0.0
        with self._lock:
            path = self._path_of()
            self._load(path, self._today(now))
            if self._count >= cap:
                return self._until_midnight(now)
            self._count += 1
            self._save(path)
            return 0.0

    def reset(self) -> None:
        with self._lock:
            self._day, self._count, self._loaded_from = "", 0, None


def _daily_path() -> Path:
    from finrail.core.settings import state
    return state() / "console" / "daily_cap.json"


BUCKETS = TokenBuckets()
DAILY = DailyCap(_daily_path)


# ── health checks ────────────────────────────────────────────────────────────
def check_state_dir() -> dict:
    from finrail.core.settings import state
    root = state()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".healthz"
        probe.write_text(str(time.time()))
        probe.unlink()
        return {"ok": True, "path": str(root)}
    except OSError as exc:
        return {"ok": False, "path": str(root), "error": type(exc).__name__}


def check_registry() -> dict:
    try:
        from finrail.core import registry
        registry.load()
        return {"ok": True, "capabilities": len(registry.commands())}
    except Exception as exc:  # noqa: BLE001 - readiness reports, never raises
        return {"ok": False, "error": type(exc).__name__}


def check_engine() -> tuple[dict, dict]:
    """(engine, engine_key): is an engine configured, and is its key present. The key's
    value never leaves this function; only whether it is set."""
    from finrail.core import llm
    eng = llm.engine()
    if eng == "claude-cli":
        return ({"ok": True, "engine": eng, "note": "local development engine"},
                {"ok": True, "note": "no API key applies to claude-cli"})
    base, key, model = llm._http_config()
    engine = {"ok": bool(base and model), "engine": eng, "model": model or ""}
    if not engine["ok"]:
        engine["error"] = "FINRAIL_ENGINE_BASE_URL or FINRAIL_MODEL is not set"
    key_check = {"ok": bool(key)}
    if not key:
        key_check["error"] = "the engine's API key variable is not set"
    return engine, key_check


def check_corpus() -> dict:
    corpus = os.environ.get("FINRAIL_RAG_CORPUS", "")
    ok = bool(re.fullmatch(r"projects/[\w-]+/locations/us-central1/ragCorpora/\d+", corpus))
    return {"ok": ok} if ok else {"ok": False, "error": "FINRAIL_RAG_CORPUS is not set to a Vertex RAG corpus"}


def liveness() -> tuple[dict, int]:
    checks = {"state_dir": check_state_dir()}
    ok = all(c["ok"] for c in checks.values())
    return {"status": "ok" if ok else "unavailable", "checks": checks}, (200 if ok else 503)


def readiness() -> tuple[dict, int]:
    engine, key = check_engine()
    checks = {"registry": check_registry(), "state_dir": check_state_dir(),
              "engine": engine, "engine_key": key, "corpus": check_corpus()}
    ok = all(c["ok"] for c in checks.values())
    return {"status": "ready" if ok else "not_ready", "checks": checks}, (200 if ok else 503)


# ── plain pages ──────────────────────────────────────────────────────────────
def plain_page(title: str, message: str) -> bytes:
    """A refusal page with no inline script or style, so it passes the CSP it ships with."""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)} · Finrail</title>"
        "<link rel='stylesheet' href='/assets/app.css'></head><body>"
        "<main class='app-main' id='main-content'><section class='outcome outcome-error' role='alert'>"
        f"<div class='outcome-header'><div><h2>{html.escape(title)}</h2><p>{html.escape(message)}</p></div></div>"
        "<div class='outcome-body'><a class='btn btn-secondary' href='/'>Open workspace</a></div>"
        "</section></main></body></html>").encode("utf-8")


async def _send_plain(send, status: int, title: str, message: str, extra: dict | None = None) -> None:
    body = plain_page(title, message)
    headers = [(b"content-type", b"text/html; charset=utf-8"),
               (b"content-length", str(len(body)).encode())]
    for name, value in (extra or {}).items():
        headers.append((name.encode("latin-1"), str(value).encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


# ── the middleware ───────────────────────────────────────────────────────────
class ConsoleOps:
    def __init__(self, app, stream=None):
        self.app = app
        self._stream = stream          # None: resolve sys.stdout at write time (tests capture it)
        self._orphans: set[asyncio.Task] = set()

    def _write(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":"), default=str)
        out = self._stream or sys.stdout
        try:
            out.write(line + "\n")
            out.flush()
        except (OSError, ValueError):
            pass

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        ctx = {"step": None, "run": None, "abandoned": False}
        token = REQUEST.set(ctx)
        started = time.perf_counter()
        status = {"code": None}

        async def send_logged(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = list(message.get("headers") or [])
                present = {name.lower() for name, _v in headers}
                for name, value in HEADERS.items():
                    if name.encode() not in present:
                        headers.append((name.encode(), value.encode()))
                message = {**message, "headers": headers}
            await send(message)

        error = None
        try:
            await self._handle(scope, receive, send_logged, ctx)
        except BaseException as exc:  # noqa: BLE001 - logged, then re-raised unchanged
            error = type(exc).__name__
            if status["code"] is None:
                status["code"] = 500
            raise
        finally:
            REQUEST.reset(token)
            record = {
                "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "method": scope.get("method"),
                "path": scope.get("path"),
                "status": status["code"],
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "client": client_ip(scope),
                "run": ctx.get("run"),
            }
            if error or ctx.get("error"):
                record["error"] = error or ctx["error"]
            self._write(record)

    async def _handle(self, scope, receive, send, ctx: dict):
        method = scope.get("method")
        if method == "POST":
            declared = 0
            for name, value in scope.get("headers") or ():
                if name == b"content-length":
                    declared = int(value) if value.isdigit() else 0
            if declared > BODY_LIMIT:
                await _send_plain(send, 413, "Request too large",
                                  f"Forms on this console accept up to {BODY_LIMIT // 1024} KB. Nothing was read.")
                return
            receive = _limited(receive, BODY_LIMIT)

        if not provider_bound(scope):
            await self.app(scope, receive, send)
            return

        key = client_ip(scope)
        wait = BUCKETS.take(key, rate_per_minute(), rate_burst())
        if wait > 0:
            await _send_plain(send, 429, "Too many requests",
                              f"This client may make {rate_per_minute()} model-backed requests a minute, "
                              f"in bursts of {rate_burst()}. Try again in {int(wait) + 1} seconds.",
                              {"retry-after": int(wait) + 1})
            return
        wait = DAILY.take(daily_cap())
        if wait > 0:
            await _send_plain(send, 429, "The demo's daily budget is used up",
                              f"This instance allows {daily_cap()} model-backed requests a day and has reached it. "
                              "The read pages and archived results remain available.",
                              {"retry-after": int(wait) + 1})
            return

        await self._with_timeout(scope, receive, send, ctx, route_timeout_s())

    async def _with_timeout(self, scope, receive, send, ctx: dict, timeout: float):
        state = {"started": False}

        async def guarded_send(message):
            if ctx["abandoned"]:
                return
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        async def guarded_receive():
            if ctx["abandoned"]:
                return {"type": "http.disconnect"}
            return await receive()

        task = asyncio.ensure_future(self.app(scope, guarded_receive, guarded_send))
        done, _pending = await asyncio.wait({task}, timeout=timeout if timeout > 0 else None)
        if done or state["started"]:
            await task                  # re-raises whatever the route raised
            return
        # The route is still inside a provider call. The thread it runs in cannot be
        # killed, so it is left to finish on its own; its late sends are dropped.
        ctx["abandoned"] = True
        self._orphans.add(task)
        task.add_done_callback(self._forget)
        step = ctx.get("step") or "the request"
        ctx["error"] = "RouteTimeout"
        await _send_plain(send, 504, "The request timed out",
                          f"Timed out after {timeout:g} seconds while waiting on {step}. "
                          "The provider call may still finish and appear in the ledger; nothing was retried.")

    def _forget(self, task: asyncio.Task) -> None:
        self._orphans.discard(task)
        if not task.cancelled():
            task.exception()            # retrieved, so the loop does not warn about it


def _limited(receive, limit: int):
    """A receive that refuses once more than `limit` bytes of body have arrived."""
    seen = {"bytes": 0}

    async def receive_limited():
        message = await receive()
        if message["type"] == "http.request":
            seen["bytes"] += len(message.get("body") or b"")
            if seen["bytes"] > limit:
                raise StarletteHTTPException(
                    413, f"Forms on this console accept up to {limit // 1024} KB.")
        return message

    return receive_limited


def sha256(text) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
