/*
 * This file is part of the MicroPython project, http://micropython.org/
 *
 * The MIT License (MIT)
 *
 * Copyright (c) 2014 Damien P. George
 * Copyright (c) 2016 Paul Sokolovsky
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
 * ThumbyOne fork: ported from ooFatFs (R0.13c, MicroPython fork
 * with opaque bdev pointers in the fs-operations API) to plain
 * FatFs R0.15 (numeric drive number in diskio, volume-qualified
 * paths everywhere else). See common/lib/fatfs/ for the shared
 * FS library used by the lobby and all slots.
 *
 * Single-volume invariant: FF_VOLUMES == 1. A second VfsFat
 * construction (e.g. mounting an SD card alongside flash) is
 * rejected with OSError at make_new time — there is no second
 * drive on the Thumby Color, and the R0.15 API would otherwise
 * silently clash over FatFs[0].
 */

#include "py/mpconfig.h"
#if MICROPY_VFS_FAT

#if !MICROPY_ENABLE_FINALISER
#error "MICROPY_VFS_FAT requires MICROPY_ENABLE_FINALISER"
#endif

#if !MICROPY_VFS
#error "with MICROPY_VFS_FAT enabled, must also enable MICROPY_VFS"
#endif

#include <string.h>
#include "py/runtime.h"
#include "py/mperrno.h"
#include "lib/fatfs/ff.h"
#include "extmod/vfs_fat.h"
#include "shared/timeutils/timeutils.h"

#if FF_MAX_SS == FF_MIN_SS
#define SECSIZE(fs) (FF_MIN_SS)
#else
#define SECSIZE(fs) ((fs)->ssize)
#endif

#define mp_obj_fat_vfs_t fs_user_mount_t

/* Canonical MKFS_PARM — MUST match ThumbyNES and ThumbyP8's
 * boot_filesystem() so the shared FAT at physical 0x660000
 * interops byte-identically across all four slots.
 *
 * Shape: FAT16, 1 KB clusters, single-FAT, MBR-partitioned.
 *
 *   fmt = FM_FAT      — FAT16 (no FAT32, no exFAT, no FM_SFD).
 *                       WITHOUT FM_SFD an MBR partition table
 *                       lives at sector 0 and the FAT volume at
 *                       the partition start. Windows treats the
 *                       9.6 MB volume as a standard removable
 *                       drive when it sees the MBR; SFD (no MBR)
 *                       can trigger "the drive is not formatted"
 *                       prompts on some Windows versions.
 *   n_fat = 1         — one FAT copy; saves 18 KB on a 9.6 MB
 *                       volume. Redundancy isn't worth much on
 *                       flash that's backed by FatFs's own cache.
 *   au_size = 1024    — 1 KB clusters → 9600 clusters, above the
 *                       FAT12 4084 cap, well below FAT16's 65524.
 *
 * The lobby's own mkfs path (when we add lobby-owned formatting)
 * will reference this same struct. */
static const MKFS_PARM mkfs_default_opt = {
    .fmt = FM_FAT,
    .n_fat = 1,
    .align = 0,
    .n_root = 0,
    .au_size = 1024,
};

/* ThumbyOne: reject second mount attempt. Called by make_new and
 * mkfs. Upstream ooFatFs tolerates multiple VFSes, R0.15 does not
 * (the globals collide), and we deliberately only support one. */
static void check_single_volume_or_raise(void) {
    if (mp_vfs_fat_get_mounted() != NULL) {
        mp_raise_OSError_with_filename(MP_EBUSY,
            "VfsFat is single-volume (FF_VOLUMES=1); unmount the existing one first");
    }
}

static mp_import_stat_t fat_vfs_import_stat(void *vfs_in, const char *path) {
    fs_user_mount_t *vfs = vfs_in;
    FILINFO fno;
    assert(vfs != NULL);
    (void)vfs;
    FRESULT res = f_stat(path, &fno);
    if (res == FR_OK) {
        if ((fno.fattrib & AM_DIR) != 0) {
            return MP_IMPORT_STAT_DIR;
        } else {
            return MP_IMPORT_STAT_FILE;
        }
    }
    return MP_IMPORT_STAT_NO_EXIST;
}

