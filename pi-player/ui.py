"""Touchscreen UI for the 7" DSI panel (800x480): immediate-mode pygame on KMS/DRM."""

from __future__ import annotations

import io
import math
import re
import time

import pygame

import eq
import gm

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

SCREEN_NOW, SCREEN_BROWSE, SCREEN_MIXER, SCREEN_EQ, SCREEN_SETTINGS = range(5)
TAB_LABELS = ("Playing", "Browse", "Mixer", "EQ", "Settings")

SOURCE_LABELS = {"floppy": "Floppy", "sd": "SD card", "local": "Music folder"}
TYPE_LABELS = {"all": "MIDI + audio", "midi": "MIDI only", "audio": "Audio only"}
PLAY_MODE_LABELS = {
    "normal": "Normal",
    "shuffle_folder": "Shuffle folder",
    "shuffle_all": "Shuffle all",
    "shuffle_favorites": "Shuffle favorites",
    "repeat": "Repeat track",
}

ROW_H = 56
SETTING_ROW_H = 40

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


def swap_icon(size: int, color=TEXT):
    big = size * 4
    surface = pygame.Surface((big, big), pygame.SRCALPHA)
    _fill_swap(surface, color, big)
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
    """Cover art bytes to a surface fitted inside size x size, or None."""
    if not data:
        return None
    try:
        image = pygame.image.load(io.BytesIO(data))
        scale = size / max(image.get_width(), image.get_height())
        dims = (max(1, round(image.get_width() * scale)), max(1, round(image.get_height() * scale)))
        try:
            return pygame.transform.smoothscale(image, dims)
        except ValueError:  # smoothscale needs 24/32-bit; palette PNGs aren't
            return pygame.transform.scale(image, dims)
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
        self.browse_folder: int | None = None  # None = folder list
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
            signature = self._signature()
            if acted or signature != self._last_signature:
                self._draw()
                pygame.display.flip()
                self._last_signature = signature
            self.clock.tick(20)

    def _signature(self):
        """What the screen shows; whole seconds, so playback redraws once a second."""
        app, s = self.app, self.app.settings
        midi = app.midi
        sleep = app.sleep_remaining
        return (
            self.screen, self.scroll, self.browse_folder, self.confirm_action,
            self.settings_page, self.mixer_solo, self._dimmed,
            time.strftime("%H:%M"), None if self._scrub is None else round(self._scrub, 3),
            app.status_text, app.current_track_name, app.current_folder_name,
            app.track_title, app.track_subtitle, id(app.track_art), app.disk_label,
            app.is_playing, int(app.position), int(app.duration),
            app.loop_status, app.track_kind, app.soundfont_slot,
            app.slot_name("a"), app.slot_name("b"),
            len(app.folders), app.folder_index, app.track_index,
            None if sleep is None else math.ceil(sleep / 60),
            (id(midi.song), midi.tempo, midi.transpose, midi.muted, midi.solo) if midi else None,
            app.eq_available, app.eq_problem, app.bt_status, app.bt_busy, tuple(app.bt_devices),
            round(s["volume"], 3), s["source"], s["file_types"], s["play_mode"],
            s["autoplay"], s["skip_bad_tracks"], s["loop_repeats"],
            tuple(s["eq_gains"]), s["eq_preset"], s["output"], s["output_name"],
            s["shuffle_excluded"], s["favorites"], s["play_counts"],
            s["resume_on_boot"], s["brightness"], s["dim_after"],
            s["alarm_enabled"], s["alarm_time"],
        )

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
        if self._dimmed or not dim_after or not self.app.backlight.available:
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
        self.surface.fill(BG)
        self._draw_header()
        body = pygame.Rect(0, HEADER_H, W, H - HEADER_H - TAB_H)
        (
            self._draw_now, self._draw_browse, self._draw_mixer,
            self._draw_eq, self._draw_settings,
        )[self.screen](body)
        self._draw_tabs()

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
        if app.settings["alarm_enabled"]:
            rect = self._text(f"Alarm {app.alarm_time_text}", self.font_sm, DIM,
                              topright=(right, 16))
            right = rect.left - 16
        status = app.status_text or app.bt_status
        if status:
            self._text(status, self.font_sm, WARN, topleft=(252, 16), max_w=right - 252)

    def _draw_now(self, body: pygame.Rect) -> None:
        app = self.app
        art = app.track_art
        if art is not None:
            box = pygame.Rect(20, body.top + 12, ART_SIZE, ART_SIZE)
            self.surface.blit(art, art.get_rect(center=box.center))
            left = box.right + 16
        else:
            left = 20
        center_x = (left + W - 20) // 2
        text_w = W - 20 - left

        title = app.track_title or "No track"
        self._text(title, self.font_lg, TEXT, center=(center_x, body.top + 34), max_w=text_w)
        if app.track_subtitle:
            self._text(app.track_subtitle, self.font_sm, DIM,
                       center=(center_x, body.top + 70), max_w=text_w)

        details = [app.current_folder_name]
        if app.track_kind:
            details.append(f"[{app.track_kind}]")
        if title != app.current_track_name and app.current_track_name:
            details.append(app.current_track_name)
        self._text("   ".join(d for d in details if d), self.font_sm, DIM,
                   center=(center_x, body.top + 96), max_w=text_w)

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
        if app.midi:  # soundfonts only change MIDI
            swap = self._button((20, y, 76, 64), "", app.swap_soundfont)
            self._icon("swap", (swap.centerx, swap.top + 24), 32, color=ACCENT)
            self._text(app.soundfont_slot.upper(), self.font_sm, TEXT,
                       center=(swap.centerx, swap.bottom - 16))

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

    def _scroll_buttons(self, body: pygame.Rect, top: int, count: int) -> None:
        view_rows = max(1, (body.bottom - top) // ROW_H)
        if count <= view_rows:
            return
        self._button((W - 74, top + 4, 62, 60), "^", lambda: self._scroll_by(-view_rows))
        self._button((W - 74, body.bottom - 64, 62, 60), "v", lambda: self._scroll_by(view_rows))

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

        top = body.top + 32
        self._list_geometry(body, len(folders), top)
        list_area = pygame.Rect(body.left, top, body.width, body.bottom - top)

        clip = self.surface.get_clip()
        self.surface.set_clip(list_area)
        for i, folder in enumerate(folders):
            y = top + i * ROW_H - self.scroll
            if y + ROW_H < top or y > body.bottom:
                continue
            rect = pygame.Rect(12, y + 3, W - 96, ROW_H - 6)
            current = i == app.folder_index
            if current:
                pygame.draw.rect(self.surface, PANEL, rect, border_radius=8)
            # Checkbox first, so its taps don't also open the folder.
            check = pygame.Rect(rect.right - check_w, rect.top, check_w, rect.height)
            self._touch(check.clip(list_area), lambda f=i: app.toggle_shuffle_all(f))
            self._touch(rect.clip(list_area), lambda f=i: self._open_folder(f))
            self._text(folder.name, self.font_md, ACCENT if current else TEXT,
                       topleft=(rect.left + 18, rect.top + 12), max_w=rect.width - 200)
            self._text(f"{len(folder.tracks)}", self.font_sm, DIM,
                       topright=(check.left - 8, rect.top + 16))
            self._checkbox(check.center, app.in_shuffle_all(i))
        self.surface.set_clip(clip)
        self._scroll_buttons(body, top, len(folders))

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
            self._icons[key] = heart_icon(size, filled) if kind == "heart" else swap_icon(size, color)
        icon = self._icons[key]
        self.surface.blit(icon, icon.get_rect(center=center))

    def _heart(self, center, filled: bool, size: int = 28) -> None:
        self._icon("heart", center, size, filled=filled)

    def _draw_track_list(self, body: pygame.Rect, folder_index: int) -> None:
        app = self.app
        folders = app.folders
        if folder_index >= len(folders):
            self.browse_folder = None
            return
        folder = folders[folder_index]

        self._button((12, body.top + 4, 80, 44), "<", self._close_folder)
        self._text(folder.name, self.font_md, TEXT,
                   topleft=(104, body.top + 14), max_w=W - 230)
        self._button((W - 118, body.top + 4, 104, 44), "Jump",
                     self._jump_to_current, font=self.font_sm)

        top = body.top + 56
        self._list_geometry(body, len(folder.tracks), top)

        clip = self.surface.get_clip()
        list_area = pygame.Rect(body.left, top, body.width, body.bottom - top)
        self.surface.set_clip(list_area)
        for i, track in enumerate(folder.tracks):
            y = top + i * ROW_H - self.scroll
            if y + ROW_H < top or y > body.bottom:
                continue
            rect = pygame.Rect(12, y + 3, W - 96, ROW_H - 6)
            current = (folder_index, i) == (app.folder_index, app.track_index)
            if current:
                pygame.draw.rect(self.surface, PANEL, rect, border_radius=8)
            # Heart first, so its taps don't also start the track.
            heart = pygame.Rect(rect.right - 70, rect.top, 70, rect.height)
            self._touch(heart.clip(list_area), lambda f=folder_index, t=i: app.toggle_favorite(f, t))
            self._touch(rect.clip(list_area), lambda f=folder_index, t=i: app.select(f, t))
            self._text(track.display_name, self.font_md, ACCENT if current else TEXT,
                       topleft=(rect.left + 16, rect.top + 12), max_w=rect.width - 260)
            ext = self._text(track.ext, self.font_sm, DIM,
                             topright=(heart.left - 8, rect.top + 16))
            plays = app.play_count(folder_index, i)
            if plays:
                self._text(str(plays), self.font_xs, DIM,
                           topright=(ext.left - 14, rect.top + 18))
            self._heart(heart.center, app.is_favorite(folder_index, i))
        self.surface.set_clip(clip)
        self._scroll_buttons(body, top, len(folder.tracks))

    def _open_folder(self, index: int) -> None:
        self.browse_folder = index
        self.scroll = 0

    def _close_folder(self) -> None:
        self.browse_folder = None
        self.scroll = 0

    def _jump_to_current(self) -> None:
        """Scroll to the playing track, switching folders if needed."""
        self.browse_folder = self.app.folder_index
        self.scroll = max(0, self.app.track_index * ROW_H - ROW_H * 2)

    def _scroll_by(self, rows: int) -> None:
        self.scroll = max(0, self.scroll + rows * ROW_H)

    # -- mixer -----------------------------------------------------------

    def _draw_mixer(self, body: pygame.Rect) -> None:
        app = self.app
        midi = app.midi
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

        self._button((600, y, 184, 48), "Reset mixer", app.reset_mixer, font=self.font_sm)

        y += 58
        self._toggle((16, y, 100, 40), "Mute", not self.mixer_solo,
                     lambda: setattr(self, "mixer_solo", False))
        self._toggle((124, y, 100, 40), "Solo", self.mixer_solo,
                     lambda: setattr(self, "mixer_solo", True))
        x = 240
        for slot in ("a", "b"):
            playing = slot == app.soundfont_slot
            rect = self._text(f"{slot.upper()}: {app.slot_name(slot)}", self.font_xs,
                              ACCENT if playing else DIM, topleft=(x, y + 11), max_w=250)
            x = rect.right + 20

        top = y + 50
        cols, gap, cell_h = 4, 8, 54
        cell_w = (W - 32 - gap * (cols - 1)) // cols
        channels = sorted(midi.song.channels.items())[:16]
        for i, (channel, program) in enumerate(channels):
            rect = pygame.Rect(16 + (i % cols) * (cell_w + gap),
                               top + (i // cols) * (cell_h + gap), cell_w, cell_h)
            muted, soloed = channel in midi.muted, channel in midi.solo
            self._touch(rect, lambda c=channel: app.toggle_channel(c, self.mixer_solo))
            color = SOLO_BG if soloed else MUTED_BG if muted else PANEL
            pygame.draw.rect(self.surface, color, rect, border_radius=8)
            audible = midi.audible(channel)
            self._text(str(channel + 1), self.font_sm, ACCENT if audible else DIM,
                       topleft=(rect.left + 10, rect.top + 5))
            tag = "SOLO" if soloed else "MUTE" if muted else ""
            if tag:
                self._text(tag, self.font_xs, TEXT, topright=(rect.right - 10, rect.top + 7))
            self._text(gm.channel_label(channel, program), self.font_xs,
                       TEXT if audible else DIM,
                       topleft=(rect.left + 10, rect.top + 30), max_w=cell_w - 20)

    def _draw_info(self, body: pygame.Rect) -> None:
        """What the file itself says - the Mixer tab's place for audio tracks."""
        app = self.app
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
        rows = [
            ("Title", info.get("title")),
            ("Artist", info.get("artist")),
            ("Album", info.get("album")),
            ("Track", info.get("tracknumber")),
            ("Year", info.get("date")),
            ("Genre", info.get("genre")),
            ("Format", "  ·  ".join(stream)),
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
            self._text(label, self.font_sm, DIM, topleft=(left, y + 2))
            self._text(value, self.font_md, TEXT, topleft=(left + 100, y), max_w=W - left - 124)
            y += 29

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
                "alarm": self._draw_alarm}.get(self.settings_page)
        if page:
            page(body)
            return
        for i, (page, label) in enumerate((("playback", "Playback"), ("system", "System"))):
            self._toggle((16 + i * 200, body.top + 6, 192, 40), label,
                         self.settings_page == page, lambda p=page: self._set_settings_page(p))

        rows = self._playback_rows() if self.settings_page == "playback" else self._system_rows()
        y = body.top + 54
        for label, value, action in rows:
            rect = pygame.Rect(16, y, W - 32, SETTING_ROW_H - 6)
            self._touch(rect, action)
            pygame.draw.rect(self.surface, PANEL, rect, border_radius=8)
            self._text(label, self.font_md, TEXT, topleft=(rect.left + 18, rect.top + 6))
            self._text(value, self.font_md, ACCENT, topright=(rect.right - 18, rect.top + 6),
                       max_w=rect.width - 320)
            y += SETTING_ROW_H

        if self.settings_page == "system":
            self._draw_power_row(y + 4)

    def _playback_rows(self):
        app, settings = self.app, self.app.settings
        loop = settings["loop_repeats"]
        return [
            ("Source", SOURCE_LABELS.get(settings["source"], settings["source"]),
             lambda: app.cycle("source")),
            ("File types", TYPE_LABELS.get(settings["file_types"], settings["file_types"]),
             lambda: app.cycle("file_types")),
            ("Soundfonts", f"A: {app.slot_name('a')}   B: {app.slot_name('b')}  >",
             lambda: self._set_settings_page("soundfonts")),
            ("Play mode", self._play_mode_label(), lambda: app.cycle("play_mode")),
            ("Autoplay", "On" if settings["autoplay"] else "Off",
             lambda: app.cycle("autoplay")),
            ("Skip bad tracks", "On" if settings["skip_bad_tracks"] else "Off",
             lambda: app.cycle("skip_bad_tracks")),
            ("ZUN loops", "Forever" if loop < 0 else str(loop),
             lambda: app.cycle("loop_repeats")),
        ]

    def _play_mode_label(self) -> str:
        mode = self.app.settings["play_mode"]
        label = PLAY_MODE_LABELS.get(mode, mode)
        if mode == "shuffle_favorites" and not self.app.favorite_count:
            label += " (none yet)"
        return label

    def _system_rows(self):
        app, settings = self.app, self.app.settings
        sleep = app.sleep_remaining
        if sleep is None:
            sleep_label = "Off"
        else:
            sleep_label = f"{math.ceil(sleep / 60)} min left (of {app.sleep_minutes})"
        has_backlight = app.backlight.available
        return [
            ("Output", f"{settings['output_name']}  >",
             lambda: self._set_settings_page("bluetooth")),
            ("Alarm", (app.alarm_time_text if settings["alarm_enabled"] else "Off") + "  >",
             lambda: self._set_settings_page("alarm")),
            ("Resume on boot", "On" if settings["resume_on_boot"] else "Off",
             lambda: app.cycle("resume_on_boot")),
            ("Sleep timer", sleep_label, app.cycle_sleep),
            ("Brightness", f"{round(settings['brightness'] * 100)}%" if has_backlight else "n/a",
             lambda: app.cycle("brightness")),
            ("Dim screen after", _fmt_idle(settings["dim_after"]) if has_backlight else "n/a",
             lambda: app.cycle("dim_after")),
        ]

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
        row_h = 50
        y = top + 54
        for address, name, state in rows[: (body.bottom - y) // row_h]:
            rect = pygame.Rect(16, y, W - 32, row_h - 6)
            active = address == output
            self._touch(rect, lambda a=address: app.select_output(a))
            pygame.draw.rect(self.surface, PANEL, rect, border_radius=8)
            self._text(name, self.font_md, ACCENT if active else TEXT,
                       topleft=(rect.left + 18, rect.top + 8), max_w=rect.width - 300)
            if active and address != "speaker":
                state = "Playing here - tap to disconnect"
            elif active:
                state = "Playing here"
            self._text(state, self.font_sm, ACCENT if active else DIM,
                       topright=(rect.right - 18, rect.top + 11))
            y += row_h

        if not app.bt_available:
            self._text("Bluetooth tools not installed (bluez)", self.font_sm, DIM,
                       topleft=(32, y + 8))
        elif len(rows) == 1 and not app.bt_busy:
            self._text("Put the speaker in pairing mode, then tap Scan.", self.font_sm, DIM,
                       topleft=(32, y + 8))

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
        row_h = 50
        y = top + 54
        for path in fonts[: (body.bottom - y) // row_h]:
            rect = pygame.Rect(16, y, W - 32, row_h - 6)
            pygame.draw.rect(self.surface, PANEL, rect, border_radius=8)
            in_use = str(path) in (app.slot_soundfont("a"), app.slot_soundfont("b"))
            self._text(path.name, self.font_md, ACCENT if in_use else TEXT,
                       topleft=(rect.left + 18, rect.top + 8), max_w=rect.width - 200)
            for i, slot in enumerate(("a", "b")):
                self._toggle((rect.right - 150 + i * 68, rect.top + 4, 60, 36), slot.upper(),
                             str(path) == app.slot_soundfont(slot),
                             lambda s=slot, p=str(path): app.assign_soundfont(s, p))
            y += row_h

        if not fonts:
            self._text("No .sf2 files found - see soundfont_dirs in settings.json",
                       self.font_sm, DIM, topleft=(32, y + 8))
        else:
            self._text("Tap A or B to fill a slot; the Playing screen swaps between them.",
                       self.font_sm, DIM, topleft=(32, y + 8), max_w=W - 64)

    def _draw_alarm(self, body: pygame.Rect) -> None:
        app = self.app
        top = self._sub_page_header(body, "Alarm", "system")
        self._toggle((W - 152, top, 136, 44), "On" if app.settings["alarm_enabled"] else "Off",
                     app.settings["alarm_enabled"], app.toggle_alarm, font=self.font_md)

        y = top + 70
        hours, minutes = divmod(app.settings["alarm_time"], 60)
        for x, label, step in ((250, f"{hours:02d}", 60), (550, f"{minutes:02d}", 5)):
            self._button((x - 84, y, 68, 60), "-", lambda s=step: app.change_alarm(-s))
            self._text(label, self.font_lg, TEXT, center=(x, y + 30))
            self._button((x + 16, y, 68, 60), "+", lambda s=step: app.change_alarm(s))
        self._text(":", self.font_lg, DIM, center=(400, y + 30))
        self._text("Hour", self.font_sm, DIM, center=(250, y + 78))
        self._text("Minute", self.font_sm, DIM, center=(550, y + 78))

        self._text("Plays a random favourite, fading up over a minute.",
                   self.font_sm, DIM, topleft=(24, y + 110), max_w=W - 48)

    def _set_settings_page(self, page: str) -> None:
        self.settings_page = page
        self.confirm_action = None
        if page == "bluetooth":
            self.app.refresh_bluetooth()

    # -- tabs ------------------------------------------------------------

    def _draw_tabs(self) -> None:
        pygame.draw.rect(self.surface, PANEL, (0, H - TAB_H, W, TAB_H))
        width = W // len(TAB_LABELS)
        for i, label in enumerate(TAB_LABELS):
            if i == SCREEN_MIXER and not self.app.midi:
                label = "Info"  # the mixer only applies to MIDI
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
            self.settings_page = {"bluetooth": "system", "alarm": "system",
                                  "soundfonts": "playback"}.get(self.settings_page,
                                                                self.settings_page)
        if screen == SCREEN_BROWSE and self.screen != SCREEN_BROWSE:
            self._jump_to_current()  # open on what's playing
        else:
            self.scroll = 0
        self.screen = screen
