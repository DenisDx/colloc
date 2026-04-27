"""SIP media service for Asterisk ARI External Media.

Implements full-duplex RTP processing, basic VAD, STT -> LLM -> TTS pipeline,
and barge-in while assistant playback is active.
"""

import asyncio
import audioop
import base64
import io
import json
import logging
import os
import random
import re
import unicodedata
import wave
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from services.common.llm_runtime import ChatRuntimeConfig, InvocationContext, fetch_enabled_tools, run_chat_with_tools, load_prompt_by_source, load_greeting_by_source
from services.common.media_clients import call_stt_service, call_tts_service
from services.common.runtime_llm_config import get_runtime_llm_config
from services.common.system_log import append_system_log


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="colloc-sip-service")

# Cache for last STT audio sent to web UI
_last_stt_audio_cache: dict[str, tuple[bytes, str]] = {}  # wav_bytes, transcript


def cache_stt_audio(wav_bytes: bytes, transcript: str) -> None:
    """Cache STT audio for web UI retrieval. Output: none. Input: wav bytes and transcript text."""
    _last_stt_audio_cache["last"] = (wav_bytes, transcript)


SIP_ROLE = os.getenv("SIP_ROLE", "ai_scripts/test.md")
SIP_GREETINGS = os.getenv("SIP_GREETINGS", "ai_scripts/greetings.md")
SIP_DEFAULT_LANGUAGE = os.getenv("SIP_DEFAULT_LANGUAGE", "ru")
SIP_MAX_SILENCE = int(os.getenv("SIP_MAX_SILENCE", "30"))
SIP_MAX_DURATION = int(os.getenv("SIP_MAX_DURATION", "600"))
SIP_VAD_THRESHOLD = int(os.getenv("SIP_VAD_THRESHOLD", "450"))
SIP_ACTIVITY_MIN_ENERGY = int(os.getenv("SIP_ACTIVITY_MIN_ENERGY", str(max(1, SIP_VAD_THRESHOLD))))
SIP_BARGE_IN_THRESHOLD = int(os.getenv("SIP_BARGE_IN_THRESHOLD", "700"))
SIP_UTTERANCE_MIN_MS = int(os.getenv("SIP_UTTERANCE_MIN_MS", "350"))
SIP_UTTERANCE_END_SILENCE_MS = int(os.getenv("SIP_UTTERANCE_END_SILENCE_MS", "850"))
SIP_VAD_ADAPTIVE_DECAY = float(os.getenv("SIP_VAD_ADAPTIVE_DECAY", "0.01"))
SIP_VAD_ADAPTIVE_RECOVERY = float(os.getenv("SIP_VAD_ADAPTIVE_RECOVERY", "0.02"))
SIP_VAD_ADAPTIVE_MIN_FACTOR = float(os.getenv("SIP_VAD_ADAPTIVE_MIN_FACTOR", "0.45"))
SIP_BARGE_ADAPTIVE_MIN_FACTOR = float(os.getenv("SIP_BARGE_ADAPTIVE_MIN_FACTOR", "0.45"))

STT_URL = os.getenv("STT_URL", "http://stt:8001")
TTS_ROUTER_URL = os.getenv("TTS_ROUTER_URL", "http://tts-router:8002")
LLM_TIMEOUT_SEC = float(os.getenv("LLM_REQUEST_TIMEOUT_SEC", "60"))

ASTERISK_HTTP_URL = os.getenv("ASTERISK_HTTP_URL", "http://asterisk:8088/ari").rstrip("/")
ASTERISK_ARI_USER = os.getenv("ASTERISK_ARI_USER", "colloc")
ASTERISK_ARI_PASSWORD = os.getenv("ASTERISK_ARI_PASSWORD", "change-me")

