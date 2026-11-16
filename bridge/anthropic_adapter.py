"""Anthropic Messages API to OpenAI / Gemini Code Assist adapter."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def map_anthropic_model(requested: Any) -> str:
    """Map an Anthropic-side model name to a bridge model ID.

    The requested model must be honoured: previously this adapter ignored
    it and hardcoded gemini-3.8-flash, so a client asking for Claude was
    silently served by Gemini while the response claimed to be Claude.

    Claude-family names (any version/date suffix) map to the bridge's
    Claude entries; anything else (Gemini/GPT-OSS bridge IDs, aliases) is
    passed through for the bridge's own alias resolution.
    """
    name = str(requested or "").strip()
    if not name:
        return "gemini-3.8-flash"
    low = name.lower()
    if low.startswith("claude") or "claude" in low:
        if "opus" in low:
            return "claude-opus-4-6"
        if "haiku" in low:
            logger.warning("No Haiku model upstream; serving %s with claude-sonnet-4-6", name)
            return "claude-sonnet-4-6"
        return "claude-sonnet-4-6"
    return name


def _anthropic_stop_reason(openai_finish: Any, has_tool_calls: bool) -> str:
    """Map an OpenAI finish_reason to the Anthropic stop_reason vocabulary.

    A truncated answer is "max_tokens", not "end_turn" (the old mapping
    claimed every non-tool answer ended naturally).
    """
    if has_tool_calls:
        return "tool_use"
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "refusal",
        "tool_calls": "tool_use",
    }.get(str(openai_finish or "stop"), "end_turn")


def translate_anthropic_payload_to_openai(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an Anthropic /v1/messages payload to OpenAI chat completions format."""
    messages: List[Dict[str, Any]] = []

    # 1. System prompt
    system = payload.get("system")
    if system:
        if isinstance(system, list):
            sys_text = "\n".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text")
        else:
            sys_text = str(system)
        if sys_text.strip():
            messages.append({"role": "system", "content": sys_text.strip()})

    # 2. Conversation messages
    for msg in payload.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            text_parts = []
            image_parts = []
            tool_calls = []
            tool_results = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                b_type = block.get("type")
                if b_type == "text":
                    text_parts.append(block.get("text", ""))
                elif b_type == "image":
                    # Anthropic image block -> OpenAI image_url part.
                    # (Images used to be silently dropped entirely.)
                    source = block.get("source") or {}
                    if source.get("type") == "base64" and source.get("data"):
                        media = source.get("media_type") or "image/png"
                        image_parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{media};base64,{source['data']}"},
                        })
                    elif source.get("type") == "url" and source.get("url"):
                        image_parts.append({
                            "type": "image_url",
                            "image_url": {"url": source["url"]},
                        })
                elif b_type == "tool_use":
                    tool_id = block.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    tool_calls.append({
                        "id": tool_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(tool_input) if isinstance(tool_input, (dict, list)) else str(tool_input)
                        }
                    })
                elif b_type == "tool_result":
                    tool_id = block.get("tool_use_id", "")
                    t_content = block.get("content", "")
                    if isinstance(t_content, list):
                        t_content = "\n".join(b.get("text", "") for b in t_content if isinstance(b, dict) and b.get("type") == "text")
                    t_content = str(t_content)
                    if block.get("is_error"):
                        # OpenAI tool messages have no is_error flag;
                        # keep the failure visible to the model as text.
                        t_content = f"[tool returned an error] {t_content}"
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": t_content
                    })

            if role == "assistant":
                asst_msg: Dict[str, Any] = {"role": "assistant"}
                if text_parts:
                    asst_msg["content"] = "\n".join(text_parts)
                if tool_calls:
                    asst_msg["tool_calls"] = tool_calls
                if text_parts or tool_calls:
                    messages.append(asst_msg)
            elif role == "user":
                if tool_results:
                    for tr in tool_results:
                        messages.append(tr)
                if image_parts:
                    parts: List[Dict[str, Any]] = []
                    if text_parts:
                        parts.append({"type": "text", "text": "\n".join(text_parts)})
                    parts.extend(image_parts)
                    messages.append({"role": "user", "content": parts})
                elif text_parts:
                    messages.append({"role": "user", "content": "\n".join(text_parts)})

    # 3. Tools translation
    openai_tools: List[Dict[str, Any]] = []
    for t in payload.get("tools", []):
        if not isinstance(t, dict):
            continue
        t_name = t.get("name")
        if not t_name:
            continue
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t_name,
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {})
            }
        })

    openai_payload: Dict[str, Any] = {
        "model": map_anthropic_model(payload.get("model")),
        "messages": messages,
        "stream": bool(payload.get("stream", False)),
    }

    if openai_tools:
        openai_payload["tools"] = openai_tools

    if "max_tokens" in payload:
        openai_payload["max_tokens"] = payload["max_tokens"]
    if "temperature" in payload:
        openai_payload["temperature"] = payload["temperature"]
    if "top_p" in payload:
        openai_payload["top_p"] = payload["top_p"]
    if payload.get("stop_sequences"):
        openai_payload["stop"] = payload["stop_sequences"]

    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type")
        if tc_type == "auto":
            openai_payload["tool_choice"] = "auto"
        elif tc_type == "any":
            openai_payload["tool_choice"] = "required"
        elif tc_type == "none":
            openai_payload["tool_choice"] = "none"
        elif tc_type == "tool" and tool_choice.get("name"):
            openai_payload["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_choice["name"]},
            }
    elif isinstance(tool_choice, str):
        openai_payload["tool_choice"] = tool_choice

    return openai_payload


