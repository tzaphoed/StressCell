"""Einstieg für die exe (PyInstaller). Ein Programm, drei Aufgaben:

    StressCell.exe                    Oberfläche starten, Browser öffnet sich
    StressCell.exe --sim 20           Simulation mit Zeitraffer 20
    StressCell.exe check              Geräte nur lesen (check_instruments.py)
    StressCell.exe curve <Datei> ...  Kalibrierkurve in den Lake Shore 370 laden (tools/lakeshore_curve.py)
    StressCell.exe fit <data.csv> <kurve.csv>   Cernox gegen das PPMS kalibrieren (tools/cernox_fit.py)
    StressCell.exe entropy --cp ... --data ...   S(T, sigma) (tools/entropy_map.py)

Konfiguration: backend\\config.toml neben der exe. Daten: data\\, Logs: logs\\.
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "check":
        del sys.argv[1]
        import check_instruments
        check_instruments.main()
        return
    if cmd == "curve":
        del sys.argv[1]
        import lakeshore_curve
        sys.exit(lakeshore_curve.main())
    if cmd == "fit":
        del sys.argv[1]
        import cernox_fit
        cernox_fit.main()
        return
    if cmd == "entropy":
        del sys.argv[1]
        import entropy_map
        entropy_map.main(sys.argv[1:])
        return
    import server
    if "--no-browser" in sys.argv:
        sys.argv.remove("--no-browser")
    else:
        def later():
            time.sleep(3.0)
            cfg = server.State.cfg or {"server": {"host": "127.0.0.1", "port": 8020}}
            webbrowser.open(f"http://{cfg['server']['host']}:{cfg['server']['port']}/")
        threading.Thread(target=later, daemon=True).start()
    print("StressCell läuft. Oberfläche: http://127.0.0.1:8020/   Beenden: dieses Fenster schließen oder Strg+C")
    server.main()


if __name__ == "__main__":
    main()
