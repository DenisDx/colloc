import asyncio
import base64
import json
import os
import resource
import shlex
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel


app = FastAPI(title="colloc-webui-backend")
REQUESTS_TOTAL = 0
REQUESTS_BY_PATH: dict[str, int] = defaultdict(int)
WS_MESSAGES_TOTAL = 0
TTS_STREAM_SOFT_CHUNK_CHARS = max(32, int(os.getenv("TTS_STREAM_SOFT_CHUNK_CHARS", "120")))
TTS_STREAM_MIN_CHUNK_CHARS = max(16, int(os.getenv("TTS_STREAM_MIN_CHUNK_CHARS", "48")))

# Background tasks for model preloading
_AUTOLOAD_CONFIG_DEFAULT = os.getenv("AUTOLOAD", "false").lower() in {"true", "1", "yes"}
_AUTOLOAD_ENABLED = _AUTOLOAD_CONFIG_DEFAULT
_LLM_RELOAD_INTERVAL_SEC = int(os.getenv("AUTOLOAD_LLM_RELOAD_INTERVAL_SEC", "180"))
_LLM_RELOAD_TASK: asyncio.Task[Any] | None = None
_AUTOLOAD_PRELOAD_TASK: asyncio.Task[Any] | None = None
_AUTOLOAD_STATE_LOCK = asyncio.Lock()


class AutoloadToggleRequest(BaseModel):
    """Runtime request to enable/disable keep-models-loaded behavior. Output: parsed body. Input: enabled boolean."""

    enabled: bool


async def _preload_stt() -> None:
    """Preload STT model via HTTP. Output: none. Input: none."""
    try:
        timeout = httpx.Timeout(120.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post("http://stt:8001/preload")
            resp.raise_for_status()
            result = resp.json()
            append_system_log("autoload", "stt", result.get("message", str(result)))
    except Exception as exc:  # noqa: BLE001
        append_system_log("autoload", "stt_error", f"STT preload failed: {exc}")


def _active_tts_preload_endpoints() -> list[tuple[str, str]]:
    """Build preload endpoint list from per-language TTS config. Output: list of (url, name). Input: none."""
    endpoints: list[tuple[str, str]] = []
    seen_providers: set[str] = set()
    piper_langs: dict[str, str] = {}  # lang -> port

    for key, val in os.environ.items():
        if not val or not key.startswith("TTS_"):
            continue
        parts = key.split("_")
        # TTS_<LANG>_<PRIMARY|FALLBACK>_PROVIDER
        if len(parts) == 4 and parts[2] in {"PRIMARY", "FALLBACK"} and parts[3] == "PROVIDER":
            lang = parts[1]
            provider = val.lower()
            port_key = f"TTS_{lang}_{parts[2]}_PORT"

            if provider == "piper" and lang not in piper_langs:
                port = os.getenv(port_key, "")
                if port:
                    piper_langs[lang] = port

            elif provider in {"kokoro", "silero"} and provider not in seen_providers:
                seen_providers.add(provider)
                if provider == "kokoro":
                    port = os.getenv("KOKORO_PORT", "6030")
                    endpoints.append((f"http://kokoro:{port}/preload", "kokoro"))
                elif provider == "silero":
                    port = os.getenv("SILERO_PORT", "6040")
                    endpoints.append((f"http://silero:{port}/preload", "silero"))

    for lang, port in piper_langs.items():
        name = f"piper-{lang.lower()}"
        endpoints.append((f"http://{name}:{port}/preload", name))

    return endpoints


def _active_tts_reset_profiles() -> list[str]:
    """Build active TTS compose profiles from per-language provider config. Output: profile list. Input: none."""
    profiles: set[str] = set()
    for key, val in os.environ.items():
        if not val or not key.startswith("TTS_"):
            continue
        parts = key.split("_")
        if len(parts) != 4 or parts[2] not in {"PRIMARY", "FALLBACK"} or parts[3] != "PROVIDER":
            continue

        lang = parts[1].lower()
        provider = val.lower().strip()
        if provider == "piper":
            profiles.add(f"tts-piper-{lang}")
        elif provider == "kokoro":
            profiles.add("tts-kokoro")
        elif provider == "silero":
            profiles.add("tts-silero")

    return sorted(profiles)


def _default_system_reset_command() -> str:
    """Build default reset command with core and active TTS dependency profiles. Output: shell command. Input: none."""
    profile_args = ["--profile core", *[f"--profile {profile}" for profile in _active_tts_reset_profiles()]]
    compose_base = f"docker compose {' '.join(profile_args)}"
    return f"{compose_base} down && {compose_base} up -d"


async def _preload_tts() -> None:
    """Preload TTS models via HTTP for all configured providers. Output: none. Input: none."""
    endpoints = _active_tts_preload_endpoints()
    for url, name in endpoints:
        try:
            timeout = httpx.Timeout(120.0, connect=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url)
                resp.raise_for_status()
                result = resp.json()
                append_system_log("autoload", name, result.get("message", str(result)))
        except Exception as exc:  # noqa: BLE001
            append_system_log("autoload", f"{name}_error", f"{name} preload failed: {exc}")


async def _reload_llm_loop() -> None:
    """Periodically reload LLM model to keep it in VRAM. Output: none. Input: none."""
    while True:
        try:
            await asyncio.sleep(_LLM_RELOAD_INTERVAL_SEC)
            if not _AUTOLOAD_ENABLED:
                continue
            timeout = httpx.Timeout(120.0, connect=5.0)
            llm_url = os.getenv("LLM_PROVIDER_PRIMARY_BASE_URL", "http://ollama:11434").rstrip("/")
            # Try to trigger a minimal generate call to keep model loaded
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{llm_url}/api/generate",
                    json={
                        "model": os.getenv("LLM_PROVIDER_PRIMARY_MODEL", ""),
                        "prompt": "ok",
                        "stream": False,
                    },
                )
                resp.raise_for_status()
            append_system_log("autoload", "llm_reload", "LLM model reloaded to keep in VRAM")
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            append_system_log("autoload", "llm_reload_error", f"LLM reload failed: {exc}")


