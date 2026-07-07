#!/usr/bin/env python3
import argparse
import csv
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from market_data import EastMoneyMarketData
from trading_strategy import OrderSignal, TrendPullbackStrategy


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
PORTFOLIO_PATH = ROOT / "portfolio.json"
LOG_PATH = ROOT / "trading_engine.log"
STOP_PATH = ROOT / "STOP_TRADING"
RUN_STATE_PATH = ROOT / "runtime_state.json"
SIGNAL_AUDIT_PATH = ROOT / "signals.csv"
SCREENSHOT_DIR = ROOT / "screenshots"
CODEX_COMPUTER_USE_REQUEST_PATH = SCREENSHOT_DIR / "latest_codex_computer_use_request.json"
STOP_POLL_SECONDS = 1
LOG_SEPARATOR = "————————————————————————————————————————————————————"
ANSI_RESET = "\033[0m"
ANSI_BLACK = "\033[30m"
ANSI_BOLD_BLUE = "\033[1;34m"
ANSI_DARK_GRAY = "\033[90m"
ANSI_BOLD_RED = "\033[1;31m"
ANSI_BOLD_GREEN = "\033[1;32m"
ANSI_BROWN = "\033[33m"
ANSI_PURPLE = "\033[35m"
ANSI_DARK_CYAN = "\033[36m"
ANSI_PINK = "\033[95m"


VALID_SIDES = {"BUY", "SELL"}
EXECUTION_MODES = {"dry_run", "manual_confirm", "ths_computer_use"}
EXECUTION_STAGES = {"dry_run", "gui_simulation", "sim_run", "small_live", "full_live"}
THS_ACCOUNT_MODES = {"simulation", "live"}


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    status: str
    message: str
    submitted_at: str
    verified_fields: dict
    screenshot_path: Optional[str] = None


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


class TradingClock:
    def __init__(self, timezone: str, sessions: list[list[str]]) -> None:
        self.tz = ZoneInfo(timezone)
        self.sessions = [(self._parse_time(start), self._parse_time(end)) for start, end in sessions]

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    def is_open(self, now: Optional[datetime] = None) -> bool:
        now = now or self.now()
        current = now.time()
        return any(start <= current <= end for start, end in self.sessions)

    def is_trade_day(self, now: Optional[datetime] = None) -> bool:
        now = now or self.now()
        return now.weekday() < 5

    @staticmethod
    def _parse_time(value: str) -> dtime:
        hour, minute = value.split(":")
        return dtime(hour=int(hour), minute=int(minute))


class PortfolioStore:
    def __init__(self, path: Path, symbols: list[str], initial_cash: float) -> None:
        self.path = path
        self.data = self._load(symbols, initial_cash)

    def cash(self) -> float:
        return float(self.data["cash"])

    def position(self, symbol: str) -> dict:
        return self.data["positions"].setdefault(symbol, self._empty_position())

    def traded_today(self, symbol: str, today: str) -> bool:
        return self.position(symbol).get("last_trade_date") == today

    def mark_trade(self, symbol: str, today: str) -> None:
        self.position(symbol)["last_trade_date"] = today

    def sync_account_snapshot(self, snapshot: dict, symbols: list[str]) -> None:
        available_cash = snapshot.get("available_cash")
        if available_cash is None:
            available_cash = snapshot.get("cash_balance")
        if available_cash is not None:
            self.data["cash"] = float(available_cash)
            self.data["available_cash"] = float(available_cash)
        if snapshot.get("total_assets") is not None:
            self.data["total_assets"] = float(snapshot["total_assets"])

        positions_by_symbol = {
            str(position.get("symbol")): position
            for position in snapshot.get("positions", [])
            if position.get("symbol")
        }
        self.data["account_positions"] = [
            self._account_position_summary_with_snapshot(position, snapshot)
            for position in snapshot.get("positions", [])
            if position.get("symbol")
        ]
        if not positions_by_symbol:
            self.data["last_account_snapshot"] = self._snapshot_summary(snapshot)
            return

        for symbol in sorted(set(symbols) | set(positions_by_symbol)):
            local = self.position(symbol)
            account_position = positions_by_symbol.get(symbol)
            previous_last_trade_date = local.get("last_trade_date")
            previous_sell_streak = int(local.get("sell_streak") or 0)
            previous_buy_count = int(local.get("buy_count") or 0)
            previous_buy_prices = list(local.get("buy_prices") or [])
            previous_latest_buy_price = local.get("latest_buy_price")
            if not account_position:
                local.update(self._empty_position())
                local["last_trade_date"] = previous_last_trade_date
                continue

            quantity = int(account_position.get("quantity") or 0)
            avg_cost = float(account_position.get("avg_cost") or 0.0)
            position_ratio_pct = self._position_ratio_pct(account_position, snapshot)
            local.update(
                {
                    "quantity": quantity,
                    "name": account_position.get("name") or local.get("name") or "",
                    "avg_cost": avg_cost,
                    "market_value": float(account_position.get("market_value") or 0.0),
                    "current_price": account_position.get("current_price"),
                    "account_profit_loss": account_position.get("profit_loss"),
                    "account_day_profit_loss": account_position.get("day_profit_loss"),
                    "account_profit_loss_pct": account_position.get("profit_loss_pct"),
                    "account_position_ratio_pct": position_ratio_pct,
                    "sellable_quantity": int(account_position.get("sellable_quantity") or 0),
                    "available_quantity": int(account_position.get("available_quantity") or 0),
                    "frozen_quantity": int(account_position.get("frozen_quantity") or 0),
                    "account_source": account_position.get("source"),
                    "last_trade_date": previous_last_trade_date,
                    "sell_streak": previous_sell_streak,
                    "buy_count": previous_buy_count,
                    "latest_buy_price": previous_latest_buy_price if quantity > 0 else None,
                    "buy_prices": previous_buy_prices if quantity > 0 else [],
                }
            )
            if quantity <= 0:
                local.update(self._empty_position())
                local["last_trade_date"] = previous_last_trade_date

        self.data["last_account_snapshot"] = self._snapshot_summary(snapshot)

    def update_max_profit(self, symbol: str, current_price: float) -> None:
        position = self.position(symbol)
        quantity = int(position["quantity"])
        avg_cost = float(position["avg_cost"])
        if quantity <= 0 or avg_cost <= 0:
            return
        profit_pct = (current_price - avg_cost) / avg_cost
        position["max_profit_pct"] = max(float(position.get("max_profit_pct") or 0.0), profit_pct)

    def apply_fill(self, signal: OrderSignal, fill_price: float) -> None:
        position = self.position(signal.symbol)
        quantity = int(position["quantity"])
        avg_cost = float(position["avg_cost"])

        if signal.side == "BUY":
            cost = signal.quantity * fill_price
            new_quantity = quantity + signal.quantity
            position["avg_cost"] = ((quantity * avg_cost) + cost) / new_quantity
            position["quantity"] = new_quantity
            self.data["cash"] = max(0.0, self.cash() - cost)
            position["max_profit_pct"] = 0.0
            position["sell_streak"] = 0
            position["buy_count"] = int(position.get("buy_count") or 0) + 1
            position["latest_buy_price"] = fill_price
            position.setdefault("buy_prices", []).append(fill_price)
            return

        sell_quantity = min(signal.quantity, quantity)
        position["quantity"] = quantity - sell_quantity
        self.data["cash"] = self.cash() + sell_quantity * fill_price
        position["sell_streak"] = int(position.get("sell_streak") or 0) + 1
        if position["quantity"] == 0:
            position.update(self._empty_position())
        elif avg_cost > 0:
            position["max_profit_pct"] = (fill_price - avg_cost) / avg_cost

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)

    @staticmethod
    def _empty_position() -> dict:
        return {
            "quantity": 0,
            "avg_cost": 0.0,
            "max_profit_pct": 0.0,
            "sell_streak": 0,
            "buy_count": 0,
            "latest_buy_price": None,
            "buy_prices": [],
            "last_trade_date": None,
        }

    @staticmethod
    def _snapshot_summary(snapshot: dict) -> dict:
        return {
            "account_mode": snapshot.get("account_mode"),
            "total_assets": snapshot.get("total_assets"),
            "available_cash": snapshot.get("available_cash"),
            "cash_balance": snapshot.get("cash_balance"),
            "market_value": snapshot.get("market_value"),
            "profit_loss": snapshot.get("profit_loss"),
            "source": snapshot.get("source"),
            "positions_count": len(snapshot.get("positions", [])),
            "synced_at": datetime.now().isoformat(timespec="seconds"),
        }

    @staticmethod
    def _account_position_summary(position: dict) -> dict:
        return PortfolioStore._account_position_summary_with_snapshot(position, {})

    @staticmethod
    def _account_position_summary_with_snapshot(position: dict, snapshot: dict) -> dict:
        return {
            "symbol": position.get("symbol"),
            "name": position.get("name"),
            "quantity": int(position.get("quantity") or 0),
            "sellable_quantity": int(position.get("sellable_quantity") or 0),
            "available_quantity": int(position.get("available_quantity") or 0),
            "frozen_quantity": int(position.get("frozen_quantity") or 0),
            "avg_cost": position.get("avg_cost"),
            "current_price": position.get("current_price"),
            "market_value": position.get("market_value"),
            "profit_loss": position.get("profit_loss"),
            "day_profit_loss": position.get("day_profit_loss"),
            "profit_loss_pct": position.get("profit_loss_pct"),
            "position_ratio_pct": PortfolioStore._position_ratio_pct(position, snapshot),
            "source": position.get("source"),
        }

    @staticmethod
    def _position_ratio_pct(position: dict, snapshot: dict) -> Optional[float]:
        market_value = position.get("market_value")
        total_assets = snapshot.get("total_assets")
        try:
            market_value_float = float(market_value)
            total_assets_float = float(total_assets)
        except (TypeError, ValueError):
            return position.get("position_ratio_pct")
        if total_assets_float <= 0:
            return position.get("position_ratio_pct")
        return round((market_value_float / total_assets_float) * 100, 4)

    def _load(self, symbols: list[str], initial_cash: float) -> dict:
        if self.path.exists():
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        else:
            data = {"cash": initial_cash, "positions": {}}

        data.setdefault("cash", initial_cash)
        data.setdefault("positions", {})
        for symbol in symbols:
            position = data["positions"].setdefault(symbol, self._empty_position())
            for key, value in self._empty_position().items():
                position.setdefault(key, value)
        return data


