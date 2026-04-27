"""Shared HTTP clients for STT and TTS services."""

from __future__ import annotations

import os
from typing import Any

import httpx


DEFAULT_STT_URL = os.getenv("STT_URL", "http://stt:8001").rstrip("/")
DEFAULT_TTS_ROUTER_URL = os.getenv("TTS_ROUTER_URL", "http://tts-router:8002").rstrip("/")


async def call_stt_service(
    audio_b64: str,
    mime_type: str,
    language_hint: str | None = None,
    stt_url: str = DEFAULT_STT_URL,
    timeout_sec: float = 120.0,
) -> dict[str, Any]:
    """Call STT service. Output: parsed response JSON. Input: audio payload, MIME type, optional language hint, service URL, timeout."""
    payload: dict[str, Any] = {"audio_b64": audio_b64, "mime_type": mime_type, "partial": False, "language_hint": language_hint}
    timeout = httpx.Timeout(timeout_sec, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{stt_url.rstrip('/')}/transcribe", json=payload)
        response.raise_for_status()
        return response.json()


async def call_tts_service(
    text: str,
    language: str | None = None,
    tts_router_url: str = DEFAULT_TTS_ROUTER_URL,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    """Call TTS router. Output: parsed response JSON. Input: text, optional language, service URL, timeout."""
    payload: dict[str, Any] = {"text": text}
    if language:
        payload["language"] = language

    timeout = httpx.Timeout(timeout_sec, connect=3.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{tts_router_url.rstrip('/')}/synthesize", json=payload)
        response.raise_for_status()
        return response.json()