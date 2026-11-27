# PyInstaller: StressCell als Ordner mit StressCell.exe und allen Abhängigkeiten.
#   pip install pyinstaller
#   pyinstaller packaging/stresscell.spec --noconfirm
# Ergebnis: dist/StressCell/  (diesen Ordner kopieren, z. B. nach C:\StressCell, nicht nach Programme)
# Gebaut wird auf Windows, automatisch über .github/workflows/build-exe.yml.

import os
import shutil
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
BACKEND = os.path.join(ROOT, "backend")
TOOLS = os.path.join(ROOT, "tools")
sys.path[:0] = [BACKEND, TOOLS]

hidden = ["server", "check_instruments", "lakeshore_curve", "cernox_fit", "entropy_map",
          "pyvisa_py", "serial", "serial.tools.list_ports"]
hidden += collect_submodules("core") + collect_submodules("instruments")
hidden += collect_submodules("uvicorn") + collect_submodules("pyvisa_py")
datas, binaries = [], []
for pkg in ("MultiPyVu",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hidden += h
if sys.platform == "win32":
    # MultiPyVu verlangt unter Windows win32com und dazu pywin32.version.txt in sysconfig 'platlib'.
    # In der exe zeigt platlib auf _internal/Lib/site-packages.
    import sysconfig
    hidden += ["win32com", "win32com.client", "pythoncom", "pywintypes"]
    ver = os.path.join(sysconfig.get_path("platlib"), "pywin32.version.txt")
    if not os.path.exists(ver):
        raise SystemExit(f"pywin32 fehlt ({ver}): pip install pywin32")
    datas.append((ver, os.path.join("Lib", "site-packages")))

a = Analysis([os.path.join(BACKEND, "stresscell_app.py")],
             pathex=[BACKEND, TOOLS],
             hiddenimports=hidden,
             datas=datas,
             binaries=binaries,
             excludes=["pytest", "_pytest"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="StressCell", console=True,
          icon=None, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="StressCell", upx=False)

# Alles, was man ändern oder lesen will, liegt neben der exe und nicht im entpackten Code
OUT = os.path.join(DISTPATH, "StressCell")


def put(src, dst):
    s, d = os.path.join(ROOT, src), os.path.join(OUT, dst)
    if os.path.isdir(s):
        shutil.copytree(s, d, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(s, d)


put("backend/config.toml", "backend/config.toml")
put("frontend/renderer", "frontend/renderer")
put("calibrations", "calibrations")
put("sequences", "sequences")
put("docs/handbuch/handbuch.html", "docs/handbuch/handbuch.html")
for f in ("StressCell_Handbuch.pdf", "FC100_Geraeteauswahl.pdf", "Aufbau_Entscheidungen.md", "Checkliste_Inbetriebnahme.md"):
    put(os.path.join("docs", f), os.path.join("docs", f))
put("README.md", "README.md")
put("packaging/LIESMICH.txt", "LIESMICH.txt")
for name, args in (("StressCell starten.bat", ""), ("Simulation.bat", " --sim 20"),
                   ("Geraete pruefen.bat", " check")):
    with open(os.path.join(OUT, name), "w", encoding="ascii", newline="\r\n") as f:
        f.write(f'@echo off\ncd /d "%~dp0"\nStressCell.exe{args}\npause\n')
