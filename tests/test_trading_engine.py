import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import trading_engine


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
        },
        "runtime": {"trade_day_schedule_enabled": True},
    }


class TradingEngineSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.portfolio = trading_engine.PortfolioStore(
            Path(self.tempdir.name) / "portfolio.json",
            ["588330"],
            50000,
        )
        self.clock = FixedClock(datetime(2026, 6, 15, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")))

    def tearDown(self) -> None:
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
            {"symbol": "588330", "side": "SELL", "quantity": 200, "limit_price": 1.234},
        )

        self.assertTrue(ok, message)

    def test_ths_field_verification_rejects_wrong_side(self) -> None:
        config = base_config()
        executor = trading_engine.ThsComputerUseExecutor(config)
        signal = trading_engine.OrderSignal("588330", "SELL", 200, 1.234)

        ok, message = executor._verify_fields(
            signal,
            {"symbol": "588330", "side": "BUY", "quantity": 200, "limit_price": 1.234},
        )

        self.assertFalse(ok)
        self.assertIn("方向", message)

    def test_stop_request_file_can_be_written_and_cleared(self) -> None:
        path = Path(self.tempdir.name) / "STOP_TRADING"

        trading_engine.request_stop(path=path)

        self.assertTrue(trading_engine.stop_requested(path))
        self.assertTrue(trading_engine.clear_stop_request(path))
        self.assertFalse(trading_engine.stop_requested(path))

    def test_wait_for_next_poll_exits_when_stop_file_exists(self) -> None:
        original_stop_path = trading_engine.STOP_PATH
        try:
            trading_engine.STOP_PATH = Path(self.tempdir.name) / "STOP_TRADING"
            trading_engine.request_stop(path=trading_engine.STOP_PATH)

            started_at = time.monotonic()
            should_continue = trading_engine.wait_for_next_poll(60)

            self.assertFalse(should_continue)
            self.assertLess(time.monotonic() - started_at, 2)
        finally:
            trading_engine.STOP_PATH = original_stop_path

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


if __name__ == "__main__":
    unittest.main()
