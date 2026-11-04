@echo off
start "" http://127.0.0.1:8020/
backend\venv\Scripts\python.exe backend\server.py %*
