"""StressCell Backend: FastAPI, REST und WebSocket /ws. Port 8020.

Start:  python server.py            config.toml entscheidet über Simulation
        python server.py --sim 50   Simulation mit Zeitraffer 50

Die Oberfläche liegt unter http://127.0.0.1:8020/ . Schreibende Aufrufe brauchen den
Schlüssel aus access.json (neben config.toml). Die Oberfläche bekommt ihn in die Seite
eingesetzt, der Python-Client liest die Datei. Das Backend hört nur auf 127.0.0.1 und
prüft Host und Origin: eine beliebige Webseite im Browser darf hier keine Hochspannung
schalten.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import secrets
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

import uvicorn                                                     # noqa: E402
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles                        # noqa: E402

from core import advisor, fc100, settings                          # noqa: E402
from core.config import load_config                                # noqa: E402
from core.hub import Hub, clean                                    # noqa: E402
from core.interpreter import StepError                             # noqa: E402
from core.runner import Busy                                       # noqa: E402
from core.safety import LimitError                                 # noqa: E402
from core.sequence import check_sequence                           # noqa: E402
from core.stack import build                                       # noqa: E402

log = logging.getLogger("stresscell.server")
FRONTEND = BACKEND.parent / "frontend" / "renderer"
DOCS = BACKEND.parent / "docs"
hub = Hub()


class State:
    cfg = None
    stack = None
    token = ""
    reload_lock = threading.Lock()


def setup(cfg_path=None, sim_speed=None):
    cfg = load_config(cfg_path)
    if sim_speed is not None:
        cfg["mode"]["simulate"] = True
        cfg["mode"]["sim_speed"] = float(sim_speed)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(cfg["paths"]["log_dir"] / "backend.log", encoding="utf-8")])
    access = Path(cfg["paths"]["config"]).parent / "access.json"
    try:
        token = json.loads(access.read_text())["token"]
    except (OSError, ValueError, KeyError):
        token = secrets.token_urlsafe(24)
        access.write_text(json.dumps({"token": token}))
    State.cfg, State.token = cfg, token


@asynccontextmanager
async def lifespan(app):
    if State.cfg is None:
        setup()
    await hub.start()
    st = build(State.cfg, publish=hub.publish)
    State.stack = st
    try:
        st.init_cap()
    except Exception as e:
        log.warning("capacitance meter not configured: %s", e)
    st.supervisor.start()
    log.info("backend up, %s", "SIMULATION" if st.simulated else "REAL INSTRUMENTS")
    yield
    State.stack.shutdown()                  # nach einem Neustart aus den Einstellungen ist es ein anderer Stack
    State.stack.inst.close()


app = FastAPI(title="StressCell", lifespan=lifespan)


def allowed_host(host: str | None) -> bool:
    return (host or "").split(":")[0] in ("127.0.0.1", "localhost")


def allowed_origin(origin: str | None) -> bool:
    if origin is None:
        return True
    rest = origin.split("://")[-1]
    host, _, port = rest.partition(":")
    own = str(State.cfg["server"]["port"]) if State.cfg else ""
    return host in ("127.0.0.1", "localhost") and port == own


@app.middleware("http")
async def access_control(request: Request, call_next):
    if not allowed_host(request.headers.get("host")):
        return JSONResponse({"detail": "host not allowed"}, status_code=403)
    if request.method != "GET":
        if not allowed_origin(request.headers.get("origin")):
            return JSONResponse({"detail": "origin not allowed"}, status_code=403)
        if not secrets.compare_digest(request.headers.get("x-stresscell-token", ""), State.token or ""):
            return JSONResponse({"detail": "token missing or wrong"}, status_code=403)
    return await call_next(request)


@app.exception_handler(LimitError)
async def _limit(request, e):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(StepError)
async def _step(request, e):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(Busy)
async def _busy(request, e):
    return JSONResponse({"detail": str(e)}, status_code=409)


# ── Lesen ────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/info")
def info():
    st = State.stack
    return clean({"simulated": st.simulated, "read_only": st.safety.read_only, "limits": st.cfg["limits"],
                  "sample": st.cfg["sample"], "calibration": st.cal.info(), "capacitance": st.cfg["capacitance"],
                  "chamber_safe": st.cfg["chamber"]["safe_states"], "instruments": st.cfg["instruments"],
                  "ac": st.cfg.get("ac", {}), "ac_chain": st.safety.ac_chain.info(),
                  "identify": _identify(st), "tick_s": st.cfg["monitor"]["tick_s"]})


def _identify(st) -> dict:
    out = {}
    for name in ("supply", "cap", "cryo", "lockin", "sample"):
        dev = getattr(st.inst, name, None)
        fn = getattr(dev, "identify", None)
        try:
            out[name] = fn() if fn else (type(dev).__name__ if dev is not None else None)
        except Exception as e:
            out[name] = f"no answer: {e}"
    return out


def status_dict():
    st = State.stack
    return clean({"last": st.supervisor.last, "run": st.runner.info(),
                  "alarms": list(st.supervisor.alarms.active.values()), "record": st.supervisor.recorder.info()})


@app.get("/status")
def status():
    return status_dict()


@app.get("/setup/checklist")
def setup_checklist():
    return clean({"items": advisor.checklist(State.stack)})


@app.get("/calc")
def calc(request: Request):
    try:
        return clean(advisor.calc(State.cfg, dict(request.query_params)))
    except (TypeError, ValueError, ZeroDivisionError) as e:
        raise HTTPException(400, f"calc: {e}")


@app.get("/calibration")
def calibration():
    cal = State.stack.cal
    out = {"info": cal.info()}
    if hasattr(cal, "table"):
        out["table"] = cal.table()
    return clean(out)


@app.get("/config")
def config_view():
    """Konfiguration zum Ansehen in der Oberfläche. Ändern nur in config.toml."""
    c = {k: v for k, v in State.cfg.items() if k != "paths"}
    return clean({"config": c, "file": str(State.cfg["paths"]["config"])})


@app.get("/help", response_class=HTMLResponse)
def help_page():
    p = DOCS / "handbuch" / "handbuch.html"
    if not p.exists():
        return HTMLResponse("<p>Handbuch nicht gefunden: docs/handbuch/handbuch.html</p>", status_code=404)
    return HTMLResponse(p.read_text(encoding="utf-8"))


@app.get("/record")
def record_info():
    return State.stack.supervisor.recorder.info()


@app.post("/record/start")
def record_start(body: dict = Body(default={})):
    try:
        d = State.stack.supervisor.recorder.start(body.get("name", "aufnahme"), body.get("interval_s", 1.0))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))
    hub.publish({"type": "record", "data": State.stack.supervisor.recorder.info()})
    return {"ok": True, "dir": d}


@app.post("/record/stop")
def record_stop():
    info = State.stack.supervisor.recorder.stop()
    hub.publish({"type": "record", "data": State.stack.supervisor.recorder.info()})
    return info


@app.post("/record/note")
def record_note(body: dict = Body(...)):
    State.stack.supervisor.recorder.note(body.get("text", ""))
    return {"ok": True}


@app.get("/settings")
def settings_get():
    return {"values": settings.current(State.cfg), "choices": settings.choices(),
            "help": {k: v[3] for k, v in settings.SETTINGS.items()}, "file": str(State.cfg["paths"]["config"])}


@app.post("/settings")
def settings_post(body: dict = Body(...)):
    """Schalter in config.toml schreiben und das Backend mit der neuen Konfiguration neu aufbauen.
    Nur im Ruhezustand: kein Lauf, keine Sitzung, unter 1 V an beiden Stapeln."""
    st = State.stack
    try:
        changes = settings.validate(body.get("values", {}))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))
    if st.runner.info()["state"] != "idle" or st.runner.session is not None:
        raise HTTPException(409, "a sequence or Python session is running: stop it first")
    try:
        v = max(abs(st.inst.supply.voltage_now(c)) for c in ("tension", "compression"))
    except Exception:
        v = 0.0 if st.simulated else None
    if v is None or v > 1.0:
        raise HTTPException(409, "ramp both stacks to 0 V first (Drive auf 0), then change the settings")
    with State.reload_lock:
        settings.write(State.cfg["paths"]["config"], changes)
        old = State.stack
        old.supervisor.stop()
        old.shutdown()
        old.inst.close()
        cfg = load_config(State.cfg["paths"]["config"])
        new = build(cfg, publish=hub.publish)
        try:
            new.init_cap()
        except Exception as e:
            log.warning("capacitance meter not configured: %s", e)
        new.supervisor.start()
        State.cfg, State.stack = cfg, new
    log.info("settings changed %s, backend rebuilt: %s", changes, "SIMULATION" if new.simulated else "REAL")
    hub.publish({"type": "reload"})
    return {"ok": True, "values": settings.current(cfg)}


@app.get("/limits/curve")
def curve():
    xs = [1.8 + (320 - 1.8) * i / 400 for i in range(401)]
    w = [fc100.voltage_window(t) for t in xs]
    return {"T": xs, "vmin": [a for a, _ in w], "vmax": [b for _, b in w]}


# ── Sequenzen ────────────────────────────────────────────────────────────
def _seq_path(name: str) -> Path:
    if not name or any(c in name for c in '/\\:') or ".." in name:
        raise HTTPException(400, "bad name")
    return State.cfg["paths"]["sequence_dir"] / (name if name.endswith(".seq.json") else name + ".seq.json")


@app.get("/sequences")
def sequences():
    return {"files": sorted(p.name for p in State.cfg["paths"]["sequence_dir"].glob("*.seq.json"))}


@app.get("/sequences/{name}")
def sequence_get(name: str):
    p = _seq_path(name)
    if not p.exists():
        raise HTTPException(404, "not found")
    return json.loads(p.read_text(encoding="utf-8"))


@app.post("/sequences/{name}")
def sequence_put(name: str, seq: dict = Body(...)):
    errors = check_sequence(seq)
    if errors:
        raise HTTPException(400, "; ".join(errors))
    _seq_path(name).write_text(json.dumps(seq, indent=1), encoding="utf-8")
    return {"ok": True}


@app.post("/sequence/check")
def sequence_check(seq: dict = Body(...)):
    return clean(State.stack.dry_run(seq))


@app.post("/run/start")
def run_start(seq: dict = Body(...)):
    State.stack.runner.start(seq)
    return State.stack.runner.info()


@app.post("/run/pause")
def run_pause():
    State.stack.runner.pause("paused by user")
    return State.stack.runner.info()


@app.post("/run/resume")
def run_resume():
    State.stack.runner.resume()
    return State.stack.runner.info()


@app.post("/run/stop")
def run_stop():
    State.stack.runner.stop("stopped by user", halt=False)
    return State.stack.runner.info()


@app.post("/stop_all")
def stop_all():
    r = State.stack.stop_all()
    if not r["ok"]:
        return JSONResponse({"detail": r["detail"]}, status_code=409)
    return r


@app.post("/alarms/ack")
def alarms_ack(body: dict = Body(default={})):
    State.stack.ack(body.get("key"))
    return {"ok": True}


# ── Einzelschritte, Python-Client ────────────────────────────────────────
@app.post("/step")
def step(st: dict = Body(...)):
    State.stack.runner.step(st)
    return status_dict()


@app.post("/session/open")
def session_open(body: dict = Body(default={})):
    return {"run_dir": State.stack.runner.open_session(body.get("name", "session"))}


@app.post("/session/close")
def session_close():
    State.stack.runner.close_session()
    return {"ok": True}


# ── Ergebnisse ───────────────────────────────────────────────────────────
@app.get("/runs")
def runs():
    root = State.cfg["paths"]["data_dir"]
    return {"runs": sorted((p.name for p in root.iterdir() if p.is_dir()), reverse=True)[:200]}


@app.get("/runs/{name}/log")
def run_log(name: str):
    p = _run_file(name, "run.log")
    return {"lines": p.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:] if p.exists() else []}


@app.get("/runs/{name}/data")
def run_data(name: str):
    p = _run_file(name, "data.csv")
    if not p.exists():
        return {"columns": [], "data": {}}
    with open(p, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    cols = list(rows[0]) if rows else []

    def conv(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return v or None
    return clean({"columns": cols, "data": {c: [conv(r.get(c)) for r in rows] for c in cols}})


@app.get("/runs/{name}/data.csv")
def run_csv(name: str):
    p = _run_file(name, "data.csv")
    if not p.exists():
        raise HTTPException(404, "no data")
    return FileResponse(p, media_type="text/csv", filename=f"{name}.csv")


def _run_file(name: str, fname: str) -> Path:
    if any(c in name for c in '/\\:') or ".." in name:
        raise HTTPException(400, "bad name")
    return State.cfg["paths"]["data_dir"] / name / fname


# ── WebSocket ────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    if not allowed_host(websocket.headers.get("host")) or not allowed_origin(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    st = State.stack
    await websocket.send_text(json.dumps(clean({"type": "hello", "history": st.supervisor.history[-3600:],
                                                **status_dict()}), default=str))
    hub.clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(websocket)


@app.get("/", response_class=HTMLResponse)
def index():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__TOKEN__", State.token),
                        headers={"Content-Security-Policy": "frame-ancestors 'none'"})


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--sim", type=float, default=None, help="simulation with this time lapse")
    a = ap.parse_args()
    setup(a.config, a.sim)
    uvicorn.run(app, host=State.cfg["server"]["host"], port=State.cfg["server"]["port"], log_level="warning")


if __name__ == "__main__":
    main()
