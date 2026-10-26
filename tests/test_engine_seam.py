"""Engine seam tests (PRD E1-E8): recorded responses only, no network.

A local HTTP stub stands in for the sponsor endpoint. The point of the seam
is that a judged capability cannot tell which engine answered, that a dead
or hostile provider surfaces as a canonical error code rather than a
plausible answer, and that there is no silent fallback to another engine.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from finrail.core import llm


class _Stub(BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, *args):  # keep the suite quiet
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.mode == "unauthorized":
            body = b'{"error": {"message": "invalid api key"}}'
            self.send_response(401)
        elif self.mode == "bad_schema":
            body = json.dumps({"choices": [{"message": {
                "content": json.dumps({"person": "Elena Marsh"})}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
            self.send_response(200)
        else:
            body = json.dumps({"choices": [{"message": {
                "content": json.dumps({"person": "Elena Marsh",
                                       "date": "12 March 2026"})}}],
                "usage": {"prompt_tokens": 56, "completion_tokens": 93}}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def stub(tmp_path, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FINRAIL_ENGINE", "nebius")
    monkeypatch.setenv("FINRAIL_ENGINE_BASE_URL",
                       f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("FINRAIL_ENGINE_API_KEY", "test-key-not-real")
    yield server
    server.shutdown()


SCHEMA = json.dumps({
    "type": "object",
    "properties": {"person": {"type": "string"}, "date": {"type": "string"}},
    "required": ["person", "date"]})


def test_http_engine_answers_and_records_usage(stub):
    _Stub.mode = "ok"
    out = llm.call(system="extract", prompt="who resigned?",
                   schema=SCHEMA, capability="test.extract")
    assert out == {"person": "Elena Marsh", "date": "12 March 2026"}


def test_bad_credentials_are_a_canonical_error_not_an_answer(stub):
    _Stub.mode = "unauthorized"
    with pytest.raises(llm.EngineFailed) as exc:
        llm.call(system="extract", prompt="who resigned?",
                 schema=SCHEMA, capability="test.extract")
    assert "PROVIDER_UNAVAILABLE" in str(exc.value)


def test_schema_violation_is_canonical_and_not_repaired(stub):
    _Stub.mode = "bad_schema"
    with pytest.raises(llm.OpFailed) as exc:
        llm.call(system="extract", prompt="who resigned?",
                 schema=SCHEMA, capability="test.extract")
    assert "SCHEMA_INVALID" in str(exc.value)


def test_tool_calls_refused_on_http_engine(stub):
    _Stub.mode = "ok"
    with pytest.raises(llm.EngineFailed) as exc:
        llm.call(system="x", prompt="y", capability="test.tools",
                 tools=["shell"])
    assert "ENGINE_UNSUPPORTED" in str(exc.value)