async def _run_autoload_preload() -> None:
    """Preload configured models for runtime keep-loaded mode. Output: none. Input: none."""
    append_system_log("autoload", "startup", "Model preloading enabled")
    await asyncio.gather(_preload_stt(), _preload_tts(), return_exceptions=True)


async def _set_autoload_runtime_enabled(enabled: bool) -> None:
    """Update runtime keep-models-loaded state. Output: none. Input: enable flag."""
    global _AUTOLOAD_ENABLED, _LLM_RELOAD_TASK, _AUTOLOAD_PRELOAD_TASK

    async with _AUTOLOAD_STATE_LOCK:
        if _AUTOLOAD_ENABLED == enabled:
            return

        _AUTOLOAD_ENABLED = enabled

        if enabled:
            append_system_log("autoload", "runtime_enabled", "Keep-models-loaded mode enabled.")
            _AUTOLOAD_PRELOAD_TASK = asyncio.create_task(_run_autoload_preload())
            if _LLM_RELOAD_INTERVAL_SEC > 0 and (_LLM_RELOAD_TASK is None or _LLM_RELOAD_TASK.done()):
                _LLM_RELOAD_TASK = asyncio.create_task(_reload_llm_loop())
                append_system_log(
                    "autoload", "llm_reload_start", f"LLM reload loop started (interval: {_LLM_RELOAD_INTERVAL_SEC}s)"
                )
            return

        append_system_log("autoload", "runtime_disabled", "Keep-models-loaded mode disabled.")
        if _AUTOLOAD_PRELOAD_TASK is not None and not _AUTOLOAD_PRELOAD_TASK.done():
            _AUTOLOAD_PRELOAD_TASK.cancel()
        if _LLM_RELOAD_TASK is not None and not _LLM_RELOAD_TASK.done():
            _LLM_RELOAD_TASK.cancel()
            _LLM_RELOAD_TASK = None


