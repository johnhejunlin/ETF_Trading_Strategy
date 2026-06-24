import json
import logging
import random
import socket
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from trading_strategy import Candle


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
EASTMONEY_MIN_INTERVAL_SECONDS = 1.0
_eastmoney_last_call = 0.0
_eastmoney_lock = threading.Lock()


@dataclass(frozen=True)
class LatestQuote:
    symbol: str
    name: str
    price: float
    change_percent: str
    previous_close: Optional[float]
    open_price: Optional[float]
    trade_time: str


class EastMoneyMarketData:
    def __init__(self, timeout_seconds: int = 10) -> None:
        self.timeout_seconds = timeout_seconds

    def daily_candles(self, symbol: str, limit: int = 120) -> list[Candle]:
        symbol = self._normalize_symbol(symbol)
        try:
            return self._tencent_daily_candles(symbol, limit)
        except RuntimeError as exc:
            logging.warning("%s 腾讯日 K 失败，切换东方财富备用源: %s", symbol, exc)
            return self._eastmoney_daily_candles(symbol, limit)

    def latest_quote(self, symbol: str) -> LatestQuote:
        symbol = self._normalize_symbol(symbol)
        market_symbol = self._tencent_symbol(symbol)
        url = "https://qt.gtimg.cn/q=" + urllib.parse.quote(market_symbol)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                    raw = response.read().decode("gbk", errors="ignore")
                return self._parse_tencent_quote(symbol, raw)
            except (OSError, socket.timeout, UnicodeDecodeError, ValueError) as exc:
                last_error = exc
                logging.warning("%s 实时行情请求失败，第 %s 次重试: %s", symbol, attempt, exc)
                time.sleep(attempt)
        raise RuntimeError(f"{symbol} 实时行情请求连续失败: {last_error}")

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
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Referer": "https://quote.eastmoney.com/",
            },
        )
        payload = self._fetch_eastmoney_json(req)

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
        req = urllib.request.Request(url, headers={"User-Agent": UA})
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

    def _fetch_eastmoney_json(self, req: urllib.request.Request) -> dict:
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                with _eastmoney_lock:
                    _wait_for_eastmoney_slot()
                    with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                        return json.loads(response.read().decode("utf-8"))
            except (OSError, socket.timeout, json.JSONDecodeError) as exc:
                last_error = exc
                logging.warning("东方财富数据请求失败，第 %s 次重试: %s", attempt, exc)
                time.sleep(attempt)
        raise RuntimeError(f"东方财富数据请求连续失败: {last_error}")

    @staticmethod
    def _parse_tencent_quote(symbol: str, raw: str) -> LatestQuote:
        symbol = EastMoneyMarketData._normalize_symbol(symbol)
        if '"' not in raw:
            raise ValueError(f"腾讯行情返回格式异常: {raw[:80]}")
        body = raw.split('"', 2)[1]
        parts = body.split("~")
        if len(parts) < 32:
            raise ValueError(f"腾讯行情字段不足: {raw[:80]}")
        return LatestQuote(
            symbol=symbol,
            name=parts[1],
            price=float(parts[3]),
            change_percent=parts[32] if len(parts) > 32 else "",
            previous_close=_optional_float(parts[4]),
            open_price=_optional_float(parts[5]),
            trade_time=parts[30] if len(parts) > 30 else "",
        )

    @staticmethod
    def _secid(symbol: str) -> str:
        symbol = EastMoneyMarketData._normalize_symbol(symbol)
        if symbol.startswith(("5", "6", "9")):
            return f"1.{symbol}"
        if symbol.startswith(("4", "8")):
            return f"0.{symbol}"
        return f"0.{symbol}"

    @staticmethod
    def _tencent_symbol(symbol: str) -> str:
        symbol = EastMoneyMarketData._normalize_symbol(symbol)
        if symbol.startswith(("5", "6", "9")):
            return f"sh{symbol}"
        if symbol.startswith(("4", "8")):
            return f"bj{symbol}"
        return f"sz{symbol}"

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        value = symbol.strip().lower()
        if "." in value:
            value = value.split(".", 1)[0]
        for prefix in ("sh", "sz", "bj"):
            if value.startswith(prefix):
                value = value[len(prefix):]
                break
        if len(value) != 6 or not value.isdigit():
            raise ValueError(f"股票代码格式异常: {symbol}")
        return value


def _optional_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wait_for_eastmoney_slot() -> None:
    global _eastmoney_last_call

    elapsed = time.time() - _eastmoney_last_call
    wait_seconds = EASTMONEY_MIN_INTERVAL_SECONDS - elapsed
    if wait_seconds > 0:
        time.sleep(wait_seconds + random.uniform(0.1, 0.5))
    _eastmoney_last_call = time.time()
