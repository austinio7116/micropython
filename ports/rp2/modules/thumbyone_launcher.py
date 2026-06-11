# ThumbyOne MPY slot: run the game picked by the C picker.
#
# The C picker (common/picker/picker.c in the ThumbyOne tree) ran
# before mp_init() and wrote the chosen game directory's path to
# /.active_game on the shared FAT. _boot_fat.py then mounted the
# FAT at "/", so we can just read the file here and exec the
# game's main.py.
#
# Mirrors the engine's own filesystem/main.py launcher flow: chdir
# into the game dir so relative file paths (TextureResource("foo.bmp"))
# resolve, add the dir to sys.path for imports, and initialise the
# save subsystem. Without the chdir the engine raises ENOENT on
# every asset load.
#
# If /.active_game is missing (picker didn't write one because it
# was the first-boot "no games" splash, or a previous game run
# deleted it to force the picker next reboot), fall through to
# the normal MicroPython REPL path.

import os
import sys

# `engine_main` is the C-side bootstrap that initialises the game
# engine's hardware (LCD, audio PWM, button state machine, RTC, etc.).
# The engine modules raise RuntimeError on first use if it hasn't been
# imported. Modern Thumby Color games do `import engine_main` at the
# top of their main.py; legacy Thumby games (which use the frozen
# `thumby` shim) don't know about it. Importing it once here covers
# both cases — second imports from the game itself are no-ops.
import engine_main  # noqa: F401


def _write_last_error(prefix, e):
    """Capture an exception traceback to /.last_error.txt. Cheap to
    call from any code path that wants to record a launch-time
    failure visible to the user via USB MSC after reboot."""
    try:
        sys.print_exception(e)
    except Exception:
        pass
    try:
        import io
        buf = io.StringIO()
        buf.write(prefix)
        sys.print_exception(e, buf)
        try:
            os.chdir("/")
        except OSError:
            pass
        with open("/.last_error.txt", "w") as ef:
            ef.write(buf.getvalue())
    except Exception:
        pass


def _wrap_text(s, width):
    """Word-wrap a string to `width` characters per line, preserving any
    existing newlines and hard-breaking over-long words."""
    out = []
    for para in s.split("\n"):
        line = ""
        for word in para.split(" "):
            while len(word) > width:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:width])
                word = word[width:]
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += " " + word
            else:
                out.append(line)
                line = word
        out.append(line)
    return "\n".join(out)


def _show_crash_screen(e):
    """Draw the game's error on screen and wait for A before returning to
    the picker. Best-effort: the crash file has already been written, so
    if anything here fails we just fall through to the reboot."""
    try:
        import engine
        import engine_io
        import engine_draw
        from engine_nodes import CameraNode, Text2DNode
        from engine_resources import FontResource
        from engine_math import Vector2

        # Tear down the crashed game's scene + audio first — otherwise its
        # leftover nodes keep ticking/drawing under the crash screen (the
        # "game carries on" behaviour) and may re-raise.
        try:
            engine.clear_scene()
        except Exception:
            pass
        try:
            import engine_audio
            for _ch in range(4):
                engine_audio.stop(_ch)
        except Exception:
            pass

        try:
            title_text = e.__class__.__name__
        except Exception:
            title_text = "Error"
        body_text = _wrap_text(str(e) or repr(e), 21)

        engine.fps_limit(60)
        engine_draw.set_background_color(engine_draw.Color(0.06, 0.0, 0.0))
        font = FontResource("/system/assets/font5x7.bmp")
        camera = CameraNode()
        title = Text2DNode(font=font, text=title_text, color=engine_draw.red,
                           position=Vector2(0, -52), letter_spacing=1)
        body = Text2DNode(font=font, text=body_text, color=engine_draw.white,
                          position=Vector2(0, 0), letter_spacing=1, line_spacing=2)
        hint = Text2DNode(font=font, text="Press A", color=engine_draw.yellow,
                          position=Vector2(0, 56), letter_spacing=1)
        camera.add_child(title)
        camera.add_child(body)
        camera.add_child(hint)

        # Wait for A to be released first (in case it was held at the
        # crash), then for a fresh press, so we don't dismiss instantly.
        while True:
            if engine.tick() and not engine_io.A.is_pressed:
                break
        while True:
            if engine.tick() and engine_io.A.is_just_pressed:
                break
    except Exception:
        pass