BARGE_IN_MIN_MS = 100
BARGE_IN_MIN_FRAMES = max(1, BARGE_IN_MIN_MS // 20)
BARGE_IN_REEVALUATE_FRAMES = 5
BARGE_IN_STOP_PHRASES = {"stop", "стоп", "выключить", "прервать", "turn off"}
BARGE_IN_WORD_RE = re.compile(r"[\w\u0400-\u04FF\u0E00-\u0E7F']+", re.UNICODE)
BARGE_IN_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
BARGE_IN_THAI_RE = re.compile(r"[\u0E00-\u0E7F]")


class CallStartRequest(BaseModel):
    """Input model for starting SIP call media session."""

    channel_id: str
    caller: str = "unknown"
    media_port: int
    language: str = SIP_DEFAULT_LANGUAGE
    asterisk_rtp_host: str | None = None
    asterisk_rtp_port: int | None = None


class CallEndRequest(BaseModel):
    """Input model for ending SIP call media session."""

    channel_id: str
    reason: str | None = None
    source: str | None = None
    cause: int | None = None
    cause_txt: str | None = None
    ari_channel_id: str | None = None
    bridge_id: str | None = None
    details: dict[str, Any] | None = None


class BargeInRequest(BaseModel):
    """Input model for forcing barge-in interrupt."""

    channel_id: str


class SipHangupRequest(BaseModel):
    """Input model for hanging up a SIP call from tools."""

    channel_id: str


class SipTransferRequest(BaseModel):
    """Input model for blind-transferring a SIP call to another extension."""

    channel_id: str
    target: str

class RtpEndpoint(asyncio.DatagramProtocol):
    """Bidirectional RTP endpoint for one External Media channel."""

    def __init__(self, call: "CallSession") -> None:
        self.call = call
        self.transport: asyncio.DatagramTransport | None = None
        self.remote_addr: tuple[str, int] | None = None
        self.tx_seq = random.randint(0, 65535)
        self.tx_ts = random.randint(0, 2**31)
        self.tx_ssrc = random.randint(1, 2**31 - 1)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Store transport when UDP endpoint starts. Output: none. Input: transport."""
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Process inbound RTP packet. Output: none. Input: packet bytes and source."""
        if len(data) < 12:
            return
        self.remote_addr = addr
        if not self.call.rtp_ready_event.is_set():
            self.call.rtp_ready_event.set()

        payload_type = data[1] & 0x7F
        self.call.rx_total_packets += 1
        self.call.rx_last_payload_type = payload_type

        cc = data[0] & 0x0F
        has_ext = (data[0] & 0x10) != 0
        header_len = 12 + (cc * 4)
        if len(data) < header_len:
            return

        if has_ext:
            if len(data) < header_len + 4:
                return
            ext_words = int.from_bytes(data[header_len + 2 : header_len + 4], "big")
            header_len += 4 + (ext_words * 4)
            if len(data) < header_len:
                return

        payload = data[header_len:]
        if not payload:
            return

        try:
            # External Media is expected to be ulaw, but callers can negotiate alaw.
            # Decode by RTP payload type to keep VAD/STT functional for both codecs.
            if payload_type == 0:
                pcm16 = audioop.ulaw2lin(payload, 2)
            elif payload_type == 8:
                pcm16 = audioop.alaw2lin(payload, 2)
            elif payload_type in {13}:
                return
            else:
                self.call.rx_unknown_pt_packets += 1
                pcm16 = audioop.ulaw2lin(payload, 2)
        except Exception:
            return

        self.call.rx_audio_packets += 1
        energy = audioop.rms(pcm16, 2) if pcm16 else 0
        self.call.rx_last_energy = energy
        if energy > self.call.rx_peak_energy:
            self.call.rx_peak_energy = energy
        if self.call.rx_total_packets % 200 == 0:
            logger.info(
                "RTP stats call=%s packets=%s audio=%s unknown_pt=%s last_pt=%s last_energy=%s peak_energy=%s",
                self.call.channel_id,
                self.call.rx_total_packets,
                self.call.rx_audio_packets,
                self.call.rx_unknown_pt_packets,
                payload_type,
                energy,
                self.call.rx_peak_energy,
            )
        self.call.on_inbound_pcm_frame(pcm16, energy)

    def send_ulaw_payload(self, payload: bytes) -> bool:
        """Send one RTP payload to remote endpoint. Output: bool. Input: ulaw payload."""
        if not self.transport or not self.remote_addr:
            return False

        self.tx_seq = (self.tx_seq + 1) % 65536
        self.tx_ts = (self.tx_ts + len(payload)) % (2**32)

        header = bytearray(12)
        header[0] = 0x80
        header[1] = 0x00
        header[2:4] = self.tx_seq.to_bytes(2, "big")
        header[4:8] = self.tx_ts.to_bytes(4, "big")
        header[8:12] = self.tx_ssrc.to_bytes(4, "big")

        packet = bytes(header) + payload
        self.transport.sendto(packet, self.remote_addr)
        return True


@dataclass
class CallSession:
    """Runtime state for one SIP call."""

    channel_id: str
    caller: str
    language: str
    media_port: int
    role_prompt: str
    greeting_text: str
    greeting_wav: bytes | None
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_activity_at: datetime = field(default_factory=datetime.utcnow)
    frame_queue: asyncio.Queue[tuple[bytes, int] | None] = field(default_factory=asyncio.Queue)
    history: list[dict[str, Any]] = field(default_factory=list)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted_event: asyncio.Event = field(default_factory=asyncio.Event)
    rtp_ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    pipeline_task: asyncio.Task[None] | None = None
    watchdog_task: asyncio.Task[None] | None = None
    playback_task: asyncio.Task[None] | None = None
    rtp_protocol: RtpEndpoint | None = None
    rtp_transport: asyncio.DatagramTransport | None = None
    rx_total_packets: int = 0
    rx_audio_packets: int = 0
    rx_last_payload_type: int = -1
    rx_peak_energy: int = 0
    rx_last_energy: int = 0
    rx_unknown_pt_packets: int = 0
    adaptive_vad_factor: float = 1.0
    adaptive_barge_factor: float = 1.0
    barge_chunks: list[bytes] = field(default_factory=list)
    barge_voiced_frames: int = 0
    barge_silence_frames: int = 0
    barge_last_eval_frames: int = 0
    barge_eval_task: asyncio.Task[None] | None = None
    stop_reason: str | None = None
    stop_reason_details: dict[str, Any] = field(default_factory=dict)

    def mark_activity(self) -> None:
        """Update last activity time. Output: none. Input: none."""
        self.last_activity_at = datetime.utcnow()

    def is_playing(self) -> bool:
        """Check whether any playback is active. Output: bool. Input: none."""
        return bool(self.playback_task and not self.playback_task.done())

    def current_vad_threshold(self) -> int:
        """Get current adaptive VAD threshold. Output: threshold int. Input: none."""
        return max(40, int(SIP_VAD_THRESHOLD * self.adaptive_vad_factor))

    def current_barge_threshold(self) -> int:
        """Get current adaptive barge-in threshold. Output: threshold int. Input: none."""
        return max(60, int(SIP_BARGE_IN_THRESHOLD * self.adaptive_barge_factor))

    def is_voice_activity(self, energy: int) -> bool:
        """Check if frame energy should reset silence timer. Output: bool. Input: frame RMS energy."""
        activity_threshold = max(self.current_vad_threshold(), SIP_ACTIVITY_MIN_ENERGY)
        return energy >= activity_threshold

    def adapt_thresholds(self, energy: int) -> None:
        """Adapt VAD/barge thresholds based on current frame energy. Output: none. Input: RMS energy."""
        vad_now = self.current_vad_threshold()
        barge_now = self.current_barge_threshold()

        if energy >= barge_now:
            self.adaptive_vad_factor = min(1.0, self.adaptive_vad_factor + SIP_VAD_ADAPTIVE_RECOVERY)
            self.adaptive_barge_factor = min(1.0, self.adaptive_barge_factor + SIP_VAD_ADAPTIVE_RECOVERY)
            return

        if energy >= vad_now:
            self.adaptive_vad_factor = min(1.0, self.adaptive_vad_factor + (SIP_VAD_ADAPTIVE_RECOVERY * 0.5))
            self.adaptive_barge_factor = min(1.0, self.adaptive_barge_factor + (SIP_VAD_ADAPTIVE_RECOVERY * 0.5))
            return

        self.adaptive_vad_factor = max(
            SIP_VAD_ADAPTIVE_MIN_FACTOR,
            self.adaptive_vad_factor - SIP_VAD_ADAPTIVE_DECAY,
        )
        self.adaptive_barge_factor = max(
            SIP_BARGE_ADAPTIVE_MIN_FACTOR,
            self.adaptive_barge_factor - SIP_VAD_ADAPTIVE_DECAY,
        )

    def on_inbound_pcm_frame(self, pcm16: bytes, energy: int) -> None:
        """Push inbound frame and trigger barge-in if needed. Output: none. Input: frame+energy."""
        self.adapt_thresholds(energy)

        try:
            self.frame_queue.put_nowait((pcm16, energy))
        except asyncio.QueueFull:
            return

        if self.is_voice_activity(energy):
            self.mark_activity()

        self.observe_barge_candidate(pcm16, energy)

    def observe_barge_candidate(self, pcm16: bytes, energy: int) -> None:
        """Collect candidate speech during playback for transcript-gated barge-in. Output: none. Input: frame and energy."""
        if not self.is_playing() or self.interrupted_event.is_set():
            self.reset_barge_candidate(cancel_task=True)
            return

        is_voiced = energy >= self.current_barge_threshold()
        if not self.barge_chunks and not is_voiced:
            return

        self.barge_chunks.append(pcm16)
        if is_voiced:
            self.barge_voiced_frames += 1
            self.barge_silence_frames = 0
        else:
            self.barge_silence_frames += 1

        should_evaluate = (
            self.barge_voiced_frames >= BARGE_IN_MIN_FRAMES
            and self.barge_voiced_frames - self.barge_last_eval_frames >= BARGE_IN_REEVALUATE_FRAMES
            and (self.barge_eval_task is None or self.barge_eval_task.done())
        )
        if should_evaluate:
            self.barge_last_eval_frames = self.barge_voiced_frames
            snapshot = b"".join(self.barge_chunks)
            self.barge_eval_task = asyncio.create_task(evaluate_barge_in_candidate(self, snapshot))

        if self.barge_silence_frames >= max(1, SIP_UTTERANCE_END_SILENCE_MS // 20):
            self.reset_barge_candidate(cancel_task=False)

    def reset_barge_candidate(self, cancel_task: bool) -> None:
        """Clear temporary barge-in candidate state. Output: none. Input: cancel active eval task flag."""
        self.barge_chunks = []
        self.barge_voiced_frames = 0
        self.barge_silence_frames = 0
        self.barge_last_eval_frames = 0
        if cancel_task and self.barge_eval_task and not self.barge_eval_task.done():
            self.barge_eval_task.cancel()
        if self.barge_eval_task and self.barge_eval_task.done():
            self.barge_eval_task = None


active_calls: dict[str, CallSession] = {}
active_calls_lock = asyncio.Lock()


def _read_text_file(path_value: str, fallback: str) -> str:
    """Read UTF-8 text file with fallback. Output: text. Input: path and fallback."""
    path = Path(path_value)
    if not path.exists():
        return fallback
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return fallback


def load_role_prompt() -> str:
    """Load SIP role prompt from source-aware config. Output: prompt text. Input: none."""
    return load_prompt_by_source("sip", "You are a helpful AI assistant on a phone call. Keep responses concise and natural.")


def load_greeting() -> tuple[str, bytes | None]:
    """Load greeting file (text or WAV) from source-aware config. Output: text or wav bytes. Input: none."""
    return load_greeting_by_source("sip", "Привет! Я на связи. Чем могу помочь?")


def pcm16_to_wav_bytes(pcm16: bytes, sample_rate: int) -> bytes:
    """Encode PCM16 into WAV. Output: wav bytes. Input: pcm16 bytes and sample rate."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_out:
        wav_out.setnchannels(1)
        wav_out.setsampwidth(2)
        wav_out.setframerate(sample_rate)
        wav_out.writeframes(pcm16)
    return buf.getvalue()


def wav_bytes_to_ulaw_frames(wav_bytes: bytes) -> list[bytes]:
    """Convert WAV to ulaw RTP payload frames (20ms). Output: frame list. Input: wav bytes."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_in:
        channels = wav_in.getnchannels()
        width = wav_in.getsampwidth()
        src_rate = wav_in.getframerate()
        frames = wav_in.readframes(wav_in.getnframes())

    if channels != 1:
        frames = audioop.tomono(frames, width, 0.5, 0.5)
    if width != 2:
        frames = audioop.lin2lin(frames, width, 2)
    if src_rate != 8000:
        frames, _ = audioop.ratecv(frames, 2, 1, src_rate, 8000, None)

    ulaw = audioop.lin2ulaw(frames, 2)
    chunk_size = 160
    chunks = [ulaw[i : i + chunk_size] for i in range(0, len(ulaw), chunk_size)]
    return [c for c in chunks if c]


def detect_barge_language(text: str, fallback_language: str) -> str:
    """Detect transcript language family for interruption rules. Output: rule language code. Input: transcript and fallback."""
    if BARGE_IN_CJK_RE.search(text):
        return "zh"
    if BARGE_IN_THAI_RE.search(text):
        return "th"
    normalized = (fallback_language or "").lower()
    if normalized.startswith("zh"):
        return "zh"
    if normalized.startswith("th"):
        return "th"
    if re.search(r"[\u0400-\u04FF]", text):
        return "ru"
    return "en"


def count_meaningful_characters(text: str, language: str) -> int:
    """Count meaningful transcript characters for interruption rules. Output: character count. Input: text and language."""
    if language == "zh":
        return len(BARGE_IN_CJK_RE.findall(text))
    if language == "th":
        return len(BARGE_IN_THAI_RE.findall(text))
    return sum(1 for char in text if unicodedata.category(char).startswith(("L", "N")))


def count_words_for_barge(text: str, language: str) -> int:
    """Count words for interruption rules. Output: word count. Input: transcript and language."""
    if language == "zh":
        return 0
    return len(BARGE_IN_WORD_RE.findall(text.lower()))


def should_interrupt_from_transcript(text: str, fallback_language: str) -> tuple[bool, dict[str, Any]]:
    """Apply SPEC interruption rules to transcript text. Output: decision and diagnostics. Input: transcript and fallback language."""
    normalized_text = " ".join(text.lower().split())
    if not normalized_text:
        return False, {"reason": "empty"}

    words = BARGE_IN_WORD_RE.findall(normalized_text)
    if any(stop_word in words for stop_word in {"stop", "стоп", "выключить", "прервать"}) or "turn off" in normalized_text:
        return True, {"reason": "stop_phrase"}

    language = detect_barge_language(text, fallback_language)
    char_count = count_meaningful_characters(text, language)
    word_count = count_words_for_barge(text, language)

    min_chars = 10
    require_words = True
    if language == "zh":
        min_chars = 2
        require_words = False
    elif language == "th":
        min_chars = 5

    chars_ok = char_count > min_chars if language in {"en", "ru"} else char_count >= min_chars
    words_ok = (word_count >= 2) if require_words else True
    return chars_ok and words_ok, {
        "reason": "rule_match" if chars_ok and words_ok else "rule_reject",
        "language": language,
        "chars": char_count,
        "words": word_count,
        "min_chars": min_chars,
        "require_words": require_words,
    }


async def evaluate_barge_in_candidate(call: CallSession, pcm8k: bytes) -> None:
    """Run STT for active playback speech and interrupt only on SPEC match. Output: none. Input: call and PCM audio."""
    if not pcm8k or not call.is_playing() or call.interrupted_event.is_set():
        return

    details = {
        "channel_id": call.channel_id,
        "bytes": len(pcm8k),
        "voiced_frames": call.barge_voiced_frames,
    }
    append_system_log("sip", "barge_stt_start", "Barge-in candidate STT started.", details)
    try:
        transcript = await call_stt_from_pcm8k(pcm8k, None, source="barge")
    except Exception:
        logger.exception("Barge-in STT failed for call %s", call.channel_id)
        append_system_log("sip", "barge_stt_error", "Barge-in candidate STT failed.", details)
        return

    decision, rule_details = should_interrupt_from_transcript(transcript, call.language)
    log_details = {**details, **rule_details, "transcript": transcript}
    append_system_log(
        "sip",
        "barge_stt_result",
        "Barge-in candidate transcript accepted." if decision else "Barge-in candidate transcript rejected.",
        log_details,
    )
    if not decision or not call.is_playing() or call.interrupted_event.is_set():
        return

    call.interrupted_event.set()
    append_system_log("sip", "barge_interrupt", "Playback interrupted by caller speech.", log_details)
    if call.playback_task and not call.playback_task.done():
        call.playback_task.cancel()


async def call_stt_from_pcm8k(pcm8k: bytes, language_hint: str | None, source: str = "turn") -> str:
    """Transcribe PCM audio using STT service. Output: transcript text. Input: pcm and optional language hint."""
    if not pcm8k:
        return ""
    pcm16k, _ = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)
    wav_bytes = pcm16_to_wav_bytes(pcm16k, 16000)
    payload = {
        "audio_b64": base64.b64encode(wav_bytes).decode("ascii"),
        "mime_type": "audio/wav",
        "partial": False,
        "language_hint": language_hint,
    }
    append_system_log(
        "sip",
        "stt_start",
        "STT request started.",
        {"channel_id": source if source.startswith("chan:") else None, "source": source, "bytes": len(pcm8k), "language": language_hint},
    )
    body = await call_stt_service(payload["audio_b64"], payload["mime_type"], language_hint, stt_url=STT_URL, timeout_sec=120.0)
    transcript = (body.get("transcript") or "").strip()
    # Cache audio for web UI playback
    cache_stt_audio(wav_bytes, transcript)
    append_system_log(
        "sip",
        "stt_stop",
        "STT request finished.",
        {"source": source, "language": language_hint, "text": transcript},
    )
    return transcript


_SENTENCE_END_RE = re.compile(r"[.!?\n]")


def _split_on_sentence_boundary(buffer: str) -> tuple[list[str], str]:
    """Extract complete sentences from a text buffer. Output: (sentences, remainder). Input: accumulated LLM text."""
    sentences: list[str] = []
    while True:
        m = _SENTENCE_END_RE.search(buffer)
        if not m:
            break
        sentence = buffer[: m.end()].strip()
        if sentence:
            sentences.append(sentence)
        buffer = buffer[m.end() :].lstrip()
    return sentences, buffer


async def stream_llm_chunks(messages: list[dict[str, Any]], call: CallSession | None = None):
    """Stream LLM response as raw text chunks via OpenAI-compatible SSE. Output: async generator of str. Input: messages."""
    runtime_config = await get_runtime_llm_config()
    if not runtime_config.primary_base_url or not runtime_config.primary_model:
        yield "LLM provider is not configured."
        return

    context = InvocationContext(
        source="sip",
        call_id=call.channel_id if call else None,
        language=call.language if call else None,
    )

    def _log_sip_event(event: str, message: str, details: dict[str, Any] | None = None) -> None:
        append_system_log("sip", event, message, details)

    tools = await fetch_enabled_tools(context, _log_sip_event)
    tool_names = [
        tool.get("function", {}).get("name", "")
        for tool in tools
        if tool.get("function", {}).get("name")
    ]
    append_system_log(
        "sip",
        "llm_start",
        "LLM stream started.",
        {
            "model": runtime_config.primary_model,
            "messages": len(messages),
            "tools_enabled": bool(tools),
            "tools_count": len(tools),
            "available_tools": tool_names,
        },
    )
    try:
        result = await run_chat_with_tools(
            messages,
            ChatRuntimeConfig(
                base_url=runtime_config.primary_base_url,
                model=runtime_config.primary_model,
                timeout_sec=LLM_TIMEOUT_SEC,
                fallback_base_url=runtime_config.fallback_base_url,
                fallback_model=runtime_config.fallback_model,
            ),
            context,
            _log_sip_event,
            tools=tools,
        )
        if result.text:
            yield result.text
        append_system_log("sip", "llm_stop", "LLM stream finished.", {"model": result.model_used, "text": result.text})
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM stream failed for model %s", runtime_config.primary_model)
        append_system_log(
            "sip",
            "llm_error",
            "LLM stream failed.",
            {"model": runtime_config.primary_model, "error": str(exc)},
        )
        yield "Извините, не удалось обработать запрос."


async def call_tts(text: str, language: str) -> list[bytes]:
    """Synthesize text via TTS router. Output: list of wav segments. Input: text and language."""
    if not text.strip():
        return []
    timeout = httpx.Timeout(40.0, connect=3.0)
    payload = {"text": text, "language": language}

    append_system_log(
        "sip",
        "tts_start",
        "TTS request started.",
        {"language": language, "text": text},
    )

    data = await call_tts_service(text, language, tts_router_url=TTS_ROUTER_URL, timeout_sec=40.0)

    segments = data.get("segments") or []
    wavs: list[bytes] = []

    if data.get("audio_b64"):
        wavs.append(base64.b64decode(data["audio_b64"]))
        append_system_log(
            "sip",
            "tts_stop",
            "TTS request finished.",
            {"language": language, "segments": len(wavs), "text": text},
        )
        return wavs

    for segment in segments:
        audio_b64 = segment.get("audio_b64")
        if audio_b64:
            wavs.append(base64.b64decode(audio_b64))
    append_system_log(
        "sip",
        "tts_stop",
        "TTS request finished.",
        {"language": language, "segments": len(wavs), "text": text},
    )
    return wavs


async def ari_hangup_channel(channel_id: str) -> None:
    """Hang up channel in Asterisk ARI. Output: none. Input: channel id."""
    url = f"{ASTERISK_HTTP_URL}/channels/{channel_id}"
    async with httpx.AsyncClient(auth=(ASTERISK_ARI_USER, ASTERISK_ARI_PASSWORD), timeout=8.0) as client:
        try:
            await client.delete(url)
        except Exception:
            logger.exception("Failed to hang up channel %s", channel_id)


async def play_wav_segments(call: CallSession, wav_segments: list[bytes]) -> bool:
    """Play synthesized WAV segments over RTP. Output: interrupted flag. Input: call + wav list."""
    if not call.rtp_protocol:
        return False

    interrupted = False
    call.interrupted_event.clear()
    warned_no_remote = False
    call.reset_barge_candidate(cancel_task=False)

    for wav_bytes in wav_segments:
        frames = wav_bytes_to_ulaw_frames(wav_bytes)
        for payload in frames:
            if call.stop_event.is_set() or call.interrupted_event.is_set():
                interrupted = True
                break
            ok = call.rtp_protocol.send_ulaw_payload(payload)
            if ok:
                call.mark_activity()
            elif not warned_no_remote:
                logger.warning("RTP remote target is not known yet for call %s", call.channel_id)
                warned_no_remote = True
            await asyncio.sleep(0.02)
        if interrupted:
            break

    call.reset_barge_candidate(cancel_task=False)
    return interrupted


async def speak_text(call: CallSession, text: str, interrupted_mark_target: dict[str, Any] | None = None) -> None:
    """Synthesize and play text to caller. Output: none. Input: call, text, optional history entry."""
    wav_segments = await call_tts(text, call.language)
    if not wav_segments:
        return
    interrupted = await play_wav_segments(call, wav_segments)
    if interrupted and interrupted_mark_target is not None:
        interrupted_mark_target["interrupted"] = True


async def process_utterance(call: CallSession, utterance_pcm8k: bytes) -> None:
    """Run one STT->LLM->TTS turn; TTS starts on first complete sentence. Output: none. Input: utterance PCM bytes."""
    try:
        transcript = await call_stt_from_pcm8k(utterance_pcm8k, None, source="turn")
    except Exception:
        logger.exception("STT failed for call %s", call.channel_id)
        return

    if not transcript:
        return

    call.history.append({"role": "user", "content": transcript})

    messages: list[dict[str, Any]] = []
    if call.role_prompt:
        messages.append({"role": "system", "content": call.role_prompt})
    messages.extend(call.history)

    # Pipeline: LLM stream → sentence split → TTS synthesis → RTP playback.
    # TTS for the first sentence starts as soon as the first sentence boundary arrives.
    tts_wav_queue: asyncio.Queue[list[bytes] | None] = asyncio.Queue()
    all_sentences: list[str] = []

    async def _llm_to_tts_producer() -> None:
        """Stream LLM, split into sentences, synthesize TTS, enqueue WAV segments."""
        buffer = ""
        try:
            async for chunk in stream_llm_chunks(messages, call=call):
                if call.stop_event.is_set() or call.interrupted_event.is_set():
                    break
                buffer += chunk
                sentences, buffer = _split_on_sentence_boundary(buffer)
                for sentence in sentences:
                    if call.stop_event.is_set() or call.interrupted_event.is_set():
                        break
                    all_sentences.append(sentence)
                    try:
                        wavs = await call_tts(sentence, call.language)
                    except Exception:
                        logger.exception("TTS failed for sentence in call %s", call.channel_id)
                        wavs = []
                    if wavs:
                        tts_wav_queue.put_nowait(wavs)
            # flush any text remaining after the last boundary
            remainder = buffer.strip()
            if remainder and not call.stop_event.is_set() and not call.interrupted_event.is_set():
                all_sentences.append(remainder)
                try:
                    wavs = await call_tts(remainder, call.language)
                except Exception:
                    logger.exception("TTS flush failed in call %s", call.channel_id)
                    wavs = []
                if wavs:
                    tts_wav_queue.put_nowait(wavs)
        except asyncio.CancelledError:
            pass
        finally:
            tts_wav_queue.put_nowait(None)

    async def _streaming_playback() -> None:
        """Drain TTS WAV queue and play segments; cancel producer on finish/interrupt."""
        producer_task = asyncio.create_task(_llm_to_tts_producer())
        try:
            while True:
                wavs = await tts_wav_queue.get()
                if wavs is None:
                    break
                if call.stop_event.is_set() or call.interrupted_event.is_set():
                    break
                seg_interrupted = await play_wav_segments(call, wavs)
                if seg_interrupted:
                    break
        finally:
            if not producer_task.done():
                producer_task.cancel()

    playback_task = asyncio.create_task(_streaming_playback())
    call.playback_task = playback_task
    interrupted = False
    try:
        await playback_task
    except asyncio.CancelledError:
        interrupted = True
    finally:
        call.playback_task = None

    answer = " ".join(all_sentences)
    assistant_item: dict[str, Any] = {"role": "assistant", "content": answer, "interrupted": interrupted}
    call.history.append(assistant_item)


async def call_pipeline_loop(call: CallSession) -> None:
    """Collect RTP frames into utterances and process turns. Output: none. Input: call context."""
    frame_ms = 20
    min_frames = max(1, SIP_UTTERANCE_MIN_MS // frame_ms)
    end_silence_frames = max(1, SIP_UTTERANCE_END_SILENCE_MS // frame_ms)

    collecting = False
    voiced_frames = 0
    silence_frames = 0
    chunks: list[bytes] = []

    # Wait for first inbound RTP packet so remote_addr is known before sending audio.
    try:
        await asyncio.wait_for(call.rtp_ready_event.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("RTP not ready within 10s for call %s, proceeding anyway", call.channel_id)

    if call.greeting_wav:
        append_system_log("sip", "greeting_start", "Greeting playback started.", {"channel_id": call.channel_id, "mode": "wav"})
        call.playback_task = asyncio.create_task(play_wav_segments(call, [call.greeting_wav]))
        try:
            greeting_interrupted = await call.playback_task
            append_system_log(
                "sip",
                "greeting_stop",
                "Greeting playback finished.",
                {"channel_id": call.channel_id, "mode": "wav", "interrupted": bool(greeting_interrupted)},
            )
        except asyncio.CancelledError:
            append_system_log("sip", "greeting_stop", "Greeting playback cancelled.", {"channel_id": call.channel_id, "mode": "wav"})
        finally:
            call.playback_task = None
    elif call.greeting_text:
        append_system_log("sip", "greeting_start", "Greeting playback started.", {"channel_id": call.channel_id, "mode": "tts"})
        call.playback_task = asyncio.create_task(speak_text(call, call.greeting_text))
        try:
            await call.playback_task
            append_system_log("sip", "greeting_stop", "Greeting playback finished.", {"channel_id": call.channel_id, "mode": "tts", "interrupted": call.interrupted_event.is_set()})
        except asyncio.CancelledError:
            append_system_log("sip", "greeting_stop", "Greeting playback cancelled.", {"channel_id": call.channel_id, "mode": "tts"})
        finally:
            call.playback_task = None

    while not call.stop_event.is_set():
        item = await call.frame_queue.get()
        if item is None:
            break

        pcm, energy = item
        is_voiced = call.is_voice_activity(energy)

        if is_voiced:
            call.mark_activity()

        if not collecting and not is_voiced:
            continue

        if not collecting and is_voiced:
            collecting = True
            voiced_frames = 0
            silence_frames = 0
            chunks = []

        if collecting:
            chunks.append(pcm)
            if is_voiced:
                voiced_frames += 1
                silence_frames = 0
            else:
                silence_frames += 1

            if silence_frames >= end_silence_frames:
                if voiced_frames >= min_frames:
                    utterance = b"".join(chunks)
                    if call.is_playing() and not call.interrupted_event.is_set():
                        append_system_log(
                            "sip",
                            "utterance_ignored",
                            "Utterance ignored because playback remained active and barge-in was not accepted.",
                            {"channel_id": call.channel_id, "bytes": len(utterance), "voiced_frames": voiced_frames},
                        )
                    else:
                        await process_utterance(call, utterance)
                collecting = False
                voiced_frames = 0
                silence_frames = 0
                chunks = []


async def call_watchdog_loop(call: CallSession) -> None:
    """Enforce max duration and silence timeout. Output: none. Input: call context."""
    while not call.stop_event.is_set():
        await asyncio.sleep(1.0)
        now = datetime.utcnow()
        duration = (now - call.started_at).total_seconds()
        idle = (now - call.last_activity_at).total_seconds()

        is_playing = bool(call.playback_task and not call.playback_task.done())

        if duration > SIP_MAX_DURATION:
            logger.info("Call %s exceeded SIP_MAX_DURATION", call.channel_id)
            call.stop_reason = "watchdog_max_duration"
            call.stop_reason_details = {
                "duration_sec": round(duration, 2),
                "max_duration_sec": SIP_MAX_DURATION,
            }
            append_system_log(
                "sip",
                "call_timeout",
                "Call exceeded maximum duration and hangup was requested.",
                {"channel_id": call.channel_id, **call.stop_reason_details},
            )
            await ari_hangup_channel(call.channel_id)
            call.stop_event.set()
            return

        if idle > SIP_MAX_SILENCE and not is_playing:
            logger.info(
                "Call %s exceeded SIP_MAX_SILENCE (rtp packets=%s audio=%s last_pt=%s last_energy=%s peak_energy=%s)",
                call.channel_id,
                call.rx_total_packets,
                call.rx_audio_packets,
                call.rx_last_payload_type,
                call.rx_last_energy,
                call.rx_peak_energy,
            )
            call.stop_reason = "watchdog_max_silence"
            call.stop_reason_details = {
                "idle_sec": round(idle, 2),
                "max_silence_sec": SIP_MAX_SILENCE,
                "is_playing": is_playing,
                "rtp_packets": call.rx_total_packets,
                "audio_packets": call.rx_audio_packets,
                "last_pt": call.rx_last_payload_type,
                "last_energy": call.rx_last_energy,
                "peak_energy": call.rx_peak_energy,
            }
            append_system_log(
                "sip",
                "call_timeout",
                "Call exceeded silence timeout and hangup was requested.",
                {"channel_id": call.channel_id, **call.stop_reason_details},
            )
            await ari_hangup_channel(call.channel_id)
            call.stop_event.set()
            return


async def start_call_session(req: CallStartRequest) -> dict[str, Any]:
    """Create call runtime and start processing tasks. Output: status dict. Input: API request."""
    async with active_calls_lock:
        if req.channel_id in active_calls:
            return {"status": "exists", "channel_id": req.channel_id}

        role_prompt = load_role_prompt()
        greeting_text, greeting_wav = load_greeting()
        call = CallSession(
            channel_id=req.channel_id,
            caller=req.caller,
            language=req.language or SIP_DEFAULT_LANGUAGE,
            media_port=req.media_port,
            role_prompt=role_prompt,
            greeting_text=greeting_text,
            greeting_wav=greeting_wav,
        )

        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: RtpEndpoint(call),
            local_addr=("0.0.0.0", req.media_port),
        )
        call.rtp_transport = transport  # type: ignore[assignment]
        call.rtp_protocol = protocol  # type: ignore[assignment]

        if req.asterisk_rtp_host and req.asterisk_rtp_port and req.asterisk_rtp_port > 0:
            call.rtp_protocol.remote_addr = (req.asterisk_rtp_host, req.asterisk_rtp_port)
            call.rtp_ready_event.set()
            logger.info(
                "Preconfigured RTP target for call %s to %s:%s",
                req.channel_id,
                req.asterisk_rtp_host,
                req.asterisk_rtp_port,
            )

        call.pipeline_task = asyncio.create_task(call_pipeline_loop(call))
        call.watchdog_task = asyncio.create_task(call_watchdog_loop(call))

        active_calls[req.channel_id] = call

    logger.info("Started SIP call session channel=%s port=%s", req.channel_id, req.media_port)
    append_system_log(
        "sip",
        "call_start",
        "SIP call session started.",
        {"channel_id": req.channel_id, "caller": req.caller, "media_port": req.media_port, "language": req.language},
    )
    return {"status": "started", "channel_id": req.channel_id, "media_port": req.media_port}


async def stop_call_session(
    channel_id: str,
    reason: str | None = None,
    reason_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stop call runtime and cleanup resources. Output: status dict. Input: channel id and optional reason details."""
    async with active_calls_lock:
        call = active_calls.pop(channel_id, None)

    if not call:
        return {"status": "not_found", "channel_id": channel_id}

    call.stop_event.set()
    call.interrupted_event.set()

    try:
        call.frame_queue.put_nowait(None)
    except Exception:
        pass

    if call.playback_task and not call.playback_task.done():
        call.playback_task.cancel()

    if call.barge_eval_task and not call.barge_eval_task.done():
        call.barge_eval_task.cancel()

    for task in (call.pipeline_task, call.watchdog_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    if call.rtp_transport:
        call.rtp_transport.close()

    effective_reason = reason or call.stop_reason or "unknown"
    effective_details: dict[str, Any] = {}
    if call.stop_reason_details:
        effective_details.update(call.stop_reason_details)
    if reason_details:
        effective_details.update(reason_details)

    logger.info(
        "Stopped SIP call session channel=%s rtp_packets=%s audio_packets=%s unknown_pt=%s last_pt=%s last_energy=%s peak_energy=%s vad_factor=%.2f barge_factor=%.2f",
        channel_id,
        call.rx_total_packets,
        call.rx_audio_packets,
        call.rx_unknown_pt_packets,
        call.rx_last_payload_type,
        call.rx_last_energy,
        call.rx_peak_energy,
        call.adaptive_vad_factor,
        call.adaptive_barge_factor,
    )
    append_system_log(
        "sip",
        "call_stop",
        "SIP call session stopped.",
        {
            "channel_id": channel_id,
            "reason": effective_reason,
            "reason_details": effective_details,
            "rtp_packets": call.rx_total_packets,
            "audio_packets": call.rx_audio_packets,
            "unknown_pt": call.rx_unknown_pt_packets,
            "last_pt": call.rx_last_payload_type,
            "last_energy": call.rx_last_energy,
            "peak_energy": call.rx_peak_energy,
            "vad_factor": round(call.adaptive_vad_factor, 2),
            "barge_factor": round(call.adaptive_barge_factor, 2),
        },
    )
    return {"status": "stopped", "channel_id": channel_id}


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health endpoint. Output: service info. Input: none."""
    return {"status": "ok", "service": "sip", "active_calls": len(active_calls)}


@app.get("/stt-audio/last")
async def get_last_stt_audio() -> dict[str, Any]:
    """Get last STT audio and transcript. Output: WAV bytes in base64 and metadata. Input: none."""
    if "last" not in _last_stt_audio_cache:
        return {"has_data": False, "message": "No STT audio cached yet"}
    wav_bytes, transcript = _last_stt_audio_cache["last"]
    return {
        "has_data": True,
        "audio_b64": base64.b64encode(wav_bytes).decode("ascii"),
        "audio_mime": "audio/wav",
        "transcript": transcript,
        "bytes": len(wav_bytes),
    }


@app.get("/ari/calls")
async def list_calls() -> dict[str, Any]:
    """List active call sessions. Output: list. Input: none."""
    return {
        "active": [
            {
                "channel_id": call.channel_id,
                "caller": call.caller,
                "media_port": call.media_port,
                "language": call.language,
            }
            for call in active_calls.values()
        ]
    }


@app.post("/ari/call/start")
async def ari_call_start(req: CallStartRequest) -> dict[str, Any]:
    """Start call session from ARI listener. Output: status dict. Input: start payload."""
    try:
        return await start_call_session(req)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"media_port bind failed: {exc}") from exc


@app.post("/ari/call/end")
async def ari_call_end(req: CallEndRequest) -> dict[str, Any]:
    """End call session from ARI listener. Output: status dict. Input: end payload."""
    details: dict[str, Any] = {}
    if req.source:
        details["source"] = req.source
    if req.cause is not None:
        details["cause"] = req.cause
    if req.cause_txt:
        details["cause_txt"] = req.cause_txt
    if req.ari_channel_id:
        details["ari_channel_id"] = req.ari_channel_id
    if req.bridge_id:
        details["bridge_id"] = req.bridge_id
    if req.details:
        details["ari_details"] = req.details

    return await stop_call_session(req.channel_id, reason=req.reason, reason_details=details)


@app.post("/ari/call/barge-in")
async def ari_call_barge_in(req: BargeInRequest) -> dict[str, Any]:
    """Interrupt playback for active call. Output: status dict. Input: channel id."""
    call = active_calls.get(req.channel_id)
    if not call:
        return {"status": "not_found", "channel_id": req.channel_id}

    call.interrupted_event.set()
    if call.playback_task and not call.playback_task.done():
        call.playback_task.cancel()

    append_system_log("sip", "barge_interrupt", "Playback interrupted by explicit ARI request.", {"channel_id": req.channel_id})

    return {"status": "interrupted", "channel_id": req.channel_id}


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Stop all calls on service shutdown. Output: none. Input: none."""
    channel_ids = list(active_calls.keys())
    for channel_id in channel_ids:
        try:
            await stop_call_session(channel_id)
        except Exception:
            logger.exception("Failed to stop call during shutdown: %s", channel_id)


@app.post("/sip/call/hangup")
async def sip_call_hangup(req: SipHangupRequest) -> dict[str, Any]:
    """Hang up an active SIP call. Output: status dict. Input: channel_id."""
    call = active_calls.get(req.channel_id)
    if not call:
        return {"status": "not_found", "channel_id": req.channel_id}
    await stop_call_session(req.channel_id)
    append_system_log("sip", "tool_hangup", "Call hung up via tool.", {"channel_id": req.channel_id})
    return {"status": "ok", "channel_id": req.channel_id, "action": "hangup"}


@app.post("/sip/call/transfer")
async def sip_call_transfer(req: SipTransferRequest) -> dict[str, Any]:
    """Blind transfer an active SIP call to a target extension. Output: status dict. Input: channel_id and target."""
    call = active_calls.get(req.channel_id)
    if not call:
        return {"status": "not_found", "channel_id": req.channel_id}

    continue_url = f"{ASTERISK_HTTP_URL}/channels/{req.channel_id}/continue"
    continue_params = {
        "extension": req.target,
        "context": "internal-users",
        "priority": "1",
    }
    redirect_url = f"{ASTERISK_HTTP_URL}/channels/{req.channel_id}/redirect"
    redirect_params = {
        "endpoint": f"PJSIP/{req.target}",
    }

    method_used = ""
    continue_error = ""
    try:
        async with httpx.AsyncClient(
            auth=(ASTERISK_ARI_USER, ASTERISK_ARI_PASSWORD), timeout=8.0
        ) as client:
            # Prefer dialplan continue: this reliably exits Stasis and routes by extension.
            try:
                continue_resp = await client.post(continue_url, params=continue_params)
                continue_resp.raise_for_status()
                method_used = "continue"
            except httpx.HTTPStatusError as exc:
                continue_error = f"HTTP {exc.response.status_code}"
                # Fallback to redirect for cases where continue is rejected by channel state.
                redirect_resp = await client.post(redirect_url, params=redirect_params)
                redirect_resp.raise_for_status()
                method_used = "redirect"
    except httpx.HTTPStatusError as exc:
        return {"status": "error", "channel_id": req.channel_id, "detail": f"ARI error {exc.response.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "channel_id": req.channel_id, "detail": str(exc)}

    append_system_log(
        "sip", "tool_transfer", "Call transferred via tool.",
        {
            "channel_id": req.channel_id,
            "target": req.target,
            "method": method_used,
            "continue_error": continue_error,
        },
    )
    return {
        "status": "ok",
        "channel_id": req.channel_id,
        "action": "transfer",
        "target": req.target,
        "method": method_used,
        "continue_error": continue_error,
    }
