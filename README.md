# FloppyPlayer-MIDI

A standalone MIDI player that reads and plays `.MID` files directly off a real 3.5" floppy disk - no PC involved. An RP2040 reads the raw magnetic flux off the disk, decodes it into a FAT12 filesystem in firmware, parses the MIDI files it finds, and drives a small polyphonic synth out to a headphone jack.

## Hardware

- RP2040 board: **Waveshare RP2040-PiZero** (40-pin Raspberry Pi Zero-compatible header)
- A real 3.5" floppy drive (high-density, 1.44MB) with its 34-pin ribbon cable
- Floppy drive power: 5V works on its own in testing, though a real drive's spec calls for both 5V (logic) and 12V (spindle/stepper motor) - provide both if possible
- Audio out: PWM + RC low-pass filter per channel into a standard headphone/aux jack (no DAC chip)
- 5 momentary buttons (Next/Prev/Play-Pause/Vol+/Vol-), 2 optional toggle switches (loop mode, autoplay), 1 optional potentiometer (volume)

### Wiring

| Signal | GPIO | Notes |
|---|---|---|
| Drive Select | 0 | |
| Motor Enable | 1 | |
| Direction | 4 | |
| Step | 5 | |
| Side Select | 8 | |
| Track00 | 9 | input, needs pull-up (see below) |
| Index | 10 | input, needs pull-up |
| Read Data | 11 | input, needs pull-up |
| Disk Change (DSKCHG, ribbon pin 34) | 27 | input, needs pull-up - **not physically wired in the reference build yet** |
| Next track | 12 | button to GND, internal pull-up |
| Prev track | 13 | button to GND, internal pull-up |
| Play/Pause | 14 | button to GND, internal pull-up |
| Volume up | 15 | button to GND, internal pull-up |
| Volume down | 17 | button to GND, internal pull-up |
| Loop-mode switch | 16 | grounded = repeat current track instead of auto-advance |
| Autoplay switch | 2 | grounded = start playing automatically after any disk scan |
| Volume pot wiper | 26 (ADC0) | B500K linear taper, 3.3V-to-GND divider |
| Audio left | 22 | PWM out -> RC filter -> jack |
| Audio right | 23 | PWM out -> RC filter -> jack |

The three open-collector drive inputs (Track00, Index, Read Data, and Disk Change if wired) all need the same treatment: a 1kΩ pull-up to +5V, then a 10kΩ/20kΩ divider down to a safe 3.3V logic level before the GPIO, since these lines idle at 5V and the RP2040's GPIOs aren't 5V-tolerant.

GPIO2/3/16/22-29 are reclaimed from this board's unused DVI/HDMI header - safe to repurpose as long as nothing is ever plugged into that port. **Not every one of those pins is actually broken out to the 40-pin header** - GPIO28/29 turned out to be wired only to the DVI clock differential pair with no header connection at all, so don't assume a pin is free without checking the board's actual schematic or silkscreen first.

**Drive activity LED**: on the reference build, the LED follows Drive Select, not Motor Enable - if yours does too, both must be toggled together to turn it off when idle (already how the firmware does it).

**Power note**: this board's 40-pin header "5V" pins are the same electrical net as USB VBUS, with no diode or switch between them. Don't power the board from both the floppy drive's 5V rail and USB at the same time - it ties two supplies directly together.

## Controls

| Button | Short press | Long press |
|---|---|---|
| Next | Next track | Next folder |
| Prev | Previous track | Previous folder |
| Play/Pause | Toggle play/pause | - |
| Vol+ | Volume up (11 discrete steps) | - |
| Vol- | Volume down | - |

The volume pot, if wired, gives continuous control and simply overrides the last button-set level whenever it's turned. Connect Serial Monitor at **115200 baud** to see track-load, error, and diagnostic messages (folder/track state, why a mount failed, why a track was skipped, etc).

## Getting started / disk requirements

