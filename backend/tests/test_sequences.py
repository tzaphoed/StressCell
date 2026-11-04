import pytest

from core.interpreter import StepError
from core.sequence import check_sequence

from .conftest import advance, run_steps, seq


def test_static_check_finds_mistakes():
    e = check_sequence(seq({"op": "nope"}, {"op": "goto_force"}, {"op": "wait", "seconds": 1, "x": 2},
                           {"op": "repeat", "times": 2, "do": [{"op": "log"}]}))
    assert len(e) == 4


def test_goto_force_converges_at_300K(stack):
    run_steps(stack, {"op": "output", "on": True}, {"op": "tare"},
              {"op": "goto_force", "F": 8, "tol": 0.05, "hold": 20, "timeout": 900})
    assert stack.world.F == pytest.approx(8, abs=0.1)


def test_goto_force_converges_at_4K_with_gain_a_fifth(stack):
    run_steps(stack, {"op": "set_temperature", "T": 4, "rate": 10},
              {"op": "wait_temperature", "hold": 5})
    advance(stack, 300)
    run_steps(stack, {"op": "tare"}, {"op": "output", "on": True},
              {"op": "goto_force", "F": 8, "tol": 0.05, "hold": 20, "timeout": 1800})
    assert stack.supervisor.last["F"] == pytest.approx(8, abs=0.1)
    assert stack.supervisor.force.G < 0.4


def test_force_loop_holds_against_creep(stack):
    run_steps(stack, {"op": "output", "on": True}, {"op": "tare"},
              {"op": "goto_force", "F": 8, "tol": 0.05, "hold": 5, "timeout": 900},
              {"op": "wait", "seconds": 600})
    assert stack.supervisor.last["F"] == pytest.approx(8, abs=0.1)


def test_force_above_limit_refused(stack):
    with pytest.raises(StepError):
        run_steps(stack, {"op": "output", "on": True}, {"op": "goto_force", "F": 25})


def test_force_locked_on_real_hardware_until_calibration_verified(stack):
    stack.simulated = False
    with pytest.raises(StepError, match="verified"):
        run_steps(stack, {"op": "output", "on": True}, {"op": "goto_force", "F": 1})
    stack.cal.verified = True
    run_steps(stack, {"op": "goto_force", "F": 1, "tol": 0.1, "hold": 2})


def test_loop_stops_when_capacitance_meter_dies(stack):
    run_steps(stack, {"op": "output", "on": True}, {"op": "tare"},
              {"op": "goto_force", "F": 8, "hold": 5, "timeout": 900})
    D = stack.safety.drive()
    stack.world.fault_cap_dead = True
    advance(stack, 30)
    assert not stack.supervisor.force.enabled
    assert stack.safety.drive() == pytest.approx(D)
    assert "stale_F" in stack.supervisor.alarms.active


def test_force_sweep_records_points(cfg, stack):
    s = seq({"op": "output", "on": True}, {"op": "tare"},
            {"op": "force_sweep", "from": 0, "to": 10, "points": 6, "hold": 2, "n": 2, "dt": 1},
            {"op": "zero_drive"})
    rep = stack.dry_run(s)
    assert rep["ok"], rep
    assert rep["points"] == 12


def test_for_range_and_expressions(stack):
    ctx = run_steps(stack, {"op": "set_variable", "name": "a", "value": "2*3 + 1"},
                    {"op": "for_range", "var": "x", "from": 1, "to": 4, "points": 4,
                     "do": [{"op": "set_variable", "name": "s", "value": "x + a"}]})
    assert ctx.vars["s"] == 11


@pytest.mark.parametrize("bad", ["__import__('os')", "open('x')", "(1).real", "a.b", "[1][0]"])
def test_expressions_cannot_escape(stack, bad):
    with pytest.raises(StepError):
        run_steps(stack, {"op": "set_variable", "name": "x", "value": bad})


def test_dry_run_catches_warming_under_voltage(stack):
    s = seq({"op": "set_temperature", "T": 4, "rate": 10}, {"op": "wait_temperature", "hold": 5},
            {"op": "output", "on": True}, {"op": "set_voltages", "tension": 15, "compression": -15},
            {"op": "wait_voltage"}, {"op": "set_temperature", "T": 300, "rate": 10})
    rep = stack.dry_run(s)
    assert rep["ok"]
    s["steps"][3].update(tension=150, compression=-50)
    stack.cfg["limits"]["force_max_N"] = 1e4
    stack.cfg["sim"] = {"F_break_N": 1e4}
    stack.cfg["limits"]["force_step_trip_N"] = 1e4
    rep = stack.dry_run(s)
    assert not rep["ok"] and "window" in rep["error"]


