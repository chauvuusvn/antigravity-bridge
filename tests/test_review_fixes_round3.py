"""Regression tests for the 2026-11-16 round-3 review fixes (Low group).

L3  schema sanitizer: nullable type lists + unions at any nesting level
L4  request fidelity: developer role, system-text duplication, stop/top_k/
    seed/response_format mapping, remote image fetch
L7  non-JSON upstream body and transport errors become typed 502s;
    streaming falls back once on a primary 5xx
L9  the server registers a cleanup hook for its HTTP client
"""

import asyncio
import io
import json
from unittest import mock

import pytest

from bridge.client import (
    AntigravityClient,
    CodeAssistError,
    _coerce_content_to_parts,
    _sanitize_schema_for_code_assist,
    build_code_assist_request,
)
from bridge.server import AntigravityBridgeServer


# ---------------------------------------------------------------- L3
def test_sanitizer_nullable_type_list():
    out = _sanitize_schema_for_code_assist({"type": ["integer", "null"]})
    assert out["type"] == "integer"
    assert out["nullable"] is True


def test_sanitizer_union_inside_items():
    out = _sanitize_schema_for_code_assist({
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        },
    })
    assert out["properties"]["tags"]["items"]["type"] == "string"
    assert "anyOf" not in out["properties"]["tags"]["items"]


def test_sanitizer_top_level_union():
    out = _sanitize_schema_for_code_assist({"oneOf": [{"type": "integer"}, {"type": "string"}]})
    assert out["type"] == "integer"


# ---------------------------------------------------------------- L4
def test_developer_role_goes_to_system_instruction():
    env = build_code_assist_request({
        "model": "gemini-3.8-flash",
        "messages": [
            {"role": "developer", "content": "Be terse."},
            {"role": "user", "content": "hi"},
        ],
    }, project_id="p")
    assert env["request"]["systemInstruction"]["parts"][0]["text"] == "Be terse."
    user_texts = [p.get("text", "") for c in env["request"]["contents"] for p in c["parts"]]
    assert "Be terse." not in user_texts


def test_system_only_payload_does_not_duplicate_system_text():
    env = build_code_assist_request({
        "model": "gemini-3.8-flash",
        "messages": [{"role": "system", "content": "You are a pirate."}],
    }, project_id="p")
    contents = env["request"]["contents"]
    assert len(contents) == 1
    assert contents[0]["parts"][0]["text"] == "Hello"
    assert env["request"]["systemInstruction"]["parts"][0]["text"] == "You are a pirate."


def test_generation_config_mappings():
    env = build_code_assist_request({
        "model": "gemini-3.8-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "stop": ["END", "STOP"],
        "top_k": 40,
        "seed": 7,
        "response_format": {"type": "json_object"},
    }, project_id="p")
    gc = env["request"]["generationConfig"]
    assert gc["stopSequences"] == ["END", "STOP"]
    assert gc["topK"] == 40
    assert gc["seed"] == 7
    assert gc["responseMimeType"] == "application/json"


class _FakeHTTPResp:
    def __init__(self, content_type, body):
        self.headers = {"Content-Type": content_type}
        self._body = body

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_remote_image_fetched_to_inline_data():
    fake = _FakeHTTPResp("image/png", b"\x89PNG-fake")
    with mock.patch("urllib.request.urlopen", return_value=fake):
        parts = _coerce_content_to_parts([{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}])
    assert parts and parts[0]["inlineData"]["mimeType"] == "image/png"
    import base64
    assert base64.b64decode(parts[0]["inlineData"]["data"]) == b"\x89PNG-fake"


def test_remote_image_failure_leaves_marker_not_silence():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("no network")):
        parts = _coerce_content_to_parts([{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}])
    assert parts and "could not be fetched" in parts[0].get("text", "")


# ---------------------------------------------------------------- L7
class FakeAuth:
    def resolve_valid_credentials(self, bearer_token=""):
        from bridge.auth import AntigravityCredentials
        return AntigravityCredentials(access_token="tok", project_id="proj")


class FakeResp:
    def __init__(self, status_code=200, payload=None, text="", json_raises=False):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("not json")
        return self._payload


class FakeHTTP:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def post(self, url, json=None, headers=None):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


PAYLOAD = {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hi"}]}
OK_GEMINI = {"response": {"candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}]}}


def make_client(responses):
    client = AntigravityClient(auth_manager=FakeAuth())
    client._http = FakeHTTP(responses)
    return client


def test_non_json_upstream_body_is_typed_502():
    client = make_client([FakeResp(200, json_raises=True)])
    with pytest.raises(CodeAssistError) as ei:
        asyncio.run(client.create_chat_completion(PAYLOAD, bearer_token="k"))
    assert ei.value.status_code == 502


def test_transport_error_is_typed_502():
    import httpx
    client = make_client([httpx.ConnectError("refused", request=httpx.Request("POST", "https://x"))])
    with pytest.raises(CodeAssistError) as ei:
        asyncio.run(client.create_chat_completion(PAYLOAD, bearer_token="k"))
    assert ei.value.status_code == 502


class FakeStreamResp:
    def __init__(self, status_code, lines=()):
        self.status_code = status_code
        self._lines = list(lines)

    async def aread(self):
        return b"server error"

    async def aiter_text(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeStreamHTTP:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def stream(self, method, url, json=None, headers=None):
        self.calls += 1
        return self._responses.pop(0)


def _sse_event(text):
    return "data: " + json.dumps({"response": {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}}) + "\n\n"


def test_streaming_falls_back_once_on_primary_5xx():
    client = AntigravityClient(auth_manager=FakeAuth())
    client._http = FakeStreamHTTP([FakeStreamResp(500), FakeStreamResp(200, [_sse_event("from-fallback")])])

    async def collect():
        out = []
        async for chunk in client.stream_chat_completion(PAYLOAD, bearer_token="k"):
            out.append(chunk)
        return "".join(out)

    text = asyncio.run(collect())
    assert client._http.calls == 2
    assert "from-fallback" in text
    assert "[DONE]" in text


def test_streaming_4xx_does_not_fall_back():
    client = AntigravityClient(auth_manager=FakeAuth())
    client._http = FakeStreamHTTP([FakeStreamResp(429)])

    async def collect():
        async for _ in client.stream_chat_completion(PAYLOAD, bearer_token="k"):
            pass

    with pytest.raises(CodeAssistError) as ei:
        asyncio.run(collect())
    assert ei.value.status_code == 429
    assert client._http.calls == 1


# ---------------------------------------------------------------- L9
def test_server_registers_cleanup_hook():
    srv = AntigravityBridgeServer.__new__(AntigravityBridgeServer)
    # Build the real object but avoid binding a port: __init__ only sets up
    # the app and routes; constructing it is enough to inspect on_cleanup.
    srv2 = AntigravityBridgeServer(host="127.0.0.1", port=0, api_key="k", home="/tmp")
    assert len(srv2.app.on_cleanup) >= 1
