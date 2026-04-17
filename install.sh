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

Colloc service setup:
- colloc_service.sh: main service entry point
- colloc.service: systemd service unit (auto-installation attempted)

Next steps:
1) Service installation:
   - If you have passwordless sudo, the service will be installed automatically above
   - Otherwise, follow the manual installation commands displayed above

2) Start Docker Compose stack:
   docker compose --profile core --profile tts --profile sip up -d --build

3) If AUTOLOAD is enabled, trigger preload check:
   curl -s -X POST http://127.0.0.1:6080/api/autoload-preload

4) If you still see permission errors in System Log, run this script again.
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

# Make service scripts executable
chmod +x "$ROOT_DIR/colloc_service.sh" 2>/dev/null || true
chmod +x "$ROOT_DIR/scripts/restart-service-hook.py" 2>/dev/null || true

# Setup sudoers entry for passwordless systemd/docker commands (optional but helpful)
SUDOERS_FILE="/etc/sudoers.d/colloc-system-service"
CURRENT_USER=$(whoami)

setup_sudoers() {
  if [[ ! -f "$SUDOERS_FILE" ]]; then
    # Create temporary sudoers snippet for this user
    TEMP_SUDOERS=$(mktemp)
    cat > "$TEMP_SUDOERS" << SUDOERS_EOF
# Colloc system service: allow passwordless systemd/docker operations
$CURRENT_USER ALL = (ALL) NOPASSWD: /bin/cp $ROOT_DIR/colloc.service /etc/systemd/system/
$CURRENT_USER ALL = (ALL) NOPASSWD: /bin/systemctl daemon-reload
$CURRENT_USER ALL = (ALL) NOPASSWD: /bin/systemctl enable colloc.service
$CURRENT_USER ALL = (ALL) NOPASSWD: /bin/systemctl start colloc.service
$CURRENT_USER ALL = (ALL) NOPASSWD: /bin/systemctl restart colloc.service
$CURRENT_USER ALL = (ALL) NOPASSWD: /bin/systemctl stop colloc.service
$CURRENT_USER ALL = (ALL) NOPASSWD: /bin/systemctl status colloc.service
SUDOERS_EOF
    
    # Attempt sudo installation
    if sudo -n true 2>/dev/null; then
      sudo mv "$TEMP_SUDOERS" "$SUDOERS_FILE"
      sudo chmod 440 "$SUDOERS_FILE"
      echo "✓ Created passwordless sudoers entry for systemd operations"
      return 0
    else
      echo "⚠ To enable passwordless sudo for systemd, manually create $SUDOERS_FILE:"
      echo ""
      cat "$TEMP_SUDOERS"
      echo ""
      rm "$TEMP_SUDOERS"
      return 1
    fi
  fi
  return 0
}

setup_sudoers || true

# Create systemd service unit in project root (not system-wide)
# User can install it with: sudo cp colloc.service /etc/systemd/system/
SERVICE_FILE="$ROOT_DIR/colloc.service"
cat > "$SERVICE_FILE" << 'SYSTEMD_EOF'
[Unit]
Description=Colloc System Services
Documentation=file://COLLOC_ROOT/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=USER_NAME
Group=docker
WorkingDirectory=COLLOC_ROOT
ExecStart=COLLOC_ROOT/colloc_service.sh
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SYSTEMD_EOF

# Replace placeholders in service file
sed -i "s|COLLOC_ROOT|$ROOT_DIR|g" "$SERVICE_FILE"
sed -i "s|USER_NAME|$(whoami)|g" "$SERVICE_FILE"

echo "Service file created: $SERVICE_FILE"

# Attempt to install systemd service
SYSTEM_SERVICE="/etc/systemd/system/colloc.service"
echo ""
echo "==========================================="
echo "Colloc systemd service installation"
echo "==========================================="

if [[ ! -f "$SYSTEM_SERVICE" ]]; then
  echo "systemd service not found at $SYSTEM_SERVICE"
  echo "Attempting to install..."
  
  if sudo -n true 2>/dev/null; then
    # sudo available without password prompt
    echo "Installing systemd service..."
    sudo cp "$SERVICE_FILE" "$SYSTEM_SERVICE"
    sudo systemctl daemon-reload
    sudo systemctl enable colloc.service
    sudo systemctl start colloc.service
    echo "✓ Service installed and started"
    sudo systemctl status colloc.service --no-pager
  else
    # No passwordless sudo, ask user
    echo ""
    echo "Passwordless sudo not available. To install the service manually, run:"
    echo ""
    echo "  sudo cp $SERVICE_FILE /etc/systemd/system/"
    echo "  sudo systemctl daemon-reload"
    echo "  sudo systemctl enable colloc.service"
    echo "  sudo systemctl start colloc.service"
    echo ""
  fi
else
  # Service exists, check if it needs update
  SYSTEM_CHECKSUM=$(sha256sum "$SYSTEM_SERVICE" 2>/dev/null | awk '{print $1}')
  LOCAL_CHECKSUM=$(sha256sum "$SERVICE_FILE" 2>/dev/null | awk '{print $1}')
  
  if [[ "$SYSTEM_CHECKSUM" != "$LOCAL_CHECKSUM" ]]; then
    echo "systemd service exists but differs from local template"
    echo "Updating..."
    
    if sudo -n true 2>/dev/null; then
      sudo cp "$SERVICE_FILE" "$SYSTEM_SERVICE"
      sudo systemctl daemon-reload
      sudo systemctl restart colloc.service
      echo "✓ Service updated and restarted"
    else
      echo ""
      echo "To update the service, run:"
      echo ""
      echo "  sudo cp $SERVICE_FILE /etc/systemd/system/"
      echo "  sudo systemctl daemon-reload"
      echo "  sudo systemctl restart colloc.service"
      echo ""
    fi
  else
    echo "✓ systemd service is up to date"
    if sudo -n true 2>/dev/null; then
      sudo systemctl status colloc.service --no-pager | head -5
    fi
  fi
fi

echo ""
echo "Installation complete!"
