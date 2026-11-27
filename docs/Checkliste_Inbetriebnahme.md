# Checkliste Inbetriebnahme (Aufbau B)

Zum Ausdrucken und Abhaken. Hintergrund und Begründungen: [`Aufbau_Entscheidungen.md`](Aufbau_Entscheidungen.md),
Handbuch Kapitel 5 (Verkabelung) und 9 (Schritt für Schritt).

## 1. Geräte und wo sie hängen

| ☐ | Gerät | Anschluss an der Zelle / am Aufbau | Anschluss zum PC | `config.toml` |
|---|---|---|---|---|
| ☐ | **Keithley 2400** (DC-Offset) | HI → **R1 = 15 kΩ** → **braun (+)** Druckstapel, LO → **blau (−)**. Front- oder Rückbuchsen: am Gerät dieselben wählen wie verkabelt | GPIB 24 | `[instruments.compression] address` |
| ☐ | Bleeder Druckstapel | **1 MΩ, ≥ 500 V** direkt zwischen braun und blau, zellseitig von R1 | | `bleeder_fitted = true` |
| ☐ | **SR830 #1** (AC-Anregung + Detektion) | SINE OUT → **R2 = 680 Ω** → **rot (+)** Zugstapel, Schirm SINE OUT → **weiß (−)** | GPIB 8 | `lockin`, `[ac] series_R_ohm = 730` |
| ☐ | Bleeder Zugstapel | **1 MΩ, ≥ 500 V** zwischen rot und weiß, zellseitig von R2 | | `bleeder_fitted = true` |
| ☐ | **SR560** (Vorverstärker) | Eingang A ← ein BNC vom Probenkopf (Innenleiter und Schirm = die beiden Thermoelementadern). OUTPUT (50 Ω) → SR830 #1 Eingang A | keiner | `[ac] preamp_gain` |
| ☐ | Thermoelement-Anschluss | beide Chromel-Enden auf zwei Lötpads nebeneinander (gleiche Temperatur), Kupfer-Koax zum BNC. Vor dem Abziehen BNC-Kurzschlusskappe drauf | | |
| ☐ | Thermoelement Type E, differentiell | Lötstelle 1 auf der Probe (elektrisch isoliert: GE-Lack/Stycast), Lötstelle 2 auf der Thermal Link Plate neben dem Cernox. Schleife klein und verdrillt | | |
| ☐ | **AH 2700A** (Kraft) | Koax direkt an den Kraftsensor, **gelbes Band = High**, **schwarz = Low**. Kein Schalter dazwischen, Schirme nicht an den Kryostaten | GPIB 28 | `capmeter`, `[capacitance] frequency_Hz` |
| ☐ | **Lake Shore 350** | Cernox 4-Leiter (I+, I−, V+, V−, paarweise verdrillt) an Eingang C, Sensor Type NTC RTD | GPIB 12 | `[cell_thermometer] input = "1"` |
| ☐ | **PPMS** | MultiVu läuft, darin der MultiPyVu-Server | Netzwerk, Port 27183 | `multipyvu_host` |
| ☐ | Oszilloskop | Tastkopf über den Zugstapel (hinter R2), **Masseklemme an weiß** (Lock-in-Seite), nie an die Keithley-Seite | | `[ac] gain_verified` |
| ☐ | optional SR830 #2 (2f) | TTL OUT von #1 → REF IN von #2, SR560-Ausgang per BNC-T an beide Eingänge | GPIB 9 | `lockin2` |
| ☐ | Masse | ein Sternpunkt. Keine zweite Erdverbindung über Gehäuse oder Schirme | | |

GPIB-Adressen sind Vorschläge: Jede Adresse darf nur einmal vorkommen. `check_instruments.py` listet, was der PC sieht.

## 2. SR560

| Einstellung | Wert | Warum |
|---|---|---|
| Stromversorgung | **Akku**, Netzkabel ab | Netzbrumm und Masseschleife über das Netzteil |
| Source | **A** | ein BNC: Innenleiter gegen Schirm. A − B nur bei zwei getrennten BNC |
| Coupling | **DC** | AC-Kopplung (0,03 Hz) dreht die Phase bei 1 Hz um etwa 2°. Bei Übersteuerung durch Thermo-Offsets: AC, und nur oberhalb von etwa 5 Hz messen |
| Filter | **Tiefpass 6 dB/Okt., Grenzfrequenz ≥ 100 × höchste Messfrequenz** (z. B. 100 kHz), **kein Hochpass** | Die Software rechnet die Filterphase des SR560 nicht heraus. Jeder Filter nahe f landet in der ECE-Phase |
| Gain | **100** zum Start, bei tiefer T 500 oder 1000 | Genau diesen Wert als `[ac] preamp_gain` eintragen, sonst ist ΔT um denselben Faktor falsch |
| Gain mode | **Low Noise** | |
| Invert | **aus** | Die Polung legt `[ac] tc_sign` fest (Zug kühlt weit weg vom Übergang) |
| OVLD-LED | darf nie leuchten | sonst Gain runter |

