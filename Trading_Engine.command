#!/usr/bin/env zsh
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

cd "$SCRIPT_DIR" || exit 1

clear
echo "AI Stock 交易引擎"
echo "工作目录: $SCRIPT_DIR"
echo "启动命令: $PYTHON_BIN trading_engine.py"
echo

echo "打开同花顺至尊版..."
open -a "同花顺至尊版" >/dev/null 2>&1 || open "/Applications/同花顺至尊版.app" >/dev/null 2>&1 || true
echo

if [[ -f STOP_TRADING ]]; then
  echo "注意: 检测到 STOP_TRADING，一键停止文件仍然存在。"
  echo "如需恢复运行，请先在项目目录执行: python3 trading_engine.py --clear-stop"
  echo
fi

running_processes="$(ps ax -o pid=,command= | awk -v self="$$" '$1 != self && $0 ~ /[[:space:]]trading_engine[.]py([[:space:]]|$)/ && $0 !~ /--status/ && $0 !~ /awk / {print}' || true)"
if [[ -n "$running_processes" ]]; then
  echo "检测到交易引擎可能已经在运行:"
  echo "$running_processes"
  echo
  read "answer?仍要再启动一个新实例吗？输入 YES 继续: "
  if [[ "$answer" != "YES" ]]; then
    echo "已取消启动。"
    echo "关闭这个窗口即可。"
    exit 0
  fi
fi

echo "账户同步由 trading_engine 发起 Codex Computer Use 请求；若快照缺失或过期，引擎会等待回写。"
echo

exec "$PYTHON_BIN" trading_engine.py
