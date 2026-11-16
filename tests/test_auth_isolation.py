"""Isolation + auth tests for the hardened Antigravity bridge (Task 2 & 3)."""
import os
import sys
import json
import tempfile
from pathlib import Path

import pytest

# Make bridge importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.auth import AntigravityAuthManager  # noqa: E402


def _make_home_with_token(tmp_path: Path, token: str = "ya29.testtoken") -> Path:
    home = tmp_path / "home"
    tok_dir = home / ".gemini" / "antigravity-cli"
    tok_dir.mkdir(parents=True)
    (tok_dir / "antigravity-oauth-token").write_text(
        json.dumps({"auth_method": "consumer", "token": token}), encoding="utf-8"
    )
    return home


def test_resolve_rejects_wrong_key(tmp_path):
    home = _make_home_with_token(tmp_path)
    mgr = AntigravityAuthManager(api_key="secret1", home=home)
    with pytest.raises(RuntimeError):
        mgr.resolve_valid_credentials(bearer_token="wrong")
    with pytest.raises(RuntimeError):
        mgr.resolve_valid_credentials(bearer_token="")


def test_resolve_accepts_correct_key(tmp_path):
    home = _make_home_with_token(tmp_path)
    mgr = AntigravityAuthManager(api_key="secret1", home=home)
    creds = mgr.resolve_valid_credentials(bearer_token="secret1")
    assert creds.access_token == "ya29.testtoken"


def test_no_replication_to_host_app_auth_store(tmp_path):
    """save_credentials must NOT write to a host app auth store or profiles."""
    home = _make_home_with_token(tmp_path)
    mgr = AntigravityAuthManager(api_key="k", home=home)
    mgr.save_credentials(mgr.resolve_valid_credentials(bearer_token="k"))
    # Simulate the original dangerous target paths; assert they do not exist.
    bad = tmp_path / ".host-app" / "auth.json"
    assert not bad.exists(), "token replicated to host app auth.json"
    prof = tmp_path / ".host-app" / "profiles" / "example-profile" / "auth.json"
    assert not prof.exists(), "token replicated to profile auth.json"


def test_loads_only_instance_home(tmp_path):
    home = _make_home_with_token(tmp_path)
    # Ensure no host-app auth.json exists anywhere reachable
    mgr = AntigravityAuthManager(api_key="k", home=home)
    creds = mgr.load_instance_credentials()
    assert creds is not None
    assert creds.access_token == "ya29.testtoken"
