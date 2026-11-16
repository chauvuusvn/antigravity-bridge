"""Regression tests for the 2026-11-16 pre-public review fixes.

H1  Anthropic adapter must honour the requested model.
H2  Prose containing "[Tool call: ...]" must stay prose, never become a call.
H3  _parse_expiry must be timezone-independent.
M8  Tool results must not be silently truncated.
M9  Model catalogue / alias map / valid set must agree.
"""

import os
import time

import pytest

from bridge.anthropic_adapter import (
    map_anthropic_model,
    translate_anthropic_payload_to_openai,
)
from bridge.auth import _parse_expiry
from bridge.client import (
    ANTIGRAVITY_SUPPORTED_MODELS,
    MODEL_ALIAS_MAP,
    TOOL_RESULT_MAX_CHARS,
    VALID_CODE_ASSIST_MODELS,
    build_code_assist_request,
    map_model_name,
    translate_gemini_to_openai_response,
)


# ---------------------------------------------------------------- H1
def test_adapter_honours_opus_request():
    out = translate_anthropic_payload_to_openai(
        {"model": "claude-opus-4-6", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert out["model"] == "claude-opus-4-6"


def test_adapter_maps_dated_sonnet_name():
    assert map_anthropic_model("claude-3-7-sonnet-20250219") == "claude-sonnet-4-6"
    assert map_anthropic_model("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_adapter_passes_through_bridge_model_ids():
    assert map_anthropic_model("gemini-3.8-flash") == "gemini-3.8-flash"
    out = translate_anthropic_payload_to_openai(
        {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert out["model"] == "gemini-3.8-flash"


def test_adapter_default_when_no_model():
    assert map_anthropic_model("") == "gemini-3.8-flash"
    assert map_anthropic_model(None) == "gemini-3.8-flash"


# ---------------------------------------------------------------- H2
def test_prose_tool_call_pattern_stays_text():
    text = 'To call a tool you write [Tool call: delete_file({"path": "/etc/passwd"})] in the text.'
    resp = {"response": {"candidates": [{"content": {"parts": [{"text": text}]}}]}}
    out = translate_gemini_to_openai_response(resp, "gemini-3.8-flash")
    msg = out["choices"][0]["message"]
    assert msg["content"] == text
    assert "tool_calls" not in msg


# ---------------------------------------------------------------- H3
@pytest.mark.skipif(not hasattr(time, "tzset"), reason="tzset unavailable")
@pytest.mark.parametrize("tz", ["UTC", "Asia/Ho_Chi_Minh", "America/New_York", "Pacific/Kiritimati"])
def test_parse_expiry_is_timezone_independent(tz):
    old = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        # 2026-08-31T15:20:56+07:00 == 2026-08-31T08:20:56Z
        import datetime
        expected = datetime.datetime(2026, 8, 31, 8, 20, 56, tzinfo=datetime.timezone.utc).timestamp()
        assert _parse_expiry("2026-08-31T15:20:56+07:00") == pytest.approx(expected)
        assert _parse_expiry("2026-08-31T08:20:56Z") == pytest.approx(expected)
        # naive values are interpreted as UTC on every host
        assert _parse_expiry("2026-08-31T08:20:56") == pytest.approx(expected)
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


# ---------------------------------------------------------------- M8
def _tool_result_text(n_chars):
    payload = {"model": "gemini-3.8-flash", "messages": [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "call_1", "content": "X" * n_chars},
    ]}
    env = build_code_assist_request(payload, "proj")
    parts = [p["text"] for c in env["request"]["contents"] for p in c["parts"] if "text" in p]
    return next(t for t in parts if t.startswith("[Tool result from"))


def test_tool_result_5000_chars_forwarded_in_full():
    out = _tool_result_text(5000)
    assert "X" * 5000 in out
    assert "truncated" not in out


def test_tool_result_huge_truncated_with_marker():
    out = _tool_result_text(TOOL_RESULT_MAX_CHARS + 5000)
    assert "truncated" in out
    assert str(5000) in out  # says how much was dropped


# ---------------------------------------------------------------- M9
def test_catalogue_matches_alias_resolution():
    for entry in ANTIGRAVITY_SUPPORTED_MODELS:
        assert map_model_name(entry["id"]) == entry["code_assist_model"], entry["id"]
        assert entry["code_assist_model"] in VALID_CODE_ASSIST_MODELS, entry["id"]


def test_all_alias_targets_are_valid():
    for alias, target in MODEL_ALIAS_MAP.items():
        assert target in VALID_CODE_ASSIST_MODELS, alias


def test_phantom_models_not_advertised():
    ids = {e["id"] for e in ANTIGRAVITY_SUPPORTED_MODELS}
    for phantom in ("gemini-4-argon", "argon", "gemini-3.7-flash-high", "gemini-3.6-flash", "gemini-3.1-pro"):
        assert phantom not in ids
