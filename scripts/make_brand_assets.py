#!/usr/bin/env python3
"""Generate every binary brand asset for FINMODELS TERMINAL from one source of truth.

WHY THIS SCRIPT EXISTS
----------------------
The brand mark used to live only as an inline `data:` URI inside public/index.html:
a <text> element rendering the glyph U+0192 (LATIN SMALL LETTER F WITH HOOK, the
"florin" / italic-math f) in `font-family='monospace'`. That was fragile for two
reasons worth recording, because both are easy to reintroduce:

  1. `font-family='monospace'` resolves to a *different* typeface on every machine,
     and in several contexts (SVG rasterised by a headless scraper, an OS icon
     cache, an RSS reader, a Slack unfurler) resolves to nothing at all -- the mark
     silently disappears. A shipped brand asset must not depend on a font being
     installed on the viewer's machine.
  2. A single inline SVG cannot serve Safari pinned tabs, iOS home-screen icons,
     Android/PWA manifest icons or social-card unfurlers. Those consumers want
     real .ico / .png bytes at specific pixel sizes, and iOS in particular
     *discards the alpha channel* and composites onto white -- a transparent icon
     that looks fine in Chrome looks broken on an iPhone home screen.

So we bake the mark down once, here, into six static files. The shape itself is
still derived from the real U+0192 glyph (Menlo Bold Italic, which is the closest
match to the mark the site has always shipped), but it is captured at build time:

  * the PNG/ICO rasters are drawn from the font directly, and
  * public/favicon.svg embeds a *traced vector outline* of that same glyph as an
    explicit <path>, so the SVG is self-contained and renders identically on a
    machine with no monospace font at all.

Tracing rather than hand-drawing a lookalike is deliberate: it guarantees the
.svg and the .png/.ico are pixel-for-pixel the same silhouette, so a browser that
prefers favicon.svg and one that falls back to favicon.ico show the same logo.

This script is deterministic -- no randomness, no timestamps -- so re-running it
on an unchanged machine reproduces byte-identical output and produces no spurious
diffs. It is safe to re-run at any time.

OUTPUT (all paths relative to the repo root)
    public/favicon.svg         vector mark, self-contained, no font dependency
    public/favicon.ico         multi-resolution 16 / 32 / 48 px
    public/apple-touch-icon.png 180x180, opaque (iOS ignores alpha)
    public/icon-192.png        192x192, opaque  (PWA manifest)
    public/icon-512.png        512x512, opaque  (PWA manifest / splash)
    public/og.png              1200x630 social card (Slack/X/LinkedIn/iMessage)

USAGE
    python3 scripts/make_brand_assets.py
"""

from __future__ import annotations

import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------
# Palette. These are copied from public/assets/terminal.css rather than
# re-invented, so the icons and the social card cannot drift away from the live
# UI. If a colour changes there, change it here and re-run.
# --------------------------------------------------------------------------
BG = (0x05, 0x06, 0x08)        # --bg        near-black terminal ground
PANEL = (0x0B, 0x0E, 0x12)     # --panel     raised panel block
BORDER = (0x1D, 0x25, 0x30)    # --border    hairline rules
AMBER = (0xFF, 0xB0, 0x00)     # --amber     primary brand
CYAN = (0x53, 0xC9, 0xE0)      # --cyan
GREEN = (0x3D, 0xD6, 0x8C)     # --green
TEXT = (0xC9, 0xD4, 0xE0)      # --text
DIM = (0x5C, 0x6B, 0x7D)       # --dim

# A grid line at full --border weight reads as a checkerboard at OG-card scale,
# which fights the type. Mixed most of the way back toward the ground it is
# felt rather than seen -- but note the grid is drawn on --panel, not --bg, so
# the mix has to clear --panel's own lift before it becomes visible at all.
GRID = tuple(round(b + (f - b) * 0.62) for b, f in zip(BG, BORDER))

MARK_CHAR = "ƒ"  # LATIN SMALL LETTER F WITH HOOK -- the site's brand glyph

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC = os.path.join(REPO_ROOT, "public")

# Supersampling factor for every raster. Pillow has no antialiased vector
# renderer, so we draw big and box/Lanczos down; 4x is the point where further
# supersampling stops changing the 16px favicon.
SS = 4


