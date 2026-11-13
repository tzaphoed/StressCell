"""Feste Messfunktionen."""

import pytest

from core import tasks

from .test_server_api import api  # noqa: F401  (Fixture)


def test_definitions_cover_all_tasks():
    d = tasks.definitions()
    assert set(d) == {"stress_hold", "stress_sweep", "frequency_sweep", "temperature_ramp"}
    assert all(p["min"] <= p["default"] <= p["max"] for t in d.values() for p in t["params"])


def test_stress_sweep_levels_back_and_area():
    seq = tasks.build("stress_sweep", {"from_MPa": 0, "to_MPa": -25, "step_MPa": 10, "back": 1}, area_mm2=0.1)
    F = [s["F"] for s in seq["steps"] if s["op"] == "goto_force"]
    assert F == [0.0, -1.0, -2.0, -2.5, -2.0, -1.0, 0.0]          # σ · A, Endwert eingeschlossen, zurück
    labels = [s["label"] for s in seq["steps"] if s["op"] == "measure"]
    assert labels[0] == "hin 0 MPa" and labels[-1] == "zurück 0 MPa"


def test_parameter_limits():
    with pytest.raises(tasks.TaskError, match="outside"):
        tasks.build("stress_sweep", {"step_MPa": 0}, 0.1)
    with pytest.raises(tasks.TaskError, match="number"):
        tasks.build("frequency_sweep", {"V": "viel"}, 0.1)
    with pytest.raises(tasks.TaskError, match="unknown"):
        tasks.build("anything", {}, 0.1)
    with pytest.raises(tasks.TaskError, match="steps"):
        tasks.build("stress_sweep", {"from_MPa": -2000, "to_MPa": 2000, "step_MPa": 0.01}, 0.1)


@pytest.mark.parametrize("name,params", [
    ("stress_hold", {"sigma_MPa": -20, "duration_s": 60, "dt_s": 5}),
    ("stress_sweep", {"from_MPa": 0, "to_MPa": -60, "step_MPa": 20, "hold_s": 5, "n": 2, "dt_s": 1}),
    ("frequency_sweep", {"f_from_Hz": 10, "f_to_Hz": 100, "points": 4, "settle_s": 2, "n": 1}),
    ("temperature_ramp", {"T_to_K": 295, "rate_K_min": 2, "dt_s": 10}),
])
def test_every_task_passes_the_dry_run(stack, name, params):
    from .conftest import run_steps
    run_steps(stack, {"op": "output", "on": True}, {"op": "tare"})
    rep = stack.dry_run(tasks.build(name, params, stack.cfg["sample"]["area_mm2"]))
    assert rep["ok"], rep["error"]
    assert rep["points"] >= 1


def test_task_api(api):
    assert "stress_sweep" in api.get("/tasks").json()["tasks"]
    api.post("/step", json={"op": "output", "on": True})
    api.post("/step", json={"op": "tare"})
    r = api.post("/task_check/stress_sweep", json={"params": {"from_MPa": 0, "to_MPa": -20, "step_MPa": 10,
                                                              "hold_s": 1, "n": 1, "dt_s": 1}})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert api.post("/task/stress_sweep", json={"params": {"step_MPa": -1}}).status_code == 400
    r = api.post("/task/frequency_sweep", json={"params": {"f_from_Hz": 10, "f_to_Hz": 20, "points": 2,
                                                           "settle_s": 0, "n": 1}})
    assert r.status_code == 200, r.text
    api.post("/run/stop")
