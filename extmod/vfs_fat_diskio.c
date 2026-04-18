/*
 * This file is part of the MicroPython project, http://micropython.org/
 *
 * The MIT License (MIT)
 *
 * Copyright (c) 2013, 2014 Damien P. George
 * Copyright (c) 2013-2023 FatFs - Generic FAT Filesystem module
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 */

/*
 * ThumbyOne fork: rewritten for plain FatFs R0.15.
 *
 * The upstream MicroPython version of this file targeted ooFatFs,
 * a MicroPython-specific fork of FatFs R0.13c that passed an opaque
 * bdev pointer as the `pdrv` argument to disk_read/write/ioctl.
 * ThumbyOne reverted to stock upstream FatFs R0.15 (same code used
 * by the NES/P8/DOOM slots and by the lobby) so that on-disk layout
 * and diskio code paths are identical across every system running
 * on the shared 9.6 MB FAT at physical flash 0x660000.
 *
 * Stock FatFs identifies drives by a BYTE pdrv in the range
 * [0, FF_VOLUMES). We build with FF_VOLUMES = 1 (Thumby Color has
 * exactly one flash device and no SD card port), so every call
 * goes to drive 0. The single currently-mounted VfsFat is tracked
 * in `g_mounted`; a second VfsFat construction is rejected at the
 * vfs_fat.c layer so this static never clashes.
 */

#include "py/mpconfig.h"
#if MICROPY_VFS && MICROPY_VFS_FAT

#include <stdint.h>

#include "py/mphal.h"
#include "py/runtime.h"
#include "py/binary.h"
#include "py/objarray.h"
#include "py/mperrno.h"
#include "lib/fatfs/ff.h"
#include "lib/fatfs/diskio.h"
#include "extmod/vfs_fat.h"

#ifdef THUMBYONE_SLOT_MODE
/* ThumbyOne MPY slot fallback. Before MicroPython is initialised,
 * the C picker mounts the shared FAT to scan /games/ and write
 * /.active_game. At that point g_mounted is NULL (no VfsFat yet),
 * so disk_read/write/ioctl divert to thumbyone_disk directly. Once
 * MPY's VfsFat(bdev) make_new runs and sets g_mounted, everything
 * routes through the bdev blockdev as normal. */
#include "thumbyone_disk.h"
#endif

/*
 * Currently-mounted VfsFat. vfs_fat.c sets this via
 * mp_vfs_fat_set_mounted() immediately before the corresponding
 * f_mount() call, and clears it after f_unmount() / on object
 * finalisation. With FF_VOLUMES = 1 this is always the source of
 * disk_read/write data for pdrv = 0. Second mount is rejected in
 * make_new, so there's no race to manage.
 */
static fs_user_mount_t *g_mounted;

void mp_vfs_fat_set_mounted(fs_user_mount_t *vfs) {
    g_mounted = vfs;
}

fs_user_mount_t *mp_vfs_fat_get_mounted(void) {
    return g_mounted;
}

/*-----------------------------------------------------------------------*/
/* Initialize a Drive                                                    */
/*-----------------------------------------------------------------------*/
DSTATUS disk_initialize(BYTE pdrv) {
    (void)pdrv;
    fs_user_mount_t *vfs = g_mounted;
    if (vfs == NULL) {
#ifdef THUMBYONE_SLOT_MODE
        /* Picker-window fallback: thumbyone_disk needs no init. */
        return 0;
#else
        return STA_NOINIT;
#endif
    }
    /* The bdev's init IOCTL gets driven in disk_ioctl(IOCTL_INIT);
     * here we just report readiness based on whether a bdev is
     * attached. */
    return 0;
}

/*-----------------------------------------------------------------------*/
/* Get Drive Status                                                      */
/*-----------------------------------------------------------------------*/
DSTATUS disk_status(BYTE pdrv) {
    (void)pdrv;
    fs_user_mount_t *vfs = g_mounted;
    if (vfs == NULL) {
#ifdef THUMBYONE_SLOT_MODE
        return 0;   /* picker-window fallback */
#else
        return STA_NOINIT;
#endif
    }
    if (vfs->blockdev.writeblocks[0] == MP_OBJ_NULL) {
        return STA_PROTECT;
    }
    return 0;
}

/*-----------------------------------------------------------------------*/
/* Read Sector(s)                                                        */
/*-----------------------------------------------------------------------*/
DRESULT disk_read(BYTE pdrv, BYTE *buff, LBA_t sector, UINT count) {
    (void)pdrv;
    fs_user_mount_t *vfs = g_mounted;
    if (vfs == NULL) {
#ifdef THUMBYONE_SLOT_MODE
        return (thumbyone_disk_read(buff, (uint32_t)sector, count) == 0)
               ? RES_OK : RES_ERROR;
#else
        return RES_PARERR;
#endif
    }
    int ret = mp_vfs_blockdev_read(&vfs->blockdev, (uint32_t)sector, count, buff);
    return ret == 0 ? RES_OK : RES_ERROR;
}

