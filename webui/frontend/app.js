const connectButton = document.getElementById("connect-btn");
const startButton = document.getElementById("start-btn");
const stopButton = document.getElementById("stop-btn");
const resetSystemButton = document.getElementById("reset-system-btn");
const playChunkButton = document.getElementById("play-chunk-btn");
const stopPlaybackButton = document.getElementById("stop-playback-btn");
const playbackStateNode = document.getElementById("playback-state");
const layoutNode = document.querySelector(".layout");
const layoutSplitter = document.getElementById("layout-splitter");
const refreshDevicesButton = document.getElementById("refresh-devices-btn");
const inputDeviceSelect = document.getElementById("input-device-select");
const outputDeviceSelect = document.getElementById("output-device-select");
const voiceStateNode = document.getElementById("voice-state");
const micStateNode = document.getElementById("mic-state");
const vuMeterNode = document.getElementById("vu-meter");
const micLevelValueNode = document.getElementById("mic-level-value");
const vadMeterNode = document.getElementById("vad-meter");
const vadLevelValueNode = document.getElementById("vad-level-value");
const vadThresholdMeterNode = document.getElementById("vad-threshold-meter");
const vadThresholdValueNode = document.getElementById("vad-threshold-value");
const vadStateNode = document.getElementById("vad-state");
const vadRelaxationInput = document.getElementById("vad-relaxation");
const vadRelaxationValueNode = document.getElementById("vad-relaxation-value");
const vadActivationInput = document.getElementById("vad-activation");
const vadActivationValueNode = document.getElementById("vad-activation-value");
const vadClosePauseInput = document.getElementById("vad-close-pause");
const vadClosePauseValueNode = document.getElementById("vad-close-pause-value");
const vadPresetNode = document.getElementById("vad-preset");
const vadCalibrateButton = document.getElementById("vad-calibrate");
const vadCalibrationStateNode = document.getElementById("vad-calibration-state");
const spectrogramNode = document.getElementById("spectrogram");
const chunkWaveformNode = document.getElementById("chunk-waveform");
const playbackSpectrumNode = document.getElementById("playback-spectrum");
const chunkDurationValueNode = document.getElementById("chunk-duration-value");
const chatLogNode = document.getElementById("chat-log");
const chatScrollNode = document.getElementById("chat-scroll");
const systemLogNode = document.getElementById("system-log");
const systemLogScrollNode = document.getElementById("system-log-scroll");
const systemLogPollIntervalInput = document.getElementById("system-log-poll-interval");
const systemLogPollIntervalValueNode = document.getElementById("system-log-poll-interval-value");
const autoscrollToggle = document.getElementById("autoscroll-toggle");
const systemPromptNode = document.getElementById("system-prompt");
const roleSelectNode = document.getElementById("role-select");
const temperatureInput = document.getElementById("temperature-input");
const temperatureValueNode = document.getElementById("temperature-value");
const reasoningToggle = document.getElementById("reasoning-toggle");
const autoloadToggle = document.getElementById("autoload-toggle");
const applyConfigButton = document.getElementById("apply-config-btn");
const textInputNode = document.getElementById("text-input");
const sendTextButton = document.getElementById("send-text-btn");
const typingIndicatorNode = document.getElementById("typing-indicator");

let socket = null;
let mediaStream = null;
let mediaRecorder = null;
let audioContext = null;
let analyser = null;
let pcmProcessorNode = null;
let pcmSinkNode = null;
let pcmCaptureSampleRate = 16000;
let spectrogramContext = null;
let rafId = null;
let timeDomainData = null;
let frequencyData = null;
let micLevel = 0;
let vadLevel = 0;
let vadThreshold = 0.08;
let vadFloorMin = 0.04;
let isCalibrating = false;

// VAD utterance state
let lastChunkBlob = null;
let utteranceBlobs = [];
let utteranceActive = false;
let chunkVadPeak = 0;
let recentChunks = [];
let pendingChunkEnvelope = [];
let utteranceEnvelope = [];
let utteranceChunkEnvelopes = [];
let recentChunkEnvelopes = [];
let pendingChunkPcm = [];
let utteranceChunkPcm = [];
let silentChunksInRow = 0;

// LLM streaming state
let llmBuffer = "";
let llmStreamActive = false;
const VAD_UTTERANCE_THRESHOLD = 0.32;
const VAD_PRE_ROLL_CHUNKS = 0;
const MEDIA_CHUNK_MS = 200;
const MIN_UTTERANCE_DURATION_SEC = 0.25;
const MIN_UTTERANCE_BYTES = 1800;
const TRAILING_SILENCE_TAIL_RATIO = 0.35;
const TRAILING_SILENCE_TAIL_AVG_THRESHOLD = 0.06;
const MAX_TRAILING_SILENCE_CHUNKS = 3;
let selectedRecorderMimeType = "";
let selectedInputDeviceId = "";
let selectedOutputDeviceId = "";
const PCM_CAPTURE_DOWNSAMPLE = 3;
const PCM_STITCH_CROSSFADE_SAMPLES = 64;
let systemLogPollTimer = null;
let systemLogPollingInFlight = false;
let ttsAudioContext = null;
let ttsAnalyser = null;
let ttsSpectrumData = null;
let ttsSpectrumRafId = null;
let activePlaybackAudio = null;
let stopPlaybackRequested = false;
let ttsPlaybackQueue = [];
let ttsPlaybackInProgress = false;
let isResizingLayout = false;
let autoloadStatusRequestInFlight = false;

const VAD_PRESETS = {
  near: { relaxation: 0.06, activation: 0.18, floorMin: 0.035 },
  noisy: { relaxation: 0.13, activation: 0.34, floorMin: 0.09 },
  far: { relaxation: 0.05, activation: 0.14, floorMin: 0.03 },
};

// Collect browser audio API capability snapshot. Output: object with capability flags. Input: none.
function getAudioCapabilities() {
  const nav = navigator || {};
  const hasMediaDevices = !!nav.mediaDevices;
  const hasModernGetUserMedia = hasMediaDevices && typeof nav.mediaDevices.getUserMedia === "function";
  const hasLegacyGetUserMedia = typeof nav.getUserMedia === "function"
    || typeof nav.webkitGetUserMedia === "function"
    || typeof nav.mozGetUserMedia === "function";
  const hasMediaRecorder = typeof window.MediaRecorder !== "undefined";
  const hasAudioContext = typeof window.AudioContext === "function" || typeof window.webkitAudioContext === "function";
  const isLocalHost = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";
  const isEmbeddedFrame = window.top !== window.self;

  return {
    protocol: window.location.protocol,
    host: window.location.host,
    isSecureContext: !!window.isSecureContext,
    isLocalHost,
    isEmbeddedFrame,
    hasMediaDevices,
    hasModernGetUserMedia,
    hasLegacyGetUserMedia,
    hasMediaRecorder,
    hasAudioContext,
  };
}

// Print capability diagnostics to event log. Output: none. Input: optional prefix label.
function logAudioCapabilities(prefix = "Browser audio capabilities") {
  const caps = getAudioCapabilities();
  appendEvent(`${prefix}: ${JSON.stringify(caps)}`);
}

// Request microphone stream with modern and legacy browser APIs. Output: media stream. Input: optional input device id.
async function requestMicrophoneStream(deviceId = "") {
  const audioConstraints = deviceId ? { deviceId: { exact: deviceId } } : true;
  if (navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === "function") {
    return navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
  }

  const legacyGetUserMedia = navigator.getUserMedia
    || navigator.webkitGetUserMedia
    || navigator.mozGetUserMedia;

  if (typeof legacyGetUserMedia === "function") {
    return new Promise((resolve, reject) => {
      legacyGetUserMedia.call(navigator, { audio: audioConstraints }, resolve, reject);
    });
  }

  const caps = getAudioCapabilities();
  const hints = [];
  if (!caps.isSecureContext && !caps.isLocalHost) {
    hints.push("open via HTTPS or localhost");
  }
  if (caps.isEmbeddedFrame) {
    hints.push("open the page directly in a regular browser tab (not an embedded preview)");
  }
  throw new Error(`getUserMedia is not available in this browser/context (${hints.join("; ") || "no supported API exposed"}).`);
}

