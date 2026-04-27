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
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from services.common.llm_runtime import ChatRuntimeConfig, InvocationContext, fetch_enabled_tools, run_chat_with_tools, load_prompt_by_source, load_greeting_by_source
from services.common.media_clients import call_stt_service, call_tts_service
from services.common.runtime_llm_config import (
    LlmRuntimeConfig,
    RuntimeConfigError,
    apply_runtime_config_to_env,
    get_runtime_llm_config,
    set_runtime_llm_config,
)
from services.common.system_log import append_system_log, resolve_system_log_path, utc_timestamp

try:
    import pynvml as _pynvml

    nvmlInit = getattr(_pynvml, "nvmlInit", None)
    nvmlGetDeviceCount = getattr(_pynvml, "nvmlGetDeviceCount", getattr(_pynvml, "nvmlDeviceGetCount", None))
    nvmlGetDeviceByIndex = getattr(
        _pynvml,
        "nvmlGetDeviceByIndex",
        getattr(_pynvml, "nvmlDeviceGetHandleByIndex", None),
    )
    nvmlDeviceGetMemoryInfo = getattr(_pynvml, "nvmlDeviceGetMemoryInfo", None)
    NVMLError = getattr(_pynvml, "NVMLError", Exception)

    PYNVML_AVAILABLE = all(
        callable(func) for func in (nvmlInit, nvmlGetDeviceCount, nvmlGetDeviceByIndex, nvmlDeviceGetMemoryInfo)
    )
    if PYNVML_AVAILABLE:
        try:
            nvmlInit()
        except Exception:
            PYNVML_AVAILABLE = False
except ImportError:
    PYNVML_AVAILABLE = False
    nvmlInit = None
    nvmlGetDeviceCount = None
    nvmlGetDeviceByIndex = None
    nvmlDeviceGetMemoryInfo = None
    NVMLError = Exception


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


class LlmRuntimeConfigRequest(BaseModel):
    """Runtime request to change primary/fallback LLM routing. Output: parsed body. Input: server/model values."""

    primary_base_url: str
    primary_model: str
    fallback_base_url: str | None = None
    fallback_model: str | None = None
    autoload_enabled: bool | None = None


class LlmRuntimeConfigTestRequest(BaseModel):
    """Runtime request to test primary LLM routing values. Output: parsed body. Input: server/model values."""

    primary_base_url: str
    primary_model: str


def _llm_runtime_to_dict(config: LlmRuntimeConfig) -> dict[str, Any]:
    """Convert runtime config to API-safe dict. Output: dict. Input: runtime config."""
    return {
        "primary_base_url": config.primary_base_url,
        "primary_model": config.primary_model,
        "fallback_base_url": config.fallback_base_url,
        "fallback_model": config.fallback_model,
        "autoload_enabled": config.autoload_enabled,
    }


async def _load_runtime_llm_config() -> LlmRuntimeConfig:
    """Load runtime LLM config and apply it to process env. Output: config. Input: none."""
    config = await get_runtime_llm_config()
    apply_runtime_config_to_env(config)
    return config


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


def _derive_hook_url(base_hook_url: str, endpoint: str) -> str:
    """Build hook URL from reset hook URL and endpoint. Output: URL string. Input: base URL and endpoint path."""
    cleaned = base_hook_url.strip()
    if not cleaned:
        return ""
    if cleaned.endswith("/restart-services"):
        return f"{cleaned.rsplit('/restart-services', 1)[0]}{endpoint}"
    return f"{cleaned.rstrip('/')}{endpoint}"


def load_webui_default_prompt() -> str:
    """Load default Web UI system prompt from env. Output: prompt text. Input: none."""
    return load_prompt_by_source("webui", "")


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


async def _trigger_llm_reload_once(config: LlmRuntimeConfig, source: str) -> None:
    """Send minimal request to keep configured model hot. Output: none. Input: runtime config and source tag."""
    llm_url = config.primary_base_url.rstrip("/")
    model = config.primary_model
    if not llm_url or not model:
        append_system_log("autoload", "llm_reload_skip", "LLM reload skipped due to empty config.", {"source": source})
        return

    timeout = httpx.Timeout(120.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{llm_url}/api/generate",
            json={
                "model": model,
                "prompt": "ok",
                "stream": False,
            },
        )
        resp.raise_for_status()


