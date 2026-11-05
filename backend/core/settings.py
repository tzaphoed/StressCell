"""Schalter aus der Oberfläche in config.toml schreiben, ohne Kommentare zu verlieren.

Nur die Schalter unter SETTINGS sind von der Oberfläche aus änderbar. Grenzwerte und Geräte
bleiben Handarbeit in config.toml: Wer die ändert, soll die Datei vor sich haben.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# Schlüssel -> (Abschnitt, Typ, Erklärung)
SETTINGS = {
    "simulate": ("mode", bool, "Simulation statt echter Geräte"),
    "sim_speed": ("mode", float, "Zeitraffer der Simulation"),
    "read_only": ("mode", bool, "Nur lesen: jeder Setzbefehl wird abgelehnt"),
    "ppms_read": ("mode", bool, "PPMS-Daten lesen. Aus: keine Temperatur, engstes Spannungsfenster"),
    "ppms_control": ("mode", bool, "StressCell stellt T, B und Kammer. Aus: das macht MultiVu"),
    "ppms_emergency_hold": ("mode", bool, "Notfall: Aufwärmen anhalten, wenn die Spannung nicht fällt"),
}


def current(cfg: dict) -> dict:
    return {k: cfg.get(sec, {}).get(k, True if typ is bool else 1.0) for k, (sec, typ, _) in SETTINGS.items()}


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(float(v))
    return '"' + str(v).replace('"', '\\"') + '"'


def validate(changes: dict) -> dict:
    out = {}
    for k, v in changes.items():
        if k not in SETTINGS:
            raise ValueError(f"setting {k!r} cannot be changed here")
        typ = SETTINGS[k][1]
        if typ is bool:
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false")
        else:
            v = float(v)
            if not 0.1 <= v <= 1000.0:
                raise ValueError(f"{k} out of range")
        out[k] = v
    return out


def write(path: Path, changes: dict) -> None:
    """Werte in ihren Abschnitt schreiben. Kommentar am Zeilenende bleibt stehen, fehlende
    Schlüssel kommen direkt unter die Abschnittsüberschrift."""
    changes = validate(changes)
    text = Path(path).read_text(encoding="utf-8")
    for k, v in changes.items():
        sec = SETTINGS[k][0]
        m = re.search(rf"^\[{re.escape(sec)}\][^\n]*\n", text, re.M)
        if m is None:
            text += f"\n[{sec}]\n{k} = {_fmt(v)}\n"
            continue
        start = m.end()
        nxt = re.search(r"^\[", text[start:], re.M)
        end = start + nxt.start() if nxt else len(text)
        block = text[start:end]
        line = re.compile(rf"^({re.escape(k)}\s*=\s*)([^#\n]*?)(\s*(#.*)?)$", re.M)
        if line.search(block):
            block = line.sub(lambda mm: mm.group(1) + _fmt(v) + mm.group(3), block, count=1)
        else:
            block = f"{k} = {_fmt(v)}\n" + block
        text = text[:start] + block + text[end:]
    tomllib.loads(text)                     # nie eine kaputte Datei hinterlassen
    Path(path).write_text(text, encoding="utf-8")
