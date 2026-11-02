"""Console operations: health, request log, rate limit, body limit, headers, prompt
gating and route timeouts. Fully offline: no provider is ever called."""
from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FINRAIL_ENGINE", "claude-cli")
    monkeypatch.setenv("FINRAIL_DEMO_ENTITY", "04123456")
    for key in ("NEBIUS_API_KEY", "FINRAIL_ENGINE_API_KEY", "FINRAIL_RAG_CORPUS",
                "FINRAIL_RATE_PER_MINUTE", "FINRAIL_RATE_BURST", "FINRAIL_DAILY_POST_CAP",
                "FINRAIL_ROUTE_TIMEOUT_S", "FINRAIL_CONSOLE_SHOW_PROMPTS"):
        monkeypatch.delenv(key, raising=False)
    from finrail.core import llm

    def no_provider(*_args, **_kwargs):
        raise AssertionError("ops tests must not call a model provider")

    monkeypatch.setattr(llm, "call", no_provider)


@pytest.fixture()
def console():
    from console import app as console_app
    return console_app


@pytest.fixture()
def client(console):
    with TestClient(console.app) as instance:
        yield instance


def _stub_dispatch(console, monkeypatch, result=None):
    """Every console action answers at once, offline, with a fixed record."""
    calls = []

    def fake(name, payload=None, task=""):
        calls.append(name)
        return result or {"status": "done", "capability": name, "id": "abc123", "result": {"ok": True}}

    monkeypatch.setattr(console, "_dispatch", fake)
    return calls


