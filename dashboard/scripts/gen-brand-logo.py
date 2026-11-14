#!/usr/bin/env python3
"""Regenerate the horizontal MEA Cowork brand lockup.

The MEA Cowork reference lockup (kept in git as
``dashboard/public/logo_horizontal_dark.bak.png``) defined the original
proportions: a compact mark on the left and a heavy geometric sans wordmark
on the right, optically balanced against each other.

This script rebuilds that lockup with the new mascot mark
(``octop-mascot-tasks.png``) and the ``MEA Cowork`` wordmark set in Poppins
ExtraBold, the closest open-source match for the reference's heavy geometric
letterforms (round O/o, angled terminals, uniform stroke weight).

Geometry is expressed in the reference's own units (753x289 artboard) so the
new lockup keeps the original rhythm:

* mark height             = 207u
* mark/wordmark gap       =  26u
* cap height              =  80u  (scaled down from the reference's 105u:
  "MEA Cowork" carries twice the characters of "Octop", so the reference cap
  would make the wordmark 3.2x the reference's width and overpower the mark)
* vertical structure      = the wordmark's CAP BAND is centred on the mark's
  centre (the reference's own rule, measured offset -2u). The reference's
  full ink block sits ~16u lower only because of its 'p' descender; a
  descender-less wordmark must not inherit that offset.

The mark is a rounded square, so it is scaled uniformly and centred in a
square box; only the height matches the reference.

Outputs (into ``dashboard/public``):

* ``logo_horizontal_dark.svg`` / ``.png``  - dark ink, for light backgrounds
* ``logo_horizontal_white.svg`` / ``.png`` - white ink, for dark backgrounds

Usage::

    uv run python dashboard/scripts/gen-brand-logo.py
    uv run python dashboard/scripts/gen-brand-logo.py --weight 600 --cap-height 84
"""

from __future__ import annotations

import argparse
import base64
import io
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DIR = REPO_ROOT / "dashboard" / "public"
# The vector mark is the same artwork as the raster mascot; embedding the
# vector keeps the SVG small (a base64 PNG costs ~140 KB).
MARK_SVG_SRC = PUBLIC_DIR / "logo_mark.svg"
MASCOT_SRC = PUBLIC_DIR / "octop-mascot-tasks.png"

FONT_CSS_URL = "https://fonts.googleapis.com/css2?family=Poppins:wght={weight};display=swap"
FONT_FILE = "Poppins-{name}.ttf"

# Poppins weights (SIL OFL). The reference Octop wordmark is a *bold* geometric
# face, not an ultra-black one: measured stroke/cap = 0.19 against Poppins
# Bold's 0.35 and ExtraBold's ~0.9 (whose 'O' counter nearly closes). So the
# default weight here is 700, paired with a proportionally smaller cap height.
FONT_WEIGHTS = {
    600: "SemiBold",
    700: "Bold",
    800: "ExtraBold",
}
DEFAULT_WEIGHT = 700

# --- reference geometry, in units of the 753x289 artboard ---------------------
ARTBOARD_H = 289
MARK_H = 207          # mark height
CAP_H = 80            # wordmark cap height (scaled down: "MEA Cowork" has
                      # twice the characters of "Octop", so keeping the
                      # reference cap of 105u would make the wordmark 3.2x the
                      # reference's width and visually overpower the mark)
GAP = 26              # mark -> wordmark gap

WORDMARK = "MEA Cowork"

INK_DARK = "#0B1220"
INK_WHITE = "#FFFFFF"

# Final raster width; the shipped PNGs render at up to 4800px wide.
PNG_WIDTH = 4800

# Embedded mark resolution inside the SVG.
MARK_EMBED_PX = 1200


@dataclass(frozen=True)
class Glyph:
    name: str
    path: str
    advance: float
    x_min: float   # ink left edge, in font units
    x_max: float   # ink right edge, in font units


