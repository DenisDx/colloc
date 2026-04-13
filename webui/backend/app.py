import asyncio
import os
import resource
import time
from collections import defaultdict
from typing import Any

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect


app = FastAPI(title="colloc-webui-backend")
REQUESTS_TOTAL = 0
REQUESTS_BY_PATH: dict[str, int] = defaultdict(int)
WS_MESSAGES_TOTAL = 0


def get_memory_usage_mb() -> float:
    """Get process memory usage. Output: memory in MB. Input: none."""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2)


def get_host_memory_snapshot() -> dict[str, float]:
    """Get host memory snapshot. Output: memory dict. Input: none."""
    meminfo: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as handle:
        for line in handle:
            key, value = line.split(":", 1)
            meminfo[key] = int(value.strip().split()[0])

    total_kb = meminfo.get("MemTotal", 0)
    available_kb = meminfo.get("MemAvailable", 0)
    used_kb = max(total_kb - available_kb, 0)

    return {
        "total_mb": round(total_kb / 1024.0, 2),
        "used_mb": round(used_kb / 1024.0, 2),
        "available_mb": round(available_kb / 1024.0, 2),
        "used_percent": round((used_kb / total_kb) * 100, 2) if total_kb else 0.0,
    }


@app.middleware("http")
async def count_requests(request: Request, call_next):
    """Track HTTP request counters. Output: response. Input: request and next handler."""
    global REQUESTS_TOTAL
    REQUESTS_TOTAL += 1
    REQUESTS_BY_PATH[request.url.path] += 1
    return await call_next(request)


def build_runtime_snapshot() -> dict[str, object]:
    """Build runtime snapshot. Output: service state dict. Input: none."""
    return {
        "domain": os.getenv("DOMAIN", ""),
        "llm_provider_primary": os.getenv("LLM_PROVIDER_PRIMARY", ""),
        "llm_provider_primary_base_url": os.getenv("LLM_PROVIDER_PRIMARY_BASE_URL", ""),
        "llm_provider_primary_model": os.getenv("LLM_PROVIDER_PRIMARY_MODEL", ""),
        "llm_provider_fallback": os.getenv("LLM_PROVIDER_FALLBACK", ""),
        "llm_provider_fallback_base_url": os.getenv("LLM_PROVIDER_FALLBACK_BASE_URL", ""),
        "llm_provider_fallback_model": os.getenv("LLM_PROVIDER_FALLBACK_MODEL", ""),
        "stt_provider_primary": os.getenv("STT_PROVIDER_PRIMARY", ""),
        "stt_model": os.getenv("STT_MODEL", ""),
        "tts_provider_primary": os.getenv("TTS_PROVIDER_PRIMARY", ""),
        "tts_provider_fallback": os.getenv("TTS_PROVIDER_FALLBACK", ""),
        "kokoro_voice": os.getenv("KOKORO_VOICE", ""),
        "piper_voice_en": os.getenv("PIPER_VOICE_EN", ""),
        "piper_voice_ru": os.getenv("PIPER_VOICE_RU", ""),
        "telegram_enabled": os.getenv("TELEGRAM_ENABLED", "false").lower() == "true",
    }


