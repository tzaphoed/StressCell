"""Zellthermometer: ein eigenes Cernox (oder anderes Thermometer) nahe der Zelle.

Jeder Treiber liefert read() -> {"T_cell": K oder None, "R_cell": Ohm oder None}.

  LakeShore       Temperaturcontroller/-monitor, rechnet selbst mit seiner Kurve.
                  218: KRDG? <1..8>; 224, 335, 336, 350: KRDG? <A..D>;
                  370, 372 (AC-Widerstandsbrücken): RDGK? <1..16> (372 auch "A").
                  Widerstand dazu über SRDG? bzw. RDGR?.
                  upload_curve() lädt eine Kalibrierkurve in einen Anwenderplatz und liest sie zurück,
                  assign_curve() weist sie dem Eingang zu (tools/lakeshore_curve.py).
  Resistance      Widerstand von einem Messgerät, Temperatur aus der Kalibrierkurve (core/thermometry):
    PPMSBridgeR   Kanal der PPMS-Resistivity-Option. Kleiner Strom, Cernox heizt sich sonst auf.
    DMMFourWire   Keysight 34401A/34461A/34465A, Keithley 2000/2001/2010, 4-Leiter. Messstrom fest
                  (bei 100 kOhm etwa 10 uA): erst oberhalb von rund 20 K brauchbar.
  SimCell         Simulation: Zelltemperatur der Sim, hinkt dem PPMS-Thermometer nach.

Ungetestet an echter Hardware, Befehle laut Handbüchern.
"""

from __future__ import annotations

import math
import time

MODELS = ("218", "224", "335", "336", "350", "370", "372")
BRIDGES = ("370", "372")            # AC-Widerstandsbrücken: RDGK?/RDGR?, Kurve über INSET
# Anwenderplätze für Kurven je Modell (laut Handbüchern; Standardkurven liegen darunter)
USER_CURVES = {"218": range(21, 29), "224": range(21, 60), "335": range(21, 60), "336": range(21, 60),
               "350": range(21, 60), "370": range(1, 21), "372": range(21, 60)}
MAX_POINTS = 200


def _fix6(x: float) -> str:
    """Zahl mit 6 signifikanten Stellen in Festkomma, wie die Lake-Shore-Geräte sie annehmen."""
    if x == 0:
        return "0"
    stellen = max(0, 5 - int(math.floor(math.log10(abs(x)))))
    return f"{x:.{stellen}f}"


def _ascii(s: str, n: int) -> str:
    """Kopfzeilentext: ohne Komma (Trenner im Befehl), nur ASCII, gekürzt."""
    s = "".join(ch for ch in str(s) if 32 <= ord(ch) < 127 and ch not in ',;"')
    return s.strip()[:n]


