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
- `stt` (FastAPI): STT service API scaffold
- `tts-router` (FastAPI): language-aware routing for Piper voices and fallback provider
- `tools` (FastAPI): tool registry and invocation API scaffold
- `redis`: session/context storage

Optional services:

- `piper-en`, `piper-ru`: per-language Piper endpoints (scaffold mode in current version)
- `kokoro`: secondary TTS endpoint (scaffold mode in current version)
- `asterisk`: SIP endpoint container with baseline configuration
- `telegram-bot`: optional bot process placeholder

Compose profiles:

- `core`: gateway, backend, redis, tools, stt, tts-router, piper, kokoro
- `tts`: explicit TTS-related profile
- `sip`: Asterisk
- `telegram`: Telegram bot

## Repository Layout

- `docker-compose.yml`: main stack
- `docker-compose.override.yml`: local development overrides with hot reload for FastAPI services
- `docker-compose.publish.yml`: optional publication of STT/TTS service ports
- `gateway/`: Nginx templates and startup script
- `webui/frontend/`: static frontend
- `webui/backend/`: FastAPI backend
- `services/stt/`: STT FastAPI service
- `services/tts_router/`: TTS routing FastAPI service
- `services/tools/`: tools FastAPI service
- `asterisk/`: Asterisk image and config

## Installation and Startup

### 1. Prerequisites

- Docker Engine 24+
- Docker Compose plugin
- Linux host (or Docker-compatible host)

Check tools:

```bash
docker --version
docker compose version
```

### 2. Configure Environment

Create your local environment file:

```bash
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
mkdir -p certs
chmod +x scripts/generate-self-signed-cert.sh
./scripts/generate-self-signed-cert.sh 192.168.1.50 ./certs
```

Update `.env`:

```bash
DOMAIN=192.168.1.50
EXTERNAL_CERTIFICATE=./certs
```

Restart gateway:

```bash
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
mkdir -p data config logs
mkdir -p data/redis data/asterisk logs/asterisk
```

### 4. Build Images

```bash
docker compose build
```

### 5. Start the Stack

Core stack:

```bash
docker compose --profile core up -d
```

Core + SIP + Telegram:

```bash
docker compose --profile core --profile sip --profile telegram up -d
```

Development mode with hot reload:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  --profile core up -d
```

Publish STT/TTS ports externally (optional):

```bash
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
docker compose --profile core up -d

# Stop
docker compose --profile core stop

# Restart
docker compose --profile core restart

# Remove containers/networks
docker compose --profile core down
```

### Logs

```bash
# All services
docker compose --profile core logs -f --tail=200

# Single services
docker compose logs -f gateway
docker compose logs -f webui-backend
docker compose logs -f stt
docker compose logs -f tts-router
docker compose logs -f tools
docker compose logs -f redis

# Optional profiles
docker compose --profile sip logs -f asterisk
docker compose --profile telegram logs -f telegram-bot
```

### Status and health

```bash
# Running containers
docker compose ps

# Render full merged config
docker compose config
```

## Service Testing

Assuming default `.env` values (`NGINX_HTTP_PORT=6080`, `NGINX_HTTPS_PORT=6443`).

Base URLs:

```bash
BASE_URL_HTTP=http://127.0.0.1:6080
BASE_URL_HTTPS=https://127.0.0.1:6443
```

HTTP and HTTPS are both available for local access. Use HTTPS when you explicitly need TLS.

### Gateway

```bash
curl -fsS "$BASE_URL/healthz"
```

### Web UI backend

```bash
curl -fsS "$BASE_URL_HTTP/api/health"
curl -fsS "$BASE_URL_HTTP/api/runtime" | jq .
```

### STT service

```bash
curl -fsS "$BASE_URL_HTTP/api/stt/health"
curl -fsS "$BASE_URL_HTTP/api/stt/providers" | jq .

curl -fsS -X POST "$BASE_URL_HTTP/api/stt/transcribe" \
  -H "Content-Type: application/json" \
  -d '{"audio_url":"https://example.com/audio.wav","language_hint":"en","partial":false}' | jq .
```

### TTS router

```bash
curl -fsS "$BASE_URL_HTTP/api/tts/health"
curl -fsS "$BASE_URL_HTTP/api/tts/voices" | jq .

curl -fsS -X POST "$BASE_URL_HTTP/api/tts/synthesize" \
  -H "Content-Type: application/json" \
  -d '{"text":"И Иван сказал: I need an apple"}' | jq .
