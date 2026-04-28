# thumbyGrayscale — Color-aware port of the Timendus thumby-grayscale
# library (https://github.com/Timendus/thumby-grayscale, v4.0.3).
#
# Original-Thumby implementation drives the SSD1306 from a second core
# with hand-rolled SPI timing to fake 4-shade grayscale by rapidly
# cycling between three sub-frames. None of that is needed on Thumby
# Color: the GC9107 LCD is genuinely 16-bit colour, so we present the
# (buffer, shading) bitplane pair as a true 4-shade RGB565 image via
# `thumby_render`.
#
# API surface mirrors the upstream module so games that
#     `from thumbyGrayscale import display, Sprite, buttonA, ...`
# work unchanged. The drawing primitives are copied verbatim from
# upstream — they're already viper-optimised pure Python that only
# touches `self.buffer` / `self.shading`. Only the show/init/grayscale
# toggle plumbing is replaced.
#
# ThumbyOne MPY slot pre-populates `sys.modules['thumbyGrayscale']`
# with this frozen module before exec'ing the game (see
# thumbyone_launcher), so the bundled-with-game copy in
# `<game_dir>/thumbyGrayscale.py` is shadowed and never imported. That
# means we don't need the original SPI/timing dance even on
# disk — anyone who imports thumbyGrayscale on Color gets us.
#
# Bit-plane / colour encoding (from upstream):
#     buffer=0, shading=0  →  BLACK    (constant 0)
#     buffer=1, shading=0  →  WHITE    (constant 1)
#     buffer=0, shading=1  →  DARKGRAY (constant 2)
#     buffer=1, shading=1  →  LIGHTGRAY(constant 3)
# `thumby_render._PAL_GRAY` matches.

import sys

from os import stat
from time import ticks_ms, ticks_diff, sleep_ms
from array import array

from thumbyButton import (
    buttonA, buttonB, buttonU, buttonD, buttonL, buttonR,
    inputPressed, inputJustPressed,
    dpadPressed, dpadJustPressed,
    actionPressed, actionJustPressed,
)
from thumbyAudio import audio  # imported for API compatibility


__version__ = '4.0.3-color'


IS_THUMBY_COLOR = "TinyCircuits Thumby Color" in sys.implementation._machine

