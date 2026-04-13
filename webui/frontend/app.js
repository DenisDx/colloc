const connectButton = document.getElementById("connect-btn");
const startButton = document.getElementById("start-btn");
const stopButton = document.getElementById("stop-btn");
const voiceStateNode = document.getElementById("voice-state");
const micStateNode = document.getElementById("mic-state");
const vuMeterNode = document.getElementById("vu-meter");
const chatLogNode = document.getElementById("chat-log");

let socket = null;
let mediaStream = null;
let mediaRecorder = null;
let audioContext = null;
let analyser = null;
let rafId = null;

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

// Request microphone stream with modern and legacy browser APIs. Output: media stream. Input: none.
async function requestMicrophoneStream() {
  if (navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === "function") {
    return navigator.mediaDevices.getUserMedia({ audio: true });
  }

  const legacyGetUserMedia = navigator.getUserMedia
    || navigator.webkitGetUserMedia
    || navigator.mozGetUserMedia;

  if (typeof legacyGetUserMedia === "function") {
    return new Promise((resolve, reject) => {
      legacyGetUserMedia.call(navigator, { audio: true }, resolve, reject);
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

// Build audio context in a cross-browser way. Output: audio context instance. Input: none.
function createAudioContext() {
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) {
    throw new Error("AudioContext is not available in this browser.");
  }
  return new Context();
}

// Append one event line. Output: none. Input: text line.
function appendEvent(line) {
  const timestamp = new Date().toLocaleTimeString();
  chatLogNode.textContent = `${chatLogNode.textContent}\n[${timestamp}] ${line}`.trim();
  chatLogNode.scrollTop = chatLogNode.scrollHeight;
}

// Update voice state label. Output: none. Input: state text.
function setVoiceState(text) {
  voiceStateNode.textContent = `State: ${text}`;
}

// Update microphone state label. Output: none. Input: state text.
function setMicState(text) {
  micStateNode.textContent = `Microphone: ${text}`;
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

  socket.addEventListener("open", () => {
    setVoiceState("connected");
    appendEvent("WebSocket connected.");
  });

  socket.addEventListener("message", (event) => {
    appendEvent(`Backend: ${event.data}`);
  });

  socket.addEventListener("close", () => {
    setVoiceState("disconnected");
    appendEvent("WebSocket closed.");
  });

  socket.addEventListener("error", () => {
    setVoiceState("error");
    appendEvent("WebSocket error.");
  });
}

// Draw VU meter from analyser node. Output: none. Input: none.
function startVuMeter() {
  if (!analyser) {
    return;
  }

  const data = new Uint8Array(analyser.fftSize);

  const tick = () => {
    analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i += 1) {
      const normalized = (data[i] - 128) / 128;
      sum += normalized * normalized;
    }
    const rms = Math.sqrt(sum / data.length);
    const level = Math.min(100, Math.round(rms * 300));
    vuMeterNode.style.width = `${level}%`;
    rafId = requestAnimationFrame(tick);
  };

  tick();
}

// Stop and clear VU meter. Output: none. Input: none.
function stopVuMeter() {
  if (rafId) {
    cancelAnimationFrame(rafId);
    rafId = null;
  }
  vuMeterNode.style.width = "0%";
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

    mediaStream = await requestMicrophoneStream();
    audioContext = createAudioContext();
    const source = audioContext.createMediaStreamSource(mediaStream);
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);

    mediaRecorder = new MediaRecorder(mediaStream);

    mediaRecorder.addEventListener("dataavailable", async (event) => {
      if (!event.data || event.data.size === 0) {
        return;
      }

      const bytes = new Uint8Array(await event.data.arrayBuffer());
      const sample = Array.from(bytes.slice(0, 48));
      socket.send(JSON.stringify({ type: "voice.chunk", bytes: bytes.byteLength, sample }));
      appendEvent(`Sent voice chunk (${bytes.byteLength} bytes).`);
    });

    mediaRecorder.start(1000);
    startVuMeter();
    setMicState("recording");
    appendEvent("Microphone started.");
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
    audioContext.close();
    audioContext = null;
  }

  analyser = null;
  stopVuMeter();
  setMicState("stopped");
  appendEvent("Microphone stopped.");
}

connectButton.addEventListener("click", connectSession);
startButton.addEventListener("click", () => {
  startMicrophone();
});
stopButton.addEventListener("click", stopMicrophone);

appendEvent("Voice interface is ready.");
logAudioCapabilities();