/*-----------------------------------------------------------------------*/
/* Write Sector(s)                                                       */
/*-----------------------------------------------------------------------*/
#if FF_FS_READONLY == 0
DRESULT disk_write(BYTE pdrv, const BYTE *buff, LBA_t sector, UINT count) {
    (void)pdrv;
    fs_user_mount_t *vfs = g_mounted;
    if (vfs == NULL) {
#ifdef THUMBYONE_SLOT_MODE
        return (thumbyone_disk_write(buff, (uint32_t)sector, count) == 0)
               ? RES_OK : RES_ERROR;
#else
        return RES_PARERR;
#endif
    }
    int ret = mp_vfs_blockdev_write(&vfs->blockdev, (uint32_t)sector, count, buff);
    if (ret == -MP_EROFS) {
        return RES_WRPRT;
    }
    return ret == 0 ? RES_OK : RES_ERROR;
}
#endif

/*-----------------------------------------------------------------------*/
/* Miscellaneous Functions                                               */
/*-----------------------------------------------------------------------*/
DRESULT disk_ioctl(BYTE pdrv, BYTE cmd, void *buff) {
    (void)pdrv;
    fs_user_mount_t *vfs = g_mounted;
    if (vfs == NULL) {
#ifdef THUMBYONE_SLOT_MODE
        /* Picker-window fallback: answer via thumbyone_disk. */
        switch (cmd) {
        case CTRL_SYNC:
            return (thumbyone_disk_sync() == 0) ? RES_OK : RES_ERROR;
        case GET_SECTOR_COUNT:
            *((LBA_t *)buff) = (LBA_t)thumbyone_disk_sector_count();
            return RES_OK;
        case GET_SECTOR_SIZE:
            *((WORD *)buff) = (WORD)thumbyone_disk_sector_size();
            return RES_OK;
        case GET_BLOCK_SIZE:
            *((DWORD *)buff) = (DWORD)(THUMBYONE_DISK_ERASE_SIZE /
                                       THUMBYONE_DISK_SECTOR_SIZE);
            return RES_OK;
        default:
            return RES_PARERR;
        }
#else
        return RES_PARERR;
#endif
    }

    /* Route through the MicroPython block-device ioctl for the
     * sizes/sync/init queries; then translate to FatFs return
     * types. We don't implement CTRL_TRIM because FF_USE_TRIM = 0. */
    static const uint8_t op_map[5] = {
        [CTRL_SYNC]        = MP_BLOCKDEV_IOCTL_SYNC,
        [GET_SECTOR_COUNT] = MP_BLOCKDEV_IOCTL_BLOCK_COUNT,
        [GET_SECTOR_SIZE]  = MP_BLOCKDEV_IOCTL_BLOCK_SIZE,
        [GET_BLOCK_SIZE]   = 0,   /* handled locally; no Python call */
    };

    mp_obj_t ret = mp_const_none;
    if (cmd < sizeof(op_map) && op_map[cmd] != 0) {
        ret = mp_vfs_blockdev_ioctl(&vfs->blockdev, op_map[cmd], 0);
    }

    switch (cmd) {
        case CTRL_SYNC:
            return RES_OK;

        case GET_SECTOR_COUNT:
            *((LBA_t *)buff) = (LBA_t)mp_obj_get_int(ret);
            return RES_OK;

        case GET_SECTOR_SIZE: {
            WORD ss;
            if (ret == mp_const_none) {
                ss = 512;
            } else {
                ss = (WORD)mp_obj_get_int(ret);
            }
            *((WORD *)buff) = ss;
            /* Cache the sector size so block read/write can size
             * transfers correctly. */
            vfs->blockdev.block_size = ss;
            return RES_OK;
        }

        case GET_BLOCK_SIZE:
            /* Erase block size in units of sectors. Flash-backed
             * MicroPython bdevs expose a 4 KB erase block / 512 B
             * sector, so 8. But we don't actually query the bdev
             * here — we just want mkfs to pick a sane default. The
             * value has no effect on existing volumes, only on
             * f_mkfs cluster selection, and the lobby (which is
             * what actually mkfs's the shared FAT on this board)
             * uses its own explicit MKFS_PARM. Return 1 to let
             * FatFs fall back to its own heuristics when mkfs is
             * driven from Python. */
            *((DWORD *)buff) = 1;
            return RES_OK;

        default:
            return RES_PARERR;
    }
}

#endif /* MICROPY_VFS && MICROPY_VFS_FAT */
