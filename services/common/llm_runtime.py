"""Shared LLM and tools runtime helpers."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx


LogCallback = Callable[[str, str, dict[str, Any] | None], None]
ToolFilter = Callable[[dict[str, Any], "InvocationContext"], bool]


@dataclass(slots=True)
class InvocationContext:
    """Describe where one inference request came from."""

    source: str
    session_id: str | None = None
    call_id: str | None = None
    language: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatRuntimeConfig:
    """Configure one OpenAI-compatible streamed chat run."""

    base_url: str
    model: str
    timeout_sec: float = 60.0
    request_options: dict[str, Any] = field(default_factory=dict)
    fallback_base_url: str = ""
    fallback_model: str = ""
    max_rounds: int = 4


@dataclass(slots=True)
class ChatRunResult:
    """Describe one completed chat run."""

    text: str
    available_tools: list[str]
    model_used: str
    fallback_error: str | None = None


def tools_enabled() -> bool:
    """Check if global tools support is enabled. Output: bool. Input: none."""
    return os.getenv("TOOLS_ENABLED", "false").strip().lower() in {"true", "1", "yes"}


def tools_base_url() -> str:
    """Return tools service base URL. Output: URL string. Input: none."""
    return os.getenv("TOOLS_BASE_URL", "http://tools:8003").rstrip("/")


def truncate_for_log(value: Any, limit: int = 500) -> Any:
    """Recursively trim large values for logs. Output: compact value. Input: any value and limit."""
    if isinstance(value, str):
        return value if len(value) <= limit else f"{value[:limit]}..."
    if isinstance(value, list):
        return [truncate_for_log(item, limit) for item in value[:10]]
    if isinstance(value, dict):
        return {key: truncate_for_log(item, limit) for key, item in list(value.items())[:20]}
    return value


def _tool_param_to_json_schema(param: dict[str, Any]) -> dict[str, Any]:
    """Convert internal param definition to JSON schema field. Output: schema dict. Input: tool param dict."""
    schema: dict[str, Any] = {"type": param.get("type", "string")}
    if param.get("description"):
        schema["description"] = param["description"]
    if "default" in param:
        schema["default"] = param["default"]
    return schema


def _prepare_tool_params_for_context(tool: dict[str, Any], context: InvocationContext) -> tuple[dict[str, Any], list[str]]:
    """Adjust tool params exposed to the model for one invocation context. Output: params and required names. Input: tool metadata and context."""
    params = dict(tool.get("params", {}))
    required = [name for name, spec in params.items() if spec.get("required")]

    if context.source == "sip" and context.call_id and tool.get("name") in {"sip_hangup", "sip_transfer"}:
        params.pop("channel_id", None)
        required = [name for name in required if name != "channel_id"]

    return params, required


def tool_to_openai_schema(tool: dict[str, Any], context: InvocationContext) -> dict[str, Any]:
    """Convert tool metadata into OpenAI tool schema. Output: tool schema dict. Input: tool metadata."""
    params, required = _prepare_tool_params_for_context(tool, context)
    properties = {name: _tool_param_to_json_schema(spec) for name, spec in params.items()}
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


async def fetch_enabled_tools(
    context: InvocationContext,
    log_event: LogCallback,
    tool_filter: ToolFilter | None = None,
) -> list[dict[str, Any]]:
    """Fetch enabled tools from tools service. Output: OpenAI tool schema list. Input: invocation context, logger, optional filter."""
    if not tools_enabled():
        return []

    timeout = httpx.Timeout(10.0, connect=2.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{tools_base_url()}/tools")
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        log_event("tools_unavailable", "Failed to fetch tools list.", {"error": str(exc)})
        return []

    enabled_tools: list[dict[str, Any]] = []
    for tool in payload.get("tools", []):
        enabled_value = tool.get("enabled", False)
        if isinstance(enabled_value, bool):
            is_enabled = enabled_value
        else:
            is_enabled = str(enabled_value).strip().lower() in {"true", "1", "yes"}
        if not is_enabled:
            continue
        if tool_filter is not None and not tool_filter(tool, context):
            continue
        enabled_tools.append(tool)
    return [tool_to_openai_schema(tool, context) for tool in enabled_tools]


def _apply_context_tool_arguments(
    context: InvocationContext,
    tool_name: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fill tool arguments from invocation context when the model should not provide them. Output: updated payload and injected fields. Input: context, tool name, payload."""
    updated_payload = dict(payload)
    injected: dict[str, Any] = {}

    if context.source == "sip" and context.call_id and tool_name in {"sip_hangup", "sip_transfer"}:
        if not str(updated_payload.get("channel_id", "")).strip():
            updated_payload["channel_id"] = context.call_id
            injected["channel_id"] = context.call_id

    return updated_payload, injected


