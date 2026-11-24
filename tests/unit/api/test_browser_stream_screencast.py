"""Screencast push mode: CDP `Page.startScreencast` event-driven frames with a
poll fallback, selected per-session by the client `start.mode` field."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from octop.api.routers.browser import stream
from octop.api.routers.browser.stream import parse_ws_binary_frame


class FakeCDPClient:
    """Minimal CDPClient stand-in: records commands, fires listeners."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []
        self.listeners: dict[str, list] = {}

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.commands.append((method, params or {}))
        if method == "Page.captureScreenshot":
            return {"data": "b64"}
        return {}

    def on(self, event: str, cb) -> None:
        self.listeners.setdefault(event, []).append(cb)

    def off(self, event: str, cb) -> None:
        lst = self.listeners.get(event, [])
        if cb in lst:
            lst.remove(cb)

    async def emit(self, event: str, params: dict) -> None:
        for cb in list(self.listeners.get(event, [])):
            await cb(params)


class FakeSession:
    """Mimics harness session internals for the screencast path."""

    def __init__(self) -> None:
        self._internal = type("_Internal", (), {})()
        self._internal._connected = True
        self._internal.client = FakeCDPClient()
        self.snapshots = 0

    async def navigate(self, url: str) -> None:  # pragma: no cover
        pass


def make_send_json():
    sent: list[dict] = []

    async def send_json(payload: dict) -> None:
        sent.append(payload)

    return send_json, sent


# mode selection ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_defaults_to_poll_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `mode` in start message → legacy poll loop, no screencast commands."""
    sess = FakeSession()
    monkeypatch.setattr(stream, "resolve_harness_session", AsyncMock(return_value=sess))

    async def no_snapshot(*a, **k) -> None:
        pass

    monkeypatch.setattr(stream, "_send_session_snapshot", no_snapshot)

    send_json, sent = make_send_json()
    ticks = 0
    polls = 0

    async def fake_stream_loop(*a, **k) -> None:
        nonlocal ticks
        ticks += 1

    monkeypatch.setattr(stream, "_stream_loop", fake_stream_loop)

    def connected() -> bool:
        nonlocal polls
        polls += 1
        return polls <= 2  # start handling + one pass of the message loop

    async def stop_after_yield() -> str:
        await asyncio.sleep(0.01)  # let stream_task run while we wait
        return json.dumps({"type": "stop"})

    await stream.run_browser_stream_session(
        send_json=send_json,
        is_connected=connected,
        receive_text=stop_after_yield,
        user_id=1,
        start_msg={"type": "start", "width": 800, "height": 600},
    )

    assert ticks == 1
    assert not any(
        m == "Page.startScreencast" for m, _ in sess._internal.client.commands
    )


@pytest.mark.asyncio
async def test_start_with_screencast_mode_pushes_binary_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sess = FakeSession()
    monkeypatch.setattr(stream, "resolve_harness_session", AsyncMock(return_value=sess))
    monkeypatch.setattr(stream, "_send_session_snapshot", AsyncMock())

    send_json, sent = make_send_json()
    binaries: list[bytes] = []

    async def send_bytes(blob: bytes) -> None:
        binaries.append(blob)

    async def stop_after_start() -> str:
        await asyncio.sleep(0.05)
        return json.dumps({"type": "stop"})

    task = asyncio.create_task(
        stream.run_browser_stream_session(
            send_json=send_json,
            is_connected=lambda: True,
            receive_text=stop_after_start,
            user_id=1,
            start_msg={"type": "start", "mode": "screencast", "width": 800, "height": 600},
            send_bytes=send_bytes,
        )
    )
    await asyncio.sleep(0.01)

    client = sess._internal.client
    assert any(m == "Page.startScreencast" for m, _ in client.commands)

    # Simulate two CDP screencast frames; client should receive binary frames.
    await client.emit(
        "Page.screencastFrame",
        {"data": "aGVsbG8=", "metadata": {"deviceWidth": 800, "deviceHeight": 600}},
    )
    await client.emit(
        "Page.screencastFrame",
        {"data": "aGVsbG8=", "metadata": {"deviceWidth": 800, "deviceHeight": 600}},
    )
    await asyncio.sleep(0.08)
    await task

    # No legacy text frames; images ride the binary channel.
    assert all(m.get("type") != "frame" for m in sent)
    assert len(binaries) == 2
    version, meta, jpeg = parse_ws_binary_frame(binaries[-1])
    assert version == 1
    assert meta["w"] == 800 and meta["h"] == 600
    assert jpeg == b"hello"  # base64("aGVsbG8=") decoded payload

    # Page.stopScreencast on teardown
    assert any(m == "Page.stopScreencast" for m, _ in client.commands)


