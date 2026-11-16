"""Auto-refresh test for hardened Antigravity bridge (Task: auto-refresh)."""
import os
import sys
import json
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.auth import AntigravityAuthManager, _parse_expiry  # noqa: E402


def test_parse_expiry_iso():
    iso = "2026-08-31T15:20:56.074422314+07:00"
    ts = _parse_expiry(iso)
    assert ts > 0
    # Should be ~8h ahead of 1970 epoch; just assert plausible
    assert ts > 1_700_000_000


def test_parse_expiry_float():
    assert _parse_expiry(1234.5) == 1234.5
    assert _parse_expiry("0") == 0.0


def test_refresh_writes_instance_local_only(tmp_path):
    """refresh_access_token should write ONLY the instance token file, not a host app auth store."""
    home = tmp_path / "home"
    tok_dir = home / ".gemini" / "antigravity-cli"
    tok_dir.mkdir(parents=True)
    tf = tok_dir / "antigravity-oauth-token"
    tf.write_text(json.dumps({
        "auth_method": "consumer",
        "token": {
            "access_token": "ya29.OLD",
            "refresh_token": "1//refresh",
            "expiry": "2020-01-01T00:00:00+00:00",
        },
    }), encoding="utf-8")

    mgr = AntigravityAuthManager(api_key="k", home=home)
    fake_resp = mock.MagicMock()
    fake_resp.read.return_value = json.dumps({
        "access_token": "ya29.NEW", "expires_in": 3600
    }).encode()
    fake_resp.__enter__.return_value = fake_resp

    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        creds = mgr.refresh_access_token(mgr.load_instance_credentials())

    assert creds.access_token == "ya29.NEW"
    # file updated with new token
    data = json.load(open(tf))
    assert data["token"]["access_token"] == "ya29.NEW"
    # NO replication
    bad = tmp_path / ".host-app" / "auth.json"
    assert not bad.exists()


def test_resolve_auto_refreshes_when_expired(tmp_path):
    home = tmp_path / "home"
    tok_dir = home / ".gemini" / "antigravity-cli"
    tok_dir.mkdir(parents=True)
    tf = tok_dir / "antigravity-oauth-token"
    tf.write_text(json.dumps({
        "auth_method": "consumer",
        "token": {
            "access_token": "ya29.OLD",
            "refresh_token": "1//refresh",
            "expiry": "2020-01-01T00:00:00+00:00",
        },
    }), encoding="utf-8")

    mgr = AntigravityAuthManager(api_key="k", home=home)
    fake_resp = mock.MagicMock()
    fake_resp.read.return_value = json.dumps({
        "access_token": "ya29.REFRESHED", "expires_in": 3600
    }).encode()
    fake_resp.__enter__.return_value = fake_resp

    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        creds = mgr.resolve_valid_credentials(bearer_token="k")
    assert creds.access_token == "ya29.REFRESHED"


def test_resolve_rejects_wrong_key(tmp_path):
    home = tmp_path / "home"
    tok_dir = home / ".gemini" / "antigravity-cli"
    tok_dir.mkdir(parents=True)
    (tok_dir / "antigravity-oauth-token").write_text(json.dumps({
        "auth_method": "consumer",
        "token": {"access_token": "ya29.x", "refresh_token": "r", "expiry": "2099-01-01T00:00:00+00:00"},
    }), encoding="utf-8")
    mgr = AntigravityAuthManager(api_key="correct", home=home)
    with pytest.raises(RuntimeError):
        mgr.resolve_valid_credentials(bearer_token="wrong")
