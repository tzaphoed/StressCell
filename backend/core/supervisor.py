"""Abfrage, Guard und Kraftregler in einem Takt.

tick() liest alle Geräte, rechnet die Kraft, setzt die Grenzen durch und macht den
nächsten Regelschritt. Live läuft das in einem eigenen Thread, im Trockenlauf ruft der
Sequenzer es selbst auf. Beide Wege führen also denselben Guard-Code aus.

Der Guard bildet in einem Durchgang EIN Ziel und schreibt es einmal. Mehrere Schreiber im
selben Takt würden sich gegenseitig überschreiben, und der letzte hätte das Fenster nicht
mehr im Blick. Zuletzt wird immer auf das Fenster geklemmt.

  arc             Strom über der Grenze: Anregung aus, beide Ausgänge aus, gelatcht.
  sample_break    Kraft fällt, ohne dass der Drive das erklärt: auf 0 V, gelatcht.
  supply_lost     Netzteil antwortet nicht: Aufwärmen anhalten, denn fallen kann die Spannung nicht.
  overforce       |F| über der Grenze: vom Istwert aus zurück, nicht vom Sollwert.
  voltage_window  Ziel außerhalb des Fensters: klemmen. Greift beim Aufwärmen, bei ausgefallenem
                  Thermometer (dann 300-K-Fenster) und bei unsicherem Kammerzustand.
  stale           Kraft zu alt: Regler aus, Drive bleibt stehen.

Greift der Guard ein, wird eine laufende Sequenz abgebrochen, und weitere Schritte sind
gesperrt, bis jemand quittiert. Das geschieht einmal je Ereignis: Bleibt die Ursache bestehen,
etwa thermische Kraft bei D = 0, muss man nach dem Quittieren handeln können. Schlägt ein Abschaltbefehl fehl, kommen Alarm und Abbruch
trotzdem, und das Aufwärmen wird angehalten.

Der Watchdog läuft in einem eigenen Thread. Hängt die Abfrage, weiß niemand mehr,
wie warm die Zelle ist, und er fährt auf das 300-K-Fenster zurück.
"""

from __future__ import annotations

import csv
import logging
import math
import threading
import time
from datetime import datetime

import numpy as np

from instruments.base import CHANNELS

from . import fc100
from .control import ForceLoop
from .thermocouple import seebeck_type_e

log = logging.getLogger("stresscell.supervisor")

# Statusmeldungen von MultiVu, bei denen der Temperaturwert nichts taugt
BAD_T_STATUS = {"General Failure", "Diagnostic", "Impedance Control Error"}

LOG_COLUMNS = ("wall", "T", "B", "chamber", "C_pF", "loss", "F", "stress_MPa", "V_T", "V_C",
               "I_T", "I_C", "D_set", "vmin", "vmax")


class Alarms:
    def __init__(self, publish):
        self.active: dict[str, dict] = {}
        self.publish = publish
        self.history: list[dict] = []

    def set(self, key, text, latched=False):
        if key in self.active:
            self.active[key]["text"] = text      # Zahlen im Text dürfen laufen, ohne das Log zu fluten
        else:
            a = {"key": key, "text": text, "latched": latched, "wall": time.time()}
            self.active[key] = a
            self.history.append(a)
            log.warning("alarm %s: %s", key, text)
            self.publish({"type": "alarms", "active": list(self.active.values())})

    def clear(self, key):
        a = self.active.get(key)
        if a and not a["latched"]:
            del self.active[key]
            self.publish({"type": "alarms", "active": list(self.active.values())})

    def ack(self, key=None):
        for k in list(self.active):
            if key in (None, k):
                del self.active[k]
        self.publish({"type": "alarms", "active": list(self.active.values())})