static mp_obj_t fat_vfs_make_new(const mp_obj_type_t *type, size_t n_args, size_t n_kw, const mp_obj_t *args) {
    mp_arg_check_num(n_args, n_kw, 1, 1, false);

    /* Single-volume invariant — see top-of-file comment. */
    check_single_volume_or_raise();

    // create new object
    fs_user_mount_t *vfs = mp_obj_malloc(fs_user_mount_t, type);

    // Initialise underlying block device
    vfs->blockdev.flags = MP_BLOCKDEV_FLAG_FREE_OBJ;
    vfs->blockdev.block_size = FF_MIN_SS; // default, will be populated by call to MP_BLOCKDEV_IOCTL_BLOCK_SIZE
    mp_vfs_blockdev_init(&vfs->blockdev, args[0]);

    /* Register as the single active mount BEFORE calling f_mount so
     * that the diskio layer (disk_read/write/ioctl) can find our
     * blockdev when FatFs starts probing the BPB. If f_mount fails
     * we leave g_mounted set — the FATFS struct is still bound to
     * drive 0 in FatFs[] and later .mount(mkfs=True) will want to
     * drive disk_write through it. */
    mp_vfs_fat_set_mounted(vfs);

    FRESULT res = f_mount(&vfs->fatfs, "", 1);
    if (res == FR_NO_FILESYSTEM) {
        /* Leave the vfs object alive — mount(mkfs=True) will build
         * a FS on it. g_mounted stays set. */
        vfs->blockdev.flags |= MP_BLOCKDEV_FLAG_NO_FILESYSTEM;
    } else if (res != FR_OK) {
        /* Unbind before raising — otherwise a retry can't create
         * a new VfsFat. f_mount with fs=0 clears the slot. */
        f_mount(0, "", 0);
        mp_vfs_fat_set_mounted(NULL);
        mp_raise_OSError(fresult_to_errno_table[res]);
    }

    return MP_OBJ_FROM_PTR(vfs);
}

