"""Einmal alle Geräte ansprechen, ohne Spannung oder Temperatur zu ändern.

    python check_instruments.py

Schreibt nach instrument_check_<Zeit>.txt, damit man das Ergebnis weiterreichen kann.
Lesebefehle sind das Einzige, was hier läuft, außer der Grundeinstellung der Messgeräte
beim Öffnen (Format, Messfunktion).
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


def main():
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    i = cfg["instruments"]
    out(f"StressCell instrument check {datetime.now():%Y-%m-%d %H:%M:%S}")

    out(f"\n[supply] {i['supply']}")
    sup = None
    if i["supply"] == "rp100":
        dev = probe("open " + i["rp100"], lambda: real.Visa(i["rp100"], "\r\n", "\n"))
        if dev:
            sup = real.RP100(dev, i["rp100_ch_tension"], i["rp100_ch_compression"])
    else:
        out("  skipped: opening the Keithleys changes their source setup")
    if sup:
        probe("identify", sup.identify)
        for c in CHANNELS:
            probe(f"{c} output", lambda: sup.output(c))
            probe(f"{c} setpoint V", lambda: sup.voltage_setpoint(c))
            probe(f"{c} now V", lambda: sup.voltage_now(c))
            probe(f"{c} current A", lambda: sup.current(c))
            probe(f"{c} slew V/s", lambda: sup.slew(c))
        probe("error queue", sup.errors)

    out(f"\n[capacitance] {i['cap']} at {i['capmeter']}")
    def open_cap():
        if i["cap"] == "e4980al":
            return real.E4980AL(real.Visa(i["capmeter"]))
        return real.AHBridge(real.Visa(i["capmeter"], timeout_ms=10000), i["cap"][2:].upper())
    cap = probe("open", open_cap)
    if cap:
        probe("identify", cap.identify)
        for _ in range(3):
            probe("read", cap.read)

    out(f"\n[cryostat] MultiPyVu at {i['multipyvu_host']}:{i['multipyvu_port']}")
    cryo = probe("open", lambda: real.PPMS(i["multipyvu_host"], i["multipyvu_port"]))
    if cryo:
        probe("identify", cryo.identify)
        probe("read", cryo.read)

    out("\n[calibration]")
    cal = probe("load", lambda: ForceCalibration.load(cfg["paths"]["calibration_dir"]
                                                      / cfg["capacitance"]["calibration"]))
    if cal and not cal.verified:
        out("  note verified = false: the force loop stays locked on real instruments")

    p = Path(f"instrument_check_{datetime.now():%Y%m%d_%H%M%S}.txt")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\nsaved", p)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
