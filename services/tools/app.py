import os
import resource
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field


app = FastAPI(title="colloc-tools")
REQUESTS_TOTAL = 0
REQUESTS_BY_PATH: dict[str, int] = defaultdict(int)


class ToolInvokeRequest(BaseModel):
    tool: str = Field(description="Tool identifier")
    payload: dict[str, object] = Field(default_factory=dict, description="Tool payload")


def get_enabled_tools() -> list[dict[str, object]]:
    """List enabled tools. Output: tool list. Input: none."""
    mapping = {
        "web_search": os.getenv("TOOLS_WEB_SEARCH_ENABLED", "false"),
        "weather": os.getenv("TOOLS_WEATHER_ENABLED", "false"),
        "url_fetch": os.getenv("TOOLS_URL_FETCH_ENABLED", "false"),
        "agent_bridge": os.getenv("TOOLS_AGENT_BRIDGE_ENABLED", "false"),
        "sip": os.getenv("TOOLS_SIP_ENABLED", "false"),
    }
    return [
        {"name": name, "enabled": value.lower() == "true"}
        for name, value in mapping.items()
    ]


def get_memory_usage_mb() -> float:
    """Get process memory usage. Output: memory in MB. Input: none."""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2)


@app.middleware("http")
async def count_requests(request: Request, call_next):
    """Track HTTP request counters. Output: response. Input: request and next handler."""
    global REQUESTS_TOTAL
    REQUESTS_TOTAL += 1
    REQUESTS_BY_PATH[request.url.path] += 1
    return await call_next(request)


@app.get("/health")
def healthcheck() -> dict[str, str]:
    """Return tools health. Output: health dict. Input: none."""
    return {"status": "ok", "service": "tools"}


@app.get("/metrics")
def metrics() -> dict[str, object]:
    """Return service metrics. Output: metrics dict. Input: none."""
    return {
        "service": "tools",
        "health": "ok",
        "requests_total": REQUESTS_TOTAL,
        "requests_by_path": dict(REQUESTS_BY_PATH),
        "memory_mb": get_memory_usage_mb(),
        "enabled_tools": get_enabled_tools(),
    }


@app.get("/tools")
def list_tools() -> dict[str, object]:
    """Return enabled tools. Output: tool summary dict. Input: none."""
    return {"tools": get_enabled_tools()}


@app.post("/invoke")
def invoke_tool(request: ToolInvokeRequest) -> dict[str, object]:
    """Invoke placeholder tool. Output: invocation result dict. Input: tool request."""
    enabled = {item["name"]: item["enabled"] for item in get_enabled_tools()}
    if not enabled.get(request.tool, False):
        raise HTTPException(status_code=400, detail=f"Tool '{request.tool}' is disabled.")

    return {
        "tool": request.tool,
        "accepted": True,
        "payload": request.payload,
    }
