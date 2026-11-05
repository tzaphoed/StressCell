"""Zellthermometer (Cernox) und einfache Datenaufnahme."""

import copy
import csv
import math

import pytest

from core.thermometry import Curve
from instruments import thermometers as th

from .conftest import advance, run_steps  # noqa: F401
from .test_server_api import api  # noqa: F401  (Fixture)


# ── Kurven ───────────────────────────────────────────────────────────────
def cernox_like(T):
    """Grobe Cernox-Kennlinie: R steigt beim Abkühlen über Dekaden."""
    return 50.0 * math.exp(1.6 * (math.log(300.0) - math.log(T))) + 40.0


def test_340_log_format(tmp_path):
    p = tmp_path / "x.340"
    lines = ["Sensor Model:   CX-1050-AA", "Serial Number:  X12345", "Data Format:    4      (Log Ohms/Kelvin)",
             "", "No.   Units      Temperature (K)", ""]
    for i, T in enumerate((300, 200, 100, 50, 20, 10, 4, 2), 1):
        lines.append(f"{i:3d}  {math.log10(cernox_like(T)):.6f}  {T:.3f}")
    p.write_text("\n".join(lines))
    c = Curve.load(p)
    assert c.T(cernox_like(100.0)) == pytest.approx(100.0, rel=1e-6)
    assert c.T(cernox_like(30.0)) == pytest.approx(30.0, rel=0.02)
    assert c.T(cernox_like(1.0)) is None and c.T(10.0) is None      # nicht extrapolieren


