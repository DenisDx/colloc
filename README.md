# Colloc

Dockerized toolkit for real-time voice communication with an LLM.

The stack is designed around a streaming pipeline with interruption support:

- Interface -> STT -> LLM -> TTS -> Interface
- LLM is an external service (Ollama, vLLM, TGI, or compatible endpoint)
- Internal services run in Docker Compose

## System Description

The repository provides a first runnable scaffold of the target architecture from SPEC.md.

Core runtime services:

- `gateway` (Nginx): single HTTP/HTTPS entrypoint, static frontend, reverse proxy to API services
- `webui-backend` (FastAPI): REST and WebSocket endpoints used by the web UI
- `stt` (FastAPI): real faster-whisper STT service with external fallback support
- `tts-router` (FastAPI): language-aware routing and server-side synthesis orchestration with provider fallback
- `tools` (FastAPI): tool registry and invocation API scaffold
- `redis`: session/context storage

Optional services:

- `piper-en`, `piper-ru`: per-language Piper FastAPI synthesis endpoints
- `kokoro`: Kokoro FastAPI synthesis endpoint
- `silero`: Silero FastAPI synthesis endpoint
- `asterisk`: SIP endpoint container with baseline configuration
- `telegram-bot`: optional bot process placeholder

Compose profiles:

- `core`: gateway, backend, redis, tools, stt, tts-router
- `tts`: enables optional TTS engines together with per-engine profiles (`tts-piper-en`, `tts-piper-ru`, `tts-kokoro`, `tts-silero`)
- `sip`: Asterisk
- `telegram`: Telegram bot

## Related Documentation

- [SPEC.md](SPEC.md) — Project specification and design
- [AGENTS.md](AGENTS.md) — Agent workflow and development rules

## Recent Changes

- Added secure system restart service (`colloc_service.sh`, `colloc.service`) with webhook-based restart hook (no Docker socket mounting).
- Renamed "Reset system" button to "Restart system" for clarity.
- Added `Stop services` / `Start services` buttons in Web UI to temporarily stop non-core services and restore them.
- Added README section for system service setup and troubleshooting.
- Silero TTS provider with per-language routing and provider fallback.
- LLM -> TTS live streaming: chunks dispatched by soft length threshold for better real-time playback.
- Interruption semantics: new LLM answer clears pending playback queue.

## Repository Layout

- `docker-compose.yml`: main stack
- `docker-compose.override.yml`: local development overrides with hot reload for FastAPI services
- `docker-compose.publish.yml`: optional publication of STT/TTS service ports
- `install.sh`: setup script for project-local directories and systemd service generation
- `colloc_service.sh`: auxiliary system service entry point (manages host-side services)
- `colloc.service`: generated systemd unit (created by `install.sh`, can be installed system-wide)
- `gateway/`: Nginx templates and startup script
- `webui/frontend/`: static frontend
- `webui/backend/`: FastAPI backend
- `services/stt/`: STT FastAPI service
- `services/tts_router/`: TTS routing FastAPI service
- `services/tools/`: tools FastAPI service
- `scripts/restart-service-hook.py`: HTTP hook listener for container-triggered system restarts
- `asterisk/`: Asterisk image and config

## Installation and Startup

### Fresh Install (From Scratch)

Use this section for a clean setup on a new host or new clone.

1. Clone repository and enter directory:

```bash
# run command
git clone <your-repo-url> colloc
# change directory
cd colloc
```

2. Create local environment file:

```bash
# copy file
cp .env.example .env
```

3. Edit `.env` with your values (LLM endpoint/model, TLS settings, optional SIP/Telegram).

4. Prepare all project-local runtime directories and permissions:

```bash
# make script executable
chmod +x install.sh
# run command
./install.sh
```

This step creates required folders in the workspace and ensures writable runtime paths for model preload under `./data`. It also generates `colloc.service` systemd unit file and attempts automatic installation.

### 4.1 System Service Installation (for automatic Restart button)

The `Restart system` button in the Web UI requires a listener service on the host to restart Docker Compose services.

**How it works:**

The `./install.sh` script will:
1. Generate `colloc.service` systemd unit file (automatically)
2. Attempt to create `/etc/sudoers.d/colloc-system-service` for passwordless sudo (if possible)
3. Attempt to install the service system-wide (if passwordless sudo is available)

**Option 1: Let install.sh handle it (Recommended)**

Just run:

```bash
# run command
./install.sh
```

If you have passwordless sudo configured, the service will be installed automatically. If not, you'll see instructions to enable it.

**Option 2: Manual Installation**

If automatic installation doesn't work or you prefer manual setup:

