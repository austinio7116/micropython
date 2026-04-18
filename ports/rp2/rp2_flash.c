/*
 * This file is part of the MicroPython project, http://micropython.org/
 *
 * The MIT License (MIT)
 *
 * Copyright (c) 2020-2021 Damien P. George
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

#include <string.h>

#include "py/mphal.h"
#include "py/runtime.h"
#include "extmod/vfs.h"
#include "modrp2.h"
#include "hardware/flash.h"
#include "pico/binary_info.h"

/* LOGICAL_BLOCK_SIZE is what we report to MicroPython's block-device
 * layer and, via vfs_fat_diskio's GET_SECTOR_SIZE, what FatFs sees.
 *
 * 512 bytes matches ThumbyNES / ThumbyP8 / lobby conventions so the
 * shared FAT at physical flash 0x660000 has ONE canonical on-disk
 * format across every slot — lobby's 512-byte-sector FAT16 mount
 * parses cleanly from MPY too, which was the C2c follow-up.
 *
 * FLASH_ERASE_BLOCK is the underlying flash granularity (4 KB). Writes
 * that don't cover a full 4 KB erase block go through a read-modify-
 * erase-program path using a static RAM buffer.
 *
 * KEEP_BLOCK_SIZE_BYTES is the granularity we still require for
 * partition alignment when constructing a sub-range rp2.Flash — users
 * pass `start` and `len` that refer to flash regions, not to logical
 * sectors, so those need to stay 4 KB aligned (one erase block). */
#define LOGICAL_BLOCK_SIZE    512u
#define FLASH_ERASE_BLOCK     FLASH_SECTOR_SIZE        /* 4096 */
#define SECTORS_PER_ERASE     (FLASH_ERASE_BLOCK / LOGICAL_BLOCK_SIZE)
#define BLOCK_SIZE_BYTES      FLASH_ERASE_BLOCK        /* legacy alias for the
                                                         partition-alignment
                                                         checks in make_new */

#ifndef MICROPY_HW_FLASH_STORAGE_BYTES
#define MICROPY_HW_FLASH_STORAGE_BYTES (1408 * 1024)
#endif
static_assert(MICROPY_HW_FLASH_STORAGE_BYTES % 4096 == 0, "Flash storage size must be a multiple of 4K");

#ifndef MICROPY_HW_FLASH_STORAGE_BASE
#define MICROPY_HW_FLASH_STORAGE_BASE (PICO_FLASH_SIZE_BYTES - MICROPY_HW_FLASH_STORAGE_BYTES)
#endif

static_assert(MICROPY_HW_FLASH_STORAGE_BYTES <= PICO_FLASH_SIZE_BYTES, "MICROPY_HW_FLASH_STORAGE_BYTES too big");
static_assert(MICROPY_HW_FLASH_STORAGE_BASE + MICROPY_HW_FLASH_STORAGE_BYTES <= PICO_FLASH_SIZE_BYTES, "MICROPY_HW_FLASH_STORAGE_BYTES too big");

typedef struct _rp2_flash_obj_t {
    mp_obj_base_t base;
    uint32_t flash_base;
    uint32_t flash_size;
} rp2_flash_obj_t;

static rp2_flash_obj_t rp2_flash_obj = {
    .base = { &rp2_flash_type },
    .flash_base = MICROPY_HW_FLASH_STORAGE_BASE,
    .flash_size = MICROPY_HW_FLASH_STORAGE_BYTES,
};

// Tag the flash drive in the binary as readable/writable (but not reformatable)
bi_decl(bi_block_device(
    BINARY_INFO_TAG_MICROPYTHON,
    "MicroPython",
    XIP_BASE + MICROPY_HW_FLASH_STORAGE_BASE,
    MICROPY_HW_FLASH_STORAGE_BYTES,
    NULL,
    BINARY_INFO_BLOCK_DEV_FLAG_READ |
    BINARY_INFO_BLOCK_DEV_FLAG_WRITE |
    BINARY_INFO_BLOCK_DEV_FLAG_PT_UNKNOWN));

