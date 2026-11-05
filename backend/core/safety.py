"""Zentrale Grenzwertprüfung.

Jeder schreibende Befehl an die Geräte läuft durch Safety: aus der Sequenz, aus dem
Python-Client, aus dem Trockenlauf und aus der Oberfläche. Die Grenzen kommen aus
config.toml, das Spannungsfenster aus core/fc100.py. Geprüft wird nur hier.

Der Guard im Supervisor setzt das Fenster auch dann durch, wenn sich die Lage ändert,
nachdem ein Befehl längst angenommen wurde, vor allem beim Aufwärmen. Das kann ein
Prüfen des Befehls allein nicht leisten: bei 4 K sind 200 V erlaubt, bei 300 K zerstören
sie die Stapel.

Abschaltwege dürfen nie voneinander abhängen: jeder Kanal und die AC-Anregung bekommen
ihren eigenen Versuch, ein Fehler am einen hält den anderen nicht auf. Lange Aufrufe an den
Kryostaten laufen außerhalb des Locks, sonst blockiert ein hängendes MultiVu das Abschalten.
"""

from __future__ import annotations

import json
import logging
import math
import threading

from instruments.base import CHANNELS
from instruments.sources import dc_channels

from . import fc100
from .acchain import AcChain

log = logging.getLogger("stresscell.safety")

CHAMBER_MODES = ("seal", "purge_seal", "vent_seal", "pump_continuous", "vent_continuous", "high_vacuum")
CHAMBER_MOVING = ("purge_seal", "vent_seal", "pump_continuous", "vent_continuous", "high_vacuum")
T_MODES = ("fast_settle", "no_overshoot")                 # MultiPyVu temperature.approach_mode
B_MODES = ("linear", "no_overshoot", "oscillate")         # MultiPyVu field.approach_mode


class LimitError(Exception):
    pass


class GuardWriteError(Exception):
    """Mindestens ein Abschaltbefehl kam nicht an."""


def _num(v, name):
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise LimitError(f"{name} must be a number")
    if not math.isfinite(v):
        raise LimitError(f"{name} must be finite")
    return v