```bash
# 1. Create sudoers entry (copy the one printed by install.sh, or use this template):
# run command with sudo
sudo tee /etc/sudoers.d/colloc-system-service > /dev/null << 'EOF'
# Colloc system service: allow passwordless systemd operations
YOUR_USERNAME ALL = (ALL) NOPASSWD: /bin/cp /path/to/colloc/colloc.service /etc/systemd/system/
YOUR_USERNAME ALL = (ALL) NOPASSWD: /bin/systemctl daemon-reload
YOUR_USERNAME ALL = (ALL) NOPASSWD: /bin/systemctl enable colloc.service
YOUR_USERNAME ALL = (ALL) NOPASSWD: /bin/systemctl start colloc.service
YOUR_USERNAME ALL = (ALL) NOPASSWD: /bin/systemctl restart colloc.service
YOUR_USERNAME ALL = (ALL) NOPASSWD: /bin/systemctl stop colloc.service
YOUR_USERNAME ALL = (ALL) NOPASSWD: /bin/systemctl status colloc.service
EOF

# 2. Install the service:
# run command with sudo
sudo cp colloc.service /etc/systemd/system/
# run command with sudo
sudo systemctl daemon-reload
# run command with sudo
sudo systemctl enable colloc.service
# run command with sudo
sudo systemctl start colloc.service
```

**Verify Installation:**

```bash
# run command with sudo
sudo systemctl status colloc.service
# run command with sudo
sudo journalctl -u colloc.service -f
```

**Note:** The "Restart system" button is optional. The Docker stack works fine without this service. If the service is not installed, clicking the button will show a connection error (expected).

### 4.2 Web UI Service Control Buttons (Stop/Start)

The Web UI has two additional control buttons:

- `Stop services`: stops running services except a protected keep-list.
- `Start services`: starts back services that were stopped by the previous action.

Default keep-list:

- `gateway`
- `webui-backend`
- `redis`

The host hook persists the stopped service set to:

- `logs/service-control-state.json`

Environment variables:

- `SYSTEM_SERVICE_CONTROL_KEEP_SERVICES`: comma-separated service names to keep running.
- `SYSTEM_RESET_HOOK_BIND_HOST`: host bind address for restart hook (`0.0.0.0` recommended).
- `SYSTEM_STOP_SERVICES_COMMAND`: command used only in `SYSTEM_RESET_MODE=command` for stop action.
- `SYSTEM_START_SERVICES_COMMAND`: command used only in `SYSTEM_RESET_MODE=command` for start action.

Examples for `SYSTEM_RESET_MODE=command`:

```bash
# run command
SYSTEM_STOP_SERVICES_COMMAND="docker compose stop stt tts-router tools silero kokoro piper-en piper-ru sip-service sip-ari asterisk"
# run command
SYSTEM_START_SERVICES_COMMAND="docker compose up -d stt tts-router tools silero kokoro piper-en piper-ru sip-service sip-ari asterisk"
```

If you use `SYSTEM_RESET_MODE=hook`, these command variables may stay empty.

After changing these variables, restart host service:

```bash
# run command with sudo
sudo systemctl restart colloc.service
```

5. Build and start full stack (core + TTS + SIP):

```bash
# run Docker command
docker compose --profile core --profile tts --profile sip up -d --build
```

6. Verify health:

```bash
# run Docker command
docker compose --profile core --profile tts --profile sip ps
# send HTTP request
curl -fsS http://127.0.0.1:6080/healthz
# send HTTP request
curl -fsS http://127.0.0.1:6080/api/health
```

7. If `AUTOLOAD=true`, verify preload:

```bash
# send HTTP request
curl -s -X POST http://127.0.0.1:6080/api/autoload-preload
# show log tail
tail -n 80 logs/system.log | sed -n '/autoload\./p'
```

If you see permission-related preload errors in `logs/system.log`, run `./install.sh` again and restart affected services.

### 1. Prerequisites

- Docker Engine 24+
- Docker Compose plugin
- Linux host (or Docker-compatible host)

Check tools:

```bash
# run Docker command
docker --version
# run Docker command
docker compose version
```

### Host Safety Rule (SPEC)

The project follows the SPEC rule: no host system modifications are required for runtime.

- All Python services run inside Docker containers.
- Diagnostics and smoke checks should be executed via `docker compose` and `docker compose exec`.
- If you run ad-hoc Python on the host, use an isolated venv only.

### 2. Configure Environment

Create your local environment file:

```bash
# copy file
cp .env.example .env
```

Then edit `.env` and set at least:

- `DOMAIN`
- `EXTERNAL_CERTIFICATE`
- LLM provider keys (`LLM_PROVIDER_PRIMARY*`, optional `LLM_PROVIDER_FALLBACK*`)
- STT/TTS provider settings
- SIP and Telegram keys if those profiles are used

### 2.1 Local HTTPS Without External Domain (Self-Signed)

If you run in a local LAN and cannot use a public domain, generate a self-signed certificate for your LAN IP (or local DNS name):

