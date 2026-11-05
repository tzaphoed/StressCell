"""Eine Quelle je Stapel, neue Treiber, AC-Kette, Frequenzsperre bei AH-Brücken.

Treiber laufen gegen Nachbauten, die nur prüfen, was über den Draht geht. Das ersetzt
keinen Test am Gerät.
"""

import copy
import math

import pytest

from .conftest import run_steps, seq
from core.acchain import AcChain
from core.safety import LimitError
from instruments import lockins, meters, real, sources


class IO:
    """Nachbau eines VISA-Geräts: protokolliert Befehle, beantwortet Fragen aus einer Tabelle."""

    def __init__(self, answers=None, default="0"):
        self.log = []
        self.answers = dict(answers or {})
        self.default = default
        self.closed = False

    def write(self, cmd):
        self.log.append(cmd)

    def query(self, cmd):
        self.log.append(cmd)
        a = self.answers.get(cmd, self.default)
        return a(cmd) if callable(a) else a

    def close(self):
        self.closed = True


# ── AC-Kette ─────────────────────────────────────────────────────────────
def test_chain_without_resistor_is_flat():
    c = AcChain(gain=math.sqrt(2), R_ohm=0.0)
    assert c.out_rms(1000.0, 1.414) == pytest.approx(0.99985, rel=1e-3)
    assert c.phase_deg(1e4) == 0.0 and c.corner_Hz() is None


def test_chain_corner_and_phase():
    c = AcChain(gain=1.414, R_ohm=290.0, C_F=3.1e-6)
    fc = c.corner_Hz()
    assert fc == pytest.approx(177.0, rel=0.01)
    assert c.phase_deg(fc) == pytest.approx(-45.0, abs=0.01)
    assert c.out_rms(fc, 1.0) == pytest.approx(math.sqrt(2) / 1.414, rel=1e-3)   # 71 % kommen an
    assert c.stack_peak(fc, c.out_rms(fc, 0.8)) == pytest.approx(0.8)


# ── Kanalquellen ─────────────────────────────────────────────────────────
def test_keithley2400_channel_setup_and_ramp():
    io = IO({":SOUR:VOLT?": "0.0", ":OUTP?": "1", ":READ?": "5.0,2.0E-6"})
    ch = sources.Keithley2400Channel(io, 6e-3)
    assert ":OUTP:SMOD NORM" in io.log and ":SOUR:VOLT:RANG 200" in io.log
    assert ":SENS:CURR:PROT 0.006" in io.log
    ch._stop.set()
    ch.set_slew(10.0)
    ch.set_voltage(3.0)
    ch._step()
    assert ch.voltage_now() == pytest.approx(1.0)       # 10 V/s * 0,1 s
    assert io.log[-1] == ":SOUR:VOLT 1.0000"
    assert ch.current() == pytest.approx(2e-6)
    with pytest.raises(IOError):
        ch.set_voltage(250.0)


def test_keithley2450_and_b2900_and_2600_commands():
    k = sources.Keithley2450Channel(IO({":SOUR:VOLT?": "0", ":OUTP?": "0"}), 5e-3)
    k._stop.set()
    assert ":SOUR:VOLT:ILIM 0.005" in k.io.log and ":OUTP:VOLT:SMOD NORM" in k.io.log
    k.set_voltage(2.0)                                     # Ausgang aus: sofort
    assert k.io.log[-1] == ":SOUR:VOLT 2.0000" and k.voltage_now() == 2.0

    b = sources.B2900Channel(IO({":SOUR2:VOLT?": "0", ":OUTP2?": "1", ":MEAS:CURR? (@2)": "1e-7"}), 2)
    b._stop.set()
    assert ":SOUR2:FUNC:MODE VOLT" in b.io.log and ":OUTP2:OFF:MODE NORM" in b.io.log
    assert b.current() == pytest.approx(1e-7)

    t = sources.Keithley2600Channel(IO({"print(smub.source.levelv)": "0", "print(smub.source.output)": "0"}), "b")
    t._stop.set()
    assert "smub.source.offmode = smub.OUTPUT_NORMAL" in t.io.log
    t.set_output(True)
    assert t.io.log[-1] == "smub.source.output = smub.OUTPUT_ON"


