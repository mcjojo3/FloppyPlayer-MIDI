# FloppyPlayer-MIDI

A standalone music player that reads and plays files directly off a real 3.5" floppy disk - no PC involved. An RP2040 reads the floppy's raw magnetic flux and decodes FAT12 in firmware; a Raspberry Pi 4 renders the MIDI through FluidSynth and drives a touchscreen UI.

It also plays from an SD card or the Pi's own music folder, and handles MP3/OGG/FLAC/WAV alongside `.MID` - but the floppy is the point.

This project has been through several architectures as it grew: one microcontroller doing everything, a two-chip split, a three-chip split with an ESP32, back to two chips, and now an RP2040 paired with a Raspberry Pi. Earlier designs aren't part of the repo; the original single-chip version is still in the git history.

## Architecture

```
[Floppy drive]      [microSD]
      |                 |
      v                 v
+------------------+            +----------------------------+
| RP2040           |            | Raspberry Pi 4 (4GB)       |
| storage server   |<-- USB --->| FluidSynth + player + GUI  |--> 3.5mm jack
| (floppy + SD,    |   (CDC     | 7" DSI touchscreen         |    or Bluetooth
|  passive)        |   serial)  |                            |    speaker
+------------------+            +----------------------------+
```

- **RP2040 (`rp2040-storage/`) - storage server.** Reads the floppy drive's raw flux and decodes FAT12 in firmware, and/or reads a microSD card. Purely passive: it never initiates anything, only answers file requests. Unchanged in substance across every architecture this project has had, because raw flux capture needs deterministic sub-microsecond timing that only its PIO hardware provides.
- **Raspberry Pi 4 (`pi-player/`) - player.** Requests files over the link, renders MIDI with FluidSynth, plays compressed audio with pygame, and owns the touchscreen UI and all settings.

### Why a Pi

Every earlier design fought the same ceiling: a microcontroller with a few hundred KB of RAM trying to be a General MIDI synthesizer. The last one needed an offline SoundFont-extraction pipeline, a knapsack allocator, and a custom cache format just to fit instruments into a **304KB** budget - and still fell back to plain oscillators for a third of the programs a typical playlist used, with no drums at all.

FluidSynth reads a 4GB `.sf2` natively with dynamic sample loading, whole MIDI files fit in RAM trivially, and GM percussion just works.

## Hardware

- **RP2040 board**: Waveshare RP2040-PiZero (40-pin Pi Zero-compatible header, onboard microSD slot)
- **Raspberry Pi 4** (4GB or better) with the official 7" DSI touchscreen
- A real 3.5" floppy drive (high-density, 1.44MB) with its 34-pin ribbon cable
- Floppy drive power: 5V works on its own in testing, though a real drive's spec calls for both 5V (logic) and 12V (spindle/stepper) - provide both if possible
- One USB cable from the Pi to the RP2040, carrying both power and the link
- Optional: a microSD card in the RP2040's slot for a larger library

### Wiring

