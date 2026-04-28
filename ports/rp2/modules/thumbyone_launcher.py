# ThumbyOne MPY slot: run the game picked by the C picker.
#
# The C picker (common/picker/picker.c in the ThumbyOne tree) ran
# before mp_init() and wrote the chosen game directory's path to
# /.active_game on the shared FAT. _boot_fat.py then mounted the
# FAT at "/", so we can just read the file here and exec the
# game's main.py.
#
# Mirrors the engine's own filesystem/main.py launcher flow: chdir
# into the game dir so relative file paths (TextureResource("foo.bmp"))
# resolve, add the dir to sys.path for imports, and initialise the
# save subsystem. Without the chdir the engine raises ENOENT on
# every asset load.
#
# If /.active_game is missing (picker didn't write one because it
# was the first-boot "no games" splash, or a previous game run
# deleted it to force the picker next reboot), fall through to
# the normal MicroPython REPL path.

import os
import sys

# `engine_main` is the C-side bootstrap that initialises the game
# engine's hardware (LCD, audio PWM, button state machine, RTC, etc.).
# The engine modules raise RuntimeError on first use if it hasn't been
# imported. Modern Thumby Color games do `import engine_main` at the
# top of their main.py; legacy Thumby games (which use the frozen
# `thumby` shim) don't know about it. Importing it once here covers
# both cases — second imports from the game itself are no-ops.
import engine_main  # noqa: F401


def _write_last_error(prefix, e):
    """Capture an exception traceback to /.last_error.txt. Cheap to
    call from any code path that wants to record a launch-time
    failure visible to the user via USB MSC after reboot."""
    try:
        sys.print_exception(e)
    except Exception:
        pass
    try:
        import io
        buf = io.StringIO()
        buf.write(prefix)
        sys.print_exception(e, buf)
        try:
            os.chdir("/")
        except OSError:
            pass
        with open("/.last_error.txt", "w") as ef:
            ef.write(buf.getvalue())
    except Exception:
        pass