async def _reload_llm_loop() -> None:
    """Periodically reload LLM model to keep it in VRAM. Output: none. Input: none."""
    global _AUTOLOAD_ENABLED
    while True:
        try:
            await asyncio.sleep(_LLM_RELOAD_INTERVAL_SEC)
            runtime_config = await _load_runtime_llm_config()
            _AUTOLOAD_ENABLED = runtime_config.autoload_enabled
            if not runtime_config.autoload_enabled:
                continue
            await _trigger_llm_reload_once(runtime_config, "interval")
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
    global _LLM_RELOAD_TASK, _AUTOLOAD_ENABLED
    runtime_config = await _load_runtime_llm_config()
    _AUTOLOAD_ENABLED = runtime_config.autoload_enabled
    append_system_log("runtime", "llm_config_loaded", "Runtime LLM config applied at startup.", _llm_runtime_to_dict(runtime_config))

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

SYSTEM_LOG_PATH = resolve_system_log_path()


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


def get_host_memory_snapshot() -> dict[str, Any]:
    """Get host memory snapshot. Output: memory dict with RAM and VRAM. Input: none."""
    result: dict[str, Any] = {}
    
    # Get RAM from /proc/meminfo
    meminfo: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as handle:
        for line in handle:
            key, value = line.split(":", 1)
            meminfo[key] = int(value.strip().split()[0])

    total_kb = meminfo.get("MemTotal", 0)
    available_kb = meminfo.get("MemAvailable", 0)
    used_kb = max(total_kb - available_kb, 0)

    result["ram"] = {
        "total_mb": round(total_kb / 1024.0, 2),
        "used_mb": round(used_kb / 1024.0, 2),
        "available_mb": round(available_kb / 1024.0, 2),
        "used_percent": round((used_kb / total_kb) * 100, 2) if total_kb else 0.0,
    }
    
    # Get VRAM using pynvml
    if PYNVML_AVAILABLE:
        try:
            device_count = nvmlGetDeviceCount()
            if device_count > 0:
                # Get first GPU
                device = nvmlGetDeviceByIndex(0)
                mem_info = nvmlDeviceGetMemoryInfo(device)
                result["vram"] = {
                    "total_mb": round(mem_info.total / (1024 * 1024), 2),
                    "used_mb": round(mem_info.used / (1024 * 1024), 2),
                    "available_mb": round(mem_info.free / (1024 * 1024), 2),
                    "used_percent": round((mem_info.used / mem_info.total) * 100, 2) if mem_info.total > 0 else 0.0,
                }
            else:
                result["vram"] = {"error": "No NVIDIA GPU detected"}
        except NVMLError as exc:
            result["vram"] = {"error": f"NVIDIA driver error: {exc}"}
        except Exception as exc:  # noqa: BLE001
            result["vram"] = {"error": f"Failed to query VRAM: {exc}"}
    else:
        result["vram"] = {"error": "pynvml not available in venv (install pynvml or nvidia-ml-py)"}
    
    return result


