"""SIP control tools: hangup and blind transfer via sip-service internal API."""

import os
from typing import Any

import httpx

_SIP_SERVICE_URL = "http://sip-service:8004"
_TIMEOUT = 10.0


def _sip_enabled() -> bool:
    """Check if SIP tools are enabled. Output: bool. Input: none."""
    return os.getenv("TOOLS_SIP_ENABLED", "false").lower() in ("true", "1", "yes")


async def hangup(channel_id: str) -> dict[str, Any]:
    """Hang up an active SIP call. Output: status dict. Input: channel_id."""
    if not _sip_enabled():
        return {"error": "sip tools are disabled (TOOLS_SIP_ENABLED=false)"}
    if not channel_id:
        return {"error": "channel_id is required"}

    url = f"{_SIP_SERVICE_URL}/sip/call/hangup"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json={"channel_id": channel_id})
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"sip-service error {exc.response.status_code}", "channel_id": channel_id}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "channel_id": channel_id}


async def transfer(channel_id: str, target: str) -> dict[str, Any]:
    """Blindly transfer an active SIP call to target extension/number. Output: status dict. Input: channel_id and target."""
    if not _sip_enabled():
        return {"error": "sip tools are disabled (TOOLS_SIP_ENABLED=false)"}
    if not channel_id:
        return {"error": "channel_id is required"}
    if not target:
        return {"error": "target is required"}

    url = f"{_SIP_SERVICE_URL}/sip/call/transfer"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json={"channel_id": channel_id, "target": target})
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"sip-service error {exc.response.status_code}", "channel_id": channel_id}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "channel_id": channel_id}
