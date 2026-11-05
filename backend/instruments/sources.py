"""Eine Quelle je Stapelgruppe.

Ohne RP100 treibt man die beiden Stapelgruppen meist mit verschiedenen Geräten: Gleichspannung
auf den Druckstapel aus einer SMU, die AC-Anregung auf den Zugstapel aus dem Lock-in. Diese
Datei baut daraus ein Netzteil mit der gewohnten Schnittstelle (``PiezoSupply``).

Quellen je Kanal (``source`` in config.toml unter [instruments.tension] und
[instruments.compression]):

  rp100     ein Kanal eines RP100. Entlädt selbst.
  smu       Source-Measure-Unit: Keithley 2400/2410/2420, 2450, 2600B (TSP), Keysight B2900.
            Rampe in Software, misst Strom (Lichtbogenerkennung bleibt).
  hv_amp    Hochspannungsverstärker, Steuerspannung vom Aux-Ausgang eines SR830/SR860.
            Kein Strommesskanal.
  ac_only   Gleichspannung fest 0 V. Der Stapel bekommt nur die AC-Anregung über R2.
  grounded  Gleichspannung fest 0 V, keine Anregung. Der Stapel hängt nur am Bleeder.

Alles außer rp100 entlädt die Stapel nicht selbst, wenn das Gerät aus oder abgesteckt ist.
Razorbill verlangt aber eine Entladung bei jeder Temperaturänderung (pyroelektrische Spannung,
Datenblatt S. 14). Darum muss parallel zu jedem solchen Stapel ein Bleeder sitzen und in der
Konfiguration bestätigt sein. Der Bleeder zieht V/R Strom, den rechnet die Supply aus der
Strommessung heraus, sonst sähe die Lichtbogenerkennung ihn.

Alle Treiber hier folgen den Handbüchern bzw. pymeasure und sind gegen Nachbauten getestet,
nicht an echten Geräten.
"""

from __future__ import annotations

import logging
import math
import threading
import time

from .base import CHANNELS, PiezoSupply

log = logging.getLogger("stresscell.sources")

V_ABS_MAX = 210.0          # mehr verträgt das FC100 bei keiner Temperatur, unabhängig vom Fenster


class ChannelSource:
    """Ein Kanal. carries_dc: kann Gleichspannung stellen. discharges: entlädt selbst."""

    carries_dc = True
    discharges = False
    measures_current = True

    def identify(self) -> str:
        return type(self).__name__

    def set_output(self, on: bool) -> None: ...
    def output(self) -> bool: ...
    def set_voltage(self, volts: float) -> None: ...
    def voltage_setpoint(self) -> float: ...
    def voltage_now(self) -> float: ...
    def current(self) -> float | None: ...
    def set_slew(self, volts_per_s: float) -> None: ...
    def slew(self) -> float: ...

    def close(self) -> None:
        pass


class SoftRampSource(ChannelSource):
    """Rampe in Software für Geräte ohne eigene Rampe. Ein Thread schreibt alle STEP_S einen
    Schritt. Erst was angekommen ist, zählt als Istwert. Bei ausgeschaltetem Ausgang wird
    sofort geschrieben: sonst läge beim Einschalten noch der alte Wert an."""

    STEP_S = 0.1

    def __init__(self):
        self.target = 0.0
        self.now = 0.0
        self.rate = 1.0
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._t = None

    def _start(self, initial: float):
        self.now = self.target = float(initial)
        self._t = threading.Thread(target=self._ramp, daemon=True, name=f"ramp-{type(self).__name__}")
        self._t.start()

    # vom Treiber
    def _write(self, v: float) -> None:
        raise NotImplementedError

    def _out(self, on: bool) -> None:
        raise NotImplementedError

    def _out_q(self) -> bool:
        raise NotImplementedError

    def _ramp(self):
        while not self._stop.wait(self.STEP_S):
            self._step()

    def _step(self):
        with self.lock:
            d = self.target - self.now
            if d == 0:
                return
            v = self.now + math.copysign(min(abs(d), self.rate * self.STEP_S), d)
        try:
            self._write(v)
            with self.lock:
                self.now = v
        except Exception:
            log.exception("%s ramp", type(self).__name__)

    def set_output(self, on):
        self._out(bool(on))

    def output(self):
        return self._out_q()

    def set_voltage(self, volts):
        v = float(volts)
        if abs(v) > V_ABS_MAX:
            raise IOError(f"{v} V beyond any FC100 limit")
        if not self.output():
            self._write(v)
            with self.lock:
                self.target = self.now = v
            return
        with self.lock:
            self.target = v

    def voltage_setpoint(self):
        return self.target

    def voltage_now(self):
        return self.now

    def set_slew(self, volts_per_s):
        self.rate = float(volts_per_s)

    def slew(self):
        return self.rate

    def close(self):
        """Eine begonnene Rampe zu Ende führen, sonst bleibt die Spannung irgendwo stehen."""
        t0 = time.monotonic()
        while self.now != self.target and time.monotonic() - t0 < 60 and self._t is not None:
            time.sleep(self.STEP_S)
        self._stop.set()


