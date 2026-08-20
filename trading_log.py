#!/usr/bin/env python3
"""Append-only, local audit log for order lifecycle and account trades."""

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional


ORDER_STATUSES = {
    "VALIDATED",
    "SUBMITTED",
    "PARTIAL",
    "FILLED",
    "UNFILLED",
    "CANCELLED",
    "REJECTED",
}

TRADING_LOG_FIELDS = [
    "event_id",
    "recorded_at",
    "event_time",
    "event_type",
    "order_status",
    "order_id",
    "contract_id",
    "source",
    "symbol",
    "name",
    "side",
    "requested_quantity",
    "executed_quantity",
    "remaining_quantity",
    "limit_price",
    "trade_price",
    "price_source",
    "trigger_condition",
    "pre_quantity",
    "post_quantity",
    "confidence",
    "evidence_path",
    "note",
]


def stable_event_id(*parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class TradingLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._event_ids: Optional[set[str]] = None

    def append(self, event: Mapping[str, object]) -> bool:
        status = str(event.get("order_status") or "")
        if status not in ORDER_STATUSES:
            raise ValueError(f"未知订单状态: {status}")
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise ValueError("TradingLog 事件必须提供 event_id。")
        if event_id in self.event_ids():
            return False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.path.exists() and self.path.stat().st_size > 0
        row = {field: event.get(field, "") for field in TRADING_LOG_FIELDS}
        row["recorded_at"] = row["recorded_at"] or datetime.now().isoformat(timespec="seconds")
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRADING_LOG_FIELDS, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow(row)
        self.event_ids().add(event_id)
        return True

    def event_ids(self) -> set[str]:
        if self._event_ids is not None:
            return self._event_ids
        self._event_ids = set()
        if not self.path.exists() or self.path.stat().st_size == 0:
            return self._event_ids
        with self.path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                event_id = str(row.get("event_id") or "")
                if event_id:
                    self._event_ids.add(event_id)
        return self._event_ids
