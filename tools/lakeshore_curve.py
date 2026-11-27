"""Kalibrierkurve (.340 oder csv) in einen Lake Shore laden, zuweisen und zurücklesen.

    python tools/lakeshore_curve.py calibrations/cernox.340 --model 350
        zeigt nur, was geschrieben würde. Kein Gerät nötig.

    python tools/lakeshore_curve.py calibrations/cernox.340 --model 350 --address GPIB0::12::INSTR --curve 21 --input C --write
        Lake Shore 224/335/336/350: schreibt Kurve 21, weist sie Eingang C zu (INCRV), liest alle Punkte zurück.

    python tools/lakeshore_curve.py calibrations/cernox.340 --model 370 --address GPIB0::12::INSTR --curve 1 --input 1 --write
        Lake Shore 370: Kurve 1, Kanal 1 (INSET), Autoscan aus (SCAN).

Was die Geräte erwarten (Befehle CRVHDR, CRVPT, CRVDEL, dazu INCRV bzw. INSET/SCAN):
  - Kurvenplätze: 370: 1 bis 20. 224/335/336/350: eigene Kurven 21 bis 59. Je höchstens 200 Punkte.
  - Format 4 = log10(Ohm) gegen Kelvin (für Cernox die richtige Wahl), 3 = Ohm gegen Kelvin.
  - Koeffizient 1 = negativ (Cernox, RuOx), 2 = positiv (Platin).
  - Punkte aufsteigend in den Sensoreinheiten. Für ein Cernox heißt das: T fällt von Punkt zu Punkt.
  - Name höchstens 15 Zeichen, Seriennummer höchstens 10.

Der 370 rechnet zwischen den Punkten linear in den Sensoreinheiten. Darum Format 4: log R ist für
ein Cernox über die Dekaden fast linear in log T, das gibt mit 100 bis 200 Punkten unter 1 mK Fehler.

Ein belegter Kurvenplatz wird nur mit --overwrite überschrieben. Nach dem Schreiben liest das Skript
jeden Punkt mit CRVPT? zurück und vergleicht. Ungetestet an echter Hardware, Befehle laut Handbuch.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import time
from pathlib import Path

MAX_POINTS = 200
CURVES = {"370": range(1, 21), "224": range(21, 60), "335": range(21, 60), "336": range(21, 60), "350": range(21, 60)}
CURVES_370 = CURVES["370"]


def parse(path: Path) -> dict:
    """-> {name, serial, fmt, limit, coef, points: [(units, T)]}, Punkte so wie in der Datei."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    head = {"name": path.stem[:15], "serial": "", "fmt": None, "limit": None, "coef": None}
    pts = []
    if path.suffix.lower() == ".340":
        keys = {"sensor model": "name", "serial number": "serial", "data format": "fmt",
                "setpoint limit": "limit", "temperature coefficient": "coef"}
        for line in text.splitlines():
            m = re.match(r"\s*([A-Za-z ]+?)\s*:\s*(\S+)", line)
            if m and m[1].lower() in keys:
                head[keys[m[1].lower()]] = m[2]
                continue
            parts = line.split()
            if len(parts) >= 3:
                try:
                    int(parts[0])
                    pts.append((float(parts[1]), float(parts[2])))
                except ValueError:
                    continue
        head["fmt"] = int(float(head["fmt"])) if head["fmt"] else 3
    else:                                         # csv/txt: R_ohm, T_K -> Format 4
        for line in text.splitlines():
            parts = [p for p in re.split(r"[,;\s]+", line.strip()) if p]
            if len(parts) < 2:
                continue
            try:
                R, T = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            if R > 0:
                pts.append((math.log10(R), T))
        head["fmt"] = 4
    head["limit"] = float(head["limit"]) if head["limit"] else None
    head["coef"] = int(float(head["coef"])) if head["coef"] else None
    head["points"] = pts
    return head


def prepare(c: dict) -> dict:
    """Prüft und bringt die Punkte in die Form, die der 370 will. Wirft ValueError mit Grund."""
    if c["fmt"] not in (3, 4):
        raise ValueError(f"data format {c['fmt']}: the 370 takes 3 (Ohm/K) or 4 (log Ohm/K) for a resistance curve")
    pts = [(u, t) for u, t in c["points"] if math.isfinite(u) and math.isfinite(t) and t > 0]
    if len(pts) < 2:
        raise ValueError("fewer than 2 valid points")
    if len(pts) > MAX_POINTS:
        raise ValueError(f"{len(pts)} points, the 370 holds at most {MAX_POINTS}. Ask Lake Shore for a 200-point "
                         "breakpoint file or thin the curve out, evenly in log R")
    pts.sort()
    u = [p[0] for p in pts]
    t = [p[1] for p in pts]
    if len(set(u)) != len(u):
        raise ValueError("sensor units repeat")
    dt = [b - a for a, b in zip(t, t[1:])]
    if not (all(d < 0 for d in dt) or all(d > 0 for d in dt)):
        raise ValueError("T is not monotonic in the sensor units")
    coef = 1 if dt[0] < 0 else 2
    if c["coef"] not in (None, coef):
        raise ValueError(f"file says coefficient {c['coef']}, the points say {coef}")
    limit = c["limit"] or math.ceil(max(t))
    name = re.sub(r"[,;\"]", "_", str(c["name"]))[:15]
    serial = re.sub(r"[,;\"]", "_", str(c["serial"]))[:10]
    return {"name": name, "serial": serial, "fmt": c["fmt"], "limit": float(limit), "coef": coef, "points": pts}


