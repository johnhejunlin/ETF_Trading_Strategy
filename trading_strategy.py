import logging
import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Candle:
    trade_date: str
    close: float


@dataclass(frozen=True)
class OrderSignal:
    symbol: str
    side: str
    quantity: int
    limit_price: Optional[float]
    note: str = ""

    def amount(self) -> float:
        return self.quantity * float(self.limit_price or 0.0)


class TrendPullbackStrategy:
    def __init__(self, config: dict, market_data, portfolio) -> None:
        self.config = config
        self.market_data = market_data
        self.portfolio = portfolio
        self.last_diagnostics: dict = {}
        self.buy_position_targets = [
            float(ratio) for ratio in config["strategy"].get("buy_position_targets", [0.5, 0.85, 1.0])
        ]
        self.max_position_ratio = float(config["strategy"].get("max_position_ratio", 1.0))
        self.sell_holding_ratio = float(config["strategy"]["sell_holding_ratio"])
        self.stop_loss_ratio = float(config["strategy"].get("stop_loss_ratio", 0.03))
        self.profit_drawdown_ratio = float(config["strategy"]["profit_drawdown_ratio"])
        self.sell_below_ma_window = int(config["strategy"].get("sell_below_ma_window", 0))
        self.clear_position_on_sell_count = int(config["strategy"]["clear_position_on_sell_count"])
        self.lot_size = int(config["strategy"]["lot_size"])

    def generate(self, symbol: str, today: str) -> Optional[OrderSignal]:
        self.last_diagnostics = {}
        candles = self.market_data.daily_candles(symbol)
        current_price = candles[-1].close
        self.portfolio.update_max_profit(symbol, current_price)

        position = self.portfolio.position(symbol)
        quantity = int(position["quantity"])
        latest_buy_price = self._latest_buy_price(position)
        self.last_diagnostics = self._trend_diagnostics(candles, quantity, current_price, latest_buy_price)
        # One completed operation per symbol per trading day, regardless of
        # direction.  This check must precede sell logic so a same-day buy can
        # never be followed by a stop-loss/profit-taking sell.
        if self.portfolio.traded_today(symbol, today):
            self.last_diagnostics["trading_block_reason"] = "今日已交易，仅监控（买入和卖出均禁止）"
            logging.info("%s 今日已交易，买入和卖出均跳过。", symbol)
            return None
        if quantity > 0:
            sell_signal = self._sell_signal(
                symbol,
                quantity,
                float(position["avg_cost"]),
                float(position["max_profit_pct"]),
                int(position.get("sell_streak") or 0),
                current_price,
                candles,
            )
            if sell_signal:
                return sell_signal

        buy_target = self._buy_target(candles, quantity, current_price, latest_buy_price)
        if buy_target:
            target_ratio, condition_note = buy_target
            equity = self.portfolio.cash() + quantity * current_price
            buy_quantity = self._target_position_quantity(
                cash=self.portfolio.cash(),
                quantity=quantity,
                price=current_price,
                equity=equity,
                target_ratio=target_ratio,
            )
            if buy_quantity > 0:
                return OrderSignal(
                    symbol=symbol,
                    side="BUY",
                    quantity=buy_quantity,
                    limit_price=current_price,
                    note=f"{condition_note}，买入至目标仓位{target_ratio:.0%}",
                )

        return None

    def _trend_diagnostics(
        self,
        candles: list[Candle],
        quantity: int,
        current_price: float,
        latest_buy_price: Optional[float],
    ) -> dict:
        closes = [c.close for c in candles]
        first_buy_rising = self._rising_through_today(closes)
        ma5 = self._ma(closes, 5)
        ma10 = self._ma(closes, 10)
        ma20 = self._ma(closes, 20)
        ma60 = self._ma(closes, 60)
        first_buy_ma = ma5 > ma10 > ma20
        add_buy_ma = first_buy_ma and ma20 > ma60
        equity = self.portfolio.cash() + quantity * current_price
        position_ratio = (quantity * current_price / equity) if equity > 0 else 0.0
        price_above_latest_buy = latest_buy_price is not None and current_price > latest_buy_price
        return {
            "position_ratio": position_ratio,
            "first_buy_rising": first_buy_rising,
            "latest_buy_price": latest_buy_price,
            "price_above_latest_buy": price_above_latest_buy,
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "ma60": ma60,
            "first_buy_ma": first_buy_ma,
            "add_buy_ma": add_buy_ma,
        }

    def _buy_target(
        self,
        candles: list[Candle],
        quantity: int,
        current_price: float,
        latest_buy_price: Optional[float],
    ) -> Optional[tuple[float, str]]:
        diagnostics = self.last_diagnostics or self._trend_diagnostics(candles, quantity, current_price, latest_buy_price)
        first_buy_rising = bool(diagnostics["first_buy_rising"])
        first_buy_ma = bool(diagnostics["first_buy_ma"])
        add_buy_ma = bool(diagnostics["add_buy_ma"])
        position_ratio = float(diagnostics["position_ratio"])
        price_above_latest_buy = bool(diagnostics["price_above_latest_buy"])
        first_target, second_target, final_target = self.buy_position_targets
        if quantity == 0:
            if first_buy_rising and first_buy_ma:
                return first_target, "空仓，今天>昨天>前天且 MA5>MA10>MA20"
            return None
        if position_ratio >= second_target:
            if position_ratio < final_target and add_buy_ma and price_above_latest_buy:
                return final_target, "仓位>=85%，当天价格大于最新买入价且 MA5>MA10>MA20>MA60"
            return None
        if position_ratio >= first_target:
            if add_buy_ma and price_above_latest_buy:
                return second_target, "仓位>=50%，当天价格大于最新买入价且 MA5>MA10>MA20>MA60"
            return None
        return None

    def _sell_signal(
        self,
        symbol: str,
        quantity: int,
        avg_cost: float,
        max_profit_pct: float,
        sell_streak: int,
        current_price: float,
        candles: list[Candle],
    ) -> Optional[OrderSignal]:
        if avg_cost <= 0:
            return None

        current_profit_pct = (current_price - avg_cost) / avg_cost
        if current_profit_pct <= -self.stop_loss_ratio:
            return OrderSignal(
                symbol=symbol,
                side="SELL",
                quantity=quantity,
                limit_price=current_price,
                note=f"亏损达到{self.stop_loss_ratio:.0%}，清仓",
            )

        if self.sell_below_ma_window:
            ma_stop = self._ma([c.close for c in candles], self.sell_below_ma_window)
            if current_price < ma_stop:
                return OrderSignal(
                    symbol=symbol,
                    side="SELL",
                    quantity=quantity,
                    limit_price=current_price,
                    note=f"收盘价跌破 MA{self.sell_below_ma_window}，清仓",
                )

        if max_profit_pct <= 0:
            return None

        trigger_profit_pct = max_profit_pct * (1 - self.profit_drawdown_ratio)
        logging.info(
            "%s 回撤检查: current_profit=%.2f%% max_profit=%.2f%% trigger=%.2f%%",
            symbol,
            current_profit_pct * 100,
            max_profit_pct * 100,
            trigger_profit_pct * 100,
        )
        if current_profit_pct > trigger_profit_pct:
            return None

        next_sell_count = sell_streak + 1
        if next_sell_count >= self.clear_position_on_sell_count:
            sell_quantity = quantity
            action_note = f"第{next_sell_count}次连续卖出，清仓"
        else:
            sell_quantity = self._round_lot(quantity * self.sell_holding_ratio)
            action_note = f"第{next_sell_count}次连续卖出，卖出持仓{self.sell_holding_ratio:.0%}"

        if sell_quantity <= 0:
            return None
        return OrderSignal(
            symbol=symbol,
            side="SELL",
            quantity=sell_quantity,
            limit_price=current_price,
            note=f"浮盈从最高点回撤达到{self.profit_drawdown_ratio:.0%}，{action_note}",
        )

    def _round_lot(self, quantity: float) -> int:
        return int(math.floor(quantity / self.lot_size) * self.lot_size)

    def _ceil_lot(self, quantity: float) -> int:
        return int(math.ceil(quantity / self.lot_size) * self.lot_size)

    def _target_position_quantity(
        self,
        *,
        cash: float,
        quantity: int,
        price: float,
        equity: float,
        target_ratio: float,
    ) -> int:
        current_value = quantity * price
        target_value = min(equity * target_ratio, equity * self.max_position_ratio)
        value_to_buy = target_value - current_value
        if value_to_buy <= 0 or cash <= 0 or price <= 0:
            return 0
        buy_quantity = self._ceil_lot(value_to_buy / price)
        max_cash_quantity = self._round_lot(cash / price)
        return min(buy_quantity, max_cash_quantity)

    @staticmethod
    def _latest_buy_price(position: dict) -> Optional[float]:
        latest_buy_price = position.get("latest_buy_price")
        if latest_buy_price is not None:
            return float(latest_buy_price)
        buy_prices = position.get("buy_prices") or []
        if buy_prices:
            return float(buy_prices[-1])
        return None

    @staticmethod
    def _ma(values: list[float], window: int) -> float:
        return sum(values[-window:]) / window

    @staticmethod
    def _rising_through_today(values: list[float]) -> bool:
        return len(values) >= 3 and values[-1] > values[-2] > values[-3]