- Standard PC-formatted 1.44MB 3.5" floppy, FAT12 filesystem
- `.MID` files placed anywhere the FAT12 driver can see: the root directory becomes the "ROOT" folder, and any subdirectory containing at least one real `.MID` file becomes its own folder (no fixed/hardcoded folder names)
- Track order within a folder follows a leading number in the file's original (pre-8.3-truncation) long filename, e.g. `10 - Theme.mid` -> truncated to `10THEM~1.MID` on disk, sorts as track 10. Files without a leading number sort last.
- **File size limit: ~1.1MB** per file (`FLOPPY_MAX_FILE_CLUSTERS`, sized for this disk's 512B/cluster format) - generous versus any real SMF file, but a hard ceiling
- **Track count limit: 24** per MIDI file (`MIDI_SEQ_MAX_TRACKS`) - covers every multi-track (SMF format 1) file seen in testing (up to 18 tracks) with headroom
- Channel 10 (MIDI channel index 9), the General MIDI percussion channel, is silenced rather than mis-rendered as pitched notes - there's no drum synthesis yet
- A single bad/marginal sector on the disk doesn't stop playback: the player retries, and if a specific spot genuinely won't read after several seconds, it skips that track automatically rather than hanging

## Architecture

**Dual-core split.** Core 0 owns buttons, floppy disk I/O, and the playlist; Core 1 owns the synth and audio output exclusively and never touches the floppy. A disk read is near-instant on a cylinder-cache hit, but several hundred ms to over a second on a cache miss that needs a fresh capture plus retries - if that ran on the same core generating audio, every miss would cause an audible glitch. The two cores talk over Arduino-Pico's inter-core FIFO, passing only small note-on/off commands.

**MIDI dispatch runs on a timer interrupt, not in `loop()`.** The dual-core split alone wasn't enough: Core 0 still did disk I/O *and* dispatch (deciding *when* to send a note) in the same `loop()`, so a blocking preload read would delay a due note-off, audibly extending whatever was playing for the read's whole duration. Dispatch now runs from a repeating 1ms hardware timer on Core 0 that never touches the floppy - it only reads data that's already been staged in RAM, so it keeps firing on schedule even while `loop()` is blocked on a disk read elsewhere.

**Streaming, not whole-file buffering.** Files are read directly off disk a window at a time - `floppy_fat12` walks a file's cluster chain once and serves arbitrary byte ranges on demand; `midi_sequencer` keeps two half-size buffers per track and double-buffers between them like a video player preloading the next segment while the current one plays. File size is effectively unbounded, even beyond the chip's 264KB of RAM. Multi-track files are merged live (K-way merge by tick) rather than pre-sorted into one array.

**Multi-slot cylinder cache.** Up to 3 cylinders' worth of sectors are cached at once (LRU eviction), since different tracks in a multi-track file are usually positioned on different cylinders simultaneously - a single-slot cache meant constant reseeking. Sector recovery accumulates across the 3 capture retries per cylinder (a sector found on an earlier attempt survives even if a later attempt's different revolution misses it), and a cylinder that fails to fully validate is never permanently cached as "already tried" - it's retried fresh the next time it's needed, since a marginal sector can succeed on a completely different revolution.

**Graceful degradation on bad sectors.** If a track's next chunk genuinely can't be read for several seconds, current notes fade out smoothly (not an abrupt cut) and the track is marked finished, letting playback auto-advance instead of hanging indefinitely.

**Disk-swap detection.** Polls the drive's Disk Change line; on a swap, invalidates the cache and playlist and remounts automatically. Also auto-retries mounting on an empty/no-disk boot instead of requiring a manual reset, and spins the drive motor down between retries to save power (and, on drives where the activity LED follows Drive Select, to also turn the LED off).

## Building

Arduino IDE with the [arduino-pico](https://github.com/earlephilhower/arduino-pico) core installed. Board: **Waveshare RP2040 PiZero** (`rp2040:rp2040:waveshare_rp2040_pizero`). Uses the core's bundled `PWMAudio` library for audio output - no other external libraries required.

**Requires Tools -> CPU Speed -> 200MHz** (this is already the default for the `waveshare_rp2040_pizero` board definition, but worth double-checking if it's ever changed). The synth's per-sample envelope math needs this headroom - at a lower clock, dense chords can overrun the audio budget and freeze the board solid.

## License

MIT - see [LICENSE](LICENSE). The PIO flux-capture core is a port of the approach used in [adafruit/Adafruit_Floppy](https://github.com/adafruit/Adafruit_Floppy) (also MIT-licensed).
