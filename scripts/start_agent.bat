@echo off
rem Travel Agent local launcher (Windows)
rem Python absolute path (no system Python required on PATH)
set "PY=C:\Users\SMZJ\.workbuddy\binaries\python\versions\3.13.12\python.exe"

rem LLM fallback (DeepSeek, OpenAI-compatible). Delete these lines to go rule-only.
rem Note: settings saved in admin console (port 8788) take priority over these.
rem Security: do NOT commit real API keys. Fill in your own key or use the admin console.
set "OPENAI_API_KEY=sk-REPLACE-WITH-YOUR-KEY"
set "OPENAI_BASE_URL=https://api.deepseek.com"
set "OPENAI_MODEL=deepseek-chat"
set "OPENAI_TIMEOUT=60"

rem src layout: make "python -m" find the travel_agent package; runtime data in data/
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"
cd /d "%ROOT%"

if exist "%PY%" (
  "%PY%" -m travel_agent.server --open
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    py -3 -m travel_agent.server --open
  ) else (
    python -m travel_agent.server --open
  )
)
pause
