import base64
import io
import os
import wave
from collections import defaultdict
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from kokoro_onnx import Kokoro
from pydantic import BaseModel, Field


app = FastAPI(title="colloc-kokoro")
REQUESTS_TOTAL = 0
REQUESTS_BY_PATH: dict[str, int] = defaultdict(int)
_MODEL: Kokoro | None = None


class SynthesisRequest(BaseModel):
    text: str = Field(description="Text for synthesis")
    voice: str | None = Field(default=None, description="Optional Kokoro voice id")
    language: str | None = Field(default=None, description="Optional language code")


def _asset_dir() -> Path:
    """Resolve local Kokoro asset directory. Output: path. Input: none."""
    return Path(os.getenv("KOKORO_MODEL_DIR", "/tmp/kokoro-models"))


def _asset_urls() -> tuple[str, str]:
    """Get Kokoro model asset URLs. Output: model and voices URLs. Input: none."""
    model_url = os.getenv(
        "KOKORO_MODEL_URL",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    )
    voices_url = os.getenv(
        "KOKORO_VOICES_URL",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
    )
    return model_url, voices_url


def _asset_paths() -> tuple[Path, Path]:
    """Get local Kokoro model asset paths. Output: model and voices paths. Input: none."""
    asset_dir = _asset_dir()
    asset_dir.mkdir(parents=True, exist_ok=True)
    return asset_dir / "kokoro-v1.0.onnx", asset_dir / "voices-v1.0.bin"


def _download_if_missing(path: Path, url: str) -> None:
    """Download file only when missing. Output: none. Input: path and URL."""
    if path.exists() and path.stat().st_size > 0:
        return

    timeout = httpx.Timeout(180.0, connect=10.0)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            path.write_bytes(response.content)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to download Kokoro asset {url}: {exc}") from exc


def _get_model() -> Kokoro:
    """Load and cache Kokoro model. Output: Kokoro object. Input: none."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL

    model_url, voices_url = _asset_urls()
    model_path, voices_path = _asset_paths()
    _download_if_missing(model_path, model_url)
    _download_if_missing(voices_path, voices_url)

    try:
        _MODEL = Kokoro(str(model_path), str(voices_path))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to initialize Kokoro model: {exc}") from exc

    return _MODEL


def _pick_voice(model: Kokoro, requested_voice: str | None) -> str:
    """Pick available Kokoro voice. Output: voice id. Input: model and optional requested voice."""
    voices = model.get_voices()
    if not voices:
        raise HTTPException(status_code=500, detail="Kokoro voices list is empty")

    if requested_voice and requested_voice in voices:
        return requested_voice

    configured = os.getenv("KOKORO_VOICE", "").strip()
    if configured and configured in voices:
        return configured

    return voices[0]


def _normalize_lang(language: str | None) -> str:
    """Map language code to Kokoro language tag. Output: language tag. Input: optional language code."""
    normalized = (language or "").strip().lower()
    if normalized in {"ru", "ru-ru"}:
        return "ru"
    return "en-us"


@app.middleware("http")
async def count_requests(request: Request, call_next):
    """Track HTTP request counters. Output: response. Input: request and next handler."""
    global REQUESTS_TOTAL
    REQUESTS_TOTAL += 1
    REQUESTS_BY_PATH[request.url.path] += 1
    return await call_next(request)


@app.get("/health")
def healthcheck() -> dict[str, str]:
    """Return Kokoro service health. Output: health dict. Input: none."""
    return {"status": "ok", "service": "kokoro"}


@app.post("/preload")
def preload_model() -> dict[str, str]:
    """Preload Kokoro model into memory. Output: status dict. Input: none."""
    try:
        model = _get_model()
        voice = _pick_voice(model, None)
        return {
            "status": "ok",
            "message": f"Kokoro model preloaded with voice '{voice}'.",
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"Failed to preload: {exc}"}


@app.get("/voices")
def voices() -> dict[str, object]:
    """Return available Kokoro voices. Output: voices dict. Input: none."""
    model = _get_model()
    return {"voices": model.get_voices(), "default_voice": os.getenv("KOKORO_VOICE", "")}


@app.post("/synthesize")
def synthesize(request: SynthesisRequest) -> dict[str, object]:
    """Synthesize WAV speech with Kokoro. Output: audio payload dict. Input: synthesis request."""
    text = (request.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is empty")

    model = _get_model()
    voice = _pick_voice(model, request.voice)
    lang = _normalize_lang(request.language)

    try:
        audio, sample_rate = model.create(text=text, voice=voice, lang=lang)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Kokoro synthesis failed: {exc}") from exc

    pcm = np.clip(audio, -1.0, 1.0)
    pcm_int16 = (pcm * 32767.0).astype(np.int16)
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_int16.tobytes())

    return {
        "provider": "kokoro",
        "voice": voice,
        "lang": lang,
        "mime_type": "audio/wav",
        "audio_b64": base64.b64encode(wav_buffer.getvalue()).decode("ascii"),
        "sample_rate": sample_rate,
    }
