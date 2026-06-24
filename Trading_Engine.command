#!/bin/zsh
cd "/Users/yangdiandian/AI Stock" || exit 1

clear
echo "AI Stock 交易引擎"
echo "工作目录: /Users/yangdiandian/AI Stock"
echo "启动命令: /usr/bin/python3 trading_engine.py"
echo

if [[ -f STOP_TRADING ]]; then
  echo "注意: 检测到 STOP_TRADING，一键停止文件仍然存在。"
  echo "如需恢复运行，请先在项目目录执行: python3 trading_engine.py --clear-stop"
  echo
fi

running_processes="$(pgrep -fl 'python3.*trading_engine.py' | grep -v "$$" || true)"
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

exec /usr/bin/python3 trading_engine.py
