#!/usr/bin/env python3
import argparse
import csv
import json
import logging
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from trading_strategy import Candle, OrderSignal, TrendPullbackStrategy


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
PORTFOLIO_PATH = ROOT / "portfolio.json"
LOG_PATH = ROOT / "trade_bot.log"
STOP_PATH = ROOT / "STOP_TRADING"
RUN_STATE_PATH = ROOT / "runtime_state.json"
SIGNAL_AUDIT_PATH = ROOT / "signals.csv"
SCREENSHOT_DIR = ROOT / "screenshots"


VALID_SIDES = {"BUY", "SELL"}
EXECUTION_MODES = {"dry_run", "manual_confirm", "ths_computer_use"}
EXECUTION_STAGES = {"dry_run", "gui_simulation", "small_live", "full_live"}


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


class EastMoneyMarketData:
    def __init__(self, timeout_seconds: int = 10) -> None:
        self.timeout_seconds = timeout_seconds

    def daily_candles(self, symbol: str, limit: int = 120) -> list[Candle]:
        try:
            return self._eastmoney_daily_candles(symbol, limit)
        except RuntimeError as exc:
            logging.warning("%s 东方财富日 K 失败，切换腾讯备用源: %s", symbol, exc)
            return self._tencent_daily_candles(symbol, limit)

    def _eastmoney_daily_candles(self, symbol: str, limit: int) -> list[Candle]:
        secid = self._secid(symbol)
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "1",
            "beg": "0",
            "end": "20500101",
            "lmt": str(limit),
        }
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        payload = self._fetch_json(req)

        klines = ((payload.get("data") or {}).get("klines") or [])
        candles: list[Candle] = []
        for item in klines:
            parts = item.split(",")
            if len(parts) < 3:
                continue
            candles.append(Candle(trade_date=parts[0], close=float(parts[2])))

        if len(candles) < 60:
            raise RuntimeError(f"{symbol} 日 K 数据不足，无法计算 60 日线。")
        return candles

    def _tencent_daily_candles(self, symbol: str, limit: int) -> list[Candle]:
        market_symbol = self._tencent_symbol(symbol)
        params = {"param": f"{market_symbol},day,,,{limit},qfq"}
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        payload = self._fetch_json(req)
        rows = (((payload.get("data") or {}).get(market_symbol) or {}).get("qfqday") or
                ((payload.get("data") or {}).get(market_symbol) or {}).get("day") or [])

        candles = [Candle(trade_date=row[0], close=float(row[2])) for row in rows if len(row) >= 3]
        if len(candles) < 60:
            raise RuntimeError(f"{symbol} 腾讯日 K 数据不足，无法计算 60 日线。")
        return candles

    def _fetch_json(self, req: urllib.request.Request) -> dict:
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (OSError, socket.timeout, json.JSONDecodeError) as exc:
                last_error = exc
                logging.warning("行情数据请求失败，第 %s 次重试: %s", attempt, exc)
                time.sleep(attempt)
        raise RuntimeError(f"行情数据请求连续失败: {last_error}")

    @staticmethod
    def _secid(symbol: str) -> str:
        if symbol.startswith(("5", "6", "9")):
            return f"1.{symbol}"
        return f"0.{symbol}"

    @staticmethod
    def _tencent_symbol(symbol: str) -> str:
        if symbol.startswith(("5", "6", "9")):
            return f"sh{symbol}"
        return f"sz{symbol}"


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
            "buy_prices": [],
            "last_trade_date": None,
        }

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

        text = f"{title}\n{message}"
        script = f'''
        tell application "WeChat" to activate
        delay 0.5
        tell application "System Events"
            keystroke "f" using command down
            delay 0.2
            keystroke {json.dumps(recipient)}
            delay 0.5
            key code 36
            delay 0.5
            keystroke {json.dumps(text)}
            key code 36
        end tell
        '''
        try:
            subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)
            logging.info("微信客户端通知已发送给 %s。", recipient)
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            logging.error("微信客户端通知失败，已保留日志: %s", exc)
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

        if signal.side == "BUY":
            max_cash = self._stage_cash_limit(stage)
            if signal.amount() > max_cash:
                return RiskDecision(False, f"订单金额 {signal.amount():.2f} 超过阶段上限 {max_cash:.2f}。")

        return RiskDecision(True, "风控通过。")

    def _stage_allows_mode(self, stage: str, mode: str) -> bool:
        if stage == "dry_run":
            return mode == "dry_run"
        if stage == "gui_simulation":
            return mode in {"dry_run", "manual_confirm", "ths_computer_use"}
        if stage in {"small_live", "full_live"}:
            return mode in {"manual_confirm", "ths_computer_use"}
        return False

    def _stage_cash_limit(self, stage: str) -> float:
        limits = self.execution.get("stage_cash_limits", {})
        if stage == "small_live":
            return float(limits.get("small_live", self.execution.get("small_live_cash", 5000)))
        if stage == "full_live":
            return float(limits.get("full_live", self.execution.get("max_live_cash", 50000)))
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

    def place_order(self, signal: OrderSignal) -> ExecutionResult:
        submitted_at = self._now()
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        intent_path = self._write_intent(signal, submitted_at)
        self._activate_ths()
        screenshot_path = self._capture_screenshot(signal, submitted_at)
        fields = self._read_verified_fields()

        if self.require_screenshot_verification:
            ok, message = self._verify_fields(signal, fields)
            if not ok:
                return ExecutionResult(
                    success=False,
                    status="verification_failed",
                    message=f"同花顺界面校验失败: {message}；intent={intent_path}",
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

        return ExecutionResult(
            success=False,
            status="real_submit_not_implemented",
            message="真实最终确认点击尚未接入外部 Computer Use 自动化适配器，已阻断。",
            submitted_at=submitted_at,
            verified_fields=fields,
            screenshot_path=str(screenshot_path) if screenshot_path else None,
        )

    def _activate_ths(self) -> None:
        try:
            subprocess.run(["open", "-a", "同花顺"], check=True, capture_output=True, text=True, timeout=8)
            time.sleep(1)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"无法打开或激活同花顺.app: {exc}") from exc

    def _capture_screenshot(self, signal: OrderSignal, submitted_at: str) -> Optional[Path]:
        path = SCREENSHOT_DIR / f"ths_{submitted_at.replace(':', '').replace('-', '')}_{signal.symbol}_{signal.side}.png"
        try:
            from PIL import ImageGrab

            image = ImageGrab.grab()
            image.save(path)
            logging.info("已保存同花顺校验截图: %s", path)
            return path
        except Exception as exc:
            if self.require_screenshot_verification:
                raise RuntimeError(f"截图失败，禁止继续执行: {exc}") from exc
            logging.warning("截图失败，按配置允许继续: %s", exc)
            return None

    def _write_intent(self, signal: OrderSignal, submitted_at: str) -> Path:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        path = SCREENSHOT_DIR / "latest_order_intent.json"
        payload = {"submitted_at": submitted_at, "order": asdict(signal)}
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return path

    def _read_verified_fields(self) -> dict:
        if not self.verification_fields_path:
            return {}
        path = Path(self.verification_fields_path)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _verify_fields(self, signal: OrderSignal, fields: dict) -> tuple[bool, str]:
        if not fields:
            return False, "未读取到 OCR/视觉校验字段。"
        if str(fields.get("symbol", "")) != signal.symbol:
            return False, f"代码不匹配: {fields.get('symbol')}"
        if str(fields.get("side", "")).upper() != signal.side:
            return False, f"方向不匹配: {fields.get('side')}"
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