def test_aux_amp_channel_numbers_and_limits():
    a = sources.AuxAmpChannel(IO({"AUXV? 1": "0"}), gain=50.0, model="sr830")
    a._stop.set()
    assert a.aux == 1 and a.current() is None
    a.set_voltage(100.0)                                   # Ausgang aus: sofort, 2 V Aux
    assert a.io.log[-1] == "AUXV 1, 2.00000"
    with pytest.raises(IOError):
        a._write(600.0)                                     # 12 V Aux: mehr als der Ausgang kann
    b = sources.AuxAmpChannel(IO({"AUXV? 0": "0"}), gain=50.0, model="sr860")
    b._stop.set()
    assert b.aux == 0


def test_fixed_zero_channel_refuses_dc():
    z = sources.FixedZeroChannel("ac_only")
    z.set_voltage(0.0)
    with pytest.raises(IOError):
        z.set_voltage(1.0)
    assert z.current() is None and not z.carries_dc


def test_split_supply_subtracts_bleeder_current():
    io = IO({":SOUR:VOLT?": "0.0", ":OUTP?": "1", ":READ?": "100.0,1.0E-4"})
    smu = sources.Keithley2400Channel(io, 6e-3)
    smu._stop.set()
    smu.now = 100.0
    sup = sources.SplitSupply({"tension": sources.FixedZeroChannel(), "compression": smu},
                              {"compression": 1e6})
    assert sup.dc_channels == ("compression",) and sup.compression_only
    assert sup.current("compression") == pytest.approx(0.0, abs=1e-12)   # 100 V / 1 MOhm = 0,1 mA
    assert sup.current("tension") is None


# ── Lock-ins, Generator, Kapazität, Voltmeter ────────────────────────────
def test_sr830_drive_with_rc_chain():
    io = IO({"SNAP? 1,2,3,4": "1e-7,2e-7,2.236e-7,63.4", "FREQ?": "21.0"})
    chain = AcChain(1.414, 680.0 + 50.0, 3.1e-6)
    d = lockins.SR830(io, chain)
    assert "OUTX 1" in io.log and "FMOD 1" in io.log
    io.log.clear()
    d.set_drive(100.0, 1.0)
    v = float(io.log[-1].split()[1])
    assert v == pytest.approx(chain.out_rms(100.0, 1.0), abs=1e-3) and v > 1.0 / 1.414
    io.log.clear()
    d.set_drive(100.0, 0.0)
    assert io.log == ["SLVL 0.004"]                       # SR830 kann nicht ganz aus
    assert d.read()["theta"] == pytest.approx(63.4)


def test_sr7265_commands():
    io = IO({"X.": "1.0E-6", "Y.": "0", "MAG.": "1.0E-6", "PHA.": "0", "FRQ.": "21.0\x00"})
    d = lockins.SR7265(io, AcChain(1.414))
    d.set_drive(21.0, 1.414)
    assert "OF. 21.0000" in io.log and any(c.startswith("OA. 1.0") for c in io.log)
    d.set_detection(2, 1.0)
    assert "REFN 2" in io.log and f"TC {d.TAU.index(1.0)}" in io.log
    assert d.read()["f"] == 21.0


def test_generator_external_reference_lowers_before_raising_frequency():
    gio, lio = IO(), IO({"SNAP? 1,2,3,4": "0,0,0,0", "FREQ?": "1"})
    gen = lockins.FunctionGenerator(gio, "33500b", vlimit_peak=3.0)
    assert "OUTP:LOAD INF" in gio.log and "VOLT:LIM:STAT ON" in gio.log and "OUTP:SYNC ON" in gio.log
    det = lockins.SR830(lio, AcChain(1.414), reference="external")
    assert "FMOD 0" in lio.log
    d = lockins.ExternalRefDrive(gen, det, AcChain(1.414))
    d.set_drive(21.0, 2.0)
    gio.log.clear()
    d.set_drive(500.0, 0.5)
    assert gio.log[:2] == ["FREQ 21.000000", "VOLT 0.35361"]    # erst leiser bei alter Frequenz
    d.set_drive(500.0, 0.0)
    assert gio.log[-1] == "OUTP OFF"


