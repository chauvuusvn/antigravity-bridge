"""Tests for the 2026-11-15 hardening pass:

- _check_auth accepts ONLY the instance's real API key (no placeholder
  keys, no keyless LAN/loopback bypass).
- Upstream errors keep their real HTTP status end-to-end (429/403 are not
  flattened to 500, and are never guessed from message text).
- The non-streaming client no longer retries the fallback host on 4xx
  (a quota/disabled verdict would just burn a second upstream call);
  fallback is reserved for endpoint-level 5xx.
"""

import asyncio

import pytest

from bridge.auth import AntigravityCredentials
from bridge.client import AntigravityClient, CodeAssistError
from bridge.server import AntigravityBridgeServer



# ---------------------------------------------------------------------------
# _check_auth
# ---------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, headers=None, remote="127.0.0.1"):
        self.headers = headers or {}
        self.remote = remote


def make_server(tmp_path):
    return AntigravityBridgeServer(api_key="real-key-123", home=tmp_path)


def test_check_auth_accepts_real_key_bearer(tmp_path):
    s = make_server(tmp_path)
    assert s._check_auth(FakeRequest({"Authorization": "Bearer real-key-123"})) is True


def test_check_auth_accepts_real_key_x_api_key(tmp_path):
    s = make_server(tmp_path)
    assert s._check_auth(FakeRequest({"x-api-key": "real-key-123"})) is True


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer any-key"},
        {"Authorization": "Bearer not-needed"},
        {"Authorization": "Bearer no-key-required"},
        {"Authorization": "Bearer sk-local"},
        {"Authorization": "placeholder-key"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer wrong-key"},
        {},
    ],
)
def test_check_auth_rejects_placeholders_wrong_and_missing_keys(tmp_path, headers):
    s = make_server(tmp_path)
    # Even from a loopback or LAN peer, placeholders / wrong / missing keys fail.
    assert s._check_auth(FakeRequest(headers, remote="127.0.0.1")) is False
    assert s._check_auth(FakeRequest(headers, remote="203.0.113.50")) is False
    assert s._check_auth(FakeRequest(headers, remote="198.51.100.108")) is False


# ---------------------------------------------------------------------------
# _status_for_error
# ---------------------------------------------------------------------------

def test_status_for_error_uses_real_upstream_status():
    assert AntigravityBridgeServer._status_for_error(CodeAssistError(429, "quota")) == 429
    assert AntigravityBridgeServer._status_for_error(CodeAssistError(403, "disabled")) == 403
    assert AntigravityBridgeServer._status_for_error(CodeAssistError(400, "bad request")) == 400
    assert AntigravityBridgeServer._status_for_error(CodeAssistError(503, "down")) == 503


def test_status_for_error_auth_and_unknown():
    assert AntigravityBridgeServer._status_for_error(RuntimeError("unauthorized")) == 401
    # Status is never guessed from digits inside an arbitrary message text.
    assert AntigravityBridgeServer._status_for_error(RuntimeError("boom 429 in text")) == 500
    assert AntigravityBridgeServer._status_for_error(RuntimeError("plain failure")) == 500


# ---------------------------------------------------------------------------
# client: fallback policy + typed errors
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeHTTP:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def post(self, url, json=None, headers=None):
        self.calls += 1
        return self._responses.pop(0)


class FakeAuth:
    def resolve_valid_credentials(self, bearer_token=""):
        return AntigravityCredentials(access_token="tok", project_id="")


PAYLOAD = {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hi"}]}
OK_GEMINI = {
    "response": {
        "candidates": [
            {"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}
        ]
    }
}


def make_client(responses):
    client = AntigravityClient(auth_manager=FakeAuth())
    client._http = FakeHTTP(responses)
    return client


def test_client_429_raises_typed_error_with_single_upstream_call():
    client = make_client([FakeResp(429, text="quota exhausted")])
    with pytest.raises(CodeAssistError) as ei:
        asyncio.run(client.create_chat_completion(PAYLOAD, bearer_token="k"))
    assert ei.value.status_code == 429
    assert client._http.calls == 1


def test_client_403_raises_typed_error_with_single_upstream_call():
    client = make_client([FakeResp(403, text="account disabled")])
    with pytest.raises(CodeAssistError) as ei:
        asyncio.run(client.create_chat_completion(PAYLOAD, bearer_token="k"))
    assert ei.value.status_code == 403
    assert client._http.calls == 1


def test_client_500_falls_back_once_and_succeeds():
    client = make_client([FakeResp(500, text="boom"), FakeResp(200, payload=OK_GEMINI)])
    out = asyncio.run(client.create_chat_completion(PAYLOAD, bearer_token="k"))
    assert client._http.calls == 2
    assert out["choices"][0]["message"]["content"] == "hello"


def test_client_500_then_429_reports_the_fallback_verdict():
    client = make_client([FakeResp(500, text="boom"), FakeResp(429, text="quota")])
    with pytest.raises(CodeAssistError) as ei:
        asyncio.run(client.create_chat_completion(PAYLOAD, bearer_token="k"))
    assert ei.value.status_code == 429
    assert client._http.calls == 2
