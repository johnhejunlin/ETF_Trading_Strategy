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
        self.rising_days = int(config["strategy"]["rising_days"])
        self.buy_cash_ratios = [float(ratio) for ratio in config["strategy"]["buy_cash_ratios"]]
        self.max_position_ratio = float(config["strategy"].get("max_position_ratio", 1.0))
        self.sell_holding_ratio = float(config["strategy"]["sell_holding_ratio"])
        self.profit_drawdown_ratio = float(config["strategy"]["profit_drawdown_ratio"])
        self.sell_below_ma_window = int(config["strategy"].get("sell_below_ma_window", 0))
        self.clear_position_on_sell_count = int(config["strategy"]["clear_position_on_sell_count"])
        self.lot_size = int(config["strategy"]["lot_size"])

    def generate(self, symbol: str, today: str) -> Optional[OrderSignal]:
        candles = self.market_data.daily_candles(symbol)
        current_price = candles[-1].close
        self.portfolio.update_max_profit(symbol, current_price)

        position = self.portfolio.position(symbol)
        quantity = int(position["quantity"])
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

        if self.portfolio.traded_today(symbol, today):
            logging.info("%s 今日已交易，跳过。", symbol)
            return None

        buy_count = int(position.get("buy_count") or 0)
        buy_prices = [float(price) for price in position.get("buy_prices", [])]
        if buy_count < len(self.buy_cash_ratios) and self._buy_condition(candles, buy_count, buy_prices):
            cash_ratio = self._buy_cash_ratio(buy_count)
            equity = self.portfolio.cash() + quantity * current_price
            position_room = max(0.0, equity * self.max_position_ratio - quantity * current_price)
            cash_to_use = min(self.portfolio.cash() * cash_ratio, position_room)
            buy_quantity = self._round_lot(cash_to_use / current_price)
            if buy_quantity > 0:
                condition_note = self._buy_condition_note(buy_count)
                return OrderSignal(
                    symbol=symbol,
                    side="BUY",
                    quantity=buy_quantity,
                    limit_price=current_price,
                    note=f"第{buy_count + 1}次买入，{condition_note}，使用可用资金{cash_ratio:.0%}",
                )

        logging.info("%s 未满足买入/卖出条件。", symbol)
        return None

    def _buy_condition(self, candles: list[Candle], buy_count: int, buy_prices: list[float]) -> bool:
        if buy_count >= len(self.buy_cash_ratios):
            return False

        closes = [c.close for c in candles]
        current_price = closes[-1]
        first_buy_rising = self._rising_through_today(closes, previous_days=2)
        ma5 = self._ma(closes, 5)
        ma10 = self._ma(closes, 10)
        ma20 = self._ma(closes, 20)
        ma60 = self._ma(closes, 60)
        first_buy_ma = ma5 > ma10 > ma20
        add_buy_ma = first_buy_ma and ma20 > ma60
        previous_buy_price = buy_prices[buy_count - 1] if buy_count > 0 and len(buy_prices) >= buy_count else None
        price_above_previous_buy = previous_buy_price is not None and current_price > previous_buy_price
        logging.info(
            "趋势检查: buy_count=%s first_buy_rising=%s previous_buy_price=%s price_above_previous_buy=%s MA5=%.4f MA10=%.4f MA20=%.4f MA60=%.4f",
            buy_count,
            first_buy_rising,
            f"{previous_buy_price:.4f}" if previous_buy_price is not None else "N/A",
            price_above_previous_buy,
            ma5,
            ma10,
            ma20,
            ma60,
        )
        if buy_count == 0:
            return first_buy_rising and first_buy_ma
        return price_above_previous_buy and add_buy_ma

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

    def _buy_cash_ratio(self, buy_count: int) -> float:
        if buy_count < len(self.buy_cash_ratios):
            return self.buy_cash_ratios[buy_count]
        return self.buy_cash_ratios[-1]

    def _buy_condition_note(self, buy_count: int) -> str:
        if buy_count == 0:
            return "前两天上涨且当天上涨且 MA5>MA10>MA20"
        return f"当天价格大于第{buy_count}次买入价格且 MA5>MA10>MA20>MA60"

    @staticmethod
    def _ma(values: list[float], window: int) -> float:
        return sum(values[-window:]) / window

    @staticmethod
    def _rising_through_today(values: list[float], previous_days: int) -> bool:
        comparisons = previous_days + 1
        return all(values[-idx] > values[-idx - 1] for idx in range(1, comparisons + 1))
