"""Einstellungen aus der Oberfläche: PPMS lesen/steuern, Simulation, Neustart."""

import time
import tomllib
from pathlib import Path

import pytest

from core import settings
from core.safety import LimitError
from instruments.base import NoCryostat

from .conftest import run_steps

ROOT = Path(__file__).resolve().parent.parent


def test_write_keeps_comments_and_inserts_missing(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[mode]\nsimulate = true          # Kommentar bleibt\nread_only = false\n\n[limits]\nx = 1\n')
    settings.write(p, {"simulate": False, "ppms_control": False, "sim_speed": 20})
    t = p.read_text()
    assert "simulate = false          # Kommentar bleibt" in t
    d = tomllib.loads(t)
    assert d["mode"] == {"simulate": False, "read_only": False, "ppms_control": False, "sim_speed": 20.0}
    assert d["limits"]["x"] == 1


def test_write_refuses_other_keys_and_bad_types(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[mode]\nsimulate = true\n")
    with pytest.raises(ValueError):
        settings.write(p, {"force_max_N": 500})
    with pytest.raises(ValueError):
        settings.write(p, {"simulate": "yes"})


def test_real_config_file_survives_a_write(tmp_path):
    p = tmp_path / "config.toml"
    p.write_bytes((ROOT / "config.toml").read_bytes())
    settings.write(p, {"ppms_read": False, "ppms_control": False, "simulate": True})
    d = tomllib.loads(p.read_text(encoding="utf-8"))
    assert d["mode"]["ppms_read"] is False and d["instruments"]["compression"]["source"] == "smu"


def test_ppms_control_off_refuses_cryostat_commands(stack):
    stack.cfg["mode"]["ppms_control"] = False
    for st in ({"op": "set_temperature", "T": 250, "rate": 2}, {"op": "set_field", "B": 1},
               {"op": "set_chamber", "mode": "seal"}):
        with pytest.raises(Exception, match="PPMS control is switched off"):
            run_steps(stack, st)
    s = stack.supervisor.tick()
    assert s["T"] is not None                           # gelesen wird weiter


def test_emergency_hold_switch(stack):
    called = []
    stack.inst.cryo.set_temperature = lambda *a, **k: called.append(a)
    stack.cfg["mode"]["ppms_emergency_hold"] = False
    stack.safety.hold_temperature(250.0)
    stack.cfg["mode"]["ppms_emergency_hold"] = True
    stack.safety.hold_temperature(250.0)
    assert len(called) == 1


def test_without_ppms_data_the_narrowest_window_applies(cfg):
    from core import stack as sm
    cfg["mode"]["ppms_read"] = False
    st = sm.build(cfg, virtual=True, noise=False)
    st.init_cap()
    s = st.supervisor.tick()
    assert isinstance(st.inst.cryo, NoCryostat)
    assert s["T"] is None and (s["vmin"], s["vmax"]) == (-20.0, 50.0)
    with pytest.raises(LimitError):
        st.safety.set_temperature(10, 2)


@pytest.fixture
def api_file(cfg, tmp_path, monkeypatch):
    """Server mit einer echten config.toml im tmp-Ordner, damit /settings schreiben kann."""
    import server
    from fastapi.testclient import TestClient
    text = (ROOT / "config.toml").read_text(encoding="utf-8")
    for k in ("data_dir", "log_dir", "calibration_dir", "sequence_dir"):
        text = text.replace(f'{k} = "', f'{k} = "{tmp_path.as_posix()}/{k}_', 1)
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    from core.config import load_config
    c = load_config(p)
    for name in ("cell_example.toml", "FC100_0260.toml"):
        (c["paths"]["calibration_dir"] / name).write_bytes((ROOT.parent / "calibrations" / name).read_bytes())
    c["mode"]["sim_speed"] = 100.0
    monkeypatch.setattr(server.State, "cfg", c)
    monkeypatch.setattr(server.State, "token", "secret")
    with TestClient(server.app, base_url="http://127.0.0.1:8020") as cl:
        cl.headers["X-StressCell-Token"] = "secret"
        time.sleep(0.1)
        yield cl, p


def test_settings_endpoint_rebuilds_backend(api_file):
    api, p = api_file
    import server
    v = api.get("/settings").json()["values"]
    assert v["simulate"] is True and v["ppms_read"] is True
    old = server.State.stack
    r = api.post("/settings", json={"values": {"ppms_read": False}})
    assert r.status_code == 200, r.text
    assert server.State.stack is not old and isinstance(server.State.stack.inst.cryo, NoCryostat)
    assert tomllib.loads(p.read_text(encoding="utf-8"))["mode"]["ppms_read"] is False
    assert api.post("/settings", json={"values": {"limits": 1}}).status_code == 400
    items = {i["key"]: i for i in api.get("/setup/checklist").json()["items"]}
    assert items["ppms"]["state"] == "warn"


def test_settings_refused_under_voltage(api_file):
    api, _ = api_file
    import server
    sup = server.State.stack.inst.supply
    real = sup.voltage_now
    sup.voltage_now = lambda c: 30.0
    try:
        r = api.post("/settings", json={"values": {"simulate": True}})
    finally:
        sup.voltage_now = real          # sonst wartet das Herunterfahren auf 0 V, das nie kommt
    assert r.status_code == 409 and "0 V" in r.json()["detail"]
