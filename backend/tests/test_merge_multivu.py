import csv
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "merge_multivu", Path(__file__).resolve().parents[2] / "tools" / "merge_multivu.py")
mm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm)

T0 = 1_790_000_000.0          # Unix-Zeit im Messzeitraum


def write_sc(p, times):
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["wall", "time", "T_cell", "R_cell", "X"])
        for t in times:
            w.writerow([f"{t:.3f}", "x", "100", "88.0", "1e-6"])


def write_mv(p, times, epoch_shift=0.0, gap_at=None):
    lines = ["[Header]", "TITLE,PPMS log", "[Data]",
             "Comment,Time Stamp (sec),Temperature (K),Temperature Status (code),Magnetic Field (Oe),Sample Space Pressure (Torr)"]
    for t in times:
        if gap_at and gap_at[0] < t < gap_at[1]:
            continue
        lines.append(f",{t - epoch_shift:.3f},{100 + (t - T0) / 60:.4f},4,{(t - T0) * 10:.1f},1e-5")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("epoch", ["1970", "1904"])
def test_merge_interpolates_T_and_B(tmp_path, epoch):
    sc, mv = tmp_path / "data.csv", tmp_path / "log.dat"
    write_sc(sc, [T0 + 30 + 5 * i for i in range(10)])
    write_mv(mv, [T0 + 2 * i for i in range(100)], epoch_shift=mm.EPOCHS[epoch])
    r = mm.merge(sc, mv)
    assert r["epoch"] == epoch and r["filled"]["T_ppms_K"] == 10
    rows = list(csv.DictReader(open(r["out"], encoding="utf-8")))
    assert float(rows[0]["T_ppms_K"]) == pytest.approx(100.5, abs=1e-3)       # 30 s nach T0, 1 K/min
    assert float(rows[0]["B_ppms_T"]) == pytest.approx(0.03, abs=1e-6)        # 300 Oe
    assert rows[0]["R_cell"] == "88.0" and "P_ppms_Torr" in rows[0]


def test_merge_offset_gap_and_outside(tmp_path):
    sc, mv = tmp_path / "data.csv", tmp_path / "log.dat"
    write_sc(sc, [T0 + 30, T0 + 100, T0 + 500])                             # 500 s: nach dem Log
    write_mv(mv, [T0 + 12 + 2 * i for i in range(100)], gap_at=(T0 + 80, T0 + 170))
    r = mm.merge(sc, mv, offset=12.0)                                       # PPMS-Uhr geht 12 s vor
    rows = list(csv.DictReader(open(r["out"], encoding="utf-8")))
    assert float(rows[0]["T_ppms_K"]) == pytest.approx(100.5 + 12 / 60, abs=1e-3)
    assert rows[1]["T_ppms_K"] == "" and rows[2]["T_ppms_K"] == ""          # Lücke, außerhalb


def test_merge_refuses_without_overlap(tmp_path):
    sc, mv = tmp_path / "data.csv", tmp_path / "log.dat"
    write_sc(sc, [T0 + 10])
    write_mv(mv, [T0 + 1e6 + i for i in range(5)])
    with pytest.raises(ValueError, match="do not overlap"):
        mm.merge(sc, mv)
