import base64
import logging
import os
import resource
import tempfile
import time
from collections import defaultdict
from threading import Lock
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from faster_whisper import WhisperModel
from pydantic import BaseModel, Field


app = FastAPI(title="colloc-stt")
REQUESTS_TOTAL = 0
REQUESTS_BY_PATH: dict[str, int] = defaultdict(int)
_MODEL: WhisperModel | None = None
_MODEL_KEY: tuple[str, str, str] | None = None
_MODEL_LOCK = Lock()
LOGGER = logging.getLogger("colloc.stt")


class TranscriptionRequest(BaseModel):
    audio_url: str | None = Field(default=None, description="External audio URL")
    audio_b64: str | None = Field(default=None, description="Base64-encoded audio bytes")
    mime_type: str | None = Field(default=None, description="Audio MIME type, e.g. audio/webm")
    language_hint: str | None = Field(default=None, description="Optional language hint")
    partial: bool = Field(default=False, description="Partial transcript flag")


def get_active_provider() -> dict[str, str]:
    """Get active provider info. Output: provider dict. Input: none."""
    return {
        "primary": os.getenv("STT_PROVIDER_PRIMARY", ""),
        "fallback": os.getenv("STT_PROVIDER_FALLBACK", ""),
        "model": os.getenv("STT_MODEL", "large-v3-turbo"),
        "external_url": os.getenv("STT_EXTERNAL_URL", ""),
        "device": os.getenv("STT_DEVICE", "cpu"),
        "compute_type": os.getenv("STT_COMPUTE_TYPE", "int8"),
    }


def get_memory_usage_mb() -> float:
    """Get process memory usage. Output: memory in MB. Input: none."""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2)


def get_whisper_model() -> WhisperModel:
    """Get cached Whisper model instance. Output: WhisperModel. Input: none."""
    global _MODEL, _MODEL_KEY
    hf_home = os.getenv("HF_HOME", "/tmp/huggingface")
    os.environ.setdefault("HF_HOME", hf_home)
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", f"{hf_home}/hub")
    os.environ.setdefault("XDG_CACHE_HOME", "/tmp")
    os.makedirs(os.environ["HF_HOME"], exist_ok=True)
    os.makedirs(os.environ["HUGGINGFACE_HUB_CACHE"], exist_ok=True)

    provider = get_active_provider()
    model_name = provider["model"] or "large-v3-turbo"
    device = provider["device"] or "cpu"
    compute_type = provider["compute_type"] or ("float16" if device != "cpu" else "int8")
    model_key = (model_name, device, compute_type)
    download_root = os.getenv("STT_MODEL_CACHE_DIR", "/tmp/faster-whisper-models")

    if _MODEL is not None and _MODEL_KEY == model_key:
        return _MODEL

    with _MODEL_LOCK:
        if _MODEL is not None and _MODEL_KEY == model_key:
            return _MODEL
        os.makedirs(download_root, exist_ok=True)
        _MODEL = WhisperModel(model_name, device=device, compute_type=compute_type, download_root=download_root)
        _MODEL_KEY = model_key
        return _MODEL


