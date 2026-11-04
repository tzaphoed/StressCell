import math

import pytest

from core.interpreter import StepError
from core.safety import LimitError
from core.thermocouple import seebeck_type_e

from .conftest import advance, run_steps, seq


@pytest.mark.parametrize("T,lo,hi", [(300, 60e-6, 63e-6), (77, 20e-6, 32e-6)])
def test_seebeck_magnitude(T, lo, hi):
    assert lo < seebeck_type_e(T) < hi


def on(st):
    st.safety.output_on()


def test_ac_refused_when_peak_leaves_window(stack):
    stack.cfg["ac"]["V_max"] = 200.0
    stack.cfg["ac"]["current_max_A"] = 1.0
    stack.cfg["limits"]["force_max_N"] = 1e4
    stack.world.p["F_break_N"] = 1e4
    on(stack)
    stack.safety.set_voltages(110, 0)
    advance(stack, 12)
    with pytest.raises(LimitError, match="window"):
        stack.safety.set_ac(10, 15, simulated=True)


def test_ac_current_limit(stack):
    on(stack)
    with pytest.raises(LimitError, match="mA"):
        stack.safety.set_ac(1000, 5, simulated=True)       # 2 pi f C V = 97 mA
    with pytest.raises(LimitError):
        stack.safety.set_ac(200, 2, simulated=True)        # 7,8 mA
    stack.safety.set_ac(100, 2, simulated=True)            # 3,9 mA


def test_ac_locked_on_real_hardware_until_gain_verified(stack):
    on(stack)
    with pytest.raises(LimitError, match="gain"):
        stack.safety.set_ac(21, 1, simulated=False)
    stack.cfg["ac"]["gain_verified"] = True
    stack.safety.set_ac(21, 1, simulated=False)


def test_dc_cannot_push_peak_out_of_window(stack):
    on(stack)
    stack.safety.set_ac(21, 10, simulated=True)
    with pytest.raises(LimitError):
        stack.safety.set_voltages(115, 0)


def test_warming_is_refused_while_ac_needs_the_room(stack):
    stack.cfg["limits"]["force_max_N"] = 1e4
    stack.world.p["F_break_N"] = 1e4
    run_steps(stack, {"op": "set_temperature", "T": 4, "rate": 10}, {"op": "wait_temperature", "hold": 5})
    advance(stack, 300)
    on(stack)
    stack.safety.set_voltages(115, 0)
    advance(stack, 15)
    stack.safety.set_ac(21, 10, simulated=True)
    with pytest.raises(LimitError):
        stack.safety.set_temperature(300, 5)


def test_guard_switches_ac_off_before_the_peak_leaves_the_window(stack):
    stack.cfg["limits"]["force_max_N"] = 1e4
    stack.world.p["F_break_N"] = 1e4
    run_steps(stack, {"op": "set_temperature", "T": 60, "rate": 10}, {"op": "wait_temperature", "hold": 5})
    advance(stack, 300)
    on(stack)
    stack.safety.set_voltages(140, 0)
    advance(stack, 20)
    stack.safety.set_ac(21, 10, simulated=True)
    stack.inst.cryo.set_temperature(300, 10)
    advance(stack, 120)
    assert stack.world.ac_V == 0.0
    assert "ac_window" in stack.supervisor.alarms.active


@pytest.mark.parametrize("stop", ["stop_all", "zero", "arc"])
def test_every_stop_ends_the_ac_drive(stack, stop):
    on(stack)
    stack.safety.set_ac(21, 5, simulated=True)
    if stop == "stop_all":
        stack.stop_all()
    elif stop == "zero":
        stack.safety.zero()
    else:
        stack.world.inject_arc("tension", 4e-3)
        advance(stack, 3)
    assert stack.world.ac_V == 0.0 and stack.safety.ac_V == 0.0


