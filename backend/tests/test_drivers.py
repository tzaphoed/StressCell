"""Treiber gegen nachgebaute Geräte: geprüft wird, was über den Draht geht.

Der RP100-Nachbau folgt dem Handbuch v6.9. Das PPMS läuft gegen den Simulationsmodus von
MultiPyVu selbst, also gegen dessen echten Client und Server. Das ersetzt keinen Test am Gerät.
"""

import re
import time

import pytest

from instruments.real import E4980AL, PPMS, RP100, AHBridge, Keithley2400Pair


class FakeRP100:
    def __init__(self):
        self.out = {1: 0, 2: 0}
        self.v = {1: 0.0, 2: 0.0}
        self.slew = {1: 100.0, 2: 100.0}
        self.log = []

    def write(self, cmd):
        self.log.append(cmd)
        m = re.fullmatch(r"OUTP([12]) ([01])", cmd)
        if m:
            self.out[int(m[1])] = int(m[2])
            return
        m = re.fullmatch(r"SOUR([12]):VOLT (-?[\d.]+)", cmd)
        if m:
            self.v[int(m[1])] = float(m[2])
            return
        m = re.fullmatch(r"SOUR([12]):VOLT:SLEW ([\d.e+-]+)", cmd)
        if m:
            self.slew[int(m[1])] = float(m[2])
            return
        raise AssertionError(f"RP100 would not understand {cmd!r}")

    def query(self, cmd):
        self.log.append(cmd)
        if cmd == "*IDN?":
            return "Razorbill,RP100,1,1"
        if cmd == "SYST:ERR?":
            return "0,No Error"
        n = int(re.search(r"[12]", cmd)[0])
        if re.fullmatch(r"OUTP[12]\?", cmd):
            return str(self.out[n])
        if re.fullmatch(r"SOUR[12]:VOLT\?", cmd) or re.fullmatch(r"SOUR[12]:VOLT:NOW\?", cmd):
            return f"{self.v[n]:.3f}"
        if re.fullmatch(r"SOUR[12]:VOLT:SLEW\?", cmd):
            return f"{self.slew[n]:g}"
        if re.fullmatch(r"MEAS[12]:CURR\?", cmd):
            return "1e-6"
        raise AssertionError(f"RP100 would not understand {cmd!r}")

    def close(self):
        pass


def test_rp100_commands_and_channel_mapping():
    io = FakeRP100()
    d = RP100(io, ch_tension=2, ch_compression=1)
    d.set_slew("tension", 1)
    d.set_voltage("tension", 12.5)
    d.set_output("compression", True)
    assert io.v[2] == 12.5 and io.out[1] == 1 and io.slew[2] == 1
    assert d.voltage_now("tension") == 12.5 and d.output("compression") and d.errors() == []
    assert d.current("tension") == 1e-6


def test_rp100_detects_setpoint_mismatch():
    io = FakeRP100()
    real_query = io.query
    io.query = lambda c: "0.000" if c == "SOUR1:VOLT?" else real_query(c)
    with pytest.raises(IOError):
        RP100(io).set_voltage("tension", 10)


@pytest.mark.parametrize("line,C,L,V", [
    ("C= 1.234567    PF L= 0.000014    NS V= 0.750   V", 1.234567, 1.4e-5, 0.75),
    ("F= 1000.00 HZ C= 4.20188     PF L=-0.0260      NS V= 15.0     V", 4.20188, -0.026, 15.0),
])
def test_ah_reply(line, C, L, V):
    r = AHBridge.parse(line)
    assert r["C_pF"] == C and r["loss"] == L and r["V_exc"] == V


def test_ah_excess_noise_is_no_value():
    assert AHBridge.parse("EXCESS NOISE")["C_pF"] is None


def test_ah_garbage_raises():
    with pytest.raises(IOError):
        AHBridge.parse("hello")


class Rec:
    def __init__(self, reply=""):
        self.log, self.reply = [], reply

    def write(self, c):
        self.log.append(c)

    def query(self, c):
        self.log.append(c)
        return self.reply

    def close(self):
        pass


def test_ah2550_keeps_1kHz():
    io = Rec()
    AHBridge(io, "2550A").configure(5000, 1.0)
    assert io.log == ["V 1.0000"]
    io = Rec()
    AHBridge(io, "2700A").configure(5000, 1.0)
    assert io.log == ["FR 5000.0", "V 1.0000"]


def test_e4980_scales_to_pF_and_rejects_status():
    io = Rec("+1.10000E-12,+1.0E-04,+0")
    d = E4980AL(io)
    assert d.read()["C_pF"] == pytest.approx(1.1)
    io.reply = "+9.9E+37,+9.9E+37,+1"
    assert d.read()["C_pF"] is None


def test_keithley_ramps_in_software():
    class K(Rec):
        def query(self, c):
            self.log.append(c)
            return "0.0" if c == ":SOUR:VOLT?" else "1"
    t, c = K(), K()
    d = Keithley2400Pair(t, c, 6e-3)
    assert ":OUTP:SMOD NORM" in t.log and ":SENS:CURR:PROT 0.006" in t.log
    d.set_slew("tension", 10.0)
    d.set_voltage("tension", 5.0)
    time.sleep(0.45)
    assert 0.0 < d.voltage_now("tension") <= 5.0
    writes = [x for x in t.log if x.startswith(":SOUR:VOLT ")]
    assert len(writes) >= 3                # nicht in einem Sprung
    d.close()


