#!/usr/bin/env python3
"""UI map collector and explicit safe click verifier for THS macOS screens.

This module captures or imports screenshots, runs Apple Vision OCR, converts
OCR boxes into potential System Events click coordinates, and stores the
observations in a local SQLite database. It can also verify a stored coordinate
with an explicit CLI command. It intentionally does not type, log in, submit,
or call any trading execution code.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from apple_account_snapshot import (
    OcrText,
    activate_app,
    capture_screenshot,
    frontmost_process_name,
    get_coregraphics_window_id,
    get_window_rect,
    normalize_ocr_text,
    run_vision_ocr,
)
from AppBridge_OCRPositionCalculation import (
    WindowRect,
    image_size_from_file,
    ocr_box_to_click_point,
)


ROOT = Path(__file__).resolve().parent
SCREENSHOTS = ROOT / "screenshots"
DEFAULT_DB = ROOT / "app_ui_map.sqlite3"

KNOWN_PAGE_PREFIXES = {
    "home": "",
    "trade": "L1F2",
    "simulation": "L1F2.L2F5",
    "buy_form": "L1F2.L2F5.L3F1",
    "buy_confirmation": "L1F2.L2F5.L3F1.AXSHEET1",
    "sell_form": "L1F2.L2F5.L3F2",
    "holdings": "L1F2.L2F5.L3F4",
    "orders": "L1F2.L2F5.L3F5",
    "trades": "L1F2.L2F5.L3F6",
    "funds": "L1F2.L2F5.L3F7",
    "sell_confirmation": "L1F2.L2F5.L3F2.AXSHEET1",
    "login_required": "L1F2.L2F5.LOGIN",
    "popup_ad": "POPUP.AD",
    "ad_webview": "AD.WEBVIEW",
    "unknown_overlay": "OVERLAY.UNKNOWN",
}
EXPECTED_PAGE_CHOICES = sorted([*KNOWN_PAGE_PREFIXES, "popup_close"])

KNOWN_TRANSITIONS = [
    ("home", "trade_tab", "trade"),
    ("trade", "simulation_entry", "simulation"),
    ("simulation", "buy_tab", "buy_form"),
    ("simulation", "sell_tab", "sell_form"),
    ("simulation", "holdings_tab", "holdings"),
]

KNOWN_ACCESSIBILITY_ELEMENTS = [
    ("home.trade_button", "home", "交易", "trade_button", "AXButton", {"scope": "window 1", "names": ["交易"]}, "AXPress", False),
    ("home.a_share_button", "home", "A股", "a_share_button", "AXButton", {"scope": "window 1", "names": ["A股"]}, "AXPress", False),
    ("home.simulation_button", "home", "模拟", "simulation_button", "AXButton", {"scope": "window 1", "names": ["模拟"]}, "AXPress", False),
    ("home.stock_button", "home", "股票", "stock_button", "AXButton", {"scope": "window 1", "names": ["股票"]}, "AXPress", False),
    ("simulation.buy_tab", "simulation", "买入", "buy_tab", "AXButton", {"scope": "window 1", "names": ["买入"]}, "AXPress", False),
    ("simulation.sell_tab", "simulation", "卖出", "sell_tab", "AXButton", {"scope": "window 1", "names": ["卖出"]}, "AXPress", False),
    ("simulation.holdings_tab", "simulation", "持仓", "holdings_tab", "AXButton", {"scope": "window 1", "names": ["持仓"]}, "AXPress", False),
    ("simulation.orders_tab", "simulation", "委托", "orders_tab", "AXButton", {"scope": "window 1", "names": ["委托"]}, "AXPress", False),
    ("simulation.trades_tab", "simulation", "成交", "trades_tab", "AXButton", {"scope": "window 1", "names": ["成交"]}, "AXPress", False),
    ("simulation.funds_tab", "simulation", "资金明细", "funds_tab", "AXButton", {"scope": "window 1", "names": ["资金明细"]}, "AXPress", False),
    ("buy_form.symbol_field", "buy_form", "代码", "symbol_field", "AXTextField", {"scope": "window 1", "near_labels": ["代码", "股票代码", "证券代码"], "input_mode": "type_only", "clipboard_allowed": False}, "AXType", True),
    ("buy_form.price_field", "buy_form", "价格", "price_field", "AXTextField", {"scope": "window 1", "near_labels": ["价格", "限价"]}, "AXSetValue", True),
    ("buy_form.quantity_field", "buy_form", "数量", "quantity_field", "AXTextField", {"scope": "window 1", "near_labels": ["数量", "委托数量", "买入量"]}, "AXSetValue", True),
    ("buy_form.reset_button", "buy_form", "重填", "reset_button", "AXButton", {"scope": "window 1", "names": ["重填"]}, "AXPress", False),
    ("buy_form.submit_button", "buy_form", "确定买入", "submit_buy_button", "AXButton", {"scope": "window 1", "names": ["确定买入", "买入（模拟账户）", "买入(模拟账户)"]}, "AXPress", True),
    ("sell_form.symbol_field", "sell_form", "代码", "symbol_field", "AXTextField", {"scope": "window 1", "near_labels": ["代码", "股票代码", "证券代码"], "input_mode": "type_only", "clipboard_allowed": False}, "AXType", True),
    ("sell_form.price_field", "sell_form", "价格", "price_field", "AXTextField", {"scope": "window 1", "near_labels": ["价格", "限价"]}, "AXSetValue", True),
    ("sell_form.quantity_field", "sell_form", "数量", "quantity_field", "AXTextField", {"scope": "window 1", "near_labels": ["数量", "委托数量", "卖出量"]}, "AXSetValue", True),
    ("sell_form.reset_button", "sell_form", "重填", "reset_button", "AXButton", {"scope": "window 1", "names": ["重填"]}, "AXPress", False),
    ("sell_form.submit_button", "sell_form", "确定卖出", "submit_sell_button", "AXButton", {"scope": "window 1", "names": ["确定卖出", "卖出（模拟账户）", "卖出(模拟账户)"]}, "AXPress", True),
    ("buy_confirmation.cancel_button", "buy_confirmation", "取消", "cancel_buy_button", "AXButton", {"scope": "window 1/AXSheet 1", "names": ["取消"]}, "AXPress", False),
    ("buy_confirmation.confirm_button", "buy_confirmation", "确认", "confirm_buy_button", "AXButton", {"scope": "window 1/AXSheet 1", "names": ["确认", "确认买入", "确定买入"]}, "AXPress", True),
    ("sell_confirmation.cancel_button", "sell_confirmation", "取消", "cancel_sell_button", "AXButton", {"scope": "window 1/AXSheet 1", "names": ["取消"]}, "AXPress", False),
    ("sell_confirmation.confirm_button", "sell_confirmation", "确认", "confirm_sell_button", "AXButton", {"scope": "window 1/AXSheet 1", "names": ["确认", "确认卖出", "确定卖出"]}, "AXPress", True),
]

SIMULATION_MARKERS = ("模拟炒股", "模拟账户", "模拟交易", "模拟练习", "大玩家")
LIVE_MARKERS = ("普通交易", "实盘", "真实交易", "证券交易")
AD_MARKERS = (
    "立即参与",
    "八强赛",
    "世界杯",
    "看世界杯",
    "选择支持的球队",
    "抽大奖",
    "支持赛事赢金豆",
    "赛事赢金豆",
)
TRADE_MARKERS = ("交易", "买入", "卖出", "撤单", "持仓", "查询", "模拟练习区")
BLOCKED_CLICK_TEXT_MARKERS = (
    "确认买入",
    "确认卖出",
    "委托买入确认",
    "委托卖出确认",
    "提交",
    "下单",
    "登录",
    "密码",
    "验证码",
)


def connect_db(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    initialize_db(conn)
    return conn


def initialize_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS ui_pages (
            page_id TEXT PRIMARY KEY,
            page_name TEXT NOT NULL,
            page_state TEXT NOT NULL DEFAULT 'unknown',
            overlay_state TEXT NOT NULL DEFAULT 'none',
            account_mode TEXT NOT NULL DEFAULT 'unknown',
            hierarchy_code TEXT NOT NULL DEFAULT '',
            executable INTEGER NOT NULL DEFAULT 0,
            anchors_json TEXT NOT NULL DEFAULT '{}',
            raw_ui_text TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ui_elements (
            element_id TEXT PRIMARY KEY,
            page_id TEXT NOT NULL,
            display_text TEXT NOT NULL,
            semantic_name TEXT NOT NULL,
            hierarchy_code TEXT NOT NULL,
            element_kind TEXT NOT NULL DEFAULT 'ocr',
            ax_role TEXT,
            selector_json TEXT NOT NULL DEFAULT '{}',
            action TEXT,
            high_risk INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(page_id) REFERENCES ui_pages(page_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ui_observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id TEXT NOT NULL,
            element_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            trusted_click INTEGER NOT NULL,
            screenshot_path TEXT NOT NULL,
            capture_mode TEXT NOT NULL DEFAULT '',
            frontmost_process TEXT,
            window_rect_json TEXT,
            image_size_json TEXT NOT NULL,
            ocr_text TEXT NOT NULL,
            confidence REAL NOT NULL,
            vision_box_json TEXT NOT NULL,
            pixel_center_json TEXT,
            click_point_json TEXT,
            content_offset_px_json TEXT,
            scale_x REAL,
            scale_y REAL,
            FOREIGN KEY(page_id) REFERENCES ui_pages(page_id) ON DELETE CASCADE,
            FOREIGN KEY(element_id) REFERENCES ui_elements(element_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ui_transitions (
            transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_page_id TEXT NOT NULL,
            action_id TEXT NOT NULL,
            to_page_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(from_page_id, action_id, to_page_id)
        );

        CREATE TABLE IF NOT EXISTS ui_click_verifications (
            verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            verified_at TEXT NOT NULL,
            page_id TEXT NOT NULL,
            element_id TEXT NOT NULL,
            observation_id INTEGER NOT NULL,
            expected_page_id TEXT NOT NULL,
            passed INTEGER NOT NULL,
            blocked INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            click_point_json TEXT NOT NULL,
            before_screenshot_path TEXT NOT NULL,
            after_screenshot_path TEXT,
            result_json TEXT NOT NULL,
            FOREIGN KEY(page_id) REFERENCES ui_pages(page_id) ON DELETE CASCADE,
            FOREIGN KEY(element_id) REFERENCES ui_elements(element_id) ON DELETE CASCADE,
            FOREIGN KEY(observation_id) REFERENCES ui_observations(observation_id) ON DELETE CASCADE
        );
        """
    )
    migrate_db(conn)
    seed_known_transitions(conn)
    conn.commit()


