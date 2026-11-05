"""Inbetriebnahme-Checkliste und Rechenhilfen für die Oberfläche.

checklist() schaut auf Konfiguration, Kalibrierung und den letzten Messtakt und sagt je Punkt,
ob er erledigt ist, was fehlt und warum es wichtig ist. Die Reihenfolge folgt dem Handbuch,
Kapitel "Schritt für Schritt". Nichts hier schaltet etwas: es sind nur Hinweise.

calc() rechnet die Zahlen, die man im Labor sonst auf einem Zettel ausrechnet.
"""

from __future__ import annotations

import math

from . import fc100
from .acchain import AcChain
from .thermocouple import seebeck_type_e

OK, OPEN, WARN, INFO = "ok", "open", "warn", "info"


def _item(key, title, state, detail, why, chapter=""):
    return {"key": key, "title": title, "state": state, "detail": detail, "why": why, "chapter": chapter}


def checklist(st) -> list[dict]:
    cfg, cal, last = st.cfg, st.cal, st.supervisor.last or {}
    lim, mode, i, ac = cfg["limits"], cfg["mode"], cfg["instruments"], cfg.get("ac", {})
    out = []

    if st.simulated:
        out.append(_item("mode", "Betriebsart", INFO, "Simulation. Nichts hier erreicht echte Geräte.",
                         "Erst in der Simulation üben, dann simulate = false.", "Software starten"))
    elif st.safety.read_only:
        out.append(_item("mode", "Betriebsart", WARN, "Echte Geräte, nur lesen. Jeder Setzbefehl wird abgelehnt.",
                         "So beginnt der erste Kontakt. Wenn alles plausibel aussieht: read_only = false.",
                         "Erster Kontakt"))
    else:
        out.append(_item("mode", "Betriebsart", OK, "Echte Geräte, Schreiben erlaubt.", "", "Erster Kontakt"))

    resp = {"Temperatur": last.get("T"), "Kapazität": last.get("C_pF"), "Netzteil": last.get("set_C")}
    missing = [k for k, v in resp.items() if v is None]
    out.append(_item("devices", "Geräte antworten", OK if not missing else OPEN,
                     "Alle antworten." if not missing else "Keine Antwort von: " + ", ".join(missing),
                     "Ohne Temperatur gilt das 300-K-Fenster, ohne Kraft kein Regler. check_instruments.py "
                     "zeigt Adressen und Fehler.", "Erster Kontakt"))

    if i.get("supply") == "split":
        bad = [c for c in ("tension", "compression")
               if i.get(c, {}).get("source") != "rp100" and not i.get(c, {}).get("bleeder_fitted")]
        out.append(_item("bleeder", "Entladewiderstände", OK if not bad else OPEN,
                         "Bestätigt." if not bad else "Nicht bestätigt: " + ", ".join(bad),
                         "Ohne RP100 entlädt niemand die Stapel, wenn ein Gerät aus ist. Pyroelektrische "
                         "Spannung beim Abkühlen schädigt sie (Datenblatt S. 14).", "Anschlussbox"))

    info = cal.info()
    if not info.get("verified"):
        out.append(_item("cal_verified", "Kalibrierung geprüft", OPEN,
                         f"{info.get('serial')}: verified = false. Kraftanzeige läuft, Kraftregler gesperrt.",
                         "Die Zahlen sind von einem Foto abgeschrieben. Mit dem Papierblatt vergleichen, "
                         "dann in der Kalibrierdatei verified = true.", "Kalibrierung"))
    else:
        out.append(_item("cal_verified", "Kalibrierung geprüft", OK, f"{info.get('serial')}", "", "Kalibrierung"))
    if info.get("model") == "razorbill":
        if not info.get("tared"):
            out.append(_item("tare", "Nullpunkt im Vakuum", OPEN,
                             "Noch kein Nullpunkt. Das Blatt gilt für Luft, im Vakuum liegt die Kraft "
                             "typisch einige 10 N daneben.",
                             "Nach 24 h Pumpen ohne Last bei Raumtemperatur 'Tare, Versatz annehmen'. Danach "
                             "korrigiert die Software die Temperatur selbst (AP006, S. 14).", "Kalibrierung"))
        else:
            out.append(_item("tare", "Nullpunkt im Vakuum", OK,
                             f"bei {info['T_ref_K']:.1f} K, Feuchteversatz {info['humidity_offset_N']:+.1f} N",
                             "", "Kalibrierung"))

    if ac.get("enabled"):
        out.append(_item("ac_gain", "AC-Kette gemessen", OK if ac.get("gain_verified") else OPEN,
                         f"gain = {ac.get('gain')}, R = {ac.get('series_R_ohm', 0):g} Ohm"
                         + ("" if ac.get("gain_verified") else ", nicht bestätigt"),
                         "Amplitude und Phase am Stapel mit dem Oszilloskop messen (Ersatzkondensator, dann "
                         "Zelle). Ohne das stimmen F_ac und dT/dsigma nicht.", "AC-Kette"))

    ch = last.get("chamber")
    full = ch in cfg["chamber"]["safe_states"]
    out.append(_item("vacuum", "Hochvakuum", OK if full else WARN,
                     f"Kammer: {ch or 'unbekannt'}" + ("" if full else ", Spannung begrenzt"),
                     "Volles Fenster nur in HiVac: Niederdruck-Helium bogt ab rund 100 V. Für die "
                     "Elastokalorik ist Hochvakuum ohnehin Pflicht, Gas ist ein Wärmeabfluss.", "Vakuum"))

    A, L = cfg["sample"].get("area_mm2", 0), cfg["sample"].get("length_mm", 0)
    geo_ok = 0.05 <= A <= 0.25 and 0.1 <= L <= 1.5
    out.append(_item("sample", "Probengeometrie", OK if geo_ok else WARN,
                     f"{A:g} mm², {L:g} mm lang",
                     "Datenblatt FC100: typisch 0,05 bis 0,25 mm² und 0,1 bis 1,5 mm. Kleiner Querschnitt "
                     "heißt große Spannung bei kleiner Kraft.", "Probe"))
    Fmax = lim["force_max_N"]
    out.append(_item("force_max", "Kraftgrenze für diese Probe", INFO,
                     f"{Fmax:g} N = {Fmax / A if A else float('nan'):.0f} MPa bei {A:g} mm²",
                     "NiMnSn ist spröde. Grenze unter der Bruchspannung wählen, nur auf Druck belasten.",
                     "Probe"))

    guesses = []
    if lim.get("T_cell_tau_s") == 300.0:
        guesses.append("T_cell_tau_s")
    if lim.get("current_trip_A") == 2e-3:
        guesses.append("current_trip_A")
    out.append(_item("measured", "Geschätzte Grenzwerte gemessen", OK if not guesses else WARN,
                     "Alle eingetragen." if not guesses else "Noch Schätzwerte: " + ", ".join(guesses),
                     "T_cell_tau_s: wie träge die Zelle dem Thermometer folgt. current_trip_A: Strom, ab dem "
                     "ein Lichtbogen angenommen wird. Beides am Aufbau bestimmen.", "Grenzwerte"))
    return out


