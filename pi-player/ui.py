"""Touchscreen UI for the 7" DSI panel (800x480): immediate-mode pygame on KMS/DRM."""

from __future__ import annotations

import io
import math
import re
import time

import pygame

import eq
import gm
import pianoroll
import stats

W, H = 800, 480
TAB_H = 64
HEADER_H = 52
ART_SIZE = 100
INFO_ART_SIZE = 200

BG = (18, 18, 22)
PANEL = (32, 32, 40)
ACCENT = (120, 170, 255)
TEXT = (235, 235, 240)
DIM = (140, 140, 155)
WARN = (255, 170, 90)
HEART = (255, 95, 125)
MUTED_BG = (90, 40, 40)
SOLO_BG = (40, 70, 120)

SCREEN_NOW, SCREEN_QUEUE, SCREEN_BROWSE, SCREEN_MIXER, SCREEN_EQ, SCREEN_SETTINGS = range(6)
TAB_LABELS = ("Playing", "Queue", "Browse", "Mixer", "EQ", "Settings")
METER_W = 128  # 16 channel bars where a MIDI file has no cover art
DRUM_CHANNEL = 9
UNUSED = (26, 26, 32)


def _hues(hues, saturation=55, value=100):
    colors = []
    for hue in hues:
        color = pygame.Color(0)
        color.hsva = (hue % 360, saturation, value, 100)
        colors.append((color.r, color.g, color.b))
    return colors


# Channels a golden angle apart, so neighbours never look alike; drums keep orange.
CHANNEL_COLORS = _hues(200 + i * 137.5 for i in range(16))
CHANNEL_COLORS[DRUM_CHANNEL] = (255, 170, 90)


def _tint(color, amount=0.22):
    """A channel's colour mixed into the panel: enough to tell cells apart, dark enough to read."""
    return tuple(round(p + (c - p) * amount) for p, c in zip(PANEL, color))
# Frequency bars run warm (bass) to cool (treble).
BAND_COLORS = _hues(i * 17 for i in range(16))

DETAIL_PAGES = {"stats": "Stats", "health": "Health"}  # the Mixer/Info tab's other pages
SOURCE_LABELS = {"floppy": "Floppy", "local": "Music folder", "usb": "USB"}
TYPE_LABELS = {"all": "MIDI + audio", "midi": "MIDI only", "audio": "Audio only"}
ALARM_ROWS = (("weekday", "Mon - Fri"), ("weekend", "Sat - Sun"))
PLAY_MODE_LABELS = {
    "normal": "Normal",
    "shuffle_folder": "Shuffle folder",
    "shuffle_all": "Shuffle all",
    "shuffle_favorites": "Shuffle favorites",
    "shuffle_favorites_ticked": "Favorites, ticked folders",
    "repeat": "Repeat track",
}

SCROLLBAR_W = 14
SCROLL_THUMB_MIN = 56   # always big enough for a finger
ROW_H = 56
SETTING_ROW_H = 40
KEYS_H = 64          # the piano roll's keyboard
ROLL_SECONDS = 3.0   # how far ahead the falling notes show
CLOCK_COLOR = (175, 175, 190)
STATS_PERIODS = ((7, "7 days"), (30, "30 days"), (None, "All time"))

# Fonts with Japanese glyphs, for Shift-JIS MIDI titles and tagged MP3s.
CJK_FONTS = "notosanscjkjp,notosanscjk,droidsansfallback,wenquanyimicrohei,takaopgothic,vlgothic"
# Text in these ranges needs the CJK font (SDL_ttf can't report missing glyphs).
_CJK = re.compile("[⺀-鿿가-힯豈-﫿＀-￯]")


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _fmt_idle(seconds: int) -> str:
    if not seconds:
        return "Never"
    return f"{seconds} s" if seconds < 60 else f"{seconds // 60} min"


def _tangent(center, r: float, tip, outward: int):
    """Where a line from tip touches the circle, on its outer side."""
    vx, vy = tip[0] - center[0], tip[1] - center[1]
    d = math.hypot(vx, vy)
    theta = math.acos(min(1.0, r / d))
    ux, uy = vx / d, vy / d
    points = [
        (center[0] + r * (ux * math.cos(a) - uy * math.sin(a)),
         center[1] + r * (ux * math.sin(a) + uy * math.cos(a)))
        for a in (theta, -theta)
    ]
    return min(points) if outward < 0 else max(points)


def _fill_heart(surface, color, s: float, inset: float) -> None:
    """Two round lobes joined to a point, filling an s-by-s square."""
    r = 0.27 * s - inset
    cy = 0.33 * s
    left, right = (0.28 * s, cy), (0.72 * s, cy)
    tip = (0.5 * s, 0.93 * s - inset * 1.7)
    pygame.draw.circle(surface, color, left, r)
    pygame.draw.circle(surface, color, right, r)
    pygame.draw.polygon(surface, color, [
        _tangent(left, r, tip, -1), left, right, _tangent(right, r, tip, +1), tip,
    ])
    if inset:
        # Fill between the shrunken lobes, or the outline's notch shows a spike.
        outer_r = 0.27 * s
        notch = cy - math.sqrt(outer_r ** 2 - (0.22 * s) ** 2)
        pygame.draw.polygon(surface, color, [left, (0.5 * s, notch + inset), right])


def _fill_swap(surface, color, s: float) -> None:
    """Two arrows circling each other, filling an s-by-s square."""
    shaft = 0.1 * s
    for top in (True, False):
        y = (0.34 if top else 0.66) * s
        if top:  # points right
            pygame.draw.rect(surface, color, (0.12 * s, y - shaft / 2, 0.68 * s, shaft))
            head = [(0.9 * s, y), (0.66 * s, y - 0.15 * s), (0.66 * s, y + 0.15 * s)]
        else:    # points left
            pygame.draw.rect(surface, color, (0.2 * s, y - shaft / 2, 0.68 * s, shaft))
            head = [(0.1 * s, y), (0.34 * s, y - 0.15 * s), (0.34 * s, y + 0.15 * s)]
        pygame.draw.polygon(surface, color, head)


def _fill_lyrics(surface, color, s: float) -> None:
    """Lines of text beside a note."""
    for i, width in enumerate((0.5, 0.5, 0.34)):
        pygame.draw.rect(surface, color, (0.06 * s, (0.22 + i * 0.22) * s, width * s, 0.1 * s),
                         border_radius=round(0.05 * s))
    pygame.draw.circle(surface, color, (0.72 * s, 0.74 * s), 0.13 * s)
    pygame.draw.rect(surface, color, (0.78 * s, 0.14 * s, 0.08 * s, 0.6 * s))
    pygame.draw.polygon(surface, color, [(0.86 * s, 0.14 * s), (0.98 * s, 0.3 * s), (0.86 * s, 0.34 * s)])


def _fill_note(surface, color, s: float) -> None:
    """A single eighth note."""
    pygame.draw.circle(surface, color, (0.4 * s, 0.72 * s), 0.16 * s)
    pygame.draw.rect(surface, color, (0.48 * s, 0.12 * s, 0.08 * s, 0.6 * s))
    pygame.draw.polygon(surface, color, [(0.56 * s, 0.12 * s), (0.8 * s, 0.32 * s),
                                         (0.8 * s, 0.46 * s), (0.56 * s, 0.28 * s)])


def _fill_info(surface, color, s: float) -> None:
    """An "i" in a ring."""
    pygame.draw.circle(surface, color, (0.5 * s, 0.5 * s), 0.46 * s, width=round(0.08 * s))
    pygame.draw.circle(surface, color, (0.5 * s, 0.3 * s), 0.065 * s)
    pygame.draw.rect(surface, color, (0.445 * s, 0.42 * s, 0.11 * s, 0.34 * s),
                     border_radius=round(0.03 * s))


def info_icon(size: int, color=TEXT):
    big = size * 4
    surface = pygame.Surface((big, big), pygame.SRCALPHA)
    _fill_info(surface, color, big)
    return pygame.transform.smoothscale(surface, (size, size))


def rounded(surface, radius: int):
    """A copy with rounded corners, matching the tiles; smoothed by drawing the mask 4x."""
    width, height = surface.get_size()
    # White throughout, so the MIN blend below only ever lowers alpha - colours stay exact.
    mask = pygame.Surface((width * 4, height * 4), pygame.SRCALPHA)
    mask.fill((255, 255, 255, 0))
    pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=radius * 4)
    out = pygame.Surface((width, height), pygame.SRCALPHA)
    out.blit(surface, (0, 0))
    out.blit(pygame.transform.smoothscale(mask, (width, height)), (0, 0),
             special_flags=pygame.BLEND_RGBA_MIN)
    return out


def note_icon(size: int, color=TEXT):
    big = size * 4
    surface = pygame.Surface((big, big), pygame.SRCALPHA)
    _fill_note(surface, color, big)
    return pygame.transform.smoothscale(surface, (size, size))


def swap_icon(size: int, color=TEXT):
    big = size * 4
    surface = pygame.Surface((big, big), pygame.SRCALPHA)
    _fill_swap(surface, color, big)
    return pygame.transform.smoothscale(surface, (size, size))


def lyrics_icon(size: int, color=TEXT):
    big = size * 4
    surface = pygame.Surface((big, big), pygame.SRCALPHA)
    _fill_lyrics(surface, color, big)
    return pygame.transform.smoothscale(surface, (size, size))


def heart_icon(size: int, filled: bool):
    """Drawn 4x larger and scaled down, which smooths the edges."""
    big = size * 4
    surface = pygame.Surface((big, big), pygame.SRCALPHA)
    _fill_heart(surface, HEART if filled else DIM, big, 0)
    if not filled:
        _fill_heart(surface, (0, 0, 0, 0), big, 0.1 * big)  # punch out the middle
    return pygame.transform.smoothscale(surface, (size, size))


