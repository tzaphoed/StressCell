"""Treiber für die echten Geräte. An echter Hardware noch nicht getestet.

Befehle:
  RP100         Razorbill RP100-Handbuch v6.9
  E4980AL       wie im pymeasure-Treiber AgilentE4980
  AH2550A/2700A wie im pymeasure-Treiber AH2500A/AH2700A, Antwortformat von dort. Für den
                2550A nimmt das an, dass er den Befehlssatz des 2500A spricht: prüfen.
  Keithley 2400 SCPI nach pymeasure Keithley2400, Rampe in Software
  PPMS          MultiPyVu 3.8 (Quantum Design). Der Server läuft auf dem MultiVu-Rechner.

check_instruments.py liest alles einmal aus, ohne etwas zu setzen.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time

from .base import (CHANNELS, CapacitanceMeter, Cryostat, Instruments, LockIn, NoCryostat, PiezoSupply,
                   SampleMeter)

log = logging.getLogger("stresscell.real")

OE_PER_T = 10000.0


class Visa:
    """Dünne Schicht über pyvisa, damit sich die Treiber ohne Gerät testen lassen."""

    def __init__(self, address, read_termination="\n", write_termination="\n", timeout_ms=3000):
        import pyvisa
        self.rm = pyvisa.ResourceManager()
        self.dev = self.rm.open_resource(address)
        self.dev.timeout = timeout_ms
        self.dev.read_termination = read_termination
        self.dev.write_termination = write_termination
        self.lock = threading.Lock()

    def write(self, cmd):
        with self.lock:
            self.dev.write(cmd)

    def query(self, cmd):
        with self.lock:
            try:
                return self.dev.query(cmd).strip()
            except Exception:
                try:
                    self.dev.clear()       # sonst gehört die nächste Antwort zur alten Frage
                except Exception:
                    pass
                raise

    def close(self):
        self.dev.close()


class RP100(PiezoSupply):
    def __init__(self, io, ch_tension=1, ch_compression=2):
        self.io = io
        self.n = {"tension": int(ch_tension), "compression": int(ch_compression)}

    def identify(self):
        return self.io.query("*IDN?")

    def errors(self) -> list[str]:
        out = []
        for _ in range(16):
            e = self.io.query("SYST:ERR?")
            if e.startswith("0"):
                return out
            out.append(e)
        return out

    def set_output(self, ch, on):
        self.io.write(f"OUTP{self.n[ch]} {1 if on else 0}")

    def output(self, ch):
        return bool(int(float(self.io.query(f"OUTP{self.n[ch]}?"))))

    def set_voltage(self, ch, volts):
        self.io.write(f"SOUR{self.n[ch]}:VOLT {volts:.4f}")
        back = self.voltage_setpoint(ch)
        if abs(back - volts) > 0.01:
            raise IOError(f"RP100 {ch}: set {volts} V, reads back {back} V")

    def voltage_setpoint(self, ch):
        return float(self.io.query(f"SOUR{self.n[ch]}:VOLT?"))

    def voltage_now(self, ch):
        return float(self.io.query(f"SOUR{self.n[ch]}:VOLT:NOW?"))

    def current(self, ch):
        return float(self.io.query(f"MEAS{self.n[ch]}:CURR?"))

    def set_slew(self, ch, volts_per_s):
        self.io.write(f"SOUR{self.n[ch]}:VOLT:SLEW {volts_per_s:g}")
        back = self.slew(ch)
        if abs(back - volts_per_s) > 0.01 * volts_per_s + 1e-3:
            raise IOError(f"RP100 {ch}: slew {volts_per_s} V/s, reads back {back}")

    def slew(self, ch):
        return float(self.io.query(f"SOUR{self.n[ch]}:VOLT:SLEW?"))

    def close(self):
        self.io.close()


class Keithley2400Pair(PiezoSupply):
    """Zwei SMU 2400 statt RP100. Jede SMU ist ein Kanal. Der 2400 hat keine eigene Rampe,
    also fährt ein Thread in kleinen Schritten.

    Das RP100 legt die Stapel beim Abschalten und stromlos über 14,7 kΩ kurz. Die SMU tut das
    nicht, deshalb verweigert build_real den Betrieb ohne Kurzschlussbox."""

    STEP_S = 0.1

    def __init__(self, io_t, io_c, compliance_A):
        self.io = {"tension": io_t, "compression": io_c}
        self.target = {c: 0.0 for c in CHANNELS}
        self.now = {c: 0.0 for c in CHANNELS}
        self.rate = {c: 1.0 for c in CHANNELS}
        self.lock = threading.Lock()
        for c, io in self.io.items():
            io.write("*CLS")
            io.write(":SOUR:FUNC VOLT")
            io.write(":SOUR:VOLT:RANG 200")
            io.write(f":SENS:CURR:PROT {compliance_A:g}")
            io.write(":OUTP:SMOD NORM")       # aus: 0 V mit niedriger Grenze, nicht offen
            self.now[c] = float(io.query(":SOUR:VOLT?"))
            self.target[c] = self.now[c]
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._ramp, daemon=True)
        self._t.start()

    def _ramp(self):
        while not self._stop.wait(self.STEP_S):
            self._step()

    def _step(self):
        for c in CHANNELS:
            with self.lock:
                d = self.target[c] - self.now[c]
                if d == 0:
                    continue
                v = self.now[c] + math.copysign(min(abs(d), self.rate[c] * self.STEP_S), d)
            try:
                self.io[c].write(f":SOUR:VOLT {v:.4f}")
                with self.lock:
                    self.now[c] = v        # erst was angekommen ist, zählt
            except Exception:
                log.exception("keithley ramp")

    def identify(self):
        return " / ".join(io.query("*IDN?") for io in self.io.values())

    def set_output(self, ch, on):
        self.io[ch].write(":OUTP ON" if on else ":OUTP OFF")

    def output(self, ch):
        return bool(int(float(self.io[ch].query(":OUTP?"))))

    def set_voltage(self, ch, volts):
        v = float(volts)
        if not self.output(ch):
            # Ausgang aus: sofort schreiben. Sonst läge beim Einschalten die alte Amplitude an.
            self.io[ch].write(f":SOUR:VOLT {v:.4f}")
            with self.lock:
                self.target[ch] = self.now[ch] = float(self.io[ch].query(":SOUR:VOLT?"))
            return
        with self.lock:
            self.target[ch] = v

    def voltage_setpoint(self, ch):
        return self.target[ch]

    def voltage_now(self, ch):
        return self.now[ch]

    def current(self, ch):
        io = self.io[ch]
        if not self.output(ch):
            return 0.0
        return float(io.query(":READ?").split(",")[1])

    def set_slew(self, ch, volts_per_s):
        self.rate[ch] = float(volts_per_s)

    def slew(self, ch):
        return self.rate[ch]

    def close(self):
        """Eine begonnene Rampe zu Ende führen, sonst bleibt die Spannung stehen."""
        t0 = time.monotonic()
        while any(self.now[c] != self.target[c] for c in CHANNELS) and time.monotonic() - t0 < 60:
            time.sleep(self.STEP_S)
        self._stop.set()
        for io in self.io.values():
            io.close()


class AHBridge(CapacitanceMeter):
    """Andeen-Hagerling 2550A (1 kHz fest) oder 2700A (50 Hz bis 20 kHz)."""

    _rx = re.compile(r"C=\s*(-?[0-9.]+)\s*PF\s+L=\s*(-?[0-9.]+)\s*([A-Z]+)\s+V=\s*(-?[0-9.]+)\s*V")

    def __init__(self, io, model="2550A"):
        self.io = io
        self.model = model

    def identify(self):
        return f"AH{self.model}"

    def configure(self, frequency_Hz, excitation_Vrms):
        if self.model == "2700A":
            self.io.write(f"FR {frequency_Hz:.1f}")
        elif abs(frequency_Hz - 1000.0) > 1e-6:
            log.warning("AH%s measures at 1 kHz only, ignoring %g Hz", self.model, frequency_Hz)
        self.io.write(f"V {excitation_Vrms:.4f}")

    @classmethod
    def parse(cls, line: str) -> dict:
        if line.strip() == "EXCESS NOISE":
            return {"C_pF": None, "loss": None, "V_exc": None}
        m = cls._rx.search(line)
        if m is None:
            raise IOError(f"AH reply not understood: {line!r}")
        return {"C_pF": float(m[1]), "loss": float(m[2]), "loss_unit": m[3], "V_exc": float(m[4])}

    def read(self):
        return self.parse(self.io.query("Q"))

    def close(self):
        self.io.close()


class E4980AL(CapacitanceMeter):
    """Keysight E4980A/E4980AL und HP/Agilent 4284A, gleicher Befehlssatz bis auf FUNC:IMP."""

    def __init__(self, io, model="e4980al"):
        self.io = io
        io.write("FORM ASC")
        io.write("FUNC:IMP CPD" if model == "hp4284a" else "FUNC:IMP:TYPE CPD")
        io.write("TRIG:SOUR INT")
        io.write(":INIT:CONT ON")

    def identify(self):
        return self.io.query("*IDN?")

    def configure(self, frequency_Hz, excitation_Vrms):
        self.io.write(f":FREQ:CW {frequency_Hz:g}")
        self.io.write(f":VOLT:LEV {excitation_Vrms:g}")

    def read(self):
        p = self.io.query(":FETCH?").split(",")
        if len(p) >= 3 and int(float(p[2])) != 0:
            return {"C_pF": None, "loss": None, "V_exc": None}
        return {"C_pF": float(p[0]) * 1e12, "loss": float(p[1]), "V_exc": None}

    def close(self):
        self.io.close()


class SR860(LockIn):
    """Befehle wie im pymeasure-Treiber SR860.

    gain ist die Spitzenspannung am Stapel je V rms an SLVL. Das SR860 gibt je nach
    Abschluss und Modus unterschiedliche Amplituden aus (Datenblatt: halbiert single-ended,
    verdoppelt an hoher Impedanz), ein 50-Ω-Verstärkereingang wirkt also anders als ein
    hochohmiger. Darum am Aufbau mit dem Oszilloskop messen, nicht ausrechnen."""

    TAU = [m * 10.0 ** e for e in range(-6, 5) for m in (1.0, 3.0)]

    def __init__(self, io, gain=None, vrms_max=2.0, chain=None, reference="internal"):
        from core.acchain import AcChain
        self.io = io
        self.chain = chain if chain is not None else AcChain(gain=float(gain))
        self.gain = self.chain.gain
        self.vrms_max = float(vrms_max)
        self.v = 0.0
        io.write("SOFF 0")      # ein alter Sinus-Offset ginge sonst verstärkt auf den Stapel
        if reference != "internal":
            io.write("RSRC 1")  # externe Referenz, z. B. SYNC eines Generators

    def identify(self):
        return self.io.query("*IDN?")

    def set_drive(self, frequency_Hz, V_stack_peak):
        v = self.chain.out_rms(frequency_Hz, float(V_stack_peak)) if V_stack_peak else 0.0
        if not 0.0 <= v <= self.vrms_max:
            raise ValueError(f"SR860: {v} V rms outside 0 ... {self.vrms_max}")
        if v == 0.0:
            self.io.write("SLVL 0")
            self.v = 0.0
            return
        # Beim Wechsel nie alte Amplitude bei neuer, höherer Frequenz: der Strom wächst mit f
        if v < self.v:
            self.io.write(f"SLVL {v:.9e}")
        self.io.write(f"FREQ {frequency_Hz:.6e}")
        self.io.write(f"SLVL {v:.9e}")
        self.v = v

    def set_detection(self, harmonic, time_constant_s):
        self.io.write(f"HARM {int(harmonic)}")
        i = min(range(len(self.TAU)), key=lambda k: abs(math.log(self.TAU[k] / time_constant_s)))
        self.io.write(f"OFLT {i}")

    def read(self):
        return {"X": float(self.io.query("OUTP? 0")), "Y": float(self.io.query("OUTP? 1")),
                "R": float(self.io.query("OUTP? 2")), "theta": float(self.io.query("OUTP? 3")),
                "f": float(self.io.query("FREQ?"))}

    def close(self):
        self.io.close()


class CompressionAmp(PiezoSupply):
    """Aufbau wie bei Ikeda: DC über Aux-Ausgang und Hochspannungsverstärker auf einer
    Stapelgruppe, die andere Gruppe bekommt nur die AC-Anregung vom Oszillatorausgang.

    tension steht fest auf 0 V, compression folgt AUXV 0 mal gain. Rampe in Software. Ohne
    Kurzschlussbox verweigert build_real den Betrieb: ein Verstärker entlädt beim Abschalten
    nichts, und sein Strom liegt bei 40 mA statt der erlaubten 10 mA."""

    compression_only = True
    STEP_S = 0.1

    def __init__(self, io, gain):
        self.io = io
        self.gain = float(gain)
        self.rate = 1.0
        self.now = float(io.query("AUXV? 0")) * self.gain
        self.target = self.now
        live = abs(self.now) > 1e-3      # nach einem Neustart: es liegt noch Spannung an, also rampen
        self.on = {c: live for c in CHANNELS}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._ramp, daemon=True)
        self._t.start()

    def _ramp(self):
        while not self._stop.wait(self.STEP_S):
            with self.lock:
                d = self.target - self.now
                if d == 0:
                    continue
                v = self.now + math.copysign(min(abs(d), self.rate * self.STEP_S), d)
            try:
                self.io.write(f"AUXV 0, {v / self.gain:.5f}")
                with self.lock:
                    self.now = v
            except Exception:
                log.exception("aux ramp")

    def identify(self):
        return "lock-in aux + HV amplifier"

    def set_output(self, ch, on):
        self.on[ch] = bool(on)
        if not on and ch == "compression":
            self._write_now(0.0)      # Aus heißt aus, die Box entlädt

    def _write_now(self, volts):
        self.io.write(f"AUXV 0, {volts / self.gain:.5f}")
        with self.lock:
            self.target = self.now = float(volts)

    def output(self, ch):
        return self.on[ch]

    def set_voltage(self, ch, volts):
        if ch == "tension":
            return                     # diese Verdrahtung hält die Zuggruppe auf 0 V DC
        if abs(volts) > 210.0:
            raise IOError(f"{volts} V beyond any FC100 limit")
        if not self.on["compression"]:
            self._write_now(float(volts))
            return
        with self.lock:
            self.target = float(volts)

    def voltage_setpoint(self, ch):
        return 0.0 if ch == "tension" else self.target

    def voltage_now(self, ch):
        return 0.0 if ch == "tension" else self.now

    def current(self, ch):
        return None                # kein Strommesskanal: Lichtbogenerkennung fehlt hier

    def set_slew(self, ch, volts_per_s):
        self.rate = float(volts_per_s)

    def slew(self, ch):
        return self.rate

    def close(self):
        t0 = time.monotonic()
        while self.now != self.target and time.monotonic() - t0 < 60:
            time.sleep(self.STEP_S)
        self._stop.set()


class PPMS(Cryostat):
    """Über MultiPyVu. Jeder Zugriff hält das Lock: Monitor und Sequenz teilen sich einen Client."""

    RETRY_S = 5.0

    def __init__(self, host, port, client=None, connect=None):
        self.connect = connect
        if client is None:
            self.connect = connect or (lambda: self._open(host, port))
            client = self.connect()
        self.c = client
        self.lock = threading.Lock()
        self._next_try = 0.0
        self.lost = False

    @staticmethod
    def _open(host, port):
        import MultiPyVu as mpv
        client = mpv.Client(host=host, port=int(port))
        client.open()
        return client

    def identify(self):
        return f"PPMS via MultiPyVu ({self.c.instrument_name})"

    def _call(self, fn, *a):
        """MultiPyVu beendet bei Verbindungsverlust mit SystemExit. Das darf hier nicht durch:
        es würde den Monitor oder den ganzen Prozess beenden, und niemand führte das Fenster nach."""
        try:
            r = fn(*a)
            self.lost = False
            return r
        except SystemExit as e:
            self.lost = True
            raise IOError(f"MultiPyVu connection lost ({e})") from None
        except Exception:
            self.lost = True
            raise

    def _reconnect(self):
        """Der Client erholt sich nicht von selbst. Ohne das bliebe T nach einem MultiVu-Neustart
        für immer aus, und das Programm stünde im 300-K-Fenster, bis es neu gestartet wird."""
        if not self.lost or self.connect is None or time.monotonic() < self._next_try:
            return
        self._next_try = time.monotonic() + self.RETRY_S
        try:
            self.c = self.connect()
            self.lost = False
        except (Exception, SystemExit) as e:
            log.warning("MultiPyVu reconnect failed: %s", e)

    def read(self):
        with self.lock:
            self._reconnect()
            T, ts = self._call(self.c.get_temperature)
            B, bs = self._call(self.c.get_field)
            ch = self._call(self.c.get_chamber)
        return {"T": T, "T_status": ts, "B": B / OE_PER_T, "B_status": bs, "chamber": ch}

    def set_temperature(self, T, rate_K_min, mode="fast_settle"):
        c = self.c
        with self.lock:
            self._call(c.set_temperature, T, rate_K_min, getattr(c.temperature.approach_mode, mode))

    def set_field(self, B, rate_T_min, mode="linear", persistent=False):
        c = self.c
        dm = c.field.driven_mode.persistent if persistent else c.field.driven_mode.driven
        with self.lock:
            self._call(c.set_field, B * OE_PER_T, rate_T_min * OE_PER_T / 60.0,
                       getattr(c.field.approach_mode, mode), dm)

    def set_chamber(self, mode):
        c = self.c
        with self.lock:
            self._call(c.set_chamber, getattr(c.chamber.mode, mode))

    def close(self):
        self.c.close_client()


class PPMSBridge(SampleMeter):
    """Widerstand der Probe über die Resistivity-Option, Kanäle laut PPMS1-Handbuch am Puck."""

    def __init__(self, ppms: PPMS, channel, current_uA):
        self.ppms, self.ch = ppms, int(channel)
        with ppms.lock:
            ppms.c.resistivity.bridge_setup(bridge_number=self.ch, channel_on=True,
                                            current_limit_uA=float(current_uA), power_limit_uW=500,
                                            voltage_limit_mV=1000)

    def read(self):
        with self.ppms.lock:
            R, status = self.ppms._call(self.ppms.c.resistivity.get_resistance, self.ch)
        return {"R_ohm": R}


# ── Zusammenbau ──────────────────────────────────────────────────────────
SUPPLIES = ("rp100", "keithley2400", "lockin_amp", "split")
CHANNEL_SOURCES = ("rp100", "smu", "hv_amp", "ac_only", "grounded")
SMU_MODELS = ("keithley2400", "keithley2410", "keithley2420", "keithley2450", "keithley2600", "b2900")
LOCKIN_MODELS = ("sr860", "sr865", "sr830", "sr7265", "sr7270")
GENERATOR_MODELS = ("33500b", "33600a", "33210a", "33220a")
CAP_MODELS = ("e4980al", "e4980a", "hp4284a", "ah2500a", "ah2550a", "ah2700a", "lockin_cap")
SAMPLE_MODELS = ("none", "ppms_bridge", "k2182a", "dmm344xx")


class VisaPool:
    """Eine Sitzung je Adresse: zwei Kanäle eines Geräts oder Lock-in für Anregung und Aux
    teilen sich Sitzung und Bus-Lock."""

    def __init__(self, opener=None):
        self.opener = opener or (lambda addr, **kw: Visa(addr, **kw))
        self.open: dict[str, object] = {}

    def get(self, address, **kw):
        if address not in self.open:
            self.open[address] = self.opener(address, **kw)
        return self.open[address]


def _ch_cfg(cfg, ch) -> dict:
    return cfg["instruments"].get(ch, {}) or {}


def dc_channels_from_cfg(cfg) -> tuple:
    """Welche Stapel Gleichspannung tragen, ohne ein Gerät zu öffnen. Für Sim und Trockenlauf."""
    i = cfg["instruments"]
    sup = i.get("supply", "rp100")
    if sup == "lockin_amp":
        return ("compression",)
    if sup == "split":
        return tuple(c for c in CHANNELS if _ch_cfg(cfg, c).get("source", "grounded") not in ("ac_only", "grounded"))
    return CHANNELS


def _check_bleeder(c, cc):
    if not cc.get("bleeder_fitted", False) or not cc.get("bleeder_R_ohm"):
        raise RuntimeError(
            f"{c} stack: source '{cc.get('source')}' does not discharge the stacks when it is off or "
            "unplugged. Razorbill requires a discharge path during every temperature change. Fit the "
            f"bleeder resistor (1 MOhm, >= 500 V) across the {c} stack, enter bleeder_R_ohm and set "
            f"bleeder_fitted = true under [instruments.{c}].")


def _smu_channel(pool, cc):
    from . import sources
    model = cc.get("model", "keithley2400")
    comp = float(cc.get("compliance_A", 6e-3))
    addr = cc["address"]
    if model in ("keithley2400", "keithley2410", "keithley2420"):
        return sources.Keithley2400Channel(pool.get(addr), comp)
    if model == "keithley2450":
        return sources.Keithley2450Channel(pool.get(addr), comp)
    if model == "keithley2600":
        return sources.Keithley2600Channel(pool.get(addr), cc.get("channel", "a"), comp, close_io=False)
    if model == "b2900":
        return sources.B2900Channel(pool.get(addr), cc.get("channel", 1), comp, close_io=False)
    raise ValueError(f"unknown SMU model {model} ({', '.join(SMU_MODELS)})")


def build_split_supply(cfg, pool):
    from . import sources
    i = cfg["instruments"]
    srcs, bleed, rp = {}, {}, None
    for c in CHANNELS:
        cc = _ch_cfg(cfg, c)
        kind = cc.get("source", "grounded")
        if kind not in CHANNEL_SOURCES:
            raise ValueError(f"[instruments.{c}] source = {kind}: use one of {', '.join(CHANNEL_SOURCES)}")
        if kind != "rp100":
            _check_bleeder(c, cc)
            bleed[c] = float(cc["bleeder_R_ohm"])
        if kind == "rp100":
            if rp is None:
                rp = RP100(pool.get(cc.get("address", i.get("rp100")), read_termination="\r\n",
                                    write_termination="\n"), i.get("rp100_ch_tension", 1),
                           i.get("rp100_ch_compression", 2))
            srcs[c] = sources.RP100Channel(rp, c)
        elif kind == "smu":
            srcs[c] = _smu_channel(pool, cc)
        elif kind == "hv_amp":
            if not cc.get("gain_verified", False):
                raise RuntimeError(f"{c} stack: measure the amplifier gain at the stack with a scope, "
                                   f"enter gain and set gain_verified = true under [instruments.{c}]")
            srcs[c] = sources.AuxAmpChannel(pool.get(cc.get("address", i.get("lockin"))), cc["gain"],
                                            cc.get("lockin_model", i.get("lockin_model", "sr860")), cc.get("aux"))
        else:
            srcs[c] = sources.FixedZeroChannel(kind)
    return sources.SplitSupply(srcs, bleed)


def build_lockin(cfg, pool):
    """AC-Anregung und Detektion. ac_source = lockin: Oszillator des Lock-ins.
    ac_source = generator: Funktionsgenerator, Lock-in auf externer Referenz."""
    from core.acchain import AcChain
    from . import lockins
    i, ac = cfg["instruments"], cfg.get("ac", {})
    chain = AcChain.from_cfg(ac)
    model = i.get("lockin_model", "sr860")
    if model not in LOCKIN_MODELS:
        raise ValueError(f"unknown lock-in {model} ({', '.join(LOCKIN_MODELS)})")
    external = ac.get("source", i.get("ac_source", "lockin")) == "generator"
    ref = "external" if external else "internal"
    io = pool.get(i["lockin"])
    if model in ("sr860", "sr865"):
        det = SR860(io, chain=chain, vrms_max=float(i.get("lockin_vrms_max", 2.0)), reference=ref)
    elif model == "sr830":
        det = lockins.SR830(io, chain, float(i.get("lockin_vrms_max", 5.0)), ref, str(i["lockin"]).upper().startswith("GPIB"))
    else:
        det = lockins.SR7265(io, chain, float(i.get("lockin_vrms_max", 5.0)), ref)
    if not external:
        return det
    gm = i.get("generator_model", "33500b")
    if gm not in GENERATOR_MODELS:
        raise ValueError(f"unknown generator {gm} ({', '.join(GENERATOR_MODELS)})")
    gen = lockins.FunctionGenerator(pool.get(i["generator"]), gm, i.get("generator_vlimit_V"))
    return lockins.ExternalRefDrive(gen, det, chain, float(i.get("generator_vrms_max", 3.5)))


def build_cap(cfg, pool):
    from . import lockins
    i = cfg["instruments"]
    m = i["cap"]
    if m in ("e4980al", "e4980a", "hp4284a"):
        return E4980AL(pool.get(i["capmeter"]), m)
    if m in ("ah2500a", "ah2550a", "ah2700a"):
        return AHBridge(pool.get(i["capmeter"], timeout_ms=10000), m[2:].upper())
    if m == "lockin_cap":
        return lockins.LockInCap(pool.get(i["capmeter"]), i.get("capmeter_model", "sr830"),
                                 i.get("lockin_cap_gain", "1M"), float(i.get("lockin_cap_phase_deg", 0.0)))
    raise ValueError(f"unknown capacitance meter {m} ({', '.join(CAP_MODELS)})")


def build_sample(cfg, pool, cryo):
    from . import meters
    i = cfg["instruments"]
    names = i.get("sample", "none")
    names = [names] if isinstance(names, str) else list(names)
    out = []
    for n in names:
        if n == "none":
            continue
        if n == "ppms_bridge":
            if isinstance(cryo, NoCryostat):
                raise RuntimeError("sample = ppms_bridge needs PPMS data: switch ppms_read on")
            out.append(PPMSBridge(cryo, i["ppms_bridge_channel"], i["ppms_bridge_current_uA"]))
        elif n == "k2182a":
            out.append(meters.K2182A(pool.get(i["voltmeter"]), float(i.get("voltmeter_nplc", 1.0))))
        elif n == "dmm344xx":
            out.append(meters.DMM344xx(pool.get(i["voltmeter"]), float(i.get("voltmeter_nplc", 1.0))))
        else:
            raise ValueError(f"unknown sample meter {n} ({', '.join(SAMPLE_MODELS)})")
    if not out:
        return None
    return out[0] if len(out) == 1 else meters.MultiSample(out)


def build_real(cfg, pool: VisaPool | None = None, cryo=None) -> Instruments:
    i = cfg["instruments"]
    pool = pool or VisaPool()
    sup = i["supply"]
    if sup == "rp100":
        supply = RP100(pool.get(i["rp100"], read_termination="\r\n", write_termination="\n"),
                       i["rp100_ch_tension"], i["rp100_ch_compression"])
    elif sup == "keithley2400":
        if not i.get("keithley_discharge_box_fitted", False):
            raise RuntimeError("Keithley operation needs the shorting box: stacks must never be left "
                               "open. Fit it, then set keithley_discharge_box_fitted = true.")
        supply = Keithley2400Pair(pool.get(i["keithley_tension"]), pool.get(i["keithley_compression"]),
                                  i["keithley_compliance_A"])
    elif sup == "lockin_amp":
        if not i.get("amp_discharge_box_fitted", False) or not i.get("amp_gain_verified", False):
            raise RuntimeError("Amplifier operation needs the shorting box, a current limit and a gain "
                               "measured at the stack: then set amp_discharge_box_fitted and "
                               "amp_gain_verified to true.")
        supply = CompressionAmp(pool.get(i["lockin"]), i["amp_dc_gain"])
    elif sup == "split":
        supply = build_split_supply(cfg, pool)
    else:
        raise ValueError(f"unknown supply {sup} ({', '.join(SUPPLIES)})")

    cap = build_cap(cfg, pool)
    if cryo is None:
        cryo = PPMS(i["multipyvu_host"], i["multipyvu_port"]) if cfg["mode"].get("ppms_read", True) else NoCryostat()
    sample = build_sample(cfg, pool, cryo)
    lockin = build_lockin(cfg, pool) if cfg.get("ac", {}).get("enabled", False) else None
    return Instruments(supply, cap, cryo, sample, lockin=lockin)


def configured_devices(cfg) -> list[tuple[str, str, str]]:
    """(Rolle, Modell, Adresse) aller VISA-Geräte laut Konfiguration. Für check_instruments."""
    i = cfg["instruments"]
    out = []
    sup = i.get("supply")
    if sup == "rp100":
        out.append(("supply", "rp100", i["rp100"]))
    elif sup == "keithley2400":
        out += [("tension", "keithley2400", i["keithley_tension"]),
                ("compression", "keithley2400", i["keithley_compression"])]
    elif sup == "lockin_amp":
        out.append(("supply", "lock-in aux + amplifier", i["lockin"]))
    elif sup == "split":
        for c in CHANNELS:
            cc = _ch_cfg(cfg, c)
            k = cc.get("source", "grounded")
            if k == "smu":
                out.append((c, cc.get("model", "keithley2400"), cc["address"]))
            elif k == "rp100":
                out.append((c, "rp100", cc.get("address", i.get("rp100"))))
            elif k == "hv_amp":
                out.append((c, "aux + amplifier", cc.get("address", i.get("lockin"))))
    out.append(("capacitance", i["cap"], i["capmeter"]))
    if cfg.get("ac", {}).get("enabled", False):
        out.append(("lock-in", i.get("lockin_model", "sr860"), i["lockin"]))
        if cfg["ac"].get("source", i.get("ac_source", "lockin")) == "generator":
            out.append(("generator", i.get("generator_model", "33500b"), i["generator"]))
    names = i.get("sample", "none")
    for n in ([names] if isinstance(names, str) else names):
        if n in ("k2182a", "dmm344xx"):
            out.append(("voltmeter", n, i["voltmeter"]))
    return out
