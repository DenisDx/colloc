#!/usr/bin/env python3
"""
System restart HTTP hook handler.
Listens on port 9999 and restarts Docker Compose services on POST /restart-services.
Run on host: python3 scripts/restart-service-hook.py
"""

import json
import logging
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler("./logs/restart-hook.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# Configuration
COLLOC_ROOT = Path(__file__).parent.parent
COMPOSE_FILE = COLLOC_ROOT / "docker-compose.yml"
WORKING_DIR = str(COLLOC_ROOT)
PORT = 9999
PROFILES = ["core"]
BIND_HOST = os.getenv("SYSTEM_RESET_HOOK_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"


class RestartHandler(BaseHTTPRequestHandler):
    """HTTP handler for system restart requests."""

    def do_POST(self):
        """Handle POST request to restart services."""
        if self.path != "/restart-services":
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Not found"}).encode())
            logger.warning(f"Invalid path requested: {self.path}")
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body) if body else {}
            source = data.get("source", "unknown")
            
            logger.info(f"Restart request from {source}")
            
            # Build docker compose command
            profile_args = " ".join([f"--profile {p}" for p in PROFILES])
            cmd = f"docker compose -f {COMPOSE_FILE} {profile_args} restart"
            
            logger.info(f"Executing: {cmd}")
            
            # Execute restart command
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=WORKING_DIR,
                capture_output=True,
                text=True,
                timeout=120,
            )
            
            if result.returncode == 0:
                logger.info("Services restarted successfully")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({
                        "status": "success",
                        "message": "Services restarted successfully",
                        "timestamp": time.time(),
                    }).encode()
                )
            else:
                logger.error(f"Restart failed: {result.stderr}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({
                        "status": "error",
                        "message": f"Restart failed: {result.stderr}",
                    }).encode()
                )
        except subprocess.TimeoutExpired:
            logger.error("Restart command timeout")
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"error": "Command timeout"}).encode()
            )
        except Exception as e:
            logger.error(f"Error handling restart request: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"error": str(e)}).encode()
            )

    def log_message(self, format, *args):
        """Suppress default HTTP server logging."""
        pass


def main():
    """Start HTTP hook server."""
    # Ensure logs directory exists
    log_dir = COLLOC_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    
    server_address = (BIND_HOST, PORT)
    httpd = HTTPServer(server_address, RestartHandler)
    
    logger.info(f"System restart hook listening on http://{BIND_HOST}:{PORT}")
    logger.info(f"Working directory: {WORKING_DIR}")
    logger.info(f"Docker Compose file: {COMPOSE_FILE}")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Hook server stopped")
        httpd.shutdown()


if __name__ == "__main__":
    main()