// Fill input/output device selects from browser media device list. Output: none. Input: none.
async function refreshDeviceLists() {
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.enumerateDevices !== "function") {
    appendEvent("Device list is not supported by this browser.");
    return;
  }

  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const inputs = devices.filter((d) => d.kind === "audioinput");
    const outputs = devices.filter((d) => d.kind === "audiooutput");

    if (inputDeviceSelect) {
      inputDeviceSelect.innerHTML = "";
      const defaultInput = document.createElement("option");
      defaultInput.value = "";
      defaultInput.textContent = "Default microphone";
      inputDeviceSelect.appendChild(defaultInput);

      for (const device of inputs) {
        const option = document.createElement("option");
        option.value = device.deviceId;
        option.textContent = device.label || `Microphone ${inputDeviceSelect.length}`;
        inputDeviceSelect.appendChild(option);
      }

      if (selectedInputDeviceId && Array.from(inputDeviceSelect.options).some((o) => o.value === selectedInputDeviceId)) {
        inputDeviceSelect.value = selectedInputDeviceId;
      }
    }

    if (outputDeviceSelect) {
      outputDeviceSelect.innerHTML = "";
      const defaultOutput = document.createElement("option");
      defaultOutput.value = "";
      defaultOutput.textContent = "Default output";
      outputDeviceSelect.appendChild(defaultOutput);

      for (const device of outputs) {
        const option = document.createElement("option");
        option.value = device.deviceId;
        option.textContent = device.label || `Speaker ${outputDeviceSelect.length}`;
        outputDeviceSelect.appendChild(option);
      }

      if (selectedOutputDeviceId && Array.from(outputDeviceSelect.options).some((o) => o.value === selectedOutputDeviceId)) {
        outputDeviceSelect.value = selectedOutputDeviceId;
      }
    }
  } catch (err) {
    appendEvent(`Device refresh error: ${err.message || err}`);
  }
}

// Draw waveform of a Blob onto the chunk-waveform canvas. Output: none. Input: Blob containing audio.
async function drawChunkWaveform(blob) {
  if (!chunkWaveformNode) {
    return;
  }

  const ctx = chunkWaveformNode.getContext("2d");
  if (!ctx) {
    return;
  }

  const width = Math.max(320, Math.floor(chunkWaveformNode.clientWidth));
  const height = 80;
  if (chunkWaveformNode.width !== width) {
    chunkWaveformNode.width = width;
  }
  chunkWaveformNode.height = height;

  ctx.fillStyle = "#06101a";
  ctx.fillRect(0, 0, width, height);

  let buffer;
  try {
    const arrayBuffer = await blob.arrayBuffer();
    const decodeCtx = new (window.AudioContext || window.webkitAudioContext)();
    buffer = await decodeCtx.decodeAudioData(arrayBuffer);
    decodeCtx.close();
    renderChunkDuration(buffer.duration);
  } catch {
    // Browser cannot decode (e.g. opus/webm); keep last known duration and draw byte-level proxy.
    const raw = new Uint8Array(await blob.arrayBuffer());
    const step = Math.max(1, Math.floor(raw.length / width));
    ctx.strokeStyle = "#3a7a9c";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let x = 0; x < width; x += 1) {
      const idx = x * step;
      const norm = idx < raw.length ? (raw[idx] - 128) / 128 : 0;
      const y = (height / 2) + norm * (height / 2 - 2);
      if (x === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    }
    ctx.stroke();
    return;
  }

  const samples = buffer.getChannelData(0);
  const step = Math.max(1, Math.floor(samples.length / width));
  const midY = height / 2;
  const amp = midY - 3;

  // Draw center line
  ctx.strokeStyle = "rgba(43,74,107,0.5)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, midY);
  ctx.lineTo(width, midY);
  ctx.stroke();

  // Draw min/max band per pixel column
  for (let x = 0; x < width; x += 1) {
    let min = 1;
    let max = -1;
    const offset = x * step;
    for (let i = 0; i < step && offset + i < samples.length; i += 1) {
      const v = samples[offset + i];
      if (v < min) min = v;
      if (v > max) max = v;
    }
    const yTop = midY - max * amp;
    const yBot = midY - min * amp;
    const energy = Math.max(Math.abs(min), Math.abs(max));
    const hue = 200 - Math.round(80 * energy);
    ctx.strokeStyle = `hsl(${hue} 80% ${25 + Math.round(45 * energy)}%)`;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x + 0.5, yTop);
    ctx.lineTo(x + 0.5, Math.max(yBot, yTop + 1));
    ctx.stroke();
  }
}

// Draw waveform from normalized envelope samples [0..1] across full canvas width. Output: none. Input: envelope sample array.
function drawEnvelopeWaveform(samples) {
  if (!chunkWaveformNode) {
    return;
  }

  const ctx = chunkWaveformNode.getContext("2d");
  if (!ctx) {
    return;
  }

  const width = Math.max(320, Math.floor(chunkWaveformNode.clientWidth));
  const height = 80;
  if (chunkWaveformNode.width !== width) {
    chunkWaveformNode.width = width;
  }
  chunkWaveformNode.height = height;

  ctx.fillStyle = "#06101a";
  ctx.fillRect(0, 0, width, height);

  if (!samples || samples.length === 0) {
    return;
  }

  const midY = height / 2;
  const amp = midY - 3;

  ctx.strokeStyle = "rgba(43,74,107,0.5)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, midY);
  ctx.lineTo(width, midY);
  ctx.stroke();

  const step = Math.max(1, Math.floor(samples.length / width));
  for (let x = 0; x < width; x += 1) {
    const offset = x * step;
    let peak = 0;
    for (let i = 0; i < step && offset + i < samples.length; i += 1) {
      if (samples[offset + i] > peak) {
        peak = samples[offset + i];
      }
    }

    const yTop = midY - peak * amp;
    const yBot = midY + peak * amp;
    const hue = 195 - Math.round(70 * peak);
    const lightness = 28 + Math.round(44 * peak);
    ctx.strokeStyle = `hsl(${hue} 82% ${lightness}%)`;
    ctx.beginPath();
    ctx.moveTo(x + 0.5, yTop);
    ctx.lineTo(x + 0.5, Math.max(yBot, yTop + 1));
    ctx.stroke();
  }
}

// Render last waveform chunk duration label. Output: none. Input: duration in seconds or null.
function renderChunkDuration(seconds) {
  if (!chunkDurationValueNode) {
    return;
  }
  if (!Number.isFinite(seconds) || seconds <= 0) {
    chunkDurationValueNode.textContent = "n/a";
    return;
  }
  chunkDurationValueNode.textContent = `${seconds.toFixed(2)} s`;
}

// Compute average envelope value on chunk tail. Output: average [0..1]. Input: envelope array.
function tailAverage(envelope) {
  if (!envelope || envelope.length === 0) {
    return 0;
  }
  const windowSize = Math.max(1, Math.floor(envelope.length * TRAILING_SILENCE_TAIL_RATIO));
  const start = Math.max(0, envelope.length - windowSize);
  let sum = 0;
  for (let i = start; i < envelope.length; i += 1) {
    sum += envelope[i];
  }
  return sum / (envelope.length - start);
}

// Build mono 16-bit PCM WAV blob from normalized samples. Output: WAV Blob. Input: samples array and sample rate.
function buildWavBlob(samples, sampleRate) {
  const safeRate = Math.max(8000, Math.floor(sampleRate || 16000));
  const bytesPerSample = 2;
  const dataSize = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  const writeString = (offset, text) => {
    for (let i = 0; i < text.length; i += 1) {
      view.setUint8(offset + i, text.charCodeAt(i));
    }
  };

  writeString(0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, safeRate, true);
  view.setUint32(28, safeRate * bytesPerSample, true);
  view.setUint16(32, bytesPerSample, true);
  view.setUint16(34, 16, true);
  writeString(36, "data");
  view.setUint32(40, dataSize, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i += 1) {
    const s = clamp(samples[i], -1, 1);
    const v = s < 0 ? s * 0x8000 : s * 0x7fff;
    view.setInt16(offset, Math.round(v), true);
    offset += 2;
  }

  return new Blob([buffer], { type: "audio/wav" });
}

