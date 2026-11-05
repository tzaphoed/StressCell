"""Kapazität -> Kraft.

Zwei Modelle, gewählt über ``model`` im Abschnitt [force] der Kalibrierdatei:

``razorbill`` (Standard für echte Zellen)
    Die Form vom Razorbill-Kalibrierblatt (Fassung ab Oktober 2024, z. B. FC100 #0260)::

        C = alpha / (F/kappa(T) + f0(T)) + Cp
        F = kappa(T) * (alpha / (C - Cp) - f0(T))

        kappa(T) = 1 / (k0 + k1*T + k2*T^2)      Steifigkeit des Titanbalkens, für alle Zellen gleich
        f0(T)    = f0_ref + df0(T) - df0(T_ref)  Nullpunkt, df0 als Polynom vom Blatt

    Vorzeichen wie auf dem Blatt: Zug positiv, die Kapazität fällt unter Zug.
    Das Blatt wird in Luft bei 290 K gemessen. Im Vakuum liegt der Nullpunkt daneben
    (Feuchte, AP006). Deshalb gilt nach Razorbill-Vorgehen (AP006, Seite 14):
    24 h pumpen, ohne Last den Nullpunkt nehmen (``tare``). Dabei wird f0_ref so gesetzt,
    dass die Kraft bei dieser Kapazität und Temperatur null ist. Die Temperaturkorrektur
    df0(T) - df0(T_ref) gilt danach relativ zu diesem Punkt.

    Ältere Blätter (vor Oktober 2024) schreiben die Steifigkeit als alpha(T) = alpha_290 *
    (0,91 + ...). Die neue Form multipliziert stattdessen die Kraft mit kappa. Für #0260
    gilt die neue Form, die Zahlen sind sonst dieselben.

``linear``
    Lineare Näherung um den Arbeitspunkt, für Tests und Zellen ohne Kalibrierblatt::

        F = sign * (C - C_zero - dC(T)) / (S * alpha(T))

Eine Datei muss ausdrücklich mit ``verified = true`` freigegeben werden, bevor der
Kraftregler an echten Geräten läuft: Die Zahlen sind vom Papier abgeschrieben.
"""

from __future__ import annotations

import json
import math
import tomllib
from pathlib import Path

import numpy as np


def _finite(*xs) -> bool:
    return all(x is not None and isinstance(x, (int, float)) and math.isfinite(x) for x in xs)


class ForceCalibration:
    """Lineares Modell. ``ForceCalibration.load`` liefert je nach Datei auch das Razorbill-Modell."""

    model = "linear"

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
            try:
                self.C_zero = float(json.loads(self._tare_file.read_text())["C_zero_pF"])
            except (ValueError, KeyError):
                pass

    @classmethod
    def load(cls, path: Path):
        with open(path, "rb") as f:
            data = tomllib.load(f)
        model = data.get("force", {}).get("model", "linear")
        if model == "razorbill":
            return RazorbillCalibration(data, Path(path))
        if model != "linear":
            raise ValueError(f"calibration: unknown model {model!r} (linear | razorbill)")
        return cls(data, Path(path))

    def _dC(self, T):
        return float(np.interp(T, self.offset[:, 0], self.offset[:, 1]))

    def _a(self, T):
        return float(np.interp(T, self.alpha[:, 0], self.alpha[:, 1]))

    def force_from_C(self, C_pF: float | None, T: float | None) -> float | None:
        if not _finite(C_pF, T):
            return None
        return self.sign * (C_pF - self.C_zero - self._dC(T)) / (self.S * self._a(T))

    def C_from_force(self, F: float, T: float) -> float:
        return F * self.sign * self.S * self._a(T) + self.C_zero + self._dC(T)

    def sensitivity_pF_N(self, F: float, T: float) -> float:
        return self.sign * self.S * self._a(T)

    def tare(self, C_pF: float, T: float, persist: bool = True) -> float:
        """Aktuelle Kraft als null setzen. Rückgabe: Kapazität bei Kraft null."""
        self.C_zero = C_pF - self._dC(T)
        if persist and self._tare_file:
            self._tare_file.write_text(json.dumps({"model": "linear", "C_zero_pF": self.C_zero, "T": T}))
        return self.C_zero

    def info(self) -> dict:
        return {"model": self.model, "serial": self.serial, "verified": self.verified, "sign": self.sign,
                "S_pF_N": self.S, "C_zero_pF": self.C_zero,
                "file": self.path.name if self.path else None}


