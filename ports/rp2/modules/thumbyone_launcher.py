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


def _run_active_game():
    try:
        with open("/.active_game") as f:
            game_dir = f.read().strip()
    except OSError:
        return  # no active game — REPL

    if not game_dir:
        return

    main_path = game_dir + "/main.py"
    try:
        with open(main_path) as f:
            code = f.read()
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

    # Give engine_save a per-game saves directory under /Saves.
    # Mirrors the engine launcher: each game gets its own namespace
    # so that `engine_save.save("foo", x)` writes under /Saves/<name>.
    try:
        import engine_save
        engine_save._init_saves_dir("/Saves" + game_dir)
    except Exception:
        pass

    g = {"__name__": "__main__", "__file__": main_path}
    try:
        exec(code, g)
    except Exception as e:
        # Game crashed — capture the traceback to /.last_error.txt
        # so the user can inspect it via USB MSC after reboot. Also
        # print to CDC in case a serial terminal is attached.
        sys.print_exception(e)
        try:
            import io
            buf = io.StringIO()
            buf.write("Game crash: " + main_path + "\n")
            sys.print_exception(e, buf)
            # Restore cwd before writing so the path is absolute.
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