@pytest.fixture(scope="module")
def mpv_server():
    mpv = pytest.importorskip("MultiPyVu")
    with mpv.Server(flags=["-s", "ppms"]) as s:
        yield s


def test_ppms_through_multipyvu_scaffolding(mpv_server):
    import MultiPyVu as mpv
    c = mpv.Client()
    c.open()
    p = PPMS("localhost", 0, client=c)
    r = p.read()
    assert r["T"] == 300.0 and r["B"] == 0.0 and isinstance(r["chamber"], str)
    p.set_temperature(290, 10)
    p.set_field(0.5, 0.6)
    p.set_field(0.0, 0.6, persistent=False)
    p.set_chamber("purge_seal")
    assert "PPMS" in p.identify()
    p.close()


def test_sr860_commands():
    from instruments.real import SR860
    io = Rec("0.5")
    d = SR860(io, gain=25.0)
    assert io.log == ["SOFF 0"]
    io.log.clear()
    d.set_drive(21.0, 5.0)
    assert io.log == ["FREQ 2.100000e+01", "SLVL 2.000000000e-01"]
    with pytest.raises(ValueError):
        d.set_drive(21.0, 60.0)            # 2,4 V rms: mehr als der Ausgang kann
    io.log.clear()
    d.set_drive(21.0, 0.0)
    assert io.log == ["SLVL 0"]
    io.log.clear()
    d.set_detection(1, 1.0)
    assert io.log[0] == "HARM 1" and io.log[1].startswith("OFLT")
    assert d.TAU[int(io.log[1].split()[1])] == 1.0


def test_compression_amp_refuses_tension_and_overrange():
    from instruments.real import CompressionAmp
    d = CompressionAmp(Rec("0.0"), gain=50.0)
    d.set_voltage("tension", 1.0)
    assert d.voltage_setpoint("tension") == 0.0
    with pytest.raises(IOError):
        d.set_voltage("compression", 300.0)
    assert d.current("tension") is None        # lieber keine Angabe als falsche 0 A
    d.close()


def test_keithley_off_output_writes_at_once_so_on_never_jumps():
    class K(Rec):
        amp = 150.0
        out = 0

        def write(self, c):
            self.log.append(c)
            if c.startswith(":SOUR:VOLT "):
                self.amp = float(c.split()[1])
            if c == ":OUTP ON":
                self.out = 1

        def query(self, c):
            return {":SOUR:VOLT?": str(self.amp), ":OUTP?": str(self.out)}.get(c, "1")
    t, c = K(), K()
    d = Keithley2400Pair(t, c, 6e-3)
    d.set_voltage("tension", 0.0)
    assert t.amp == 0.0 and d.voltage_now("tension") == 0.0
    d.set_output("tension", True)
    d._stop.set()


def test_keithley_failed_write_is_not_counted_as_done():
    class K(Rec):
        fail = False

        def write(self, c):
            if self.fail and c.startswith(":SOUR:VOLT"):
                raise IOError("bus")
            self.log.append(c)

        def query(self, c):
            return "0.0" if c == ":SOUR:VOLT?" else "1"
    t, c = K(), K()
    d = Keithley2400Pair(t, c, 6e-3)
    d._stop.set()
    d.set_slew("tension", 10.0)
    d.set_voltage("tension", 1.0)
    t.fail = True
    d._step()
    assert d.voltage_now("tension") == 0.0
    t.fail = False
    d._step()
    assert d.voltage_now("tension") == pytest.approx(1.0)


def test_ppms_systemexit_becomes_ioerror():
    class C:
        def get_temperature(self):
            raise SystemExit(0)
    p = PPMS("x", 0, client=C())
    with pytest.raises(IOError):
        p.read()


def test_sr860_never_old_amplitude_at_higher_frequency():
    from instruments.real import SR860
    io = Rec("0")
    d = SR860(io, gain=1.0)
    d.set_drive(21.0, 1.0)
    io.log.clear()
    d.set_drive(1000.0, 0.1)
    assert io.log[0].startswith("SLVL") and io.log[1].startswith("FREQ")


def test_ppms_reconnects_after_loss():
    class Dead:
        def get_temperature(self):
            raise SystemExit(0)

    class Alive:
        instrument_name = "PPMS"

        def get_temperature(self):
            return 10.0, "Stable"

        def get_field(self):
            return 0.0, "Stable"

        def get_chamber(self):
            return "HiVac"
    p = PPMS("x", 0, client=Dead(), connect=Alive)
    with pytest.raises(IOError):
        p.read()
    p._next_try = 0.0
    assert p.read()["T"] == 10.0


def test_compression_amp_ramps_after_restart():
    from instruments.real import CompressionAmp
    io = Rec("2.0")                     # Aux steht auf 2 V, also 100 V am Stapel
    d = CompressionAmp(io, gain=50.0)
    d.set_voltage("compression", 0.0)
    assert not any(c.startswith("AUXV 0, 0.00000") for c in io.log)
    d.target = d.now                    # Rampe beenden
    d.close()


def test_slew_raster_is_tolerated():
    io = FakeRP100()
    real = io.query
    io.query = lambda c: "1.004" if c.endswith("SLEW?") else real(c)
    RP100(io).set_slew("tension", 1.0)