def decode_art(data: bytes | None, size: int = ART_SIZE):
    """Cover art bytes to a surface fitted inside size x size, corners rounded; or None."""
    if not data:
        return None
    try:
        image = pygame.image.load(io.BytesIO(data))
        scale = size / max(image.get_width(), image.get_height())
        dims = (max(1, round(image.get_width() * scale)), max(1, round(image.get_height() * scale)))
        try:
            image = pygame.transform.smoothscale(image, dims)
        except ValueError:  # smoothscale needs 24/32-bit; palette PNGs aren't
            image = pygame.transform.scale(image, dims)
        return rounded(image, max(6, size // 10))
    except Exception:
        return None


class Ui:
    def __init__(self, app, fullscreen: bool = True):
        self.app = app
        self.rotate_touch = bool(app.settings.get("rotate_touch_180", False))
        pygame.init()
        pygame.display.set_caption("FloppyPlayer")
        # SCALED fits the 800x480 surface to any display and maps touch for us.
        flags = pygame.SCALED
        flags |= pygame.FULLSCREEN if fullscreen else pygame.RESIZABLE
        self.surface = pygame.display.set_mode((W, H), flags)
        pygame.mouse.set_visible(False)
        self._cjk_path = pygame.font.match_font(CJK_FONTS)
        self._fallbacks: dict[int, pygame.font.Font | None] = {}
        self.font_lg = pygame.font.SysFont("dejavusans", 34)
        self.font_md = pygame.font.SysFont("dejavusans", 24)
        self.font_sm = pygame.font.SysFont("dejavusans", 19)
        self.font_xs = pygame.font.SysFont("dejavusans", 16)
        self._font_sizes = {
            id(self.font_lg): 34, id(self.font_md): 24, id(self.font_sm): 19, id(self.font_xs): 16,
        }
        self.clock = pygame.time.Clock()
        self.screen = SCREEN_NOW
        self.scroll = 0
        self.running = True
        self.last_tap = (0, 0)
        self.browse_folder: int | str | None = None  # a folder, "most" or "recent"; None = the list
        self.confirm_action: str | None = None
        self.settings_page = "playback"  # or a sub-page: system, bluetooth, soundfonts, alarm
        self.mixer_solo = False           # channel taps solo instead of mute
        self._text_cache: dict = {}
        self._icons: dict = {}
        self._last_signature = None
        self._hits: list[tuple] = []  # (rect, action, drag, release)
        self._drag = None          # action of the slider being dragged
        self._release = None       # called when that touch lifts
        self._drag_pos = None      # latest drag position, applied once a frame
        self._scrub: float | None = None  # progress-bar drag, applied on release
        self._last_input = time.monotonic()
        self._dimmed = False
        self._meter_box: pygame.Rect | None = None  # where this frame drew the meters
        self._last_meters = None
        self.roll_open = False       # the falling-notes view, over everything
        self._roll: tuple = (None, None)  # (song, its Roll)
        self.detail_page = "main"    # the Mixer/Info tab's page: "main" or "stats"
        self.stats_days: int | None = 7
        self._font_clock = None
        self._voices_seen = None
        self._voices_at = 0.0
        self._drag_bar = False     # the scrollbar is being dragged

    # -- helpers ---------------------------------------------------------

    def _touch(self, rect, action, drag: bool = False, release=None) -> pygame.Rect:
        rect = pygame.Rect(rect)
        self._hits.append((rect, action, drag, release))
        return rect

    def _fallback_for(self, font):
        """Same-size CJK font, for text the regular font has no glyphs for."""
        key = id(font)
        if key not in self._fallbacks:
            size = self._font_sizes.get(key)
            self._fallbacks[key] = (
                pygame.font.Font(self._cjk_path, size) if self._cjk_path and size else None
            )
        return self._fallbacks[key]

    def _render(self, text: str, font, color, max_w):
        """Cached text rendering - most strings are unchanged frame to frame."""
        key = (text, id(font), color, max_w)
        surf = self._text_cache.get(key)
        if surf is None:
            if not text.isascii() and _CJK.search(text):
                font = self._fallback_for(font) or font
            if max_w and font.size(text)[0] > max_w:
                while len(text) > 1 and font.size(text + "...")[0] > max_w:
                    text = text[:-1]
                text += "..."
            surf = font.render(text, True, color)
            if len(self._text_cache) > 400:
                self._text_cache.clear()
            self._text_cache[key] = surf
        return surf

    def _text(self, text, font, color, center=None, topleft=None, topright=None, max_w=None):
        surf = self._render(str(text), font, color, max_w)
        rect = surf.get_rect()
        if center:
            rect.center = center
        if topleft:
            rect.topleft = topleft
        if topright:
            rect.topright = topright
        self.surface.blit(surf, rect)
        return rect

    def _button(self, rect, label, action, *, color=PANEL, fg=TEXT, font=None):
        rect = self._touch(rect, action)
        pygame.draw.rect(self.surface, color, rect, border_radius=10)
        self._text(label, font or self.font_md, fg, center=rect.center, max_w=rect.width - 8)
        return rect

    def _toggle(self, rect, label, active, action, font=None):
        return self._button(
            rect, label, action, color=ACCENT if active else PANEL,
            fg=BG if active else TEXT, font=font or self.font_sm,
        )

    # -- event loop ------------------------------------------------------

    def run(self) -> None:
        # Redraw only when something on screen changed - it saves CPU for audio.
        while self.running:
            acted = self._handle_events()
            self.app.tick()
            self._update_dim()
            if self.roll_open and not self._dimmed:
                self._draw()  # the notes move every frame
                pygame.display.flip()
                self._last_signature = None
                self.clock.tick(30)
                continue
            signature = self._signature()
            if acted or signature != self._last_signature:
                self._draw()
                pygame.display.flip()
                self._last_signature = signature
            elif self._meter_box is not None:
                self._update_meters()
            self.clock.tick(20)

    def _update_meters(self) -> None:
        """Repaint just the meters when nothing else changed - far cheaper than a frame."""
        levels = self.app.visual_levels
        if levels is None:
            return
        shown = tuple(round(level * 20) for level in levels)
        if shown != self._last_meters:
            self._last_meters = shown
            self._paint_meters(self._meter_box, levels)
            pygame.display.update(self._meter_box)

    def _signature(self):
        """What the screen shows; whole seconds, so playback redraws once a second."""
        app, s = self.app, self.app.settings
        if self._dimmed and s["dim_clock"]:  # the clock face: once a minute is plenty
            return ("clock", time.strftime("%H:%M"), app.track_title, app.is_playing,
                    app.next_alarm_text)
        midi = app.midi
        sleep = app.sleep_remaining
        voices = self._voices() if midi and self.screen == SCREEN_MIXER else None
        readings = (tuple(sorted((k, str(v)) for k, v in app.health.items()))
                    if self.detail_page == "health" else None)
        return (
            self.detail_page, self.stats_days, voices, readings,
            self.screen, self.scroll, self.browse_folder, self.confirm_action,
            self.settings_page, self.mixer_solo, self._dimmed, self._drag_bar,
            time.strftime("%H:%M"), None if self._scrub is None else round(self._scrub, 3),
            app.status_text, app.current_track_name, app.current_folder_name,
            app.track_title, app.track_subtitle, id(app.track_art), app.disk_label,
            tuple(app.queue), app.lyric_line, id(app.lyrics),
            app.is_playing, int(app.position), int(app.duration),
            app.loop_status, app.track_kind, app.soundfont_slot,
            app.slot_name("a"), app.slot_name("b"),
            id(app.folders), app.folder_index, app.track_index,
            None if sleep is None else math.ceil(sleep / 60),
            (id(midi.song), midi.tempo, midi.transpose, midi.muted, midi.solo) if midi else None,
            app.eq_available, app.eq_problem, app.bt_status, app.bt_busy, tuple(app.bt_devices),
            round(s["volume"], 3), s["source"], s["file_types"], s["play_mode"],
            s["autoplay"], s["skip_bad_tracks"], s["loop_repeats"], s["back_limit"],
            tuple(s["eq_gains"]), s["eq_preset"], s["output"], s["output_name"],
            s["shuffle_excluded"], s["favorites"], s["play_counts"], s["recent"], app.ids_version,
            s["resume_on_boot"], s["brightness"], s["dim_after"], s["dim_clock"], s["dim_level"],
            s["alarm_enabled"], s["alarm_time"], round(s["alarm_volume"], 3),
            s["alarm_weekend_enabled"], s["alarm_weekend_time"], s["alarm_pick"], s["alarm_snooze"],
            app.alarm_ringing,
            s["show_lyrics"], s["loudness_match"], s["auto_source"], s["synth_polyphony"],
            s["show_audio_bars"], app.audio_bars is not None,
        )

    def _voices(self):
        """The synth's voice count, read 4 times a second - enough to follow, cheap to redraw."""
        now = time.monotonic()
        if now - self._voices_at >= 0.25:
            midi = self.app.midi
            self._voices_seen = midi.voices() if midi else None
            self._voices_at = now
        return self._voices_seen

    def stop(self) -> None:
        self.running = False

    def close(self) -> None:
        """Tear pygame down - after playback, since it takes the mixer with it."""
        pygame.quit()

    def _handle_events(self) -> bool:
        """True if anything happened, so run() knows to redraw."""
        acted = False
        for event in pygame.event.get():
            acted = True
            if event.type == pygame.QUIT:
                self.app.quit()
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                self.app.quit()
            elif event.type == pygame.FINGERDOWN:
                # Touch under KMSDRM arrives as fingers, normalised 0-1.
                self._press(self._finger_pos(event))
            elif event.type == pygame.FINGERMOTION:
                self._drag_pos = self._finger_pos(event)
            elif event.type == pygame.FINGERUP:
                self._lift()
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                # Skip the mouse event SDL synthesises from a touch.
                if not getattr(event, "touch", False):
                    self._press(event.pos)
            elif event.type == pygame.MOUSEMOTION and event.buttons[0]:
                if not getattr(event, "touch", False):
                    self._drag_pos = event.pos
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                self._lift()
            elif event.type == pygame.MOUSEWHEEL:
                self.scroll = max(0, self.scroll - event.y * ROW_H)
        if self._drag and self._drag_pos:
            self.last_tap = self._drag_pos
            self._drag()
        self._drag_pos = None
        return acted

    def _finger_pos(self, event):
        return self._orient((int(event.x * W), int(event.y * H)))

    def _orient(self, pos):
        """Mirror touch when the display is rotated 180 in cmdline.txt."""
        if not self.rotate_touch:
            return pos
        return (W - pos[0], H - pos[1])

    def _press(self, pos) -> None:
        self._last_input = time.monotonic()
        if self._dimmed:
            self.wake()
            return  # the touch that wakes the screen doesn't also press a button
        self.last_tap = pos
        for rect, action, drag, release in self._hits:
            if rect.collidepoint(pos):
                self._drag = action if drag else None
                self._release = release
                action()
                return

    def _lift(self) -> None:
        release, self._release, self._drag = self._release, None, None
        if release:
            release()

    def _update_dim(self) -> None:
        dim_after = self.app.settings["dim_after"]
        if self._dimmed or not dim_after:
            return
        if time.monotonic() - self._last_input > dim_after:
            self._dimmed = True
            self.app.set_dimmed(True)

    def wake(self) -> None:
        """Undim, also called by the alarm."""
        self._dimmed = False
        self._last_input = time.monotonic()
        self.app.set_dimmed(False)

    # -- drawing ---------------------------------------------------------

    def _draw(self) -> None:
        self._hits = []
        self._meter_box = None
        self.surface.fill(BG)
        if self._dimmed and self.app.settings["dim_clock"]:
            self._draw_clock_face()
            return
        if self.roll_open:
            if self.app.midi:
                self._draw_roll()
                return
            self.roll_open = False  # the track changed to audio
        self._draw_header()
        body = pygame.Rect(0, HEADER_H, W, H - HEADER_H - TAB_H)
        (
            self._draw_now, self._draw_queue, self._draw_browse, self._draw_mixer,
            self._draw_eq, self._draw_settings,
        )[self.screen](body)
        self._draw_tabs()
        if self._dimmed:
            self._dim_screen()

    def _dim_screen(self) -> None:
        """Without backlight control the panel stays lit, so darken the screen itself instead -
        the dimmed screen still shows what's playing, just faintly."""
        if self.app.backlight.available:
            return
        shade = pygame.Surface((W, H))
        shade.fill((0, 0, 0))  # black, so Dim to Off really is a black screen
        shade.set_alpha(round(255 * (1 - max(0.0, min(self.app.settings["dim_level"], 1.0)))))
        self.surface.blit(shade, (0, 0))

    def _draw_header(self) -> None:
        app = self.app
        pygame.draw.rect(self.surface, PANEL, (0, 0, W, HEADER_H))
        source = SOURCE_LABELS.get(app.settings["source"], "?")
        if app.disk_label:
            source = f"{source}: {app.disk_label}"
        self._text(source, self.font_sm, DIM, topleft=(16, 16), max_w=220)
        clock = self._text(time.strftime("%H:%M"), self.font_md, TEXT, topright=(W - 16, 12))
        right = clock.left - 20
        sleep = app.sleep_remaining
        if sleep is not None:
            rect = self._text(f"Sleep {math.ceil(sleep / 60)} min", self.font_sm, ACCENT,
                              topright=(right, 16))
            right = rect.left - 16
        next_alarm = app.next_alarm_text
        if next_alarm:
            rect = self._text(f"Alarm {next_alarm}", self.font_sm, DIM,
                              topright=(right, 16))
            right = rect.left - 16
        status = app.status_text or app.bt_status
        if status:
            self._text(status, self.font_sm, WARN, topleft=(252, 16), max_w=right - 252)

    def _draw_now(self, body: pygame.Rect) -> None:
        app = self.app
        lyrics = app.lyrics if app.settings["show_lyrics"] else None
        if lyrics is not None:
            self._draw_lyrics(body, lyrics)
        else:
            self._draw_title(body)

        # Loop state and anything the mixer has changed.
        notes = [app.loop_status]
        midi = app.midi
        if midi:
            if midi.tempo != 1.0:
                notes.append(f"Tempo {round(midi.tempo * 100)}%")
            if midi.transpose:
                notes.append(f"Key {midi.transpose:+d}")
            if midi.muted or midi.solo:
                notes.append("Mixer active")
        line = "   |   ".join(n for n in notes if n)
        if line:
            self._text(line, self.font_sm, ACCENT, center=(W // 2, body.top + 128), max_w=W - 40)

        # Progress - drag to scrub, seeks when the finger lifts.
        pos, dur = app.position, app.duration
        if self._scrub is not None and dur > 0:
            pos = self._scrub * dur
        bar = pygame.Rect(60, body.top + 152, W - 120, 10)
        if dur > 0:
            self._touch(
                bar.inflate(0, 36),  # thin to look at, easy to hit
                lambda: setattr(self, "_scrub",
                                max(0.0, min((self.last_tap[0] - bar.left) / bar.width, 1.0))),
                drag=True, release=self._commit_scrub,
            )
        pygame.draw.rect(self.surface, PANEL, bar, border_radius=5)
        if dur > 0:
            filled = bar.copy()
            filled.width = int(bar.width * min(pos / dur, 1.0))
            pygame.draw.rect(self.surface, ACCENT, filled, border_radius=5)
        self._text(_fmt_time(pos), self.font_sm, DIM, topleft=(60, bar.bottom + 6))
        self._text(_fmt_time(dur), self.font_sm, DIM, topright=(W - 60, bar.bottom + 6))
        plays = app.play_count(app.folder_index, app.track_index)
        if plays:
            self._text(str(plays), self.font_sm, DIM, center=(W // 2, bar.bottom + 17))

        y = body.top + 204
        self._button((W // 2 - 230, y, 130, 64), "<<", app.prev_track)
        self._button(
            (W // 2 - 65, y, 130, 64),
            "Pause" if app.is_playing else "Play",
            app.toggle_play,
            color=ACCENT, fg=BG,
        )
        self._button((W // 2 + 100, y, 130, 64), ">>", app.next_track)
        if app.current_track_name:
            heart = self._button((W - 96, y, 76, 64), "", app.toggle_current_favorite)
            self._heart(heart.center, app.current_is_favorite, size=32)
        if app.alarm_ringing and app.snooze_minutes:
            # While the alarm plays, that corner snoozes instead of its usual job.
            button = self._button((20, y, 76, 64), "", app.snooze, color=WARN, fg=BG)
            self._text("Snooze", self.font_xs, BG, center=(button.centerx, button.top + 24))
            self._text(f"{app.snooze_minutes} min", self.font_xs, BG,
                       center=(button.centerx, button.bottom - 18))
        elif app.midi:  # soundfonts only change MIDI
            swap = self._button((20, y, 76, 64), "", app.swap_soundfont)
            self._icon("swap", (swap.centerx, swap.top + 24), 32, color=ACCENT)
            self._text(app.soundfont_slot.upper(), self.font_sm, TEXT,
                       center=(swap.centerx, swap.bottom - 16))
        elif app.lyrics is not None:
            # Lyrics take the soundfont button's place on audio files.
            showing = app.settings["show_lyrics"]
            button = self._button((20, y, 76, 64), "", app.toggle_lyrics,
                                  color=ACCENT if showing else PANEL)
            self._icon("lyrics", (button.centerx, button.top + 24), 30, color=BG if showing else ACCENT)
            self._text("Lyrics", self.font_xs, BG if showing else TEXT,
                       center=(button.centerx, button.bottom - 14))
        elif app.current_track_name:
            # No lyrics: the same spot opens the file's Info instead.
            button = self._button((20, y, 76, 64), "", lambda: self._select_screen(SCREEN_MIXER))
            self._icon("info", (button.centerx, button.top + 24), 30, color=ACCENT)
            self._text("Info", self.font_xs, TEXT, center=(button.centerx, button.bottom - 14))

        # Volume - tap or drag along the bar.
        vol_bar = pygame.Rect(60, y + 96, W - 120, 26)
        self._touch(
            vol_bar.inflate(0, 20),
            lambda: app.set_volume((self.last_tap[0] - vol_bar.left) / vol_bar.width),
            drag=True,
        )
        pygame.draw.rect(self.surface, PANEL, vol_bar, border_radius=13)
        level = vol_bar.copy()
        level.width = int(vol_bar.width * app.settings["volume"])
        pygame.draw.rect(self.surface, ACCENT, level, border_radius=13)
        self._text("Volume", self.font_sm, DIM, topleft=(60, vol_bar.bottom + 6))
        self._text(f"{round(app.settings['volume'] * 100)}%", self.font_sm, DIM,
                   topright=(W - 60, vol_bar.bottom + 6))

    def _draw_title(self, body: pygame.Rect) -> None:
        """The art box, then the title, artist and file beside it."""
        app = self.app
        box = self._draw_art_box(pygame.Rect(20, body.top + 12, ART_SIZE, ART_SIZE))
        left = box.right + 16 if box else 20

        title = app.track_title or "No track"
        self._text_clear_of(title, self.font_lg, TEXT, body.top + 34, left)
        if app.track_subtitle:
            self._text_clear_of(app.track_subtitle, self.font_sm, DIM, body.top + 70, left)

        details = [app.current_folder_name]
        if app.track_kind:
            details.append(f"[{app.track_kind}]")
        if title != app.current_track_name and app.current_track_name:
            details.append(app.current_track_name)
        self._text_clear_of("   ".join(d for d in details if d), self.font_sm, DIM, body.top + 96, left)

    def _text_clear_of(self, text, font, color, y: int, left: int) -> None:
        """Centred on the screen, unless that would run into the art on the left - then it
        starts just right of it and uses the rest of the width."""
        max_w = W - 20 - left
        width = self._render(str(text), font, color, max_w).get_width()
        self._text(text, font, color, center=(max(W // 2, left + width // 2), y), max_w=max_w)

    def _draw_art_box(self, art_box: pygame.Rect) -> pygame.Rect | None:
        """MIDI: channel meters - tap for the piano roll. Audio: the cover or frequency bars, a
        tap switching which for every audio track; a dark note tile when there's no cover."""
        app = self.app
        track = app.current_track
        bars_box = pygame.Rect(art_box.topleft, (METER_W, art_box.height))
        levels = app.visual_levels
        if app.midi:
            box = bars_box
        elif track is None or track.kind != "audio":
            return None
        elif app.settings["show_audio_bars"] and not app.bars_unavailable:
            box = bars_box
            if levels is None:
                self._paint_meters(box, [0.0] * 16)  # empty until they're worked out
        else:
            box, levels = art_box, None
            art = app.track_art  # also when this file can't have bars (too long, no numpy)
            if art is not None:
                self.surface.blit(art, art.get_rect(center=box.center))
            else:
                self._note_tile(box)
        if levels is not None:
            self._paint_meters(box, levels)
            if not self._dimmed:
                self._meter_box = box
        self._touch(box, self._open_roll if app.midi else app.toggle_cover_view)
        return box

    def _note_tile(self, box: pygame.Rect) -> None:
        """The stand-in for cover art: a dark tile with a note."""
        pygame.draw.rect(self.surface, PANEL, box, border_radius=max(4, box.width // 10))
        self._icon("note", box.center, round(min(box.width, box.height) * 0.44), color=DIM)

    def _paint_meters(self, box: pygame.Rect, levels) -> None:
        midi = self.app.midi
        used = midi.song.channels if midi and midi.song else range(16)  # audio bars: all in use
        pygame.draw.rect(self.surface, BG, box)
        gap = 2
        bar_w = (box.width - gap * 15) / 16
        for channel, level in enumerate(levels):
            x = round(box.left + channel * (bar_w + gap))
            slot = pygame.Rect(x, box.top, round(bar_w), box.height)
            pygame.draw.rect(self.surface, PANEL if channel in used else UNUSED, slot, border_radius=3)
            height = round(slot.height * min(1.0, level))
            if height > 1:
                color = (CHANNEL_COLORS if midi else BAND_COLORS)[channel]
                pygame.draw.rect(self.surface, color, (x, slot.bottom - height, slot.width, height),
                                 border_radius=3)

    def _draw_lyrics(self, body: pygame.Rect, lyrics) -> None:
        """The title on top, then the current line with one before and two after."""
        app = self.app
        heading = "  -  ".join(p for p in (app.track_title, app.track_info.get("artist", "")) if p)
        self._text(heading, self.font_sm, DIM, center=(W // 2, body.top + 16), max_w=W - 40)
        index = app.lyric_line
        for offset, y in ((-1, 46), (0, 78), (1, 110), (2, 136)):
            i = index + offset
            if index < 0 and offset == 0:
                text = "♪"  # before the first timed line
            elif 0 <= i < len(lyrics.lines):
                text = lyrics.lines[i] or ("♪" if lyrics.synced else "")
            else:
                continue
            if offset == 0:
                fits = self.font_md.size(text)[0] <= W - 40
                font, color = (self.font_md if fits else self.font_sm), (ACCENT if lyrics.synced else TEXT)
            else:
                font, color = self.font_sm, DIM
            self._text(text, font, color, center=(W // 2, body.top + y), max_w=W - 40)

    # -- clock face and piano roll -----------------------------------------

    def _draw_clock_face(self) -> None:
        """Dimmed with the clock on: time, date, what's playing and the next alarm. It drifts a
        few pixels each minute so nothing sits in one place all night."""
        app = self.app
        if self._font_clock is None:
            self._font_clock = pygame.font.SysFont("dejavusans", 150)
        now = time.localtime()
        shift = (now.tm_min % 5 - 2) * 8
        x, y = W // 2 + shift, 170 + shift // 2
        self._text(time.strftime("%H:%M"), self._font_clock, CLOCK_COLOR, center=(x, y))
        self._text(f"{time.strftime('%A')} {now.tm_mday} {time.strftime('%B')}", self.font_md, DIM,
                   center=(x, y + 100))
        lines = []
        if app.is_playing and app.track_title:
            lines.append(app.track_title)
        if app.next_alarm_text:
            lines.append(f"Alarm {app.next_alarm_text}")
        for i, line in enumerate(lines):
            self._text(line, self.font_sm, DIM, center=(x, y + 150 + i * 30), max_w=W - 80)

    def _open_roll(self) -> None:
        self.roll_open = True

    def _close_roll(self) -> None:
        self.roll_open = False

    def _roll_for(self, song) -> pianoroll.Roll:
        if self._roll[0] is not song:
            self._roll = (song, pianoroll.build(song))
        return self._roll[1]

    def _draw_roll(self) -> None:
        """Falling notes in each channel's colour, landing on the keys as they sound."""
        app, midi = self.app, self.app.midi
        roll = self._roll_for(midi.song)
        self._touch((0, 0, W, H), self._close_roll)
        position, shift = app.position, midi.transpose
        low, high = roll.low + shift - 2, roll.high + shift + 2
        key_w = W / (high - low + 1)
        keys_top = H - KEYS_H
        pixels_per_second = keys_top / ROLL_SECONDS
        lit = {}
        for start, end, channel, note in roll.window(position, position + ROLL_SECONDS):
            pitch = note + shift
            color = CHANNEL_COLORS[channel] if midi.audible(channel) else PANEL
            top = keys_top - (end - position) * pixels_per_second
            bottom = keys_top - (start - position) * pixels_per_second
            x = (pitch - low) * key_w
            rect = pygame.Rect(round(x) + 1, round(max(top, 0)), max(2, round(key_w) - 2),
                               round(min(bottom, keys_top) - max(top, 0)))
            if rect.height > 0:
                pygame.draw.rect(self.surface, color, rect, border_radius=3)
            if start <= position < end and midi.audible(channel):
                lit[pitch] = color
        for pitch in range(low, high + 1):
            black = pitch % 12 in (1, 3, 6, 8, 10)
            color = lit.get(pitch, (45, 45, 55) if black else (205, 205, 215))
            x = round((pitch - low) * key_w)
            width = round((pitch - low + 1) * key_w) - x - 1
            pygame.draw.rect(self.surface, color, (x, keys_top + (0 if black else 4), width,
                                                   KEYS_H - (18 if black else 4)), border_radius=2)
        self._text(app.track_title, self.font_sm, DIM, topleft=(12, 8), max_w=W - 200)
        self._text("Tap to close", self.font_xs, DIM, topright=(W - 12, 10))

    def _commit_scrub(self) -> None:
        if self._scrub is not None:
            self.app.seek_fraction(self._scrub)
        self._scrub = None

    # -- browse ----------------------------------------------------------

    def _draw_browse(self, body: pygame.Rect) -> None:
        if self.browse_folder is None:
            self._draw_folder_list(body)
        else:
            self._draw_track_list(body, self.browse_folder)

    def _list_geometry(self, body: pygame.Rect, count: int, top: int):
        view_h = body.bottom - top
        self.scroll = min(self.scroll, max(0, count * ROW_H - view_h))
        return view_h

    def _scrollbar(self, body: pygame.Rect, top: int, count: int, by_row: bool = False,
                   row_h: int = ROW_H) -> None:
        """A bar down the right of a list: drag it, or touch where you want to be. Nothing on the
        rows themselves scrolls, so a tap on a row is always the row."""
        view_h = body.bottom - top
        # Pages that show whole rows travel a row at a time, so the last row is reachable.
        shown = (view_h // row_h) * row_h if by_row else view_h
        shown = max(row_h, shown)
        span = count * row_h - shown  # how far the list can travel
        if span <= 0:
            return
        track = pygame.Rect(W - SCROLLBAR_W - 8, top + 4, SCROLLBAR_W, view_h - 12)
        pygame.draw.rect(self.surface, PANEL, track, border_radius=SCROLLBAR_W // 2)
        thumb_h = max(SCROLL_THUMB_MIN, round(track.height * shown / (count * row_h)))
        travel = track.height - thumb_h
        at = round(travel * min(1.0, self.scroll / span)) if span else 0
        thumb = pygame.Rect(track.left, track.top + at, track.width, thumb_h)
        pygame.draw.rect(self.surface, ACCENT if self._drag_bar else DIM, thumb,
                         border_radius=SCROLLBAR_W // 2)
        # The touch area is wider than the bar, so it can be hit without aiming.
        grab = pygame.Rect(track.left - 16, track.top, track.width + 24, track.height)
        self._touch(grab, lambda: self._scroll_to(track, thumb_h, span, by_row and row_h),
                    drag=True, release=lambda: setattr(self, "_drag_bar", False))

    def _scroll_to(self, track: pygame.Rect, thumb_h: int, span: int, by_row) -> None:
        """Put the middle of the thumb where the finger is."""
        self._drag_bar = True
        travel = max(1, track.height - thumb_h)
        at = (self.last_tap[1] - track.top - thumb_h / 2) / travel
        self.scroll = max(0, min(round(span * at), span))
        if by_row:  # pages that only show whole rows; by_row carries the row height
            self.scroll = min(round(self.scroll / by_row) * by_row, span)

    def _browse_entries(self, target) -> tuple[str, list] | None:
        """Title and (folder, track) positions for a folder or one of the play-history lists."""
        app = self.app
        if target == "most":
            return "Most played", app.most_played()
        if target == "recent":
            return "Recently played", app.recently_played()
        if isinstance(target, int) and 0 <= target < len(app.folders):
            folder = app.folders[target]
            return folder.name, [(target, t) for t in range(len(folder.tracks))]
        return None

    def _draw_folder_list(self, body: pygame.Rect) -> None:
        app = self.app
        folders = app.folders
        if not folders:
            self._text("Nothing found", self.font_md, DIM, center=body.center)
            return
        check_w = 70  # checkbox column at the end of each row
        column_x = 12 + (W - 96) - check_w // 2
        self._text("Folders", self.font_sm, DIM, topleft=(30, body.top + 6))
        self._text("Shuffle all", self.font_xs, DIM, center=(column_x, body.top + 16))

        # The play-history lists first, once they have something in them.
        rows = []
        for key in ("most", "recent"):
            title, entries = self._browse_entries(key)
            if entries:
                rows.append((key, title, len(entries)))
        rows += [(i, folder.name, len(folder.tracks)) for i, folder in enumerate(folders)]

        top = body.top + 32
        self._list_geometry(body, len(rows), top)
        list_area = pygame.Rect(body.left, top, body.width, body.bottom - top)

        clip = self.surface.get_clip()
        self.surface.set_clip(list_area)
        for row, (target, name, count) in enumerate(rows):
            y = top + row * ROW_H - self.scroll
            if y + ROW_H < top or y > body.bottom:
                continue
            rect = pygame.Rect(12, y + 3, W - 96, ROW_H - 6)
            folder = isinstance(target, int)
            current = folder and target == app.folder_index
            if current:
                pygame.draw.rect(self.surface, PANEL, rect, border_radius=8)
            # Checkbox first, so its taps don't also open the folder.
            check = pygame.Rect(rect.right - check_w, rect.top, check_w, rect.height)
            if folder:
                self._touch(check.clip(list_area), lambda f=target: app.toggle_shuffle_all(f))
            self._touch(rect.clip(list_area), lambda f=target: self._open_folder(f))
            self._text(name, self.font_md, ACCENT if current else TEXT if folder else WARN,
                       topleft=(rect.left + 18, rect.top + 12), max_w=rect.width - 200)
            self._text(str(count), self.font_sm, DIM, topright=(check.left - 8, rect.top + 16))
            if folder:
                self._checkbox(check.center, app.in_shuffle_all(target))
        self.surface.set_clip(clip)
        self._scrollbar(body, top, len(rows))

    def _checkbox(self, center, checked: bool) -> None:
        box = pygame.Rect(0, 0, 30, 30)
        box.center = center
        if checked:
            pygame.draw.rect(self.surface, ACCENT, box, border_radius=6)
            pygame.draw.lines(self.surface, BG, False, [
                (box.left + 7, box.centery), (box.left + 13, box.bottom - 8),
                (box.right - 7, box.top + 8),
            ], 4)
        else:
            pygame.draw.rect(self.surface, DIM, box, width=2, border_radius=6)

    def _icon(self, kind, center, size: int, *, filled: bool = False, color=TEXT) -> None:
        key = (kind, size, filled, color)
        if key not in self._icons:
            if kind == "heart":
                self._icons[key] = heart_icon(size, filled)
            else:
                draw = {"lyrics": lyrics_icon, "note": note_icon, "info": info_icon}.get(kind, swap_icon)
                self._icons[key] = draw(size, color)
        icon = self._icons[key]
        self.surface.blit(icon, icon.get_rect(center=center))

    def _heart(self, center, filled: bool, size: int = 28) -> None:
        self._icon("heart", center, size, filled=filled)

    def _draw_track_list(self, body: pygame.Rect, target) -> None:
        app = self.app
        found = self._browse_entries(target)
        if found is None:
            self.browse_folder = None
            return
        title, entries = found
        mixed = not isinstance(target, int)  # tracks from several folders

        self._button((12, body.top + 4, 80, 44), "<", self._close_folder)
        self._text(title, self.font_md, TEXT, topleft=(104, body.top + 14), max_w=W - 360)
        self._button((W - 238, body.top + 4, 112, 44), "Queue all",
                     lambda: app.queue_all(entries), font=self.font_sm)
        self._button((W - 118, body.top + 4, 104, 44), "Jump",
                     self._jump_to_current, font=self.font_sm)

        top = body.top + 56
        self._list_geometry(body, len(entries), top)

        clip = self.surface.get_clip()
        list_area = pygame.Rect(body.left, top, body.width, body.bottom - top)
        self.surface.set_clip(list_area)
        for row, (f, t) in enumerate(entries):
            y = top + row * ROW_H - self.scroll
            if y + ROW_H < top or y > body.bottom:
                continue
            rect = pygame.Rect(12, y + 3, W - 96, ROW_H - 6)
            current = (f, t) == (app.folder_index, app.track_index)
            if current:
                pygame.draw.rect(self.surface, PANEL, rect, border_radius=8)
            # Heart and queue first, so their taps don't also start the track.
            heart = pygame.Rect(rect.right - 70, rect.top, 70, rect.height)
            queue = pygame.Rect(heart.left - 62, rect.top, 62, rect.height)
            self._touch(heart.clip(list_area), lambda f=f, t=t: app.toggle_favorite(f, t))
            self._touch(queue.clip(list_area), lambda f=f, t=t: app.toggle_queued(f, t))
            self._touch(rect.clip(list_area), lambda f=f, t=t: app.select(f, t))
            self._track_label(rect, f, t, current, mixed, right=queue.left - 8)
            self._queue_mark(queue.center, app.queue_position(f, t))
            self._heart(heart.center, app.is_favorite(f, t))
        self.surface.set_clip(clip)
        self._scrollbar(body, top, len(entries))

    def _track_label(self, rect, f: int, t: int, current: bool, mixed: bool, right: int) -> None:
        """Name (and folder, for mixed lists), then the play count and type up to `right`."""
        app = self.app
        track = app.folders[f].tracks[t]
        ext = self._text(track.ext, self.font_sm, DIM, topright=(right, rect.top + 16))
        plays = app.play_count(f, t)
        name_right = ext.left - 12
        if plays:
            count = self._text(str(plays), self.font_xs, DIM, topright=(ext.left - 14, rect.top + 18))
            name_right = count.left - 12
        color = ACCENT if current else TEXT
        width = name_right - rect.left - 16
        if mixed:
            self._text(track.display_name, self.font_md, color,
                       topleft=(rect.left + 16, rect.top + 3), max_w=width)
            self._text(app.folders[f].name, self.font_xs, DIM,
                       topleft=(rect.left + 16, rect.top + 30), max_w=width)
        else:
            self._text(track.display_name, self.font_md, color,
                       topleft=(rect.left + 16, rect.top + 12), max_w=width)

    def _queue_mark(self, center, place: int) -> None:
        """A + to queue the track, or its place in the queue."""
        if place:
            box = pygame.Rect(0, 0, 34, 30)
            box.center = center
            pygame.draw.rect(self.surface, ACCENT, box, border_radius=8)
            self._text(str(place), self.font_sm, BG, center=box.center)
        else:
            self._text("+", self.font_lg, DIM, center=center)

    def _open_folder(self, target) -> None:
        self.browse_folder = target
        self.scroll = 0

    def _close_folder(self) -> None:
        self.browse_folder = None
        self.scroll = 0

    def _jump_to_current(self) -> None:
        """Scroll to the playing track, switching folders if needed."""
        self.browse_folder = self.app.folder_index
        self.scroll = max(0, self.app.track_index * ROW_H - ROW_H * 2)

    # -- queue -----------------------------------------------------------

    def _draw_queue(self, body: pygame.Rect) -> None:
        app = self.app
        queued = app.queue
        top = body.top + 6
        self._text("Up next", self.font_md, TEXT, topleft=(16, top + 8))
        mode = PLAY_MODE_LABELS.get(app.settings["play_mode"], "")
        if not queued:
            self._text("Nothing queued", self.font_md, DIM, center=(W // 2, body.centery - 24))
            self._text("Tap + next to a track in Browse to play it next.", self.font_sm, DIM,
                       center=(W // 2, body.centery + 12))
            return
        count = f"{len(queued)} track{'s' if len(queued) != 1 else ''}, then back to {mode}"
        self._text(count, self.font_sm, DIM, topleft=(140, top + 12), max_w=W - 300)
        self._button((W - 136, top, 120, 44), "Clear", app.clear_queue, font=self.font_sm)

        list_top = body.top + 56
        self._list_geometry(body, len(queued), list_top)
        list_area = pygame.Rect(body.left, list_top, body.width, body.bottom - list_top)
        clip = self.surface.get_clip()
        self.surface.set_clip(list_area)
        for row, (f, t) in enumerate(queued):
            y = list_top + row * ROW_H - self.scroll
            if y + ROW_H < list_top or y > body.bottom:
                continue
            rect = pygame.Rect(12, y + 3, W - 96, ROW_H - 6)
            pygame.draw.rect(self.surface, PANEL, rect, border_radius=8)
            remove = pygame.Rect(rect.right - 70, rect.top, 70, rect.height)
            # Up and down move a track through the queue; the row itself plays it now.
            moves = pygame.Rect(remove.left - 104, rect.top, 104, rect.height)
            # The buttons are registered first, so their taps don't also play the row.
            self._touch(remove.clip(list_area), lambda f=f, t=t: app.toggle_queued(f, t))
            for i, (label, delta, usable) in enumerate((("^", -1, row > 0),
                                                        ("v", +1, row < len(queued) - 1))):
                spot = pygame.Rect(moves.left + i * 52, moves.top, 52, moves.height)
                if usable:
                    self._touch(spot.clip(list_area),
                                lambda f=f, t=t, d=delta: app.move_queued(f, t, d))
                self._text(label, self.font_md, TEXT if usable else PANEL, center=spot.center)
            self._touch(rect.clip(list_area), lambda f=f, t=t: app.play_queued(f, t))
            self._text(str(row + 1), self.font_sm, ACCENT, center=(rect.left + 24, rect.centery))
            inner = pygame.Rect(rect.left + 30, rect.top, rect.width - 30, rect.height)
            self._track_label(inner, f, t, False, True, right=moves.left - 8)
            self._text("×", self.font_lg, DIM, center=remove.center)
        self.surface.set_clip(clip)
        self._scrollbar(body, list_top, len(queued))

    # -- mixer -----------------------------------------------------------

    def _set_detail_page(self, page: str) -> None:
        self.detail_page = page

    def _draw_mixer(self, body: pygame.Rect) -> None:
        """The fourth tab: the Mixer for MIDI, Info for audio - each with a Stats page."""
        app = self.app
        midi = app.midi
        if self.detail_page in DETAIL_PAGES:
            self._draw_detail(body)
            return
        if midi is None:
            self._draw_info(body)
            return

        y = body.top + 8
        self._text("Tempo", self.font_sm, DIM, topleft=(16, y + 13))
        self._button((84, y, 52, 48), "-", lambda: app.change_tempo(-0.05))
        self._text(f"{round(midi.tempo * 100)}%", self.font_md, TEXT, center=(182, y + 24))
        self._button((228, y, 52, 48), "+", lambda: app.change_tempo(+0.05))

        self._text("Key", self.font_sm, DIM, topleft=(300, y + 13))
        self._button((344, y, 52, 48), "-", lambda: app.change_transpose(-1))
        self._text(f"{midi.transpose:+d}", self.font_md, TEXT, center=(430, y + 24))
        self._button((464, y, 52, 48), "+", lambda: app.change_transpose(+1))

        self._button((536, y, 116, 48), "Reset", app.reset_mixer, font=self.font_sm)
        self._button((664, y, 120, 48), "Stats", lambda: self._set_detail_page("stats"),
                     font=self.font_sm)

        y += 58
        self._toggle((16, y, 100, 40), "Mute", not self.mixer_solo,
                     lambda: setattr(self, "mixer_solo", False))
        self._toggle((124, y, 100, 40), "Solo", self.mixer_solo,
                     lambda: setattr(self, "mixer_solo", True))
        voices = self._voices()
        if voices is not None:
            # Near the polyphony limit, FluidSynth starts cutting notes off.
            now, peak = voices
            busy = peak >= 0.9 * midi.polyphony
            self._text(f"Voices {now} / {midi.polyphony}   peak {peak}", self.font_sm,
                       WARN if busy else DIM, topright=(W - 16, y + 10))

        top = y + 50
        cols, gap, cell_h = 4, 8, 54
        cell_w = (W - 32 - gap * (cols - 1)) // cols
        channels = sorted(midi.song.channels.items())[:16]
        for i, (channel, program) in enumerate(channels):
            rect = pygame.Rect(16 + (i % cols) * (cell_w + gap),
                               top + (i // cols) * (cell_h + gap), cell_w, cell_h)
            muted, soloed = channel in midi.muted, channel in midi.solo
            audible = midi.audible(channel)
            self._touch(rect, lambda c=channel: app.toggle_channel(c, self.mixer_solo))
            # Each cell carries its channel's meter colour, so the two screens line up.
            hue = CHANNEL_COLORS[channel]
            color = SOLO_BG if soloed else MUTED_BG if muted else _tint(hue) if audible else PANEL
            pygame.draw.rect(self.surface, color, rect, border_radius=8)
            pygame.draw.rect(self.surface, hue if audible else _tint(hue, 0.45),
                             (rect.left + 5, rect.top + 8, 5, rect.height - 16), border_radius=3)
            number = self._text(str(channel + 1), self.font_sm, hue if audible else DIM,
                                topleft=(rect.left + 18, rect.top + 5))
            tag = "SOLO" if soloed else "MUTE" if muted else ""
            backup = midi.source_of(channel)
            if tag:
                self._text(tag, self.font_xs, TEXT, topright=(rect.right - 10, rect.top + 7))
            elif backup:  # the playing soundfont didn't have it
                self._text(backup[0], self.font_xs, WARN, topright=(rect.right - 10, rect.top + 7),
                           max_w=rect.right - number.right - 20)
            label = midi.instrument_label(channel) or gm.channel_label(channel, program)
            self._text(label, self.font_xs, TEXT if audible else DIM,
                       topleft=(rect.left + 18, rect.top + 30), max_w=cell_w - 28)

    def _draw_info(self, body: pygame.Rect) -> None:
        """What the file itself says - the Mixer tab's place for audio tracks."""
        app = self.app
        stats_button = self._button((W - 136, body.top + 8, 120, 44), "Stats",
                                    lambda: self._set_detail_page("stats"), font=self.font_sm)
        track, info = app.current_track, app.track_info
        if track is None or not info:
            self._text("Nothing loaded", self.font_md, DIM, center=body.center)
            return

        left = 24
        art = self._large_art(info.get("art"))
        if art is not None:
            box = pygame.Rect(24, body.top + 16, INFO_ART_SIZE, INFO_ART_SIZE)
            self.surface.blit(art, art.get_rect(center=box.center))
            left = box.right + 28

        stream = [info.get("codec") or track.ext]
        if info.get("bits"):
            stream.append(f"{info['bits']}-bit")
        if info.get("sample_rate"):
            stream.append(f"{info['sample_rate'] / 1000:g} kHz")
        if info.get("channels"):
            stream.append({1: "mono", 2: "stereo"}.get(info["channels"], f"{info['channels']} ch"))
        if info.get("bitrate"):
            stream.append(f"{round(info['bitrate'] / 1000)} kbps")
        size = info.get("size") or 0
        gain = ""
        if info.get("replaygain"):
            gain = f"{info['replaygain'][0]:+.1f} dB"
            gain += " - in use" if app.settings["loudness_match"] else " - Loudness match is off"
        lyrics = info.get("lyrics")
        rows = [
            ("Title", info.get("title")),
            ("Artist", info.get("artist")),
            ("Album", info.get("album")),
            ("Track", info.get("tracknumber")),
            ("Year", info.get("date")),
            ("Genre", info.get("genre")),
            ("Format", "  ·  ".join(stream)),
            ("Gain", gain),
            ("Lyrics", ("Timed" if lyrics.synced else "Plain") if lyrics else ""),
            ("Length", _fmt_time(info["duration"]) if info.get("duration") else ""),
            ("Size", f"{size / 1_000_000:.1f} MB" if size else ""),
            ("File", track.display_name),
            ("Folder", app.current_folder.name if app.current_folder else ""),
            ("Plays", str(app.play_count(app.folder_index, app.track_index))),
        ]
        y = body.top + 16
        for label, value in rows:
            if not value or y > body.bottom - 28:
                continue
            right = stats_button.left - 12 if y < stats_button.bottom else W - 24
            self._text(label, self.font_sm, DIM, topleft=(left, y + 2))
            self._text(value, self.font_md, TEXT, topleft=(left + 100, y), max_w=right - left - 100)
            y += 29

    def _draw_detail(self, body: pygame.Rect) -> None:
        """Stats and Health share the tab, with a back button and a page switch."""
        top = body.top + 6
        self._button((16, top, 80, 44), "<", lambda: self._set_detail_page("main"))
        for i, (page, label) in enumerate(DETAIL_PAGES.items()):
            self._toggle((104 + i * 120, top, 114, 44), label, self.detail_page == page,
                         lambda p=page: self._set_detail_page(p))
        if self.detail_page == "health":
            self._draw_health(body, top)
        else:
            self._draw_stats(body, top)

    def _draw_stats(self, body: pygame.Rect, top: int) -> None:
        """Listening time, plays and the most played tracks over a chosen period."""
        app = self.app
        for i, (days, label) in enumerate(STATS_PERIODS):
            self._toggle((W - 346 + i * 110, top, 104, 44), label, self.stats_days == days,
                         lambda d=days: setattr(self, "stats_days", d))
        numbers = app.stats(self.stats_days)
        summary = (f"{stats.duration_text(numbers['seconds'])} listened   ·   "
                   f"{numbers['plays']} plays   ·   {numbers['tracks']} tracks")
        self._text(summary, self.font_md, ACCENT, topleft=(24, top + 62), max_w=W - 48)
        if not numbers["top"]:
            self._note_tile(pygame.Rect(W // 2 - 40, top + 110, 80, 80))
            self._text("Nothing played in this time yet.", self.font_sm, DIM,
                       center=(W // 2, top + 214))
            return
        self._text("Most played", self.font_sm, DIM, topleft=(24, top + 104))
        y = top + 126
        for rank, ((bucket, key), plays) in enumerate(numbers["top"], 1):
            if y + 44 > body.bottom:
                break
            folder, _, name = app.history_name(bucket, key).rpartition("/")
            source = SOURCE_LABELS.get(bucket.split(":")[0], "Any source")
            if ":" in bucket and bucket.split(":", 1)[1]:
                source += f" {bucket.split(':', 1)[1]}"
            self._text(str(rank), self.font_md, ACCENT, center=(34, y + 20))
            self._note_tile(pygame.Rect(56, y + 2, 38, 38))
            self._text(name, self.font_sm, TEXT, topleft=(108, y + 2), max_w=W - 280)
            self._text(f"{folder}  ·  {source}", self.font_xs, DIM, topleft=(108, y + 24),
                       max_w=W - 280)
            self._text(f"{plays} play{'s' if plays != 1 else ''}", self.font_sm, DIM,
                       topright=(W - 24, y + 10))
            y += 44

    def _draw_health(self, body: pygame.Rect, top: int) -> None:
        """What the Pi is doing: addresses, memory, heat and space, read while this page is up."""
        app = self.app
        app.want_health()
        readings = app.health
        if not readings:
            self._text("Reading...", self.font_md, DIM, center=(W // 2, body.centery))
            return
        self._text(readings.get("model", ""), self.font_md, TEXT, topleft=(24, top + 62),
                   max_w=W - 48)
        host = readings.get("host", "")
        if host:
            self._text(f"{host}  ·  {readings.get('os', '')}", self.font_sm, DIM,
                       topleft=(24, top + 90), max_w=W - 48)

        temp = readings.get("temperature")
        hot = temp is not None and temp >= 70
        rows: list[tuple[str, object, bool]] = [
            ("Address", readings.get("addresses") or ["Not on a network"], False)]
        if temp is not None:
            rows.append(("Temperature", f"{temp:.0f} °C", hot))
        rows.append(("Throttling", readings.get("throttling", "n/a"),
                     bool(readings.get("throttled_now"))))
        for key, label in (("memory", "Memory"), ("cpu", "CPU"), ("load", "Load"),
                           ("uptime", "Up"), ("storage", "SD card"), ("music", "Music folder")):
            if readings.get(key):
                busy = key == "memory" and readings.get("memory_share", 0) > 0.9
                rows.append((label, readings[key], busy))

        # Two columns, filling the left one first: everything fits without scrolling.
        columns = ((24, 132, 276), (420, 528, W - 544))
        start = top + 124
        column, y = 0, start
        for label, value, warn in rows:
            lines = value if isinstance(value, list) else [str(value)]
            if y + 26 * len(lines) > body.bottom - 8 and column + 1 < len(columns):
                column, y = column + 1, start
            label_x, value_x, width = columns[column]
            self._text(label, self.font_sm, DIM, topleft=(label_x, y + 2))
            for line in lines:
                self._text(line, self.font_sm, WARN if warn else TEXT, topleft=(value_x, y + 2),
                           max_w=width)
                y += 26
            y += 4

    def _large_art(self, data):
        if not data:
            return None
        key = ("art", hash(data))
        if key not in self._icons:
            self._icons = {k: v for k, v in self._icons.items() if k[0] != "art"}  # one cover at a time
            self._icons[key] = decode_art(data, INFO_ART_SIZE)
        return self._icons[key]

    # -- EQ --------------------------------------------------------------

    def _draw_eq(self, body: pygame.Rect) -> None:
        app, settings = self.app, self.app.settings
        gains = eq.normalise(settings["eq_gains"])

        names = list(eq.PRESETS)
        gap = 8
        width = (W - 32 - gap * (len(names) - 1)) // len(names)
        for i, name in enumerate(names):
            self._toggle((16 + i * (width + gap), body.top + 8, width, 44), name,
                         name == settings["eq_preset"], lambda n=name: app.apply_eq_preset(n))

        area = pygame.Rect(16, body.top + 60, W - 32, body.bottom - body.top - 66)
        if not app.eq_available:
            self._text(f"EQ not active: {app.eq_problem}", self.font_sm, WARN,
                       center=(W // 2, area.top + 14), max_w=W - 32)
        col_w = area.width // len(eq.BANDS)
        track_top, track_bottom = area.top + 36, area.bottom - 44
        half = (track_bottom - track_top) // 2
        zero_y = track_top + half
        for band, (label, gain) in enumerate(zip(eq.BANDS, gains)):
            cx = area.left + band * col_w + col_w // 2
            track = pygame.Rect(cx - 7, track_top, 14, track_bottom - track_top)
            self._touch(
                (cx - col_w // 2 + 4, track_top - 24, col_w - 8, track.height + 48),
                lambda b=band: app.set_eq_band(b, (zero_y - self.last_tap[1]) / half * eq.MAX_DB),
                drag=True,
            )
            pygame.draw.rect(self.surface, PANEL, track, border_radius=7)
            pygame.draw.line(self.surface, DIM, (cx - 24, zero_y), (cx + 24, zero_y), 2)
            knob_y = round(zero_y - gain / eq.MAX_DB * half)
            fill = pygame.Rect(cx - 7, min(knob_y, zero_y), 14, abs(knob_y - zero_y))
            pygame.draw.rect(self.surface, ACCENT, fill)
            pygame.draw.circle(self.surface, TEXT, (cx, knob_y), 13)
            self._text(f"{label} Hz  {gain:+.1f}", self.font_sm, DIM,
                       center=(cx, area.bottom - 14))

    # -- settings --------------------------------------------------------

    def _draw_settings(self, body: pygame.Rect) -> None:
        page = {"bluetooth": self._draw_bluetooth, "soundfonts": self._draw_soundfonts,
                "alarm": self._draw_alarm, "alarm_pick": self._draw_alarm_pick,
                "screen": self._draw_screen_page}.get(self.settings_page)
        if page:
            page(body)
            return
        pages = (("playback", "Playback"), ("library", "Library"), ("system", "System"))
        for i, (page, label) in enumerate(pages):
            self._toggle((16 + i * 200, body.top + 6, 192, 40), label,
                         self.settings_page == page, lambda p=page: self._set_settings_page(p))

        rows = {"playback": self._playback_rows, "library": self._library_rows,
                "system": self._system_rows}[self.settings_page]()
        y = self._setting_rows(rows, body.top + 54, body)
        if self.settings_page == "system":
            self._draw_power_row(y + 4)

    def _setting_rows(self, rows, y: int, body: pygame.Rect | None = None) -> int:
        """Label on the left, value on the right, the whole row tappable; returns the y after.
        With a body, the page scrolls once its rows no longer fit."""
        width, start = W - 32, y
        if body is not None:
            self._scrollbar(body, y, len(rows), by_row=True, row_h=SETTING_ROW_H)
            if len(rows) * SETTING_ROW_H > body.bottom - y:
                width = W - 96  # leave the bar its gutter
            y -= self.scroll
        for label, value, action in rows:
            rect = pygame.Rect(16, y, width, SETTING_ROW_H - 6)
            # Whole rows only, and never over the page's own tabs above them.
            if body is None or start <= rect.top and rect.bottom <= body.bottom:
                self._touch(rect, action)
                pygame.draw.rect(self.surface, PANEL, rect, border_radius=8)
                self._text(label, self.font_md, TEXT, topleft=(rect.left + 18, rect.top + 6))
                self._text(value, self.font_md, ACCENT, topright=(rect.right - 18, rect.top + 6),
                           max_w=rect.width - 320)
            y += SETTING_ROW_H
        return y

    def _draw_screen_page(self, body: pygame.Rect) -> None:
        app, settings = self.app, self.app.settings
        top = self._sub_page_header(body, "Screen", "system")
        has_backlight = app.backlight.available
        rows = [
            ("Brightness", f"{round(settings['brightness'] * 100)}%" if has_backlight else "n/a",
             lambda: app.cycle("brightness")),
            ("Dim screen after", _fmt_idle(settings["dim_after"]), lambda: app.cycle("dim_after")),
            ("Dim to", app.dim_level_label, lambda: app.cycle("dim_level")),
            ("Clock when dimmed", "On" if settings["dim_clock"] else "Off",
             lambda: app.cycle("dim_clock")),
        ]
        y = self._setting_rows(rows, top + 54)
        hint = ("With the clock on, the dimmed screen shows the time, what's playing and the "
                "next alarm. A touch wakes it.")
        if not has_backlight:
            hint = ("This screen has no backlight control, so dimming darkens the picture itself - "
                    "Dim to Off leaves it black.")
        elif settings["dim_clock"] and settings["dim_level"] <= 0:
            hint = "Dim to Off turns the backlight off, so the clock won't be visible."
        warn = settings["dim_level"] <= 0 and settings["dim_clock"] and has_backlight
        self._text(hint, self.font_sm, WARN if warn else DIM, topleft=(24, y + 10), max_w=W - 48)

    def _playback_rows(self):
        app, settings = self.app, self.app.settings
        loop = settings["loop_repeats"]
        return [
            ("Play mode", self._play_mode_label(), lambda: app.cycle("play_mode")),
            ("Autoplay", "On" if settings["autoplay"] else "Off",
             lambda: app.cycle("autoplay")),
            ("Back history", f"{settings['back_limit']} tracks" if settings["back_limit"] else "Off",
             lambda: app.cycle("back_limit")),
            ("Soundfonts", f"A: {app.slot_name('a')}   B: {app.slot_name('b')}  >",
             lambda: self._set_settings_page("soundfonts")),
            ("ZUN loops", "Forever" if loop < 0 else str(loop),
             lambda: app.cycle("loop_repeats")),
            ("Polyphony", f"{settings['synth_polyphony']} voices"
                          if settings["synth_polyphony"] > 0 else "Unlimited",
             lambda: app.cycle("synth_polyphony")),
            ("Loudness match", "On (ReplayGain)" if settings["loudness_match"] else "Off",
             lambda: app.cycle("loudness_match")),
            ("Skip bad tracks", "On" if settings["skip_bad_tracks"] else "Off",
             lambda: app.cycle("skip_bad_tracks")),
        ]

    def _library_rows(self):
        app, settings = self.app, self.app.settings
        return [
            ("Source", SOURCE_LABELS.get(settings["source"], settings["source"]),
             lambda: app.cycle("source")),
            ("Switch to new disks/drives", "On" if settings["auto_source"] else "Off",
             lambda: app.cycle("auto_source")),
            ("File types", TYPE_LABELS.get(settings["file_types"], settings["file_types"]),
             lambda: app.cycle("file_types")),
        ]

    def _play_mode_label(self) -> str:
        mode = self.app.settings["play_mode"]
        label = PLAY_MODE_LABELS.get(mode, mode)
        if mode.startswith("shuffle_favorites") and not self.app.favorites_in_mode:
            label += " (none yet)"
        return label

    def _system_rows(self):
        app, settings = self.app, self.app.settings
        sleep = app.sleep_remaining
        if sleep is None:
            sleep_label = "Off"
        else:
            sleep_label = f"{math.ceil(sleep / 60)} min left (of {app.sleep_minutes})"
        return [
            ("Output", f"{settings['output_name']}  >",
             lambda: self._set_settings_page("bluetooth")),
            ("Alarm", f"{app.alarm_summary}  >",
             lambda: self._set_settings_page("alarm")),
            ("Resume on boot", "On" if settings["resume_on_boot"] else "Off",
             lambda: app.cycle("resume_on_boot")),
            ("Sleep timer", sleep_label, app.cycle_sleep),
            ("Screen", f"{self._screen_summary()}  >", lambda: self._set_settings_page("screen")),
        ]

    def _screen_summary(self) -> str:
        """ "100%, clock after 1 min" - brightness only where the backlight can be set."""
        settings = self.app.settings
        dim_after = settings["dim_after"]
        dims = "clock" if settings["dim_clock"] else "dim"
        parts = [f"{round(settings['brightness'] * 100)}%"] if self.app.backlight.available else []
        if parts and dim_after:
            parts[0] += f" / {self.app.dim_level_label.split(' -')[0].lower()}"
        parts.append(f"{dims} after {_fmt_idle(dim_after)}" if dim_after else "never dims")
        return ", ".join(parts)

    def _draw_power_row(self, y: int) -> None:
        app = self.app
        if self.confirm_action:
            label, action = {
                "reboot": ("Reboot now?", app.reboot),
                "shutdown": ("Power off?", app.shutdown),
            }[self.confirm_action]
            self._button((16, y, 240, 56), label, action, color=(170, 60, 60))
            self._button((272, y, 160, 56), "Cancel",
                         lambda: setattr(self, "confirm_action", None))
            self._text("Tap again to confirm.", self.font_sm, WARN, topleft=(448, y + 18))
            return
        # Exit and Restart act at once; the two that cut power ask first.
        self._button((16, y, 130, 56), "Exit", app.quit, font=self.font_sm)
        self._button((158, y, 150, 56), "Restart", app.restart_app, font=self.font_sm)
        self._button((320, y, 150, 56), "Reboot",
                     lambda: setattr(self, "confirm_action", "reboot"),
                     color=(90, 70, 50), font=self.font_sm)
        self._button((482, y, 175, 56), "Shut down",
                     lambda: setattr(self, "confirm_action", "shutdown"),
                     color=(120, 50, 50), font=self.font_sm)

    def _draw_bluetooth(self, body: pygame.Rect) -> None:
        app = self.app
        top = self._sub_page_header(body, "Output", "system")
        if app.bt_available:
            self._button((W - 136, top, 120, 44), "Scan",
                         lambda: app.refresh_bluetooth(scan=True), font=self.font_sm)
        status = app.bt_status or ("Working..." if app.bt_busy else "")
        if status:
            self._text(status, self.font_sm, WARN, topleft=(220, top + 12), max_w=W - 376)

        output = app.settings["output"]
        rows = [("speaker", "Speaker jack", "")]
        rows += [
            (d.address, d.name, "Connected" if d.connected else "Paired" if d.paired else "New")
            for d in app.bt_devices
        ]
        visible, width = self._page_rows(body, top + 54, len(rows))
        y = top + 54
        for i, y in visible:
            address, name, state = rows[i]
            rect = pygame.Rect(16, y, width, ROW_H - 6)
            active = address == output
            self._touch(rect, lambda a=address: app.select_output(a))
            pygame.draw.rect(self.surface, PANEL, rect, border_radius=8)
            self._text(name, self.font_md, ACCENT if active else TEXT,
                       topleft=(rect.left + 18, rect.top + 11), max_w=rect.width - 300)
            if active and address != "speaker":
                state = "Playing here - tap to disconnect"
            elif active:
                state = "Playing here"
            self._text(state, self.font_sm, ACCENT if active else DIM,
                       topright=(rect.right - 18, rect.top + 14))
            y += ROW_H

        if not app.bt_available:
            self._text("Bluetooth tools not installed (bluez)", self.font_sm, DIM,
                       topleft=(32, y + 8))
        elif len(rows) == 1 and not app.bt_busy:
            self._text("Put the speaker in pairing mode, then tap Scan.", self.font_sm, DIM,
                       topleft=(32, y + 8))

    def _page_rows(self, body: pygame.Rect, top: int, count: int) -> tuple[list, int]:
        """(index, y) of the rows that fit, scrolled a whole row at a time so every row shown is
        whole and tappable; plus the row width, narrowed when the scrollbar is there."""
        view_rows = max(1, (body.bottom - top) // ROW_H)
        self._scrollbar(body, top, count, by_row=True)
        first = min(self.scroll // ROW_H, max(0, count - view_rows))
        self.scroll = first * ROW_H
        rows = [(i, top + (i - first) * ROW_H) for i in range(first, min(count, first + view_rows))]
        return rows, (W - 96 if count > view_rows else W - 32)

    def _sub_page_header(self, body: pygame.Rect, title: str, parent: str) -> int:
        """Back button and title; returns the y the page's own rows start at."""
        top = body.top + 6
        self._button((16, top, 80, 44), "<", lambda: self._set_settings_page(parent))
        self._text(title, self.font_md, TEXT, topleft=(112, top + 8))
        return top

    def _draw_soundfonts(self, body: pygame.Rect) -> None:
        app = self.app
        top = self._sub_page_header(body, "Soundfonts", "playback")
        for i, slot in enumerate(("a", "b")):
            self._toggle((W - 232 + i * 112, top, 104, 44), f"Play {slot.upper()}",
                         slot == app.soundfont_slot, lambda s=slot: app.use_soundfont_slot(s))

        fonts = app.available_soundfonts()
        visible, width = self._page_rows(body, top + 54, len(fonts))
        y = top + 54
        for index, y in visible:
            path = fonts[index]
            rect = pygame.Rect(16, y, width, ROW_H - 6)
            pygame.draw.rect(self.surface, PANEL, rect, border_radius=8)
            in_use = str(path) in (app.slot_soundfont("a"), app.slot_soundfont("b"))
            self._text(app.soundfont_label(path), self.font_md, ACCENT if in_use else TEXT,
                       topleft=(rect.left + 18, rect.top + 11), max_w=rect.width - 200)
            for i, slot in enumerate(("a", "b")):
                self._toggle((rect.right - 150 + i * 68, rect.top + 5, 60, 40), slot.upper(),
                             str(path) == app.slot_soundfont(slot),
                             lambda s=slot, p=str(path): app.assign_soundfont(s, p))
            y += ROW_H

        if not fonts:
            self._text("No .sf2 files found - see soundfont_dirs in settings.json",
                       self.font_sm, DIM, topleft=(32, y + 8))
        elif y + 30 <= body.bottom:  # when there's room under the list
            self._text("Tap A or B to fill a slot; the Playing screen swaps between them.",
                       self.font_sm, DIM, topleft=(32, y + 8), max_w=W - 64)

    def _draw_alarm(self, body: pygame.Rect) -> None:
        app = self.app
        top = self._sub_page_header(body, "Alarm", "system")
        # One row per alarm: days, hour -/+, minute -/+, and its own On/Off.
        for row, (kind, days) in enumerate(ALARM_ROWS):
            y = top + 60 + row * 84
            enabled = app.alarm_enabled(kind)
            self._text(days, self.font_md, TEXT if enabled else DIM, topleft=(24, y + 16))
            hours, minutes = divmod(app.alarm_minutes(kind), 60)
            for x, label, step in ((290, f"{hours:02d}", 60), (530, f"{minutes:02d}", 5)):
                self._button((x - 96, y, 60, 60), "-", lambda s=step, k=kind: app.change_alarm(-s, k))
                self._text(label, self.font_lg, TEXT if enabled else DIM, center=(x, y + 30))
                self._button((x + 36, y, 60, 60), "+", lambda s=step, k=kind: app.change_alarm(s, k))
            self._text(":", self.font_lg, DIM, center=(410, y + 30))
            self._toggle((W - 136, y + 8, 120, 44), "On" if enabled else "Off", enabled,
                         lambda k=kind: app.toggle_alarm(k), font=self.font_md)

        # What it wakes you on, and how long Snooze puts it off.
        y = top + 60 + 2 * 84 - 20
        self._button((24, y, 420, 44), f"Wake to: {app.alarm_pick_label}",
                     lambda: self._set_settings_page("alarm_pick"), font=self.font_sm)
        snooze = app.snooze_minutes
        self._button((W - 296, y, 272, 44), f"Snooze {snooze} min" if snooze else "Snooze off",
                     lambda: app.cycle("alarm_snooze"), font=self.font_sm)

        # Tap or drag, like the Playing screen's volume; the alarm fades up to this.
        level = app.settings["alarm_volume"]
        bar = pygame.Rect(60, top + 60 + 2 * 84 + 64, W - 120, 26)
        self._text("Alarm volume", self.font_sm, DIM, topleft=(60, bar.top - 32))
        self._text(f"{round(level * 100)}%", self.font_sm, TEXT, topright=(W - 60, bar.top - 32))
        self._touch(bar.inflate(0, 24),
                    lambda: app.set_alarm_volume((self.last_tap[0] - bar.left) / bar.width), drag=True)
        pygame.draw.rect(self.surface, PANEL, bar, border_radius=13)
        filled = bar.copy()
        filled.width = int(bar.width * level)
        pygame.draw.rect(self.surface, ACCENT, filled, border_radius=13)

    def _draw_alarm_pick(self, body: pygame.Rect) -> None:
        """Which tracks the alarm may wake you on."""
        app = self.app
        top = self._sub_page_header(body, "Wake to", "alarm")
        options = app.alarm_pick_options()
        current = app.settings["alarm_pick"]
        rows, width = self._page_rows(body, top + 54, len(options))
        for i, y in rows:
            value, label = options[i]
            chosen = value == current
            rect = pygame.Rect(16, y + 3, width, ROW_H - 6)
            pygame.draw.rect(self.surface, ACCENT if chosen else PANEL, rect, border_radius=8)
            self._touch(rect, lambda v=value: app.set_alarm_pick(v))
            self._text(label, self.font_md, BG if chosen else TEXT,
                       topleft=(rect.left + 18, rect.top + 14), max_w=width - 160)
            if i > 1:
                tracks = len(app.folders[i - 2].tracks)
                self._text(f"{tracks} track{'s' if tracks != 1 else ''}", self.font_sm,
                           BG if chosen else DIM, topright=(rect.right - 18, rect.top + 18))

    def _set_settings_page(self, page: str) -> None:
        self.settings_page = page
        self.confirm_action = None
        self.scroll = 0
        if page == "bluetooth":
            self.app.refresh_bluetooth()

    # -- tabs ------------------------------------------------------------

    def _draw_tabs(self) -> None:
        pygame.draw.rect(self.surface, PANEL, (0, H - TAB_H, W, TAB_H))
        width = W // len(TAB_LABELS)
        for i, label in enumerate(TAB_LABELS):
            if i == SCREEN_MIXER and not self.app.midi:
                label = "Info"  # the mixer only applies to MIDI
            elif i == SCREEN_QUEUE and self.app.queue:
                label = f"Queue {len(self.app.queue)}"
            rect = pygame.Rect(i * width, H - TAB_H, width, TAB_H)
            self._touch(rect, lambda s=i: self._select_screen(s))
            if i == self.screen:
                pygame.draw.rect(self.surface, BG, rect.inflate(-8, -8), border_radius=8)
            self._text(label, self.font_sm, ACCENT if i == self.screen else DIM,
                       center=rect.center)

    def _select_screen(self, screen: int) -> None:
        # Leaving Settings drops a pending confirmation.
        if self.screen == SCREEN_SETTINGS and screen != SCREEN_SETTINGS:
            self.confirm_action = None
            self.settings_page = {"bluetooth": "system", "alarm": "system", "screen": "system",
                                  "soundfonts": "playback"}.get(self.settings_page,
                                                                self.settings_page)
        if screen == SCREEN_BROWSE and self.screen != SCREEN_BROWSE:
            self._jump_to_current()  # open on what's playing
        else:
            self.scroll = 0
        self.screen = screen