```bash
# create directories
mkdir -p certs
# make script executable
chmod +x scripts/generate-self-signed-cert.sh
# run command
./scripts/generate-self-signed-cert.sh 192.168.1.50 ./certs
```

Update `.env`:

```bash
# run command
DOMAIN=192.168.1.50
# run command
EXTERNAL_CERTIFICATE=./certs
```

Restart gateway:

```bash
# run Docker command
docker compose --profile core up -d gateway
```

Open:

```text
https://192.168.1.50:6443/
```

Browser note: a self-signed certificate shows a warning until trusted manually. This is expected.

Note:

- External service ports may be any valid ports.
- Local container/code service ports in this project use the 6xxx convention.

### 3. Prepare Local Directories

```bash
# create directories
mkdir -p data config logs
# create directories
mkdir -p data/redis data/asterisk logs/asterisk
```

### 4. Build Images

```bash
# run Docker command
docker compose build
```

### 5. Start the Stack

Core stack:

```bash
# run Docker command
docker compose --profile core up -d
```

Core + SIP + Telegram:

```bash
# run Docker command
docker compose --profile core --profile sip --profile telegram up -d
```

Development mode with hot reload:

```bash
# run Docker command
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  --profile core up -d
```

Publish STT/TTS ports externally (optional):

```bash
# run Docker command
docker compose \
  -f docker-compose.yml \
  -f docker-compose.publish.yml \
  --profile core up -d
```

## Operations and Maintenance Commands

## Live System Status Page

The stack now includes a live status page served by gateway:

- `http://127.0.0.1:6080/status.html`

Backend status endpoints:

- `GET /api/system-status`: one aggregated snapshot
- `WS /ws/system-status`: live updates (every 2 seconds)

The status payload contains:

- service health for core services
- host memory snapshot
- loaded model/provider settings from runtime config
- per-service request counters (for services instrumented with metrics)

### Stack lifecycle

```bash
# Start
# run Docker command
docker compose --profile core up -d

# Stop
# run Docker command
docker compose --profile core stop

# Restart (keeps containers and data)
# run Docker command
docker compose --profile core restart

# Full restart (remove containers/networks and bring back up)
# run Docker command
docker compose --profile core down && docker compose --profile core up -d

# Remove containers/networks
# run Docker command
docker compose --profile core down
```

### Logs

```bash
# All services
# run Docker command
docker compose --profile core logs -f --tail=200

# Single services
# run Docker command
docker compose logs -f gateway
# run Docker command
docker compose logs -f webui-backend
# run Docker command
docker compose logs -f stt
# run Docker command
docker compose logs -f tts-router
# run Docker command
docker compose logs -f tools
# run Docker command
docker compose logs -f redis

# Optional profiles
# run Docker command
docker compose --profile core --profile sip logs -f asterisk
# run Docker command
docker compose --profile telegram logs -f telegram-bot
```

### Status and health

```bash
# Running containers
# run Docker command
docker compose ps

# Render full merged config
# run Docker command
docker compose config
```

## Service Testing

Assuming default `.env` values (`NGINX_HTTP_PORT=6080`, `NGINX_HTTPS_PORT=6443`).

Base URLs:

```bash
# run command
BASE_URL_HTTP=http://127.0.0.1:6080
# run command
BASE_URL_HTTPS=https://127.0.0.1:6443
```

HTTP and HTTPS are both available for local access. Use HTTPS when you explicitly need TLS.

### Gateway

```bash
# send HTTP request
curl -fsS "$BASE_URL_HTTP/healthz"
```

### Web UI backend

```bash
# send HTTP request
curl -fsS "$BASE_URL_HTTP/api/health"
# send HTTP request
curl -fsS "$BASE_URL_HTTP/api/runtime" | jq .
```

### STT service

```bash
# send HTTP request
curl -fsS "$BASE_URL_HTTP/api/stt/health"
# send HTTP request
curl -fsS "$BASE_URL_HTTP/api/stt/providers" | jq .

# send HTTP request
curl -fsS -X POST "$BASE_URL_HTTP/api/stt/transcribe" \
  -H "Content-Type: application/json" \
  -d '{"audio_url":"https://example.com/audio.wav","language_hint":"en","partial":false}' | jq .
```

### TTS router

```bash
# send HTTP request
curl -fsS "$BASE_URL_HTTP/api/tts/health"
# send HTTP request
curl -fsS "$BASE_URL_HTTP/api/tts/voices" | jq .

# send HTTP request
curl -fsS -X POST "$BASE_URL_HTTP/api/tts/synthesize" \
  -H "Content-Type: application/json" \
  -d '{"text":"И Иван сказал: I need an apple"}' | jq .

# Russian-only test (useful for validating Silero route if configured as RU primary):
# send HTTP request
curl -fsS -X POST "$BASE_URL_HTTP/api/tts/synthesize" \
  -H "Content-Type: application/json" \
  -d '{"text":"Проверка связи прошла успешно: сигнал чистый, помех нет, я на связи и готов к работе."}' \
  | jq '{mode, provider, providers, fallback_used, segments: (.segments | length), errors}'
```

