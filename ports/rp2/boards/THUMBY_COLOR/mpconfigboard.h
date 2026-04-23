// Board and hardware specific configuration
#define MICROPY_HW_BOARD_NAME                   "TinyCircuits Thumby Color"

// FatFs VFS + USB MSC for drag-and-drop file access. ThumbyOne's
// shared FAT normally lives at physical flash 0x660000 (9.6 MB).
// When ThumbyOne is built with THUMBYONE_WITH_MD=ON the NES
// partition grows to 2 MB to hold PicoDrive, shifting the FAT up
// 1 MB to 0x760000 (8.6 MB). Standalone mp-thumby on this board
// uses the same layout as the ThumbyOne it's paired with so a
// single device can be reflashed between the two without
// reformatting the FAT. The parent build passes THUMBYONE_WITH_MD
// through ExternalProject_Add as a CMake define.
/* Guarded so the ThumbyOne slot build can disable MSC via
 * -DMICROPY_HW_USB_MSC=0 (lobby owns USB in that mode). */
#ifndef MICROPY_HW_USB_MSC
#define MICROPY_HW_USB_MSC                      (1)
#endif
#if defined(THUMBYONE_WITH_MD) && THUMBYONE_WITH_MD
#define MICROPY_HW_FLASH_STORAGE_BYTES          (0x8A0000u)  // 8.6 MB
#define MICROPY_HW_FLASH_STORAGE_BASE           (0x760000u)
#else
#define MICROPY_HW_FLASH_STORAGE_BYTES          (0x9A0000u)  // 9.6 MB
#define MICROPY_HW_FLASH_STORAGE_BASE           (0x660000u)
#endif

#define MICROPY_PY_DEFLATE_COMPRESS (1)