def test_frequency_sweep_shows_a_plateau(stack):
    rows = []
    stack.publish = lambda ev: rows.append(ev["data"]) if ev["type"] == "point" else None
    run_steps(stack, {"op": "output", "on": True}, {"op": "ac_on", "f": 2, "V": 2},
              {"op": "frequency_sweep", "from": 2, "to": 200, "points": 9, "settle": 1, "n": 1})
    R = {round(r["f_ac"], 1): r["R"] / r["V_ac"] for r in rows}
    assert R[20.0] > 1.5 * R[2.0]                # unter 1/tau_i fällt das Signal
    assert 0.7 < R[20.0] / R[63.2] < 1.4       # Plateau
    assert max(r["V_ac"] for r in rows) == 2 and min(r["V_ac"] for r in rows) < 1.5


def test_temperature_sweep_finds_curie_shift_with_field(stack):
    stack.cfg["limits"]["force_max_N"] = 1e4

    def peak(B):
        rows = []
        stack.publish = lambda ev: rows.append(ev["data"]) if ev["type"] == "point" else None
        run_steps(stack, {"op": "set_field", "B": B, "rate": 0.6}, {"op": "wait_field"},
                  {"op": "set_temperature", "T": 280, "rate": 10}, {"op": "wait_temperature", "hold": 60})
        run_steps(stack, {"op": "ac_on", "f": 21, "V": 2},
                  {"op": "temperature_sweep", "to": 320, "rate": 2, "dt": 10})
        return max(rows, key=lambda r: r["R"])["T"]
    stack.safety.output_on()
    assert peak(5.0) - peak(0.0) == pytest.approx(5.0, abs=2.0)


def test_pickup_appears_in_quadrature_and_scales_with_field(stack):
    stack.world.p["dTc_dB"] = 0.0
    on(stack)
    stack.safety.set_ac(100, 2, simulated=True)
    stack.world.B = stack.world.B_set = 9.0
    a = stack.inst.lockin.read()
    stack.world.B = stack.world.B_set = 0.0
    b = stack.inst.lockin.read()
    assert a["X"] == pytest.approx(b["X"], rel=1e-6)
    assert a["Y"] - b["Y"] == pytest.approx(2e-11 * 100 * 9 * 100 * 2 / math.sqrt(2), rel=0.01)


def test_measure_gain_gives_force_amplitude(stack):
    run_steps(stack, {"op": "output", "on": True}, {"op": "tare"}, {"op": "measure_gain", "step": 4, "settle": 5})
    assert 0.2 < stack.supervisor.G_at(300.0) < 1.2
    assert stack.safety.drive() == pytest.approx(0.0)
    run_steps(stack, {"op": "ac_on", "f": 21, "V": 2})
    advance(stack, 2)
    s = stack.supervisor.last
    assert s["F_ac_N"] == pytest.approx(stack.supervisor.G_at(300.0) * 2)
    assert s["T_ac_K"] > 0 and s["dT_dsigma_K_MPa"] > 0


def test_field_map_sequence_dry_run(stack):
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "sequences" / "ece_field_map.seq.json"
    rep = stack.dry_run(json.loads(p.read_text()))
    assert rep["ok"], rep["error"]
    assert rep["points"] > 50


def test_compression_only_wiring_keeps_tension_at_zero(stack):
    stack.inst.supply.compression_only = True
    on(stack)
    stack.safety.set_drive(-60)
    assert stack.inst.supply.voltage_setpoint("tension") == 0.0
    assert stack.inst.supply.voltage_setpoint("compression") == pytest.approx(60.0)


def test_gain_is_not_used_far_from_where_it_was_measured(stack):
    sup = stack.supervisor
    sup.G_table = [(300.0, 0.4)]
    assert sup.G_at(301.0) == 0.4
    assert sup.G_at(4.0) is None


def test_startup_ends_a_drive_left_running(cfg):
    from core import stack as sm
    st = sm.build(cfg, virtual=True, noise=False)
    st.world.ac_f, st.world.ac_V = 21.0, 5.0
    st.init_cap()
    assert st.world.ac_V == 0.0
