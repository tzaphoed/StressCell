"""Lauf einer Sequenz, Datenablage, Einzelschritte und Trockenlauf.

Ein Lauf bekommt einen Ordner data/<Zeit>_<Name>/ mit sequence.json, run_info.json
(Grenzen, Kalibrierung, Probe), run.log und data.csv.

Einzelschritte aus dem Python-Client laufen durch denselben Interpreter, schreiben aber
in eine Sitzung (open_session). Es läuft immer nur eines von beiden.

Ein Stopp von Guard oder STOP ALL gilt, bis jemand quittiert. Sonst würde das nächste
Kommando eines Python-Skripts einfach weiterfahren, weil zwischen zwei Aufrufen gerade
nichts lief, das sich abbrechen ließ.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime

from .interpreter import Abort, Context, Interpreter, StepError
from .sequence import check_sequence, check_steps

log = logging.getLogger("stresscell.runner")


def _slug(s):
    return re.sub(r"[^\w\-]+", "_", s or "run").strip("_")[:60] or "run"


class Busy(Exception):
    pass


# Das darf man im gestoppten Zustand immer: es macht die Lage sicherer.
SAFE_OPS = ("zero_drive", "release_force", "ac_off", "log")
AUTO_SESSION_OPS = ("measure", "force_sweep", "temperature_sweep", "frequency_sweep")


class Runner:
    def __init__(self, stack):
        self.st = stack
        self.state = "idle"             # idle | running | paused
        self.name = ""
        self.ctx: Context | None = None
        self.thread: threading.Thread | None = None
        self.run_dir = None
        self.message = ""
        self.result = None
        self._log_fh = None
        self.session: Context | None = None
        self.session_auto = False
        self._step_lock = threading.Lock()
        self._step_ctx: Context | None = None
        self._lock = threading.Lock()
        self.halted: str | None = None
        self._start_stopped: str | None = None

    def info(self) -> dict:
        return {"state": self.state, "name": self.name, "message": self.message, "result": self.result,
                "run_dir": str(self.run_dir) if self.run_dir else None,
                "points": (self.ctx or self.session).points if (self.ctx or self.session) else 0,
                "session": self.session is not None, "halted": self.halted}

    def _emit(self):
        self.st.publish({"type": "run", "data": self.info()})

    def _log(self, msg):
        line = f"{datetime.now():%H:%M:%S}  {msg}"
        if self._log_fh:
            self._log_fh.write(line + "\n")
            self._log_fh.flush()
        self.st.publish({"type": "log", "line": line})

    def _open_dir(self, name, seq=None):
        cfg = self.st.cfg
        d = cfg["paths"]["data_dir"] / f"{datetime.now():%Y-%m-%d_%H%M%S}_{_slug(name)}"
        d.mkdir(parents=True, exist_ok=True)
        if seq is not None:
            (d / "sequence.json").write_text(json.dumps(seq, indent=1), encoding="utf-8")
        info = {"started": datetime.now().isoformat(timespec="seconds"), "name": name,
                "simulated": self.st.simulated, "limits": cfg["limits"], "sample": cfg["sample"],
                "chamber": cfg["chamber"], "capacitance": cfg["capacitance"], "calibration": self.st.cal.info()}
        (d / "run_info.json").write_text(json.dumps(info, indent=1, default=str), encoding="utf-8")
        self._log_fh = open(d / "run.log", "a", encoding="utf-8")
        self.run_dir = d
        return d

    # ── Sequenz ──────────────────────────────────────────────────────────
    def start(self, seq: dict, dry_first: bool = True):
        with self._lock:
            if self.halted:
                raise Busy(f"stopped: {self.halted}. Acknowledge first.")
            if self.state != "idle":
                raise Busy("a sequence is running")
            if self.session is not None and not self.session_auto:
                raise Busy("a Python session is open: close it first")
            if self._step_lock.locked():
                raise Busy("a step is running")
            self.state, self._start_stopped = "starting", None
        if self.session is not None:
            self.close_session()               # nur die automatische, für lose Messpunkte
        try:
            errors = check_sequence(seq)
            if errors:
                raise StepError("; ".join(errors))
            if dry_first:
                rep = self.st.dry_run(seq)
                if not rep["ok"]:
                    raise StepError("dry run failed: " + rep["error"])
            d = self._open_dir(seq.get("name", "sequence"), seq)
        except BaseException:
            self.state = "idle"
            raise
        self.ctx = Context(self.st, d / "data.csv", self._log)
        self.name, self.message, self.result = seq.get("name", ""), "", None
        with self._lock:
            if self._start_stopped:
                self.state = "idle"
                raise Busy(f"start cancelled: {self._start_stopped}")
            self.state = "running"
        self.thread = threading.Thread(target=self._run, args=(seq,), name="sequence", daemon=True)
        self.thread.start()
        self._emit()

    def _run(self, seq):
        ctx = self.ctx
        self._log(f"start: {seq.get('name', '')}")
        try:
            Interpreter(ctx).run(seq["steps"])
            self.result = "finished"
        except Abort as e:
            self.result = f"aborted: {e}"
        except StepError as e:
            self.result = f"error: {e}"
        except BaseException as e:             # auch SystemExit, sonst bliebe state auf running
            log.exception("sequence failed")
            self.result = f"error: {e!r}"
        self._log(self.result)
        self._finish()

    def _finish(self):
        self.st.supervisor.force.stop()
        self.st.safety.ac_off()
        if self.ctx:
            self.ctx.close()
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        self.state = "idle"
        self._emit()

    def pause(self, message=""):
        if self.ctx and self.state == "running":
            self.ctx.resume.clear()
            self.state, self.message = "paused", message
            self._emit()

    def resume(self):
        if self.ctx and self.state == "paused":
            self.state, self.message = "running", ""
            self.ctx.resume.set()
            self._emit()

    def stop(self, reason="stopped", halt=True):
        """halt: weitere Schritte bleiben gesperrt, bis jemand quittiert."""
        with self._lock:
            if self.state == "starting":
                self._start_stopped = reason
        if halt:
            first = self.halted is None
            self.halted = reason
            self.st.safety.halted = reason
            self.st.safety.ac_off()
            if first:
                self.st.supervisor.alarms.set("halted", f"Stopped: {reason}. Acknowledge to continue.",
                                              latched=True)
        targets = [self._step_ctx]
        if self.state in ("running", "paused"):
            targets.append(self.ctx)
        for ctx in targets:
            if ctx is not None:
                ctx.abort_reason = reason
                ctx.abort.set()

    def wait(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)

    # ── Python-Client ────────────────────────────────────────────────────
    def open_session(self, name="session", auto=False):
        with self._lock:
            if self.state != "idle":
                raise Busy("a sequence is running")
            if self.session is None:
                d = self._open_dir(name)
                self.session = Context(self.st, d / "data.csv", self._log)
                self.session_auto = auto
                self._emit()
            elif not auto:
                self.session_auto = False
        return str(self.run_dir)

    def close_session(self):
        """Ein Skript kann gehen, seine Regelung und seine Anregung nicht."""
        self.st.supervisor.force.stop()
        self.st.safety.ac_off()
        if self.session is not None:
            self.session.close()
            self.session = None
            self.session_auto = False
            if self._log_fh:
                self._log_fh.close()
                self._log_fh = None
            self._emit()

    def step(self, st: dict):
        errors = check_steps([st])
        if errors:
            raise StepError("; ".join(errors))
        with self._lock:
            if self.state != "idle":
                raise Busy("a sequence is running")
            if self.halted and st.get("op") not in SAFE_OPS \
                    and not (st.get("op") == "output" and st.get("on") is False):
                raise Busy(f"stopped: {self.halted}. Acknowledge first.")
            if not self._step_lock.acquire(blocking=False):
                raise Busy("another step is running")
        ok = False
        try:
            if self.session is None and st.get("op") in AUTO_SESSION_OPS:
                self.open_session("session", auto=True)   # sonst gingen die Messpunkte verloren
            ctx = self.session or Context(self.st, None, self._log)
            ctx.abort.clear()
            ctx.abort_reason = ""
            self._step_ctx = ctx
            Interpreter(ctx).step(st)
            ok = True
        except Abort as e:
            raise StepError(f"aborted: {e}")
        finally:
            self._step_ctx = None
            # Nur der Schritt, der den Regler gestartet hat, nimmt ihn beim Scheitern mit.
            # Ein abgelehnter Tippfehler lässt eine laufende Kraftregelung in Ruhe.
            if not ok and st.get("op") in ("goto_force", "force_sweep"):
                self.st.supervisor.force.stop()
            self._step_lock.release()