### Tools service

```bash
# send HTTP request
curl -fsS "$BASE_URL_HTTP/api/tools/health"
# send HTTP request
curl -fsS "$BASE_URL_HTTP/api/tools/tools" | jq .

# send HTTP request
curl -fsS -X POST "$BASE_URL_HTTP/api/tools/invoke" \
  -H "Content-Type: application/json" \
  -d '{"tool":"web_search","payload":{"query":"weather in Berlin"}}' | jq .
```

### Redis

```bash
# Without password
# run Docker command
docker compose exec redis redis-cli ping

# With password
# run Docker command
docker compose exec redis sh -lc 'redis-cli -a "$REDIS_PASSWORD" ping'
```

### Piper, Kokoro and Silero (container-level checks)

```bash
# run Docker command
docker compose exec piper-en sh -lc 'curl -fsS "http://127.0.0.1:${TTS_EN_PRIMARY_PORT:-6010}/health"'
# run Docker command
docker compose exec piper-ru sh -lc 'curl -fsS "http://127.0.0.1:${TTS_RU_PRIMARY_PORT:-6011}/health"'
# run Docker command
docker compose exec kokoro sh -lc 'curl -fsS "http://127.0.0.1:${KOKORO_PORT:-6030}/health"'
# run Docker command
docker compose exec silero sh -lc 'curl -fsS "http://127.0.0.1:${SILERO_PORT:-6040}/health"'

# run Docker command
docker compose exec piper-en sh -lc 'curl -fsS -X POST "http://127.0.0.1:${TTS_EN_PRIMARY_PORT:-6010}/synthesize" -H "Content-Type: application/json" -d "{\"text\":\"Hello from Piper EN\"}" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get(\"provider\"), len(d.get(\"audio_b64\",\"\")))"'
# run Docker command
docker compose exec kokoro sh -lc 'curl -fsS -X POST "http://127.0.0.1:${KOKORO_PORT:-6030}/synthesize" -H "Content-Type: application/json" -d "{\"text\":\"Hello from Kokoro\"}" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get(\"provider\"), len(d.get(\"audio_b64\",\"\")))"'
# run Docker command
docker compose exec silero sh -lc 'curl -fsS -X POST "http://127.0.0.1:${SILERO_PORT:-6040}/synthesize" -H "Content-Type: application/json" -d "{\"text\":\"Проверка синтеза Silero\",\"language\":\"ru\"}" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get(\"provider\"), len(d.get(\"audio_b64\",\"\")))"'
```

### Asterisk

```bash
# run Docker command
docker compose --profile core --profile sip ps asterisk
# run Docker command
docker compose --profile core --profile sip exec asterisk asterisk -rx 'core show uptime'
# run Docker command
docker compose --profile core --profile sip exec asterisk asterisk -rx 'pjsip show endpoints'

# Realtime Asterisk logs (tail + follow)
# run Docker command
docker compose --profile core --profile sip logs -f --tail=200 asterisk

#reload pjsip. can use "core reload", "dialplan reload" ...
# run Docker command
docker compose --profile core --profile sip exec asterisk asterisk -rx 'pjsip reload'

docker compose --profile core --profile sip exec asterisk asterisk -rx 'dialplan reload'

#console
docker compose --profile core --profile sip exec -it asterisk asterisk -r

```
console commands:
```
pjsip reload
dialplan reload
dialplan show
dialplan show zadarma-in
pjsip list endpoints
pjsip show endpoints
```



## Per-Service Test Requests

This section provides one compact smoke-check request per service.

Before running checks, start all required profiles:

```bash
# run Docker command
docker compose --profile core --profile tts --profile sip up -d
```

### 1. gateway

```bash
# send HTTP request
curl -fsS "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/healthz"
```

### 2. webui-backend

```bash
# send HTTP request
curl -fsS "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/health"
```

### 3. stt

```bash
# send HTTP request
curl -fsS "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/stt/health"
# send HTTP request
curl -fsS -X POST "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/stt/transcribe" \
  -H "Content-Type: application/json" \
  -d '{"audio_url":"https://example.com/a.wav","language_hint":"en","partial":true}'
```

### 4. tts-router

```bash
# send HTTP request
curl -fsS "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/tts/health"
# send HTTP request
curl -fsS -X POST "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/tts/synthesize" \
  -H "Content-Type: application/json" \
  -d '{"text":"Привет, I need two apples"}'
```

### 5. tools

```bash
# send HTTP request
curl -fsS "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/tools/health"
# send HTTP request
curl -fsS -X POST "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/tools/invoke" \
  -H "Content-Type: application/json" \
  -d '{"tool":"web_search","payload":{"query":"docker compose healthcheck"}}'
```

