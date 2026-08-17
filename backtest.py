#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from market_data_store import MARKET_DATA_DB_PATH, init_market_data_db


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CACHE_DIR = ROOT / ".cache"
MINUTE_DB_PATH = MARKET_DATA_DB_PATH
DEFAULT_BACKTEST_START = "2025-01-01"
REPORT_PNG = ROOT / "backtest_588330.png"
REPORT_HTML = ROOT / "backtest_588330.html"


@dataclass
class Trade:
    trade_date: pd.Timestamp
    side: str
    price: float
    quantity: int
    gross_amount: float
    fee: float
    slippage_cost: float
    realized_pnl: float
    cash: float
    position: int
    equity: float
    note: str


@dataclass
class ExecutionPrice:
    price: float
    source: str
    timestamp: str | None = None


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def set_report_paths(symbol: str, start: str, end: str) -> None:
    global REPORT_PNG, REPORT_HTML
    label = f"{symbol}_{start.replace('-', '')}_{end.replace('-', '')}"
    REPORT_PNG = ROOT / f"backtest_{label}.png"
    REPORT_HTML = ROOT / f"backtest_{label}.html"


def fetch_tencent_daily(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    *,
    limit: int = 2000,
    refresh_cache: bool = False,
) -> pd.DataFrame:
    requested_start = pd.to_datetime(start or DEFAULT_BACKTEST_START).normalize() - pd.Timedelta(days=120)
    requested_end = pd.to_datetime(end or date.today().isoformat()).normalize() + pd.Timedelta(days=1)
    init_daily_db(MINUTE_DB_PATH)
    cached = read_daily_db(MINUTE_DB_PATH, symbol, requested_start, requested_end)
    latest_cached_date = latest_daily_date(MINUTE_DB_PATH, symbol)
    target_date = requested_end - pd.Timedelta(days=1)
    if cached is not None and not refresh_cache and latest_cached_date is not None and latest_cached_date >= target_date:
        annotate_daily_frame(cached)
        return cached

    market_symbol = f"sh{symbol}" if symbol.startswith(("5", "6", "9")) else f"sz{symbol}"
    params = {"param": f"{market_symbol},day,,,{limit},qfq"}
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        if cached is not None:
            annotate_daily_frame(cached)
            return cached
        raise

    node = (payload.get("data") or {}).get(market_symbol) or {}
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        if cached is not None:
            annotate_daily_frame(cached)
            return cached
        raise RuntimeError(f"未获取到 {symbol} 的日 K 数据。")

    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    for column in ["open", "close", "high", "low", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna().sort_values("date").reset_index(drop=True)
    upsert_daily_db(MINUTE_DB_PATH, symbol, df)
    result = read_daily_db(MINUTE_DB_PATH, symbol, requested_start, requested_end)
    if result is None or result.empty:
        raise RuntimeError(f"数据库中没有 {symbol} 的日 K 数据。")
    annotate_daily_frame(result)
    return result


def load_minute_prices(
    symbol: str,
    start: str,
    end: str,
    *,
    enabled: bool,
    refresh_cache: bool = False,
) -> pd.DataFrame | None:
    if not enabled:
        return None

    frequency = "1m"
    requested_start = pd.to_datetime(start).normalize()
    requested_end = pd.to_datetime(end).normalize() + pd.Timedelta(days=1)
    init_minute_db(MINUTE_DB_PATH)
    import_legacy_minute_cache(symbol, frequency)
    cached = read_minute_db(MINUTE_DB_PATH, symbol, frequency, requested_start, requested_end)
    latest_dt = latest_minute_datetime(MINUTE_DB_PATH, symbol, frequency)

    try:
        from mootdx.quotes import Quotes
    except ImportError as exc:
        if cached is not None:
            return cached
        raise RuntimeError("使用分时成交价需要安装 mootdx：python3 -m pip install mootdx") from exc

    try:
        client = Quotes.factory(market="std")
        page_size = 800
        max_pages = 180
        frames = []
        oldest_seen = None
        stop_at = None if latest_dt is None or refresh_cache else latest_dt
        for page in range(max_pages):
            offset = page * page_size
            minute = client.bars(symbol=symbol, frequency=7, start=offset, offset=page_size)
            if minute is None or minute.empty:
                break
            minute = normalize_minute_frame(minute)
            if minute.empty:
                break
            frames.append(minute)
            oldest = minute["datetime"].min()
            if oldest_seen is not None and oldest >= oldest_seen:
                break
            oldest_seen = oldest
            if stop_at is not None and oldest <= stop_at:
                break
            if stop_at is None and oldest <= requested_start:
                break
            time.sleep(0.05)
    except Exception:
        if cached is not None:
            return cached
        raise

    if frames:
        fetched = pd.concat(frames, ignore_index=True)
        if latest_dt is not None and not refresh_cache:
            fetched = fetched[fetched["datetime"] > latest_dt]
        if not fetched.empty:
            upsert_minute_db(MINUTE_DB_PATH, symbol, frequency, fetched)
    elif cached is None:
        raise RuntimeError(f"未获取到 {symbol} 的 1 分钟 K 数据。")

    result = read_minute_db(MINUTE_DB_PATH, symbol, frequency, requested_start, requested_end)
    if result is None or result.empty:
        if cached is not None:
            annotate_minute_frame(cached)
            return cached
        raise RuntimeError(f"数据库中没有 {symbol} 的 1 分钟 K 数据。")
    annotate_minute_frame(result)
    return result


def init_minute_db(db_path: Path) -> None:
    init_market_data_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS minute_bars (
                symbol TEXT NOT NULL,
                frequency TEXT NOT NULL,
                datetime TEXT NOT NULL,
                open REAL,
                close REAL NOT NULL,
                high REAL,
                low REAL,
                volume REAL,
                amount REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, frequency, datetime)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_minute_bars_lookup ON minute_bars(symbol, frequency, datetime)")


def init_daily_db(db_path: Path) -> None:
    init_market_data_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_bars (
                symbol TEXT NOT NULL,
                adjustment TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL NOT NULL,
                close REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                volume REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, adjustment, date)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_bars_lookup ON daily_bars(symbol, adjustment, date)")


def import_legacy_minute_cache(symbol: str, frequency: str) -> None:
    cache_path = CACHE_DIR / f"minute_{symbol}_1m.csv"
    if not cache_path.exists() or latest_minute_datetime(MINUTE_DB_PATH, symbol, frequency) is not None:
        return
    cached = pd.read_csv(cache_path)
    if cached.empty or "datetime" not in cached.columns:
        return
    cached = normalize_minute_frame(cached)
    if not cached.empty:
        upsert_minute_db(MINUTE_DB_PATH, symbol, frequency, cached)


def latest_minute_datetime(db_path: Path, symbol: str, frequency: str) -> pd.Timestamp | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(datetime) FROM minute_bars WHERE symbol = ? AND frequency = ?",
            (symbol, frequency),
        ).fetchone()
    if not row or not row[0]:
        return None
    return pd.to_datetime(row[0])


def latest_daily_date(db_path: Path, symbol: str, adjustment: str = "qfq") -> pd.Timestamp | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(date) FROM daily_bars WHERE symbol = ? AND adjustment = ?",
            (symbol, adjustment),
        ).fetchone()
    if not row or not row[0]:
        return None
    return pd.to_datetime(row[0])


