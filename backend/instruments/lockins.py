"""Lock-ins, Funktionsgeneratoren und die Lock-in-Kapazitätsmessung.

Schnittstelle ``LockIn`` (siehe base.py): set_drive(f, V_stack_peak), set_detection(h, tau),
read() -> {"X", "Y", "R", "theta", "f"}. Die Umrechnung von Stapelspannung auf Geräteausgang
macht die AcChain (Verstärkung und RC-Tiefpass), die jeder Treiber mitbekommt.

  SR830          Stanford Research, Befehle laut Handbuch. SINE OUT bis 5 V rms, minimal
                 4 mV rms: "aus" heißt hier 4 mV, am Stapel harmlos.
  SR860, SR865A  in real.py (SR860), SR865A spricht denselben Befehlssatz.
  SR7265/SR7270  Signal Recovery (EG&G). Gleitkomma-Befehle mit Punkt (OF., OA., X., ...).
                 Zeitkonstanten-Tabelle wie in pymeasure. Am Gerät prüfen.
  Generator      Keysight 33500B/33600A, 33210A/33220A. Ausgang auf hochohmig (OUTP:LOAD INF),
                 SYNC-Ausgang an den Referenzeingang des Lock-ins, der läuft auf externer
                 Referenz. Der 33500B kann eine Ausgangsgrenze (VOLT:LIM), die nutzen wir.
  LockInCap      Kapazität des Kraftsensors mit einem zweiten Lock-in: SINE OUT an High, Low
                 an den Stromeingang. C = Y / (2 pi f V). Selbstbau, reicht für etwa 0,1 N.

Kein Treiber an echter Hardware getestet. check_instruments.py fragt nur *IDN? ab.
"""

from __future__ import annotations

import math

from .base import CapacitanceMeter, LockIn


def _nearest(table, value):
    return min(range(len(table)), key=lambda k: abs(math.log(table[k] / value)))


class SR830(LockIn):
    TAU = [m * 10.0 ** e for e in range(-5, 5) for m in (1.0, 3.0)]   # 10 us ... 30 ks, OFLT 0..19
    VRMS_MIN = 0.004

    def __init__(self, io, chain, vrms_max=5.0, reference="internal", gpib=True):
        self.io = io
        self.chain = chain
        self.vrms_max = float(vrms_max)
        self.v = 0.0
        io.write(f"OUTX {1 if gpib else 0}")             # Antworten an die richtige Schnittstelle
        io.write("FMOD 1" if reference == "internal" else "FMOD 0")

    def identify(self):
        return self.io.query("*IDN?")

    def set_drive(self, frequency_Hz, V_stack_peak):
        if V_stack_peak == 0.0:
            self.io.write(f"SLVL {self.VRMS_MIN:.3f}")
            self.v = 0.0
            return
        v = self.chain.out_rms(frequency_Hz, V_stack_peak)
        if not self.VRMS_MIN <= v <= self.vrms_max:
            raise ValueError(f"SR830: {v:.4f} V rms outside {self.VRMS_MIN} ... {self.vrms_max}")
        if v < self.v:
            self.io.write(f"SLVL {v:.3f}")              # erst runter, dann die höhere Frequenz
        self.io.write(f"FREQ {frequency_Hz:.4f}")
        self.io.write(f"SLVL {v:.3f}")
        self.v = v

    def set_detection(self, harmonic, time_constant_s):
        self.io.write(f"HARM {int(harmonic)}")
        self.io.write(f"OFLT {_nearest(self.TAU, time_constant_s)}")

    def read(self):
        x, y, r, th = (float(p) for p in self.io.query("SNAP? 1,2,3,4").split(","))
        return {"X": x, "Y": y, "R": r, "theta": th, "f": float(self.io.query("FREQ?"))}

    def close(self):
        self.io.close()


class SR7265(LockIn):
    """Signal Recovery 7265/7270. TC-Index laut Tabelle unten (pymeasure DSPBase)."""

    TAU = [10e-6, 20e-6, 40e-6, 80e-6, 160e-6, 320e-6, 640e-6, 5e-3, 10e-3, 20e-3, 50e-3, 100e-3,
           200e-3, 500e-3, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0, 1e3, 2e3, 5e3, 1e4,
           2e4, 5e4, 1e5]

    def __init__(self, io, chain, vrms_max=5.0, reference="internal"):
        self.io = io
        self.chain = chain
        self.vrms_max = float(vrms_max)
        self.v = 0.0
        io.write("IE 0" if reference == "internal" else "IE 2")

    def identify(self):
        return self.io.query("ID")

    def set_drive(self, frequency_Hz, V_stack_peak):
        if V_stack_peak == 0.0:
            self.io.write("OA. 0")
            self.v = 0.0
            return
        v = self.chain.out_rms(frequency_Hz, V_stack_peak)
        if not 0.0 < v <= self.vrms_max:
            raise ValueError(f"SR7265: {v:.4f} V rms outside 0 ... {self.vrms_max}")
        if v < self.v:
            self.io.write(f"OA. {v:.6f}")
        self.io.write(f"OF. {frequency_Hz:.4f}")
        self.io.write(f"OA. {v:.6f}")
        self.v = v

    def set_detection(self, harmonic, time_constant_s):
        self.io.write(f"REFN {int(harmonic)}")
        self.io.write(f"TC {_nearest(self.TAU, time_constant_s)}")

    def _f(self, cmd):
        return float(self.io.query(cmd).strip().strip("\x00"))

    def read(self):
        return {"X": self._f("X."), "Y": self._f("Y."), "R": self._f("MAG."), "theta": self._f("PHA."),
                "f": self._f("FRQ.")}

    def close(self):
        self.io.close()


