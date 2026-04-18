/*
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
 *
 */
#include "tusb.h"
#if CFG_TUD_MSC
#include "mpconfigboard.h"
#include "hardware/flash.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"

/* ThumbyOne slot mode: expose 512-byte logical sectors to USB MSC
 * so the host sees the same drive layout as NES/P8/lobby (single
 * canonical on-disk format across every slot). Writes that don't
 * cover a full 4 KB flash erase block go through read-modify-
 * erase-program using a static 4 KB buffer.
 *
 * BLOCK_SIZE is the LOGICAL block reported to the host via SCSI
 * READ_CAPACITY; FLASH_ERASE_SIZE is the underlying flash erase
 * granularity. Older mp-thumby msc_disk.c assumed these were equal
 * and wrote full 4 KB erase blocks per READ/WRITE — that forced
 * MSC to expose 4 KB sectors, which mismatches the shared-FAT
 * 512-byte format and made Windows prompt "drive needs formatting"
 * on every MPY boot. */
#define BLOCK_SIZE          (512u)
#define FLASH_ERASE_SIZE    (FLASH_SECTOR_SIZE)   /* 4096 */
#define SECTORS_PER_ERASE   (FLASH_ERASE_SIZE / BLOCK_SIZE)
#define BLOCK_COUNT         (MICROPY_HW_FLASH_STORAGE_BYTES / BLOCK_SIZE)
#define FLASH_BASE_ADDR     (PICO_FLASH_SIZE_BYTES - MICROPY_HW_FLASH_STORAGE_BYTES)
#define FLASH_MMAP_ADDR     (XIP_BASE + FLASH_BASE_ADDR)

static bool ejected = false;

// Invoked when received SCSI_CMD_INQUIRY
// Application fill vendor id, product id and revision with string up to 8, 16, 4 characters respectively
void tud_msc_inquiry_cb(uint8_t lun, uint8_t vendor_id[8], uint8_t product_id[16], uint8_t product_rev[4]) {
    memcpy(vendor_id, MICROPY_HW_USB_MSC_INQUIRY_VENDOR_STRING, MIN(strlen(MICROPY_HW_USB_MSC_INQUIRY_VENDOR_STRING), 8));
    memcpy(product_id, MICROPY_HW_USB_MSC_INQUIRY_PRODUCT_STRING, MIN(strlen(MICROPY_HW_USB_MSC_INQUIRY_PRODUCT_STRING), 16));
    memcpy(product_rev, MICROPY_HW_USB_MSC_INQUIRY_REVISION_STRING, MIN(strlen(MICROPY_HW_USB_MSC_INQUIRY_REVISION_STRING), 4));
}

// Invoked when received Test Unit Ready command.
// return true allowing host to read/write this LUN e.g SD card inserted
bool tud_msc_test_unit_ready_cb(uint8_t lun) {
    if (ejected) {
        tud_msc_set_sense(lun, SCSI_SENSE_NOT_READY, 0x3a, 0x00);
        return false;
    }
    return true;
}

// Invoked when received SCSI_CMD_READ_CAPACITY_10 and SCSI_CMD_READ_FORMAT_CAPACITY to determine the disk size
// Application update block count and block size
void tud_msc_capacity_cb(uint8_t lun, uint32_t *block_count, uint16_t *block_size) {
    *block_size = BLOCK_SIZE;
    *block_count = BLOCK_COUNT;
}

// Invoked when received Start Stop Unit command
// - Start = 0 : stopped power mode, if load_eject = 1 : unload disk storage
// - Start = 1 : active mode, if load_eject = 1 : load disk storage
bool tud_msc_start_stop_cb(uint8_t lun, uint8_t power_condition, bool start, bool load_eject) {
    if (load_eject) {
        if (start) {
            // load disk storage
            ejected = false;
        } else {
            // unload disk storage
            ejected = true;
        }
    }
    return true;
}

