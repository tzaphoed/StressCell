"""Python-Zugang zu StressCell.

    from stresscell_client import Lab

    with Lab(session="Sr2RuO4_force_sweep") as lab:      # Backend muss laufen
        lab.output(True)
        lab.tare()
        lab.set_temperature(4, rate=5)
        lab.wait_temperature()
        for F in (0, 2, 4, 6):
            lab.goto_force(F)
            lab.measure(n=10, dt=1, label=f"{F} N")
        lab.zero_drive()

Jeder Aufruf blockiert, bis der Schritt fertig ist, und läuft durch dieselben Grenzen wie
eine Sequenz. Messpunkte landen in data/<Zeit>_<session>/data.csv. Ein Grenzfehler kommt
als LabError zurück. Nach STOP ALL oder einem Guard-Eingriff werden weitere Schritte
abgelehnt, bis lab.acknowledge() gerufen wurde: bitte nicht im Skript automatisieren. Fehlt das Backend: Lab.simulated() läuft ohne Server und ohne Warten,
praktisch zum Ausprobieren eines Skripts.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path


class LabError(Exception):
    pass


class _Http:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Content-Type": "application/json", "X-StressCell-Token": self.token or ""})
        try:
            with urllib.request.urlopen(req) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                msg = json.load(e).get("detail", str(e))
            except ValueError:
                msg = str(e)
            raise LabError(msg) from None


class _Local:
    def __init__(self, stack):
        self.st = stack

    def step(self, st):
        from core.interpreter import StepError
        from core.runner import Busy
        from core.safety import LimitError
        try:
            self.st.runner.step(st)
        except (StepError, LimitError, Busy) as e:
            raise LabError(str(e)) from None


class Lab:
    def __init__(self, url="http://127.0.0.1:8020", token=None, session=None, _local=None):
        self._local = _local
        self._session = session
        if _local is None:
            if token is None:
                f = Path(__file__).resolve().parent / "access.json"
                token = json.loads(f.read_text())["token"] if f.exists() else ""
            self._http = _Http(url, token)

    @classmethod
    def simulated(cls, config=None, session=None, **sim):
        """Alles im selben Prozess: simulierte Geräte, virtuelle Uhr, sofort fertig."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from core.config import load_config
        from core.stack import build
        cfg = load_config(config)
        cfg["mode"]["simulate"] = True
        cfg.setdefault("sim", {}).update(sim)
        st = build(cfg, virtual=True, noise=True)
        st.init_cap()
        st.supervisor.tick()
        return cls(session=session, _local=_Local(st))

    # ── Sitzung ──────────────────────────────────────────────────────────
    def __enter__(self):
        if self._session:
            self.open_session(self._session)
        return self

    def __exit__(self, *exc):
        self.close_session()
        return False

    def open_session(self, name):
        if self._local:
            return self._local.st.runner.open_session(name)
        return self._http.call("POST", "/session/open", {"name": name})["run_dir"]

    def close_session(self):
        if self._local:
            self._local.st.runner.close_session()
        else:
            self._http.call("POST", "/session/close", {})

    def _step(self, op, **params):
        st = {"op": op, **{k: v for k, v in params.items() if v is not None}}
        if self._local:
            self._local.step(st)
        else:
            self._http.call("POST", "/step", st)

    # ── Lesen ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        if self._local:
            return {"last": self._local.st.supervisor.tick()}
        return self._http.call("GET", "/status")

    @property
    def last(self) -> dict:
        return self.status()["last"]

    @property
    def T(self):
        return self.last.get("T")

    @property
    def B(self):
        return self.last.get("B")

    @property
    def F(self):
        return self.last.get("F")

    @property
    def C(self):
        return self.last.get("C_pF")

    # ── Schritte, Bedeutung siehe core/sequence.py ───────────────────────
    def set_temperature(self, T, rate=None, mode=None):
        self._step("set_temperature", T=T, rate=rate, mode=mode)

    def wait_temperature(self, tol=None, hold=None, timeout=None):
        self._step("wait_temperature", tol=tol, hold=hold, timeout=timeout)

    def set_field(self, B, rate=None, persistent=None):
        self._step("set_field", B=B, rate=rate, persistent=persistent)

    def wait_field(self, tol=None, timeout=None):
        self._step("wait_field", tol=tol, timeout=timeout)

    def set_chamber(self, mode):
        self._step("set_chamber", mode=mode)

    def output(self, on: bool):
        self._step("output", on=bool(on))

    def set_voltages(self, tension, compression, slew=None):
        self._step("set_voltages", tension=tension, compression=compression, slew=slew)

    def set_drive(self, D, slew=None):
        self._step("set_drive", D=D, slew=slew)

    def wait_voltage(self, timeout=None):
        self._step("wait_voltage", timeout=timeout)

    def zero_drive(self, slew=None):
        self._step("zero_drive", slew=slew)

    def tare(self, accept=None):
        self._step("tare", accept=accept)

    def ac_on(self, f, V):
        self._step("ac_on", f=f, V=V)

    def ac_off(self):
        self._step("ac_off")

    def measure_gain(self, step=None, settle=None):
        self._step("measure_gain", step=step, settle=settle)

    def frequency_sweep(self, start, stop, points, settle=None, n=None, dt=None, label=None):
        self._step("frequency_sweep", **{"from": start, "to": stop}, points=points, settle=settle,
                   n=n, dt=dt, label=label)

    def temperature_sweep(self, to, rate=None, dt=None, label=None):
        self._step("temperature_sweep", to=to, rate=rate, dt=dt, label=label)

    def goto_force(self, F, tol=None, hold=None, timeout=None):
        self._step("goto_force", F=F, tol=tol, hold=hold, timeout=timeout)

    def release_force(self):
        self._step("release_force")

    def measure(self, n=1, dt=1.0, label=""):
        self._step("measure", n=n, dt=dt, label=label)

    def wait(self, seconds):
        self._step("wait", seconds=seconds)

    def log(self, message):
        self._step("log", message=message)

    def stop_all(self):
        if self._local:
            self._local.st.stop_all()
        else:
            self._http.call("POST", "/stop_all", {})

    def acknowledge(self):
        """Nach einem Stopp oder Guard-Eingriff: erst nachsehen, dann weiter."""
        if self._local:
            self._local.st.ack()
        else:
            self._http.call("POST", "/alarms/ack", {})
