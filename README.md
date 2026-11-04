# StressCell

Messprogramm für die Razorbill FC100 im PPMS. Aufbau wie SpecHeat: Python-Backend,
austauschbare Treiber, Simulation, Sequenzer, Grenzwertprüfung an einer Stelle.

Anleitung zu Geräten und Einbau: das Dokument "FC100 im PPMS: Anleitung".

## Start

```
setup.bat              einmal, aus einer Miniforge-Prompt (Python 3.11 oder neuer)
start.bat --sim 20     Simulation mit Zeitraffer 20, Oberfläche unter http://127.0.0.1:8020/
start.bat              nach config.toml, für echte Geräte simulate = false
```

Tests: `backend\venv\Scripts\python.exe -m pytest backend`

Geräte zum ersten Mal ansprechen, ohne etwas zu setzen:
`backend\venv\Scripts\python.exe backend\check_instruments.py`

## Aufbau

```
backend/
  server.py              FastAPI, REST + WebSocket /ws, Port 8020, Oberfläche unter /
  stresscell_client.py   Python-Zugang: Lab
  config.toml            Grenzwerte, Pfade, Geräteadressen
  check_instruments.py   Geräte nur lesen
  core/fc100.py          Spannungsfenster aus dem Datenblatt, Aufteilung Drive auf V_T und V_C
  core/safety.py         Grenzwertprüfung für jeden schreibenden Befehl
  core/supervisor.py     Abfrage, Guard, Kraftregler im selben Takt, Watchdog
  core/control.py        Kraftregler
  core/calibration.py    Kapazität -> Kraft
  core/thermocouple.py   Seebeck Type E, NIST SRD 60
  core/sequence.py       Sequenzformat, Doku im Kopf der Datei
  core/interpreter.py    Ausführung, derselbe Code für Lauf, Trockenlauf und Python
  core/runner.py         Lauf, Sitzungen, Datenablage
  core/stack.py          alles zusammen, Trockenlauf
  instruments/base.py    Schnittstellen
  instruments/sim.py     Simulation von Zelle, Netzteil, Kapazitätsmessung und PPMS
  instruments/real.py    Treiber
sequences/ece_*         AC-Elastokalorik: Plateau suchen, Temperatursweeps je Feld
frontend/renderer/       Oberfläche, läuft im Browser, Bibliotheken liegen in vendor/
sequences/               Beispielsequenzen
scripts/                 Beispielskript
calibrations/            Kalibrierung je Zelle
```

## Vorzeichen

D = V_T - V_C. D > 0 ist Zug. F > 0 ist Zug, sofern das Vorzeichen in der Kalibrierdatei stimmt.

## Vor dem ersten Einsatz an echten Geräten

1. In `config.toml`: `simulate = false`, `read_only = true`, Adressen, `force_max_N` für die Probe.
2. `check_instruments.py` laufen lassen.
3. Kalibrierdatei vom Blatt der Zelle ausfüllen, Vorzeichen prüfen, `verified = true`.
4. Erst dann `read_only = false`.

Für die AC-Anregung zusätzlich: Verstärkung der Kette am Stapel mit dem Oszilloskop messen,
`[ac] gain` eintragen, `gain_verified = true`. Für den Aufbau ohne RP100 auch `amp_gain_verified`
und `amp_discharge_box_fitted`.

Vor dem Betrieb messen oder setzen: `T_cell_tau_s`, `current_trip_A`, `force_noise_N`, `force_max_N`.
Mit Niederdruck-He gilt ±80 V je Kanal (`sealed_V_max`), das volle Fenster nur in HiVac.

Stand: 193 Tests gegen Simulation, nachgebaute Geräte und den Simulationsmodus von MultiPyVu,
zwei unabhängige Prüfrunden. An echter Hardware lief es noch nicht. Im Windows-Betrieb ist nur der
Code geprüft, nicht `setup.bat` und `start.bat`.

## Stoppen

STOP ALL und jeder Eingriff des Guards sperren weitere Schritte, bis jemand quittiert (Oberfläche:
Acknowledge, Python: `lab.acknowledge()`). Zurückfahren bleibt immer erlaubt.

## Was die Software nicht abfangen kann

Stürzt der Rechner ab, hält das RP100 seine Spannung. Wärmt der Kryostat dann auf, führt niemand
das Fenster nach. Beim normalen Beenden fährt das Backend vorher auf 0 V. `V_T` und `V_C` sind die
Kommandowerte des Netzteils, keine Messung am Stapel. Das Token schützt gegen fremde Webseiten,
nicht gegen andere Programme auf demselben Rechner.
