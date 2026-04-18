// Board and hardware specific configuration
#define MICROPY_HW_BOARD_NAME                   "TinyCircuits Thumby Color"

// FatFs VFS + USB MSC for drag-and-drop file access. ThumbyOne's
// shared FAT lives at physical flash 0x660000 (6.625 MB) and
// spans 9.6 MB; overriding the default top-of-flash placement
// here so the MPY slot's FatFs points at the ThumbyOne shared
// region. Standalone mp-thumby on this board now also lives at
// this layout — deliberate, because a single Thumby Color may be
// reflashed between standalone and ThumbyOne firmwares without
// reformatting, and FAT on-disk layout matches across both.
/* Guarded so the ThumbyOne slot build can disable MSC via
 * -DMICROPY_HW_USB_MSC=0 (lobby owns USB in that mode). */
#ifndef MICROPY_HW_USB_MSC
#define MICROPY_HW_USB_MSC                      (1)
#endif
#define MICROPY_HW_FLASH_STORAGE_BYTES          (0x9A0000u)  // 9.6 MB
#define MICROPY_HW_FLASH_STORAGE_BASE           (0x660000u)

#define MICROPY_PY_DEFLATE_COMPRESS (1)