// Callback invoked when received READ10 command.
// Copy disk's data to buffer (up to bufsize) and return number of copied bytes.
int32_t tud_msc_read10_cb(uint8_t lun, uint32_t lba, uint32_t offset, void *buffer, uint32_t bufsize) {
    /* XIP-map read works for any byte-aligned range. `offset` is
     * usually 0 (whole-sector reads), but non-zero partial reads
     * are also valid per SCSI READ(10). */
    memcpy(buffer,
           (void *)(FLASH_MMAP_ADDR + lba * BLOCK_SIZE + offset),
           bufsize);
    return (int32_t)bufsize;
}

/* 4 KB read-modify-erase-program buffer for sub-erase-size MSC
 * writes. Static: caller frames are small and MSC may write a few
 * sectors at a time during host-initiated file copies. See also
 * rp2_flash.c's s_rmw_buf — same idea. */
static uint8_t s_msc_rmw_buf[FLASH_ERASE_SIZE];

// Callback invoked when received WRITE10 command.
// Process data in buffer to disk's storage and return number of written bytes
int32_t tud_msc_write10_cb(uint8_t lun, uint32_t lba, uint32_t offset, uint8_t *buffer, uint32_t bufsize) {
    uint32_t abs_offset = lba * BLOCK_SIZE + offset;
    const uint8_t *src = buffer;
    uint32_t remaining = bufsize;

    while (remaining > 0) {
        uint32_t block_addr    = FLASH_BASE_ADDR
                               + (abs_offset / FLASH_ERASE_SIZE) * FLASH_ERASE_SIZE;
        uint32_t offset_in_blk = abs_offset % FLASH_ERASE_SIZE;
        uint32_t space_in_blk  = FLASH_ERASE_SIZE - offset_in_blk;
        uint32_t chunk         = (remaining < space_in_blk) ? remaining : space_in_blk;

        const uint8_t *prog_src = src;
        uint32_t prog_len = chunk;
        uint32_t prog_offset = offset_in_blk;
        if (!(offset_in_blk == 0 && chunk == FLASH_ERASE_SIZE)) {
            /* Partial 4 KB block: preserve the untouched bytes via
             * the XIP mapping before we erase. */
            memcpy(s_msc_rmw_buf,
                   (const void *)(XIP_BASE + block_addr),
                   FLASH_ERASE_SIZE);
            memcpy(s_msc_rmw_buf + offset_in_blk, src, chunk);
            prog_src = s_msc_rmw_buf;
            prog_len = FLASH_ERASE_SIZE;
            prog_offset = 0;
        }
        uint32_t ints = save_and_disable_interrupts();
        flash_range_erase(block_addr, FLASH_ERASE_SIZE);
        flash_range_program(block_addr + prog_offset, prog_src, prog_len);
        restore_interrupts(ints);

        abs_offset += chunk;
        src        += chunk;
        remaining  -= chunk;
    }
    return (int32_t)bufsize;
}

// Callback invoked when received an SCSI command not in built-in list below
// - READ_CAPACITY10, READ_FORMAT_CAPACITY, INQUIRY, MODE_SENSE6, REQUEST_SENSE
// - READ10 and WRITE10 has their own callbacks
int32_t tud_msc_scsi_cb(uint8_t lun, uint8_t const scsi_cmd[16], void *buffer, uint16_t bufsize) {
    int32_t resplen = 0;
    switch (scsi_cmd[0]) {
        case SCSI_CMD_PREVENT_ALLOW_MEDIUM_REMOVAL:
            // Sync the logical unit if needed.
            break;

        default:
            // Set Sense = Invalid Command Operation
            tud_msc_set_sense(lun, SCSI_SENSE_ILLEGAL_REQUEST, 0x20, 0x00);
            // negative means error -> tinyusb could stall and/or response with failed status
            resplen = -1;
            break;
    }
    return resplen;
}
#endif
