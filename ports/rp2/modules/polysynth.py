"""ThumbyOne polysynth compatibility shim.

The original Thumby's polysynth library is a 7-voice PIO-based
synthesiser that drives bare GPIOs directly: 7 wave-generator output
pins (7, 8, 9, 10, 11, 21, 22), an inhibit pin (25), and a mixed
audio output (28). On Color those GPIOs collide with the LCD
backlight, the RGB LED PWMs, and the A / B / RB buttons — running
the upstream library would brick the device while the song plays.

This shim is installed into `sys.modules['polysynth']` by the
launcher when it spots a `polysynth.py` file in the legacy game
folder. The upstream library's source therefore never gets parsed,
none of its GPIO claims happen, and the game's `polysynth.setpitch`
/ `polysynth.play` calls land in this module instead.

Routing: all 7 logical voices map 1:1 onto engine_audio's 7 channels
(CHANNEL_COUNT was bumped from 4 to 7 in 1.11 specifically for this
shim). Each voice owns a `ToneSoundResource` whose `shape` is set
from the game's `configure(types)` call:

  - SQUARE  → ToneSoundResource shape=1 (chiptune square wave)
  - NOISE   → ToneSoundResource shape=2 (22-bit LFSR — bit-for-bit
              the same algorithm as the upstream library's pio_lfsr)

`instant_freq=True` is set on every voice so polysynth's rapid pitch
changes (arpeggios, vibrato, slides) bypass the engine's normal
fade-down/fade-up smoothing. Phase locking and phase offset are
implemented by writing the engine's new `phase` property — voices
align in phase within one engine sample (45 µs at 22050 Hz), which
is well under any audible threshold for chord coherence.

What's NOT supported:
  - Direct GPIO read of the synth output pins (PSdemo's oscilloscope
    feature reads RP2040 GPIO registers at 0x40014000; that address
    is wrong on RP2350 anyway, and the synth output isn't on a real
    GPIO. The visualiser will show garbage but won't crash).
"""

import math
import time

try:
    import engine_audio as _engine_audio
    from engine_resources import ToneSoundResource as _ToneSoundResource
    _AUDIO_OK = True
except ImportError:
    _engine_audio = None
    _ToneSoundResource = None
    _AUDIO_OK = False

try:
    from machine import Timer as _MachineTimer
except ImportError:
    _MachineTimer = None


# --- Public constants (unchanged from upstream) -------------------------

SQUARE = 0
NOISE = 1
twelveroottwo = 1.059463094
czero = 8.175798916


# --- Engine-side ToneSoundResource shape values -------------------------
# These match enum tone_shape in engine_tone_sound_resource.h.
_TONE_SHAPE_SINE = 0
_TONE_SHAPE_SQUARE = 1
_TONE_SHAPE_NOISE = 2

# Map polysynth voice type → engine ToneSoundResource shape.
_TYPE_TO_SHAPE = {SQUARE: _TONE_SHAPE_SQUARE, NOISE: _TONE_SHAPE_NOISE}


# --- Internal state -----------------------------------------------------

_VOICE_COUNT = 7
_corecount = 7
_config = []
_logical_pitch = [None] * _VOICE_COUNT
_tone_res = [None] * _VOICE_COUNT
_tone_active = [False] * _VOICE_COUNT


def _ensure_voice_resources():
    """Allocate one ToneSoundResource per voice on first use. Done
    lazily so the shim still imports cleanly outside the engine."""
    if not _AUDIO_OK:
        return
    for i in range(_VOICE_COUNT):
        if _tone_res[i] is None:
            t = _ToneSoundResource()
            t.instant_freq = True   # no fade — polysynth wants snappy pitch
            t.shape = _TONE_SHAPE_SQUARE  # default; configure() overrides
            _tone_res[i] = t


# --- Voice update -------------------------------------------------------

def _set_voice(logical, hz):
    """Update a single voice's pitch. Frequency 0 / None → silent."""
    if not (0 <= logical < _VOICE_COUNT):
        return
    if not _AUDIO_OK:
        return
    if hz is None or hz <= 0:
        if _tone_active[logical]:
            try:
                _engine_audio.stop(logical)
            except Exception:
                pass
            _tone_active[logical] = False
        _logical_pitch[logical] = None
        return
    _logical_pitch[logical] = hz
    try:
        _tone_res[logical].frequency = float(hz)
        if not _tone_active[logical]:
            _engine_audio.play(_tone_res[logical], logical, True)
            _tone_active[logical] = True
    except Exception:
        pass


# --- Public API ---------------------------------------------------------