# --------------------------------------------------------------------------
# Font discovery
# --------------------------------------------------------------------------
# (path, ttc_index, human name). Ordered by preference. Menlo is macOS's
# Plex-Mono-adjacent default and its U+0192 is a clean hooked f; the rest are
# fallbacks so this script still produces something usable on a machine with a
# different font set.
_MONO_CANDIDATES = [
    ("/System/Library/Fonts/Menlo.ttc", {"regular": 0, "bold": 1, "bolditalic": 3}),
    ("/System/Library/Fonts/SFNSMono.ttf", {"regular": 0, "bold": 0, "bolditalic": 0}),
    ("/System/Library/Fonts/Monaco.ttf", {"regular": 0, "bold": 0, "bolditalic": 0}),
    ("/System/Library/Fonts/Supplemental/PTMono.ttc", {"regular": 0, "bold": 0, "bolditalic": 0}),
    ("/System/Library/Fonts/Supplemental/Andale Mono.ttf", {"regular": 0, "bold": 0, "bolditalic": 0}),
    ("/System/Library/Fonts/Supplemental/Courier New.ttf", {"regular": 0, "bold": 0, "bolditalic": 0}),
    ("/Library/Fonts/Arial Unicode.ttf", {"regular": 0, "bold": 0, "bolditalic": 0}),
]


def find_font_family():
    """Return (path, {style: ttc_index}) for the first usable monospace family.

    Returns (None, None) if nothing loads. Callers must handle that: falling
    back to PIL's default bitmap font keeps the script from crashing, but the
    output looks amateurish and the caller should say so loudly.
    """
    for path, styles in _MONO_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            probe = ImageFont.truetype(path, 32, index=styles["regular"])
            # Reject a font that cannot draw the brand glyph -- an empty mask
            # here would silently ship a blank favicon.
            if probe.getmask(MARK_CHAR).getbbox() is None:
                continue
            return path, styles
        except Exception:
            continue
    return None, None


FONT_PATH, FONT_STYLES = find_font_family()
HAVE_TTF = FONT_PATH is not None


def font(style: str, size: int):
    """Load one style at one size, or PIL's default bitmap font as last resort."""
    if not HAVE_TTF:
        return ImageFont.load_default()
    return ImageFont.truetype(FONT_PATH, size, index=FONT_STYLES[style])


# --------------------------------------------------------------------------
# The mark, as pixels
# --------------------------------------------------------------------------
def mark_mask(target_ink_height: int, style: str = "bolditalic") -> Image.Image:
    """Render U+0192 as an 8-bit alpha mask cropped tight to its ink.

    Sized by *ink* height rather than font size on purpose: font size includes
    ascent/descent padding that varies by typeface, so laying out against it
    would make the mark jump around if the font fallback ever changed.
    """
    if not HAVE_TTF:
        # Bitmap fallback: no scalable glyph available, so draw at default size
        # and scale the raster. Ugly, but non-fatal.
        f = ImageFont.load_default()
        probe = Image.new("L", (200, 200), 0)
        ImageDraw.Draw(probe).text((20, 20), MARK_CHAR, font=f, fill=255)
        box = probe.getbbox() or (0, 0, 1, 1)
        cut = probe.crop(box)
        scale = target_ink_height / max(1, cut.height)
        return cut.resize(
            (max(1, round(cut.width * scale)), target_ink_height), Image.LANCZOS
        )

    # Two-pass sizing: measure the ink height at a probe size, then scale the
    # font size by the ratio. One correction pass lands within a pixel.
    size = max(8, target_ink_height * 2)
    for _ in range(4):
        f = ImageFont.truetype(FONT_PATH, int(size), index=FONT_STYLES[style])
        mask = f.getmask(MARK_CHAR, mode="L")
        box = mask.getbbox()
        if not box:
            break
        ink_h = box[3] - box[1]
        if abs(ink_h - target_ink_height) <= 1:
            break
        size = size * target_ink_height / max(1, ink_h)

    f = ImageFont.truetype(FONT_PATH, int(round(size)), index=FONT_STYLES[style])
    # Draw into a generous canvas then crop to ink, so the italic lean and the
    # descender tail are never clipped by the layout box.
    pad = int(size)
    canvas = Image.new("L", (int(size * 3), int(size * 3)), 0)
    ImageDraw.Draw(canvas).text((pad, pad), MARK_CHAR, font=f, fill=255)
    box = canvas.getbbox()
    return canvas.crop(box) if box else canvas


