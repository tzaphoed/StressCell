import pytest

from core.interpreter import StepError
from core.safety import LimitError

from .conftest import advance, run_steps


def free_force(st):
    """Kraftgrenzen aus dem Weg, damit nur der geprüfte Mechanismus eingreift."""
    st.cfg["limits"]["force_max_N"] = 1e4
    st.world.p["F_break_N"] = 1e4
    st.cfg["limits"]["force_step_trip_N"] = 1e4


def cool(st, T=4.0):
    run_steps(st, {"op": "set_temperature", "T": T, "rate": 10}, {"op": "wait_temperature", "hold": 5})
    advance(st, 300)


def test_refuses_outside_window_at_300K(stack):
    stack.safety.output_on()
    with pytest.raises(LimitError):
        stack.safety.set_voltages(121, 0)
    with pytest.raises(LimitError):
        stack.safety.set_voltages(0, -21)
    assert stack.safety.setpoints() == {"tension": 0.0, "compression": 0.0}


def test_refuses_nan_and_text(stack):
    stack.safety.output_on()
    for bad in (float("nan"), float("inf"), "x", None):
        with pytest.raises(LimitError):
            stack.safety.set_voltages(bad, 0)


def test_slew_limit(stack):
    stack.safety.output_on()
    with pytest.raises(LimitError):
        stack.safety.set_voltages(10, 0, slew=1000)
    with pytest.raises(LimitError):
        stack.safety.set_voltages(10, 0, slew=0)


def test_voltage_needs_output_on(stack):
    with pytest.raises(LimitError):
        stack.safety.set_voltages(10, 0)


def test_no_voltage_before_first_reading(cfg):
    from core import stack as sm
    st = sm.build(cfg, virtual=True, noise=False)
    with pytest.raises(LimitError):
        st.safety.output_on()


def test_output_on_clears_stale_setpoint(stack):
    """Das RP100 behält den Sollwert bei ausgeschaltetem Ausgang."""
    stack.inst.supply.set_voltage("tension", 100.0)
    stack.safety.output_on()
    assert stack.inst.supply.voltage_setpoint("tension") == 0.0
    assert stack.world.v["tension"] == 0.0


def test_read_only_refuses_everything(stack):
    stack.safety.read_only = True
    for fn in (stack.safety.output_on, lambda: stack.safety.set_temperature(10, 1),
               lambda: stack.safety.set_field(1, 0.1), lambda: stack.safety.set_chamber("seal")):
        with pytest.raises(LimitError):
            fn()


def test_temperature_limits(stack):
    with pytest.raises(LimitError):
        stack.safety.set_temperature(400, 1)
    with pytest.raises(LimitError):
        stack.safety.set_temperature(10, 11)   # Datenblatt: niemals über 10 K/min
    with pytest.raises(LimitError):
        stack.safety.set_field(20, 0.1)


def test_excitation_limit_40Vpp(stack):
    with pytest.raises(LimitError):
        stack.safety.configure_cap(1000, 15)   # AH2700A könnte 15 V rms, die Zelle verträgt 14,1


def test_warming_with_high_voltage_refused(stack):
    free_force(stack)
    cool(stack)
    stack.safety.output_on()
    stack.safety.set_voltages(190, -190)
    advance(stack, 60)
    with pytest.raises(LimitError):
        stack.safety.set_temperature(300, 5)
    assert stack.world.T_set == 4.0


def test_guard_derates_while_warming(stack):
    """Der Fall, den Befehlsprüfung nicht abfängt: jemand wärmt von außen auf."""
    free_force(stack)
    cool(stack)
    stack.safety.output_on()
    stack.safety.set_voltages(190, -190)
    advance(stack, 60)
    assert stack.world.v["tension"] == pytest.approx(190, abs=0.5)
    stack.inst.cryo.set_temperature(300, 10)           # am Sicherheitscode vorbei wie MultiVu-Bedienung
    w = stack.world
    from core.fc100 import voltage_window
    while w.T < 299.9:
        advance(stack, 1)
        lo, hi = voltage_window(w.T_cell)
        assert lo - 0.5 <= w.v["tension"] <= hi + 0.5, (w.T_cell, w.v)
        assert lo - 0.5 <= w.v["compression"] <= hi + 0.5, (w.T_cell, w.v)
    assert "voltage_window" in [a["key"] for a in stack.supervisor.alarms.history]