class RuntimeState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def record_event(self, key: str, value: object) -> None:
        self.data[key] = value
        self.save()

    def increment(self, key: str) -> int:
        value = int(self.data.get(key) or 0) + 1
        self.data[key] = value
        self.save()
        return value

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        with self.path.open(encoding="utf-8") as handle:
            return json.load(handle)


class Notifier:
    def __init__(self, config: dict) -> None:
        self.config = config.get("notification", {})

    def notify(self, title: str, message: str) -> bool:
        channel = self.config.get("channel", "log")
        logging.info("[通知:%s] %s - %s", channel, title, message)
        if channel == "wechat_client":
            return self._send_wechat_client(title, message)
        return True

    def _send_wechat_client(self, title: str, message: str) -> bool:
        recipient = self.config.get("wechat_recipient", "")
        if not recipient:
            logging.warning("未配置 notification.wechat_recipient，微信通知降级为日志。")
            return False

        logging.warning(
            "微信客户端通知需要 Codex Computer Use 或人工发送，后台脚本不再自动操作 WeChat。recipient=%s message=%s",
            recipient,
            f"{title}\n{message}",
        )
        return False


class RiskManager:
    def __init__(self, config: dict, clock: TradingClock) -> None:
        self.config = config
        self.clock = clock
        self.risk = config.get("risk", {})
        self.execution = config.get("execution", {})
        self.runtime = config.get("runtime", {})

    def validate_order(
        self,
        signal: OrderSignal,
        portfolio: PortfolioStore,
        today: str,
        ignore_hours: bool = False,
        ignore_trade_day: bool = False,
    ) -> RiskDecision:
        if STOP_PATH.exists():
            return RiskDecision(False, f"检测到一键停止文件: {STOP_PATH}")
        if signal.symbol not in self.risk.get("allowed_symbols", self.config.get("symbols", [])):
            return RiskDecision(False, f"{signal.symbol} 不在交易白名单内。")
        if signal.side not in VALID_SIDES:
            return RiskDecision(False, f"无效买卖方向: {signal.side}")
        if signal.quantity <= 0:
            return RiskDecision(False, "订单数量必须大于 0。")
        if signal.limit_price is None or signal.limit_price <= 0:
            return RiskDecision(False, "第一版真实/模拟执行必须有正数限价。")
        if not ignore_trade_day and self.runtime.get("trade_day_schedule_enabled", True) and not self.clock.is_trade_day():
            return RiskDecision(False, "当前不是交易日，禁止下单。")
        if not ignore_hours and not self.clock.is_open():
            return RiskDecision(False, "当前不在配置交易时段内，禁止下单。")
        if portfolio.traded_today(signal.symbol, today):
            return RiskDecision(False, f"{signal.symbol} 今日已执行过交易。")

        position = portfolio.position(signal.symbol)
        if signal.side == "SELL" and signal.quantity > int(position.get("quantity") or 0):
            return RiskDecision(False, "卖出数量超过本地持仓。")

        max_orders_per_day = int(self.risk.get("max_orders_per_day", 1))
        if max_orders_per_day < 1:
            return RiskDecision(False, "risk.max_orders_per_day 必须至少为 1。")

        stage = self.execution.get("stage", "dry_run")
        mode = self.execution.get("mode", "dry_run")
        if stage not in EXECUTION_STAGES:
            return RiskDecision(False, f"未知执行阶段: {stage}")
        if mode not in EXECUTION_MODES:
            return RiskDecision(False, f"未知执行模式: {mode}")
        if not self._stage_allows_mode(stage, mode):
            return RiskDecision(False, f"执行阶段 {stage} 不允许模式 {mode}。")
        ths_account_mode = str(self.execution.get("ths_account_mode", "simulation"))
        if ths_account_mode not in THS_ACCOUNT_MODES:
            return RiskDecision(False, f"未知同花顺账户模式: {ths_account_mode}")
        if mode == "ths_computer_use" and ths_account_mode != "simulation" and not self.execution.get("live_account_enabled", False):
            return RiskDecision(False, "同花顺 GUI 调试完成前必须使用模拟交易入口，实盘账户未启用。")
        if stage == "sim_run" and ths_account_mode != "simulation":
            return RiskDecision(False, "sim_run 阶段只允许使用同花顺模拟账户。")

        if signal.side == "BUY":
            available_cash = portfolio.cash()
            if signal.amount() > available_cash:
                return RiskDecision(False, f"订单金额 {signal.amount():.2f} 超过账户可用金额 {available_cash:.2f}。")
            max_cash = self._stage_cash_limit(stage)
            if signal.amount() > max_cash:
                return RiskDecision(False, f"订单金额 {signal.amount():.2f} 超过阶段上限 {max_cash:.2f}。")

        return RiskDecision(True, "风控通过。")

    def _stage_allows_mode(self, stage: str, mode: str) -> bool:
        if stage == "dry_run":
            return mode == "dry_run"
        if stage == "gui_simulation":
            return mode in {"dry_run", "manual_confirm", "ths_computer_use"}
        if stage == "sim_run":
            return mode in {"manual_confirm", "ths_computer_use"}
        if stage in {"small_live", "full_live"}:
            return mode in {"manual_confirm", "ths_computer_use"}
        return False

    def _stage_cash_limit(self, stage: str) -> float:
        limits = self.execution.get("stage_cash_limits", {})
        if stage == "small_live":
            return float(limits.get("small_live", self.execution.get("small_live_cash", 5000)))
        if stage == "full_live":
            return float(limits.get("full_live", self.execution.get("max_live_cash", 50000)))
        if stage == "sim_run":
            return float(limits.get("sim_run", self.execution.get("max_live_cash", 50000)))
        if stage == "gui_simulation":
            return float(limits.get("gui_simulation", self.execution.get("max_live_cash", 50000)))
        return float(limits.get("dry_run", self.execution.get("max_live_cash", 50000)))