@app.on_event("startup")
async def startup_preload() -> None:
    """Preload models on startup if AUTOLOAD is enabled. Output: none. Input: none."""
    global _LLM_RELOAD_TASK
    if not _AUTOLOAD_ENABLED:
        return

    await _run_autoload_preload()

    # Start LLM reload loop
    if _LLM_RELOAD_INTERVAL_SEC > 0:
        _LLM_RELOAD_TASK = asyncio.create_task(_reload_llm_loop())
        append_system_log(
            "autoload", "llm_reload_start", f"LLM reload loop started (interval: {_LLM_RELOAD_INTERVAL_SEC}s)"
        )


@app.on_event("shutdown")
def shutdown_cleanup() -> None:
    """Cancel background tasks on shutdown. Output: none. Input: none."""
    global _LLM_RELOAD_TASK, _AUTOLOAD_PRELOAD_TASK
    if _LLM_RELOAD_TASK is not None and not _LLM_RELOAD_TASK.done():
        _LLM_RELOAD_TASK.cancel()
    if _AUTOLOAD_PRELOAD_TASK is not None and not _AUTOLOAD_PRELOAD_TASK.done():
        _AUTOLOAD_PRELOAD_TASK.cancel()



def resolve_system_log_path() -> Path:
    """Resolve writable system log path. Output: absolute path. Input: none."""
    candidates: list[Path] = []
    env_path = os.getenv("SYSTEM_LOG_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        [
            Path("/srv/logs/system.log"),
        ]
    )

    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            return path
        except OSError:
            continue

    return candidates[0]


SYSTEM_LOG_PATH = resolve_system_log_path()


