# FloppyPlayer-MIDI

A standalone music player that reads and plays files directly off a real 3.5" floppy disk - no PC involved. An RP2040 reads the floppy's raw magnetic flux and decodes FAT12 in firmware; a Raspberry Pi 4 renders the MIDI through FluidSynth and drives a touchscreen UI.

It also plays from USB drives or the Pi's own music folder, and handles MP3/OGG/FLAC/WAV alongside `.MID` - but the floppy is the point.

This project has been through several architectures as it grew: one microcontroller doing everything, a two-chip split, a three-chip split with an ESP32, back to two chips, and now an RP2040 paired with a Raspberry Pi. Earlier designs aren't part of the repo; the original single-chip version is still in the git history.

## Architecture

```
[Floppy drive]                   [USB drives]
      |                                |
      v                                v
+------------------+            +----------------------------+
| RP2040           |            | Raspberry Pi 4 (4GB)       |
| storage server   |<-- USB --->| FluidSynth + player + GUI  |--> 3.5mm jack
| (floppy,         |   (CDC     | 7" DSI touchscreen         |    or Bluetooth
|  passive)        |   serial)  |                            |    speaker
+------------------+            +----------------------------+
```

- **RP2040 (`rp2040-storage/`) - storage server.** Reads the floppy drive's raw flux and decodes FAT12 in firmware. Its microSD slot keeps a copy of every floppy file read, so a disk that's been played before loads from the card without spinning the drive. Purely passive: it never initiates anything, only answers file requests. Unchanged in substance across every architecture this project has had, because raw flux capture needs deterministic sub-microsecond timing that only its PIO hardware provides.
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
- Optional: USB drives with music, plugged into the Pi

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

Anything General MIDI works. Every `.sf2` in `~/soundfonts` (or `pi-player/soundfonts`) shows up in Settings, and so does every folder of them (see *Folder SoundFonts* below).

**2. Run the installer**

```bash
bash ~/FloppyPlayer-MIDI/pi-player/system/install.sh    # or ~/pi-player/system/install.sh
sudo raspi-config nonint do_boot_behaviour B2          # console autologin, no desktop
sudo reboot
```

`install.sh` runs as your normal user (it asks for sudo where needed) and is safe to re-run. Every step runs even if an earlier one fails, and it ends with a list of any that did. It:

- installs FluidSynth, pygame, numpy (for the audio bars), BlueZ and a Japanese font (for Touhou titles), and creates the `.venv`
- adds you to the `dialout video input render audio bluetooth` groups
- raises the real-time limits for the `audio` group - without them FluidSynth's audio thread runs at normal priority and playback microstutters
- copies `system/10-quantum.conf` (a 2048-sample PipeWire buffer - the default 128 underruns on a Pi 4) and `system/20-floppyplayer-eq.conf` (the EQ) into `~/.config/pipewire/pipewire.conf.d/`, starts PipeWire at boot, and makes the EQ the default output
- adds udev rules so the app can dim the backlight and USB drives mount read-only when plugged in
- sets BlueZ's `AlwaysPairable`, without which a paired speaker is forgotten at every reboot
- adds a sudoers entry so Shut down / Reboot work without a password
- installs `floppyplayer.service` with your user name and paths, and enables it

**3. Watching it**

```bash
journalctl -u floppyplayer -f
```

Settings live in `~/.config/floppyplayer/settings.json`, with `track-ids.json` (what each file is), `plays.log` (every counted play) and the audio-bar cache beside them. `--windowed` runs in a window, which is easier with a desktop session up.

**Updating.** Copy the *whole* `pi-player/` folder (except `.venv`), not just the files that changed - the modules depend on each other, and one stale file is enough to crash at startup. Then re-run `install.sh` and `sudo systemctl restart floppyplayer`.

**If it won't start.** The service gives up after 4 crashes in a minute and leaves you at the console. The reason is in the log:

```bash
journalctl -u floppyplayer -b --no-pager | grep -A25 Traceback | tail -40
```

**Shut down properly.** A Pi can corrupt its SD card if power is yanked - use the Shut down button (Settings → System). Once things settle, `raspi-config` → Performance → Overlay FS makes the root filesystem read-only and removes the risk - but then settings, favorites, play counts and Bluetooth pairings changed after that are lost at every reboot.

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

Six tabs along the bottom: **Playing**, **Queue**, **Browse**, **Mixer** (**Info** on audio files), **EQ** and **Settings**. What it can do:

