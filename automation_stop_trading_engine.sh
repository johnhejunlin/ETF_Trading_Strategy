#!/bin/zsh
set -euo pipefail

cd "/Users/yangdiandian/AI Stock"

echo "$(date '+%Y-%m-%d %H:%M:%S') requesting trading engine stop."
/usr/bin/python3 trading_engine.py --stop
