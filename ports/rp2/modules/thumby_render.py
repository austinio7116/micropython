# thumby_render — bare-metal renderer for Thumby legacy games on
# Thumby Color (ThumbyOne MPY slot).
#
# Replaces the Sprite2DNode / scene-graph round-trip the upstream
# `ssd1306` shim used. Each frame:
#
#   game writes 1bpp (and optionally a parallel 1bpp shading plane)
#       │
#       ▼
#   thumby_render.present_*(buf [, shading])
#       │
#       ▼
#   viper expand directly into engine_draw.back_fb_data()
#       │
#       ▼
#   engine.tick()  ← refreshes button state, DMAs framebuffer to LCD,
#                    swaps buffers, clears the next backbuffer to black
#
# No Sprite2DNode, no CameraNode, no depth buffer reads, no scene-graph
# traversal. Empty-graph engine.tick() is essentially "update buttons
# + DMA + clear" — about as cheap as we can get without taking the LCD
# driver out of the engine's hands.
#
# Two viper paths each for mono and gray:
#   * 1x fast path — no scaling, no LUT lookup, fully unrolled bit
#     extraction. Hot path; most users will land here at the default
#     1.0 scale.
#   * Scaled path — nearest-neighbour upscale via two precomputed
#     bytearray LUTs (one per axis, rebuilt only when scale changes).
#
# LB/RB cycle through the scale presets at runtime — original Thumby
# has no LB/RB so this hijack never collides with legacy game input.

import sys

IS_THUMBY_COLOR = "TinyCircuits Thumby Color" in sys.implementation._machine

# engine_main bootstraps the engine's hardware (LCD/PWM/buttons/RTC)
# in C. Already imported by thumbyone_launcher before the game runs;
# importing again here is a no-op but makes this module standalone.
import engine_main  # noqa: F401
import engine
import engine_draw
from engine_resources import TextureResource

# fps_limit chosen large enough to never gate display.update() — the
# game's own thumbyGraphics.setFPS frame-pacing loop in update() does
# the real timing work.
engine.fps_limit(240)

# Source dimensions are fixed by the original Thumby's SSD1306.
_SRC_W = const(72)
_SRC_H = const(40)
_SRC_PAGES = const(5)         # 40 / 8
_FB_W = const(128)
_FB_H = const(128)

# 4-entry RGB565 palette indexed by (buffer_bit | (shading_bit << 1)).
# Ordering matches the standard Timendus library:
#   0 = (b=0, s=0) → BLACK
#   1 = (b=1, s=0) → WHITE
#   2 = (b=0, s=1) → DARKGRAY
#   3 = (b=1, s=1) → LIGHTGRAY
# Greys chosen for visible contrast on the GC9107 panel; ~33 % and
# ~66 % luma in RGB565 (5/6/5).
_PAL_GRAY = bytearray(8)      # 4 entries × 2 bytes
def _pack(idx, val):
    _PAL_GRAY[idx*2]   = val & 0xff
    _PAL_GRAY[idx*2+1] = (val >> 8) & 0xff
_pack(0, 0x0000)              # BLACK
_pack(1, 0xFFFF)              # WHITE
_pack(2, 0x4208)              # DARKGRAY  (~33 %)
_pack(3, 0x8410)              # LIGHTGRAY (~66 %)
del _pack

# Mono palette: shading is treated as 0, so only entries 0 and 1 are
# ever indexed. Kept separate so present_mono can still go through the
# palette-driven scaled path without a special case.
_PAL_MONO = bytearray(8)
_PAL_MONO[0] = 0x00; _PAL_MONO[1] = 0x00     # BLACK
_PAL_MONO[2] = 0xFF; _PAL_MONO[3] = 0xFF     # WHITE
_PAL_MONO[4] = 0x00; _PAL_MONO[5] = 0x00     # would-be DARKGRAY (unused)
_PAL_MONO[6] = 0xFF; _PAL_MONO[7] = 0xFF     # would-be LIGHTGRAY (unused)

