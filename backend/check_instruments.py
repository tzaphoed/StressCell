"""Alle Geräte ansprechen, ohne Spannung, Temperatur oder Messeinstellung zu ändern.

    python check_instruments.py              Konfiguration aus config.toml
    python check_instruments.py andere.toml

Was passiert:
  1. Liste aller VISA-Geräte, die der Rechner sieht (Adressen zum Eintragen in config.toml).
  2. Jedes konfigurierte Gerät: Adresse öffnen und *IDN? (Signal Recovery: ID) fragen.
  3. RP100: Kanäle lesen. Kapazitätsmessung: drei Messwerte. PPMS: T, B, Kammer.
  4. Kalibrierung laden, Nullpunkt und Empfindlichkeit zeigen.
  5. Sicherheitsrelevante Konfiguration prüfen (Bleeder, Verstärkung, verified).

Geschrieben wird nur bei der Kapazitätsmessung, und auch nur Format und Messfunktion
beim Öffnen. Das Ergebnis landet in instrument_check_<Zeit>.txt zum Weitergeben.
"""

from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.calibration import ForceCalibration   # noqa: E402
from core.config import load_config             # noqa: E402
from instruments import real                    # noqa: E402
from instruments.base import CHANNELS           # noqa: E402

lines = []


def out(s=""):
    print(s)
    lines.append(s)


def probe(name, fn):
    t0 = time.perf_counter()
    try:
        v = fn()
        out(f"  ok   {name}: {v}  ({(time.perf_counter()-t0)*1e3:.0f} ms)")
        return v
    except Exception as e:
        out(f"  FAIL {name}: {type(e).__name__}: {e}")
        return None


def config_findings(cfg) -> list[str]:
    """Was vor dem ersten Einschalten noch fehlt. Leer heißt: nichts gefunden, nicht: alles gut."""
    i, ac, f = cfg["instruments"], cfg.get("ac", {}), []
    if i.get("supply") == "split":
        for c in CHANNELS:
            cc = i.get(c, {})
            if cc.get("source") != "rp100" and not cc.get("bleeder_fitted"):
                f.append(f"[instruments.{c}] bleeder_fitted = false: the program will refuse to start")
            if cc.get("source") == "hv_amp" and not cc.get("gain_verified"):
                f.append(f"[instruments.{c}] hv_amp gain not verified with a scope")
    if ac.get("enabled") and not ac.get("gain_verified"):
        f.append("[ac] gain_verified = false: AC drive stays locked on real instruments")
    if cfg["mode"].get("simulate", True):
        f.append("[mode] simulate = true: the program runs the simulation, not these instruments")
    if not cfg["mode"].get("read_only", False):
        f.append("[mode] read_only = false: for the first contact read_only = true is safer")
    if cfg["limits"].get("force_max_N", 0) > 50:
        f.append(f"[limits] force_max_N = {cfg['limits']['force_max_N']} N: is that right for your sample?")
    return f


def main():
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    i = cfg["instruments"]
    out(f"StressCell instrument check {datetime.now():%Y-%m-%d %H:%M:%S}")
    out(f"config: {cfg['paths']['config']}")

    out("\n[VISA] resources visible on this computer")
    try:
        import pyvisa
        rm = pyvisa.ResourceManager()
        for r in rm.list_resources():
            out(f"  {r}")
    except Exception as e:
        out(f"  FAIL pyvisa: {e}")

    out("\n[configured instruments] identify only")
    opened = {}
    for role, model, addr in real.configured_devices(cfg):
        out(f"- {role}: {model} at {addr}")
        if addr in opened:
            out("  (same session as above)")
            continue
        kw = {"read_termination": "\r\n"} if model == "rp100" else {}
        dev = probe("open", lambda: real.Visa(addr, **kw))
        opened[addr] = dev
        if dev is None:
            continue
        if model.startswith("sr72"):
            probe("ID", lambda: dev.query("ID"))        # Signal Recovery kennt kein *IDN?
        else:
            probe("*IDN?", lambda: dev.query("*IDN?"))
        if model == "rp100":
            sup = real.RP100(dev, i.get("rp100_ch_tension", 1), i.get("rp100_ch_compression", 2))
            for c in CHANNELS:
                probe(f"{c} output", lambda: sup.output(c))
                probe(f"{c} setpoint V", lambda: sup.voltage_setpoint(c))
                probe(f"{c} now V", lambda: sup.voltage_now(c))
                probe(f"{c} current A", lambda: sup.current(c))
            probe("error queue", sup.errors)

    out(f"\n[capacitance] {i['cap']}: three readings")
    cap = probe("open", lambda: real.build_cap(cfg, real.VisaPool(lambda a, **kw: opened.get(a) or real.Visa(a, **kw))))
    if cap:
        for _ in range(3):
            probe("read", cap.read)

    ct = cfg.get("cell_thermometer", {})
    if ct.get("source") == "lakeshore":
        from core.thermometry import Curve
        from instruments import thermometers
        out(f"\n[cell thermometer] Lake Shore {ct.get('model')} input {ct.get('input')} at {ct.get('address')}")
        io = opened.get(ct.get("address"))
        if io is not None:
            ls = thermometers.LakeShore(io, ct.get("model", "336"), ct.get("input", "A"))
            info = probe("assigned curve", ls.curve_info)
            for _ in range(3):
                probe("read (device curve)", ls.read)
            cp = cfg["paths"]["calibration_dir"] / ct.get("calibration", "")
            if ct.get("calibration") and cp.is_file():
                curve = probe(f"load {cp.name}", lambda: Curve.load(cp))
                if curve is not None:
                    out(f"  file: sensor {curve.model or '?'}, serial {curve.serial or '?'}, "
                        f"{curve.T_range[0]:g} ... {curve.T_range[1]:g} K")
                    if info and info.get("serial") and curve.serial and info["serial"] != curve.serial:
                        out(f"  WARNING serial in the device ({info['serial']}) differs from the file ({curve.serial})")
                    probe("read (file curve)", lambda: thermometers.LakeShore(io, ct.get("model", "336"),
                                                                              ct.get("input", "A"), curve).read())
            else:
                out(f"  note no curve file {cp.name} in calibrations/: cannot compare with the device curve")

    out(f"\n[cryostat] MultiPyVu at {i['multipyvu_host']}:{i['multipyvu_port']}")
    cryo = probe("open", lambda: real.PPMS(i["multipyvu_host"], i["multipyvu_port"]))
    if cryo:
        probe("identify", cryo.identify)
        probe("read", cryo.read)

    out("\n[calibration]")
    cal = probe("load", lambda: ForceCalibration.load(cfg["paths"]["calibration_dir"]
                                                      / cfg["capacitance"]["calibration"]))
    if cal:
        for k, v in cal.info().items():
            out(f"  {k}: {v}")
        if not cal.verified:
            out("  note verified = false: the force loop stays locked on real instruments")

    out("\n[configuration] open points")
    for f in config_findings(cfg) or ["none found"]:
        out(f"  - {f}")

    p = Path(f"instrument_check_{datetime.now():%Y%m%d_%H%M%S}.txt")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\nsaved", p)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
