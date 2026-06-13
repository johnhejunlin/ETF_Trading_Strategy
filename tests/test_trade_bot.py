import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import trade_bot


class FixedClock(trade_bot.TradingClock):
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


class TradeBotSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.portfolio = trade_bot.PortfolioStore(
            Path(self.tempdir.name) / "portfolio.json",
            ["588330"],
            50000,
        )
        self.clock = FixedClock(datetime(2026, 6, 15, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_risk_allows_whitelisted_dry_run_order(self) -> None:
        manager = trade_bot.RiskManager(base_config(), self.clock)
        signal = trade_bot.OrderSignal("588330", "BUY", 100, 10.0)

        decision = manager.validate_order(signal, self.portfolio, "2026-06-15")

        self.assertTrue(decision.allowed, decision.reason)

    def test_risk_blocks_non_whitelisted_symbol(self) -> None:
        manager = trade_bot.RiskManager(base_config(), self.clock)
        signal = trade_bot.OrderSignal("000001", "BUY", 100, 10.0)

        decision = manager.validate_order(signal, self.portfolio, "2026-06-15")

        self.assertFalse(decision.allowed)
        self.assertIn("白名单", decision.reason)

    def test_risk_blocks_small_live_order_over_stage_cash_limit(self) -> None:
        config = base_config()
        config["execution"]["mode"] = "ths_computer_use"
        config["execution"]["stage"] = "small_live"
        manager = trade_bot.RiskManager(config, self.clock)
        signal = trade_bot.OrderSignal("588330", "BUY", 600, 10.0)

        decision = manager.validate_order(signal, self.portfolio, "2026-06-15")

        self.assertFalse(decision.allowed)
        self.assertIn("阶段上限", decision.reason)

    def test_dry_run_executor_returns_execution_result(self) -> None:
        result = trade_bot.DryRunExecutor().place_order(trade_bot.OrderSignal("588330", "BUY", 100, 10.0))

        self.assertTrue(result.success)
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.verified_fields["symbol"], "588330")

    def test_ths_field_verification_requires_exact_order_fields(self) -> None:
        config = base_config()
        config["execution"]["mode"] = "ths_computer_use"
        config["execution"]["stage"] = "gui_simulation"
        executor = trade_bot.ThsComputerUseExecutor(config)
        signal = trade_bot.OrderSignal("588330", "SELL", 200, 1.234)

        ok, message = executor._verify_fields(
            signal,
            {"symbol": "588330", "side": "SELL", "quantity": 200, "limit_price": 1.234},
        )

        self.assertTrue(ok, message)

    def test_ths_field_verification_rejects_wrong_side(self) -> None:
        config = base_config()
        executor = trade_bot.ThsComputerUseExecutor(config)
        signal = trade_bot.OrderSignal("588330", "SELL", 200, 1.234)

        ok, message = executor._verify_fields(
            signal,
            {"symbol": "588330", "side": "BUY", "quantity": 200, "limit_price": 1.234},
        )

        self.assertFalse(ok)
        self.assertIn("方向", message)


if __name__ == "__main__":
    unittest.main()
