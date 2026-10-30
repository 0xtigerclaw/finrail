# Operating the console

How to run the web console as a service: what it reports about itself, what it refuses, what it logs, and what is still tied to one instance. The code is `console/ops.py`, wired into `console/app.py` as one ASGI middleware plus two routes.

Login, sessions and identity are out of scope here. Nothing below authenticates anyone; it keeps an unauthenticated public demo cheap, observable and hard to abuse.

## Endpoints

| Route | Answers | Checks |
|---|---|---|
| `GET /healthz` | `200` or `503`, JSON | Liveness: the process answers and the state dir takes a write. |
| `GET /readyz` | `200` or `503`, JSON | Readiness: registry loads, state dir writable, an engine is configured, its API key is set, `FINRAIL_RAG_CORPUS` names a Vertex corpus. No provider is called. |

Both bodies have the shape `{"status": ..., "checks": {name: {"ok": bool, ...}}}`. Secret values never appear: the key check reports only whether the variable is set, and the corpus check only whether the setting is well formed. `claude-cli` as the engine passes both engine checks with a note, since no key applies to it; it is a local development engine, not a deployment.

Point the platform's liveness probe at `/healthz` and its readiness or startup probe at `/readyz`. The Dockerfile's `HEALTHCHECK` uses `/healthz`. The container runs as the unprivileged user `finrail`, which owns `/data`.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `FINRAIL_RATE_PER_MINUTE` | `10` | Sustained provider-bound requests allowed per client per minute. |
| `FINRAIL_RATE_BURST` | `5` | How many of those a client may make at once before the bucket is empty. |
| `FINRAIL_DAILY_POST_CAP` | unset (no cap) | Provider-bound requests allowed per UTC day across all clients: the demo's budget. Replaces `FINRAIL_DEMO_POST_LIMIT`. |
| `FINRAIL_ROUTE_TIMEOUT_S` | `180` | Seconds a provider-bound route may run before the console answers `504`. |
| `FINRAIL_CONSOLE_SHOW_PROMPTS` | off | `1` shows full model prompts and answers on the Ledger pages. Off, they stay on disk. |
| `FINRAIL_STATE` | `~/.finrail` | State dir. The daily counter and the ledger live under it. |

Engine and corpus variables (`FINRAIL_ENGINE`, `NEBIUS_API_KEY` or `FINRAIL_ENGINE_API_KEY`, `FINRAIL_RAG_CORPUS`) are described in the README; `/readyz` reports whether they are set.

## Provider-bound routes

A request is provider-bound when it is a `POST` to `/ask`, `/reports/*`, `/proof/*`, `/screen/*`, `/dataroom/propose` or `/memo/*`. Each of those can end in a paid call to Nebius, Vertex, Tavily or TypeSafe. The rate limit, the daily cap and the route timeout apply to exactly this set. Reads and `/approvals/*` are never limited or timed out.

## Rate limit

Each client gets a token bucket: `FINRAIL_RATE_BURST` tokens to start, refilled at `FINRAIL_RATE_PER_MINUTE` per minute, one token per provider-bound request. An empty bucket answers `429` with a plain page and a `Retry-After` header saying when the next token arrives. The client key is the first hop of `X-Forwarded-For`, else the socket peer.

The daily cap is one counter for the whole instance. When it is reached, every provider-bound request answers `429` with `Retry-After` set to the seconds until the UTC day turns. The counter is written to `<state>/console/daily_cap.json` on every increment, so a restart continues the day's count. Pages show the remaining allowance in the demo notice.

Both refusals are decided before the form is read, so a refused request costs nothing.

## Headers and limits

Every response carries:

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
```

The pages have no inline script or style, so the policy needs no nonce and no `unsafe-inline`. Google Fonts is the one external origin, for the serif face. A new page that needs an inline style must use a data attribute and `static/app.js` instead; the test suite checks the rendered pages for inline attributes.

A `POST` body over 64 KB answers `413`. With a `Content-Length` header the refusal happens before any byte is read; a chunked body is cut off at the limit.

## Timeouts

A provider-bound route that is still running after `FINRAIL_ROUTE_TIMEOUT_S` answers `504` with a plain page naming the capability it was waiting on, and the worker is free again. The route's thread cannot be killed: the provider call runs to its own timeout, its result lands in the ledger as usual, and its late response is dropped. Nothing is retried.

## Request log

One JSON line per request on stdout, which Cloud Run collects as the request log:

```json
{"time":"2026-10-30T18:02:11.482+00:00","method":"POST","path":"/proof/run","status":303,"duration_ms":41873.2,"client":"203.0.113.9","run":"8f1c2a9d4e"}
```

| Field | Meaning |
|---|---|
| `time` | UTC, ISO 8601, milliseconds. |
| `method`, `path` | The request. Query strings are not logged: `/?q=` carries question text. |
| `status` | The status sent, `500` when the route raised. |
| `duration_ms` | Wall time from the first byte of the request to the last byte of the response. |
| `client` | First hop of `X-Forwarded-For`, else the socket peer. |
| `run` | The run ID a mutating route created, else `null`. |
| `error` | Present on `5xx` only: the exception class, or `RouteTimeout` on a `504`. |

Form bodies and question text are never logged. The CLI's rule that stdout belongs to the result (`finrail/core/log.py`) is the CLI's; the console is a server and its stdout is the platform's log stream.

The log line is written whether the request was served, refused or timed out, and health probes are logged like everything else. Filter `path` on `/healthz` and `/readyz` in the log viewer if the probes are noisy.

## What is still per instance

- **Token buckets** live in process memory. A second instance has its own, so a client's effective rate is the configured rate times the instance count. Restart empties them.
- **The daily cap** is one file per state dir. Two instances keep two counts, so the effective daily budget is the cap times the instance count.
- **Client identity** is the first hop of `X-Forwarded-For`. Behind Cloud Run the platform appends the true peer to whatever the client sent, so a client can choose its own first hop. For a stricter key, take the last hop that is not a trusted proxy.
- **The ledger and run records** are the instance's local disk, as the Dockerfile says.

Once there is a second instance, the buckets and the daily counter need a shared store (Memorystore or Firestore), and the client key needs the trusted-proxy rule above. Until then, keep the demo at one instance, which is also what keeps the ledger a judge reads the ledger the demo wrote.