def test_thermometer_failure_falls_back_to_300K_window(stack):
    free_force(stack)
    cool(stack)
    stack.safety.output_on()
    stack.safety.set_voltages(190, -190)
    advance(stack, 60)
    stack.world.fault_T_dead = True
    advance(stack, 60)
    assert stack.world.v["tension"] <= 120.5
    assert stack.world.v["compression"] >= -20.5
    assert "stale_T" in stack.supervisor.alarms.active


def test_chamber_change_refused_with_high_voltage(stack):
    free_force(stack)
    cool(stack)
    stack.safety.output_on()
    stack.safety.set_voltages(100, -100)
    advance(stack, 30)
    with pytest.raises(LimitError):
        stack.safety.set_chamber("purge_seal")


def test_guard_limits_voltage_if_chamber_moves_from_outside(stack):
    free_force(stack)
    cool(stack)
    stack.safety.output_on()
    stack.safety.set_voltages(100, -100)
    advance(stack, 30)
    stack.inst.cryo.set_chamber("purge_seal")
    advance(stack, 15)
    assert abs(stack.world.v["tension"]) <= 50.5 and abs(stack.world.v["compression"]) <= 50.5


def test_arc_opens_outputs_and_latches(stack):
    stack.safety.output_on()
    stack.safety.set_voltages(50, 0)
    advance(stack, 10)
    stack.world.inject_arc("tension", 4e-3)
    advance(stack, 3)
    assert not stack.world.on["tension"] and not stack.world.on["compression"]
    stack.world.clear_faults()
    advance(stack, 2)
    with pytest.raises(LimitError):
        stack.safety.output_on()
    stack.ack()
    stack.safety.output_on()
    assert stack.world.on["tension"]


def test_single_current_spike_is_ignored(stack):
    stack.safety.output_on()
    stack.world.inject_arc("tension", 4e-3)
    advance(stack, 1)
    stack.world.clear_faults()
    advance(stack, 5)
    assert stack.world.on["tension"]


def test_normal_ramp_does_not_trip_arc(stack):
    stack.safety.output_on()
    stack.safety.set_voltages(100, -10, slew=10)
    advance(stack, 15)
    assert "arc" not in stack.supervisor.alarms.active


def test_sample_break_ramps_down_and_latches(stack):
    stack.cfg["limits"]["force_max_N"] = 100.0
    stack.world.p["F_break_N"] = 15.0
    stack.safety.output_on()
    stack.safety.set_voltages(120, -20, slew=1)
    advance(stack, 40)
    assert stack.world.broken
    assert "sample_break" in stack.supervisor.alarms.active
    advance(stack, 30)
    assert abs(stack.world.v["tension"]) < 0.1


def test_overforce_reduces_drive(stack):
    stack.cfg["limits"]["force_max_N"] = 10.0
    stack.world.p["F_break_N"] = 1000.0
    stack.cfg["limits"]["force_step_trip_N"] = 1000.0
    stack.safety.output_on()
    stack.safety.set_voltages(120, -20, slew=10)
    advance(stack, 60)
    assert abs(stack.world.F) <= 10.5


def test_overforce_with_thermal_stress_does_not_push_further(stack):
    """Kraft aus thermischer Fehlanpassung bei D = 0: der Guard darf den Drive nicht
    in die Gegenrichtung vergrößern."""
    stack.world.p["thermal_mismatch_um"] = 30.0
    stack.cfg["limits"]["force_max_N"] = 5.0
    stack.world.p["F_break_N"] = 1000.0
    stack.cfg["limits"]["force_step_trip_N"] = 1000.0
    stack.safety.output_on()
    cool(stack)
    assert "overforce" in stack.supervisor.alarms.active
    assert stack.safety.setpoints() == {"tension": 0.0, "compression": 0.0}


def test_stop_all(stack):
    stack.safety.output_on()
    stack.safety.set_voltages(100, 0)
    advance(stack, 12)
    stack.stop_all()
    advance(stack, 8)
    assert stack.world.v["tension"] == pytest.approx(0, abs=0.01)
    assert stack.world.on["tension"]


def test_shutdown_ramps_to_zero(stack):
    stack.safety.output_on()
    stack.safety.set_voltages(100, 0)
    advance(stack, 12)
    stack.shutdown()
    assert stack.inst.supply.voltage_setpoint("tension") == 0.0


