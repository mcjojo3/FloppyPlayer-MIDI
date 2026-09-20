"""Listening history for the Stats page: every counted play appended as one line to a log,
so nothing large is rewritten while playing."""

from __future__ import annotations

import datetime
import logging
import time
from collections import Counter
from pathlib import Path

log = logging.getLogger(__name__)


class PlayLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._cache: tuple = (None, [])  # (file stamp, entries)

    def add(self, scope: str, key: str, when: float | None = None) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"{int(when if when is not None else time.time())}\t{scope}\t{key}\n")
        except OSError as exc:
            log.warning("Could not log the play: %s", exc)

    def entries(self) -> list[tuple[int, str, str]]:
        """(time, scope, key) of every logged play; re-read only when the file changed."""
        try:
            stat = self.path.stat()
        except OSError:
            return []
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._cache[0] != stamp:
            entries = []
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split("\t")
                if len(parts) == 3 and parts[0].isdigit():
                    entries.append((int(parts[0]), parts[1], parts[2]))
            self._cache = (stamp, entries)
        return self._cache[1]


def summary(log_: PlayLog, play_counts: dict, listen_days: dict, days: int | None, now=None) -> dict:
    """Listening time, plays, tracks and the top tracks over the last `days` (None: all time).
    All time counts come from the play counts, which go back further than the log."""
    now = now if now is not None else time.time()
    if days is None:
        tally = Counter({(scope, key): n for scope, counts in play_counts.items()
                         if isinstance(counts, dict) for key, n in counts.items() if n > 0})
        seconds = sum(listen_days.values())
    else:
        since = now - days * 86400
        tally = Counter((scope, key) for when, scope, key in log_.entries() if when >= since)
        first_day = datetime.date.fromtimestamp(since).isoformat()
        seconds = sum(s for day, s in listen_days.items() if day >= first_day)
    return {
        "seconds": seconds,
        "plays": sum(tally.values()),
        "tracks": len(tally),
        "top": tally.most_common(5),
    }


def duration_text(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"
