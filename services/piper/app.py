import io
import os
import re
import wave
from collections import defaultdict
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from piper.voice import PiperVoice
from pydantic import BaseModel, Field


app = FastAPI(title="colloc-piper")
REQUESTS_TOTAL = 0
REQUESTS_BY_PATH: dict[str, int] = defaultdict(int)
_VOICE_CACHE: dict[str, PiperVoice] = {}


class SynthesisRequest(BaseModel):
    text: str = Field(description="Text for synthesis")
    voice: str | None = Field(default=None, description="Optional Piper voice id")


def _model_dir() -> Path:
    """Resolve local model directory. Output: path. Input: none."""
    return Path(os.getenv("PIPER_MODEL_DIR", "/srv/data/piper-models"))


def _voice_id() -> str:
    """Return active voice id. Output: voice id string. Input: none."""
    raw = os.getenv("PIPER_VOICE", "en_US-lessac-medium").strip()
    return raw.removesuffix(".onnx")


def _voice_urls(voice_id: str) -> tuple[str, str]:
    """Build model/config URLs for a Piper voice. Output: model URL and config URL. Input: voice id."""
    base = os.getenv("PIPER_VOICE_BASE_URL", "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0").rstrip("/")
    match = re.match(r"^([a-z]{2})_([A-Z]{2})-([A-Za-z0-9_]+)-([A-Za-z0-9_]+)$", voice_id)
    if not match:
        raise HTTPException(status_code=500, detail=f"Unsupported Piper voice id format: {voice_id}")

    lang, country, speaker, quality = match.groups()
    rel = f"{lang}/{lang}_{country}/{speaker}/{quality}/{voice_id}.onnx"
    return f"{base}/{rel}", f"{base}/{rel}.json"


def _model_paths(voice_id: str) -> tuple[Path, Path]:
    """Build local model/config paths. Output: model path and config path. Input: voice id."""
    model_dir = _model_dir()
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / f"{voice_id}.onnx"
    config_path = model_dir / f"{voice_id}.onnx.json"
    return model_path, config_path


def _download_if_missing(path: Path, url: str) -> None:
    """Download file only when missing. Output: none. Input: path and URL."""
    if path.exists() and path.stat().st_size > 0:
        return

    timeout = httpx.Timeout(120.0, connect=10.0)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            path.write_bytes(response.content)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to download Piper model asset {url}: {exc}") from exc


def _get_voice(voice_id: str) -> PiperVoice:
    """Load and cache Piper voice. Output: PiperVoice instance. Input: voice id."""
    cached = _VOICE_CACHE.get(voice_id)
    if cached is not None:
        return cached

    model_path, config_path = _model_paths(voice_id)
    model_url, config_url = _voice_urls(voice_id)
    _download_if_missing(model_path, model_url)
    _download_if_missing(config_path, config_url)

    use_cuda = os.getenv("PIPER_DEVICE", "cpu").lower() in {"cuda", "gpu", "vram"}
    try:
        voice = PiperVoice.load(model_path, config_path=config_path, use_cuda=use_cuda)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to initialize Piper voice {voice_id}: {exc}") from exc

    _VOICE_CACHE[voice_id] = voice
    return voice


@app.middleware("http")
async def count_requests(request: Request, call_next):
    """Track HTTP request counters. Output: response. Input: request and next handler."""
    global REQUESTS_TOTAL
    REQUESTS_TOTAL += 1
    REQUESTS_BY_PATH[request.url.path] += 1
    return await call_next(request)


@app.get("/health")
def healthcheck() -> dict[str, str]:
    """Return Piper service health. Output: health dict. Input: none."""
    return {"status": "ok", "service": "piper"}


@app.post("/preload")
def preload_model() -> dict[str, str]:
    """Preload Piper voice model into memory. Output: status dict. Input: none."""
    try:
        voice_id = _voice_id()
        voice = _get_voice(voice_id)
        return {
            "status": "ok",
            "message": f"Piper voice '{voice_id}' preloaded.",
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"Failed to preload: {exc}"}


@app.get("/metrics")
def metrics() -> dict[str, object]:
    """Return service metrics. Output: metrics dict. Input: none."""
    return {
        "service": "piper",
        "requests_total": REQUESTS_TOTAL,
        "requests_by_path": dict(REQUESTS_BY_PATH),
        "active_voice": _voice_id(),
        "cached_voices": sorted(_VOICE_CACHE.keys()),
    }


@app.post("/synthesize")
def synthesize(request: SynthesisRequest) -> dict[str, object]:
    """Synthesize WAV speech with Piper. Output: audio payload dict. Input: synthesis request."""
    text = (request.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is empty")

    voice_id = (request.voice or _voice_id()).strip().removesuffix(".onnx")
    voice = _get_voice(voice_id)

    buffer = io.BytesIO()
    try:
        with wave.open(buffer, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Piper synthesis failed: {exc}") from exc

    return {
        "provider": "piper",
        "voice": voice_id,
        "mime_type": "audio/wav",
        "audio_b64": __import__("base64").b64encode(buffer.getvalue()).decode("ascii"),
        "sample_rate": getattr(getattr(voice, "config", None), "sample_rate", None),
    }
