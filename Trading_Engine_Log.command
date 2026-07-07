#!/usr/bin/env zsh
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

cd "$SCRIPT_DIR" || exit 1

clear
echo "AI Stock 交易引擎启动 + 实时日志"
echo "工作目录: $SCRIPT_DIR"
echo "日志文件: $SCRIPT_DIR/trading_engine.log"
echo "停止交易引擎: python3 trading_engine.py --stop"
echo "----------------------------------------"
echo

"$PYTHON_BIN" trading_engine.py --clear-stop >/dev/null 2>&1 || true

echo "$(date '+%Y-%m-%d %H:%M:%S') 打开同花顺至尊版..."
open -a "同花顺至尊版" >/dev/null 2>&1 || open "/Applications/同花顺至尊版.app" >/dev/null 2>&1 || true

touch trading_engine.log

running_processes="$(ps ax -o pid=,command= | awk -v self="$$" '$1 != self && $2 ~ /(^|\\/)python[0-9.]*$/ && $0 ~ /trading_engine\\.py/ {print}' || true)"
if [[ -n "$running_processes" ]]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') trading_engine.py 已在运行，直接进入实时日志。"
  echo
  echo "关闭此窗口不会自动停止既有后台引擎；如需停止请执行: python3 trading_engine.py --stop"
  echo "----------------------------------------"
  exec tail -n 80 -f trading_engine.log
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') 前台启动 trading_engine.py，当前窗口即实时日志。"
  echo "按 Ctrl+C 可停止本次前台引擎；也可另开终端执行: python3 trading_engine.py --stop"
  echo "----------------------------------------"
  exec "$PYTHON_BIN" trading_engine.py
fi