class Executor:
    def place_order(self, signal: OrderSignal) -> ExecutionResult:
        raise NotImplementedError

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")


class DryRunExecutor(Executor):
    def place_order(self, signal: OrderSignal) -> ExecutionResult:
        logging.info("[DRY-RUN] 计划下单: %s", signal)
        return ExecutionResult(
            success=True,
            status="dry_run",
            message="dry-run 记录成功，未触碰同花顺。",
            submitted_at=self._now(),
            verified_fields=asdict(signal),
        )


class ManualConfirmExecutor(Executor):
    def place_order(self, signal: OrderSignal) -> ExecutionResult:
        answer = input(f"确认提交真实订单 {signal}? 输入 YES 继续: ").strip()
        if answer != "YES":
            return ExecutionResult(
                success=False,
                status="manual_cancelled",
                message="用户取消真实订单。",
                submitted_at=self._now(),
                verified_fields={},
            )
        return ExecutionResult(
            success=False,
            status="manual_not_implemented",
            message="人工确认后真实下单接口尚未接入，未提交订单。",
            submitted_at=self._now(),
            verified_fields={},
        )


class ThsComputerUseExecutor(Executor):
    def __init__(self, config: dict) -> None:
        self.config = config
        self.execution = config.get("execution", {})
        self.stage = self.execution.get("stage", "dry_run")
        self.final_confirm_enabled = bool(self.execution.get("final_confirm_enabled", False))
        self.require_screenshot_verification = bool(self.execution.get("require_screenshot_verification", True))
        self.price_tolerance = float(self.execution.get("price_tolerance", 0.001))
        self.verification_fields_path = self.execution.get("verification_fields_path", "")
        self.ths_account_mode = str(self.execution.get("ths_account_mode", "simulation"))
        self.codex_wait_seconds = int(self.execution.get("codex_computer_use_timeout_seconds", 180))

    def place_order(self, signal: OrderSignal) -> ExecutionResult:
        submitted_at = self._now()
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        intent_path = self._write_intent(signal, submitted_at)
        self._request_codex_computer_use(signal, intent_path, submit=False)
        fields = self._read_verified_fields_after(intent_path)
        screenshot_path = None

        if self.require_screenshot_verification:
            ok, message = self._verify_fields(signal, fields)
            if not ok:
                return ExecutionResult(
                    success=False,
                    status="codex_computer_use_required",
                    message=(
                        f"需要使用 Codex Computer Use 完成同花顺界面填单/字段校验: {message}；"
                        f"intent={intent_path}"
                    ),
                    submitted_at=submitted_at,
                    verified_fields=fields,
                    screenshot_path=str(screenshot_path) if screenshot_path else None,
                )

        if self.stage == "gui_simulation" or not self.final_confirm_enabled:
            return ExecutionResult(
                success=True,
                status="gui_simulation_verified",
                message="已完成同花顺激活、截图和字段校验；阶段/配置禁止最终确认，未提交真实订单。",
                submitted_at=submitted_at,
                verified_fields=fields,
                screenshot_path=str(screenshot_path) if screenshot_path else None,
            )

        submitted_fields = self._read_verified_fields_after(intent_path)
        if not submitted_fields.get("submitted"):
            self._request_codex_computer_use(signal, intent_path, submit=True)
            submitted_fields = self._read_verified_fields_after(intent_path)
        if self.require_screenshot_verification:
            ok, message = self._verify_fields(signal, submitted_fields)
            if not ok:
                return ExecutionResult(
                    success=False,
                    status="submit_verification_failed",
                    message=f"同花顺提交后界面校验失败: {message}；intent={intent_path}",
                    submitted_at=submitted_at,
                    verified_fields=submitted_fields,
                    screenshot_path=None,
                )
        if not submitted_fields.get("submitted"):
            return ExecutionResult(
                success=False,
                status="codex_computer_use_submit_required",
                message="需要使用 Codex Computer Use 完成最终模拟账户提交，并写回 submitted=true，禁止记录成交。",
                submitted_at=submitted_at,
                verified_fields=submitted_fields,
                screenshot_path=None,
            )
        return ExecutionResult(
            success=True,
            status="submitted",
            message="已确认 Codex Computer Use 完成同花顺最终买入/卖出按钮点击；请以同花顺委托/成交回报为准。",
            submitted_at=submitted_at,
            verified_fields=submitted_fields,
            screenshot_path=None,
        )

    def _capture_screenshot(self, signal: OrderSignal, submitted_at: str) -> Optional[Path]:
        path = SCREENSHOT_DIR / f"ths_{submitted_at.replace(':', '').replace('-', '')}_{signal.symbol}_{signal.side}.png"
        try:
            from PIL import ImageGrab

            image = ImageGrab.grab()
            image.save(path)
            logging.info("已保存同花顺校验截图: %s", path)
            return path
        except Exception as exc:
            logging.warning("Pillow 截图失败，尝试 macOS screencapture: %s", exc)
        try:
            screencapture = shutil.which("screencapture")
            if not screencapture:
                raise RuntimeError("未找到 screencapture 命令")
            subprocess.run([screencapture, "-x", str(path)], check=True, capture_output=True, text=True, timeout=8)
            logging.info("已保存同花顺校验截图: %s", path)
            return path
        except Exception as exc:
            if self.require_screenshot_verification:
                raise RuntimeError(f"截图失败，禁止继续执行: {exc}") from exc
            logging.warning("截图失败，按配置允许继续: %s", exc)
            return None

    def _run_gui_bridge(self, intent_path: Path, submit: bool = False) -> Optional[Path]:
        raise RuntimeError("已禁用后台 GUI bridge；请使用 Codex Computer Use 完成同花顺界面交互。")

    def _request_codex_computer_use(self, signal: OrderSignal, intent_path: Path, submit: bool = False) -> None:
        verification_path = self._verification_fields_path() or (SCREENSHOT_DIR / "latest_verified_order.json")
        payload = {
            "action": "ths_order_submit" if submit else "ths_order_verify",
            "created_at": self._now(),
            "source": "trading_engine",
            "app": "同花顺至尊版",
            "account_mode": self.ths_account_mode,
            "intent_path": str(intent_path),
            "verification_fields_path": str(verification_path),
            "order": asdict(signal),
            "submit": submit,
            "status": "pending",
            "instruction": (
                "请使用 Codex Computer Use 操作同花顺模拟账户界面，填入并校验订单字段；"
                "完成后写回 verification_fields_path，source 必须为 codex_computer_use。"
            ),
        }
        CODEX_COMPUTER_USE_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        CODEX_COMPUTER_USE_REQUEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info(
            "已请求 Codex Computer Use 执行同花顺%s: %s",
            "最终提交" if submit else "填单校验",
            CODEX_COMPUTER_USE_REQUEST_PATH,
        )
        self._wait_for_codex_verification(intent_path, submit=submit)

    def _wait_for_codex_verification(self, intent_path: Path, submit: bool = False) -> None:
        deadline = time.monotonic() + max(0, self.codex_wait_seconds)
        while time.monotonic() <= deadline:
            fields = self._read_verified_fields_after(intent_path)
            if fields and (not submit or fields.get("submitted")):
                return
            if stop_requested():
                return
            time.sleep(1)

    def _write_intent(self, signal: OrderSignal, submitted_at: str) -> Path:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        path = SCREENSHOT_DIR / "latest_order_intent.json"
        payload = {"submitted_at": submitted_at, "order": asdict(signal)}
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return path

    def _verification_fields_path(self) -> Optional[Path]:
        if not self.verification_fields_path:
            return None
        path = Path(self.verification_fields_path)
        if not path.is_absolute():
            path = ROOT / path
        return path

    def _read_verified_fields(self, override_path: Optional[Path] = None) -> dict:
        path = override_path or self._verification_fields_path()
        if path is None:
            return {}
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _read_verified_fields_after(self, intent_path: Path) -> dict:
        path = self._verification_fields_path()
        if path is None or not path.exists():
            return {}
        if path.stat().st_mtime < intent_path.stat().st_mtime:
            return {}
        return self._read_verified_fields(path)

    def _verify_fields(self, signal: OrderSignal, fields: dict) -> tuple[bool, str]:
        if not fields:
            return False, "未读取到 OCR/视觉校验字段。"
        if str(fields.get("symbol", "")) != signal.symbol:
            return False, f"代码不匹配: {fields.get('symbol')}"
        if str(fields.get("side", "")).upper() != signal.side:
            return False, f"方向不匹配: {fields.get('side')}"
        if str(fields.get("source", "")).strip() != "codex_computer_use":
            return False, f"校验来源不匹配: {fields.get('source')}"
        account_mode = self._normalize_account_mode(fields.get("account_mode") or fields.get("trade_mode"))
        if account_mode != self.ths_account_mode:
            return False, f"同花顺账户模式不匹配: {fields.get('account_mode') or fields.get('trade_mode')}"
        try:
            quantity = int(fields.get("quantity"))
            price = float(fields.get("limit_price"))
        except (TypeError, ValueError):
            return False, "数量或价格字段无法解析。"
        if quantity != signal.quantity:
            return False, f"数量不匹配: {quantity}"
        expected_price = float(signal.limit_price or 0.0)
        if abs(price - expected_price) > self.price_tolerance:
            return False, f"价格不匹配: {price} != {expected_price}"
        return True, "校验通过。"

    @staticmethod
    def _normalize_account_mode(value: object) -> str:
        text = str(value or "").strip().lower()
        if text in {"simulation", "simulate", "mock", "paper", "模拟", "模拟交易", "模拟盘"}:
            return "simulation"
        if text in {"live", "real", "cash", "实盘", "真实", "真实交易", "普通交易"}:
            return "live"
        return text


