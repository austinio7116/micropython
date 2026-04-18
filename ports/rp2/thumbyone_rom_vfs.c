/*
 * ThumbyOne ROM-backed read-only VFS.
 *
 * Exposes the Tiny Game Engine's /system/ tree (fonts, launcher
 * assets, settings template, splash graphics, crash handler) as a
 * MicroPython VFS that serves straight out of firmware ROM. No FAT
 * space is consumed; /system/ ships with the MPY slot firmware and
 * is accessed via the regular MicroPython VFS layer, so both Python
 * (`open('/system/...')`) and the engine's C-level mp_stream loaders
 * (FontResource, TextureResource, etc.) see /system/ transparently.
 *
 * The byte blob + entry table come from pack_system_rom.py; it walks
 * the engine's filesystem/system/ directory at build time and emits
 * tbyone_system_blob[] + tbyone_system_entries[] in a generated .c
 * file (build_device/mpy_slot/tbyone_system_data.c or similar).
 *
 * Usage from Python (mounted in _boot_fat.py):
 *
 *   import vfs
 *   from thumbyone_rom import ThumbyOneRomVFS
 *   vfs.mount(ThumbyOneRomVFS(), '/system', readonly=True)
 *
 * After that, `open('/system/assets/outrunner_outline.bmp')` returns
 * a read-only file-like object backed by the firmware ROM. Writes
 * raise OSError(EROFS).
 */

#include "py/obj.h"
#include "py/objtuple.h"
#include "py/runtime.h"
#include "py/stream.h"
#include "py/mperrno.h"
#include "extmod/vfs.h"

#include <string.h>

/* -------- Generated data (see pack_system_rom.py output) ---------- */

typedef struct {
    const char *path;    /* relative to the mount point — e.g. "/assets/foo.bmp" */
    uint32_t    offset;  /* byte offset into tbyone_system_blob (0 if dir) */
    uint32_t    length;  /* byte length (0 if dir) */
    uint8_t     type;    /* 0 = file, 1 = dir */
} tbyone_rom_entry_t;

extern const uint8_t           tbyone_system_blob[];
extern const tbyone_rom_entry_t tbyone_system_entries[];
extern const size_t            tbyone_system_entry_count;

/* -------- Entry lookup --------------------------------------------- */

/* Normalise an incoming path before lookup. Accepts NULL → "/".
 * Strips trailing slashes except for the root itself. Copies into
 * caller-provided buffer; returns pointer to buffer, or NULL if the
 * input overflowed. */
static const char *normalise_path(const char *in, char *out, size_t out_size) {
    if (in == NULL || in[0] == 0) {
        if (out_size < 2) return NULL;
        out[0] = '/';
        out[1] = 0;
        return out;
    }
    size_t n = strlen(in);
    if (n >= out_size) return NULL;
    memcpy(out, in, n + 1);
    /* Strip trailing '/' (but preserve the bare root "/"). */
    while (n > 1 && out[n - 1] == '/') {
        out[--n] = 0;
    }
    return out;
}

static const tbyone_rom_entry_t *find_entry(const char *path) {
    char buf[128];
    const char *p = normalise_path(path, buf, sizeof(buf));
    if (p == NULL) return NULL;
    for (size_t i = 0; i < tbyone_system_entry_count; ++i) {
        if (strcmp(p, tbyone_system_entries[i].path) == 0) {
            return &tbyone_system_entries[i];
        }
    }
    return NULL;
}

/* -------- File object (read-only stream) --------------------------- */

typedef struct _tbyone_rom_file_obj_t {
    mp_obj_base_t base;
    const uint8_t *data;
    uint32_t       length;
    uint32_t       pos;
} tbyone_rom_file_obj_t;

extern const mp_obj_type_t tbyone_rom_file_type;

static mp_uint_t rom_file_read(mp_obj_t o, void *buf, mp_uint_t size, int *errcode) {
    tbyone_rom_file_obj_t *self = MP_OBJ_TO_PTR(o);
    uint32_t remaining = self->length - self->pos;
    if (size > remaining) size = remaining;
    memcpy(buf, self->data + self->pos, size);
    self->pos += (uint32_t)size;
    return size;
}

static mp_uint_t rom_file_write(mp_obj_t o, const void *buf, mp_uint_t size, int *errcode) {
    (void)o; (void)buf; (void)size;
    *errcode = MP_EROFS;
    return MP_STREAM_ERROR;
}

