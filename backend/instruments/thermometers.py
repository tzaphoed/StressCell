"""Zellthermometer: ein eigenes Cernox (oder anderes Thermometer) nahe der Zelle.

Jeder Treiber liefert read() -> {"T_cell": K oder None, "R_cell": Ohm oder None}.

  LakeShore       Temperaturcontroller/-monitor, rechnet selbst mit seiner Kurve.
                  218: KRDG? <1..8>; 224, 335, 336, 350: KRDG? <A..D>; 372: RDGK? <1..16>.
                  Widerstand dazu über SRDG? bzw. RDGR?.
  Resistance      Widerstand von einem Messgerät, Temperatur aus der Kalibrierkurve (core/thermometry):
    PPMSBridgeR   Kanal der PPMS-Resistivity-Option. Kleiner Strom, Cernox heizt sich sonst auf.
    DMMFourWire   Keysight 34401A/34461A/34465A, Keithley 2000/2001/2010, 4-Leiter. Messstrom fest
                  (bei 100 kOhm etwa 10 uA): erst oberhalb von rund 20 K brauchbar.
  SimCell         Simulation: Zelltemperatur der Sim, hinkt dem PPMS-Thermometer nach.

Ungetestet an echter Hardware, Befehle laut Handbüchern.
"""

from __future__ import annotations


class LakeShore:
    def __init__(self, io, model="336", input="A"):
        self.io, self.model, self.inp = io, str(model), str(input)

    def identify(self):
        return self.io.query("*IDN?")

    def _q(self, cmd):
        v = float(self.io.query(cmd).strip().split(",")[0])
        return v

    def read(self):
        if self.model == "372":
            T, R = self._q(f"RDGK? {self.inp}"), self._q(f"RDGR? {self.inp}")
        else:
            T, R = self._q(f"KRDG? {self.inp}"), self._q(f"SRDG? {self.inp}")
        # Lake Shore meldet Über- oder Unterlauf als 0: das ist keine Temperatur
        return {"T_cell": T if T > 0 else None, "R_cell": R if R > 0 else None}

    def close(self):
        self.io.close()


class Resistance:
    """Widerstand von irgendwoher, Temperatur aus der Kurve."""

    def __init__(self, read_R, curve, name="resistance"):
        self.read_R, self.curve, self.name = read_R, curve, name

    def identify(self):
        if self.curve is None:
            return f"{self.name}, NO CURVE: only R is recorded (tools/cernox_fit.py makes one)"
        return f"{self.name}, curve {self.curve.name} ({self.curve.T_range[0]:g} ... {self.curve.T_range[1]:g} K)"

    def read(self):
        R = self.read_R()
        return {"T_cell": self.curve.T(R) if self.curve is not None else None, "R_cell": R}

    def close(self):
        pass


def ppms_bridge_reader(ppms, channel, current_uA):
    """Kanal der PPMS-Resistivity-Option als Widerstandsquelle."""
    ch = int(channel)
    with ppms.lock:
        ppms.c.resistivity.bridge_setup(bridge_number=ch, channel_on=True, current_limit_uA=float(current_uA),
                                        power_limit_uW=1.0, voltage_limit_mV=100)

    def read_R():
        with ppms.lock:
            R, status = ppms._call(ppms.c.resistivity.get_resistance, ch)
        return R
    return read_R


def dmm_reader(io, nplc=1.0):
    io.write("CONF:FRES")
    io.write(f"FRES:NPLC {nplc:g}")
    return lambda: float(io.query("READ?"))


class SimCell:
    def __init__(self, world, offset_K=0.0, fail=False):
        self.w, self.offset, self.fail = world, float(offset_K), fail

    def identify(self):
        return "simulated cell thermometer"

    def read(self):
        if self.fail:
            raise IOError("simulated thermometer failure")
        self.w.advance()
        return {"T_cell": self.w.T_cell + self.offset, "R_cell": None}

    def close(self):
        pass