def test_csv_format_and_validation(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("R_ohm,T_K\n" + "\n".join(f"{cernox_like(T)},{T}" for T in (2, 5, 20, 77, 300)))
    assert Curve.load(p).T(cernox_like(77)) == pytest.approx(77)
    with pytest.raises(ValueError):
        Curve([1, 2, 3], [1, 3, 2])                                   # nicht monoton


def test_lakeshore_commands():
    class IO:
        def __init__(self, ans):
            self.ans, self.log = ans, []

        def query(self, c):
            self.log.append(c)
            return self.ans[c]

    a = th.LakeShore(IO({"KRDG? B": "+4.2310", "SRDG? B": "+12345.6"}), "336", "B")
    assert a.read() == {"T_cell": 4.231, "R_cell": 12345.6}
    b = th.LakeShore(IO({"RDGK? 3": "+0.0000", "RDGR? 3": "+0.0000"}), "372", "3")
    assert b.read() == {"T_cell": None, "R_cell": None}             # Überlauf ist keine Temperatur


def test_resistance_thermometer_uses_curve():
    c = Curve([cernox_like(T) for T in (2, 10, 100, 300)], [2, 10, 100, 300])
    r = th.Resistance(lambda: cernox_like(10.0), c)
    assert r.read()["T_cell"] == pytest.approx(10.0)


# ── Sicherheit mit Zellthermometer ───────────────────────────────────────
def cell_stack(cfg, window="max", ppms=True):
    from core import stack as sm
    c = copy.deepcopy(cfg)
    c["cell_thermometer"].update(source="lakeshore", window=window, margin_K=1.0)
    c["mode"]["ppms_read"] = ppms
    st = sm.build(c, virtual=True, noise=False, seed=1)
    st.init_cap()
    st.supervisor.tick()
    return st


def test_window_uses_the_warmer_of_ppms_and_cell(cfg):
    st = cell_stack(cfg)
    sf = st.safety
    sf.T, sf.T_slow, sf.dTdt = 5.0, 5.0, 0.0
    sf.T_cell, sf.dTcdt = 60.0, 0.0                         # Zelle wärmer (gerade abgekühlt)
    assert sf.T_eff() == pytest.approx(61.0)
    sf.T_cell = 3.0                                         # PPMS wärmer (Aufwärmen)
    assert sf.T_eff() == pytest.approx(5.0 + cfg["limits"]["T_margin_K"])


def test_cernox_only_window_and_fallback(cfg):
    st = cell_stack(cfg, window="cernox")
    sf = st.safety
    sf.T, sf.T_slow = 50.0, 50.0
    sf.T_cell, sf.dTcdt = 4.0, 0.0
    assert sf.T_eff() == pytest.approx(5.0)
    sf.T_cell = None                                        # Cernox weg: PPMS springt ein
    assert sf.T_eff() == pytest.approx(50.0 + cfg["limits"]["T_margin_K"])


def test_cell_thermometer_replaces_ppms_when_ppms_is_off(cfg):
    st = cell_stack(cfg, window="cernox", ppms=False)
    st.world.T_set = 4.0
    st.world.T_rate = 10.0 / 60.0
    advance(st, 3000, tick=10.0)
    s = st.supervisor.last
    assert s["T"] is None and s["T_cell"] == pytest.approx(4.0, abs=0.5)
    # Das Cernox öffnet das Temperaturfenster (bei 300 K wäre vmin = -20 V). Die Kammer bleibt ohne
    # PPMS unbekannt, also gilt weiter die Grenze für Druckwechsel: ±50 V.
    assert (s["vmin"], s["vmax"]) == (-50.0, 50.0)
    assert s["T_sample"] == s["T_cell"]


def test_bad_cell_reading_is_ignored(cfg):
    st = cell_stack(cfg)
    st.inst.cell_thermo.read = lambda: {"T_cell": 0.0, "R_cell": None}
    s = st.supervisor.tick()
    assert s["T_cell"] is None and s["T_sample"] == s["T"]


def test_cell_thermometer_failure_falls_back_after_stale(cfg):
    st = cell_stack(cfg)
    advance(st, 5)
    assert st.safety.T_cell is not None
    st.inst.cell_thermo.fail = True
    advance(st, 30)
    assert st.safety.T_cell is None and st.supervisor.last["vmin"] is not None


def test_dry_run_has_cell_thermometer_too(cfg):
    from .conftest import seq
    st = cell_stack(cfg, window="cernox", ppms=False)
    rep = st.dry_run(seq({"op": "output", "on": True}, {"op": "set_drive", "D": 10}, {"op": "measure", "n": 2}))
    assert rep["ok"], rep["error"]


# ── Aufnahme ─────────────────────────────────────────────────────────────
def rows(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_recorder_writes_every_interval_with_notes(stack):
    rec = stack.supervisor.recorder
    d = rec.start("probe 1/x", interval_s=5.0)
    assert "rec_probe_1_x" in d
    advance(stack, 21)
    rec.note("Offset -5 N")
    advance(stack, 5)
    info = rec.stop()
    r = rows(info["path"])
    assert len(r) == info["rows"] >= 5
    assert {"T", "T_cell", "F", "C_pF", "V_T", "V_C", "theta_stack", "alarms"} <= set(r[0])
    assert any(x["note"] == "Offset -5 N" for x in r)
    assert float(r[1]["t_s"]) - float(r[0]["t_s"]) == pytest.approx(5.0, abs=1.01)
    advance(stack, 10)
    assert len(rows(info["path"])) == len(r)                # gestoppt heißt gestoppt


def test_recorder_runs_in_read_only_mode(stack):
    stack.safety.read_only = True
    rec = stack.supervisor.recorder
    rec.start("ro", 1.0)
    advance(stack, 3)
    assert rec.stop()["rows"] >= 3


def test_recorder_rejects_bad_interval(stack):
    with pytest.raises(ValueError):
        stack.supervisor.recorder.start("x", 0.0)


def test_record_api(api):
    r = api.post("/record/start", json={"name": "api", "interval_s": 0.5})
    assert r.status_code == 200
    assert api.get("/record").json()["active"]
    api.post("/record/note", json={"text": "hallo"})
    assert api.post("/record/start", json={"interval_s": -1}).status_code == 400
    assert api.post("/record/stop").status_code == 200
    assert not api.get("/record").json()["active"]


def test_cernox_fit_tool_averages_cooling_and_warming(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("cf", Path(__file__).resolve().parents[2] / "tools" / "cernox_fit.py")
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)
    rows = []
    Ts = [300 - i for i in range(0, 296)] + [4 + i for i in range(0, 296)]
    for k, T in enumerate(Ts):
        lag = 0.5 if k < 296 else -0.5               # Zelle hinkt nach: beim Abkühlen wärmer, beim Aufwärmen kälter
        rows.append({"T": T, "R_cell": cernox_like(T + lag)})
    R, T, warn = cf.fit(rows)
    c = Curve(R, T)
    for x in (10, 50, 200):
        assert c.T(cernox_like(x)) == pytest.approx(x, rel=0.02)


def test_settings_cell_choices(tmp_path):
    from core import settings
    p = tmp_path / "c.toml"
    p.write_text('[mode]\nsimulate = true\n\n[cell_thermometer]\nsource = "none"   # Kommentar\n')
    settings.write(p, {"cell_source": "lakeshore", "cell_window": "cernox"})
    t = p.read_text()
    assert 'source = "lakeshore"   # Kommentar' in t and 'window = "cernox"' in t
    with pytest.raises(ValueError):
        settings.write(p, {"cell_source": "thermos"})


def test_lakeshore_software_curve_and_curve_header(tmp_path):
    class IO:
        def __init__(self, ans):
            self.ans, self.log = ans, []

        def query(self, c):
            self.log.append(c)
            return self.ans[c]

    p = tmp_path / "x.340"
    p.write_text("Sensor Model:   CX-1050-SD\nSerial Number:  X98765\nData Format:    4\n\n" +
                 "\n".join(f"{i} {math.log10(cernox_like(T)):.6f} {T}" for i, T in enumerate((300, 100, 20, 4, 2), 1)))
    c = Curve.load(p)
    assert (c.serial, c.model) == ("X98765", "CX-1050-SD")
    io = IO({"SRDG? A": f"{cernox_like(20.0):.4f}", "INCRV? A": "21", "CRVHDR? 21": "CX-1050,X98765,4,325.0,1"})
    ls = th.LakeShore(io, "336", "A", c)
    assert ls.read()["T_cell"] == pytest.approx(20.0, rel=1e-4)
    assert "KRDG? A" not in io.log                          # Software-Kurve: Gerät nur nach R fragen
    assert ls.curve_info() == {"curve": 21, "name": "CX-1050", "serial": "X98765", "format": "4", "limit_K": "325.0"}
    io.ans["INCRV? A"] = "0"
    assert ls.curve_info()["curve"] == 0


def test_check_instruments_runs_through_with_fake_devices(cfg, tmp_path, monkeypatch, capsys):
    import check_instruments
    from instruments import real

    class Fake:
        def __init__(self, addr, **kw):
            self.addr = addr

        def write(self, c):
            pass

        def query(self, c):
            return {"*IDN?": "FAKE,1,2,3", "INCRV? A": "21", "CRVHDR? 21": "CX-1050,X98765,4,325.0,1",
                    "KRDG? A": "+295.10", "SRDG? A": "+62.5", ":FETCH?": "+1.30000E-12,+1.0E-04,+0"}.get(c, "0")

        def close(self):
            pass

    c = copy.deepcopy(cfg)
    c["cell_thermometer"].update(source="lakeshore", model="336", input="A", address="GPIB0::12::INSTR")
    c["paths"]["config"] = tmp_path / "config.toml"
    monkeypatch.setattr(real, "Visa", Fake)
    monkeypatch.setattr(real, "PPMS", lambda *a: (_ for _ in ()).throw(IOError("no MultiVu here")))
    monkeypatch.setattr(check_instruments, "load_config", lambda *a: c)
    monkeypatch.chdir(tmp_path)
    check_instruments.main()
    text = capsys.readouterr().out
    assert "[cell thermometer] Lake Shore 336" in text and "X98765" in text and "295.1" in text
    assert "FAIL open: OSError: no MultiVu here" in text
    assert list(tmp_path.glob("instrument_check_*.txt"))
