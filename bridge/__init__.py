"""Antigravity Local OAuth Bridge.

Provides a local OpenAI- and Anthropic-compatible HTTP daemon that presents
Antigravity IDE/CLI client headers, manages Google OAuth 2.0 authentication,
and translates OpenAI / Anthropic chat payloads into Gemini / Code Assist
generateContent requests for any local client application.
"""

from __future__ import annotations

try:  # Repository layout: import as bridge.*
    from .auth import (
        AntigravityAuthManager,
        AntigravityCredentials,
    )
    from .client import (
        AntigravityClient,
        ANTIGRAVITY_SUPPORTED_MODELS,
    )
    from .server import (
        AntigravityBridgeServer,
        run_server,
    )
except ImportError as _layout_error:  # Installed layout: antigravity_bridge.*
    # Only fall back when the missing module is this package's own layout.
    # A failure INSIDE a submodule (e.g. httpx not installed) must surface
    # as itself, not be masked by a second, misleading import attempt.
    if _layout_error.name not in (None, "bridge") and not str(_layout_error.name).startswith("bridge."):
        raise
    from antigravity_bridge.auth import (
        AntigravityAuthManager,
        AntigravityCredentials,
    )
    from antigravity_bridge.client import (
        AntigravityClient,
        ANTIGRAVITY_SUPPORTED_MODELS,
    )
    from antigravity_bridge.server import (
        AntigravityBridgeServer,
        run_server,
    )

__all__ = [
    "AntigravityAuthManager",
    "AntigravityCredentials",
    "AntigravityClient",
    "ANTIGRAVITY_SUPPORTED_MODELS",
    "AntigravityBridgeServer",
    "run_server",
]
