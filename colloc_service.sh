#!/usr/bin/env bash
# Colloc system service main entry point.
# Manages auxiliary host-side services needed by the Colloc stack:
# - System restart hook (HTTP endpoint for container-triggered restarts)
# Output: service status logs to journal. Input: none.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/opt/venv/bin:$PATH"
export PYTHONUNBUFFERED=1

# Create logs directory if it doesn't exist
mkdir -p "$PROJECT_ROOT/logs"

# Log function
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$PROJECT_ROOT/logs/service.log"
}

log "Colloc service starting (PID $$)"
cd "$PROJECT_ROOT"

# Start: System restart hook (background)
log "Starting system restart hook listener on port 9999..."
python3 "$PROJECT_ROOT/scripts/restart-service-hook.py" >> "$PROJECT_ROOT/logs/service.log" 2>&1 &
HOOK_PID=$!
log "Hook process started (PID $HOOK_PID)"

# Trap signal handlers to stop all background processes
cleanup() {
    log "Colloc service stopping..."
    if [[ -n "${HOOK_PID:-}" ]] && kill -0 "$HOOK_PID" 2>/dev/null; then
        log "Stopping hook process (PID $HOOK_PID)"
        kill "$HOOK_PID" 2>/dev/null || true
        wait "$HOOK_PID" 2>/dev/null || true
    fi
    log "Colloc service stopped"
    exit 0
}

trap cleanup SIGTERM SIGINT

# Wait for all background processes
wait