def calc(cfg, q: dict) -> dict:
    """Rechenhilfen. q: f, V, T, F, A_mm2, V_tc_uV, R2, gain, preamp."""
    ac = cfg.get("ac", {})
    f = float(q.get("f", 21.0))
    V = float(q.get("V", 1.0))
    T = float(q.get("T", 300.0))
    F = float(q.get("F", 1.0))
    A = float(q.get("A_mm2", cfg["sample"].get("area_mm2", 0.1)))
    L = float(q.get("L_mm", cfg["sample"].get("length_mm", 1.0)))
    C = float(ac.get("C_F", 3.1e-6))
    chain = AcChain(float(q.get("gain", ac.get("gain", math.sqrt(2)))), float(q.get("R2", ac.get("series_R_ohm", 0.0))), C)
    Imax = float(ac.get("current_max_A", 5e-3))
    vmin, vmax = fc100.voltage_window(T)
    S = seebeck_type_e(T)
    V_tc = float(q.get("V_tc_uV", 1.0)) * 1e-6 / float(q.get("preamp", 1.0))
    return {
        "I_stack_mA": 2 * math.pi * f * C * V * 1e3,
        "V_peak_max_V": Imax / (2 * math.pi * f * C),
        "out_rms_V": chain.out_rms(f, V),
        "corner_Hz": chain.corner_Hz(),
        "phase_RC_deg": chain.phase_deg(f),
        "stress_MPa": F / A if A else None,
        # Obergrenze: 1,5e-4 je V Spitze an 1 mm bei 300 K, fünfmal weniger bei 4 K. Epoxid schluckt einen Teil.
        "strain_est": 1.5e-4 * V * (0.2 + 0.8 * min(max((T - 4.0) / 296.0, 0.0), 1.0)) / max(L, 1e-3),
        "seebeck_uV_K": S * 1e6,
        "dT_mK": V_tc / S * 1e3,
        "window_V": [vmin, vmax],
        "drive_max_V": vmax - vmin,
    }
