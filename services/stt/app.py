import os
import resource
from collections import defaultdict

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field


app = FastAPI(title="colloc-stt")
REQUESTS_TOTAL = 0
REQUESTS_BY_PATH: dict[str, int] = defaultdict(int)


class TranscriptionRequest(BaseModel):
    audio_url: str | None = Field(default=None, description="External audio URL")
    language_hint: str | None = Field(default=None, description="Optional language hint")
    partial: bool = Field(default=False, description="Partial transcript flag")


def get_active_provider() -> dict[str, str]:
    """Get active STT provider info. Output: provider dict. Input: none."""
    return {
        "primary": os.getenv("STT_PROVIDER_PRIMARY", ""),
        "fallback": os.getenv("STT_PROVIDER_FALLBACK", ""),
        "model": os.getenv("STT_MODEL", ""),
        "external_url": os.getenv("STT_EXTERNAL_URL", ""),
    }


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
    """Return STT health. Output: health dict. Input: none."""
    return {"status": "ok", "service": "stt"}


@app.get("/metrics")
def metrics() -> dict[str, object]:
    """Return service metrics. Output: metrics dict. Input: none."""
    provider = get_active_provider()
    return {
        "service": "stt",
        "health": "ok",
        "requests_total": REQUESTS_TOTAL,
        "requests_by_path": dict(REQUESTS_BY_PATH),
        "memory_mb": get_memory_usage_mb(),
        "models": {
            "stt_model": provider["model"],
            "external_stt_url": provider["external_url"],
        },
    }


@app.get("/providers")
def list_providers() -> dict[str, str]:
    """Return STT providers. Output: provider dict. Input: none."""
    return get_active_provider()


@app.post("/transcribe")
def transcribe(request: TranscriptionRequest) -> dict[str, object]:
    """Return placeholder transcript. Output: transcript dict. Input: transcription request."""
    provider = get_active_provider()
    transcript = "partial transcript" if request.partial else "final transcript"
    return {
        "provider": provider,
        "language_hint": request.language_hint,
        "audio_url": request.audio_url,
        "partial": request.partial,
        "transcript": transcript,
    }