class Safety:
    def __init__(self, inst, cfg: dict, persist: bool = False):
        self.inst = inst
        self.cfg = cfg
        self.lim = cfg["limits"]
        self.safe_states = set(cfg["chamber"]["safe_states"])
        self.sealed_states = set(cfg["chamber"].get("sealed_states", ()))
        self.read_only = bool(cfg["mode"].get("read_only", False))
        self.lock = threading.RLock()
        # vom Supervisor gepflegt
        self.T: float | None = None
        self.T_slow: float | None = None # was die Zelle vermutlich hat, wenn sie dem Thermometer nachläuft
        self.dTdt = 0.0
        self.T_cell: float | None = None # eigenes Thermometer an der Zelle (Cernox), wenn vorhanden
        self.dTcdt = 0.0
        self.cell_cfg = cfg.get("cell_thermometer", {})
        self.chamber: str | None = None
        self.chamber_hold = 0            # so viele Abfragen dem Kammerzustand nicht trauen
        self.state_known = False         # False: es gab noch keine Abfrage, also nichts annehmen
        self.latched: str | None = None  # Lichtbogen oder Probenbruch: erst nach Quittieren weiter
        self.epoch = 0                   # jedes Stoppen zählt hoch, ein Takt im Flug schreibt dann nichts
        self.halted: str | None = None   # gestoppt und noch nicht quittiert
        self.expect_on = False
        self.outputs_seen = False        # nach einem Neustart zählt, was die Ausgänge gerade tun
        self.ac_cfg = cfg.get("ac", {})
        self.ac_chain = AcChain.from_cfg(self.ac_cfg)
        self.ac_f = 0.0
        self.ac_V = 0.0                  # Spitzenspannung am Stapel
        self.ac_fault: str | None = None
        self._latch_file = None
        paths = cfg.get("paths", {})
        if persist and "log_dir" in paths:
            self._latch_file = paths["log_dir"] / "latched.json"
            try:
                self.latched = json.loads(self._latch_file.read_text())["reason"]
            except (OSError, ValueError, KeyError):
                pass

    # ── Latch ────────────────────────────────────────────────────────────
    def latch(self, reason: str | None):
        """Überlebt einen Neustart: ein Lichtbogen ist nicht vorbei, weil das Programm neu lief."""
        self.latched = reason
        if self._latch_file:
            try:
                if reason:
                    self._latch_file.write_text(json.dumps({"reason": reason}))
                else:
                    self._latch_file.unlink(missing_ok=True)
            except OSError:
                log.exception("latch file")

    # ── Fenster ──────────────────────────────────────────────────────────
    def T_eff(self):
        """Temperatur für das Fenster. Mit Zellthermometer standardmäßig die höhere aus PPMS (mit
        Reserve und Nachlaufmodell) und Zelle (mit eigener Reserve): beim Abkühlen ist die Zelle wärmer,
        beim Aufwärmen das PPMS-Thermometer. window = "cernox": nur die Zelle, das PPMS als Ersatz."""
        cands = []
        if self.T is not None:
            ref = self.T if self.T_slow is None else max(self.T, self.T_slow)
            cands.append(fc100.effective_temperature(ref, self.dTdt, self.lim["T_margin_K"], self.lim["T_lookahead_s"]))
        if self.T_cell is not None:
            c = fc100.effective_temperature(self.T_cell, self.dTcdt, float(self.cell_cfg.get("margin_K", 1.0)),
                                            self.lim["T_lookahead_s"])
            if self.cell_cfg.get("window", "max") == "cernox":
                return c
            cands.append(c)
        return max(cands) if cands else None

    def chamber_limit(self, chamber=None) -> float | None:
        c = self.chamber if chamber is None else chamber
        if c in self.safe_states and self.chamber_hold <= 0:
            return None
        if c in self.sealed_states and self.chamber_hold <= 0:
            return self.lim["sealed_V_max"]
        return self.lim["transition_V_max"]

    def chamber_ok(self, chamber=None) -> bool:
        return self.chamber_limit(chamber) is None

    def window(self, T="now", chamber=None) -> tuple[float, float]:
        """Erlaubtes (Vmin, Vmax) je Kanal. Ohne Temperatur gilt das 300-K-Fenster."""
        Te = self.T_eff() if isinstance(T, str) else fc100.effective_temperature(T, 0.0, self.lim["T_margin_K"])
        vmin, vmax = fc100.voltage_window(Te)
        v = self.chamber_limit(chamber)
        if v is not None:
            vmin, vmax = max(vmin, -v), min(vmax, v)
        return vmin, vmax

    def dc_window(self):
        """Fenster für den DC-Anteil. Läuft die Anregung, muss die Spitze hineinpassen."""
        vmin, vmax = self.window()
        return vmin + self.ac_V, vmax - self.ac_V

    def _w(self, what):
        if self.read_only:
            raise LimitError(f"read-only mode: {what} refused")

    def _hv(self, what):
        self._w(what)
        if self.latched:
            raise LimitError(f"{what} refused: {self.latched}. Acknowledge first.")
        if self.halted:
            raise LimitError(f"{what} refused: stopped ({self.halted}). Acknowledge first.")
        if not self.state_known:
            raise LimitError(f"{what} refused: no temperature reading yet")

    def setpoints(self):
        s = self.inst.supply
        return {c: s.voltage_setpoint(c) for c in CHANNELS}

    def peaks(self):
        """Größter Betrag aus Sollwert und Istwert: eine laufende Rampe zählt mit."""
        s = self.inst.supply
        return {c: max(abs(s.voltage_setpoint(c)), abs(s.voltage_now(c))) for c in CHANNELS}

    def outputs_on(self):
        s = self.inst.supply
        return all(s.output(c) for c in CHANNELS)

    # ── Netzteil ─────────────────────────────────────────────────────────
    def check_voltages(self, v_t, v_c):
        v_t, v_c = _num(v_t, "tension voltage"), _num(v_c, "compression voltage")
        vmin, vmax = self.dc_window()
        for name, v in (("tension", v_t), ("compression", v_c)):
            if not vmin - 1e-9 <= v <= vmax + 1e-9:
                raise LimitError(f"{name} {v:g} V outside the window {vmin:.1f} ... {vmax:.1f} V "
                                 f"at T = {self.T if self.T is not None else 'unknown'} K")
        return v_t, v_c

    def _slew(self, slew):
        slew = _num(self.lim["slew_default_V_s"] if slew is None else slew, "slew rate")
        if not 0 < slew <= self.lim["slew_max_V_s"]:
            raise LimitError(f"slew rate {slew:g} V/s outside 0 ... {self.lim['slew_max_V_s']:g} V/s")
        return slew

    def set_voltages(self, v_t, v_c, slew=None, epoch=None):
        with self.lock:
            if epoch is not None and epoch != self.epoch:
                return
            self._hv("set voltages")
            v_t, v_c = self.check_voltages(v_t, v_c)
            slew = self._slew(slew)
            if not self.outputs_on():
                raise LimitError("outputs are off, switch them on first")
            self._apply(v_t, v_c, slew)

    def _apply(self, v_t, v_c, slew):
        s = self.inst.supply
        s.set_slew("tension", slew)
        s.set_slew("compression", slew)
        s.set_voltage("tension", v_t)
        s.set_voltage("compression", v_c)

    def split(self, D, vmin, vmax):
        """Drive auf die Stapel verteilen. Trägt nur eine Gruppe Gleichspannung (die andere ist
        ac_only oder grounded), bekommt sie den ganzen Drive."""
        dc = dc_channels(self.inst.supply)
        if dc == ("compression",):
            return 0.0, -D
        if dc == ("tension",):
            return D, 0.0
        if not dc:
            if abs(D) > 1e-9:
                raise ValueError("no stack is wired for DC voltage, the drive stays at 0 V")
            return 0.0, 0.0
        return fc100.split_drive(D, vmin, vmax)

    def set_drive(self, D, slew=None, epoch=None):
        with self.lock:
            D = _num(D, "drive")
            vmin, vmax = self.dc_window()
            try:
                v_t, v_c = self.split(D, vmin, vmax)
            except ValueError as e:
                raise LimitError(str(e))
            self.set_voltages(v_t, v_c, slew, epoch)

    def check_drive(self, D):
        D = _num(D, "drive")
        vmin, vmax = self.dc_window()
        try:
            v_t, v_c = self.split(D, vmin, vmax)
        except ValueError as e:
            raise LimitError(str(e))
        return self.check_voltages(v_t, v_c)

    def drive(self):
        sp = self.setpoints()
        return sp["tension"] - sp["compression"]

    def output_on(self):
        """Das Netzteil merkt sich seinen Sollwert auch bei ausgeschaltetem Ausgang
        (RP100-Handbuch). Deshalb zuerst auf 0 setzen, zurücklesen, dann schalten."""
        with self.lock:
            self._hv("output on")
            s = self.inst.supply
            for c in CHANNELS:
                if not s.output(c):
                    s.set_slew(c, self.lim["slew_default_V_s"])
                    s.set_voltage(c, 0.0)
                    if abs(s.voltage_setpoint(c)) > 1e-3:
                        raise LimitError(f"{c}: setpoint did not go to 0 V")
                    if abs(s.voltage_now(c)) > 1.0:
                        raise LimitError(f"{c}: still {s.voltage_now(c):.1f} V, wait for the discharge")
            for c in CHANNELS:
                s.set_output(c, True)
            self.expect_on = True

    def output_off(self, force=False):
        """Schließt die Stapel über den Entladewiderstand. Das entlädt in rund 0,1 s, die Probe
        bekommt also einen Kraftsprung. Darum nur nahe 0 V, außer der Guard ruft bei einem
        Lichtbogen (force). Auch im gelatchten Zustand erlaubt, nicht im Nur-Lese-Modus."""
        with self.lock:
            self._w("output off")
            s = self.inst.supply
            self.ac_off()
            if not force:
                v = max(abs(s.voltage_now(c)) for c in CHANNELS)
                if v > self.lim["output_off_V_max"]:
                    raise LimitError(f"{v:.1f} V on the stacks: zero the drive before switching off")
            self.expect_on = False
            errors = []
            for c in CHANNELS:                    # der Sollwert darf das Abschalten nicht aufhalten
                try:
                    s.set_output(c, False)
                except Exception as e:
                    errors.append(f"{c} output: {e}")
            for c in CHANNELS:
                try:
                    s.set_voltage(c, 0.0)
                except Exception as e:
                    errors.append(f"{c} setpoint: {e}")
            if errors:
                raise GuardWriteError("; ".join(errors))

    def zero(self, slew=None):
        """Beide Kanäle auf 0 V rampen. Immer erlaubt, auch im gelatchten Zustand."""
        self._w("zero")
        self.ac_off()
        self.guard_set(0.0, 0.0, self._slew(slew))

    def guard_set(self, v_t, v_c, slew=None, epoch=None):
        """Für Guard und Stopp: fährt in Richtung des sicheren Zustands, ohne Fensterprüfung.
        epoch: Stand beim Lesen. Wurde seither gestoppt, sind die Werte überholt."""
        with self.lock:
            if self.read_only or (epoch is not None and epoch != self.epoch):
                return
            s = self.inst.supply
            slew = self.lim["guard_slew_V_s"] if slew is None else slew
            errors = []
            for c, v in (("tension", v_t), ("compression", v_c)):
                try:
                    s.set_slew(c, slew)    # Scheitert die Rampe, muss die Spannung trotzdem hin
                except Exception as e:
                    log.warning("slew %s: %s", c, e)
                for attempt in (1, 2):
                    try:
                        s.set_voltage(c, v)
                        break
                    except Exception as e:
                        if attempt == 2:
                            errors.append(f"{c}: {e}")
            if errors:
                raise GuardWriteError("; ".join(errors))

    def bump_epoch(self):
        with self.lock:
            self.epoch += 1

    def _cryo_control(self, what):
        m = self.cfg.get("mode", {})
        if not m.get("ppms_read", True):
            raise LimitError(f"{what} refused: PPMS is switched off in the settings")
        if not m.get("ppms_control", True):
            raise LimitError(f"{what} refused: PPMS control is switched off, set it at MultiVu")

    def hold_temperature(self, T):
        """Letztes Mittel, wenn die Spannung nicht fällt: nicht weiter aufwärmen. Läuft auch bei
        abgeschalteter PPMS-Steuerung, solange ppms_emergency_hold nicht false ist."""
        m = self.cfg.get("mode", {})
        if not m.get("ppms_read", True) or not m.get("ppms_emergency_hold", True):
            return
        if not self.read_only and T is not None:
            self.inst.cryo.set_temperature(T, self.lim["T_rate_max"] / 2.0)

    # ── AC-Anregung ──────────────────────────────────────────────────────
    def ac_current(self, f, V):
        return 2.0 * math.pi * f * self.ac_cfg["C_F"] * V

    def bridge_conflict(self, f) -> str | None:
        """Die AH-Brücke regt den Kraftsensor mit bis zu 15 V bei ihrer Messfrequenz an, das koppelt
        in die Thermoelementdrähte. Liegt die Detektionsfrequenz h*f nahe einem Vielfachen davon,
        geht das Übersprechen durch den Lock-in-Filter. Rückgabe: Begründung oder None."""
        cap = str(self.cfg.get("instruments", {}).get("cap", ""))
        if not cap.startswith("ah"):
            return None
        fb = 1000.0 if cap != "ah2700a" else float(self.cfg.get("capacitance", {}).get("frequency_Hz", 1000.0))
        frac = float(self.ac_cfg.get("bridge_guard_frac", 0.03))
        fd = f * int(self.ac_cfg.get("harmonic", 1))
        for k in (1, 2, 3):
            if abs(fd - k * fb) <= frac * k * fb:
                return (f"detection at {fd:g} Hz is within {frac*100:.0f} % of {k} x {fb:g} Hz, where the "
                        f"{cap.upper()} excites the force sensor: crosstalk into the thermocouple")
        return None

    def set_ac(self, f, V, simulated=False, accept=False):
        with self.lock:
            self._hv("AC drive")
            c = self.ac_cfg
            if self.inst.lockin is None or not c.get("enabled", False):
                raise LimitError("no lock-in configured")
            if not simulated and not c.get("gain_verified", False):
                raise LimitError("AC drive is locked: measure the gain at the stack with a scope, "
                                 "enter it, then set gain_verified = true")
            f, V = _num(f, "frequency"), _num(V, "amplitude")
            if not c["f_min_Hz"] <= f <= c["f_max_Hz"]:
                raise LimitError(f"frequency {f:g} Hz outside {c['f_min_Hz']:g} ... {c['f_max_Hz']:g} Hz")
            why = self.bridge_conflict(f)
            if why and accept is not True:
                raise LimitError(f"{why}. Choose another frequency, or accept = true.")
            if not 0 < V <= c["V_max"]:
                raise LimitError(f"amplitude {V:g} V outside 0 ... {c['V_max']:g} V")
            I = self.ac_current(f, V)
            if I > c["current_max_A"]:
                raise LimitError(f"{V:g} V at {f:g} Hz drives {I*1e3:.1f} mA into the stack, "
                                 f"limit {c['current_max_A']*1e3:.1f} mA")
            if not self.outputs_on():
                raise LimitError("outputs are off, switch them on first")
            sp = self.setpoints()[c["channel"]]
            vmin, vmax = self.window()
            if sp - V < vmin or sp + V > vmax:
                raise LimitError(f"{sp:g} V +/- {V:g} V leaves the window {vmin:.1f} ... {vmax:.1f} V")
            self.inst.lockin.set_drive(f, V)
            self.ac_f, self.ac_V = f, V

    def ac_off(self):
        """Hält nie etwas auf: läuft die Anregung nicht, wird das Gerät gar nicht angesprochen,
        und ein Gerätefehler wird gemerkt statt geworfen. Das Abschalten der Spannung geht vor."""
        with self.lock:
            if self.ac_V and self.inst.lockin is not None and not self.read_only:
                try:
                    self.inst.lockin.set_drive(self.ac_f, 0.0)
                    self.ac_fault = None
                except Exception as e:
                    self.ac_fault = str(e)
                    log.exception("AC drive could not be switched off")
                    return
            self.ac_f, self.ac_V = 0.0, 0.0
            self.ac_fault = None

    def ac_peak_ok(self, vmin, vmax, sp) -> bool:
        v = sp[self.ac_cfg["channel"]] if self.ac_V else 0.0
        return not self.ac_V or (vmin <= v - self.ac_V and v + self.ac_V <= vmax)

    # ── Kryostat ─────────────────────────────────────────────────────────
    def set_detection(self, harmonic, tau):
        """Lock-in: Harmonische und Zeitkonstante. Keine Hochspannung, aber im Nur-Lese-Modus gesperrt."""
        with self.lock:
            self._w("lock-in setup")
            if self.inst.lockin is None:
                raise LimitError("no lock-in configured")
            h, tau = int(_num(harmonic, "harmonic")), _num(tau, "time constant")
            if not 1 <= h <= 9 or not 1e-5 <= tau <= 3e4:
                raise LimitError("harmonic 1 ... 9, time constant 10 us ... 30 ks")
            self.inst.lockin.set_detection(h, tau)
            self.ac_cfg["harmonic"], self.ac_cfg["time_constant_s"] = h, tau

    def set_temperature(self, T, rate, mode="fast_settle"):
        with self.lock:
            self._w("set temperature")
            self._cryo_control("set temperature")
            if mode not in T_MODES:
                raise LimitError(f"temperature mode {mode}: use {' | '.join(T_MODES)}")
            T, rate = _num(T, "temperature"), _num(rate, "rate")
            lim = self.lim
            if not lim["T_min"] <= T <= lim["T_max"]:
                raise LimitError(f"T = {T:g} K outside {lim['T_min']} ... {lim['T_max']} K")
            if not 0 < rate <= lim["T_rate_max"]:
                raise LimitError(f"rate {rate:g} K/min outside 0 ... {lim['T_rate_max']} K/min")
            # Beim Aufwärmen wird das Fenster enger. Besser hier ablehnen, als den
            # Guard mitten in der Messung zurückfahren zu lassen.
            sp = self.setpoints()
            vmin, vmax = fc100.voltage_window(T + lim["T_margin_K"])
            for c, v in sp.items():
                if not vmin + self.ac_V <= v <= vmax - self.ac_V:
                    raise LimitError(f"{c} is at {v:g} V, the window at {T:g} K is "
                                     f"{vmin:.1f} ... {vmax:.1f} V. Reduce the drive first.")
        self.inst.cryo.set_temperature(T, rate, mode)

    def set_field(self, B, rate, persistent=False, mode="linear"):
        """mode: linear | no_overshoot | oscillate. oscillate fährt um das Ziel herum und hält das
        Restfeld des supraleitenden Magneten klein, vor allem für Messungen bei B = 0."""
        with self.lock:
            self._w("set field")
            self._cryo_control("set field")
            if mode not in B_MODES:
                raise LimitError(f"field mode {mode}: use {' | '.join(B_MODES)}")
            B, rate = _num(B, "field"), _num(rate, "rate")
            if abs(B) > self.lim["B_max"]:
                raise LimitError(f"|B| = {abs(B):g} T above {self.lim['B_max']} T")
            if not 0 < rate <= self.lim["B_rate_max"]:
                raise LimitError(f"rate {rate:g} T/min outside 0 ... {self.lim['B_rate_max']} T/min")
        self.inst.cryo.set_field(B, rate, mode=mode, persistent=bool(persistent))

    def set_chamber(self, mode):
        with self.lock:
            self._w("set chamber")
            self._cryo_control("set chamber")
            if mode not in CHAMBER_MODES:
                raise LimitError(f"unknown chamber mode {mode}")
            if mode in CHAMBER_MOVING:
                v = self.lim["transition_V_max"]
                if any(x > v for x in self.peaks().values()):
                    raise LimitError(f"pressure change with more than {v:g} V on the stacks: "
                                     "the gas passes the range where it arcs. Reduce the drive first.")
                self.chamber_hold = 3     # der gelesene Zustand hinkt dem Befehl hinterher
        self.inst.cryo.set_chamber(mode)

    def configure_cap(self, frequency_Hz, excitation_Vrms):
        with self.lock:
            self._w("configure capacitance meter")
            f = _num(frequency_Hz, "frequency")
            v = _num(excitation_Vrms, "excitation")
            if not 0 < v <= self.lim["excitation_max_Vrms"]:
                raise LimitError(f"excitation {v:g} V rms outside 0 ... {self.lim['excitation_max_Vrms']} V rms")
            self.inst.cap.configure(f, v)