// Stitch per-chunk PCM arrays with a short crossfade to suppress boundary clicks. Output: flattened PCM array. Input: array of PCM chunk arrays.
function stitchPcmChunksSmooth(chunks) {
  if (!chunks || chunks.length === 0) {
    return [];
  }

  const result = chunks[0].slice();
  for (let chunkIndex = 1; chunkIndex < chunks.length; chunkIndex += 1) {
    const nextChunk = chunks[chunkIndex];
    if (!nextChunk || nextChunk.length === 0) {
      continue;
    }

    const overlap = Math.min(PCM_STITCH_CROSSFADE_SAMPLES, result.length, nextChunk.length);
    if (overlap <= 0) {
      result.push(...nextChunk);
      continue;
    }

    const start = result.length - overlap;
    for (let i = 0; i < overlap; i += 1) {
      const t = (i + 1) / (overlap + 1);
      result[start + i] = result[start + i] * (1 - t) + nextChunk[i] * t;
    }
    result.push(...nextChunk.slice(overlap));
  }

  return result;
}

// Build audio context in a cross-browser way. Output: audio context instance. Input: none.
function createAudioContext() {
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) {
    throw new Error("AudioContext is not available in this browser.");
  }
  return new Context();
}

// Pick recording MIME type supported by both MediaRecorder and HTMLAudio playback. Output: MIME type or empty string. Input: none.
function pickRecorderMimeType() {
  if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") {
    return "";
  }

  const probeAudio = document.createElement("audio");
  const canPlay = (mime) => !!probeAudio.canPlayType(mime);
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/ogg",
    "audio/mp4;codecs=mp4a.40.2",
    "audio/mp4",
  ];

  for (const mime of candidates) {
    if (MediaRecorder.isTypeSupported(mime) && canPlay(mime)) {
      return mime;
    }
  }

  for (const mime of candidates) {
    if (MediaRecorder.isTypeSupported(mime)) {
      return mime;
    }
  }

  return "";
}

// Append one event line. Output: none. Input: text line.
function appendEvent(line) {
  const timestamp = new Date().toLocaleTimeString();
  chatLogNode.textContent = `${chatLogNode.textContent}\n[${timestamp}] ${line}`.trim();
  if (autoscrollToggle.checked && chatScrollNode) {
    chatScrollNode.scrollTop = chatScrollNode.scrollHeight;
  }
}

// Replace complete system log content from snapshot. Output: none. Input: lines array.
function setSystemLog(lines) {
  if (!systemLogNode) {
    return;
  }
  systemLogNode.textContent = lines && lines.length > 0 ? lines.join("\n") : "No system log yet.";
  if (systemLogScrollNode) {
    systemLogScrollNode.scrollTop = systemLogScrollNode.scrollHeight;
  }
}

// Append one line to system log panel. Output: none. Input: text line.
function appendSystemLogLine(line) {
  if (!systemLogNode) {
    return;
  }
  systemLogNode.textContent = `${systemLogNode.textContent}\n${line}`.trim();
  if (systemLogScrollNode) {
    systemLogScrollNode.scrollTop = systemLogScrollNode.scrollHeight;
  }
}

// Read configured system log polling interval in milliseconds. Output: integer milliseconds. Input: none.
function getSystemLogPollIntervalMs() {
  if (!systemLogPollIntervalInput) {
    return 1000;
  }
  return clamp(parseInt(systemLogPollIntervalInput.value, 10), 100, 10000);
}

// Render system log polling interval label. Output: none. Input: none.
function renderSystemLogPollInterval() {
  if (!systemLogPollIntervalValueNode) {
    return;
  }
  systemLogPollIntervalValueNode.textContent = `${(getSystemLogPollIntervalMs() / 1000).toFixed(1)} s`;
}

// Convert Blob to base64 string. Output: base64 string. Input: Blob.
async function blobToBase64(blob) {
  const buffer = await blob.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

// Apply selected playback device to an audio element. Output: whether custom sink is active. Input: HTMLAudioElement.
async function applyPlaybackDevice(audio) {
  if (!audio || !selectedOutputDeviceId) {
    return false;
  }

  if (typeof audio.setSinkId !== "function") {
    appendEvent("Playback device selection is not supported in this browser.");
    return false;
  }

  try {
    await audio.setSinkId(selectedOutputDeviceId);
    return true;
  } catch (err) {
    appendEvent(`Playback device apply error: ${err.message || err}`);
    return false;
  }
}

// Play last sent utterance fragment via hidden Audio element. Output: none. Input: none.
async function playLastChunk() {
  if (!lastChunkBlob) {
    appendEvent("No sent fragment available.");
    return;
  }
  const url = URL.createObjectURL(lastChunkBlob);
  const audio = new Audio(url);

  await applyPlaybackDevice(audio);

  audio.onended = () => URL.revokeObjectURL(url);
  audio.onerror = () => {
    appendEvent("Playback error: browser could not decode sent fragment audio.");
    URL.revokeObjectURL(url);
  };
  audio.play().catch((err) => appendEvent(`Playback error: ${err.message}`));
  appendEvent(`Playing last sent fragment (${lastChunkBlob.size} bytes, type: ${lastChunkBlob.type || "unknown"}).`);
}

// Finalize accumulated utterance blobs and send to backend. Output: none. Input: none.
async function sendUtterance() {
  if (utteranceBlobs.length === 0 || !socket || socket.readyState !== WebSocket.OPEN) {
    return;
  }

  const trimmedBlobs = utteranceBlobs.slice();
  const trimmedChunkEnvelopes = utteranceChunkEnvelopes.slice();
  const trimmedChunkPcm = utteranceChunkPcm.slice();

  let droppedTrailingChunks = 0;
  while (trimmedBlobs.length > 1 && droppedTrailingChunks < MAX_TRAILING_SILENCE_CHUNKS) {
    const lastEnvelope = trimmedChunkEnvelopes[trimmedChunkEnvelopes.length - 1] || [];
    const tailAvg = tailAverage(lastEnvelope);
    if (tailAvg >= TRAILING_SILENCE_TAIL_AVG_THRESHOLD) {
      break;
    }
    trimmedBlobs.pop();
    trimmedChunkEnvelopes.pop();
    trimmedChunkPcm.pop();
    droppedTrailingChunks += 1;
  }

  if (droppedTrailingChunks > 0) {
    appendEvent(`Trimmed trailing silence: removed ${droppedTrailingChunks} chunk(s).`);
  }

  const chunkCount = trimmedBlobs.length;
  const sentEnvelope = trimmedChunkEnvelopes.flat();
  const sentPcm = stitchPcmChunksSmooth(trimmedChunkPcm);
  const mimeType = utteranceBlobs[0].type || "audio/webm";
  const combined = new Blob(trimmedBlobs, { type: mimeType });
  utteranceBlobs = [];
  utteranceEnvelope = [];
  utteranceChunkEnvelopes = [];
  utteranceChunkPcm = [];
  const estimatedDurationSec = (chunkCount * MEDIA_CHUNK_MS) / 1000;

  if (chunkCount === 0) {
    appendEvent("Skipped fragment after trailing silence trim (no audio left).");
    return;
  }

  // Ignore likely glitch fragments (very short / tiny payloads).
  if (estimatedDurationSec < MIN_UTTERANCE_DURATION_SEC || combined.size < MIN_UTTERANCE_BYTES) {
    appendEvent(
      `Skipped tiny fragment (${estimatedDurationSec.toFixed(2)} s est, ${combined.size} bytes).`
    );
    return;
  }

  try {
    const pcmSampleRate = pcmCaptureSampleRate / PCM_CAPTURE_DOWNSAMPLE;

    // Prefer WAV (built from PCM) to avoid invalid WebM header issues when chunks
    // don't include the original EBML header from the start of the MediaRecorder session.
    let sendBlob, sendMime;
    if (sentPcm.length > 0) {
      sendBlob = buildWavBlob(sentPcm, pcmSampleRate);
      sendMime = "audio/wav";
    } else {
      sendBlob = combined;
      sendMime = mimeType;
    }

    const audio_b64 = await blobToBase64(sendBlob);
    socket.send(JSON.stringify({ type: "voice.utterance", audio_b64, mime_type: sendMime }));

    // Update preview only for fragments that were actually sent.
    lastChunkBlob = sendMime === "audio/wav" ? sendBlob : (sentPcm.length > 0 ? buildWavBlob(sentPcm, pcmSampleRate) : combined);
    playChunkButton.disabled = false;
    const playbackDurationSec = sentPcm.length > 0 ? sentPcm.length / pcmSampleRate : estimatedDurationSec;
    renderChunkDuration(playbackDurationSec);
    drawEnvelopeWaveform(sentEnvelope);

    appendEvent(`Sent voice utterance (${sendBlob.size} bytes, type: ${sendMime}).`);
  } catch (err) {
    appendEvent(`Utterance encode error: ${err.message}`);
  }
}

// Send text message to backend. Output: none. Input: none.
function sendText() {
  const text = (textInputNode.value || "").trim();
  if (!text) {
    return;
  }
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    appendEvent("Connect session first.");
    return;
  }
  socket.send(JSON.stringify({ type: "text.query", text }));
  appendEvent(`You: ${text}`);
  textInputNode.value = "";
}

