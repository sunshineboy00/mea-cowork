"""Screencast binary frame protocol: 1B version + 4B big-endian meta length
+ meta JSON + raw JPEG payload."""

import json

from octop.api.routers.browser.stream import encode_screencast_frame, parse_ws_binary_frame

# encode_screencast_frame ------------------------------------------------------


def test_encode_appends_jpeg_after_metadata() -> None:
    payload = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0x01, 0x02])
    blob = encode_screencast_frame(payload, {"seq": 7, "w": 1280, "h": 800})

    assert blob[0] == 1  # version
    meta_len = int.from_bytes(blob[1:5], "big")
    meta = json.loads(blob[5 : 5 + meta_len])
    assert meta == {"seq": 7, "w": 1280, "h": 800}
    assert blob[5 + meta_len :] == payload


def test_encode_is_lossless_roundtrip() -> None:
    payload = b"\x00\xffjpeg-bytes"
    version, meta, data = parse_ws_binary_frame(
        encode_screencast_frame(payload, {"seq": 1})
    )
    assert version == 1
    assert meta == {"seq": 1}
    assert data == payload


# parse_ws_binary_frame --------------------------------------------------------


def test_parse_rejects_unknown_version() -> None:
    blob = b"\x02" + (0).to_bytes(4, "big") + b"{}"
    try:
        parse_ws_binary_frame(blob)
    except ValueError as exc:
        assert "version" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown version")


def test_parse_rejects_truncated_header() -> None:
    try:
        parse_ws_binary_frame(b"\x01\x00\x00")
    except ValueError as exc:
        assert "truncated" in str(exc)
    else:
        raise AssertionError("expected ValueError for truncated header")


def test_parse_rejects_meta_length_mismatch() -> None:
    blob = b"\x01" + (99).to_bytes(4, "big") + b"{}"
    try:
        parse_ws_binary_frame(blob)
    except ValueError as exc:
        assert "metadata length" in str(exc)
    else:
        raise AssertionError("expected ValueError for bad meta length")
