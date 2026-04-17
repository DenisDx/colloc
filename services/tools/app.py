"""Tools service: tool registry and invocation API."""

import os
import resource
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from services.tools.handlers import web_search as _web_search
from services.tools.handlers import fetch as _fetch
from services.tools.handlers import get_image as _get_image
from services.tools.handlers import weather as _weather
from services.tools.handlers import agent_bridge as _agent_bridge
from services.tools.handlers import sip_tools as _sip_tools

app = FastAPI(title="colloc-tools")
REQUESTS_TOTAL = 0
REQUESTS_BY_PATH: dict[str, int] = defaultdict(int)

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS = [
    {
        "name": "web_search",
        "description": "Search the web. Returns titles, URLs and snippets.",
        "enabled_env": ["TOOLS_WEBSEARCH_ENABLED", "TOOLS_WEB_SEARCH_ENABLED"],
        "params": {
            "query": {"type": "string", "required": True, "description": "Search query"},
            "num_results": {"type": "integer", "required": False, "default": 5, "description": "Max results"},
        },
    },
    {
        "name": "url_fetch",
        "description": "Fetch the content of a URL and return plain text.",
        "enabled_env": ["TOOLS_FETCH_ENABLED"],
        "params": {
            "url": {"type": "string", "required": True, "description": "URL to fetch"},
            "extract_text": {"type": "boolean", "required": False, "default": True, "description": "Strip HTML tags"},
        },
    },
    {
        "name": "get_image",
        "description": "Search for images. Returns image URLs.",
        "enabled_env": ["TOOLS_GET_IMAGE_ENABLED"],
        "params": {
            "query": {"type": "string", "required": True, "description": "Image search query"},
            "num_results": {"type": "integer", "required": False, "default": 5, "description": "Max results"},
        },
    },
    {
        "name": "weather",
        "description": "Get current weather for a location.",
        "enabled_env": ["TOOLS_WEATHER_ENABLED"],
        "params": {
            "location": {"type": "string", "required": True, "description": "City name or coordinates"},
        },
    },
    {
        "name": "agent_bridge",
        "description": "Send a message to an external OpenAI-compatible agent and return its response.",
        "enabled_env": ["TOOLS_AGENT_BRIDGE_ENABLED"],
        "params": {
            "messages": {"type": "array", "required": True, "description": "OpenAI-style messages list"},
            "model": {"type": "string", "required": False, "description": "Model override"},
            "url": {"type": "string", "required": False, "description": "Agent base URL override"},
        },
    },
    {
        "name": "sip_hangup",
        "description": "Hang up an active SIP call.",
        "enabled_env": ["TOOLS_SIP_ENABLED"],
        "params": {
            "channel_id": {"type": "string", "required": True, "description": "Asterisk channel ID"},
        },
    },
    {
        "name": "sip_transfer",
        "description": "Blind transfer an active SIP call to another extension.",
        "enabled_env": ["TOOLS_SIP_ENABLED"],
        "params": {
            "channel_id": {"type": "string", "required": True, "description": "Asterisk channel ID"},
            "target": {"type": "string", "required": True, "description": "Target extension or number"},
        },
    },
]


def _is_enabled(env_keys: list[str]) -> bool:
    """Check if a tool is enabled via any of the given env keys. Output: bool. Input: env key list."""
    for key in env_keys:
        if os.getenv(key, "").lower() in ("true", "1", "yes"):
            return True
    return False


def get_tool_list() -> list[dict[str, Any]]:
    """Return all tools with enabled status. Output: tool list. Input: none."""
    return [
        {
            "name": td["name"],
            "description": td["description"],
            "enabled": _is_enabled(td["enabled_env"]),
            "params": td["params"],
        }
        for td in _TOOL_DEFINITIONS
    ]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ToolInvokeRequest(BaseModel):
    """Tool invocation request."""
    tool: str = Field(description="Tool identifier")
    payload: dict[str, Any] = Field(default_factory=dict, description="Tool payload")


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def count_requests(request: Request, call_next):
    """Track HTTP request counters. Output: response. Input: request and next handler."""
    global REQUESTS_TOTAL
    REQUESTS_TOTAL += 1
    REQUESTS_BY_PATH[request.url.path] += 1
    return await call_next(request)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def healthcheck() -> dict[str, str]:
    """Return tools health. Output: health dict. Input: none."""
    return {"status": "ok", "service": "tools"}


@app.get("/metrics")
def metrics() -> dict[str, Any]:
    """Return service metrics. Output: metrics dict. Input: none."""
    return {
        "service": "tools",
        "health": "ok",
        "requests_total": REQUESTS_TOTAL,
        "requests_by_path": dict(REQUESTS_BY_PATH),
        "memory_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2),
        "enabled_tools": [t for t in get_tool_list() if t["enabled"]],
    }


@app.get("/tools")
def list_tools() -> dict[str, Any]:
    """Return all tools with enabled status. Output: tool summary dict. Input: none."""
    return {"tools": get_tool_list()}


@app.post("/invoke")
async def invoke_tool(request: ToolInvokeRequest) -> dict[str, Any]:
    """Invoke a tool by name. Output: invocation result dict. Input: tool name and payload."""
    tool_map = {t["name"]: t for t in _TOOL_DEFINITIONS}
    td = tool_map.get(request.tool)
    if not td:
        raise HTTPException(status_code=404, detail=f"Unknown tool '{request.tool}'.")
    if not _is_enabled(td["enabled_env"]):
        raise HTTPException(status_code=400, detail=f"Tool '{request.tool}' is disabled.")

    p = request.payload

    if request.tool == "web_search":
        result = await _web_search.search(
            query=p.get("query", ""),
            num_results=int(p.get("num_results", 5)),
        )
    elif request.tool == "url_fetch":
        result = await _fetch.fetch(
            url=p.get("url", ""),
            extract_text=bool(p.get("extract_text", True)),
        )
    elif request.tool == "get_image":
        result = await _get_image.search_image(
            query=p.get("query", ""),
            num_results=int(p.get("num_results", 5)),
        )
    elif request.tool == "weather":
        result = await _weather.get_weather(location=p.get("location", ""))
    elif request.tool == "agent_bridge":
        result = await _agent_bridge.call_agent(
            messages=p.get("messages", []),
            model=p.get("model"),
            url=p.get("url"),
            api_key=p.get("api_key"),
        )
    elif request.tool == "sip_hangup":
        result = await _sip_tools.hangup(channel_id=p.get("channel_id", ""))
    elif request.tool == "sip_transfer":
        result = await _sip_tools.transfer(
            channel_id=p.get("channel_id", ""),
            target=p.get("target", ""),
        )
    else:
        raise HTTPException(status_code=501, detail=f"Tool '{request.tool}' has no implementation.")

    return {"tool": request.tool, "result": result}
