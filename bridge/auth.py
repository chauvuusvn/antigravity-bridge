"""Isolated Antigravity token loader + auto-refresh for Local Bridge (hardened fork).

Security model:
- Each bridge instance is bound to exactly ONE account via its HOME env var.
- Tokens are read ONLY from $HOME/.gemini/antigravity-cli/antigravity-oauth-token.
- NO cross-account discovery, NO replication into any host application's auth store or profile.
- Bearer mismatch => unauthorized (401), never falls back to a default account.
- Auto-refresh: 120s before expiry, mint a fresh access_token via the stored
  refresh_token and write it back to the SAME instance-local file (nested agy format).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

REFRESH_SKEW_SECONDS = 120
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Public client credentials from the Antigravity OAuth plugin (consumer tier).
DEFAULT_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
DEFAULT_CLIENT_SECRET = "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"
DEFAULT_PROJECT_ID = "aicode-consumers"

# agy stores the OAuth token here, relative to the instance HOME.
CLAUTH_TOKEN_REL = ".gemini/antigravity-cli/antigravity-oauth-token"


@dataclass
class AntigravityCredentials:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    email: str = ""
    project_id: str = ""
    source: str = "instance_home"

    @property
    def is_expired(self) -> bool:
        if not self.access_token:
            return True
        if self.expires_at <= 0:
            return False
        return (self.expires_at - time.time()) < REFRESH_SKEW_SECONDS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "email": self.email,
            "project_id": self.project_id,
            "source": self.source,
            "updated_at": time.time(),
        }


def _parse_expiry(value) -> float:
    """Parse agy expiry which may be a float or an ISO-8601 string with tz."""
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        # Numeric seconds
        return float(s)
    except ValueError:
        pass
    # ISO format: 2026-08-31T15:20:56.074422314+07:00
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # A naive timestamp from agy is UTC. Interpreting it in the
            # host's local timezone (the old behaviour) shifted the epoch
            # by the local UTC offset (e.g. -7h in Asia/Ho_Chi_Minh, +5h
            # in US Eastern), corrupting every expiry decision.
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        # An aware datetime's .timestamp() is the correct epoch on any host.
        return dt.timestamp()
    except Exception:
        return 0.0


class AntigravityAuthManager:
    """Instance-local auth: one account, one HOME, auto-refresh, no replication.

    api_key: per-instance bridge API key the HTTP client must present.
    home: the instance HOME directory (e.g. .../hagi-oauth-accounts/oauth1).
    """

    def __init__(self, api_key: str, home: Optional[Path] = None) -> None:
        self.api_key = api_key
        self.home = Path(home or os.environ.get("HOME") or Path.home()).expanduser()
        self._lock = threading.Lock()
        self._cached: Optional[AntigravityCredentials] = None

    @property
    def token_file(self) -> Path:
        return self.home / CLAUTH_TOKEN_REL

    def load_instance_credentials(self) -> Optional[AntigravityCredentials]:
        """Read ONLY the token file inside this instance's HOME (nested agy format)."""
        tf = self.token_file
        if not tf.is_file():
            return None
        try:
            with open(tf, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("Failed reading instance token %s: %s", tf, e)
            return None
        token_field = data.get("token") or {}
        if isinstance(token_field, dict):
            token = token_field.get("access_token") or token_field.get("token") or ""
            refresh = token_field.get("refresh_token") or ""
            raw_expiry = token_field.get("expiry") or token_field.get("expires_at")
        else:
            token = token_field or ""
            refresh = ""
            raw_expiry = None
        if not token:
            return None
        creds = AntigravityCredentials(
            access_token=token,
            refresh_token=refresh,
            expires_at=_parse_expiry(raw_expiry),
            email=data.get("email") or "",
            source="instance_home",
        )
        return creds

    def _write_token_file(self, creds: AntigravityCredentials) -> None:
        """Persist refreshed token back to the SAME instance-local file (nested format)."""
        tf = self.token_file
        try:
            tf.parent.mkdir(parents=True, exist_ok=True)
            # FIX 2026-10-05: server chạy UTC nhưng code cũ dùng time.localtime()
            # rồi gắn cứng suffix +07:00 -> expiry trong file sai lệch 7 tiếng
            # (trông như hết hạn dù token còn hạn). Ghi đúng giờ +07:00 thật.
            import datetime as _dt
            _tz7 = _dt.timezone(_dt.timedelta(hours=7))
            _exp = _dt.datetime.fromtimestamp(creds.expires_at, tz=_tz7)
            payload = {
                "auth_method": "consumer",
                "token": {
                    "access_token": creds.access_token,
                    "token_type": "Bearer",
                    "refresh_token": creds.refresh_token,
                    "expiry": _exp.strftime("%Y-%m-%dT%H:%M:%S.000000+07:00"),
                },
            }
            tmp = tf.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, tf)
            logger.info("Refreshed token written to %s", tf)
        except Exception as e:
            logger.error("Failed writing refreshed token to %s: %s", tf, e)

    def refresh_access_token(self, creds: AntigravityCredentials) -> AntigravityCredentials:
        """Mint a fresh access_token via refresh_token (Google OAuth token endpoint)."""
        if not creds.refresh_token:
            raise RuntimeError("No refresh_token available; re-login with the Antigravity CLI (agy) first.")
        payload = urllib.parse.urlencode({
            "client_id": DEFAULT_CLIENT_ID,
            "client_secret": DEFAULT_CLIENT_SECRET,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")
        req = urllib.request.Request(
            TOKEN_ENDPOINT,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        new_token = raw.get("access_token")
        if not new_token:
            raise RuntimeError(f"Token refresh failed: {raw}")
        expires_in = float(raw.get("expires_in") or 3600.0)
        creds.access_token = new_token
        creds.expires_at = time.time() + expires_in
        if raw.get("refresh_token"):
            creds.refresh_token = raw["refresh_token"]
        # Persist back to instance-local file (no replication elsewhere).
        self._write_token_file(creds)
        return creds

    def resolve_valid_credentials(self, bearer_token: str = "") -> AntigravityCredentials:
        """Return valid instance credentials; auto-refresh if within skew.

        Raises RuntimeError("unauthorized") on bearer mismatch/empty — NO fallback.
        """
        with self._lock:
            if not bearer_token or bearer_token != self.api_key:
                raise RuntimeError("unauthorized")
            creds = self._cached or self.load_instance_credentials()
            if not creds:
                raise RuntimeError(
                    f"No Antigravity token in instance HOME {self.home}. Log in with the Antigravity CLI (agy) first."
                )
            if creds.is_expired:
                if creds.refresh_token:
                    logger.info("Token near/at expiry; refreshing...")
                    creds = self.refresh_access_token(creds)
                else:
                    raise RuntimeError(
                        f"Antigravity token for {creds.email or 'account'} is expired. Re-login with the Antigravity CLI (agy)."
                    )
            self._cached = creds
            return creds

    # ---- save_credentials: intentionally a NO-OP for replication ----
    # Refresh writes directly via _write_token_file; we never sync tokens anywhere else.
    def save_credentials(self, creds: AntigravityCredentials) -> None:  # pragma: no cover - disabled by design
        logger.debug("save_credentials disabled in isolated bridge; refresh writes instance-local only.")

    def clear_credentials(self) -> bool:
        """Clear only the in-memory cache. Token file is agy-owned; do not delete."""
        self._cached = None
        return True
