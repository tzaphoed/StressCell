"""Datenaufnahme ohne Sequenz: „Aufnahme an“, und alles Wichtige landet in einer CSV.

Der Recorder hängt am Messtakt des Supervisors. Er schreibt nur Dateien und schaltet nichts, darum
läuft er auch im Nur-Lese-Modus und neben einer laufenden Sequenz oder einem Python-Skript. Der Guard
hält dabei wie immer alles im erlaubten Bereich.

Ablage: data/<Datum_Zeit>_rec_<Name>/data.csv, dazu config.toml und die Kalibrierung als Kopie,
damit man später weiß, womit gerechnet wurde. Jede Zeile hat dieselben Spalten. Zusätzliche
Messgrößen eines Zusatzgeräts (z. B. V_tc, R_ohm) kommen als sample_<Name> dazu.
"""

from __future__ import annotations

import csv
import json
import math
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

COLUMNS = ("wall", "time", "t_s", "note", "T", "T_cell", "R_cell", "T_eff", "B", "chamber",
           "F", "stress_MPa", "C_pF", "loss", "V_T", "V_C", "I_T", "I_C", "D_set", "vmin", "vmax",
           "f_ac", "V_ac", "X", "Y", "R", "theta", "theta_stack", "T_ac_K", "F_ac_N", "G_used",
           "dT_dsigma_K_MPa", "dT_dc_K", "force_target", "alarms")
SAMPLE_KEYS = ("V_tc", "R_ohm")


class Recorder:
    def __init__(self, cfg, cal=None):
        self.cfg, self.cal = cfg, cal
        self.lock = threading.Lock()
        self.path: Path | None = None
        self._f = None
        self._w = None
        self.interval = 1.0
        self._last = None
        self._t0 = None
        self.rows = 0
        self._note = ""

    @property
    def active(self) -> bool:
        return self._f is not None

    def start(self, name="aufnahme", interval_s=1.0) -> str:
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "aufnahme"))[:60] or "aufnahme"
        iv = float(interval_s)
        if not math.isfinite(iv) or not 0.2 <= iv <= 3600:
            raise ValueError("recording interval 0.2 ... 3600 s")
        with self.lock:
            self._close()
            d = self.cfg["paths"]["data_dir"] / f"{datetime.now():%Y-%m-%d_%H%M%S}_rec_{name}"
            d.mkdir(parents=True, exist_ok=True)
            for src in (self.cfg["paths"].get("config"),
                        self.cfg["paths"]["calibration_dir"] / self.cfg["capacitance"]["calibration"]):
                try:
                    if src and Path(src).exists():
                        shutil.copy(src, d / Path(src).name)
                except OSError:
                    pass
            if self.cal is not None:
                (d / "calibration_state.json").write_text(json.dumps(self.cal.info(), default=str, indent=1))
            self.path = d / "data.csv"
            self._f = open(self.path, "w", newline="", encoding="utf-8")
            self._w = csv.writer(self._f)
            self._w.writerow(COLUMNS + tuple(f"sample_{k}" for k in SAMPLE_KEYS))
            self._f.flush()
            self.interval, self._last, self._t0, self.rows, self._note = iv, None, None, 0, ""
            return str(d)

    def note(self, text: str):
        """Kommt in die nächste Zeile, z. B. "Probe 2 eingebaut" oder "Offset -5 N"."""
        self._note = (self._note + "; " if self._note else "") + str(text)[:200]

    def maybe_write(self, s: dict, alarms=()):
        if not self.active:
            return
        t = s.get("t")
        if t is None or (self._last is not None and t - self._last < self.interval - 1e-9):
            return
        with self.lock:
            if not self.active:
                return
            self._last = t
            if self._t0 is None:
                self._t0 = t
            force = s.get("force") or {}
            row = {**s, "time": datetime.fromtimestamp(s.get("wall") or time.time()).isoformat(timespec="seconds"),
                   "t_s": round(t - self._t0, 3), "note": self._note,
                   "force_target": force.get("target") if force.get("enabled") else None,
                   "alarms": " | ".join(alarms)}
            sm = s.get("sample") or {}
            vals = [row.get(k) for k in COLUMNS] + [sm.get(k) for k in SAMPLE_KEYS]
            self._w.writerow(["" if v is None else v for v in vals])
            self._f.flush()
            self.rows += 1
            self._note = ""

    def stop(self) -> dict:
        with self.lock:
            info = self.info()
            self._close()
            return info

    def _close(self):
        if self._f is not None:
            try:
                self._f.close()
            finally:
                self._f = self._w = None

    def info(self) -> dict:
        return {"active": self.active, "path": str(self.path) if self.path else None, "rows": self.rows,
                "interval_s": self.interval}