@pytest.mark.asyncio
async def test_screencast_listener_detached_on_tab_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """switch_tab rebuilds the CDP client; the snapshot tick must re-arm the
    screencast listener on the fresh client."""
    sess = FakeSession()
    old_client = sess._internal.client
    monkeypatch.setattr(stream, "resolve_harness_session", AsyncMock(return_value=sess))
    monkeypatch.setattr(stream, "_send_session_snapshot", AsyncMock())

    send_json, _sent = make_send_json()

    async def stop_soon() -> str:
        await asyncio.sleep(0.3)
        return json.dumps({"type": "stop"})

    task = asyncio.create_task(
        stream.run_browser_stream_session(
            send_json=send_json,
            is_connected=lambda: True,
            receive_text=stop_soon,
            user_id=1,
            start_msg={"type": "start", "mode": "screencast"},
        )
    )
    await asyncio.sleep(0.02)
    assert any(m == "Page.startScreencast" for m, _ in old_client.commands)

    # Simulate tab switch: client replaced, new client has no listener.
    new_client = FakeCDPClient()
    sess._internal.client = new_client
    await asyncio.sleep(0.25)  # allow a snapshot tick to re-arm
    await task

    assert any(m == "Page.startScreencast" for m, _ in new_client.commands)


class FailingScreencastClient(FakeCDPClient):
    """Rejects startScreencast like an old/headless-limited Chrome would."""

    async def send(self, method: str, params: dict | None = None) -> dict:
        if method == "Page.startScreencast":
            raise RuntimeError("startScreencast rejected")
        return await super().send(method, params)


@pytest.mark.asyncio
async def test_start_screencast_failure_falls_back_to_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Screencast arm failure must degrade to the poll loop, not black-screen."""
    sess = FakeSession()
    sess._internal.client = FailingScreencastClient()
    monkeypatch.setattr(stream, "resolve_harness_session", AsyncMock(return_value=sess))
    monkeypatch.setattr(stream, "_send_session_snapshot", AsyncMock())

    poll_calls = 0

    async def fake_poll_loop(*a, **k) -> None:
        nonlocal poll_calls
        poll_calls += 1

    monkeypatch.setattr(stream, "_stream_loop", fake_poll_loop)

    send_json, sent = make_send_json()
    polls = 0

    def connected() -> bool:
        nonlocal polls
        polls += 1
        return polls <= 2

    async def stop_after_yield() -> str:
        await asyncio.sleep(0.01)
        return json.dumps({"type": "stop"})

    await stream.run_browser_stream_session(
        send_json=send_json,
        is_connected=connected,
        receive_text=stop_after_yield,
        user_id=1,
        start_msg={"type": "start", "mode": "screencast", "width": 800, "height": 600},
    )

    assert poll_calls == 1
    assert any(
        m.get("type") == "stream_mode" and m.get("mode") == "poll" for m in sent
    )


@pytest.mark.asyncio
async def test_screencast_params_include_viewport_and_page_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """startScreencast must carry maxWidth/maxHeight and be preceded by
    Page.enable (some Chrome builds only dispatch Page events after enable)."""
    sess = FakeSession()
    monkeypatch.setattr(stream, "resolve_harness_session", AsyncMock(return_value=sess))
    monkeypatch.setattr(stream, "_send_session_snapshot", AsyncMock())

    send_json, _sent = make_send_json()

    async def stop_soon() -> str:
        await asyncio.sleep(0.15)
        return json.dumps({"type": "stop"})

    await stream.run_browser_stream_session(
        send_json=send_json,
        is_connected=lambda: True,
        receive_text=stop_soon,
        user_id=1,
        start_msg={"type": "start", "mode": "screencast", "width": 800, "height": 600},
    )

    commands = sess._internal.client.commands
    assert ("Page.enable", {}) in commands
    arm = [(m, p) for m, p in commands if m == "Page.startScreencast"]
    assert arm, "startScreencast never sent"
    method, params = arm[0]
    assert params.get("maxWidth") == 800
    assert params.get("maxHeight") == 600


@pytest.mark.asyncio
async def test_quality_steps_down_on_superseded_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst of superseded (dropped) frames must trigger a re-arm of
    startScreencast at a lower JPEG quality (adaptive degradation)."""
    sess = FakeSession()
    monkeypatch.setattr(stream, "resolve_harness_session", AsyncMock(return_value=sess))
    monkeypatch.setattr(stream, "_send_session_snapshot", AsyncMock())

    send_json, _sent = make_send_json()

    async def stop_soon() -> str:
        await asyncio.sleep(0.45)
        return json.dumps({"type": "stop"})

    task = asyncio.create_task(
        stream.run_browser_stream_session(
            send_json=send_json,
            is_connected=lambda: True,
            receive_text=stop_soon,
            user_id=1,
            start_msg={"type": "start", "mode": "screencast", "width": 800, "height": 600},
        )
    )
    await asyncio.sleep(0.02)

    client = sess._internal.client
    # Rapid burst: each frame within the 33ms rate-limit window supersedes
    # the previous pending one, driving the drop counter past the threshold.
    for _ in range(15):
        await client.emit(
            "Page.screencastFrame",
            {"data": "aGVsbG8=", "metadata": {"deviceWidth": 800, "deviceHeight": 600}},
        )
    await asyncio.sleep(0.4)
    await task

    arms = [p for m, p in client.commands if m == "Page.startScreencast"]
    qualities = [p.get("quality") for p in arms]
    assert 70 in qualities
    assert 55 in qualities, f"expected quality step-down, saw {qualities}"