def test_lockin_capacitance():
    # 1,1 pF bei 1 kHz und 1 V rms: 6,9 nA in Y
    y = 2 * math.pi * 1000.0 * 1.0 * 1.1e-12
    d = lockins.LockInCap(IO({"SNAP? 1,2": f"{y*1e-3},{y}"}), "sr830")
    d.configure(1000.0, 1.0)
    r = d.read()
    assert r["C_pF"] == pytest.approx(1.1) and r["loss"] == pytest.approx(1e-3)


def test_voltmeters_and_multisample():
    k = meters.K2182A(IO({":READ?": "1.5E-6"}))
    m = meters.DMM344xx(IO({"READ?": "2E-3"}))
    assert k.read() == {"V_tc": 1.5e-6}
    assert "CONF:VOLT:DC 0.1" in m.io.log

    class Broken:
        def read(self):
            raise IOError

    ms = meters.MultiSample([Broken(), k])
    assert ms.read()["V_tc"] == 1.5e-6


# ── Zusammenbau ──────────────────────────────────────────────────────────
class Pool(real.VisaPool):
    def __init__(self):
        def opener(addr, **kw):
            io = IO({":SOUR:VOLT?": "0", ":OUTP?": "0", "AUXV? 0": "0"})
            io.addr = addr
            return io
        super().__init__(opener)


def split_cfg(cfg):
    c = copy.deepcopy(cfg)
    c["instruments"]["supply"] = "split"
    c["instruments"]["tension"]["bleeder_fitted"] = True
    c["instruments"]["compression"]["bleeder_fitted"] = True
    c["ac"].update(gain=1.414, series_R_ohm=290.0, V_max=2.5)     # Aufbau A aus dem Handbuch
    return c


def test_build_refuses_without_bleeder(cfg):
    c = copy.deepcopy(cfg)
    c["instruments"]["supply"] = "split"
    with pytest.raises(RuntimeError, match="bleeder"):
        real.build_real(c, Pool(), cryo=object())


@pytest.mark.parametrize("cap,cls", [("e4980a", real.E4980AL), ("hp4284a", real.E4980AL),
                                     ("ah2500a", real.AHBridge), ("lockin_cap", lockins.LockInCap)])
def test_build_split_with_variants(cfg, cap, cls):
    c = split_cfg(cfg)
    c["instruments"]["cap"] = cap
    c["instruments"]["sample"] = ["k2182a", "dmm344xx"]
    pool = Pool()
    inst = real.build_real(c, pool, cryo=object())
    try:
        assert isinstance(inst.supply, sources.SplitSupply)
        assert inst.supply.dc_channels == ("compression",)
        assert isinstance(inst.cap, cls)
        assert isinstance(inst.sample, meters.MultiSample)
        assert isinstance(inst.lockin, real.SR860) and inst.lockin.chain.R == 290.0
        roles = [r for r, _, _ in real.configured_devices(c)]
        assert "compression" in roles and "lock-in" in roles and "voltmeter" in roles
    finally:
        inst.supply.close()


def test_build_generator_path(cfg):
    c = split_cfg(cfg)
    c["ac"]["source"] = "generator"
    c["instruments"]["lockin_model"] = "sr830"
    inst = real.build_real(c, Pool(), cryo=object())
    try:
        assert isinstance(inst.lockin, lockins.ExternalRefDrive)
    finally:
        inst.supply.close()


def test_hv_amp_needs_verified_gain(cfg):
    c = split_cfg(cfg)
    c["instruments"]["compression"].update(source="hv_amp", gain=50.0)
    with pytest.raises(RuntimeError, match="gain"):
        real.build_real(c, Pool(), cryo=object())
    c["instruments"]["compression"]["gain_verified"] = True
    inst = real.build_real(c, Pool(), cryo=object())
    assert isinstance(inst.supply.src["compression"], sources.AuxAmpChannel)
    inst.supply.close()


