"""Kalibrierkurve für ein unkalibriertes Cernox aus einer Aufnahme machen.

Vorgehen (Handbuch, Kapitel "Zellthermometer"):
  1. [cell_thermometer] source = "ppms_bridge" oder "dmm", calibration leer lassen: dann wird nur R_cell
     aufgezeichnet.
  2. Ohne Probenlast langsam abkühlen und aufwärmen (höchstens 0,5 K/min), dabei "Aufnahme" mit 5 s.
  3. python tools/cernox_fit.py data/<Ordner>/data.csv calibrations/cernox_eigen.csv
  4. In config.toml calibration = "cernox_eigen.csv" eintragen.

Das Skript mittelt R in Temperaturbins des PPMS-Thermometers (log-verteilt), getrennt nach Abkühlen
und Aufwärmen, und nimmt den Mittelwert beider Richtungen: so fällt der Nachlauf der Zelle gegenüber
dem PPMS-Thermometer heraus, solange er in beide Richtungen gleich ist. Weichen Abkühlen und Aufwärmen
um mehr als 2 % ab, warnt es: dann langsamer fahren.
"""

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from core.thermometry import Curve, write_curve_csv   # noqa: E402


def fit(rows, bins_per_decade=40):
    pts = []
    for r in rows:
        try:
            T, R = float(r["T"]), float(r["R_cell"])
        except (KeyError, TypeError, ValueError):
            continue
        if T > 0 and R > 0:
            pts.append((T, R))
    if len(pts) < 10:
        raise SystemExit("too few rows with T and R_cell")
    # Richtung aus dem Verlauf von T
    dirs = [0] + [1 if b[0] > a[0] else -1 for a, b in zip(pts, pts[1:])]
    out_T, out_R, warn = [], [], []
    lo, hi = min(p[0] for p in pts), max(p[0] for p in pts)
    n = max(3, int(math.log10(hi / lo) * bins_per_decade))
    edges = [lo * (hi / lo) ** (i / n) for i in range(n + 1)]
    for a, b in zip(edges, edges[1:]):
        sel = {d: [p for p, dd in zip(pts, dirs) if a <= p[0] < b and dd == d] for d in (-1, 1)}
        means = [(sum(p[0] for p in s) / len(s), math.exp(sum(math.log(p[1]) for p in s) / len(s)))
                 for s in sel.values() if s]
        if not means:
            continue
        if len(means) == 2 and abs(means[0][1] / means[1][1] - 1) > 0.02:
            warn.append(f"{a:.3g}-{b:.3g} K: cooling and warming differ by "
                        f"{abs(means[0][1] / means[1][1] - 1) * 100:.1f} %")
        out_T.append(sum(m[0] for m in means) / len(means))
        out_R.append(math.exp(sum(math.log(m[1]) for m in means) / len(means)))
    return out_R, out_T, warn


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        R, T, warn = fit(list(csv.DictReader(f)))
    Curve(R, T)                                            # prüft Monotonie
    write_curve_csv(Path(sys.argv[2]), R, T, f"from {sys.argv[1]}, {len(R)} points")
    for w in warn:
        print("warning:", w)
    print(f"wrote {sys.argv[2]}: {len(R)} points, {min(T):.3g} ... {max(T):.3g} K")


if __name__ == "__main__":
    main()
