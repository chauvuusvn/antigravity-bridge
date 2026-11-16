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
except ImportError:  # Installed layout: import as antigravity_bridge.*
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