def test_output_off_refused_under_voltage_but_arc_still_opens(stack):
    stack.safety.output_on()
    stack.safety.set_voltages(50, 0)
    advance(stack, 10)
    with pytest.raises(LimitError):
        stack.safety.output_off()
    assert stack.world.on["tension"]
    stack.safety.zero()
    advance(stack, 10)
    stack.safety.output_off()
    assert not stack.world.on["tension"]


def test_single_lost_cryostat_answer_does_not_trip_the_guard(stack):
    free_force(stack)
    cool(stack)
    stack.safety.output_on()
    stack.safety.set_voltages(100, -100)
    advance(stack, 25)
    stack.world.fault_T_dead = True
    advance(stack, 2)
    stack.world.clear_faults()
    advance(stack, 2)
    assert stack.safety.setpoints()["tension"] == 100
    assert "voltage_window" not in stack.supervisor.alarms.active


def test_supply_loss_raises_alarm(stack):
    stack.inst.supply.voltage_setpoint = lambda c: (_ for _ in ()).throw(IOError("usb"))
    advance(stack, 2)
    assert "supply_lost" not in stack.supervisor.alarms.active     # eine Lücke bricht nichts ab
    advance(stack, 8)
    assert "supply_lost" in stack.supervisor.alarms.active


# ── Befunde der unabhängigen Prüfung ─────────────────────────────────────

def test_default_sealed_chamber_limits_voltage_to_100V(cfg):
    from core import stack as sm
    cfg["limits"]["sealed_V_max"] = 80.0
    st = sm.build(cfg, virtual=True, noise=False)
    st.init_cap()
    st.world.T = st.world.T_cell = st.world.T_set = 4.0
    st.world.chamber = "Purged and Sealed"
    st.supervisor.tick()
    assert st.safety.window() == (-80.0, 80.0)
    st.world.chamber = "HiVac"
    st.supervisor.tick()
    assert st.safety.window() == (-200.0, 200.0)


def test_shipped_config_is_conservative():
    import tomllib
    from pathlib import Path
    c = tomllib.load(open(Path(__file__).resolve().parents[1] / "config.toml", "rb"))
    assert c["chamber"]["safe_states"] == ["HiVac"]
    assert c["limits"]["sealed_V_max"] < 100.0


@pytest.mark.parametrize("value,status", [(0.0, "Stable"), (float("nan"), "Stable"), (4.0, "General Failure"),
                                          (500.0, "Stable"), (-3.0, "Stable")])
def test_implausible_temperature_never_opens_the_window(stack, value, status):
    orig = stack.inst.cryo.read
    stack.inst.cryo.read = lambda: {**orig(), "T": value, "T_status": status}
    advance(stack, 3)
    assert stack.safety.window()[0] >= -20.0 - 1e-9
    assert stack.safety.window()[1] <= 120.0 + 1e-9


def test_nan_temperature_does_not_kill_lookahead(stack):
    orig = stack.inst.cryo.read
    stack.inst.cryo.read = lambda: {**orig(), "T": float("nan")}
    advance(stack, 2)
    stack.inst.cryo.read = orig
    advance(stack, 2)
    assert stack.safety.dTdt == stack.safety.dTdt


def test_jump_is_ignored_until_it_stays(stack):
    orig = stack.inst.cryo.read
    stack.inst.cryo.read = lambda: {**orig(), "T": 100.0}
    advance(stack, 2)
    assert stack.safety.T == 300.0
    advance(stack, 30)
    assert stack.safety.T in (None, 300.0)            # nach unten nie von selbst
    assert stack.safety.window() == (-20.0, 120.0)
    stack.ack()
    advance(stack, 2)
    assert stack.safety.T == 100.0


def test_overforce_does_not_override_window_in_same_tick(stack):
    """Fenster +-50 V, Sollwerte +-150 V, F über Grenze: nie ein Sollwert außerhalb."""
    free_force(stack)
    cool(stack)
    stack.safety.output_on()
    stack.safety.set_voltages(150, -150, slew=10)
    advance(stack, 20)
    stack.cfg["limits"]["force_max_N"] = 5.0
    stack.inst.cryo.set_chamber("pump_continuous")
    advance(stack, 1)
    for _ in range(3):
        sp = stack.safety.setpoints()
        assert max(abs(v) for v in sp.values()) <= 50.0 + 1e-6
        advance(stack, 1)


