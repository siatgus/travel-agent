@echo off
rem Travel Agent admin launcher (Windows)
rem Python absolute path (no system Python required on PATH)
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"

rem src layout: make "python -m" find the travel_agent package
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"
cd /d "%ROOT%"

if exist "%PY%" (
  "%PY%" -m travel_agent.admin --open
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    py -3 -m travel_agent.admin --open
  ) else (
    python -m travel_agent.admin --open
  )
)
pause