def configure(types=None, corecount=7):
    """Apply per-voice wave-shape configuration. The upstream library
    allocates PIO state machines here; we just set each pre-allocated
    ToneSoundResource's `shape` to match."""
    global _corecount, _config
    if types is None:
        types = []
    _config = types[:]
    while len(_config) < corecount:
        _config.append(SQUARE)
    _corecount = corecount

    _ensure_voice_resources()
    if _AUDIO_OK:
        for i in range(_VOICE_COUNT):
            shape = _TYPE_TO_SHAPE.get(_config[i] if i < len(_config) else SQUARE,
                                       _TONE_SHAPE_SQUARE)
            try:
                _tone_res[i].shape = shape
            except Exception:
                pass


def enabled(value=None):
    """Original: get/set active mixer channel count. The engine mixer
    is always running on every channel; we just track the number for
    games that rely on the return value."""
    global _corecount
    if value is not None:
        _corecount = value
    return _corecount


def stop(mixer=True, chan=True, song=True):
    global _playing, _stream
    if song:
        _playing = False
        if _timer is not None:
            try:
                _timer.deinit()
            except Exception:
                pass
        for i in range(_VOICE_COUNT):
            _notes[i] = None
            _instruments[i] = None
        _stream = None
    if chan:
        for i in range(_VOICE_COUNT):
            _set_voice(i, None)


def setpitch(chan, pitch):
    """Set logical channel's frequency in Hz. None or 0 disables."""
    if not (0 <= chan < _corecount):
        return
    _ensure_voice_resources()
    _set_voice(chan, pitch)


def setnote(chan, pitch):
    """Set channel pitch via MIDI number (60 = middle C). None disables."""
    if pitch is None:
        setpitch(chan, None)
        return
    setpitch(chan, czero * (twelveroottwo ** pitch))


def instrument(phaselock=False, phase=None, detune=0,
               vibspeed=0, vibamount=0, rise=0, length=None):
    """Tuple matches the upstream layout — sequencer indexes by position."""
    return (phaselock, phase, detune, vibspeed, vibamount, rise, length)


# --- Phase locking helpers ---------------------------------------------

def _set_phase(logical, phase_fraction):
    """Reset a voice's phase. `phase_fraction` is in [0, 1) where
    0.0 = start of cycle. Used by the sequencer for phaselocked notes
    and explicit phase offsets."""
    if not (0 <= logical < _VOICE_COUNT):
        return
    if not _AUDIO_OK or _tone_res[logical] is None:
        return
    try:
        _tone_res[logical].phase = float(phase_fraction)
    except Exception:
        pass


def _start_locked_note(logical, midi_pitch, phase_offset_halfcycles):
    """Start a phase-locked note at a specific phase offset. Original
    polysynth's `phase` instrument parameter is in halfcycles
    (0.0 = aligned, 0.5 = 90°, 1.0 = 180°, etc.); we convert to a
    fraction-of-cycle for the engine's `phase` property."""
    # halfcycles → fraction-of-full-cycle: divide by 2 (one full cycle
    # is two halfcycles), then mod 1 to normalise.
    p = (phase_offset_halfcycles * 0.5) % 1.0
    _set_phase(logical, p)
    setnote(logical, midi_pitch)


def _start_aligned_note(logical, midi_pitch):
    """Start a phase-locked note at phase 0 (aligned with siblings)."""
    _set_phase(logical, 0.0)
    setnote(logical, midi_pitch)


# --- Sequencer ----------------------------------------------------------

_notes = [None] * _VOICE_COUNT
_instruments = [None] * _VOICE_COUNT
_ilist = {}
_stream = None
_playing = False
_eventstart = 0
_setenabled = False
_autoreset = False
_timer = _MachineTimer() if _MachineTimer is not None else None