def test_cooling_keeps_window_narrow_until_the_cell_followed(stack):
    stack.cfg["limits"]["T_cell_tau_s"] = 300.0
    run_steps(stack, {"op": "set_temperature", "T": 4, "rate": 10}, {"op": "wait_temperature", "hold": 5})
    assert stack.supervisor.last["T"] == pytest.approx(4, abs=0.1)
    assert stack.safety.window()[1] < 200.0          # Thermometer sagt 4 K, die Zelle noch nicht
    advance(stack, 3000)
    assert stack.safety.window()[1] == 200.0


def test_failing_lockin_does_not_block_stop(stack):
    stack.safety.output_on()
    stack.safety.set_voltages(50, 0)
    advance(stack, 8)
    stack.inst.lockin.set_drive = lambda f, v: (_ for _ in ()).throw(IOError("timeout"))
    r = stack.stop_all()
    assert r["ok"] and stack.inst.supply.voltage_setpoint("tension") == 0.0


def test_failing_lockin_during_ac_still_stops_voltage(stack):
    stack.safety.output_on()
    stack.safety.set_ac(21, 2, simulated=True)
    stack.inst.lockin.set_drive = lambda f, v: (_ for _ in ()).throw(IOError("timeout"))
    assert stack.stop_all()["ok"]
    advance(stack, 2)
    assert "ac_fault" in stack.supervisor.alarms.active


def test_one_failing_channel_does_not_stop_the_other(stack):
    stack.safety.output_on()
    stack.safety.set_voltages(50, -10)
    advance(stack, 8)
    real = stack.inst.supply.set_voltage

    def bad(c, v):
        if c == "tension":
            raise IOError("bus")
        real(c, v)
    stack.inst.supply.set_voltage = bad
    r = stack.stop_all()
    assert not r["ok"]
    assert stack.world.v_set["compression"] == 0.0


def test_arc_opens_outputs_even_if_setpoint_write_fails(stack):
    stack.safety.output_on()
    stack.inst.supply.set_voltage = lambda c, v: (_ for _ in ()).throw(IOError("bus"))
    stack.world.inject_arc("tension", 4e-3)
    advance(stack, 3)
    assert not stack.world.on["tension"] and not stack.world.on["compression"]
    assert "arc" in stack.supervisor.alarms.active


def test_guard_write_failure_raises_alarm_and_holds_warming(stack):
    free_force(stack)
    cool(stack)
    stack.safety.output_on()
    stack.safety.set_voltages(150, -150, slew=10)
    advance(stack, 20)
    stack.inst.supply.set_voltage = lambda c, v: (_ for _ in ()).throw(IOError("bus"))
    stack.inst.cryo.set_temperature(300, 10)
    advance(stack, 300)
    assert "guard_write" in stack.supervisor.alarms.active
    assert stack.world.T_set < 100.0                  # Aufwärmen angehalten


def test_supply_loss_stops_warming(stack):
    free_force(stack)
    cool(stack)
    stack.safety.output_on()
    stack.safety.set_voltages(100, -100, slew=10)
    advance(stack, 15)
    stack.inst.cryo.set_temperature(300, 10)
    advance(stack, 3)
    for name in ("voltage_setpoint", "voltage_now", "current", "output"):
        setattr(stack.inst.supply, name, lambda c: (_ for _ in ()).throw(IOError("usb")))
    advance(stack, 10)
    assert stack.world.T_set < 100.0


def test_overforce_acts_from_actual_voltage_not_setpoint(stack):
    """Rampe auf 100 V mit 1 V/s, Grenze 5 N: Spitze darf nicht weit über die Grenze."""
    stack.cfg["limits"]["force_max_N"] = 5.0
    stack.world.p["F_break_N"] = 1e4
    stack.safety.output_on()
    stack.safety.set_drive(100, slew=1)
    peak = 0.0
    for _ in range(60):
        advance(stack, 1)
        peak = max(peak, abs(stack.world.F))
    assert peak < 9.0
    assert abs(stack.world.F) < 6.0


def test_stop_all_is_not_overwritten_by_a_tick_in_flight(stack):
    stack.cfg["limits"]["force_max_N"] = 5.0
    stack.world.p["F_break_N"] = 1e4
    stack.safety.output_on()
    stack.safety.set_voltages(40, -20)
    advance(stack, 5)
    sup = stack.supervisor
    epoch = stack.safety.epoch
    s = sup._read()
    sup._update_safety(s)
    stack.stop_all()                                  # Stop zwischen Lesen und Guard
    sup._guard(s, epoch)
    assert stack.safety.setpoints() == {"tension": 0.0, "compression": 0.0}