async def invoke_tool(
    context: InvocationContext,
    log_event: LogCallback,
    tool_name: str,
    arguments_json: str,
) -> dict[str, Any]:
    """Invoke one tool through the tools service. Output: result dict. Input: invocation context, logger, tool name and JSON args."""
    parse_error = ""
    try:
        payload = json.loads(arguments_json) if arguments_json else {}
    except ValueError:
        payload = {}
        parse_error = "Invalid JSON arguments from LLM; fallback to empty payload."

    payload, injected_arguments = _apply_context_tool_arguments(context, tool_name, payload)

    started_at = time.monotonic()
    log_event(
        "tool_call",
        "Executing tool requested by LLM.",
        {
            "tool": tool_name,
            "arguments": truncate_for_log(payload),
            "arguments_json_preview": truncate_for_log(arguments_json),
            "parse_error": parse_error,
            "injected_arguments": truncate_for_log(injected_arguments),
        },
    )

    timeout = httpx.Timeout(30.0, connect=5.0)
    status = "ok"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{tools_base_url()}/invoke", json={"tool": tool_name, "payload": payload})
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPStatusError as exc:
        status = "http_error"
        result = {
            "tool": tool_name,
            "error": f"HTTP {exc.response.status_code} from tools service",
            "detail": (exc.response.text or "")[:1000],
        }
    except Exception as exc:  # noqa: BLE001
        status = "exception"
        result = {"tool": tool_name, "error": str(exc)}

    log_event(
        "tool_result",
        "Tool execution finished.",
        {
            "tool": tool_name,
            "status": status,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "result": truncate_for_log(result),
        },
    )
    return result


