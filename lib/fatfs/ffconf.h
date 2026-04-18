/*---------------------------------------------------------------------------/
/  FatFs R0.15 configuration for ThumbyOne
/---------------------------------------------------------------------------*/
/*
 * One config, used by the lobby, every slot (NES/P8/DOOM), AND by
 * mp-thumby's extmod VFS layer once we port it off oofatfs. Any
 * tweak here changes on-disk format interop across all four
 * systems — keep that invariant in mind.
 *
 * Key choices for this board:
 *
 *   FF_VOLUMES = 1          Single shared 9.6 MB FAT at physical
 *                           0x660000. No SD card, no RAM drive,
 *                           ever. Plain numeric drive number.
 *
 *   FF_FS_RPATH = 2         MicroPython's extmod/vfs_fat.c calls
 *                           f_chdir + f_getcwd. Slots don't use
 *                           these, but the cost is trivial and the
 *                           single ffconf has to cover MPY's needs.
 *
 *   FF_LFN_UNICODE = 0      TCHAR = char. Matches extmod's
 *                           assumption (it passes ASCII paths from
 *                           Python strs). LFN still enabled so we
 *                           can see full filenames dropped from a
 *                           host.
 *
 *   FF_CODE_PAGE = 932      Matches what ThumbyNES has been shipping
 *                           (upstream default). 437 would save
 *                           flash via smaller ffunicode tables but
 *                           changing it without isolated test risks
 *                           regressions in NES's ROM picker. Revisit
 *                           as a size optimisation later.
 *
 *   FF_FS_EXFAT = 0         FAT16 only. Our canonical format is
 *                           FAT16 with 1 KB clusters — exFAT would
 *                           add ~10 KB of unused code.
 *
 *   FF_FS_NORTC = 0         File timestamps matter to the host when
 *                           the user drags files via USB MSC. The
 *                           lobby/slots provide get_fattime via a
 *                           stub (fixed date) or RTC read.
 *
 *   FF_FS_LOCK = 0          No concurrent opens from different
 *                           tasks — our firmware is
 *                           single-threaded at the FS level. Saves
 *                           a few KB of BSS.
 */

#define FFCONF_DEF      80286   /* Revision ID (FatFs R0.15) */


/*---------------------------------------------------------------------------/
/ Function Configurations
/---------------------------------------------------------------------------*/

#define FF_FS_READONLY  0
#define FF_FS_MINIMIZE  0
#define FF_USE_FIND     0
#define FF_USE_MKFS     1   /* lobby formats the shared FAT */
#define FF_USE_FASTSEEK 0
#define FF_USE_EXPAND   1   /* NES defragmenter + P8 sync use f_expand */
#define FF_USE_CHMOD    0
#define FF_USE_LABEL    1   /* lobby labels "THUMBYONE"          */
#define FF_USE_FORWARD  0

#define FF_USE_STRFUNC  0
#define FF_PRINT_LLI    1
#define FF_PRINT_FLOAT  1
#define FF_STRF_ENCODE  3


/*---------------------------------------------------------------------------/
/ Locale and Namespace Configurations
/---------------------------------------------------------------------------*/

#define FF_CODE_PAGE    932

#define FF_USE_LFN      1
#define FF_MAX_LFN      255
#define FF_LFN_UNICODE  0
#define FF_LFN_BUF      255
#define FF_SFN_BUF      12

#define FF_FS_RPATH     2   /* chdir + getcwd enabled — MicroPython needs both */


/*---------------------------------------------------------------------------/
/ Drive/Volume Configurations
/---------------------------------------------------------------------------*/

#define FF_VOLUMES          1
#define FF_STR_VOLUME_ID    0
#define FF_VOLUME_STRS      "RAM","NAND","CF","SD","SD2","USB","USB2","USB3"
#define FF_MULTI_PARTITION  0
/* FF_MIN_SS = 512: matches ThumbyNES/P8's native flash_disk sector size.
 * FF_MAX_SS = 4096 (or MICROPY_FATFS_MAX_SS): matches mp-thumby's
 * rp2.Flash bdev, which reports the 4 KB flash erase block as its
 * native block size. With FF_MAX_SS > FF_MIN_SS, FatFs queries
 * disk_ioctl(GET_SECTOR_SIZE) at mount and runs in variable-sector
 * mode — each mount picks up the underlying bdev's real block size,
 * so the same ff.c works across NES/P8 slots (512-byte sectors) and
 * the MPY slot (4 KB sectors) without any translation layer. FIL
 * objects on the MPY side carry a 4 KB sector buffer. Acceptable
 * given we're not opening many Python files concurrently.
 *
 * This was the root cause of "VfsFat.mkfs on rp2.Flash produces
 * junk": with FF_MAX_SS hardcoded to 512, FatFs issued 512-byte
 * disk_read calls that the blockdev layer inflated to block_size
 * bytes (4096 from rp2.Flash's ioctl response), overflowing the
 * caller's 512-byte buffer and corrupting every sector read. */
#define FF_MIN_SS           512
#ifdef MICROPY_FATFS_MAX_SS
/* MPY build only: MICROPY_FATFS_MAX_SS = FLASH_SECTOR_SIZE (4096)
 * so FatFs switches to variable-sector mode and picks up the
 * rp2.Flash bdev's 4 KB native block size. */
#define FF_MAX_SS           (MICROPY_FATFS_MAX_SS)
#else
/* NES / P8 / lobby: fixed 512-byte sectors. Matches the logical
 * sector size exposed by thumbyone_disk and the per-slot flash
 * disks; avoids the FATFS struct sprouting an `ssize` member that
 * existing slot code doesn't expect. */
#define FF_MAX_SS           512
#endif
#define FF_LBA64            0
#define FF_MIN_GPT          0x10000000
#define FF_USE_TRIM         0


/*---------------------------------------------------------------------------/
/ System Configurations
/---------------------------------------------------------------------------*/

#define FF_FS_TINY          0
#define FF_FS_EXFAT         0

#define FF_FS_NORTC         0
#define FF_NORTC_MON        1
#define FF_NORTC_MDAY       1
#define FF_NORTC_YEAR       2026

#define FF_FS_NOFSINFO      0
#define FF_FS_LOCK          0

#define FF_FS_REENTRANT     0
#define FF_FS_TIMEOUT       1000


/*--- End of configuration options ---*/