// Send system prompt and role configuration to backend. Output: none. Input: none.
function sendSessionConfig() {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    appendEvent("Connect session to apply config.");
    return;
  }
  const system_prompt = (systemPromptNode.value || "").trim();
  const role = roleSelectNode.value;
  const temperature = temperatureInput ? clamp(parseFloat(temperatureInput.value), 0, 2) : 0.7;
  const reasoning = !!(reasoningToggle && reasoningToggle.checked);
  socket.send(JSON.stringify({ type: "session.config", system_prompt, role, reasoning, options: { temperature } }));
  appendEvent(`Config applied: role=${role}${system_prompt ? ", system_prompt set" : ""}, temperature=${temperature.toFixed(2)}, reasoning=${reasoning}.`);
}

// Render current temperature value near the slider. Output: none. Input: none.
function renderTemperatureValue() {
  if (!temperatureInput || !temperatureValueNode) {
    return;
  }
  temperatureValueNode.textContent = clamp(parseFloat(temperatureInput.value), 0, 2).toFixed(2);
}

// Fetch current runtime keep-models-loaded state. Output: none. Input: none.
async function refreshAutoloadStatus() {
  if (!autoloadToggle || autoloadStatusRequestInFlight) {
    return;
  }
  autoloadStatusRequestInFlight = true;
  try {
    const response = await fetch("/api/autoload-status");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    autoloadToggle.checked = !!payload.enabled;
    autoloadToggle.dataset.configDefault = payload.configured_default ? "true" : "false";
  } catch (err) {
    appendEvent(`Keep-models-loaded status error: ${err.message || err}`);
  } finally {
    autoloadStatusRequestInFlight = false;
  }
}

// Update runtime keep-models-loaded state until backend restart. Output: none. Input: none.
async function setAutoloadEnabled(enabled) {
  if (!autoloadToggle) {
    return;
  }
  autoloadToggle.disabled = true;
  try {
    const response = await fetch("/api/autoload-status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !!enabled }),
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    autoloadToggle.checked = !!payload.enabled;
    appendEvent(`Keep models loaded ${payload.enabled ? "enabled" : "disabled"}.`);
  } catch (err) {
    autoloadToggle.checked = !enabled;
    appendEvent(`Keep-models-loaded toggle error: ${err.message || err}`);
  } finally {
    autoloadToggle.disabled = false;
  }
}

// Append LLM streaming token in-place on the last log line. Output: none. Input: token string.
function appendLlmToken(token) {
  if (!llmStreamActive) {
    llmBuffer = "";
    typingIndicatorNode.textContent = "";
    llmStreamActive = true;
  }
  llmBuffer += token;
  typingIndicatorNode.textContent = llmBuffer;
}

// Finalize streaming LLM response – commit to log, keep indicator until next response starts. Output: none. Input: full response text.
function finalizeLlmResponse(text) {
  const finalText = (text || llmBuffer || "").trim();
  typingIndicatorNode.textContent = finalText || typingIndicatorNode.textContent;
  llmBuffer = "";
  llmStreamActive = false;
  appendEvent(`AI: ${finalText || "(empty)"}`);
}

// Interrupt active TTS playback when a new LLM response starts. Output: none. Input: none.
function interruptPlaybackForNewLlmResponse() {
  const hadPlayback = ttsPlaybackInProgress || ttsPlaybackQueue.length > 0 || !!activePlaybackAudio;
  stopPlaybackRequested = true;
  ttsPlaybackQueue = [];

  if (activePlaybackAudio) {
    activePlaybackAudio.pause();
    activePlaybackAudio = null;
  }

  if ("speechSynthesis" in window) {
    window.speechSynthesis.cancel();
  }

  stopPlaybackSpectrum();
  ttsPlaybackInProgress = false;
  if (stopPlaybackButton) {
    stopPlaybackButton.disabled = true;
  }
  setPlaybackState("idle", hadPlayback ? "interrupted by new AI response" : "idle");
}

// Speak TTS segments via browser speech synthesis as fallback playback. Output: promise. Input: TTS payload.
async function playTtsPayloadNow(payload) {
  if (!payload || payload.mode === "error") {
    throw new Error(payload && payload.error ? payload.error : "unknown error");
  }

  const segments = Array.isArray(payload.segments) ? payload.segments.filter((segment) => segment && segment.text) : [];
  if (segments.length === 0) {
    throw new Error("TTS returned no playable segments.");
  }

  if (payload.mode === "server_audio") {
    await playServerAudioSegments(segments);
    return;
  }

  if (!("speechSynthesis" in window)) {
    throw new Error("browser speech synthesis is not supported");
  }

  window.speechSynthesis.cancel();
  await new Promise((resolve) => {
    const speakNext = (index) => {
      if (stopPlaybackRequested || index >= segments.length) {
        resolve();
        return;
      }
      const segment = segments[index];
      const utterance = new SpeechSynthesisUtterance(segment.text);
      utterance.lang = segment.locale || "en-US";
      utterance.onend = () => speakNext(index + 1);
      utterance.onerror = () => speakNext(index + 1);
      window.speechSynthesis.speak(utterance);
    };
    speakNext(0);
  });
}

// Queue TTS payload and start sequential playback if needed. Output: none. Input: TTS payload.
function playTtsPayload(payload) {
  if (!payload) {
    return;
  }

  ttsPlaybackQueue.push(payload);
  if (ttsPlaybackInProgress) {
    return;
  }

  stopPlaybackRequested = false;
  ttsPlaybackInProgress = true;
  if (stopPlaybackButton) {
    stopPlaybackButton.disabled = false;
  }
  setPlaybackState("playing", "playing");

  void (async () => {
    try {
      while (ttsPlaybackQueue.length > 0) {
        if (stopPlaybackRequested) {
          ttsPlaybackQueue = [];
          break;
        }
        const nextPayload = ttsPlaybackQueue.shift();
        if (!nextPayload) {
          continue;
        }
        await playTtsPayloadNow(nextPayload);
      }
      if (!stopPlaybackRequested) {
        setPlaybackState("idle", "idle");
      }
    } catch (err) {
      appendEvent(`TTS playback error: ${err.message || err}`);
      setPlaybackState("error", "error");
      ttsPlaybackQueue = [];
    } finally {
      ttsPlaybackInProgress = false;
      if (stopPlaybackButton) {
        stopPlaybackButton.disabled = true;
      }
    }
  })();
}

// Stop active TTS playback. Output: none. Input: none.
function stopPlayback() {
  stopPlaybackRequested = true;
  ttsPlaybackQueue = [];

  if (activePlaybackAudio) {
    activePlaybackAudio.pause();
    activePlaybackAudio = null;
  }

  if ("speechSynthesis" in window) {
    window.speechSynthesis.cancel();
  }

  stopPlaybackSpectrum();
  if (stopPlaybackButton) {
    stopPlaybackButton.disabled = true;
  }
  setPlaybackState("idle", "stopped");
  appendEvent("Playback stopped.");
}

// Ensure playback spectrum canvas size matches rendered size. Output: none. Input: none.
function syncPlaybackSpectrumSize() {
  if (!playbackSpectrumNode) {
    return;
  }
  const width = Math.max(320, Math.floor(playbackSpectrumNode.clientWidth));
  const height = 70;
  if (playbackSpectrumNode.width !== width || playbackSpectrumNode.height !== height) {
    playbackSpectrumNode.width = width;
    playbackSpectrumNode.height = height;
  }
}

// Draw instantaneous playback spectrum bars. Output: none. Input: none.
function renderPlaybackSpectrum() {
  if (!ttsAnalyser || !playbackSpectrumNode || !ttsSpectrumData) {
    return;
  }

  const ctx = playbackSpectrumNode.getContext("2d", { alpha: false });
  if (!ctx) {
    return;
  }

  ttsAnalyser.getByteFrequencyData(ttsSpectrumData);
  syncPlaybackSpectrumSize();
  const width = playbackSpectrumNode.width;
  const height = playbackSpectrumNode.height;
  ctx.fillStyle = "#06101a";
  ctx.fillRect(0, 0, width, height);

  const bars = 32;
  const barGap = 2;
  const barWidth = Math.max(2, Math.floor((width - barGap * (bars - 1)) / bars));
  const bucket = Math.max(1, Math.floor(ttsSpectrumData.length / bars));

  for (let i = 0; i < bars; i += 1) {
    let sum = 0;
    const start = i * bucket;
    const end = Math.min(ttsSpectrumData.length, start + bucket);
    for (let j = start; j < end; j += 1) {
      sum += ttsSpectrumData[j];
    }
    const avg = end > start ? sum / (end - start) : 0;
    const normalized = avg / 255;
    const barHeight = Math.max(2, Math.round(normalized * (height - 4)));
    const x = i * (barWidth + barGap);
    const y = height - barHeight;
    const hue = 170 - Math.round(120 * normalized);
    ctx.fillStyle = `hsl(${hue} 85% ${30 + Math.round(45 * normalized)}%)`;
    ctx.fillRect(x, y, barWidth, barHeight);
  }

  ttsSpectrumRafId = requestAnimationFrame(renderPlaybackSpectrum);
}

// Start playback spectrum analyzer for current audio element. Output: none. Input: HTMLAudioElement.
function startPlaybackSpectrum(audio) {
  if (!playbackSpectrumNode || !audio) {
    return;
  }

  stopPlaybackSpectrum();
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) {
    return;
  }

  ttsAudioContext = new Context();
  const source = ttsAudioContext.createMediaElementSource(audio);
  ttsAnalyser = ttsAudioContext.createAnalyser();
  ttsAnalyser.fftSize = 512;
  ttsSpectrumData = new Uint8Array(ttsAnalyser.frequencyBinCount);
  source.connect(ttsAnalyser);
  ttsAnalyser.connect(ttsAudioContext.destination);
  renderPlaybackSpectrum();
}