def migrate_db(conn: sqlite3.Connection) -> None:
    page_columns = {row["name"] for row in conn.execute("PRAGMA table_info(ui_pages)").fetchall()}
    if "page_state" not in page_columns:
        conn.execute("ALTER TABLE ui_pages ADD COLUMN page_state TEXT NOT NULL DEFAULT 'unknown'")
    if "overlay_state" not in page_columns:
        conn.execute("ALTER TABLE ui_pages ADD COLUMN overlay_state TEXT NOT NULL DEFAULT 'none'")
    element_columns = {row["name"] for row in conn.execute("PRAGMA table_info(ui_elements)").fetchall()}
    element_migrations = {
        "element_kind": "TEXT NOT NULL DEFAULT 'ocr'",
        "ax_role": "TEXT",
        "selector_json": "TEXT NOT NULL DEFAULT '{}'",
        "action": "TEXT",
        "high_risk": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in element_migrations.items():
        if column not in element_columns:
            conn.execute(f"ALTER TABLE ui_elements ADD COLUMN {column} {definition}")


def seed_known_transitions(conn: sqlite3.Connection) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for from_page, action, to_page in KNOWN_TRANSITIONS:
        conn.execute(
            """
            INSERT OR IGNORE INTO ui_transitions
                (from_page_id, action_id, to_page_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (from_page, action, to_page, now),
        )


def infer_account_mode(raw_text: str) -> str:
    if any(marker in raw_text for marker in LIVE_MARKERS):
        return "live"
    if any(marker in raw_text for marker in SIMULATION_MARKERS):
        return "simulation"
    return "unknown"


def infer_page_state(raw_text: str) -> str:
    has_ad = any(marker in raw_text for marker in AD_MARKERS)
    has_trade = any(marker in raw_text for marker in TRADE_MARKERS)
    has_simulation = any(marker in raw_text for marker in SIMULATION_MARKERS)
    has_login = any(marker in raw_text for marker in ("登录", "密码", "验证码"))
    if has_ad and not has_trade:
        return "ad_webview"
    if has_login:
        return "login_required"
    if "股票代码" in raw_text and "限价" in raw_text and "买入" in raw_text:
        return "buy_form"
    if "股票代码" in raw_text and "限价" in raw_text and "卖出" in raw_text:
        return "sell_form"
    if ("持仓股" in raw_text or "持仓/可用" in raw_text) and has_simulation:
        return "holdings"
    if has_simulation and has_trade:
        return "trade"
    if has_trade:
        return "trade"
    return "unknown"


def infer_overlay_state(raw_text: str, page_state: str) -> str:
    has_ad = any(marker in raw_text for marker in AD_MARKERS)
    if has_ad and page_state != "ad_webview":
        return "popup_ad"
    if any(marker in raw_text for marker in ("确定", "取消", "我知道", "以后再说")):
        return "modal"
    return "none"


def build_app_state(page_id: str, raw_text: str) -> dict[str, Any]:
    page_state = infer_page_state(raw_text)
    if page_id == "holdings" and "持仓" in raw_text and any(marker in raw_text for marker in SIMULATION_MARKERS):
        page_state = "holdings"
    overlay_state = infer_overlay_state(raw_text, page_state)
    account_mode = infer_account_mode(raw_text)
    anchors = build_anchors(page_id, raw_text, page_state=page_state, overlay_state=overlay_state)
    safe_actions = []
    if overlay_state in {"popup_ad", "modal"}:
        safe_actions.append("close_overlay")
    if page_state == "ad_webview":
        safe_actions.append("back")
    return {
        "page_state": page_state,
        "overlay_state": overlay_state,
        "account_mode": account_mode,
        "anchors": anchors,
        "safe_actions": safe_actions,
    }


def build_anchors(
    page_id: str,
    raw_text: str,
    *,
    page_state: str | None = None,
    overlay_state: str | None = None,
) -> dict[str, bool]:
    page_state = page_state or infer_page_state(raw_text)
    overlay_state = overlay_state or infer_overlay_state(raw_text, page_state)
    anchors = {
        "simulation": any(marker in raw_text for marker in SIMULATION_MARKERS),
        "live": any(marker in raw_text for marker in LIVE_MARKERS),
        "has_trade": any(marker in raw_text for marker in TRADE_MARKERS),
        "has_buy": "买入" in raw_text,
        "has_sell": "卖出" in raw_text,
        "has_holdings": "持仓" in raw_text or "持仓股" in raw_text,
        "has_login": any(marker in raw_text for marker in ("登录", "密码", "验证码")),
        "has_ad": any(marker in raw_text for marker in AD_MARKERS),
        "overlay_clear": overlay_state == "none",
    }
    if page_id == "buy_form":
        anchors["expected_page"] = page_state == "buy_form" and overlay_state == "none"
    elif page_id == "holdings":
        anchors["expected_page"] = page_state == "holdings" and overlay_state == "none"
    elif page_id == "simulation":
        anchors["expected_page"] = anchors["simulation"] and overlay_state == "none"
    elif page_id == "trade":
        anchors["expected_page"] = page_state == "trade" and overlay_state == "none"
    elif page_id == "login_required":
        anchors["expected_page"] = page_state == "login_required"
    elif page_id == "popup_ad":
        anchors["expected_page"] = overlay_state == "popup_ad"
    elif page_id == "ad_webview":
        anchors["expected_page"] = page_state == "ad_webview"
    elif page_id == "unknown_overlay":
        anchors["expected_page"] = overlay_state != "none"
    else:
        anchors["expected_page"] = bool(raw_text.strip()) and overlay_state == "none"
    return anchors


def semantic_name(text: str, index: int) -> str:
    cleaned = re.sub(r"\s+", "_", text.strip())
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "ocr_item"
    return f"{cleaned[:40]}_{index:03d}"


def element_id_for(page_id: str, text: str, index: int) -> str:
    return f"{page_id}.{semantic_name(text, index)}"


def hierarchy_code_for(page_id: str, index: int) -> str:
    prefix = KNOWN_PAGE_PREFIXES.get(page_id, "")
    level = len(prefix.split(".")) + 1 if prefix else 1
    code = f"L{level}F{index}"
    return f"{prefix}.{code}" if prefix else code


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def parse_rect(value: str | None) -> tuple[int, int, int, int] | None:
    if not value:
        return None
    parts = [int(float(part.strip())) for part in value.split(",") if part.strip()]
    if len(parts) != 4:
        raise SystemExit("--window-rect expects x,y,width,height")
    return parts[0], parts[1], parts[2], parts[3]


def perform_safe_click(app_name: str, x: float, y: float) -> None:
    script = (
        f'tell application "{app_name}" to activate\n'
        "delay 0.1\n"
        'tell application "System Events"\n'
        f"  click at {{{int(round(x))}, {int(round(y))}}}\n"
        "end tell"
    )
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)


def store_accessibility_element(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    page_name: str,
    element_id: str,
    display_text: str,
    semantic_name: str,
    ax_role: str,
    selector: dict[str, Any],
    action: str,
    account_mode: str,
    high_risk: bool = False,
    page_state: str | None = None,
    overlay_state: str | None = None,
    executable: bool = False,
) -> dict[str, Any]:
    """Upsert a semantic Accessibility element without coordinate data."""
    now = datetime.now().isoformat(timespec="seconds")
    hierarchy_code = str(selector.get("path", ""))
    resolved_page_state = page_state or page_id
    resolved_overlay_state = overlay_state or (
        "modal" if page_id.endswith("_confirmation") else "none"
    )
    anchors = {
        "simulation": account_mode == "simulation",
        "expected_page": True,
        "accessibility": True,
    }
    conn.execute(
        """
        INSERT INTO ui_pages (
            page_id, page_name, page_state, overlay_state, account_mode,
            hierarchy_code, executable, anchors_json, raw_ui_text,
            first_seen_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(page_id) DO UPDATE SET
            page_name=excluded.page_name,
            page_state=excluded.page_state,
            overlay_state=excluded.overlay_state,
            account_mode=excluded.account_mode,
            hierarchy_code=excluded.hierarchy_code,
            executable=excluded.executable,
            anchors_json=excluded.anchors_json,
            raw_ui_text=excluded.raw_ui_text,
            updated_at=excluded.updated_at
        """,
        (
            page_id,
            page_name,
            resolved_page_state,
            resolved_overlay_state,
            account_mode,
            KNOWN_PAGE_PREFIXES.get(page_id, ""),
            1 if executable else 0,
            json_dumps(anchors),
            display_text,
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO ui_elements (
            element_id, page_id, display_text, semantic_name, hierarchy_code,
            element_kind, ax_role, selector_json, action, high_risk,
            first_seen_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 'accessibility', ?, ?, ?, ?, ?, ?)
        ON CONFLICT(element_id) DO UPDATE SET
            page_id=excluded.page_id,
            display_text=excluded.display_text,
            semantic_name=excluded.semantic_name,
            hierarchy_code=excluded.hierarchy_code,
            element_kind=excluded.element_kind,
            ax_role=excluded.ax_role,
            selector_json=excluded.selector_json,
            action=excluded.action,
            high_risk=excluded.high_risk,
            updated_at=excluded.updated_at
        """,
        (
            element_id,
            page_id,
            display_text,
            semantic_name,
            hierarchy_code,
            ax_role,
            json_dumps(selector),
            action,
            1 if high_risk else 0,
            now,
            now,
        ),
    )
    conn.commit()
    return {
        "page_id": page_id,
        "element_id": element_id,
        "element_kind": "accessibility",
        "display_text": display_text,
        "ax_role": ax_role,
        "selector": selector,
        "action": action,
        "high_risk": high_risk,
        "database_path": conn.execute("PRAGMA database_list").fetchone()["file"],
    }


def store_known_accessibility_elements(conn: sqlite3.Connection) -> dict[str, Any]:
    """Store the complete stable semantic control catalog used by the bridge."""
    stored: list[str] = []
    for (
        element_id,
        page_id,
        display_text,
        semantic_name_value,
        ax_role,
        selector,
        action,
        high_risk,
    ) in KNOWN_ACCESSIBILITY_ELEMENTS:
        is_confirmation = page_id.endswith("_confirmation")
        selector_payload = {"process": "同花顺", **selector}
        if "path" not in selector_payload:
            if selector_payload.get("names"):
                names = "|".join(selector_payload["names"])
                selector_payload["path"] = (
                    f"{selector_payload['scope']}/{ax_role}[name={names}]"
                )
            elif selector_payload.get("near_labels"):
                labels = "|".join(selector_payload["near_labels"])
                selector_payload["path"] = (
                    f"{selector_payload['scope']}/{ax_role}[near_label={labels}]"
                )
        store_accessibility_element(
            conn,
            page_id=page_id,
            page_name=page_id,
            element_id=element_id,
            display_text=display_text,
            semantic_name=semantic_name_value,
            ax_role=ax_role,
            selector=selector_payload,
            action=action,
            account_mode="simulation" if page_id != "home" else "unknown",
            high_risk=high_risk,
            overlay_state="modal" if is_confirmation else "none",
            executable=page_id not in {"home", "buy_confirmation", "sell_confirmation"},
        )
        stored.append(element_id)
    return {
        "stored": len(stored),
        "element_ids": stored,
        "database_path": conn.execute("PRAGMA database_list").fetchone()["file"],
    }


def store_page_capture(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    page_name: str | None,
    image_path: Path,
    items: Iterable[OcrText],
    source: str,
    capture_mode: str = "",
    frontmost_process: str | None = None,
    window_rect: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    observed_at = datetime.now().isoformat(timespec="seconds")
    ordered = normalize_ocr_text(list(items))
    raw_text = " || ".join(item.text for item in ordered)
    app_state = build_app_state(page_id, raw_text)
    account_mode = app_state["account_mode"]
    page_state = app_state["page_state"]
    overlay_state = app_state["overlay_state"]
    anchors = app_state["anchors"]
    executable = 1 if account_mode == "simulation" and overlay_state == "none" else 0
    image_size = image_size_from_file(image_path)
    page_label = page_name or page_id
    page_hierarchy = KNOWN_PAGE_PREFIXES.get(page_id, "")

    conn.execute(
        """
        INSERT INTO ui_pages (
            page_id, page_name, page_state, overlay_state, account_mode, hierarchy_code, executable,
            anchors_json, raw_ui_text, first_seen_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(page_id) DO UPDATE SET
            page_name=excluded.page_name,
            page_state=excluded.page_state,
            overlay_state=excluded.overlay_state,
            account_mode=excluded.account_mode,
            hierarchy_code=excluded.hierarchy_code,
            executable=excluded.executable,
            anchors_json=excluded.anchors_json,
            raw_ui_text=excluded.raw_ui_text,
            updated_at=excluded.updated_at
        """,
        (
            page_id,
            page_label,
            page_state,
            overlay_state,
            account_mode,
            page_hierarchy,
            executable,
            json_dumps(anchors),
            raw_text,
            observed_at,
            observed_at,
        ),
    )

    stored = 0
    trusted_click = 1 if window_rect is not None else 0
    for index, item in enumerate(ordered, start=1):
        element_id = element_id_for(page_id, item.text, index)
        element_hierarchy = hierarchy_code_for(page_id, index)
        conn.execute(
            """
            INSERT INTO ui_elements (
                element_id, page_id, display_text, semantic_name,
                hierarchy_code, first_seen_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(element_id) DO UPDATE SET
                display_text=excluded.display_text,
                semantic_name=excluded.semantic_name,
                hierarchy_code=excluded.hierarchy_code,
                updated_at=excluded.updated_at
            """,
            (
                element_id,
                page_id,
                item.text,
                semantic_name(item.text, index),
                element_hierarchy,
                observed_at,
                observed_at,
            ),
        )

        pixel_center = None
        click_point = None
        content_offset = None
        scale_x = None
        scale_y = None
        if window_rect is not None:
            result = ocr_box_to_click_point(item, image_size, WindowRect(*window_rect))
            pixel_center = asdict(result.pixel_center)
            click_point = asdict(result.click_point)
            content_offset = asdict(result.content_offset_px)
            scale_x = result.scale_x
            scale_y = result.scale_y

        conn.execute(
            """
            INSERT INTO ui_observations (
                page_id, element_id, observed_at, source, trusted_click,
                screenshot_path, capture_mode, frontmost_process,
                window_rect_json, image_size_json, ocr_text, confidence,
                vision_box_json, pixel_center_json, click_point_json,
                content_offset_px_json, scale_x, scale_y
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page_id,
                element_id,
                observed_at,
                source,
                trusted_click,
                str(image_path),
                capture_mode,
                frontmost_process,
                json_dumps(window_rect) if window_rect else None,
                json_dumps(asdict(image_size)),
                item.text,
                float(item.confidence),
                json_dumps(asdict(item)),
                json_dumps(pixel_center) if pixel_center else None,
                json_dumps(click_point) if click_point else None,
                json_dumps(content_offset) if content_offset else None,
                scale_x,
                scale_y,
            ),
        )
        stored += 1

    conn.commit()
    return {
        "page_id": page_id,
        "page_name": page_label,
        "page_state": page_state,
        "overlay_state": overlay_state,
        "account_mode": account_mode,
        "executable": bool(executable),
        "anchors": anchors,
        "safe_actions": app_state["safe_actions"],
        "screenshot_path": str(image_path),
        "ocr_items": stored,
        "trusted_click": bool(trusted_click),
        "database_path": conn.execute("PRAGMA database_list").fetchone()["file"],
    }


def capture_page(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    app_name: str,
    bundle_id: str | None,
    process_name: str,
) -> dict[str, Any]:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    activate_app(app_name, bundle_id)
    time.sleep(0.5)
    rect = get_window_rect(process_name)
    window_id = get_coregraphics_window_id("同花顺", app_name)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    image_path = SCREENSHOTS / f"ui_map_{timestamp}_{page_id}.png"
    capture_mode = capture_screenshot(image_path, window_id, rect)
    items = run_vision_ocr(image_path)
    return store_page_capture(
        conn,
        page_id=page_id,
        page_name=page_id,
        image_path=image_path,
        items=items,
        source="apple_vision_ocr_capture",
        capture_mode=capture_mode,
        frontmost_process=frontmost_process_name(),
        window_rect=rect,
    )


def find_latest_click_observation(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    element_id: str | None = None,
    text: str | None = None,
) -> sqlite3.Row:
    if element_id:
        row = conn.execute(
            """
            SELECT o.*, e.display_text, e.hierarchy_code
            FROM ui_observations o
            JOIN ui_elements e ON e.element_id = o.element_id
            WHERE o.element_id = ? AND o.trusted_click = 1 AND o.click_point_json IS NOT NULL
            ORDER BY o.observation_id DESC
            LIMIT 1
            """,
            (element_id,),
        ).fetchone()
        if row:
            return row
        raise RuntimeError(f"no trusted click observation for element_id={element_id}")
    if not text:
        raise RuntimeError("verify click requires an element id or OCR text")
    row = conn.execute(
        """
        SELECT o.*, e.display_text, e.hierarchy_code
        FROM ui_observations o
        JOIN ui_elements e ON e.element_id = o.element_id
        WHERE o.page_id = ? AND o.ocr_text = ? AND o.trusted_click = 1 AND o.click_point_json IS NOT NULL
        ORDER BY o.observation_id DESC
        LIMIT 1
        """,
        (page_id, text),
    ).fetchone()
    if row:
        return row
    row = conn.execute(
        """
        SELECT o.*, e.display_text, e.hierarchy_code
        FROM ui_observations o
        JOIN ui_elements e ON e.element_id = o.element_id
        WHERE o.page_id = ? AND o.ocr_text LIKE ? AND o.trusted_click = 1 AND o.click_point_json IS NOT NULL
        ORDER BY o.confidence DESC, o.observation_id DESC
        LIMIT 1
        """,
        (page_id, f"%{text}%"),
    ).fetchone()
    if row:
        return row
    raise RuntimeError(f"no trusted click observation for page={page_id} text={text}")


def blocked_click_reason(text: str) -> str | None:
    return next((marker for marker in BLOCKED_CLICK_TEXT_MARKERS if marker in text), None)


def verify_click_coordinate(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    expected_page_id: str,
    app_name: str,
    bundle_id: str | None,
    process_name: str,
    element_id: str | None = None,
    text: str | None = None,
    wait_seconds: float = 1.0,
) -> dict[str, Any]:
    row = find_latest_click_observation(conn, page_id=page_id, element_id=element_id, text=text)
    click_text = str(row["ocr_text"])
    click_point = json.loads(row["click_point_json"])
    block_marker = blocked_click_reason(click_text)
    if block_marker:
        result = {
            "page_id": page_id,
            "element_id": row["element_id"],
            "observation_id": row["observation_id"],
            "ocr_text": click_text,
            "expected_page_id": expected_page_id,
            "passed": False,
            "blocked": True,
            "block_reason": f"blocked high-risk click text: {block_marker}",
            "click_point": click_point,
            "before_screenshot_path": row["screenshot_path"],
            "after_screenshot_path": None,
        }
        store_click_verification(conn, result)
        return result

    perform_safe_click(app_name, float(click_point["x"]), float(click_point["y"]))
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    capture = capture_page(
        conn,
        page_id=expected_page_id,
        app_name=app_name,
        bundle_id=bundle_id,
        process_name=process_name,
    )
    passed = click_verification_passed(capture, expected_page_id=expected_page_id)
    result = {
        "page_id": page_id,
        "element_id": row["element_id"],
        "observation_id": row["observation_id"],
        "ocr_text": click_text,
        "expected_page_id": expected_page_id,
        "passed": passed,
        "blocked": False,
        "block_reason": None,
        "click_point": click_point,
        "before_screenshot_path": row["screenshot_path"],
        "after_screenshot_path": capture["screenshot_path"],
        "after_capture": capture,
    }
    store_click_verification(conn, result)
    return result


def click_verification_passed(capture: dict[str, Any], *, expected_page_id: str) -> bool:
    if expected_page_id == "popup_close":
        return capture.get("overlay_state") == "none" and capture.get("page_state") != "unknown"
    return bool(capture["anchors"].get("expected_page"))


def store_click_verification(conn: sqlite3.Connection, result: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO ui_click_verifications (
            verified_at, page_id, element_id, observation_id,
            expected_page_id, passed, blocked, block_reason,
            click_point_json, before_screenshot_path, after_screenshot_path, result_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            result["page_id"],
            result["element_id"],
            int(result["observation_id"]),
            result["expected_page_id"],
            1 if result["passed"] else 0,
            1 if result["blocked"] else 0,
            result.get("block_reason"),
            json_dumps(result["click_point"]),
            result["before_screenshot_path"],
            result.get("after_screenshot_path"),
            json_dumps(result),
        ),
    )
    conn.commit()


def import_screenshot(
    conn: sqlite3.Connection,
    *,
    page_id: str,
    image_path: Path,
    window_rect: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    items = run_vision_ocr(image_path)
    return store_page_capture(
        conn,
        page_id=page_id,
        page_name=page_id,
        image_path=image_path,
        items=items,
        source="manual_screenshot_import",
        capture_mode="manual_import",
        frontmost_process=None,
        window_rect=window_rect,
    )


def list_pages(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT p.page_id, p.page_name, p.page_state, p.overlay_state, p.account_mode, p.hierarchy_code,
               p.executable, p.updated_at, COUNT(o.observation_id) AS observations
        FROM ui_pages p
        LEFT JOIN ui_observations o ON o.page_id = p.page_id
        GROUP BY p.page_id
        ORDER BY p.updated_at DESC, p.page_id ASC
        """
    ).fetchall()
    return [
        {
            "page_id": row["page_id"],
            "page_name": row["page_name"],
            "page_state": row["page_state"],
            "overlay_state": row["overlay_state"],
            "account_mode": row["account_mode"],
            "hierarchy_code": row["hierarchy_code"],
            "executable": bool(row["executable"]),
            "updated_at": row["updated_at"],
            "observations": row["observations"],
        }
        for row in rows
    ]


def export_map(conn: sqlite3.Connection, output_path: Path) -> dict[str, Any]:
    pages = [dict(row) for row in conn.execute("SELECT * FROM ui_pages ORDER BY page_id")]
    elements = [dict(row) for row in conn.execute("SELECT * FROM ui_elements ORDER BY page_id, hierarchy_code")]
    transitions = [dict(row) for row in conn.execute("SELECT * FROM ui_transitions ORDER BY from_page_id, action_id")]
    observations = [
        dict(row)
        for row in conn.execute(
            """
            SELECT observation_id, page_id, element_id, observed_at, source,
                   trusted_click, screenshot_path, capture_mode, frontmost_process,
                   window_rect_json, image_size_json, ocr_text, confidence,
                   vision_box_json, pixel_center_json, click_point_json,
                   content_offset_px_json, scale_x, scale_y
            FROM ui_observations
            ORDER BY observation_id
            """
        )
    ]
    click_verifications = [
        dict(row)
        for row in conn.execute(
            """
            SELECT verification_id, verified_at, page_id, element_id,
                   observation_id, expected_page_id, passed, blocked,
                   block_reason, click_point_json, before_screenshot_path,
                   after_screenshot_path, result_json
            FROM ui_click_verifications
            ORDER BY verification_id
            """
        )
    ]
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "database_path": conn.execute("PRAGMA database_list").fetchone()["file"],
        "pages": pages,
        "elements": elements,
        "transitions": transitions,
        "observations": observations,
        "click_verifications": click_verifications,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"output_path": str(output_path), "pages": len(pages), "elements": len(elements), "observations": len(observations)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a read-only THS UI map SQLite database.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--capture-page", choices=sorted(KNOWN_PAGE_PREFIXES), help="Capture current app screen as this page.")
    parser.add_argument("--import-screenshot", type=Path, help="Import a manual screenshot and run OCR.")
    parser.add_argument("--page", choices=sorted(KNOWN_PAGE_PREFIXES), help="Page id for --import-screenshot.")
    parser.add_argument("--window-rect", help="Optional x,y,width,height; makes imported screenshot click coordinates trusted.")
    parser.add_argument("--list-pages", action="store_true")
    parser.add_argument("--export-json", type=Path)
    parser.add_argument("--verify-click-text", help="Click latest trusted OCR coordinate matching this text, then OCR-check --expect-page.")
    parser.add_argument("--verify-click-element", help="Click latest trusted coordinate for this element_id, then OCR-check --expect-page.")
    parser.add_argument("--from-page", choices=sorted(KNOWN_PAGE_PREFIXES), help="Source page for --verify-click-text.")
    parser.add_argument("--expect-page", choices=EXPECTED_PAGE_CHOICES, help="Expected page/state after click verification.")
    parser.add_argument("--wait-seconds", type=float, default=1.0)
    parser.add_argument("--app-name", default="同花顺")
    parser.add_argument("--bundle-id", default="cn.com.10jqka.macstock")
    parser.add_argument("--process-name", default="同花顺")
    args = parser.parse_args()

    conn = connect_db(args.db)
    if args.capture_page:
        result = capture_page(
            conn,
            page_id=args.capture_page,
            app_name=args.app_name,
            bundle_id=args.bundle_id,
            process_name=args.process_name,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.import_screenshot:
        if not args.page:
            raise SystemExit("--import-screenshot requires --page")
        result = import_screenshot(
            conn,
            page_id=args.page,
            image_path=args.import_screenshot,
            window_rect=parse_rect(args.window_rect),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.list_pages:
        print(json.dumps(list_pages(conn), ensure_ascii=False, indent=2))
        return 0
    if args.export_json:
        print(json.dumps(export_map(conn, args.export_json), ensure_ascii=False, indent=2))
        return 0
    if args.verify_click_text or args.verify_click_element:
        if not args.expect_page:
            raise SystemExit("--verify-click requires --expect-page")
        if args.verify_click_text and not args.from_page:
            raise SystemExit("--verify-click-text requires --from-page")
        result = verify_click_coordinate(
            conn,
            page_id=args.from_page or "",
            expected_page_id=args.expect_page,
            app_name=args.app_name,
            bundle_id=args.bundle_id,
            process_name=args.process_name,
            element_id=args.verify_click_element,
            text=args.verify_click_text,
            wait_seconds=args.wait_seconds,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
