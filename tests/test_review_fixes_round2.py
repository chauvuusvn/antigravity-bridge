"""Regression tests for the 2026-11-16 round-2 review fixes (Medium group).

M1  unparseable expiry counts as expired; missing expiry (legacy) does not
M2  non-ASCII bearer token -> clean auth failure, never an exception
M3  payload shape validation (non-object body / non-list messages)
M4  refresh failures: revoked token -> unauthorized; Google 5xx -> 502 class
M6  real usage from usageMetadata (non-streaming + streaming)
M7  finish_reason / stop_reason fidelity (length, content_filter, max_tokens)
M10 token-file writes preserve unknown fields; load populates project_id
M12 AntigravityClient() with neither manager nor key fails clearly
M14 Anthropic adapter: images, top_p/stop/tool_choice, thinking blocks
L1  auth header: case-insensitive scheme, x-api-key not shadowed
L8  Anthropic error envelope shape
"""

import asyncio
import json
import urllib.error
from unittest import mock

import pytest

from bridge.anthropic_adapter import (
    stream_anthropic_sse_adapter,
    translate_anthropic_payload_to_openai,
    translate_openai_response_to_anthropic,
)
from bridge.auth import AntigravityAuthManager, AntigravityCredentials
from bridge.client import (
    AntigravityClient,
    translate_gemini_stream_event,
    translate_gemini_to_openai_response,
)
from bridge.server import AntigravityBridgeServer


# ---------------------------------------------------------------- M1
def test_unparseable_expiry_is_expired(tmp_path):
    home = tmp_path / "home"
    tok_dir = home / ".gemini" / "antigravity-cli"
    tok_dir.mkdir(parents=True)
    (tok_dir / "antigravity-oauth-token").write_text(json.dumps({
        "auth_method": "consumer",
        "token": {"access_token": "ya29.x", "refresh_token": "1//r", "expiry": "garbage-date"},
    }), encoding="utf-8")
    mgr = AntigravityAuthManager(api_key="k", home=home)
    creds = mgr.load_instance_credentials()
    assert creds is not None and creds.is_expired


def test_missing_expiry_legacy_token_not_expired(tmp_path):
    home = tmp_path / "home"
    tok_dir = home / ".gemini" / "antigravity-cli"
    tok_dir.mkdir(parents=True)
    (tok_dir / "antigravity-oauth-token").write_text(json.dumps({
        "auth_method": "consumer", "token": "ya29.legacy",
    }), encoding="utf-8")
    mgr = AntigravityAuthManager(api_key="k", home=home)
    creds = mgr.load_instance_credentials()
    assert creds is not None and not creds.is_expired


# ---------------------------------------------------------------- M2/L1
class _Req:
    def __init__(self, headers):
        self.headers = headers


def _server():
    return AntigravityBridgeServer.__new__(AntigravityBridgeServer)


def test_non_ascii_bearer_is_clean_failure():
    srv = _server()
    srv.api_key = "realkey"
    assert srv._check_auth(_Req({"Authorization": "Bearer kéy"})) is False


def test_lowercase_bearer_scheme_accepted():
    srv = _server()
    srv.api_key = "realkey"
    assert srv._check_auth(_Req({"Authorization": "bearer realkey"})) is True


def test_x_api_key_not_shadowed_by_wrong_authorization():
    srv = _server()
    srv.api_key = "realkey"
    req = _Req({"Authorization": "Bearer wrong", "x-api-key": "realkey"})
    assert srv._check_auth(req) is True


# ---------------------------------------------------------------- M3/L8
def test_validate_payload():
    v = AntigravityBridgeServer._validate_payload
    assert v({"messages": []}) is None
    assert v([1, 2]) is not None
    assert v("text") is not None
    assert v(42) is not None
    assert v({"messages": "notalist"}) is not None


def test_anthropic_error_envelope():
    resp = AntigravityBridgeServer._anthropic_error("bad", 400)
    body = json.loads(resp.text)
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    resp = AntigravityBridgeServer._anthropic_error("no", 401)
    assert json.loads(resp.text)["error"]["type"] == "authentication_error"


# ---------------------------------------------------------------- M4
def test_refresh_revoked_token_maps_to_unauthorized(tmp_path):
    mgr = AntigravityAuthManager(api_key="k", home=tmp_path)
    creds = AntigravityCredentials(access_token="old", refresh_token="1//revoked")
    err = urllib.error.HTTPError("https://oauth2.googleapis.com/token", 400, "Bad Request", {}, None)
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError) as ei:
            mgr.refresh_access_token(creds)
    assert "unauthorized" in str(ei.value)


def test_refresh_google_5xx_maps_to_502(tmp_path):
    mgr = AntigravityAuthManager(api_key="k", home=tmp_path)
    creds = AntigravityCredentials(access_token="old", refresh_token="1//r")
    err = urllib.error.HTTPError("https://oauth2.googleapis.com/token", 503, "Unavailable", {}, None)
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError) as ei:
            mgr.refresh_access_token(creds)
    assert AntigravityBridgeServer._status_for_error(ei.value) == 502


