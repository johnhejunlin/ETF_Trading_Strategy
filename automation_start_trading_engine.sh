#!/bin/zsh
set -euo pipefail

cd "/Users/yangdiandian/AI Stock"

/usr/bin/python3 trading_engine.py --clear-stop

if /usr/bin/pgrep -fl 'python3.*trading_engine.py' >/dev/null 2>&1; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') trading_engine.py already running; skip start."
  exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') opening Trading_Engine.command."
/usr/bin/open "/Users/yangdiandian/AI Stock/Trading_Engine.command"
