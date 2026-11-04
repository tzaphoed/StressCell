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
from contextlib import asynccontextmanager
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

import uvicorn                                                     # noqa: E402
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse           # noqa: E402
from fastapi.staticfiles import StaticFiles                        # noqa: E402

from core import fc100                                             # noqa: E402
from core.config import load_config                                # noqa: E402
from core.hub import Hub, clean                                    # noqa: E402
from core.interpreter import StepError                             # noqa: E402
from core.runner import Busy                                       # noqa: E402
from core.safety import LimitError                                 # noqa: E402
from core.sequence import check_sequence                           # noqa: E402
from core.stack import build                                       # noqa: E402

log = logging.getLogger("stresscell.server")
FRONTEND = BACKEND.parent / "frontend" / "renderer"
hub = Hub()


class State:
    cfg = None
    stack = None
    token = ""


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
    st.shutdown()
    st.inst.close()


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
                  "chamber_safe": st.cfg["chamber"]["safe_states"], "instruments": {
                      "supply": st.cfg["instruments"]["supply"], "cap": st.cfg["instruments"]["cap"]},
                  "tick_s": st.cfg["monitor"]["tick_s"]})


def status_dict():
    st = State.stack
    return clean({"last": st.supervisor.last, "run": st.runner.info(),
                  "alarms": list(st.supervisor.alarms.active.values())})


@app.get("/status")
def status():
    return status_dict()


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
