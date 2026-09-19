"""Display backlight via sysfs. Writes need the udev rule from system/install.sh."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class Backlight:
    def __init__(self, root: Path = Path("/sys/class/backlight")):
        self._path: Path | None = None
        self._max = 0
        self._warned = False
        try:
            candidates = sorted(p for p in root.iterdir() if (p / "brightness").exists())
        except OSError:
            candidates = []
        if candidates:
            self._path = candidates[0]
            try:
                self._max = int((self._path / "max_brightness").read_text().strip())
            except (OSError, ValueError):
                self._path = None

    @property
    def available(self) -> bool:
        return self._path is not None and self._max > 0

    def set(self, fraction: float) -> None:
        if not self.available:
            return
        # Never write 0 - some panels treat it as off, which looks like a crash.
        level = max(1, round(self._max * max(0.0, min(fraction, 1.0))))
        try:
            (self._path / "brightness").write_text(str(level))
        except OSError as exc:
            if not self._warned:
                log.warning("Cannot set backlight (%s) - see system/install.sh", exc)
                self._warned = True
