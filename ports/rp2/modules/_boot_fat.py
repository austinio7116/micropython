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

del vfs, bdev, fs
