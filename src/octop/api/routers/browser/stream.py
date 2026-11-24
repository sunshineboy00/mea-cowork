"""WebSocket browser screencast — attaches to octop-browser sessions.

Wire protocol matches the dashboard ``useBrowserStream`` hook (screencast)
and ``useBrowserSessionState`` (listen-only status):

Client → Server::

  {"type": "start", "url": "", "width": 1280, "height": 800,
   "reuse_session": true, "session_id": "<profile>", "mode": "screencast"}
  {"type": "navigate", "url": "https://..."}
  {"type": "click", "x": 100, "y": 200}
  {"type": "mousedown", "x": 100, "y": 200, "button": "left", "buttons": 1}
  {"type": "mousemove", "x": 150, "y": 250, "button": "left", "buttons": 1}
  {"type": "mousemove", "x": 160, "y": 260, "button": "none", "buttons": 0}
  {"type": "mouseup", "x": 150, "y": 250, "button": "left", "buttons": 0}
  {"type": "stop"}

Server → Client (control messages, JSON text)::

  {"type": "status", "status": "streaming"}
  {"type": "tabs", "tabs": [{id, url, title, active}]}
  {"type": "session_update", "session_id": "...", "current_url": "...", ...}
  {"type": "error", "message": "..."}

Server → Client (image frames):

* legacy ``poll`` mode — ``{"type": "frame", "data": "<base64 jpeg>"}`` text
  frames at a fixed interval (unchanged behaviour).
* ``screencast`` mode — CDP ``Page.startScreencast`` event-driven push, one
  WebSocket **binary** frame per image::

      1 byte   version (0x01)
      4 bytes  metadata JSON length, big-endian
      N bytes  metadata JSON ({"w", "h", "seq"})
      rest     raw JPEG payload

  Fallback to base64 ``frame`` text frames when the transport has no binary
  send hook (Bridge peer relay).

Auth: ``?token=<JWT>`` query param (browsers cannot set Authorization on WS).
Listen-only (``?listen_only=1``): still requires ``start``, but never launches
Chrome — attaches to an existing harness session or pushes idle updates.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from octop.api.deps import resolve_user_from_token
from octop.api.routers.browser.harness import (
    control_owner_for,
    harness_list_tabs,
    harness_page_url,
    resolve_harness_session,
)
from octop.infra.utils.browser_media import user_browser_profile

logger = logging.getLogger(__name__)

router = APIRouter()

_FRAME_INTERVAL_S = 1 / 12
_SESSION_INTERVAL_S = 1.0
# Screencast: the CDP client is re-checked every 250 ms (tab switches rebuild
# the CDPClient, so listeners must re-arm on the fresh instance).
_SCREENCAST_SNAPSHOT_INTERVAL_S = 1.0
_SCREENCAST_RECHECK_INTERVAL_S = 0.25
# Adaptive transmission profile: an ordered ladder from best to worst.
# Chrome pushes ~60 FPS on animated pages (measured); the default entry caps
# at 30 FPS. On sustained frame drops (slow client) we degrade one entry per
# window; after a sustained clean window we step back up (asymmetric
# hysteresis: drops trigger instantly, recovery needs stability).
_SCREENCAST_PROFILE_STEPS = (
    {"fps": 60, "quality": 70, "scale": 1.0},
    {"fps": 30, "quality": 70, "scale": 1.0},
    {"fps": 30, "quality": 55, "scale": 1.0},
    {"fps": 30, "quality": 40, "scale": 0.75},
    {"fps": 30, "quality": 40, "scale": 0.5},
    {"fps": 15, "quality": 40, "scale": 0.5},
)
_SCREENCAST_DEFAULT_PROFILE = 1
_SCREENCAST_DROPS_FOR_STEPDOWN = 10
_SCREENCAST_STABLE_S_FOR_STEPUP = 8.0
# Re-arm failures tolerated mid-session (client swaps) before poll fallback.
_SCREENCAST_REARM_FAILURES = 4
# Remote cursor echo: min interval between getComputedStyle probes.
_CURSOR_QUERY_INTERVAL_S = 0.12
# Stream observability: windowed counters logged at INFO each period.
_SCREENCAST_STATS_INTERVAL_S = 10.0
_WS_FRAME_VERSION = 1


def encode_screencast_frame(jpeg: bytes, metadata: dict[str, Any]) -> bytes:
    """Pack one screencast frame into the WS binary wire format."""
    meta = json.dumps(metadata, separators=(",", ":")).encode()
    return (
        struct.pack(">BI", _WS_FRAME_VERSION, len(meta)) + meta + jpeg
    )


def parse_ws_binary_frame(blob: bytes) -> tuple[int, dict[str, Any], bytes]:
    """Parse one WS binary frame; raises ValueError on malformed input."""
    if len(blob) < 5:
        raise ValueError("truncated binary frame header")
    version, meta_len = struct.unpack(">BI", blob[:5])
    if version != _WS_FRAME_VERSION:
        raise ValueError(f"unsupported binary frame version {version}")
    if len(blob) < 5 + meta_len:
        raise ValueError("truncated binary frame metadata length")
    meta = json.loads(blob[5 : 5 + meta_len])
    return version, meta, blob[5 + meta_len :]


def _normalize_nav_url(raw: str) -> str:
    """Prefix a host with ``https://`` unless it already has an http(s) scheme.

    Matches :func:`octop.infra.utils.url.normalize_nav_url`, including the
    case-insensitive scheme check: ``HTTPS://host`` must not become
    ``https://HTTPS://host``.
    """
    t = raw.strip()
    if not t:
        return ""
    if t.lower().startswith(("http://", "https://")):
        return t
    return f"https://{t}"


async def _capture_jpeg(sess: Any) -> str | None:
    try:
        # Check if internal session is fully connected to prevent race conditions during tab switching
        if not getattr(sess._internal, "_connected", False):  # noqa: SLF001
            return None

        # Wrap in a timeout to prevent hanging if the CDP client gets into an inconsistent state during reconnect
        result = await asyncio.wait_for(
            sess._internal.client.send(  # noqa: SLF001
                "Page.captureScreenshot",
                {"format": "jpeg", "quality": 80},
            ),
            timeout=1.5,
        )
        data = result.get("data")
        return str(data) if data else None
    except Exception as exc:
        logger.debug("capture jpeg failed: %s", exc)
        return None


async def _send_json(ws: WebSocket, payload: dict[str, Any]) -> None:
    if ws.application_state == WebSocketState.CONNECTED:
        await ws.send_text(json.dumps(payload))


async def _send_session_snapshot(
    send_json: Any,
    profile: str,
    *,
    sess: Any | None,
) -> None:
    """Push one session_update (+ tabs) frame for *profile*."""
    if sess is None:
        await send_json(
            {
                "type": "session_update",
                "session_id": profile,
                "conversation_id": profile,
                "channel_source": "dashboard",
                "state": "idle",
                "control_owner": control_owner_for(profile),
                "current_url": "",
            },
        )
        await send_json({"type": "tabs", "tabs": []})
        return

    url = await harness_page_url(sess)
    await send_json(
        {
            "type": "session_update",
            "session_id": profile,
            "conversation_id": profile,
            "channel_source": "dashboard",
            "state": "streaming" if url else "idle",
            "control_owner": control_owner_for(profile),
            "current_url": url,
        },
    )
    tabs = await harness_list_tabs(sess)
    await send_json({"type": "tabs", "tabs": tabs})


async def _stream_loop(
    send_json: Any,
    is_connected: Any,
    sess: Any,
    profile: str,
    *,
    listen_only: bool,
) -> None:
    await send_json({"type": "status", "status": "browser_started"})
    await send_json({"type": "status", "status": "streaming"})

    next_snapshot = 0.0
    while is_connected():
        started = time.monotonic()
        if started >= next_snapshot:
            await _send_session_snapshot(send_json, profile, sess=sess)
            next_snapshot = started + _SESSION_INTERVAL_S

        if not listen_only:
            frame = await _capture_jpeg(sess)
            if frame:
                await send_json({"type": "frame", "data": frame})

        await asyncio.sleep(max(0, _FRAME_INTERVAL_S - (time.monotonic() - started)))


class _ScreencastRelay:
    """Bridge CDP ``Page.screencastFrame`` events to one stream client.

    Chrome pushes a frame whenever the page compositor produces a new one
    (measured ~60 FPS on animated pages, nothing on static ones). Each frame
    must be acknowledged via ``Page.screencastFrameAck`` or Chrome stops
    sending. The relay rate-limits to the active profile's FPS cap and drops
    older queued frames so a slow client never accumulates lag; the profile
    (fps / JPEG quality / resolution scale) self-tunes via
    :meth:`should_adjust` driven by *backpressure*, never by that deliberate
    fps-cap coalescing — coalescing is the cap doing its job, not the client
    falling behind. Conflating the two made any animating page degrade to the
    bottom of the ladder within seconds.
    """

    def __init__(
        self,
        client: Any,
        send_frame: Any,
        *,
        profile_idx: int = _SCREENCAST_DEFAULT_PROFILE,
        vw: int = 0,
        vh: int = 0,
    ) -> None:
        self._client = client
        self._send_frame = send_frame
        self._profile_idx = profile_idx
        self._vw = vw
        self._vh = vh
        self._seq = 0
        self._last_push = 0.0
        # Frames coalesced away by the fps cap (observability only — must not
        # drive degradation) vs. frames whose WS send overran the frame budget.
        self._coalesced = 0
        self._backpressure = 0
        self._stable_s = 0.0
        self._last_check = 0.0
        self._pending: asyncio.Task[None] | None = None
        self._stats = {"pushed": 0, "dropped": 0, "bytes": 0, "window_s": 0.0}
        client.on("Page.screencastFrame", self._on_frame)

    @property
    def client(self) -> Any:
        return self._client

    @property
    def profile(self) -> dict[str, Any]:
        return _SCREENCAST_PROFILE_STEPS[self._profile_idx]

    async def arm(self) -> None:
        """Enable the Page domain and (re)start the screencast at the current
        profile (fps cap / JPEG quality / resolution scale). Raises when
        Chrome rejects the command — the caller decides between retry and
        poll fallback."""
        with contextlib.suppress(Exception):
            await self._client.send("Page.enable")
        p = self.profile
        params: dict[str, Any] = {
            "format": "jpeg",
            "quality": p["quality"],
            "everyNthFrame": 1,
        }
        if self._vw > 0 and self._vh > 0:
            params["maxWidth"] = max(1, int(self._vw * p["scale"]))
            params["maxHeight"] = max(1, int(self._vh * p["scale"]))
        await self._client.send("Page.startScreencast", params)

    def should_adjust(self) -> int:
        """Adaptive profile decision, called once per recheck tick.

        Returns +1 (step up after sustained clean window), -1 (step down on
        send backpressure), or 0. The caller re-arms startScreencast on ±1 so
        the new fps cap / quality / scale takes effect.
        """
        now = time.monotonic()
        dt = now - self._last_check if self._last_check else 0.0
        self._last_check = now
        self._stats["window_s"] += dt
        if self._backpressure >= _SCREENCAST_DROPS_FOR_STEPDOWN:
            self._backpressure = 0
            self._stable_s = 0.0
            if self._profile_idx + 1 < len(_SCREENCAST_PROFILE_STEPS):
                self._profile_idx += 1
                return -1
        elif self._backpressure == 0:
            self._stable_s += dt
            if (
                self._stable_s >= _SCREENCAST_STABLE_S_FOR_STEPUP
                and self._profile_idx > 0
            ):
                self._profile_idx -= 1
                self._stable_s = 0.0
                return 1
        else:
            self._stable_s = 0.0
        return 0

    def take_stats(self) -> dict[str, Any]:
        """Windowed counters for observability; resets the window."""
        out = {
            "pushed": self._stats["pushed"],
            "dropped": self._stats["dropped"],
            "coalesced": self._coalesced,
            "backpressure": self._backpressure,
            "bytes": self._stats["bytes"],
            "window_s": round(self._stats["window_s"], 1),
            "profile": f"{self.profile['fps']}fps/q{self.profile['quality']}/x{self.profile['scale']}",
        }
        self._coalesced = 0
        self._stats = {"pushed": 0, "dropped": 0, "bytes": 0, "window_s": 0.0}
        return out

    def detach(self) -> None:
        self._client.off("Page.screencastFrame", self._on_frame)
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    async def _on_frame(self, params: dict[str, Any]) -> None:
        data = params.get("data")
        if not data:
            return
        await self._ack(params.get("sessionId"))
        now = time.monotonic()
        min_interval = 1.0 / self.profile["fps"]
        elapsed = now - self._last_push
        if elapsed >= min_interval:
            self._last_push = now
            await self._push(data, params.get("metadata") or {})
            return
        # Rate-limited: remember this frame and push it after the remaining
        # wait, superseding any still-queued frame (only the latest matters).
        if self._pending is not None:
            self._pending.cancel()
            self._coalesced += 1
            self._stats["dropped"] += 1
        delay = min_interval - elapsed
        self._pending = asyncio.create_task(self._push_later(data, params.get("metadata") or {}, delay))

    async def _push_later(self, data: str, metadata: dict[str, Any], delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            self._last_push = time.monotonic()
            await self._push(data, metadata)
        except asyncio.CancelledError:
            pass

    async def _push(self, b64_jpeg: str, metadata: dict[str, Any]) -> None:
        self._seq += 1
        self._stats["pushed"] += 1
        self._stats["bytes"] += len(b64_jpeg) * 3 // 4
        meta = {
            "seq": self._seq,
            "w": int(metadata.get("deviceWidth") or 0),
            "h": int(metadata.get("deviceHeight") or 0),
        }
        started = time.monotonic()
        await self._send_frame(b64_jpeg, meta)
        # A send that overruns the current frame budget means the client (or the
        # network) cannot absorb the profile's throughput — that, and only that,
        # is the signal that warrants degrading.
        if time.monotonic() - started > 1.0 / max(1, int(self.profile["fps"])):
            self._backpressure += 1

    async def _ack(self, session_id: Any) -> None:
        params: dict[str, Any] = {}
        if session_id is not None:
            params["sessionId"] = session_id
        try:
            await self._client.send("Page.screencastFrameAck", params)
        except Exception as exc:  # noqa: BLE001
            logger.debug("screencast ack failed: %s", exc)


async def _screencast_loop(
    send_json: Any,
    send_frame: Any,
    is_connected: Any,
    sess: Any,
    profile: str,
    *,
    vw: int = 0,
    vh: int = 0,
) -> None:
    """Event-driven screencast: Chrome pushes, we relay.

    * Initial arm failure degrades to the poll loop (never a black screen).
    * The CDP client is re-checked every 250 ms; tab switches rebuild it and a
      swapped client stops pushing frames until re-armed. A few consecutive
      re-arm failures also degrade to poll.
    * Sustained frame drops (slow client) step the JPEG quality down.
    * Status/tabs snapshots keep the 1 s cadence.
    """
    await send_json({"type": "status", "status": "browser_started"})
    await send_json({"type": "status", "status": "streaming"})
    await send_json({"type": "stream_mode", "mode": "screencast"})

    async def fall_back_to_poll(reason: str) -> None:
        logger.warning("screencast unusable (%s); degrading to poll mode", reason)
        await send_json({"type": "stream_mode", "mode": "poll"})
        await _stream_loop(send_json, is_connected, sess, profile, listen_only=False)

    client = sess._internal.client  # noqa: SLF001
    relay = _ScreencastRelay(client, send_frame, vw=vw, vh=vh)
    try:
        await relay.arm()
    except Exception as exc:  # noqa: BLE001
        relay.detach()
        await fall_back_to_poll(f"initial startScreencast failed: {exc}")
        return

    rearm_failures = 0
    next_snapshot = 0.0
    next_stats = 0.0
    try:
        while is_connected():
            started = time.monotonic()
            current = sess._internal.client  # noqa: SLF001
            if current is not relay.client:
                profile_idx = relay._profile_idx  # noqa: SLF001
                relay.detach()
                relay = _ScreencastRelay(current, send_frame, profile_idx=profile_idx, vw=vw, vh=vh)
                try:
                    await relay.arm()
                    rearm_failures = 0
                except Exception as exc:  # noqa: BLE001
                    rearm_failures += 1
                    if rearm_failures >= _SCREENCAST_REARM_FAILURES:
                        relay.detach()
                        await fall_back_to_poll(
                            f"re-arm failed {rearm_failures}x: {exc}"
                        )
                        return
                    logger.debug("re-arm attempt %d failed: %s", rearm_failures, exc)
            elif (adjust := relay.should_adjust()) != 0:
                with contextlib.suppress(Exception):
                    await relay.arm()
                logger.info(
                    "screencast profile %s for %s",
                    relay.profile,
                    profile,
                )
                _ = adjust
            if started >= next_snapshot:
                await _send_session_snapshot(send_json, profile, sess=sess)
                next_snapshot = started + _SCREENCAST_SNAPSHOT_INTERVAL_S
            if started >= next_stats:
                stats = relay.take_stats()
                if stats["pushed"] or stats["dropped"]:
                    logger.info(
                        "screencast stats profile=%s pushed=%d dropped=%d "
                        "coalesced=%d backpressure=%d bytes=%d window=%ss",
                        stats["profile"],
                        stats["pushed"],
                        stats["dropped"],
                        stats["coalesced"],
                        stats["backpressure"],
                        stats["bytes"],
                        stats["window_s"],
                    )
                next_stats = started + _SCREENCAST_STATS_INTERVAL_S
            await asyncio.sleep(
                max(0, _SCREENCAST_RECHECK_INTERVAL_S - (time.monotonic() - started))
            )
    finally:
        relay.detach()
        with contextlib.suppress(Exception):
            await sess._internal.client.send("Page.stopScreencast")  # noqa: SLF001


async def _listen_state_loop(
    send_json: Any,
    is_connected: Any,
    profile: str,
) -> None:
    """Push session_update events without launching Chrome or capturing frames.

    Re-resolves the harness registry each tick so a browser started later by
    the agent becomes visible without reconnecting. Idle (no session) ticks
    use a longer interval to avoid a request/render storm on the dashboard.
    """
    await send_json({"type": "status", "status": "streaming"})

    while is_connected():
        sess = await resolve_harness_session(profile, create=False)
        await _send_session_snapshot(send_json, profile, sess=sess)
        await asyncio.sleep(_SESSION_INTERVAL_S if sess is not None else 2.0)


_CDP_BUTTON_MASK = {"left": 1, "right": 2, "middle": 4, "none": 0}


def _cdp_button(raw: object) -> str:
    name = str(raw or "left").lower()
    return name if name in _CDP_BUTTON_MASK else "left"


def _cdp_buttons(msg: dict[str, Any], *, button: str, event: str) -> int:
    if "buttons" in msg and msg["buttons"] is not None:
        try:
            return max(0, int(msg["buttons"]))
        except (TypeError, ValueError):
            pass
    if event == "mouseup" or button == "none":
        return 0
    return _CDP_BUTTON_MASK.get(button, 0)


async def _dispatch_mouse(
    sess: Any,
    *,
    event_type: str,
    x: int,
    y: int,
    button: str = "none",
    buttons: int = 0,
    click_count: int = 0,
) -> None:
    params: dict[str, Any] = {
        "type": event_type,
        "x": x,
        "y": y,
        "button": button,
        "buttons": buttons,
    }
    if click_count:
        params["clickCount"] = click_count
    await sess._internal.client.send("Input.dispatchMouseEvent", params)  # noqa: SLF001


def _cdp_click_count(msg: dict[str, Any]) -> int:
    raw = msg.get("clickCount", msg.get("click_count", 1))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


_KEY_CODES = {
    "Backspace": 8,
    "Tab": 9,
    "Enter": 13,
    "Escape": 27,
    "PageUp": 33,
    "PageDown": 34,
    "End": 35,
    "Home": 36,
    "ArrowLeft": 37,
    "ArrowUp": 38,
    "ArrowRight": 39,
    "ArrowDown": 40,
    "Delete": 46,
}


async def _dispatch_key(sess: Any, msg: dict[str, Any]) -> None:
    key = str(msg.get("key") or "")
    if not key or key in {"Process", "Dead", "Unidentified"}:
        return
    modifiers = sum(
        bit
        for field, bit in (("altKey", 1), ("ctrlKey", 2), ("metaKey", 4), ("shiftKey", 8))
        if msg.get(field)
    )
    key_code = _KEY_CODES.get(key, ord(key.upper()) if len(key) == 1 and key.isascii() else 0)
    params: dict[str, Any] = {
        "key": key,
        "code": str(msg.get("code") or key),
        "windowsVirtualKeyCode": int(msg.get("keyCode") or key_code),
        "modifiers": modifiers,
    }
    down = {**params, "type": "rawKeyDown", "autoRepeat": bool(msg.get("repeat"))}
    if key == "Enter":
        down.update(type="keyDown", text="\r", unmodifiedText="\r")
    if key.lower() == "a" and modifiers & 6 and not modifiers & 1:
        # Chromium's native editing shortcuts depend on the server OS. Keep
        # Ctrl+A / Cmd+A working when the dashboard and Chrome run on different OSes.
        down["commands"] = ["selectAll"]
    await sess._internal.client.send("Input.dispatchKeyEvent", down)  # noqa: SLF001
    await sess._internal.client.send(  # noqa: SLF001
        "Input.dispatchKeyEvent", {**params, "type": "keyUp"}
    )


def _make_frame_sender(send_bytes: Any | None, send_json: Any) -> Any:
    """Return ``send(b64_jpeg, meta)`` for the screencast relay.

    With a binary transport, decodes and packs into the WS binary format.
    Without one (Bridge peer relay), degrades to the legacy base64 text frame.
    """
    import base64

    if send_bytes is None:

        async def send_text_frame(b64_jpeg: str, meta: dict[str, Any]) -> None:
            await send_json({"type": "frame", "data": b64_jpeg})

        return send_text_frame

    async def send_binary_frame(b64_jpeg: str, meta: dict[str, Any]) -> None:
        blob = encode_screencast_frame(base64.b64decode(b64_jpeg), meta)
        await send_bytes(blob)

    return send_binary_frame


async def _maybe_push_cursor_style(
    sess: Any,
    x: int,
    y: int,
    send_json: Any,
    cursor_state: dict[str, Any],
) -> None:
    """Query the remote element's CSS cursor (throttled + deduplicated) and
    push ``{"type": "cursor"}`` so the dashboard canvas can echo it locally —
    screencast frames do not contain the remote cursor."""
    now = time.monotonic()
    if now - float(cursor_state.get("last", 0.0)) < _CURSOR_QUERY_INTERVAL_S:
        return
    cursor_state["last"] = now
    expression = (
        "(function(){var e=document.elementFromPoint("
        f"{int(x)},{int(y)}"
        ");return e?getComputedStyle(e).cursor:'default';})()"
    )
    try:
        result = await asyncio.wait_for(
            sess._internal.client.send(  # noqa: SLF001
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            ),
            timeout=0.5,
        )
        value = str(result.get("result", {}).get("value") or "default")
    except Exception:  # noqa: BLE001
        return
    if value and value != cursor_state.get("value"):
        cursor_state["value"] = value
        await send_json({"type": "cursor", "cursor": value})


async def _handle_client_event(
    sess: Any,
    msg: dict[str, Any],
    send_json: Any | None = None,
    cursor_state: dict[str, Any] | None = None,
) -> None:
    t = msg.get("type")
    if t == "navigate":
        url = _normalize_nav_url(str(msg.get("url") or ""))
        if url:
            await sess.navigate(url)
    elif t == "goback":
        await sess.go_back()
    elif t == "goforward":
        await sess.go_forward()
    elif t == "reload":
        await sess.reload()
    elif t == "click":
        await sess.click(x=int(msg.get("x") or 0), y=int(msg.get("y") or 0))
    elif t == "dblclick":
        x, y = int(msg.get("x") or 0), int(msg.get("y") or 0)
        await sess.click(x=x, y=y)
        await sess.click(x=x, y=y)
    elif t in ("mousedown", "mouseup", "mousemove"):
        x, y = int(msg.get("x") or 0), int(msg.get("y") or 0)
        button = _cdp_button(msg.get("button"))
        buttons = _cdp_buttons(msg, button=button, event=str(t))
        click_count = _cdp_click_count(msg)
        if send_json is not None and cursor_state is not None:
            with contextlib.suppress(Exception):
                await _maybe_push_cursor_style(sess, x, y, send_json, cursor_state)
        if t == "mousedown":
            await _dispatch_mouse(
                sess,
                event_type="mousePressed",
                x=x,
                y=y,
                button=button if button != "none" else "left",
                buttons=buttons or _CDP_BUTTON_MASK.get(button, 1),
                click_count=click_count,
            )
        elif t == "mouseup":
            await _dispatch_mouse(
                sess,
                event_type="mouseReleased",
                x=x,
                y=y,
                button=button if button != "none" else "left",
                buttons=0,
                click_count=click_count,
            )
        else:
            await _dispatch_mouse(
                sess,
                event_type="mouseMoved",
                x=x,
                y=y,
                button=button,
                buttons=buttons,
            )
    elif t == "scroll":
        delta_x = float(msg.get("deltaX") or msg.get("delta_x") or 0)
        delta_y = float(msg.get("deltaY") or msg.get("delta_y") or 0)
        x = int(msg.get("x") or 0)
        y = int(msg.get("y") or 0)
        if abs(delta_x) > 0.5 or abs(delta_y) > 0.5:
            with contextlib.suppress(Exception):
                await _dispatch_mouse(sess, event_type="mouseMoved", x=x, y=y)
                await sess._internal.client.send(  # noqa: SLF001
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": x,
                        "y": y,
                        "deltaX": delta_x,
                        "deltaY": delta_y,
                    },
                )
        else:
            direction = "down" if delta_y > 0 else "up"
            amount = max(int(abs(delta_y)), 50)
            await sess.scroll(direction=direction, amount=amount)
        # Scrolling changes the element under the pointer without a moving
        # mouse, so the echoed cursor style would stay stale (a link's pointer
        # handedness persisting over plain text). Refresh it here; the sampling
        # throttle in ``_maybe_push_cursor_style`` bounds the cost.
        if send_json is not None and cursor_state is not None:
            with contextlib.suppress(Exception):
                await _maybe_push_cursor_style(sess, x, y, send_json, cursor_state)
    elif t == "type":
        text = str(msg.get("text") or "")
        if text:
            # Committed IME / pasted text is one insertion, not one CDP round
            # trip per character. The page's input event still reaches the recorder.
            await sess._internal.client.send("Input.insertText", {"text": text})  # noqa: SLF001
    elif t == "keydown":
        await _dispatch_key(sess, msg)
    elif t == "tab_switch":
        tab_id = msg.get("tab_id")
        if tab_id is not None:
            await sess.switch_tab(str(tab_id))
    elif t == "tab_close":
        tab_id = msg.get("tab_id")
        if tab_id is not None:
            await sess.close_tab(str(tab_id))
    elif t == "tab_new":
        await sess.new_tab()
    elif t == "resize":
        w = int(msg.get("width") or 0)
        h = int(msg.get("height") or 0)
        if w > 0 and h > 0:
            with contextlib.suppress(Exception):
                await sess._internal.client.send(  # noqa: SLF001
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": w,
                        "height": h,
                        "deviceScaleFactor": 1,
                        "mobile": False,
                    },
                )


@router.websocket("/browser-stream/ws")
async def browser_stream_ws(
    websocket: WebSocket,
    token: str | None = Query(default=None),
    listen_only: int = Query(default=0),
    width: int = Query(default=1280),
    height: int = Query(default=800),
) -> None:
    server = websocket.app.state.octop_server
    if not token:
        await websocket.close(code=4001, reason="missing token")
        return
    try:
        user = resolve_user_from_token(server, token)
    except Exception as exc:
        await websocket.close(code=4001, reason=f"auth failed: {exc}")
        return

    await websocket.accept()

    async def send_json(payload: dict[str, Any]) -> None:
        await _send_json(websocket, payload)

    def is_connected() -> bool:
        return websocket.application_state == WebSocketState.CONNECTED

    async def receive_text() -> str:
        return await websocket.receive_text()

    async def send_bytes(blob: bytes) -> None:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_bytes(blob)

    try:
        await run_browser_stream_session(
            send_json=send_json,
            is_connected=is_connected,
            receive_text=receive_text,
            user_id=int(user.id),
            listen_only=bool(listen_only),
            default_width=width,
            default_height=height,
            send_bytes=send_bytes,
        )
    except WebSocketDisconnect:
        pass
    finally:
        if websocket.application_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


async def run_browser_stream_session(
    *,
    send_json: Any,
    is_connected: Any,
    receive_text: Any,
    user_id: int,
    listen_only: bool = False,
    default_width: int = 1280,
    default_height: int = 800,
    start_msg: dict[str, Any] | None = None,
    send_bytes: Any | None = None,
) -> None:
    """Run one screencast / listen-only session over arbitrary send/receive hooks.

    Used by the local dashboard WebSocket and by Bridge peer relay so both share
    the same Chrome / harness lifecycle.

    ``send_bytes(blob)`` is the WebSocket binary send hook; when absent
    (Bridge peer relay) screencast frames degrade to base64 ``frame`` text
    messages so the JSON-envelope relay keeps working.
    """
    sess: Any | None = None
    profile = user_browser_profile(user_id)
    stream_task: asyncio.Task[None] | None = None

    try:
        if start_msg is None:
            raw = await asyncio.wait_for(receive_text(), timeout=15.0)
            start_msg = json.loads(raw)
        if not isinstance(start_msg, dict) or start_msg.get("type") != "start":
            await send_json({"type": "error", "message": "expected start message"})
            return

        if listen_only:
            stream_task = asyncio.create_task(_listen_state_loop(send_json, is_connected, profile))
        else:
            sess = await resolve_harness_session(profile)
            assert sess is not None  # create=True always returns or raises

            start_url = _normalize_nav_url(str(start_msg.get("url") or ""))
            if start_url and start_url not in {"about:blank"}:
                try:
                    await sess.navigate(start_url)
                except Exception as exc:
                    logger.warning("initial navigate to %s failed: %s", start_url, exc)
                    await send_json(
                        {
                            "type": "error",
                            "message": f"导航到 {start_url} 失败：{exc}",
                        },
                    )

            vw = int(start_msg.get("width") or default_width)
            vh = int(start_msg.get("height") or default_height)
            if vw > 0 and vh > 0:
                with contextlib.suppress(Exception):
                    await sess._internal.client.send(  # noqa: SLF001
                        "Emulation.setDeviceMetricsOverride",
                        {
                            "width": vw,
                            "height": vh,
                            "deviceScaleFactor": 1,
                            "mobile": False,
                        },
                    )

            if str(start_msg.get("mode") or "") == "screencast":
                stream_task = asyncio.create_task(
                    _screencast_loop(
                        send_json,
                        _make_frame_sender(send_bytes, send_json),
                        is_connected,
                        sess,
                        profile,
                        vw=vw,
                        vh=vh,
                    )
                )
            else:
                stream_task = asyncio.create_task(
                    _stream_loop(
                        send_json,
                        is_connected,
                        sess,
                        profile,
                        listen_only=False,
                    )
                )

        cursor_state: dict[str, Any] = {"last": 0.0, "value": "default"}
        while is_connected():
            try:
                raw = await receive_text()
            except WebSocketDisconnect:
                break
            try:
                msg = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "stop":
                break
            if sess is not None:
                with contextlib.suppress(Exception):
                    await _handle_client_event(
                        sess, msg, send_json=send_json, cursor_state=cursor_state
                    )
    except TimeoutError:
        await send_json({"type": "error", "message": "timed out waiting for start"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("browser stream failed")
        if is_connected():
            await send_json({"type": "error", "message": str(exc)})
            await send_json({"type": "status", "status": "error"})
    finally:
        if stream_task is not None:
            stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stream_task
