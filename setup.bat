@echo off
rem Einmal ausführen, aus einer Miniforge-Prompt (Python 3.11 oder neuer).
python -m venv backend\venv
backend\venv\Scripts\python.exe -m pip install -r backend\requirements.txt
