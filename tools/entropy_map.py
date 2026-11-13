"""Entropie S(T, σ) bei einem Feld aus C_p(T) bei σ = 0 und der elastokalorischen Steigung g = (∂T/∂σ)_S.

Verfahren (Adiabaten, Methode der Charakteristiken):
  Entlang einer Adiabate bleibt S gleich, und ihre Steigung ist g(T, σ). Für jeden Rasterpunkt (T, σ)
  wird dT/dσ' = g(T, σ') von σ zurück bis σ' = 0 integriert. Dort liegt die Temperatur T0, und
  S(T, σ) = S(T0, 0) = ∫ C_p/T dT bis T0. Wärmekapazität unter Spannung wird nicht gebraucht.

Gültig nur, wo die Antwort reversibel ist: elastischer Bereich, Curie-Übergang zweiter Ordnung, und die
kleinen reversiblen Schleifen. Über einen hysteretischen Übergang erster Ordnung (Martensit) liefert die
AC-Methode nicht den Entropiesprung. Dort ist das Ergebnis falsch, und das Skript kann das nicht
erkennen. Diese Grenze muss man selbst ziehen, z. B. aus der Hysterese im Spannungssweep hin und zurück.

Eingaben:
  --cp      CSV mit T [K] und C [J/(mol K) oder J/(kg K), egal, nur einheitlich] bei σ = 0 und diesem Feld
  --data    eine oder mehrere StressCell-Dateien (data.csv eines Laufs oder einer Aufnahme) mit den Spalten
            T_sample (sonst T_cell, sonst T), stress_MPa und dT_dsigma_signed_K_MPa (mit Vorzeichen,
            sonst dT_dsigma_K_MPa mit Warnung), alle beim selben Feld
  --out     Ergebnis-CSV: T_K, sigma_MPa, S, dS_iso = S(T, σ) - S(T, 0), T0_K
  --S0      Entropie bei der tiefsten T der C_p-Datei (Standard 0: dann ist S relativ)

Beispiel:
  python tools/entropy_map.py --cp cp_0T.csv --data data/2026-11-20_*/data.csv --out S_0T.csv

Vorher: g weit weg vom Übergang gegen T·α/(ρ c) prüfen. Ein Fehler in g geht direkt in T0 und damit in S ein.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

import numpy as np


# ── Einlesen ──────────────────────────────────────────────────────────────
def read_cp(path) -> tuple[np.ndarray, np.ndarray]:
    T, C = [], []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        parts = [p for p in line.replace(";", ",").replace("\t", ",").split(",") if p.strip()]
        if len(parts) < 2:
            parts = line.split()
        try:
            t, c = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            continue
        if t > 0 and math.isfinite(c):
            T.append(t)
            C.append(c)
    if len(T) < 3:
        raise SystemExit(f"{path}: need at least 3 rows T, C")
    o = np.argsort(T)
    return np.asarray(T)[o], np.asarray(C)[o]


def read_g(paths, T_col=None) -> np.ndarray:
    """Zeilen (T, σ, g) aus StressCell-Dateien. g in K/MPa, σ in MPa."""
    pts, unsigned = [], [0]
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cols = [T_col] if T_col else ["T_sample", "T_cell", "T"]
                T = next((r.get(c) for c in cols if r.get(c) not in (None, "")), None)
                g = r.get("dT_dsigma_signed_K_MPa")
                if g in (None, ""):
                    g = r.get("dT_dsigma_K_MPa")
                    unsigned[0] += g not in (None, "")
                try:
                    pts.append((float(T), float(r["stress_MPa"]), float(g)))
                except (TypeError, ValueError, KeyError):
                    continue
    if unsigned[0]:
        print(f"warning: {unsigned[0]} rows without dT_dsigma_signed_K_MPa, using the unsigned value. "
              "The sign of g decides on which side the adiabat runs: check it.")
    a = np.asarray([p for p in pts if all(map(math.isfinite, p))])
    if len(a) < 4:
        raise SystemExit("too few rows with temperature, stress_MPa and dT_dsigma_K_MPa")
    return a


# ── Rechnung ─────────────────────────────────────────────────────────────
def entropy_at_zero_stress(T, C, S0=0.0):
    """S(T, 0) = S0 + ∫ C/T dT, Trapez."""
    x = C / T
    S = np.concatenate([[0.0], np.cumsum(0.5 * (x[1:] + x[:-1]) * np.diff(T))])
    return S + S0


class GField:
    """g(T, σ) auf einem Raster aus Messpunkten: erst je Spannungsstufe über T interpolieren, dann
    zwischen den Stufen linear in σ. Außerhalb der gemessenen Spannungen konstant fortgesetzt,
    außerhalb des T-Bereichs NaN."""

    def __init__(self, pts, sigma_tol=None):
        s = pts[:, 1]
        tol = sigma_tol if sigma_tol is not None else max(1e-6, 0.02 * (s.max() - s.min() + 1e-12))
        order = np.argsort(s)
        groups, cur = [], [order[0]]
        for i in order[1:]:
            if s[i] - s[cur[-1]] <= tol:
                cur.append(i)
            else:
                groups.append(cur)
                cur = [i]
        groups.append(cur)
        self.levels = []
        for g in groups:
            sub = pts[g]
            o = np.argsort(sub[:, 0])
            T, G = sub[o, 0], sub[o, 2]
            Tu, idx = np.unique(T, return_index=True)
            Gu = np.array([G[T == t].mean() for t in Tu])
            if len(Tu) >= 2:
                self.levels.append((float(sub[:, 1].mean()), Tu, Gu))
        if not self.levels:
            raise SystemExit("each stress level needs at least two temperatures")
        self.sig = np.array([l[0] for l in self.levels])

    def __call__(self, T, sigma):
        vals = []
        for s, Tl, Gl in self.levels:
            vals.append(np.interp(T, Tl, Gl, left=np.nan, right=np.nan))
        vals = np.array(vals)
        if len(self.sig) == 1:
            return float(vals[0])
        return float(np.interp(sigma, self.sig, vals))


def adiabat_T0(g, T, sigma, steps=200):
    """Temperatur bei σ = 0 auf der Adiabate durch (T, σ). RK4 von σ nach 0."""
    if sigma == 0:
        return T
    h = -sigma / steps
    s, t = sigma, T
    for _ in range(steps):
        k1 = g(t, s)
        k2 = g(t + 0.5 * h * k1, s + 0.5 * h)
        k3 = g(t + 0.5 * h * k2, s + 0.5 * h)
        k4 = g(t + h * k3, s + h)
        t += h * (k1 + 2 * k2 + 2 * k3 + k4) / 6.0
        s += h
        if not math.isfinite(t):
            return float("nan")
    return t


def entropy_map(Tcp, C, pts, T_grid, sigma_grid, S0=0.0, steps=200):
    S0T = entropy_at_zero_stress(Tcp, C, S0)
    g = GField(pts)
    rows = []
    for s in sigma_grid:
        for T in T_grid:
            T0 = adiabat_T0(g, T, s, steps)
            ok = math.isfinite(T0) and Tcp[0] <= T0 <= Tcp[-1] and Tcp[0] <= T <= Tcp[-1]
            S = float(np.interp(T0, Tcp, S0T)) if ok else float("nan")
            Sz = float(np.interp(T, Tcp, S0T)) if Tcp[0] <= T <= Tcp[-1] else float("nan")
            rows.append((T, s, S, S - Sz, T0))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cp", required=True)
    ap.add_argument("--data", required=True, nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--S0", type=float, default=0.0)
    ap.add_argument("--T-col", default=None, help="Temperaturspalte, Standard T_sample, T_cell, T")
    ap.add_argument("--nT", type=int, default=200)
    ap.add_argument("--nsigma", type=int, default=41)
    ap.add_argument("--steps", type=int, default=200)
    a = ap.parse_args(argv)
    Tcp, C = read_cp(a.cp)
    pts = read_g(a.data, a.T_col)
    Tlo, Thi = max(Tcp[0], pts[:, 0].min()), min(Tcp[-1], pts[:, 0].max())
    smin, smax = min(0.0, pts[:, 1].min()), max(0.0, pts[:, 1].max())
    rows = entropy_map(Tcp, C, pts, np.linspace(Tlo, Thi, a.nT), np.linspace(smin, smax, a.nsigma), a.S0, a.steps)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["T_K", "sigma_MPa", "S", "dS_iso", "T0_K"])
        w.writerows(rows)
    good = sum(1 for r in rows if math.isfinite(r[2]))
    print(f"wrote {a.out}: {len(rows)} points, {good} with a value, T {Tlo:.3g}...{Thi:.3g} K, "
          f"sigma {smin:.3g}...{smax:.3g} MPa")
    print("Only valid where the response is reversible. Not across the hysteretic martensitic transition.")


if __name__ == "__main__":
    main(sys.argv[1:])