def _run_active_game():
    try:
        with open("/.active_game") as f:
            game_dir = f.read().strip()
    except OSError:
        return  # no active game — REPL

    if not game_dir:
        return

    # Modern Thumby Color games ship main.py. Legacy original-Thumby
    # games instead ship <dirname>.py inside the game folder, matching
    # the pre-Color launcher convention. Try main.py first, then fall
    # back to the legacy name so `import thumby` games (rendered via
    # the frozen thumby/ssd1306 shim) Just Work.
    main_path = game_dir + "/main.py"
    is_legacy_game = False
    try:
        with open(main_path) as f:
            code = f.read()
    except OSError:
        slash = game_dir.rfind("/")
        dir_name = game_dir[slash + 1:] if slash >= 0 else game_dir
        legacy_path = game_dir + "/" + dir_name + ".py"
        try:
            with open(legacy_path) as f:
                code = f.read()
            main_path = legacy_path
            is_legacy_game = True
        except OSError:
            # Path in /.active_game no longer points at a real game.
            return

    # Pre-load our frozen thumbyGrayscale BEFORE adding the game's
    # directory to sys.path. Many original-Thumby grayscale games
    # ship a local copy of Timendus's thumbyGrayscale.py with raw
    # SSD1306 SPI/_thread code that uses MicroPython integer-literal
    # forms our parser rejects (e.g. `0xD0000000+0x01C` style hex
    # joins) — if the launcher imports thumbyGrayscale AFTER game_dir
    # is on sys.path, the local copy wins the search and crashes the
    # parse. Importing here pulls our frozen module while sys.path
    # still has only the default .frozen / /lib entries; the resulting
    # sys.modules['thumbyGrayscale'] cache then short-circuits any
    # subsequent `import thumbyGrayscale` from the game itself.
    _gs_mod = None
    try:
        import thumbyGrayscale as _gs_mod
        sys.modules['thumbyGrayscale'] = _gs_mod
    except Exception as _gs_e:
        _write_last_error("thumbyGrayscale import failed:\n", _gs_e)
        _gs_mod = None

    # Same defensive ordering for polysynth — pre-import our frozen
    # software-emulated polysynth.py BEFORE adding game_dir to
    # sys.path. Otherwise a game folder containing its own
    # polysynth.py (PSdemo / TinyFreddy ship one) wins the search,
    # the upstream PIO-driven library gets imported, and its
    # configure() tries to claim GPIOs 7-25 — which on Color are
    # the LCD backlight, RGB LED PWMs, and A/B/RB buttons. The
    # outcome on the v1.10/v1.11 build was an ENOMEM at the first
    # PIO StateMachine allocation. Pre-importing populates
    # sys.modules['polysynth'] with our shim before sys.path moves,
    # so a subsequent `import polysynth` from the game short-
    # circuits to the cache and never reads the bundled file.
    _ps_mod = None
    try:
        import polysynth as _ps_mod
        sys.modules['polysynth'] = _ps_mod
    except Exception as _ps_e:
        _write_last_error("polysynth pre-import failed:\n", _ps_e)
        _ps_mod = None

    # Add game dir to sys.path for `import` inside the game.
    if game_dir not in sys.path:
        sys.path.insert(0, game_dir)

    # chdir into the game dir so relative paths (e.g.
    # `TextureResource("sprite.bmp")`) resolve against the game's
    # own folder — this matches engine/filesystem/main.py's flow.
    original_cwd = None
    try:
        original_cwd = os.getcwd()
    except OSError:
        pass
    try:
        os.chdir(game_dir)
    except OSError:
        pass

    # Per-game save namespace under /Saves — matches the engine
    # launcher's convention.
    try:
        import engine_save
        engine_save._init_saves_dir("/Saves" + game_dir)
    except Exception:
        pass

    # A handful of original-Thumby games (Umby & Glow being the
    # canonical example) inline the entire Timendus Grayscale class
    # into a per-game `display.py` instead of importing thumbyGrayscale.
    # The sys.modules trick above can't help those; we'd otherwise need
    # to fork each game's display.py. Instead, sniff the file: if it
    # looks like the Timendus inline pattern (`class Grayscale` plus
    # an `_thread` import — the second-core SPI-driver giveaway), shim
    # `import display` to our frozen thumbyGrayscale singleton with
    # the three names other game files import (`display`,
    # `display_buffer`, `display_update`). The user's original game
    # files stay untouched on the FAT.
    if _gs_mod is not None:
        try:
            with open(game_dir + "/display.py", "rb") as _df:
                _head = _df.read(2048)
            if b"class Grayscale" in _head and b"_thread" in _head:
                # `from X import Y` in MicroPython resolves Y as an
                # attribute lookup on whatever sys.modules['X'] holds,
                # so a plain object with the right attributes works
                # just as well as a real ModuleType — and avoids the
                # `import types` dependency (the types module isn't
                # frozen into our build, so importing it would raise).
                class _DisplayShim:
                    pass
                _shim = _DisplayShim()
                _shim.display        = _gs_mod.display
                _shim.display_buffer = _gs_mod.display.buffer
                _shim.display_update = _gs_mod.display.update
                _shim.Sprite         = _gs_mod.Sprite
                sys.modules['display'] = _shim
                del _shim
        except OSError:
            pass
        except Exception as _shim_e:
            _write_last_error("display.py shim install failed:\n", _shim_e)
    del _gs_mod

    # Polysynth shim. Original-Thumby games PSdemo and TinyFreddy ship
    # with a `polysynth.py` library that drives bare GPIOs (7, 8, 9,
    # 10, 11, 21, 22, 25) via PIO state machines for 7-voice
    # synthesis. On Color those GPIOs are claimed by the LCD backlight
    # (7), RGB LED PWMs (10/11), and A/B/RB buttons (21/22/25); letting
    # the upstream library run would brick the device while the song
    # plays. Detect the bundled `polysynth.py` and override
    # sys.modules['polysynth'] with our frozen software-emulated
    # version BEFORE the game's first import resolves it. The bundled
    # source therefore never gets parsed, no GPIO claims happen, and
    # the game's `polysynth.setpitch` / `polysynth.play` calls land in
    # the frozen shim which routes through engine_audio's 7-channel
    # mixer using ToneSoundResource (square / noise / sine, with
    # per-voice phase and instant_freq exposed for chord locking and
    # arpeggios respectively — engine 1.11 additions).
    # _ps_mod was pre-imported above; sys.modules['polysynth']
    # already points at our shim. The bundled polysynth.py in
    # PSdemo/TinyFreddy never gets parsed because the cache short-
    # circuits the import. Detection here is a no-op safety check —
    # if for any reason _ps_mod failed to import earlier (frozen
    # module missing?), make sure sys.modules is set so the game's
    # bundled file still doesn't run.
    if _ps_mod is not None:
        sys.modules['polysynth'] = _ps_mod

    # Legacy original-Thumby games (the ones we found via the
    # <dirname>.py filename fallback) often read buttons via raw
    # `machine.Pin(N, Pin.IN, Pin.PULL_UP).value` with original-Thumby
    # GPIO numbers (3,4,5,6,24,27). On Color those pin numbers map to
    # completely different functions (LCD reset, charge-status, rumble
    # PWM, etc.), so a real `Pin(5)` would steal the rumble pin and
    # cause constant vibration once the engine's PWM duty is touched
    # by anything in the slice — exactly what we saw with Umby & Glow.
    #
    # We can't just `machine.Pin = _PinShim` because the `machine`
    # module's globals dict is read-only (MP_DEFINE_CONST_DICT). The
    # working approach is to replace `sys.modules['machine']` with a
    # wrapper instance: `Pin` resolves to our shim, every other
    # attribute is forwarded to the real `machine` module via
    # `__getattr__`. `from machine import Pin` and `from machine
    # import freq, reset, PWM, Timer, ...` all still work.
    if is_legacy_game:
        try:
            import machine as _real_machine
            import engine_io as _eio_for_pin
            _real_pin_class = _real_machine.Pin
            _LEGACY_BTN_MAP = {
                3:  _eio_for_pin.LEFT,
                4:  _eio_for_pin.UP,
                5:  _eio_for_pin.RIGHT,
                6:  _eio_for_pin.DOWN,
                24: _eio_for_pin.B,
                27: _eio_for_pin.A,
            }
            # Cache the wrapper instances per pin so MicroPython's GC
            # can't free them out from under a bound `.value` method.
            _LEGACY_BTN_CACHE = {}
            class _LegacyButtonPin:
                # Original Thumby buttons are active-low (pull-up,
                # 0 == pressed). engine_io.X.is_pressed is True when
                # the user is pressing it, so invert. _eio_for_pin
                # is captured via closure from the enclosing
                # function — works fine for regular instance methods
                # in MicroPython (the static-method case in the PWM
                # shim is what fails).
                def __init__(self, btn): self._btn = btn
                def value(self, *args):
                    _eio_for_pin.update_buttons()
                    return 0 if self._btn.is_pressed else 1
                def init(self, *args, **kwargs): pass
                def irq(self, *args, **kwargs): return None

            # Pin numbers that legacy games access for non-button
            # purposes BUT which collide with Color hardware we must
            # not let them reconfigure. Each one returns an inert
            # _NoopPin so the hardware GPIO stays as the engine
            # configured it.
            #
            #   GPIO 0..2  — original Thumby's UART link-cable pins
            #                (Umby&Glow's comms.py, RocketCup's link
            #                code). On Color these are wired to D-pad
            #                LEFT (0), UP (1), RIGHT (2) — letting
            #                the game reconfigure them as OUT bricks
            #                three of the four D-pad buttons.
            _LEGACY_BLOCK_PINS = (0, 1, 2)

            # Sentinel returned by Pin(28) so PWM(Pin(28)) can detect
            # the buzzer pattern reliably. We can't use `int(pin)`
            # for detection because real machine.Pin doesn't reliably
            # implement __int__ (returns gpio level on some ports).
            class _BuzzerPinSentinel:
                """Quacks like a Pin enough that legacy games which
                construct PWM around it work transparently. Holds no
                hardware claim — the underlying audio output happens
                via thumbyAudio's existing pin-23 PWM."""
                def value(self, *args): return 0
                def init(self, *args, **kwargs): pass
                def irq(self, *args, **kwargs): return None
                def on(self, *args): pass
                def off(self, *args): pass
                def low(self): pass
                def high(self): pass
                def toggle(self): pass
            _BUZZER_PIN = _BuzzerPinSentinel()

            class _NoopPin:
                """Inert Pin wrapper for hardware-conflicting
                original-Thumby pins (link cable on 0/1/2). Pretends
                to be a Pin but never touches hardware."""
                def __init__(self, *args, **kwargs): pass
                def value(self, *args): return 0
                def init(self, *args, **kwargs): pass
                def irq(self, *args, **kwargs): return None
                def on(self, *args): pass
                def off(self, *args): pass
                def low(self): pass
                def high(self): pass
                def toggle(self): pass

            class _PinShim:
                IN        = _real_pin_class.IN
                OUT       = _real_pin_class.OUT
                PULL_UP   = _real_pin_class.PULL_UP
                PULL_DOWN = _real_pin_class.PULL_DOWN
                def __new__(cls, pin_id, *args, **kwargs):
                    if pin_id in _LEGACY_BTN_MAP:
                        if pin_id not in _LEGACY_BTN_CACHE:
                            _LEGACY_BTN_CACHE[pin_id] = _LegacyButtonPin(_LEGACY_BTN_MAP[pin_id])
                        return _LEGACY_BTN_CACHE[pin_id]
                    if pin_id == 28:
                        return _BUZZER_PIN
                    if pin_id in _LEGACY_BLOCK_PINS:
                        return _NoopPin()
                    return _real_pin_class(pin_id, *args, **kwargs)
            # PWM shim. Original-Thumby buzzer is on GPIO 28 — eight
            # legacy games (Umby & Glow audio, BadApple, PSdemo,
            # TinyFreddy, MicroMeows, Thexecutor, Journey3Dg's
            # musicplayer, Bowling Days) do `_spkr = PWM(Pin(28))`
            # then drive freq/duty_u16 on it. On Color, GPIO 28 is
            # unconnected and the buzzer moved to GPIO 23 (claimed by
            # the engine's audio path). When a legacy game asks for
            # `PWM(Pin(28))`, redirect to the existing PWM the
            # frozen thumbyAudio module already created on pin 23 so
            # the same freq/duty calls actually produce sound, with
            # cubic volume scaling matching the lobby's volume slider.
            _real_pwm_class = _real_machine.PWM
            # Capture the modules we need at construction time. Closure
            # works for the regular instance methods below; the C
            # built-in modules engine_io / engine_audio are NOT in
            # sys.modules (mp_module_get_builtin returns them directly
            # without populating the dict), so we MUST capture by
            # reference here, not look them up later.
            try:
                import thumbyAudio as _tha_for_pwm
            except Exception:
                _tha_for_pwm = None
            try:
                import engine_audio as _eng_audio_for_pwm
            except Exception:
                _eng_audio_for_pwm = None
            # Mutable state for the legacy PWM shim. Module-level dict
            # so we don't rely solely on closure capture (which has
            # been historically flaky for class methods defined inside
            # a function on this MicroPython fork — see the static-
            # method note below). Holds the engine_audio module ref
            # once we've loaded it, plus diagnostic counters.
            _legacy_pwm_state = {
                'eng_audio': None,
                'eng_audio_tried': False,
            }
            class _LegacyBuzzerPwm:
                # Forward to thumbyAudio's pin-23 PWM. Two scaling
                # regimes depending on what the underlying PWM is doing:
                #
                #  * Tone mode (audible PWM carrier, < 20 kHz): the PWM
                #    cycle IS the audio. Duty cycle controls amplitude
                #    of the tone. Class-D amp + buzzer response is non-
                #    linear, so use cube scaling on volume — without it,
                #    even mid-slider feels indistinguishable from max.
                #    Used by Umby & Glow, CosmicSurvivor, and most other
                #    legacy games that play tones via PWM(Pin(28)).
                #
                #  * PCM mode (carrier >= 20 kHz, currently only set
                #    when a game does thumby.audio.set(80000) before
                #    streaming samples): duty IS the audio sample.
                #    Re-centre around 50 % duty so the swing stays in
                #    the class-D amp's linear region (instead of
                #    railing 0 % / 100 %). Volume attenuates the swing
                #    toward the silent midpoint. Used by BadApple.
                #
                # We detect mode by querying the underlying PWM's
                # current frequency on each duty write. The query is a
                # single register read (~< 1 µs) and BadApple's audio
                # rate is 8 kHz, so the overhead is ~8 ms/sec — fine.
                #
                # Instance methods (NOT @staticmethod) — closure capture
                # of the enclosing-function locals (e.g. _tha_for_pwm)
                # was empirically broken for static methods defined in
                # a class defined inside a function on this MicroPython
                # fork. Instance methods work.
                def freq(self, f):
                    if _tha_for_pwm is None: return
                    try: _tha_for_pwm.audio.pwm.freq(f)
                    except Exception: pass
                def duty_u16(self, d):
                    if _tha_for_pwm is None: return
                    try:
                        pwm = _tha_for_pwm.audio.pwm
                        # Lazy-load engine_audio. We don't trust the
                        # outer closure import here because the user
                        # has hit closure-capture failures on this
                        # exact pattern before. A module-level dict
                        # for state is more reliable than a closure cell.
                        if not _legacy_pwm_state['eng_audio_tried']:
                            _legacy_pwm_state['eng_audio_tried'] = True
                            try:
                                import engine_audio as _ea
                                _legacy_pwm_state['eng_audio'] = _ea
                            except Exception:
                                _legacy_pwm_state['eng_audio'] = None
                        ea = _legacy_pwm_state['eng_audio']
                        v = 1.0
                        if ea is not None:
                            v = ea.get_volume()
                            if v < 0.0: v = 0.0
                            if v > 1.0: v = 1.0
                        try:
                            cur_freq = pwm.freq()
                        except Exception:
                            cur_freq = 0
                        if cur_freq >= 20000:
                            # PCM mode — re-centre input around 0x8000
                            # (50 % duty) with v-attenuated swing.
                            # input 0..0xFFFF → centred 0x4000..0xC000
                            centred = 0x4000 + (d >> 1)
                            d = 0x8000 + int((centred - 0x8000) * v)
                        else:
                            # Tone mode — cube scaling for perceptual
                            # response across the slider range.
                            d = int(d * v * v * v)
                        pwm.duty_u16(d)
                    except Exception: pass
                def deinit(self): pass
                def init(self, *args, **kwargs): pass
            _BUZZER_PWM_INSTANCE = _LegacyBuzzerPwm()
            class _PwmShim:
                def __new__(cls, pin_obj, *args, **kwargs):
                    # PWM(Pin(28)) — Pin shim returned _BUZZER_PIN
                    # (singleton sentinel) for pin id 28, so we can
                    # detect the buzzer-PWM pattern by identity check
                    # without relying on machine.Pin's int conversion
                    # (which is unreliable across ports — sometimes
                    # returns the GPIO level instead of the pin id).
                    if pin_obj is _BUZZER_PIN:
                        return _BUZZER_PWM_INSTANCE
                    return _real_pwm_class(pin_obj, *args, **kwargs)

            # UART shim. Original-Thumby uses UART(0) on pins 0/1 for
            # the link-cable. Color has no equivalent (engine_link
            # uses USB CDC). Provide a no-op UART class so games that
            # try to multiplayer don't crash; single-player still
            # works. Only RocketCup and Umby & Glow's comms.py care.
            _real_uart_class = getattr(_real_machine, 'UART', None)
            class _LegacyUart:
                def __init__(self, *args, **kwargs): pass
                def init(self, *args, **kwargs): pass
                def deinit(self, *args, **kwargs): pass
                def read(self, *args, **kwargs): return None
                def readinto(self, buf, *args, **kwargs): return 0
                def readline(self): return None
                def write(self, data, *args, **kwargs):
                    try: return len(data)
                    except TypeError: return 0
                def any(self): return 0
                def txdone(self): return True
            class _UartShim:
                def __new__(cls, *args, **kwargs):
                    # Always return a no-op UART for legacy games.
                    # No real link-cable hardware to talk to anyway.
                    return _LegacyUart()

            # Capture engine module ref for the freq hijack below.
            try:
                import engine as _eng_for_freq
            except Exception:
                _eng_for_freq = None

            def _machine_freq_via_engine(*args):
                """Route machine.freq(hz) through engine.freq(hz) so the
                engine's audio mixer's PWM-IRQ wrap value gets re-
                computed for the new clock. The default machine.freq
                in the rp2 port just calls set_sys_clock_khz, which
                changes clk_sys but doesn't tell engine_audio — the
                audio IRQ then keeps firing at the (now wrong) old
                rate. PSdemo's `machine.freq(125_000_000)` triggers
                exactly this: clock drops, IRQ rate drops with it,
                every audio sample takes longer than expected, and
                playback comes out one octave low. Routing through
                engine.freq() restores the IRQ wrap so audio stays
                pinned to 22050 Hz regardless of clock changes."""
                if _eng_for_freq is not None and len(args) == 1:
                    try:
                        _eng_for_freq.freq(args[0])
                        return None
                    except Exception:
                        # Fall through to the raw path below if
                        # engine.freq fails for any reason.
                        pass
                return _real_machine.freq(*args)

            class _MachineShim:
                Pin = _PinShim
                PWM = _PwmShim
                UART = _UartShim if _real_uart_class is not None else None
                freq = staticmethod(_machine_freq_via_engine)
                def __getattr__(self, name):
                    return getattr(_real_machine, name)
            sys.modules['machine'] = _MachineShim()

            # Some original-Thumby games (TinyGolf is the canonical
            # example) bypass thumbyButton entirely and access the raw
            # `swL` / `swR` / `swU` / `swD` / `swA` / `swB` Pin objects
            # exposed by the upstream thumbyHardware module on the
            # original-Thumby code path. The Color path of
            # thumbyHardware doesn't define those (only swBuzzer +
            # reset), so any access raises AttributeError. Backfill
            # them here using our _LegacyButtonPin instances so games
            # that read e.g. `thumbyHardware.swL.value()` get the same
            # engine_io-backed wrapper as `Pin(3).value()`.
            try:
                if 'thumbyHardware' in sys.modules:
                    _th = sys.modules['thumbyHardware']
                    def _btn_for(pin_id):
                        if pin_id not in _LEGACY_BTN_CACHE:
                            _LEGACY_BTN_CACHE[pin_id] = _LegacyButtonPin(_LEGACY_BTN_MAP[pin_id])
                        return _LEGACY_BTN_CACHE[pin_id]
                    _th.swL = _btn_for(3)
                    _th.swR = _btn_for(5)
                    _th.swU = _btn_for(4)
                    _th.swD = _btn_for(6)
                    _th.swA = _btn_for(27)
                    _th.swB = _btn_for(24)
                    del _btn_for, _th
            except Exception as _th_e:
                _write_last_error("thumbyHardware sw* augmentation failed:\n", _th_e)
        except Exception as _pin_e:
            _write_last_error("Pin shim install failed:\n", _pin_e)

    g = {"__name__": "__main__", "__file__": main_path}
    try:
        exec(code, g)
    except Exception as e:
        # Game crashed — capture the traceback to /.last_error.txt first
        # (inspectable via USB MSC), THEN show it on screen and wait for a
        # button so the player sees what happened before we reboot to the
        # picker (otherwise it just silently kicks back).
        _write_last_error("Game crash: " + main_path + "\n", e)
        _show_crash_screen(e)
    finally:
        if original_cwd is not None:
            try:
                os.chdir(original_cwd)
            except OSError:
                pass


_run_active_game()
del _run_active_game