**Playing**
- MIDI through FluidSynth, and MP3/OGG/FLAC/WAV alongside it
- Seek, volume, favorites and play counts; tap a track to play it, **+** to queue it
- Play modes: normal, repeat, and shuffle by folder, by everything, or by favorites - with or without the folders you've unticked
- Prev walks back through what you actually played, whatever put it there; Next walks forward again
- A queue that cuts in and hands back to the play mode afterwards
- Tempo, transpose and per-channel mute/solo on MIDI; ZUN loop points and GS/XG setup messages are honoured
- Resume on boot, a sleep timer, and separate weekday/weekend alarms with Snooze (the Pi has no clock of its own, so alarms need the network at boot)

**Sources**
- The floppy (read-only, cached on the RP2040's SD card), the Pi's music folder, and USB drives
- Switches to a disk or drive as you plug it in, if you turn that on
- Filter to MIDI only, audio only, or both
- Tracks are known by their contents, so renaming, moving or copying one keeps its hearts, counts and history - and two copies count as one track

**Sound**
- Two SoundFont slots, swapped mid-song without losing your place; an instrument the playing one lacks comes from the other, then from any other SoundFont
- Folder SoundFonts - a folder of one-instrument `.sf2` files used as one SoundFont (below)
- 5-band EQ inside PipeWire, so it covers MIDI and audio alike
- Loudness match from ReplayGain tags (tag a library on a PC with e.g. `rsgain easy <folder>`)
- Polyphony from 8 voices to unlimited, applied while a song plays
- Output to the 3.5mm jack or a Bluetooth speaker, which it pairs with, waits for at boot and reconnects to on its own

**On screen**
- Cover art, lyrics (`.lrc` beside the song, or its own tags), a spectrum on audio files
- 16 channel meters in per-channel colours, and a falling-notes view when you tap them
- Listening stats over 7 days, 30 days or all time, and a Health page - addresses, temperature, throttling, memory, load, uptime, free space
- Brightness and dimming, with an optional clock while dimmed
- Scrollbars on every long list: drag them, or touch where you want to be

### Folder SoundFonts

A sub-folder of `soundfonts/` full of one-instrument `.sf2` files (like the Edirol SD-90 packs) is treated as one SoundFont. An `instruments.txt` in the folder maps files to instruments:

```
1   = 01. St. Piano 1.sf2      # General MIDI program 1-128, as on GM charts
6.1 = 06.01 Brite FM EP.sf2    # variation 1 of program 6 (bank select 1)
drums 9 = Room Kit.sf2         # drum kit 9 (GS: 1 Standard, 9 Room, 17 Power)
```

A `#` switches a line off; without the file, a leading number in each file name is used. Only instruments a song needs are loaded, and at most 40% of RAM is kept loaded, so the first song to use a heavy instrument takes a few seconds. `pi-player/soundfonts/Edirol_SD-90/instruments.txt` is the mapping for the SD-90 packs from Musical Artifacts.

## Disk requirements

- Standard PC-formatted 1.44MB 3.5" floppy, FAT12
- Playable files in the root directory, or in any subdirectory - each subdirectory holding at least one playable file becomes its own folder. Up to 54 entries per folder.
- Track order follows a leading number in the file's original (pre-8.3-truncation) name, e.g. `10 - Theme.mid` → `10THEM~1.MID` sorts as track 10. Unnumbered files sort last, alphabetically.
- Files already read are copied to the RP2040's SD card (`/CACHE/`, one folder per disk) and served from there next time. The copy is tied to the disk's FAT and directory, so changing the disk's files makes it read fresh. Delete `/CACHE` to clear it.
- The drive motor stops after 15 seconds without a read and spins back up (about half a second) when needed - a 3.5" drive's heads rest on the disk, so leaving it spinning wears the media.
- A single bad sector doesn't stop playback: the RP2040 retries, and a file that genuinely won't read is skipped with a message rather than hanging. Because whole files are loaded before playback starts, a bad sector fails cleanly up front instead of interrupting a song mid-play.

## ZUN/Touhou loop points

MIDI files that mark an internal loop region with CC#2 loop back to it at the end of the track instead of ending. The **ZUN loops** setting controls how many times (default: once; *Forever* loops like the games). Files without the marker are unaffected.

## GS and XG setup messages

These files open with SysEx for a Roland SC-88Pro or Yamaha XG module. It's passed to FluidSynth, and part-mode messages are acted on here too: a song that puts a second drum kit on another channel plays it as drums rather than as whatever melodic patch was selected, and GM/GS/XG resets put drums back on channel 10. Seeks and loops rebuild that along with everything else.

## License

MIT - see [LICENSE](LICENSE). The PIO flux-capture core is a port of the approach used in [adafruit/Adafruit_Floppy](https://github.com/adafruit/Adafruit_Floppy).