# ── SMU ──────────────────────────────────────────────────────────────────
class Keithley2400Channel(SoftRampSource):
    """Keithley 2400, 2410, 2420, 2440 (SCPI). Der 2410 hat einen 1000-V-Bereich: die Grenze
    setzt hier V_ABS_MAX und das Fenster, nicht das Gerät."""

    def __init__(self, io, compliance_A=6e-3):
        super().__init__()
        self.io = io
        io.write("*CLS")
        io.write(":SOUR:FUNC VOLT")
        io.write(":SOUR:VOLT:RANG 200")
        io.write(f":SENS:CURR:PROT {compliance_A:g}")
        io.write(":SENS:FUNC 'CURR'")
        io.write(":FORM:ELEM VOLT,CURR")
        io.write(":OUTP:SMOD NORM")          # aus: 0 V mit kleiner Grenze, nicht offen
        self._start(float(io.query(":SOUR:VOLT?")))

    def identify(self):
        return self.io.query("*IDN?")

    def _write(self, v):
        self.io.write(f":SOUR:VOLT {v:.4f}")

    def _out(self, on):
        self.io.write(":OUTP ON" if on else ":OUTP OFF")

    def _out_q(self):
        return bool(int(float(self.io.query(":OUTP?"))))

    def current(self):
        if not self.output():
            return 0.0
        return float(self.io.query(":READ?").split(",")[1])

    def close(self):
        super().close()
        self.io.close()


class Keithley2450Channel(SoftRampSource):
    """Keithley 2450 im SCPI-Modus (nicht TSP, am Gerät unter Menu > Settings umstellen)."""

    def __init__(self, io, compliance_A=6e-3):
        super().__init__()
        self.io = io
        io.write("*CLS")
        io.write(":SOUR:FUNC VOLT")
        io.write(":SOUR:VOLT:RANG 200")
        io.write(f":SOUR:VOLT:ILIM {compliance_A:g}")
        io.write(":SENS:FUNC \"CURR\"")
        io.write(":OUTP:VOLT:SMOD NORM")
        self._start(float(io.query(":SOUR:VOLT?")))

    def identify(self):
        return self.io.query("*IDN?")

    def _write(self, v):
        self.io.write(f":SOUR:VOLT {v:.4f}")

    def _out(self, on):
        self.io.write(":OUTP ON" if on else ":OUTP OFF")

    def _out_q(self):
        return bool(int(float(self.io.query(":OUTP?"))))

    def current(self):
        if not self.output():
            return 0.0
        return float(self.io.query(":MEAS:CURR?"))

    def close(self):
        super().close()
        self.io.close()