async def get_ollama_models() -> dict[str, Any]:
    """Get loaded models from ollama. Output: models dict. Input: none."""
    try:
        runtime_config = await _load_runtime_llm_config()
        ollama_base_url = runtime_config.primary_base_url or "http://ollama:11434"
        timeout = httpx.Timeout(2.0, connect=1.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{ollama_base_url}/api/ps")
            resp.raise_for_status()
            data = resp.json()
            models = {}
            for model in data.get("models", []):
                name = model.get("name", "unknown")
                size = model.get("size", 0)
                models[name] = {
                    "size_mb": round(size / (1024 * 1024), 2),
                    "size_gb": round(size / (1024 * 1024 * 1024), 2),
                }
            return {"loaded_models": models}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def get_service_models_memory() -> dict[str, Any]:
    """Get loaded models info from our services with device info. Output: models dict. Input: none."""
    result: dict[str, Any] = {}
    
    timeout = httpx.Timeout(2.0, connect=1.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Get piper info (CPU/GPU device aware)
        for lang in ["en", "ru", "zh"]:
            try:
                resp = await client.get(f"http://piper-{lang}:6020/metrics", timeout=timeout)
                if resp.status_code == 200:
                    metrics = resp.json()
                    if "piper" not in result:
                        result["piper"] = {}
                    result["piper"][lang] = {
                        "cached_voices": metrics.get("cached_voices", []),
                        "active_voice": metrics.get("active_voice"),
                        "memory_mb": metrics.get("memory_mb"),
                        "device": metrics.get("device", "cpu"),
                    }
            except Exception:  # noqa: BLE001
                pass
        
        # Get kokoro info
        try:
            resp = await client.get("http://kokoro:6030/metrics", timeout=timeout)
            if resp.status_code == 200:
                metrics = resp.json()
                result["kokoro"] = {
                    "health": metrics.get("health", "unknown"),
                    "memory_mb": metrics.get("memory_mb"),
                    "device": metrics.get("device", "cpu"),
                }
        except Exception:  # noqa: BLE001
            pass
        
        # Get silero info
        try:
            resp = await client.get("http://silero:6040/metrics", timeout=timeout)
            if resp.status_code == 200:
                metrics = resp.json()
                result["silero"] = {
                    "models_loaded": metrics.get("models_loaded", []),
                    "memory_mb": metrics.get("memory_mb"),
                    "device": metrics.get("device", "cpu"),
                }
        except Exception:  # noqa: BLE001
            pass
        
        # Get STT info
        try:
            resp = await client.get("http://stt:8001/metrics", timeout=timeout)
            if resp.status_code == 200:
                metrics = resp.json()
                result["stt"] = {
                    "memory_mb": metrics.get("memory_mb"),
                    "model": metrics.get("models", {}).get("stt_model"),
                    "device": metrics.get("models", {}).get("stt_device", "gpu"),
                    "compute_type": metrics.get("models", {}).get("stt_compute_type"),
                }
        except Exception:  # noqa: BLE001
            pass
    
    return result


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


def collect_recent_sip_events(limit: int = 8) -> list[str]:
    """Collect recent SIP lifecycle lines from system log. Output: list of log lines. Input: max line count."""
    interesting = {
        "sip.ari_start",
        "sip.ari_ready",
        "sip.ari_stop",
        "sip.call_start",
        "sip.call_stop",
        "sip.call_timeout",
        "sip.ari_channel_destroyed",
    }
    lines = tail_system_log(250)
    matched = [line for line in lines if any(token in line for token in interesting)]
    return matched[-limit:]


async def build_asterisk_status(client: httpx.AsyncClient) -> dict[str, Any]:
    """Build Asterisk/SIP diagnostics block for status page. Output: service-style status dict. Input: HTTP client."""
    asterisk_http_url = os.getenv("ASTERISK_HTTP_URL", "http://asterisk:8088/ari").rstrip("/")
    asterisk_ari_user = os.getenv("ASTERISK_ARI_USER", "colloc")
    asterisk_ari_password = os.getenv("ASTERISK_ARI_PASSWORD", "change-me")
    asterisk_ari_app = os.getenv("ASTERISK_ARI_APP", "colloc-call-handler")
    sip_service_url = os.getenv("ASTERISK_SIP_SERVICE_URL", "http://sip-service:8004").rstrip("/")

    result: dict[str, Any] = {
        "name": "asterisk",
        "health": "unknown",
        "latency_ms": None,
        "memory_mb": None,
        "requests_total": None,
        "requests_by_path": {},
        "details": {
            "ari_app": asterisk_ari_app,
            "ari_url": asterisk_http_url,
            "sip_service_url": sip_service_url,
            "active_calls_count": 0,
            "active_calls": [],
            "recent_events": collect_recent_sip_events(),
            "config": {
                "sip_max_silence_sec": int(os.getenv("SIP_MAX_SILENCE", "30")),
                "sip_max_duration_sec": int(os.getenv("SIP_MAX_DURATION", "600")),
                "asterisk_pjsip_port": int(os.getenv("ASTERISK_PJSIP_PORT", "6060")),
                "asterisk_rtp_start": int(os.getenv("ASTERISK_RTP_START", "6700")),
                "asterisk_rtp_end": int(os.getenv("ASTERISK_RTP_END", "6800")),
            },
        },
    }

    ari_ok = False
    ari_started_at = time.perf_counter()
    try:
        ari_response = await client.get(
            f"{asterisk_http_url}/asterisk/info",
            auth=(asterisk_ari_user, asterisk_ari_password),
        )
        ari_response.raise_for_status()
        ari_ok = True
        result["latency_ms"] = round((time.perf_counter() - ari_started_at) * 1000, 2)
        ari_body = ari_response.json() if ari_response.content else {}
        result["details"]["ari_info"] = {
            "system": ari_body.get("system"),
            "asterisk_id": ari_body.get("asterisk_id"),
            "version": ari_body.get("version"),
        }
    except Exception as exc:  # noqa: BLE001
        result["details"]["ari_error"] = str(exc)

    sip_ok = False
    try:
        sip_health = await client.get(f"{sip_service_url}/health")
        sip_health.raise_for_status()
        health_body = sip_health.json() if sip_health.content else {}
        sip_ok = str(health_body.get("status", "")).lower() == "ok"
        result["details"]["sip_health"] = health_body
    except Exception as exc:  # noqa: BLE001
        result["details"]["sip_health_error"] = str(exc)

    try:
        calls_response = await client.get(f"{sip_service_url}/ari/calls")
        calls_response.raise_for_status()
        calls_payload = calls_response.json() if calls_response.content else {}
        active_calls = calls_payload.get("active", []) if isinstance(calls_payload, dict) else []
        result["details"]["active_calls_count"] = len(active_calls)
        result["details"]["active_calls"] = active_calls
    except Exception as exc:  # noqa: BLE001
        result["details"]["active_calls_error"] = str(exc)

    if ari_ok and sip_ok:
        result["health"] = "ok"
    elif (not ari_ok) and (not sip_ok):
        result["health"] = "down"
    else:
        result["health"] = "unknown"

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
        asterisk_status = await build_asterisk_status(client)

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

    checks.append(asterisk_status)

    return {
        "type": "system.status",
        "timestamp": utc_timestamp(),
        "runtime": build_runtime_snapshot(),
        "host_memory": get_host_memory_snapshot(),
        "models": {
            "ollama": await get_ollama_models(),
            "services": await get_service_models_memory(),
        },
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


@app.get("/api/llm-runtime-config")
async def llm_runtime_config_get() -> dict[str, Any]:
    """Return active runtime LLM config. Output: config dict. Input: none."""
    runtime_config = await _load_runtime_llm_config()
    return {"status": "ok", **_llm_runtime_to_dict(runtime_config)}


@app.post("/api/llm-runtime-config")
async def llm_runtime_config_set(request: LlmRuntimeConfigRequest) -> dict[str, Any]:
    """Update runtime LLM config in Redis and current process env. Output: config dict. Input: request body."""
    previous_autoload_enabled = _AUTOLOAD_ENABLED
    current = await _load_runtime_llm_config()
    merged = LlmRuntimeConfig(
        primary_base_url=request.primary_base_url,
        primary_model=request.primary_model,
        fallback_base_url=current.fallback_base_url if request.fallback_base_url is None else request.fallback_base_url,
        fallback_model=current.fallback_model if request.fallback_model is None else request.fallback_model,
        autoload_enabled=current.autoload_enabled if request.autoload_enabled is None else request.autoload_enabled,
    )

    try:
        saved = await set_runtime_llm_config(merged)
    except RuntimeConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to persist runtime LLM config: {exc}") from exc

    apply_runtime_config_to_env(saved)
    if previous_autoload_enabled != saved.autoload_enabled:
        await _set_autoload_runtime_enabled(saved.autoload_enabled)
    append_system_log("runtime", "llm_config_updated", "Runtime LLM config updated.", _llm_runtime_to_dict(saved))

    if saved.autoload_enabled:
        try:
            await _trigger_llm_reload_once(saved, "runtime_update")
            append_system_log("autoload", "llm_reload", "LLM model reloaded after runtime config update")
        except Exception as exc:  # noqa: BLE001
            append_system_log("autoload", "llm_reload_error", f"LLM reload after runtime config update failed: {exc}")

    return {"status": "ok", **_llm_runtime_to_dict(saved)}


@app.post("/api/llm-runtime-config/test")
async def llm_runtime_config_test(request: LlmRuntimeConfigTestRequest) -> dict[str, Any]:
    """Test primary LLM server/model with one short chat completion call. Output: test result. Input: request body."""
    base_url = request.primary_base_url.strip().rstrip("/")
    model = request.primary_model.strip()

    if not base_url or not model:
        raise HTTPException(status_code=400, detail="primary_base_url and primary_model are required")

    started_at = time.perf_counter()
    timeout = httpx.Timeout(20.0, connect=3.0)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        "max_tokens": 16,
        "temperature": 0.0,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url}/v1/chat/completions", json=payload)
            response.raise_for_status()
            body = response.json()

        preview = ""
        choices = body.get("choices") if isinstance(body, dict) else None
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first, dict) else {}
            preview = str(message.get("content", ""))[:200] if isinstance(message, dict) else ""

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        append_system_log(
            "runtime",
            "llm_config_test_ok",
            "Runtime LLM config test succeeded.",
            {
                "primary_base_url": base_url,
                "primary_model": model,
                "duration_ms": duration_ms,
            },
        )
        return {
            "status": "ok",
            "primary_base_url": base_url,
            "primary_model": model,
            "duration_ms": duration_ms,
            "response_preview": preview,
        }
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        append_system_log(
            "runtime",
            "llm_config_test_error",
            "Runtime LLM config test failed.",
            {
                "primary_base_url": base_url,
                "primary_model": model,
                "duration_ms": duration_ms,
                "error": str(exc),
            },
        )
        raise HTTPException(status_code=502, detail=f"LLM config test failed: {exc}") from exc


@app.post("/api/autoload-status")
async def autoload_set_status(request: AutoloadToggleRequest) -> dict[str, Any]:
    """Set runtime keep-models-loaded state and persist it. Output: status dict. Input: enabled boolean."""
    current = await _load_runtime_llm_config()
    merged = LlmRuntimeConfig(
        primary_base_url=current.primary_base_url,
        primary_model=current.primary_model,
        fallback_base_url=current.fallback_base_url,
        fallback_model=current.fallback_model,
        autoload_enabled=bool(request.enabled),
    )

    try:
        saved = await set_runtime_llm_config(merged)
    except RuntimeConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to persist runtime autoload config: {exc}") from exc

    apply_runtime_config_to_env(saved)
    await _set_autoload_runtime_enabled(request.enabled)
    append_system_log("runtime", "autoload_updated", "Runtime autoload setting updated.", {"autoload_enabled": saved.autoload_enabled})
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
    """Trigger full system restart via configured reset backend. Output: status dict. Input: none."""
    reset_mode = os.getenv("SYSTEM_RESET_MODE", "command").strip().lower()
    hook_url = os.getenv("SYSTEM_RESET_HOOK_URL", "").strip()
    reset_command = os.getenv("SYSTEM_RESET_COMMAND", "").strip() or _default_system_reset_command()
    reset_cwd = os.getenv("SYSTEM_RESET_CWD", "/app").strip() or "/app"

    append_system_log("system", "reset.requested", "System restart requested via API.", {"mode": reset_mode})

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


@app.post("/api/system-stop-services")
async def system_stop_services() -> dict[str, Any]:
    """Stop non-core services via hook/command backend. Output: status dict. Input: none."""
    reset_mode = os.getenv("SYSTEM_RESET_MODE", "command").strip().lower()
    hook_url = _derive_hook_url(os.getenv("SYSTEM_RESET_HOOK_URL", "").strip(), "/stop-services")
    stop_command = os.getenv("SYSTEM_STOP_SERVICES_COMMAND", "").strip()
    reset_cwd = os.getenv("SYSTEM_RESET_CWD", "/app").strip() or "/app"

    append_system_log("system", "stop_services.requested", "Stop services requested via API.", {"mode": reset_mode})

    if reset_mode == "hook":
        if not hook_url:
            raise HTTPException(status_code=400, detail="SYSTEM_RESET_HOOK_URL is empty for hook mode.")
        timeout = httpx.Timeout(30.0, connect=3.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(hook_url, json={"source": "webui-backend", "requested_at": time.time()})
                resp.raise_for_status()
                payload = resp.json() if resp.content else {}
            append_system_log("system", "stop_services.dispatched", "Stop services request dispatched to hook.", {"hook_url": hook_url})
            return {"status": "accepted", "mode": "hook", **payload}
        except Exception as exc:  # noqa: BLE001
            append_system_log("system", "stop_services.error", f"Stop services hook failed: {exc}", {"hook_url": hook_url})
            raise HTTPException(status_code=502, detail=f"Stop services hook failed: {exc}") from exc

    if reset_mode == "command":
        if not stop_command:
            raise HTTPException(status_code=400, detail="SYSTEM_STOP_SERVICES_COMMAND is empty for command mode.")
        try:
            subprocess.Popen(
                ["/bin/sh", "-lc", f"cd {shlex.quote(reset_cwd)} && ({stop_command})"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            append_system_log("system", "stop_services.dispatched", "Stop services command started.", {"cwd": reset_cwd})
            return {"status": "accepted", "mode": "command", "message": "Stop services command started."}
        except Exception as exc:  # noqa: BLE001
            append_system_log("system", "stop_services.error", f"Stop services command failed: {exc}", {"cwd": reset_cwd})
            raise HTTPException(status_code=500, detail=f"Stop services command failed: {exc}") from exc

    raise HTTPException(status_code=400, detail="Unsupported SYSTEM_RESET_MODE. Use 'hook' or 'command'.")


@app.post("/api/system-start-services")
async def system_start_services() -> dict[str, Any]:
    """Start previously stopped services via hook/command backend. Output: status dict. Input: none."""
    reset_mode = os.getenv("SYSTEM_RESET_MODE", "command").strip().lower()
    hook_url = _derive_hook_url(os.getenv("SYSTEM_RESET_HOOK_URL", "").strip(), "/start-services")
    start_command = os.getenv("SYSTEM_START_SERVICES_COMMAND", "").strip()
    reset_cwd = os.getenv("SYSTEM_RESET_CWD", "/app").strip() or "/app"

    append_system_log("system", "start_services.requested", "Start services requested via API.", {"mode": reset_mode})

    if reset_mode == "hook":
        if not hook_url:
            raise HTTPException(status_code=400, detail="SYSTEM_RESET_HOOK_URL is empty for hook mode.")
        timeout = httpx.Timeout(30.0, connect=3.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(hook_url, json={"source": "webui-backend", "requested_at": time.time()})
                resp.raise_for_status()
                payload = resp.json() if resp.content else {}
            append_system_log("system", "start_services.dispatched", "Start services request dispatched to hook.", {"hook_url": hook_url})
            return {"status": "accepted", "mode": "hook", **payload}
        except Exception as exc:  # noqa: BLE001
            append_system_log("system", "start_services.error", f"Start services hook failed: {exc}", {"hook_url": hook_url})
            raise HTTPException(status_code=502, detail=f"Start services hook failed: {exc}") from exc

    if reset_mode == "command":
        if not start_command:
            raise HTTPException(status_code=400, detail="SYSTEM_START_SERVICES_COMMAND is empty for command mode.")
        try:
            subprocess.Popen(
                ["/bin/sh", "-lc", f"cd {shlex.quote(reset_cwd)} && ({start_command})"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            append_system_log("system", "start_services.dispatched", "Start services command started.", {"cwd": reset_cwd})
            return {"status": "accepted", "mode": "command", "message": "Start services command started."}
        except Exception as exc:  # noqa: BLE001
            append_system_log("system", "start_services.error", f"Start services command failed: {exc}", {"cwd": reset_cwd})
            raise HTTPException(status_code=500, detail=f"Start services command failed: {exc}") from exc

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


@app.get("/api/memory")
async def memory_info() -> dict[str, Any]:
    """Return detailed memory and models info. Output: memory snapshot dict. Input: none."""
    return {
        "type": "memory.snapshot",
        "timestamp": utc_timestamp(),
        "host_memory": get_host_memory_snapshot(),
        "models": {
            "ollama": await get_ollama_models(),
            "services": await get_service_models_memory(),
        },
    }


@app.get("/api/sip-stt-audio/last")
async def sip_last_stt_audio() -> dict[str, Any]:
    """Fetch last STT audio from SIP service. Output: audio metadata and base64. Input: none."""
    try:
        timeout = httpx.Timeout(3.0, connect=1.0)
        sip_service_url = os.getenv("ASTERISK_SIP_SERVICE_URL", "http://sip-service:8004").rstrip("/")
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{sip_service_url}/stt-audio/last")
            resp.raise_for_status()
            data = resp.json()
            return data
    except Exception as exc:  # noqa: BLE001
        return {"has_data": False, "error": str(exc)}


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
    try:
        body = await call_stt_service(audio_b64, mime_type, language_hint)
        return {
            "transcript": body.get("transcript", ""),
            "provider": str(body.get("provider", {}).get("primary", "")),
            "device": str(body.get("provider", {}).get("device", "")),
            "device_runtime": str(body.get("provider", {}).get("device_runtime", "")),
            "compute_type": str(body.get("provider", {}).get("compute_type", "")),
            "timings_ms": body.get("timings_ms", {}),
            "error": "",
            "status_code": 200,
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
    try:
        return await call_tts_service(text)
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
    runtime_config = await _load_runtime_llm_config()
    base_url = runtime_config.primary_base_url
    model = runtime_config.primary_model

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
    context = InvocationContext(source="webui")

    def _log_llm_event(event: str, message: str, details: dict[str, Any] | None = None) -> None:
        append_system_log("llm", event, message, details)

    tools = await fetch_enabled_tools(context, _log_llm_event)
    tool_names = [
        tool.get("function", {}).get("name", "")
        for tool in tools
        if tool.get("function", {}).get("name")
    ]
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
            "tools_enabled": bool(tools),
            "tools_count": len(tools),
            "available_tools": tool_names,
            "last_user_preview": last_user_message[:200],
            "system_message_preview": messages[0]["content"][:200] if messages and messages[0].get("role") == "system" else "",
        },
    )
    await send_event({"type": "llm.start"})

    try:
        result = await run_chat_with_tools(
            messages,
            ChatRuntimeConfig(
                base_url=base_url,
                model=model,
                timeout_sec=60.0,
                request_options={**reasoning_payload, **temperature_payload},
                fallback_base_url=runtime_config.fallback_base_url,
                fallback_model=runtime_config.fallback_model,
            ),
            context,
            _log_llm_event,
            tools=tools,
        )
    except Exception as exc:  # noqa: BLE001
        append_system_log(
            "llm",
            "error",
            "LLM request failed.",
            {"model": model, "error": str(exc)},
        )
        await send_event({"type": "error", "message": f"LLM request failed: {exc}"})
        return

    if result.fallback_error:
        await send_event({"type": "llm.warn", "message": f"Primary LLM failed ({result.fallback_error}), trying fallback."})

    full_response = result.text

    if full_response:
        tts_buffer = ""
        tts_chunk_index = 0
        tts_queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
        tts_task = asyncio.create_task(_stream_tts_chunks(websocket, send_event, tts_queue))

        async def handle_token(token: str) -> None:
            """Accumulate token into response and live TTS queue. Output: none. Input: token text."""
            nonlocal tts_buffer, tts_chunk_index
            tts_buffer += token
            ready_chunks, tts_buffer = _split_ready_tts_chunks(tts_buffer)
            for chunk_text in ready_chunks:
                tts_chunk_index += 1
                await tts_queue.put((tts_chunk_index, chunk_text))

        await handle_token(full_response)
        await send_event({"type": "llm.token", "token": full_response})
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
