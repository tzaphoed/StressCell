import json
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api(cfg, monkeypatch):
    import server
    cfg["mode"]["sim_speed"] = 100.0
    monkeypatch.setattr(server.State, "cfg", cfg)
    monkeypatch.setattr(server.State, "token", "secret")
    with TestClient(server.app, base_url="http://127.0.0.1:8020") as c:
        c.headers["X-StressCell-Token"] = "secret"
        time.sleep(0.1)
        yield c


def test_read_without_token_write_needs_token(api):
    assert api.get("/status", headers={"X-StressCell-Token": ""}).status_code == 200
    r = api.post("/stop_all", headers={"X-StressCell-Token": "wrong"})
    assert r.status_code == 403
    assert api.post("/stop_all").status_code == 200


def test_foreign_website_cannot_write(api):
    r = api.post("/step", json={"op": "output", "on": True},
                 headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_dns_rebinding_host_refused(api):
    assert api.get("/status", headers={"Host": "evil.example"}).status_code == 403


def test_limit_error_comes_back_as_400(api):
    api.post("/step", json={"op": "output", "on": True})
    r = api.post("/step", json={"op": "set_voltages", "tension": 200, "compression": 0})
    assert r.status_code == 400 and "window" in r.json()["detail"]


def test_unknown_op_400(api):
    assert api.post("/step", json={"op": "rm_rf"}).status_code == 400


def test_sequence_round_trip_and_path_escape(api):
    s = {"format": "stresscell-sequence", "version": 1, "name": "x", "steps": [{"op": "wait", "seconds": 1}]}
    assert api.post("/sequences/x", json=s).status_code == 200
    assert api.get("/sequences/x.seq.json").json()["name"] == "x"
    assert api.post("/sequence/check", json=s).json()["ok"]
    assert api.post("/sequences/..%2F..%2Fevil", json=s).status_code in (400, 404, 405)


def test_run_and_data(api):
    s = {"format": "stresscell-sequence", "version": 1, "name": "r", "steps": [
        {"op": "output", "on": True}, {"op": "measure", "n": 3, "dt": 1, "label": "p"}]}
    assert api.post("/run/start", json=s).status_code == 200
    for _ in range(100):
        info = api.get("/status").json()["run"]
        if info["state"] == "idle":
            break
        time.sleep(0.05)
    assert info["result"] == "finished"
    run = api.get("/runs").json()["runs"][0]
    d = api.get(f"/runs/{run}/data").json()
    assert len(d["data"]["F"]) == 3


def test_websocket_rejects_foreign_origin(api):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with api.websocket_connect("/ws", headers={"Host": "127.0.0.1:8020", "Origin": "https://evil.example"}):
            pass


def test_websocket_hello(api):
    with api.websocket_connect("/ws", headers={"Host": "127.0.0.1:8020"}) as ws:
        assert json.loads(ws.receive_text())["type"] == "hello"


def test_other_localhost_port_is_refused(api):
    r = api.post("/stop_all", headers={"Origin": "http://localhost:9999"})
    assert r.status_code == 403


def test_windows_drive_path_refused(api):
    assert api.get("/sequences/C:evil").status_code == 400


def test_read_only_stop_all_is_an_error(api):
    import server
    server.State.stack.safety.read_only = True
    assert api.post("/stop_all").status_code == 409
    server.State.stack.safety.read_only = False
