"""Runtime LLM configuration storage backed by Redis."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

RUNTIME_LLM_CONFIG_KEY = "colloc:runtime:llm_config:v1"


@dataclass(slots=True)
class LlmRuntimeConfig:
    """Runtime LLM routing values. Output: dataclass instance. Input: primary/fallback server and model values."""

    primary_base_url: str
    primary_model: str
    fallback_base_url: str
    fallback_model: str
    autoload_enabled: bool


class RuntimeConfigError(Exception):
    """Raised when runtime config cannot be validated or persisted."""


def _env_defaults() -> LlmRuntimeConfig:
    """Build defaults from environment. Output: default runtime config. Input: none."""
    return LlmRuntimeConfig(
        primary_base_url=os.getenv("LLM_PROVIDER_PRIMARY_BASE_URL", "").strip(),
        primary_model=os.getenv("LLM_PROVIDER_PRIMARY_MODEL", "").strip(),
        fallback_base_url=os.getenv("LLM_PROVIDER_FALLBACK_BASE_URL", "").strip(),
        fallback_model=os.getenv("LLM_PROVIDER_FALLBACK_MODEL", "").strip(),
        autoload_enabled=os.getenv("AUTOLOAD", "false").strip().lower() in {"true", "1", "yes"},
    )


def _parse_bool(value: Any, default: bool) -> bool:
    """Parse mixed payload bool values. Output: bool. Input: raw value and default."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def _sanitize(config: LlmRuntimeConfig) -> LlmRuntimeConfig:
    """Normalize runtime config fields. Output: sanitized config. Input: raw config."""
    return LlmRuntimeConfig(
        primary_base_url=config.primary_base_url.strip().rstrip("/"),
        primary_model=config.primary_model.strip(),
        fallback_base_url=config.fallback_base_url.strip().rstrip("/"),
        fallback_model=config.fallback_model.strip(),
        autoload_enabled=bool(config.autoload_enabled),
    )


def _parse_runtime_payload(payload: dict[str, Any], defaults: LlmRuntimeConfig) -> LlmRuntimeConfig:
    """Parse Redis payload with fallback to defaults. Output: parsed config. Input: payload and defaults."""
    return _sanitize(
        LlmRuntimeConfig(
            primary_base_url=str(payload.get("primary_base_url", defaults.primary_base_url) or defaults.primary_base_url),
            primary_model=str(payload.get("primary_model", defaults.primary_model) or defaults.primary_model),
            fallback_base_url=str(payload.get("fallback_base_url", defaults.fallback_base_url) or defaults.fallback_base_url),
            fallback_model=str(payload.get("fallback_model", defaults.fallback_model) or defaults.fallback_model),
            autoload_enabled=_parse_bool(payload.get("autoload_enabled", defaults.autoload_enabled), defaults.autoload_enabled),
        )
    )


async def _read_resp_value(reader: asyncio.StreamReader) -> Any:
    """Read one RESP value from Redis stream. Output: parsed value. Input: stream reader."""
    token = await reader.readexactly(1)
    line = await reader.readline()
    payload = line[:-2]

    if token == b"+":
        return payload.decode("utf-8", errors="replace")
    if token == b"-":
        raise RuntimeConfigError(payload.decode("utf-8", errors="replace"))
    if token == b":":
        return int(payload.decode("ascii"))
    if token == b"$":
        length = int(payload.decode("ascii"))
        if length < 0:
            return None
        data = await reader.readexactly(length)
        await reader.readexactly(2)
        return data.decode("utf-8", errors="replace")
    if token == b"*":
        count = int(payload.decode("ascii"))
        if count < 0:
            return None
        values = []
        for _ in range(count):
            values.append(await _read_resp_value(reader))
        return values

    raise RuntimeConfigError(f"Unsupported Redis RESP token: {token!r}")


def _build_resp_command(*parts: str) -> bytes:
    """Build one RESP command payload. Output: command bytes. Input: command parts."""
    encoded = [part.encode("utf-8") for part in parts]
    chunks = [f"*{len(encoded)}\r\n".encode("ascii")]
    for item in encoded:
        chunks.append(f"${len(item)}\r\n".encode("ascii"))
        chunks.append(item)
        chunks.append(b"\r\n")
    return b"".join(chunks)


async def _redis_execute(*parts: str, timeout_sec: float = 2.0) -> Any:
    """Execute one Redis command using low-level TCP. Output: parsed response. Input: command parts."""
    host = os.getenv("REDIS_HOST", "redis").strip() or "redis"
    port = int(os.getenv("REDIS_PORT", "6379"))
    db = int(os.getenv("REDIS_DB", "0"))
    password = os.getenv("REDIS_PASSWORD", "").strip()

    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None

    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_sec)

        if password:
            writer.write(_build_resp_command("AUTH", password))
            await writer.drain()
            await asyncio.wait_for(_read_resp_value(reader), timeout=timeout_sec)

        if db:
            writer.write(_build_resp_command("SELECT", str(db)))
            await writer.drain()
            await asyncio.wait_for(_read_resp_value(reader), timeout=timeout_sec)

        writer.write(_build_resp_command(*parts))
        await writer.drain()
        return await asyncio.wait_for(_read_resp_value(reader), timeout=timeout_sec)
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()


def apply_runtime_config_to_env(config: LlmRuntimeConfig) -> None:
    """Apply runtime config values to current process environment. Output: none. Input: config."""
    os.environ["LLM_PROVIDER_PRIMARY_BASE_URL"] = config.primary_base_url
    os.environ["LLM_PROVIDER_PRIMARY_MODEL"] = config.primary_model
    os.environ["LLM_PROVIDER_FALLBACK_BASE_URL"] = config.fallback_base_url
    os.environ["LLM_PROVIDER_FALLBACK_MODEL"] = config.fallback_model
    os.environ["AUTOLOAD"] = "true" if config.autoload_enabled else "false"


async def get_runtime_llm_config() -> LlmRuntimeConfig:
    """Read runtime config from Redis or environment defaults. Output: runtime config. Input: none."""
    defaults = _sanitize(_env_defaults())
    try:
        raw_payload = await _redis_execute("GET", RUNTIME_LLM_CONFIG_KEY)
        if not raw_payload:
            return defaults
        parsed = json.loads(str(raw_payload))
        if not isinstance(parsed, dict):
            return defaults
        return _parse_runtime_payload(parsed, defaults)
    except Exception:
        return defaults


async def set_runtime_llm_config(config: LlmRuntimeConfig) -> LlmRuntimeConfig:
    """Persist runtime config to Redis. Output: saved config. Input: runtime config."""
    sanitized = _sanitize(config)
    if not sanitized.primary_base_url or not sanitized.primary_model:
        raise RuntimeConfigError("primary_base_url and primary_model are required")

    payload = json.dumps(asdict(sanitized), ensure_ascii=True)
    await _redis_execute("SET", RUNTIME_LLM_CONFIG_KEY, payload)
    return sanitized
