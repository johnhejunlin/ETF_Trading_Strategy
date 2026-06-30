#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

cd "$SCRIPT_DIR"

"$PYTHON_BIN" trading_engine.py --clear-stop

if pgrep -fl 'python3.*trading_engine.py' >/dev/null 2>&1; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') trading_engine.py already running; skip start."
  exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') opening Trading_Engine.command."
open "$SCRIPT_DIR/Trading_Engine.command"