def _run_active_game():
    try:
        with open("/.active_game") as f:
            game_dir = f.read().strip()
    except OSError:
        return  # no active game — REPL

    if not game_dir:
        return

    # Modern Thumby Color games ship main.py. Legacy original-Thumby
    # games instead ship <dirname>.py inside the game folder, matching
    # the pre-Color launcher convention. Try main.py first, then fall
    # back to the legacy name so `import thumby` games (rendered via
    # the frozen thumby/ssd1306 shim) Just Work.
    main_path = game_dir + "/main.py"
    is_legacy_game = False
    try:
        with open(main_path) as f:
            code = f.read()
    except OSError:
        slash = game_dir.rfind("/")
        dir_name = game_dir[slash + 1:] if slash >= 0 else game_dir
        legacy_path = game_dir + "/" + dir_name + ".py"
        try:
            with open(legacy_path) as f:
                code = f.read()
            main_path = legacy_path
            is_legacy_game = True
        except OSError:
            # Path in /.active_game no longer points at a real game.
            return

    # Pre-load our frozen thumbyGrayscale BEFORE adding the game's
    # directory to sys.path. Many original-Thumby grayscale games
    # ship a local copy of Timendus's thumbyGrayscale.py with raw
    # SSD1306 SPI/_thread code that uses MicroPython integer-literal
    # forms our parser rejects (e.g. `0xD0000000+0x01C` style hex
    # joins) — if the launcher imports thumbyGrayscale AFTER game_dir
    # is on sys.path, the local copy wins the search and crashes the
    # parse. Importing here pulls our frozen module while sys.path
    # still has only the default .frozen / /lib entries; the resulting
    # sys.modules['thumbyGrayscale'] cache then short-circuits any
    # subsequent `import thumbyGrayscale` from the game itself.
    _gs_mod = None
    try:
        import thumbyGrayscale as _gs_mod
        sys.modules['thumbyGrayscale'] = _gs_mod
    except Exception as _gs_e:
        _write_last_error("thumbyGrayscale import failed:\n", _gs_e)
        _gs_mod = None

    # Add game dir to sys.path for `import` inside the game.
    if game_dir not in sys.path:
        sys.path.insert(0, game_dir)

    # chdir into the game dir so relative paths (e.g.
    # `TextureResource("sprite.bmp")`) resolve against the game's
    # own folder — this matches engine/filesystem/main.py's flow.
    original_cwd = None
    try:
        original_cwd = os.getcwd()
    except OSError:
        pass
    try:
        os.chdir(game_dir)
    except OSError:
        pass

    # Per-game save namespace under /Saves — matches the engine
    # launcher's convention.
    try:
        import engine_save
        engine_save._init_saves_dir("/Saves" + game_dir)
    except Exception:
        pass

    # A handful of original-Thumby games (Umby & Glow being the
    # canonical example) inline the entire Timendus Grayscale class
    # into a per-game `display.py` instead of importing thumbyGrayscale.
    # The sys.modules trick above can't help those; we'd otherwise need
    # to fork each game's display.py. Instead, sniff the file: if it
    # looks like the Timendus inline pattern (`class Grayscale` plus
    # an `_thread` import — the second-core SPI-driver giveaway), shim
    # `import display` to our frozen thumbyGrayscale singleton with
    # the three names other game files import (`display`,
    # `display_buffer`, `display_update`). The user's original game
    # files stay untouched on the FAT.
    if _gs_mod is not None:
        try:
            with open(game_dir + "/display.py", "rb") as _df:
                _head = _df.read(2048)
            if b"class Grayscale" in _head and b"_thread" in _head:
                # `from X import Y` in MicroPython resolves Y as an
                # attribute lookup on whatever sys.modules['X'] holds,
                # so a plain object with the right attributes works
                # just as well as a real ModuleType — and avoids the
                # `import types` dependency (the types module isn't
                # frozen into our build, so importing it would raise).
                class _DisplayShim:
                    pass
                _shim = _DisplayShim()
                _shim.display        = _gs_mod.display
                _shim.display_buffer = _gs_mod.display.buffer
                _shim.display_update = _gs_mod.display.update
                _shim.Sprite         = _gs_mod.Sprite
                sys.modules['display'] = _shim
                del _shim
        except OSError:
            pass
        except Exception as _shim_e:
            _write_last_error("display.py shim install failed:\n", _shim_e)
    del _gs_mod

    # Legacy original-Thumby games (the ones we found via the
    # <dirname>.py filename fallback) often read buttons via raw
    # `machine.Pin(N, Pin.IN, Pin.PULL_UP).value` with original-Thumby
    # GPIO numbers (3,4,5,6,24,27). On Color those pin numbers map to
    # completely different functions (LCD reset, charge-status, rumble
    # PWM, etc.), so a real `Pin(5)` would steal the rumble pin and
    # cause constant vibration once the engine's PWM duty is touched
    # by anything in the slice — exactly what we saw with Umby & Glow.
    #
    # We can't just `machine.Pin = _PinShim` because the `machine`
    # module's globals dict is read-only (MP_DEFINE_CONST_DICT). The
    # working approach is to replace `sys.modules['machine']` with a
    # wrapper instance: `Pin` resolves to our shim, every other
    # attribute is forwarded to the real `machine` module via
    # `__getattr__`. `from machine import Pin` and `from machine
    # import freq, reset, PWM, Timer, ...` all still work.
    if is_legacy_game:
        try:
            import machine as _real_machine
            import engine_io as _eio_for_pin
            _real_pin_class = _real_machine.Pin
            _LEGACY_BTN_MAP = {
                3:  _eio_for_pin.LEFT,
                4:  _eio_for_pin.UP,
                5:  _eio_for_pin.RIGHT,
                6:  _eio_for_pin.DOWN,
                24: _eio_for_pin.B,
                27: _eio_for_pin.A,
            }
            # Cache the wrapper instances per pin so MicroPython's GC
            # can't free them out from under a bound `.value` method.
            _LEGACY_BTN_CACHE = {}
            class _LegacyButtonPin:
                # Original Thumby buttons are active-low (pull-up,
                # 0 == pressed). engine_io.X.is_pressed is True when
                # the user is pressing it, so invert.
                def __init__(self, btn): self._btn = btn
                def value(self, *args):
                    _eio_for_pin.update_buttons()
                    return 0 if self._btn.is_pressed else 1
                def init(self, *args, **kwargs): pass
                def irq(self, *args, **kwargs): return None
            class _PinShim:
                IN        = _real_pin_class.IN
                OUT       = _real_pin_class.OUT
                PULL_UP   = _real_pin_class.PULL_UP
                PULL_DOWN = _real_pin_class.PULL_DOWN
                def __new__(cls, pin_id, *args, **kwargs):
                    if pin_id in _LEGACY_BTN_MAP:
                        if pin_id not in _LEGACY_BTN_CACHE:
                            _LEGACY_BTN_CACHE[pin_id] = _LegacyButtonPin(_LEGACY_BTN_MAP[pin_id])
                        return _LEGACY_BTN_CACHE[pin_id]
                    return _real_pin_class(pin_id, *args, **kwargs)
            class _MachineShim:
                Pin = _PinShim
                def __getattr__(self, name):
                    return getattr(_real_machine, name)
            sys.modules['machine'] = _MachineShim()
        except Exception as _pin_e:
            _write_last_error("Pin shim install failed:\n", _pin_e)

    g = {"__name__": "__main__", "__file__": main_path}
    try:
        exec(code, g)
    except Exception as e:
        # Game crashed — capture the traceback to /.last_error.txt
        # so the user can inspect it via USB MSC after reboot.
        _write_last_error("Game crash: " + main_path + "\n", e)
    finally:
        if original_cwd is not None:
            try:
                os.chdir(original_cwd)
            except OSError:
                pass


_run_active_game()
del _run_active_game