// Stop playback spectrum analyzer and reset canvas. Output: none. Input: none.
function stopPlaybackSpectrum() {
  if (ttsSpectrumRafId) {
    cancelAnimationFrame(ttsSpectrumRafId);
    ttsSpectrumRafId = null;
  }

  if (ttsAudioContext) {
    ttsAudioContext.close();
    ttsAudioContext = null;
  }

  ttsAnalyser = null;
  ttsSpectrumData = null;

  if (playbackSpectrumNode) {
    const ctx = playbackSpectrumNode.getContext("2d", { alpha: false });
    if (ctx) {
      syncPlaybackSpectrumSize();
      ctx.fillStyle = "#06101a";
      ctx.fillRect(0, 0, playbackSpectrumNode.width, playbackSpectrumNode.height);
    }
  }
}

// Decode base64 audio to Blob. Output: audio Blob. Input: base64 payload and MIME type.
function base64ToBlob(audioB64, mimeType) {
  const binary = atob(audioB64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new Blob([bytes], { type: mimeType || "audio/wav" });
}

// Play one audio blob and resolve on end. Output: promise. Input: audio Blob.
async function playBlob(blob) {
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  activePlaybackAudio = audio;

  const customSinkApplied = await applyPlaybackDevice(audio);
  if (!customSinkApplied) {
    startPlaybackSpectrum(audio);
  } else {
    stopPlaybackSpectrum();
  }

  return new Promise((resolve, reject) => {
    let settled = false;
    const finalize = (isError, error) => {
      if (settled) {
        return;
      }
      settled = true;
      activePlaybackAudio = null;
      stopPlaybackSpectrum();
      URL.revokeObjectURL(url);
      if (isError) {
        reject(error);
      } else {
        resolve();
      }
    };

    audio.onended = () => {
      finalize(false);
    };
    audio.onpause = () => {
      if (stopPlaybackRequested) {
        finalize(false);
      }
    };
    audio.onerror = () => {
      finalize(true, new Error("browser failed to decode TTS audio"));
    };
    audio.play().catch((err) => {
      if (stopPlaybackRequested) {
        finalize(false);
      } else {
        finalize(true, err);
      }
    });
  });
}

// Play server-provided audio segments sequentially. Output: promise. Input: TTS segment array.
async function playServerAudioSegments(segments) {
  for (const segment of segments) {
    if (stopPlaybackRequested) {
      break;
    }
    if (!segment.audio_b64) {
      continue;
    }
    const blob = base64ToBlob(segment.audio_b64, segment.mime_type || "audio/wav");
    await playBlob(blob);
  }
}

// Update voice state label. Output: none. Input: state text.
function setVoiceState(text) {
  voiceStateNode.textContent = `State: ${text}`;
  syncControlButtons();
}

// Update microphone state label. Output: none. Input: state text.
function setMicState(text) {
  micStateNode.textContent = `Microphone: ${text}`;
  syncControlButtons();
}

// Check whether websocket session is currently connected. Output: boolean. Input: none.
function isSessionConnected() {
  return !!socket && socket.readyState === WebSocket.OPEN;
}

// Check whether websocket session is currently connecting. Output: boolean. Input: none.
function isSessionConnecting() {
  return !!socket && socket.readyState === WebSocket.CONNECTING;
}

// Check whether microphone recorder is currently active. Output: boolean. Input: none.
function isMicrophoneRecording() {
  return !!mediaRecorder && mediaRecorder.state === "recording";
}

// Update control button states based on session/microphone status. Output: none. Input: none.
function syncControlButtons() {
  if (connectButton) {
    connectButton.disabled = isSessionConnected() || isSessionConnecting();
  }
  if (startButton) {
    startButton.disabled = !isSessionConnected() || isMicrophoneRecording();
  }
  if (stopButton) {
    stopButton.disabled = !isMicrophoneRecording();
  }
}

// Request backend-triggered full system reset. Output: none. Input: none.
async function resetSystem() {
  if (!resetSystemButton) {
    return;
  }
  const confirmed = window.confirm("This will trigger full system restart. Continue?");
  if (!confirmed) {
    return;
  }

  resetSystemButton.disabled = true;
  try {
    const response = await fetch("/api/system-reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "manual-ui-request" }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.message || `HTTP ${response.status}`);
    }
    appendEvent(`System reset requested: ${payload.message || "accepted"}.`);
  } catch (err) {
    appendEvent(`System reset failed: ${err.message || err}`);
  } finally {
    resetSystemButton.disabled = false;
  }
}

// Update playback state badge. Output: none. Input: state key and optional label.
function setPlaybackState(state, label) {
  if (!playbackStateNode) {
    return;
  }

  playbackStateNode.classList.remove("idle", "playing", "error");
  playbackStateNode.classList.add(state);
  playbackStateNode.textContent = `Playback: ${label || state}`;
}

// Set first-column width based on pointer X coordinate. Output: none. Input: clientX.
function applyLayoutResize(clientX) {
  if (!layoutNode || window.matchMedia("(max-width: 1979px)").matches) {
    return;
  }
  const rect = layoutNode.getBoundingClientRect();
  const minWidth = 320;
  const maxWidth = Math.max(minWidth, rect.width - 420);
  const px = clamp(clientX - rect.left, minWidth, maxWidth);
  document.documentElement.style.setProperty("--voice-col-width", `${Math.round(px)}px`);
}

// Start dragging layout splitter. Output: none. Input: mouse event.
function onLayoutResizeStart(event) {
  if (!layoutNode || window.matchMedia("(max-width: 1979px)").matches) {
    return;
  }
  isResizingLayout = true;
  document.body.style.userSelect = "none";
  applyLayoutResize(event.clientX);
}

// Update layout splitter drag. Output: none. Input: mouse event.
function onLayoutResizeMove(event) {
  if (!isResizingLayout) {
    return;
  }
  applyLayoutResize(event.clientX);
}

// Finish layout splitter drag. Output: none. Input: none.
function onLayoutResizeEnd() {
  if (!isResizingLayout) {
    return;
  }
  isResizingLayout = false;
  document.body.style.userSelect = "";
}

// Clamp value to inclusive range. Output: clamped number. Input: value, min, max.
function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

const VAD_LOG_MIN = 0.001;
const VAD_LOG_MAX = 0.5;
const VAD_LOG_STEPS = 1000;

// Map linear slider position [0..VAD_LOG_STEPS] to logarithmic speed [VAD_LOG_MIN..VAD_LOG_MAX]. Output: speed value. Input: integer slider position.
function vadSliderToSpeed(positionStr) {
  const t = clamp(parseInt(positionStr, 10), 0, VAD_LOG_STEPS) / VAD_LOG_STEPS;
  return clamp(
    Math.exp(Math.log(VAD_LOG_MIN) + t * (Math.log(VAD_LOG_MAX) - Math.log(VAD_LOG_MIN))),
    VAD_LOG_MIN,
    VAD_LOG_MAX
  );
}

// Map speed value to integer slider position for logarithmic scale. Output: integer position [0..VAD_LOG_STEPS]. Input: speed value.
function vadSpeedToSlider(speed) {
  const s = clamp(speed, VAD_LOG_MIN, VAD_LOG_MAX);
  const t = (Math.log(s) - Math.log(VAD_LOG_MIN)) / (Math.log(VAD_LOG_MAX) - Math.log(VAD_LOG_MIN));
  return Math.round(clamp(t, 0, 1) * VAD_LOG_STEPS);
}

// Format speed value for display: 3 decimal places for sub-0.01, 2 otherwise. Output: string. Input: speed number.
function formatVadSpeed(speed) {
  return speed < 0.01 ? speed.toFixed(3) : speed.toFixed(2);
}

// Read current VAD relaxation speed from logarithmic slider. Output: number in [VAD_LOG_MIN, VAD_LOG_MAX]. Input: none.
function getVadRelaxationSpeed() {
  return vadSliderToSpeed(vadRelaxationInput.value);
}

// Read current VAD activation speed from logarithmic slider. Output: number in [VAD_LOG_MIN, VAD_LOG_MAX]. Input: none.
function getVadActivationSpeed() {
  return vadSliderToSpeed(vadActivationInput.value);
}

// Sync labels near VAD sliders. Output: none. Input: none.
function renderVadControls() {
  vadRelaxationValueNode.textContent = formatVadSpeed(getVadRelaxationSpeed());
  vadActivationValueNode.textContent = formatVadSpeed(getVadActivationSpeed());
  if (vadClosePauseValueNode) {
    vadClosePauseValueNode.textContent = `${getVadClosePauseSec().toFixed(2)} s`;
  }
}

// Read configured pause before utterance close in seconds. Output: seconds. Input: none.
function getVadClosePauseSec() {
  if (!vadClosePauseInput) {
    return 0.6;
  }
  const ms = clamp(parseInt(vadClosePauseInput.value, 10), 200, 2000);
  return ms / 1000;
}

// Update short calibration state label. Output: none. Input: state text.
function setCalibrationState(text) {
  vadCalibrationStateNode.textContent = `Calibration: ${text}`;
}

// Estimate immediate normalized signal level from analyser time-domain data. Output: signal value in [0,1]. Input: none.
function estimateSignalLevel() {
  if (!analyser) {
    return 0;
  }

  if (!timeDomainData) {
    timeDomainData = new Uint8Array(analyser.fftSize);
  }

  analyser.getByteTimeDomainData(timeDomainData);
  let sum = 0;
  for (let i = 0; i < timeDomainData.length; i += 1) {
    const normalized = (timeDomainData[i] - 128) / 128;
    sum += normalized * normalized;
  }

  const rms = Math.sqrt(sum / timeDomainData.length);
  return clamp(rms * 4.2, 0, 1);
}

// Apply preset values to VAD sliders and floor model. Output: none. Input: preset key.
function applyVadPreset(presetKey) {
  const preset = VAD_PRESETS[presetKey];
  if (!preset) {
    return;
  }

  vadRelaxationInput.value = vadSpeedToSlider(preset.relaxation);
  vadActivationInput.value = vadSpeedToSlider(preset.activation);
  vadFloorMin = preset.floorMin;
  vadThreshold = clamp(Math.max(vadThreshold, vadFloorMin), 0.04, 0.95);
  renderVadControls();
  appendEvent(`Applied VAD preset: ${presetKey}.`);
}

// Handle manual slider edits and mark custom preset. Output: none. Input: none.
function onVadSliderChange() {
  if (vadPresetNode.value !== "custom") {
    vadPresetNode.value = "custom";
  }
  renderVadControls();
}

// Auto-calibrate VAD floor from ambient silence for 2.5 seconds. Output: none. Input: none.
async function autoCalibrateSilence() {
  if (!analyser || !mediaRecorder || mediaRecorder.state !== "recording") {
    appendEvent("Auto-calibration needs active microphone recording.");
    setCalibrationState("start microphone first");
    return;
  }

  if (isCalibrating) {
    return;
  }

  isCalibrating = true;
  vadCalibrateButton.disabled = true;
  setCalibrationState("listening 2.5s");
  appendEvent("VAD auto-calibration started. Keep silence for 2.5 seconds.");

  const samples = [];
  const sampleStart = performance.now();

  await new Promise((resolve) => {
    const timerId = window.setInterval(() => {
      samples.push(estimateSignalLevel());
      if (performance.now() - sampleStart >= 2500) {
        window.clearInterval(timerId);
        resolve();
      }
    }, 40);
  });

  samples.sort((a, b) => a - b);
  const p80 = samples.length > 0 ? samples[Math.floor(samples.length * 0.8)] : 0.04;
  const avg = samples.length > 0 ? samples.reduce((acc, value) => acc + value, 0) / samples.length : 0.04;
  const calibratedFloor = clamp(Math.max(avg * 1.25 + 0.02, p80 * 1.08 + 0.01), 0.02, 0.22);

  vadFloorMin = calibratedFloor;
  vadThreshold = clamp(Math.max(vadThreshold, vadFloorMin), 0.04, 0.95);

  setCalibrationState(`done (${Math.round(vadFloorMin * 100)}%)`);
  appendEvent(`VAD auto-calibration done. Noise floor: ${(vadFloorMin * 100).toFixed(1)}%.`);

  isCalibrating = false;
  vadCalibrateButton.disabled = false;
}

// Update mic level, VAD level, and threshold indicators. Output: none. Input: normalized levels.
function renderVadIndicators(signalLevel, currentVadLevel, currentThreshold) {
  const micPercent = Math.round(clamp(signalLevel, 0, 1) * 100);
  const vadPercent = Math.round(clamp(currentVadLevel, 0, 1) * 100);
  const thresholdPercent = Math.round(clamp(currentThreshold, 0, 1) * 100);

  vuMeterNode.style.width = `${micPercent}%`;
  micLevelValueNode.textContent = `${micPercent}%`;

  vadMeterNode.style.width = `${vadPercent}%`;
  vadLevelValueNode.textContent = `${vadPercent}%`;

  vadThresholdMeterNode.style.width = `${thresholdPercent}%`;
  vadThresholdValueNode.textContent = `${thresholdPercent}%`;

  if (vadPercent >= 35) {
    vadStateNode.textContent = "VAD: speech";
    vadStateNode.classList.add("active");
    vadStateNode.classList.remove("idle");
  } else {
    vadStateNode.textContent = "VAD: waiting";
    vadStateNode.classList.add("idle");
    vadStateNode.classList.remove("active");
  }
}

// Ensure spectrogram internal buffer matches rendered size. Output: none. Input: none.
function syncSpectrogramSize() {
  if (!spectrogramNode) {
    return;
  }

  const width = Math.max(320, Math.floor(spectrogramNode.clientWidth));
  const height = 180;
  if (spectrogramNode.width !== width || spectrogramNode.height !== height) {
    spectrogramNode.width = width;
    spectrogramNode.height = height;
  }
}

// Draw one scrolling spectrogram column from FFT data. Output: none. Input: Uint8Array frequency magnitudes.
function drawSpectrogramColumn(freq) {
  if (!spectrogramContext || !spectrogramNode || !freq) {
    return;
  }

  syncSpectrogramSize();
  const width = spectrogramNode.width;
  const height = spectrogramNode.height;

  spectrogramContext.drawImage(spectrogramNode, -1, 0);

  for (let y = 0; y < height; y += 1) {
    const normalizedY = 1 - y / Math.max(1, height - 1);
    const binIndex = Math.floor(normalizedY * (freq.length - 1));
    const magnitude = freq[binIndex] / 255;
    const hue = 210 - Math.round(160 * magnitude);
    const lightness = 8 + Math.round(58 * magnitude);
    spectrogramContext.fillStyle = `hsl(${hue} 90% ${lightness}%)`;
    spectrogramContext.fillRect(width - 1, y, 1, 1);
  }
}

// Connect backend websocket session. Output: none. Input: none.
function connectSession() {
  if (socket && socket.readyState === WebSocket.OPEN) {
    appendEvent("Session is already connected.");
    return;
  }

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
  setVoiceState("connecting");
  syncControlButtons();

  socket.addEventListener("open", () => {
    setVoiceState("connected");
    appendEvent("WebSocket connected.");
    syncControlButtons();
  });

  socket.addEventListener("message", (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      appendEvent(`Backend raw: ${event.data}`);
      return;
    }

    switch (msg.type) {
      case "session.ready":
        appendEvent(`Backend: ${msg.message}`);
        break;
      case "session.config.ack":
        if (reasoningToggle) {
          reasoningToggle.checked = !!msg.reasoning;
        }
        if (temperatureInput && Number.isFinite(Number(msg.temperature))) {
          temperatureInput.value = clamp(Number(msg.temperature), 0, 2).toFixed(2);
          renderTemperatureValue();
        }
        appendEvent(`Config ack: role=${msg.role}${msg.system_prompt ? ", prompt set" : ""}, temperature=${Number.isFinite(Number(msg.temperature)) ? Number(msg.temperature).toFixed(2) : "0.70"}, reasoning=${!!msg.reasoning}.`);
        break;
      case "stt.result":
        appendEvent(`STT: ${msg.text || "(empty)"}`);
        break;
      case "llm.start":
        interruptPlaybackForNewLlmResponse();
        break;
      case "llm.token":
        appendLlmToken(msg.token || "");
        break;
      case "llm.done":
        finalizeLlmResponse(msg.text || "");
        break;
      case "tts.result":
        playTtsPayload(msg.payload || {});
        break;
      case "llm.warn":
        appendEvent(`⚠ ${msg.message}`);
        break;
      case "error":
        appendEvent(`Error: ${msg.message}`);
        break;
      default:
        appendEvent(`Backend: ${JSON.stringify(msg)}`);
    }
  });

  socket.addEventListener("close", () => {
    if (isMicrophoneRecording()) {
      stopMicrophone();
    }
    setVoiceState("disconnected");
    appendEvent("WebSocket closed.");
    syncControlButtons();
  });

  socket.addEventListener("error", () => {
    setVoiceState("error");
    appendEvent("WebSocket error.");
    syncControlButtons();
  });
}