def build_executor(config: dict) -> Executor:
    mode = config.get("execution", {}).get("mode", "dry_run")
    if mode == "dry_run":
        return DryRunExecutor()
    if mode == "manual_confirm":
        return ManualConfirmExecutor()
    if mode == "ths_computer_use":
        return ThsComputerUseExecutor(config)
    raise RuntimeError(f"未知执行模式: {mode}")


def sync_portfolio_from_account(
    config: dict,
    portfolio: PortfolioStore,
    runtime_state: RuntimeState,
    *,
    force_refresh: bool = False,
    reason: str = "account_sync",
) -> tuple[bool, str]:
    execution = config.get("execution", {})
    command_template = str(execution.get("account_bridge_command", "")).strip()
    account_mode = str(execution.get("ths_account_mode", "simulation"))
    if account_mode != "simulation":
        return False, "当前阶段只允许同步同花顺模拟账户资金。"

    snapshot_path = resolve_execution_path(execution.get("account_snapshot_path", "screenshots/latest_account_snapshot.json"))
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    max_age_seconds = int(execution.get("account_snapshot_max_age_seconds", 300))
    allowed_sources = account_snapshot_allowed_sources(execution)
    if command_template:
        return False, "后台账户桥接命令已禁用；请使用 Codex Computer Use 写入同花顺账户快照。"

    min_mtime = time.time() if force_refresh else None
    snapshot = None if force_refresh else load_recent_account_snapshot(
        snapshot_path,
        account_mode,
        max_age_seconds=max_age_seconds,
        allowed_sources=allowed_sources,
    )
    if snapshot is None:
        request_codex_account_snapshot(
            snapshot_path,
            account_mode,
            int(execution.get("codex_computer_use_timeout_seconds", 180)),
            reason=reason,
            min_mtime=min_mtime,
            allowed_sources=allowed_sources,
            symbol=str(config["symbols"][0]),
            app_name=str(execution.get("ths_app_name") or "同花顺至尊版"),
            bundle_id=str(execution.get("ths_bundle_id") or "cn.com.10jqka.iHexinFee"),
            process_name=str(execution.get("ths_process_name") or "EQHexinFee"),
        )
        snapshot = load_recent_account_snapshot(
            snapshot_path,
            account_mode,
            max_age_seconds=max_age_seconds,
            min_mtime=min_mtime,
            allowed_sources=allowed_sources,
        )
    if snapshot is None:
        return (
            False,
            "Codex Computer Use 账户快照不存在、过期或无效: "
            f"{snapshot_path}；已写入待处理请求: {CODEX_COMPUTER_USE_REQUEST_PATH}。"
            "请在 Codex 会话中处理该请求并写回快照，或先用已有新快照重新运行。",
        )
    logging.info(
        "使用同花顺账户快照: source=%s path=%s age=%ss",
        snapshot.get("source"),
        snapshot_path,
        snapshot.get("fallback_snapshot_age_seconds"),
    )
    if snapshot.get("account_mode") != account_mode:
        return False, f"同花顺账户模式不匹配: {snapshot.get('account_mode')}"
    if snapshot.get("available_cash") is None and snapshot.get("cash_balance") is None:
        return False, "同花顺账户快照缺少可用资金/资金余额。"

    portfolio.sync_account_snapshot(snapshot, config["symbols"])
    portfolio.save()
    runtime_state.record_event("last_account_snapshot", portfolio.data.get("last_account_snapshot", {}))
    positions = snapshot.get("positions", [])
    logging.info(
        "已同步同花顺模拟账户: 可用资金=%s 资金余额=%s 总资产=%s 持仓数=%s",
        format_money(float(snapshot.get("available_cash") or 0.0)),
        format_money(float(snapshot.get("cash_balance") or 0.0)),
        format_money(float(snapshot.get("total_assets") or 0.0)),
        len(positions),
    )
    return True, "同花顺模拟账户资金同步完成。"