@pytest.mark.asyncio
async def test_mousemove_pushes_remote_cursor_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hover mousemove should trigger a throttled cursor-style query and push
    the result to the client as ``{"type": "cursor"}``."""
    sess = FakeSession()
    cursor_results = ["pointer", "pointer", "text"]
    calls = {"n": 0}

    async def fake_send(method: str, params: dict | None = None) -> dict:
        if method == "Runtime.evaluate":
            calls["n"] += 1
            value = cursor_results[min(calls["n"] - 1, len(cursor_results) - 1)]
            return {"result": {"type": "string", "value": value}}
        return {}

    sess._internal.client.send = fake_send  # type: ignore[method-assign]

    send_json, sent = make_send_json()
    state: dict = {"last": 0.0, "value": "default"}

    now = 100.0
    monkeypatch.setattr(stream.time, "monotonic", lambda: now)

    for x in (10, 20, 300, 400):
        now += 0.2  # advance the fake clock past the 0.12s throttle
        await stream._handle_client_event(
            sess,
            {"type": "mousemove", "x": x, "y": 5, "button": "none", "buttons": 0},
            send_json=send_json,
            cursor_state=state,
        )

    cursors = [m for m in sent if m.get("type") == "cursor"]
    values = [m.get("cursor") for m in cursors]
    # Throttled: 4 moves spaced 0.2s → 4 queries allowed; transitions deduped.
    assert "pointer" in values
    assert "text" in values
    assert calls["n"] == 4
    # Deduplicated: no consecutive duplicates.
    assert values == list(dict.fromkeys(values))


def test_profile_steps_up_after_stable_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero drops for a sustained window must step the profile back up
    toward the 60 FPS entry."""
    now = 100.0
    monkeypatch.setattr(stream.time, "monotonic", lambda: now)

    client = FakeCDPClient()
    relay = stream._ScreencastRelay(client, AsyncMock(), vw=800, vh=600)
    assert relay.profile["fps"] == 30  # default entry

    results = []
    for _ in range(50):
        now += 0.25  # recheck tick
        results.append(relay.should_adjust())

    assert 1 in results, f"expected a step-up signal, saw {results}"
    assert relay.profile["fps"] == 60
    assert relay.profile["quality"] == 70


def test_profile_scale_degrades_on_sustained_drops() -> None:
    """Three step-downs walk the ladder into the resolution-scaled entry."""
    client = FakeCDPClient()
    relay = stream._ScreencastRelay(client, AsyncMock(), vw=800, vh=600)

    downs = 0
    for _ in range(2):
        relay._superseded = stream._SCREENCAST_DROPS_FOR_STEPDOWN
        downs += int(relay.should_adjust() == -1)
    assert downs == 2
    assert relay.profile["scale"] == 0.75  # idx3: 30fps/q40/x0.75
    assert relay.profile["quality"] == 40

    for _ in range(3):
        relay._superseded = stream._SCREENCAST_DROPS_FOR_STEPDOWN
        downs += int(relay.should_adjust() == -1)
    assert downs == 4  # ladder bottom: idx5
    assert relay.profile["scale"] == 0.5
    assert relay.profile["fps"] == 15


def test_take_stats_counts_pushes_and_drops() -> None:
    """Windowed counters reflect pushes/drops and reset on read."""

    async def send_frame(b64: str, meta: dict) -> None:
        pass

    relay = stream._ScreencastRelay(FakeCDPClient(), send_frame)

    async def run_pushes() -> None:
        await relay._push("aGVsbG8=", {})

    asyncio.run(run_pushes())
    relay._stats["dropped"] = 2
    relay._stats["window_s"] = 10.0

    stats = relay.take_stats()
    assert stats["pushed"] == 1
    assert stats["dropped"] == 2
    assert stats["bytes"] == 6  # len("aGVsbG8=") * 3 // 4
    assert stats["window_s"] == 10.0
    assert "profile" in stats

    # Reset: next window starts empty.
    assert relay.take_stats()["pushed"] == 0