def test_python_session_stays_stopped_until_acknowledged(stack):
    from core.runner import Busy
    stack.runner.step({"op": "output", "on": True})
    stack.stop_all()
    with pytest.raises(Busy, match="Acknowledge"):
        stack.runner.step({"op": "set_drive", "D": 20})
    stack.runner.step({"op": "zero_drive"})           # sicher bleibt erlaubt
    stack.ack()
    stack.runner.step({"op": "set_drive", "D": 5})


def test_guard_abort_stops_python_session_too(stack):
    from core.runner import Busy
    stack.runner.step({"op": "output", "on": True})
    stack.world.inject_arc("tension", 4e-3)
    advance(stack, 3)
    stack.world.clear_faults()
    with pytest.raises(Busy):
        stack.runner.step({"op": "output", "on": True})


def test_read_only_stop_says_it_did_nothing(stack):
    stack.safety.output_on()
    stack.safety.set_voltages(50, 0)
    stack.safety.read_only = True
    r = stack.stop_all()
    assert not r["ok"] and "read-only" in r["detail"]


def test_latch_survives_restart(cfg):
    from core import stack as sm
    from core.clock import VirtualClock
    from instruments import sim

    def real_like():
        clock = VirtualClock()
        inst, w = sim.build_sim(clock, cfg.get("sim", {}), noise=False)
        st = sm.Stack(cfg, inst, clock, False, world=w)
        st.supervisor.tick()
        return st
    a = real_like()
    a.safety.latch("arc or short suspected")
    b = real_like()
    with pytest.raises(LimitError):
        b.safety.output_on()
    b.ack()
    b.safety.output_on()


def test_intermittent_arc_trips(stack):
    stack.safety.output_on()
    for i in range(30):
        stack.world.inject_arc("tension", 4e-3 if i % 2 == 0 else 0.0)
        advance(stack, 1)
    assert "arc" in stack.supervisor.alarms.active


def test_output_dropping_by_itself_is_noticed(stack):
    stack.safety.output_on()
    stack.world.on["compression"] = False
    advance(stack, 2)
    assert "output_lost" in stack.supervisor.alarms.active


def test_chamber_change_checks_actual_voltage(stack):
    free_force(stack)
    cool(stack)
    stack.safety.output_on()
    stack.safety.set_voltages(100, -100)
    advance(stack, 12)
    stack.safety.zero()                               # Soll 0, Ist noch hoch
    with pytest.raises(LimitError):
        stack.safety.set_chamber("pump_continuous")


def test_chamber_command_narrows_window_at_once(stack):
    free_force(stack)
    cool(stack)
    stack.safety.output_on()
    stack.safety.set_chamber("purge_seal")
    with pytest.raises(LimitError):
        stack.safety.set_voltages(100, -100)


def test_low_force_failure_is_detected(stack):
    stack.world.p["F_break_N"] = 2.0
    stack.safety.output_on()
    stack.safety.set_drive(10, slew=0.2)
    advance(stack, 40)
    assert stack.world.broken and "sample_break" in stack.supervisor.alarms.active


def test_no_false_break_when_drive_unloads_a_thermal_force(stack):
    stack.cfg["limits"]["force_max_N"] = 1e3
    stack.world.p["F_break_N"] = 1e3
    stack.world.p["thermal_mismatch_um"] = -40.0
    cool(stack, 100)
    stack.safety.output_on()
    stack.safety.set_voltages(50, -20, slew=10)
    advance(stack, 15)
    assert "sample_break" not in stack.supervisor.alarms.active


def test_loop_stops_when_force_no_longer_follows(stack):
    from core.interpreter import StepError
    run_steps(stack, {"op": "output", "on": True}, {"op": "tare"})
    stack.world.broken = True
    with pytest.raises(StepError):
        run_steps(stack, {"op": "goto_force", "F": 10, "timeout": 1800})
    assert "force_loop" in stack.supervisor.alarms.active
    assert abs(stack.safety.drive()) < 40


# ── Zweite Prüfrunde ─────────────────────────────────────────────────────

def test_upward_jump_may_stand(stack):
    run_steps(stack, {"op": "set_temperature", "T": 100, "rate": 10}, {"op": "wait_temperature", "hold": 2})
    orig = stack.inst.cryo.read
    stack.inst.cryo.read = lambda: {**orig(), "T": 200.0}
    advance(stack, 8)
    assert stack.safety.T == 200.0


