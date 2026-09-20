"""Shuffle order: every track once per pass, and Prev retraces it."""

from __future__ import annotations

import random

Position = tuple[int, int]  # (folder_index, track_index)


class ShuffleOrder:
    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()
        self._positions: list[Position] = []
        self._order: list[Position] = []
        self._index = 0

    def reset(self, positions, first: Position | None = None) -> None:
        """Start a new pass; first leads it if given, even if not in positions."""
        self._positions = list(positions)
        order = [p for p in self._positions if p != first]
        self._rng.shuffle(order)
        if first is not None:
            order.insert(0, first)
        self._order = order
        self._index = 0

    @property
    def current(self) -> Position | None:
        return self._order[self._index] if self._order else None

    def peek(self, direction: int) -> Position | None:
        """The neighbour in the current pass, or None at a pass boundary."""
        i = self._index + direction
        if 0 <= i < len(self._order):
            return self._order[i]
        return None

    def go_to(self, position: Position) -> bool:
        """Put the cursor on a track of this pass - where Prev landed through play history."""
        try:
            self._index = self._order.index(position)
        except ValueError:
            return False
        return True

    def step(self, direction: int) -> Position | None:
        if not self._order:
            return None
        i = self._index + direction
        if i >= len(self._order):
            self._new_pass()
            i = 0
        elif i < 0:
            i = 0  # nothing before the first track of a pass
        self._index = i
        return self._order[i]

    def _new_pass(self) -> None:
        last = self._order[-1]
        order = list(self._positions)
        self._rng.shuffle(order)
        # Don't let the track that just ended open the next pass.
        if len(order) > 1 and order[0] == last:
            j = self._rng.randrange(1, len(order))
            order[0], order[j] = order[j], order[0]
        self._order = order