def commands(c: dict, curve: int, model: str = "370") -> list[str]:
    rng = CURVES.get(str(model))
    if rng is None:
        raise ValueError(f"model {model}: use one of {', '.join(CURVES)}")
    if curve not in rng:
        raise ValueError(f"Model {model}: curve number {rng.start} to {rng.stop - 1}")
    cmds = [f"CRVDEL {curve}",
            f"CRVHDR {curve},{c['name']},{c['serial']},{c['fmt']},{c['limit']:g},{c['coef']}"]
    cmds += [f"CRVPT {curve},{i},{u:.6g},{t:.6g}" for i, (u, t) in enumerate(c["points"], 1)]
    return cmds


def verify(io, c: dict, curve: int, rel=2e-5) -> list[str]:
    bad = []
    for i, (u, t) in enumerate(c["points"], 1):
        ru, rt = (float(x) for x in io.query(f"CRVPT? {curve},{i}").split(",")[:2])
        if not (math.isclose(ru, u, rel_tol=rel, abs_tol=1e-6) and math.isclose(rt, t, rel_tol=rel, abs_tol=1e-6)):
            bad.append(f"point {i}: wrote {u:.6g},{t:.6g}, read {ru:.6g},{rt:.6g}")
    return bad


def assign(io, channel, curve: int, coef: int, scan_off=True, pause_s=0.1, model: str = "370") -> list[str]:
    """Kurve dem Eingang zuweisen, sonst nichts am Eingang ändern.
    370: INSET <ch>,<an>,<dwell>,<pause>,<Kurve>,<tempco>. 224/335/336/350: INCRV <Eingang>,<Kurve>."""
    if str(model) != "370":
        cmds = [f"INCRV {channel},{curve}"]
        for cmd in cmds:
            io.write(cmd)
            time.sleep(pause_s)
        return cmds
    on, dwell, pause, _, _ = [p.strip() for p in io.query(f"INSET? {channel}").split(",")[:5]]
    cmds = [f"INSET {channel},1,{dwell},{pause},{curve},{coef}"]
    if scan_off:
        cmds.append(f"SCAN {channel},0")              # Scanner fest auf diesem Kanal, kein Autoscan
    for cmd in cmds:
        io.write(cmd)
        time.sleep(pause_s)
    return cmds


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--model", default="350", choices=sorted(CURVES), help="Lake-Shore-Modell (Vorgabe 350)")
    ap.add_argument("--address", help="VISA-Adresse, z. B. GPIB0::12::INSTR")
    ap.add_argument("--curve", type=int, help="Kurvenplatz: 370 1..20 (Vorgabe 1), sonst 21..59 (Vorgabe 21)")
    ap.add_argument("--input", "--channel", dest="channel", help="Eingang (350: A..D) bzw. Kanal (370: Nummer), dem die Kurve zugewiesen wird")
    ap.add_argument("--keep-scan", action="store_true", help="Autoscan nicht ausschalten")
    ap.add_argument("--write", action="store_true", help="wirklich schreiben (ohne: nur anzeigen)")
    ap.add_argument("--overwrite", action="store_true", help="belegten Kurvenplatz überschreiben")
    a = ap.parse_args(argv)
    if a.curve is None:
        a.curve = CURVES[a.model].start
    if a.channel is not None and a.model == "370":
        a.channel = int(a.channel)

    c = prepare(parse(Path(a.file)))
    cmds = commands(c, a.curve, a.model)
    t = [p[1] for p in c["points"]]
    print(f"{a.file}: {c['name']} / {c['serial'] or '-'}, format {c['fmt']}, coefficient {c['coef']}, "
          f"{len(c['points'])} points, {min(t):g} ... {max(t):g} K, limit {c['limit']:g} K")
    if not a.write:
        print("\nwould send:")
        for cmd in cmds[:6] + (["..."] if len(cmds) > 7 else []) + cmds[-1:]:
            print("  " + cmd)
        if a.channel is not None:
            if a.model == "370":
                print(f"  INSET {a.channel},1,<dwell>,<pause>,{a.curve},{c['coef']}")
                if not a.keep_scan:
                    print(f"  SCAN {a.channel},0")
            else:
                print(f"  INCRV {a.channel},{a.curve}")
        print("\nnothing written. Add --address ... --write to load it.")
        return 0
    if not a.address:
        ap.error("--write needs --address")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    from instruments.real import Visa
    io = Visa(a.address, timeout_ms=5000)
    try:
        print("device:", io.query("*IDN?"))
        old = io.query(f"CRVHDR? {a.curve}")
        print(f"curve {a.curve} now: {old}")
        used = any(float(x) != 0 for x in io.query(f"CRVPT? {a.curve},1").split(",")[:2])   # leerer Platz: 0,0
        if used and not a.overwrite:
            print(f"curve {a.curve} is in use. Pick a free slot with --curve or add --overwrite.")
            return 1
        for i, cmd in enumerate(cmds):
            io.write(cmd)
            time.sleep(0.5 if i == 0 else 0.06)       # nach CRVDEL braucht der 370 einen Moment
            if i % 20 == 0:
                print(f"  {i}/{len(cmds)}", end="\r")
        print(f"  written {len(c['points'])} points, reading back ...")
        bad = verify(io, c, a.curve)
        if bad:
            print("MISMATCH:\n  " + "\n  ".join(bad[:10]))
            return 1
        print("  read back: all points match")
        if a.channel is not None:
            for cmd in assign(io, a.channel, a.curve, c["coef"], scan_off=not a.keep_scan, model=a.model):
                print("  sent", cmd)
            time.sleep(3.0)
            R, K = ("RDGR?", "RDGK?") if a.model == "370" else ("SRDG?", "KRDG?")
            print(f"  input {a.channel}: {io.query(f'{R} {a.channel}')} Ohm, {io.query(f'{K} {a.channel}')} K")
    finally:
        io.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
