import os
import resource
from collections import defaultdict
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field


app = FastAPI(title="colloc-tts-router")
REQUESTS_TOTAL = 0
REQUESTS_BY_PATH: dict[str, int] = defaultdict(int)


class SynthesisRequest(BaseModel):
    text: str = Field(description="Text for synthesis")


def language_to_speech_locale(language: str) -> str:
    """Map internal language code to speech locale. Output: locale string. Input: language code."""
    if language == "RU":
        return "ru-RU"
    if language == "EN":
        return "en-US"
    return "en-US"


def classify_character(char: str) -> str:
    """Classify one character. Output: language code. Input: one character."""
    if "A" <= char <= "Z" or "a" <= char <= "z":
        return "EN"
    if "А" <= char <= "я" or char in {"Ё", "ё"}:
        return "RU"
    return "OTHER"


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


def split_text_by_language(text: str) -> list[dict[str, str]]:
    """Split text by language groups. Output: segment list. Input: source text."""
    if not text.strip():
        return []

    segments: list[dict[str, str]] = []
    current_language = "OTHER"
    current_chars: list[str] = []

    for char in text:
        language = classify_character(char)
        normalized_language = current_language if language == "OTHER" else language
        if not current_chars:
            current_language = normalized_language
            current_chars.append(char)
            continue

        if normalized_language != current_language and language != "OTHER":
            segments.append({"language": current_language, "text": "".join(current_chars).strip()})
            current_chars = [char]
            current_language = normalized_language
            continue

        current_chars.append(char)

    tail = "".join(current_chars).strip()
    if tail:
        segments.append({"language": current_language, "text": tail})

    return [segment for segment in segments if segment["text"]]


def get_voice_map() -> dict[str, dict[str, Any]]:
    """Build Piper voice map. Output: voice mapping dict. Input: none."""
    mapping: dict[str, dict[str, Any]] = {}
    for key, voice in os.environ.items():
        if not key.startswith("PIPER_VOICE_"):
            continue

        suffix = key.removeprefix("PIPER_VOICE_")
        port = os.getenv(f"PIPER_PORT_{suffix}")
        if not port:
            continue

        mapping[suffix] = {
            "voice": voice,
            "port": int(port),
            "url": f"http://piper-{suffix.lower()}:{port}",
        }

    return mapping


async def call_provider(provider: str, segment: dict[str, Any]) -> dict[str, Any]:
    """Call one TTS provider for a text segment. Output: provider result dict. Input: provider name and segment data."""
    timeout = httpx.Timeout(60.0, connect=5.0)

    if provider == "piper":
        target = segment.get("target") or {}
        base_url = target.get("url")
        if not base_url:
            raise HTTPException(status_code=500, detail=f"Piper target is not configured for language {segment.get('language')}")
        payload = {"text": segment["text"], "voice": target.get("voice")}
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url}/synthesize", json=payload)
            response.raise_for_status()
            data = response.json()
            data["provider"] = "piper"
            return data

    if provider == "kokoro":
        kokoro_port = int(os.getenv("KOKORO_PORT", "6030"))
        payload = {
            "text": segment["text"],
            "voice": os.getenv("KOKORO_VOICE", ""),
            "language": segment.get("locale", "en-US"),
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"http://kokoro:{kokoro_port}/synthesize", json=payload)
            response.raise_for_status()
            data = response.json()
            data["provider"] = "kokoro"
            return data

    if provider == "external":
        external_url = os.getenv("TTS_EXTERNAL_URL", "").rstrip("/")
        if not external_url:
            raise HTTPException(status_code=500, detail="TTS_EXTERNAL_URL is not configured")
        payload = {"text": segment["text"], "language": segment.get("locale", "en-US")}
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(external_url, json=payload)
            response.raise_for_status()
            data = response.json()
            data["provider"] = "external"
            return data

    raise HTTPException(status_code=500, detail=f"Unknown TTS provider: {provider}")


@app.get("/health")
def healthcheck() -> dict[str, str]:
    """Return TTS router health. Output: health dict. Input: none."""
    return {"status": "ok", "service": "tts-router"}