#ifdef THUMBYONE_SLOT_MODE
#include "hardware/structs/qmi.h"
#include "thumbyone_handoff.h"
/* Single-slot scratch: the critical flash section locks out core 1,
 * so core 0 has exclusive access while this is live. */
static uint32_t s_saved_atrans[4];
#endif

// Flash erase and write must run with interrupts disabled and the other core suspended,
// because the XIP bit gets disabled.
static uint32_t begin_critical_flash_section(void) {
    if (multicore_lockout_victim_is_initialized(1 - get_core_num())) {
        multicore_lockout_start_blocking();
    }
    uint32_t ints = save_and_disable_interrupts();
#ifdef THUMBYONE_SLOT_MODE
    /* SDK flash_range_erase / flash_range_program reset QMI ATRANS
     * and the fast-XIP config on return. Save now; restore on
     * end_critical_flash_section. Without this, the next instruction
     * fetch reads from the wrong physical address (no ATRANS) and
     * crashes the chained MPY image. */
    s_saved_atrans[0] = qmi_hw->atrans[0];
    s_saved_atrans[1] = qmi_hw->atrans[1];
    s_saved_atrans[2] = qmi_hw->atrans[2];
    s_saved_atrans[3] = qmi_hw->atrans[3];
#endif
    return ints;
}

static void end_critical_flash_section(uint32_t state) {
#ifdef THUMBYONE_SLOT_MODE
    qmi_hw->atrans[0] = s_saved_atrans[0];
    qmi_hw->atrans[1] = s_saved_atrans[1];
    qmi_hw->atrans[2] = s_saved_atrans[2];
    qmi_hw->atrans[3] = s_saved_atrans[3];
    thumbyone_xip_fast_setup();
#endif
    restore_interrupts(state);
    if (multicore_lockout_victim_is_initialized(1 - get_core_num())) {
        multicore_lockout_end_blocking();
    }
}

static mp_obj_t rp2_flash_make_new(const mp_obj_type_t *type, size_t n_args, size_t n_kw, const mp_obj_t *all_args) {
    // Parse arguments
    enum { ARG_start, ARG_len };
    static const mp_arg_t allowed_args[] = {
        { MP_QSTR_start, MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = -1} },
        { MP_QSTR_len,   MP_ARG_KW_ONLY | MP_ARG_INT, {.u_int = -1} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed_args)];
    mp_arg_parse_all_kw_array(n_args, n_kw, all_args, MP_ARRAY_SIZE(allowed_args), allowed_args, args);

    if (args[ARG_start].u_int == -1 && args[ARG_len].u_int == -1) {
        #ifndef NDEBUG
        extern char __flash_binary_end;
        assert((uintptr_t)&__flash_binary_end - XIP_BASE <= MICROPY_HW_FLASH_STORAGE_BASE);
        #endif

        // Default singleton object that accesses entire flash
        return MP_OBJ_FROM_PTR(&rp2_flash_obj);
    }

    rp2_flash_obj_t *self = mp_obj_malloc(rp2_flash_obj_t, &rp2_flash_type);

    mp_int_t start = args[ARG_start].u_int;
    if (start == -1) {
        start = 0;
    } else if (!(0 <= start && start < MICROPY_HW_FLASH_STORAGE_BYTES && start % BLOCK_SIZE_BYTES == 0)) {
        mp_raise_ValueError(NULL);
    }

    mp_int_t len = args[ARG_len].u_int;
    if (len == -1) {
        len = MICROPY_HW_FLASH_STORAGE_BYTES - start;
    } else if (!(0 < len && start + len <= MICROPY_HW_FLASH_STORAGE_BYTES && len % BLOCK_SIZE_BYTES == 0)) {
        mp_raise_ValueError(NULL);
    }

    self->flash_base = MICROPY_HW_FLASH_STORAGE_BASE + start;
    self->flash_size = len;

    return MP_OBJ_FROM_PTR(self);
}

