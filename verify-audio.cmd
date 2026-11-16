@echo off
setlocal
cd /d "%~dp0"
if "%~2"=="" (
  echo Usage: verify-audio.cmd source.m4a exported.mp4
  exit /b 2
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 alignstudio\mp4aac.py "%~1" "%~2"
) else (
  python alignstudio\mp4aac.py "%~1" "%~2"
)
endlocal
