import vfs
import machine, rp2


# ThumbyOne: the lobby is the ONLY thing allowed to format the
# shared FAT. Do NOT pass mkfs=True on mount — a transient mount
# failure from a slot would otherwise silently wipe the user's
# entire drive. If mount fails here, fail loudly (raise) and let
# main.c fall through to the REPL with the picker's last frame on
# screen; the user reboots to the lobby (LB+RB is the documented
# wipe path) and their files are intact.
bdev = rp2.Flash()
fs = vfs.VfsFat(bdev)
vfs.mount(fs, "/")

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

del vfs, bdev, fs