class FunctionGenerator:
    """Keysight/Agilent 33210A, 33220A, 33500B, 33600A. Amplitude als V rms an hochohmiger Last."""

    def __init__(self, io, model="33500b", vlimit_peak=None):
        self.io = io
        self.model = model
        io.write("OUTP OFF")
        io.write("OUTP:LOAD INF")
        io.write("FUNC SIN")
        io.write("VOLT:UNIT VRMS")
        io.write("VOLT:OFFS 0")
        io.write("OUTP:SYNC ON")
        if vlimit_peak and model in ("33500b", "33600a"):
            io.write(f"VOLT:LIM:HIGH {vlimit_peak:g}")
            io.write(f"VOLT:LIM:LOW {-vlimit_peak:g}")
            io.write("VOLT:LIM:STAT ON")
        self.on = False

    def identify(self):
        return self.io.query("*IDN?")

    def apply(self, f, vrms):
        if vrms <= 0:
            self.io.write("OUTP OFF")
            self.on = False
            return
        self.io.write(f"FREQ {f:.6f}")
        self.io.write(f"VOLT {vrms:.5f}")
        if not self.on:
            self.io.write("OUTP ON")
            self.on = True

    def close(self):
        self.io.close()


class ExternalRefDrive(LockIn):
    """Generator regt an, Lock-in misst auf externer Referenz (SYNC des Generators)."""

    def __init__(self, gen: FunctionGenerator, detector: LockIn, chain, vrms_max=7.0):
        self.gen, self.det, self.chain = gen, detector, chain
        self.vrms_max = float(vrms_max)
        self.v = 0.0
        self.f = None

    def identify(self):
        return f"{self.gen.identify()} -> {self.det.identify()}"

    def set_drive(self, frequency_Hz, V_stack_peak):
        if V_stack_peak == 0.0:
            self.gen.apply(frequency_Hz, 0.0)
            self.v = 0.0
            return
        v = self.chain.out_rms(frequency_Hz, V_stack_peak)
        if not 0.0 < v <= self.vrms_max:
            raise ValueError(f"generator: {v:.4f} V rms outside 0 ... {self.vrms_max}")
        if v < self.v and self.f is not None:
            self.gen.apply(self.f, v)                  # erst bei alter Frequenz leiser
        self.gen.apply(frequency_Hz, v)
        self.f, self.v = frequency_Hz, v

    def set_detection(self, harmonic, time_constant_s):
        self.det.set_detection(harmonic, time_constant_s)

    def read(self):
        return self.det.read()

    def close(self):
        for d in (self.gen, self.det):
            try:
                d.close()
            except Exception:
                pass


class LockInCap(CapacitanceMeter):
    """Kraftsensor mit einem (zweiten) Lock-in: SINE OUT an High, Low an den Stromeingang.
    Der kapazitive Strom liegt 90° vor der Spannung, also in Y. C = Y / (2 pi f V_rms).
    phase_deg gleicht Kabelphasen aus (einmal mit offenem Kabelende bestimmen)."""

    def __init__(self, io, model="sr830", current_gain="1M", phase_deg=0.0):
        self.io, self.model = io, model
        self.f, self.v = 1000.0, 1.0
        if model == "sr830":
            io.write("OUTX 1")
            io.write("FMOD 1")
            io.write("ISRC 2" if current_gain == "1M" else "ISRC 3")
        else:
            io.write("RSRC 0")
            io.write("IVMD 1")
            io.write("ICUR 0" if current_gain == "1M" else "ICUR 1")
        io.write(f"PHAS {phase_deg:g}")

    def identify(self):
        return f"{self.io.query('*IDN?')} (capacitance via current input)"

    def configure(self, frequency_Hz, excitation_Vrms):
        self.f, self.v = float(frequency_Hz), float(excitation_Vrms)
        self.io.write(f"FREQ {self.f:.4f}")
        self.io.write(f"SLVL {self.v:.4f}")

    def read(self):
        if self.model == "sr830":
            x, y = (float(p) for p in self.io.query("SNAP? 1,2").split(","))
        else:
            x, y = (float(p) for p in self.io.query("SNAP? 0,1").split(","))
        if y <= 0:
            return {"C_pF": None, "loss": None, "V_exc": self.v}
        return {"C_pF": y / (2.0 * math.pi * self.f * self.v) * 1e12, "loss": x / y, "V_exc": self.v}

    def close(self):
        self.io.close()
