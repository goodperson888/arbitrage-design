"""Arrival-ordered price observations with indexed source-time expiry."""
from decimal import Decimal
from heapq import heappop, heappush


class PriceHistory:
    def __init__(self):
        self._rows: dict[int, tuple[int, Decimal]] = {}
        self._expiry: list[tuple[int, int]] = []
        self._sequence = 0

    def append(self, row: tuple[int, Decimal]) -> None:
        sequence = self._sequence
        self._sequence += 1
        self._rows[sequence] = row
        heappush(self._expiry, (row[0], sequence))

    def expire_before(self, cutoff: int) -> None:
        # A later-arriving snapshot may have an earlier source timestamp.
        # Expire by source time without reordering the observations used by the
        # momentum lookup, or rebuilding the whole window for every message.
        while self._expiry and self._expiry[0][0] < cutoff:
            _, sequence = heappop(self._expiry)
            del self._rows[sequence]

    def __len__(self):
        return len(self._rows)

    def __iter__(self):
        return iter(self._rows.values())

    def __reversed__(self):
        return reversed(self._rows.values())

    def __getitem__(self, index):
        if index == -1:
            return next(reversed(self._rows.values()))
        return list(self._rows.values())[index]