def account_snapshot_allowed_sources(execution: dict) -> set[str]:
    raw_sources = execution.get("account_snapshot_allowed_sources", ["codex_computer_use"])
    if isinstance(raw_sources, str):
        raw_sources = [raw_sources]
    sources = {str(source).strip() for source in raw_sources if str(source).strip()}
    return sources or {"codex_computer_use"}


def request_codex_account_snapshot(
    snapshot_path: Path,
    account_mode: str,
    wait_seconds: int,
    *,
    reason: str = "account_sync",
    min_mtime: Optional[float] = None,
    allowed_sources: Optional[set[str]] = None,
    symbol: str = "588330",
    app_name: str = "同花顺至尊版",
    bundle_id: str = "cn.com.10jqka.iHexinFee",
    process_name: str = "EQHexinFee",
) -> None:
    allowed_sources = allowed_sources or {"codex_computer_use"}
    payload = {
        "action": "ths_account_snapshot",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": "trading_engine",
        "reason": reason,
        "app": "同花顺至尊版",
        "account_mode": account_mode,
        "account_snapshot_path": str(snapshot_path),
        "status": "pending",
        "instruction": (
            "请使用 Codex Computer Use 读取同花顺模拟账户资金和持仓，写回 account_snapshot_path；"
            f"source 必须属于 {sorted(allowed_sources)}。"
        ),
        "allowed_sources": sorted(allowed_sources),
    }
    CODEX_COMPUTER_USE_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    CODEX_COMPUTER_USE_REQUEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("已请求 Codex Computer Use 同步同花顺账户快照: %s", CODEX_COMPUTER_USE_REQUEST_PATH)
    deadline = time.monotonic() + max(0, wait_seconds)
    next_log_at = time.monotonic() + 30
    next_apple_bridge_at = time.monotonic() if "apple_vision_ocr" in allowed_sources else None
    while time.monotonic() <= deadline:
        if next_apple_bridge_at is not None and time.monotonic() >= next_apple_bridge_at:
            run_apple_account_snapshot_bridge(
                snapshot_path,
                symbol=symbol,
                app_name=app_name,
                bundle_id=bundle_id,
                process_name=process_name,
            )
            next_apple_bridge_at = time.monotonic() + 10
        snapshot = load_recent_account_snapshot(
            snapshot_path,
            account_mode,
            max_age_seconds=max(wait_seconds, 1),
            min_mtime=min_mtime,
            allowed_sources=allowed_sources,
        )
        if snapshot is not None:
            mark_codex_request_status("completed", "账户快照已写回。")
            return
        if stop_requested():
            mark_codex_request_status("stopped", "检测到停止文件，停止等待账户快照。")
            return
        if time.monotonic() >= next_log_at:
            logging.info("仍在等待 Codex Computer Use 写回账户快照: %s", snapshot_path)
            next_log_at = time.monotonic() + 30
        time.sleep(1)
    mark_codex_request_status("timed_out", f"{wait_seconds}s 内未收到 Codex Computer Use 账户快照。")


def run_apple_app_bridge_navigation(
    symbol: str,
    *,
    app_name: str = "同花顺至尊版",
    bundle_id: str = "cn.com.10jqka.iHexinFee",
    process_name: str = "EQHexinFee",
) -> bool:
    script_path = ROOT / "App_Bridge_AppleScript.py"
    if not script_path.exists():
        logging.warning("AppleScript App bridge 脚本不存在: %s", script_path)
        return False
    output_path = SCREENSHOT_DIR / "latest_applescript_bridge_holdings.json"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--app-name",
                app_name,
                "--bundle-id",
                bundle_id,
                "--process-name",
                process_name,
                "--symbol",
                symbol,
                "--output",
                str(output_path),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=75,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("AppleScript App bridge 执行失败: %s", exc)
        return False
    if result.returncode != 0:
        logging.warning("AppleScript App bridge 未完成持仓页校验: %s", result.stderr.strip() or result.stdout.strip())
        return False
    logging.info("AppleScript App bridge 已进入并校验模拟持仓页: %s", output_path)
    return True


def run_apple_account_snapshot_bridge(
    snapshot_path: Path,
    *,
    symbol: str = "588330",
    app_name: str = "同花顺至尊版",
    bundle_id: str = "cn.com.10jqka.iHexinFee",
    process_name: str = "EQHexinFee",
) -> None:
    if not run_apple_app_bridge_navigation(
        symbol,
        app_name=app_name,
        bundle_id=bundle_id,
        process_name=process_name,
    ):
        return

    script_path = ROOT / "apple_account_snapshot.py"
    if not script_path.exists():
        logging.warning("Apple Vision OCR 账户快照脚本不存在: %s", script_path)
        return
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--app-name",
                app_name,
                "--bundle-id",
                bundle_id,
                "--process-name",
                process_name,
                "--symbol",
                symbol,
                "--write-latest",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("Apple Vision OCR 账户快照桥执行失败: %s", exc)
        return
    if result.returncode != 0:
        logging.warning("Apple Vision OCR 账户快照桥未写回有效快照: %s", result.stderr.strip() or result.stdout.strip())
        return
    try:
        snapshot = json.loads(result.stdout)
    except json.JSONDecodeError:
        logging.warning("Apple Vision OCR 账户快照桥输出不是有效 JSON: %s", result.stdout.strip())
        return
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("Apple Vision OCR 账户快照桥已写回: %s", snapshot_path)