def test_overforce_without_drive_cause_does_not_lock_out(stack):
    """Falscher Nullpunkt: F zeigt 268 N bei D = 0. Quittieren muss reichen, um zu handeln."""
    stack.cal.C_zero = 1.1 - 0.134
    advance(stack, 2)
    assert "overforce" in stack.supervisor.alarms.active
    stack.ack()
    advance(stack, 3)
    assert stack.runner.halted is None
    stack.runner.step({"op": "tare", "accept": True})
    advance(stack, 2)
    assert "overforce" not in stack.supervisor.alarms.active


def test_thermal_overforce_lets_you_warm_up(stack):
    stack.world.p["thermal_mismatch_um"] = 30.0
    stack.cfg["limits"]["force_max_N"] = 5.0
    stack.world.p["F_break_N"] = 1e4
    stack.runner.step({"op": "output", "on": True})
    run_steps(stack, {"op": "set_temperature", "T": 100, "rate": 10})
    advance(stack, 200)
    stack.ack()
    advance(stack, 2)
    stack.runner.step({"op": "set_temperature", "T": 300, "rate": 5})


def test_user_can_drive_against_thermal_force(stack):
    stack.world.p["thermal_mismatch_um"] = 30.0
    stack.cfg["limits"]["force_max_N"] = 5.0
    stack.world.p["F_break_N"] = 1e4
    stack.safety.output_on()
    run_steps(stack, {"op": "set_temperature", "T": 150, "rate": 10})
    advance(stack, 200)
    stack.ack()
    advance(stack, 2)
    F0 = stack.world.F
    assert F0 > 5.0
    stack.safety.set_drive(-40, slew=1)
    advance(stack, 25)
    assert stack.world.F < F0 - 2.0


def test_stop_all_under_overforce_really_ends_at_zero(stack):
    stack.cfg["limits"]["force_max_N"] = 5.0
    stack.world.p["F_break_N"] = 1e4
    stack.safety.output_on()
    stack.safety.set_voltages(15, -5, slew=10)
    advance(stack, 1)
    stack.stop_all()
    advance(stack, 30)
    assert stack.safety.setpoints() == {"tension": 0.0, "compression": 0.0}
    assert abs(stack.world.v["tension"]) < 0.01


def test_zero_drive_under_overforce_is_not_overwritten(stack):
    stack.cfg["limits"]["force_max_N"] = 5.0
    stack.world.p["F_break_N"] = 1e4
    stack.safety.output_on()
    stack.safety.set_voltages(15, -5, slew=10)
    advance(stack, 2)
    stack.safety.zero(slew=1)
    advance(stack, 25)
    assert stack.safety.setpoints() == {"tension": 0.0, "compression": 0.0}


def test_tick_started_before_the_write_is_dropped_by_second_epoch(stack):
    stack.safety.output_on()
    sup = stack.supervisor
    calls = []
    orig = stack.safety.guard_set

    def spy(*a, **k):
        calls.append(stack.safety.epoch)
        return orig(*a, **k)
    stack.safety.guard_set = spy
    e0 = stack.safety.epoch
    stack.stop_all()
    assert stack.safety.epoch == e0 + 2


def test_loop_step_in_flight_is_dropped(stack):
    stack.safety.output_on()
    e = stack.safety.epoch
    stack.safety.bump_epoch()
    stack.safety.set_drive(20, epoch=e)
    assert stack.safety.drive() == 0.0


def test_halted_safety_refuses_a_step_past_the_check(stack):
    stack.safety.output_on()
    stack.runner.stop("x")
    with pytest.raises(LimitError):
        stack.safety.set_drive(10)


def test_failed_slew_does_not_prevent_stop(stack):
    stack.safety.output_on()
    stack.safety.set_voltages(50, 0)
    advance(stack, 8)
    stack.inst.supply.set_slew = lambda c, v: (_ for _ in ()).throw(IOError("lost reply"))
    assert stack.stop_all()["ok"]
    assert stack.inst.supply.voltage_setpoint("tension") == 0.0


def test_single_lost_write_is_retried(stack):
    stack.safety.output_on()
    real = stack.inst.supply.set_voltage
    n = {"k": 0}

    def flaky(c, v):
        n["k"] += 1
        if n["k"] == 1:
            raise IOError("lost")
        real(c, v)
    stack.inst.supply.set_voltage = flaky
    assert stack.stop_all()["ok"]


