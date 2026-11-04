"""Simulation von FC100, Netzteil, Kapazitätsmessung und PPMS.

Physikalisch grob, aber mit dem, was für Regelung und Sicherheit zählt.

Kennwerte aus dem Datenblatt: ±45 µm Hub bei 300 K (D = ±140 V), ±25 µm bei 4 K
(D = ±400 V), Blockierkraft ±170 N (300 K) und ±100 N (4 K), Stapelkapazität
3,1 µF (Zug) und 6,2 µF (Druck), Sensor 1,1 pF und 0,5 fF/N. Die Netzteilwerte
(14,7 kΩ Entladewiderstand, Rampe im Gerät) stammen aus dem RP100-Handbuch.

Annahmen der Simulation, nicht aus Herstellerangaben: Hysterese als Spiel, Kriechen
erster Ordnung, Probensteifigkeit, Differenzausdehnung zur Titanzelle.

Sprünge in der Zeit (VirtualClock) werden in Schritte von höchstens dt_max zerlegt.
"""

from __future__ import annotations

import math
import random
import threading

from .base import CHANNELS, CapacitanceMeter, Cryostat, Instruments, LockIn, PiezoSupply, SampleMeter

SIM_DEFAULTS = {
    "T_start": 300.0,
    "T_lag_s": 20.0,            # Zelle hinkt dem PPMS-Thermometer hinterher
    "sample_k_N_um": 10.0,      # Probensteifigkeit, z.B. 100 GPa, 0,1 mm², 1 mm
    "F_break_N": 60.0,          # Probe reißt oder Klebung löst sich
    "thermal_mismatch_um": 3.0, # Differenzausdehnung bei 4 K gegenüber 300 K
    "backlash_V": 4.0,          # Hysterese der Stapel, als Drive
    "creep_frac": 0.05,
    "creep_tau_s": 60.0,
    "C_noise_fF": 0.003,        # 1σ je Messung, entspricht 6 mN/√Hz bei 1 Hz
    "S_pF_N": 0.0005,           # Vorzeichen: Zug vergrößert hier C
    "C0_pF": 1.1,
    "R0_ohm": 10.0,
    "gauge": 0.01,              # relative Widerstandsänderung pro N
    # AC-Elastokalorik, frei gewählte Modellwerte
    "sample_length_mm": 1.0,
    "Tc_K": 300.0,              # kontinuierlicher Curie-Übergang
    "dTc_dB": 1.0,              # K/T
    "Tc_width_K": 4.0,
    "dTdeps_el": 5.0,           # K pro Dehnung, nichtkritischer Anteil
    "dTdeps_peak": 300.0,       # K pro Dehnung am Übergang
    "tau_i_s": 0.05,            # Wärmeabfluss der Probe zu den Platten
    "tau_th_s": 1e-3,           # Thermoelement
    "pickup_V_per_THz": 2e-11,  # Induktion: V pro (T Hz V_stack), in Quadratur
    "preamp_gain": 100.0,
}

C_STACK = {"tension": 3.1e-6, "compression": 6.2e-6}
R_DISCHARGE = 14.7e3


def _lerp(T, a300, a4):
    x = min(max((T - 4.0) / 296.0, 0.0), 1.0)
    return a4 + (a300 - a4) * x