async def fetch_service_status(
    client: httpx.AsyncClient,
    name: str,
    health_url: str,
    metrics_url: str | None = None,
) -> dict[str, Any]:
    """Fetch one service status. Output: service status dict. Input: HTTP client and URLs."""
    result: dict[str, Any] = {
        "name": name,
        "health": "down",
        "latency_ms": None,
        "memory_mb": None,
        "requests_total": None,
        "requests_by_path": {},
        "details": {},
    }
    started_at = time.perf_counter()
    try:
        response = await client.get(health_url)
        response.raise_for_status()
        result["health"] = "ok"
        result["latency_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
        try:
            result["details"] = response.json()
        except ValueError:
            result["details"] = {"raw": response.text[:200]}
    except Exception as exc:  # noqa: BLE001
        result["details"] = {"error": str(exc)}
        result["health"] = "down"
        return result

    if metrics_url:
        try:
            metrics_response = await client.get(metrics_url)
            metrics_response.raise_for_status()
            metrics = metrics_response.json()
            result["memory_mb"] = metrics.get("memory_mb")
            result["requests_total"] = metrics.get("requests_total")
            result["requests_by_path"] = metrics.get("requests_by_path", {})
            result["models"] = metrics.get("models", {})
            if "enabled_tools" in metrics:
                result["enabled_tools"] = metrics.get("enabled_tools", [])
        except Exception as exc:  # noqa: BLE001
            result["metrics_error"] = str(exc)

    return result


async def build_system_snapshot() -> dict[str, Any]:
    """Build system status snapshot. Output: full snapshot dict. Input: none."""
    timeout = httpx.Timeout(2.0, connect=1.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        checks = await asyncio.gather(
            fetch_service_status(client, "gateway", "http://gateway:8080/healthz", None),
            fetch_service_status(
                client,
                "webui-backend",
                "http://127.0.0.1:8000/api/health",
                "http://127.0.0.1:8000/api/metrics",
            ),
            fetch_service_status(client, "stt", "http://stt:8001/health", "http://stt:8001/metrics"),
            fetch_service_status(client, "tts-router", "http://tts-router:8002/health", "http://tts-router:8002/metrics"),
            fetch_service_status(client, "tools", "http://tools:8003/health", "http://tools:8003/metrics"),
            fetch_service_status(client, "piper-en", f"http://piper-en:{os.getenv('PIPER_PORT_EN', '6010')}/", None),
            fetch_service_status(client, "piper-ru", f"http://piper-ru:{os.getenv('PIPER_PORT_RU', '6011')}/", None),
            fetch_service_status(client, "kokoro", f"http://kokoro:{os.getenv('KOKORO_PORT', '6030')}/", None),
        )

    redis_ok = False
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("redis", 6379), timeout=1.5)
        writer.write(b"*1\r\n$4\r\nPING\r\n")
        await writer.drain()
        payload = await asyncio.wait_for(reader.read(64), timeout=1.5)
        redis_ok = b"+PONG" in payload
        writer.close()
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        redis_ok = False

    checks.append(
        {
            "name": "redis",
            "health": "ok" if redis_ok else "down",
            "latency_ms": None,
            "memory_mb": None,
            "requests_total": None,
            "requests_by_path": {},
            "details": {},
        }
    )

    checks.append(
        {
            "name": "asterisk",
            "health": "unknown",
            "latency_ms": None,
            "memory_mb": None,
            "requests_total": None,
            "requests_by_path": {},
            "details": {
                "note": "Container-level health must be provided by orchestrator checks.",
            },
        }
    )

    return {
        "type": "system.status",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runtime": build_runtime_snapshot(),
        "host_memory": get_host_memory_snapshot(),
        "services": checks,
    }


@app.get("/api/health")
def healthcheck() -> dict[str, str]:
    """Return backend health. Output: health dict. Input: none."""
    return {"status": "ok", "service": "webui-backend"}


@app.get("/api/runtime")
def get_runtime() -> dict[str, object]:
    """Return backend runtime snapshot. Output: runtime dict. Input: none."""
    return build_runtime_snapshot()


@app.get("/api/metrics")
def metrics() -> dict[str, object]:
    """Return backend metrics. Output: metrics dict. Input: none."""
    return {
        "service": "webui-backend",
        "health": "ok",
        "requests_total": REQUESTS_TOTAL,
        "requests_by_path": dict(REQUESTS_BY_PATH),
        "ws_messages_total": WS_MESSAGES_TOTAL,
        "memory_mb": get_memory_usage_mb(),
        "models": {
            "llm_primary_model": os.getenv("LLM_PROVIDER_PRIMARY_MODEL", ""),
            "llm_fallback_model": os.getenv("LLM_PROVIDER_FALLBACK_MODEL", ""),
        },
    }


@app.get("/api/system-status")
async def system_status() -> dict[str, Any]:
    """Return system status snapshot. Output: snapshot dict. Input: none."""
    return await build_system_snapshot()


@app.websocket("/ws")
async def websocket_session(websocket: WebSocket) -> None:
    """Handle websocket echo session. Output: none. Input: websocket connection."""
    await websocket.accept()
    await websocket.send_json(
        {
            "type": "session.ready",
            "message": "Colloc websocket session established.",
        }
    )

    try:
        while True:
            payload = await websocket.receive_text()
            global WS_MESSAGES_TOTAL
            WS_MESSAGES_TOTAL += 1
            await websocket.send_json(
                {
                    "type": "session.echo",
                    "message": payload,
                }
            )
    except WebSocketDisconnect:
        return


@app.websocket("/ws/system-status")
async def websocket_system_status(websocket: WebSocket) -> None:
    """Stream live system status. Output: periodic status events. Input: websocket connection."""
    await websocket.accept()

    try:
        while True:
            snapshot = await build_system_snapshot()
            await websocket.send_json(snapshot)
            await asyncio.sleep(2.0)
    except WebSocketDisconnect:
        return
