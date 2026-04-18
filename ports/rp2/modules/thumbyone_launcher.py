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
#
# Diagnostic trail: every stage writes a short marker into
# /.launch_trace.txt before it runs. If a launch hangs, the user
# can return to the lobby, USB-mount the drive, and see exactly
# which stage never completed. /.last_error.txt still captures
# full exception tracebacks on crashes.

import os
import sys


_TRACE_PATH = "/.launch_trace.txt"


def _trace(msg):
    try:
        with open(_TRACE_PATH, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _run_active_game():
    # First stage — wipe any previous trace so the file only contains
    # the current launch attempt. This write itself goes through the
    # root mount; if _boot_fat.py failed to mount the FAT this will
    # raise, so we report that separately below.
    mount_err = None
    try:
        import vfs as _vfs_mod
        mount_err = getattr(_vfs_mod, "_thumbyone_boot_fat_mount_err", None)
    except Exception:
        pass

    try:
        with open(_TRACE_PATH, "w") as f:
            f.write("launcher start\n")
            if mount_err is not None:
                f.write("FAT MOUNT FAILED: " + repr(mount_err) + "\n")
    except Exception:
        # Trace write failed — no point continuing; user won't see
        # anything but we can't write an error either. Return so
        # main.c drops to the (invisible) REPL.
        return

    if mount_err is not None:
        # No mounted FAT = nothing to launch. The picker wrote
        # /.active_game before the handoff, but we can't read it now.
        # Let the user return to the lobby; their files are intact
        # because we no longer auto-format.
        return

    try:
        with open("/.active_game") as f:
            game_dir = f.read().strip()
    except OSError:
        _trace("no /.active_game — falling through to REPL")
        return

    if not game_dir:
        _trace("/.active_game was empty")
        return

    _trace("game_dir = " + game_dir)

    main_path = game_dir + "/main.py"
    try:
        with open(main_path) as f:
            code = f.read()
    except OSError:
        _trace("FAILED: could not read " + main_path)
        return
    _trace("read main.py (" + str(len(code)) + " bytes)")

    if game_dir not in sys.path:
        sys.path.insert(0, game_dir)

    original_cwd = None
    try:
        original_cwd = os.getcwd()
    except OSError:
        pass
    try:
        os.chdir(game_dir)
    except OSError:
        _trace("WARN: chdir failed")
    _trace("chdir done")

    try:
        import engine_save
        engine_save._init_saves_dir("/Saves" + game_dir)
    except Exception:
        pass
    _trace("engine_save init done")

    g = {"__name__": "__main__", "__file__": main_path}
    _trace("about to exec")
    try:
        exec(code, g)
        _trace("exec returned normally")
    except Exception as e:
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
            _trace("wrote /.last_error.txt")
        except Exception:
            _trace("FAILED: could not write /.last_error.txt")
    finally:
        if original_cwd is not None:
            try:
                os.chdir(original_cwd)
            except OSError:
                pass


_run_active_game()
del _run_active_game
