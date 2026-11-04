"""Das ganze Programm ohne HTTP: Geräte, Sicherheit, Supervisor, Runner.

Der Server benutzt das, die Tests auch, und für den Trockenlauf baut sich der Stack eine
Kopie mit simulierten Geräten und virtueller Uhr.
"""

from __future__ import annotations

import copy
import time

from instruments import sim

from .calibration import ForceCalibration
from .clock import RealClock, VirtualClock
from .interpreter import Abort, Context, Interpreter, StepError
from .runner import Runner
from .safety import Safety
from .sequence import check_sequence
from .supervisor import Supervisor


class Stack:
    def __init__(self, cfg, inst, clock, simulated: bool, publish=lambda ev: None, cal=None, world=None):
        self.cfg = cfg
        self.inst = inst
        self.clock = clock
        self.simulated = simulated
        self.world = world
        self.publish = publish
        self.dry_tick_s = 5.0
        self.cal = cal or ForceCalibration.load(cfg["paths"]["calibration_dir"] / cfg["capacitance"]["calibration"])
        self.safety = Safety(inst, cfg, persist=not simulated)
        self.supervisor = Supervisor(inst, self.safety, self.cal, cfg, clock, publish)
        self.runner = Runner(self)
        self.supervisor.on_guard_abort = lambda why: self.runner.stop(f"guard: {why}")
        if self.safety.latched:
            self.supervisor.alarms.set("latched", f"Still latched from the last run: {self.safety.latched}.",
                                       latched=True)

    def init_cap(self):
        c = self.cfg["capacitance"]
        self.safety.configure_cap(c["frequency_Hz"], c["excitation_Vrms"])
        a = self.cfg.get("ac", {})
        if self.inst.lockin is not None and a.get("enabled", False) and not self.safety.read_only:
            self.inst.lockin.set_drive(a["f_min_Hz"], 0.0)     # was vor einem Neustart lief, endet hier
            self.inst.lockin.set_detection(a["harmonic"], a["time_constant_s"])

    def stop_all(self) -> dict:
        """Sequenz, Skript und Regler stoppen, Anregung aus, beide Kanäle auf 0 V. Die Ausgänge
        bleiben an, damit die Stapel über das Netzteil auf 0 V liegen. Die Antwort sagt, was
        wirklich geschah: im Nur-Lese-Modus geschieht nichts."""
        sf = self.safety
        self.runner.stop("stop all")
        self.supervisor.force.stop()
        sf.bump_epoch()                  # ein Takt im Flug schreibt seine alten Werte nicht mehr
        sf.ac_off()
        if sf.read_only:
            return {"ok": False, "detail": "read-only mode: no voltage was changed"}
        try:
            sf.guard_set(0.0, 0.0)
            sf.bump_epoch()              # auch ein Takt, der eben vor dem Schreiben begann
            sp = sf.setpoints()
        except Exception as e:
            sf.bump_epoch()
            self.supervisor.alarms.set("guard_write", f"STOP ALL did not reach the supply: {e}")
            self.supervisor._hold_T({"T": sf.T})
            return {"ok": False, "detail": str(e)}
        if any(abs(v) > 1e-3 for v in sp.values()):
            return {"ok": False, "detail": f"setpoints after stop: {sp}"}
        return {"ok": True}

    def ack(self, key=None):
        self.supervisor.alarms.ack(key)
        active = self.supervisor.alarms.active
        if not any(a["latched"] for a in active.values()):
            self.safety.latch(None)
            self.runner.halted = None
            self.safety.halted = None
            self.supervisor.forget_T_rejects()

    def shutdown(self):
        """Programmende: nichts unter Spannung zurücklassen. Beim Aufwärmen ist niemand
        mehr da, der das Fenster nachführt."""
        try:
            self.stop_all()
        except BaseException:
            pass
        self._drain()
        self.supervisor.stop()

    def _drain(self, timeout_s=300.0):
        """Das Programm endet erst, wenn die Spannung unten ist. guard_slew zum Rampen,
        und solange tickt der Guard weiter."""
        if self.clock.virtual or self.safety.read_only:
            return
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            try:
                s = self.inst.supply
                if max(abs(s.voltage_now(c)) for c in ("tension", "compression")) < 0.5:
                    return
            except Exception:
                return
            time.sleep(0.2 / max(self.clock.speed, 1.0))

    def dry_run(self, seq: dict) -> dict:
        errors = check_sequence(seq)
        if errors:
            return {"ok": False, "error": "; ".join(errors), "duration_s": 0.0}
        s = self.supervisor.last
        clock = VirtualClock()
        scfg = {**self.cfg.get("sim", {})}
        if s.get("T") is not None:
            scfg["T_start"] = s["T"]
        inst, w = sim.build_sim(clock, scfg, noise=False)
        cal = copy.deepcopy(self.cal)
        cal._tare_file = None
        w.cal = cal                      # die Sim antwortet mit den Kalibrierdaten der echten Zelle
        if s.get("B") is not None:
            w.B = w.B_set = s["B"]
        if s.get("chamber") is not None:
            w.chamber = s["chamber"]
        for c, n in (("tension", "T"), ("compression", "C")):
            if s.get(f"set_{n}") is not None:
                w.v[c] = w.v_set[c] = s[f"set_{n}"]
            w.on[c] = bool(s.get(f"on_{n}"))
        if self.safety.ac_V:
            w.ac_f, w.ac_V = self.safety.ac_f, self.safety.ac_V
        lim_cfg = copy.deepcopy({k: v for k, v in self.cfg.items() if k != "paths"})
        lim_cfg["paths"] = self.cfg["paths"]
        lim_cfg["_dry"] = True
        st = Stack(lim_cfg, inst, clock, self.simulated, cal=cal, world=w)
        st.safety.expect_on = self.safety.expect_on
        st.safety.outputs_seen = True
        st.safety.ac_f, st.safety.ac_V = self.safety.ac_f, self.safety.ac_V
        st.supervisor.tick()
        ctx = Context(st)
        msgs = []
        ctx.log_fn = msgs.append
        try:
            Interpreter(ctx).run(seq["steps"])
            alarms = [a["text"] for a in st.supervisor.alarms.history]
            return {"ok": not alarms, "error": "; ".join(alarms), "duration_s": clock.now(),
                    "points": ctx.points, "log": msgs[-200:]}
        except (StepError, Abort) as e:
            return {"ok": False, "error": str(e), "duration_s": clock.now(), "points": ctx.points,
                    "log": msgs[-200:]}


def build(cfg, publish=lambda ev: None, speed=None, virtual=False, noise=True, seed=None) -> Stack:
    if cfg["mode"].get("simulate", True):
        if virtual:
            clock = VirtualClock()
        else:
            clock = RealClock(speed if speed is not None else cfg["mode"].get("sim_speed", 1.0))
        inst, w = sim.build_sim(clock, cfg.get("sim", {}), noise=noise, seed=seed)
        return Stack(cfg, inst, clock, True, publish, world=w)
    from instruments import real
    return Stack(cfg, real.build_real(cfg), RealClock(1.0), False, publish)
