"""Kapazität -> Kraft.

Razorbill liefert zu jeder Zelle ein Kalibrierblatt mit der Temperaturabhängigkeit.
Das Datenblatt nennt nur den Weg (Kapazität in Auslenkung umrechnen, temperaturabhängigen
Versatz abziehen, Verstärkung über einen Koeffizienten alpha), keine Formel und auch
nicht das Vorzeichen. Darum steht hier die lineare Näherung um den Arbeitspunkt, und die
Datei muss ausdrücklich mit verified = true freigegeben werden, bevor der Kraftregler an
echten Geräten läuft. Wer die Form des Kalibrierblatts einbauen will: nur force_from_C ändern.

    F = sign * (C - C_zero - dC(T)) / (S * alpha(T))
"""

from __future__ import annotations

import json
import math
import tomllib
from pathlib import Path

import numpy as np


class ForceCalibration:
    def __init__(self, data: dict, path: Path | None = None):
        cell = data.get("cell", {})
        f = data["force"]
        self.serial = cell.get("serial", "")
        self.verified = bool(cell.get("verified", False))
        self.sign = float(f.get("sign", 1))
        self.S = float(f["S_pF_N"])
        self.C_zero = float(f["C_zero_pF"])
        self.offset = np.array(f.get("offset_table", [[0.0, 0.0], [400.0, 0.0]]), float)
        self.alpha = np.array(f.get("alpha_table", [[0.0, 1.0], [400.0, 1.0]]), float)
        if self.sign not in (1.0, -1.0) or self.S <= 0:
            raise ValueError("calibration: sign must be +1 or -1 and S_pF_N positive")
        if (self.alpha[:, 1] <= 0).any():
            raise ValueError("calibration: alpha must be positive")
        self.path = path
        self._tare_file = path.with_suffix(".tare.json") if path else None
        if self._tare_file and self._tare_file.exists():
            self.C_zero = float(json.loads(self._tare_file.read_text())["C_zero_pF"])

    @classmethod
    def load(cls, path: Path) -> "ForceCalibration":
        with open(path, "rb") as f:
            return cls(tomllib.load(f), Path(path))

    def _dC(self, T):
        return float(np.interp(T, self.offset[:, 0], self.offset[:, 1]))

    def _a(self, T):
        return float(np.interp(T, self.alpha[:, 0], self.alpha[:, 1]))

    def force_from_C(self, C_pF: float | None, T: float | None) -> float | None:
        if C_pF is None or T is None or not (math.isfinite(C_pF) and math.isfinite(T)):
            return None
        return self.sign * (C_pF - self.C_zero - self._dC(T)) / (self.S * self._a(T))

    def C_from_force(self, F: float, T: float) -> float:
        return F * self.sign * self.S * self._a(T) + self.C_zero + self._dC(T)

    def tare(self, C_pF: float, T: float, persist: bool = True) -> float:
        """Aktuelle Kraft als null setzen."""
        self.C_zero = C_pF - self._dC(T)
        if persist and self._tare_file:
            self._tare_file.write_text(json.dumps({"C_zero_pF": self.C_zero, "T": T}))
        return self.C_zero

    def info(self) -> dict:
        return {"serial": self.serial, "verified": self.verified, "sign": self.sign,
                "S_pF_N": self.S, "C_zero_pF": self.C_zero,
                "file": self.path.name if self.path else None}