def _log_lines(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


# ── health ───────────────────────────────────────────────────────────────────
def test_healthz_is_json_and_reports_the_state_dir(client, tmp_path):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    body = res.json()
    assert body["status"] == "ok"
    assert body["checks"]["state_dir"]["ok"] is True
    assert "<html" not in res.text


def test_healthz_503_when_the_state_dir_cannot_be_written(client, tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where a directory should be")
    monkeypatch.setenv("FINRAIL_STATE", str(blocked))
    res = client.get("/healthz")
    assert res.status_code == 503
    assert res.json()["status"] == "unavailable"
    assert res.json()["checks"]["state_dir"]["ok"] is False


def test_readyz_503_when_the_engine_key_is_missing(client, monkeypatch):
    monkeypatch.setenv("FINRAIL_ENGINE", "nebius")
    monkeypatch.setenv("FINRAIL_RAG_CORPUS", "projects/p/locations/us-central1/ragCorpora/1")
    res = client.get("/readyz")
    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["engine_key"]["ok"] is False
    assert body["checks"]["engine"]["ok"] is True
    assert body["checks"]["registry"]["ok"] is True
    assert body["checks"]["corpus"]["ok"] is True


def test_readyz_200_with_key_and_corpus_and_never_shows_the_key(client, monkeypatch):
    monkeypatch.setenv("FINRAIL_ENGINE", "nebius")
    monkeypatch.setenv("NEBIUS_API_KEY", "sk-this-must-not-appear")
    monkeypatch.setenv("FINRAIL_RAG_CORPUS", "projects/p/locations/us-central1/ragCorpora/1")
    res = client.get("/readyz")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "ready"
    assert "sk-this-must-not-appear" not in res.text
    assert "ragCorpora/1" not in res.text


def test_readyz_503_without_the_corpus_setting(client, monkeypatch, capsys):
    monkeypatch.setenv("FINRAIL_ENGINE", "nebius")
    monkeypatch.setenv("NEBIUS_API_KEY", "k")
    res = client.get("/readyz")
    assert res.status_code == 503
    assert res.json()["checks"]["corpus"]["ok"] is False
    # A deliberate 503 is an answer, not a failure: no error field in its log line.
    line = [line for line in _log_lines(capsys) if line["path"] == "/readyz"][-1]
    assert line["status"] == 503 and "error" not in line


# ── request log ──────────────────────────────────────────────────────────────
def test_every_request_writes_one_json_line_to_stdout(client, capsys):
    res = client.get("/capabilities", headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
    assert res.status_code == 200
    lines = [line for line in _log_lines(capsys) if line["path"] == "/capabilities"]
    assert len(lines) == 1
    line = lines[0]
    assert line["method"] == "GET" and line["status"] == 200
    assert line["client"] == "10.0.0.1"   # the last hop: the one the platform appended
    assert isinstance(line["duration_ms"], (int, float)) and line["duration_ms"] >= 0
    assert line["run"] is None and "error" not in line
    assert set(line) >= {"time", "method", "path", "status", "duration_ms", "client", "run"}


def test_log_falls_back_to_the_socket_peer(client, capsys):
    client.get("/healthz")
    line = [line for line in _log_lines(capsys) if line["path"] == "/healthz"][-1]
    assert line["client"] == "testclient"


def test_log_names_the_run_a_route_created_and_never_the_form(console, client, capsys, monkeypatch):
    def fake(name, payload, task="", source=""):
        return {"status": "done", "capability": name, "id": "abc123", "result": {"ok": True}}

    monkeypatch.setattr(console.dispatch_core, "dispatch", fake)
    res = client.post("/screen/run", data={"company_id": "04123456 SECRET-FORM-VALUE"})
    assert res.status_code == 200
    out = capsys.readouterr().out
    assert "SECRET-FORM-VALUE" not in out
    line = [json.loads(l) for l in out.splitlines() if l.startswith("{")][-1]
    assert line["path"] == "/screen/run" and line["method"] == "POST"
    assert line["run"] == "abc123"


def test_log_carries_the_exception_class_on_5xx(console, monkeypatch, capsys):
    def boom():
        raise RuntimeError("deliberate")

    monkeypatch.setattr(console, "_pending", boom)
    with TestClient(console.app, raise_server_exceptions=False) as client:
        res = client.get("/approvals")
    assert res.status_code == 500
    line = [line for line in _log_lines(capsys) if line["path"] == "/approvals"][-1]
    assert line["status"] == 500 and line["error"] == "RuntimeError"


# ── rate limit ───────────────────────────────────────────────────────────────
def test_sixth_burst_request_is_refused_with_429_and_retry_after(console, client, monkeypatch):
    monkeypatch.setenv("FINRAIL_RATE_PER_MINUTE", "10")
    monkeypatch.setenv("FINRAIL_RATE_BURST", "5")
    _stub_dispatch(console, monkeypatch)
    for _ in range(5):
        assert client.post("/screen/run", data={"company_id": "04123456"}).status_code == 200
    refused = client.post("/screen/run", data={"company_id": "04123456"})
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1
    assert "Too many requests" in refused.text
    assert "<script" not in refused.text
    # Reads are never limited, and another client has its own bucket.
    assert client.get("/screen").status_code == 200
    other = client.post("/screen/run", data={"company_id": "04123456"},
                        headers={"x-forwarded-for": "198.51.100.7"})
    assert other.status_code == 200


def test_bucket_refills_over_time():
    from console import ops
    buckets = ops.TokenBuckets()
    now = 1000.0
    for _ in range(5):
        assert buckets.take("c", 10, 5, now=now) == 0
    wait = buckets.take("c", 10, 5, now=now)
    assert 0 < wait <= 6
    assert buckets.take("c", 10, 5, now=now + 6.1) == 0


def test_daily_cap_persists_across_a_restart(console, client, monkeypatch, tmp_path):
    from console import ops
    monkeypatch.setenv("FINRAIL_DAILY_POST_CAP", "2")
    _stub_dispatch(console, monkeypatch)
    assert client.post("/screen/run", data={"company_id": "04123456"}).status_code == 200
    assert client.post("/screen/run", data={"company_id": "04123456"}).status_code == 200
    third = client.post("/screen/run", data={"company_id": "04123456"})
    assert third.status_code == 429 and "daily budget" in third.text
    assert "retry-after" in third.headers
    saved = json.loads((tmp_path / "state" / "console" / "daily_cap.json").read_text())
    assert saved["count"] == 2
    ops.DAILY.reset()                       # a restart: memory gone, the file remains
    assert client.post("/screen/run", data={"company_id": "04123456"}).status_code == 429
    assert "2 of 2" not in client.get("/").text and "0 of 2 demo actions left today" in client.get("/").text


# ── body limit and headers ───────────────────────────────────────────────────
def test_oversized_form_body_is_refused_with_413(client):
    res = client.post("/ask", data={"question": "x" * (64 * 1024 + 1)})
    assert res.status_code == 413
    assert "64 KB" in res.text


def test_oversized_chunked_body_is_refused_with_413(console):
    def chunks():
        yield b"question=" + b"y" * 40000
        yield b"z" * 40000

    with TestClient(console.app, raise_server_exceptions=False) as client:
        res = client.post("/ask", content=chunks(),
                          headers={"content-type": "application/x-www-form-urlencoded"})
    assert res.status_code == 413


def test_security_headers_on_pages_and_refusals(console, client, monkeypatch):
    for res in (client.get("/"), client.get("/healthz"), client.get("/assets/app.css")):
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["x-frame-options"] == "DENY"
        assert res.headers["referrer-policy"] == "no-referrer"
        csp = res.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp
    monkeypatch.setenv("FINRAIL_RATE_BURST", "1")
    _stub_dispatch(console, monkeypatch)
    client.post("/screen/run", data={"company_id": "04123456"})
    refused = client.post("/screen/run", data={"company_id": "04123456"})
    assert refused.status_code == 429 and refused.headers["x-frame-options"] == "DENY"


def test_pages_carry_no_inline_script_or_style(client):
    for path in ("/", "/runs", "/proof", "/reports", "/screen", "/cli"):
        text = client.get(path).text
        assert "<script>" not in text and "<style" not in text and " style='" not in text and ' style="' not in text, path


# ── ledger exposure ──────────────────────────────────────────────────────────
def _write_model_call(tmp_path):
    ledger = tmp_path / "state" / "events"
    ledger.mkdir(parents=True, exist_ok=True)
    event = {"kind": "model_call", "seq": 7, "at": "2026-10-30T10:00:00+00:00", "thread": "t1", "run": "run77",
             "data": {"capability": "finance.reports.analyze", "engine": "nebius", "model": "nvidia/Nemotron-3.5-Super-VL",
                      "prompt": "THE-FULL-PROMPT-TEXT", "answer": "THE-FULL-ANSWER-TEXT",
                      "usage": {"input_tokens": 1234, "output_tokens": 56}}}
    (ledger / "_ledger.jsonl").write_text(json.dumps(event) + "\n")


def test_prompts_are_hidden_by_default(client, tmp_path):
    from console import ops
    _write_model_call(tmp_path)
    page = client.get("/runs", params={"kind": "model_call"})
    assert page.status_code == 200
    assert "THE-FULL-PROMPT-TEXT" not in page.text and "THE-FULL-ANSWER-TEXT" not in page.text
    assert "Model call" in page.text and "finance.reports.analyze" in page.text
    assert "Nemotron-3.5-Super-VL" in page.text and "1234 tokens in" in page.text and "56 tokens out" in page.text
    assert ops.sha256("THE-FULL-PROMPT-TEXT") in page.text
    assert "ledger on disk" in page.text and "_ledger.jsonl" in page.text


def test_prompts_shown_when_the_setting_is_on(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_CONSOLE_SHOW_PROMPTS", "1")
    _write_model_call(tmp_path)
    page = client.get("/runs", params={"kind": "model_call"})
    assert "THE-FULL-PROMPT-TEXT" in page.text and "THE-FULL-ANSWER-TEXT" in page.text
    assert "ledger on disk" not in page.text


# ── timeouts ─────────────────────────────────────────────────────────────────
def test_slow_provider_route_answers_504_naming_the_step(console, client, monkeypatch, capsys):
    monkeypatch.setenv("FINRAIL_ROUTE_TIMEOUT_S", "0.2")

    def slow(name, payload, task="", source=""):
        time.sleep(0.6)
        return {"status": "done", "capability": name, "id": "late1"}

    monkeypatch.setattr(console.dispatch_core, "dispatch", slow)
    started = time.monotonic()
    res = client.post("/screen/run", data={"company_id": "04123456"})
    assert res.status_code == 504
    assert time.monotonic() - started < 0.55
    assert "finance.screen.run" in res.text and "Timed out after 0.2 seconds" in res.text
    assert res.headers["x-frame-options"] == "DENY"
    time.sleep(0.5)                          # let the abandoned thread finish quietly
    line = [line for line in _log_lines(capsys) if line["path"] == "/screen/run"][-1]
    assert line["status"] == 504 and line["error"] == "RouteTimeout"
