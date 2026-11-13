"""Feste Messfunktionen statt frei gebauter Sequenzen.

Jede Funktion hat feste Parameter mit Grenzen und baut daraus eine Schrittliste aus den getesteten
Einzelbefehlen (goto_force, measure, ac_on, frequency_sweep, temperature_sweep). Die Liste läuft über
den Runner: vorher ein Trockenlauf auf der simulierten Zelle, währenddessen der Guard, danach eine
Ablage in data/. Es gibt keinen Editor und keine Schleifen, die jemand falsch zusammensteckt.

Spannung σ in MPa, negativ ist Druck. Umrechnung in Kraft über [sample] area_mm2: F = σ · A.
"""

from __future__ import annotations

import math

# Parameter: (Name, Beschriftung, Vorgabe, Einheit, kleinster, größter Wert, Erklärung)
TASKS = {
    "stress_hold": {
        "label": "Spannung halten und aufnehmen",
        "help": "Der Kraftregler fährt die Spannung an und hält sie. Währenddessen wird im Intervall gemessen.",
        "params": [
            ("sigma_MPa", "σ", -20.0, "MPa", -2000, 2000, "Zielspannung, negativ ist Druck"),
            ("duration_s", "Dauer", 600.0, "s", 1, 172800, "So lange halten und messen"),
            ("dt_s", "Intervall", 5.0, "s", 1, 3600, "Abstand der Messpunkte"),
            ("tol_N", "Toleranz", 0.1, "N", 0.01, 10, "Etwa das Dreifache des Kraftrauschens"),
        ],
    },
    "stress_sweep": {
        "label": "Spannungssweep",
        "help": "Stufen von σ_start nach σ_ende, je Stufe regeln, warten, messen. Auf Wunsch zurück.",
        "params": [
            ("from_MPa", "σ Start", 0.0, "MPa", -2000, 2000, ""),
            ("to_MPa", "σ Ende", -100.0, "MPa", -2000, 2000, "negativ ist Druck"),
            ("step_MPa", "Schritt", 10.0, "MPa", 0.01, 1000, "Betrag, das Vorzeichen folgt aus Start und Ende"),
            ("hold_s", "Warten je Stufe", 30.0, "s", 0, 3600, "nach Erreichen, bevor gemessen wird; bei AC etwa 5 τ"),
            ("n", "Messungen je Stufe", 3, "", 1, 100, ""),
            ("dt_s", "Abstand", 2.0, "s", 1, 600, "zwischen den Messungen einer Stufe"),
            ("back", "auch zurück", 1, "0/1", 0, 1, "1: danach dieselben Stufen rückwärts (Hysterese sehen)"),
            ("tol_N", "Toleranz", 0.1, "N", 0.01, 10, ""),
        ],
    },
    "frequency_sweep": {
        "label": "Frequenzsweep",
        "help": "AC an, Frequenzen logarithmisch, je Frequenz einschwingen und messen. Sucht das Plateau.",
        "params": [
            ("f_from_Hz", "f Start", 5.0, "Hz", 0.5, 5000, ""),
            ("f_to_Hz", "f Ende", 500.0, "Hz", 0.5, 5000, ""),
            ("points", "Punkte", 15, "", 2, 200, "logarithmisch verteilt"),
            ("V", "V Spitze", 0.5, "V", 0.01, 20, "am Stapel; über der Stromgrenze senkt die Software selbst"),
            ("settle_s", "Einschwingen", 8.0, "s", 0, 600, "etwa 5 τ des Lock-ins"),
            ("n", "Messungen je f", 3, "", 1, 100, ""),
            ("ac_off", "AC danach aus", 1, "0/1", 0, 1, ""),
        ],
    },
    "temperature_ramp": {
        "label": "Temperaturrampe mit Aufnahme",
        "help": "Das PPMS fährt mit der Rate, gemessen wird während der Rampe. Braucht PPMS-Steuerung.",
        "params": [
            ("T_to_K", "T Ziel", 250.0, "K", 1.8, 320, ""),
            ("rate_K_min", "Rate", 0.5, "K/min", 0.01, 10, "für AC-Messungen 0,3 bis 0,5 K/min"),
            ("dt_s", "Intervall", 5.0, "s", 1, 600, ""),
        ],
    },
}

MAX_STEPS = 4000


class TaskError(ValueError):
    pass


def definitions() -> dict:
    return {k: {"label": v["label"], "help": v["help"],
                "params": [dict(zip(("name", "label", "default", "unit", "min", "max", "help"), p)) for p in v["params"]]}
            for k, v in TASKS.items()}


def _params(name: str, given: dict) -> dict:
    if name not in TASKS:
        raise TaskError(f"unknown measurement {name!r}")
    out = {}
    for pname, label, dflt, unit, lo, hi, _ in TASKS[name]["params"]:
        v = given.get(pname, dflt)
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise TaskError(f"{label}: not a number")
        if not math.isfinite(v) or not lo <= v <= hi:
            raise TaskError(f"{label} = {v:g} {unit} outside {lo:g} ... {hi:g}")
        out[pname] = v
    return out


def _levels(a, b, step):
    n = int(math.floor(abs(b - a) / step + 1e-9))
    s = math.copysign(step, b - a) if b != a else 0.0
    lv = [a + i * s for i in range(n + 1)]
    if abs(lv[-1] - b) > 1e-9:
        lv.append(b)
    return lv


def build(name: str, given: dict, area_mm2: float) -> dict:
    """Schrittliste als Sequenz im üblichen Format. area_mm2: Probenquerschnitt."""
    p = _params(name, given)
    if area_mm2 <= 0:
        raise TaskError("sample area must be positive ([sample] area_mm2)")
    F = lambda s: round(s * area_mm2, 6)            # MPa · mm² = N
    steps = []
    if name == "stress_hold":
        steps = [{"op": "goto_force", "F": F(p["sigma_MPa"]), "tol": p["tol_N"], "hold": 5},
                 {"op": "measure", "n": max(1, int(p["duration_s"] // p["dt_s"])), "dt": p["dt_s"],
                  "label": f"hold {p['sigma_MPa']:g} MPa"}]
    elif name == "stress_sweep":
        up = _levels(p["from_MPa"], p["to_MPa"], p["step_MPa"])
        legs = [("hin", up)] + ([("zurück", up[-2::-1])] if p["back"] >= 0.5 else [])
        for leg, lv in legs:
            for s in lv:
                steps.append({"op": "goto_force", "F": F(s), "tol": p["tol_N"], "hold": p["hold_s"]})
                steps.append({"op": "measure", "n": int(p["n"]), "dt": p["dt_s"], "label": f"{leg} {s:g} MPa"})
    elif name == "frequency_sweep":
        steps = [{"op": "ac_on", "f": p["f_from_Hz"], "V": p["V"]},
                 {"op": "frequency_sweep", "from": p["f_from_Hz"], "to": p["f_to_Hz"], "points": int(p["points"]),
                  "settle": p["settle_s"], "n": int(p["n"]), "dt": 1, "label": "frequency sweep"}]
        if p["ac_off"] >= 0.5:
            steps.append({"op": "ac_off"})
    elif name == "temperature_ramp":
        steps = [{"op": "temperature_sweep", "to": p["T_to_K"], "rate": p["rate_K_min"], "dt": p["dt_s"],
                  "label": f"ramp to {p['T_to_K']:g} K"}]
    if len(steps) > MAX_STEPS:
        raise TaskError(f"{len(steps)} steps: use larger steps")
    return {"format": "stresscell-sequence", "version": 1, "name": name, "steps": steps,
            "task": {"name": name, "params": p}}
