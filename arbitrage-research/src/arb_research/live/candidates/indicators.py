"""Streaming version of the frozen T070 Wilder/Supertrend calculation."""
from __future__ import annotations

import math

MINUTE = 60_000_000


class Supertrend:
    def __init__(self, period: int, state: dict | None = None):
        self.period = period
        self.state = state or {"previous": None, "seed": [], "atr": None,
                               "upper": None, "lower": None, "direction": None,
                               "confirmed": False}

    def push(self, bar: dict) -> dict:
        s = self.state
        previous = s["previous"]
        if previous is None or bar["openUs"] != previous["endUs"]:
            self.__init__(self.period)
            s = self.state
            previous = None
        tr = bar["high"] - bar["low"]
        if previous:
            tr = max(tr, abs(bar["high"] - previous["close"]),
                     abs(bar["low"] - previous["close"]))
        if s["atr"] is None:
            s["seed"].append(tr)
            if len(s["seed"]) == self.period:
                s["atr"] = math.fsum(s["seed"]) / self.period
        else:
            s["atr"] = (s["atr"] * (self.period - 1) + tr) / self.period
        flip = None
        if s["atr"] is not None:
            mid = (bar["high"] + bar["low"]) / 2
            upper, lower = mid + 3 * s["atr"], mid - 3 * s["atr"]
            s["upper"] = upper if (s["upper"] is None or upper < s["upper"] or previous["close"] > s["upper"]) else s["upper"]
            s["lower"] = lower if (s["lower"] is None or lower > s["lower"] or previous["close"] < s["lower"]) else s["lower"]
            old = s["direction"]
            if old is None:
                s["direction"] = "DOWN"
            elif old == "DOWN" and bar["close"] > s["upper"]:
                s["direction"] = "UP"
            elif old == "UP" and bar["close"] < s["lower"]:
                s["direction"] = "DOWN"
            if old is not None and old != s["direction"]:
                flip, s["confirmed"] = s["direction"], True
        s["previous"] = bar
        return {"barEndUs": bar["endUs"], "atr": s["atr"],
                "direction": s["direction"] if s["confirmed"] and s["atr"] else None,
                "flip": flip if s["atr"] else None}