### 6. redis

```bash
# run Docker command
docker compose exec redis redis-cli ping
```

### 7. piper-en

```bash
# run Docker command
docker compose exec piper-en sh -lc 'curl -fsS "http://127.0.0.1:${TTS_EN_PRIMARY_PORT:-6010}/health"'
# run Docker command
docker compose exec piper-en sh -lc 'curl -fsS -X POST "http://127.0.0.1:${TTS_EN_PRIMARY_PORT:-6010}/synthesize" -H "Content-Type: application/json" -d "{\"text\":\"hello\"}" | python -c "import sys,json; d=json.load(sys.stdin); print(len(d.get(\"audio_b64\",\"\")))"'
```

### 8. piper-ru

```bash
# run Docker command
docker compose exec piper-ru sh -lc 'curl -fsS "http://127.0.0.1:${TTS_RU_PRIMARY_PORT:-6011}/health"'
# run Docker command
docker compose exec piper-ru sh -lc 'curl -fsS -X POST "http://127.0.0.1:${TTS_RU_PRIMARY_PORT:-6011}/synthesize" -H "Content-Type: application/json" -d "{\"text\":\"привет\"}" | python -c "import sys,json; d=json.load(sys.stdin); print(len(d.get(\"audio_b64\",\"\")))"'
```

### 9. kokoro

```bash
# run Docker command
docker compose exec kokoro sh -lc 'curl -fsS "http://127.0.0.1:${KOKORO_PORT:-6030}/health"'
# run Docker command
docker compose exec kokoro sh -lc 'curl -fsS -X POST "http://127.0.0.1:${KOKORO_PORT:-6030}/synthesize" -H "Content-Type: application/json" -d "{\"text\":\"hello\"}" | python -c "import sys,json; d=json.load(sys.stdin); print(len(d.get(\"audio_b64\",\"\")))"'
```

### 10. silero

```bash
# run Docker command
docker compose exec silero sh -lc 'curl -fsS "http://127.0.0.1:${SILERO_PORT:-6040}/health"'
# run Docker command
docker compose exec silero sh -lc 'curl -fsS -X POST "http://127.0.0.1:${SILERO_PORT:-6040}/synthesize" -H "Content-Type: application/json" -d "{\"text\":\"проверка синтеза\",\"language\":\"ru\"}" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get(\"provider\"), len(d.get(\"audio_b64\",\"\")))"'
```

### 11. sip-service

```bash
# run Docker command
docker compose --profile core --profile sip exec sip-service curl -fsS http://127.0.0.1:8004/health
# run Docker command
docker compose --profile core --profile sip logs sip-service
```

### 12. asterisk

```bash
# run Docker command
docker compose --profile core --profile sip exec asterisk asterisk -rx 'core show uptime'
# run Docker command
docker compose --profile core --profile sip exec asterisk asterisk -rx 'pjsip show endpoints'
```

### 13. telegram-bot

```bash
# run Docker command
docker compose --profile telegram ps telegram-bot
# run Docker command
docker compose --profile telegram logs --tail=50 telegram-bot
```

## Development Validation

Run config validation before deployment:

```bash
# run Docker command
docker compose -f docker-compose.yml config
# run Docker command
docker compose -f docker-compose.yml -f docker-compose.override.yml config
# run Docker command
docker compose -f docker-compose.yml -f docker-compose.publish.yml config
```

Build validation:

```bash
# run Docker command
docker build -t colloc-base:dev .
# run Docker command
docker build -t colloc-asterisk:dev ./asterisk
```

## LLM Query Examples

The primary LLM pipeline runs over WebSocket at `ws://HOST:6080/ws`.

### Verify LLM is configured

```bash
# send HTTP request
curl -s http://127.0.0.1:6080/api/runtime | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('llm_provider_primary_base_url'), d.get('llm_provider_primary_model'))"
```

If the values are empty, set `LLM_PROVIDER_PRIMARY_BASE_URL` and `LLM_PROVIDER_PRIMARY_MODEL` in `.env` and restart the stack.

---

### Option A — Python in container (recommended)

Uses Python environment from `webui-backend` container. No host package installation is required.

**Text query:**

```bash
# run Docker command
docker compose exec -T webui-backend python - <<'PYEOF'
import asyncio, json, websockets

# run command
async def main():
# run command
    async with websockets.connect("ws://127.0.0.1:6080/ws") as ws:
# run command
        print(await ws.recv())  # session.ready
# run command
        await ws.send(json.dumps({"type": "text.query", "text": "What is the capital of France?"}))
# run command
        while True:
# run command
            msg = json.loads(await ws.recv())
# run command
            if msg["type"] == "llm.token":
# run command
                print(msg["token"], end="", flush=True)
# run command
            elif msg["type"] in ("llm.done", "error"):
# run command
                print()
# run command
                print(json.dumps(msg, ensure_ascii=False))
# run command
                break

# run command
asyncio.run(main())
# run command
PYEOF
```