def test_gap_in_force_is_not_a_break(stack):
    stack.world.p["F_break_N"] = 1e4
    stack.cfg["limits"]["force_max_N"] = 1e3
    stack.safety.output_on()
    stack.safety.set_drive(20, slew=1)
    advance(stack, 15)
    stack.safety.set_drive(0, slew=1)
    stack.world.fault_cap_dead = True
    advance(stack, 8)
    stack.world.fault_cap_dead = False
    advance(stack, 4)
    assert "sample_break" not in stack.supervisor.alarms.active


@pytest.mark.parametrize("sigma", [0.1, 0.2, 0.4])
def test_noise_does_not_fake_a_break(cfg, sigma):
    from core import stack as sm
    cfg["sim"] = {"C_noise_fF": sigma * 0.5}
    cfg["limits"]["slew_default_V_s"] = 1.0
    st = sm.build(cfg, virtual=True, noise=True, seed=3)
    st.init_cap()
    st.supervisor.tick()
    st.safety.output_on()
    st.safety.set_drive(4)
    advance(st, 3600)
    assert "sample_break" not in st.supervisor.alarms.active


def test_output_on_racing_the_read_is_no_loss(stack):
    sup = stack.supervisor
    s = sup._read()                       # gelesen, als die Ausgänge noch aus waren
    stack.safety.output_on()
    sup._update_safety(s)
    sup._guard(s, stack.safety.epoch)
    assert "output_lost" not in sup.alarms.active


def test_restart_with_outputs_on_notices_a_later_loss(stack):
    stack.world.on["tension"] = stack.world.on["compression"] = True
    stack.safety.outputs_seen = False
    stack.safety.expect_on = False
    advance(stack, 1)
    assert stack.safety.expect_on
    stack.world.on["tension"] = False
    advance(stack, 1)
    assert "output_lost" in stack.supervisor.alarms.active


def test_on_right_after_off(stack):
    stack.safety.output_on()
    stack.safety.output_off()
    stack.safety.output_on()
    assert stack.world.on["tension"]


def test_second_supply_failure_holds_again(stack):
    free_force(stack)
    stack.safety.output_on()
    orig = stack.inst.supply.voltage_setpoint
    dead = lambda c: (_ for _ in ()).throw(IOError("usb"))
    stack.inst.supply.voltage_setpoint = dead
    advance(stack, 8)
    assert stack.supervisor._T_held
    stack.inst.supply.voltage_setpoint = orig
    advance(stack, 2)
    assert not stack.supervisor._T_held
    stack.inst.supply.voltage_setpoint = dead
    stack.inst.cryo.set_temperature(320, 5)
    advance(stack, 8)
    assert stack.world.T_set < 320


def test_arc_retries_a_failed_switch_off(stack):
    stack.safety.output_on()
    real = stack.inst.supply.set_output
    n = {"k": 0}

    def flaky(c, on):
        n["k"] += 1
        if n["k"] <= 2:
            raise IOError("lost")
        real(c, on)
    stack.inst.supply.set_output = flaky
    stack.world.inject_arc("tension", 4e-3)
    advance(stack, 5)
    assert not stack.world.on["tension"]


def test_exception_in_update_keeps_guard_alive(stack):
    orig = stack.cal.force_from_C
    stack.cal.force_from_C = lambda *a: 1 / 0
    advance(stack, 2)
    assert "guard_error" in stack.supervisor.alarms.active
    stack.cal.force_from_C = orig


def test_alpha_zero_rejected():
    from core.calibration import ForceCalibration
    with pytest.raises(ValueError):
        ForceCalibration({"force": {"S_pF_N": 1e-3, "C_zero_pF": 1, "alpha_table": [[0, 0.0], [400, 1.0]]}})


def test_latch_file_not_shared_with_simulation(cfg):
    from core import stack as sm
    p = cfg["paths"]["log_dir"] / "latched.json"
    a = sm.build(cfg, virtual=True, noise=False)
    a.safety.latch("x")
    assert not p.exists()


def test_long_gap_does_not_legalise_a_low_reading(stack):
    orig = stack.inst.cryo.read
    stack.inst.cryo.read = lambda: {**orig(), "T": 5.0}
    advance(stack, 1500)
    assert stack.safety.window() == (-20.0, 120.0)
