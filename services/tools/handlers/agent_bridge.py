"""Agent bridge handler: call an external agent via OpenAI-compatible protocol."""

import os
from typing import Any

import httpx

_TIMEOUT = 60.0


async def call_agent(
    messages: list[dict[str, str]],
    model: str | None = None,
    url: str | None = None,
    api_key: str | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    """Call external agent via OpenAI-compatible /v1/chat/completions. Output: response dict. Input: messages and config."""
    if not os.getenv("TOOLS_AGENT_BRIDGE_ENABLED", "false").lower() in ("true", "1", "yes"):
        return {"error": "agent_bridge is disabled (TOOLS_AGENT_BRIDGE_ENABLED=false)"}

    base_url = (url or os.getenv("TOOLS_AGENT_BRIDGE_URL", "")).rstrip("/")
    if not base_url:
        return {"error": "TOOLS_AGENT_BRIDGE_URL is not configured"}

    effective_model = model or os.getenv("TOOLS_AGENT_BRIDGE_MODEL", "").strip()
    effective_key = api_key or os.getenv("TOOLS_AGENT_BRIDGE_API_KEY", "").strip()

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if effective_key:
        headers["Authorization"] = f"Bearer {effective_key}"

    body: dict[str, Any] = {"messages": messages, "stream": False}
    if effective_model:
        body["model"] = effective_model

    endpoint = f"{base_url}/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(endpoint, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"Agent API error {exc.response.status_code}", "url": endpoint}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "url": endpoint}

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        content = None

    return {
        "provider": base_url,
        "model": data.get("model", effective_model),
        "content": content,
        "usage": data.get("usage"),
        "finish_reason": data.get("choices", [{}])[0].get("finish_reason"),
    }