class SimWorld:
    def __init__(self, clock, cfg: dict | None = None, noise: bool = True, seed: int | None = None):
        self.clock = clock
        self.p = {**SIM_DEFAULTS, **(cfg or {})}
        self.noise = noise
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.t = clock.now()
        self.dt_max = 0.25

        T0 = float(self.p["T_start"])
        self.T = T0                # PPMS-Thermometer
        self.T_cell = T0
        self.T_set = T0
        self.T_rate = 10.0 / 60.0
        self.B = 0.0
        self.B_set = 0.0
        self.B_rate = 0.01
        self.chamber = "Purged and Sealed"
        self.chamber_until = None
        self.chamber_next = None

        self.on = {c: False for c in CHANNELS}
        self.v = {c: 0.0 for c in CHANNELS}
        self.v_set = {c: 0.0 for c in CHANNELS}
        self.slew = {c: 100.0 for c in CHANNELS}
        self.i = {c: 0.0 for c in CHANNELS}

        self.x_play = 0.0
        self.x_creep = 0.0
        self.F = 0.0
        self.broken = False

        # Fehler zum Einspritzen in Tests
        self.fault_arc = {c: 0.0 for c in CHANNELS}
        self.fault_cap_dead = False
        self.fault_T_dead = False
        self.excitation = 1.0
        self.frequency = 1000.0
        self.ac_f = 0.0
        self.ac_V = 0.0
        self.cal = None

    # ── Physik ───────────────────────────────────────────────────────────
    def advance(self):
        with self.lock:
            now = self.clock.now()
            dt = now - self.t
            if dt <= 0:
                return
            steps = max(1, min(4000, int(math.ceil(dt / self.dt_max))))
            h = dt / steps
            for _ in range(steps):
                self._step(h)
            self.t = now
            if self.chamber_until is not None and now >= self.chamber_until:
                self.chamber, self.chamber_until = self.chamber_next, None

    def _step(self, h):
        p = self.p
        dT = self.T_set - self.T
        self.T += math.copysign(min(abs(dT), self.T_rate * h), dT)
        self.T_cell += (self.T - self.T_cell) * (1.0 - math.exp(-h / p["T_lag_s"]))
        dB = self.B_set - self.B
        self.B += math.copysign(min(abs(dB), self.B_rate * h), dB)

        for c in CHANNELS:
            old = self.v[c]
            if self.on[c]:
                d = self.v_set[c] - old
                new = old + math.copysign(min(abs(d), self.slew[c] * h), d)
            else:
                new = old * math.exp(-h / (R_DISCHARGE * C_STACK[c]))
            self.v[c] = new
            self.i[c] = C_STACK[c] * (new - old) / h + self.fault_arc[c]

        g = _lerp(self.T_cell, 45.0 / 140.0, 25.0 / 400.0)        # µm pro V Drive
        k_c = _lerp(self.T_cell, 170.0 / 45.0, 100.0 / 25.0)      # N/µm
        D = self.v["tension"] - self.v["compression"]

        # Spiel: die Ausgangsgröße folgt erst, wenn der Eingang die Lücke überbrückt
        b = p["backlash_V"] / 2.0
        if D > self.x_play + b:
            self.x_play = D - b
        elif D < self.x_play - b:
            self.x_play = D + b
        self.x_creep += (self.x_play - self.x_creep) * (1.0 - math.exp(-h / p["creep_tau_s"]))
        drive = (1.0 - p["creep_frac"]) * self.x_play + p["creep_frac"] * self.x_creep

        x = g * drive + p["thermal_mismatch_um"] * (300.0 - self.T_cell) / 296.0
        k_s = 0.0 if self.broken else p["sample_k_N_um"]
        k = 0.0 if self.broken else k_c * k_s / (k_c + k_s)
        self.F = k * x
        if abs(self.F) > p["F_break_N"]:
            self.broken = True
            self.F = 0.0

    # ── Fehler ───────────────────────────────────────────────────────────
    def inject_arc(self, ch="tension", amps=4e-3):
        self.fault_arc[ch] = amps

    def clear_faults(self):
        self.fault_arc = {c: 0.0 for c in CHANNELS}
        self.fault_cap_dead = False
        self.fault_T_dead = False


class SimSupply(PiezoSupply):
    def __init__(self, w: SimWorld):
        self.w = w

    def identify(self):
        return "SIM,PiezoSupply"

    def set_output(self, ch, on):
        self.w.advance()
        self.w.on[ch] = bool(on)

    def output(self, ch):
        return self.w.on[ch]

    def set_voltage(self, ch, volts):
        self.w.advance()
        self.w.v_set[ch] = float(volts)

    def voltage_setpoint(self, ch):
        return self.w.v_set[ch]

    def voltage_now(self, ch):
        self.w.advance()
        return self.w.v[ch]

    def current(self, ch):
        self.w.advance()
        return self.w.i[ch]

    def set_slew(self, ch, volts_per_s):
        self.w.advance()
        self.w.slew[ch] = float(volts_per_s)

    def slew(self, ch):
        return self.w.slew[ch]


class SimCap(CapacitanceMeter):
    def __init__(self, w: SimWorld):
        self.w = w

    def identify(self):
        return "SIM,CapacitanceMeter"

    def configure(self, frequency_Hz, excitation_Vrms):
        self.w.frequency, self.w.excitation = float(frequency_Hz), float(excitation_Vrms)

    def read(self):
        w = self.w
        w.advance()
        if w.fault_cap_dead:
            raise IOError("simulated capacitance meter failure")
        n = w.rng.gauss(0.0, w.p["C_noise_fF"] * 1e-3) if w.noise else 0.0
        if w.cal is not None:
            return {"C_pF": w.cal.C_from_force(w.F, w.T) + n, "loss": 1e-5, "V_exc": w.excitation}
        return {"C_pF": w.p["C0_pF"] + w.p["S_pF_N"] * w.F + n, "loss": 1e-5, "V_exc": w.excitation}