def _ensure_font(cache_dir: Path, weight: int = DEFAULT_WEIGHT) -> Path:
    """Fetch the requested Poppins weight (SIL OFL) into ``cache_dir`` if absent."""
    if weight not in FONT_WEIGHTS:
        raise ValueError(f"unsupported weight {weight}; pick one of {sorted(FONT_WEIGHTS)}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / FONT_FILE.format(name=FONT_WEIGHTS[weight])
    if target.exists():
        return target

    ua = "Mozilla/5.0 (Windows NT 6.1)"
    css = subprocess.run(
        [
            "curl",
            "-sS",
            "-m",
            "20",
            "-A",
            ua,
            FONT_CSS_URL.format(weight=weight),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    url_match = re.search(r"url\((https://[^)]+\.ttf)\)", css)
    if url_match is None:
        raise RuntimeError(
            f"could not locate a TTF url for Poppins {weight} in:\n{css}"
        )
    url = url_match.group(1)

    subprocess.run(
        ["curl", "-sS", "-m", "40", "-A", ua, "-o", str(target), url],
        check=True,
    )
    return target


def _kern_pairs(tt) -> dict[tuple[str, str], int]:
    """Extract x-advance kerning pairs from the GPOS table, if present."""
    if "GPOS" not in tt:
        return {}
    pairs: dict[tuple[str, str], int] = {}
    gpos = tt["GPOS"].table  # force lazy decompilation of the sub-tables
    if gpos is None or not hasattr(gpos, "LookupList"):
        return {}

    def walk(table, lookup_type):
        for lookup in table.LookupList.Lookup:
            if lookup.LookupType != lookup_type:
                continue
            for sub in lookup.SubTable:
                if getattr(sub, "Format", None) != 1:
                    continue
                coverage = sub.Coverage.glyphs
                for i, first in enumerate(coverage):
                    record = sub.PairSet[i].PairValueRecord
                    for j, second in enumerate(coverage):
                        value = record[j].Value1
                        if value.XAdvance:
                            pairs[(first, second)] = int(value.XAdvance)

    walk(gpos, 2)  # PairPos
    return pairs


def _build_wordmark(font_path: Path) -> tuple[list[Glyph], float, float, dict]:
    """Return (glyphs, total advance including kerning, cap height, kern pairs)."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.ttLib import TTFont

    tt = TTFont(str(font_path))
    cap_height = tt["OS/2"].sCapHeight or round(tt["head"].unitsPerEm * 0.7)
    cmap = tt.getBestCmap()
    hmtx = tt["hmtx"]
    glyph_set = tt.getGlyphSet()
    kern = _kern_pairs(tt)

    def outline(glyph_name: str) -> tuple[str, float, float]:
        """Return (path data, ink x_min, ink x_max) in font units."""
        from fontTools.pens.boundsPen import BoundsPen

        pen = SVGPathPen(glyph_set)
        glyph_set[glyph_name].draw(pen)
        bounds_pen = BoundsPen(glyph_set)
        glyph_set[glyph_name].draw(bounds_pen)
        bounds = bounds_pen.bounds
        if bounds is None:  # whitespace-only glyph (e.g. the space)
            return pen.getCommands(), 0.0, 0.0
        return pen.getCommands(), float(bounds[0]), float(bounds[2])

    glyphs: list[Glyph] = []
    total = 0.0
    previous: str | None = None
    for char in WORDMARK:
        glyph_name = cmap.get(ord(char))
        if glyph_name is None:
            raise RuntimeError(f"font lacks a glyph for {char!r}")
        advance = hmtx[glyph_name][0]
        if previous is not None:
            total += kern.get((previous, glyph_name), 0)
        path, x_min, x_max = outline(glyph_name)
        glyphs.append(
            Glyph(name=glyph_name, path=path, advance=advance, x_min=x_min, x_max=x_max)
        )
        total += advance
        previous = glyph_name
    return glyphs, total, cap_height, kern


def _mark_inner_svg() -> str:
    """Return the inner markup of ``logo_mark.svg`` (defs + body).

    The mark ships as a standalone 2000x2000 SVG. We inline its markup into
    the lockup so the result stays vector; the base64 PNG alternative would
    add ~140 KB per file.
    """
    raw = MARK_SVG_SRC.read_text(encoding="utf-8")
    inner = re.search(r"<svg[^>]*>(.*)</svg>", raw, re.DOTALL)
    if inner is None:
        raise RuntimeError(f"could not parse {MARK_SVG_SRC}")
    return inner.group(1)


def _mascot_data_uri(size: int) -> str:
    """Embed the mascot PNG (trimmed + scaled) as a base64 data URI.

    Fallback used only when the vector mark is unavailable.
    """
    with Image.open(MASCOT_SRC) as img:
        img = img.convert("RGBA")
        bbox = img.getchannel("A").getbbox()
        if bbox:
            img = img.crop(bbox)
        img = img.resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _compose_svg(ink: str, font_path: Path) -> tuple[str, float]:
    """Build the lockup SVG. Returns (svg, total width in artboard units)."""
    glyphs, total_advance, cap_height, kern = _build_wordmark(font_path)

    scale = CAP_H / cap_height

    # Walk the glyphs to find the ink extents of the wordmark.
    pen = 0.0
    ink_start = None
    ink_end = None
    previous: str | None = None
    for glyph in glyphs:
        if previous is not None:
            pen += kern.get((previous, glyph.name), 0)
        if glyph.name != "space":
            lo = pen + glyph.x_min
            hi = pen + glyph.x_max
            ink_start = lo if ink_start is None else min(ink_start, lo)
            ink_end = hi if ink_end is None else max(ink_end, hi)
        pen += glyph.advance
        previous = glyph.name

    # The reference gap is measured ink-to-ink, so shift the wordmark so the
    # first glyph's ink starts exactly GAP units after the mark's right edge.
    text_x = MARK_H + GAP - ink_start * scale
    ink_width = (ink_end - ink_start) * scale
    total_w = text_x + ink_width

    mark_y = (ARTBOARD_H - MARK_H) / 2
    mark_centre = mark_y + MARK_H / 2

    # The reference wordmark is vertically built by aligning the CAP BAND
    # (the uppercase mass) with the mark's centre -- measured offset is only
    # -2u. The reference's full ink block sits ~16u lower simply because the
    # 'p' descender extends below the baseline; that is a geometric
    # by-product of the word, not a design intent.
    #
    # "MEA Cowork" has no descender, so copying the 16u drop would push the
    # wordmark visibly below the mark. Align the cap band instead.
    mark_centre = mark_y + MARK_H / 2
    cap_block_centre = mark_centre
    baseline = cap_block_centre + CAP_H / 2

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_w:.2f} {ARTBOARD_H}" '
        f'width="{total_w:.2f}" height="{ARTBOARD_H}">',
    ]
    # --- mark (vector, inlined and scaled into the mark box) ---------------
    parts.append(
        f'<g transform="translate(0 {mark_y:.2f}) scale({MARK_H / 2000:.6f})">'
    )
    parts.append(_mark_inner_svg())
    parts.append("</g>")
    parts.append(
        # --- wordmark --------------------------------------------------------
        f'<g transform="translate({text_x:.2f} {baseline:.2f})" fill="{ink}">'
    )

    pen_x = 0.0
    previous = None
    for glyph in glyphs:
        if previous is not None:
            pen_x += kern.get((previous, glyph.name), 0)
        if glyph.name != "space":
            # `translate` is not affected by the trailing `scale`, so the
            # pen offset must be pre-multiplied into artboard units.
            parts.append(
                f'<path transform="translate({pen_x * scale:.2f} 0) '
                f'scale({scale:.6f} {-scale:.6f})" d="{glyph.path}"/>'
            )
        pen_x += glyph.advance
        previous = glyph.name
    parts.append("</g>")
    parts.append("</svg>")
    return "\n".join(parts), total_w


def _chromium_executable() -> str | None:
    """Locate a Chromium binary Playwright can drive.

    Playwright's bundled ``chromium_headless_shell`` is version-locked, but the
    machine may already carry a Chrome for Testing build from another
    Playwright release. Fall back to that rather than forcing a download.
    """
    import glob
    import os

    patterns = [
        os.path.expanduser(
            "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/"
            "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        ),
        os.path.expanduser(
            "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/"
            "Chromium.app/Contents/MacOS/Chromium"
        ),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for pattern in patterns:
        for candidate in sorted(glob.glob(pattern), reverse=True):
            if os.path.exists(candidate):
                return candidate
    return None


def _svg_to_png(svg: str, out_path: Path, width: int, height: int) -> None:
    """Rasterise the SVG with headless Chromium for pixel-accurate output."""
    from playwright.sync_api import sync_playwright

    svg_uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>html,body{margin:0;padding:0;background:transparent;}</style>"
        f"</head><body><img id='logo' src='{svg_uri}' "
        f"width='{width}' height='{height}'></body></html>"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as handle:
        handle.write(html)
        html_path = handle.name

    executable = _chromium_executable()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=executable)
            page = browser.new_page(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )
            page.goto(f"file://{html_path}")
            page.wait_for_timeout(500)
            page.screenshot(path=str(out_path), omit_background=True)
            browser.close()
    finally:
        Path(html_path).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate the MEA Cowork lockup.")
    parser.add_argument(
        "--font-cache", type=Path, default=REPO_ROOT / ".cache" / "fonts"
    )
    parser.add_argument("--only", choices=["dark", "white", "both"], default="both")
    parser.add_argument(
        "--weight",
        type=int,
        choices=sorted(FONT_WEIGHTS),
        default=DEFAULT_WEIGHT,
        help="Poppins weight for the wordmark (default: %(default)s)",
    )
    parser.add_argument(
        "--cap-height",
        type=int,
        default=None,
        help="wordmark cap height in artboard units (default: %(default)s)",
    )
    args = parser.parse_args()

    if not MASCOT_SRC.exists():
        raise SystemExit(f"missing mascot source: {MASCOT_SRC}")

    global CAP_H
    if args.cap_height is not None:
        CAP_H = args.cap_height

    font_path = _ensure_font(args.font_cache, args.weight)
    variants = ["dark", "white"] if args.only == "both" else [args.only]
    print(
        f"wordmark: Poppins {args.weight} ({FONT_WEIGHTS[args.weight]}), "
        f"cap {CAP_H}u, mark {MARK_H}u"
    )

    for variant in variants:
        ink = INK_DARK if variant == "dark" else INK_WHITE
        svg, total_w = _compose_svg(ink, font_path)

        svg_path = PUBLIC_DIR / f"logo_horizontal_{variant}.svg"
        svg_path.write_text(svg, encoding="utf-8")
        print(f"wrote {svg_path.relative_to(REPO_ROOT)}  ({total_w:.1f}u x {ARTBOARD_H}u)")

        png_width = PNG_WIDTH
        png_height = int(round(png_width * ARTBOARD_H / total_w))
        png_path = PUBLIC_DIR / f"logo_horizontal_{variant}.png"
        _svg_to_png(svg, png_path, png_width, png_height)
        print(f"wrote {png_path.relative_to(REPO_ROOT)}  ({png_width}x{png_height})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