@app.get("/metrics")
def metrics() -> dict[str, object]:
    """Return service metrics. Output: metrics dict. Input: none."""
    return {
        "service": "tts-router",
        "health": "ok",
        "requests_total": REQUESTS_TOTAL,
        "requests_by_path": dict(REQUESTS_BY_PATH),
        "memory_mb": get_memory_usage_mb(),
        "models": {
            "tts_primary": os.getenv("TTS_PROVIDER_PRIMARY", ""),
            "tts_fallback": os.getenv("TTS_PROVIDER_FALLBACK", ""),
            "piper_voices": get_voice_map(),
            "kokoro_voice": os.getenv("KOKORO_VOICE", ""),
        },
    }


@app.get("/voices")
def list_voices() -> dict[str, object]:
    """Return voice map. Output: voice map dict. Input: none."""
    return {
        "primary": os.getenv("TTS_PROVIDER_PRIMARY", ""),
        "fallback": os.getenv("TTS_PROVIDER_FALLBACK", ""),
        "voices": get_voice_map(),
        "kokoro_voice": os.getenv("KOKORO_VOICE", ""),
        "kokoro_port": os.getenv("KOKORO_PORT", ""),
    }


@app.post("/synthesize")
async def synthesize(request: SynthesisRequest) -> dict[str, object]:
    """Synthesize speech using primary/fallback providers. Output: audio segments dict. Input: synthesis request."""
    voice_map = get_voice_map()
    segments = split_text_by_language(request.text)
    planned_segments = []
    primary_provider = os.getenv("TTS_PROVIDER_PRIMARY", "piper").lower()
    fallback_provider = os.getenv("TTS_PROVIDER_FALLBACK", "kokoro").lower()
    external_url = os.getenv("TTS_EXTERNAL_URL", "").rstrip("/")

    for segment in segments:
        language = segment["language"] if segment["language"] in voice_map else "EN"
        target = voice_map.get(language)
        planned_segments.append(
            {
                "language": language,
                "locale": language_to_speech_locale(language),
                "text": segment["text"],
                "target": target,
                "provider": primary_provider,
            }
        )

    if primary_provider == "external" and external_url:
        timeout = httpx.Timeout(60.0, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(external_url, json={"text": request.text, "segments": planned_segments})
                response.raise_for_status()
                payload = response.json()
                payload.setdefault("mode", "server_audio")
                payload.setdefault("segments", planned_segments)
                payload.setdefault("provider", primary_provider)
                payload.setdefault("fallback", {"provider": fallback_provider, "external_url": external_url})
                return payload
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"External TTS request failed: {exc}") from exc

    synthesized_segments: list[dict[str, Any]] = []
    provider_errors: list[dict[str, str]] = []
    used_fallback = False

    for segment in planned_segments:
        chosen_provider = primary_provider
        try:
            result = await call_provider(chosen_provider, segment)
        except Exception as primary_exc:  # noqa: BLE001
            if fallback_provider and fallback_provider != chosen_provider:
                chosen_provider = fallback_provider
                used_fallback = True
                try:
                    result = await call_provider(chosen_provider, segment)
                except Exception as fallback_exc:  # noqa: BLE001
                    provider_errors.append(
                        {
                            "language": str(segment.get("language", "")),
                            "primary_error": str(primary_exc),
                            "fallback_error": str(fallback_exc),
                        }
                    )
                    continue
            else:
                provider_errors.append(
                    {
                        "language": str(segment.get("language", "")),
                        "primary_error": str(primary_exc),
                    }
                )
                continue

        synthesized_segments.append(
            {
                "language": segment.get("language"),
                "locale": segment.get("locale"),
                "text": segment.get("text"),
                "provider": result.get("provider", chosen_provider),
                "voice": result.get("voice", ""),
                "mime_type": result.get("mime_type", "audio/wav"),
                "audio_b64": result.get("audio_b64", ""),
                "sample_rate": result.get("sample_rate"),
            }
        )

    if not synthesized_segments:
        raise HTTPException(status_code=502, detail=f"No TTS segments synthesized: {provider_errors}")

    return {
        "mode": "server_audio",
        "provider": primary_provider,
        "fallback_used": used_fallback,
        "segments": synthesized_segments,
        "errors": provider_errors,
        "fallback": {
            "provider": fallback_provider,
            "external_url": external_url,
        },
    }