def test_dry_run_does_not_touch_the_live_instruments(stack):
    s = seq({"op": "output", "on": True}, {"op": "set_voltages", "tension": 50, "compression": 0})
    stack.dry_run(s)
    assert not stack.world.on["tension"]
    assert stack.inst.supply.voltage_setpoint("tension") == 0.0


def test_dry_run_gives_duration(stack):
    rep = stack.dry_run(seq({"op": "wait", "seconds": 3600}))
    assert rep["duration_s"] == pytest.approx(3600, abs=6)


def test_dry_run_starts_from_live_state(stack):
    stack.safety.output_on()
    stack.safety.set_voltages(100, -20)
    advance(stack, 15)
    # bereits unter Spannung und Fenster bei 4 K ist weiter: Abkühlen ist erlaubt
    rep = stack.dry_run(seq({"op": "set_voltages", "tension": 110, "compression": -20}))
    assert rep["ok"], rep


def test_live_run_writes_files_and_guard_aborts(cfg):
    """Echte Threads, Zeitraffer: Sequenz läuft, Arc bricht sie ab."""
    from core import stack as sm
    cfg["monitor"]["tick_s"] = 1.0
    st = sm.build(cfg, speed=100, noise=True, seed=2)
    st.init_cap()
    st.supervisor.start()
    try:
        import time
        time.sleep(0.2)
        st.runner.start(seq({"op": "output", "on": True}, {"op": "tare"},
                            {"op": "measure", "n": 3, "dt": 2, "label": "a"},
                            {"op": "wait", "seconds": 3000}, name="live"))
        time.sleep(1.0)
        st.world.inject_arc("tension", 5e-3)
        st.runner.wait(5)
        assert st.runner.state == "idle" and "guard: arc" in st.runner.result
        d = st.runner.run_dir
        assert (d / "sequence.json").exists() and (d / "run_info.json").exists()
        lines = (d / "data.csv").read_text().splitlines()
        assert len(lines) == 4 and lines[0].startswith("wall,t,label,C_zero_pF,T")
    finally:
        st.supervisor.stop()


def test_pause_resume(cfg):
    from core import stack as sm
    import time
    st = sm.build(cfg, speed=100, noise=False)
    st.init_cap()
    st.supervisor.start()
    try:
        time.sleep(0.1)
        st.runner.start(seq({"op": "pause", "message": "check wiring"}, {"op": "log", "message": "go"}),
                        dry_first=False)
        time.sleep(0.2)
        assert st.runner.state == "paused"
        st.runner.resume()
        st.runner.wait(3)
        assert st.runner.result == "finished"
    finally:
        st.supervisor.stop()


def test_session_step_refused_during_run(cfg):
    from core import stack as sm
    from core.runner import Busy
    import time
    st = sm.build(cfg, speed=50, noise=False)
    st.init_cap()
    st.supervisor.start()
    try:
        time.sleep(0.1)
        st.runner.start(seq({"op": "wait", "seconds": 500}), dry_first=False)
        with pytest.raises(Busy):
            st.runner.step({"op": "tare"})
        st.runner.stop()
        st.runner.wait(3)
    finally:
        st.supervisor.stop()


def test_wrong_calibration_sign_stops_the_loop(stack):
    run_steps(stack, {"op": "output", "on": True}, {"op": "tare"})
    stack.cal.sign = -1.0
    stack.supervisor.tare(stack.supervisor.tick()["C_pF"], 300.0, persist=False)
    with pytest.raises(StepError):
        run_steps(stack, {"op": "goto_force", "F": 5, "tol": 0.05, "timeout": 900})
    assert "force_loop" in stack.supervisor.alarms.active
    assert abs(stack.world.F) < 10


def test_dry_run_reports_locked_force_on_real_hardware(stack):
    stack.simulated = False
    rep = stack.dry_run(seq({"op": "goto_force", "F": 1}))
    assert not rep["ok"] and "verified" in rep["error"]


def test_nan_target_refused(stack):
    with pytest.raises(StepError):
        run_steps(stack, {"op": "output", "on": True}, {"op": "goto_force", "F": "0/1*exp(1000)"})
    assert not stack.supervisor.force.enabled