// Connect dedicated websocket stream for system log panel. Output: none. Input: none.
async function fetchSystemLogSnapshot() {
  if (systemLogPollingInFlight) {
    return;
  }
  systemLogPollingInFlight = true;
  try {
    const response = await fetch("/api/log");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    setSystemLog(payload.lines || []);
  } catch (err) {
    if (systemLogNode && (!systemLogNode.textContent || systemLogNode.textContent === "No system log yet.")) {
      systemLogNode.textContent = `System log snapshot unavailable: ${err.message || err}`;
    }
  } finally {
    systemLogPollingInFlight = false;
  }
}

// Schedule next system log poll using current UI interval. Output: none. Input: none.
function scheduleSystemLogPoll() {
  if (systemLogPollTimer) {
    window.clearTimeout(systemLogPollTimer);
  }
  systemLogPollTimer = window.setTimeout(async () => {
    await fetchSystemLogSnapshot();
    scheduleSystemLogPoll();
  }, getSystemLogPollIntervalMs());
}

// Start periodic system log polling. Output: none. Input: none.
function startSystemLogPolling() {
  renderSystemLogPollInterval();
  fetchSystemLogSnapshot();
  scheduleSystemLogPoll();
}

// Start microphone visualization, adaptive VAD update, and spectrum rendering loop. Output: none. Input: none.
function startAudioVisualization() {
  if (!analyser) {
    return;
  }

  timeDomainData = new Uint8Array(analyser.fftSize);
  frequencyData = new Uint8Array(analyser.frequencyBinCount);

  if (spectrogramNode) {
    spectrogramContext = spectrogramNode.getContext("2d", { alpha: false });
    syncSpectrogramSize();
    if (spectrogramContext) {
      spectrogramContext.fillStyle = "#06101a";
      spectrogramContext.fillRect(0, 0, spectrogramNode.width, spectrogramNode.height);
    }
  }

  const tick = () => {
    analyser.getByteTimeDomainData(timeDomainData);
    analyser.getByteFrequencyData(frequencyData);

    let sum = 0;
    for (let i = 0; i < timeDomainData.length; i += 1) {
      const normalized = (timeDomainData[i] - 128) / 128;
      sum += normalized * normalized;
    }

    const rms = Math.sqrt(sum / timeDomainData.length);
    const signalLevel = clamp(rms * 4.2, 0, 1);

    const relaxationSpeed = getVadRelaxationSpeed();
    const activationSpeed = getVadActivationSpeed();

    // Show raw microphone level without smoothing.
    micLevel = signalLevel;

    const floorTarget = vadFloorMin + signalLevel * 0.22;
    if (signalLevel > vadThreshold) {
      vadThreshold += (signalLevel - vadThreshold) * activationSpeed;
    } else {
      vadThreshold += (floorTarget - vadThreshold) * relaxationSpeed;
    }
    vadThreshold = clamp(vadThreshold, Math.max(0.04, vadFloorMin), 0.95);

    const vadRaw = clamp((signalLevel - vadThreshold + 0.06) * 5, 0, 1);
    if (vadRaw > vadLevel) {
      vadLevel += (vadRaw - vadLevel) * activationSpeed;
    } else {
      vadLevel += (vadRaw - vadLevel) * relaxationSpeed;
    }

    renderVadIndicators(micLevel, vadLevel, vadThreshold);
    drawSpectrogramColumn(frequencyData);
    chunkVadPeak = Math.max(chunkVadPeak, vadLevel);
    pendingChunkEnvelope.push(signalLevel);
    rafId = requestAnimationFrame(tick);
  };

  tick();
}

