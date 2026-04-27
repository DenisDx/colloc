const heartbeatNode = document.getElementById("heartbeat");
const hostMemoryRamNode = document.getElementById("host-memory-ram");
const hostMemoryVramNode = document.getElementById("host-memory-vram");
const modelsOllamaNode = document.getElementById("models-ollama");
const modelsServicesNode = document.getElementById("models-services");
const servicesNode = document.getElementById("services");
const asteriskHealthNode = document.getElementById("asterisk-health");
const asteriskLatencyNode = document.getElementById("asterisk-latency");
const asteriskActiveCallsNode = document.getElementById("asterisk-active-calls");
const asteriskCallsBodyNode = document.getElementById("asterisk-calls-body");
const asteriskRecentEventsNode = document.getElementById("asterisk-recent-events");
const asteriskConfigNode = document.getElementById("asterisk-config");

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

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
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

function renderAsteriskDiagnostics(service) {
  if (!service) {
    asteriskHealthNode.textContent = "unknown";
    asteriskHealthNode.className = "summary-value status status-unknown";
    asteriskLatencyNode.textContent = "-";
    asteriskActiveCallsNode.textContent = "0";
    asteriskCallsBodyNode.innerHTML = '<tr><td colspan="4">No active calls</td></tr>';
    asteriskRecentEventsNode.textContent = "No events";
    asteriskConfigNode.textContent = "No config data";
    return;
  }

  const details = service.details || {};
  const activeCalls = Array.isArray(details.active_calls) ? details.active_calls : [];
  const recentEvents = Array.isArray(details.recent_events) ? details.recent_events : [];
  const config = details.config || {};

  asteriskHealthNode.textContent = service.health || "unknown";
  asteriskHealthNode.className = `summary-value status ${statusClass(service.health || "unknown")}`;
  asteriskLatencyNode.textContent = Number.isFinite(service.latency_ms) ? `${service.latency_ms} ms` : "-";
  asteriskActiveCallsNode.textContent = String(details.active_calls_count || activeCalls.length || 0);

  if (activeCalls.length === 0) {
    asteriskCallsBodyNode.innerHTML = '<tr><td colspan="4">No active calls</td></tr>';
  } else {
    asteriskCallsBodyNode.innerHTML = activeCalls
      .map((call) => {
        return `
          <tr>
            <td>${escapeHtml(call.channel_id || "-")}</td>
            <td>${escapeHtml(call.caller || "-")}</td>
            <td>${escapeHtml(call.language || "-")}</td>
            <td>${escapeHtml(call.media_port || "-")}</td>
          </tr>
        `;
      })
      .join("");
  }

  asteriskRecentEventsNode.textContent = recentEvents.length > 0 ? recentEvents.join("\n") : "No recent SIP events";

  const configView = {
    ari_app: details.ari_app || null,
    ari_url: details.ari_url || null,
    sip_service_url: details.sip_service_url || null,
    sip_health: details.sip_health || null,
    ari_info: details.ari_info || null,
    ari_error: details.ari_error || null,
    sip_health_error: details.sip_health_error || null,
    active_calls_error: details.active_calls_error || null,
    config,
  };
  asteriskConfigNode.textContent = prettyJson(configView);
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
    
    const services = Array.isArray(payload.services) ? payload.services : [];
    const asteriskService = services.find((service) => service.name === "asterisk");
    renderAsteriskDiagnostics(asteriskService);
    renderServices(services.filter((service) => service.name !== "asterisk"));
  });

  socket.addEventListener("close", () => {
    heartbeatNode.textContent = "Disconnected. Reconnecting in 2s...";
    setTimeout(connectStatusWebSocket, 2000);
  });
}

connectStatusWebSocket();