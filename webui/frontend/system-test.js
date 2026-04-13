const runtimeNode = document.getElementById("runtime");
const socketLogNode = document.getElementById("socket-log");
const messageNode = document.getElementById("message");
const sendButton = document.getElementById("send");

// Append one line to a log node. Output: none. Input: node and text value.
function appendLog(node, value) {
  node.textContent = `${node.textContent}\n${value}`.trim();
}

// Load runtime snapshot. Output: none. Input: none.
async function loadRuntime() {
  const response = await fetch("/api/runtime");
  const payload = await response.json();
  runtimeNode.textContent = JSON.stringify(payload, null, 2);
}

// Connect websocket session. Output: none. Input: none.
function connectSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

  socket.addEventListener("open", () => {
    appendLog(socketLogNode, "socket open");
  });

  socket.addEventListener("message", (event) => {
    appendLog(socketLogNode, event.data);
  });

  socket.addEventListener("close", () => {
    appendLog(socketLogNode, "socket closed");
  });

  sendButton.addEventListener("click", () => {
    if (!messageNode.value) {
      return;
    }
    socket.send(messageNode.value);
    messageNode.value = "";
  });
}

loadRuntime().catch((error) => {
  runtimeNode.textContent = `Runtime request failed: ${error}`;
});

connectSocket();
