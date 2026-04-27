import os
import re
import resource
from collections import defaultdict
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from num2words import num2words
from pydantic import BaseModel, Field


app = FastAPI(title="colloc-tts-router")
REQUESTS_TOTAL = 0
REQUESTS_BY_PATH: dict[str, int] = defaultdict(int)

# Recognised provider names
KNOWN_PROVIDERS = {"piper", "kokoro", "silero", "external"}


class SynthesisRequest(BaseModel):
    text: str = Field(description="Text for synthesis")


# ---------------------------------------------------------------------------
# Per-language config helpers
# Format: TTS_<LANG>_<PRIMARY|FALLBACK>_<PARAM>
# e.g.  TTS_RU_PRIMARY_PROVIDER=piper
#       TTS_RU_PRIMARY_VOICE=ru_RU-irina-medium
#       TTS_RU_PRIMARY_PORT=6011
#       TTS_EN_PRIMARY_PROVIDER=silero
#       TTS_EN_PRIMARY_VOICE=en_0
#       TTS_EN_FALLBACK_PROVIDER=kokoro
# ---------------------------------------------------------------------------

def _env_lang_param(lang: str, tier: str, param: str) -> str:
    """Read per-language TTS env var. Output: value string. Input: lang, tier (PRIMARY|FALLBACK), param."""
    return os.getenv(f"TTS_{lang.upper()}_{tier.upper()}_{param.upper()}", "")


def get_lang_config(lang: str) -> dict[str, Any]:
    """Build per-language provider config. Output: config dict. Input: language code."""
    primary_provider = _env_lang_param(lang, "PRIMARY", "PROVIDER").lower()
    fallback_provider = _env_lang_param(lang, "FALLBACK", "PROVIDER").lower()

    return {
        "primary": {
            "provider": primary_provider or "",
            "voice": _env_lang_param(lang, "PRIMARY", "VOICE"),
            "port": _env_lang_param(lang, "PRIMARY", "PORT"),
            "url": _env_lang_param(lang, "PRIMARY", "URL"),
        },
        "fallback": {
            "provider": fallback_provider or "",
            "voice": _env_lang_param(lang, "FALLBACK", "VOICE"),
            "port": _env_lang_param(lang, "FALLBACK", "PORT"),
            "url": _env_lang_param(lang, "FALLBACK", "URL"),
        },
    }


def list_configured_languages() -> list[str]:
    """Return language codes that have at least a primary provider configured. Output: list. Input: none."""
    langs = set()
    for key in os.environ:
        if not key.startswith("TTS_"):
            continue
        parts = key.split("_")
        # TTS_<LANG>_PRIMARY_PROVIDER  → parts = [TTS, LANG, PRIMARY, PROVIDER]
        if len(parts) >= 4 and parts[2] in {"PRIMARY", "FALLBACK"} and parts[3] == "PROVIDER":
            langs.add(parts[1])
    return sorted(langs)


def get_active_providers() -> set[str]:
    """Return set of provider names referenced by any language config. Output: set of strings. Input: none."""
    active: set[str] = set()
    for lang in list_configured_languages():
        cfg = get_lang_config(lang)
        for tier in ("primary", "fallback"):
            p = cfg[tier]["provider"]
            if p:
                active.add(p)
    return active


# ---------------------------------------------------------------------------
# Service URL resolution per provider
# ---------------------------------------------------------------------------

def _provider_url_for_segment(provider: str, lang: str, tier: str) -> str:
    """Resolve base HTTP URL for a provider instance. Output: URL string. Input: provider, lang, tier."""
    cfg = get_lang_config(lang)[tier]

    if provider == "piper":
        port = cfg.get("port") or ""
        if port:
            return f"http://piper-{lang.lower()}:{port}"
        return ""

    if provider == "kokoro":
        # Kokoro is a shared service; keep one canonical port to avoid per-language drift.
        port = os.getenv("KOKORO_PORT", "6030")
        return f"http://kokoro:{port}"

    if provider == "silero":
        # Silero is a shared service; route through SILERO_PORT only.
        port = os.getenv("SILERO_PORT", "6040")
        return f"http://silero:{port}"

    if provider == "external":
        return (cfg.get("url") or os.getenv("TTS_EXTERNAL_URL", "")).rstrip("/")

    return ""


# ---------------------------------------------------------------------------
# Character-level language classifier
# ---------------------------------------------------------------------------

def classify_character(char: str) -> str:
    """Classify one character. Output: language code. Input: one character."""
    if "A" <= char <= "Z" or "a" <= char <= "z":
        return "EN"
    if "А" <= char <= "я" or char in {"Ё", "ё"}:
        return "RU"
    return "OTHER"