**RP2040 <-> floppy drive** (unchanged since this project's original single-chip design):

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
| Disk Change (DSKCHG, ribbon pin 34) | 27 | input - **not physically wired in the reference build yet**, so firmware enables the internal pull-up as a stopgap; switch to a real external pull-up once wired |

The four open-collector drive inputs (Track00, Index, Read Data, and Disk Change if wired) all need the same treatment: a 1kΩ pull-up to +5V, then a 10kΩ/20kΩ divider down to a safe 3.3V logic level before the GPIO, since these lines idle at 5V and the RP2040's GPIOs aren't 5V-tolerant.

**RP2040 <-> onboard microSD** (Waveshare RP2040-PiZero's built-in slot, SPI0 - no external wiring): SCK=GPIO18, MOSI=GPIO19, MISO=GPIO20, CS=GPIO21.

**RP2040 <-> Pi**: one USB cable. No GPIO wiring, and the Pi powers the board.

**RP2040 diagnostics**: GPIO12 (TX) / GPIO13 (RX) at 115200 baud. These moved off USB when the link took it over, so reading boot messages needs a USB-TTL adapter and a common ground - the USB serial monitor will show the binary protocol instead.

**Audio**: the Pi's 3.5mm jack, or a Bluetooth speaker picked in Settings. (A PCM5102 I2S DAC is a drop-in upgrade if its noise floor bothers you - `dtoverlay=hifiberry-dac` in `/boot/firmware/config.txt` plus BCK/LCK/DIN on GPIO18/19/21, no application changes.)

## Pi setup

Raspberry Pi OS 64-bit (tested on Trixie; Bookworm works too). The official 7" DSI touchscreen needs no extra configuration.

**1. Get the code and a SoundFont**

Only `pi-player/` is needed on the Pi. Clone the repo, or copy that folder over (e.g. to `~/pi-player`):

```bash
git clone <this-repo> ~/FloppyPlayer-MIDI    # or copy pi-player/ to the Pi
mkdir -p ~/soundfonts                         # then copy one or more .sf2 files here
```

Anything General MIDI works. Every `.sf2` in `~/soundfonts` (or `pi-player/soundfonts`) shows up in Settings.

**2. Run the installer**

```bash
bash ~/FloppyPlayer-MIDI/pi-player/system/install.sh    # or ~/pi-player/system/install.sh
sudo raspi-config nonint do_boot_behaviour B2          # console autologin, no desktop
sudo reboot
```

`install.sh` runs as your normal user (it asks for sudo where needed) and is safe to re-run. Every step runs even if an earlier one fails, and it ends with a list of any that did. It:

- installs FluidSynth, pygame, BlueZ and a Japanese font (for Touhou titles), and creates the `.venv`
- adds you to the `dialout video input render audio bluetooth` groups
- raises the real-time limits for the `audio` group - without them FluidSynth's audio thread runs at normal priority and playback microstutters
- copies `system/10-quantum.conf` (a 2048-sample PipeWire buffer - the default 128 underruns on a Pi 4) and `system/20-floppyplayer-eq.conf` (the EQ) into `~/.config/pipewire/pipewire.conf.d/`, starts PipeWire at boot, and makes the EQ the default output
- adds a udev rule so the app can dim the backlight, and a sudoers entry so Shut down / Reboot work without a password
- installs `floppyplayer.service` with your user name and paths, and enables it

**3. Watching it**

```bash
journalctl -u floppyplayer -f
```

Settings live in `~/.config/floppyplayer/settings.json`. `--windowed` runs in a window, which is easier with a desktop session up.

**Updating.** Copy the *whole* `pi-player/` folder (except `.venv`), not just the files that changed - the modules depend on each other, and one stale file is enough to crash at startup. Then re-run `install.sh` and `sudo systemctl restart floppyplayer`.

**If it won't start.** The service gives up after 4 crashes in a minute and leaves you at the console. The reason is in the log:

```bash
journalctl -u floppyplayer -b --no-pager | grep -A25 Traceback | tail -40
```

**Shut down properly.** A Pi can corrupt its SD card if power is yanked - use the Shut down button (Settings → System). Once things settle, `raspi-config` → Performance → Overlay FS makes the root filesystem read-only and removes the risk.

**Checking audio health**: `pw-top -b -n 5` - the `ERR` column counts underruns directly, so it should hold steady rather than rise. Under the service, `top -H` should show the audio thread with a negative PR (e.g. `-61`).

### Bringing it up in stages

If something doesn't work, test the layers separately rather than all at once:

```bash
cd ~/FloppyPlayer-MIDI/pi-player    # or ~/pi-player

# 1. Link only - lists what's on the floppy, no audio involved
.venv/bin/python -c "
from link_client import LinkClient, BACKEND_FLOPPY
link = LinkClient('/dev/ttyACM0'); link.hello()
for e in link.list_root(BACKEND_FLOPPY): print(e.filename, e.size)
"

# 2. Synth only - no link, no UI
.venv/bin/python -c "
from playback import Playback
from storage import Track
from pathlib import Path
p = Playback(str(Path.home() / 'soundfonts/YourFont.sf2'))
t = Track(display_name='test.mid', ext='MID', path=Path('test.mid'))
p.load(t, Path('test.mid').read_bytes()); p.play()
import time; time.sleep(20)
"
```

## Building the RP2040 firmware

Open `rp2040-storage/rp2040-storage.ino` in the Arduino IDE with the [arduino-pico](https://github.com/earlephilhower/arduino-pico) core, board "Waveshare RP2040 PiZero". No external libraries beyond what the core bundles (don't separately install a Library Manager "SdFat" package - it conflicts with the core's own copy).

## Using it

The touchscreen has five tabs:

- **Playing** - title, artist/album or MIDI copyright, cover art, progress (tap or drag to seek), transport, a heart to favorite the track, volume (tap or drag). On MIDI tracks a swap button flips between SoundFonts A and B without losing your place. The header shows the source, any status message, the alarm, the sleep timer and the time.
- **Browse** - every folder and track found, tap to play. Each folder has a **Shuffle all** checkbox (untick to leave it out of Shuffle all), and each track a **heart** for favorites.
- **Mixer** (MIDI) - tempo 50-200%, transpose ±12 semitones, per-channel mute/solo, and which SoundFont is playing. Mixer changes reset on the next track.
- **Info** (audio files, in the Mixer's place) - cover art, title, artist, album, track number, year, genre, format (codec, bit depth, sample rate, channels, bitrate), length, file size and play count, all read from the file's own tags.
- **EQ** - 5 bands (60 Hz - 12 kHz, ±12 dB) plus presets. It runs inside PipeWire, so it covers MIDI and audio alike, and the output drops automatically by the largest boost so it can't clip. If it isn't active, the tab says why.
- **Settings** - *Playback*: source, file types, SoundFonts, play mode, autoplay, skip bad tracks, ZUN loops. *System*: output (speaker jack or a Bluetooth speaker - tap Scan with the speaker in pairing mode), alarm, resume on boot, sleep timer (fades out, then pauses), brightness, screen dimming, and exit/restart/reboot/shut down.

**SoundFonts** are held in two slots, A and B, set on the *Soundfonts* page: tap A or B next to a file to fill that slot, and *Play A* / *Play B* to choose which one is playing. The Playing screen's swap button then flips between them mid-song, which makes comparing two SoundFonts on the same passage easy.

**The alarm** (Settings → System → Alarm) starts a random favorite at the set time, fading up over a minute. It cancels the sleep timer and wakes the screen. The Pi has no battery-backed clock, so it needs a network connection at boot to know the time.

**Source** cycles Floppy → SD card → Music folder. **File types** filters to MIDI only, audio only, or both. The floppy is only ever read.

**Play mode**: *Normal* plays in order through every folder; *Shuffle folder* shuffles the current folder; *Shuffle all* shuffles every ticked folder; *Shuffle favorites* shuffles your hearted tracks (everything, if you have none yet); *Repeat track* repeats one track. Next always behaves like the track ending, so it follows the mode too. Shuffles play every track once before repeating, and Prev retraces them.

**Play counts** show as a number under the progress bar and next to each track in Browse. A play counts once you've listened to more than 10% of the track (listening time, so seeking ahead doesn't count); in Repeat track mode each repeat counts.

Favorites, play counts and Shuffle all ticks are remembered per source - and per disk label on floppy and SD, since every floppy has a ROOT folder.

**Resume on boot** picks up where you left off: Normal and Repeat return to the same track and position, *Shuffle folder* starts a random track in the folder you were in, *Shuffle all* anywhere in the ticked folders, and *Shuffle favorites* on a random favorite.

## Disk requirements

- Standard PC-formatted 1.44MB 3.5" floppy, FAT12 (or a FAT32 SD card)
- Playable files in the root directory, or in any subdirectory - each subdirectory holding at least one playable file becomes its own folder. On SD, the library lives under a top-level `/MIDI` folder, with album subfolders inside it. Up to 54 entries per folder.
- Track order follows a leading number in the file's original (pre-8.3-truncation) name, e.g. `10 - Theme.mid` → `10THEM~1.MID` sorts as track 10. Unnumbered files sort last, alphabetically.
- The drive motor stops after 15 seconds without a read and spins back up (about half a second) when needed - a 3.5" drive's heads rest on the disk, so leaving it spinning wears the media.
- A single bad sector doesn't stop playback: the RP2040 retries, and a file that genuinely won't read is skipped with a message rather than hanging. Because whole files are loaded before playback starts, a bad sector fails cleanly up front instead of interrupting a song mid-play.

## ZUN/Touhou loop points

MIDI files that mark an internal loop region with CC#2 loop back to it at the end of the track instead of ending. The **ZUN loops** setting controls how many times (default: forever, like the games). Files without the marker are unaffected.

## License

MIT - see [LICENSE](LICENSE). The PIO flux-capture core is a port of the approach used in [adafruit/Adafruit_Floppy](https://github.com/adafruit/Adafruit_Floppy).