def square_icon(size: int) -> Image.Image:
    """One opaque square app icon: amber mark centred on the #050608 ground.

    Opaque edge to edge, with no rounded corners, because iOS discards the alpha
    channel of apple-touch-icon (compositing anything transparent onto white)
    and applies its own corner mask. Supplying our own rounding would produce a
    double-rounded, inset-looking icon on the home screen.
    """
    S = size * SS
    img = Image.new("RGB", (S, S), BG)

    # 0.66 of the square is the largest the hooked f can go before the ascender
    # hook and descender tail start feeling cramped against the edges. It is
    # also large enough to stay legible when the .ico is shown at 16px.
    mask = mark_mask(int(S * 0.66))
    # Optically centre on the ink box rather than the advance box -- the italic
    # f otherwise sits visibly left of centre.
    x = (S - mask.width) // 2
    y = (S - mask.height) // 2
    img.paste(Image.new("RGB", mask.size, AMBER), (x, y), mask)

    return img.resize((size, size), Image.LANCZOS)


# --------------------------------------------------------------------------
# The mark, as vector -- marching squares + Ramer-Douglas-Peucker
# --------------------------------------------------------------------------
# We trace the *rendered* glyph rather than reading the font's own outline
# because fontTools is not a dependency of this repo and we do not want to add
# one for a build step that runs a handful of times a year. Tracing a 1024px
# antialiased render at the 50% coverage isoline reproduces the curve to well
# under a tenth of a pixel at any size a favicon is ever displayed.

def _marching_squares(grid, w, h, level=127.5):
    """Return closed contours [[(x, y), ...], ...] at `level` over a grayscale grid.

    Subpixel-accurate: segment endpoints are linearly interpolated along each
    cell edge, which is what keeps the traced curve smooth instead of stepped.
    """
    def val(x, y):
        return grid[y * w + x]

    def interp(x0, y0, x1, y1):
        a, b = val(x0, y0), val(x1, y1)
        t = 0.5 if a == b else (level - a) / (b - a)
        t = min(1.0, max(0.0, t))
        return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)

    segs = []
    for y in range(h - 1):
        for x in range(w - 1):
            tl, tr, br, bl = val(x, y), val(x + 1, y), val(x + 1, y + 1), val(x, y + 1)
            case = ((tl > level) << 3) | ((tr > level) << 2) | ((br > level) << 1) | (bl > level)
            if case in (0, 15):
                continue
            top = lambda: interp(x, y, x + 1, y)
            right = lambda: interp(x + 1, y, x + 1, y + 1)
            bottom = lambda: interp(x, y + 1, x + 1, y + 1)
            left = lambda: interp(x, y, x, y + 1)
            if case in (1, 14):
                segs.append((left(), bottom()))
            elif case in (2, 13):
                segs.append((bottom(), right()))
            elif case in (3, 12):
                segs.append((left(), right()))
            elif case in (4, 11):
                segs.append((top(), right()))
            elif case in (6, 9):
                segs.append((top(), bottom()))
            elif case in (7, 8):
                segs.append((left(), top()))
            elif case == 5 or case == 10:
                # Saddle: resolve with the cell-centre average so the two
                # branches are separated the same way the rasteriser did.
                centre = (tl + tr + br + bl) / 4.0
                if (case == 5) == (centre > level):
                    segs.append((left(), top()))
                    segs.append((bottom(), right()))
                else:
                    segs.append((left(), bottom()))
                    segs.append((top(), right()))

    # Stitch segments end-to-end into closed loops. Endpoints are compared on a
    # quantised key because they are floats produced by the same interpolation
    # on both sides of a shared cell edge.
    def key(p):
        return (round(p[0], 4), round(p[1], 4))

    # Stitch segments into closed loops.
    #
    # The walk is deliberately *undirected*: the marching-squares case table
    # above emits each segment with an arbitrary orientation (case 1 and case
    # 14 describe the same edge crossing with opposite inside/outside), so
    # chaining strictly end-to-start silently breaks every loop into two-point
    # fragments. Since the path is emitted with fill-rule="evenodd", winding
    # direction carries no meaning for us and we can ignore it entirely.
    #
    # Endpoints are indexed in a dict so the walk is O(n): at trace resolution
    # the glyph perimeter is several thousand segments and rescanning the list
    # per hop is unusably slow.
    adj = {}
    for i, (a, b) in enumerate(segs):
        adj.setdefault(key(a), []).append(i)
        adj.setdefault(key(b), []).append(i)

    used = [False] * len(segs)
    contours = []
    for i0 in range(len(segs)):
        if used[i0]:
            continue
        used[i0] = True
        a0, b0 = segs[i0]
        contour = [a0, b0]
        cur = b0
        while True:
            nxt = None
            for idx in adj.get(key(cur), ()):
                if not used[idx]:
                    nxt = idx
                    break
            if nxt is None:
                break
            used[nxt] = True
            a, b = segs[nxt]
            cur = b if key(a) == key(cur) else a
            contour.append(cur)
            if key(cur) == key(a0):
                break
        if len(contour) > 8:
            contours.append(contour)
    return contours