async def load_audio_bytes(request: TranscriptionRequest) -> bytes:
    """Load raw audio bytes from request. Output: bytes. Input: transcription request."""
    if request.audio_b64:
        try:
            return base64.b64decode(request.audio_b64)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid audio_b64 payload: {exc}") from exc

    if request.audio_url:
        timeout = httpx.Timeout(20.0, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(request.audio_url)
                response.raise_for_status()
                return response.content
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Failed to fetch audio_url: {exc}") from exc

    raise HTTPException(status_code=400, detail="Either audio_b64 or audio_url must be provided.")


async def transcribe_external(request: TranscriptionRequest) -> dict[str, Any]:
    """Delegate transcription to external STT service. Output: STT response dict. Input: transcription request."""
    external_url = os.getenv("STT_EXTERNAL_URL", "").rstrip("/")
    if not external_url:
        raise HTTPException(status_code=500, detail="STT_EXTERNAL_URL is not configured.")

    timeout = httpx.Timeout(60.0, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(external_url, json=request.model_dump())
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"External STT request failed: {exc}") from exc

    payload.setdefault("provider", get_active_provider())
    return payload


def _mime_to_ext(mime_type: str | None) -> str:
    """Map MIME type to a file extension for ffmpeg format detection. Output: str. Input: mime type string."""
    if not mime_type:
        return ".webm"
    base = mime_type.split(";")[0].strip().lower()
    return {
        "audio/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/mp4": ".mp4",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/flac": ".flac",
    }.get(base, ".webm")


async def transcribe_faster_whisper(request: TranscriptionRequest) -> dict[str, Any]:
    """Transcribe audio locally with faster-whisper. Output: STT response dict. Input: transcription request."""
    audio_bytes = await load_audio_bytes(request)
    provider = get_active_provider()
    model = get_whisper_model()

    beam_size = int(os.getenv("STT_BEAM_SIZE", "1" if request.partial else "5"))
    vad_filter = os.getenv("STT_VAD_FILTER", "true").lower() == "true"
    ext = _mime_to_ext(request.mime_type)

    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=True) as tmp:
            tmp.write(audio_bytes)
            tmp.flush()
            segments, info = model.transcribe(
                tmp.name,
                language=request.language_hint or None,
                beam_size=beam_size,
                vad_filter=vad_filter,
                condition_on_previous_text=False,
                without_timestamps=True,
            )
            transcript = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"faster-whisper transcription failed: {exc}") from exc

    audio_source = "b64" if request.audio_b64 else ("url" if request.audio_url else "none")
    return {
        "provider": provider,
        "language_hint": request.language_hint,
        "audio_source": audio_source,
        "mime_type": request.mime_type,
        "partial": request.partial,
        "detected_language": getattr(info, "language", None),
        "detected_language_probability": getattr(info, "language_probability", None),
        "transcript": transcript,
    }


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


@app.post("/preload")
def preload_model() -> dict[str, str]:
    """Preload Whisper model into memory. Output: status dict. Input: none."""
    try:
        model = get_whisper_model()
        return {
            "status": "ok",
            "message": f"STT model '{get_active_provider()['model']}' preloaded.",
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"Failed to preload: {exc}"}


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
            "stt_device": provider["device"],
            "stt_compute_type": provider["compute_type"],
        },
    }


@app.get("/providers")
def list_providers() -> dict[str, str]:
    """Return STT providers. Output: provider dict. Input: none."""
    return get_active_provider()


@app.post("/transcribe")
async def transcribe(request: TranscriptionRequest) -> dict[str, object]:
    """Transcribe audio via configured provider. Output: transcript dict. Input: transcription request."""
    started_at = time.perf_counter()
    primary = os.getenv("STT_PROVIDER_PRIMARY", "faster_whisper").lower()
    fallback = os.getenv("STT_PROVIDER_FALLBACK", "").lower()
    provider = get_active_provider()
    audio_source = "b64" if request.audio_b64 else ("url" if request.audio_url else "none")
    audio_size = len(request.audio_b64 or "") if request.audio_b64 else 0

    LOGGER.info(
        "stt.task.accepted provider=%s model=%s device=%s compute_type=%s source=%s mime_type=%s audio_b64_chars=%s partial=%s",
        primary,
        provider.get("model", ""),
        provider.get("device", ""),
        provider.get("compute_type", ""),
        audio_source,
        request.mime_type,
        audio_size,
        request.partial,
    )

    if primary == "external":
        result = await transcribe_external(request)
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        LOGGER.info("stt.task.completed provider=external elapsed_ms=%.1f", elapsed_ms)
        return result

    try:
        result = await transcribe_faster_whisper(request)
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        transcript_len = len(str(result.get("transcript", "")))
        LOGGER.info(
            "stt.task.completed provider=faster_whisper elapsed_ms=%.1f transcript_chars=%s",
            elapsed_ms,
            transcript_len,
        )
        return result
    except HTTPException as exc:
        if fallback == "external" and os.getenv("STT_EXTERNAL_URL", "").strip():
            fallback_result = await transcribe_external(request)
            fallback_result["fallback_used"] = True
            fallback_result["primary_error"] = exc.detail
            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            LOGGER.warning(
                "stt.task.fallback provider=external elapsed_ms=%.1f primary_error=%s",
                elapsed_ms,
                exc.detail,
            )
            return fallback_result
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        LOGGER.error("stt.task.failed elapsed_ms=%.1f error=%s", elapsed_ms, exc.detail)
        raise
