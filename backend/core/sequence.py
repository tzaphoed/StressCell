"""Sequenzformat und statische Prüfung.

Datei (*.seq.json):

    {"format": "stresscell-sequence", "version": 1, "name": "...",
     "steps": [...], "editor": {...}}      # editor: Blockly-Workspace, vom Sequenzer ignoriert

Zahlenfelder dürfen Ausdrücke sein: "2*F0 + 1". Erlaubt sind die Grundrechenarten,
Vergleiche, min, max, abs, log, exp, sqrt, Variablen und die Messwerte T, B, F, C, D.

    {"op": "set_temperature", "T": <K>, "rate": <K/min>, "mode": "fast_settle"}
    {"op": "wait_temperature", "tol": <K>, "hold": <s>, "timeout": <s>}
    {"op": "set_field", "B": <T>, "rate": <T/min>, "persistent": false}
    {"op": "wait_field", "tol": <T>, "timeout": <s>}
    {"op": "set_chamber", "mode": "purge_seal"}
    {"op": "output", "on": true}
    {"op": "set_voltages", "tension": <V>, "compression": <V>, "slew": <V/s>}
    {"op": "set_drive", "D": <V>, "slew": <V/s>}          # D = V_T - V_C, positiv ist Zug
    {"op": "wait_voltage", "timeout": <s>}
    {"op": "zero_drive", "slew": <V/s>}
    {"op": "tare", "accept": false}                       # nur bei Drive nahe 0, ohne Last auf der Probe
    {"op": "goto_force", "F": <N>, "tol": <N>, "hold": <s>, "timeout": <s>}
                                                          # Regler bleibt danach an
    {"op": "release_force"}                               # Regler aus, Drive bleibt
    {"op": "measure", "n": <Anzahl>, "dt": <s>, "label": "<text>"}
    {"op": "force_sweep", "from": <N>, "to": <N>, "points": <n>, "tol": <N>, "hold": <s>,
     "timeout": <s>, "n": <Anzahl>, "dt": <s>, "label": "<text>"}
    {"op": "measure_gain", "step": <V>, "settle": <s>}    # dF/dD für die Kraftamplitude der Anregung
    {"op": "ac_on", "f": <Hz>, "V": <V Spitze am Stapel>}
    {"op": "ac_off"}
    {"op": "frequency_sweep", "from": <Hz>, "to": <Hz>, "points": <n>, "settle": <s>, "n": <Anzahl>,
     "dt": <s>, "label": "<text>"}                        # logarithmisch, sucht das Plateau
    {"op": "temperature_sweep", "to": <K>, "rate": <K/min>, "dt": <s>, "label": "<text>"}
                                                          # misst während der Rampe
    {"op": "for_range", "var": "T", "from": <x>, "to": <x>, "points": <n>,
     "spacing": "lin" | "log", "do": [...]}
    {"op": "repeat", "times": <n>, "do": [...]}
    {"op": "set_variable", "name": "x", "value": <expr>}
    {"op": "wait", "seconds": <s>}
    {"op": "log", "message": "<text>"}                    # {x} setzt Variablen ein
    {"op": "pause", "message": "<text>"}                  # wartet auf Resume
"""

from __future__ import annotations

FORMAT = "stresscell-sequence"

# op -> (Pflichtfelder, optionale Felder)
OPS: dict[str, tuple[tuple, tuple]] = {
    "set_temperature": (("T",), ("rate", "mode")),
    "wait_temperature": ((), ("tol", "hold", "timeout")),
    "set_field": (("B",), ("rate", "persistent")),
    "wait_field": ((), ("tol", "timeout")),
    "set_chamber": (("mode",), ()),
    "output": (("on",), ()),
    "set_voltages": (("tension", "compression"), ("slew",)),
    "set_drive": (("D",), ("slew",)),
    "wait_voltage": ((), ("timeout",)),
    "zero_drive": ((), ("slew",)),
    "tare": ((), ("accept",)),
    "goto_force": (("F",), ("tol", "hold", "timeout")),
    "release_force": ((), ()),
    "measure": ((), ("n", "dt", "label")),
    "force_sweep": (("from", "to", "points"), ("tol", "hold", "timeout", "n", "dt", "label")),
    "measure_gain": ((), ("step", "settle")),
    "ac_on": (("f", "V"), ()),
    "ac_off": ((), ()),
    "frequency_sweep": (("from", "to", "points"), ("settle", "n", "dt", "label")),
    "temperature_sweep": (("to",), ("rate", "dt", "label")),
    "for_range": (("var", "from", "to", "points", "do"), ("spacing",)),
    "repeat": (("times", "do"), ()),
    "set_variable": (("name", "value"), ()),
    "wait": (("seconds",), ()),
    "log": (("message",), ()),
    "pause": ((), ("message",)),
}
CONTAINERS = ("for_range", "repeat")
COMMON = ("op", "id", "comment")


def check_steps(steps, path="steps") -> list[str]:
    errors = []
    if not isinstance(steps, list):
        return [f"{path}: must be a list"]
    for i, st in enumerate(steps):
        here = f"{path}[{i}]"
        if not isinstance(st, dict):
            errors.append(f"{here}: must be an object")
            continue
        op = st.get("op")
        if op not in OPS:
            errors.append(f"{here}: unknown op {op!r}")
            continue
        req, opt = OPS[op]
        here = f"{here} {op}"
        for k in req:
            if k not in st:
                errors.append(f"{here}: missing {k}")
        for k in st:
            if k not in req and k not in opt and k not in COMMON:
                errors.append(f"{here}: unknown field {k}")
        if op in CONTAINERS and "do" in st:
            errors += check_steps(st["do"], f"{here}.do")
    return errors


def check_sequence(seq: dict) -> list[str]:
    if not isinstance(seq, dict):
        return ["sequence must be an object"]
    errors = []
    if seq.get("format") != FORMAT:
        errors.append(f"format must be {FORMAT}")
    return errors + check_steps(seq.get("steps"))