#if FF_FS_REENTRANT
static mp_obj_t fat_vfs_del(mp_obj_t self_in) {
    mp_obj_fat_vfs_t *self = MP_OBJ_TO_PTR(self_in);
    (void)self;
    /* R0.15's f_unmount("") expands to f_mount(0, "", 0) — releases
     * any sync object and clears FatFs[0]. We also drop g_mounted
     * so a fresh VfsFat can be created in the next GC cycle. */
    f_unmount("");
    if (mp_vfs_fat_get_mounted() == self) {
        mp_vfs_fat_set_mounted(NULL);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(fat_vfs_del_obj, fat_vfs_del);
#endif

static mp_obj_t fat_vfs_mkfs(mp_obj_t bdev_in) {
    // create new object (also binds g_mounted, opens drive 0)
    fs_user_mount_t *vfs = MP_OBJ_TO_PTR(fat_vfs_make_new(&mp_fat_vfs_type, 1, 0, &bdev_in));

    /* R0.15 f_mkfs: path-addressed, MKFS_PARM-configured. Use our
     * canonical parameters so the on-disk shape matches the lobby. */
    uint8_t working_buf[FF_MAX_SS];
    FRESULT res = f_mkfs("", &mkfs_default_opt, working_buf, sizeof(working_buf));
    if (res == FR_MKFS_ABORTED) {
        /* Falling back to FAT32 — happens if the volume is too
         * small for the requested FAT16 geometry or too large for
         * the au_size. For Thumby Color's 9.6 MB this shouldn't
         * happen; keep the fallback for safety on smaller test
         * volumes. */
        MKFS_PARM fallback = mkfs_default_opt;
        fallback.fmt = FM_FAT32;
        fallback.au_size = 0;   /* let FatFs pick */
        res = f_mkfs("", &fallback, working_buf, sizeof(working_buf));
    }
    if (res != FR_OK) {
        mp_raise_OSError(fresult_to_errno_table[res]);
    }

    /* f_mkfs wrote a new BPB to disk but doesn't populate the
     * FATFS struct. Re-mount to refresh in-memory state. */
    res = f_mount(&vfs->fatfs, "", 1);
    if (res != FR_OK) {
        mp_raise_OSError(fresult_to_errno_table[res]);
    }
    vfs->blockdev.flags &= ~MP_BLOCKDEV_FLAG_NO_FILESYSTEM;

    // set the filesystem label if it's configured
    #ifdef MICROPY_HW_FLASH_FS_LABEL
    f_setlabel(MICROPY_HW_FLASH_FS_LABEL);
    #endif

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(fat_vfs_mkfs_fun_obj, fat_vfs_mkfs);
static MP_DEFINE_CONST_STATICMETHOD_OBJ(fat_vfs_mkfs_obj, MP_ROM_PTR(&fat_vfs_mkfs_fun_obj));

typedef struct _mp_vfs_fat_ilistdir_it_t {
    mp_obj_base_t base;
    mp_fun_1_t iternext;
    mp_fun_1_t finaliser;
    bool is_str;
    DIR dir;
} mp_vfs_fat_ilistdir_it_t;

static mp_obj_t mp_vfs_fat_ilistdir_it_iternext(mp_obj_t self_in) {
    mp_vfs_fat_ilistdir_it_t *self = MP_OBJ_TO_PTR(self_in);

    for (;;) {
        FILINFO fno;
        FRESULT res = f_readdir(&self->dir, &fno);
        char *fn = fno.fname;
        if (res != FR_OK || fn[0] == 0) {
            // stop on error or end of dir
            break;
        }

        // Note that FatFS already filters . and .., so we don't need to

        // make 4-tuple with info about this entry
        mp_obj_tuple_t *t = MP_OBJ_TO_PTR(mp_obj_new_tuple(4, NULL));
        if (self->is_str) {
            t->items[0] = mp_obj_new_str_from_cstr(fn);
        } else {
            t->items[0] = mp_obj_new_bytes((const byte *)fn, strlen(fn));
        }
        if (fno.fattrib & AM_DIR) {
            // dir
            t->items[1] = MP_OBJ_NEW_SMALL_INT(MP_S_IFDIR);
        } else {
            // file
            t->items[1] = MP_OBJ_NEW_SMALL_INT(MP_S_IFREG);
        }
        t->items[2] = MP_OBJ_NEW_SMALL_INT(0); // no inode number
        t->items[3] = mp_obj_new_int_from_uint(fno.fsize);

        return MP_OBJ_FROM_PTR(t);
    }

    // ignore error because we may be closing a second time
    f_closedir(&self->dir);

    return MP_OBJ_STOP_ITERATION;
}

static mp_obj_t mp_vfs_fat_ilistdir_it_del(mp_obj_t self_in) {
    mp_vfs_fat_ilistdir_it_t *self = MP_OBJ_TO_PTR(self_in);
    // ignore result / error because we may be closing a second time.
    f_closedir(&self->dir);
    return mp_const_none;
}

static mp_obj_t fat_vfs_ilistdir_func(size_t n_args, const mp_obj_t *args) {
    mp_obj_fat_vfs_t *self = MP_OBJ_TO_PTR(args[0]);
    (void)self;
    bool is_str_type = true;
    const char *path;
    if (n_args == 2) {
        if (mp_obj_get_type(args[1]) == &mp_type_bytes) {
            is_str_type = false;
        }
        path = mp_obj_str_get_str(args[1]);
    } else {
        path = "";
    }

    // Create a new iterator object to list the dir
    mp_vfs_fat_ilistdir_it_t *iter = mp_obj_malloc_with_finaliser(mp_vfs_fat_ilistdir_it_t, &mp_type_polymorph_iter_with_finaliser);
    iter->iternext = mp_vfs_fat_ilistdir_it_iternext;
    iter->finaliser = mp_vfs_fat_ilistdir_it_del;
    iter->is_str = is_str_type;
    FRESULT res = f_opendir(&iter->dir, path);
    if (res != FR_OK) {
        mp_raise_OSError(fresult_to_errno_table[res]);
    }

    return MP_OBJ_FROM_PTR(iter);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(fat_vfs_ilistdir_obj, 1, 2, fat_vfs_ilistdir_func);

static mp_obj_t fat_vfs_remove_internal(mp_obj_t vfs_in, mp_obj_t path_in, mp_int_t attr) {
    mp_obj_fat_vfs_t *self = MP_OBJ_TO_PTR(vfs_in);
    (void)self;
    const char *path = mp_obj_str_get_str(path_in);

    FILINFO fno;
    FRESULT res = f_stat(path, &fno);

    if (res != FR_OK) {
        mp_raise_OSError(fresult_to_errno_table[res]);
    }

    // check if path is a file or directory
    if ((fno.fattrib & AM_DIR) == attr) {
        res = f_unlink(path);

        if (res != FR_OK) {
            mp_raise_OSError(fresult_to_errno_table[res]);
        }
        return mp_const_none;
    } else {
        mp_raise_OSError(attr ? MP_ENOTDIR : MP_EISDIR);
    }
}

static mp_obj_t fat_vfs_remove(mp_obj_t vfs_in, mp_obj_t path_in) {
    return fat_vfs_remove_internal(vfs_in, path_in, 0); // 0 == file attribute
}
static MP_DEFINE_CONST_FUN_OBJ_2(fat_vfs_remove_obj, fat_vfs_remove);

static mp_obj_t fat_vfs_rmdir(mp_obj_t vfs_in, mp_obj_t path_in) {
    return fat_vfs_remove_internal(vfs_in, path_in, AM_DIR);
}
static MP_DEFINE_CONST_FUN_OBJ_2(fat_vfs_rmdir_obj, fat_vfs_rmdir);

static mp_obj_t fat_vfs_rename(mp_obj_t vfs_in, mp_obj_t path_in, mp_obj_t path_out) {
    mp_obj_fat_vfs_t *self = MP_OBJ_TO_PTR(vfs_in);
    (void)self;
    const char *old_path = mp_obj_str_get_str(path_in);
    const char *new_path = mp_obj_str_get_str(path_out);
    FRESULT res = f_rename(old_path, new_path);
    if (res == FR_EXIST) {
        // if new_path exists then try removing it (but only if it's a file)
        fat_vfs_remove_internal(vfs_in, path_out, 0); // 0 == file attribute
        // try to rename again
        res = f_rename(old_path, new_path);
    }
    if (res == FR_OK) {
        return mp_const_none;
    } else {
        mp_raise_OSError(fresult_to_errno_table[res]);
    }

}
static MP_DEFINE_CONST_FUN_OBJ_3(fat_vfs_rename_obj, fat_vfs_rename);

static mp_obj_t fat_vfs_mkdir(mp_obj_t vfs_in, mp_obj_t path_o) {
    mp_obj_fat_vfs_t *self = MP_OBJ_TO_PTR(vfs_in);
    (void)self;
    const char *path = mp_obj_str_get_str(path_o);
    FRESULT res = f_mkdir(path);
    if (res == FR_OK) {
        return mp_const_none;
    } else {
        mp_raise_OSError(fresult_to_errno_table[res]);
    }
}
static MP_DEFINE_CONST_FUN_OBJ_2(fat_vfs_mkdir_obj, fat_vfs_mkdir);

// Change current directory.
static mp_obj_t fat_vfs_chdir(mp_obj_t vfs_in, mp_obj_t path_in) {
    mp_obj_fat_vfs_t *self = MP_OBJ_TO_PTR(vfs_in);
    (void)self;
    const char *path;
    path = mp_obj_str_get_str(path_in);

    FRESULT res = f_chdir(path);

    if (res != FR_OK) {
        mp_raise_OSError(fresult_to_errno_table[res]);
    }

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_2(fat_vfs_chdir_obj, fat_vfs_chdir);

// Get the current directory.
static mp_obj_t fat_vfs_getcwd(mp_obj_t vfs_in) {
    mp_obj_fat_vfs_t *self = MP_OBJ_TO_PTR(vfs_in);
    (void)self;
    char buf[MICROPY_ALLOC_PATH_MAX + 1];
    FRESULT res = f_getcwd(buf, sizeof(buf));
    if (res != FR_OK) {
        mp_raise_OSError(fresult_to_errno_table[res]);
    }
    return mp_obj_new_str_from_cstr(buf);
}
static MP_DEFINE_CONST_FUN_OBJ_1(fat_vfs_getcwd_obj, fat_vfs_getcwd);

// Get the status of a file or directory.
static mp_obj_t fat_vfs_stat(mp_obj_t vfs_in, mp_obj_t path_in) {
    mp_obj_fat_vfs_t *self = MP_OBJ_TO_PTR(vfs_in);
    (void)self;
    const char *path = mp_obj_str_get_str(path_in);

    FILINFO fno;
    if (path[0] == 0 || (path[0] == '/' && path[1] == 0)) {
        // stat root directory
        fno.fsize = 0;
        fno.fdate = 0x2821; // Jan 1, 2000
        fno.ftime = 0;
        fno.fattrib = AM_DIR;
    } else {
        FRESULT res = f_stat(path, &fno);
        if (res != FR_OK) {
            mp_raise_OSError(fresult_to_errno_table[res]);
        }
    }

    mp_obj_tuple_t *t = MP_OBJ_TO_PTR(mp_obj_new_tuple(10, NULL));
    mp_int_t mode = 0;
    if (fno.fattrib & AM_DIR) {
        mode |= MP_S_IFDIR;
    } else {
        mode |= MP_S_IFREG;
    }
    mp_int_t seconds = timeutils_seconds_since_epoch(
        1980 + ((fno.fdate >> 9) & 0x7f),
        (fno.fdate >> 5) & 0x0f,
        fno.fdate & 0x1f,
        (fno.ftime >> 11) & 0x1f,
        (fno.ftime >> 5) & 0x3f,
        2 * (fno.ftime & 0x1f)
        );
    t->items[0] = MP_OBJ_NEW_SMALL_INT(mode); // st_mode
    t->items[1] = MP_OBJ_NEW_SMALL_INT(0); // st_ino
    t->items[2] = MP_OBJ_NEW_SMALL_INT(0); // st_dev
    t->items[3] = MP_OBJ_NEW_SMALL_INT(0); // st_nlink
    t->items[4] = MP_OBJ_NEW_SMALL_INT(0); // st_uid
    t->items[5] = MP_OBJ_NEW_SMALL_INT(0); // st_gid
    t->items[6] = mp_obj_new_int_from_uint(fno.fsize); // st_size
    t->items[7] = mp_obj_new_int_from_uint(seconds); // st_atime
    t->items[8] = mp_obj_new_int_from_uint(seconds); // st_mtime
    t->items[9] = mp_obj_new_int_from_uint(seconds); // st_ctime

    return MP_OBJ_FROM_PTR(t);
}
static MP_DEFINE_CONST_FUN_OBJ_2(fat_vfs_stat_obj, fat_vfs_stat);

// Get the status of a VFS.
static mp_obj_t fat_vfs_statvfs(mp_obj_t vfs_in, mp_obj_t path_in) {
    mp_obj_fat_vfs_t *self = MP_OBJ_TO_PTR(vfs_in);
    (void)self;
    (void)path_in;

    DWORD nclst;
    FATFS *fatfs;
    FRESULT res = f_getfree("", &nclst, &fatfs);
    if (FR_OK != res) {
        mp_raise_OSError(fresult_to_errno_table[res]);
    }

    mp_obj_tuple_t *t = MP_OBJ_TO_PTR(mp_obj_new_tuple(10, NULL));

    t->items[0] = MP_OBJ_NEW_SMALL_INT(fatfs->csize * SECSIZE(fatfs)); // f_bsize
    t->items[1] = t->items[0]; // f_frsize
    t->items[2] = MP_OBJ_NEW_SMALL_INT((fatfs->n_fatent - 2)); // f_blocks
    t->items[3] = MP_OBJ_NEW_SMALL_INT(nclst); // f_bfree
    t->items[4] = t->items[3]; // f_bavail
    t->items[5] = MP_OBJ_NEW_SMALL_INT(0); // f_files
    t->items[6] = MP_OBJ_NEW_SMALL_INT(0); // f_ffree
    t->items[7] = MP_OBJ_NEW_SMALL_INT(0); // f_favail
    t->items[8] = MP_OBJ_NEW_SMALL_INT(0); // f_flags
    t->items[9] = MP_OBJ_NEW_SMALL_INT(FF_MAX_LFN); // f_namemax

    return MP_OBJ_FROM_PTR(t);
}
static MP_DEFINE_CONST_FUN_OBJ_2(fat_vfs_statvfs_obj, fat_vfs_statvfs);

static mp_obj_t vfs_fat_mount(mp_obj_t self_in, mp_obj_t readonly, mp_obj_t mkfs) {
    fs_user_mount_t *self = MP_OBJ_TO_PTR(self_in);

    // Read-only device indicated by writeblocks[0] == MP_OBJ_NULL.
    // User can specify read-only device by:
    //  1. readonly=True keyword argument
    //  2. nonexistent writeblocks method (then writeblocks[0] == MP_OBJ_NULL already)
    if (mp_obj_is_true(readonly)) {
        self->blockdev.writeblocks[0] = MP_OBJ_NULL;
    }

    // check if we need to make the filesystem
    FRESULT res = (self->blockdev.flags & MP_BLOCKDEV_FLAG_NO_FILESYSTEM) ? FR_NO_FILESYSTEM : FR_OK;
    if (res == FR_NO_FILESYSTEM && mp_obj_is_true(mkfs)) {
        uint8_t working_buf[FF_MAX_SS];
        res = f_mkfs("", &mkfs_default_opt, working_buf, sizeof(working_buf));
        if (res == FR_OK) {
            /* Refresh in-memory FATFS state after the write. See
             * comment in fat_vfs_mkfs for why this is required. */
            res = f_mount(&self->fatfs, "", 1);
        }
    }
    if (res != FR_OK) {
        mp_raise_OSError(fresult_to_errno_table[res]);
    }
    self->blockdev.flags &= ~MP_BLOCKDEV_FLAG_NO_FILESYSTEM;

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_3(vfs_fat_mount_obj, vfs_fat_mount);

static mp_obj_t vfs_fat_umount(mp_obj_t self_in) {
    fs_user_mount_t *self = MP_OBJ_TO_PTR(self_in);
    (void)self;
    /* keep the FAT filesystem mounted internally so the VFS methods
     * can still be used — matches upstream behaviour. If the user
     * really wants to hard-unmount (e.g. to replace with a second
     * VfsFat), they can delete the object and force GC. */
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(fat_vfs_umount_obj, vfs_fat_umount);

static const mp_rom_map_elem_t fat_vfs_locals_dict_table[] = {
    #if FF_FS_REENTRANT
    { MP_ROM_QSTR(MP_QSTR___del__), MP_ROM_PTR(&fat_vfs_del_obj) },
    #endif
    { MP_ROM_QSTR(MP_QSTR_mkfs), MP_ROM_PTR(&fat_vfs_mkfs_obj) },
    { MP_ROM_QSTR(MP_QSTR_open), MP_ROM_PTR(&fat_vfs_open_obj) },
    { MP_ROM_QSTR(MP_QSTR_ilistdir), MP_ROM_PTR(&fat_vfs_ilistdir_obj) },
    { MP_ROM_QSTR(MP_QSTR_mkdir), MP_ROM_PTR(&fat_vfs_mkdir_obj) },
    { MP_ROM_QSTR(MP_QSTR_rmdir), MP_ROM_PTR(&fat_vfs_rmdir_obj) },
    { MP_ROM_QSTR(MP_QSTR_chdir), MP_ROM_PTR(&fat_vfs_chdir_obj) },
    { MP_ROM_QSTR(MP_QSTR_getcwd), MP_ROM_PTR(&fat_vfs_getcwd_obj) },
    { MP_ROM_QSTR(MP_QSTR_remove), MP_ROM_PTR(&fat_vfs_remove_obj) },
    { MP_ROM_QSTR(MP_QSTR_rename), MP_ROM_PTR(&fat_vfs_rename_obj) },
    { MP_ROM_QSTR(MP_QSTR_stat), MP_ROM_PTR(&fat_vfs_stat_obj) },
    { MP_ROM_QSTR(MP_QSTR_statvfs), MP_ROM_PTR(&fat_vfs_statvfs_obj) },
    { MP_ROM_QSTR(MP_QSTR_mount), MP_ROM_PTR(&vfs_fat_mount_obj) },
    { MP_ROM_QSTR(MP_QSTR_umount), MP_ROM_PTR(&fat_vfs_umount_obj) },
};
static MP_DEFINE_CONST_DICT(fat_vfs_locals_dict, fat_vfs_locals_dict_table);

static const mp_vfs_proto_t fat_vfs_proto = {
    .import_stat = fat_vfs_import_stat,
};

MP_DEFINE_CONST_OBJ_TYPE(
    mp_fat_vfs_type,
    MP_QSTR_VfsFat,
    MP_TYPE_FLAG_NONE,
    make_new, fat_vfs_make_new,
    protocol, &fat_vfs_proto,
    locals_dict, &fat_vfs_locals_dict
    );

#endif // MICROPY_VFS_FAT
