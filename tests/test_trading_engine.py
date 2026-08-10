import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import Optional
from unittest import mock
from zoneinfo import ZoneInfo

import trading_engine


class FixedClock(trading_engine.TradingClock):
    def __init__(self, value: datetime) -> None:
        super().__init__("Asia/Shanghai", [["09:15", "11:30"], ["13:00", "15:15"]])
        self.value = value

    def now(self) -> datetime:
        return self.value


class StubMarketData:
    pass


class StubQuote:
    def __init__(self, limit_up: Optional[float] = 1.5, limit_down: Optional[float] = 1.0) -> None:
        self.limit_up = limit_up
        self.limit_down = limit_down


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
            "ths_app_name": "同花顺",
            "ths_bundle_id": "cn.com.10jqka.macstock",
            "ths_process_name": "同花顺",
            "ths_interaction_mode": "accessibility_first",
            "ths_visual_fallback_enabled": True,
            "trade_password_env": "THS_TRADE_PASSWORD",
            "stage_cash_limits": {
                "dry_run": 50000,
                "gui_simulation": 50000,
                "sim_run": 50000,
                "small_live": 5000,
                "full_live": 50000,
            },
            "require_screenshot_verification": True,
            "final_confirm_enabled": False,
            "price_tolerance": 0.001,
            "verification_fields_path": "",
            "order_verification_allowed_sources": ["applescript_bridge"],
            "account_snapshot_path": "",
            "account_snapshot_allowed_sources": ["applescript_bridge"],
            "applescript_bridge_timeout_seconds": 0,
            "account_bridge_command": "",
            "gui_bridge_command": "",
        },
        "runtime": {
            "trade_day_schedule_enabled": True,
            "screenshots_cleanup": {
                "enabled": True,
                "retention_days": 7,
                "max_files": 300,
                "keep_latest_files": True,
            },
        },
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
        config["execution"]["mode"] = "ths_applescript"
        config["execution"]["stage"] = "small_live"
        manager = trading_engine.RiskManager(config, self.clock)
        signal = trading_engine.OrderSignal("588330", "BUY", 600, 10.0)

        decision = manager.validate_order(signal, self.portfolio, "2026-06-15")

        self.assertFalse(decision.allowed)
        self.assertIn("阶段上限", decision.reason)

    def test_risk_allows_sim_run_with_simulation_account(self) -> None:
        config = base_config()
        config["execution"]["mode"] = "ths_applescript"
        config["execution"]["stage"] = "sim_run"
        config["execution"]["ths_account_mode"] = "simulation"
        manager = trading_engine.RiskManager(config, self.clock)
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 10.0)

        decision = manager.validate_order(signal, self.portfolio, "2026-06-15")

        self.assertTrue(decision.allowed, decision.reason)

    def test_simulation_can_trade_same_symbol_again_with_daily_limit(self) -> None:
        config = base_config()
        config["execution"]["mode"] = "ths_applescript"
        config["execution"]["stage"] = "sim_run"
        config["execution"]["simulation_allow_repeated_symbol_trades"] = True
        config["risk"]["max_orders_per_day"] = 2
        manager = trading_engine.RiskManager(config, self.clock)
        self.portfolio.position("588330")["quantity"] = 100
        self.portfolio.mark_trade("588330", "2026-06-15")

        decision = manager.validate_order(
            trading_engine.OrderSignal("588330", "SELL", 100, 1.0), self.portfolio, "2026-06-15"
        )

        self.assertTrue(decision.allowed, decision.reason)

    def test_simulation_repeated_trades_still_obey_daily_limit(self) -> None:
        config = base_config()
        config["execution"]["simulation_allow_repeated_symbol_trades"] = True
        config["risk"]["max_orders_per_day"] = 1
        manager = trading_engine.RiskManager(config, self.clock)
        self.portfolio.mark_trade("588330", "2026-06-15")

        decision = manager.validate_order(
            trading_engine.OrderSignal("588330", "BUY", 100, 1.0), self.portfolio, "2026-06-15"
        )

        self.assertFalse(decision.allowed)
        self.assertIn("最大交易次数", decision.reason)

    def test_risk_blocks_sim_run_with_live_account(self) -> None:
        config = base_config()
        config["execution"]["mode"] = "ths_applescript"
        config["execution"]["stage"] = "sim_run"
        config["execution"]["ths_account_mode"] = "live"
        config["execution"]["live_account_enabled"] = True
        manager = trading_engine.RiskManager(config, self.clock)
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 10.0)

        decision = manager.validate_order(signal, self.portfolio, "2026-06-15")

        self.assertFalse(decision.allowed)
        self.assertIn("sim_run", decision.reason)

    def test_risk_blocks_buy_order_over_synced_available_cash(self) -> None:
        manager = trading_engine.RiskManager(base_config(), self.clock)
        self.portfolio.data["cash"] = 100.0
        signal = trading_engine.OrderSignal("588330", "BUY", 200, 1.0)

        decision = manager.validate_order(signal, self.portfolio, "2026-06-15")

        self.assertFalse(decision.allowed)
        self.assertIn("账户可用金额", decision.reason)

    def test_dry_run_executor_returns_execution_result(self) -> None:
        result = trading_engine.DryRunExecutor().place_order(trading_engine.OrderSignal("588330", "BUY", 100, 10.0))

        self.assertTrue(result.success)
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.verified_fields["symbol"], "588330")

    def test_buy_execution_limit_price_uses_limit_up(self) -> None:
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 1.23, "test")

        priced = trading_engine.apply_execution_limit_price(signal, StubQuote(limit_up=1.536, limit_down=1.024))

        self.assertEqual(priced.limit_price, 1.536)
        self.assertIn("涨停价", priced.note)

    def test_sell_execution_limit_price_uses_limit_down(self) -> None:
        signal = trading_engine.OrderSignal("588330", "SELL", 100, 1.23, "test")

        priced = trading_engine.apply_execution_limit_price(signal, StubQuote(limit_up=1.536, limit_down=1.024))

        self.assertEqual(priced.limit_price, 1.024)
        self.assertIn("跌停价", priced.note)

    def test_execution_limit_price_requires_limit_prices(self) -> None:
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 1.23)

        with self.assertRaises(RuntimeError):
            trading_engine.apply_execution_limit_price(signal, StubQuote(limit_up=None, limit_down=1.024))

    def test_ths_field_verification_requires_exact_order_fields(self) -> None:
        config = base_config()
        config["execution"]["mode"] = "ths_applescript"
        config["execution"]["stage"] = "gui_simulation"
        executor = trading_engine.ThsAppleScriptExecutor(config)
        signal = trading_engine.OrderSignal("588330", "SELL", 200, 1.234)

        ok, message = executor._verify_fields(
            signal,
            {
                "account_mode": "simulation",
                "symbol": "588330",
                "side": "SELL",
                "quantity": 200,
                "sellable_quantity": 200,
                "limit_price": 1.234,
                "source": "applescript_bridge",
            },
        )

        self.assertTrue(ok, message)

    def test_ths_field_verification_rejects_wrong_side(self) -> None:
        config = base_config()
        executor = trading_engine.ThsAppleScriptExecutor(config)
        signal = trading_engine.OrderSignal("588330", "SELL", 200, 1.234)

        ok, message = executor._verify_fields(
            signal,
            {
                "account_mode": "simulation",
                "symbol": "588330",
                "side": "BUY",
                "quantity": 200,
                "limit_price": 1.234,
                "source": "applescript_bridge",
            },
        )

        self.assertFalse(ok)
        self.assertIn("方向", message)

    def test_ths_field_verification_rejects_bridge_validation_errors(self) -> None:
        executor = trading_engine.ThsAppleScriptExecutor(base_config())
        signal = trading_engine.OrderSignal("588330", "SELL", 100, 1.234)

        ok, message = executor._verify_fields(
            signal,
            {
                "account_mode": "simulation", "symbol": "588330", "side": "SELL",
                "quantity": 100, "limit_price": 1.234, "source": "applescript_bridge",
                "validation_errors": ["missing simulation account context"],
            },
        )

        self.assertFalse(ok)
        self.assertIn("missing simulation account context", message)

    def test_ths_field_verification_accepts_configured_applescript_source(self) -> None:
        config = base_config()
        config["execution"]["order_verification_allowed_sources"] = ["applescript_bridge", "applescript_vision_ocr"]
        executor = trading_engine.ThsAppleScriptExecutor(config)
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 1.234)

        ok, message = executor._verify_fields(
            signal,
            {
                "account_mode": "simulation",
                "symbol": "588330",
                "side": "BUY",
                "quantity": 100,
                "limit_price": 1.234,
                "source": "applescript_vision_ocr",
            },
        )

        self.assertTrue(ok, message)

    def test_ths_applescript_requires_fresh_bridge_verification_file(self) -> None:
        config = base_config()
        verification_path = Path(self.tempdir.name) / "verified.json"
        config["execution"]["mode"] = "ths_applescript"
        config["execution"]["stage"] = "gui_simulation"
        config["execution"]["verification_fields_path"] = str(verification_path)
        executor = trading_engine.ThsAppleScriptExecutor(config)
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 1.234)

        result = executor.place_order(signal)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "gui_bridge_required")
        self.assertIn("受控 GUI bridge", result.message)

    def test_ths_applescript_accepts_fresh_verified_fields(self) -> None:
        config = base_config()
        verification_path = Path(self.tempdir.name) / "verified.json"
        config["execution"]["mode"] = "ths_applescript"
        config["execution"]["stage"] = "gui_simulation"
        config["execution"]["verification_fields_path"] = str(verification_path)
        executor = trading_engine.ThsAppleScriptExecutor(config)
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 1.234)

        original_write_intent = executor._write_intent

        def write_intent_and_verification(signal: trading_engine.OrderSignal, submitted_at: str) -> Path:
            intent_path = original_write_intent(signal, submitted_at)
            verification_path.write_text(
                json.dumps(
                    {
                        "account_mode": "simulation",
                        "symbol": "588330",
                        "side": "BUY",
                        "quantity": 100,
                        "limit_price": 1.234,
                        "source": "applescript_bridge",
                        "submitted": False,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return intent_path

        with mock.patch.object(executor, "_write_intent", side_effect=write_intent_and_verification):
            result = executor.place_order(signal)

        self.assertTrue(result.success)
        self.assertEqual(result.status, "gui_simulation_verified")
        self.assertEqual(result.verified_fields["source"], "applescript_bridge")

    def test_ths_applescript_sim_run_requires_bridge_submitted_flag(self) -> None:
        config = base_config()
        verification_path = Path(self.tempdir.name) / "verified.json"
        config["execution"]["mode"] = "ths_applescript"
        config["execution"]["stage"] = "sim_run"
        config["execution"]["final_confirm_enabled"] = True
        config["execution"]["verification_fields_path"] = str(verification_path)
        executor = trading_engine.ThsAppleScriptExecutor(config)
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 1.234)

        original_write_intent = executor._write_intent

        def write_intent_and_verification(signal: trading_engine.OrderSignal, submitted_at: str) -> Path:
            intent_path = original_write_intent(signal, submitted_at)
            verification_path.write_text(
                json.dumps(
                    {
                        "account_mode": "simulation",
                        "symbol": "588330",
                        "side": "BUY",
                        "quantity": 100,
                        "limit_price": 1.234,
                        "source": "applescript_bridge",
                        "submitted": False,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return intent_path

        with mock.patch.object(executor, "_write_intent", side_effect=write_intent_and_verification):
            result = executor.place_order(signal)

        self.assertFalse(result.success)
        self.assertEqual(result.status, "gui_bridge_submit_required")

    def test_gui_bridge_is_skipped_when_not_configured(self) -> None:
        executor = trading_engine.ThsAppleScriptExecutor(base_config())

        with mock.patch("trading_engine.subprocess.run") as run:
            result = executor._run_gui_bridge(Path(self.tempdir.name) / "intent.json")

        self.assertIsNone(result)
        run.assert_not_called()

    def test_gui_bridge_command_receives_intent_verification_and_submit_flag(self) -> None:
        config = base_config()
        verification_path = Path(self.tempdir.name) / "verified.json"
        config["execution"]["verification_fields_path"] = str(verification_path)
        config["execution"]["gui_bridge_command"] = (
            "python3 AppBridge_AppleScript.py --action buy "
            "--interaction-mode {interaction_mode} {visual_fallback_flag} "
            "--intent {intent_path} --verification {verification_fields_path} {submit_flag}"
        )
        executor = trading_engine.ThsAppleScriptExecutor(config)
        intent_path = Path(self.tempdir.name) / "intent.json"
        intent_path.write_text("{}", encoding="utf-8")

        with mock.patch("trading_engine.subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            result = executor._run_gui_bridge(intent_path, submit=True)

        self.assertEqual(result, verification_path)
        command = run.call_args.args[0]
        self.assertIn("AppBridge_AppleScript.py", command)
        self.assertIn("--submit", command)
        self.assertIn("--interaction-mode accessibility_first", command)
        self.assertIn(str(intent_path), command)
        self.assertIn(str(verification_path), command)

    def test_gui_bridge_can_fail_closed_without_visual_fallback(self) -> None:
        config = base_config()
        config["execution"]["ths_visual_fallback_enabled"] = False
        config["execution"]["gui_bridge_command"] = (
            "python3 AppBridge_AppleScript.py {visual_fallback_flag}"
        )
        executor = trading_engine.ThsAppleScriptExecutor(config)
        intent_path = Path(self.tempdir.name) / "intent.json"
        intent_path.write_text("{}", encoding="utf-8")

        with mock.patch(
            "trading_engine.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout="", stderr=""),
        ) as run:
            executor._run_gui_bridge(intent_path)

        self.assertIn("--no-visual-fallback", run.call_args.args[0])

    def test_portfolio_syncs_cash_and_positions_from_account_snapshot(self) -> None:
        snapshot = {
            "account_mode": "simulation",
            "total_assets": 60809.91,
            "available_cash": 60660.91,
            "cash_balance": 60660.91,
            "market_value": 149.0,
            "profit_loss": 7.96,
            "source": "test",
            "positions": [
                {
                    "symbol": "588330",
                    "name": "双创50ETF",
                    "quantity": 100,
                    "avg_cost": 1.41,
                    "market_value": 149.0,
                    "current_price": 1.49,
                    "profit_loss": 7.96,
                    "day_profit_loss": 4.9,
                    "profit_loss_pct": 5.64,
                    "sellable_quantity": 100,
                    "available_quantity": 100,
                    "frozen_quantity": 0,
                }
            ],
        }

        self.portfolio.sync_account_snapshot(snapshot, ["588330"])

        self.assertEqual(self.portfolio.cash(), 60660.91)
        self.assertEqual(self.portfolio.data["available_cash"], 60660.91)
        self.assertEqual(self.portfolio.data["total_assets"], 60809.91)
        position = self.portfolio.position("588330")
        self.assertEqual(position["quantity"], 100)
        self.assertEqual(position["name"], "双创50ETF")
        self.assertEqual(position["avg_cost"], 1.41)
        self.assertEqual(position["market_value"], 149.0)
        self.assertEqual(position["account_day_profit_loss"], 4.9)
        self.assertEqual(position["account_position_ratio_pct"], 0.245)
        self.assertEqual(len(self.portfolio.data["account_positions"]), 1)
        self.assertEqual(self.portfolio.data["account_positions"][0]["symbol"], "588330")
        self.assertEqual(self.portfolio.data["account_positions"][0]["position_ratio_pct"], 0.245)

    def test_account_sync_uses_applescript_bridge_snapshot_file(self) -> None:
        config = base_config()
        runtime_state = trading_engine.RuntimeState(Path(self.tempdir.name) / "runtime_state.json")
        snapshot_path = Path(self.tempdir.name) / "account.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "account_mode": "simulation",
                    "available_cash": 12345.67,
                    "cash_balance": 12345.67,
                    "source": "applescript_bridge",
                    "positions": [{"symbol": "588330", "quantity": 200, "avg_cost": 1.23, "market_value": 246.0}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        config["execution"]["account_snapshot_path"] = str(snapshot_path)

        ok, message = trading_engine.sync_portfolio_from_account(config, self.portfolio, runtime_state)

        self.assertTrue(ok, message)
        self.assertEqual(self.portfolio.cash(), 12345.67)
        self.assertEqual(self.portfolio.position("588330")["quantity"], 200)

    def test_account_sync_accepts_apple_vision_snapshot_when_allowed(self) -> None:
        config = base_config()
        runtime_state = trading_engine.RuntimeState(Path(self.tempdir.name) / "runtime_state.json")
        snapshot_path = Path(self.tempdir.name) / "account.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "account_mode": "simulation",
                    "total_assets": 60796.11,
                    "available_cash": 60660.91,
                    "cash_balance": 60660.91,
                    "market_value": 135.2,
                    "source": "apple_vision_ocr",
                    "positions": [
                        {
                            "symbol": "588330",
                            "quantity": 100,
                            "sellable_quantity": 100,
                            "avg_cost": 1.41,
                            "current_price": 1.352,
                            "market_value": 135.2,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        config["execution"]["account_snapshot_path"] = str(snapshot_path)
        config["execution"]["account_snapshot_allowed_sources"] = ["applescript_bridge", "apple_vision_ocr"]

        ok, message = trading_engine.sync_portfolio_from_account(config, self.portfolio, runtime_state)

        self.assertTrue(ok, message)
        self.assertEqual(self.portfolio.cash(), 60660.91)
        self.assertEqual(self.portfolio.position("588330")["quantity"], 100)

    def test_account_sync_accepts_apple_vision_snapshot_by_default(self) -> None:
        snapshot_path = Path(self.tempdir.name) / "account.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "account_mode": "simulation",
                    "available_cash": 60660.91,
                    "source": "apple_vision_ocr",
                    "positions": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        snapshot = trading_engine.load_recent_account_snapshot(snapshot_path, "simulation")

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["source"], "apple_vision_ocr")

    def test_account_sync_rejects_snapshot_with_ocr_validation_errors(self) -> None:
        snapshot_path = Path(self.tempdir.name) / "account.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "account_mode": "simulation",
                    "available_cash": 60660.91,
                    "source": "apple_vision_ocr",
                    "validation_errors": ["missing anchors: target_symbol"],
                    "positions": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        snapshot = trading_engine.load_recent_account_snapshot(
            snapshot_path,
            "simulation",
            allowed_sources={"apple_vision_ocr"},
        )

        self.assertIsNone(snapshot)

    def test_account_snapshot_request_runs_apple_bridge_when_allowed(self) -> None:
        original_request_path = trading_engine.APPLESCRIPT_BRIDGE_REQUEST_PATH
        request_path = Path(self.tempdir.name) / "request.json"
        snapshot_path = Path(self.tempdir.name) / "missing_account.json"
        trading_engine.APPLESCRIPT_BRIDGE_REQUEST_PATH = request_path
        self.addCleanup(setattr, trading_engine, "APPLESCRIPT_BRIDGE_REQUEST_PATH", original_request_path)

        with mock.patch("trading_engine.run_apple_account_snapshot_bridge") as bridge:
            trading_engine.request_applescript_account_snapshot(
                snapshot_path,
                "simulation",
                1,
                allowed_sources={"applescript_bridge", "apple_vision_ocr"},
            )

        bridge.assert_called_once_with(
            snapshot_path,
            symbol="588330",
            app_name="同花顺",
            bundle_id="cn.com.10jqka.macstock",
            process_name="同花顺",
        )

    def test_account_snapshot_request_retries_apple_bridge_while_waiting(self) -> None:
        original_request_path = trading_engine.APPLESCRIPT_BRIDGE_REQUEST_PATH
        request_path = Path(self.tempdir.name) / "request.json"
        snapshot_path = Path(self.tempdir.name) / "missing_account.json"
        trading_engine.APPLESCRIPT_BRIDGE_REQUEST_PATH = request_path
        self.addCleanup(setattr, trading_engine, "APPLESCRIPT_BRIDGE_REQUEST_PATH", original_request_path)
        times = iter([0, 0, 0, 0, 0, 0, 0, 10, 10, 10, 10, 20, 20, 20, 31])

        def fake_monotonic() -> int:
            try:
                return next(times)
            except StopIteration:
                return 31

        with (
            mock.patch("trading_engine.run_apple_account_snapshot_bridge") as bridge,
            mock.patch("trading_engine.time.monotonic", side_effect=fake_monotonic),
            mock.patch("trading_engine.time.sleep", return_value=None),
        ):
            trading_engine.request_applescript_account_snapshot(
                snapshot_path,
                "simulation",
                30,
                allowed_sources={"apple_vision_ocr"},
            )

        self.assertGreaterEqual(bridge.call_count, 2)
        self.assertFalse(request_path.exists())

    def test_apple_account_snapshot_bridge_runs_app_bridge_first_and_writes_configured_snapshot(self) -> None:
        snapshot_path = Path(self.tempdir.name) / "custom_account.json"
        app_bridge = mock.Mock(returncode=0, stdout='{"source":"applescript_vision_ocr"}', stderr="")
        snapshot_payload = {
            "account_mode": "simulation",
            "available_cash": 60660.91,
            "cash_balance": 60660.91,
            "source": "apple_vision_ocr",
            "positions": [{"symbol": "588330", "quantity": 100}],
        }
        account_bridge = mock.Mock(returncode=0, stdout=json.dumps(snapshot_payload, ensure_ascii=False), stderr="")

        with mock.patch("trading_engine.subprocess.run", side_effect=[app_bridge, account_bridge]) as run:
            trading_engine.run_apple_account_snapshot_bridge(snapshot_path, symbol="588330")

        self.assertEqual(run.call_count, 2)
        self.assertIn("AppBridge_AppleScript.py", run.call_args_list[0].args[0][1])
        self.assertIn("apple_account_snapshot.py", run.call_args_list[1].args[0][1])
        written = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(written["source"], "apple_vision_ocr")
        self.assertEqual(written["available_cash"], 60660.91)

    def test_apple_account_snapshot_bridge_stops_when_app_bridge_fails(self) -> None:
        snapshot_path = Path(self.tempdir.name) / "custom_account.json"
        app_bridge = mock.Mock(returncode=1, stdout="failed", stderr="")

        with mock.patch("trading_engine.subprocess.run", return_value=app_bridge) as run:
            trading_engine.run_apple_account_snapshot_bridge(snapshot_path, symbol="588330")

        run.assert_called_once()
        self.assertFalse(snapshot_path.exists())

    def test_run_once_syncs_account_before_stop_file_check(self) -> None:
        config = base_config()
        trading_engine.request_stop(path=trading_engine.STOP_PATH)

        with mock.patch("trading_engine.sync_portfolio_from_account", return_value=(True, "synced")) as sync:
            trading_engine.run_once(config, ignore_hours=True, ignore_trade_day=True)

        self.assertTrue(sync.called)

    def test_refresh_signal_before_execution_resyncs_account_and_regenerates_signal(self) -> None:
        config = base_config()
        runtime_state = trading_engine.RuntimeState(Path(self.tempdir.name) / "runtime_state.json")
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 1.23)

        with mock.patch("trading_engine.sync_portfolio_from_account", return_value=(True, "synced")) as sync:
            strategy_patch = mock.patch("trading_engine.TrendPullbackStrategy")
            strategy_class = strategy_patch.start()
            self.addCleanup(strategy_patch.stop)
            strategy_class.return_value.generate.return_value = signal
            strategy_class.return_value.last_diagnostics = {"checked": True}
            refreshed_signal, diagnostics, message = trading_engine.refresh_signal_before_execution(
                config,
                self.portfolio,
                runtime_state,
                StubMarketData(),
                "588330",
                "2026-06-15",
            )

        self.assertTrue(sync.called)
        self.assertTrue(sync.call_args.kwargs["force_refresh"])
        self.assertEqual(sync.call_args.kwargs["reason"], "before_order")
        self.assertEqual(refreshed_signal, signal)
        self.assertEqual(diagnostics, {"checked": True})
        self.assertIn("交易前账户", message)

    def test_run_once_can_skip_start_account_sync_after_app_open_sync(self) -> None:
        config = base_config()
        trading_engine.request_stop(path=trading_engine.STOP_PATH)

        with mock.patch("trading_engine.sync_portfolio_from_account", return_value=(True, "synced")) as sync:
            trading_engine.run_once(config, ignore_hours=True, ignore_trade_day=True, sync_account_at_start=False)

        self.assertFalse(sync.called)

    def test_app_open_account_sync_does_not_force_refresh_by_default(self) -> None:
        config = base_config()

        with mock.patch("trading_engine.sync_portfolio_from_account", return_value=(True, "synced")) as sync:
            synced = trading_engine.sync_account_after_app_open(config)

        self.assertTrue(synced)
        self.assertFalse(sync.call_args.kwargs["force_refresh"])
        self.assertEqual(sync.call_args.kwargs["reason"], "app_open")

    def test_app_open_account_sync_can_force_refresh_when_configured(self) -> None:
        config = base_config()
        config["execution"]["force_account_sync_on_app_open"] = True

        with mock.patch("trading_engine.sync_portfolio_from_account", return_value=(True, "synced")) as sync:
            synced = trading_engine.sync_account_after_app_open(config)

        self.assertTrue(synced)
        self.assertTrue(sync.call_args.kwargs["force_refresh"])

    def test_account_snapshot_request_marks_timeout(self) -> None:
        original_request_path = trading_engine.APPLESCRIPT_BRIDGE_REQUEST_PATH
        request_path = Path(self.tempdir.name) / "applescript_request.json"
        snapshot_path = Path(self.tempdir.name) / "missing_account.json"
        trading_engine.APPLESCRIPT_BRIDGE_REQUEST_PATH = request_path
        self.addCleanup(setattr, trading_engine, "APPLESCRIPT_BRIDGE_REQUEST_PATH", original_request_path)

        trading_engine.request_applescript_account_snapshot(
            snapshot_path,
            "simulation",
            0,
            allowed_sources={"applescript_bridge"},
        )

        payload = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "timed_out")
        self.assertIn("未收到", payload["message"])

    def test_ths_field_verification_rejects_live_account_mode(self) -> None:
        config = base_config()
        executor = trading_engine.ThsAppleScriptExecutor(config)
        signal = trading_engine.OrderSignal("588330", "BUY", 100, 1.234)

        ok, message = executor._verify_fields(
            signal,
            {
                "account_mode": "live",
                "symbol": "588330",
                "side": "BUY",
                "quantity": 100,
                "limit_price": 1.234,
                "source": "applescript_bridge",
            },
        )

        self.assertFalse(ok)
        self.assertIn("账户模式", message)

    def test_risk_blocks_ths_live_account_without_explicit_enable(self) -> None:
        config = base_config()
        config["execution"]["mode"] = "ths_applescript"
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

    def test_open_live_log_window_does_not_auto_control_terminal(self) -> None:
        path = Path(self.tempdir.name) / "trading_engine.log"
        with mock.patch("trading_engine.subprocess.run") as run:
            opened = trading_engine.open_live_log_window(path)

        self.assertFalse(opened)
        run.assert_not_called()

    def test_open_trading_app_uses_configured_app_name(self) -> None:
        with mock.patch("trading_engine.subprocess.run") as run:
            opened = trading_engine.open_trading_app({"ths_app_name": "同花顺"})

        self.assertTrue(opened)
        run.assert_called_once_with(
            ["open", "-a", "同花顺"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_cleanup_screenshots_deletes_old_artifacts_and_keeps_latest_files(self) -> None:
        original_dir = trading_engine.SCREENSHOT_DIR
        screenshot_dir = Path(self.tempdir.name) / "screenshots"
        screenshot_dir.mkdir()
        trading_engine.SCREENSHOT_DIR = screenshot_dir
        self.addCleanup(setattr, trading_engine, "SCREENSHOT_DIR", original_dir)
        old_file = screenshot_dir / "apple_account_snapshot_20260101T000000.png"
        fresh_file = screenshot_dir / "apple_account_snapshot_20260707T000000.json"
        latest_file = screenshot_dir / "latest_account_snapshot.json"
        ignored_file = screenshot_dir / "notes.txt"
        for path in (old_file, fresh_file, latest_file, ignored_file):
            path.write_text("x", encoding="utf-8")
        now = time.time()
        os.utime(old_file, (now - 10 * 86400, now - 10 * 86400))
        os.utime(fresh_file, (now, now))
        os.utime(latest_file, (now - 30 * 86400, now - 30 * 86400))

        result = trading_engine.cleanup_screenshots(base_config(), now=now)

        self.assertEqual(result.deleted, 1)
        self.assertFalse(old_file.exists())
        self.assertTrue(fresh_file.exists())
        self.assertTrue(latest_file.exists())
        self.assertTrue(ignored_file.exists())

    def test_cleanup_screenshots_enforces_max_files_with_oldest_first(self) -> None:
        original_dir = trading_engine.SCREENSHOT_DIR
        screenshot_dir = Path(self.tempdir.name) / "screenshots"
        screenshot_dir.mkdir()
        trading_engine.SCREENSHOT_DIR = screenshot_dir
        self.addCleanup(setattr, trading_engine, "SCREENSHOT_DIR", original_dir)
        config = base_config()
        config["runtime"]["screenshots_cleanup"]["retention_days"] = 30
        config["runtime"]["screenshots_cleanup"]["max_files"] = 2
        now = time.time()
        files = [screenshot_dir / f"apple_bridge_navigation_20260707T00000{i}.json" for i in range(4)]
        for index, path in enumerate(files):
            path.write_text("{}", encoding="utf-8")
            os.utime(path, (now + index, now + index))

        result = trading_engine.cleanup_screenshots(config, now=now + 10)

        self.assertEqual(result.deleted, 2)
        self.assertFalse(files[0].exists())
        self.assertFalse(files[1].exists())
        self.assertTrue(files[2].exists())
        self.assertTrue(files[3].exists())

    def test_cleanup_screenshots_respects_disabled_unless_forced(self) -> None:
        original_dir = trading_engine.SCREENSHOT_DIR
        screenshot_dir = Path(self.tempdir.name) / "screenshots"
        screenshot_dir.mkdir()
        trading_engine.SCREENSHOT_DIR = screenshot_dir
        self.addCleanup(setattr, trading_engine, "SCREENSHOT_DIR", original_dir)
        config = base_config()
        config["runtime"]["screenshots_cleanup"]["enabled"] = False
        old_file = screenshot_dir / "apple_account_snapshot_20260101T000000.json"
        old_file.write_text("{}", encoding="utf-8")
        now = time.time()
        os.utime(old_file, (now - 10 * 86400, now - 10 * 86400))

        skipped = trading_engine.cleanup_screenshots(config, now=now)
        forced = trading_engine.cleanup_screenshots(config, force=True, now=now)

        self.assertEqual(skipped.deleted, 0)
        self.assertEqual(forced.deleted, 1)
        self.assertFalse(old_file.exists())

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