# ---------------------------------------------------------------- M6/M7
def test_usage_and_finish_reason_non_streaming():
    resp = {"response": {
        "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "MAX_TOKENS"}],
        "usageMetadata": {"promptTokenCount": 123, "candidatesTokenCount": 45, "totalTokenCount": 168},
    }}
    out = translate_gemini_to_openai_response(resp, "gemini-3.8-flash")
    assert out["usage"] == {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168}
    assert out["choices"][0]["finish_reason"] == "length"

    resp2 = {"response": {"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "SAFETY"}]}}
    out2 = translate_gemini_to_openai_response(resp2, "gemini-3.8-flash")
    assert out2["choices"][0]["finish_reason"] == "content_filter"


def test_stream_event_usage_and_finish():
    event = {"response": {
        "candidates": [{"content": {"parts": [{"text": "tail"}]}, "finishReason": "MAX_TOKENS"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 7, "totalTokenCount": 17},
    }}
    chunk = translate_gemini_stream_event(event, "gemini-3.8-flash", "chatcmpl-x")
    assert chunk["choices"][0]["finish_reason"] == "length"
    assert chunk["usage"]["total_tokens"] == 17


def test_anthropic_stop_reason_max_tokens():
    resp = {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 9}}
    out = translate_openai_response_to_anthropic(resp, model_name="m")
    assert out["stop_reason"] == "max_tokens"
    assert out["usage"] == {"input_tokens": 3, "output_tokens": 9}


# ---------------------------------------------------------------- M10
def test_write_token_file_preserves_unknown_fields(tmp_path):
    home = tmp_path / "home"
    tok_dir = home / ".gemini" / "antigravity-cli"
    tok_dir.mkdir(parents=True)
    tf = tok_dir / "antigravity-oauth-token"
    tf.write_text(json.dumps({
        "auth_method": "consumer",
        "email": "user@example.com",
        "project_id": "my-project",
        "token": {"access_token": "old", "refresh_token": "1//r", "expiry": "2020-01-01T00:00:00+00:00", "extra_field": "keep-me"},
    }), encoding="utf-8")
    mgr = AntigravityAuthManager(api_key="k", home=home)
    creds = mgr.load_instance_credentials()
    assert creds.project_id == "my-project"
    creds.access_token = "new-token"
    import time as _t
    creds.expires_at = _t.time() + 3600
    mgr._write_token_file(creds)
    data = json.load(open(tf))
    assert data["email"] == "user@example.com"
    assert data["project_id"] == "my-project"
    assert data["token"]["extra_field"] == "keep-me"
    assert data["token"]["access_token"] == "new-token"


# ---------------------------------------------------------------- M12
def test_client_requires_manager_or_key():
    with pytest.raises(ValueError):
        AntigravityClient()
    client = AntigravityClient(api_key="k")
    assert client.auth_manager.api_key == "k"


# ---------------------------------------------------------------- M14
def test_adapter_forwards_image_and_sampling_controls():
    out = translate_anthropic_payload_to_openai({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
            {"type": "text", "text": "what is this?"},
        ]}],
        "top_p": 0.9,
        "stop_sequences": ["END"],
        "tool_choice": {"type": "any"},
    })
    content = out["messages"][0]["content"]
    assert isinstance(content, list)
    assert any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/png") for p in content)
    assert out["top_p"] == 0.9
    assert out["stop"] == ["END"]
    assert out["tool_choice"] == "required"


def test_adapter_tool_result_error_marker():
    out = translate_anthropic_payload_to_openai({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True},
        ]}],
    })
    tool_msg = next(m for m in out["messages"] if m["role"] == "tool")
    assert "error" in tool_msg["content"] and "boom" in tool_msg["content"]


def test_adapter_response_thinking_block():
    resp = {"choices": [{"message": {"content": "answer", "reasoning_content": "because..."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 6}}
    out = translate_openai_response_to_anthropic(resp, model_name="m")
    assert out["content"][0] == {"type": "thinking", "thinking": "because..."}
    assert out["content"][1]["type"] == "text"


def test_adapter_stream_thinking_and_real_stop_reason():
    async def gen():
        yield 'data: {"choices": [{"delta": {"reasoning_content": "think"}, "finish_reason": null}]}\n\n'
        yield 'data: {"choices": [{"delta": {"content": "ans"}, "finish_reason": null}]}\n\n'
        yield 'data: {"choices": [{"delta": {}, "finish_reason": "length"}], "usage": {"prompt_tokens": 4, "completion_tokens": 42}}\n\n'
        yield "data: [DONE]\n\n"

    async def collect():
        events = []
        async for e in stream_anthropic_sse_adapter(gen(), model_name="m"):
            events.append(e)
        return "".join(events)

    text = asyncio.run(collect())
    assert '"type": "thinking"' in text and "thinking_delta" in text
    assert '"stop_reason": "max_tokens"' in text
    assert '"output_tokens": 42' in text
