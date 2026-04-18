import vfs
import machine, rp2


# ThumbyOne: the lobby is the ONLY thing that's allowed to mkfs the
# shared FAT. If VfsFat can't mount, DO NOT auto-format — that path
# was silently wiping the user's entire drive when something went
# wrong after a picker → MicroPython handoff (e.g. VfsFat's internal
# f_mount returned a transient error for any reason). Fail loudly
# instead: catch the exception, log a marker for the launcher trace
# to pick up, and let the launcher continue. Without a mounted root
# the launcher will fail to open /.active_game — which is a
# recoverable state (reboot to lobby, all files intact).
bdev = rp2.Flash()
_mount_err = None
try:
    fs = vfs.VfsFat(bdev)
    vfs.mount(fs, "/")
except Exception as e:
    _mount_err = e
    fs = None

# ThumbyOne: mount the ROM-backed /system/ VFS on top of the shared
# FAT. The engine's filesystem/system/ tree (fonts, launcher assets,
# crash handler, splash images) was packed into firmware at build
# time by pack_system_rom.py and is served read-only from flash ROM
# via the native `thumbyone_rom` module. Saves ~376 KB of FAT space
# and means /system/ is always present without a first-boot copy.
try:
    import thumbyone_rom
    vfs.mount(thumbyone_rom.ThumbyOneRomVFS(), "/system", readonly=True)
    del thumbyone_rom
except ImportError:
    pass
except Exception:
    pass

# Stash the mount error on the vfs module so the launcher can pick
# it up and include it in /.launch_trace.txt. Using vfs.* as a
# scratch namespace — it's guaranteed to be importable by the
# launcher. On success this stays None.
vfs._thumbyone_boot_fat_mount_err = _mount_err

del _mount_err
del bdev, fs