def _audiotick(_dummy=None):
    global _playing, _eventstart, _stream
    stop_now = True

    if _stream is not None and _stream.nextevent is not None:
        stop_now = False
        # Collect phase-locked starts so they all land in adjacent
        # Python statements — the engine's audio ISR runs at 22050 Hz
        # (one sample every 45 µs), so a tight Python loop's stores
        # all hit within the same sample → voices align in phase.
        locked_starts = []
        while (_stream.nextevent is not None and
               _stream.nextevent[0] < time.ticks_ms() - _eventstart):
            event = _stream.nextevent
            etype = event[1]
            if etype == 1:                       # note on
                lc = event[2]
                pitch = event[3]
                _notes[lc] = pitch
                _instruments[lc] = None
                ins = _ilist.get(event[4])
                if ins is None:
                    setnote(lc, pitch)
                else:
                    pitch_midi = pitch + ins[2]  # detune
                    if ins[4] or ins[5] or ins[6]:
                        # vibrato / rise / length — needs per-tick update
                        _instruments[lc] = (
                            ins[0], pitch_midi, ins[3], ins[4],
                            event[0] + _eventstart, lc, ins[5], ins[6],
                        )
                    if ins[1] is not None:
                        # Phase offset specified — phase-locked start
                        # at the requested angle.
                        locked_starts.append((lc, pitch_midi, ins[1]))
                    elif ins[0]:
                        # Phase-locked, no offset → phase 0.
                        locked_starts.append((lc, pitch_midi, 0.0))
                    else:
                        # Free-running — wave continues from current
                        # phase, just retunes.
                        setnote(lc, pitch_midi)
            elif etype == 0:                     # note off
                _notes[event[2]] = None
                setnote(event[2], None)
                _instruments[event[2]] = None
            elif etype == 2 and _setenabled:     # set channel count
                _corecount_set(event[2])
            elif etype == 3:                     # callback
                try:
                    event[2](event[3])
                except Exception:
                    pass
            _stream.readevent()

        # Apply phase-locked starts together. Each one writes phase=X
        # then frequency back-to-back; the loop runs in microseconds
        # so all voices reset phase within the same engine sample.
        for lc, midi, phase_off in locked_starts:
            _start_locked_note(lc, midi, phase_off)

        if _stream.nextevent is None and _autoreset:
            try:
                if _stream.reset():
                    _eventstart = time.ticks_ms()
            except Exception:
                pass

    # Update persistent instruments (vibrato / rise / length-bound).
    now_ms = time.ticks_ms()
    for i in range(_VOICE_COUNT):
        ins = _instruments[i]
        if ins is None:
            continue
        stop_now = False
        phaselock, base_pitch, vib_spd, vib_amt, start_ms, lc, rise, length = ins
        if length and (now_ms > start_ms + length):
            _instruments[i] = None
            _set_voice(lc, None)
            continue
        pitch = base_pitch
        if vib_amt:
            pitch += math.sin((now_ms - start_ms) / 1000.0
                              * 2 * math.pi * vib_spd) * vib_amt
        if rise:
            pitch += (now_ms - start_ms) / 1000.0 * rise
        # Pitch-only update — keep current phase, just retune.
        setnote(lc, pitch)

    if stop_now:
        _playing = False
        if _timer is not None:
            try:
                _timer.deinit()
            except Exception:
                pass
        for i in range(_VOICE_COUNT):
            _notes[i] = None
            _instruments[i] = None
        _stream = None


def _corecount_set(n):
    global _corecount
    _corecount = n


def _start_timer():
    if _timer is None:
        return
    try:
        _timer.init(freq=50, mode=_MachineTimer.PERIODIC, callback=_audiotick)
    except Exception:
        pass


class StreamWrapper:
    """Wraps a list of polysynth events into the upstream-shape stream."""
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.nextevent = None
        self.readevent()

    def reset(self):
        self.pos = 0
        self.nextevent = None
        self.readevent()
        return True

    def readevent(self):
        if self.pos < len(self.data):
            self.nextevent = self.data[self.pos]
            self.pos += 1
        else:
            self.nextevent = None
        return self.nextevent


def play(song, ins=None, autoenable=True, loop=False):
    global _stream, _eventstart, _playing, _instruments
    global _ilist, _setenabled, _autoreset
    if ins is None:
        ins = {}
    _ensure_voice_resources()
    _stream = StreamWrapper(song)
    _playing = True
    _autoreset = loop
    _instruments = [None] * _VOICE_COUNT
    _ilist = ins
    _setenabled = autoenable
    _eventstart = time.ticks_ms()
    _start_timer()


def playstream(song, ins=None, autoenable=True, loop=False):
    global _stream, _eventstart, _playing, _instruments
    global _ilist, _setenabled, _autoreset
    if ins is None:
        ins = {}
    _ensure_voice_resources()
    _stream = song
    _playing = True
    _autoreset = loop
    _instruments = [None] * _VOICE_COUNT
    _ilist = ins
    _setenabled = autoenable
    _eventstart = time.ticks_ms()
    _start_timer()


def playnote(chan, pitch, ins=None):
    global _instruments, _playing
    _ensure_voice_resources()
    if ins is None or pitch is None:
        _instruments[chan] = None
        setnote(chan, pitch)
        return
    if ins[4] or ins[5] or ins[6]:  # vibrato / rise / length — persist
        _instruments[chan] = (
            ins[0], pitch + ins[2], ins[3], ins[4],
            time.ticks_ms(), chan, ins[5], ins[6],
        )
    if ins[1] is not None:
        _start_locked_note(chan, pitch + ins[2], ins[1])
    elif ins[0]:
        _start_aligned_note(chan, pitch + ins[2])
    else:
        setnote(chan, pitch + ins[2])
    if not _playing:
        _playing = True
        _start_timer()