# Screen-shape constants. Matches upstream so any imported game that
# pokes _BUFF_SIZE / _WIDTH / _HEIGHT directly still gets sane values.
_WIDTH         = const(72)
_HEIGHT        = const(40)
_BUFF_SIZE     = const((_HEIGHT // 8) * _WIDTH)   # 360
_BUFF_INT_SIZE = const(_BUFF_SIZE // 4)           # 90


# --- Sprite class (verbatim from upstream Timendus library) ---------
# Handles both single-bitplane sprites (legacy mono) and shaded
# sprites (`bitmapData = (buffer_bytes, shading_bytes)`). Exposed as
# `thumbyGrayscale.Sprite` so `from thumbyGrayscale import Sprite`
# resolves here.

class Sprite:
    @micropython.native
    def __init__(self, width, height, bitmapData, x=0, y=0, key=-1, mirrorX=False, mirrorY=False):
        self.width = width
        self.height = height
        self.bitmapSource = bitmapData
        self.bitmapByteCount = width * (height // 8)
        if (height % 8):
            self.bitmapByteCount += width
        self.frameCount = 1
        self.currentFrame = 0
        self._shaded = False
        self._usesFile = False
        if isinstance(bitmapData, (tuple, list)):
            if (len(bitmapData) != 2) or (type(bitmapData[0]) != type(bitmapData[1])):
                raise ValueError('bitmapData must be a bytearray, string, or tuple of two bytearrays or strings')
            self._shaded = True
            if isinstance(bitmapData[0], str):
                self._usesFile = True
                if stat(bitmapData[0])[6] != stat(bitmapData[1])[6]:
                    raise ValueError('Sprite files must match in size')
                self.bitmap = (bytearray(self.bitmapByteCount), bytearray(self.bitmapByteCount))
                self.files = (open(bitmapData[0], 'rb'), open(bitmapData[1], 'rb'))
                self.files[0].readinto(self.bitmap[0])
                self.files[1].readinto(self.bitmap[1])
                self.frameCount = stat(bitmapData[0])[6] // self.bitmapByteCount
            elif isinstance(bitmapData[0], bytearray):
                if len(bitmapData[0]) != len(bitmapData[1]):
                    raise ValueError('Sprite bitplanes must match in size')
                self.frameCount = len(bitmapData[0]) // self.bitmapByteCount
                self.bitmap = [
                    memoryview(bitmapData[0])[0:self.bitmapByteCount],
                    memoryview(bitmapData[1])[0:self.bitmapByteCount],
                ]
            else:
                raise ValueError('bitmapData must be a bytearray, string, or tuple of two bytearrays or strings')
        elif isinstance(bitmapData, str):
            self._usesFile = True
            self.bitmap = bytearray(self.bitmapByteCount)
            self.file = open(bitmapData, 'rb')
            self.file.readinto(self.bitmap)
            self.frameCount = stat(bitmapData)[6] // self.bitmapByteCount
        elif isinstance(bitmapData, bytearray):
            self.bitmap = memoryview(bitmapData)[0:self.bitmapByteCount]
            self.frameCount = len(bitmapData) // self.bitmapByteCount
        else:
            raise ValueError('bitmapData must be a bytearray, string, or tuple of two bytearrays or strings')
        self.x = x
        self.y = y
        self.key = key
        self.mirrorX = mirrorX
        self.mirrorY = mirrorY

    @micropython.native
    def getFrame(self):
        return self.currentFrame

    @micropython.native
    def setFrame(self, frame):
        if (frame >= 0 and (self.currentFrame is not frame % (self.frameCount))):
            self.currentFrame = frame % (self.frameCount)
            offset = self.bitmapByteCount * self.currentFrame
            if self._shaded:
                if self._usesFile:
                    self.files[0].seek(offset)
                    self.files[1].seek(offset)
                    self.files[0].readinto(self.bitmap[0])
                    self.files[1].readinto(self.bitmap[1])
                else:
                    self.bitmap[0] = memoryview(self.bitmapSource[0])[offset:offset + self.bitmapByteCount]
                    self.bitmap[1] = memoryview(self.bitmapSource[1])[offset:offset + self.bitmapByteCount]
            else:
                if self._usesFile:
                    self.file.seek(offset)
                    self.file.readinto(self.bitmap)
                else:
                    self.bitmap = memoryview(self.bitmapSource)[offset:offset + self.bitmapByteCount]


# --- Grayscale class -------------------------------------------------
#
# Public attributes the game sees:
#   .buffer   memoryview, 360 bytes, 1bpp MONO_VLSB primary plane
#   .shading  memoryview, 360 bytes, 1bpp MONO_VLSB shading plane
#   .width    72
#   .height   40
#   .display  self  (upstream uses self.display.buffer for the SSD1306
#                    flat buffer; we mirror that with self.display = self)
#
# Public methods:
#   fill, setPixel, getPixel
#   drawFilledRectangle, drawRectangle, drawLine
#   setFont, drawText
#   blit, drawSprite, blitWithMask, drawSpriteWithMask
#   show, show_async, update, setFPS
#   brightness, invert
#   enableGrayscale, disableGrayscale  (toggle palette mode)
#   poweron, poweroff, reset, init_display, write_cmd  (no-ops)
#   __enter__/__exit__  (calls enable/disable on entry/exit)
#   calibrate  (no-op on Color — no SSD1306 timing to calibrate)

class Grayscale:

    # Numeric colour constants. Matches upstream so games can use
    # display.BLACK, display.WHITE, etc., or pass 0..3 directly.
    BLACK     = 0
    WHITE     = 1
    DARKGRAY  = 2
    LIGHTGRAY = 3

    def __init__(self):
        # Twin-plane drawBuffer: first half = .buffer, second = .shading.
        # Memory layout matches upstream so any code that mview()s
        # drawBuffer or treats the two planes as contiguous still works.
        self.drawBuffer = bytearray(_BUFF_SIZE * 2)
        self.buffer  = memoryview(self.drawBuffer)[:_BUFF_SIZE]
        self.shading = memoryview(self.drawBuffer)[_BUFF_SIZE:]

        self.display = self            # SSD1306-shape compatibility
        self.pages   = _HEIGHT // 8
        self.width   = _WIDTH
        self.height  = _HEIGHT
        self.max_x   = _WIDTH - 1
        self.max_y   = _HEIGHT - 1

        # Frame-pacing state (used by update())
        self.frameRate     = 0
        self.lastUpdateEnd = 0

        # Default to grayscale enabled. Games typically call
        # enableGrayscale() at startup; we save them the dance.
        # disableGrayscale() flips us to mono presentation.
        self._grayscale = True

        # Default font. /lib/font5x7.bin is exposed by the ROM-VFS
        # mount in _boot_fat.py so this works without any FAT footprint.
        self.setFont('/lib/font5x7.bin', 5, 7, 1)
        self.fill(0)

        # Tiny shim: copy any pre-drawn frame from `thumbyGraphics` if
        # the game imported it before us. Matches upstream behaviour
        # so games that fall back from std → grayscale mid-run keep
        # their visible state.
        if 'thumbyGraphics' in sys.modules:
            try:
                self.buffer[:] = sys.modules['thumbyGraphics'].display.display.buffer
            except Exception:
                pass

    # `with display:` context. Upstream toggles grayscale on
    # enter/exit; we mirror that even though the ops are cheap.
    def __enter__(self):
        self.enableGrayscale()
        return self
    def __exit__(self, type, value, traceback):
        self.disableGrayscale()

    # --- SSD1306-shape no-ops -------------------------------------
    def reset(self):       pass
    def init_display(self): pass
    def poweron(self):     pass
    def poweroff(self):    pass
    def write_cmd(self, cmd): pass
    def write_window_cmd(self): pass
    def write_data(self, buf): pass

    # invert/brightness — accepted but ignored. The Color LCD doesn't
    # have the SSD1306 invert/contrast quirks the upstream library
    # works around. Keep the signatures so games don't crash.
    @micropython.native
    def invert(self, invert):
        # Could redirect through a "negate before palette lookup" flag
        # in thumby_render later; for now, no-op — almost no legacy
        # game relies on it being honoured.
        pass

    @micropython.native
    def brightness(self, c):
        pass

    # --- Grayscale enable/disable ---------------------------------
    @micropython.native
    def enableGrayscale(self):
        self._grayscale = True

    @micropython.native
    def disableGrayscale(self):
        self._grayscale = False

    # --- Frame submission -----------------------------------------
    @micropython.native
    def show(self):
        # Hand both planes to the bare-metal renderer. When grayscale
        # is disabled we still hand over both planes, but route through
        # the mono present_* path so only the .buffer plane drives
        # output (the standard Thumby visual).
        import thumby_render
        if self._grayscale:
            thumby_render.present_gray(self.buffer, self.shading)
        else:
            thumby_render.present_mono(self.buffer)

    @micropython.native
    def show_async(self):
        # Upstream's async show defers the buffer copy to the GPU
        # thread. We don't have one — every frame is synchronous via
        # engine.tick(). Treat as alias.
        self.show()

    @micropython.native
    def setFPS(self, newFrameRate):
        self.frameRate = newFrameRate

    @micropython.native
    def update(self):
        # Push frame, then frame-pace identically to upstream so games
        # that rely on setFPS get the right cadence. The button
        # update() calls during the wait keep edge-detection state
        # fresh for justPressed() callers.
        self.show()
        if self.frameRate > 0:
            frameTimeMs = 1000 // self.frameRate
            lastUpdateEnd = self.lastUpdateEnd
            frameTimeRemaining = frameTimeMs - ticks_diff(ticks_ms(), lastUpdateEnd)
            while frameTimeRemaining > 1:
                buttonA.update()
                buttonB.update()
                buttonU.update()
                buttonD.update()
                buttonL.update()
                buttonR.update()
                sleep_ms(1)
                frameTimeRemaining = frameTimeMs - ticks_diff(ticks_ms(), lastUpdateEnd)
            while frameTimeRemaining > 0:
                frameTimeRemaining = frameTimeMs - ticks_diff(ticks_ms(), lastUpdateEnd)
        self.lastUpdateEnd = ticks_ms()

    # --- Font ------------------------------------------------------
    def setFont(self, fontFile, width, height, space):
        sz = stat(fontFile)[6]
        self.font_bmap = bytearray(sz)
        with open(fontFile, 'rb') as fh:
            fh.readinto(self.font_bmap)
        self.font_width    = width
        self.font_height   = height
        self.font_space    = space
        self.font_glyphcnt = sz // width

    # --- Calibrate (no-op on Color) -------------------------------
    def calibrate(self):
        # Upstream presents a calibration UI for the OLED-vs-OLED2
        # timing trick. None of that exists on Color — return
        # immediately so games don't wait on a calibration dialog.
        pass

    # --- Drawing primitives — verbatim viper from upstream ---------
    # The only changes from Timendus's code are formatting; behaviour
    # is identical so games render pixel-for-pixel as on Thumby.

    @micropython.viper
    def fill(self, colour: int):
        buffer  = ptr32(self.buffer)
        shading = ptr32(self.shading)
        f1 = -1 if colour & 1 else 0
        f2 = -1 if colour & 2 else 0
        i = 0
        while i < _BUFF_INT_SIZE:
            buffer[i]  = f1
            shading[i] = f2
            i += 1

    @micropython.viper
    def drawFilledRectangle(self, x: int, y: int, width: int, height: int, colour: int):
        if x + width <= 0 or x >= _WIDTH or y + height <= 0 or y >= _HEIGHT:
            return
        if width <= 0 or height <= 0:
            return
        if x < 0:
            width += x
            x = 0
        if y < 0:
            height += y
            y = 0
        x2 = x + width
        y2 = y + height
        if x2 > _WIDTH:
            x2 = _WIDTH
            width = _WIDTH - x
        if y2 > _HEIGHT:
            y2 = _HEIGHT
            height = _HEIGHT - y

        buffer  = ptr8(self.buffer)
        shading = ptr8(self.shading)

        o = (y >> 3) * _WIDTH
        oe = o + x2
        o += x
        strd = _WIDTH - width

        c1 = colour & 1
        c2 = colour & 2
        v1 = 0xff if c1 else 0
        v2 = 0xff if c2 else 0

        yb = y & 7
        ybh = 8 - yb
        if height <= ybh:
            m = ((1 << height) - 1) << yb
        else:
            m = 0xff << yb
        im = 255 - m
        while o < oe:
            if c1: buffer[o]  |= m
            else:  buffer[o]  &= im
            if c2: shading[o] |= m
            else:  shading[o] &= im
            o += 1
        height -= ybh
        while height >= 8:
            o += strd
            oe += _WIDTH
            while o < oe:
                buffer[o]  = v1
                shading[o] = v2
                o += 1
            height -= 8
        if height > 0:
            o += strd
            oe += _WIDTH
            m = (1 << height) - 1
            im = 255 - m
            while o < oe:
                if c1: buffer[o]  |= m
                else:  buffer[o]  &= im
                if c2: shading[o] |= m
                else:  shading[o] &= im
                o += 1

    @micropython.viper
    def drawRectangle(self, x: int, y: int, width: int, height: int, colour: int):
        dfr = self.drawFilledRectangle
        dfr(x, y, width, 1, colour)
        dfr(x, y, 1, height, colour)
        dfr(x, y + height - 1, width, 1, colour)
        dfr(x + width - 1, y, 1, height, colour)

    @micropython.viper
    def setPixel(self, x: int, y: int, colour: int):
        if x < 0 or x >= _WIDTH or y < 0 or y >= _HEIGHT:
            return
        o = (y >> 3) * _WIDTH + x
        m = 1 << (y & 7)
        im = 255 - m
        buffer  = ptr8(self.buffer)
        shading = ptr8(self.shading)
        if colour & 1: buffer[o]  |= m
        else:          buffer[o]  &= im
        if colour & 2: shading[o] |= m
        else:          shading[o] &= im

    @micropython.viper
    def getPixel(self, x: int, y: int) -> int:
        if x < 0 or x >= _WIDTH or y < 0 or y >= _HEIGHT:
            return 0
        o = (y >> 3) * _WIDTH + x
        m = 1 << (y & 7)
        buffer  = ptr8(self.buffer)
        shading = ptr8(self.shading)
        colour = 0
        if buffer[o]  & m: colour  = 1
        if shading[o] & m: colour |= 2
        return colour

    @micropython.viper
    def drawLine(self, x0: int, y0: int, x1: int, y1: int, colour: int):
        if x0 == x1:
            self.drawFilledRectangle(x0, y0, 1, y1 - y0, colour)
            return
        if y0 == y1:
            self.drawFilledRectangle(x0, y0, x1 - x0, 1, colour)
            return
        dx = x1 - x0
        dy = y1 - y0
        sx = 1
        if dy < 0:
            x0, x1 = x1, x0
            y0, y1 = y1, y0
            dy = 0 - dy
            dx = 0 - dx
        if dx < 0:
            dx = 0 - dx
            sx = -1
        x = x0
        y = y0
        buffer  = ptr8(self.buffer)
        shading = ptr8(self.shading)

        o = (y >> 3) * _WIDTH + x
        m = 1 << (y & 7)
        im = 255 - m
        c1 = colour & 1
        c2 = colour & 2

        if dx > dy:
            err = dx >> 1
            x1 += 1
            while x != x1:
                if 0 <= x < _WIDTH and 0 <= y < _HEIGHT:
                    if c1: buffer[o]  |= m
                    else:  buffer[o]  &= im
                    if c2: shading[o] |= m
                    else:  shading[o] &= im
                err -= dy
                if err < 0:
                    y += 1
                    m <<= 1
                    if m & 0x100:
                        o += _WIDTH
                        m = 1
                        im = 0xfe
                    else:
                        im = 255 - m
                    err += dx
                x += sx
                o += sx
        else:
            err = dy >> 1
            y1 += 1
            while y != y1:
                if 0 <= x < _WIDTH and 0 <= y < _HEIGHT:
                    if c1: buffer[o]  |= m
                    else:  buffer[o]  &= im
                    if c2: shading[o] |= m
                    else:  shading[o] &= im
                err -= dx
                if err < 0:
                    x += sx
                    o += sx
                    err += dy
                y += 1
                m <<= 1
                if m & 0x100:
                    o += _WIDTH
                    m = 1
                    im = 0xfe
                else:
                    im = 255 - m

    @micropython.viper
    def drawText(self, stringToPrint, x: int, y: int, colour: int):
        buffer        = ptr8(self.buffer)
        shading       = ptr8(self.shading)
        font_bmap     = ptr8(self.font_bmap)
        font_width    = int(self.font_width)
        font_space    = int(self.font_space)
        font_glyphcnt = int(self.font_glyphcnt)
        sm1o = 0xff if colour & 1 else 0
        sm1a = 255 - sm1o
        sm2o = 0xff if colour & 2 else 0
        sm2a = 255 - sm2o
        ou = (y >> 3) * _WIDTH + x
        ol = ou + _WIDTH
        shu = y & 7
        shl = 8 - shu
        for c in memoryview(stringToPrint):
            if isinstance(c, str):
                co = int(ord(c)) - 0x20
            else:
                co = int(c) - 0x20
            if co < font_glyphcnt:
                gi = co * font_width
                gx = 0
                while gx < font_width:
                    if 0 <= x < _WIDTH:
                        gb = font_bmap[gi + gx]
                        gbu = gb << shu
                        gbl = gb >> shl
                        if 0 <= ou < _BUFF_SIZE:
                            buffer[ou]  = (buffer[ou]  | (gbu & sm1o)) & 255 - (gbu & sm1a)
                            shading[ou] = (shading[ou] | (gbu & sm2o)) & 255 - (gbu & sm2a)
                        if (shl != 8) and (0 <= ol < _BUFF_SIZE):
                            buffer[ol]  = (buffer[ol]  | (gbl & sm1o)) & 255 - (gbl & sm1a)
                            shading[ol] = (shading[ol] | (gbl & sm2o)) & 255 - (gbl & sm2a)
                    ou += 1
                    ol += 1
                    x += 1
                    gx += 1
            ou += font_space
            ol += font_space
            x += font_space

    @micropython.viper
    def blit(self, src, x: int, y: int, width: int, height: int, key: int, mirrorX: int, mirrorY: int):
        if x + width < 0 or x >= _WIDTH:
            return
        if y + height < 0 or y >= _HEIGHT:
            return
        buffer  = ptr8(self.buffer)
        shading = ptr8(self.shading)

        if isinstance(src, (tuple, list)):
            shd = 1
            src1 = ptr8(src[0])
            src2 = ptr8(src[1])
        else:
            shd = 0
            src1 = ptr8(src)
            src2 = ptr8(0)

        stride = width

        srcx = 0; srcy = 0
        dstx = x; dsty = y
        sdx = 1
        if mirrorX:
            sdx = -1
            srcx += width - 1
            if dstx < 0:
                srcx += dstx
                width += dstx
                dstx = 0
        else:
            if dstx < 0:
                srcx = 0 - dstx
                width += dstx
                dstx = 0
        if dstx + width > _WIDTH:
            width = _WIDTH - dstx
        if mirrorY:
            srcy = height - 1
            if dsty < 0:
                srcy += dsty
                height += dsty
                dsty = 0
        else:
            if dsty < 0:
                srcy = 0 - dsty
                height += dsty
                dsty = 0
        if dsty + height > _HEIGHT:
            height = _HEIGHT - dsty

        srco = (srcy >> 3) * stride + srcx
        srcm = 1 << (srcy & 7)

        dsto = (dsty >> 3) * _WIDTH + dstx
        dstm = 1 << (dsty & 7)
        dstim = 255 - dstm

        while height != 0:
            srcco = srco
            dstco = dsto
            i = width
            while i != 0:
                v = 0
                if src1[srcco] & srcm: v = 1
                if shd and (src2[srcco] & srcm): v |= 2
                if (key == -1) or (v != key):
                    if v & 1: buffer[dstco]  |= dstm
                    else:     buffer[dstco]  &= dstim
                    if v & 2: shading[dstco] |= dstm
                    else:     shading[dstco] &= dstim
                srcco += sdx
                dstco += 1
                i -= 1
            dstm <<= 1
            if dstm & 0x100:
                dsto += _WIDTH
                dstm = 1
                dstim = 0xfe
            else:
                dstim = 255 - dstm
            if mirrorY:
                srcm >>= 1
                if srcm == 0:
                    srco -= stride
                    srcm = 0x80
            else:
                srcm <<= 1
                if srcm & 0x100:
                    srco += stride
                    srcm = 1
            height -= 1

    @micropython.native
    def drawSprite(self, s):
        self.blit(s.bitmap, s.x, s.y, s.width, s.height, s.key, s.mirrorX, s.mirrorY)

    @micropython.viper
    def blitWithMask(self, src, x: int, y: int, width: int, height: int, key: int, mirrorX: int, mirrorY: int, mask):
        if x + width < 0 or x >= _WIDTH:
            return
        if y + height < 0 or y >= _HEIGHT:
            return
        buffer  = ptr8(self.buffer)
        shading = ptr8(self.shading)

        if isinstance(src, (tuple, list)):
            shd = 1
            src1 = ptr8(src[0])
            src2 = ptr8(src[1])
        else:
            shd = 0
            src1 = ptr8(src)
            src2 = ptr8(0)

        if isinstance(mask, (tuple, list)):
            maskp = ptr8(mask[0])
        else:
            maskp = ptr8(mask)

        stride = width

        srcx = 0; srcy = 0
        dstx = x; dsty = y
        sdx = 1
        if mirrorX:
            sdx = -1
            srcx += width - 1
            if dstx < 0:
                srcx += dstx
                width += dstx
                dstx = 0
        else:
            if dstx < 0:
                srcx = 0 - dstx
                width += dstx
                dstx = 0
        if dstx + width > _WIDTH:
            width = _WIDTH - dstx
        if mirrorY:
            srcy = height - 1
            if dsty < 0:
                srcy += dsty
                height += dsty
                dsty = 0
        else:
            if dsty < 0:
                srcy = 0 - dsty
                height += dsty
                dsty = 0
        if dsty + height > _HEIGHT:
            height = _HEIGHT - dsty

        srco = (srcy >> 3) * stride + srcx
        srcm = 1 << (srcy & 7)

        dsto = (dsty >> 3) * _WIDTH + dstx
        dstm = 1 << (dsty & 7)
        dstim = 255 - dstm

        while height != 0:
            srcco = srco
            dstco = dsto
            i = width
            while i != 0:
                if maskp[srcco] & srcm:
                    if src1[srcco] & srcm: buffer[dstco]  |= dstm
                    else:                  buffer[dstco]  &= dstim
                    if shd and (src2[srcco] & srcm): shading[dstco] |= dstm
                    else:                            shading[dstco] &= dstim
                srcco += sdx
                dstco += 1
                i -= 1
            dstm <<= 1
            if dstm & 0x100:
                dsto += _WIDTH
                dstm = 1
                dstim = 0xfe
            else:
                dstim = 255 - dstm
            if mirrorY:
                srcm >>= 1
                if srcm == 0:
                    srco -= stride
                    srcm = 0x80
            else:
                srcm <<= 1
                if srcm & 0x100:
                    srco += stride
                    srcm = 1
            height -= 1

    @micropython.native
    def drawSpriteWithMask(self, s, m):
        self.blitWithMask(s.bitmap, s.x, s.y, s.width, s.height, s.key, s.mirrorX, s.mirrorY, m.bitmap)


# Module-level singleton — matches upstream so
# `from thumbyGrayscale import display` works.
display = Grayscale()