static mp_uint_t rom_file_ioctl(mp_obj_t o, mp_uint_t request, uintptr_t arg, int *errcode) {
    tbyone_rom_file_obj_t *self = MP_OBJ_TO_PTR(o);
    switch (request) {
        case MP_STREAM_SEEK: {
            struct mp_stream_seek_t *s = (struct mp_stream_seek_t *)(uintptr_t)arg;
            mp_int_t base;
            switch (s->whence) {
                case 0: base = 0; break;                /* SEEK_SET */
                case 1: base = self->pos; break;        /* SEEK_CUR */
                case 2: base = self->length; break;     /* SEEK_END */
                default: *errcode = MP_EINVAL; return MP_STREAM_ERROR;
            }
            mp_int_t new_pos = base + (mp_int_t)s->offset;
            if (new_pos < 0) new_pos = 0;
            if (new_pos > (mp_int_t)self->length) new_pos = self->length;
            self->pos = (uint32_t)new_pos;
            s->offset = self->pos;
            return 0;
        }
        case MP_STREAM_FLUSH:
        case MP_STREAM_CLOSE:
            return 0;
        default:
            *errcode = MP_EINVAL;
            return MP_STREAM_ERROR;
    }
}

static void rom_file_print(const mp_print_t *print, mp_obj_t self_in, mp_print_kind_t kind) {
    (void)kind;
    tbyone_rom_file_obj_t *self = MP_OBJ_TO_PTR(self_in);
    mp_printf(print, "<RomFile %u/%u>", (unsigned)self->pos, (unsigned)self->length);
}

static const mp_rom_map_elem_t rom_file_locals_dict_table[] = {
    { MP_ROM_QSTR(MP_QSTR_read),      MP_ROM_PTR(&mp_stream_read_obj) },
    { MP_ROM_QSTR(MP_QSTR_readinto),  MP_ROM_PTR(&mp_stream_readinto_obj) },
    { MP_ROM_QSTR(MP_QSTR_readline),  MP_ROM_PTR(&mp_stream_unbuffered_readline_obj) },
    { MP_ROM_QSTR(MP_QSTR_seek),      MP_ROM_PTR(&mp_stream_seek_obj) },
    { MP_ROM_QSTR(MP_QSTR_tell),      MP_ROM_PTR(&mp_stream_tell_obj) },
    { MP_ROM_QSTR(MP_QSTR_close),     MP_ROM_PTR(&mp_stream_close_obj) },
    { MP_ROM_QSTR(MP_QSTR___enter__), MP_ROM_PTR(&mp_identity_obj) },
    { MP_ROM_QSTR(MP_QSTR___exit__),  MP_ROM_PTR(&mp_stream___exit___obj) },
};
static MP_DEFINE_CONST_DICT(rom_file_locals_dict, rom_file_locals_dict_table);

static const mp_stream_p_t rom_file_stream_p = {
    .read  = rom_file_read,
    .write = rom_file_write,
    .ioctl = rom_file_ioctl,
    .is_text = false,
};

MP_DEFINE_CONST_OBJ_TYPE(
    tbyone_rom_file_type,
    MP_QSTR_RomFile,
    MP_TYPE_FLAG_ITER_IS_STREAM,
    print,       rom_file_print,
    protocol,    &rom_file_stream_p,
    locals_dict, &rom_file_locals_dict
);

/* -------- VFS object ----------------------------------------------- */

typedef struct _tbyone_rom_vfs_obj_t {
    mp_obj_base_t base;
} tbyone_rom_vfs_obj_t;

extern const mp_obj_type_t tbyone_rom_vfs_type;

static mp_obj_t rom_vfs_make_new(const mp_obj_type_t *type, size_t n_args, size_t n_kw, const mp_obj_t *args) {
    (void)n_args; (void)n_kw; (void)args;
    tbyone_rom_vfs_obj_t *self = mp_obj_malloc(tbyone_rom_vfs_obj_t, type);
    return MP_OBJ_FROM_PTR(self);
}