# Persistent 128×128 RGB565 shadow texture. We render INTO this rather
# than into the engine's backbuffer directly, then:
#  1. memcpy the shadow into the current back framebuffer so THIS
#     tick's DMA sends the new frame (without this, double-buffering
#     means the LCD lags one frame behind the shadow — bad for things
#     like `display.update(); time.sleep(2)` end-of-level screens that
#     update once and then block).
#  2. set_background(shadow) so the engine's normal end-of-tick clear
#     re-paints the new back from the shadow too — covers button-poll
#     induced ticks that happen between game frames (otherwise they'd
#     DMA a stale or freshly-blacked buffer, producing flicker).
# Both planes always carry the latest shadow content; no lag, no flicker.
#
# 32 KB on the heap; same memory traffic as the engine's existing
# clear-to-colour path (which we replaced).
_shadow = TextureResource(_FB_W, _FB_H, 0, 16)
_fb = _shadow.data
engine_draw.set_background(_shadow)
# Cached bytearray reference for the engine's back framebuffer. The
# engine swaps which physical buffer this points at on every tick, so
# we re-resolve ptr32(_back_fb_data) inside the viper kernel each call.
_back_fb_data = engine_draw.back_fb_data()

# --- FPS overlay ---------------------------------------------------
# Tiny diagnostic readout so users can see the actual frame rate
# without hooking up a debugger. Rendered into the top-right corner
# of the shadow's bezel area on every present_*() call. Uses the
# 3×5 ROM-mounted font for compactness.
_FPS_FONT_PATH = "/system/lib/font3x5.bin"
_FPS_FONT_W = const(3)
_FPS_FONT_H = const(5)
try:
    with open(_FPS_FONT_PATH, "rb") as _ff:
        _fps_font = bytearray(_ff.read())
    _fps_font_ok = True
except OSError:
    _fps_font = bytearray(0)
    _fps_font_ok = False
_FPS_OVERLAY_X = const(108)   # 3 digits @ 4 px = 12 px, plus margin
_FPS_OVERLAY_Y = const(1)
_FPS_OVERLAY_W = const(20)    # blank rectangle to wipe before redraw
_FPS_OVERLAY_H = const(7)

@micropython.viper
def _fps_clear(fb: ptr16, x0: int, y0: int, w: int, h: int):
    j: int = 0
    while j < h:
        row: int = (y0 + j) * 128 + x0
        i: int = 0
        while i < w:
            fb[row + i] = 0
            i += 1
        j += 1

@micropython.viper
def _fps_draw_digit(fb: ptr16, font: ptr8, digit: int, x0: int, y0: int):
    # Glyph 0 in font is ' ' (0x20). Digit '0' is 0x30 → glyph 16.
    gi: int = (digit + 16) * 3
    col: int = 0
    while col < 3:
        b: int = int(font[gi + col])
        row: int = 0
        while row < 5:
            fb_idx: int = (y0 + row) * 128 + (x0 + col)
            fb[fb_idx] = 0xFFFF if (b & (1 << row)) else 0
            row += 1
        col += 1

def _draw_fps_overlay():
    if not _fps_font_ok:
        return
    fps_f = engine.get_running_fps()
    fps = int(fps_f + 0.5)
    if fps < 0:    fps = 0
    if fps > 999:  fps = 999
    _fps_clear(_fb, _FPS_OVERLAY_X, _FPS_OVERLAY_Y,
               _FPS_OVERLAY_W, _FPS_OVERLAY_H)
    # Right-aligned three-digit render. Skip leading zeros for
    # readability.
    x = _FPS_OVERLAY_X + 16
    n = fps
    drew_any = False
    for _i in range(3):
        d = n % 10
        n //= 10
        if d != 0 or n != 0 or not drew_any:
            _fps_draw_digit(_fb, _fps_font, d, x, _FPS_OVERLAY_Y)
            drew_any = True
        x -= 4
        if n == 0 and drew_any:
            break

