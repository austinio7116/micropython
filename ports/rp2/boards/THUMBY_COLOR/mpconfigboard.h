// Board and hardware specific configuration
#define MICROPY_HW_BOARD_NAME                   "TinyCircuits Thumby Color"

// FatFs VFS + USB MSC for drag-and-drop file access.  Both the
// flash-storage region and the slot identity come from
// ThumbyOne/common/slot_layout.h — the canonical layout source
// shared with the lobby and every other slot.  Standalone mp-thumby
// on this board uses the same layout as the ThumbyOne it's paired
// with, so a single device can be reflashed between the two without
// reformatting the FAT.

#include "slot_layout.h"

/* Guarded so the ThumbyOne slot build can disable MSC via
 * -DMICROPY_HW_USB_MSC=0 (lobby owns USB in that mode). */
#ifndef MICROPY_HW_USB_MSC
#define MICROPY_HW_USB_MSC                      (1)
#endif

#define MICROPY_HW_FLASH_STORAGE_BASE           THUMBYONE_FAT_OFFSET
#define MICROPY_HW_FLASH_STORAGE_BYTES          THUMBYONE_FAT_SIZE

#define MICROPY_PY_DEFLATE_COMPRESS (1)
