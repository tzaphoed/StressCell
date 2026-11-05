"""Kalibrierkurven für Widerstandsthermometer (Cernox, RuOx) und ihre Auswertung.

Formate:
  *.340    Lake Shore Kurvendatei. Kopfzeilen "Name: Wert", "Data Format: 3" (Ohm/Kelvin) oder
           "4" (log10 Ohm/Kelvin), danach Zeilen "Nr  Einheit  Temperatur".
  *.csv    zwei Spalten R_ohm, T_K (Komma oder Semikolon), Kopfzeile erlaubt.
  *.txt    wie csv, durch Leerzeichen getrennt.

Interpoliert wird log(T) gegen log(R), das ist für Cernox über viele Dekaden glatt. Außerhalb
der Kurve gibt es keinen Wert: eine extrapolierte Temperatur würde das Spannungsfenster öffnen.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np


class Curve:
    def __init__(self, R, T, name=""):
        R, T = np.asarray(R, float), np.asarray(T, float)
        ok = (R > 0) & (T > 0) & np.isfinite(R) & np.isfinite(T)
        R, T = R[ok], T[ok]
        if len(R) < 3:
            raise ValueError("thermometer curve: need at least 3 valid points")
        order = np.argsort(R)
        self.lnR, self.lnT = np.log(R[order]), np.log(T[order])
        if len(np.unique(self.lnR)) != len(self.lnR):
            raise ValueError("thermometer curve: resistance values repeat")
        d = np.diff(self.lnT)
        if not (np.all(d < 0) or np.all(d > 0)):
            raise ValueError("thermometer curve: T(R) is not monotonic")
        self.name = name
        self.R_range = (float(np.exp(self.lnR[0])), float(np.exp(self.lnR[-1])))
        self.T_range = tuple(sorted((float(np.exp(self.lnT[0])), float(np.exp(self.lnT[-1])))))

    def T(self, R_ohm) -> float | None:
        if R_ohm is None or not isinstance(R_ohm, (int, float)) or not math.isfinite(R_ohm) or R_ohm <= 0:
            return None
        x = math.log(R_ohm)
        if x < self.lnR[0] or x > self.lnR[-1]:
            return None
        return float(math.exp(np.interp(x, self.lnR, self.lnT)))

    @classmethod
    def load(cls, path: Path) -> "Curve":
        path = Path(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() == ".340":
            return cls._load_340(text, path.name)
        R, T = [], []
        for line in text.splitlines():
            parts = [p for p in re.split(r"[,;\s]+", line.strip()) if p]
            if len(parts) < 2:
                continue
            try:
                R.append(float(parts[0]))
                T.append(float(parts[1]))
            except ValueError:
                continue                                   # Kopfzeile
        return cls(R, T, path.name)

    @classmethod
    def _load_340(cls, text, name):
        fmt = 3
        R, T = [], []
        for line in text.splitlines():
            m = re.match(r"\s*Data Format:\s*(\d)", line, re.I)
            if m:
                fmt = int(m[1])
                continue
            parts = line.split()
            if len(parts) >= 3:
                try:
                    n, u, t = int(parts[0]), float(parts[1]), float(parts[2])
                except ValueError:
                    continue
                R.append(10.0 ** u if fmt == 4 else u)
                T.append(t)
        if fmt not in (3, 4):
            raise ValueError(f"{name}: .340 data format {fmt} is not a resistance curve (use 3 or 4)")
        return cls(R, T, name)


def write_curve_csv(path: Path, R, T, note: str = ""):
    """Eigene Kalibrierung gegen das PPMS-Thermometer speichern (tools/cernox_fit.py)."""
    lines = [f"# {note}"] if note else []
    lines.append("R_ohm,T_K")
    lines += [f"{r:.6g},{t:.6g}" for r, t in zip(R, T)]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
