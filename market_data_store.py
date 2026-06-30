from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


ROOT = Path(__file__).resolve().parent
MARKET_DATA_DB_PATH = ROOT / "market_data.sqlite3"


def init_market_data_db(db_path: Path = MARKET_DATA_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS realtime_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                name TEXT,
                price REAL NOT NULL,
                change_percent TEXT,
                previous_close REAL,
                open_price REAL,
                trade_time TEXT,
                fetched_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_realtime_quotes_lookup ON realtime_quotes(symbol, fetched_at)")


def upsert_daily_bars(
    rows: Iterable[dict],
    *,
    db_path: Path = MARKET_DATA_DB_PATH,
    adjustment: str = "qfq",
) -> None:
    payload = []
    for row in rows:
        payload.append(
            (
                str(row["symbol"]),
                adjustment,
                str(row["date"]),
                float(row["open"]),
                float(row["close"]),
                float(row["high"]),
                float(row["low"]),
                _optional_float(row.get("volume")),
            )
        )
    if not payload:
        return
    init_market_data_db(db_path)
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
            payload,
        )


def insert_realtime_quote(
    *,
    symbol: str,
    name: str,
    price: float,
    change_percent: str,
    previous_close: Optional[float],
    open_price: Optional[float],
    trade_time: str,
    db_path: Path = MARKET_DATA_DB_PATH,
    fetched_at: Optional[datetime] = None,
) -> None:
    init_market_data_db(db_path)
    timestamp = (fetched_at or datetime.now()).isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO realtime_quotes(
                symbol, name, price, change_percent, previous_close, open_price, trade_time, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                name,
                float(price),
                change_percent,
                previous_close,
                open_price,
                trade_time,
                timestamp,
            ),
        )


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
