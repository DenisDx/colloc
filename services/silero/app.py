import base64
import io
import os
import wave
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

app = FastAPI(title="colloc-silero")
REQUESTS_TOTAL = 0
REQUESTS_BY_PATH: dict[str, int] = defaultdict(int)
_MODEL_CACHE: dict[str, Any] = {}

SUPPORTED_LANGUAGES = {"ru", "en", "de", "es", "fr", "tt", "indic", "ua"}
SAMPLE_RATE = 48000
MODEL_IDS = {
    "ru": "v5_4_ru",
    "en": "v3_en",
    "de": "v3_de",
    "es": "v3_es",
    "fr": "v3_fr",
    "indic": "v4_indic",
    "ua": "v4_ua",
    "tt": "v4_cyrillic",
}


class SynthesisRequest(BaseModel):
    text: str = Field(description="Text for synthesis")
    voice: str | None = Field(default=None, description="Optional speaker id")
    language: str | None = Field(default=None, description="Language code: ru, en, de, es, fr, tt, indic, ua")
    sample_rate: int | None = Field(default=None, description="Output sample rate (8000, 24000, 48000)")


def _model_dir() -> Path:
    """Resolve local model cache directory. Output: path. Input: none."""
    return Path(os.getenv("SILERO_MODEL_DIR", "/srv/data/silero-models"))


def _default_language() -> str:
    """Return configured default language. Output: language code. Input: none."""
    return os.getenv("SILERO_LANGUAGE", "ru").lower()


def _default_voice() -> str:
    """Return configured default speaker id. Output: speaker string. Input: none."""
    return os.getenv("SILERO_VOICE", "").strip().lower()


def _model_id(language: str) -> str:
    """Return Silero model id for language. Output: model id string. Input: language code."""
    configured = os.getenv("SILERO_MODEL_ID", "").strip()
    if configured:
        return configured
    return MODEL_IDS.get(language, "v3_en")


def _model_url(language: str) -> str:
    """Build Silero model URL for a language. Output: URL string. Input: language code."""
    base = os.getenv(
        "SILERO_MODEL_BASE_URL",
        "https://models.silero.ai/models/tts",
    ).rstrip("/")
    return f"{base}/{language}/{_model_id(language)}.pt"


def _get_model(language: str) -> Any:
    """Load and cache Silero model for a language. Output: Silero model. Input: language code."""
    if language in _MODEL_CACHE:
        return _MODEL_CACHE[language]

    import torch  # type: ignore[import]

    model_dir = _model_dir()
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / f"silero_tts_{_model_id(language)}.pt"

    if not model_path.exists() or model_path.stat().st_size == 0:
        url = _model_url(language)
        timeout = httpx.Timeout(300.0, connect=15.0)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
                model_path.write_bytes(response.content)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Failed to download Silero model {url}: {exc}") from exc

    try:
        device = torch.device("cpu")
        model = torch.package.PackageImporter(str(model_path)).load_pickle("tts_models", "model")
        model.to(device)
        _MODEL_CACHE[language] = model
        return model
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to load Silero model for {language}: {exc}") from exc


def _resolve_language(requested: str | None) -> str:
    """Normalize language code to Silero-supported value. Output: language code. Input: optional code."""
    lang = (requested or _default_language()).lower().split("-")[0]
    if lang not in SUPPORTED_LANGUAGES:
        lang = "ru" if lang in {"ru"} else "en"
    return lang


def _pick_speaker(model: Any, requested: str | None, language: str) -> str:
    """Pick an available speaker id for the model. Output: speaker string. Input: model, requested id, language."""
    speakers = getattr(model, "speakers", None) or []
    normalized_speakers = {str(s).lower(): str(s) for s in speakers}
    configured = (requested or _default_voice() or "").strip().lower()

    if configured and configured in normalized_speakers:
        return normalized_speakers[configured]
    if speakers:
        return speakers[0]
    if language == "ru":
        return "baya"
    return "en_0"


def _pcm_to_wav_b64(samples: "np.ndarray", sample_rate: int) -> str:
    """Convert float PCM array to base64-encoded WAV. Output: base64 string. Input: samples and rate."""
    buf = io.BytesIO()
    pcm_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_int16.tobytes())
    return base64.b64encode(buf.getvalue()).decode()


@app.middleware("http")
async def count_requests(request: Request, call_next):
    """Track HTTP request counters. Output: response. Input: request and next handler."""
    global REQUESTS_TOTAL
    REQUESTS_TOTAL += 1
    REQUESTS_BY_PATH[request.url.path] += 1
    return await call_next(request)


@app.get("/health")
def healthcheck() -> dict[str, str]:
    """Return Silero service health. Output: health dict. Input: none."""
    return {"status": "ok", "service": "silero"}


@app.post("/preload")
def preload_model() -> dict[str, str]:
    """Preload Silero model for configured language. Output: status dict. Input: none."""
    try:
        lang = _default_language()
        model = _get_model(lang)
        speaker = _pick_speaker(model, None, lang)
        return {"status": "ok", "message": f"Silero model preloaded: language={lang}, speaker={speaker}."}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"Failed to preload: {exc}"}


@app.get("/speakers")
def list_speakers() -> dict[str, object]:
    """Return available speakers for configured language. Output: speakers dict. Input: none."""
    lang = _default_language()
    try:
        model = _get_model(lang)
        return {
            "language": lang,
            "default_voice": _default_voice(),
            "speakers": getattr(model, "speakers", []),
        }
    except Exception as exc:  # noqa: BLE001
        return {"language": lang, "error": str(exc), "speakers": []}


@app.get("/metrics")
def metrics() -> dict[str, object]:
    """Return service metrics. Output: metrics dict. Input: none."""
    import resource as _resource

    return {
        "service": "silero",
        "health": "ok",
        "requests_total": REQUESTS_TOTAL,
        "requests_by_path": dict(REQUESTS_BY_PATH),
        "memory_mb": round(_resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2),
        "models_loaded": list(_MODEL_CACHE.keys()),
        "default_language": _default_language(),
        "default_voice": _default_voice(),
    }


@app.post("/synthesize")
def synthesize(request: SynthesisRequest) -> dict[str, object]:
    """Synthesize WAV speech with Silero. Output: audio payload dict. Input: synthesis request."""
    import torch  # type: ignore[import]

    text = (request.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is empty")

    language = _resolve_language(request.language)
    out_sample_rate = request.sample_rate or SAMPLE_RATE
    if out_sample_rate not in {8000, 24000, 48000}:
        out_sample_rate = SAMPLE_RATE

    model = _get_model(language)
    speaker = _pick_speaker(model, request.voice, language)

    try:
        with torch.no_grad():
            audio = model.apply_tts(
                text=text,
                speaker=speaker,
                sample_rate=out_sample_rate,
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Silero synthesis failed: {exc}") from exc

    samples = audio.numpy() if hasattr(audio, "numpy") else np.array(audio)
    audio_b64 = _pcm_to_wav_b64(samples, out_sample_rate)

    return {
        "audio_b64": audio_b64,
        "mime_type": "audio/wav",
        "sample_rate": out_sample_rate,
        "voice": speaker,
        "language": language,
        "provider": "silero",
    }
