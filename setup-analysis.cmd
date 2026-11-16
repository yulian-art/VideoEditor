@echo off
setlocal
cd /d "%~dp0"
py -3.12 -m venv .analysis-venv
if errorlevel 1 (
  echo Python 3.12 is required for this setup. Install it first or configure an existing analysis Python.
  exit /b 1
)
.analysis-venv\Scripts\python.exe -m pip install -r requirements-analysis.txt
if errorlevel 1 exit /b 1
.analysis-venv\Scripts\python.exe scripts\download_semantic.py
if errorlevel 1 exit /b 1
echo Configure this interpreter in the application:
echo %CD%\.analysis-venv\Scripts\python.exe
echo Select a local faster-whisper model directory in the analysis settings.
endlocal