def build_assistant_tool_message(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Build assistant message with tool calls for follow-up round. Output: message dict. Input: tool call list."""
    return {"role": "assistant", "content": "", "tool_calls": tool_calls}


async def stream_chat_completion(
    client: httpx.AsyncClient,
    url: str,
    request_body: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Read one streaming chat completion. Output: response text and tool call list. Input: HTTP client, URL, request body."""
    content_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}

    async with client.stream("POST", url, json=request_body) as response:
        response.raise_for_status()
        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})
            token = delta.get("content", "")
            if token:
                content_parts.append(token)

            for call_delta in delta.get("tool_calls", []):
                index = int(call_delta.get("index", 0))
                existing = tool_calls.setdefault(
                    index,
                    {
                        "id": call_delta.get("id", ""),
                        "type": call_delta.get("type", "function"),
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if call_delta.get("id"):
                    existing["id"] = call_delta["id"]
                function_delta = call_delta.get("function", {})
                if function_delta.get("name"):
                    existing["function"]["name"] = function_delta["name"]
                if function_delta.get("arguments"):
                    existing["function"]["arguments"] += function_delta["arguments"]

    ordered_tool_calls = [tool_calls[idx] for idx in sorted(tool_calls)]
    return "".join(content_parts), ordered_tool_calls


async def _run_chat_rounds(
    client: httpx.AsyncClient,
    url: str,
    request_body: dict[str, Any],
    messages: list[dict[str, Any]],
    config: ChatRuntimeConfig,
    context: InvocationContext,
    log_event: LogCallback,
) -> str:
    """Execute tool-call rounds for one configured model. Output: final response text. Input: client, URL, request body, messages, config, context, logger."""
    current_messages: list[dict[str, Any]] = list(messages)
    for round_index in range(config.max_rounds):
        round_number = round_index + 1
        log_event(
            "chat_round_start",
            "LLM chat round started.",
            {"round": round_number, "messages": len(current_messages)},
        )
        current_request_body = {**request_body, "messages": current_messages}
        round_timeout_sec = max(float(config.timeout_sec) + 15.0, 30.0)
        try:
            iteration_response, tool_calls = await asyncio.wait_for(
                stream_chat_completion(client, url, current_request_body),
                timeout=round_timeout_sec,
            )
        except TimeoutError as exc:
            log_event(
                "chat_round_timeout",
                "LLM chat round timed out before stream completion.",
                {
                    "round": round_number,
                    "timeout_sec": round_timeout_sec,
                    "messages": len(current_messages),
                },
            )
            raise RuntimeError("LLM stream timed out before completion.") from exc

        log_event(
            "chat_round_done",
            "LLM chat round finished streaming.",
            {
                "round": round_number,
                "response_chars": len(iteration_response),
                "tool_calls_count": len(tool_calls),
            },
        )

        if tool_calls:
            log_event(
                "tool_calls_detected",
                "LLM returned tool calls.",
                {
                    "count": len(tool_calls),
                    "tools": [
                        call.get("function", {}).get("name", "")
                        for call in tool_calls
                    ],
                },
            )
            current_messages.append(build_assistant_tool_message(tool_calls))
            for tool_call in tool_calls:
                function = tool_call.get("function", {})
                tool_name = function.get("name", "")
                if not tool_name:
                    log_event(
                        "tool_call_invalid",
                        "Skipped invalid tool call without function name.",
                        {"tool_call": truncate_for_log(tool_call)},
                    )
                    continue
                tool_result = await invoke_tool(context, log_event, tool_name, function.get("arguments", ""))
                current_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id", ""),
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                )
            continue
        return iteration_response

    log_event(
        "chat_round_limit",
        "LLM exceeded configured tool-call round limit.",
        {"max_rounds": config.max_rounds},
    )
    raise RuntimeError("LLM exceeded tool-call round limit.")


async def run_chat_with_tools(
    messages: list[dict[str, Any]],
    config: ChatRuntimeConfig,
    context: InvocationContext,
    log_event: LogCallback,
    tool_filter: ToolFilter | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> ChatRunResult:
    """Run one LLM conversation with optional tools and fallback. Output: final text, available tool names, model used. Input: messages, config, context, logger, optional tool filter."""
    base_url = config.base_url.rstrip("/")
    if not base_url or not config.model:
        raise RuntimeError("LLM provider is not configured.")

    if tools is None:
        tools = await fetch_enabled_tools(context, log_event, tool_filter=tool_filter)
    tool_names = [tool.get("function", {}).get("name", "") for tool in tools if tool.get("function", {}).get("name")]
    request_body: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "stream": True,
        **config.request_options,
    }
    if tools:
        request_body["tools"] = tools
        request_body["tool_choice"] = "auto"

    timeout = httpx.Timeout(config.timeout_sec, connect=5.0)
    endpoint = f"{base_url}/v1/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response_text = await _run_chat_rounds(client, endpoint, request_body, messages, config, context, log_event)
            return ChatRunResult(text=response_text, available_tools=tool_names, model_used=config.model)
    except Exception as exc:  # noqa: BLE001
        fallback_base_url = config.fallback_base_url.rstrip("/")
        if not fallback_base_url or not config.fallback_model or fallback_base_url == base_url:
            raise exc

        log_event(
            "fallback",
            "Primary LLM failed; switching to fallback provider.",
            {"error": str(exc), "fallback_model": config.fallback_model},
        )
        fallback_request_body = {**request_body, "model": config.fallback_model}
        fallback_endpoint = f"{fallback_base_url}/v1/chat/completions"
        async with httpx.AsyncClient(timeout=timeout) as client:
            response_text = await _run_chat_rounds(
                client,
                fallback_endpoint,
                fallback_request_body,
                messages,
                config,
                context,
                log_event,
            )
            return ChatRunResult(
                text=response_text,
                available_tools=tool_names,
                model_used=config.fallback_model,
                fallback_error=str(exc),
            )


def load_prompt_by_source(source: str, default_text: str = "") -> str:
    """Load prompt file path by source, read and return text. Output: prompt text. Input: source, default text."""
    from pathlib import Path

    # Determine env var based on source
    env_var = f"{source.upper()}_ROLE" if source else "WEBUI_ROLE"
    file_path = os.getenv(env_var, "").strip()

    if not file_path:
        return default_text

    try:
        path = Path(file_path)
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        pass

    return default_text


def load_greeting_by_source(source: str, default_text: str = "") -> tuple[str, bytes | None]:
    """Load greeting (text or WAV) by source. Output: (text, wav_bytes) tuple. Input: source, default text."""
    from pathlib import Path

    # Determine env var based on source
    env_var = f"{source.upper()}_GREETINGS" if source else "WEBUI_GREETINGS"
    file_path = os.getenv(env_var, "").strip()

    if not file_path:
        return default_text, None

    try:
        path = Path(file_path)
        if not path.exists():
            return default_text, None

        if path.suffix.lower() == ".wav":
            return "", path.read_bytes()
        else:
            text = path.read_text(encoding="utf-8").strip()
            return text, None
    except Exception:
        pass

    return default_text, None