static mp_obj_t rp2_flash_readblocks(size_t n_args, const mp_obj_t *args) {
    rp2_flash_obj_t *self = MP_OBJ_TO_PTR(args[0]);
    /* block_num is in LOGICAL_BLOCK_SIZE units (512 bytes). */
    uint32_t offset = mp_obj_get_int(args[1]) * LOGICAL_BLOCK_SIZE;
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(args[2], &bufinfo, MP_BUFFER_WRITE);
    if (n_args == 4) {
        offset += mp_obj_get_int(args[3]);
    }
    memcpy(bufinfo.buf, (void *)(XIP_BASE + self->flash_base + offset), bufinfo.len);
    // mp_event_handle_nowait() is called here to avoid a fail in registering
    // USB at boot time, if the board is busy loading files or scanning the file
    // system. mp_event_handle_nowait() will call the TinyUSB task if needed.
    mp_event_handle_nowait();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(rp2_flash_readblocks_obj, 3, 4, rp2_flash_readblocks);

/* Read-modify-erase-program buffer for sub-erase-size writes. One
 * erase block's worth. Static because a 4 KB stack frame would push
 * the MP runtime close to its limits on some call paths. Single-
 * threaded access is enforced by begin_critical_flash_section
 * locking core 1 out, so no synchronisation needed here. */
static uint8_t s_rmw_buf[FLASH_ERASE_BLOCK];

/* Erase + program a single 4 KB flash block, substituting `len`
 * bytes starting at `offset_in_block` with `src`. If the write
 * covers the whole block we skip the XIP preload step. */
static void rmw_one_block(uint32_t flash_block_addr,
                          uint32_t offset_in_block,
                          const uint8_t *src,
                          uint32_t len) {
    if (!(offset_in_block == 0 && len == FLASH_ERASE_BLOCK)) {
        /* Preload existing block contents from XIP before we erase.
         * XIP sees whatever's currently committed to flash — safe
         * because we are the only writer (critical section gates
         * concurrent core-1 flash access). */
        memcpy(s_rmw_buf,
               (const void *)(XIP_BASE + flash_block_addr),
               FLASH_ERASE_BLOCK);
        memcpy(s_rmw_buf + offset_in_block, src, len);
        src = s_rmw_buf;
        len = FLASH_ERASE_BLOCK;
        offset_in_block = 0;
    }
    uint32_t atomic_state = begin_critical_flash_section();
    flash_range_erase(flash_block_addr, FLASH_ERASE_BLOCK);
    flash_range_program(flash_block_addr + offset_in_block, src, len);
    end_critical_flash_section(atomic_state);
}

static mp_obj_t rp2_flash_writeblocks(size_t n_args, const mp_obj_t *args) {
    rp2_flash_obj_t *self = MP_OBJ_TO_PTR(args[0]);
    /* block_num is in LOGICAL_BLOCK_SIZE units. */
    uint32_t offset = mp_obj_get_int(args[1]) * LOGICAL_BLOCK_SIZE;
    mp_buffer_info_t bufinfo;
    mp_get_buffer_raise(args[2], &bufinfo, MP_BUFFER_READ);

    if (n_args == 3) {
        /* 3-arg form: erase-then-program. MicroPython's blockdev
         * contract says any prior contents of the affected range
         * are discarded, so for 512-byte logical sectors we need
         * to walk the range erase-block at a time, preserving the
         * UNTOUCHED bytes in any partially-overlapped 4 KB block. */
        const uint8_t *src = bufinfo.buf;
        uint32_t remaining = bufinfo.len;
        uint32_t cur_offset = offset;
        while (remaining > 0) {
            uint32_t block_addr     = self->flash_base
                                    + (cur_offset / FLASH_ERASE_BLOCK) * FLASH_ERASE_BLOCK;
            uint32_t offset_in_blk  = cur_offset % FLASH_ERASE_BLOCK;
            uint32_t space_in_blk   = FLASH_ERASE_BLOCK - offset_in_blk;
            uint32_t chunk          = (remaining < space_in_blk) ? remaining : space_in_blk;

            rmw_one_block(block_addr, offset_in_blk, src, chunk);

            cur_offset += chunk;
            src        += chunk;
            remaining  -= chunk;
        }
        mp_event_handle_nowait();
    } else {
        /* 4-arg form: program-only, no erase. Caller guarantees the
         * target region is already erased (all 0xFF) and wants an
         * overlay write. flash_range_program supports any offset
         * that's a multiple of FLASH_PAGE_SIZE (256 bytes); our
         * 512-byte sectors always satisfy this. */
        offset += mp_obj_get_int(args[3]);
        uint32_t atomic_state = begin_critical_flash_section();
        flash_range_program(self->flash_base + offset, bufinfo.buf, bufinfo.len);
        end_critical_flash_section(atomic_state);
        mp_event_handle_nowait();
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(rp2_flash_writeblocks_obj, 3, 4, rp2_flash_writeblocks);

static mp_obj_t rp2_flash_ioctl(mp_obj_t self_in, mp_obj_t cmd_in, mp_obj_t arg_in) {
    rp2_flash_obj_t *self = MP_OBJ_TO_PTR(self_in);
    mp_int_t cmd = mp_obj_get_int(cmd_in);
    switch (cmd) {
        case MP_BLOCKDEV_IOCTL_INIT:
            return MP_OBJ_NEW_SMALL_INT(0);
        case MP_BLOCKDEV_IOCTL_DEINIT:
            return MP_OBJ_NEW_SMALL_INT(0);
        case MP_BLOCKDEV_IOCTL_SYNC:
            return MP_OBJ_NEW_SMALL_INT(0);
        case MP_BLOCKDEV_IOCTL_BLOCK_COUNT:
            /* Sector count in LOGICAL_BLOCK_SIZE units. */
            return MP_OBJ_NEW_SMALL_INT(self->flash_size / LOGICAL_BLOCK_SIZE);
        case MP_BLOCKDEV_IOCTL_BLOCK_SIZE:
            /* Logical sector size presented to FatFs and MSC. */
            return MP_OBJ_NEW_SMALL_INT(LOGICAL_BLOCK_SIZE);
        case MP_BLOCKDEV_IOCTL_BLOCK_ERASE: {
            /* Erase the 4 KB flash block containing the addressed
             * logical sector. `arg_in` is in LOGICAL_BLOCK_SIZE
             * units; multiply and round down to erase alignment. */
            uint32_t logical_offset = mp_obj_get_int(arg_in) * LOGICAL_BLOCK_SIZE;
            uint32_t block_addr = self->flash_base
                                + (logical_offset / FLASH_ERASE_BLOCK) * FLASH_ERASE_BLOCK;
            mp_uint_t atomic_state = begin_critical_flash_section();
            flash_range_erase(block_addr, FLASH_ERASE_BLOCK);
            end_critical_flash_section(atomic_state);
            // TODO check return value
            return MP_OBJ_NEW_SMALL_INT(0);
        }
        default:
            return mp_const_none;
    }
}
static MP_DEFINE_CONST_FUN_OBJ_3(rp2_flash_ioctl_obj, rp2_flash_ioctl);

static const mp_rom_map_elem_t rp2_flash_locals_dict_table[] = {
    { MP_ROM_QSTR(MP_QSTR_readblocks), MP_ROM_PTR(&rp2_flash_readblocks_obj) },
    { MP_ROM_QSTR(MP_QSTR_writeblocks), MP_ROM_PTR(&rp2_flash_writeblocks_obj) },
    { MP_ROM_QSTR(MP_QSTR_ioctl), MP_ROM_PTR(&rp2_flash_ioctl_obj) },
};
static MP_DEFINE_CONST_DICT(rp2_flash_locals_dict, rp2_flash_locals_dict_table);

MP_DEFINE_CONST_OBJ_TYPE(
    rp2_flash_type,
    MP_QSTR_Flash,
    MP_TYPE_FLAG_NONE,
    make_new, rp2_flash_make_new,
    locals_dict, &rp2_flash_locals_dict
    );
