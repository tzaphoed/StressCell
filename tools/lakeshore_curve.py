"""Kalibrierkurve eines Cernox in den Lake Shore laden (218, 224, 335, 336, 350, 370, 372).

    python tools/lakeshore_curve.py --kurve 21 --nur-zeigen     nur nachsehen, nichts schreiben
    python tools/lakeshore_curve.py --kurve 21                  Kurve aus config.toml hochladen
    python tools/lakeshore_curve.py --kurve 21 --zuweisen       hochladen und dem Eingang zuweisen
    python tools/lakeshore_curve.py --kurve 21 --datei calibrations/X12345.340 --zuweisen

Was passiert:
  1. Gerät, Eingang und Adresse kommen aus [cell_thermometer] in config.toml, die Kurvendatei aus
     --datei oder aus calibration (Ordner calibrations, .340 oder csv R_ohm,T_K).
  2. Das Programm zeigt, was gerade im Zielplatz steht und welche Kurve der Eingang benutzt,
     und fragt nach, bevor es etwas überschreibt.
  3. Platz löschen, Kopfzeile und Punkte schreiben (aufsteigend in den Sensoreinheiten,
     log10(Ohm) bei Format 4), dann jeden Punkt zurücklesen. Weicht einer ab: Abbruch mit Meldung.
  4. Mit --zuweisen: Kurve dem Eingang zuweisen und nachlesen. Beim 370/372 bleiben Dwell, Pause
     und Tempco des Kanals unverändert.

Danach in config.toml curve_in = "device" setzen, dann rechnet das Gerät mit der Kurve.

Vorher StressCell beenden: zwei Programme am selben GPIB-Gerät stören sich.
Mit read_only = true in config.toml schreibt das Werkzeug nichts.
Die Befehle stammen aus den Handbüchern und sind nur gegen nachgebaute Geräte getestet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from core.config import load_config            # noqa: E402
from core.thermometry import Curve             # noqa: E402
from instruments import thermometers as th     # noqa: E402


def main(argv=None, opener=None, eingabe=input) -> int:
    ap = argparse.ArgumentParser(description="Kalibrierkurve in den Lake Shore laden")
    ap.add_argument("--config", help="config.toml (Vorgabe: backend/config.toml)")
    ap.add_argument("--kurve", type=int, required=True, help="Anwenderplatz, z. B. 21 (370: 1 ... 20)")
    ap.add_argument("--datei", help="Kurvendatei, Vorgabe: [cell_thermometer] calibration")
    ap.add_argument("--format", type=int, choices=(3, 4), default=4, help="4 = log Ohm/K (Vorgabe), 3 = Ohm/K")
    ap.add_argument("--name", default="", help="Kurvenname im Gerät, höchstens 15 Zeichen")
    ap.add_argument("--serie", default="", help="Seriennummer im Gerät, höchstens 10 Zeichen")
    ap.add_argument("--zuweisen", action="store_true", help="nach dem Hochladen dem Eingang zuweisen")
    ap.add_argument("--nur-zeigen", action="store_true", help="nur lesen, nichts schreiben")
    ap.add_argument("--ja", action="store_true", help="ohne Rückfrage überschreiben")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    ct = cfg.get("cell_thermometer", {})
    if ct.get("source") != "lakeshore":
        print("[cell_thermometer] source ist nicht \"lakeshore\": nichts zu tun.")
        return 2
    model, inp, addr = str(ct.get("model", "336")), str(ct.get("input", "A")), ct.get("address")
    if a.kurve not in th.USER_CURVES.get(model, ()):
        r = th.USER_CURVES.get(model)
        print(f"Platz {a.kurve} ist am {model} kein Anwenderplatz"
              + (f" (erlaubt {r.start} ... {r.stop - 1})." if r else "."))
        return 2

    datei = Path(a.datei) if a.datei else cfg["paths"]["calibration_dir"] / str(ct.get("calibration", ""))
    if not datei.is_file():
        print(f"Kurvendatei {datei} nicht gefunden.")
        return 2
    kurve = Curve.load(datei)
    print(f"Datei {datei.name}: {len(kurve.lnR)} Punkte, {kurve.T_range[0]:g} ... {kurve.T_range[1]:g} K, "
          f"Sensor {kurve.model or '?'}, Seriennummer {kurve.serial or '?'}")
    if len(kurve.lnR) > th.MAX_POINTS:
        print(f"  Hinweis: mehr als {th.MAX_POINTS} Punkte, wird gleichmäßig ausgedünnt.")

    if opener is None:
        from instruments.real import Visa
        opener = lambda adr: Visa(adr, timeout_ms=5000)   # noqa: E731
    io = opener(addr)
    try:
        ls = th.LakeShore(io, model, inp)
        print(f"Gerät an {addr}: {ls.identify()}")
        jetzt = ls.assigned_curve()
        print(f"Eingang {inp} benutzt Kurve: {'?' if jetzt is None else jetzt}")
        ziel = ls.curve_header(a.kurve)
        belegt = bool(ziel["name"].strip()) and ziel["name"].strip().lower() not in ("user curve", "")
        print(f"Platz {a.kurve} enthält: {ziel['name'] or '(leer)'} {ziel['serial']}".rstrip())

        if a.nur_zeigen:
            return 0
        if cfg["mode"].get("read_only", False):
            print("read_only = true in config.toml: es wird nichts geschrieben.")
            return 3
        if jetzt == a.kurve and not a.ja:
            print(f"Achtung: Eingang {inp} rechnet gerade mit Platz {a.kurve}. Während des Schreibens "
                  "stimmt seine Temperatur nicht.")
        if not a.ja:
            frage = (f"Platz {a.kurve} " + ("überschreiben" if belegt else "beschreiben")
                     + (f" und Eingang {inp} zuweisen" if a.zuweisen else "") + "? [j/N] ")
            if eingabe(frage).strip().lower() not in ("j", "ja", "y", "yes"):
                print("Abgebrochen, nichts geschrieben.")
                return 1

        pause = 0.05 if model == "370" else 0.02
        erg = ls.upload_curve(kurve, a.kurve, a.name, a.serie, a.format, pause_s=pause)
        h = erg["header"]
        print(f"Geschrieben und zurückgelesen: {erg['points']} Punkte, größte Abweichung "
              f"{erg['max_rel_dev']:.1e}, Kopf {h['name']} / {h['serial']}, Format {h['format']}, "
              f"Grenze {h['limit_K']} K" + (", ausgedünnt" if erg["thinned"] else ""))
        if a.zuweisen:
            ls.assign_curve(a.kurve)
            print(f"Eingang {inp} rechnet jetzt mit Platz {a.kurve}.")
            T = ls.read()
            print(f"Erster Wert: T = {T['T_cell']} K, R = {T['R_cell']} Ohm")
        print('Jetzt in config.toml [cell_thermometer] curve_in = "device" setzen.')
        return 0
    finally:
        io.close()


if __name__ == "__main__":
    sys.exit(main())
