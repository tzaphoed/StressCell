"""Vom Geräteausgang zum Stapel: Verstärkung und RC-Tiefpass der AC-Kette.

Die Anregung läuft vom Oszillator (Lock-in oder Generator) über einen Serienwiderstand R2
auf den Zugstapel. R2 begrenzt den Strom auch dann auf unter 10 mA, wenn jemand den
Ausgang voll aufdreht. Zusammen mit der Stapelkapazität C bildet er einen Tiefpass::

    H(f) = 1 / (1 + j 2 pi f R C)

Bei f_c = 1 / (2 pi R C) liegen am Stapel noch 71 % der Spannung, und die Phase hinkt um 45°.
Die Kette rechnet das heraus: Wer 1 V Spitze am Stapel will, bekommt am Ausgang entsprechend
mehr. Die Phase des Thermoelement-Signals wird gegen die Spannung am Stapel angegeben,
nicht gegen den Oszillator.

    gain   V Spitze am Stapel je V rms am Ausgang, bei tiefer Frequenz. Direkt: sqrt(2) = 1,414.
           Mit Tegam 2350: 50 * sqrt(2). Mit dem Oszilloskop messen, gain_verified = true.
    R      Summe aus R2 und Innenwiderstand des Ausgangs (50 Ohm). 0: kein Tiefpass.
    C      Stapelkapazität in F. Fällt beim Abkühlen. Wer genau sein will, misst bei der
           Messtemperatur Amplitude und Phase am Stapel mit dem Oszilloskop.
"""

from __future__ import annotations

import cmath
import math


class AcChain:
    def __init__(self, gain: float = math.sqrt(2.0), R_ohm: float = 0.0, C_F: float = 3.1e-6):
        if gain <= 0 or R_ohm < 0 or C_F <= 0:
            raise ValueError("AC chain: gain and C must be positive, R not negative")
        self.gain, self.R, self.C = float(gain), float(R_ohm), float(C_F)

    @classmethod
    def from_cfg(cls, ac: dict) -> "AcChain":
        return cls(float(ac.get("gain", math.sqrt(2.0))), float(ac.get("series_R_ohm", 0.0)),
                   float(ac.get("C_F", 3.1e-6)))

    def H(self, f: float) -> complex:
        return 1.0 / complex(1.0, 2.0 * math.pi * f * self.R * self.C)

    def corner_Hz(self) -> float | None:
        return None if self.R == 0 else 1.0 / (2.0 * math.pi * self.R * self.C)

    def out_rms(self, f: float, V_stack_peak: float) -> float:
        """Was der Ausgang liefern muss, damit am Stapel V_stack_peak ankommt."""
        return V_stack_peak / (self.gain * abs(self.H(f)))

    def stack_peak(self, f: float, V_out_rms: float) -> float:
        return V_out_rms * self.gain * abs(self.H(f))

    def phase_deg(self, f: float) -> float:
        """Phase der Stapelspannung gegen den Ausgang, negativ: hinkt nach."""
        return math.degrees(cmath.phase(self.H(f)))

    def info(self) -> dict:
        return {"gain": self.gain, "R_ohm": self.R, "C_F": self.C, "corner_Hz": self.corner_Hz()}
