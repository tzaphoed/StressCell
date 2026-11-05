# StressCell

Messprogramm für die Razorbill FC100 im PPMS, gebaut für AC-Elastokalorik an NiMnSn. Python-Backend,
austauschbare Treiber, Simulation, Sequenzer, Grenzwertprüfung an einer Stelle, Oberfläche im Browser.

**Das Handbuch:** [`docs/StressCell_Handbuch.pdf`](docs/StressCell_Handbuch.pdf), im Programm unter „Hilfe“ (F1).
Es erklärt Sicherheit, Zelle, Geräte, Verkabelung, Kalibrierung, Konfiguration und die Messung Schritt für Schritt.
Für den Laborschrank: [`docs/FC100_Geraeteauswahl.pdf`](docs/FC100_Geraeteauswahl.pdf).

## Start

```
setup.bat              einmal, aus einer Miniforge-Prompt (Python 3.11 oder neuer)
start.bat --sim 20     Simulation mit Zeitraffer 20, Oberfläche unter http://127.0.0.1:8020/
start.bat              nach config.toml, für echte Geräte simulate = false
```

Tests: `backend\venv\Scripts\python.exe -m pytest backend`

Geräte zum ersten Mal ansprechen, ohne etwas zu setzen:
`backend\venv\Scripts\python.exe backend\check_instruments.py`

Die Seite „Start“ in der Oberfläche zeigt live, was vor dem ersten Einsatz noch offen ist.

## Was eingebaut ist

- **Kalibrierung FC100 #0260** nach Razorbill-Blatt (QA 2024-10-02): `F = κ(T)·(α/(C − Cp) − f0(T))`,
  Temperaturgang des Nullpunkts, Nullpunkt im Vakuum per Tare. Datei `calibrations/FC100_0260.toml`,
  steht auf `verified = false`, bis die Zahlen am Papierblatt geprüft sind.
- **Eine Quelle je Stapel** (`supply = "split"`): SMU (Keithley 2400/2410/2420/2450/2600B, Keysight B2900),
  Verstärker am Lock-in-Aux, nur AC, geerdet, oder RP100. Ohne RP100 Pflicht: Bleeder je Stapel.
- **AC-Elastokalorik:** Lock-ins SR830, SR860, SR865A, SR7265/7270 oder Funktionsgenerator 33210A/33220A/33500B/33600A
  mit externer Referenz. AC-Kette mit Verstärkung und RC-Tiefpass, Phase gegen die Spannung am Stapel.
- **Kraft:** E4980A/E4980AL, HP 4284A, AH2500A/2550A/2700A, Lock-in mit Stromeingang. Sperre gegen Übersprechen der AH-Brücke.
- **Laden/Entladen:** Keithley 2182A, Keysight 34401A/34461A/34465A für die Thermospannung.
- **Oberfläche auf Deutsch:** Inbetriebnahme-Checkliste, Rechner, Kalibriertabelle über T, Gerätepanel, Erklärtexte, Handbuch.

## Aufbau

```
backend/
  server.py              FastAPI, REST + WebSocket /ws, Port 8020, Oberfläche unter /, Handbuch unter /help
  stresscell_client.py   Python-Zugang: Lab
  config.toml            Geräte, Grenzwerte, Pfade. Jede Zeile kommentiert
  check_instruments.py   Geräte nur lesen, offene Punkte der Konfiguration
  core/fc100.py          Spannungsfenster aus dem Datenblatt, Aufteilung Drive auf V_T und V_C
  core/safety.py         Grenzwertprüfung für jeden schreibenden Befehl
  core/supervisor.py     Abfrage, Guard, Kraftregler im selben Takt, Watchdog
  core/calibration.py    Kapazität -> Kraft (Razorbill-Formel und lineares Modell)
  core/acchain.py        Verstärkung und RC-Tiefpass der AC-Kette
  core/advisor.py        Inbetriebnahme-Checkliste und Rechenhilfen
  core/control.py        Kraftregler
  core/thermocouple.py   Seebeck Type E, NIST SRD 60
  core/sequence.py       Sequenzformat, Doku im Kopf der Datei
  core/interpreter.py    Ausführung, derselbe Code für Lauf, Trockenlauf und Python
  core/runner.py         Lauf, Sitzungen, Datenablage
  core/stack.py          alles zusammen, Trockenlauf
  instruments/base.py    Schnittstellen
  instruments/sim.py     Simulation
  instruments/real.py    RP100, Keithley-Paar, E4980/4284, AH-Brücken, SR860, PPMS, Zusammenbau
  instruments/sources.py eine Quelle je Stapel: SMU, Verstärker, nur AC
  instruments/lockins.py SR830, SR7265, Generator, Lock-in-Kapazität
  instruments/meters.py  Nanovoltmeter und DMM
frontend/renderer/       Oberfläche, Bibliotheken in vendor/
sequences/               Beispielsequenzen, ece_* für die Elastokalorik
calibrations/            Kalibrierung je Zelle
docs/                    Handbuch und Geräteauswahl (HTML-Quelle, build_docs.py, render_pdf.js)
```

## Vorzeichen und Polarität

D = V_T − V_C. D > 0 ist Zug. F > 0 ist Zug, die Kapazität fällt unter Zug.
Adern: rot Zug +, weiß Zug −, braun Druck +, blau Druck − (Datenblatt Tabelle 1).

## Stand

Über 250 Tests gegen Simulation, nachgebaute Geräte und den Simulationsmodus von MultiPyVu.
**An echter Hardware lief noch nichts.** Der erste Kontakt ist `check_instruments.py` mit `read_only = true`.

## Stoppen

STOP ALL und jeder Eingriff des Guards sperren weitere Schritte, bis jemand quittiert (Oberfläche:
Quittieren, Python: `lab.acknowledge()`). Zurückfahren bleibt immer erlaubt.

## Was die Software nicht abfangen kann

Stürzt der Rechner ab, hält die Quelle ihre Spannung. Wärmt der Kryostat dann auf, führt niemand
das Fenster nach. Unter 100 K nicht unbeaufsichtigt aufwärmen. `V_T` und `V_C` sind Kommandowerte,
keine Messung am Stapel. Das Token schützt gegen fremde Webseiten, nicht gegen andere Programme auf
demselben Rechner.
