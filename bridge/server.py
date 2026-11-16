"""HTTP server for isolated Antigravity Local Bridge (hardened fork).

Dual-Stack endpoints:
- OpenAI-compatible: /v1/chat/completions, /v1/models
- Anthropic-compatible: /v1/messages, /messages
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

try:
    from .auth import AntigravityAuthManager
    from .client import ANTIGRAVITY_SUPPORTED_MODELS, AntigravityClient, CodeAssistError
    from .version import __version__
    from .anthropic_adapter import (
        translate_anthropic_payload_to_openai,
        translate_openai_response_to_anthropic,
        stream_anthropic_sse_adapter,
    )
except ImportError:  # Installed layout
    from antigravity_bridge.auth import AntigravityAuthManager
    from antigravity_bridge.client import ANTIGRAVITY_SUPPORTED_MODELS, AntigravityClient, CodeAssistError
    from antigravity_bridge.version import __version__
    from antigravity_bridge.anthropic_adapter import (
        translate_anthropic_payload_to_openai,
        translate_openai_response_to_anthropic,
        stream_anthropic_sse_adapter,
    )

logger = logging.getLogger(__name__)

DEFAULT_BRIDGE_PORT = 8100
DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_CLIENT_MAX_SIZE = 64 * 1024 * 1024


class AntigravityBridgeServer:
    def __init__(
        self,
        host: str = DEFAULT_BRIDGE_HOST,
        port: int = DEFAULT_BRIDGE_PORT,
        api_key: str = "",
        home: Optional[Path] = None,
        auth_manager: Optional[AntigravityAuthManager] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.api_key = api_key
        self.home = Path(home or os.environ.get("HOME") or Path.home()).expanduser()
        self.auth_manager = auth_manager or AntigravityAuthManager(api_key=self.api_key, home=self.home)
        self.client = AntigravityClient(self.auth_manager)
        self.app = web.Application(client_max_size=DEFAULT_CLIENT_MAX_SIZE)
        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.on_cleanup.append(self._on_cleanup)
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/v1/models", self.handle_list_models)
        self.app.router.add_post("/v1/chat/completions", self.handle_chat_completions)
        # Anthropic Messages API routes for Claude Code CLI
        self.app.router.add_post("/v1/messages", self.handle_anthropic_messages)
        self.app.router.add_post("/messages", self.handle_anthropic_messages)

    async def _on_cleanup(self, app: web.Application) -> None:
        """Release the upstream HTTP connection pool on shutdown."""
        try:
            await self.client.close()
        except Exception:
            pass

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "bridge": "antigravity",
            "version": __version__,
            "home_configured": bool(self.home),
            "dual_stack": True,
            "timestamp": time.time(),
        })

    def _check_auth(self, request: web.Request) -> bool:
        """Return True only for this instance's real API key.

        Hardened 2026-11-15: placeholder keys ("any-key", "not-needed", ...)
        and keyless requests from LAN/Tailscale peers are no longer accepted.
        Every legitimate local client already holds the real
        per-instance key from bridge/keys.json.

        The scheme match is case-insensitive (per RFC 7235), a correct
        x-api-key is honoured even when a wrong Authorization header is
        also present, and comparison is byte-wise so a non-ASCII token
        yields a clean 401 instead of crashing compare_digest with a 500.
        """
        if not self.api_key:
            return False
        candidates = []
        auth_header = request.headers.get("Authorization")
        if auth_header:
            if auth_header.lower().startswith("bearer "):
                candidates.append(auth_header[7:].strip())
            else:
                candidates.append(auth_header.strip())
        api_key_header = request.headers.get("x-api-key")
        if api_key_header:
            candidates.append(api_key_header.strip())
        expected = self.api_key.encode("utf-8")
        return any(c and hmac.compare_digest(c.encode("utf-8"), expected) for c in candidates)

    @staticmethod
    def _status_for_error(e: Exception) -> int:
        """Map an exception to the HTTP status the bridge should return.

        CodeAssistError carries the real upstream status (429 quota
        exhausted, 403 account disabled). The auth layer's
        RuntimeError("unauthorized") maps to 401. Everything else is a
        bridge-side 500.
        """
        if isinstance(e, CodeAssistError):
            return e.status_code if 400 <= e.status_code <= 599 else 500
        if "unauthorized" in str(e):
            return 401
        if "token refresh failed" in str(e):
            return 502
        return 500

    @staticmethod
    def _validate_payload(payload) -> Optional[str]:
        """Return an error message if the decoded JSON body is unusable.

        Valid JSON that is not an object (a list, a string, a number)
        used to crash the handlers with a 500, and a non-list "messages"
        was silently replaced with an invented "Hello" upstream. Both
        are client errors: answer 400.
        """
        if not isinstance(payload, dict):
            return "Request body must be a JSON object."
        messages = payload.get("messages")
        if messages is not None and not isinstance(messages, list):
            return '"messages" must be an array.'
        return None

    @staticmethod
    def _anthropic_error(message: str, status: int) -> web.Response:
        """Anthropic-shaped error envelope: {"type": "error", "error": {...}}."""
        err_type = {
            400: "invalid_request_error",
            401: "authentication_error",
            403: "permission_error",
            404: "not_found_error",
            429: "rate_limit_error",
        }.get(status, "api_error")
        return web.json_response(
            {"type": "error", "error": {"type": err_type, "message": message}},
            status=status,
        )

    async def handle_list_models(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": {"message": "unauthorized", "type": "auth_error"}}, status=401)
        models = []
        for m in ANTIGRAVITY_SUPPORTED_MODELS:
            models.append({
                "id": m["id"],
                "object": "model",
                "created": int(time.time()),
                "owned_by": "antigravity",
                "permission": [],
                "description": m.get("description", ""),
            })
        return web.json_response({"object": "list", "data": models})

    async def handle_chat_completions(self, request: web.Request) -> web.StreamResponse:
        if not self._check_auth(request):
            return web.json_response({"error": {"message": "unauthorized", "type": "auth_error"}}, status=401)
        bearer = self.api_key

        try:
            payload = await request.json()
        except Exception as e:
            return web.json_response(
                {"error": {"message": f"Invalid JSON payload: {e}", "type": "invalid_request_error"}},
                status=400,
            )

        validation_error = self._validate_payload(payload)
        if validation_error:
            return web.json_response(
                {"error": {"message": validation_error, "type": "invalid_request_error"}},
                status=400,
            )

        is_stream = bool(payload.get("stream"))
        if is_stream:
            stream_gen = self.client.stream_chat_completion(payload, bearer_token=bearer)
            try:
                first_chunk = await stream_gen.__anext__()
            except Exception as e:
                logger.error("Stream connect error: %s", e)
                status_code = self._status_for_error(e)
                return web.json_response(
                    {"error": {"message": str(e), "type": "api_error", "code": status_code}},
                    status=status_code,
                )
            response = web.StreamResponse(
                status=200, reason="OK",
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
            try:
                await response.prepare(request)
                await response.write(first_chunk.encode("utf-8"))
                try:
                    async for chunk_str in stream_gen:
                        await response.write(chunk_str.encode("utf-8"))
                except Exception as e:
                    # Mid-stream failure: tell the client explicitly
                    # instead of a clean EOF that looks like a complete
                    # (but truncated) answer.
                    logger.error("Stream error: %s", e)
                    try:
                        err_chunk = {"error": {"message": str(e), "type": "api_error", "code": self._status_for_error(e)}}
                        await response.write(f"data: {json.dumps(err_chunk)}\n\n".encode("utf-8"))
                        await response.write(b"data: [DONE]\n\n")
                    except Exception:
                        pass
                await response.write_eof()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
                logger.debug("Client disconnected: %s", e)
            except Exception as e:
                if "closing transport" in str(e).lower() or "connection" in str(e).lower():
                    logger.debug("Connection lost: %s", e)
                else:
                    logger.error("Unexpected stream error: %s", e)
            return response
        else:
            try:
                result = await self.client.create_chat_completion(payload, bearer_token=bearer)
                return web.json_response(result)
            except Exception as e:
                status_code = self._status_for_error(e)
                if status_code == 401:
                    return web.json_response({"error": {"message": "unauthorized", "type": "auth_error"}}, status=401)
                logger.error("Chat completion error: %s", e)
                return web.json_response(
                    {"error": {"message": str(e), "type": "api_error", "code": status_code}},
                    status=status_code,
                )

    async def handle_anthropic_messages(self, request: web.Request) -> web.StreamResponse:
        """Handle Anthropic /v1/messages requests from Claude Code CLI."""
        if not self._check_auth(request):
            return self._anthropic_error("unauthorized", 401)
        bearer = self.api_key

        try:
            anthropic_payload = await request.json()
        except Exception as e:
            return self._anthropic_error(f"Invalid JSON payload: {e}", 400)

        validation_error = self._validate_payload(anthropic_payload)
        if validation_error:
            return self._anthropic_error(validation_error, 400)

        model_name = anthropic_payload.get("model", "claude-3-7-sonnet-20250219")
        openai_payload = translate_anthropic_payload_to_openai(anthropic_payload)
        is_stream = bool(anthropic_payload.get("stream", False))

        if is_stream:
            stream_gen = self.client.stream_chat_completion(openai_payload, bearer_token=bearer)
            anthropic_sse_gen = stream_anthropic_sse_adapter(stream_gen, model_name=model_name)
            
            try:
                first_event = await anthropic_sse_gen.__anext__()
            except Exception as e:
                logger.error("Anthropic stream connect error: %s", e)
                return self._anthropic_error(str(e), self._status_for_error(e))

            response = web.StreamResponse(
                status=200, reason="OK",
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
            try:
                await response.prepare(request)
                await response.write(first_event.encode("utf-8"))
                try:
                    async for event_str in anthropic_sse_gen:
                        await response.write(event_str.encode("utf-8"))
                except Exception as e:
                    logger.error("Anthropic SSE stream error: %s", e)
                    try:
                        err_event = {"type": "error", "error": {"type": "api_error", "message": str(e)}}
                        await response.write(f"event: error\ndata: {json.dumps(err_event)}\n\n".encode("utf-8"))
                    except Exception:
                        pass
                await response.write_eof()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
                logger.debug("Anthropic client disconnected: %s", e)
            except Exception as e:
                logger.debug("Anthropic stream ended: %s", e)
            return response
        else:
            try:
                openai_result = await self.client.create_chat_completion(openai_payload, bearer_token=bearer)
                anthropic_resp = translate_openai_response_to_anthropic(openai_result, model_name=model_name)
                return web.json_response(anthropic_resp)
            except Exception as e:
                logger.error("Anthropic messages error: %s", e)
                return self._anthropic_error(str(e), self._status_for_error(e))


def run_server(host: str = DEFAULT_BRIDGE_HOST, port: int = DEFAULT_BRIDGE_PORT,
               api_key: str = "", home: Optional[str] = None) -> None:
    server = AntigravityBridgeServer(host=host, port=port, api_key=api_key, home=Path(home) if home else None)
    web.run_app(server.app, host=host, port=port)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Isolated Antigravity Local Bridge")
    p.add_argument("--host", default=DEFAULT_BRIDGE_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_BRIDGE_PORT)
    p.add_argument("--api-key", default="", help="Bearer key (prefer --api-key-env)")
    p.add_argument("--api-key-env", default="ANTIGRAVITY_BRIDGE_API_KEY")
    p.add_argument("--home", required=True, help="Instance HOME dir (per-account home)")
    args = p.parse_args()

    api_key = args.api_key or os.environ.get(args.api_key_env, "")
    if not api_key:
        p.error("provide --api-key or a non-empty --api-key-env")
    run_server(host=args.host, port=args.port, api_key=api_key, home=args.home)


if __name__ == "__main__":
    main()