// Stop and clear live audio visualization widgets. Output: none. Input: none.
function stopAudioVisualization() {
  if (rafId) {
    cancelAnimationFrame(rafId);
    rafId = null;
  }

  micLevel = 0;
  vadLevel = 0;
  vadThreshold = 0.08;
  vadFloorMin = 0.04;
  timeDomainData = null;
  frequencyData = null;
  chunkVadPeak = 0;
  pendingChunkEnvelope = [];
  utteranceEnvelope = [];
  utteranceChunkEnvelopes = [];
  pendingChunkPcm = [];
  utteranceChunkPcm = [];
  recentChunkEnvelopes = [];
  silentChunksInRow = 0;

  renderVadIndicators(0, 0, 0);

  if (spectrogramContext && spectrogramNode) {
    spectrogramContext.fillStyle = "#06101a";
    spectrogramContext.fillRect(0, 0, spectrogramNode.width, spectrogramNode.height);
  }
}

// Start microphone capture and chunk forwarding. Output: none. Input: none.
async function startMicrophone() {
  try {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      appendEvent("Connect session first.");
      return;
    }

    if (mediaRecorder && mediaRecorder.state === "recording") {
      appendEvent("Microphone is already active.");
      return;
    }

    if (typeof MediaRecorder === "undefined") {
      throw new Error("MediaRecorder is not available in this browser.");
    }

    mediaStream = await requestMicrophoneStream(selectedInputDeviceId);
    await refreshDeviceLists();
    audioContext = createAudioContext();
    const source = audioContext.createMediaStreamSource(mediaStream);
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);

    pcmCaptureSampleRate = audioContext.sampleRate;
    pcmProcessorNode = audioContext.createScriptProcessor(2048, 1, 1);
    pcmSinkNode = audioContext.createGain();
    pcmSinkNode.gain.value = 0;
    pcmProcessorNode.onaudioprocess = (procEvent) => {
      const input = procEvent.inputBuffer.getChannelData(0);
      for (let i = 0; i < input.length; i += PCM_CAPTURE_DOWNSAMPLE) {
        pendingChunkPcm.push(input[i]);
      }
    };
    source.connect(pcmProcessorNode);
    pcmProcessorNode.connect(pcmSinkNode);
    pcmSinkNode.connect(audioContext.destination);

    selectedRecorderMimeType = pickRecorderMimeType();
    mediaRecorder = selectedRecorderMimeType
      ? new MediaRecorder(mediaStream, { mimeType: selectedRecorderMimeType })
      : new MediaRecorder(mediaStream);
    appendEvent(`Recorder format: ${selectedRecorderMimeType || mediaRecorder.mimeType || "browser default"}.`);

    mediaRecorder.addEventListener("dataavailable", async (event) => {
      if (!event.data || event.data.size === 0) {
        chunkVadPeak = 0;
        return;
      }

      const peakThisChunk = chunkVadPeak;
      chunkVadPeak = 0;
      const preRollChunks = recentChunks.slice(-VAD_PRE_ROLL_CHUNKS);
      const chunkEnvelope = pendingChunkEnvelope.slice();
      pendingChunkEnvelope = [];
      const chunkPcm = pendingChunkPcm.slice();
      pendingChunkPcm = [];
      const preRollEnvelopes = recentChunkEnvelopes.slice(-VAD_PRE_ROLL_CHUNKS);

      if (peakThisChunk >= VAD_UTTERANCE_THRESHOLD) {
        if (!utteranceActive && preRollChunks.length > 0) {
          utteranceBlobs.push(...preRollChunks);
          for (const envelope of preRollEnvelopes) {
            utteranceEnvelope.push(...envelope);
            utteranceChunkEnvelopes.push(envelope.slice());
          }
        }
        utteranceBlobs.push(event.data);
        utteranceEnvelope.push(...chunkEnvelope);
        utteranceChunkEnvelopes.push(chunkEnvelope.slice());
        utteranceChunkPcm.push(chunkPcm.slice());
        utteranceActive = true;
        silentChunksInRow = 0;
        appendEvent(`Voice chunk accepted by VAD (peak ${(peakThisChunk * 100).toFixed(0)}%).`);
      } else if (utteranceActive) {
        // Keep collecting all chunks inside an active utterance; trim only the final payload before send.
        utteranceBlobs.push(event.data);
        utteranceEnvelope.push(...chunkEnvelope);
        utteranceChunkEnvelopes.push(chunkEnvelope.slice());
        utteranceChunkPcm.push(chunkPcm.slice());
        silentChunksInRow += 1;
        const silenceChunksNeeded = Math.max(1, Math.ceil((getVadClosePauseSec() * 1000) / MEDIA_CHUNK_MS));
        if (silentChunksInRow >= silenceChunksNeeded) {
          utteranceActive = false;
          silentChunksInRow = 0;
          await sendUtterance();
        }
      } else {
        silentChunksInRow = 0;
        appendEvent(`Voice chunk skipped by VAD (peak ${(peakThisChunk * 100).toFixed(0)}% < threshold).`);
      }

      recentChunks.push(event.data);
      if (recentChunks.length > VAD_PRE_ROLL_CHUNKS) {
        recentChunks.shift();
      }
      recentChunkEnvelopes.push(chunkEnvelope);
      if (recentChunkEnvelopes.length > VAD_PRE_ROLL_CHUNKS) {
        recentChunkEnvelopes.shift();
      }
    });

    mediaRecorder.start(MEDIA_CHUNK_MS);
    startAudioVisualization();
    setMicState("recording");
    appendEvent("Microphone started.");
    syncControlButtons();
  } catch (error) {
    setMicState("error");
    const details = error && error.message ? error.message : String(error);
    appendEvent(`Microphone error: ${details}`);
    const caps = getAudioCapabilities();
    if (!caps.isSecureContext && !caps.isLocalHost) {
      appendEvent("Tip: microphone APIs require HTTPS or localhost.");
    }
    if (caps.isEmbeddedFrame) {
      appendEvent("Tip: this page is running in an embedded frame; open it directly in your browser.");
    }
    if (!caps.hasModernGetUserMedia && !caps.hasLegacyGetUserMedia) {
      appendEvent("Tip: getUserMedia API is missing in this runtime. Check browser privacy mode, policies, or preview sandbox.");
    }
    logAudioCapabilities("Microphone diagnostics");
    syncControlButtons();
  }
}