class Keithley2600Channel(SoftRampSource):
    """Keithley 2611B/2612B/2614B/2634B, TSP. channel "a" oder "b". Beide Kanäle eines Geräts
    teilen sich eine VISA-Sitzung, das Bus-Lock sitzt im Visa-Objekt."""

    def __init__(self, io, channel="a", compliance_A=6e-3, close_io=True):
        super().__init__()
        self.io = io
        self.s = f"smu{str(channel).lower()}"
        self.close_io = close_io
        s = self.s
        io.write(f"{s}.source.func = {s}.OUTPUT_DCVOLTS")
        io.write(f"{s}.source.rangev = 200")
        io.write(f"{s}.source.limiti = {compliance_A:g}")
        io.write(f"{s}.source.offmode = {s}.OUTPUT_NORMAL")
        self._start(float(io.query(f"print({s}.source.levelv)")))

    def identify(self):
        return f"{self.io.query('*IDN?')} {self.s}"

    def _write(self, v):
        self.io.write(f"{self.s}.source.levelv = {v:.4f}")

    def _out(self, on):
        self.io.write(f"{self.s}.source.output = {self.s}.OUTPUT_{'ON' if on else 'OFF'}")

    def _out_q(self):
        return bool(int(float(self.io.query(f"print({self.s}.source.output)"))))

    def current(self):
        if not self.output():
            return 0.0
        return float(self.io.query(f"print({self.s}.measure.i())"))

    def close(self):
        super().close()
        if self.close_io:
            self.io.close()


class B2900Channel(SoftRampSource):
    """Keysight B2901A/B2902A/B2911A/B2912A. channel 1 oder 2."""

    def __init__(self, io, channel=1, compliance_A=6e-3, close_io=True):
        super().__init__()
        self.io = io
        self.n = int(channel)
        self.close_io = close_io
        n = self.n
        io.write("*CLS")
        io.write(f":SOUR{n}:FUNC:MODE VOLT")
        io.write(f":SOUR{n}:VOLT:RANG 200")
        io.write(f":SENS{n}:CURR:PROT {compliance_A:g}")
        io.write(f":OUTP{n}:OFF:MODE NORM")
        self._start(float(io.query(f":SOUR{n}:VOLT?")))

    def identify(self):
        return f"{self.io.query('*IDN?')} ch{self.n}"

    def _write(self, v):
        self.io.write(f":SOUR{self.n}:VOLT {v:.4f}")

    def _out(self, on):
        self.io.write(f":OUTP{self.n} {'ON' if on else 'OFF'}")

    def _out_q(self):
        return bool(int(float(self.io.query(f":OUTP{self.n}?"))))

    def current(self):
        if not self.output():
            return 0.0
        return float(self.io.query(f":MEAS:CURR? (@{self.n})"))

    def close(self):
        super().close()
        if self.close_io:
            self.io.close()


# ── Verstärker an Aux ────────────────────────────────────────────────────
class AuxAmpChannel(SoftRampSource):
    """Hochspannungsverstärker (Tegam 2350, PiezoDrive PD200, Trek, Falco) hinter dem
    Aux-Ausgang eines Lock-ins. SR860: Aux 0..3, SR830: Aux 1..4. gain: V am Stapel je V Aux.
    Kein Strommesskanal, also keine Lichtbogenerkennung auf diesem Kanal."""

    measures_current = False

    def __init__(self, io, gain, model="sr860", aux=None, close_io=False):
        super().__init__()
        self.io = io
        self.gain = float(gain)
        self.model = model
        self.aux = int(aux if aux is not None else (1 if model == "sr830" else 0))
        self.close_io = close_io
        self.on = False
        now = float(io.query(f"AUXV? {self.aux}")) * self.gain
        self.on = abs(now) > 1e-3          # nach Neustart liegt noch Spannung an: rampen, nicht springen
        self._start(now)

    def identify(self):
        return f"{self.model} aux {self.aux} x {self.gain:g}"

    def _write(self, v):
        ctrl = v / self.gain
        if abs(ctrl) > 10.5:
            raise IOError(f"aux would need {ctrl:.2f} V")
        self.io.write(f"AUXV {self.aux}, {ctrl:.5f}")

    def _out(self, on):
        self.on = bool(on)
        if not on:
            self._write(0.0)
            with self.lock:
                self.target = self.now = 0.0

    def _out_q(self):
        return self.on

    def current(self):
        return None

    def close(self):
        super().close()
        if self.close_io:
            self.io.close()


