"""Grenzen der Razorbill FC100 aus dem Datenblatt (Version 2.4, Tabelle 2).

    T > 250 K        Vmin = -20 V                Vmax = +120 V
    100 K ... 250 K  Vmin = -50 + (T-100)/5      Vmax = +120 V
    10 K ... 100 K   Vmin = -200 + 5/3 (T-10)    Vmax = 200 - 8/9 (T-10)
    T < 10 K         Vmin = -200 V               Vmax = +200 V

Das Fenster gilt für jede Stapelgruppe einzeln. Die Lebensdauer fällt laut AP002
bei kleinen Überschreitungen etwa mit der sechsten Potenz der Spannung.

Vorzeichen (Datenblatt): positive Spannung am Zugstapel zieht an der Probe,
positive Spannung am Druckstapel drückt. Die Auslenkung hängt also an der
Differenz. Hier heißt sie "Drive": D = V_T - V_C, D > 0 bedeutet Zug.
"""

from __future__ import annotations

import math


def voltage_window(T: float | None) -> tuple[float, float]:
    """(Vmin, Vmax) in V bei T in K. Ohne gültige Temperatur gilt das engste Fenster."""
    if T is None or not math.isfinite(T):
        return -20.0, 120.0
    if T >= 250.0:
        return -20.0, 120.0
    if T >= 100.0:
        return -50.0 + (T - 100.0) / 5.0, 120.0
    if T >= 10.0:
        return -200.0 + 5.0 / 3.0 * (T - 10.0), 200.0 - 8.0 / 9.0 * (T - 10.0)
    return -200.0, 200.0


def effective_temperature(T: float | None, dTdt_K_s: float = 0.0, margin_K: float = 0.0,
                          lookahead_s: float = 0.0) -> float | None:
    """Temperatur, mit der gerechnet wird. Der PPMS-Thermometer sitzt nicht an den
    Stapeln: beim Aufwärmen rechnet man mit dem, was sie gleich haben werden."""
    if T is None or not math.isfinite(T):
        return None
    return T + margin_K + max(0.0, dTdt_K_s) * lookahead_s


def split_drive(D: float, vmin: float, vmax: float) -> tuple[float, float]:
    """Drive D auf (V_T, V_C) verteilen, mit so wenig Gleichtakt wie möglich.

    Bei 300 K: D = +140 V ergibt (+120, -20), D = -140 V ergibt (-20, +120).
    """
    half = D / 2.0
    lo, hi = vmin + abs(half), vmax - abs(half)
    if lo > hi + 1e-9:
        raise ValueError(f"drive {D:g} V is outside the window, at most {vmax - vmin:g} V")
    cm = min(max(0.0, lo), hi)
    return cm + half, cm - half