# Scale state — fixed for the lifetime of the slot run. The lobby
# writes a single global scale factor (a float, e.g. "1.75\n") to
# /.legacy_scale before booting the MPY slot; we read it once here and
# never change it at runtime. Defaults to 1.0 (pixel-perfect 72×40
# centered) when the file is missing or malformed. Runtime LB/RB
# scaling has been removed: the lobby is a better home for this
# preference because (a) it has UI capacity the legacy game lacks,
# (b) it persists naturally with the lobby's other settings, and
# (c) LB/RB no longer collides with legacy game input.
def _read_active_scale():
    try:
        with open("/.legacy_scale") as f:
            v = float(f.read().strip())
        if v < 0.5: v = 0.5    # below 0.5x is unreadable
        if v > 3.2: v = 3.2    # above ~3.2 either axis exceeds 128
        return v
    except (OSError, ValueError):
        return 1.0

_scale = _read_active_scale()

# LUTs map output (post-scale) coordinates → source (pre-scale)
# coordinates. Built once at import and never changed — the per-frame
# hot path pays zero divide cost.
_lut_x = bytearray(_FB_W)
_lut_y = bytearray(_FB_H)
_out_w = _SRC_W              # scaled output width
_out_h = _SRC_H              # scaled output height
_x0 = (_FB_W - _SRC_W) >> 1  # top-left corner of viewport
_y0 = (_FB_H - _SRC_H) >> 1
_is_1x = True                # whether we can use the unrolled fast path

@micropython.viper
def _shadow_clear_black(fb: ptr32):
    # 128*128*2 = 32768 bytes / 4 = 8192 words.
    i: int = 0
    while i < 8192:
        fb[i] = 0
        i += 1

@micropython.viper
def _shadow_blit_to_back(src: ptr32, dst: ptr32):
    # Word-wise copy of the 32 KB shadow to the engine's current back
    # framebuffer. Forces this-tick's DMA to send our latest frame
    # rather than the one-frame-stale double-buffered copy.
    i: int = 0
    while i < 8192:
        dst[i] = src[i]
        i += 1

def _apply_scale():
    global _out_w, _out_h, _x0, _y0, _is_1x
    ow = int(_SRC_W * _scale)
    oh = int(_SRC_H * _scale)
    if ow > _FB_W: ow = _FB_W
    if oh > _FB_H: oh = _FB_H
    _out_w = ow
    _out_h = oh
    _x0 = (_FB_W - ow) >> 1
    _y0 = (_FB_H - oh) >> 1
    _is_1x = (ow == _SRC_W) and (oh == _SRC_H)
    for i in range(ow):
        _lut_x[i] = (i * _SRC_W) // ow if ow else 0
    for j in range(oh):
        _lut_y[j] = (j * _SRC_H) // oh if oh else 0
    _shadow_clear_black(_fb)

_apply_scale()


# --- Viper render kernels -------------------------------------------
#
# All four kernels share the same source-bit unpack convention:
#   byte[(y//8)*72 + x] bit (y%8) = pixel(x, y)
# which is the SSD1306 MONO_VLSB layout the original Thumby uses.
#
# All write straight into the engine framebuffer at (x0, y0).

@micropython.viper
def _present_mono_1x(buf: ptr8, fb: ptr16, x0: int, y0: int):
    # 72×40 → 128-wide framebuffer at (x0, y0). Unrolled 8-bit column
    # write with hardcoded row offsets (* 128 stride).
    page: int = 0
    while page < 5:
        col: int = 0
        while col < 72:
            b: int = int(buf[page*72 + col])
            base: int = (y0 + page*8) * 128 + (x0 + col)
            fb[base       ] = 0xFFFF if (b & 1)   else 0
            fb[base + 128 ] = 0xFFFF if (b & 2)   else 0
            fb[base + 256 ] = 0xFFFF if (b & 4)   else 0
            fb[base + 384 ] = 0xFFFF if (b & 8)   else 0
            fb[base + 512 ] = 0xFFFF if (b & 16)  else 0
            fb[base + 640 ] = 0xFFFF if (b & 32)  else 0
            fb[base + 768 ] = 0xFFFF if (b & 64)  else 0
            fb[base + 896 ] = 0xFFFF if (b & 128) else 0
            col += 1
        page += 1

