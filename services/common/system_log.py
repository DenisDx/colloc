"""Shared system log helpers.

Provides one write path and timestamp format for all services writing to system.log.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


def resolve_system_log_path() -> Path:
    """Resolve writable system log path. Output: absolute path. Input: none."""
    candidates: list[Path] = []
    env_path = os.getenv("SYSTEM_LOG_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("/srv/logs/system.log"))

    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            return path
        except OSError:
            continue

    return candidates[0]


def utc_timestamp() -> str:
    """Build UTC timestamp with milliseconds. Output: ISO-8601 string. Input: none."""
    return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


def append_system_log(component: str, event: str, message: str, details: dict[str, Any] | None = None) -> str:
    """Append one system log line. Output: written text line. Input: component, event, message, optional details."""
    line = f"[{utc_timestamp()}] {component}.{event}: {message}"
    if details:
        line = f"{line} | {json.dumps(details, ensure_ascii=False)}"

    log_path = resolve_system_log_path()
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")
    return line