def _rdp(points, epsilon):
    """Ramer-Douglas-Peucker polyline simplification (iterative, no recursion limit)."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = points[i]
        bx, by = points[j]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, best_d = -1, 0.0
        for k in range(i + 1, j):
            px, py = points[k]
            if norm == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best_d:
                best, best_d = k, d
        if best_d > epsilon and best > 0:
            keep[best] = True
            stack.append((i, best))
            stack.append((best, j))
    return [p for p, k in zip(points, keep) if k]


def mark_svg_path(view: float = 100.0, inset: float = 0.66) -> str:
    """Trace the brand glyph into SVG path data inside a `view`-unit square.

    `inset` is the fraction of the square the glyph ink occupies -- kept equal to
    square_icon()'s 0.66 so the vector favicon and the raster favicons are the
    same size relative to their ground.
    """
    RES = 1024  # trace resolution; error scales as ~1/RES and this is plenty
    mask = mark_mask(RES).convert("L")
    # One-pixel transparent border guarantees every contour closes inside the
    # grid instead of running off an edge.
    padded = Image.new("L", (mask.width + 2, mask.height + 2), 0)
    padded.paste(mask, (1, 1))

    w, h = padded.size
    # tobytes() rather than getdata(): identical flat row-major samples for an
    # "L" image, but getdata() is deprecated as of Pillow 12 and would emit a
    # warning on every build.
    grid = list(padded.tobytes())
    contours = _marching_squares(grid, w, h)
    if not contours:
        raise RuntimeError("glyph trace produced no contours")

    # Scale so the taller dimension of the ink box fills `inset` of the view,
    # then centre. The mark is much taller than wide, so height governs.
    scale = (view * inset) / max(w - 2, h - 2)
    off_x = (view - (w - 2) * scale) / 2.0 - scale
    off_y = (view - (h - 2) * scale) / 2.0 - scale

    # Simplify in *output* units so the tolerance means something visually:
    # 0.03 of a 100-unit box is 0.03% of the rendered size -- a hundredth of a
    # pixel at favicon scale, and still under half a pixel if something renders
    # the mark at 1024px. That lands around 1.5 KB of path data, comfortably
    # inside the 4 KB budget we want for a file fetched on every page load.
    eps_px = 0.03 / scale

    parts = []
    for contour in contours:
        simple = _rdp(contour, eps_px)
        if len(simple) < 3:
            continue
        pts = [(p[0] * scale + off_x, p[1] * scale + off_y) for p in simple]
        d = "M" + " ".join(f"{x:.2f},{y:.2f}" for x, y in pts) + "Z"
        parts.append(d)
    return "".join(parts)


def build_favicon_svg(path: str) -> None:
    d = mark_svg_path()
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='100' height='100' "
        "viewBox='0 0 100 100' role='img' aria-label='FINMODELS TERMINAL'>"
        "<title>FINMODELS TERMINAL</title>"
        f"<rect width='100' height='100' fill='#050608'/>"
        f"<path fill='#ffb000' fill-rule='evenodd' d='{d}'/>"
        "</svg>\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(svg)


# --------------------------------------------------------------------------
# Typography helpers for the social card
# --------------------------------------------------------------------------
# Terminal type is set with wide tracking, which Pillow cannot do -- it has no
# letter-spacing parameter. We draw character by character instead. The font is
# monospace so every advance is identical, which makes the arithmetic exact.

def _advance(f, tracking: float) -> float:
    return f.getlength("M") + tracking


def tracked_width(text: str, f, tracking: float) -> float:
    if not text:
        return 0.0
    adv = _advance(f, tracking)
    # The final glyph contributes its advance but not its trailing tracking.
    return (len(text) - 1) * adv + f.getlength("M")


def fit_tracked(text: str, style: str, max_width: float, start_size: int,
                tracking_ratio: float):
    """Largest size <= start_size at which `text` fits `max_width`.

    Width is linear in size (tracking is expressed as a fraction of the size),
    so we solve directly and then step down defensively. Auto-fitting rather
    than hard-coding sizes is what guarantees the card can never ship with
    clipped type if the copy is edited later.
    """
    size = start_size
    while size > 8:
        f = font(style, size)
        if tracked_width(text, f, size * tracking_ratio) <= max_width:
            return f, size * tracking_ratio, size
        size -= 1
    f = font(style, 8)
    return f, 8 * tracking_ratio, 8


def draw_tracked(draw, xy_ink_topleft, text, f, tracking, fill):
    """Draw letter-spaced text with its *ink* top-left at the given point.

    Positioning by ink rather than by baseline keeps the vertical rhythm of the
    card independent of the font's ascent, which differs between fallbacks.
    """
    x, y_top = xy_ink_topleft
    ref = draw.textbbox((0, 0), text, font=f)
    y = y_top - ref[1]
    adv = _advance(f, tracking)
    for i, ch in enumerate(text):
        draw.text((x + i * adv, y), ch, font=f, fill=fill)


def tracked_ink_height(draw, text, f) -> int:
    box = draw.textbbox((0, 0), text, font=f)
    return box[3] - box[1]


# --------------------------------------------------------------------------
# The social card
# --------------------------------------------------------------------------
OG_W, OG_H = 1200, 630
OG_SAFE = 60          # hard requirement: no text ink closer than this to an edge
OG_TEXT_LEFT = 104    # where the type column actually starts (well inside safe)
OG_TEXT_RIGHT = 1096

HEADLINE = "FINMODELS TERMINAL"
SUBTITLE = "12 QUANT MODELS · CPYTHON → WEBASSEMBLY · IN YOUR BROWSER"
MNEMONICS = "DCF · BSM · HESTON · MPT · VAR · FAMA-FRENCH · REVERSE-DCF"


def build_og(path: str) -> dict:
    img = Image.new("RGB", (OG_W, OG_H), BG)
    d = ImageDraw.Draw(img)

    # --- ground furniture -------------------------------------------------
    # A single panel inset from the edge reads as a terminal window without
    # needing chrome, title bars or fake UI. Unfurlers on some platforms crop a
    # few pixels off the edges, so nothing meaningful lives outside it.
    p0, p1 = 40, 40
    p2, p3 = OG_W - 41, OG_H - 41
    d.rectangle([p0, p1, p2, p3], fill=PANEL, outline=BORDER, width=1)

    # Graph-paper grid, clipped to the panel. 48px pitch is coarse enough to
    # stay out of the type's way at the ~600px wide thumbnail most feeds show.
    for gx in range(p0 + 48, p2, 48):
        d.line([(gx, p1 + 1), (gx, p3 - 1)], fill=GRID, width=1)
    for gy in range(p1 + 48, p3, 48):
        d.line([(p0 + 1, gy), (p2 - 1, gy)], fill=GRID, width=1)

    # --- type, measured before it is drawn --------------------------------
    max_w = OG_TEXT_RIGHT - OG_TEXT_LEFT

    f_head, tr_head, sz_head = fit_tracked(HEADLINE, "bold", max_w, 82, 0.11)
    f_sub, tr_sub, sz_sub = fit_tracked(SUBTITLE, "regular", max_w, 30, 0.055)
    f_mnem, tr_mnem, sz_mnem = fit_tracked(MNEMONICS, "regular", max_w, 23, 0.055)

    mark_h = 58
    rule_h = 5
    h_head = tracked_ink_height(d, HEADLINE, f_head)
    h_sub = tracked_ink_height(d, SUBTITLE, f_sub)
    h_mnem = tracked_ink_height(d, MNEMONICS, f_mnem)

    # Gaps below each block. Tuned so the amber rule reads as attached to the
    # headline (small gap above, larger below) rather than floating.
    gaps = [78, 46, 52, 60]
    stack = [mark_h, h_head, rule_h, h_sub, h_mnem]
    total = sum(stack) + sum(gaps)

    # Centre the whole stack in the panel so editing the copy cannot push the
    # layout off balance.
    y = p1 + ((p3 - p1) - total) / 2.0

    # Draw onto a transparent overlay so we can measure the exact ink bounds of
    # everything that matters and assert the safe margin before compositing.
    overlay = Image.new("RGBA", (OG_W, OG_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    mask = mark_mask(mark_h)
    overlay.paste(Image.new("RGBA", mask.size, AMBER + (255,)),
                  (OG_TEXT_LEFT, int(round(y))), mask)
    y += stack[0] + gaps[0]

    draw_tracked(od, (OG_TEXT_LEFT, round(y)), HEADLINE, f_head, tr_head, AMBER + (255,))
    y += stack[1] + gaps[1]

    od.rectangle([OG_TEXT_LEFT, round(y), OG_TEXT_LEFT + 216, round(y) + rule_h - 1],
                 fill=AMBER + (255,))
    y += stack[2] + gaps[2]

    draw_tracked(od, (OG_TEXT_LEFT, round(y)), SUBTITLE, f_sub, tr_sub, TEXT + (255,))
    y += stack[3] + gaps[3]

    draw_tracked(od, (OG_TEXT_LEFT, round(y)), MNEMONICS, f_mnem, tr_mnem, DIM + (255,))

    ink = overlay.getbbox()
    if ink is None:
        raise RuntimeError("og.png: nothing was drawn")
    if (ink[0] < OG_SAFE or ink[1] < OG_SAFE
            or ink[2] > OG_W - OG_SAFE or ink[3] > OG_H - OG_SAFE):
        raise RuntimeError(f"og.png: ink {ink} breaks the {OG_SAFE}px safe margin")

    img.paste(overlay, (0, 0), overlay)
    img.save(path, format="PNG", optimize=True)

    return {
        "ink_bbox": ink,
        "headline_size": sz_head,
        "subtitle_size": sz_sub,
        "mnemonic_size": sz_mnem,
    }


# --------------------------------------------------------------------------
def main() -> int:
    os.makedirs(PUBLIC, exist_ok=True)
    out = lambda name: os.path.join(PUBLIC, name)

    if HAVE_TTF:
        probe = ImageFont.truetype(FONT_PATH, 24, index=FONT_STYLES["regular"])
        print(f"font: {FONT_PATH} -> {probe.getname()}")
    else:
        print("font: WARNING - no TrueType monospace font found; "
              "falling back to PIL's default bitmap font. Output will look poor.",
              file=sys.stderr)

    build_favicon_svg(out("favicon.svg"))

    # A genuine multi-resolution .ico: 16 / 32 / 48.
    #
    # Each frame is rendered independently from the glyph at its own 4x
    # supersample rather than letting the ICO writer downscale one large master.
    # The writer's internal resize is bicubic; at 16px that visibly softens the
    # hairline of the f's hook, and 16px is exactly the size that matters most
    # for a favicon. `sizes` is still passed because Pillow uses it to decide
    # which frames to emit.
    # The base image must be the *largest* frame: Pillow's ICO writer skips any
    # requested size larger than the image it was called on, so saving from the
    # 16px frame silently produces a single-resolution file.
    frames = [square_icon(s) for s in (48, 32, 16)]
    frames[0].save(out("favicon.ico"), format="ICO",
                   sizes=[(16, 16), (32, 32), (48, 48)],
                   append_images=frames[1:])

    square_icon(180).save(out("apple-touch-icon.png"), format="PNG", optimize=True)
    square_icon(192).save(out("icon-192.png"), format="PNG", optimize=True)
    square_icon(512).save(out("icon-512.png"), format="PNG", optimize=True)

    og_info = build_og(out("og.png"))

    print()
    for name in ("favicon.svg", "favicon.ico", "apple-touch-icon.png",
                 "icon-192.png", "icon-512.png", "og.png"):
        p = out(name)
        print(f"  {name:<22} {os.path.getsize(p):>8,} bytes")

    print()
    print(f"og.png ink bbox {og_info['ink_bbox']} "
          f"(safe margin {OG_SAFE}px, canvas {OG_W}x{OG_H})")
    print(f"og.png type sizes: headline {og_info['headline_size']}px, "
          f"subtitle {og_info['subtitle_size']}px, "
          f"mnemonics {og_info['mnemonic_size']}px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