SR830 #1 dazu: Eingang **A**, Shield **Float**, Coupling DC (unter 160 mHz Pflicht, darüber egal), Reserve **Low Noise**.

Größenordnung: Type E hat bei 250 K rund 60 µV/K, bei tiefer T nur einige µV/K. 10 mK bei 250 K sind 0,6 µV am Thermoelement,
mit Gain 100 also 60 µV am SR830. Bei 10 K und 1 mK sind es wenige nV: dort Gain 1000 und lange Zeitkonstante.

## 3. Erstes Einschalten

Schutzschichten, von außen nach innen:

1. **`read_only = true`**: Jeder Setzbefehl wird abgelehnt. Erst alles lesen, dann weiter.
2. **Keithley 2400, Spannungsschutz am Gerät** (V-Source Protection, über CONFIG › SOURCE V): **20 V** (kleinste Stufe). Das ist die Hardwaregrenze, falls die Software versagt. StressCell setzt sie nicht zurück.
3. **`[limits] drive_cap_V`**: eigene Obergrenze je Stapel. Die Kraft kann nie größer werden als etwa **1,3 N/V × drive_cap_V** (Datenblatt: Blockierkraft 170 N bei 140 V). Also
   `drive_cap_V = zulässige Spannung × Querschnitt / 1,3 N/V`. Beispiel: 0,1 mm², 30 MPa → 3 N → **2,3 V**.
4. **`[limits] force_max_N`**: darüber fährt der Guard den Drive zurück, gemessen an der echten Kraft.
5. **`[force_loop] G0_N_per_V = 1.3`**: G überschätzt heißt: Der Regler macht zu kleine Schritte und schwingt nicht über.
6. **Probenbrucherkennung, STOP ALL, Rampe 1 V/s**: sind immer aktiv.

**Nicht vergessen:** Die Spannungsgrenzen begrenzen nur die Kraft, die die Piezos erzeugen. Beim Abkühlen baut sich durch die unterschiedliche
Kontraktion von Probe und Zelle auch bei 0 V Kraft auf. Erste Abkühlung möglichst ohne Probe, sonst die Kraft beobachten.

Ablauf:

- ☐ `simulate = false`, `read_only = true`, Adressen eingetragen, `check_instruments.py` ohne FAIL
- ☐ Kapazität bei 290 K in Luft ≈ 1,312 pF, PPMS liefert T, B und Kammer, der 350 liefert R an Eingang C
- ☐ Kalibrierblatt geprüft, `verified = true`
- ☐ Bleeder eingebaut und nachgemessen, `bleeder_fitted = true`
- ☐ Spannungsschutz am Keithley 20 V, `drive_cap_V` aus der Probengröße berechnet, `force_max_N` gesetzt
- ☐ `read_only = false`. Ausgänge an bei 0 V, von Hand 1 V Drive: Hat die Kraft das richtige Vorzeichen? Druck: D < 0, F < 0
- ☐ dF/dD messen (`measure_gain`), dann „Kraft halten“ mit 0,5 N
- ☐ Oszilloskop am Zugstapel: AC-Amplitude und Phase gegen Rechnung, `gain_verified = true`
- ☐ Auf HiVac pumpen, Tare mit „annehmen“
- ☐ Erst danach `drive_cap_V` hochsetzen, und nur so weit wie nötig

**AC und spröde Probe:** Die AC-Kraft muss kleiner bleiben als der Druck-Offset, sonst wird die Probe in jedem Zyklus auf Zug belastet.

## 4. Was noch zu messen und einzutragen ist

| ☐ | Was | Wohin |
|---|---|---|
| ☐ | Querschnitt (Breite × Dicke) und freie Länge der Probe zwischen den Klebestellen | `[sample] area_mm2`, `length_mm` |
| ☐ | Gemessene Werte von R1, R2 und den Bleedern | `series_R_ohm` (R2 + 50 Ω), `bleeder_R_ohm` |
| ☐ | Gain am SR560 | `[ac] preamp_gain` |
| ☐ | Messfrequenz der AH, mit Abstand zu f und 2f | `[capacitance] frequency_Hz` |
| ☐ | Seriennummer des Cernox und `.340`-Datei | `calibrations/`, `[cell_thermometer] calibration` |
| ☐ | Maximalfeld des Magneten | `[limits] B_max` |
| ☐ | Nachlauf der Zelle gegen das PPMS-Thermometer | `[limits] T_cell_tau_s` |
