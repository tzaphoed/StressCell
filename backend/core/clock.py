"""Uhren.

RealClock     echte Zeit, optional mit Zeitraffer (nur für die Simulation sinnvoll).
VirtualClock  springt sofort, wird vom Trockenlauf benutzt.

Die Simulationsphysik rechnet nur mit clock.now(). Dadurch läuft derselbe
Simulationscode im Live-Betrieb (Zeitraffer) und im Trockenlauf (virtuell).
"""

from __future__ import annotations

import time


class RealClock:
    virtual = False

    def __init__(self, speed: float = 1.0):
        self.speed = float(speed)
        self._t0_real = time.monotonic()
        self._t0_wall = time.time()

    def now(self) -> float:
        """Laborzeit in s (bei speed > 1 schneller als die Wanduhr)."""
        return (time.monotonic() - self._t0_real) * self.speed

    def wall(self) -> float:
        return time.time()

    def sleep(self, dt: float) -> None:
        """Schläft dt Laborsekunden."""
        if dt > 0:
            time.sleep(dt / self.speed)


class VirtualClock:
    virtual = True
    speed = float("inf")

    def __init__(self, start: float = 0.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def wall(self) -> float:
        return self._t

    def sleep(self, dt: float) -> None:
        if dt > 0:
            self._t += dt