**Text query with system prompt:**

```bash
# run Docker command
docker compose exec -T webui-backend python - <<'PYEOF'
import asyncio, json, websockets

# run command
async def main():
# run command
    async with websockets.connect("ws://127.0.0.1:6080/ws") as ws:
# run command
        await ws.recv()  # session.ready
# run command
        await ws.send(json.dumps({"type": "session.config", "role": "translator",
# run command
                                  "system_prompt": "Translate all messages to Russian."}))
# run command
        await ws.recv()  # session.config.ack
# run command
        await ws.send(json.dumps({"type": "text.query", "text": "Good morning, how are you?"}))
# run command
        while True:
# run command
            msg = json.loads(await ws.recv())
# run command
            if msg["type"] == "llm.token":
# run command
                print(msg["token"], end="", flush=True)
# run command
            elif msg["type"] in ("llm.done", "error"):
# run command
                print(); break

# run command
asyncio.run(main())
# run command
PYEOF
```

---

### Option B — websocat with `-n` flag

`-n` / `--no-close` prevents websocat from sending a Close frame when stdin reaches EOF,
so it keeps reading server messages after sending. Press **Ctrl-C** once the response appears.

If `websocat` is already available, use the commands below. To keep host unchanged, prefer Option A.

**Text query:**

```bash
# print JSON payload
echo '{"type":"text.query","text":"What is the capital of France?"}' \
  | websocat -n "ws://127.0.0.1:6080/ws"
```

**Text query with session config:**

```bash
# print JSON lines for websocat
printf '%s\n%s\n' \
  '{"type":"session.config","role":"translator","system_prompt":"Translate to Russian."}' \
  '{"type":"text.query","text":"Good morning!"}' \
  | websocat -n "ws://127.0.0.1:6080/ws"
```

**Interactive multi-turn dialog:**

```bash
# open WebSocket client
websocat "ws://127.0.0.1:6080/ws"
# Type JSON messages manually, one per line:
# {"type":"text.query","text":"Hello, tell me a joke."}
# {"type":"text.query","text":"Tell me another one."}
```

---

### Direct LLM call bypassing gateway (from host)

Use these commands to check the primary Ollama instance directly, without going through colloc.

**Step 1 — TCP reachability:**

```bash
# send HTTP request
curl -sv --max-time 5 http://192.168.1.110:11434/api/tags
```

Expected: HTTP 200 with JSON list of models.
`Connection reset by peer` or timeout → Ollama is not running or the port is firewalled.

**Step 2 — List loaded models:**

```bash
# send HTTP request
curl -s http://192.168.1.110:11434/api/tags | python3 -c \
  "import sys,json; [print(m['name']) for m in json.load(sys.stdin).get('models',[])]"
```

Check that `juilpark/gemma-4-26B-A4B-it-heretic:q4_k_m` (or whatever is in `.env`) appears in the list.
If the model is missing, pull it first: `ollama pull <model>`.

**Step 3 — Test generation via Ollama native API:**

```bash
# send HTTP request
curl -s http://192.168.1.110:11434/api/generate \
  -H "Content-Type: application/json" \
  -d '{"model":"juilpark/gemma-4-26B-A4B-it-heretic:q4_k_m","prompt":"What is the capital of France?","stream":false}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('response',''))"
```

**Step 4 — Test via OpenAI-compatible endpoint (used by colloc):**

```bash
# send HTTP request
curl -s http://192.168.1.110:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"juilpark/gemma-4-26B-A4B-it-heretic:q4_k_m","messages":[{"role":"user","content":"What is the capital of France?"}],"stream":false}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'])"
```

> **Note:** Ollama exposes the OpenAI-compatible API at `/v1/` starting from version 0.1.24.
> If you get a 404 on `/v1/chat/completions`, upgrade Ollama.

**Using values from `.env` directly:**

```bash
# load environment variables
source .env
# Quick connectivity check
# send HTTP request
curl -sv --max-time 5 "${LLM_PROVIDER_PRIMARY_BASE_URL}/api/tags"

# Full generation test (OpenAI-compatible)
# send HTTP request
curl -s "${LLM_PROVIDER_PRIMARY_BASE_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"${LLM_PROVIDER_PRIMARY_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello.\"}],\"stream\":false}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'])"
```

### Message types reference

| Type | Direction | Description |
|---|---|---|
| `session.config` | client→server | Set `role` and/or `system_prompt` for the session |
| `text.query` | client→server | Send a text message to LLM |
| `voice.utterance` | client→server | Send base64 audio for STT→LLM pipeline |
| `session.ready` | server→client | Session established confirmation |
| `session.config.ack` | server→client | Config acknowledged |
| `stt.result` | server→client | Transcript from STT |
| `llm.token` | server→client | Streaming token from LLM |
| `llm.done` | server→client | Full LLM response, session history updated |
| `llm.warn` | server→client | Non-fatal warning (e.g. primary LLM failed, trying fallback) |
| `error` | server→client | Error message |