def append_system_log(component: str, event: str, message: str, details: dict[str, Any] | None = None) -> str:
    """Append one system log line. Output: written text line. Input: component, event, message, optional details."""
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"[{timestamp}] {component}.{event}: {message}"
    if details:
        line = f"{line} | {json.dumps(details, ensure_ascii=False)}"
    with open(SYSTEM_LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")
    return line


def build_reasoning_payload(base_url: str, enabled: bool) -> dict[str, Any]:
    """Build provider-compatible reasoning fields. Output: request field dict. Input: base URL and UI boolean."""
    normalized = base_url.lower()
    if "11434" in normalized or "ollama" in normalized:
        return {"reasoning_effort": "medium" if enabled else "none"}
    return {"reasoning": enabled}


def build_temperature_payload(base_url: str, temperature: float) -> dict[str, Any]:
    """Build provider-compatible temperature fields. Output: request field dict. Input: base URL and temperature."""
    safe_temperature = max(0.0, min(2.0, temperature))
    normalized = base_url.lower()
    if "11434" in normalized or "ollama" in normalized:
        return {"options": {"temperature": safe_temperature}}
    return {"temperature": safe_temperature}


def tail_system_log(limit: int = 200) -> list[str]:
    """Read tail of system log. Output: list of lines. Input: max line count."""
    if not SYSTEM_LOG_PATH.exists():
        return []
    with open(SYSTEM_LOG_PATH, encoding="utf-8") as handle:
        lines = handle.readlines()
    return [line.rstrip("\n") for line in lines[-limit:]]


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
            *[
                fetch_service_status(client, name, preload_url.replace("/preload", "/health"), None)
                for preload_url, name in _active_tts_preload_endpoints()
            ],
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


@app.get("/api/autoload-status")
def autoload_status() -> dict[str, Any]:
    """Return autoload configuration status. Output: status dict. Input: none."""
    global _LLM_RELOAD_TASK
    return {
        "enabled": _AUTOLOAD_ENABLED,
        "configured_default": _AUTOLOAD_CONFIG_DEFAULT,
        "llm_reload_interval_sec": _LLM_RELOAD_INTERVAL_SEC,
        "llm_reload_task_running": _LLM_RELOAD_TASK is not None and not _LLM_RELOAD_TASK.done(),
    }


@app.post("/api/autoload-status")
async def autoload_set_status(request: AutoloadToggleRequest) -> dict[str, Any]:
    """Set runtime keep-models-loaded state until process restart. Output: status dict. Input: enabled boolean."""
    await _set_autoload_runtime_enabled(request.enabled)
    return autoload_status()


@app.post("/api/autoload-preload")
async def autoload_preload_now() -> dict[str, Any]:
    """Manually trigger model preloading. Output: result dict. Input: none."""
    if not _AUTOLOAD_ENABLED:
        return {"status": "disabled", "message": "Keep-models-loaded mode is disabled"}

    results = {
        "stt": {},
        "tts": {},
    }

    try:
        await _preload_stt()
        results["stt"]["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        results["stt"]["status"] = "error"
        results["stt"]["message"] = str(exc)

    try:
        await _preload_tts()
        results["tts"]["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        results["tts"]["status"] = "error"
        results["tts"]["message"] = str(exc)

    return results


@app.post("/api/system-reset")
async def system_reset() -> dict[str, Any]:
    """Trigger full system reset via configured reset backend. Output: status dict. Input: none."""
    reset_mode = os.getenv("SYSTEM_RESET_MODE", "command").strip().lower()
    hook_url = os.getenv("SYSTEM_RESET_HOOK_URL", "").strip()
    reset_command = os.getenv("SYSTEM_RESET_COMMAND", "").strip() or _default_system_reset_command()
    reset_cwd = os.getenv("SYSTEM_RESET_CWD", "/app").strip() or "/app"

    append_system_log("system", "reset.requested", "System reset requested via API.", {"mode": reset_mode})

    if reset_mode == "hook":
        if not hook_url:
            reset_mode = "command"
            append_system_log(
                "system",
                "reset.mode_fallback",
                "SYSTEM_RESET_HOOK_URL is empty, fallback to command mode.",
                {"cwd": reset_cwd},
            )
        else:
            timeout = httpx.Timeout(15.0, connect=3.0)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(hook_url, json={"source": "webui-backend", "requested_at": time.time()})
                    resp.raise_for_status()
                append_system_log("system", "reset.dispatched", "Reset request dispatched to hook.", {"hook_url": hook_url})
                return {"status": "accepted", "mode": "hook", "message": "Reset hook accepted request."}
            except Exception as exc:  # noqa: BLE001
                append_system_log("system", "reset.error", f"Reset hook failed: {exc}", {"hook_url": hook_url})
                raise HTTPException(status_code=502, detail=f"Reset hook failed: {exc}") from exc

    if reset_mode == "command":
        try:
            subprocess.Popen(
                ["/bin/sh", "-lc", f"cd {shlex.quote(reset_cwd)} && ({reset_command})"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            append_system_log("system", "reset.dispatched", "Reset command started.", {"cwd": reset_cwd, "command": reset_command})
            return {"status": "accepted", "mode": "command", "message": "Reset command started."}
        except Exception as exc:  # noqa: BLE001
            append_system_log("system", "reset.error", f"Reset command failed: {exc}", {"cwd": reset_cwd})
            raise HTTPException(status_code=500, detail=f"Reset command failed: {exc}") from exc

    raise HTTPException(status_code=400, detail="Unsupported SYSTEM_RESET_MODE. Use 'hook' or 'command'.")


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


@app.get("/api/log")
def get_system_log() -> dict[str, Any]:
    """Return current system log tail. Output: log tail dict. Input: none."""
    return {"lines": tail_system_log()}


@app.websocket("/api/log")
async def websocket_system_log(websocket: WebSocket) -> None:
    """Stream system log tail and appended lines. Output: websocket events. Input: websocket connection."""
    await websocket.accept()
    await websocket.send_json({"type": "system.log.snapshot", "lines": tail_system_log()})

    position = SYSTEM_LOG_PATH.stat().st_size if SYSTEM_LOG_PATH.exists() else 0

    try:
        while True:
            await asyncio.sleep(0.5)
            if not SYSTEM_LOG_PATH.exists():
                continue

            file_size = SYSTEM_LOG_PATH.stat().st_size
            if file_size < position:
                position = 0
            if file_size == position:
                continue

            with open(SYSTEM_LOG_PATH, encoding="utf-8") as handle:
                handle.seek(position)
                new_text = handle.read()
                position = handle.tell()

            for line in new_text.splitlines():
                if line:
                    await websocket.send_json({"type": "system.log", "line": line})
    except WebSocketDisconnect:
        return


@app.websocket("/ws/log")
async def websocket_system_log_legacy(websocket: WebSocket) -> None:
    """Backward/fallback websocket endpoint for system log. Output: websocket events. Input: websocket connection."""
    await websocket_system_log(websocket)


@app.websocket("/ws")
async def websocket_session(websocket: WebSocket) -> None:
    """Handle voice/text session with real STT→LLM pipeline. Output: none. Input: websocket connection."""
    await websocket.accept()
    await websocket.send_json({"type": "session.ready", "message": "Colloc websocket session established."})

    session = _SessionState()

    try:
        while True:
            payload = await websocket.receive_text()
            global WS_MESSAGES_TOTAL
            WS_MESSAGES_TOTAL += 1

            try:
                msg = __import__("json").loads(payload)
            except ValueError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON."})
                continue

            msg_type = msg.get("type", "")

            if msg_type == "session.config":
                session.system_prompt = msg.get("system_prompt", session.system_prompt)
                session.role = msg.get("role", session.role)
                session.reasoning = bool(msg.get("reasoning", session.reasoning))
                options = msg.get("options") if isinstance(msg.get("options"), dict) else {}
                if "temperature" in options:
                    try:
                        session.temperature = max(0.0, min(2.0, float(options.get("temperature", session.temperature))))
                    except (TypeError, ValueError):
                        pass
                append_system_log(
                    "session",
                    "config",
                    "Session config updated.",
                    {
                        "role": session.role,
                        "has_system_prompt": bool(session.system_prompt.strip()),
                        "system_prompt_preview": session.system_prompt[:200],
                        "reasoning": session.reasoning,
                        "temperature": session.temperature,
                    },
                )
                await websocket.send_json(
                    {
                        "type": "session.config.ack",
                        "system_prompt": session.system_prompt,
                        "role": session.role,
                        "reasoning": session.reasoning,
                        "temperature": session.temperature,
                    }
                )

            elif msg_type == "voice.utterance":
                audio_b64 = msg.get("audio_b64", "")
                mime_type = msg.get("mime_type", "audio/webm")
                if not audio_b64:
                    await websocket.send_json({"type": "error", "message": "voice.utterance: audio_b64 is empty."})
                    continue

                append_system_log(
                    "stt",
                    "start",
                    "STT transcription started.",
                    {
                        "source": "voice",
                        "mime_type": mime_type,
                        "language_hint": msg.get("language_hint"),
                        "audio_b64_chars": len(audio_b64),
                    },
                )

                stt_result = await _call_stt(audio_b64, mime_type, msg.get("language_hint"))
                if stt_result.get("error"):
                    append_system_log(
                        "stt",
                        "error",
                        f"STT transcription failed: {stt_result.get('error')}",
                        {
                            "source": "voice",
                            "mime_type": mime_type,
                            "language_hint": msg.get("language_hint"),
                            "audio_b64_chars": len(audio_b64),
                            "status_code": stt_result.get("status_code"),
                            "response_detail": stt_result.get("response_detail", ""),
                            "response_preview": stt_result.get("response_preview", ""),
                            "provider": stt_result.get("provider", ""),
                            "device": stt_result.get("device", ""),
                            "device_runtime": stt_result.get("device_runtime", ""),
                            "compute_type": stt_result.get("compute_type", ""),
                            "timings_ms": stt_result.get("timings_ms", {}),
                        },
                    )
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": f"STT failed: {stt_result.get('error')}",
                        }
                    )
                    continue

                transcript = stt_result.get("transcript", "")
                stt_placeholder = transcript in {"final transcript", "partial transcript"}
                append_system_log(
                    "stt",
                    "complete",
                    f"STT transcription finished{' (placeholder)' if stt_placeholder else ''}: {transcript[:200]}",
                    {
                        "source": "voice",
                        "chars": len(transcript),
                        "text_preview": transcript[:200],
                        "placeholder": stt_placeholder,
                        "provider": stt_result.get("provider", ""),
                        "device": stt_result.get("device", ""),
                        "device_runtime": stt_result.get("device_runtime", ""),
                        "compute_type": stt_result.get("compute_type", ""),
                        "timings_ms": stt_result.get("timings_ms", {}),
                    },
                )
                await websocket.send_json({"type": "stt.result", "text": transcript, "source": "voice"})

                if transcript:
                    session.history.append({"role": "user", "content": transcript})
                    await _stream_llm(websocket, session)

            elif msg_type == "text.query":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                session.history.append({"role": "user", "content": text})
                await _stream_llm(websocket, session)

            else:
                await websocket.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})

    except WebSocketDisconnect:
        return


@dataclass
class _SessionState:
    """Per-websocket session state."""
    system_prompt: str = ""
    role: str = "assistant"
    reasoning: bool = False
    temperature: float = 0.7
    history: list[dict[str, str]] = field(default_factory=list)


async def _call_stt(audio_b64: str, mime_type: str, language_hint: str | None) -> dict[str, Any]:
    """Transcribe audio via STT service. Output: result dict. Input: base64 audio, MIME type, optional language hint."""
    timeout = httpx.Timeout(120.0, connect=5.0)
    payload: dict[str, Any] = {"audio_b64": audio_b64, "mime_type": mime_type, "partial": False}
    if language_hint:
        payload["language_hint"] = language_hint

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post("http://stt:8001/transcribe", json=payload)
            resp.raise_for_status()
            body = resp.json()
            return {
                "transcript": body.get("transcript", ""),
                "provider": str(body.get("provider", {}).get("primary", "")),
                "device": str(body.get("provider", {}).get("device", "")),
                "device_runtime": str(body.get("provider", {}).get("device_runtime", "")),
                "compute_type": str(body.get("provider", {}).get("compute_type", "")),
                "timings_ms": body.get("timings_ms", {}),
                "error": "",
                "status_code": resp.status_code,
                "response_detail": "",
                "response_preview": "",
            }
    except httpx.HTTPStatusError as exc:
        response_preview = (exc.response.text or "")[:1000]
        response_detail = ""
        try:
            parsed = exc.response.json()
            response_detail = str(parsed.get("detail", ""))
        except Exception:  # noqa: BLE001
            response_detail = ""
        return {
            "transcript": "",
            "provider": "",
            "device": "",
            "device_runtime": "",
            "compute_type": "",
            "timings_ms": {},
            "error": f"HTTP {exc.response.status_code} from STT service",
            "status_code": exc.response.status_code,
            "response_detail": response_detail,
            "response_preview": response_preview,
        }
    except httpx.RequestError as exc:
        exc_msg = repr(exc) if not str(exc) else str(exc)
        return {
            "transcript": "",
            "provider": "",
            "device": "",
            "device_runtime": "",
            "compute_type": "",
            "timings_ms": {},
            "error": f"STT transport error: {exc_msg}",
            "status_code": None,
            "response_detail": "",
            "response_preview": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "transcript": "",
            "provider": "",
            "device": "",
            "device_runtime": "",
            "compute_type": "",
            "timings_ms": {},
            "error": f"Unexpected STT error: {exc}",
            "status_code": None,
            "response_detail": "",
            "response_preview": "",
        }


async def _call_tts(text: str) -> dict[str, Any]:
    """Prepare TTS playback plan via router. Output: TTS payload dict. Input: text."""
    timeout = httpx.Timeout(30.0, connect=2.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post("http://tts-router:8002/synthesize", json={"text": text})
            response.raise_for_status()
            return response.json()
    except Exception as exc:  # noqa: BLE001
        return {"mode": "error", "error": str(exc), "segments": []}


def _split_ready_tts_chunks(text: str, *, flush: bool = False) -> tuple[list[str], str]:
    """Split buffered LLM text into ready TTS chunks. Output: ready chunks and tail. Input: buffered text and flush flag."""
    ready_chunks: list[str] = []
    chunk_start = 0
    punctuation = {".", "!", "?", ";", ":", "\n"}

    index = 0
    while index < len(text):
        char = text[index]
        current_len = index - chunk_start + 1

        if char in punctuation:
            chunk = text[chunk_start : index + 1].strip()
            if chunk:
                ready_chunks.append(chunk)
            chunk_start = index + 1
            index += 1
            continue

        if current_len >= TTS_STREAM_SOFT_CHUNK_CHARS:
            window = text[chunk_start : index + 1]
            split_offset = max(window.rfind(" "), window.rfind("\t"))
            if split_offset >= TTS_STREAM_MIN_CHUNK_CHARS:
                split_at = chunk_start + split_offset + 1
                chunk = text[chunk_start:split_at].strip()
                if chunk:
                    ready_chunks.append(chunk)
                chunk_start = split_at

        index += 1

    tail = text[chunk_start:]
    if flush:
        chunk = tail.strip()
        if chunk:
            ready_chunks.append(chunk)
        return ready_chunks, ""

    return ready_chunks, tail


async def _stream_tts_chunks(
    websocket: WebSocket,
    send_event: Any,
    queue: asyncio.Queue[tuple[int, str] | None],
) -> None:
    """Synthesize and send queued TTS chunks in order. Output: none. Input: websocket sender and text queue."""
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            break

        chunk_index, chunk_text = item
        append_system_log(
            "tts",
            "input",
            "Sentence prepared for TTS stage.",
            {"chars": len(chunk_text), "text_preview": chunk_text[:200], "chunk_index": chunk_index, "dispatched": True},
        )
        tts_payload = await _call_tts(chunk_text)
        tts_payload["chunk_index"] = chunk_index
        tts_payload["chunk_text"] = chunk_text
        tts_payload["streaming"] = True
        append_system_log(
            "tts",
            "result",
            "TTS chunk completed.",
            {
                "chunk_index": chunk_index,
                "chars": len(chunk_text),
                "mode": tts_payload.get("mode", "unknown"),
                "provider": tts_payload.get("provider", ""),
                "segments": len(tts_payload.get("segments", [])),
                "error": tts_payload.get("error", ""),
            },
        )
        await send_event({"type": "tts.result", "payload": tts_payload})
        queue.task_done()


async def _stream_llm(websocket: WebSocket, session: "_SessionState") -> None:
    """Stream LLM response tokens back through websocket. Output: none. Input: websocket, session state."""
    base_url = os.getenv("LLM_PROVIDER_PRIMARY_BASE_URL", "").rstrip("/")
    model = os.getenv("LLM_PROVIDER_PRIMARY_MODEL", "")

    websocket_send_lock = asyncio.Lock()

    async def send_event(payload: dict[str, Any]) -> None:
        """Serialize websocket sends inside one session. Output: none. Input: outbound message payload."""
        async with websocket_send_lock:
            await websocket.send_json(payload)

    if not base_url or not model:
        await send_event({"type": "error", "message": "LLM not configured (check LLM_PROVIDER_PRIMARY_BASE_URL and LLM_PROVIDER_PRIMARY_MODEL)."})
        return

    messages: list[dict[str, str]] = []
    system_parts: list[str] = []
    if session.role:
        system_parts.append(f"You are a helpful {session.role}.")
    if session.system_prompt:
        system_parts.append(session.system_prompt)
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    messages.extend(session.history)

    last_user_message = next((msg["content"] for msg in reversed(messages) if msg.get("role") == "user"), "")
    reasoning_payload = build_reasoning_payload(base_url, session.reasoning)
    temperature_payload = build_temperature_payload(base_url, session.temperature)

    append_system_log(
        "llm",
        "request",
        "Calling LLM provider.",
        {
            "model": model,
            "messages": len(messages),
            "reasoning": session.reasoning,
            "temperature": session.temperature,
            "reasoning_payload": reasoning_payload,
            "temperature_payload": temperature_payload,
            "last_user_preview": last_user_message[:200],
            "system_message_preview": messages[0]["content"][:200] if messages and messages[0].get("role") == "system" else "",
        },
    )
    await send_event({"type": "llm.start"})

    request_body = {"model": model, "messages": messages, "stream": True, **reasoning_payload, **temperature_payload}
    url = f"{base_url}/v1/chat/completions"
    timeout = httpx.Timeout(60.0, connect=5.0)

    full_response = ""
    tts_buffer = ""
    tts_chunk_index = 0
    tts_queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
    tts_task = asyncio.create_task(_stream_tts_chunks(websocket, send_event, tts_queue))

    async def handle_token(token: str) -> None:
        """Accumulate token into response and live TTS queue. Output: none. Input: token text."""
        nonlocal full_response, tts_buffer, tts_chunk_index
        full_response += token
        tts_buffer += token
        ready_chunks, tts_buffer = _split_ready_tts_chunks(tts_buffer)
        for chunk_text in ready_chunks:
            tts_chunk_index += 1
            await tts_queue.put((tts_chunk_index, chunk_text))

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=request_body) as resp:
                resp.raise_for_status()
                async for raw_line in resp.aiter_lines():
                    line = raw_line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = __import__("json").loads(data)
                        token = chunk["choices"][0].get("delta", {}).get("content", "")
                        if token:
                            await handle_token(token)
                            await send_event({"type": "llm.token", "token": token})
                    except (KeyError, ValueError):
                        pass

    except Exception as exc:  # noqa: BLE001
        # Attempt fallback provider
        fallback_url = os.getenv("LLM_PROVIDER_FALLBACK_BASE_URL", "").rstrip("/")
        fallback_model = os.getenv("LLM_PROVIDER_FALLBACK_MODEL", "")
        if fallback_url and fallback_model and fallback_url != base_url:
            append_system_log(
                "llm",
                "fallback",
                "Primary LLM failed; switching to fallback provider.",
                {"error": str(exc), "fallback_model": fallback_model},
            )
            await send_event({"type": "llm.warn", "message": f"Primary LLM failed ({exc}), trying fallback."})
            fallback_body = {**request_body, "model": fallback_model}
            fallback_endpoint = f"{fallback_url}/v1/chat/completions"
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("POST", fallback_endpoint, json=fallback_body) as resp:
                        resp.raise_for_status()
                        async for raw_line in resp.aiter_lines():
                            line = raw_line.strip()
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[len("data:"):].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = __import__("json").loads(data)
                                token = chunk["choices"][0].get("delta", {}).get("content", "")
                                if token:
                                    await handle_token(token)
                                    await send_event({"type": "llm.token", "token": token})
                            except (KeyError, ValueError):
                                pass
            except Exception as fb_exc:  # noqa: BLE001
                await send_event({"type": "error", "message": f"Fallback LLM also failed: {fb_exc}"})
                await tts_queue.put(None)
                await tts_task
                return
        else:
            await send_event({"type": "error", "message": f"LLM request failed: {exc}"})
            await tts_queue.put(None)
            await tts_task
            return

    if full_response:
        tail_chunks, _ = _split_ready_tts_chunks(tts_buffer, flush=True)
        for chunk_text in tail_chunks:
            tts_chunk_index += 1
            await tts_queue.put((tts_chunk_index, chunk_text))
        session.history.append({"role": "assistant", "content": full_response})
        append_system_log(
            "llm",
            "response",
            "LLM response received.",
            {"chars": len(full_response), "text_preview": full_response[:200]},
        )
        await send_event({"type": "llm.done", "text": full_response})
    await tts_queue.put(None)
    await tts_task




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
