"""Ausführung der Sequenzschritte. Derselbe Code für Lauf, Trockenlauf und Python-Client."""

from __future__ import annotations

import ast
import csv
import math
import operator
import threading
import time

import numpy as np

from instruments.base import CHANNELS

from .safety import LimitError
from .sequence import OPS


class Abort(Exception):
    pass


class StepError(Exception):
    pass


_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod}
_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}
_FN = {"min": min, "max": max, "abs": abs, "log": math.log, "log10": math.log10, "exp": math.exp,
       "sqrt": math.sqrt}


def _eval(n, names):
    if isinstance(n, ast.Expression):
        return _eval(n.body, names)
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
        return n.value
    if isinstance(n, ast.Name):
        if n.id in names:
            return names[n.id]
        raise StepError(f"unknown variable {n.id}")
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
        v = _eval(n.operand, names)
        return -v if isinstance(n.op, ast.USub) else v
    if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
        a, b = _eval(n.left, names), _eval(n.right, names)
        if isinstance(n.op, ast.Pow) and (abs(b) > 16 or abs(a) > 1e6):
            raise StepError("power too large")        # 9**9**9 hielte sonst Guard und Watchdog an
        return _BIN[type(n.op)](float(a), float(b))
    if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in _CMP:
        return _CMP[type(n.ops[0])](_eval(n.left, names), _eval(n.comparators[0], names))
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FN and not n.keywords:
        return _FN[n.func.id](*[_eval(a, names) for a in n.args])
    raise StepError("expression not allowed")


