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
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

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
SERVICE_CONTROL_STATE_FILE = COLLOC_ROOT / "logs" / "service-control-state.json"
KEEP_SERVICES = [
    item.strip()
    for item in os.getenv("SYSTEM_SERVICE_CONTROL_KEEP_SERVICES", "gateway,webui-backend,redis").split(",")
    if item.strip()
]


def _run_compose(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """Run docker compose command. Output: completed process. Input: argument list and timeout."""
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    logger.info("Executing: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=WORKING_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_compose_background(args: list[str]) -> subprocess.Popen[str]:
    """Run docker compose command in background. Output: process handle. Input: argument list."""
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    logger.info("Executing in background: %s", " ".join(cmd))
    return subprocess.Popen(
        cmd,
        cwd=WORKING_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _json_response(handler: BaseHTTPRequestHandler, status_code: int, payload: dict[str, Any]) -> None:
    """Write JSON HTTP response. Output: none. Input: handler, status code, payload."""
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(payload).encode())


def _list_running_services() -> list[str]:
    """List running compose services. Output: service list. Input: none."""
    result = _run_compose(["ps", "--services", "--filter", "status=running"], timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Failed to list running services")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _read_service_control_state() -> dict[str, Any]:
    """Read saved service-control state. Output: state dict. Input: none."""
    if not SERVICE_CONTROL_STATE_FILE.exists():
        return {}
    try:
        return json.loads(SERVICE_CONTROL_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_service_control_state(state: dict[str, Any]) -> None:
    """Persist service-control state. Output: none. Input: state dict."""
    SERVICE_CONTROL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SERVICE_CONTROL_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


class RestartHandler(BaseHTTPRequestHandler):
    """HTTP handler for system restart requests."""

    def do_POST(self):
        """Handle POST request to restart services."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        data = json.loads(body) if body else {}
        source = data.get("source", "unknown")

        try:
            if self.path == "/restart-services":
                self._handle_restart(source)
                return
            if self.path == "/stop-services":
                self._handle_stop_services(source)
                return
            if self.path == "/start-services":
                self._handle_start_services(source)
                return

            _json_response(self, 404, {"error": "Not found"})
            logger.warning("Invalid path requested: %s", self.path)
        except subprocess.TimeoutExpired:
            logger.error("Command timeout while handling %s", self.path)
            _json_response(self, 504, {"error": "Command timeout"})
        except Exception as exc:  # noqa: BLE001
            logger.error("Error handling %s: %s", self.path, exc)
            _json_response(self, 500, {"error": str(exc)})

    def _handle_restart(self, source: str) -> None:
        """Handle POST /restart-services. Output: none. Input: source string."""
        logger.info("Restart request from %s", source)
        profile_args: list[str] = []
        for profile in PROFILES:
            profile_args.extend(["--profile", profile])

        process = _run_compose_background([*profile_args, "restart"])

        def _wait_restart(proc: subprocess.Popen[str]) -> None:
            """Wait for restart process and write final status to log. Output: none. Input: process handle."""
            stdout, stderr = proc.communicate()
            if proc.returncode == 0:
                logger.info("Services restarted successfully")
                if stdout.strip():
                    logger.info("Restart output: %s", stdout.strip())
                return
            logger.error("Restart failed with code %s", proc.returncode)
            if stderr.strip():
                logger.error("Restart error output: %s", stderr.strip())

        Thread(target=_wait_restart, args=(process,), daemon=True).start()

        _json_response(
            self,
            202,
            {
                "status": "accepted",
                "message": "Restart request accepted and running in background",
                "timestamp": time.time(),
            },
        )

    def _handle_stop_services(self, source: str) -> None:
        """Handle POST /stop-services. Output: none. Input: source string."""
        logger.info("Stop-services request from %s", source)
        running = _list_running_services()
        to_stop = [service for service in running if service not in KEEP_SERVICES]

        if to_stop:
            result = _run_compose(["stop", *to_stop])
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "Stop services failed")

        state = {
            "last_stopped_services": to_stop,
            "keep_services": KEEP_SERVICES,
            "updated_at": time.time(),
        }
        _write_service_control_state(state)

        _json_response(
            self,
            200,
            {
                "status": "success",
                "message": "Non-core services stopped.",
                "stopped_services": to_stop,
                "kept_services": KEEP_SERVICES,
            },
        )

    def _handle_start_services(self, source: str) -> None:
        """Handle POST /start-services. Output: none. Input: source string."""
        logger.info("Start-services request from %s", source)
        state = _read_service_control_state()
        to_start = [service for service in state.get("last_stopped_services", []) if isinstance(service, str) and service]

        if to_start:
            result = _run_compose(["up", "-d", *to_start])
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "Start services failed")

        _json_response(
            self,
            200,
            {
                "status": "success",
                "message": "Previously stopped services started.",
                "started_services": to_start,
            },
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