class SimPPMS(Cryostat):
    def __init__(self, w: SimWorld):
        self.w = w

    def identify(self):
        return "SIM,PPMS"

    def read(self):
        w = self.w
        w.advance()
        if w.fault_T_dead:
            raise IOError("simulated temperature read failure")
        return {"T": w.T, "T_status": "Stable" if abs(w.T - w.T_set) < 0.05 else "Chasing",
                "B": w.B, "B_status": "Stable" if abs(w.B - w.B_set) < 1e-4 else "Chasing",
                "chamber": w.chamber}

    def set_temperature(self, T, rate_K_min, mode="fast_settle"):
        self.w.advance()
        self.w.T_set, self.w.T_rate = float(T), float(rate_K_min) / 60.0

    def set_field(self, B, rate_T_min, mode="linear", persistent=False):
        self.w.advance()
        self.w.B_set, self.w.B_rate = float(B), float(rate_T_min) / 60.0

    def set_chamber(self, mode):
        w = self.w
        w.advance()
        target = {"seal": "Sealed (condition unknown)", "purge_seal": "Purged and Sealed",
                  "vent_seal": "Vented and Sealed", "pump_continuous": "Pumping Continuously",
                  "vent_continuous": "Flooding Continuously", "high_vacuum": "HiVac"}[mode]
        transit = {"purge_seal": "Performing Purge/Seal", "vent_seal": "Performing Vent/Seal",
                   "high_vacuum": "Pre-HiVac"}.get(mode)
        if transit:
            w.chamber, w.chamber_next, w.chamber_until = transit, target, w.clock.now() + 120.0
        else:
            w.chamber = target


class SimSample(SampleMeter):
    def __init__(self, w: SimWorld):
        self.w = w

    def read(self):
        w = self.w
        w.advance()
        if w.broken:
            return {"R_ohm": float("inf")}
        n = w.rng.gauss(0.0, 1e-5) if w.noise else 0.0
        return {"R_ohm": w.p["R0_ohm"] * (1.0 + w.p["gauge"] * w.F + 1e-3 * w.T_cell + n)}


class SimLockIn(LockIn):
    """Temperaturoszillation der Probe als Bandpass aus Wärmeabfluss und Thermoelement."""

    def __init__(self, w: SimWorld):
        self.w = w
        self.harmonic = 1

    def identify(self):
        return "SIM,LockIn"

    def set_drive(self, frequency_Hz, V_stack_peak):
        self.w.advance()
        self.w.ac_f, self.w.ac_V = float(frequency_Hz), float(V_stack_peak)

    def set_detection(self, harmonic, time_constant_s):
        self.harmonic = int(harmonic)

    def read(self):
        import cmath

        from core.thermocouple import seebeck_type_e
        w = self.w
        w.advance()
        p = w.p
        f, V = w.ac_f, w.ac_V
        z = 0j
        if f > 0 and V > 0 and self.harmonic == 1 and not w.broken:
            g = _lerp(w.T_cell, 45.0 / 140.0, 25.0 / 400.0)
            k_c = _lerp(w.T_cell, 170.0 / 45.0, 100.0 / 25.0)
            k_s = p["sample_k_N_um"]
            F = g * V * k_c * k_s / (k_c + k_s)                 # N
            eps = F / k_s * 1e-6 / (p["sample_length_mm"] * 1e-3)
            Tc = p["Tc_K"] + p["dTc_dB"] * abs(w.B)
            peak = 1.0 / (1.0 + ((w.T_cell - Tc) / p["Tc_width_K"]) ** 2)
            dT = eps * (p["dTdeps_el"] + p["dTdeps_peak"] * peak)
            wt = 2.0 * math.pi * f
            H = 1j * wt * p["tau_i_s"] / (1 + 1j * wt * p["tau_i_s"]) / (1 + 1j * wt * p["tau_th_s"])
            v = dT * seebeck_type_e(w.T_cell) * p["preamp_gain"] * H
            z = (v + 1j * p["pickup_V_per_THz"] * p["preamp_gain"] * abs(w.B) * f * V) / math.sqrt(2.0)
        if w.noise:
            z += complex(w.rng.gauss(0, 2e-8), w.rng.gauss(0, 2e-8))
        return {"X": z.real, "Y": z.imag, "R": abs(z), "theta": math.degrees(cmath.phase(z)), "f": f}


def build_sim(clock, cfg=None, noise=True, seed=None) -> tuple[Instruments, SimWorld]:
    w = SimWorld(clock, cfg, noise=noise, seed=seed)
    return Instruments(SimSupply(w), SimCap(w), SimPPMS(w), SimSample(w), lockin=SimLockIn(w)), w
