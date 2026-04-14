#!/usr/bin/env bash
set -euo pipefail

# Prepare project-local runtime directories and writable paths for container user (uid 1000).
# Output: directories/files created and permission summary. Input: none.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".env" ]]; then
  echo "ERROR: .env is missing in $ROOT_DIR"
  echo "Create it first: cp .env.example .env"
  exit 1
fi

# Project-local base directories.
BASE_DIRS=(
  "data"
  "config"
  "logs"
  "logs/asterisk"
  "data/redis"
  "data/asterisk"
)

# Runtime/cache/model directories used by app services.
RUNTIME_DIRS=(
  "data/tmp"
  "data/huggingface"
  "data/xdg-cache"
  "data/faster-whisper-models"
  "data/piper-models"
  "data/piper-models/en"
  "data/piper-models/ru"
  "data/kokoro-models"
  "data/silero-models"
)

# Ensure directory tree exists.
mkdir -p "${BASE_DIRS[@]}" "${RUNTIME_DIRS[@]}"

# Ensure system log exists in project logs.
touch "logs/system.log"

# Best-effort permission helper to avoid noisy output in restricted/rootless environments.
safe_chmod() {
  local mode="$1"
  shift
  chmod "$mode" "$@" 2>/dev/null || return 1
}

perm_warning=0

# Keep safe defaults for generic project directories.
safe_chmod 775 data config logs logs/asterisk data/redis data/asterisk || perm_warning=1
safe_chmod 664 logs/system.log || perm_warning=1

# Runtime dirs must be writable for non-root container user across varying host UID/GID setups.
safe_chmod -R 777 "${RUNTIME_DIRS[@]}" || perm_warning=1

cat <<'EOF'
Install preparation complete.
Created/updated:
- data/, config/, logs/
- data/tmp, data/huggingface, data/xdg-cache, data/faster-whisper-models
- data/piper-models/{en,ru}, data/kokoro-models, data/silero-models
- logs/system.log

Next steps:
1) Start stack:
   docker compose --profile core --profile tts --profile sip up -d --build
2) If AUTOLOAD is enabled, trigger preload check:
   curl -s -X POST http://127.0.0.1:6080/api/autoload-preload
3) If you still see permission errors in System Log, run this script again.
EOF

if [[ "$perm_warning" -eq 1 ]]; then
  cat <<'EOF'

Notice:
- Some chmod operations were not permitted by host filesystem policy.
- This is expected in some rootless/restricted Docker setups.
- If preload still reports permission errors, run this once after stack start:
  docker exec -u 0 colloc-webui-backend sh -lc 'mkdir -p /srv/data/piper-models/en /srv/data/piper-models/ru /srv/data/silero-models /srv/data/kokoro-models /srv/data/huggingface /srv/data/xdg-cache /srv/data/faster-whisper-models /srv/data/tmp && chmod -R 777 /srv/data/piper-models /srv/data/silero-models /srv/data/kokoro-models /srv/data/huggingface /srv/data/xdg-cache /srv/data/faster-whisper-models /srv/data/tmp'
EOF
fi
