"""Asterisk ARI listener.

Creates ARI mixing bridges and External Media channels for SIP calls,
then coordinates call lifecycle with colloc SIP media service.
"""

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx
import websockets
from websockets.exceptions import ConnectionClosedError, InvalidStatus


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def resolve_system_log_path() -> Path:
    """Resolve writable system log path. Output: absolute path. Input: none."""
    candidates: list[Path] = []
    env_path = os.getenv("SYSTEM_LOG_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        [
            Path("/srv/logs/system.log"),
        ]
    )

    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            return path
        except OSError:
            continue

    return candidates[0]


SYSTEM_LOG_PATH = resolve_system_log_path()


def append_system_log(component: str, event: str, message: str, details: dict | None = None) -> str:
    """Append one system log line. Output: written text line. Input: component, event, message, optional details."""
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"[{timestamp}] {component}.{event}: {message}"
    if details:
        line = f"{line} | {json.dumps(details, ensure_ascii=False)}"
    with open(SYSTEM_LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")
    return line


ASTERISK_HTTP_URL = os.getenv("ASTERISK_HTTP_URL", "http://asterisk:8088/ari").rstrip("/")
ASTERISK_ARI_USER = os.getenv("ASTERISK_ARI_USER", "colloc")
ASTERISK_ARI_PASSWORD = os.getenv("ASTERISK_ARI_PASSWORD", "change-me")
ASTERISK_ARI_APP = os.getenv("ASTERISK_ARI_APP", "colloc-call-handler")

SIP_SERVICE_URL = os.getenv("ASTERISK_SIP_SERVICE_URL", "http://sip-service:8004").rstrip("/")
SIP_MEDIA_HOST = os.getenv("SIP_MEDIA_HOST", "sip-service")
SIP_MEDIA_PORT_START = int(os.getenv("SIP_MEDIA_PORT_START", "6900"))
SIP_MEDIA_PORT_END = int(os.getenv("SIP_MEDIA_PORT_END", "6999"))
SIP_DEFAULT_LANGUAGE = os.getenv("SIP_DEFAULT_LANGUAGE", "ru")


@dataclass
class ManagedCall:
    """Internal call mapping between caller, bridge, and external media channel."""

    caller_channel_id: str
    bridge_id: str
    external_channel_id: str
    media_port: int
    caller: str


class MediaPortAllocator:
    """Simple cyclic media-port allocator for External Media RTP endpoints."""

    def __init__(self, start: int, end: int) -> None:
        self.start = start
        self.end = end
        self._next = start
        self._in_use: set[int] = set()
        self._lock = asyncio.Lock()

    async def alloc(self) -> int:
        """Allocate free UDP port. Output: port int. Input: none."""
        async with self._lock:
            capacity = self.end - self.start + 1
            for _ in range(capacity):
                candidate = self._next
                self._next = candidate + 1
                if self._next > self.end:
                    self._next = self.start
                if candidate not in self._in_use:
                    self._in_use.add(candidate)
                    return candidate
            raise RuntimeError("No free SIP media ports")

    async def release(self, port: int) -> None:
        """Release previously allocated port. Output: none. Input: port int."""
        async with self._lock:
            self._in_use.discard(port)


class AriListener:
    """ARI event loop for bridge + external media call control."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(auth=(ASTERISK_ARI_USER, ASTERISK_ARI_PASSWORD), timeout=10.0)
        self.calls_by_caller_channel: dict[str, ManagedCall] = {}
        self.calls_by_external_channel: dict[str, ManagedCall] = {}
        self.alloc = MediaPortAllocator(SIP_MEDIA_PORT_START, SIP_MEDIA_PORT_END)

    def _ws_url(self) -> str:
        """Build ARI websocket URL. Output: websocket URL string. Input: none."""
        if ASTERISK_HTTP_URL.startswith("https://"):
            base = "wss://" + ASTERISK_HTTP_URL[len("https://") :]
        else:
            base = "ws://" + ASTERISK_HTTP_URL[len("http://") :]

        params = {
            "app": ASTERISK_ARI_APP,
            "subscribeAll": "true",
            "api_key": f"{ASTERISK_ARI_USER}:{ASTERISK_ARI_PASSWORD}",
        }
        return f"{base}/events?{urlencode(params)}"

    async def _ari_post(self, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        """Call ARI POST endpoint. Output: JSON dict. Input: path, params, optional JSON body."""
        url = f"{ASTERISK_HTTP_URL}{path}"
        response = await self.client.post(url, params=params, json=json_body)
        response.raise_for_status()
        if not response.content:
            return {}
        try:
            return response.json()
        except Exception:
            return {}

    async def _ari_delete(self, path: str, *, params: dict | None = None) -> None:
        """Call ARI DELETE endpoint. Output: none. Input: path and params."""
        url = f"{ASTERISK_HTTP_URL}{path}"
        response = await self.client.delete(url, params=params)
        if response.status_code not in {200, 204, 404}:
            response.raise_for_status()

    async def _sip_post(self, path: str, payload: dict) -> None:
        """Call SIP service endpoint. Output: none. Input: path and payload."""
        url = f"{SIP_SERVICE_URL}{path}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()

    async def _is_external_media_channel(self, event: dict) -> bool:
        """Detect whether channel belongs to External Media leg. Output: bool. Input: ARI event."""
        channel = event.get("channel") or {}
        name = str(channel.get("name") or "")
        if name.startswith("UnicastRTP/"):
            return True
        endpoint = str(channel.get("channeltype") or "")
        return endpoint.lower() == "unicastrtp"

    async def _handle_stasis_start(self, event: dict) -> None:
        """Handle caller StasisStart by creating bridge + external media channel."""
        if await self._is_external_media_channel(event):
            return

        channel = event.get("channel") or {}
        caller_channel_id = str(channel.get("id") or "")
        if not caller_channel_id:
            return
        if caller_channel_id in self.calls_by_caller_channel:
            return

        caller = str((channel.get("caller") or {}).get("number") or "unknown")
        media_port = await self.alloc.alloc()

        logger.info("StasisStart caller=%s channel=%s media_port=%s", caller, caller_channel_id, media_port)
        append_system_log(
            "sip",
            "ari_start",
            "ARI call handling started.",
            {"channel_id": caller_channel_id, "caller": caller, "media_port": media_port},
        )

        bridge_id = ""
        external_channel_id = ""
        asterisk_rtp_host = ""
        asterisk_rtp_port = 0

        try:
            # Answer caller channel explicitly; otherwise call may leave Stasis quickly without media.
            try:
                await self._ari_post(f"/channels/{caller_channel_id}/answer")
            except httpx.HTTPStatusError as exc:
                # 409 means channel is already answered/up; continue flow.
                if exc.response.status_code != 409:
                    raise

            bridge = await self._ari_post(
                "/bridges",
                json_body={
                    "type": "mixing",
                    "name": f"sip-{caller_channel_id[:8]}",
                },
            )
            bridge_id = str(bridge.get("id") or "")
            if not bridge_id:
                raise RuntimeError("Bridge creation returned empty id")

            await self._ari_post(f"/bridges/{bridge_id}/addChannel", params={"channel": caller_channel_id})

            ext = await self._ari_post(
                "/channels/externalMedia",
                params={
                    "app": ASTERISK_ARI_APP,
                    "external_host": f"{SIP_MEDIA_HOST}:{media_port}",
                    "format": "ulaw",
                    "encapsulation": "rtp",
                    "transport": "udp",
                    "connection_type": "client",
                    "direction": "both",
                },
            )
            external_channel_id = str(ext.get("id") or "")
            if not external_channel_id:
                raise RuntimeError("External media channel returned empty id")

            # Asterisk reports where it expects inbound RTP from external media app.
            channelvars = ext.get("channelvars") or {}
            host = str(channelvars.get("UNICASTRTP_LOCAL_ADDRESS") or "").strip()
            port_raw = channelvars.get("UNICASTRTP_LOCAL_PORT")
            try:
                port = int(port_raw) if port_raw is not None else 0
            except (TypeError, ValueError):
                port = 0

            if host and port > 0:
                asterisk_rtp_host = host
                asterisk_rtp_port = port

            await self._ari_post(f"/bridges/{bridge_id}/addChannel", params={"channel": external_channel_id})

            await self._sip_post(
                "/ari/call/start",
                {
                    "channel_id": caller_channel_id,
                    "caller": caller,
                    "media_port": media_port,
                    "language": SIP_DEFAULT_LANGUAGE,
                    "asterisk_rtp_host": asterisk_rtp_host,
                    "asterisk_rtp_port": asterisk_rtp_port,
                },
            )

            managed = ManagedCall(
                caller_channel_id=caller_channel_id,
                bridge_id=bridge_id,
                external_channel_id=external_channel_id,
                media_port=media_port,
                caller=caller,
            )
            self.calls_by_caller_channel[caller_channel_id] = managed
            self.calls_by_external_channel[external_channel_id] = managed
            logger.info(
                "Call ready caller_channel=%s external_channel=%s bridge=%s rtp_target=%s:%s",
                caller_channel_id,
                external_channel_id,
                bridge_id,
                asterisk_rtp_host or "unknown",
                asterisk_rtp_port,
            )
            append_system_log(
                "sip",
                "ari_ready",
                "ARI bridge and external media are ready.",
                {
                    "channel_id": caller_channel_id,
                    "external_channel_id": external_channel_id,
                    "bridge_id": bridge_id,
                    "rtp_target": f"{asterisk_rtp_host or 'unknown'}:{asterisk_rtp_port}",
                },
            )
        except Exception:
            logger.exception("Failed to initialize call for channel=%s", caller_channel_id)
            append_system_log("sip", "ari_error", "ARI call initialization failed.", {"channel_id": caller_channel_id})
            await self._safe_cleanup(caller_channel_id, external_channel_id, bridge_id, media_port)
            raise

    async def _safe_cleanup(
        self,
        caller_channel_id: str,
        external_channel_id: str,
        bridge_id: str,
        media_port: int,
    ) -> None:
        """Best-effort cleanup after setup failure. Output: none. Input: IDs and media port."""
        try:
            await self._sip_post("/ari/call/end", {"channel_id": caller_channel_id})
        except Exception:
            pass
        for channel_id in (external_channel_id, caller_channel_id):
            if channel_id:
                try:
                    await self._ari_delete(f"/channels/{channel_id}")
                except Exception:
                    pass
        if bridge_id:
            try:
                await self._ari_delete(f"/bridges/{bridge_id}")
            except Exception:
                pass
        await self.alloc.release(media_port)

    async def _handle_stasis_end(self, event: dict) -> None:
        """Handle StasisEnd by tearing down bridge and SIP media session."""
        channel = event.get("channel") or {}
        channel_id = str(channel.get("id") or "")
        if not channel_id:
            return

        managed = self.calls_by_caller_channel.pop(channel_id, None)
        if not managed:
            managed = self.calls_by_external_channel.pop(channel_id, None)
            if managed:
                self.calls_by_caller_channel.pop(managed.caller_channel_id, None)
        if not managed:
            return

        self.calls_by_external_channel.pop(managed.external_channel_id, None)
        self.calls_by_caller_channel.pop(managed.caller_channel_id, None)

        logger.info("StasisEnd channel=%s caller=%s", channel_id, managed.caller_channel_id)
        append_system_log(
            "sip",
            "ari_stop",
            "ARI call handling stopped.",
            {"channel_id": channel_id, "caller_channel_id": managed.caller_channel_id, "bridge_id": managed.bridge_id},
        )

        try:
            await self._sip_post("/ari/call/end", {"channel_id": managed.caller_channel_id})
        except Exception:
            logger.exception("Failed to notify sip-service about call end")

        for doomed_channel in (managed.external_channel_id, managed.caller_channel_id):
            try:
                await self._ari_delete(f"/channels/{doomed_channel}")
            except Exception:
                pass

        try:
            await self._ari_delete(f"/bridges/{managed.bridge_id}")
        except Exception:
            pass

        await self.alloc.release(managed.media_port)

    async def _handle_dtmf(self, event: dict) -> None:
        """Trigger barge-in on DTMF '#'. Output: none. Input: ARI event."""
        channel = event.get("channel") or {}
        channel_id = str(channel.get("id") or "")
        digit = str(event.get("digit") or "")
        if digit != "#":
            return

        managed = self.calls_by_caller_channel.get(channel_id)
        if not managed:
            managed = self.calls_by_external_channel.get(channel_id)
        if not managed:
            return

        logger.info("DTMF barge-in caller_channel=%s", managed.caller_channel_id)
        try:
            await self._sip_post("/ari/call/barge-in", {"channel_id": managed.caller_channel_id})
        except Exception:
            logger.exception("Failed to trigger barge-in in sip-service")

    async def _handle_channel_destroyed(self, event: dict) -> None:
        """Log channel destroy causes for diagnostics. Output: none. Input: ARI event."""
        channel = event.get("channel") or {}
        channel_id = str(channel.get("id") or "")
        if not channel_id:
            return

        managed = self.calls_by_caller_channel.get(channel_id) or self.calls_by_external_channel.get(channel_id)
        if not managed:
            return

        cause = event.get("cause")
        cause_txt = str(event.get("cause_txt") or "")
        logger.info(
            "ChannelDestroyed channel=%s caller=%s cause=%s cause_txt=%s",
            channel_id,
            managed.caller_channel_id,
            cause,
            cause_txt or "unknown",
        )
        append_system_log(
            "sip",
            "ari_channel_destroyed",
            "ARI channel destroyed event received.",
            {"channel_id": channel_id, "caller_channel_id": managed.caller_channel_id, "cause": cause, "cause_txt": cause_txt or "unknown"},
        )

    async def handle_event(self, event: dict) -> None:
        """Route ARI event by type. Output: none. Input: event dict."""
        event_type = str(event.get("type") or "")
        if event_type == "StasisStart":
            await self._handle_stasis_start(event)
        elif event_type == "StasisEnd":
            await self._handle_stasis_end(event)
        elif event_type == "ChannelDtmfReceived":
            await self._handle_dtmf(event)
        elif event_type == "ChannelDestroyed":
            await self._handle_channel_destroyed(event)

    async def run_forever(self) -> None:
        """Run websocket loop with reconnect logic. Output: none. Input: none."""
        ws_url = self._ws_url()
        headers = {
            "Authorization": "Basic "
            + base64.b64encode(f"{ASTERISK_ARI_USER}:{ASTERISK_ARI_PASSWORD}".encode("ascii")).decode("ascii")
        }

        while True:
            try:
                logger.info("Connecting ARI websocket %s", ws_url)
                async with websockets.connect(ws_url, additional_headers=headers, ping_interval=20, ping_timeout=10) as ws:
                    logger.info("Connected to ARI websocket")
                    async for raw in ws:
                        try:
                            event = json.loads(raw)
                        except ValueError:
                            continue
                        try:
                            await self.handle_event(event)
                        except Exception:
                            logger.exception("Error while processing ARI event")
            except (ConnectionClosedError, InvalidStatus) as exc:
                logger.warning("ARI websocket disconnected (%s); reconnect in 2s", exc)
                await asyncio.sleep(2)
            except OSError as exc:
                logger.warning("ARI websocket transport/connect error (%s); reconnect in 2s", exc)
                await asyncio.sleep(2)
            except Exception:
                logger.exception("Unexpected ARI listener error; reconnect in 2s")
                await asyncio.sleep(2)

    async def close(self) -> None:
        """Close HTTP client. Output: none. Input: none."""
        await self.client.aclose()


async def main() -> None:
    """Entrypoint for ARI listener process. Output: none. Input: none."""
    listener = AriListener()
    try:
        await listener.run_forever()
    finally:
        await listener.close()


if __name__ == "__main__":
    asyncio.run(main())
