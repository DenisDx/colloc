const heartbeatNode = document.getElementById("heartbeat");
const hostMemoryNode = document.getElementById("host-memory");
const modelsNode = document.getElementById("models");
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

function connectStatusWebSocket() {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${scheme}//${window.location.host}/ws/system-status`);

  socket.addEventListener("open", () => {
    heartbeatNode.textContent = "Connected. Waiting for updates...";
  });

  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    heartbeatNode.textContent = `Last update: ${payload.timestamp}`;
    hostMemoryNode.textContent = prettyJson(payload.host_memory || {});
    modelsNode.textContent = prettyJson(payload.runtime || {});
    renderServices(payload.services || []);
  });

  socket.addEventListener("close", () => {
    heartbeatNode.textContent = "Disconnected. Reconnecting in 2s...";
    setTimeout(connectStatusWebSocket, 2000);
  });
}

connectStatusWebSocket();