def read_minute_db(
    db_path: Path,
    symbol: str,
    frequency: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame | None:
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(
            """
            SELECT datetime, open, close, high, low, volume, amount
            FROM minute_bars
            WHERE symbol = ?
              AND frequency = ?
              AND datetime >= ?
              AND datetime < ?
            ORDER BY datetime
            """,
            conn,
            params=(symbol, frequency, start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
        )
    if df.empty:
        return None
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df.dropna(subset=["datetime", "close"]).reset_index(drop=True)


def read_daily_db(
    db_path: Path,
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    adjustment: str = "qfq",
) -> pd.DataFrame | None:
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(
            """
            SELECT date, open, close, high, low, volume
            FROM daily_bars
            WHERE symbol = ?
              AND adjustment = ?
              AND date >= ?
              AND date < ?
            ORDER BY date
            """,
            conn,
            params=(symbol, adjustment, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
        )
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.dropna(subset=["date", "close"]).reset_index(drop=True)


def upsert_minute_db(db_path: Path, symbol: str, frequency: str, minute: pd.DataFrame) -> None:
    rows = []
    for row in minute.to_dict("records"):
        dt = pd.to_datetime(row["datetime"]).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(
            (
                symbol,
                frequency,
                dt,
                value_or_none(row.get("open")),
                value_or_none(row.get("close")),
                value_or_none(row.get("high")),
                value_or_none(row.get("low")),
                value_or_none(row.get("volume")),
                value_or_none(row.get("amount")),
            )
        )
    if not rows:
        return
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO minute_bars(symbol, frequency, datetime, open, close, high, low, volume, amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, frequency, datetime) DO UPDATE SET
                open = excluded.open,
                close = excluded.close,
                high = excluded.high,
                low = excluded.low,
                volume = excluded.volume,
                amount = excluded.amount,
                updated_at = CURRENT_TIMESTAMP
            """,
            rows,
        )


def upsert_daily_db(db_path: Path, symbol: str, daily: pd.DataFrame, adjustment: str = "qfq") -> None:
    rows = []
    for row in daily.to_dict("records"):
        dt = pd.to_datetime(row["date"]).strftime("%Y-%m-%d")
        rows.append(
            (
                symbol,
                adjustment,
                dt,
                value_or_none(row.get("open")),
                value_or_none(row.get("close")),
                value_or_none(row.get("high")),
                value_or_none(row.get("low")),
                value_or_none(row.get("volume")),
            )
        )
    if not rows:
        return
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO daily_bars(symbol, adjustment, date, open, close, high, low, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, adjustment, date) DO UPDATE SET
                open = excluded.open,
                close = excluded.close,
                high = excluded.high,
                low = excluded.low,
                volume = excluded.volume,
                updated_at = CURRENT_TIMESTAMP
            """,
            rows,
        )


def value_or_none(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def annotate_minute_frame(minute: pd.DataFrame) -> None:
    minute.attrs["minute_db_path"] = str(MINUTE_DB_PATH)
    minute.attrs["minute_rows"] = int(len(minute))
    minute.attrs["minute_start"] = minute["datetime"].min().strftime("%Y-%m-%d %H:%M") if not minute.empty else None
    minute.attrs["minute_end"] = minute["datetime"].max().strftime("%Y-%m-%d %H:%M") if not minute.empty else None


def annotate_daily_frame(daily: pd.DataFrame) -> None:
    daily.attrs["daily_db_path"] = str(MINUTE_DB_PATH)
    daily.attrs["daily_rows"] = int(len(daily))
    daily.attrs["daily_start"] = daily["date"].min().strftime("%Y-%m-%d") if not daily.empty else None
    daily.attrs["daily_end"] = daily["date"].max().strftime("%Y-%m-%d") if not daily.empty else None


def normalize_minute_frame(minute: pd.DataFrame) -> pd.DataFrame:
    result = minute.copy().reset_index(drop="datetime" in minute.columns)
    if "datetime" not in result.columns:
        result = result.reset_index()
    result["datetime"] = pd.to_datetime(result["datetime"], errors="coerce")
    if "volume" not in result.columns and "vol" in result.columns:
        result["volume"] = result["vol"]
    keep_columns = [column for column in ["datetime", "open", "close", "high", "low", "volume", "amount"] if column in result.columns]
    result = result[keep_columns]
    for column in ["open", "close", "high", "low", "volume", "amount"]:
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    return result.dropna(subset=["datetime", "close"]).sort_values("datetime").reset_index(drop=True)


def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    for window in [5, 10, 20, 60]:
        result[f"ma{window}"] = result["close"].rolling(window).mean()

    first_buy_rising = rising_through_today(result["close"])
    result["first_buy_rising"] = first_buy_rising
    result["first_buy_condition"] = (
        result["first_buy_rising"]
        & (result["ma5"] > result["ma10"])
        & (result["ma10"] > result["ma20"])
    )
    result["add_buy_ma_condition"] = result["ma5"] > result["ma10"]
    result["add_buy_ma_condition"] = result["add_buy_ma_condition"] & (result["ma10"] > result["ma20"])
    result["add_buy_ma_condition"] = result["add_buy_ma_condition"] & (result["ma20"] > result["ma60"])
    return result


def rising_through_today(close: pd.Series) -> pd.Series:
    return (close > close.shift(1)) & (close.shift(1) > close.shift(2))


def run_backtest(
    df: pd.DataFrame,
    config: dict,
    start: str,
    end: str,
    *,
    minute_prices: pd.DataFrame | None = None,
    execution_price_mode: str = "intraday",
    execution_time: str = "15:00",
) -> tuple[pd.DataFrame, list[Trade], dict]:
    strategy = config["strategy"]
    backtest_config = config.get("backtest", {})
    cash = float(config["portfolio"]["initial_cash"])
    initial_cash = cash
    position = 0
    avg_cost = 0.0
    max_profit_pct = 0.0
    sell_streak = 0
    buy_count = 0
    buy_prices: list[float] = []
    latest_buy_price = None
    lot_size = int(strategy["lot_size"])
    buy_position_targets = [float(ratio) for ratio in strategy.get("buy_position_targets", [0.5, 0.85, 1.0])]
    max_position_ratio = float(strategy.get("max_position_ratio", 1.0))
    sell_holding_ratio = float(strategy["sell_holding_ratio"])
    stop_loss_ratio = float(strategy.get("stop_loss_ratio", 0.03))
    profit_drawdown_ratio = float(strategy["profit_drawdown_ratio"])
    sell_below_ma_window = int(strategy.get("sell_below_ma_window", 0))
    clear_position_on_sell_count = int(strategy["clear_position_on_sell_count"])
    commission_rate = float(backtest_config.get("commission_rate", 0.0))
    min_commission = float(backtest_config.get("min_commission", 0.0))
    stamp_tax_rate = float(backtest_config.get("stamp_tax_rate", 0.0))
    slippage_rate = float(backtest_config.get("slippage_bps", 0.0)) / 10000
    limit_threshold_pct = float(backtest_config.get("limit_threshold_pct", 0.195))

    mask = (df["date"] >= pd.to_datetime(start)) & (df["date"] <= pd.to_datetime(end))
    bt = df.loc[mask].copy().reset_index(drop=True)
    actual_start = bt.iloc[0]["date"].strftime("%Y-%m-%d") if len(bt) else start
    actual_end = bt.iloc[-1]["date"].strftime("%Y-%m-%d") if len(bt) else end
    trades: list[Trade] = []
    realized_pnls: list[float] = []
    total_fees = 0.0
    total_slippage_cost = 0.0
    total_turnover = 0.0
    equity_values = []
    cash_values = []
    position_values = []
    avg_cost_values = []
    current_build_profit_values = []
    execution_source_counts: dict[str, int] = {}
    current_build_realized_pnl = 0.0

    for idx, row in bt.iterrows():
        price = float(row["close"])
        execution_price_base = resolve_execution_price(row, minute_prices, execution_price_mode, execution_time)
        traded_today = False

        if position > 0 and avg_cost > 0:
            current_profit_pct = (price - avg_cost) / avg_cost
            max_profit_pct = max(max_profit_pct, current_profit_pct)
            ma_stop_value = row.get(f"ma{sell_below_ma_window}") if sell_below_ma_window else None
            sell_quantity = 0
            sell_note = ""
            if current_profit_pct <= -stop_loss_ratio:
                sell_quantity = position
                sell_note = f"stop_loss={stop_loss_ratio:.0%}, clear_position"
            elif sell_below_ma_window and pd.notna(ma_stop_value) and price < float(ma_stop_value):
                sell_quantity = position
                sell_note = f"close_below_MA{sell_below_ma_window}, clear_position"
            else:
                trigger_profit_pct = max_profit_pct * (1 - profit_drawdown_ratio)
                if max_profit_pct > 0 and current_profit_pct <= trigger_profit_pct:
                    next_sell_count = sell_streak + 1
                    if next_sell_count >= clear_position_on_sell_count:
                        sell_quantity = position
                        sell_note = f"sell_count={next_sell_count}, clear_position"
                    else:
                        sell_quantity = int(math.floor((position * sell_holding_ratio) / lot_size) * lot_size)
                        sell_note = f"sell_count={next_sell_count}, sell_half"

            if sell_quantity > 0:
                next_sell_count = sell_streak + 1
                execution_price = execution_price_base.price * (1 - slippage_rate)
                gross_amount = sell_quantity * execution_price
                fee = trade_fee(gross_amount, commission_rate, min_commission, stamp_tax_rate)
                slippage_cost = sell_quantity * (execution_price_base.price - execution_price)
                realized_pnl = sell_quantity * (execution_price - avg_cost) - fee
                cash += gross_amount - fee
                position -= sell_quantity
                current_build_realized_pnl += realized_pnl
                sell_streak = next_sell_count
                traded_today = True
                equity = cash + position * price
                realized_pnls.append(realized_pnl)
                total_fees += fee
                total_slippage_cost += slippage_cost
                total_turnover += gross_amount
                execution_source_counts[execution_price_base.source] = execution_source_counts.get(execution_price_base.source, 0) + 1
                trades.append(
                    Trade(
                        row["date"],
                        "SELL",
                        execution_price,
                        sell_quantity,
                        gross_amount,
                        fee,
                        slippage_cost,
                        realized_pnl,
                        cash,
                        position,
                        equity,
                        f"{sell_note}, close={price:.4f}, execution_base={execution_price_base.price:.4f}"
                        f"({execution_price_base.source}{' ' + execution_price_base.timestamp if execution_price_base.timestamp else ''}), "
                        f"max_profit={max_profit_pct:.2%}, current_profit={current_profit_pct:.2%}",
                    )
                )
                if position == 0:
                    avg_cost = 0.0
                    max_profit_pct = 0.0
                    sell_streak = 0
                    buy_count = 0
                    buy_prices = []
                    latest_buy_price = None
                    current_build_realized_pnl = 0.0
                else:
                    max_profit_pct = current_profit_pct
        buy_target = buy_target_for_position(row, cash, position, price, latest_buy_price, buy_position_targets)
        if not traded_today and buy_target:
            target_ratio, condition_note = buy_target
            equity_before_buy = cash + position * price
            current_position_value = position * price
            buy_quantity = target_lot_quantity(
                cash,
                current_position_value,
                equity_before_buy,
                min(target_ratio, max_position_ratio),
                execution_price_base.price,
                slippage_rate,
                lot_size,
                commission_rate,
                min_commission,
            )
            if buy_quantity > 0:
                execution_price = execution_price_base.price * (1 + slippage_rate)
                gross_amount = buy_quantity * execution_price
                fee = trade_fee(gross_amount, commission_rate, min_commission, 0.0)
                slippage_cost = buy_quantity * (execution_price - execution_price_base.price)
                cost = gross_amount + fee
                old_position = position
                old_cost = old_position * avg_cost
                if old_position == 0:
                    current_build_realized_pnl = 0.0
                cash -= cost
                position += buy_quantity
                avg_cost = (old_cost + cost) / position
                max_profit_pct = 0.0
                sell_streak = 0
                buy_count += 1
                buy_prices.append(execution_price)
                latest_buy_price = execution_price
                equity = cash + position * price
                total_fees += fee
                total_slippage_cost += slippage_cost
                total_turnover += gross_amount
                execution_source_counts[execution_price_base.source] = execution_source_counts.get(execution_price_base.source, 0) + 1
                trades.append(
                    Trade(
                        row["date"],
                        "BUY",
                        execution_price,
                        buy_quantity,
                        gross_amount,
                        fee,
                        slippage_cost,
                        0.0,
                        cash,
                        position,
                        equity,
                        f"buy_count={buy_count}, target_position={target_ratio:.0%}, close={price:.4f}, "
                        f"execution_base={execution_price_base.price:.4f}"
                        f"({execution_price_base.source}{' ' + execution_price_base.timestamp if execution_price_base.timestamp else ''}), "
                        f"{condition_note}",
                    )
                )

        equity_values.append(cash + position * price)
        cash_values.append(cash)
        position_values.append(position)
        avg_cost_values.append(avg_cost)
        current_build_profit = current_build_realized_pnl + position * (price - avg_cost) if position > 0 else 0.0
        current_build_profit_values.append(current_build_profit)

    bt["equity"] = equity_values
    bt["cash"] = cash_values
    bt["position"] = position_values
    bt["avg_cost"] = avg_cost_values
    bt["current_build_profit"] = current_build_profit_values
    final_equity = float(bt.iloc[-1]["equity"]) if len(bt) else initial_cash
    winning_trades = [pnl for pnl in realized_pnls if pnl > 0]
    losing_trades = [pnl for pnl in realized_pnls if pnl < 0]
    gross_profit = sum(winning_trades)
    gross_loss = abs(sum(losing_trades))
    average_equity = float(bt["equity"].mean()) if len(bt) else initial_cash
    drawdown_info = max_drawdown_detail(bt)
    limit_info = limit_and_suspension_info(bt, limit_threshold_pct)
    stats = {
        "initial_cash": initial_cash,
        "requested_start": start,
        "requested_end": end,
        "actual_start": actual_start,
        "actual_end": actual_end,
        "final_equity": final_equity,
        "profit": final_equity - initial_cash,
        "profit_pct": (final_equity / initial_cash - 1) if initial_cash else 0.0,
        "trade_count": len(trades),
        "buy_count": sum(1 for trade in trades if trade.side == "BUY"),
        "sell_count": sum(1 for trade in trades if trade.side == "SELL"),
        "max_drawdown": drawdown_info["max_drawdown"],
        **drawdown_info,
        "win_rate": (len(winning_trades) / len(realized_pnls)) if realized_pnls else 0.0,
        "profit_loss_ratio": (gross_profit / gross_loss) if gross_loss else None,
        "realized_trade_count": len(realized_pnls),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "turnover": (total_turnover / average_equity) if average_equity else 0.0,
        "total_turnover_amount": total_turnover,
        "total_fees": total_fees,
        "total_slippage_cost": total_slippage_cost,
        "commission_rate": commission_rate,
        "min_commission": min_commission,
        "stamp_tax_rate": stamp_tax_rate,
        "slippage_bps": slippage_rate * 10000,
        "limit_threshold_pct": limit_threshold_pct,
        "max_position_ratio": max_position_ratio,
        "stop_loss_ratio": stop_loss_ratio,
        "sell_below_ma_window": sell_below_ma_window,
        "execution_price_mode": execution_price_mode,
        "execution_time": execution_time,
        "execution_source_counts": execution_source_counts,
        "daily_db_path": df.attrs.get("daily_db_path"),
        "daily_rows": df.attrs.get("daily_rows", 0),
        "daily_start": df.attrs.get("daily_start"),
        "daily_end": df.attrs.get("daily_end"),
        "minute_db_path": minute_prices.attrs.get("minute_db_path") if minute_prices is not None else None,
        "minute_rows": minute_prices.attrs.get("minute_rows") if minute_prices is not None else 0,
        "minute_start": minute_prices.attrs.get("minute_start") if minute_prices is not None else None,
        "minute_end": minute_prices.attrs.get("minute_end") if minute_prices is not None else None,
        **limit_info,
        "future_function_check": "风险提示：当前策略仍用当日收盘价计算信号；即使成交价改为分时价，若实盘只能收盘后确认信号，仍应改为次日开盘或次日分时成交回测。",
        "survivorship_bias_check": "单标的回测无股票池筛选幸存者偏差；但数据源只覆盖当前可查询标的。若扩展为多股票策略，需要纳入退市/停牌/历史成分股数据。",
    }
    return bt, trades, stats


def resolve_execution_price(
    row: pd.Series,
    minute_prices: pd.DataFrame | None,
    mode: str,
    execution_time: str,
) -> ExecutionPrice:
    close_price = float(row["close"])
    if mode != "intraday" or minute_prices is None or minute_prices.empty:
        return ExecutionPrice(close_price, "daily_close")

    trade_date = row["date"].strftime("%Y-%m-%d")
    target = pd.to_datetime(f"{trade_date} {execution_time}")
    day_start = pd.to_datetime(trade_date)
    day_end = day_start + pd.Timedelta(days=1)
    same_day = minute_prices[(minute_prices["datetime"] >= day_start) & (minute_prices["datetime"] < day_end)]
    if same_day.empty:
        return ExecutionPrice(close_price, "daily_close_fallback")

    at_or_after = same_day[same_day["datetime"] >= target]
    selected = at_or_after.iloc[0] if not at_or_after.empty else same_day.iloc[-1]
    timestamp = selected["datetime"].strftime("%H:%M")
    return ExecutionPrice(float(selected["close"]), "mootdx_1m", timestamp)


def max_drawdown_detail(bt: pd.DataFrame) -> dict:
    if bt.empty:
        return {
            "max_drawdown": 0.0,
            "drawdown_peak_date": None,
            "drawdown_trough_date": None,
            "drawdown_peak_equity": None,
            "drawdown_trough_equity": None,
        }

    peak = bt["equity"].cummax()
    drawdown = bt["equity"] / peak - 1
    trough_idx = drawdown.idxmin()
    peak_idx = bt.loc[:trough_idx, "equity"].idxmax()
    return {
        "max_drawdown": float(drawdown.loc[trough_idx]),
        "drawdown_peak_date": bt.loc[peak_idx, "date"].strftime("%Y-%m-%d"),
        "drawdown_trough_date": bt.loc[trough_idx, "date"].strftime("%Y-%m-%d"),
        "drawdown_peak_equity": float(bt.loc[peak_idx, "equity"]),
        "drawdown_trough_equity": float(bt.loc[trough_idx, "equity"]),
    }


def buy_target_for_position(
    row: pd.Series,
    cash: float,
    position: int,
    price: float,
    latest_buy_price: float | None,
    targets: list[float],
) -> tuple[float, str] | None:
    first_target, second_target, final_target = targets
    equity = cash + position * price
    position_ratio = (position * price / equity) if equity > 0 else 0.0
    if position == 0:
        if bool(row["first_buy_condition"]):
            return first_target, "empty position, previous 2 days rising plus today rising and MA5>MA10>MA20"
        return None

    price_above_latest_buy = latest_buy_price is not None and price > latest_buy_price
    add_buy_condition = bool(row["add_buy_ma_condition"]) and price_above_latest_buy
    if position_ratio >= second_target:
        if position_ratio < final_target and add_buy_condition:
            return final_target, "position>=85%, close above latest buy price and MA5>MA10>MA20>MA60"
        return None
    if position_ratio >= first_target:
        if add_buy_condition:
            return second_target, "position>=50%, close above latest buy price and MA5>MA10>MA20>MA60"
        return None
    return None


def trade_fee(amount: float, commission_rate: float, min_commission: float, extra_rate: float) -> float:
    if amount <= 0:
        return 0.0
    commission = max(amount * commission_rate, min_commission) if commission_rate > 0 else 0.0
    return commission + amount * extra_rate


def affordable_lot_quantity(
    cash_available: float,
    close_price: float,
    slippage_rate: float,
    lot_size: int,
    commission_rate: float,
    min_commission: float,
) -> int:
    execution_price = close_price * (1 + slippage_rate)
    quantity = int(math.floor((cash_available / execution_price) / lot_size) * lot_size)
    while quantity > 0:
        gross_amount = quantity * execution_price
        fee = trade_fee(gross_amount, commission_rate, min_commission, 0.0)
        if gross_amount + fee <= cash_available:
            return quantity
        quantity -= lot_size
    return 0


def target_lot_quantity(
    cash_available: float,
    current_position_value: float,
    equity: float,
    target_ratio: float,
    close_price: float,
    slippage_rate: float,
    lot_size: int,
    commission_rate: float,
    min_commission: float,
) -> int:
    execution_price = close_price * (1 + slippage_rate)
    value_to_buy = equity * target_ratio - current_position_value
    if value_to_buy <= 0 or cash_available <= 0 or execution_price <= 0:
        return 0
    quantity = int(math.ceil((value_to_buy / execution_price) / lot_size) * lot_size)
    while quantity > 0:
        gross_amount = quantity * execution_price
        fee = trade_fee(gross_amount, commission_rate, min_commission, 0.0)
        if gross_amount + fee <= cash_available:
            return quantity
        quantity -= lot_size
    return 0


def limit_and_suspension_info(bt: pd.DataFrame, limit_threshold_pct: float) -> dict:
    if bt.empty:
        return {
            "suspended_days": 0,
            "limit_up_days": 0,
            "limit_down_days": 0,
            "limit_note": "无回测数据，无法检查停牌/涨跌停。",
        }

    pct_change = bt["close"].pct_change()
    volume_zero = bt["volume"].fillna(0) <= 0
    limit_up = pct_change >= limit_threshold_pct
    limit_down = pct_change <= -limit_threshold_pct
    return {
        "suspended_days": int(volume_zero.sum()),
        "limit_up_days": int(limit_up.sum()),
        "limit_down_days": int(limit_down.sum()),
        "limit_note": "停牌以成交量为 0 的已返回交易日近似识别；涨跌停以收盘涨跌幅阈值近似识别，未使用逐笔盘口，可能低估盘中不可成交情况。",
    }


def plot_report(symbol: str, bt: pd.DataFrame, trades: list[Trade], stats: dict, start: str, end: str) -> None:
    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "Heiti TC", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, (ax_price, ax_profit) = plt.subplots(
        2,
        1,
        figsize=(14, 9),
        sharex=True,
        gridspec_kw={"height_ratios": [2, 1]},
    )

    ax_price.plot(bt["date"], bt["close"], label="Close", color="#1f2937", linewidth=1.6)
    ax_price.plot(bt["date"], bt["ma5"], label="MA5", color="#2563eb", linewidth=1)
    ax_price.plot(bt["date"], bt["ma10"], label="MA10", color="#16a34a", linewidth=1)
    ax_price.plot(bt["date"], bt["ma20"], label="MA20", color="#f59e0b", linewidth=1)
    ax_price.plot(bt["date"], bt["ma60"], label="MA60", color="#dc2626", linewidth=1)

    for trade in trades:
        marker = "^" if trade.side == "BUY" else "v"
        color = "#dc2626" if trade.side == "BUY" else "#16a34a"
        label = "Buy" if trade.side == "BUY" else "Sell"
        ax_price.scatter(trade.trade_date, trade.price, marker=marker, s=100, color=color, edgecolor="white", zorder=5)
        ax_price.annotate(
            f"{label}\n{trade.quantity}@{trade.price:.3f}",
            (trade.trade_date, trade.price),
            xytext=(0, 22 if trade.side == "BUY" else -34),
            textcoords="offset points",
            ha="center",
            fontsize=8,
            color=color,
            arrowprops={"arrowstyle": "-", "color": color, "linewidth": 0.8},
        )

    summary = (
        f"Symbol: {symbol} | Period: {start} to {end}\n"
        f"Initial: {stats['initial_cash']:,.2f}  Final: {stats['final_equity']:,.2f}  "
        f"Profit: {stats['profit']:,.2f} ({stats['profit_pct']:.2%})\n"
        f"Trades: {stats['trade_count']}  Buy: {stats['buy_count']}  Sell: {stats['sell_count']}  "
        f"Max drawdown: {stats['max_drawdown']:.2%}"
    )
    ax_price.set_title("588330 Trend Pullback Strategy Backtest", fontsize=15, loc="left")
    ax_price.text(0.01, 0.98, summary, transform=ax_price.transAxes, va="top", ha="left", fontsize=10,
                  bbox={"facecolor": "white", "edgecolor": "#d1d5db", "alpha": 0.9, "boxstyle": "round,pad=0.45"})
    ax_price.set_ylabel("Price")
    ax_price.legend(loc="upper right", ncols=5, fontsize=9)
    ax_price.grid(True, alpha=0.25)

    profit = bt["equity"] - stats["initial_cash"]
    position_ratio = position_ratio_series(bt)
    ax_profit.plot(bt["date"], profit, label="Profit", color="#2563eb", linewidth=1.8)
    ax_profit.axhline(0, color="#6b7280", linestyle="--", linewidth=1, label="Zero profit")
    ax_profit.fill_between(bt["date"], 0, profit, color="#2563eb", alpha=0.12)
    ax_profit.set_ylabel("Profit")
    ax_profit.set_xlabel("Date")
    ax_profit.grid(True, alpha=0.25)
    ax_position = ax_profit.twinx()
    ax_position.plot(bt["date"], position_ratio, label="Position", color="#f59e0b", linestyle=":", linewidth=1.5)
    ax_position.set_ylabel("Position")
    lines, labels = ax_profit.get_legend_handles_labels()
    pos_lines, pos_labels = ax_position.get_legend_handles_labels()
    ax_profit.legend(lines + pos_lines, labels + pos_labels, loc="upper left")

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(REPORT_PNG, dpi=180)
    plt.close(fig)


def save_html_report(symbol: str, bt: pd.DataFrame, trades: list[Trade], stats: dict, start: str, end: str) -> None:
    chart_bt = bt.copy()
    chart_bt["date_label"] = chart_bt["date"].dt.strftime("%Y-%m-%d")
    chart_bt["profit"] = chart_bt["equity"] - float(stats["initial_cash"])
    chart_bt["position_ratio"] = position_ratio_series(chart_bt)
    profit_range = padded_range([float(value) for value in chart_bt["profit"]] + [0.0])
    position_range = [0, max(1.0, float(chart_bt["position_ratio"].max()) * 1.12 if len(chart_bt) else 1.0)]
    price_range = padded_range(
        [float(value) for column in ["high", "low", "ma5", "ma10", "ma20", "ma60"] for value in chart_bt[column].dropna()]
    )
    marker_gap = (price_range[1] - price_range[0]) * 0.06 if len(price_range) == 2 else 0.03
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    trade_rows = []
    for trade in trades:
        trade_rows.append({
            "date": trade.trade_date.strftime("%Y-%m-%d"),
            "side": trade.side,
            "price": round(trade.price, 4),
            "quantity": trade.quantity,
            "gross_amount": round(trade.gross_amount, 2),
            "fee": round(trade.fee, 2),
            "slippage_cost": round(trade.slippage_cost, 2),
            "realized_pnl": round(trade.realized_pnl, 2),
            "cash": round(trade.cash, 2),
            "position": trade.position,
            "equity": round(trade.equity, 2),
            "note": compact_trade_note(trade),
        })
    trade_rows_by_date: dict[str, list[dict]] = {}
    for row in trade_rows:
        trade_rows_by_date.setdefault(row["date"], []).append(row)

    chart_data = build_chart_data(chart_bt, trade_rows_by_date)
    chart_data_panel = render_chart_data_panel(chart_data[-1] if chart_data else {})

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.68, 0.32],
        specs=[[{}], [{"secondary_y": True}]],
    )
    fig.add_trace(
        go.Candlestick(
            x=chart_bt["date_label"],
            open=chart_bt["open"],
            high=chart_bt["high"],
            low=chart_bt["low"],
            close=chart_bt["close"],
            name="K线",
            increasing={"line": {"color": "#dc2626"}, "fillcolor": "#dc2626"},
            decreasing={"line": {"color": "#16a34a"}, "fillcolor": "#16a34a"},
            hoverinfo="skip",
            hovertemplate=None,
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=chart_bt["date_label"],
            y=chart_bt["close"],
            mode="lines",
            name="光标定位",
            line={"color": "rgba(0,0,0,0)", "width": 8},
            opacity=0,
            showlegend=False,
            hovertemplate="%{x}<extra></extra>",
        ),
        row=1,
        col=1,
    )
    add_drawdown_annotations(fig, stats, chart_bt)
    line_styles = [
        ("ma5", "MA5", "#2563eb", 1.5),
        ("ma10", "MA10", "#f59e0b", 1.5),
        ("ma20", "MA20", "#7c3aed", 1.5),
        ("ma60", "MA60", "#475467", 1.5),
    ]
    for column, name, color, width in line_styles:
        fig.add_trace(
            go.Scatter(
                x=chart_bt["date_label"],
                y=chart_bt[column],
                mode="lines",
                name=name,
                line={"color": color, "width": width},
                hoverinfo="skip",
                hovertemplate=None,
            ),
            row=1,
            col=1,
        )

    for side, color, symbol_name in [("BUY", "#dc2626", "triangle-up"), ("SELL", "#16a34a", "triangle-down")]:
        side_trades = [trade for trade in trades if trade.side == side]
        marker_prices = offset_trade_marker_prices(side_trades, chart_bt, side, marker_gap)
        fig.add_trace(
            go.Scatter(
                x=[trade.trade_date.strftime("%Y-%m-%d") for trade in side_trades],
                y=marker_prices,
                mode="markers",
                name="买入" if side == "BUY" else "卖出",
                marker={"symbol": symbol_name, "size": 15, "color": color, "line": {"color": "white", "width": 1.4}},
                hoverinfo="skip",
                hovertemplate=None,
            ),
            row=1,
            col=1,
        )

    fig.add_trace(
        go.Scatter(
            x=chart_bt["date_label"],
            y=chart_bt["profit"],
            mode="lines",
            name="利润",
            line={"color": "#2563eb", "width": 2.4},
            hovertemplate="%{x}<br>利润: %{y:,.2f}<extra></extra>",
        ),
        row=2,
        col=1,
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=chart_bt["date_label"],
            y=chart_bt["position_ratio"],
            mode="lines",
            name="仓位",
            line={"color": "#f59e0b", "width": 1.8, "dash": "dot"},
            fill="tozeroy",
            fillcolor="rgba(245, 158, 11, 0.12)",
            hovertemplate="%{x}<br>仓位: %{y:.2%}<extra></extra>",
        ),
        row=2,
        col=1,
        secondary_y=True,
    )
    fig.add_hline(
        y=0,
        line_dash="dash",
        line_color="#667085",
        annotation_text="零利润",
        annotation_position="bottom right",
        row=2,
        col=1,
        secondary_y=False,
    )
    if stats["drawdown_peak_date"] and stats["drawdown_trough_date"]:
        peak_profit = float(stats["drawdown_peak_equity"] - stats["initial_cash"])
        trough_profit = float(stats["drawdown_trough_equity"] - stats["initial_cash"])
        fig.add_trace(
            go.Scatter(
                x=[stats["drawdown_peak_date"], stats["drawdown_trough_date"]],
                y=[peak_profit, trough_profit],
                mode="lines+markers+text",
                name="最大回撤",
                line={"color": "#ef4444", "width": 2, "dash": "dash"},
                marker={"size": 9, "color": ["#dc2626", "#16a34a"], "line": {"color": "white", "width": 1}},
                text=["峰值", "谷底"],
                textposition=["top center", "bottom center"],
                hovertemplate="%{x}<br>利润: %{y:,.2f}<extra>最大回撤</extra>",
            ),
            row=2,
            col=1,
            secondary_y=False,
        )
    fig.update_layout(
        height=760,
        margin={"l": 52, "r": 28, "t": 28, "b": 30},
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1},
        template="plotly_white",
    )
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="利润", range=profit_range, row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="仓位", range=position_range, tickformat=".0%", row=2, col=1, secondary_y=True)
    fig.update_xaxes(type="category", rangeslider_visible=False)
    fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor", spikecolor="#98a2b3", spikethickness=1)
    fig.update_xaxes(showticklabels=False, title_text=None, row=2, col=1)
    chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn", config={"responsive": True, "displaylogo": False})
    chart_script = render_chart_data_script(chart_data)
    metrics_html = render_metrics(stats, start, end)
    diagnostics_html = render_diagnostics(stats)
    trade_table = render_trade_table(trade_rows)

    content = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(symbol)} 回测报告</title>
  <style>
    :root {{
      color-scheme: light;
      --text: #172033;
      --muted: #667085;
      --line: #d8dee9;
      --buy: #dc2626;
      --sell: #16a34a;
      --panel: #ffffff;
      --bg: #f6f8fb;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    main {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1 {{
      margin: 0 0 16px;
      font-size: 24px;
      letter-spacing: 0;
    }}
    .report-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .report-header h1 {{
      margin: 0;
    }}
    .generated-at {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
      text-align: right;
      white-space: nowrap;
      padding-top: 7px;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }}
    .metric strong {{
      display: block;
      font-size: clamp(17px, 1.45vw, 22px);
      line-height: 1.22;
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: normal;
    }}
    .metric strong.long {{
      font-size: clamp(15px, 1.2vw, 19px);
    }}
    .chart-panel, .table-panel, .diagnostics {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-top: 14px;
    }}
    .chart-panel .plotly-graph-div:focus {{
      outline: 2px solid #98a2b3;
      outline-offset: 2px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid #edf0f5;
      padding: 8px 10px;
      text-align: right;
      white-space: nowrap;
    }}
    th:first-child, td:first-child, th:last-child, td:last-child {{ text-align: left; }}
    th {{ color: var(--muted); font-weight: 600; }}
    .buy {{ color: var(--buy); font-weight: 700; }}
    .sell {{ color: var(--sell); font-weight: 700; }}
    .hint {{ color: var(--muted); font-size: 13px; margin-top: 8px; }}
    .chart-data {{
      border-bottom: 1px solid #edf0f5;
      border-radius: 8px;
      margin-bottom: 10px;
      padding-bottom: 12px;
      background: transparent;
    }}
    .chart-data h2 {{
      font-size: 16px;
      margin: 0 0 10px;
      color: var(--text);
    }}
    .chart-data-grid {{
      display: grid;
      grid-template-columns: minmax(120px, 0.85fr) minmax(300px, 1.65fr) repeat(5, minmax(118px, 1fr));
      gap: 8px;
    }}
    .chart-data-item {{
      border: 1px solid #edf0f5;
      border-radius: 8px;
      padding: 8px 10px;
      background: #fbfcfe;
      min-height: 58px;
    }}
    .chart-data-item span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 2px;
    }}
    .chart-data-item strong {{
      display: block;
      font-size: 14px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .chart-data-item.ma-item strong {{
      white-space: nowrap;
      overflow-wrap: normal;
      word-break: keep-all;
      font-size: clamp(13px, 1.05vw, 15px);
    }}
    .ma-gt {{ color: #dc2626; font-weight: 800; }}
    .ma-lt {{ color: #16a34a; font-weight: 800; }}
    .ma-eq {{ color: #172033; font-weight: 800; }}
    .trade-summary {{
      border-top: 1px solid #edf0f5;
      margin-top: 8px;
      padding-top: 12px;
    }}
    .trade-summary h2 {{
      font-size: 16px;
      margin: 0 0 10px;
    }}
    .trade-list {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
      gap: 8px;
      max-height: 220px;
      overflow: auto;
    }}
    .trade-item {{
      border: 1px solid #edf0f5;
      border-radius: 8px;
      padding: 8px 10px;
      line-height: 1.45;
      font-size: 13px;
      background: #fbfcfe;
    }}
    .trade-item strong {{
      display: block;
      margin-bottom: 2px;
      font-size: 13px;
    }}
    .trade-item span {{
      color: var(--muted);
    }}
    .diagnostics h2 {{
      font-size: 18px;
      margin: 0 0 10px;
    }}
    .diagnostics ul {{
      margin: 0;
      padding-left: 18px;
      color: var(--text);
      line-height: 1.7;
      font-size: 14px;
    }}
    @media (max-width: 860px) {{
      main {{ padding: 14px; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .report-header {{ display: block; }}
      .generated-at {{ text-align: left; padding-top: 0; margin-top: 6px; white-space: normal; }}
      .chart-data-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .chart-data-item.ma-item {{ grid-column: 1 / -1; }}
      .chart-data-item.ma-item strong {{ white-space: normal; }}
      .table-panel {{ overflow-x: auto; }}
    }}
  </style>
</head>
<body>
  <main>
    <header class="report-header">
      <h1>{html.escape(symbol)} 趋势回撤策略回测</h1>
      <div class="generated-at">本次回测：{html.escape(generated_at)}</div>
    </header>
    <section class="metrics">
      {metrics_html}
    </section>
    <section class="chart-panel">
      {chart_data_panel}
      {chart_html}
      {chart_script}
      <div class="hint">图表显示所有开市交易日，隐藏周末和节假日等非交易日；悬停时上方数据栏同步更新。</div>
    </section>
    <section class="diagnostics">
      <h2>回测质量检查</h2>
      {diagnostics_html}
    </section>
    <section class="table-panel">
      <h2 style="font-size:18px;margin:0 0 10px;">交易明细</h2>
      {trade_table}
    </section>
  </main>
</body>
</html>
"""
    REPORT_HTML.write_text(content, encoding="utf-8")


def padded_range(values: list[float]) -> list[float]:
    clean_values = [value for value in values if math.isfinite(value)]
    if not clean_values:
        return [0, 1]
    low = min(clean_values)
    high = max(clean_values)
    if low == high:
        pad = max(abs(low) * 0.01, 1)
    else:
        pad = (high - low) * 0.12
    return [low - pad, high + pad]


def add_drawdown_annotations(fig: go.Figure, stats: dict, chart_bt: pd.DataFrame) -> None:
    peak_date = stats.get("drawdown_peak_date")
    trough_date = stats.get("drawdown_trough_date")
    if not peak_date or not trough_date:
        return

    price_values = []
    for column in ["open", "high", "low", "close", "ma5", "ma10", "ma20", "ma60"]:
        price_values.extend(float(value) for value in chart_bt[column].dropna())
    price_low, price_high = padded_range(price_values)

    fig.add_vrect(
        x0=peak_date,
        x1=trough_date,
        fillcolor="rgba(239, 68, 68, 0.14)",
        line_width=0,
        layer="below",
        annotation_text=f"最大回撤 {stats['max_drawdown']:.2%}",
        annotation_position="top left",
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=[peak_date, trough_date],
            y=[price_high, price_high],
            mode="markers+text",
            name="回撤区间",
            marker={"size": 8, "color": ["#dc2626", "#16a34a"], "line": {"color": "white", "width": 1}},
            text=["回撤峰值", "回撤谷底"],
            textposition=["top center", "top center"],
            hovertemplate="%{x}<br>%{text}<extra>最大回撤区间</extra>",
        ),
        row=1,
        col=1,
    )


def render_metrics(stats: dict, start: str, end: str) -> str:
    profit_loss_ratio = stats["profit_loss_ratio"]
    cards = [
        ("区间", f"{start} 至 {end}"),
        ("初始资金", f"{stats['initial_cash']:,.2f}"),
        ("期末权益", f"{stats['final_equity']:,.2f}"),
        ("总利润", f"{stats['profit']:,.2f}"),
        ("收益率", f"{stats['profit_pct']:.2%}"),
        ("最大回撤", f"{stats['max_drawdown']:.2%}"),
        ("回撤区间", f"{stats['drawdown_peak_date']} 至 {stats['drawdown_trough_date']}"),
        ("胜率", f"{stats['win_rate']:.2%}"),
        ("盈亏比", f"{profit_loss_ratio:.2f}" if profit_loss_ratio is not None else "无亏损"),
        ("换手率", f"{stats['turnover']:.2f}x"),
        ("交易次数", f"{stats['trade_count']}"),
        ("手续费", f"{stats['total_fees']:,.2f}"),
        ("滑点成本", f"{stats['total_slippage_cost']:,.2f}"),
        ("成交额", f"{stats['total_turnover_amount']:,.2f}"),
        ("停牌天数", f"{stats['suspended_days']}"),
        ("涨停/跌停", f"{stats['limit_up_days']} / {stats['limit_down_days']}"),
    ]
    rows = []
    for label, value in cards:
        value_class = "long" if label == "区间" else ""
        rows.append(
            f'<div class="metric"><span>{html.escape(label)}</span><strong class="{value_class}">{html.escape(value)}</strong></div>'
        )
    return "\n".join(rows)


def render_diagnostics(stats: dict) -> str:
    execution_counts = ", ".join(
        f"{source}: {count}" for source, count in sorted(stats.get("execution_source_counts", {}).items())
    ) or "无"
    daily_db_note = (
        f"日线数据库：{stats['daily_db_path']}，本次读取 {stats['daily_rows']:,} 根，"
        f"覆盖 {stats['daily_start']} 至 {stats['daily_end']}。"
        if stats.get("daily_db_path")
        else "日线数据库：未启用。"
    )
    minute_db_note = (
        f"分时数据库：{stats['minute_db_path']}，本次读取 {stats['minute_rows']:,} 根，"
        f"覆盖 {stats['minute_start']} 至 {stats['minute_end']}。"
        if stats.get("minute_db_path")
        else "分时数据库：未启用。"
    )
    items = [
        f"成交价：当前模式 {stats['execution_price_mode']}，目标成交时间 {stats['execution_time']}；实际交易成交价来源统计：{execution_counts}。",
        daily_db_note,
        minute_db_note,
        f"手续费：佣金 {stats['commission_rate']:.4%}，最低 {stats['min_commission']:.2f} 元，印花税 {stats['stamp_tax_rate']:.4%}，已计入资金曲线。",
        f"滑点：每笔按 {stats['slippage_bps']:.2f} bps 估算，买入加价、卖出降价，已计入成交价和资金曲线。",
        f"胜率：按每次卖出产生的已实现盈亏统计，共 {stats['realized_trade_count']} 次已实现盈亏。",
        f"换手率：总成交额 / 平均账户权益，本次为 {stats['turnover']:.2f}x。",
        f"停牌/涨跌停：停牌 {stats['suspended_days']} 天，涨停 {stats['limit_up_days']} 天，跌停 {stats['limit_down_days']} 天；阈值 {stats['limit_threshold_pct']:.2%}。{stats['limit_note']}",
        f"未来函数：{stats['future_function_check']}",
        f"幸存者偏差：{stats['survivorship_bias_check']}",
    ]
    return "<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in items) + "</ul>"


def position_ratio_series(bt: pd.DataFrame) -> pd.Series:
    equity = bt["equity"].replace(0, pd.NA)
    ratio = (bt["position"] * bt["close"]) / equity
    return ratio.fillna(0.0).clip(lower=0.0)


def offset_trade_marker_prices(trades: list[Trade], chart_bt: pd.DataFrame, side: str, marker_gap: float) -> list[float]:
    by_date = chart_bt.set_index("date_label")
    prices = []
    for trade in trades:
        trade_date = trade.trade_date.strftime("%Y-%m-%d")
        if trade_date in by_date.index:
            row = by_date.loc[trade_date]
            base = float(row["low"] if side == "BUY" else row["high"])
            prices.append(base - marker_gap if side == "BUY" else base + marker_gap)
        else:
            prices.append(trade.price)
    return prices


def build_chart_data(chart_bt: pd.DataFrame, trades_by_date: dict[str, list[dict]]) -> list[dict]:
    rows = []
    for row in chart_bt.to_dict("records"):
        date_label = row["date_label"]
        rows.append(
            {
                "date": date_label,
                "open": rounded_value(row.get("open")),
                "high": rounded_value(row.get("high")),
                "low": rounded_value(row.get("low")),
                "close": rounded_value(row.get("close")),
                "ma5": rounded_value(row.get("ma5")),
                "ma10": rounded_value(row.get("ma10")),
                "ma20": rounded_value(row.get("ma20")),
                "ma60": rounded_value(row.get("ma60")),
                "profit": rounded_value(row.get("profit"), 2),
                "currentBuildProfit": rounded_value(row.get("current_build_profit"), 2),
                "positionRatio": rounded_value(row.get("position_ratio"), 4),
                "position": int(row.get("position") or 0),
                "trades": trades_by_date.get(date_label, []),
            }
        )
    return rows


def rounded_value(value: object, digits: int = 4) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def render_chart_data_panel(data: dict) -> str:
    values = chart_data_display_values(data)
    return f"""
      <section class="chart-data" id="chart-data-panel">
        <h2>图例数据</h2>
        <div class="chart-data-grid">
          <div class="chart-data-item"><span>日期</span><strong data-chart-field="date">{html.escape(values["date"])}</strong></div>
          <div class="chart-data-item ma-item"><span>均线</span><strong data-chart-field="ma">{values["ma"]}</strong></div>
          <div class="chart-data-item"><span>买入</span><strong data-chart-field="buy">{html.escape(values["buy"])}</strong></div>
          <div class="chart-data-item"><span>卖出</span><strong data-chart-field="sell">{html.escape(values["sell"])}</strong></div>
          <div class="chart-data-item"><span>利润</span><strong data-chart-field="profit">{html.escape(values["profit"])}</strong></div>
          <div class="chart-data-item"><span>建仓以来盈利</span><strong data-chart-field="currentBuildProfit">{html.escape(values["currentBuildProfit"])}</strong></div>
          <div class="chart-data-item"><span>仓位</span><strong data-chart-field="position">{html.escape(values["position"])}</strong></div>
        </div>
      </section>
    """


def chart_data_display_values(data: dict) -> dict[str, str]:
    if not data:
        return {
            "date": "-",
            "ma": "-",
            "buy": "-",
            "sell": "-",
            "profit": "-",
            "currentBuildProfit": "-",
            "position": "-",
        }
    trades = data.get("trades") or []
    buy_text = "；".join(
        f"{trade['quantity']:,}@{trade['price']:.3f}"
        for trade in trades
        if trade["side"] == "BUY"
    ) or "-"
    sell_text = "；".join(
        f"{trade['quantity']:,}@{trade['price']:.3f}"
        for trade in trades
        if trade["side"] == "SELL"
    ) or "-"
    return {
        "date": str(data.get("date") or "-"),
        "ma": ma_comparison_html(data),
        "buy": buy_text,
        "sell": sell_text,
        "profit": f"{float(data.get('profit') or 0):,.2f}",
        "currentBuildProfit": f"{float(data.get('currentBuildProfit') or 0):,.2f}",
        "position": f"{float(data.get('positionRatio') or 0):.0%}（{int(data.get('position') or 0):,}股）",
    }


def ma_comparison_html(data: dict) -> str:
    pairs = [("MA5", data.get("ma5")), ("MA10", data.get("ma10")), ("MA20", data.get("ma20")), ("MA60", data.get("ma60"))]
    parts = []
    for index, (label, value) in enumerate(pairs):
        parts.append(label)
        if index < len(pairs) - 1:
            sign, css_class = compare_values(value, pairs[index + 1][1])
            parts.append(f'<span class="{css_class}">{html.escape(sign)}</span>')
    return " ".join(parts)


def compare_values(left: object, right: object) -> tuple[str, str]:
    if left is None or right is None or pd.isna(left) or pd.isna(right):
        return "=", "ma-eq"
    left_value = float(left)
    right_value = float(right)
    if left_value > right_value:
        return ">", "ma-gt"
    if left_value < right_value:
        return "<", "ma-lt"
    return "=", "ma-eq"


def format_optional(value: object) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):.4f}"


def render_chart_data_script(chart_data: list[dict]) -> str:
    payload = json.dumps(chart_data, ensure_ascii=False)
    return f"""
      <script>
        (() => {{
          const chartRows = {payload};
          const chartMap = new Map(chartRows.map((row) => [row.date, row]));
          const panel = document.getElementById("chart-data-panel");
          const graph = document.querySelector(".chart-panel .plotly-graph-div");
          if (!panel || !graph) return;
          graph.tabIndex = 0;
          graph.setAttribute("aria-label", "K线图，使用左右方向键移动光标");
          let activeIndex = Math.max(chartRows.length - 1, 0);
          const setText = (field, value) => {{
            const node = panel.querySelector(`[data-chart-field="${{field}}"]`);
            if (node) node.textContent = value;
          }};
          const setHtml = (field, value) => {{
            const node = panel.querySelector(`[data-chart-field="${{field}}"]`);
            if (node) node.innerHTML = value;
          }};
          const fmt = (value, digits = 4) => value === null || value === undefined || Number.isNaN(Number(value)) ? "-" : Number(value).toFixed(digits);
          const compare = (left, right) => {{
            if (left === null || left === undefined || right === null || right === undefined || Number.isNaN(Number(left)) || Number.isNaN(Number(right))) {{
              return `<span class="ma-eq">=</span>`;
            }}
            if (Number(left) > Number(right)) return `<span class="ma-gt">&gt;</span>`;
            if (Number(left) < Number(right)) return `<span class="ma-lt">&lt;</span>`;
            return `<span class="ma-eq">=</span>`;
          }};
          const maHtml = (row) => `MA5 ${{compare(row.ma5, row.ma10)}} MA10 ${{compare(row.ma10, row.ma20)}} MA20 ${{compare(row.ma20, row.ma60)}} MA60`;
          const render = (row) => {{
            if (!row) return;
            setText("date", row.date || "-");
            setHtml("ma", maHtml(row));
            const buyText = (row.trades || []).filter((trade) => trade.side === "BUY").map((trade) => `${{Number(trade.quantity).toLocaleString("zh-CN")}}@${{Number(trade.price).toFixed(3)}}`).join("；") || "-";
            const sellText = (row.trades || []).filter((trade) => trade.side === "SELL").map((trade) => `${{Number(trade.quantity).toLocaleString("zh-CN")}}@${{Number(trade.price).toFixed(3)}}`).join("；") || "-";
            setText("buy", buyText);
            setText("sell", sellText);
            setText("profit", Number(row.profit || 0).toLocaleString("zh-CN", {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }}));
            setText("currentBuildProfit", Number(row.currentBuildProfit || 0).toLocaleString("zh-CN", {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }}));
            setText("position", `${{Number(row.positionRatio || 0).toLocaleString("zh-CN", {{ style: "percent", maximumFractionDigits: 0 }})}}（${{Number(row.position || 0).toLocaleString("zh-CN")}}股）`);
          }};
          const cursorTraceIndex = () => (graph.data || []).findIndex((trace) => trace.name === "光标定位");
          const moveCursor = (index) => {{
            if (!chartRows.length) return;
            activeIndex = Math.max(0, Math.min(chartRows.length - 1, index));
            render(chartRows[activeIndex]);
            const curveNumber = cursorTraceIndex();
            if (window.Plotly && curveNumber >= 0) {{
              window.Plotly.Fx.hover(graph, [{{ curveNumber, pointNumber: activeIndex }}], ["xy"]);
            }}
          }};
          graph.on("plotly_hover", (event) => {{
            const point = event.points && event.points[0];
            const date = point && point.x;
            const row = chartMap.get(date);
            if (!row) return;
            const hoverIndex = chartRows.findIndex((item) => item.date === row.date);
            if (hoverIndex >= 0) activeIndex = hoverIndex;
            render(row);
          }});
          graph.addEventListener("click", () => graph.focus());
          graph.addEventListener("keydown", (event) => {{
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
            event.preventDefault();
            moveCursor(activeIndex + (event.key === "ArrowRight" ? 1 : -1));
          }});
          if (chartRows.length) moveCursor(chartRows.length - 1);
        }})();
      </script>
    """


def compact_trade_note(trade: Trade) -> str:
    note = trade.note
    source = "分时价" if "mootdx_1m" in note else "收盘价"
    if trade.side == "BUY":
        buy_match = re.search(r"buy_count=(\d+)", note)
        target_match = re.search(r"target_position=([0-9.]+%)", note)
        buy_count = buy_match.group(1) if buy_match else ""
        target = target_match.group(1) if target_match else ""
        prefix = f"第{buy_count}次买入" if buy_count else "买入"
        return f"{prefix}，目标仓位{target}，{source}" if target else f"{prefix}，{source}"

    if "close_below_MA" in note:
        ma_match = re.search(r"close_below_MA(\d+)", note)
        ma = ma_match.group(1) if ma_match else ""
        return f"跌破MA{ma}，清仓，{source}" if ma else f"跌破均线，清仓，{source}"

    sell_match = re.search(r"sell_count=(\d+)", note)
    sell_count = sell_match.group(1) if sell_match else ""
    if "clear_position" in note:
        return f"第{sell_count}次回撤卖出，清仓，{source}" if sell_count else f"回撤卖出，清仓，{source}"
    if "sell_half" in note:
        return f"第{sell_count}次回撤卖出，减半，{source}" if sell_count else f"回撤卖出，减半，{source}"
    return f"卖出，{source}"


def render_trade_table(trades: list[dict]) -> str:
    if not trades:
        return "<p class=\"hint\">暂无交易。</p>"

    rows = []
    for trade in trades:
        side_class = "buy" if trade["side"] == "BUY" else "sell"
        side_label = "买入" if trade["side"] == "BUY" else "卖出"
        rows.append(
            "<tr>"
            f"<td>{html.escape(trade['date'])}</td>"
            f"<td class=\"{side_class}\">{side_label}</td>"
            f"<td>{trade['price']:.3f}</td>"
            f"<td>{trade['quantity']:,}</td>"
            f"<td>{trade['gross_amount']:,.2f}</td>"
            f"<td>{trade['fee']:,.2f}</td>"
            f"<td>{trade['slippage_cost']:,.2f}</td>"
            f"<td>{trade['realized_pnl']:,.2f}</td>"
            f"<td>{trade['cash']:,.2f}</td>"
            f"<td>{trade['position']:,}</td>"
            f"<td>{trade['equity']:,.2f}</td>"
            f"<td>{html.escape(trade['note'])}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>日期</th><th>方向</th><th>价格</th><th>数量</th><th>成交额</th><th>手续费</th><th>滑点</th><th>已实现盈亏</th><th>现金</th><th>持仓</th><th>权益</th><th>说明</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def open_report_in_browser() -> None:
    report_path = REPORT_HTML.resolve()
    if sys.platform == "darwin":
        edge_result = subprocess.run(["open", "-a", "Microsoft Edge", str(report_path)], check=False)
        if edge_result.returncode == 0:
            return
        subprocess.run(["open", str(report_path)], check=False)
        return
    webbrowser.open(report_path.as_uri())


def main() -> None:
    parser = argparse.ArgumentParser(description="回测 588330 趋势回撤策略")
    parser.add_argument("--start", default=DEFAULT_BACKTEST_START)
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--execution-price", choices=["intraday", "close"], default="intraday", help="成交价来源：intraday 使用 1 分钟 K，close 使用日收盘价")
    parser.add_argument("--execution-time", default="15:00", help="intraday 模式下的目标成交时间，例如 09:31 或 15:00")
    parser.add_argument("--refresh-daily-cache", action="store_true", help="重新从腾讯刷新日 K 并写入数据库")
    parser.add_argument("--refresh-minute-cache", action="store_true", help="忽略数据库最新时间，重新回补 mootdx 1 分钟 K")
    parser.add_argument("--png", action="store_true", help="额外生成 PNG 图片；默认只生成 HTML")
    parser.add_argument("--no-open", action="store_true", help="只生成报告，不自动打开浏览器")
    args = parser.parse_args()

    config = load_config()
    symbol = config["symbols"][0]
    raw = fetch_tencent_daily(symbol, args.start, args.end, refresh_cache=args.refresh_daily_cache)
    data = prepare_indicators(raw)
    data.attrs.update(raw.attrs)
    minute_prices = load_minute_prices(
        symbol,
        args.start,
        args.end,
        enabled=args.execution_price == "intraday",
        refresh_cache=args.refresh_minute_cache,
    )
    bt, trades, stats = run_backtest(
        data,
        config,
        args.start,
        args.end,
        minute_prices=minute_prices,
        execution_price_mode=args.execution_price,
        execution_time=args.execution_time,
    )
    set_report_paths(symbol, stats["actual_start"], stats["actual_end"])
    save_html_report(symbol, bt, trades, stats, stats["actual_start"], stats["actual_end"])
    if args.png:
        plot_report(symbol, bt, trades, stats, stats["actual_start"], stats["actual_end"])
    if not args.no_open:
        open_report_in_browser()

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"html={REPORT_HTML}")
    if args.png:
        print(f"chart={REPORT_PNG}")


if __name__ == "__main__":
    main()
