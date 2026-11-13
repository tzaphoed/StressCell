"""Zweiter Lock-in für 2f, gleichzeitig mit 1f."""

import copy

import pytest

from instruments import lockins, real

from .conftest import advance, run_steps
from .test_split import Pool, split_cfg


class IO:
    def __init__(self, ans=None):
        self.log, self.ans = [], ans or {}

    def write(self, c):
        self.log.append(c)

    def query(self, c):
        self.log.append(c)
        return self.ans.get(c, "0")

    def close(self):
        pass


def test_detector_sr830_external_reference_and_harmonic():
    io = IO({"SNAP? 1,2,3,4": "1e-8,2e-8,2.24e-8,63", "FREQ?": "42"})
    d = lockins.Detector(io, "sr830")
    assert io.log[:2] == ["OUTX 1", "FMOD 0"]
    d.set_detection(2, 1.0)
    assert "HARM 2" in io.log
    assert d.read()["f"] == 42.0
    with pytest.raises(IOError):
        d.set_drive(21, 1.0)                     # misst nur, treibt nie


def test_build_real_with_second_lockin(cfg):
    c = split_cfg(cfg)
    c["instruments"]["lockin2"] = "GPIB0::5::INSTR"
    inst = real.build_real(c, Pool(), cryo=object())
    try:
        assert isinstance(inst.lockin2, lockins.Detector)
        assert ("lock-in 2 (2f)", "sr830", "GPIB0::5::INSTR") in real.configured_devices(c)
    finally:
        inst.supply.close()


def test_sim_reports_2f_next_to_1f(cfg):
    from core import stack as sm
    c = copy.deepcopy(cfg)
    c["instruments"]["lockin2"] = "GPIB0::5::INSTR"
    st = sm.build(c, virtual=True, noise=False, seed=1)
    st.init_cap()
    st.world.T_set, st.world.T = 302.0, 302.0      # neben dem Sim-Übergang bei 300 K: 2f ist nicht null
    st.world.T_cell = 302.0
    st.supervisor.tick()
    run_steps(st, {"op": "output", "on": True}, {"op": "ac_on", "f": 20, "V": 2.0})
    advance(st, 5)
    s = st.supervisor.last
    assert s["R"] > 0 and s["R2"] is not None and s["R2"] > 0
    assert s["T_2f_K"] is not None and s["T_2f_K"] < s["T_ac_K"]
    rec = st.supervisor.recorder
    rec.start("2f", 1.0)
    advance(st, 2)
    import csv
    rows = list(csv.DictReader(open(rec.stop()["path"], encoding="utf-8")))
    assert rows[-1]["T_2f_K"] != ""