class RazorbillCalibration:
    """Kalibrierblatt-Form von Razorbill, siehe Moduldoku."""

    model = "razorbill"

    def __init__(self, data: dict, path: Path | None = None):
        cell = data.get("cell", {})
        f = data["force"]
        self.serial = cell.get("serial", "")
        self.sheet = cell.get("sheet", "")
        self.verified = bool(cell.get("verified", False))
        self.alpha = float(f["alpha_NpF"])
        self.f0_sheet = float(f["f0_N"])
        self.Cp = float(f["Cp_pF"])
        self.T_sheet = float(f.get("T_sheet_K", 290.0))
        self.k = [float(x) for x in f.get("kappa_denominator", [0.91, 5e-5, 9e-7])]
        self.df0_poly = [float(x) for x in f.get("df0_poly", [0.0])]
        lo, hi = f.get("df0_range_K", [0.0, 310.0])
        self.df0_range = (float(lo), float(hi))
        self.sign = float(f.get("sign", 1))
        if self.alpha <= 0 or self.f0_sheet <= 0 or self.Cp < 0:
            raise ValueError("calibration: alpha_NpF and f0_N must be positive, Cp_pF not negative")
        if self.sign not in (1.0, -1.0):
            raise ValueError("calibration: sign must be +1 or -1")
        if len(self.k) != 3 or any(self.kappa(T) <= 0 for T in (0.0, 150.0, 400.0)):
            raise ValueError("calibration: kappa_denominator must be [k0, k1, k2] and positive")
        self.f0_ref = self.f0_sheet
        self.T_ref = self.T_sheet
        self.tared = False
        self.path = path
        self._tare_file = path.with_suffix(".tare.json") if path else None
        if self._tare_file and self._tare_file.exists():
            try:
                d = json.loads(self._tare_file.read_text())
                if d.get("model") == "razorbill":
                    self.f0_ref, self.T_ref = float(d["f0_ref_N"]), float(d["T_ref_K"])
                    self.tared = True
            except (ValueError, KeyError):
                pass

    # ── Bausteine ───────────────────────────────────────────────────────
    def kappa(self, T: float) -> float:
        k0, k1, k2 = self.k
        return 1.0 / (k0 + k1 * T + k2 * T * T)

    def df0(self, T: float) -> float:
        """Polynom vom Blatt. Außerhalb des gemessenen Bereichs wird geklemmt, nicht extrapoliert."""
        T = min(max(T, self.df0_range[0]), self.df0_range[1])
        return float(sum(c * T ** i for i, c in enumerate(self.df0_poly)))

    def f0(self, T: float) -> float:
        return self.f0_ref + self.df0(T) - self.df0(self.T_ref)

    # ── Umrechnung ──────────────────────────────────────────────────────
    def force_from_C(self, C_pF: float | None, T: float | None) -> float | None:
        if not _finite(C_pF, T) or C_pF - self.Cp <= 0:
            return None
        return self.sign * self.kappa(T) * (self.alpha / (C_pF - self.Cp) - self.f0(T))

    def C_from_force(self, F: float, T: float) -> float:
        return self.alpha / (self.sign * F / self.kappa(T) + self.f0(T)) + self.Cp

    def sensitivity_pF_N(self, F: float, T: float) -> float:
        """dC/dF. Bei #0260 um -0,97 fF/N bei Kraft null."""
        x = self.sign * F / self.kappa(T) + self.f0(T)
        return -self.sign * self.alpha / (self.kappa(T) * x * x)

    @property
    def C_zero(self) -> float:
        """Kapazität bei Kraft null und T_ref."""
        return self.alpha / self.f0_ref + self.Cp

    def tare(self, C_pF: float, T: float, persist: bool = True) -> float:
        """Kraft bei (C, T) ist ab jetzt null. Rückgabe: diese Kapazität."""
        if not _finite(C_pF, T) or C_pF - self.Cp <= 0:
            raise ValueError("tare: capacitance not usable")
        self.f0_ref = self.alpha / (C_pF - self.Cp)
        self.T_ref = float(T)
        self.tared = True
        if persist and self._tare_file:
            self._tare_file.write_text(json.dumps({"model": "razorbill", "f0_ref_N": self.f0_ref,
                                                   "T_ref_K": self.T_ref, "C_pF": C_pF}))
        return C_pF

    def info(self) -> dict:
        return {"model": self.model, "serial": self.serial, "sheet": self.sheet, "verified": self.verified,
                "sign": self.sign, "alpha_NpF": self.alpha, "f0_sheet_N": self.f0_sheet, "Cp_pF": self.Cp,
                "f0_ref_N": self.f0_ref, "T_ref_K": self.T_ref, "tared": self.tared,
                "humidity_offset_N": self.f0_ref - self.f0_sheet - (self.df0(self.T_ref) - self.df0(self.T_sheet)),
                "C_zero_pF": self.C_zero, "S_pF_N": self.sensitivity_pF_N(0.0, self.T_ref),
                "file": self.path.name if self.path else None}

    def table(self, temps=(2, 4, 10, 25, 50, 100, 150, 200, 250, 290, 300)) -> list[dict]:
        """Zum Nachschlagen in der Oberfläche: Nullpunkt, Empfindlichkeit, kappa je Temperatur."""
        return [{"T": T, "kappa": self.kappa(T), "f0_N": self.f0(T), "C0_pF": self.C_from_force(0.0, T),
                 "S_fF_N": 1e3 * self.sensitivity_pF_N(0.0, T)} for T in temps]


def load_calibration(path: Path):
    return ForceCalibration.load(path)
