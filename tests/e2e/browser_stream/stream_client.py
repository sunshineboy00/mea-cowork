"""Minimal client for the browser screencast WebSocket.

Decodes the binary wire format (1B version + 4B big-endian metadata length +
JSON metadata + JPEG payload) and exposes the measurements the E2E suite
asserts on: frame rate, sequence continuity, input→echo latency, cursor echo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import aiohttp

WIRE_VERSION = 1
_JPEG_MAGIC = b"\xff\xd8"


class WireFormatError(AssertionError):
    """Raised when a binary frame does not match the documented layout."""


@dataclass
class Frame:
    recv_at: float
    seq: int
    width: int
    height: int
    jpeg_bytes: int


def parse_frame(blob: bytes) -> Frame:
    if len(blob) < 6:
        raise WireFormatError(f"frame too short: {len(blob)}B")
    version = blob[0]
    if version != WIRE_VERSION:
        raise WireFormatError(f"wire version {version}, expected {WIRE_VERSION}")
    meta_len = struct.unpack(">I", blob[1:5])[0]
    if meta_len == 0 or 5 + meta_len >= len(blob):
        raise WireFormatError(f"bad metadata length {meta_len} for {len(blob)}B frame")
    meta = json.loads(blob[5 : 5 + meta_len].decode())
    jpeg = blob[5 + meta_len :]
    if jpeg[:2] != _JPEG_MAGIC:
        raise WireFormatError("payload is not a JPEG")
    return Frame(
        recv_at=time.monotonic(),
        seq=int(meta.get("seq") or 0),
        width=int(meta.get("w") or 0),
        height=int(meta.get("h") or 0),
        jpeg_bytes=len(jpeg),
    )


@dataclass
class StreamClient:
    """Collects frames and control messages from one stream session."""

    ws: aiohttp.ClientWebSocketResponse
    frames: list[Frame] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    mode: str | None = None
    cursor: str | None = None
    errors: list[str] = field(default_factory=list)

    def _absorb(self, msg: aiohttp.WSMessage) -> Frame | None:
        if msg.type == aiohttp.WSMsgType.BINARY:
            frame = parse_frame(msg.data)
            self.frames.append(frame)
            return frame
        if msg.type == aiohttp.WSMsgType.TEXT:
            payload = json.loads(msg.data)
            self.messages.append(payload)
            kind = payload.get("type")
            if kind == "stream_mode":
                self.mode = payload.get("mode")
            elif kind == "cursor":
                self.cursor = payload.get("cursor")
            elif kind == "error":
                self.errors.append(str(payload.get("message") or ""))
        return None

    async def pump(self, seconds: float) -> None:
        """Read messages for ``seconds``."""
        end = time.monotonic() + seconds
        while (remaining := end - time.monotonic()) > 0:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), timeout=min(0.25, remaining))
            except TimeoutError:
                continue
            self._absorb(msg)

    async def drain(self, quiet: float = 0.4) -> None:
        """Read until nothing arrives for ``quiet`` seconds."""
        while True:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), timeout=quiet)
            except TimeoutError:
                return
            self._absorb(msg)

    async def wait_frame(self, timeout: float) -> Frame | None:
        end = time.monotonic() + timeout
        while (remaining := end - time.monotonic()) > 0:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), timeout=min(0.5, remaining))
            except TimeoutError:
                continue
            frame = self._absorb(msg)
            if frame is not None:
                return frame
        return None

    async def wait_for_mode(self, timeout: float) -> str | None:
        end = time.monotonic() + timeout
        while self.mode is None and (remaining := end - time.monotonic()) > 0:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), timeout=min(0.5, remaining))
            except TimeoutError:
                continue
            self._absorb(msg)
        return self.mode

    async def start(self, *, url: str, width: int, height: int, mode: str = "screencast") -> None:
        await self.ws.send_json(
            {"type": "start", "mode": mode, "width": width, "height": height, "url": url}
        )

    async def navigate(self, url: str) -> None:
        await self.ws.send_json({"type": "navigate", "url": url})

    async def move(self, x: int, y: int) -> None:
        await self.ws.send_json({"type": "mousemove", "x": x, "y": y})

    async def stop(self) -> None:
        await self.ws.send_json({"type": "stop"})


@asynccontextmanager
async def open_stream(
    backend: Any,
    *,
    url: str,
    width: int = 1024,
    height: int = 768,
    mode: str = "screencast",
    mode_timeout: float = 60.0,
) -> AsyncIterator[StreamClient]:
    """Open one authenticated stream session and start it on ``url``."""
    ws_base = str(backend.base_url).replace("http://", "ws://").replace("https://", "wss://")
    ws_url = (
        f"{ws_base}/api/browser-stream/ws"
        f"?token={backend.token}&width={width}&height={height}"
    )
    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(ws_url, max_msg_size=16 * 1024 * 1024)
        client = StreamClient(ws=ws)
        try:
            await client.start(url=url, width=width, height=height, mode=mode)
            negotiated = await client.wait_for_mode(timeout=mode_timeout)
            if negotiated is not None:
                client.mode = negotiated
            yield client
            with contextlib.suppress(Exception):
                await client.stop()
        finally:
            await ws.close()
    finally:
        await session.close()


def median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[idx]