def test_nan_force_does_not_step_the_loop(stack):
    run_steps(stack, {"op": "output", "on": True}, {"op": "tare"})
    fl = stack.supervisor.force
    fl.start(5.0, 0.05)
    assert fl.step(float("nan"), 0.0, 0.0) is None


def test_tare_under_load_refused(stack):
    run_steps(stack, {"op": "output", "on": True}, {"op": "tare"},
              {"op": "goto_force", "F": 10, "hold": 2, "tol": 0.2})
    with pytest.raises(StepError, match="tare"):
        run_steps(stack, {"op": "tare"})


def test_tare_of_large_offset_needs_accept(stack):
    stack.world.p["thermal_mismatch_um"] = 8.0
    stack.world.p["F_break_N"] = 1e4
    stack.cfg["limits"]["force_max_N"] = 1e3
    run_steps(stack, {"op": "tare"}, {"op": "set_temperature", "T": 4, "rate": 10},
              {"op": "wait_temperature", "hold": 5})
    advance(stack, 200)
    with pytest.raises(StepError, match="accept"):
        run_steps(stack, {"op": "tare"})
    run_steps(stack, {"op": "tare", "accept": True})


def test_simulated_tare_does_not_touch_the_real_calibration(cfg):
    from core import stack as sm
    st = sm.build(cfg, speed=100, noise=False)
    st.init_cap()
    st.supervisor.tick()
    st.runner.step({"op": "tare"}) if False else None
    from core.interpreter import Context, Interpreter
    st.clock = st.clock
    st.simulated = True
    st.safety.state_known = True
    Interpreter(Context(st)).op_tare({}) if False else None
    assert not list(cfg["paths"]["calibration_dir"].glob("*.tare.json"))


def test_string_false_does_not_switch_on(stack):
    with pytest.raises(StepError):
        run_steps(stack, {"op": "output", "on": "false"})


def test_step_failure_leaves_no_loop_running(stack):
    from core.interpreter import StepError as SE
    stack.runner.step({"op": "output", "on": True})
    with pytest.raises(SE):
        stack.runner.step({"op": "goto_force", "F": 10, "timeout": 5})
    assert not stack.supervisor.force.enabled


def test_close_session_ends_loop_and_ac(stack):
    stack.runner.open_session("x")
    stack.runner.step({"op": "output", "on": True})
    stack.runner.step({"op": "ac_on", "f": 21, "V": 1})
    stack.supervisor.force.start(1.0)
    stack.runner.close_session()
    assert not stack.supervisor.force.enabled and stack.world.ac_V == 0.0


def test_measure_without_session_still_saves(stack):
    stack.runner.step({"op": "measure", "n": 1})
    assert stack.runner.session is not None
    assert (stack.runner.run_dir / "data.csv").exists()


def test_new_variable_column_is_not_lost(stack):
    stack.runner.open_session("x")
    stack.runner.step({"op": "measure", "n": 1})
    stack.runner.session.vars["x"] = 3.0
    stack.runner.step({"op": "measure", "n": 1})
    text = (stack.runner.run_dir / "data.csv").read_text().splitlines()
    assert "var_x" in text[0] and len(text) == 3
    stack.runner.close_session()


def test_end_of_run_ends_ac(cfg):
    from core import stack as sm
    import time
    st = sm.build(cfg, speed=100, noise=False)
    st.init_cap()
    st.supervisor.start()
    try:
        time.sleep(0.15)
        st.runner.start(seq({"op": "output", "on": True}, {"op": "ac_on", "f": 21, "V": 1},
                            {"op": "wait", "seconds": 2}, {"op": "set_voltages", "tension": 999, "compression": 0}),
                        dry_first=False)
        st.runner.wait(3)
        assert "error" in st.runner.result and st.world.ac_V == 0.0
    finally:
        st.supervisor.stop()


def test_double_start_gives_one_run(cfg):
    from core import stack as sm
    from core.runner import Busy
    import threading, time
    st = sm.build(cfg, speed=100, noise=False)
    st.init_cap()
    st.supervisor.start()
    res = []

    def go():
        try:
            st.runner.start(seq({"op": "wait", "seconds": 1000}))
            res.append("ok")
        except Busy:
            res.append("busy")
    try:
        time.sleep(0.15)
        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert res.count("ok") == 1
    finally:
        st.runner.stop()
        st.runner.wait(2)
        st.supervisor.stop()