def translate_openai_response_to_anthropic(openai_resp: Dict[str, Any], model_name: str = "claude-3-7-sonnet-20250219") -> Dict[str, Any]:
    """Convert an OpenAI chat completion object to Anthropic /v1/messages response format."""
    msg_id = f"msg_{uuid.uuid4().hex[:16]}"
    choices = openai_resp.get("choices", [])
    first_choice = choices[0] if choices else {}
    msg = first_choice.get("message", {})

    content_blocks: List[Dict[str, Any]] = []

    reasoning = msg.get("reasoning_content")
    if reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning})

    text = msg.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    tool_calls = msg.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function", {})
        try:
            parsed_input = json.loads(fn.get("arguments", "{}"))
        except Exception:
            parsed_input = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
            "name": fn.get("name", ""),
            "input": parsed_input
        })

    stop_reason = _anthropic_stop_reason(first_choice.get("finish_reason"), bool(tool_calls))

    usage = openai_resp.get("usage", {})
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model_name,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 10),
            "output_tokens": usage.get("completion_tokens", 10)
        }
    }


async def stream_anthropic_sse_adapter(openai_stream_gen: AsyncIterator[str], model_name: str = "claude-3-7-sonnet-20250219") -> AsyncIterator[str]:
    """Translate OpenAI SSE chunks into Anthropic SSE event blocks."""
    msg_id = f"msg_{uuid.uuid4().hex[:16]}"
    
    # 1. message_start
    start_event = {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model_name,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1}
        }
    }
    yield f"event: message_start\ndata: {json.dumps(start_event)}\n\n"

    # Block tracking
    current_block_type: Optional[str] = None
    block_index = 0
    accumulated_tool_calls: Dict[int, Dict[str, Any]] = {}
    total_output_tokens = 0
    last_finish_reason: Any = None
    stream_usage: Dict[str, Any] = {}

    async for chunk_str in openai_stream_gen:
        lines = chunk_str.strip().split("\n")
        for line in lines:
            line = line.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            json_str = line[6:].strip()
            try:
                data = json.loads(json_str)
            except Exception:
                continue

            if isinstance(data.get("usage"), dict):
                stream_usage = data["usage"]
            choices = data.get("choices") or []
            if not choices:
                continue
            if choices[0].get("finish_reason"):
                last_finish_reason = choices[0]["finish_reason"]
            delta = choices[0].get("delta") or {}

            # Handle reasoning content -> Anthropic thinking block
            reasoning_piece = delta.get("reasoning_content")
            if reasoning_piece:
                total_output_tokens += 1
                if current_block_type != "thinking":
                    if current_block_type is not None:
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"
                        block_index += 1
                    current_block_type = "thinking"
                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'thinking', 'thinking': ''}})}\n\n"
                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'thinking_delta', 'thinking': reasoning_piece}})}\n\n"

            # Handle text content
            content_piece = delta.get("content")
            if content_piece:
                total_output_tokens += 1
                if current_block_type != "text":
                    if current_block_type is not None:
                        # Close previous block
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"
                        block_index += 1
                    current_block_type = "text"
                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'text', 'text': ''}})}\n\n"

                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'text_delta', 'text': content_piece}})}\n\n"

            # Handle tool call
            t_calls = delta.get("tool_calls")
            if t_calls:
                for tc in t_calls:
                    tc_idx = tc.get("index", 0)
                    fn = tc.get("function") or {}
                    fn_name = fn.get("name")
                    fn_args = fn.get("arguments", "")

                    if tc_idx not in accumulated_tool_calls:
                        if current_block_type is not None:
                            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"
                            block_index += 1
                        current_block_type = "tool_use"
                        t_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"
                        accumulated_tool_calls[tc_idx] = {"id": t_id, "name": fn_name or ""}
                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'tool_use', 'id': t_id, 'name': fn_name or ''}})}\n\n"

                    if fn_args:
                        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'input_json_delta', 'partial_json': fn_args}})}\n\n"

    # Close last block if open
    if current_block_type is not None:
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"

    # message_delta and message_stop: real stop reason and real usage
    # when the upstream stream reported them; chunk count is only a
    # fallback estimate, never a fabricated constant presented as fact.
    stop_reason = _anthropic_stop_reason(last_finish_reason, bool(accumulated_tool_calls))
    output_tokens = stream_usage.get("completion_tokens")
    if not isinstance(output_tokens, int) or output_tokens <= 0:
        output_tokens = max(total_output_tokens, 1)
    delta_event = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens}
    }
    yield f"event: message_delta\ndata: {json.dumps(delta_event)}\n\n"
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