class Context:
    """Laufzeit einer Ausführung: Variablen, Datei, Pause, Abbruch."""

    def __init__(self, stack, data_file=None, log_fn=None):
        self.st = stack
        self.clock = stack.clock
        self.sup = stack.supervisor
        self.safety = stack.safety
        self.vars: dict = {}
        self.abort = threading.Event()
        self.abort_reason = ""
        self.resume = threading.Event()
        self.resume.set()
        self.log_fn = log_fn or (lambda m: None)
        self.data_file = data_file
        self.writer = None
        self.columns = None
        self.points = 0
        self.defaults = stack.cfg.get("defaults", {})

    # ── Zeit ─────────────────────────────────────────────────────────────
    def check(self):
        if self.abort.is_set():
            raise Abort(self.abort_reason or "stopped")
        while not self.resume.is_set():
            if self.abort.wait(0.1):
                raise Abort(self.abort_reason or "stopped")

    def sleep(self, seconds: float):
        tick = self.st.cfg["monitor"]["tick_s"]
        if self.clock.virtual:
            step = self.st.dry_tick_s
            end = self.clock.now() + seconds
            while True:
                self.check()
                left = end - self.clock.now()
                if left <= 1e-9:
                    return
                self.clock.sleep(min(step, left))
                self.sup.tick()
        end = self.clock.now() + seconds
        while True:
            self.check()
            left = end - self.clock.now()
            if left <= 0:
                return
            self.clock.sleep(min(left, tick / 2))

    def snapshot(self) -> dict:
        return self.sup.last

    def fresh(self) -> dict:
        """Ein Messwert, der nach dem Aufruf entstanden ist."""
        if self.clock.virtual:
            return self.sup.tick()
        t0 = self.sup.last.get("t", -1)
        for _ in range(100):
            self.sleep(self.st.cfg["monitor"]["tick_s"] / 2)
            if self.sup.last.get("t", -1) > t0:
                return self.sup.last
        raise StepError("no fresh reading: the monitor is not running")

    # ── Werte ────────────────────────────────────────────────────────────
    def names(self) -> dict:
        s = self.snapshot()
        live = {"T": s.get("T"), "B": s.get("B"), "F": s.get("F"), "C": s.get("C_pF"), "D": s.get("D_set")}
        return {**{k: v for k, v in live.items() if v is not None}, **self.vars}

    def val(self, x) -> float:
        if isinstance(x, bool):
            return x
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, str):
            try:
                v = float(_eval(ast.parse(x, mode="eval"), self.names()))
            except (SyntaxError, ArithmeticError, ValueError, TypeError) as e:
                raise StepError(f"bad expression {x!r}: {e}")
            if not math.isfinite(v):
                raise StepError(f"expression {x!r} is not finite")
            return v
        raise StepError(f"bad value {x!r}")

    def text(self, t: str) -> str:
        out = str(t)
        for k, v in self.vars.items():
            out = out.replace("{" + k + "}", f"{v:g}" if isinstance(v, float) else str(v))
        return out

    def log(self, msg: str):
        self.log_fn(msg)

    # ── Daten ────────────────────────────────────────────────────────────
    def record(self, s: dict, label: str):
        row = {"wall": s.get("wall"), "t": s.get("t"), "label": label, "C_zero_pF": self.st.cal.C_zero}
        for k in ("T", "B", "F", "stress_MPa", "C_pF", "loss", "V_exc", "V_T", "V_C", "I_T", "I_C", "D_set",
                  "f_ac", "V_ac", "X", "Y", "R", "theta", "T_ac_K", "F_ac_N", "dT_dsigma_K_MPa",
                  "phi_RC_deg", "theta_stack", "dT_dc_K", "G_used"):
            row[k] = s.get(k)
        row.update(s.get("sample") or {})
        row.update({f"var_{k}": v for k, v in self.vars.items() if isinstance(v, (int, float))})
        self.points += 1
        self.st.publish({"type": "point", "data": {k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                                                   for k, v in row.items()}})
        if self.data_file is None:
            return
        if self.writer is None:
            self.columns = list(row)
            self._open(new=True)
        elif any(k not in self.columns for k in row):
            self._grow(row)
        self.writer.writerow({k: "" if v is None else v for k, v in row.items()})
        self._fh.flush()

    def _open(self, new):
        self._fh = open(self.data_file, "a", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self._fh, fieldnames=self.columns, extrasaction="ignore")
        if new:
            self.writer.writeheader()

    def _grow(self, row):
        """Eine neue Spalte, etwa eine Variable aus einer Schleife: Datei neu schreiben,
        sonst fiele sie stillschweigend weg."""
        self._fh.close()
        with open(self.data_file, newline="", encoding="utf-8") as f:
            old = list(csv.DictReader(f))
        self.columns += [k for k in row if k not in self.columns]
        with open(self.data_file, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.columns)
            w.writeheader()
            w.writerows(old)
        self._open(new=False)

    def close(self):
        if self.writer is not None:
            self._fh.close()
            self.writer = None


