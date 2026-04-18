import vfs
import machine, rp2


# Try to mount the filesystem, and format the flash if it doesn't exist.
#
# ThumbyOne note: the lobby is responsible for formatting the shared
# FAT (canonical shape: FAT16, 1 KB clusters, label "THUMBYONE").
# Passing mkfs=True below is belt-and-braces — if the user has
# somehow landed in MPY with an empty shared region, we'd rather
# self-recover than brick the slot. The resulting on-disk format
# matches the lobby's because VfsFat.mkfs uses the same default
# MKFS_PARM that vfs_fat.c ships with.
bdev = rp2.Flash()
fs = vfs.VfsFat(bdev)
vfs.mount(fs, "/", mkfs=True)

# ThumbyOne: mount the ROM-backed /system/ VFS on top of the shared
# FAT. The engine's filesystem/system/ tree (fonts, launcher assets,
# crash handler, splash images) was packed into firmware at build
# time by pack_system_rom.py and is served read-only from flash ROM
# via the native `thumbyone_rom` module. This saves ~376 KB of FAT
# space and means /system/ is always present without a first-boot
# copy step.
try:
    import thumbyone_rom
    vfs.mount(thumbyone_rom.ThumbyOneRomVFS(), "/system", readonly=True)
    del thumbyone_rom
except ImportError:
    pass

del vfs, bdev, fs