class Supervisor:
    def __init__(self, inst, safety, cal, cfg, clock, publish=lambda ev: None):
        self.inst = inst
        self.safety = safety
        self.cal = cal
        self.cfg = cfg
        self.lim = cfg["limits"]
        self.clock = clock
        self.publish = publish
        self.alarms = Alarms(publish)
        self.force = ForceLoop(cfg["force_loop"])
        self.area = cfg["sample"]["area_mm2"]
        self.last: dict = {}
        self.history: list[dict] = []
        self.on_guard_abort = lambda reason: None
        self.G_table: list[tuple[float, float]] = []   # (T, dF/dD) aus measure_gain
        self._T_prev = None
        self._F_prev = None
        self._D_prev = None
        self._trip = 0.0
        self._T_rej: list[float] = []
        self._T_held = False
        self._edges: set[str] = set()       # Ursachen, die schon einen Abbruch ausgelöst haben
        self._noise2 = cfg["limits"]["force_noise_N"] ** 2
        self._noise_n = 0
        self._break_pending = None
        self._last_supply_ok = None
        self._last_T_ok = None
        self._last_F_ok = None
        self._last_chamber_ok = None
        self._last_log = None
        self._log_file = None
        self._log_day = None
        self._stop = threading.Event()
        self._thread = None
        self._wd_thread = None
        self._tick_wall = None
        self.tick_lock = threading.Lock()

    def tare(self, C_pF, T, persist=True):
        """Nullpunkt setzen. Der Sprung im Kraftwert ist dann kein Probenbruch."""
        with self.tick_lock:
            self._F_prev = None
            return self.cal.tare(C_pF, T, persist)

    def G_at(self, T):
        """dF/dD bei T, nur in der Nähe eines gemessenen Punkts. G ändert sich mit T um
        den Faktor 5, ein alter Wert wäre im Sweep schlicht falsch."""
        if T is None or not self.G_table:
            return None
        pts = sorted(self.G_table)
        lo, hi = pts[0][0] - 2.0, pts[-1][0] + 2.0
        if not lo <= T <= hi:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return float(np.interp(T, xs, ys))

    # ── Takt ─────────────────────────────────────────────────────────────
    def tick(self) -> dict:
        with self.tick_lock:
            epoch = self.safety.epoch          # vor dem Lesen: was danach gestoppt wird, gilt
            s = self._read()
            try:
                self._update_safety(s)
                self._guard(s, epoch)
                self._control(s, epoch)
            except Exception as e:              # der Takt läuft weiter, Alarm und Abbruch auch
                log.exception("guard failed")
                self.alarms.set("guard_error", f"Guard error: {e}")
                self._abort("guard error")
            self.last = s
            self._remember(s)
            self._tick_wall = time.monotonic()
            self.alarms.clear("watchdog")
            return s

    def _try(self, fn, name):  # noqa: D401
        try:
            return fn()
        except Exception as e:
            log.debug("%s: %s", name, e)
            return None

    def _read(self) -> dict:
        now = self.clock.now()
        s = {"t": now, "wall": self.clock.wall(), "expect_on": self.safety.expect_on}
        cr = self._try(self.inst.cryo.read, "cryo") or {}
        s.update({k: cr.get(k) for k in ("T", "T_status", "B", "B_status", "chamber")})
        cp = self._try(self.inst.cap.read, "cap") or {}
        s.update({"C_pF": cp.get("C_pF"), "loss": cp.get("loss"), "V_exc": cp.get("V_exc")})
        sup = self.inst.supply
        names = {"tension": "T", "compression": "C"}
        for c, n in names.items():
            s[f"V_{n}"] = self._try(lambda: sup.voltage_now(c), "V")
            s[f"set_{n}"] = self._try(lambda: sup.voltage_setpoint(c), "set")
            s[f"I_{n}"] = self._try(lambda: sup.current(c), "I")
            s[f"on_{n}"] = self._try(lambda: sup.output(c), "out")
        s["F"] = None
        if self.inst.sample is not None:
            sm = self._try(self.inst.sample.read, "sample") or {}
            s["sample"] = sm
        if self.inst.lockin is not None:
            li = self._try(self.inst.lockin.read, "lockin") or {}
            s.update({"X": li.get("X"), "Y": li.get("Y"), "R": li.get("R"), "theta": li.get("theta")})
            s["f_ac"], s["V_ac"] = self.safety.ac_f, self.safety.ac_V
        if s["set_T"] is not None and s["set_C"] is not None:
            s["D_set"] = s["set_T"] - s["set_C"]
        else:
            s["D_set"] = None
        return s

    def _elastocaloric(self, s) -> dict:
        """Temperaturamplitude aus der Thermospannung. Die Kraftamplitude lässt sich am FC100 nicht
        direkt lesen, das Kapazitätsgerät ist zu langsam: sie kommt aus dF/dD (op measure_gain)."""
        out = {"T_ac_K": None, "F_ac_N": None, "dT_dsigma_K_MPa": None}
        if s.get("R") is None or s.get("T") is None or not s.get("V_ac"):
            return out
        gain = self.cfg.get("ac", {}).get("preamp_gain", 1.0)
        out["T_ac_K"] = math.sqrt(2.0) * s["R"] / gain / seebeck_type_e(s["T"])
        G = self.G_at(s["T"])
        if G:
            out["F_ac_N"] = G * s["V_ac"]
            out["dT_dsigma_K_MPa"] = out["T_ac_K"] / (out["F_ac_N"] / self.area)
        return out

    def _sane_T(self, s):
        """Eine Temperatur, die nicht stimmen kann, wäre schlimmer als gar keine: T = 0 würde
        das volle Fenster öffnen. Darum zählt sie als fehlend."""
        T, now = s.get("T"), s["t"]
        if T is None:
            return None
        ok = isinstance(T, (int, float)) and math.isfinite(T) and 1.0 <= T <= 400.0 \
            and s.get("T_status") not in BAD_T_STATUS
        if ok and self._T_prev is not None:
            # Die Lücke zählt nur bis T_stale_s: sonst erlaubte eine lange Pause jeden Wert
            allowed = self.lim["T_jump_K"] + 0.5 * min(now - self._T_prev[0], self.lim["T_stale_s"])
            if abs(T - self._T_prev[1]) > allowed:
                self._T_rej.append(T)
                # Nach oben darf ein stehender Wert gelten, das Fenster wird dadurch enger.
                # Nach unten nie von selbst: das öffnete es (quittieren setzt zurück).
                if T > self._T_prev[1] and len(self._T_rej) >= 5 \
                        and max(self._T_rej[-5:]) - min(self._T_rej[-5:]) < 1.0:
                    self._T_rej.clear()
                    return T
                return None
        if ok:
            self._T_rej.clear()
            return T
        return None

    def _update_safety(self, s):
        sf = self.safety
        now = s["t"]
        s["T_raw"] = s.get("T")
        s["T"] = self._sane_T(s)
        s["F"] = self.cal.force_from_C(s["C_pF"], s["T"])
        s["stress_MPa"] = s["F"] / self.area if s["F"] is not None else None
        if s["T"] is not None:
            if self._T_prev is not None and now > self._T_prev[0]:
                dt = now - self._T_prev[0]
                raw = (s["T"] - self._T_prev[1]) / dt
                sf.dTdt += (raw - sf.dTdt) * 0.5
                tau = max(self.lim["T_cell_tau_s"], 1e-3)
                sf.T_slow += (s["T"] - sf.T_slow) * (1.0 - math.exp(-dt / tau))
            elif sf.T_slow is None:
                sf.T_slow = s["T"]
            self._T_prev = (now, s["T"])
            self._last_T_ok = now
            sf.T = s["T"]
            sf.state_known = True
        elif self._last_T_ok is not None and now - self._last_T_ok > self.lim["T_stale_s"]:
            sf.T = None
        # Eine einzelne verlorene Antwort darf nicht das Fenster umwerfen, erst ein alter Wert
        if s.get("chamber") is not None:
            sf.chamber = s["chamber"]
            self._last_chamber_ok = now
            sf.chamber_hold = max(0, sf.chamber_hold - 1)
        elif self._last_chamber_ok is not None and now - self._last_chamber_ok > self.lim["T_stale_s"]:
            sf.chamber = None
        vmin, vmax = sf.window()
        s["vmin"], s["vmax"] = vmin, vmax
        s["T_eff"] = sf.T_eff()
        s["chamber_ok"] = sf.chamber_ok()
        if s["F"] is not None:
            self._last_F_ok = now
        if s["set_T"] is not None and s["set_C"] is not None:
            self._last_supply_ok = now
        if not sf.outputs_seen and s["on_T"] is not None and s["on_C"] is not None:
            sf.outputs_seen = True
            sf.expect_on = sf.expect_on or bool(s["on_T"] and s["on_C"])
        if self.inst.lockin is not None:
            s.update(self._elastocaloric(s))

    # ── Guard ────────────────────────────────────────────────────────────
    def _abort(self, why):
        self.force.stop()
        self.on_guard_abort(why)

    def _note(self, key, text, **kw):
        if self.safety.read_only:
            text += " READ-ONLY MODE: nothing is being done about it."
        self.alarms.set(key, text, **kw)

    def _edge(self, key, active, text, why):
        """Bedingung, die andauern kann: Alarm solange sie besteht, Abbruch nur beim Eintritt."""
        if active:
            self._note(key, text)
            if key not in self._edges:
                self._edges.add(key)
                self._abort(why)
        else:
            self._edges.discard(key)
            self.alarms.clear(key)

    def forget_T_rejects(self):
        """Nach dem Quittieren: wer hingesehen hat, darf einen Temperatursprung bestätigen."""
        self._T_prev = None
        self._T_rej.clear()

    def _guard(self, s, epoch):
        sf, lim, al = self.safety, self.lim, self.alarms
        now = s["t"]
        if s["T"] is None and sf.T is None:
            self._note("stale_T", "No valid temperature: voltage limited to the 300 K window.")
        else:
            al.clear("stale_T")
        if abs(sf.dTdt) * 60.0 > lim["T_rate_max"] * 1.2:
            al.set("T_rate", f"Temperature changes at {abs(sf.dTdt)*60:.1f} K/min, datasheet limit "
                             f"{lim['T_rate_max']:g} K/min.")
        else:
            al.clear("T_rate")
        if s.get("V_exc") is not None and s["V_exc"] > lim["excitation_max_Vrms"]:
            al.set("excitation", f"Capacitance excitation {s['V_exc']:.2f} V rms above the cell limit.")
        else:
            al.clear("excitation")
        lost = s["expect_on"] and sf.expect_on and (s["on_T"] is False or s["on_C"] is False)
        self._edge("output_lost", lost, "A supply output went off by itself.", "output lost")
        if sf.ac_fault:
            al.set("ac_fault", f"AC drive may still be running: {sf.ac_fault}")
            sf.ac_off()                                  # weiter versuchen
        else:
            al.clear("ac_fault")

        # Lichtbogen: Strom, den die Rampe nicht erklärt. Ein Zähler mit Abfall, damit auch
        # Überschläge in jedem zweiten Takt auffallen.
        cur = [abs(s[k]) for k in ("I_T", "I_C") if s[k] is not None]
        worst = max(cur, default=0.0)
        self._trip = self._trip + 1.0 if worst > lim["current_trip_A"] else max(0.0, self._trip - 0.25)
        if self._trip >= lim["current_trip_ticks"] and "arc" not in al.active:
            self._trip = 0.0
            sf.latch("arc or short suspected")
            self._note("arc", f"Current {worst*1e3:.2f} mA: arc or short suspected, outputs off. "
                              "Inspect wiring before acknowledging.", latched=True)
            self._abort("arc")
            if not sf.read_only:
                try:
                    sf.output_off(force=True)
                except Exception as e:
                    self._write_failed(s, e)
            return
        if "arc" in al.active and not sf.read_only and (s["on_T"] or s["on_C"]):
            try:
                sf.output_off(force=True)                # der erste Versuch kam nicht an
            except Exception as e:
                self._write_failed(s, e)

        target, slew = None, None

        # Probe gerissen: |F| fällt, der Drive erklärt das nicht, und es bleibt so. Zwei Takte,
        # damit ein Rauschausreißer nichts latcht. Schwelle mit dem gemessenen Rauschen.
        F, Fp, Dp = s["F"], self._F_prev, self._D_prev
        Dn = s["V_T"] - s["V_C"] if s["V_T"] is not None and s["V_C"] is not None else None
        pend = self._break_pending
        self._break_pending = None
        if F is not None and Fp is not None and Dn is not None and Dp is not None:
            sigma = math.sqrt(self._noise2)
            need = max(lim["force_step_trip_N"], lim["force_step_frac"] * abs(Fp), 6.0 * sigma)
            dF = F - Fp
            if abs(Dn - Dp) < 0.1:
                self._noise_n += 1
                rate = 0.2 if self._noise_n < 50 else 0.02       # anfangs schnell lernen
                self._noise2 += rate * (min(dF * dF, 100.0 * self._noise2) / 2.0 - self._noise2)
            if pend is not None and abs(F) <= abs(pend[0]) - pend[1] and "sample_break" not in al.active:
                sf.latch("sample failure suspected")
                self._note("sample_break", f"Force fell from {pend[0]:.2f} N to {F:.2f} N, more than the "
                                           "drive explains: sample or epoxy failure suspected, ramping "
                                           "to 0 V.", latched=True)
                self._abort("sample failure")
            elif pend is None and abs(Fp) - abs(F) > need \
                    and abs(dF) > lim["G_ceiling_N_V"] * abs(Dn - Dp) + need:
                self._break_pending = (Fp, need)
        # Nach einer Lücke gibt es keinen Vergleich: Kraft und Drive müssen vom selben Takt stammen
        self._F_prev, self._D_prev = (F, Dn) if F is not None and Dn is not None else (None, None)
        if "sample_break" in al.active:
            target, slew = (0.0, 0.0), lim["slew_max_V_s"]

        # Netzteil: erst nach stale_s ernst, eine verlorene Antwort bricht nichts ab
        vt, vc = s["set_T"], s["set_C"]
        if vt is None or vc is None:
            if self._last_supply_ok is not None and now - self._last_supply_ok > lim["stale_s"]:
                self._edge("supply_lost", True, "No answer from the piezo supply. It holds its last "
                                                "voltage: stopping the temperature change.", "supply lost")
                self._hold_T(s)
            return
        self._edge("supply_lost", False, "", "")
        self._T_held = False

        # Kraftgrenze: vom Istwert aus, sonst läuft die Rampe mit Guard-Tempo weiter in die Überkraft
        over = F is not None and abs(F) > lim["force_max_N"]
        self._edge("overforce", over, f"|F| = {abs(F or 0.0):.2f} N above {lim['force_max_N']} N.", "force limit")
        if over and target is None and Dn is not None:
            Dset = vt - vc
            if F * (Dset - Dn) < 0:
                pass                       # die laufende Rampe baut die Kraft schon ab, auch nach STOP
            elif F * Dn > 0 or abs(Dset - Dn) > 0.1:
                # Drive erzeugt die Kraft: zurück. Sonst nur eine Rampe anhalten, die sie vergrößert.
                # Bei thermischer Kraft und D = 0 gibt es nichts zu tun: nicht gegenschieben.
                D = 0.9 * Dn if F * Dn > 0 else Dn
                if getattr(sf.inst.supply, "compression_only", False):
                    target = (0.0, -D)
                else:
                    cm = (s["V_T"] + s["V_C"]) / 2.0
                    target = (cm + D / 2.0, cm - D / 2.0)
                slew = lim["slew_default_V_s"]

        # Spannungsfenster, immer zuletzt
        vmin, vmax = s["vmin"], s["vmax"]
        if not sf.ac_peak_ok(vmin, vmax, {"tension": vt, "compression": vc}):
            self._note("ac_window", "DC plus AC peak left the voltage window: AC drive switched off.")
            sf.ac_off()
            self._abort("AC peak outside the window")
        base = target if target is not None else (vt, vc)
        nt, nc = min(max(base[0], vmin), vmax), min(max(base[1], vmin), vmax)
        # Winziger Überstand am Rand (Rundung beim Schreiben) ist kein Ereignis
        clipped = max(abs(nt - base[0]), abs(nc - base[1])) > 0.01
        self._edge("voltage_window", clipped, f"Voltage outside the window {vmin:.1f} ... {vmax:.1f} V: "
                                              "ramping back.", "voltage window")
        if clipped:
            slew = lim["guard_slew_V_s"]
        if target is not None or clipped:
            try:
                sf.guard_set(nt, nc, slew, epoch)
            except Exception as e:
                self._write_failed(s, e)

        # Kraftsignal ausgefallen, Regler darf nicht blind weiterfahren
        if self.force.enabled and (self._last_F_ok is None or now - self._last_F_ok > lim["stale_s"]):
            al.set("stale_F", "No force reading: force loop stopped, drive held.")
            self._abort("force reading lost")
        elif s["F"] is not None:
            al.clear("stale_F")

    def _write_failed(self, s, e):
        log.error("guard write failed: %s", e)
        self.alarms.set("guard_write", f"Guard could not reach the supply: {e}. "
                                       "Stopping the temperature change.")
        self._abort("guard write failed")
        self._hold_T(s)

    def _hold_T(self, s):
        """In einem eigenen Thread: MultiVu kennt keinen Timeout, der Takt darf daran nicht hängen."""
        T = s.get("T") or self.safety.T or (self._T_prev[1] if self._T_prev else None)
        if self._T_held or T is None:
            return
        self._T_held = True

        def hold():
            try:
                self.safety.hold_temperature(T)
            except Exception:
                log.exception("could not hold the temperature")
                self._T_held = False           # beim nächsten Takt wieder versuchen
        if self.clock.virtual:
            hold()
        else:
            threading.Thread(target=hold, name="hold-T", daemon=True).start()

    # ── Regelung ─────────────────────────────────────────────────────────
    def _control(self, s, epoch):
        fl, sf = self.force, self.safety
        if not fl.enabled or s["D_set"] is None or epoch != sf.epoch:
            return
        Dnow = (s["V_T"] or 0.0) - (s["V_C"] or 0.0)
        D = fl.step(s["F"], s["D_set"], Dnow, math.sqrt(self._noise2))
        if fl.fault:
            self._note("force_loop", f"{fl.fault} Force loop stopped, drive held.")
            self._abort("force loop")
            return
        if D is None:
            return
        vmin, vmax = sf.dc_window()
        lim = vmax - vmin
        if abs(D) > lim:
            self._note("force_loop", f"Force loop reached the voltage window at |D| = {lim:.0f} V "
                                     f"with {fl.error:.2f} N still to go.")
            self._abort("force loop saturated")
            return
        try:
            sf.set_drive(D, epoch=epoch)
        except Exception as e:
            self._note("force_loop", f"Force loop stopped: {e}")
            self._abort("force loop")

    # ── Verlauf ──────────────────────────────────────────────────────────
    def _remember(self, s):
        row = {k: s.get(k) for k in ("t", "wall", "T", "B", "F", "stress_MPa", "C_pF", "V_T", "V_C",
                                     "I_T", "I_C", "D_set", "vmin", "vmax", "chamber", "chamber_ok", "T_raw",
                                     "T_status", "B_status", "on_T", "on_C", "set_T", "set_C", "T_eff",
                                     "sample", "X", "Y", "R", "theta", "f_ac", "V_ac", "T_ac_K",
                                     "F_ac_N", "dT_dsigma_K_MPa")}
        row["force"] = self.force.info()
        self.publish({"type": "monitor", "data": row})
        self.history.append(row)
        keep = int(self.cfg["monitor"]["history_s"] / self.cfg["monitor"]["tick_s"])
        if len(self.history) > keep * 1.2:
            del self.history[:len(self.history) - keep]
        if self.clock.virtual:
            return
        if self._last_log is None or s["t"] - self._last_log >= self.cfg["monitor"]["log_interval_s"]:
            self._last_log = s["t"]
            self._log_row(s)

    def _log_row(self, s):
        day = datetime.now().strftime("%Y-%m-%d")
        if day != self._log_day:
            if self._log_file:
                self._log_file.close()
            p = self.cfg["paths"]["log_dir"] / "cell" / f"{day}.csv"
            new = not p.exists()
            self._log_file = open(p, "a", newline="", encoding="utf-8")
            self._log_day = day
            self._csv = csv.writer(self._log_file)
            if new:
                self._csv.writerow(LOG_COLUMNS)
        self._csv.writerow(["" if s.get(k) is None else s.get(k) for k in LOG_COLUMNS])
        self._log_file.flush()

    # ── Thread ───────────────────────────────────────────────────────────
    def start(self):
        self._stop.clear()
        self._tick_wall = time.monotonic()
        self._thread = threading.Thread(target=self._loop, name="supervisor", daemon=True)
        self._thread.start()
        self._wd_thread = threading.Thread(target=self._watchdog, name="watchdog", daemon=True)
        self._wd_thread.start()

    def _loop(self):
        dt = self.cfg["monitor"]["tick_s"]
        while not self._stop.is_set():
            t0 = self.clock.now()
            try:
                self.tick()
            except BaseException:                # auch SystemExit: dieser Thread darf nicht sterben
                log.exception("tick failed")
            self.clock.sleep(max(0.0, dt - (self.clock.now() - t0)))

    def _watchdog(self):
        limit = float(self.cfg["monitor"].get("watchdog_s", 60.0))
        while not self._stop.is_set():
            time.sleep(1.0)
            stalled = time.monotonic() - (self._tick_wall or time.monotonic())
            if stalled > limit:
                self.alarms.set("watchdog", f"Monitor stalled for {stalled:.0f} s: "
                                            "limiting voltage to the 300 K window.")
                self.force.enabled = False      # kein tick_lock: der könnte es sein, der hängt
                self.safety.T = None
                try:
                    vmin, vmax = fc100.voltage_window(None)
                    self.safety.ac_off()
                    sp = self.safety.setpoints()
                    nt = min(max(sp["tension"], vmin), vmax)
                    nc = min(max(sp["compression"], vmin), vmax)
                    if nt != sp["tension"] or nc != sp["compression"]:
                        self.safety.guard_set(nt, nc)
                except Exception:
                    log.exception("watchdog could not reach the supply")
                self.on_guard_abort("watchdog")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._log_file:
            self._log_file.close()
            self._log_file = None
            self._log_day = None
