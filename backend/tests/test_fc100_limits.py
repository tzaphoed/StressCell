import pytest

from core.fc100 import effective_temperature, split_drive, voltage_window


@pytest.mark.parametrize("T,lo,hi", [
    (300, -20, 120), (250, -20, 120), (175, -35, 120), (100, -50, 120),
    (55, -125, 160), (10, -200, 200), (4, -200, 200), (1.8, -200, 200), (None, -20, 120),
])
def test_table_2(T, lo, hi):
    a, b = voltage_window(T)
    assert a == pytest.approx(lo) and b == pytest.approx(hi)


def test_window_is_continuous_and_narrows_on_warming():
    prev = voltage_window(0.0)
    for k in range(1, 3500):
        cur = voltage_window(k * 0.1)
        assert cur[0] >= prev[0] - 1e-9 and cur[1] <= prev[1] + 1e-9
        assert abs(cur[0] - prev[0]) < 0.3 and abs(cur[1] - prev[1]) < 0.3
        prev = cur


def test_nan_means_narrowest():
    assert voltage_window(float("nan")) == (-20.0, 120.0)


def test_lookahead_only_when_warming():
    assert effective_temperature(10, 0.2, 2, 60) == pytest.approx(24)
    assert effective_temperature(10, -0.2, 2, 60) == pytest.approx(12)
    assert effective_temperature(None) is None


@pytest.mark.parametrize("D,vt,vc", [(0, 0, 0), (40, 20, -20), (140, 120, -20), (-140, -20, 120), (100, 80, -20)])
def test_split_drive_300K(D, vt, vc):
    a, b = split_drive(D, -20, 120)
    assert a == pytest.approx(vt) and b == pytest.approx(vc)
    assert a - b == pytest.approx(D)


def test_split_drive_beyond_window():
    with pytest.raises(ValueError):
        split_drive(141, -20, 120)
    assert split_drive(400, -200, 200) == (200, -200)