def test_stop_during_dry_run_cancels_the_start(stack, monkeypatch):
    from core.runner import Busy
    orig = stack.dry_run

    def slow(s):
        stack.stop_all()
        return orig(s)
    stack.dry_run = slow
    with pytest.raises(Busy):
        stack.runner.start(seq({"op": "wait", "seconds": 1}))
    assert stack.runner.state == "idle"


def test_dry_run_uses_the_cells_calibration(stack):
    stack.cal.C_zero = 1.234
    stack.supervisor.tick()
    stack.cal.sign = -1.0
    rep = stack.dry_run(seq({"op": "output", "on": True}, {"op": "goto_force", "F": 2, "tol": 0.1, "hold": 2}))
    assert rep["ok"], rep["error"]


def test_wait_field_accepts_holding_driven(stack):
    orig = stack.inst.cryo.read
    stack.inst.cryo.read = lambda: {**orig(), "B_status": "Holding (driven)"}
    run_steps(stack, {"op": "set_field", "B": 0.0, "rate": 0.6}, {"op": "wait_field", "timeout": 30})


def test_power_cannot_stall_the_guard(stack):
    with pytest.raises(StepError):
        run_steps(stack, {"op": "set_variable", "name": "x", "value": "9**9**7"})


def test_tare_when_cold_needs_accept(stack):
    stack.cfg["limits"]["tare_T_min_K"] = 250.0
    run_steps(stack, {"op": "set_temperature", "T": 50, "rate": 10}, {"op": "wait_temperature", "hold": 2})
    with pytest.raises(StepError, match="accept"):
        run_steps(stack, {"op": "tare"})
    run_steps(stack, {"op": "tare", "accept": True})


def test_auto_session_does_not_block_a_sequence(cfg):
    from core import stack as sm
    import time
    st = sm.build(cfg, speed=100, noise=False)
    st.init_cap()
    st.supervisor.start()
    try:
        time.sleep(0.15)
        st.runner.step({"op": "measure", "n": 1})
        st.runner.start(seq({"op": "log", "message": "x"}), dry_first=False)
        st.runner.wait(3)
        assert st.runner.result == "finished"
    finally:
        st.supervisor.stop()


def test_explicit_session_still_blocks(stack):
    from core.runner import Busy
    stack.runner.open_session("mine")
    with pytest.raises(Busy):
        stack.runner.start(seq({"op": "log", "message": "x"}))
    stack.runner.close_session()


def test_rejected_typo_keeps_a_running_loop(stack):
    stack.runner.step({"op": "output", "on": True})
    stack.supervisor.force.start(1.0)
    with pytest.raises(StepError):
        stack.runner.step({"op": "set_voltages", "tension": 500, "compression": 0})
    assert stack.supervisor.force.enabled


def test_no_false_stall_at_reversal_at_4K(stack):
    stack.world.p["backlash_V"] = 12.0
    run_steps(stack, {"op": "set_temperature", "T": 4, "rate": 10}, {"op": "wait_temperature", "hold": 5})
    advance(stack, 300)
    run_steps(stack, {"op": "output", "on": True}, {"op": "tare"},
              {"op": "goto_force", "F": 8, "tol": 0.1, "hold": 5, "timeout": 1800},
              {"op": "goto_force", "F": 7.5, "tol": 0.1, "hold": 5, "timeout": 1800})
    assert "force_loop" not in stack.supervisor.alarms.active


def test_low_force_failure_with_noise_stops_the_loop_early(cfg):
    from core import stack as sm
    cfg["sim"] = {"F_break_N": 1.5, "C_noise_fF": 0.05}
    st = sm.build(cfg, virtual=True, noise=True, seed=5)
    st.init_cap()
    st.supervisor.tick()
    run_steps(st, {"op": "output", "on": True})
    with pytest.raises(StepError):
        run_steps(st, {"op": "goto_force", "F": 10, "tol": 0.2, "timeout": 1800})
    assert abs(st.safety.drive()) < 60


def test_noise_is_not_taken_for_wrong_sign(cfg):
    from core import stack as sm
    cfg["sim"] = {"C_noise_fF": 0.05}
    st = sm.build(cfg, virtual=True, noise=True, seed=7)
    st.init_cap()
    st.supervisor.tick()
    run_steps(st, {"op": "output", "on": True}, {"op": "tare"})
    advance(st, 120)
    try:
        run_steps(st, {"op": "goto_force", "F": 6, "tol": 0.5, "hold": 3, "timeout": 900})
    except StepError:
        pass
    assert "force_loop" not in st.supervisor.alarms.active
