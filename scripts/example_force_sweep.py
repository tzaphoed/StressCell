"""Beispiel für den Python-Zugang. Läuft ohne Backend und ohne Geräte:

    python scripts/example_force_sweep.py          # Simulation, sofort fertig
    python scripts/example_force_sweep.py --lab    # gegen das laufende Backend
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from stresscell_client import Lab, LabError  # noqa: E402

lab = Lab(session="force_sweep") if "--lab" in sys.argv else Lab.simulated(session="force_sweep")

with lab:
    lab.output(True)
    lab.tare()                          # Nullpunkt vor dem Abkühlen, ohne Last
    lab.set_temperature(50, rate=5)
    lab.wait_temperature(tol=0.1, hold=30)
    for F in (0, 2, 4, 6, 8):
        lab.goto_force(F, tol=0.05, hold=5)
        lab.measure(n=3, dt=1, label=f"{F} N")
        print(f"F = {lab.F:6.3f} N   T = {lab.T:6.2f} K   C = {lab.C:.6f} pF")
    try:
        lab.goto_force(50)              # über force_max_N: wird abgelehnt
    except LabError as e:
        print("refused:", e)
    lab.zero_drive()
    lab.wait_voltage()