static mp_obj_t rom_vfs_mount(mp_obj_t self_in, mp_obj_t readonly, mp_obj_t mkfs) {
    (void)self_in; (void)readonly; (void)mkfs;
    /* Always read-only. Nothing to do at mount time. */
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_3(rom_vfs_mount_obj, rom_vfs_mount);

static mp_obj_t rom_vfs_umount(mp_obj_t self_in) {
    (void)self_in;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(rom_vfs_umount_obj, rom_vfs_umount);

static mp_obj_t rom_vfs_open(mp_obj_t self_in, mp_obj_t path_in, mp_obj_t mode_in) {
    (void)self_in;
    const char *path = mp_obj_str_get_str(path_in);
    const char *mode = mp_obj_str_get_str(mode_in);

    /* Reject any write intent. */
    for (const char *c = mode; *c; ++c) {
        if (*c == 'w' || *c == 'a' || *c == '+' || *c == 'x') {
            mp_raise_OSError(MP_EROFS);
        }
    }

    const tbyone_rom_entry_t *e = find_entry(path);
    if (e == NULL || e->type != 0) {
        mp_raise_OSError(MP_ENOENT);
    }

    tbyone_rom_file_obj_t *f = mp_obj_malloc(tbyone_rom_file_obj_t, &tbyone_rom_file_type);
    f->data   = tbyone_system_blob + e->offset;
    f->length = e->length;
    f->pos    = 0;
    return MP_OBJ_FROM_PTR(f);
}
static MP_DEFINE_CONST_FUN_OBJ_3(rom_vfs_open_obj, rom_vfs_open);

static mp_obj_t rom_vfs_stat(mp_obj_t self_in, mp_obj_t path_in) {
    (void)self_in;
    const char *path = mp_obj_str_get_str(path_in);
    const tbyone_rom_entry_t *e = find_entry(path);
    if (e == NULL) mp_raise_OSError(MP_ENOENT);

    mp_obj_tuple_t *t = MP_OBJ_TO_PTR(mp_obj_new_tuple(10, NULL));
    t->items[0] = MP_OBJ_NEW_SMALL_INT(e->type == 1 ? MP_S_IFDIR : MP_S_IFREG);
    t->items[1] = MP_OBJ_NEW_SMALL_INT(0); /* ino */
    t->items[2] = MP_OBJ_NEW_SMALL_INT(0); /* dev */
    t->items[3] = MP_OBJ_NEW_SMALL_INT(0); /* nlink */
    t->items[4] = MP_OBJ_NEW_SMALL_INT(0); /* uid */
    t->items[5] = MP_OBJ_NEW_SMALL_INT(0); /* gid */
    t->items[6] = mp_obj_new_int_from_uint(e->length); /* size */
    t->items[7] = MP_OBJ_NEW_SMALL_INT(0); /* atime */
    t->items[8] = MP_OBJ_NEW_SMALL_INT(0); /* mtime */
    t->items[9] = MP_OBJ_NEW_SMALL_INT(0); /* ctime */
    return MP_OBJ_FROM_PTR(t);
}
static MP_DEFINE_CONST_FUN_OBJ_2(rom_vfs_stat_obj, rom_vfs_stat);

/* ilistdir iterator: yields (name, type, ino, size) tuples for each
 * direct child of `path`. Implements its own iterator so we don't
 * materialise the whole list. */

typedef struct _rom_ilistdir_iter_t {
    mp_obj_base_t base;
    mp_fun_1_t    iternext;
    size_t        next_idx;        /* scan position in the entry table */
    size_t        prefix_len;      /* length of the parent path including trailing '/' */
    char          prefix[128];     /* normalised parent path + '/' */
} rom_ilistdir_iter_t;

static mp_obj_t rom_ilistdir_iternext(mp_obj_t self_in) {
    rom_ilistdir_iter_t *self = MP_OBJ_TO_PTR(self_in);

    while (self->next_idx < tbyone_system_entry_count) {
        const tbyone_rom_entry_t *e = &tbyone_system_entries[self->next_idx++];
        /* Skip the parent directory itself. */
        size_t path_len = strlen(e->path);
        if (path_len <= self->prefix_len) continue;
        /* Child must start with prefix. */
        if (strncmp(e->path, self->prefix, self->prefix_len) != 0) continue;
        /* Must be a direct child — no '/' after the prefix. */
        const char *tail = e->path + self->prefix_len;
        if (strchr(tail, '/') != NULL) continue;

        mp_obj_tuple_t *t = MP_OBJ_TO_PTR(mp_obj_new_tuple(4, NULL));
        t->items[0] = mp_obj_new_str_from_cstr(tail);
        t->items[1] = MP_OBJ_NEW_SMALL_INT(e->type == 1 ? MP_S_IFDIR : MP_S_IFREG);
        t->items[2] = MP_OBJ_NEW_SMALL_INT(0);             /* inode */
        t->items[3] = mp_obj_new_int_from_uint(e->length); /* size */
        return MP_OBJ_FROM_PTR(t);
    }
    return MP_OBJ_STOP_ITERATION;
}

static mp_obj_t rom_vfs_ilistdir_func(size_t n_args, const mp_obj_t *args) {
    (void)args;  /* we don't need self */
    const char *path = (n_args == 2) ? mp_obj_str_get_str(args[1]) : "/";

    char norm[128];
    if (normalise_path(path, norm, sizeof(norm)) == NULL) {
        mp_raise_OSError(MP_EINVAL);
    }

    const tbyone_rom_entry_t *e = find_entry(norm);
    if (e == NULL || e->type != 1) mp_raise_OSError(MP_ENOTDIR);

    rom_ilistdir_iter_t *it = m_new_obj(rom_ilistdir_iter_t);
    it->base.type = &mp_type_polymorph_iter;
    it->iternext  = rom_ilistdir_iternext;
    it->next_idx  = 0;

    size_t plen = strlen(norm);
    if (plen == 1 && norm[0] == '/') {
        /* Root: children are any path with exactly one '/' at index 0. */
        it->prefix[0] = '/';
        it->prefix[1] = 0;
        it->prefix_len = 1;
    } else {
        /* Non-root: prefix = normalised path + '/'. */
        if (plen + 2 >= sizeof(it->prefix)) mp_raise_OSError(MP_EINVAL);
        memcpy(it->prefix, norm, plen);
        it->prefix[plen]     = '/';
        it->prefix[plen + 1] = 0;
        it->prefix_len = plen + 1;
    }

    return MP_OBJ_FROM_PTR(it);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(rom_vfs_ilistdir_obj, 1, 2, rom_vfs_ilistdir_func);

/* Write-side methods — all raise EROFS. Needed so vfs.mkdir,
 * vfs.remove etc. don't produce confusing AttributeErrors when
 * code accidentally tries them on the read-only volume. */
static mp_obj_t rom_vfs_rofs(mp_obj_t a, mp_obj_t b) {
    (void)a; (void)b;
    mp_raise_OSError(MP_EROFS);
}
static mp_obj_t rom_vfs_rofs1(mp_obj_t a) {
    (void)a;
    mp_raise_OSError(MP_EROFS);
}
static MP_DEFINE_CONST_FUN_OBJ_2(rom_vfs_rofs2_obj, rom_vfs_rofs);
static MP_DEFINE_CONST_FUN_OBJ_1(rom_vfs_rofs1_obj, rom_vfs_rofs1);

static mp_import_stat_t rom_vfs_import_stat(void *self_in, const char *path) {
    (void)self_in;
    const tbyone_rom_entry_t *e = find_entry(path);
    if (e == NULL) return MP_IMPORT_STAT_NO_EXIST;
    return (e->type == 1) ? MP_IMPORT_STAT_DIR : MP_IMPORT_STAT_FILE;
}

static const mp_rom_map_elem_t rom_vfs_locals_dict_table[] = {
    { MP_ROM_QSTR(MP_QSTR_mount),    MP_ROM_PTR(&rom_vfs_mount_obj) },
    { MP_ROM_QSTR(MP_QSTR_umount),   MP_ROM_PTR(&rom_vfs_umount_obj) },
    { MP_ROM_QSTR(MP_QSTR_open),     MP_ROM_PTR(&rom_vfs_open_obj) },
    { MP_ROM_QSTR(MP_QSTR_stat),     MP_ROM_PTR(&rom_vfs_stat_obj) },
    { MP_ROM_QSTR(MP_QSTR_ilistdir), MP_ROM_PTR(&rom_vfs_ilistdir_obj) },
    /* read-only — the write-side methods raise EROFS */
    { MP_ROM_QSTR(MP_QSTR_mkdir),    MP_ROM_PTR(&rom_vfs_rofs1_obj) },
    { MP_ROM_QSTR(MP_QSTR_rmdir),    MP_ROM_PTR(&rom_vfs_rofs1_obj) },
    { MP_ROM_QSTR(MP_QSTR_remove),   MP_ROM_PTR(&rom_vfs_rofs1_obj) },
    { MP_ROM_QSTR(MP_QSTR_rename),   MP_ROM_PTR(&rom_vfs_rofs2_obj) },
};
static MP_DEFINE_CONST_DICT(rom_vfs_locals_dict, rom_vfs_locals_dict_table);

static const mp_vfs_proto_t rom_vfs_proto = {
    .import_stat = rom_vfs_import_stat,
};

MP_DEFINE_CONST_OBJ_TYPE(
    tbyone_rom_vfs_type,
    MP_QSTR_ThumbyOneRomVFS,
    MP_TYPE_FLAG_NONE,
    make_new,    rom_vfs_make_new,
    protocol,    &rom_vfs_proto,
    locals_dict, &rom_vfs_locals_dict
);

/* -------- Module registration -------------------------------------- */

static const mp_rom_map_elem_t thumbyone_rom_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),          MP_ROM_QSTR(MP_QSTR_thumbyone_rom) },
    { MP_ROM_QSTR(MP_QSTR_ThumbyOneRomVFS),   MP_ROM_PTR(&tbyone_rom_vfs_type) },
};
static MP_DEFINE_CONST_DICT(thumbyone_rom_module_globals, thumbyone_rom_module_globals_table);

const mp_obj_module_t mp_module_thumbyone_rom = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&thumbyone_rom_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_thumbyone_rom, mp_module_thumbyone_rom);
