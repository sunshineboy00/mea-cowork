# Browser stream end-to-end tests

Drives the **real stack** to prove the browser screencast path works end to end:

| Layer | What is real |
|-------|--------------|
| Backend | a spawned `octop run` on a throwaway `OCTOP_HOME` (setup wizard → SQLite → admin → login) |
| Browser | a real Chrome/Chromium over CDP, launched headless with `--no-sandbox` |
| Transport | the authenticated `/api/browser-stream/ws` WebSocket and its binary frame format |
| UI | the built dashboard SPA, driven like a user: onboarding → *启动浏览器* → painted canvas |

## Running

```bash
# 1. the UI test needs the bundle that is actually served
make build-frontend        # or: cd dashboard && npm run build

# 2. run the suite
OCTOP_E2E_BROWSER=1 uv run pytest tests/e2e/browser_stream -q
```

The suite is skipped unless `OCTOP_E2E_BROWSER=1`, and every test carries the
`slow` marker, so plain `pytest` / `make test-fast` stay hermetic.

| Env var | Effect |
|---------|--------|
| `OCTOP_E2E_BROWSER=1` | enable the suite (required) |
| `OCTOP_E2E_KEEP_HOME=1` | keep the throwaway `OCTOP_HOME` for inspection |
| `BROWSER_USE_CHROME_BIN` | pin a Chrome binary instead of auto-discovery |
| `BROWSER_USE_NO_SANDBOX=1` | set automatically by the harness; needed wherever Chrome cannot use its own sandbox |

## What each test protects

`test_stream_e2e.py`

- **negotiation** — the session must come up in `screencast` mode, never a silent poll fallback
- **frame flow** — a live page keeps producing frames (floor of 4 FPS; headless software rasterisation is the bottleneck, not the relay)
- **sequence continuity** — `seq` gaps mean the server skipped a frame; superseded CDP frames legitimately never get a seq
- **static quiet** — no repaint, no frames (the bandwidth argument)
- **input→echo latency** — measured on a hover-only repaint page so every frame after a mouse move is *caused* by it; median must stay far below poll-mode latency
- **cursor echo** — screencast frames carry no cursor, so `{"type":"cursor"}` must report the CSS value
- **viewport coordinates** — `focus.html` only has a text field in the *centre* of the viewport, so a client that maps clicks through the (shrunken) decoded frame misses it; the echoed cursor proves which element the server actually hit
- **wire contract** — bad version / truncated headers raise instead of rendering garbage

`test_dashboard_ui_e2e.py`

- the page's own WebSocket receives binary frames in `screencast` mode
- the viewer canvas is actually painted (size beyond the 300×150 default, colour
  variance and opaque pixels). A dropped-every-frame bug leaves it uniformly
  transparent — a class of failure that server-side tests cannot see.

## Environment notes

- **Chrome sandbox**: macOS reports no sandbox requirement, so the server does not
  pass `--no-sandbox`; restricted hosts (containers, CI, sandboxes) then abort
  Chrome before it binds CDP and the stream fails with
  `failed to attach browser profile … did not receive a valid HTTP response`.
  The harness sets `BROWSER_USE_NO_SANDBOX=1` for exactly this reason.
- **Fixture pages are served over HTTP**: the server normalises target URLs, so
  `data:text/html,…` becomes `https://data:…` and navigation fails.
- **`/remote-browser` redirects to `/workbench/browser`** and does not auto-start
  the session — the test clicks *启动浏览器* like a user does.
- **Frontend mapping is covered by unit tests, not E2E**: the pointer mapping
  (`getBoundingClientRect` → remote viewport, including the `object-fit: contain`
  letterbox offset) lives purely in the dashboard, so
  `dashboard/src/utils/__tests__/canvasCoords.test.ts` pins it against
  degraded frames, letterbox bars and clamping. The E2E above pins the server
  side of the same contract.
