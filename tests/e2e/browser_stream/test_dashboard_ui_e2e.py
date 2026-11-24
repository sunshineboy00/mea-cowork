"""End-to-end assertions for the dashboard UI itself, in a real Chrome.

The binary stream can be perfectly correct server-side and still show a black
viewport if the client drops frames — the exact class of bug unit tests miss.
This drives the built SPA: log in, open the remote browser, start the session,
then assert the canvas is actually painted from the screencast stream.

Requires a built dashboard (``make build-frontend`` or ``npm run build``) so
the served bundle contains the code under test.

Run with ``OCTOP_E2E_BROWSER=1``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from tests.e2e.browser_stream.conftest import launch_chrome, stop_chrome

pytestmark = [pytest.mark.slow, pytest.mark.e2e]

# ws probe installed before any app script runs: counts frames on the socket
# the UI itself uses, so the measurement reflects what the user receives.
WS_PROBE = """
window.__e2e = { frames: 0, bytes: 0, texts: [], url: null };
(function () {
  const Orig = window.WebSocket;
  if (!Orig) return;
  function Wrapped(url, protocols) {
    const ws = protocols === undefined ? new Orig(url) : new Orig(url, protocols);
    window.__e2e.url = String(url);
    ws.addEventListener('message', (ev) => {
      const s = window.__e2e;
      if (typeof ev.data === 'string') {
        if (s.texts.length < 60) s.texts.push(ev.data.slice(0, 160));
      } else {
        s.frames += 1;
        s.bytes += (ev.data.byteLength || ev.data.size || 0);
      }
    });
    return ws;
  }
  Wrapped.prototype = Orig.prototype;
  Wrapped.CONNECTING = 0; Wrapped.OPEN = 1; Wrapped.CLOSING = 2; Wrapped.CLOSED = 3;
  window.WebSocket = Wrapped;
})();
"""

# The viewer canvas is the only one with real dimensions; sample it for
# variance (a painted page has many colours, an untouched canvas has one).
CANVAS_PROBE = """
(function () {
  const list = Array.from(document.querySelectorAll('canvas'));
  if (!list.length) return { canvases: 0 };
  const best = list.reduce((a, b) => (a.width * a.height >= b.width * b.height ? a : b));
  const ctx = best.getContext('2d');
  if (!ctx) return { canvases: list.length, ctx: false };
  let data;
  try { data = ctx.getImageData(0, 0, Math.min(best.width, 480),
                                Math.min(best.height, 320)).data; }
  catch (e) { return { canvases: list.length, err: String(e) }; }
  const colors = new Set();
  let opaque = 0;
  for (let i = 0; i < data.length; i += 16) {
    if (data[i + 3] > 0) opaque += 1;
    colors.add((data[i] >> 4) + ',' + (data[i + 1] >> 4) + ',' + (data[i + 2] >> 4));
  }
  return { canvases: list.length, w: best.width, h: best.height,
           opaqueSamples: opaque, colors: colors.size };
})()
"""

DISMISS_POPUP = """
(() => {
  const hit = Array.from(document.querySelectorAll('button, a')).find(
    (b) => /稍后再说|跳过|知道了|Skip/.test((b.textContent || '').trim())
  );
  if (hit) { hit.click(); return 'dismissed'; }
  const close = document.querySelector('.ant-modal-close');
  if (close) { close.click(); return 'closed'; }
  return 'none';
})()
"""

CLICK_START = """
(() => {
  const btn = Array.from(document.querySelectorAll('button')).find(
    (b) => /启动浏览器/.test((b.textContent || '').trim())
  );
  if (btn && !btn.disabled) { btn.click(); return 'clicked'; }
  return 'none';
})()
"""


async def _evaluate(client, expression: str):  # noqa: ANN001, ANN202
    res = await client.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
    return res.get("result", {}).get("value")


async def test_dashboard_paints_screencast_frames(backend, tmp_path) -> None:  # noqa: ANN001
    from octop_browser.cdp.client import CDPClient

    app = backend.base_url
    chrome = launch_chrome(tmp_path / "ui-profile", window="1440,900")
    try:
        client = CDPClient()
        await client.connect(chrome.ws_url())
        await client.send("Page.enable")
        await client.send("Runtime.enable")
        await client.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )
        await client.send("Page.addScriptToEvaluateOnNewDocument", {"source": WS_PROBE})

        # Seed the token on the app origin, then open the remote browser page.
        await client.send("Page.navigate", {"url": f"{app}/login"})
        await asyncio.sleep(2.5)
        await _evaluate(client, f"localStorage.setItem('auth_token', {json.dumps(backend.token)})")
        await client.send("Page.navigate", {"url": f"{app}/remote-browser"})
        await asyncio.sleep(3.0)

        # First-run onboarding covers the workbench on a fresh install.
        for _ in range(6):
            if await _evaluate(client, DISMISS_POPUP) == "none":
                break
            await asyncio.sleep(1.0)

        started = "none"
        for _ in range(10):
            started = await _evaluate(client, CLICK_START)
            if started == "clicked":
                break
            await asyncio.sleep(1.0)

        # Frames must reach the page and land on the canvas.
        stats: dict = {}
        for _ in range(45):
            await asyncio.sleep(1.0)
            stats = await _evaluate(
                client, "window.__e2e ? JSON.parse(JSON.stringify(window.__e2e)) : {}"
            ) or {}
            if stats.get("frames", 0) >= 25:
                break

        canvas = await _evaluate(client, CANVAS_PROBE) or {}
        errors = await _evaluate(
            client,
            "Array.from(document.querySelectorAll('.ant-message-error, [role=alert]'))"
            ".map((e) => e.textContent.slice(0, 80))",
        ) or []

        mode = None
        for raw in stats.get("texts") or []:
            with contextlib.suppress(Exception):
                payload = json.loads(raw)
                if payload.get("type") == "stream_mode":
                    mode = payload.get("mode")

        diag = (
            f"start_button={started} frames={stats.get('frames', 0)} "
            f"bytes={stats.get('bytes', 0)} mode={mode} canvas={canvas} errors={errors}"
        )
        assert stats.get("frames", 0) >= 10, f"UI received no screencast frames: {diag}"
        assert mode == "screencast", f"UI fell back to another stream mode: {diag}"
        assert canvas.get("canvases", 0) >= 1, f"no canvas mounted: {diag}"
        assert canvas.get("w", 0) > 100, f"canvas still at its default size: {diag}"
        # A painted frame has real variance; a dropped-every-frame bug leaves
        # the canvas uniformly transparent (1 colour, 0 opaque samples).
        assert canvas.get("colors", 0) >= 20, f"canvas is not painted: {diag}"
        assert canvas.get("opaqueSamples", 0) > 0, f"canvas is transparent: {diag}"
        assert not errors, f"page surfaced an error: {diag}"
        assert str(stats.get("url") or "").startswith(("ws://", "wss://")), diag
        await client.close()
    finally:
        stop_chrome(chrome)