class LakeShore:
    """curve = None: das Gerät rechnet mit der Kurve, die dem Eingang zugewiesen ist (KRDG?/RDGK?).
    curve = Curve: StressCell liest den Widerstand (SRDG?/RDGR?) und rechnet selbst."""

    def __init__(self, io, model="336", input="A", curve=None):
        self.io, self.model, self.inp, self.curve = io, str(model), str(input), curve
        if self.model not in MODELS:
            raise ValueError(f"Lake Shore model {self.model!r} unknown, use one of {', '.join(MODELS)}")

    def identify(self):
        return self.io.query("*IDN?")

    def _q(self, cmd):
        v = float(self.io.query(cmd).strip().split(",")[0])
        return v

    def read(self):
        bridge = self.model in BRIDGES
        R = self._q(f"RDGR? {self.inp}" if bridge else f"SRDG? {self.inp}")
        R = R if R > 0 else None
        if self.curve is not None:
            return {"T_cell": self.curve.T(R), "R_cell": R}
        T = self._q(f"RDGK? {self.inp}" if bridge else f"KRDG? {self.inp}")
        # Lake Shore meldet Über- oder Unterlauf als 0: das ist keine Temperatur
        return {"T_cell": T if T > 0 else None, "R_cell": R}

    # ── Kurven ────────────────────────────────────────────────────────────
    def assigned_curve(self) -> int | None:
        """Nummer der Kurve am Eingang, 0 = keine. Beim 218 nicht abfragbar (None)."""
        if self.model in BRIDGES:
            return int(float(self._inset()[3]))
        if self.model == "218":
            return None
        return int(float(self.io.query(f"INCRV? {self.inp}")))

    def curve_header(self, n: int) -> dict:
        parts = [p.strip() for p in self.io.query(f"CRVHDR? {int(n)}").split(",")] + [""] * 6
        name, serial, fmt, limit = parts[:4]
        return {"curve": int(n), "name": name, "serial": serial, "format": fmt, "limit_K": limit}

    def curve_info(self) -> dict:
        """Welche Kurve das Gerät für diesen Eingang benutzt. Nur lesen."""
        n = self.assigned_curve()
        if n is None:
            return {"note": f"model {self.model}: check the assigned curve at the front panel"}
        if n == 0:
            return {"curve": 0, "note": "no curve assigned: the device reports only sensor units"}
        return self.curve_header(n)

    def _inset(self) -> list[str]:
        """INSET? der Brücken: <an>,<dwell>,<pause>,<kurve>,<tempco>. Andere Feldzahl: abbrechen,
        statt etwas Falsches zurückzuschreiben."""
        parts = [p.strip() for p in self.io.query(f"INSET? {self.inp}").split(",")]
        if len(parts) != 5:
            raise IOError(f"INSET? {self.inp} answered {','.join(parts)!r}, expected 5 fields: "
                          "set the curve at the front panel")
        return parts

    def _check_slot(self, n: int):
        if int(n) not in USER_CURVES[self.model]:
            r = USER_CURVES[self.model]
            raise ValueError(f"curve {n} is not a user curve on the {self.model} (use {r.start} ... {r.stop - 1})")

    def upload_curve(self, curve, n: int, name: str = "", serial: str = "", fmt: int = 4,
                     pause_s: float = 0.0) -> dict:
        """Kurve in den Anwenderplatz n schreiben und Punkt für Punkt zurücklesen.

        fmt 4 = log10(Ohm)/K (empfohlen für Cernox), 3 = Ohm/K. Die Punkte gehen in aufsteigenden
        Sensoreinheiten ins Gerät. Mehr als 200 Punkte werden gleichmäßig ausgedünnt, ohne neue Punkte
        zu erfinden. Rückgabe: Kopfzeile, Punktzahl, größte Abweichung beim Rücklesen."""
        self._check_slot(n)
        if fmt not in (3, 4):
            raise ValueError("format must be 3 (ohm/K) or 4 (log ohm/K)")
        R = [math.exp(x) for x in curve.lnR]           # aufsteigend in R
        T = [math.exp(x) for x in curve.lnT]
        if len(R) > MAX_POINTS:
            idx = sorted({round(i * (len(R) - 1) / (MAX_POINTS - 1)) for i in range(MAX_POINTS)})
            R, T = [R[i] for i in idx], [T[i] for i in idx]
        units = [math.log10(r) if fmt == 4 else r for r in R]
        coeff = 1 if T[0] > T[-1] else 2                  # 1 negativ (Cernox), 2 positiv
        limit = _fix6(math.ceil(max(T)))
        nm = _ascii(name or curve.name or "StressCell", 15)
        sn = _ascii(serial or curve.serial or "", 10)
        def schreibe(cmd):
            self.io.write(cmd)
            if pause_s:
                time.sleep(pause_s)          # ältere Geräte (370) brauchen Luft zwischen den Befehlen

        schreibe(f"CRVDEL {int(n)}")
        schreibe(f"CRVHDR {int(n)},{nm},{sn},{fmt},{limit},{coeff}")
        sent = []
        for i, (u, t) in enumerate(zip(units, T), start=1):
            us, ts = _fix6(u), _fix6(t)
            schreibe(f"CRVPT {int(n)},{i},{us},{ts}")
            sent.append((float(us), float(ts)))
        worst = 0.0
        for i, (u, t) in enumerate(sent, start=1):
            back = [p.strip() for p in self.io.query(f"CRVPT? {int(n)},{i}").split(",")]
            bu, bt = float(back[0]), float(back[1])
            dev = max(abs(bu - u) / max(abs(u), 1e-9), abs(bt - t) / max(abs(t), 1e-9))
            worst = max(worst, dev)
            if dev > 1e-4:
                raise IOError(f"curve {n} point {i}: sent {u:g},{t:g}, device has {bu:g},{bt:g}")
        head = self.curve_header(n)
        if head["name"].strip() != nm:
            raise IOError(f"curve {n} header: sent name {nm!r}, device has {head['name']!r}")
        return {"header": head, "points": len(sent), "max_rel_dev": worst,
                "T_range": (min(T), max(T)), "thinned": len(sent) < len(curve.lnR)}

    def assign_curve(self, n: int) -> int:
        """Kurve n dem Eingang zuweisen und nachlesen. Beim 370/372 bleibt der Rest von INSET gleich."""
        self._check_slot(n)
        if self.model in BRIDGES:
            on, dwell, pause, _, tempco = self._inset()
            self.io.write(f"INSET {self.inp},{on},{dwell},{pause},{int(n)},{tempco}")
        else:
            self.io.write(f"INCRV {self.inp},{int(n)}")
        got = self.assigned_curve()
        if got is not None and got != int(n):
            raise IOError(f"input {self.inp} uses curve {got} after assigning {n}")
        return int(n)

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
