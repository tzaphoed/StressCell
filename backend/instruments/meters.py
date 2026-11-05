"""Zusätzliche Messgrößen an der Probe (``SampleMeter``).

  K2182A        Keithley 2182A Nanovoltmeter, Kanal 1. Für das volle Delta-T_ad beim Laden und
                Entladen: Thermospannung direkt, ohne Vorverstärker.
  DMM344xx      Keysight 34401A, 34461A, 34465A. Bereich 100 mV. Reicht für K, nicht für mK.
  MultiSample   mehrere Messgeräte zugleich, die Spalten werden zusammengeführt.

Die Thermospannung heißt "V_tc" (V). Der Supervisor rechnet sie mit der Seebeck-Kurve von
Type E in "dT_dc_K" um (Spalte im Log).
"""

from __future__ import annotations

from .base import SampleMeter


class K2182A(SampleMeter):
    def __init__(self, io, nplc=1.0, channel=1):
        self.io = io
        io.write(":SENS:FUNC 'VOLT'")
        io.write(f":SENS:CHAN {int(channel)}")
        io.write(f":SENS:VOLT:NPLC {nplc:g}")

    def identify(self):
        return self.io.query("*IDN?")

    def read(self):
        return {"V_tc": float(self.io.query(":READ?"))}

    def close(self):
        self.io.close()


class DMM344xx(SampleMeter):
    def __init__(self, io, nplc=1.0):
        self.io = io
        io.write("CONF:VOLT:DC 0.1")
        io.write(f"VOLT:DC:NPLC {nplc:g}")

    def identify(self):
        return self.io.query("*IDN?")

    def read(self):
        return {"V_tc": float(self.io.query("READ?"))}

    def close(self):
        self.io.close()


class MultiSample(SampleMeter):
    def __init__(self, meters):
        self.meters = list(meters)

    def read(self):
        out = {}
        for m in self.meters:
            try:
                out.update(m.read())
            except Exception:          # ein ausgefallenes Gerät nimmt den anderen nichts weg
                pass
        return out

    def close(self):
        for m in self.meters:
            try:
                m.close()
            except Exception:
                pass