# ── feste 0 V ────────────────────────────────────────────────────────────
class FixedZeroChannel(ChannelSource):
    """Stapel ohne Gleichspannung. ac_only: Anregung kommt über R2 vom Oszillator,
    grounded: Stapel hängt nur am Bleeder."""

    carries_dc = False
    measures_current = False

    def __init__(self, kind="ac_only"):
        self.kind = kind
        self.on = False
        self.rate = 1.0

    def identify(self):
        return f"{self.kind} (0 V DC)"

    def set_output(self, on):
        self.on = bool(on)

    def output(self):
        return self.on

    def set_voltage(self, volts):
        if abs(float(volts)) > 1e-9:
            raise IOError(f"this stack is wired as {self.kind}: no DC voltage possible")

    def voltage_setpoint(self):
        return 0.0

    def voltage_now(self):
        return 0.0

    def current(self):
        return None

    def set_slew(self, volts_per_s):
        self.rate = float(volts_per_s)

    def slew(self):
        return self.rate


class RP100Channel(ChannelSource):
    """Ein Kanal eines RP100, wenn die andere Gruppe an einem anderen Gerät hängt."""

    discharges = True

    def __init__(self, rp, ch):
        self.rp, self.ch = rp, ch

    def identify(self):
        return f"{self.rp.identify()} {self.ch}"

    def set_output(self, on):
        self.rp.set_output(self.ch, on)

    def output(self):
        return self.rp.output(self.ch)

    def set_voltage(self, volts):
        self.rp.set_voltage(self.ch, volts)

    def voltage_setpoint(self):
        return self.rp.voltage_setpoint(self.ch)

    def voltage_now(self):
        return self.rp.voltage_now(self.ch)

    def current(self):
        return self.rp.current(self.ch)

    def set_slew(self, volts_per_s):
        self.rp.set_slew(self.ch, volts_per_s)

    def slew(self):
        return self.rp.slew(self.ch)


# ── zusammengesetzt ──────────────────────────────────────────────────────
class SplitSupply(PiezoSupply):
    def __init__(self, sources: dict, bleeder_ohm: dict | None = None):
        self.src = {c: sources[c] for c in CHANNELS}
        self.bleeder = {c: (bleeder_ohm or {}).get(c) for c in CHANNELS}
        self.dc_channels = tuple(c for c in CHANNELS if self.src[c].carries_dc)

    @property
    def compression_only(self) -> bool:
        return self.dc_channels == ("compression",)

    def identify(self):
        return " | ".join(f"{c}: {self.src[c].identify()}" for c in CHANNELS)

    def set_output(self, ch, on):
        self.src[ch].set_output(on)

    def output(self, ch):
        return self.src[ch].output()

    def set_voltage(self, ch, volts):
        self.src[ch].set_voltage(volts)

    def voltage_setpoint(self, ch):
        return self.src[ch].voltage_setpoint()

    def voltage_now(self, ch):
        return self.src[ch].voltage_now()

    def current(self, ch):
        i = self.src[ch].current()
        R = self.bleeder[ch]
        if i is None or not R:
            return i
        return i - self.src[ch].voltage_now() / R      # was durch den Bleeder fließt, ist kein Lichtbogen

    def set_slew(self, ch, volts_per_s):
        self.src[ch].set_slew(volts_per_s)

    def slew(self, ch):
        return self.src[ch].slew()

    def close(self):
        seen = set()
        for s in self.src.values():
            if id(s) not in seen:
                seen.add(id(s))
                try:
                    s.close()
                except Exception:
                    log.exception("close %s", s)


def dc_channels(supply) -> tuple:
    """Welche Stapelgruppen Gleichspannung tragen können."""
    if getattr(supply, "compression_only", False):
        return ("compression",)
    d = getattr(supply, "dc_channels", None)
    if d is not None:
        return tuple(d)
    return CHANNELS
