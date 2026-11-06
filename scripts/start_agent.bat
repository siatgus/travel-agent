@echo off
rem Travel Agent local launcher (Windows)
rem Python absolute path (no system Python required on PATH)
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"

rem LLM fallback (DeepSeek, OpenAI-compatible). Delete these lines to go rule-only.
rem Note: settings saved in admin console (port 8788) take priority over these.
rem 安全提示：不要把真实 API Key 提交进仓库，填你自己的 Key 或改用管理后台保存。
set "OPENAI_API_KEY=sk-在这里填你自己的KEY"
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