@micropython.viper
def _present_gray_1x(buf: ptr8, shade: ptr8, fb: ptr16,
                     pal: ptr16, x0: int, y0: int):
    # Same shape as mono_1x but each output pixel is looked up from a
    # 4-entry palette indexed by (buf_bit | (shade_bit << 1)).
    page: int = 0
    while page < 5:
        col: int = 0
        while col < 72:
            byte_idx: int = page*72 + col
            b: int = int(buf[byte_idx])
            s: int = int(shade[byte_idx])
            base: int = (y0 + page*8) * 128 + (x0 + col)
            fb[base       ] = pal[(b        & 1) | ((s        & 1) << 1)]
            fb[base + 128 ] = pal[((b >> 1) & 1) | (((s >> 1) & 1) << 1)]
            fb[base + 256 ] = pal[((b >> 2) & 1) | (((s >> 2) & 1) << 1)]
            fb[base + 384 ] = pal[((b >> 3) & 1) | (((s >> 3) & 1) << 1)]
            fb[base + 512 ] = pal[((b >> 4) & 1) | (((s >> 4) & 1) << 1)]
            fb[base + 640 ] = pal[((b >> 5) & 1) | (((s >> 5) & 1) << 1)]
            fb[base + 768 ] = pal[((b >> 6) & 1) | (((s >> 6) & 1) << 1)]
            fb[base + 896 ] = pal[((b >> 7) & 1) | (((s >> 7) & 1) << 1)]
            col += 1
        page += 1

@micropython.viper
def _present_mono_scaled(buf: ptr8, fb: ptr16,
                         lut_x: ptr8, lut_y: ptr8,
                         out_w: int, out_h: int, x0: int, y0: int):
    out_y: int = 0
    while out_y < out_h:
        src_y: int = int(lut_y[out_y])
        page_off: int = (src_y >> 3) * 72
        bit_y: int = src_y & 7
        fb_row: int = (y0 + out_y) * 128 + x0
        out_x: int = 0
        while out_x < out_w:
            src_x: int = int(lut_x[out_x])
            b: int = (int(buf[page_off + src_x]) >> bit_y) & 1
            fb[fb_row + out_x] = 0xFFFF if b else 0
            out_x += 1
        out_y += 1

@micropython.viper
def _present_gray_scaled(buf: ptr8, shade: ptr8, fb: ptr16, pal: ptr16,
                         lut_x: ptr8, lut_y: ptr8,
                         out_w: int, out_h: int, x0: int, y0: int):
    out_y: int = 0
    while out_y < out_h:
        src_y: int = int(lut_y[out_y])
        page_off: int = (src_y >> 3) * 72
        bit_y: int = src_y & 7
        fb_row: int = (y0 + out_y) * 128 + x0
        out_x: int = 0
        while out_x < out_w:
            src_x: int = int(lut_x[out_x])
            byte_idx: int = page_off + src_x
            b: int = (int(buf[byte_idx])   >> bit_y) & 1
            s: int = (int(shade[byte_idx]) >> bit_y) & 1
            fb[fb_row + out_x] = pal[b | (s << 1)]
            out_x += 1
        out_y += 1


# --- Public entry points --------------------------------------------

def present_mono(buffer):
    """Render a 1bpp MONO_VLSB 72×40 buffer + tick the engine.

    The viper kernels touch only the viewport region inside the
    128×128 shadow texture; the engine clears the back framebuffer to
    the shadow on every tick (set_background path), so the bezel
    stays black between game frames without us touching it.
    """
    if _is_1x:
        _present_mono_1x(buffer, _fb, _x0, _y0)
    else:
        _present_mono_scaled(buffer, _fb, _lut_x, _lut_y,
                             _out_w, _out_h, _x0, _y0)
    _draw_fps_overlay()
    _shadow_blit_to_back(_fb, _back_fb_data)
    engine.tick()


def present_gray(buffer, shading):
    """Render two parallel 1bpp planes as 4-shade grayscale + tick."""
    if _is_1x:
        _present_gray_1x(buffer, shading, _fb, _PAL_GRAY, _x0, _y0)
    else:
        _present_gray_scaled(buffer, shading, _fb, _PAL_GRAY,
                             _lut_x, _lut_y, _out_w, _out_h, _x0, _y0)
    _draw_fps_overlay()
    _shadow_blit_to_back(_fb, _back_fb_data)
    engine.tick()
