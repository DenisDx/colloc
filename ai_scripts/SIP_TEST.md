# SIP/Asterisk Testing Guide

## Test Setup

### 1. Enable SIP Profile

Update `.env`:
```
SIP_ENABLED=true
SIP_SERVICE_PORT=8004
SIP_ROLE=ai_scripts/test.md
SIP_GREETINGS=ai_scripts/greetings.md
SIP_MAX_SILENCE=30
SIP_MAX_DURATION=600
ASTERISK_PJSIP_PORT=6060
```

Start services:
```bash
docker compose --profile core --profile tts --profile sip up -d
```

### 2. Verify Container Health

```bash
# Check sip-service
docker compose --profile sip exec sip-service curl -fsS http://127.0.0.1:8004/health
# Expected: {"status": "ok", "service": "sip"}

# Check Asterisk
docker compose --profile sip exec asterisk asterisk -rx 'core show uptime'
docker compose --profile sip exec asterisk asterisk -rx 'pjsip show endpoints'
# Expected: colloc-endpoint ACTIVE
```

### 3. Get Local Network IP

```bash
# Find your local IP (used in SIP client)
hostname -I
# Example output: 192.168.1.100 172.17.0.1
# Use the one on your local network, not 172.17.x.x
```

## Manual SIP Testing via Softphone

### Option A: Using Linphone CLI

```bash
# Install on host (if not in container)
apt-get install -y linphone

# Connect to Asterisk
linphone -c /dev/null

# In Linphone:
> register sip:colloc@192.168.1.100:6060 colloc change-me
> call sip:test@192.168.1.100:6060

# Listen to greeting and respond
```

### Option B: Using Twilio STUN/ICE (No special software)

If your local network doesn't support direct SIP, use a SIP service:
1. Configure SIP forwarding to your local Asterisk (complex)
2. Or use Docker port mapping to host IP (see docker-compose.yml)

### Option C: Using SIPp (Automated Testing)

```bash
# SIPp is a SIP call generator
# Create a simple UAC (User Agent Client) script to test inbound calls

# Install
apt-get install -y sipp

# Basic call test
sipp -sf scenario.xml -u alice@192.168.1.100 -s sip:test@192.168.1.100:6060

# See SIPp documentation for scenario files
```

## Containerized Testing (Recommended)

Use a softphone inside Docker:

```bash
# Client container
docker run -it --rm --network colloc-network -v /etc/asound.conf:/etc/asound.conf \
  -e DISPLAY \
  ubuntu:22.04 bash

# Inside:
apt-get update && apt-get install -y linphone
linphone
```

## Expected Call Flow

1. **Incoming Call**: Asterisk accepts SIP INVITE
2. **Greeting**: SIP service synthesizes greeting via TTS
3. **Silence**: Asterisk captures audio from caller
4. **STT**: Audio sent to STT, returns transcript
5. **LLM**: Transcript sent to LLM, returns response
6. **TTS**: Response synthesized via TTS
7. **Playback**: Audio returned to caller via Asterisk
8. **Loop**: Repeat until timeout or hangup

## Logging & Diagnostics

### Real-time Logs

```bash
# SIP service
docker compose --profile sip logs -f sip-service

# Asterisk
docker compose --profile sip logs -f asterisk

# STT
docker compose --profile core logs -f stt

# TTS router
docker compose --profile core logs -f tts-router
```

### Asterisk CLI Commands

```bash
# In container
docker compose --profile sip exec asterisk bash
asterisk -r

# At CLI prompt:
> pjsip show endpoints
> core show channels
> sip show peers
> stasis show applications
> core set verbose 5    # Increase verbosity
```

### Check Call State

```bash
# Monitor active calls
docker compose --profile sip exec asterisk asterisk -rx 'core show channels'
```

## Troubleshooting

### Issue: Call not connecting

**Solution**: 
1. Verify Asterisk port is exposed: `netstat -tuln | grep 6060`
2. Check firewall: `sudo ufw allow 6060/udp`
3. Verify SIP client credentials match `asterisk/etc/pjsip.conf`

### Issue: Greeting not heard

**Solution**:
1. Check `SIP_GREETINGS` file exists and is readable
2. Check TTS/tts-router is healthy: `docker compose --profile core logs tts-router`
3. Verify language setting matches content (Russian/English)

### Issue: No response after speaking

**Solution**:
1. Check STT service: `docker compose --profile core logs stt`
2. Check SIP service logs: `docker compose --profile sip logs sip-service`
3. Verify LLM is running and accessible
4. Check TTS router is responding

### Issue: Call drops after 30 seconds

**Check**: `SIP_MAX_SILENCE` timeout. Caller must speak within this window.

## Performance Notes

- **STT Latency**: Expect 2-5 seconds for transcription (depends on audio length)
- **LLM Latency**:  Depends on model and response length (2-10+ seconds)
- **TTS Latency**: Expect 1-3 seconds for synthesis
- **Total**: ~6-20 seconds per turn (optimization needed for real-time)

## Security Testing

### Strong Passwords

Before production, update in `asterisk/etc/pjsip.conf`:
```ini
[colloc-auth]
type = auth
auth_type = userpass
username = colloc
password = STRONG_PASSWORD_HERE
```

Rebuild asterisk:
```bash
docker compose --profile sip build --no-cache asterisk
```

### Port Security

Expose only necessary ports via gateway:
```bash
# Only HTTPS and SIP signaling public
NGINX_HTTPS_PORT=6443
ASTERISK_PJSIP_PORT=6060
# RTP ports: internal only
```

## Next Steps

- Full end-to-end test with real SIP infrastructure
- Load testing with multiple concurrent calls
- Audio quality assessment (codec, packet loss)
- Barge-in (caller interrupt) validation
- Language detection and multi-language support
- Context persistence across calls (Redis integration)