// Stop microphone capture session. Output: none. Input: none.
function stopMicrophone() {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
  }

  if (mediaStream) {
    mediaStream.getTracks().forEach((track) => track.stop());
    mediaStream = null;
  }

  if (audioContext) {
    if (pcmProcessorNode) {
      pcmProcessorNode.disconnect();
      pcmProcessorNode.onaudioprocess = null;
      pcmProcessorNode = null;
    }
    if (pcmSinkNode) {
      pcmSinkNode.disconnect();
      pcmSinkNode = null;
    }
    audioContext.close();
    audioContext = null;
  }

  analyser = null;
  stopAudioVisualization();
  recentChunks = [];
  recentChunkEnvelopes = [];
  utteranceChunkEnvelopes = [];
  pendingChunkPcm = [];
  utteranceChunkPcm = [];
  silentChunksInRow = 0;
  setMicState("stopped");
  appendEvent("Microphone stopped.");
  syncControlButtons();

  if (utteranceActive && utteranceBlobs.length > 0) {
    utteranceActive = false;
    sendUtterance();
  }
}

connectButton.addEventListener("click", connectSession);
startButton.addEventListener("click", () => { startMicrophone(); });
stopButton.addEventListener("click", stopMicrophone);
if (resetSystemButton) {
  resetSystemButton.addEventListener("click", resetSystem);
}
playChunkButton.addEventListener("click", playLastChunk);
if (stopPlaybackButton) {
  stopPlaybackButton.addEventListener("click", stopPlayback);
}
if (layoutSplitter) {
  layoutSplitter.addEventListener("mousedown", onLayoutResizeStart);
}
window.addEventListener("mousemove", onLayoutResizeMove);
window.addEventListener("mouseup", onLayoutResizeEnd);
if (refreshDevicesButton) {
  refreshDevicesButton.addEventListener("click", () => {
    refreshDeviceLists();
  });
}
if (inputDeviceSelect) {
  inputDeviceSelect.addEventListener("change", () => {
    selectedInputDeviceId = inputDeviceSelect.value || "";
    appendEvent(`Microphone device selected: ${inputDeviceSelect.options[inputDeviceSelect.selectedIndex]?.textContent || "default"}.`);
  });
}
if (outputDeviceSelect) {
  outputDeviceSelect.addEventListener("change", () => {
    selectedOutputDeviceId = outputDeviceSelect.value || "";
    appendEvent(`Playback device selected: ${outputDeviceSelect.options[outputDeviceSelect.selectedIndex]?.textContent || "default"}.`);
  });
}
sendTextButton.addEventListener("click", sendText);
applyConfigButton.addEventListener("click", sendSessionConfig);
textInputNode.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendText();
  }
});
vadRelaxationInput.addEventListener("input", onVadSliderChange);
vadActivationInput.addEventListener("input", onVadSliderChange);
if (vadClosePauseInput) {
  vadClosePauseInput.addEventListener("input", onVadSliderChange);
}
vadPresetNode.addEventListener("change", () => {
  if (vadPresetNode.value === "custom") {
    appendEvent("VAD preset switched to custom.");
    return;
  }
  applyVadPreset(vadPresetNode.value);
});
vadCalibrateButton.addEventListener("click", () => {
  autoCalibrateSilence();
});
window.addEventListener("resize", syncSpectrogramSize);
window.addEventListener("resize", syncPlaybackSpectrumSize);

appendEvent("Voice interface is ready.");
logAudioCapabilities();
syncControlButtons();
refreshDeviceLists();
startSystemLogPolling();
if (navigator.mediaDevices && typeof navigator.mediaDevices.addEventListener === "function") {
  navigator.mediaDevices.addEventListener("devicechange", () => {
    refreshDeviceLists();
  });
}
if (systemLogPollIntervalInput) {
  systemLogPollIntervalInput.addEventListener("input", () => {
    renderSystemLogPollInterval();
    scheduleSystemLogPoll();
  });
}
if (temperatureInput) {
  temperatureInput.addEventListener("input", renderTemperatureValue);
}
if (autoloadToggle) {
  autoloadToggle.addEventListener("change", () => {
    setAutoloadEnabled(autoloadToggle.checked);
  });
}
applyVadPreset("near");
renderVadIndicators(0, 0, 0);
setCalibrationState("idle");
renderTemperatureValue();
stopPlaybackSpectrum();
setPlaybackState("idle", "idle");
refreshAutoloadStatus();