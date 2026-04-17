const heartbeatNode = document.getElementById("heartbeat");
const hostMemoryRamNode = document.getElementById("host-memory-ram");
const hostMemoryVramNode = document.getElementById("host-memory-vram");
const modelsOllamaNode = document.getElementById("models-ollama");
const modelsServicesNode = document.getElementById("models-services");
const servicesNode = document.getElementById("services");

function prettyJson(data) {
  return JSON.stringify(data, null, 2);
}

function statusClass(status) {
  if (status === "ok") {
    return "status-ok";
  }
  if (status === "down") {
    return "status-down";
  }
  return "status-unknown";
}

function renderServices(services) {
  servicesNode.innerHTML = "";
  services.forEach((service) => {
    const card = document.createElement("article");
    card.className = "service";

    const details = {
      latency_ms: service.latency_ms,
      memory_mb: service.memory_mb,
      requests_total: service.requests_total,
      requests_by_path: service.requests_by_path,
      models: service.models || {},
      enabled_tools: service.enabled_tools || [],
      details: service.details || {},
    };

    card.innerHTML = `
      <div><strong>${service.name}</strong></div>
      <div class="status ${statusClass(service.health)}">${service.health}</div>
      <pre>${prettyJson(details)}</pre>
    `;
    servicesNode.appendChild(card);
  });
}

function formatMemoryInfo(memInfo) {
  if (!memInfo) return "No data";
  if (memInfo.error) return `Error: ${memInfo.error}`;
  
  const lines = [];
  lines.push(`Total: ${memInfo.total_mb} MB`);
  lines.push(`Used:  ${memInfo.used_mb} MB (${memInfo.used_percent}%)`);
  lines.push(`Available: ${memInfo.available_mb} MB`);
  return lines.join("\n");
}

function formatServiceModels(models) {
  if (!models || Object.keys(models).length === 0) {
    return "No models loaded";
  }

  const lines = [];
  
  // Format piper models
  if (models.piper) {
    lines.push("Piper:");
    Object.entries(models.piper).forEach(([key, value]) => {
      if (value.device !== undefined) {
        lines.push(`  ${key}:`);
        lines.push(`    Device: ${value.device.toUpperCase()}`);
        lines.push(`    Memory: ${value.memory_mb} MB`);
        if (value.active_voice) lines.push(`    Active: ${value.active_voice}`);
        if (value.cached_voices?.length > 0) lines.push(`    Cached: ${value.cached_voices.join(", ")}`);
      }
    });
  }

  // Format kokoro
  if (models.kokoro) {
    lines.push("Kokoro:");
    lines.push(`  Device: ${(models.kokoro.device || "cpu").toUpperCase()}`);
    lines.push(`  Memory: ${models.kokoro.memory_mb} MB`);
  }

  // Format silero
  if (models.silero) {
    lines.push("Silero:");
    lines.push(`  Device: ${(models.silero.device || "cpu").toUpperCase()}`);
    lines.push(`  Memory: ${models.silero.memory_mb} MB`);
    if (models.silero.models_loaded?.length > 0) {
      lines.push(`  Loaded: ${models.silero.models_loaded.join(", ")}`);
    }
  }

  // Format STT
  if (models.stt) {
    lines.push("STT:");
    lines.push(`  Device: ${(models.stt.device || "gpu").toUpperCase()}`);
    lines.push(`  Memory: ${models.stt.memory_mb} MB`);
    lines.push(`  Model: ${models.stt.model}`);
    if (models.stt.compute_type) lines.push(`  Compute: ${models.stt.compute_type}`);
  }

  return lines.join("\n");
}

function connectStatusWebSocket() {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${scheme}//${window.location.host}/ws/system-status`);

  socket.addEventListener("open", () => {
    heartbeatNode.textContent = "Connected. Waiting for updates...";
  });

  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    heartbeatNode.textContent = `Last update: ${payload.timestamp}`;
    
    // Display RAM and VRAM separately
    if (payload.host_memory) {
      hostMemoryRamNode.textContent = prettyJson(payload.host_memory.ram || {});
      hostMemoryVramNode.textContent = formatMemoryInfo(payload.host_memory.vram);
    }
    
    // Display model info
    if (payload.models) {
      modelsOllamaNode.textContent = prettyJson(payload.models.ollama || {});
      modelsServicesNode.textContent = formatServiceModels(payload.models.services || {});
    } else {
      modelsOllamaNode.textContent = "No model data";
      modelsServicesNode.textContent = "No model data";
    }
    
    renderServices(payload.services || []);
  });

  socket.addEventListener("close", () => {
    heartbeatNode.textContent = "Disconnected. Reconnecting in 2s...";
    setTimeout(connectStatusWebSocket, 2000);
  });
}

connectStatusWebSocket();