def language_to_speech_locale(language: str) -> str:
    """Map language code to BCP-47 speech locale. Output: locale string. Input: language code."""
    mapping = {"RU": "ru-RU", "EN": "en-US", "DE": "de-DE", "ES": "es-ES", "FR": "fr-FR"}
    return mapping.get(language.upper(), "en-US")


def verbalize_non_letters(text: str, language: str) -> str:
    """Verbalize non-letter tokens by segment language. Output: transformed text. Input: text and language."""
    lang = (language or "EN").upper()
    digit_names = {
        "RU": {
            "0": "ноль",
            "1": "один",
            "2": "два",
            "3": "три",
            "4": "четыре",
            "5": "пять",
            "6": "шесть",
            "7": "семь",
            "8": "восемь",
            "9": "девять",
        },
        "EN": {
            "0": "zero",
            "1": "one",
            "2": "two",
            "3": "three",
            "4": "four",
            "5": "five",
            "6": "six",
            "7": "seven",
            "8": "eight",
            "9": "nine",
        },
    }
    symbol_names = {
        "RU": {
            "*": "звездочка",
            "#": "решетка",
            "+": "плюс",
            "=": "равно",
            "/": "слэш",
            "\\": "обратный слэш",
            "@": "собака",
            "&": "амперсанд",
            "%": "процент",
            "$": "доллар",
            "€": "евро",
            "£": "фунт",
            "§": "параграф",
            "№": "номер",
        },
        "EN": {
            "*": "asterisk",
            "#": "hash",
            "+": "plus",
            "=": "equals",
            "/": "slash",
            "\\": "backslash",
            "@": "at",
            "&": "ampersand",
            "%": "percent",
            "$": "dollar",
            "€": "euro",
            "£": "pound",
            "§": "section",
            "№": "number",
        },
    }
    pause_punctuation = {".", ",", "!", "?", ":", ";", "…"}

    active_digit_names = digit_names.get(lang, digit_names["EN"])
    active_symbol_names = symbol_names.get(lang, symbol_names["EN"])
    num2words_lang = "ru" if lang == "RU" else "en"

    # Normalize inline minus/hyphen usage so TTS reads it as punctuation pause.
    result = re.sub(r"(?<=\S)-(?=\S)", " - ", text)

    # Replace digit runs with language-specific words while preserving original words.
    def _replace_digits(match: re.Match[str]) -> str:
        digits = match.group(0)

        # For 4 or fewer digits, try full number pronunciation.
        # Keep digit-by-digit fallback for unsupported cases and errors.
        if len(digits) <= 4 and not (len(digits) > 1 and digits.startswith("0")):
            try:
                return f" {num2words(int(digits), lang=num2words_lang)} "
            except Exception:
                pass

        return " " + " ".join(active_digit_names.get(ch, ch) for ch in digits) + " "

    result = re.sub(r"\d+", _replace_digits, result)

    # Replace configured symbols with spoken forms, but keep pause punctuation intact.
    escaped_symbols = "".join(re.escape(sym) for sym in active_symbol_names.keys() if sym not in pause_punctuation)
    if escaped_symbols:
        symbol_re = re.compile(f"[{escaped_symbols}]")
        result = symbol_re.sub(lambda m: f" {active_symbol_names.get(m.group(0), m.group(0))} ", result)

    # Normalize extra spaces created by replacements.
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"\s+([.,!?:;…])", r"\1", result)
    return result.strip()


def split_text_by_language(text: str) -> list[dict[str, str]]:
    """Split text into language-homogeneous segments. Output: segment list. Input: source text."""
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

    segments = [s for s in segments if s["text"]]

    # Forward pass: digits/symbols inherit the language of the preceding block.
    last_known = "OTHER"
    for seg in segments:
        if seg["language"] != "OTHER":
            last_known = seg["language"]
        elif last_known != "OTHER":
            seg["language"] = last_known

    # Backward pass: leading digits/symbols inherit language from the first following block.
    last_known_rev = "OTHER"
    for seg in reversed(segments):
        if seg["language"] != "OTHER":
            last_known_rev = seg["language"]
        elif last_known_rev != "OTHER":
            seg["language"] = last_known_rev

    # Merge adjacent same-language segments produced by the resolution above.
    merged: list[dict[str, str]] = []
    for seg in segments:
        if merged and merged[-1]["language"] == seg["language"]:
            merged[-1]["text"] = merged[-1]["text"] + " " + seg["text"]
        else:
            merged.append({"language": seg["language"], "text": seg["text"]})

    return merged


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


