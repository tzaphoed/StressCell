"""Kraftregler.

Die Kraft liegt nur mit dem Takt der Kapazitätsmessung vor, die Stapel haben Hysterese
und kriechen. Darum ein integrierender Regler: je Messwert ein Schritt

    dD = lambda * (F_soll - F) / G

G = dF/dD wird aus den eigenen Schritten nachgeführt. Bei 300 K liegt es rund fünfmal
höher als bei 4 K, ein fester Wert wäre an einem der Enden falsch. Ein neuer Schritt
kommt erst, wenn die Rampe des vorigen fertig ist, sonst regelt man gegen die eigene
Verzögerung.

Stimmt das Vorzeichen der Kalibrierung nicht, wäre das eine Mitkopplung. Läuft die Kraft
zweimal nacheinander gegen den Schritt, meldet der Regler das, statt weiterzufahren.

Eine Probe, die sich gelöst hat, zeigt keinen Kraftsprung, wenn sie schon bei kleiner Kraft
versagt. Sie zeigt nur, dass die Kraft nicht mehr folgt. Darum zählt der Regler auch das.
"""

from __future__ import annotations

import math


class ForceLoop:
    def __init__(self, cfg: dict):
        self.c = cfg
        self.enabled = False
        self.target = 0.0
        self.tol = cfg["default_tol_N"]
        self.G = cfg["G0_N_per_V"]
        self._F_ref = None
        self._D_ref = None
        self.error = None
        self.fault: str | None = None
        self._dir = 0
        self._F_start = None
        self._travel = 0.0

    def start(self, target: float, tol: float | None = None):
        if not math.isfinite(float(target)):
            raise ValueError("force target must be finite")
        self.target = float(target)
        self.tol = float(tol if tol is not None else self.c["default_tol_N"])
        self.enabled = True
        self._F_ref = self._D_ref = None
        self.fault = None
        self._dir = 0
        self._F_start = None
        self._travel = 0.0

    def stop(self):
        self.enabled = False

    def _watch(self, F, dD, noise):
        """Über mehrere Schritte einer Richtung, nicht je Schritt: ein Einzelschritt liegt oft im
        Spiel der Stapel oder im Rauschen und sagt nichts."""
        d = 1 if dD > 0 else -1
        if d != self._dir or self._F_start is None:
            self._dir, self._F_start, self._travel = d, self._F_ref, 0.0
        self._travel += abs(dD)
        moved = (F - self._F_start) * d
        if self._travel >= self.c["against_V"] and moved < -max(self.tol, 5.0 * noise):
            self.fault = "Force moves against the drive: check the sign in the calibration file."
        elif self._travel >= self.c["stall_V"]:
            if abs(moved) < max(self.c["stall_N"], 5.0 * noise):
                self.fault = "Force does not follow the drive: sample or epoxy failure suspected."
            self._F_start, self._travel = F, 0.0

    def step(self, F, D_set: float, D_now: float, noise: float = 0.0) -> float | None:
        """Neuer Drive oder None, wenn gerade nichts zu tun ist."""
        if not self.enabled or F is None or not math.isfinite(F):
            return None
        if abs(D_set - D_now) > self.c["settle_V"]:
            return None
        self.error = self.target - F
        if self._F_ref is not None:
            dD = D_set - self._D_ref
            dF = F - self._F_ref
            if abs(dD) >= 0.5:
                if dF * dD > 0 and abs(dF) > 3.0 * noise:
                    g = dF / dD
                    self.G = min(self.c["G_max"], max(self.c["G_min"], 0.7 * self.G + 0.3 * g))
                self._watch(F, dD, noise)
                if self.fault:
                    return None
        if abs(self.error) <= 0.5 * self.tol:
            return None
        self._F_ref, self._D_ref = F, D_set
        step = self.c["lambda"] * self.error / self.G
        m = self.c["max_step_V"]
        return D_set + max(-m, min(m, step))

    def info(self) -> dict:
        return {"enabled": self.enabled, "target": self.target, "tol": self.tol,
                "G": self.G, "error": self.error}
