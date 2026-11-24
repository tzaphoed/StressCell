"""Lake Shore 370/372 lesen, Kalibrierkurve hochladen, zurücklesen und zuweisen."""

import importlib.util
import math
from pathlib import Path

import pytest

from core.thermometry import Curve
from instruments import thermometers as th

ROOT = Path(__file__).resolve().parents[2]


class FakeLakeShore:
    """Speichert Kurven wie das Gerät (6 signifikante Stellen) und kennt INSET/INCRV."""

    def __init__(self, model="336", verdreht=None):
        self.model, self.log, self.curves, self.verdreht = model, [], {}, verdreht
        self.inset = {"1": ["1", "10", "3", "0", "1"], "A": ["1", "10", "3", "0", "1"]}
        self.incrv = {"A": 0, "B": 0}
        self.closed = False

    def write(self, c):
        self.log.append(c)
        cmd, _, arg = c.partition(" ")
        p = [x.strip() for x in arg.split(",")]
        if cmd == "CRVDEL":
            self.curves[int(p[0])] = {"hdr": ["User Curve", "", "4", "325", "1"], "pts": {}}
        elif cmd == "CRVHDR":
            self.curves.setdefault(int(p[0]), {"pts": {}})["hdr"] = p[1:6]
        elif cmd == "CRVPT":
            u, t = float(f"{float(p[2]):.6g}"), float(f"{float(p[3]):.6g}")
            if self.verdreht == int(p[1]):
                t *= 1.01                                   # Gerät verschluckt sich an einem Punkt
            self.curves[int(p[0])]["pts"][int(p[1])] = (u, t)
        elif cmd == "INSET":
            self.inset[p[0]] = p[1:6]
        elif cmd == "INCRV":
            self.incrv[p[0]] = int(p[1])

    def query(self, c):
        self.log.append(c)
        cmd, _, arg = c.partition(" ")
        p = [x.strip() for x in arg.split(",")]
        if cmd == "*IDN?":
            return f"LSCI,MODEL{self.model},0,1.0"
        if cmd == "CRVHDR?":
            return ",".join(self.curves.get(int(p[0]), {"hdr": ["", "", "4", "325", "1"]})["hdr"])
        if cmd == "CRVPT?":
            u, t = self.curves[int(p[0])]["pts"][int(p[1])]
            return f"{u:+.6g},{t:+.6g}" + (",0" if self.model == "372" else "")
        if cmd == "INSET?":
            return ",".join(self.inset[p[0]])
        if cmd == "INCRV?":
            return str(self.incrv[p[0]])
        if cmd in ("RDGR?", "SRDG?"):
            return "+1234.56"
        if cmd in ("RDGK?", "KRDG?"):
            return "+4.2000"
        raise AssertionError(f"unexpected query {c}")

    def close(self):
        self.closed = True


def cernox(n=60):
    T = [2.0 * (300 / 2.0) ** (i / (n - 1)) for i in range(n)]
    R = [80.0 * (300.0 / t) ** 1.2 for t in T]                    # NTC: R fällt mit T
    return Curve(R, T, "X12345.340")


def test_370_reads_like_372():
    io = FakeLakeShore("370")
    r = th.LakeShore(io, "370", "1").read()
    assert r == {"T_cell": 4.2, "R_cell": 1234.56}
    assert io.log == ["RDGR? 1", "RDGK? 1"]


def test_unknown_model_is_refused():
    with pytest.raises(ValueError, match="unknown"):
        th.LakeShore(FakeLakeShore(), "371", "1")


def test_upload_336_ascending_units_and_readback():
    io = FakeLakeShore("336")
    ls = th.LakeShore(io, "336", "A")
    erg = ls.upload_curve(cernox(), 21, serial="X12345")
    pts = io.curves[21]["pts"]
    u = [pts[i][0] for i in sorted(pts)]
    assert len(u) == 60 and u == sorted(u)                         # aufsteigend in log10(R)
    assert pts[1][1] == pytest.approx(300.0, rel=1e-5)            # kleinster R = höchste T
    assert io.curves[21]["hdr"][:3] == ["X12345.340", "X12345", "4"]
    assert io.curves[21]["hdr"][4] == "1"                          # negativer Koeffizient
    assert erg["max_rel_dev"] < 1e-5 and not erg["thinned"]
    assert ls.assign_curve(21) == 21 and io.incrv["A"] == 21
    assert ls.curve_info()["name"] == "X12345.340"


def test_370_slots_and_inset_keeps_channel_settings():
    io = FakeLakeShore("370")
    ls = th.LakeShore(io, "370", "1")
    with pytest.raises(ValueError, match="user curve"):
        ls.upload_curve(cernox(), 21)                              # 370: nur 1 ... 20
    ls.upload_curve(cernox(), 5, fmt=3)
    assert io.curves[5]["hdr"][2] == "3"
    ls.assign_curve(5)
    assert io.inset["1"] == ["1", "10", "3", "5", "1"]             # Dwell, Pause, Tempco unverändert
    assert ls.curve_info()["curve"] == 5