def test_dc_channels_from_config(cfg):
    c = split_cfg(cfg)
    assert real.dc_channels_from_cfg(c) == ("compression",)
    c["instruments"]["tension"]["source"] = "smu"
    assert real.dc_channels_from_cfg(c) == ("tension", "compression")
    c["instruments"]["supply"] = "rp100"
    assert real.dc_channels_from_cfg(c) == ("tension", "compression")


# ── Sicherheit mit geteilter Verdrahtung ─────────────────────────────────
@pytest.fixture
def split_stack(cfg):
    from core import stack as sm
    c = split_cfg(cfg)
    st = sm.build(c, virtual=True, noise=False, seed=1)
    st.init_cap()
    st.supervisor.tick()
    return st


def test_split_sim_puts_whole_drive_on_compression(split_stack):
    st = split_stack
    run_steps(st, {"op": "output", "on": True})
    st.safety.set_drive(-60)
    assert st.inst.supply.voltage_setpoint("tension") == 0.0
    assert st.inst.supply.voltage_setpoint("compression") == pytest.approx(60.0)
    with pytest.raises(LimitError):
        st.safety.set_drive(+30)        # Zug hieße -30 V am Druckstapel, bei 300 K sind nur -20 V erlaubt


def test_dry_run_knows_the_real_wiring(split_stack):

    rep = split_stack.dry_run(seq({"op": "output", "on": True}, {"op": "set_drive", "D": 25}))
    assert not rep["ok"]


def test_bridge_crosstalk_guard(stack):
    sf = stack.safety
    stack.cfg["instruments"]["cap"] = "ah2550a"
    run_steps(stack, {"op": "output", "on": True})
    with pytest.raises(LimitError, match="crosstalk"):
        sf.set_ac(990.0, 0.1, simulated=True)
    with pytest.raises(LimitError, match="crosstalk"):
        sf.set_ac(2010.0, 0.05, simulated=True)            # nahe der zweiten Oberwelle der Brücke
    sf.set_ac(990.0, 0.1, simulated=True, accept=True)
    sf.set_ac(21.0, 0.5, simulated=True)
    stack.cfg["instruments"]["cap"] = "e4980al"
    assert sf.bridge_conflict(1000.0) is None


def test_frequency_sweep_skips_bridge_frequencies(stack):
    stack.cfg["instruments"]["cap"] = "ah2550a"
    ctx = run_steps(stack, {"op": "output", "on": True}, {"op": "ac_on", "f": 100, "V": 0.2},
                    {"op": "frequency_sweep", "from": 900, "to": 1100, "points": 3, "settle": 1, "n": 1})
    assert ctx.points == 2                               # 1000 Hz übersprungen


def test_phase_is_reported_against_the_stack(stack):
    stack.safety.ac_chain = AcChain(25.0, 290.0, 3.1e-6)
    run_steps(stack, {"op": "output", "on": True}, {"op": "ac_on", "f": 177.0, "V": 0.5})
    s = stack.supervisor.tick()
    assert s["phi_RC_deg"] == pytest.approx(-45.0, abs=0.3)
    assert s["theta_stack"] == pytest.approx(((s["theta"] + 45.0 + 180) % 360) - 180, abs=0.3)


def test_dc_thermocouple_column(stack):
    stack.inst.sample.read = lambda: {"V_tc": 61e-6}
    s = stack.supervisor.tick()
    assert s["dT_dc_K"] == pytest.approx(1.0, rel=0.03)  # Type E bei 300 K: rund 61 uV/K


def test_check_instruments_lists_open_points(cfg):
    import check_instruments
    c = copy.deepcopy(cfg)
    c["instruments"]["supply"] = "split"
    found = "\n".join(check_instruments.config_findings(c))
    assert "bleeder_fitted" in found and "gain_verified" in found and "simulate" in found