def build_executor(config: dict) -> Executor:
    mode = config.get("execution", {}).get("mode", "dry_run")
    if mode == "dry_run":
        return DryRunExecutor()
    if mode == "manual_confirm":
        return ManualConfirmExecutor()
    if mode == "ths_computer_use":
        return ThsComputerUseExecutor(config)
    raise RuntimeError(f"未知执行模式: {mode}")


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


def run_once(config: dict, ignore_hours: bool = False, ignore_trade_day: bool = False) -> None:
    clock = TradingClock(config["timezone"], config["trading_sessions"])
    notifier = Notifier(config)
    risk_manager = RiskManager(config, clock)

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
    portfolio = PortfolioStore(PORTFOLIO_PATH, config["symbols"], float(config["portfolio"]["initial_cash"]))
    strategy = TrendPullbackStrategy(config, EastMoneyMarketData(), portfolio)
    executor = build_executor(config)
    runtime_state = RuntimeState(RUN_STATE_PATH)

    for symbol in config["symbols"]:
        if portfolio.traded_today(symbol, today):
            logging.info("%s 今日已执行过交易，跳过。", symbol)
            continue

        try:
            signal = strategy.generate(symbol, today)
        except RuntimeError as exc:
            message = f"{symbol} 策略检查失败: {exc}"
            logging.error(message)
            notifier.notify("AI Stock 策略异常", message)
            portfolio.save()
            continue
        if not signal:
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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )

    config = load_config()
    execution = config.get("execution", {})
    logging.info(
        "启动交易机器人，股票代码: %s，mode=%s，stage=%s",
        ",".join(config["symbols"]),
        execution.get("mode", "dry_run"),
        execution.get("stage", "dry_run"),
    )
    if args.check_config:
        clock = TradingClock(config["timezone"], config["trading_sessions"])
        RiskManager(config, clock)
        build_executor(config)
        print("config ok")
        return

    Notifier(config).notify("AI Stock 启动", f"mode={execution.get('mode', 'dry_run')} stage={execution.get('stage', 'dry_run')}")
    if args.ignore_trade_day and execution.get("mode", "dry_run") != "dry_run":
        raise SystemExit("--ignore-trade-day 只允许搭配 execution.mode=dry_run 使用。")

    if args.once:
        run_once(config, args.ignore_hours, args.ignore_trade_day)
        return

    while True:
        run_once(config, args.ignore_hours, args.ignore_trade_day)
        time.sleep(int(config["poll_seconds"]))


if __name__ == "__main__":
    main()