## SIP/Asterisk Voice Calls

### Configuration

To enable SIP voice calls via Asterisk, set up environment and profile:

```bash
# In .env:
# run command
SIP_ENABLED=true
# run command
SIP_SERVICE_PORT=8004
# run command
SIP_ROLE=ai_scripts/test.md              # Path to role/system prompt
# run command
SIP_GREETINGS=ai_scripts/greetings.md    # Greeting text or WAV file
# run command
SIP_MAX_SILENCE=30                        # Silence timeout (seconds)
# run command
SIP_MAX_DURATION=600                      # Call duration limit (seconds)
# run command
ASTERISK_PJSIP_PORT=6060                  # SIP listen port
# run command
ASTERISK_RTP_START=6700
# run command
ASTERISK_RTP_END=6800
```

### Starting with SIP

```bash
# Start core services + SIP
# run Docker command
docker compose --profile core --profile tts --profile sip up -d
```

### External NAT Port Forwarding (Important)

If calls from LAN work but calls from the Internet drop (for example around 30-40 seconds), check NAT port forwarding and advertised SIP settings.

Use this mapping model:

- Public SIP port on router (WAN), example: `5060/udp`
- Forwarded to host internal SIP port, example: `192.168.x.x:6060/udp`
- RTP range forwarded one-to-one, example: `6700-6800/udp`

What to set where:

1. Docker/.env (internal listener ports)

```bash
# Internal SIP bind port used by Asterisk container
ASTERISK_PJSIP_PORT=6060

# Internal RTP range used by Asterisk container
ASTERISK_RTP_START=6700
ASTERISK_RTP_END=6800
```

2. Asterisk transport advertisement (`asterisk/etc/pjsip.conf`)

For external transport, `bind` must stay internal, but `external_signaling_port` must match PUBLIC router port:

```ini
[transport-udp-external]
type = transport
protocol = udp
bind = 0.0.0.0:6060
external_signaling_address = your.public.domain.or.ip
external_signaling_port = 5060
external_media_address = your.public.domain.or.ip
```

Why this matters: if `external_signaling_port` is wrong (for example `6060` while WAN port is `5060`), remote clients may send in-dialog `ACK/BYE` to the wrong public port and calls can be dropped.

3. Router/NAT

- Forward `WAN:5060/udp` -> `LAN_HOST:6060/udp`
- Forward `WAN:6700-6800/udp` -> `LAN_HOST:6700-6800/udp`
- Disable SIP ALG on router if possible

Apply and verify after changes:

```bash
# Reload PJSIP config
docker compose --profile core --profile sip exec asterisk asterisk -rx 'pjsip reload'

# Reload dialplan (extentions.conf)
docker compose --profile core --profile sip exec asterisk asterisk -rx 'dialplan reload'

# Check runtime transport values
docker compose --profile core --profile sip exec asterisk asterisk -rx 'pjsip show transport transport-udp-external'
```

Expected runtime state for the example above:

- `bind : 0.0.0.0:6060`
- `external_signaling_port : 5060`

### Role and Greeting Files

Create role and greeting files in `ai_scripts/` directory:

**Role file (e.g., `ai_scripts/test.md`)**: System prompt / instructions for the AI during SIP calls.

```markdown
# SIP Test Role

You are a helpful AI assistant on a phone call. Your role is to:
- Be concise and natural in your responses
- Respond in Russian if the user speaks Russian
- Answer questions and provide assistance

Remember to keep responses short and clear for phone conversations.
```

**Greeting file (e.g., `ai_scripts/greetings.md`)**: Welcome message (plain text or WAV file).

```
Привет! Я виртуальный ассистент. Как я могу вам помочь?
```

If the greeting file has a `.wav` extension, it will be played as-is without TTS synthesis.

### Architecture

SIP call flow:

1. **Incoming Call**: Caller connects via SIP to Asterisk (port 6060).
2. **Stasis**: Asterisk routes the call through Stasis application `colloc-call-handler`.
3. **ARI Events**: Asterisk notifies `sip-service` via WebSocket using ARI (Asterisk REST Interface).
4. **Greeting**: SIP service retrieves greeting file and synthesizes (if text) via TTS.
5. **Main Loop**:
   - Receive audio from caller
   - Send to STT for transcription
   - Send transcript to LLM
   - Send LLM response to TTS
   - Play audio back to caller
6. **Timeout/Cleanup**: Call ends on silence timeout (`SIP_MAX_SILENCE`) or duration limit (`SIP_MAX_DURATION`).

### Smoke Test

Check SIP service and Asterisk:

```bash
# SIP service health
# run Docker command
docker compose --profile core --profile sip exec sip-service curl -fsS http://127.0.0.1:8004/health

# Asterisk uptime
# run Docker command
docker compose --profile core --profile sip exec asterisk asterisk -rx 'core show uptime'

# Show configured PJSIP endpoints
# run Docker command
docker compose --profile core --profile sip exec asterisk asterisk -rx 'pjsip show endpoints'
```

Why both profiles are required:

- `sip-service` has dependency on `stt` from `core` profile.
- If you run commands with only `--profile sip`, Docker Compose can fail with:
  `service "sip-service" depends on undefined service "stt": invalid compose project`.

### Restarting Asterisk

Use one of these options:

```bash
# Soft restart (only Asterisk container)
# run Docker command
docker compose --profile core --profile sip restart asterisk

# Recreate Asterisk and ARI listener (recommended after config changes)
# run Docker command
docker compose --profile core --profile sip up -d --force-recreate asterisk sip-ari

# Full SIP stack restart
# run Docker command
docker compose --profile core --profile sip restart asterisk sip-service sip-ari
```

After restart, verify:

```bash
# run Docker command
docker compose --profile core --profile sip ps asterisk sip-ari sip-service
# run Docker command
docker compose --profile core --profile sip exec -T asterisk asterisk -rx 'core show uptime'
# run Docker command
docker compose --profile core --profile sip exec -T asterisk asterisk -rx 'pjsip show endpoints'
```

### Known Limitations

- **Audio Frame Routing**: Current implementation is a placeholder. Real audio streaming requires AudioSocket protocol or bridge mixing integration (TODO).
- **Authentication**: Default PJSIP credentials (`username: colloc, password: change-me`) must be changed in production.
- **Conversation Context**: Full multi-turn context in Redis is not yet implemented; each call session uses simple turn-based history.
- **Barge-In**: Interruption handling for caller input is stubbed (TODO: implement Asterisk channel stop and return-to-listen).

### Testing with SIP Client

Use a SIP softphone (Linphone, Zoiper, MicroSIP) from another device in the same LAN.

1. Ensure services are running:

```bash
# run Docker command
docker compose --profile core --profile tts --profile sip up -d
# run Docker command
docker compose --profile core --profile sip ps asterisk sip-service sip-ari
```

2. Check Asterisk endpoint state:

```bash
# run Docker command
docker compose --profile core --profile sip exec -T asterisk asterisk -rx 'pjsip show endpoints'
```

You should see `colloc-endpoint` with transport `0.0.0.0:6060`.

3. Find host LAN IP (on machine where Docker stack runs):

```bash
# show host IP addresses
hostname -I
```

Use your LAN address (for example `192.168.1.100`).

4. Configure SIP account in softphone:

- SIP server / domain: `<LAN_HOST_IP>`
- SIP port: `6060` (or your `ASTERISK_PJSIP_PORT`)
- Transport: `UDP`
- Username/Auth ID: `colloc`
- Password: `change-me` (from `asterisk/etc/pjsip.conf`)

5. Make a test call from the client:

- Dial any numeric extension, for example `100`.
- Current dialplan (`colloc-inbound`) routes `_X.` to `Stasis(colloc-call-handler)`.

6. Watch runtime logs during call:

```bash
# run Docker command
docker compose --profile core --profile sip logs -f asterisk sip-ari sip-service
```

Expected call path:

- `asterisk`: incoming SIP INVITE and channel enters Stasis app `colloc-call-handler`
- `sip-ari`: creates bridge + External Media channel
- `sip-service`: starts call session, plays greeting, processes STT -> LLM -> TTS

Example Linphone CLI sequence:

```bash
# start Linphone CLI
linphonec
# run command
register sip:colloc@192.168.1.100:6060 colloc change-me
# run command
call 100
```

If registration/call fails:

- verify client and server are in the same subnet,
- verify host firewall allows UDP `6060` and RTP range `6700-6800`,
- re-check credentials in `asterisk/etc/pjsip.conf` and restart Asterisk:

```bash
# run Docker command
docker compose --profile core --profile sip up -d --force-recreate asterisk sip-ari
```

## Current Scope and Limitations

This is the first operational scaffold, not a final production implementation.

- STT and TTS pipeline are functional for real-time operation; tools service is still a scaffold for pluggable tool execution
- Piper/Kokoro run as real server-side synthesis services; model assets are downloaded on first use
- Silero runs as a real server-side synthesis service and requires PyTorch in the runtime image
- SIP flow is baseline-configured and requires telephony hardening for production
- Security hardening beyond container defaults (secrets manager, firewall policies, IDS, SIEM) is out of scope for this first version


Recreate containeds 
```sh
#webui-backend sip-service .....
docker compose --profile core --profile sip up -d --build --force-recreate ....LIST... 

#For SIP stuff
docker compose --profile core --profile sip up -d --build --force-recreate sip-service sip-ari

```