def mark_codex_request_status(status: str, message: str) -> None:
    try:
        payload = json.loads(CODEX_COMPUTER_USE_REQUEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    payload["status"] = status
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["message"] = message
    try:
        CODEX_COMPUTER_USE_REQUEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logging.warning("更新 Codex Computer Use 请求状态失败: %s", CODEX_COMPUTER_USE_REQUEST_PATH)


def load_recent_account_snapshot(
    snapshot_path: Path,
    account_mode: str,
    max_age_seconds: int = 300,
    min_mtime: Optional[float] = None,
    allowed_sources: Optional[set[str]] = None,
) -> Optional[dict]:
    if not snapshot_path.exists():
        return None
    if min_mtime is not None and snapshot_path.stat().st_mtime < min_mtime:
        return None
    age_seconds = time.time() - snapshot_path.stat().st_mtime
    if age_seconds > max_age_seconds:
        return None
    try:
        with snapshot_path.open(encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if snapshot.get("account_mode") != account_mode:
        return None
    allowed_sources = allowed_sources or {"codex_computer_use"}
    if snapshot.get("source") not in allowed_sources:
        return None
    if snapshot.get("validation_errors"):
        return None
    if snapshot.get("warnings"):
        return None
    if snapshot.get("available_cash") is None and snapshot.get("cash_balance") is None:
        return None
    snapshot["fallback_snapshot_age_seconds"] = round(age_seconds, 1)
    return snapshot


def resolve_execution_path(value: object) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        path = ROOT / path
    return path


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def audit_signal(signal: OrderSignal, result: Optional[ExecutionResult], decision: RiskDecision) -> None:
    exists = SIGNAL_AUDIT_PATH.exists()
    with SIGNAL_AUDIT_PATH.open("a", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(["created_at", "symbol", "side", "quantity", "limit_price", "amount", "risk_allowed", "risk_reason", "status", "message"])
        status = result.status if result else "risk_blocked"
        message = result.message if result else decision.reason
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            signal.symbol,
            signal.side,
            signal.quantity,
            signal.limit_price,
            f"{signal.amount():.2f}",
            decision.allowed,
            decision.reason,
            status,
            message,
        ])


def format_trade_time(value: str) -> str:
    if len(value) >= 14 and value[:14].isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]} {value[8:10]}:{value[10:12]}:{value[12:14]}"
    return value or "N/A"


def format_block_time(value: str) -> str:
    return format_trade_time(value).replace(" ", "  ", 1)


def format_change_percent(value: str) -> str:
    if not value:
        return "N/A"
    return value if value.endswith("%") else f"{value}%"


def format_relation(left: float, right: float) -> str:
    if left > right:
        return ">"
    if left < right:
        return "<"
    return "="


def format_money(value: float) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def color_text(text: str, color: str) -> str:
    return f"{color}{text}{ANSI_RESET}"


def change_percent_is_down(line: str) -> bool:
    marker = "涨幅="
    if marker not in line:
        return False
    value = line.split(marker, 1)[1].strip()
    return value.startswith("-")


def color_log_line(line: str) -> str:
    content = line.strip()
    if content == LOG_SEPARATOR:
        return color_text(line, ANSI_BLACK)
    if content.startswith("股票名称："):
        return color_text(line, ANSI_BOLD_BLUE)
    if content.startswith("行情时间："):
        return color_text(line, ANSI_DARK_GRAY)
    if content.startswith("实时行情："):
        return color_text(line, ANSI_BOLD_GREEN if change_percent_is_down(line) else ANSI_BOLD_RED)
    if content.startswith("当前仓位："):
        return color_text(line, ANSI_BROWN)
    if content.startswith("趋势检查："):
        return color_text(line, ANSI_PURPLE)
    if content.startswith("均线="):
        return color_text(line, ANSI_DARK_CYAN)
    if content.startswith("交易条件："):
        if "未满足" in content:
            return color_text(line, ANSI_DARK_GRAY)
        if "满足买入" in content:
            return color_text(line, ANSI_BOLD_RED)
        if "满足卖出" in content:
            return color_text(line, ANSI_BOLD_GREEN)
        return color_text(line, ANSI_DARK_GRAY)
    if "风控" in content or "风险" in content:
        return color_text(line, ANSI_PINK)
    return line


class TerminalColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return "\n".join(color_log_line(line) for line in text.splitlines())


