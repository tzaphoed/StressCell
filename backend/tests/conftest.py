import copy
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import resolve_paths          # noqa: E402
from core import stack as stack_mod            # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    with open(ROOT / "config.toml", "rb") as f:
        c = tomllib.load(f)
    c["paths"].update(data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs"),
                      calibration_dir=str(tmp_path / "cal"), sequence_dir=str(tmp_path / "seq"))
    resolve_paths(c, tmp_path)
    for name in ("cell_example.toml", "FC100_0260.toml"):
        (c["paths"]["calibration_dir"] / name).write_bytes((ROOT.parent / "calibrations" / name).read_bytes())
    c["capacitance"]["calibration"] = "cell_example.toml"   # lineare Sim-Zelle, eigene Tests für #0260
    # Die Grundtests laufen mit zwei DC-Kanälen (RP100) und der Ikeda-Kette. Die geteilte
    # Verdrahtung ohne RP100 hat eigene Tests (test_split.py).
    c["instruments"]["supply"] = "rp100"
    c["cell_thermometer"]["source"] = "none"     # eigene Tests mit Zellthermometer (test_cernox_recorder.py)
    c["ac"].update(gain=25.0, series_R_ohm=0.0, V_max=20.0)
    c["paths"]["config"] = tmp_path / "config.toml"
    c["limits"]["slew_default_V_s"] = 10.0      # Tests laufen sonst minutenlang durch die Rampen
    c["limits"]["sealed_V_max"] = 400.0         # eigener Test für die Vorgabe
    c["limits"]["tare_T_min_K"] = 0.0           # eigener Test
    c["limits"]["T_cell_tau_s"] = 20.0          # passt zur Sim-Verzögerung
    return c


@pytest.fixture
def stack(cfg):
    """Simulation mit virtueller Uhr, ohne Rauschen, Monitor wird von Hand getaktet."""
    st = stack_mod.build(cfg, virtual=True, noise=False, seed=1)
    st.init_cap()
    st.supervisor.tick()
    return st


def seq(*steps, name="t"):
    return {"format": "stresscell-sequence", "version": 1, "name": name, "steps": list(steps)}


def run_steps(st, *steps):
    """Schritte in der virtuellen Uhr ausführen, Guard läuft dabei mit."""
    from core.interpreter import Context, Interpreter
    ctx = Context(st)
    Interpreter(ctx).run(list(steps))
    return ctx


def relax_break(st):
    """Bruchlogik aus dem Weg, wenn sie nicht geprüft wird."""
    st.cfg["limits"]["force_step_trip_N"] = 1e4
    st.world.p["F_break_N"] = 1e4


def advance(st, seconds, tick=1.0):
    t = 0.0
    while t < seconds:
        st.clock.sleep(tick)
        st.supervisor.tick()
        t += tick
