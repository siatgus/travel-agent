#!/usr/bin/env bash
# Travel Agent data-admin launcher (macOS / Linux)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"
PY="C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
if [ -x "$PY" ]; then
  exec "$PY" -m travel_agent.admin --open
elif command -v python3 >/dev/null 2>&1; then
  exec python3 -m travel_agent.admin --open
else
  exec python -m travel_agent.admin --open
fi
