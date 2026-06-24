import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import trading_engine
import ths_simulation_bridge


class FixedClock(trading_engine.TradingClock):
    def __init__(self, value: datetime) -> None:
        super().__init__("Asia/Shanghai", [["09:15", "11:30"], ["13:00", "15:15"]])
        self.value = value

    def now(self) -> datetime:
        return self.value


def base_config() -> dict:
    return {
        "symbols": ["588330"],
        "timezone": "Asia/Shanghai",
        "trading_sessions": [["09:15", "11:30"], ["13:00", "15:15"]],
        "portfolio": {"initial_cash": 50000},
        "risk": {"max_orders_per_day": 1, "allowed_symbols": ["588330"]},
        "execution": {
            "mode": "dry_run",
            "stage": "dry_run",
            "ths_account_mode": "simulation",
            "live_account_enabled": False,
            "max_live_cash": 50000,
            "small_live_cash": 5000,
            "stage_cash_limits": {
                "dry_run": 50000,
                "gui_simulation": 50000,
                "small_live": 5000,
                "full_live": 50000,
            },
            "require_screenshot_verification": True,
            "final_confirm_enabled": False,
            "price_tolerance": 0.001,
            "verification_fields_path": "",
            "gui_bridge_command": "",
        },
        "runtime": {"trade_day_schedule_enabled": True},
    }


class TradingEngineSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_stop_path = trading_engine.STOP_PATH
        trading_engine.STOP_PATH = Path(self.tempdir.name) / "STOP_TRADING"
        self.portfolio = trading_engine.PortfolioStore(
            Path(self.tempdir.name) / "portfolio.json",
            ["588330"],
            50000,
        )
        self.clock = FixedClock(datetime(2026, 6, 15, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")))

    def tearDown(self) -> None:
        trading_engine.STOP_PATH = self.original_stop_path
        self.tempdir.cleanup()

    def test_risk_allows_whitelisted_dry_run_order(self) -> None:
        manager = trading_engine.RiskManager(base_config(), self.clock)
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 10.0)

        decision = manager.validate_order(signal, self.portfolio, "2026-06-15")

        self.assertTrue(decision.allowed, decision.reason)

    def test_risk_blocks_non_whitelisted_symbol(self) -> None:
        manager = trading_engine.RiskManager(base_config(), self.clock)
        signal = trading_engine.OrderSignal("000001", "BUY", 100, 10.0)

        decision = manager.validate_order(signal, self.portfolio, "2026-06-15")

        self.assertFalse(decision.allowed)
        self.assertIn("白名单", decision.reason)

    def test_risk_blocks_small_live_order_over_stage_cash_limit(self) -> None:
        config = base_config()
        config["execution"]["mode"] = "ths_computer_use"
        config["execution"]["stage"] = "small_live"
        manager = trading_engine.RiskManager(config, self.clock)
        signal = trading_engine.OrderSignal("588330", "BUY", 600, 10.0)

        decision = manager.validate_order(signal, self.portfolio, "2026-06-15")

        self.assertFalse(decision.allowed)
        self.assertIn("阶段上限", decision.reason)

    def test_dry_run_executor_returns_execution_result(self) -> None:
        result = trading_engine.DryRunExecutor().place_order(trading_engine.OrderSignal("588330", "BUY", 100, 10.0))

        self.assertTrue(result.success)
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.verified_fields["symbol"], "588330")

    def test_ths_field_verification_requires_exact_order_fields(self) -> None:
        config = base_config()
        config["execution"]["mode"] = "ths_computer_use"
        config["execution"]["stage"] = "gui_simulation"
        executor = trading_engine.ThsComputerUseExecutor(config)
        signal = trading_engine.OrderSignal("588330", "SELL", 200, 1.234)

        ok, message = executor._verify_fields(
            signal,
            {"account_mode": "simulation", "symbol": "588330", "side": "SELL", "quantity": 200, "limit_price": 1.234},
        )

        self.assertTrue(ok, message)

    def test_ths_field_verification_rejects_wrong_side(self) -> None:
        config = base_config()
        executor = trading_engine.ThsComputerUseExecutor(config)
        signal = trading_engine.OrderSignal("588330", "SELL", 200, 1.234)

        ok, message = executor._verify_fields(
            signal,
            {"account_mode": "simulation", "symbol": "588330", "side": "BUY", "quantity": 200, "limit_price": 1.234},
        )

        self.assertFalse(ok)
        self.assertIn("方向", message)

    def test_ths_gui_bridge_command_receives_intent_and_verification_paths(self) -> None:
        config = base_config()
        verification_path = Path(self.tempdir.name) / "verified.json"
        bridge_path = Path(self.tempdir.name) / "bridge.py"
        bridge_path.write_text(
            "import json, sys\n"
            "intent = sys.argv[sys.argv.index('--intent') + 1]\n"
            "verification = sys.argv[sys.argv.index('--verification') + 1]\n"
            "order = json.load(open(intent, encoding='utf-8'))['order']\n"
            "json.dump({'account_mode': 'simulation', 'symbol': order['symbol'], 'side': order['side'], 'quantity': order['quantity'], 'limit_price': order['limit_price']}, open(verification, 'w', encoding='utf-8'))\n",
            encoding="utf-8",
        )
        config["execution"]["mode"] = "ths_computer_use"
        config["execution"]["stage"] = "gui_simulation"
        config["execution"]["verification_fields_path"] = str(verification_path)
        config["execution"]["gui_bridge_command"] = f"python3 {bridge_path} --intent {{intent_path}} --verification {{verification_fields_path}}"
        executor = trading_engine.ThsComputerUseExecutor(config)
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 1.234)

        with mock.patch.object(executor, "_activate_ths"), mock.patch.object(executor, "_capture_screenshot", return_value=None):
            result = executor.place_order(signal)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "gui_simulation_verified")
        self.assertEqual(result.verified_fields["symbol"], "588330")

    def test_ths_bridge_uses_observed_field_order(self) -> None:
        script = ths_simulation_bridge.build_applescript("588330", "BUY", "100", "1.234")

        self.assertIn('set value of text field 2 to "588330"', script)
        self.assertIn('set value of text field 1 to "1.234"', script)
        self.assertIn('set value of text field 3 to "100"', script)

    def test_ths_bridge_navigates_to_simulation_trade_panel(self) -> None:
        script = ths_simulation_bridge.build_applescript("588330", "BUY", "100", "1.234")

        self.assertIn("my waitForTradePanel()", script)
        self.assertIn('click button "登 录"', script)
        self.assertIn("click at {20, 276}", script)
        self.assertIn('click button "模拟"', script)
        self.assertIn("未能定位同花顺交易/模拟面板", script)

    def test_ths_field_verification_rejects_live_account_mode(self) -> None:
        config = base_config()
        executor = trading_engine.ThsComputerUseExecutor(config)
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 1.234)

        ok, message = executor._verify_fields(
            signal,
            {"account_mode": "live", "symbol": "588330", "side": "BUY", "quantity": 100, "limit_price": 1.234},
        )

        self.assertFalse(ok)
        self.assertIn("账户模式", message)

    def test_risk_blocks_ths_live_account_without_explicit_enable(self) -> None:
        config = base_config()
        config["execution"]["mode"] = "ths_computer_use"
        config["execution"]["stage"] = "gui_simulation"
        config["execution"]["ths_account_mode"] = "live"
        manager = trading_engine.RiskManager(config, self.clock)
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 10.0)

        decision = manager.validate_order(signal, self.portfolio, "2026-06-15")

        self.assertFalse(decision.allowed)
        self.assertIn("模拟交易", decision.reason)

    def test_stop_request_file_can_be_written_and_cleared(self) -> None:
        path = Path(self.tempdir.name) / "STOP_TRADING"

        trading_engine.request_stop(path=path)

        self.assertTrue(trading_engine.stop_requested(path))
        self.assertTrue(trading_engine.clear_stop_request(path))
        self.assertFalse(trading_engine.stop_requested(path))

    def test_wait_for_next_poll_exits_when_stop_file_exists(self) -> None:
        trading_engine.request_stop(path=trading_engine.STOP_PATH)

        started_at = time.monotonic()
        should_continue = trading_engine.wait_for_next_poll(60)

        self.assertFalse(should_continue)
        self.assertLess(time.monotonic() - started_at, 2)

    def test_open_live_log_window_uses_tail_follow(self) -> None:
        path = Path(self.tempdir.name) / "trading_engine.log"
        with mock.patch("trading_engine.subprocess.run") as run:
            run.return_value = subprocess_result = mock.Mock()
            subprocess_result.stdout = ""

            opened = trading_engine.open_live_log_window(path)

        self.assertTrue(opened)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["osascript", "-e"])
        self.assertIn("tail -f", command[2])
        self.assertIn(str(path), command[2])

    def test_terminal_color_lines_follow_white_background_palette(self) -> None:
        up_line = trading_engine.color_log_line("实时行情：最新价=1.3630  涨幅=3.26%")
        down_line = trading_engine.color_log_line("实时行情：最新价=1.3000  涨幅=-1.20%")
        buy_line = trading_engine.color_log_line("交易条件：满足买入条件")
        sell_line = trading_engine.color_log_line("交易条件：满足卖出条件")
        neutral_line = trading_engine.color_log_line("交易条件：未满足买入/卖出条件")

        self.assertTrue(up_line.startswith(trading_engine.ANSI_BOLD_RED))
        self.assertTrue(down_line.startswith(trading_engine.ANSI_BOLD_GREEN))
        self.assertTrue(buy_line.startswith(trading_engine.ANSI_BOLD_RED))
        self.assertTrue(sell_line.startswith(trading_engine.ANSI_BOLD_GREEN))
        self.assertTrue(neutral_line.startswith(trading_engine.ANSI_DARK_GRAY))
        self.assertTrue(up_line.endswith(trading_engine.ANSI_RESET))


if __name__ == "__main__":
    unittest.main()
