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
        except OSError:
            # Path in /.active_game no longer points at a real game.
            return

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

    # Hijack `import thumbyGrayscale` so any legacy game that bundles
    # the Timendus library gets our Color-aware frozen version instead.
    # The bundled copy lives in the game directory; without this trick
    # the launcher's `sys.path.insert(0, game_dir)` would shadow the
    # frozen module. By pre-populating sys.modules we win regardless
    # of sys.path order — `from thumbyGrayscale import display` short-
    # circuits to our cached entry. Skip silently if the module isn't
    # available (build without the grayscale shim).
    _gs_mod = None
    try:
        import thumbyGrayscale as _gs_mod
        sys.modules['thumbyGrayscale'] = _gs_mod
    except ImportError:
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
                import types as _types
                _shim = _types.ModuleType('display')
                _shim.display        = _gs_mod.display
                _shim.display_buffer = _gs_mod.display.buffer
                _shim.display_update = _gs_mod.display.update
                _shim.Sprite         = _gs_mod.Sprite
                sys.modules['display'] = _shim
                del _shim, _types
            del _df, _head
        except OSError:
            pass
    del _gs_mod

    g = {"__name__": "__main__", "__file__": main_path}
    try:
        exec(code, g)
    except Exception as e:
        # Game crashed — capture the traceback to /.last_error.txt
        # so the user can inspect it via USB MSC after reboot (blank
        # LCD + no host connection makes this the only channel).
        sys.print_exception(e)
        try:
            import io
            buf = io.StringIO()
            buf.write("Game crash: " + main_path + "\n")
            sys.print_exception(e, buf)
            try:
                os.chdir("/")
            except OSError:
                pass
            with open("/.last_error.txt", "w") as ef:
                ef.write(buf.getvalue())
        except Exception:
            pass
    finally:
        if original_cwd is not None:
            try:
                os.chdir(original_cwd)
            except OSError:
                pass


_run_active_game()
del _run_active_game
