"""Kalibrierung nach Razorbill-Blatt (FC100 #0260) und das lineare Modell."""

import json
import math
from pathlib import Path

import pytest

from core.calibration import ForceCalibration, RazorbillCalibration

CAL = Path(__file__).resolve().parents[2] / "calibrations" / "FC100_0260.toml"


@pytest.fixture
def cal(tmp_path):
    p = tmp_path / "FC100_0260.toml"
    p.write_bytes(CAL.read_bytes())
    return ForceCalibration.load(p)


def test_loads_razorbill_model(cal):
    assert isinstance(cal, RazorbillCalibration)
    assert not cal.verified                      # vom Foto abgeschrieben: erst prüfen


def test_sheet_values_at_290K(cal):
    # Gegen die Kurve auf dem Blatt: -200 N bei rund 1,54 pF, +200 N bei rund 1,14 pF
    assert cal.C_from_force(-200.0, 290.0) == pytest.approx(1.5405, abs=2e-3)
    assert cal.C_from_force(200.0, 290.0) == pytest.approx(1.1437, abs=2e-3)
    assert cal.kappa(290.0) == pytest.approx(1.0, abs=1e-3)


def test_df0_polynomial_matches_plot(cal):
    # Abgelesen aus "Change in f0": 100 K um 102 N, 200 K um 79 N, 290 K um 36 N
    for T, v in ((100, 102.3), (150, 93.2), (200, 79.0), (250, 58.2), (290, 35.8)):
        assert cal.df0(T) == pytest.approx(v, abs=0.3)


def test_tension_lowers_capacitance(cal):
    assert cal.sensitivity_pF_N(0.0, 290.0) < 0
    assert cal.sensitivity_pF_N(0.0, 290.0) == pytest.approx(-0.97e-3, rel=0.02)


@pytest.mark.parametrize("T", [2.0, 50.0, 150.0, 300.0])
@pytest.mark.parametrize("F", [-150.0, -3.0, 0.0, 7.5, 120.0])
def test_round_trip(cal, T, F):
    assert cal.force_from_C(cal.C_from_force(F, T), T) == pytest.approx(F, abs=1e-9)


def test_tare_in_vacuum_then_cooling_keeps_zero(cal):
    # Im Vakuum liegt der Nullpunkt um die Feuchte daneben. Tare bei 295 K, dann folgt die
    # Kapazität ohne Last der Temperaturkurve vom Blatt: die Kraft bleibt null.
    offset = 34.0
    true = RazorbillCalibration.__new__(RazorbillCalibration)
    true.__dict__.update(cal.__dict__)
    true.f0_ref = cal.f0_sheet + offset
    C_room = true.C_from_force(0.0, 295.0)
    assert cal.force_from_C(C_room, 295.0) == pytest.approx(offset * cal.kappa(295.0), rel=1e-3)
    cal.tare(C_room, 295.0, persist=False)
    for T in (250.0, 100.0, 10.0, 2.0):
        assert cal.force_from_C(true.C_from_force(0.0, T), T) == pytest.approx(0.0, abs=1e-6)
        assert cal.force_from_C(true.C_from_force(25.0, T), T) == pytest.approx(25.0, abs=1e-6)


def test_kappa_scales_force_at_low_T(cal):
    cal.tare(cal.C_from_force(0.0, 290.0), 290.0, persist=False)
    # Gleiche kleine Kapazitätsänderung ergibt bei 4 K mehr Kraft: kappa = 1,10 und f0 steigt,
    # die Empfindlichkeit fällt von 0,97 auf 0,79 fF/N.
    dC = -1e-4
    F290 = cal.force_from_C(cal.C_from_force(0.0, 290.0) + dC, 290.0)
    F4 = cal.force_from_C(cal.C_from_force(0.0, 4.0) + dC, 4.0)
    assert F4 / F290 == pytest.approx(cal.sensitivity_pF_N(0, 290.0) / cal.sensitivity_pF_N(0, 4.0), rel=1e-3)
    assert F4 / F290 == pytest.approx(1.23, abs=0.01)


def test_tare_persists_and_reloads(cal):
    C = cal.C_from_force(-12.0, 280.0)
    cal.tare(C, 280.0)
    d = json.loads(cal._tare_file.read_text())
    assert d["model"] == "razorbill"
    again = ForceCalibration.load(cal.path)
    assert again.tared and again.force_from_C(C, 280.0) == pytest.approx(0.0, abs=1e-9)


def test_bad_readings_give_no_force(cal):
    assert cal.force_from_C(None, 300.0) is None
    assert cal.force_from_C(1.2, None) is None
    assert cal.force_from_C(cal.Cp, 300.0) is None          # Division durch null
    assert cal.force_from_C(float("nan"), 300.0) is None


def test_polynomial_clamped_outside_range(cal):
    assert cal.df0(500.0) == cal.df0(305.0)
    assert math.isfinite(cal.force_from_C(1.3, 400.0))


def test_invalid_files_rejected(tmp_path):
    p = tmp_path / "x.toml"
    p.write_text('[force]\nmodel = "razorbill"\nalpha_NpF = -1\nf0_N = 1\nCp_pF = 0\n')
    with pytest.raises(ValueError):
        ForceCalibration.load(p)
    p.write_text('[force]\nmodel = "magic"\n')
    with pytest.raises(ValueError):
        ForceCalibration.load(p)


def test_simulation_answers_like_the_sheet(cfg):
    from core import stack as sm
    cfg["capacitance"]["calibration"] = "FC100_0260.toml"
    st = sm.build(cfg, virtual=True, noise=False)
    st.init_cap()
    s = st.supervisor.tick()
    assert s["C_pF"] == pytest.approx(st.cal.C_from_force(0.0, s["T"]), abs=1e-6)
    assert s["F"] == pytest.approx(0.0, abs=1e-6)
    assert st.cal.info()["model"] == "razorbill"