class Interpreter:
    def __init__(self, ctx: Context):
        self.c = ctx
        self.lim = ctx.st.cfg["limits"]

    def run(self, steps):
        for st in steps:
            self.step(st)

    def step(self, st: dict):
        self.c.check()
        op = st.get("op")
        if op not in OPS:
            raise StepError(f"unknown op {op!r}")
        try:
            return getattr(self, "op_" + op)(st)
        except LimitError as e:
            raise StepError(f"{op}: {e}") from e

    # ── Kryostat ─────────────────────────────────────────────────────────
    def op_set_temperature(self, st):
        c = self.c
        rate = c.val(st.get("rate", self.lim["T_rate_max"] / 2))
        c.safety.set_temperature(c.val(st["T"]), rate, st.get("mode", "fast_settle"))
        c.vars["_T_set"] = c.val(st["T"])

    def _wait(self, done, timeout, what, hold=0.0):
        c = self.c
        tick = c.st.cfg["monitor"]["tick_s"]
        if timeout > self.lim["wait_max_s"]:
            raise StepError(f"timeout above {self.lim['wait_max_s']} s")
        t0 = c.clock.now()
        since = None
        while True:
            c.sleep(tick)
            now = c.clock.now()
            if done(c.snapshot()):
                since = now if since is None else since
                if now - since >= hold:
                    return
            else:
                since = None
            if now - t0 > timeout:
                raise StepError(f"{what}: timeout after {timeout:g} s")

    def op_wait_temperature(self, st):
        c = self.c
        tol, hold = c.val(st.get("tol", 0.1)), c.val(st.get("hold", 30))
        target = c.vars.get("_T_set")
        if target is None:
            raise StepError("wait_temperature without a preceding set_temperature")

        def done(s):
            return s.get("T") is not None and abs(s["T"] - target) <= tol and s.get("T_status") == "Stable"
        self._wait(done, c.val(st.get("timeout", 14400)), "wait_temperature", hold)

    def op_set_field(self, st):
        c = self.c
        B = c.val(st["B"])
        c.safety.set_field(B, c.val(st.get("rate", self.lim["B_rate_max"])), st.get("persistent", False),
                           st.get("mode", "linear"))
        c.vars["_B_set"] = B

    def op_wait_field(self, st):
        c = self.c
        target = c.vars.get("_B_set")
        if target is None:
            raise StepError("wait_field without a preceding set_field")
        tol = c.val(st.get("tol", 1e-3))

        def done(s):
            return s.get("B") is not None and abs(s["B"] - target) <= tol \
                and str(s.get("B_status")).startswith(("Stable", "Holding"))
        self._wait(done, c.val(st.get("timeout", 7200)), "wait_field")

    def op_set_chamber(self, st):
        self.c.safety.set_chamber(st["mode"])

    # ── Netzteil ─────────────────────────────────────────────────────────
    def op_output(self, st):
        c = self.c
        if st["on"] not in (True, False):
            raise StepError("output: on must be true or false")
        if st["on"]:
            c.safety.output_on()
        else:
            c.sup.force.stop()
            c.safety.output_off()

    def op_set_voltages(self, st):
        c = self.c
        vt, vc = c.safety.check_voltages(c.val(st["tension"]), c.val(st["compression"]))
        self._manual_drive()                 # erst nach der Prüfung: ein Tippfehler lässt den Regler an
        c.safety.set_voltages(vt, vc, c.val(st["slew"]) if "slew" in st else None)

    def op_set_drive(self, st):
        c = self.c
        D = c.val(st["D"])
        c.safety.check_drive(D)
        self._manual_drive()
        c.safety.set_drive(D, c.val(st["slew"]) if "slew" in st else None)

    def _manual_drive(self):
        if self.c.sup.force.enabled:
            self.c.sup.force.stop()
            self.c.log("force loop released: drive set by hand")

    def op_wait_voltage(self, st):
        c = self.c

        def done(s):
            return all(s.get(a) is not None and s.get(b) is not None and abs(s[a] - s[b]) < 0.2
                       for a, b in (("V_T", "set_T"), ("V_C", "set_C")))
        self._wait(done, c.val(st.get("timeout", 600)), "wait_voltage")

    def op_zero_drive(self, st):
        c = self.c
        c.sup.force.stop()
        c.safety.zero(c.val(st["slew"]) if "slew" in st else None)

    def op_tare(self, st):
        c = self.c
        s = c.fresh()
        if s.get("C_pF") is None or s.get("T") is None:
            raise StepError("tare: no capacitance or temperature reading")
        lim = self.lim
        D = s.get("D_set")
        if D is None or abs(D) > lim["tare_D_max_V"]:
            raise StepError(f"tare: drive is {D} V, tare needs it near 0: a tare under load hides that load "
                            "from the force limit")
        shift = abs(s.get("F") or 0.0)
        if (shift > lim["tare_F_max_N"] or s["T"] < lim["tare_T_min_K"]) and st.get("accept") is not True:
            raise StepError(f"tare would remove {shift:.1f} N at {s['T']:.1f} K. That may be real thermal "
                            "stress. Use accept = true if the sample is not loaded.")
        z = c.sup.tare(s["C_pF"], s["T"], persist=not (c.clock.virtual or c.st.simulated))
        c.log(f"tare: C_zero = {z:.6f} pF at {s['T']:.3f} K")

    # ── Kraft ────────────────────────────────────────────────────────────
    def _force_gate(self):
        st = self.c.st
        if not st.simulated and not st.cal.verified:
            raise StepError("force control is locked: set verified = true in the calibration "
                            "file after checking it against the cell's calibration sheet")

    def _goto(self, F, tol, hold, timeout):
        c = self.c
        self._force_gate()
        if not math.isfinite(F):
            raise StepError("force target must be finite")
        if abs(F) > self.lim["force_max_N"]:
            raise StepError(f"|F| = {abs(F):g} N above force_max_N = {self.lim['force_max_N']} N")
        c.sup.force.start(F, tol)

        def done(s):
            return s.get("F") is not None and abs(s["F"] - F) <= tol

        def checked(s):
            if not c.sup.force.enabled:
                raise StepError("force loop stopped")
            return done(s)
        self._wait(checked, timeout, f"goto_force {F:g} N", hold)

    def op_goto_force(self, st):
        c = self.c
        self._goto(c.val(st["F"]), c.val(st.get("tol", c.sup.force.c["default_tol_N"])),
                   c.val(st.get("hold", 10)), c.val(st.get("timeout", 1800)))

    def op_release_force(self, st):
        self.c.sup.force.stop()

    def op_measure(self, st):
        c = self.c
        n, dt = int(c.val(st.get("n", 1))), c.val(st.get("dt", 1))
        label = c.text(st.get("label", ""))
        for i in range(n):
            if i:
                c.sleep(dt)
            c.record(c.fresh(), label)

    def op_force_sweep(self, st):
        c = self.c
        pts = np.linspace(c.val(st["from"]), c.val(st["to"]), int(c.val(st["points"])))
        tol = c.val(st.get("tol", c.sup.force.c["default_tol_N"]))
        for F in pts:
            self._goto(float(F), tol, c.val(st.get("hold", 10)), c.val(st.get("timeout", 1800)))
            c.vars["F_set"] = float(F)
            self.op_measure({"n": st.get("n", 1), "dt": st.get("dt", 1), "label": st.get("label", "")})

    # ── AC-Elastokalorik ─────────────────────────────────────────────────
    def op_ac_on(self, st):
        c = self.c
        c.safety.set_ac(c.val(st["f"]), c.val(st["V"]), simulated=c.st.simulated,
                        accept=st.get("accept") is True)

    def op_lockin_setup(self, st):
        c = self.c
        c.safety.set_detection(c.val(st.get("harmonic", c.safety.ac_cfg.get("harmonic", 1))),
                               c.val(st.get("tau", c.safety.ac_cfg.get("time_constant_s", 1.0))))

    def op_ac_off(self, st):
        self.c.safety.ac_off()

    def op_measure_gain(self, st):
        """dF/dD aus zwei DC-Punkten. Die Anregung bleibt dabei aus: das Kapazitätsgerät mittelt
        über sie, die Stapel haben aber auch kleine Hysterese, also beide Richtungen."""
        c = self.c
        step, settle = c.val(st.get("step", 4.0)), c.val(st.get("settle", 20.0))
        if c.safety.ac_V:
            raise StepError("measure_gain: switch the AC drive off first")
        c.sup.force.stop()
        D0 = c.safety.drive()
        F = []
        for D in (D0 - step, D0 + step, D0 - step, D0 + step):
            c.safety.set_drive(D)
            self.op_wait_voltage({})
            c.sleep(settle)
            s = c.fresh()
            if s.get("F") is None:
                raise StepError("measure_gain: no force reading")
            F.append(s["F"])
        c.safety.set_drive(D0)
        self.op_wait_voltage({})
        G = abs((F[3] - F[2]) / (2.0 * step))      # zweite Schleife, die erste räumt die Hysterese
        if G <= 0:
            raise StepError("measure_gain: force does not follow the drive")
        G_min = float(c.sup.force.c.get("G_min", 0.02))
        if G < G_min:
            # Schritt innerhalb der Stapelhysterese oder Probe nicht im Kraftfluss: lieber kein G als ein
            # falsches, das dT/dsigma um Größenordnungen verfälscht
            raise StepError(f"measure_gain: dF/dD = {G:.4f} N/V is implausibly small (below {G_min} N/V). "
                            f"Use a larger step than {step:g} V, or check that the sample carries force.")
        c.sup.G_table.append((s["T"], G, D0))      # D0: gilt nur nahe diesem Offset
        c.log(f"measure_gain: dF/dD = {G:.4f} N/V at {s.get('T'):.2f} K")

    def op_frequency_sweep(self, st):
        c = self.c
        V = c.safety.ac_V
        if not V:
            raise StepError("frequency_sweep needs a running AC drive")
        fs = np.geomspace(c.val(st["from"]), c.val(st["to"]), int(c.val(st["points"])))
        settle = c.val(st.get("settle", 5))
        label = st.get("label", "frequency sweep")
        cfg = c.safety.ac_cfg
        for f in fs:
            why = c.safety.bridge_conflict(float(f))
            if why and st.get("accept") is not True:
                c.log(f"frequency_sweep: {f:.4g} Hz skipped, {why}")
                continue
            # Oben begrenzt der Strom. Dann Amplitude senken statt abzubrechen, R / V_ac bleibt vergleichbar.
            v = min(V, 0.99 * cfg["current_max_A"] / (2.0 * math.pi * float(f) * cfg["C_F"]))
            c.safety.set_ac(float(f), v, simulated=c.st.simulated, accept=st.get("accept") is True)
            c.sleep(settle)
            self.op_measure({"n": st.get("n", 3), "dt": st.get("dt", 1), "label": label})

    def op_temperature_sweep(self, st):
        c = self.c
        to, rate = c.val(st["to"]), c.val(st.get("rate", 0.5))
        dt = c.val(st.get("dt", 5))
        label = c.text(st.get("label", ""))
        start = c.snapshot().get("T")
        if start is None:
            raise StepError("temperature_sweep: no temperature reading")
        c.safety.set_temperature(to, rate)
        c.vars["_T_set"] = to
        limit = abs(to - start) / rate * 60.0 * 1.5 + 600.0
        if limit > self.lim["wait_max_s"]:
            raise StepError(f"sweep would take more than {self.lim['wait_max_s']} s")
        t0 = c.clock.now()
        while True:
            c.sleep(dt)
            s = c.fresh()
            c.record(s, label)
            if s.get("T") is not None and abs(s["T"] - to) < 0.02:
                return
            if c.clock.now() - t0 > limit:
                raise StepError("temperature_sweep: timeout")

    # ── Ablauf ───────────────────────────────────────────────────────────
    def op_for_range(self, st):
        c = self.c
        a, b, n = c.val(st["from"]), c.val(st["to"]), int(c.val(st["points"]))
        if st.get("spacing", "lin") == "log":
            if a <= 0 or b <= 0:
                raise StepError("log spacing needs positive limits")
            xs = np.geomspace(a, b, n)
        else:
            xs = np.linspace(a, b, n)
        for x in xs:
            c.vars[st["var"]] = float(x)
            self.run(st["do"])

    def op_repeat(self, st):
        for i in range(int(self.c.val(st["times"]))):
            self.c.vars["i"] = i
            self.run(st["do"])

    def op_set_variable(self, st):
        self.c.vars[st["name"]] = self.c.val(st["value"])

    def op_wait(self, st):
        s = self.c.val(st["seconds"])
        if s > self.lim["wait_max_s"]:
            raise StepError(f"wait above {self.lim['wait_max_s']} s")
        self.c.sleep(s)

    def op_log(self, st):
        self.c.log(self.c.text(st["message"]))

    def op_pause(self, st):
        c = self.c
        c.log("paused: " + c.text(st.get("message", "")))
        if c.clock.virtual:
            return
        c.st.runner.pause(st.get("message", ""))
        c.check()
