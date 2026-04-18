/*
 * ThumbyOne MPY slot — wrap watchdog_reboot so a bare reset (from
 * engine.reset(), machine.reset(), or any fatal-panic path that
 * ends in watchdog_reboot) chains back into a fresh MPY slot
 * instead of dropping the user at the top-level lobby.
 *
 * Without this, engine.reset() is the same as "reboot the chip
 * and land on the lobby" — you then have to press RB to get back
 * into the MPY picker. With it, engine.reset() goes
 *   engine.reset()
 *     → watchdog_reboot()                                 [vendored SDK]
 *     → __wrap_watchdog_reboot()                          [this file]
 *         → thumbyone_handoff_prepare_slot(MPY)            [sets scratch magic]
 *         → __real_watchdog_reboot()                       [actually resets]
 *     → bootrom runs the lobby's main()
 *     → thumbyone_handoff_consume_if_present() finds the MPY magic
 *     → bootrom chains into the MPY partition
 *     → MPY slot's C picker runs again
 *
 * Feels like "exit to game picker" — the stock Thumby Color UX.
 *
 * Linker-wrapped via -Wl,--wrap=watchdog_reboot in
 * ports/rp2/CMakeLists.txt (slot-mode only).
 */
#include "pico/stdlib.h"
#include "hardware/watchdog.h"
#include "thumbyone_handoff.h"
#include "slot_layout.h"

/* Declared by the linker once --wrap is in effect. */
void __real_watchdog_reboot(uint32_t pc, uint32_t sp, uint32_t delay_ms);

void __wrap_watchdog_reboot(uint32_t pc, uint32_t sp, uint32_t delay_ms);

void __wrap_watchdog_reboot(uint32_t pc, uint32_t sp, uint32_t delay_ms) {
    /* Ask the bootrom+lobby to chain us right back into the MPY
     * slot on the next boot. prepare_slot just writes the scratch
     * magic; it does NOT reboot, so we can fall through to the
     * real watchdog_reboot and let IT kick the chip. */
    thumbyone_handoff_prepare_slot(THUMBYONE_SLOT_MPY);
    __real_watchdog_reboot(pc, sp, delay_ms);
}
