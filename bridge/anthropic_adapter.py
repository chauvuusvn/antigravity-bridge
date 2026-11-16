"""Anthropic Messages API to OpenAI / Gemini Code Assist adapter for Hagi Bridge."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


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
            tool_calls = []
            tool_results = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                b_type = block.get("type")
                if b_type == "text":
                    text_parts.append(block.get("text", ""))
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
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": str(t_content)
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
                if text_parts:
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
        "model": "gemini-3.8-flash",
        "messages": messages,
        "stream": bool(payload.get("stream", False)),
    }

    if openai_tools:
        openai_payload["tools"] = openai_tools

    if "max_tokens" in payload:
        openai_payload["max_tokens"] = payload["max_tokens"]
    if "temperature" in payload:
        openai_payload["temperature"] = payload["temperature"]

    return openai_payload


def translate_openai_response_to_anthropic(openai_resp: Dict[str, Any], model_name: str = "claude-3-7-sonnet-20250219") -> Dict[str, Any]:
    """Convert an OpenAI chat completion object to Anthropic /v1/messages response format."""
    msg_id = f"msg_{uuid.uuid4().hex[:16]}"
    choices = openai_resp.get("choices", [])
    first_choice = choices[0] if choices else {}
    msg = first_choice.get("message", {})

    content_blocks: List[Dict[str, Any]] = []

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

    stop_reason = "tool_use" if tool_calls else ("end_turn" if first_choice.get("finish_reason") == "stop" else "end_turn")

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

            choices = data.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

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

    # message_delta and message_stop
    stop_reason = "tool_use" if accumulated_tool_calls else "end_turn"
    delta_event = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": max(total_output_tokens, 10)}
    }
    yield f"event: message_delta\ndata: {json.dumps(delta_event)}\n\n"
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