def test_372_inset_with_wrong_field_count_is_not_written():
    io = FakeLakeShore("372")
    io.inset["1"] = ["1", "10", "3"]
    ls = th.LakeShore(io, "372", "1")
    with pytest.raises(IOError, match="5 fields"):
        ls.assign_curve(21)
    assert not any(c.startswith("INSET ") for c in io.log)


def test_readback_mismatch_stops():
    io = FakeLakeShore("336", verdreht=7)
    with pytest.raises(IOError, match="point 7"):
        th.LakeShore(io, "336", "A").upload_curve(cernox(), 22)


def test_more_than_200_points_are_thinned_without_new_points():
    io = FakeLakeShore("372")
    c = cernox(450)
    erg = th.LakeShore(io, "372", "1").upload_curve(c, 30)
    assert erg["points"] == 200 and erg["thinned"]
    T_file = [math.exp(x) for x in c.lnT]
    T_dev = [t for _, t in io.curves[30]["pts"].values()]
    assert all(min(abs(t - f) / f for f in T_file) < 1e-5 for t in T_dev)   # nur Punkte aus der Datei
    assert max(T_dev) == pytest.approx(300.0, rel=1e-5) and min(T_dev) == pytest.approx(2.0, rel=1e-5)


# ── Werkzeug tools/lakeshore_curve.py ────────────────────────────────────

def _tool():
    spec = importlib.util.spec_from_file_location("lakeshore_curve", ROOT / "tools" / "lakeshore_curve.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _config(tmp_path, model="370", inp="1", read_only=False):
    text = (ROOT / "backend" / "config.toml").read_text(encoding="utf-8")
    text = text.replace('model = "372"', f'model = "{model}"', 1).replace('input = "1"', f'input = "{inp}"', 1)
    text = text.replace('calibration = "cernox.340"', 'calibration = "X12345.340"', 1)
    if read_only:
        text = text.replace("read_only = false", "read_only = true", 1)
    (tmp_path / "backend").mkdir()
    p = tmp_path / "backend" / "config.toml"
    p.write_text(text, encoding="utf-8")
    cal = tmp_path / "calibrations"
    cal.mkdir()
    zeilen = ["Sensor Model:   CX-1050-SD", "Serial Number:  X12345", "Data Format:    4      (Log Ohms/Kelvin)",
              "", "No.   Units      Temperature (K)", ""]
    c = cernox(40)
    for i, (lr, lt) in enumerate(zip(c.lnR, c.lnT), start=1):
        zeilen.append(f"{i}  {math.log10(math.exp(lr)):.5f}  {math.exp(lt):.4f}")
    (cal / "X12345.340").write_text("\n".join(zeilen) + "\n", encoding="utf-8")
    return p


def test_tool_uploads_and_assigns_after_confirmation(tmp_path):
    io = FakeLakeShore("370")
    rc = _tool().main(["--config", str(_config(tmp_path)), "--kurve", "3", "--zuweisen"],
                      opener=lambda adr: io, eingabe=lambda q: "j")
    assert rc == 0 and len(io.curves[3]["pts"]) == 40
    assert io.curves[3]["hdr"][1] == "X12345"
    assert io.inset["1"][3] == "3" and io.closed


def test_tool_show_only_and_read_only_write_nothing(tmp_path):
    io = FakeLakeShore("370")
    t = _tool()
    assert t.main(["--config", str(_config(tmp_path)), "--kurve", "3", "--nur-zeigen"], opener=lambda adr: io) == 0
    io2 = FakeLakeShore("370")
    rc = t.main(["--config", str(_config(tmp_path / "ro", read_only=True) if (tmp_path / "ro").mkdir() is None else ""),
                 "--kurve", "3", "--ja"], opener=lambda adr: io2)
    assert rc == 3
    for f in (io, io2):
        assert not any(c.split(" ")[0] in ("CRVDEL", "CRVHDR", "CRVPT", "INSET") for c in f.log)


def test_tool_refuses_wrong_slot_and_answer_no(tmp_path):
    io = FakeLakeShore("370")
    t = _tool()
    cfgp = _config(tmp_path)
    assert t.main(["--config", str(cfgp), "--kurve", "25"], opener=lambda adr: io) == 2
    assert t.main(["--config", str(cfgp), "--kurve", "3"], opener=lambda adr: io, eingabe=lambda q: "") == 1
    assert not any(c.startswith("CRV") and not c.startswith("CRVHDR?") for c in io.log)