def setup_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    file_formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(file_formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(TerminalColorFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)


def signal_condition_text(signal: Optional[OrderSignal]) -> str:
    if not signal:
        return "未满足买入/卖出条件"
    side = "买入" if signal.side == "BUY" else "卖出"
    return f"满足{side}条件"


def log_cycle_summary(symbol: str, quote, portfolio: PortfolioStore, diagnostics: dict, signal: Optional[OrderSignal]) -> None:
    position = portfolio.position(symbol)
    quantity = int(position.get("quantity") or 0)
    holding_value = quantity * quote.price
    cash = portfolio.cash()
    equity = portfolio.cash() + holding_value
    realtime_position_ratio = (holding_value / equity) if equity > 0 else 0.0
    latest_buy_price = diagnostics.get("latest_buy_price")
    ma5 = float(diagnostics.get("ma5") or 0.0)
    ma10 = float(diagnostics.get("ma10") or 0.0)
    ma20 = float(diagnostics.get("ma20") or 0.0)
    ma60 = float(diagnostics.get("ma60") or 0.0)
    latest_buy_price_text = f"{latest_buy_price:.4f}" if latest_buy_price is not None else "N/A"

    logging.info(
        "\n" + "\n".join([
            LOG_SEPARATOR,
            f"股票名称：{symbol}  {quote.name}",
            f"行情时间：{format_block_time(quote.trade_time)}",
            f"实时行情：最新价={quote.price:.4f}  涨幅={format_change_percent(quote.change_percent)}",
            f"当前仓位：仓位={realtime_position_ratio:.2%}  持仓金额={format_money(holding_value)}  剩余金额={format_money(cash)}",
            (
                f"趋势检查：连续上涨={'是' if diagnostics.get('first_buy_rising') else '否'}  "
                f"最新买价={latest_buy_price_text}  "
                f"高于最新买价={'是' if diagnostics.get('price_above_latest_buy') else '否'}"
            ),
            (
                "          "
                f"均线=(MA5={ma5:.4f}) {format_relation(ma5, ma10)} "
                f"(MA10={ma10:.4f}) {format_relation(ma10, ma20)} "
                f"(MA20={ma20:.4f}) {format_relation(ma20, ma60)} "
                f"(MA60={ma60:.4f})"
            ),
            f"交易条件：{signal_condition_text(signal)}",
            LOG_SEPARATOR,
        ])
    )


def stop_requested(path: Optional[Path] = None) -> bool:
    path = path or STOP_PATH
    return path.exists()


def request_stop(reason: str = "user_requested", path: Optional[Path] = None) -> None:
    path = path or STOP_PATH
    payload = {
        "requested_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "pid": os.getpid(),
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def clear_stop_request(path: Optional[Path] = None) -> bool:
    path = path or STOP_PATH
    if not path.exists():
        return False
    path.unlink()
    return True


def running_engine_processes() -> list[str]:
    try:
        result = subprocess.run(["ps", "ax", "-o", "pid=,command="], check=True, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []

    current_pid = str(os.getpid())
    processes = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        pid, command = parts
        if pid == current_pid:
            continue
        command_lower = command.lower()
        executable = Path(command.split(maxsplit=1)[0]).name.lower()
        if "trading_engine.py" in command and executable.startswith("python"):
            processes.append(f"{pid} {command}")
    return processes


def latest_log_lines(path: Path = LOG_PATH, limit: int = 8) -> list[str]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", errors="replace") as handle:
        return handle.readlines()[-limit:]


def open_live_log_window(path: Path = LOG_PATH) -> bool:
    path.touch(exist_ok=True)
    logging.info("按 Codex Computer Use 优先原则，不自动操作 Terminal。实时日志命令: tail -f %s", path)
    return False


def open_trading_app(execution: dict) -> bool:
    app_name = str(execution.get("ths_app_name") or "同花顺至尊版").strip()
    if not app_name:
        return False
    try:
        subprocess.run(["open", "-a", app_name], check=True, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        fallback = Path("/Applications") / f"{app_name}.app"
        if fallback.exists():
            try:
                subprocess.run(["open", str(fallback)], check=True, capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as fallback_exc:
                logging.warning("同花顺 App 打开失败: %s；fallback=%s error=%s", app_name, fallback, fallback_exc)
                return False
            logging.info("已打开同花顺 App: %s", fallback)
            return True
        logging.warning("同花顺 App 打开失败: %s；error=%s", app_name, exc)
        return False
    logging.info("已打开同花顺 App: %s", app_name)
    return True


def print_status() -> None:
    print(f"一键停止文件: {'已启用' if stop_requested() else '未启用'} ({STOP_PATH})")
    processes = running_engine_processes()
    if processes:
        print("正在运行的交易引擎进程:")
        for process in processes:
            print(f"  {process}")
    else:
        print("正在运行的交易引擎进程: 未发现")
    print(f"日志文件: {LOG_PATH}")
    lines = latest_log_lines()
    if lines:
        print("最近日志:")
        for line in lines:
            print(f"  {line.rstrip()}")
    print("常用控制命令:")
    print("  前台可视运行: python3 trading_engine.py")
    print("  停止引擎:     python3 trading_engine.py --stop")
    print("  恢复允许运行: python3 trading_engine.py --clear-stop")


def wait_for_next_poll(seconds: int) -> bool:
    deadline = time.monotonic() + max(0, seconds)
    while time.monotonic() < deadline:
        if stop_requested():
            logging.info("检测到停止文件，退出持续运行。")
            return False
        time.sleep(min(STOP_POLL_SECONDS, max(0, deadline - time.monotonic())))
    return True


def sync_account_after_app_open(config: dict) -> bool:
    execution = config.get("execution", {})
    portfolio = PortfolioStore(PORTFOLIO_PATH, config["symbols"], float(config["portfolio"]["initial_cash"]))
    runtime_state = RuntimeState(RUN_STATE_PATH)
    ok, sync_message = sync_portfolio_from_account(
        config,
        portfolio,
        runtime_state,
        force_refresh=bool(execution.get("force_account_sync_on_app_open", False)),
        reason="app_open",
    )
    if not ok:
        logging.error(sync_message)
        Notifier(config).notify("AI Stock 账户同步失败", sync_message)
        return False
    logging.info("打开同花顺 App 后账户资金/持仓验证完成。")
    return True


def refresh_signal_before_execution(
    config: dict,
    portfolio: PortfolioStore,
    runtime_state: RuntimeState,
    market_data: EastMoneyMarketData,
    symbol: str,
    today: str,
) -> tuple[Optional[OrderSignal], dict, str]:
    ok, sync_message = sync_portfolio_from_account(
        config,
        portfolio,
        runtime_state,
        force_refresh=True,
        reason="before_order",
    )
    if not ok:
        return None, {}, sync_message
    refreshed_strategy = TrendPullbackStrategy(config, market_data, portfolio)
    try:
        refreshed_signal = refreshed_strategy.generate(symbol, today)
    except RuntimeError as exc:
        return None, refreshed_strategy.last_diagnostics, f"{symbol} 二次账户同步后策略检查失败: {exc}"
    return refreshed_signal, refreshed_strategy.last_diagnostics, "交易前账户资金/持仓验证完成。"


def sync_account_after_execution(config: dict, portfolio: PortfolioStore, runtime_state: RuntimeState) -> tuple[bool, str]:
    return sync_portfolio_from_account(
        config,
        portfolio,
        runtime_state,
        force_refresh=True,
        reason="after_order",
    )


def apply_execution_limit_price(signal: OrderSignal, quote) -> OrderSignal:
    if signal.side == "BUY":
        price = getattr(quote, "limit_up", None)
        if price is None or float(price) <= 0:
            raise RuntimeError(f"{signal.symbol} 缺少涨停价，禁止生成买入执行指令。")
        return replace(signal, limit_price=float(price), note=f"{signal.note}；执行限价=涨停价{float(price):.4f}")
    if signal.side == "SELL":
        price = getattr(quote, "limit_down", None)
        if price is None or float(price) <= 0:
            raise RuntimeError(f"{signal.symbol} 缺少跌停价，禁止生成卖出执行指令。")
        return replace(signal, limit_price=float(price), note=f"{signal.note}；执行限价=跌停价{float(price):.4f}")
    raise RuntimeError(f"未知买卖方向: {signal.side}")


def run_once(
    config: dict,
    ignore_hours: bool = False,
    ignore_trade_day: bool = False,
    *,
    sync_account_at_start: bool = True,
) -> None:
    clock = TradingClock(config["timezone"], config["trading_sessions"])
    notifier = Notifier(config)
    risk_manager = RiskManager(config, clock)
    portfolio = PortfolioStore(PORTFOLIO_PATH, config["symbols"], float(config["portfolio"]["initial_cash"]))
    runtime_state = RuntimeState(RUN_STATE_PATH)
    if sync_account_at_start:
        ok, sync_message = sync_portfolio_from_account(config, portfolio, runtime_state, reason="run_once_start")
        if not ok:
            logging.error(sync_message)
            notifier.notify("AI Stock 账户同步失败", sync_message)
            return

    if STOP_PATH.exists():
        message = f"检测到一键停止文件: {STOP_PATH}"
        logging.error(message)
        notifier.notify("AI Stock 已停止", message)
        return
    if not ignore_trade_day and config.get("runtime", {}).get("trade_day_schedule_enabled", True) and not clock.is_trade_day():
        logging.info("当前不是交易日，跳过本轮检查。")
        return
    if not ignore_hours and not clock.is_open():
        logging.info("当前不在交易时间内，跳过本轮检查。")
        return

    today = clock.today()
    market_data = EastMoneyMarketData()
    strategy = TrendPullbackStrategy(config, market_data, portfolio)
    executor = build_executor(config)

    for symbol in config["symbols"]:
        if portfolio.traded_today(symbol, today):
            logging.info("%s 今日已执行过交易，跳过。", symbol)
            continue

        try:
            quote = market_data.latest_quote(symbol)
        except RuntimeError as exc:
            message = f"{symbol} 实时行情获取失败: {exc}"
            logging.error(message)
            notifier.notify("AI Stock 行情异常", message)
            portfolio.save()
            continue

        try:
            signal = strategy.generate(symbol, today)
        except RuntimeError as exc:
            message = f"{symbol} 策略检查失败: {exc}"
            logging.error(message)
            notifier.notify("AI Stock 策略异常", message)
            portfolio.save()
            continue
        log_cycle_summary(symbol, quote, portfolio, strategy.last_diagnostics, signal)
        if not signal:
            portfolio.save()
            continue

        original_signal = signal
        signal, refreshed_diagnostics, refresh_message = refresh_signal_before_execution(
            config,
            portfolio,
            runtime_state,
            market_data,
            symbol,
            today,
        )
        if not signal:
            logging.warning("交易前账户二次校验后不再满足交易条件: %s", refresh_message)
            notifier.notify("AI Stock 交易前校验取消", refresh_message)
            portfolio.save()
            continue
        if asdict(signal) != asdict(original_signal):
            logging.info("交易前账户二次同步后信号已更新: 原始=%s 最新=%s", original_signal, signal)
            log_cycle_summary(symbol, quote, portfolio, refreshed_diagnostics, signal)
        try:
            signal = apply_execution_limit_price(signal, quote)
        except RuntimeError as exc:
            message = f"{symbol} 执行限价生成失败: {exc}"
            logging.error(message)
            notifier.notify("AI Stock 执行限价异常", message)
            portfolio.save()
            continue

        notifier.notify("AI Stock 生成交易信号", f"{signal.side} {signal.symbol} {signal.quantity} @ {signal.limit_price}；{signal.note}")
        decision = risk_manager.validate_order(
            signal,
            portfolio,
            today,
            ignore_hours=ignore_hours,
            ignore_trade_day=ignore_trade_day,
        )
        if not decision.allowed:
            logging.error("风控拦截: %s", decision.reason)
            notifier.notify("AI Stock 风控拦截", decision.reason)
            audit_signal(signal, None, decision)
            portfolio.save()
            continue

        try:
            result = executor.place_order(signal)
        except RuntimeError as exc:
            result = ExecutionResult(
                success=False,
                status="execution_error",
                message=str(exc),
                submitted_at=datetime.now().isoformat(timespec="seconds"),
                verified_fields={},
            )

        audit_signal(signal, result, decision)
        runtime_state.record_event("last_execution_result", asdict(result))
        if result.success:
            if result.status == "gui_simulation_verified":
                runtime_state.increment("gui_simulation_success_count")
                portfolio.save()
                notifier.notify("AI Stock GUI 模拟通过", f"{result.status}: {result.message}")
                logging.info("%s GUI 模拟校验通过，未记录为真实成交: %s", symbol, signal)
                break
            portfolio.apply_fill(signal, signal.limit_price or 0.0)
            portfolio.mark_trade(symbol, today)
            portfolio.save()
            ok, sync_message = sync_account_after_execution(config, portfolio, runtime_state)
            if not ok:
                logging.error("交易后账户资金/持仓验证失败: %s", sync_message)
                notifier.notify("AI Stock 交易后校验失败", sync_message)
            else:
                logging.info("交易后账户资金/持仓验证完成。")
            notifier.notify("AI Stock 执行成功", f"{result.status}: {result.message}")
            logging.info("%s 已记录交易状态: %s", symbol, signal)
            break

        portfolio.save()
        notifier.notify("AI Stock 执行失败", f"{result.status}: {result.message}")
        logging.error("执行失败: %s", result)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Stock 自动量化交易平台 v1")
    parser.add_argument("--once", action="store_true", help="只运行一次检查")
    parser.add_argument("--ignore-hours", action="store_true", help="忽略交易时段限制，便于测试策略")
    parser.add_argument("--ignore-trade-day", action="store_true", help="忽略交易日限制，仅用于 dry-run/测试")
    parser.add_argument("--check-config", action="store_true", help="只检查配置并退出")
    parser.add_argument("--status", action="store_true", help="查看运行状态、停止文件和最近日志")
    parser.add_argument("--stop", action="store_true", help="写入 STOP_TRADING，让持续运行的交易引擎尽快退出")
    parser.add_argument("--clear-stop", action="store_true", help="清除 STOP_TRADING，允许交易引擎再次运行")
    parser.add_argument("--open-log", action="store_true", help="持续运行时额外打开实时日志窗口")
    args = parser.parse_args()

    setup_logging()

    if args.stop:
        request_stop()
        logging.info("已写入停止文件: %s", STOP_PATH)
        print(f"已请求停止交易引擎: {STOP_PATH}")
        return

    if args.clear_stop:
        removed = clear_stop_request()
        logging.info("%s停止文件: %s", "已清除" if removed else "未发现", STOP_PATH)
        print(f"{'已清除' if removed else '未发现'}停止文件: {STOP_PATH}")
        return

    if args.status:
        print_status()
        return

    config = load_config()
    execution = config.get("execution", {})
    logging.info(
        "启动交易机器人，股票代码: %s，执行模式=%s，执行阶段=%s",
        ",".join(config["symbols"]),
        execution.get("mode", "dry_run"),
        execution.get("stage", "dry_run"),
    )
    if args.check_config:
        clock = TradingClock(config["timezone"], config["trading_sessions"])
        RiskManager(config, clock)
        build_executor(config)
        print("配置检查通过")
        return

    Notifier(config).notify("AI Stock 启动", f"执行模式={execution.get('mode', 'dry_run')} 执行阶段={execution.get('stage', 'dry_run')}")
    account_synced_after_app_open = False
    if execution.get("mode") == "ths_computer_use":
        open_trading_app(execution)
        account_synced_after_app_open = sync_account_after_app_open(config)
        if not account_synced_after_app_open:
            return
    if args.ignore_trade_day and execution.get("mode", "dry_run") != "dry_run":
        raise SystemExit("--ignore-trade-day 只允许搭配 execution.mode=dry_run 使用。")

    if args.once:
        run_once(
            config,
            args.ignore_hours,
            args.ignore_trade_day,
            sync_account_at_start=not account_synced_after_app_open,
        )
        return

    poll_seconds = int(config["poll_seconds"])
    logging.info("进入持续运行。前台可按 Ctrl+C 退出；也可运行 python3 trading_engine.py --stop。")
    if args.open_log:
        open_live_log_window()
    try:
        while True:
            if stop_requested():
                logging.info("检测到停止文件，退出持续运行。")
                break
            run_once(
                config,
                args.ignore_hours,
                args.ignore_trade_day,
                sync_account_at_start=not account_synced_after_app_open,
            )
            if not wait_for_next_poll(poll_seconds):
                break
    except KeyboardInterrupt:
        logging.info("收到 Ctrl+C，交易机器人已停止。")
        print("\n已停止 trading_engine。")
    finally:
        logging.info("交易机器人退出。")


if __name__ == "__main__":
    main()