# ---------------------------------------------------------------------------
# Provider call dispatch
# ---------------------------------------------------------------------------

async def call_provider(provider: str, segment: dict[str, Any], tier: str = "primary") -> dict[str, Any]:
    """Call one TTS provider for a text segment. Output: provider result dict. Input: provider id, segment, tier."""
    timeout = httpx.Timeout(60.0, connect=5.0)
    lang = segment.get("language", "EN")
    base_url = _provider_url_for_segment(provider, lang, tier)
    voice = get_lang_config(lang)[tier].get("voice") or ""

    if provider == "piper":
        if not base_url:
            raise HTTPException(status_code=500, detail=f"Piper URL not configured for language {lang}")
        payload = {"text": segment["text"], "voice": voice or None}
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url}/synthesize", json=payload)
            response.raise_for_status()
            data = response.json()
            data["provider"] = "piper"
            return data

    if provider == "kokoro":
        payload = {"text": segment["text"], "voice": voice or None, "language": segment.get("locale", "en-US")}
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url}/synthesize", json=payload)
            response.raise_for_status()
            data = response.json()
            data["provider"] = "kokoro"
            return data

    if provider == "silero":
        payload = {"text": segment["text"], "voice": voice or None, "language": lang.lower()}
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url}/synthesize", json=payload)
            response.raise_for_status()
            data = response.json()
            data["provider"] = "silero"
            return data

    if provider == "external":
        if not base_url:
            raise HTTPException(status_code=500, detail="TTS_EXTERNAL_URL is not configured")
        payload = {"text": segment["text"], "language": segment.get("locale", "en-US")}
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(base_url, json=payload)
            response.raise_for_status()
            data = response.json()
            data["provider"] = "external"
            return data

    raise HTTPException(status_code=500, detail=f"Unknown TTS provider: {provider}")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

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
        "languages": list_configured_languages(),
        "active_providers": sorted(get_active_providers()),
    }


@app.get("/voices")
def list_voices() -> dict[str, object]:
    """Return per-language voice configuration. Output: config dict. Input: none."""
    langs = list_configured_languages()
    return {
        "languages": {lang: get_lang_config(lang) for lang in langs},
        "active_providers": sorted(get_active_providers()),
    }


@app.post("/synthesize")
async def synthesize(request: SynthesisRequest) -> dict[str, object]:
    """Synthesize speech for all languages using per-lang provider config. Output: audio segments dict. Input: synthesis request."""
    configured_langs = set(list_configured_languages())
    raw_segments = split_text_by_language(request.text)

    # Map each detected language to a configured language (fallback to EN)
    planned_segments = []
    for seg in raw_segments:
        lang = seg["language"] if seg["language"] in configured_langs else "EN"
        cfg = get_lang_config(lang)
        normalized_text = verbalize_non_letters(seg["text"], lang)
        planned_segments.append(
            {
                "language": lang,
                "locale": language_to_speech_locale(lang),
                "text": normalized_text,
                "primary_provider": cfg["primary"]["provider"],
                "fallback_provider": cfg["fallback"]["provider"],
            }
        )

    synthesized_segments: list[dict[str, Any]] = []
    provider_errors: list[dict[str, str]] = []
    used_fallback = False

    for segment in planned_segments:
        primary = segment["primary_provider"]
        fallback = segment["fallback_provider"]

        chosen_provider = primary
        chosen_tier = "primary"
        try:
            result = await call_provider(primary, segment, tier="primary")
        except Exception as primary_exc:  # noqa: BLE001
            if fallback and fallback != primary:
                chosen_provider = fallback
                chosen_tier = "fallback"
                used_fallback = True
                try:
                    result = await call_provider(fallback, segment, tier="fallback")
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
                "tier": chosen_tier,
                "voice": result.get("voice", ""),
                "mime_type": result.get("mime_type", "audio/wav"),
                "audio_b64": result.get("audio_b64", ""),
                "sample_rate": result.get("sample_rate"),
            }
        )

    if not synthesized_segments:
        raise HTTPException(status_code=502, detail=f"No TTS segments synthesized: {provider_errors}")

    # Derive top-level provider for response summary
    used_providers = list(dict.fromkeys(s["provider"] for s in synthesized_segments))
    return {
        "mode": "server_audio",
        "provider": used_providers[0] if used_providers else "",
        "providers": used_providers,
        "fallback_used": used_fallback,
        "segments": synthesized_segments,
        "errors": provider_errors,
    }
