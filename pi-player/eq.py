"""5-band EQ: a PipeWire filter-chain sink (system/20-floppyplayer-eq.conf) all
playback goes through. Python only sets its band gains."""

from __future__ import annotations

import logging

import pw

log = logging.getLogger(__name__)

INPUT_NODE = "effect_input.floppyplayer_eq"
OUTPUT_NODE = "effect_output.floppyplayer_eq"

BANDS = ("60", "250", "1k", "4k", "12k")  # must match the filter-chain config
MAX_DB = 12.0

PRESETS = {
    "Flat": (0.0, 0.0, 0.0, 0.0, 0.0),
    "Bass": (6.0, 3.0, 0.0, 0.0, 0.0),
    "Treble": (0.0, 0.0, 0.0, 3.0, 6.0),
    "Vocal": (-2.0, 0.0, 3.0, 2.0, 0.0),
    "Loudness": (5.0, 1.0, -1.0, 2.0, 4.0),
}


def normalise(gains) -> list[float]:
    values = list(gains or [])[: len(BANDS)]
    values += [0.0] * (len(BANDS) - len(values))
    return [max(-MAX_DB, min(MAX_DB, float(g))) for g in values]


def headroom_db(gains) -> float:
    """How far to drop the output so boosted bands can't clip."""
    return max(0.0, max(normalise(gains)))


class Equalizer:
    def __init__(self):
        self._node_id: int | None = None
        self.problem = "Checking PipeWire..."  # why it isn't active, for the UI

    @property
    def available(self) -> bool:
        return self._node_id is not None

    def refresh(self) -> bool:
        found = pw.nodes()
        node = pw.find_node(INPUT_NODE, found)
        self._node_id = node["id"] if node else None
        if node:
            self.problem = ""
        elif not found:
            self._fail("can't reach PipeWire", "pw-dump failed")
        else:
            self._fail("EQ sink not loaded", f"no {INPUT_NODE} node - is 20-floppyplayer-eq.conf in ~/.config/pipewire/pipewire.conf.d?")
        return self.available

    def apply(self, gains) -> bool:
        params = {f"eq_band_{i + 1}:Gain": g for i, g in enumerate(normalise(gains))}
        if self._node_id is not None and pw.set_props(self._node_id, params):
            return True
        # The node id changes whenever PipeWire restarts - look it up again.
        if not self.refresh():
            return False
        if pw.set_props(self._node_id, params):
            return True
        self._fail("PipeWire rejected the settings", "pw-cli set-param failed")
        self._node_id = None
        return False

    def _fail(self, problem: str, detail: str) -> None:
        if problem != self.problem:
            log.warning("EQ not active: %s (%s)", problem, detail)
        self.problem = problem
