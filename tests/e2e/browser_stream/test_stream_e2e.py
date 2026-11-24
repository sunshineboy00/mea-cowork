"""End-to-end assertions for the browser screencast WebSocket.

Covers what unit tests cannot: a real Chrome, a real session, the real
authenticated WebSocket, and the binary wire format as observed by a client.

Run with ``OCTOP_E2E_BROWSER=1``.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from tests.e2e.browser_stream.stream_client import (
    StreamClient,
    WireFormatError,
    median,
    open_stream,
    parse_frame,
    percentile,
)

pytestmark = [pytest.mark.slow, pytest.mark.e2e]

VIEW_W, VIEW_H = 1024, 768


def _page(pages_url: str, name: str) -> str:
    """Unique URL per session.

    Sessions are reused across tests (one Chrome profile), and re-navigating a
    static page to the *same* URL repaints nothing — which looks like "no
    frames". A unique query string forces a real navigation, so a page that
    never animates still yields its initial frame.
    """
    return f"{pages_url}/{name}?run={uuid4().hex}"


async def _first_frame(client: StreamClient, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while not client.frames and time.monotonic() < deadline:
        await client.pump(0.5)
    assert client.frames, f"no frames arrived; server errors: {client.errors}"
    return client.frames[0]


async def test_session_negotiates_screencast_and_streams_frames(backend, pages_url) -> None:
    async with open_stream(backend, url=_page(pages_url, "anim.html"),
                           width=VIEW_W, height=VIEW_H) as client:
        assert client.mode == "screencast", f"negotiated {client.mode!r}, expected screencast"
        first = await _first_frame(client)
        assert not client.errors, f"server reported errors: {client.errors}"
        assert first.seq == 1, f"first frame seq={first.seq}"
        assert first.width and first.height, f"frame metadata lacks viewport: {first}"


async def test_frame_rate_is_capped_not_stalled(backend, pages_url) -> None:
    """A live page must keep producing frames after warm-up."""
    async with open_stream(backend, url=_page(pages_url, "anim.html"),
                           width=VIEW_W, height=VIEW_H) as client:
        await _first_frame(client)
        await client.pump(1.5)  # let the compositor settle
        client.frames.clear()
        started = time.monotonic()
        await client.pump(3.0)
        elapsed = time.monotonic() - started

        fps = len(client.frames) / elapsed
        # Headless software rasterisation is the real bottleneck on CI-class
        # machines (measured 7–16 FPS at 1024×768), so this is a floor, not the
        # product's ceiling (the relay cap itself is 30/60 FPS).
        assert fps >= 4.0, f"screencast stalled at {fps:.1f} FPS ({len(client.frames)} frames)"


async def test_sequence_numbers_are_contiguous(backend, pages_url) -> None:
    """``seq`` counts frames the relay sent, so a gap means a skipped frame.

    Superseded (rate-limited) CDP frames never get a seq, which is why
    contiguity is the right invariant rather than "one seq per Chrome frame".
    """
    async with open_stream(backend, url=_page(pages_url, "anim.html"),
                           width=VIEW_W, height=VIEW_H) as client:
        await _first_frame(client)
        await client.pump(2.5)
        seqs = [f.seq for f in client.frames]
        assert len(seqs) >= 4, f"too few frames to judge continuity: {seqs}"
        assert seqs == sorted(seqs), f"seq went backwards: {seqs[:10]}"
        gaps = {b - a for a, b in zip(seqs, seqs[1:], strict=False)}
        assert gaps == {1}, f"non-contiguous seq (gaps={sorted(gaps)}): {seqs[:12]}"


async def test_static_page_stops_emitting_frames(backend, pages_url) -> None:
    """No repaint → no frames. This is the whole bandwidth argument."""
    async with open_stream(backend, url=_page(pages_url, "anim.html"),
                           width=VIEW_W, height=VIEW_H) as client:
        await _first_frame(client)
        await client.pump(1.0)
        await client.navigate(_page(pages_url, "static.html"))
        await asyncio.sleep(0.5)
        await client.drain(0.5)

        baseline = len(client.frames)
        await client.pump(2.5)
        emitted = len(client.frames) - baseline
        assert emitted <= 1, f"static page emitted {emitted} frames in 2.5s (expected 0–1)"


async def test_input_to_echo_latency(backend, pages_url) -> None:
    """Measure the round trip on a page that only repaints on hover.

    A static page with a hover-only colour change means each frame that follows
    a mouse move is *caused* by that input — no animation noise, no stale
    in-flight frames.
    """
    async with open_stream(backend, url=_page(pages_url, "hover.html"),
                           width=VIEW_W, height=VIEW_H) as client:
        await _first_frame(client)
        await client.pump(0.5)
        await client.drain(0.6)

        samples: list[float] = []
        for index in range(12):
            x = 600 if index % 2 else 100  # cross the hover boundary each time
            await client.drain(0.08)
            sent = time.monotonic()
            await client.move(x, 200)
            frame = await client.wait_frame(timeout=1.0)
            if frame is not None:
                samples.append((frame.recv_at - sent) * 1000)
            await asyncio.sleep(0.1)

        assert len(samples) >= 6, f"only {len(samples)}/12 inputs produced a frame: {samples}"
        med = median(samples)
        p90 = percentile(samples, 0.9)
        # Generous ceiling: the point is catching a regression back to
        # poll-mode latency (67ms median measured) or a broken echo path.
        assert med < 250.0, f"echo latency median {med:.1f}ms (p90 {p90:.1f}ms)"


async def test_cursor_style_is_echoed(backend, pages_url) -> None:
    """Frames carry no cursor, so the server must echo the CSS cursor value."""
    async with open_stream(backend, url=_page(pages_url, "hover.html"),
                           width=VIEW_W, height=VIEW_H) as client:
        await _first_frame(client)
        await client.pump(0.5)
        await client.drain(0.4)
        client.cursor = None

        seen: list[str] = []
        for index in range(8):
            await client.move(600 if index % 2 else 100, 200)
            await asyncio.sleep(0.25)
            await client.drain(0.05)
            if client.cursor and client.cursor not in seen:
                seen.append(client.cursor)
            if "pointer" in seen:
                break

        assert "pointer" in seen, f"cursor echo never reported 'pointer' (saw {seen})"


def test_wire_contract_rejects_malformed_frames() -> None:
    """Bad version / truncated headers must fail loudly, not render garbage."""
    with pytest.raises(WireFormatError):
        parse_frame(b"\x02\x00\x00\x00\x02{}")
    with pytest.raises(WireFormatError):
        parse_frame(b"\x01\x00\x00")
