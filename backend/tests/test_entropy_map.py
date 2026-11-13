"""Entropie aus C_p bei σ = 0 und der elastokalorischen Steigung, gegen ein analytisches Modell."""

import csv
import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "entropy_map.py"
spec = importlib.util.spec_from_file_location("entropy_map", TOOL)
em = importlib.util.module_from_spec(spec)
spec.loader.exec_module(em)

A, B = 1.0, -1e-5            # S(T, σ) = A ln T + B σ T   (σ in MPa, Druck negativ)


def S_true(T, s):
    return A * math.log(T) + B * s * T


def g_true(T, s):
    # (∂T/∂σ)_S = -(∂S/∂σ)_T / (∂S/∂T)_σ
    return -(B * T) / (A / T + B * s)


def test_matches_analytic_entropy(tmp_path):
    Tcp = np.linspace(40.0, 220.0, 721)
    C = np.full_like(Tcp, A)                     # C = T (∂S/∂T) bei σ = 0
    pts = np.array([(T, s, g_true(T, s)) for s in (0, -20, -40, -60, -80, -100) for T in np.arange(40, 221, 1.0)])
    rows = em.entropy_map(Tcp, C, pts, [80.0, 120.0, 180.0], [0.0, -50.0, -100.0], S0=A * math.log(40.0))
    for T, s, S, dS, T0 in rows:
        assert S == pytest.approx(S_true(T, s), abs=2e-4), (T, s)
        assert dS == pytest.approx(B * s * T, abs=2e-4)


def test_cli_with_stresscell_files(tmp_path):
    cp = tmp_path / "cp.csv"
    cp.write_text("T_K,C\n" + "\n".join(f"{T},{A}" for T in np.linspace(40, 220, 361)))
    data = tmp_path / "data.csv"
    with open(data, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["T", "T_sample", "stress_MPa", "dT_dsigma_K_MPa", "F"])
        for s in (0, -50, -100):
            for T in range(50, 211, 2):
                w.writerow([T + 3, T, s, g_true(T, s), 0])
        w.writerow(["", "", "", "", ""])                       # leere Zeilen stören nicht
    out = tmp_path / "S.csv"
    em.main(["--cp", str(cp), "--data", str(data), "--out", str(out), "--nT", "20", "--nsigma", "5",
             "--S0", str(A * math.log(40))])
    rows = list(csv.DictReader(open(out)))
    vals = [r for r in rows if r["S"] != "nan"]
    assert len(vals) > 50
    r = min(vals, key=lambda r: abs(float(r["T_K"]) - 130) + abs(float(r["sigma_MPa"]) + 50))
    assert float(r["S"]) == pytest.approx(S_true(float(r["T_K"]), float(r["sigma_MPa"])), abs=5e-4)


def test_outside_cp_range_gives_no_value():
    Tcp = np.linspace(100.0, 150.0, 51)
    C = np.ones_like(Tcp)
    pts = np.array([(T, s, 0.5) for s in (0, -100) for T in np.arange(90, 161, 1.0)])   # 0,5 K/MPa: weit weg
    rows = em.entropy_map(Tcp, C, pts, [120.0], [-100.0])
    assert math.isnan(rows[0][2])                              # T0 = 170 K liegt außerhalb der C_p-Daten


def test_prefers_signed_column(tmp_path, capsys):
    data = tmp_path / "d.csv"
    data.write_text("T_sample,stress_MPa,dT_dsigma_K_MPa,dT_dsigma_signed_K_MPa\n100,0,0.5,-0.5\n110,0,0.5,-0.5\n"
                    "100,-10,0.5,-0.5\n110,-10,0.5,-0.5\n")
    pts = em.read_g([data])
    assert (pts[:, 2] == -0.5).all()
    data.write_text("T_sample,stress_MPa,dT_dsigma_K_MPa\n100,0,0.5\n110,0,0.5\n100,-10,0.5\n110,-10,0.5\n")
    em.read_g([data])
    assert "unsigned" in capsys.readouterr().out