```

### Tools service

```bash
curl -fsS "$BASE_URL_HTTP/api/tools/health"
curl -fsS "$BASE_URL_HTTP/api/tools/tools" | jq .

curl -fsS -X POST "$BASE_URL_HTTP/api/tools/invoke" \
  -H "Content-Type: application/json" \
  -d '{"tool":"web_search","payload":{"query":"weather in Berlin"}}' | jq .
```

### Redis

```bash
# Without password
docker compose exec redis redis-cli ping

# With password
docker compose exec redis sh -lc 'redis-cli -a "$REDIS_PASSWORD" ping'
```

### Piper and Kokoro (container-level checks)

```bash
docker compose exec piper-en sh -lc 'curl -fsS "http://127.0.0.1:$PIPER_PORT_EN/" >/dev/null && echo ok'
docker compose exec piper-ru sh -lc 'curl -fsS "http://127.0.0.1:$PIPER_PORT_RU/" >/dev/null && echo ok'
docker compose exec kokoro sh -lc 'curl -fsS "http://127.0.0.1:$KOKORO_PORT/" >/dev/null && echo ok'
```

### Asterisk

```bash
docker compose --profile sip ps asterisk
docker compose --profile sip exec asterisk asterisk -rx 'core show uptime'
docker compose --profile sip exec asterisk asterisk -rx 'pjsip show endpoints'
```

## Per-Service Test Requests

This section provides one compact smoke-check request per service.

Before running checks, start all required profiles:

```bash
docker compose --profile core --profile tts --profile sip up -d
```

### 1. gateway

```bash
curl -fsS "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/healthz"
```

### 2. webui-backend

```bash
curl -fsS "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/health"
```

### 3. stt

```bash
curl -fsS "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/stt/health"
curl -fsS -X POST "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/stt/transcribe" \
  -H "Content-Type: application/json" \
  -d '{"audio_url":"https://example.com/a.wav","language_hint":"en","partial":true}'
```

### 4. tts-router

```bash
curl -fsS "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/tts/health"
curl -fsS -X POST "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/tts/synthesize" \
  -H "Content-Type: application/json" \
  -d '{"text":"Привет, I need two apples"}'
```

### 5. tools

```bash
curl -fsS "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/tools/health"
curl -fsS -X POST "http://127.0.0.1:${NGINX_HTTP_PORT:-6080}/api/tools/invoke" \
  -H "Content-Type: application/json" \
  -d '{"tool":"web_search","payload":{"query":"docker compose healthcheck"}}'
```

### 6. redis

```bash
docker compose exec redis redis-cli ping
```

### 7. piper-en

```bash
docker compose exec piper-en sh -lc 'curl -fsS "http://127.0.0.1:$PIPER_PORT_EN/" >/dev/null && echo ok'
```

### 8. piper-ru

```bash
docker compose exec piper-ru sh -lc 'curl -fsS "http://127.0.0.1:$PIPER_PORT_RU/" >/dev/null && echo ok'
```

### 9. kokoro

```bash
docker compose exec kokoro sh -lc 'curl -fsS "http://127.0.0.1:$KOKORO_PORT/" >/dev/null && echo ok'
```

### 10. asterisk

```bash
docker compose --profile sip exec asterisk asterisk -rx 'core show uptime'
docker compose --profile sip exec asterisk asterisk -rx 'pjsip show endpoints'
```

### 11. telegram-bot

```bash
docker compose --profile telegram ps telegram-bot
docker compose --profile telegram logs --tail=50 telegram-bot
```

## Development Validation

Run config validation before deployment:

```bash
docker compose -f docker-compose.yml config
docker compose -f docker-compose.yml -f docker-compose.override.yml config
docker compose -f docker-compose.yml -f docker-compose.publish.yml config
```

Build validation:

```bash
docker build -t colloc-base:dev .
docker build -t colloc-asterisk:dev ./asterisk
```

## Current Scope and Limitations

This is the first operational scaffold, not a final production implementation.

- STT/TTS/tools currently provide functional API contracts with placeholder behavior
- Piper/Kokoro containers are scaffold endpoints and should be replaced with real runtime images
- SIP flow is baseline-configured and requires telephony hardening for production
- Security hardening beyond container defaults (secrets manager, firewall policies, IDS, SIEM) is out of scope for this first version
