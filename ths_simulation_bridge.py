#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill 同花顺 Mac 模拟交易 ticket from trading_engine intent.")
    parser.add_argument("--intent", required=True, help="Path to latest_order_intent.json")
    parser.add_argument("--verification", required=True, help="Path to write verified order fields")
    args = parser.parse_args()

    intent_path = Path(args.intent)
    verification_path = Path(args.verification)
    payload = json.loads(intent_path.read_text(encoding="utf-8"))
    order = payload["order"]

    symbol = str(order["symbol"])
    side = str(order["side"]).upper()
    quantity = str(int(order["quantity"]))
    limit_price = f"{float(order['limit_price']):.4f}".rstrip("0").rstrip(".")

    script = build_applescript(symbol, side, quantity, limit_price)
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return result.returncode
    actual = parse_bridge_output(result.stdout)

    verification_path.parent.mkdir(parents=True, exist_ok=True)
    verification_path.write_text(
        json.dumps(
            {
                "account_mode": "simulation",
                "symbol": actual.get("symbol") or symbol,
                "side": side,
                "quantity": int(actual.get("quantity") or quantity),
                "limit_price": float(actual.get("limit_price") or limit_price),
                "expected_symbol": symbol,
                "expected_quantity": int(quantity),
                "expected_limit_price": float(limit_price),
                "source": "ths_simulation_bridge",
                "submitted": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    time.sleep(0.3)
    return 0


def parse_bridge_output(output: str) -> dict[str, str]:
    fields = {}
    for item in output.strip().split("|"):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def build_applescript(symbol: str, side: str, quantity: str, limit_price: str) -> str:
    side_button = "买入" if side == "BUY" else "卖出"
    return f'''
    tell application "同花顺" to activate
    delay 0.5
    tell application "System Events"
        tell process "同花顺"
            set frontmost to true
            my waitForTradePanel()
            tell window 1
                click button "模拟"
                delay 0.3
                click button "{side_button}"
                delay 0.3
                if exists button "重填" then click button "重填"
                delay 0.3
                set value of text field 2 to "{symbol}"
                delay 0.2
                set value of text field 1 to "{limit_price}"
                delay 0.2
                set value of text field 3 to "{quantity}"
                delay 0.2
                set actualPrice to value of text field 1
                set actualSymbol to value of text field 2
                set actualQuantity to value of text field 3
            end tell
        end tell
    end tell
    return "symbol=" & actualSymbol & "|limit_price=" & actualPrice & "|quantity=" & actualQuantity

    on waitForTradePanel()
        tell application "System Events"
            tell process "同花顺"
                repeat with attempt from 1 to 12
                    if exists window 1 then
                        tell window 1
                            if exists button "登 录" then
                                click button "登 录"
                                delay 3
                            end if
                            if (exists button "模拟") and (exists button "买入") and (exists button "卖出") then
                                return
                            end if
                        end tell
                    end if
                    click at {{20, 276}}
                    delay 1
                end repeat
                error "未能定位同花顺交易/模拟面板"
            end tell
        end tell
    end waitForTradePanel
    '''


if __name__ == "__main__":
    raise SystemExit(main())
