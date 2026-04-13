import os
import resource
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field


app = FastAPI(title="colloc-tts-router")
REQUESTS_TOTAL = 0
REQUESTS_BY_PATH: dict[str, int] = defaultdict(int)


class SynthesisRequest(BaseModel):
    text: str = Field(description="Text for synthesis")


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
def synthesize(request: SynthesisRequest) -> dict[str, object]:
    """Return segmented synthesis plan. Output: synthesis plan dict. Input: synthesis request."""
    voice_map = get_voice_map()
    segments = split_text_by_language(request.text)
    planned_segments = []

    for segment in segments:
        language = segment["language"] if segment["language"] in voice_map else "EN"
        target = voice_map.get(language)
        planned_segments.append(
            {
                "language": language,
                "text": segment["text"],
                "target": target,
            }
        )

    return {
        "segments": planned_segments,
        "fallback": {
            "provider": os.getenv("TTS_PROVIDER_FALLBACK", ""),
            "external_url": os.getenv("TTS_EXTERNAL_URL", ""),
        },
    }
