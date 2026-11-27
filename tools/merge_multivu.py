"""PPMS-Temperatur und Feld aus einer MultiVu-Datei nachträglich an StressCell-Daten hängen.

Messrechner und PPMS-Rechner sind nicht verbunden. StressCell schreibt in jede Zeile die Uhrzeit
(Spalte "wall", Unix-Sekunden), MultiVu in jede Zeile "Time Stamp (sec)". Dieses Skript legt beides
übereinander und interpoliert T und B auf die StressCell-Zeilen.

    python tools/merge_multivu.py data/<Ordner>/data.csv ppms_log.dat
    python tools/merge_multivu.py data/<Ordner>/data.csv ppms_log.dat --offset 12.5

Ergebnis: data_ppms.csv neben data.csv, mit den neuen Spalten T_ppms_K, B_ppms_T und, wenn in der
Datei, P_ppms_Torr. Zeilen außerhalb der MultiVu-Daten oder in Lücken über --max-gap bleiben leer.

--offset: Sekunden, die die Uhr des PPMS-Rechners VOR der des Messrechners geht. Beide Uhren vor dem
Messtag stellen (oder einmal die Differenz notieren). Bei 1 K/min sind 30 s Versatz 0,5 K Fehler.

MultiVu: Am PPMS-Rechner unter Utilities > Log Data Temperatur, Feld und Kammerdruck mitschreiben,
Intervall 1 bis 5 s. Spaltennamen werden flexibel gesucht ("Temperature (K)", "Magnetic Field (Oe)").
Ob der Zeitstempel ab 1970 oder 1904 zählt, erkennt das Skript an der Überlappung mit den Daten.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

import numpy as np

EPOCHS = {"1970": 0.0, "1904": -2082844800.0}    # 1904: Sekunden von 1904 bis 1970 abziehen


def read_multivu(path: Path) -> dict:
    """-> {"t": array, "T": array|None, "B_T": array|None, "P": array|None, "cols": [...]}."""
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.strip().lower() == "[data]":
            start = i + 1
            break
    rows = list(csv.reader(lines[start:]))
    head = [h.strip() for h in rows[0]]

    def col(*want, avoid=()):
        for j, h in enumerate(head):
            hl = h.lower()
            if all(w in hl for w in want) and not any(a in hl for a in avoid):
                return j
        return None

    jt = col("time stamp")
    if jt is None:
        raise ValueError(f"{path}: no 'Time Stamp (sec)' column. Columns: {', '.join(head)}")
    jT = col("temperature", "(k)", avoid=("rate", "status", "setpoint", "set point"))
    jB = col("field", "(oe)", avoid=("rate", "status", "setpoint", "set point"))
    jP = col("pressure", "(torr)")

    def num(r, j):
        try:
            return float(r[j]) if j is not None and j < len(r) and r[j].strip() else math.nan
        except ValueError:
            return math.nan

    data = [r for r in rows[1:] if len(r) > jt and r[jt].strip()]
    t = np.array([num(r, jt) for r in data])
    ok = np.isfinite(t)
    out = {"t": t[ok], "cols": head}
    for key, j, scale in (("T", jT, 1.0), ("B_T", jB, 1e-4), ("P", jP, 1.0)):
        out[key] = None if j is None else np.array([num(r, j) for r in data])[ok] * scale
    o = np.argsort(out["t"])
    for k in ("t", "T", "B_T", "P"):
        if out[k] is not None:
            out[k] = out[k][o]
    return out


def pick_epoch(t_mv, t_sc, offset):
    """Epoche, bei der sich die Zeitbereiche am meisten überlappen."""
    best, best_ov = None, -1.0
    for name, shift in EPOCHS.items():
        a, b = t_mv[0] + shift - offset, t_mv[-1] + shift - offset
        ov = min(b, t_sc[-1]) - max(a, t_sc[0])
        if ov > best_ov:
            best, best_ov = name, ov
    return best, best_ov


def interp(t_mv, y, t_sc, max_gap):
    """Lineare Interpolation, leer außerhalb und in Lücken größer max_gap."""
    good = np.isfinite(y)
    tm, ym = t_mv[good], y[good]
    out = np.full(len(t_sc), np.nan)
    if len(tm) < 2:
        return out
    i = np.searchsorted(tm, t_sc)
    inside = (i > 0) & (i < len(tm))
    ii = i[inside]
    gap = tm[ii] - tm[ii - 1]
    v = np.interp(t_sc[inside], tm, ym)
    v[gap > max_gap] = np.nan
    out[inside] = v
    exact = np.isin(t_sc, tm)                         # Randpunkte genau getroffen
    out[exact] = np.interp(t_sc[exact], tm, ym)
    return out


def merge(data_csv: Path, multivu: Path, offset=0.0, max_gap=60.0, out_path: Path | None = None) -> dict:
    with open(data_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows or "wall" not in rows[0]:
        raise ValueError(f"{data_csv}: no 'wall' column (StressCell data.csv expected)")
    t_sc = np.array([float(r["wall"]) if r.get("wall") else math.nan for r in rows])
    mv = read_multivu(multivu)
    if len(mv["t"]) < 2:
        raise ValueError(f"{multivu}: fewer than 2 data rows")
    name, ov = pick_epoch(mv["t"], t_sc[np.isfinite(t_sc)], offset)
    if ov <= 0:
        raise ValueError("the time ranges do not overlap with either epoch (1970/1904). Check --offset and "
                         "that the MultiVu file covers the measurement")
    t_mv = mv["t"] + EPOCHS[name] - offset
    new = {}
    for key, colname in (("T", "T_ppms_K"), ("B_T", "B_ppms_T"), ("P", "P_ppms_Torr")):
        if mv[key] is not None:
            new[colname] = interp(t_mv, mv[key], t_sc, max_gap)
    out_path = out_path or data_csv.with_name(data_csv.stem + "_ppms.csv")
    fields = list(rows[0].keys()) + list(new)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, r in enumerate(rows):
            r = dict(r)
            for k, v in new.items():
                r[k] = "" if not math.isfinite(v[i]) else f"{v[i]:.6g}"
            w.writerow(r)
    filled = {k: int(np.isfinite(v).sum()) for k, v in new.items()}
    return {"out": out_path, "epoch": name, "rows": len(rows), "filled": filled}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_csv")
    ap.add_argument("multivu_dat")
    ap.add_argument("--offset", type=float, default=0.0, help="s, um die die PPMS-Uhr vorgeht")
    ap.add_argument("--max-gap", type=float, default=60.0, help="s, größere Lücken nicht überbrücken")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    r = merge(Path(a.data_csv), Path(a.multivu_dat), a.offset, a.max_gap, Path(a.out) if a.out else None)
    print(f"wrote {r['out']}: {r['rows']} rows, MultiVu time from {r['epoch']}, filled "
          + ", ".join(f"{k} {n}" for k, n in r["filled"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
