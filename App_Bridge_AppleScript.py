#!/usr/bin/env python3
"""Navigate THS to the simulated holdings page with macOS AppleScript tools.

This bridge is intentionally narrow: it opens THS, navigates to simulated
trading holdings, captures the screen, runs Apple Vision OCR, and writes a
verification JSON. It does not place orders.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from apple_account_snapshot import (
    OcrText,
    SWIFT_WINDOWS,
    activate_app,
    build_snapshot,
    capture_screenshot,
    frontmost_process_name,
    get_coregraphics_window_id,
    get_window_rect,
    normalize_ocr_text,
    run,
    run_vision_ocr,
)


ROOT = Path(__file__).resolve().parent
SCREENSHOTS = ROOT / "screenshots"
DEFAULT_OUTPUT = SCREENSHOTS / "latest_applescript_bridge_holdings.json"


class AppleScriptBridgeError(RuntimeError):
    pass


def run_osascript(script: str) -> None:
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        raise AppleScriptBridgeError(f"osascript failed: {message}") from exc


def click_at(x: int, y: int) -> None:
    run_osascript(
        'tell application "同花顺至尊版" to activate\n'
        'delay 0.1\n'
        'tell application "System Events"\n'
        f"  click at {{{x}, {y}}}\n"
        "end tell"
    )


def click_relative(rect: tuple[int, int, int, int], rel_x: float, rel_y: float) -> None:
    x, y, width, height = rect
    click_at(int(x + width * rel_x), int(y + height * rel_y))


def relative_point(rect: tuple[int, int, int, int], rel_x: float, rel_y: float) -> tuple[int, int]:
    x, y, width, height = rect
    return int(x + width * rel_x), int(y + height * rel_y)


def get_any_window_rect(process_name: str, owner_hint: str, title_hint: str) -> tuple[int, int, int, int] | None:
    rect = get_window_rect(process_name)
    if rect:
        return rect
    swift = shutil.which("swift")
    if not swift:
        return None
    with tempfile.TemporaryDirectory() as tempdir:
        swift_path = Path(tempdir) / "window_list.swift"
        swift_path.write_text(SWIFT_WINDOWS, encoding="utf-8")
        try:
            output = run([swift, str(swift_path)], timeout=10).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
    candidates: list[tuple[float, tuple[int, int, int, int]]] = []
    for window in json.loads(output):
        owner = str(window.get("owner_name", ""))
        title = str(window.get("window_name", ""))
        width = float(window.get("width", 0))
        height = float(window.get("height", 0))
        if width < 400 or height < 300:
            continue
        if owner_hint not in owner and title_hint not in title:
            continue
        score = width * height
        if window.get("is_onscreen"):
            score *= 2
        candidates.append(
            (
                score,
                (
                    int(float(window.get("x", 0))),
                    int(float(window.get("y", 0))),
                    int(width),
                    int(height),
                ),
            )
        )
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def ocr_center_to_screen(
    image_path: Path,
    item: OcrText,
    rect: tuple[int, int, int, int],
    *,
    target: str | None = None,
) -> tuple[int, int]:
    with Image.open(image_path) as image:
        image_width, image_height = image.size
    target_fraction = 0.5
    if target and target in item.text and len(item.text) > len(target):
        target_index = item.text.find(target)
        target_fraction = (target_index + len(target) / 2) / max(len(item.text), 1)
    pixel_x = (item.x + item.width * target_fraction) * image_width
    pixel_y = (1 - (item.y + item.height / 2)) * image_height
    shadow_x = max(0, image_width - rect[2]) / 2
    shadow_y = max(0, image_height - rect[3]) / 2
    return int(rect[0] + pixel_x - shadow_x), int(rect[1] + pixel_y - shadow_y)


def item_screen_center(
    image_path: Path,
    item: OcrText,
    rect: tuple[int, int, int, int],
    target: str,
) -> tuple[int, int, float, float]:
    screen_x, screen_y = ocr_center_to_screen(image_path, item, rect, target=target)
    rel_x = (screen_x - rect[0]) / rect[2]
    rel_y = (screen_y - rect[1]) / rect[3]
    return screen_x, screen_y, rel_x, rel_y


def find_ocr_text(
    image_path: Path,
    items: list[OcrText],
    rect: tuple[int, int, int, int],
    targets: list[str],
    *,
    min_rel_y: float = 0.0,
    max_rel_y: float = 1.0,
    min_rel_x: float = 0.0,
    max_rel_x: float = 1.0,
) -> tuple[str, OcrText, int, int] | None:
    candidates: list[tuple[float, str, OcrText, int, int]] = []
    for item in items:
        for target in targets:
            if target not in item.text:
                continue
            screen_x, screen_y, rel_x, rel_y = item_screen_center(image_path, item, rect, target)
            if not (min_rel_x <= rel_x <= max_rel_x and min_rel_y <= rel_y <= max_rel_y):
                continue
            exact_bonus = 0 if item.text.strip() == target else 2.0
            center_bias = abs((min_rel_y + max_rel_y) / 2 - rel_y) + abs((min_rel_x + max_rel_x) / 2 - rel_x)
            candidates.append((exact_bonus + center_bias, target, item, screen_x, screen_y))
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate[0])
    _, target, item, screen_x, screen_y = candidates[0]
    return target, item, screen_x, screen_y


def click_ocr_text(
    image_path: Path,
    items: list[OcrText],
    rect: tuple[int, int, int, int],
    targets: list[str],
    *,
    min_rel_y: float = 0.0,
    max_rel_y: float = 1.0,
    min_rel_x: float = 0.0,
    max_rel_x: float = 1.0,
    offset_x: int = 0,
    offset_y: int = 0,
    fallback: tuple[float, float] | None = None,
) -> dict[str, Any]:
    match = find_ocr_text(
        image_path,
        items,
        rect,
        targets,
        min_rel_y=min_rel_y,
        max_rel_y=max_rel_y,
        min_rel_x=min_rel_x,
        max_rel_x=max_rel_x,
    )
    if match:
        target, item, screen_x, screen_y = match
        screen_x += offset_x
        screen_y += offset_y
        click_at(screen_x, screen_y)
        return {"method": "ocr_text", "target": target, "matched_text": item.text, "x": screen_x, "y": screen_y}
    if fallback:
        screen_x, screen_y = relative_point(rect, fallback[0], fallback[1])
        click_at(screen_x, screen_y)
        return {
            "method": "fallback_relative",
            "target": targets,
            "rel_x": fallback[0],
            "rel_y": fallback[1],
            "x": screen_x,
            "y": screen_y,
        }
    raise RuntimeError(f"OCR text not found: {targets}")


def capture_ocr(
    *,
    app_name: str,
    process_name: str,
    symbol: str,
    label: str,
) -> tuple[Path, list[OcrText], dict[str, Any]]:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    image_path = SCREENSHOTS / f"apple_bridge_navigation_{timestamp}_{label}.png"
    window_id = get_coregraphics_window_id("同花顺", app_name)
    rect = get_any_window_rect(process_name, "同花顺", app_name)
    capture_mode = capture_screenshot(image_path, window_id, rect)
    items = run_vision_ocr(image_path)
    snapshot = build_snapshot(items, symbol, image_path, capture_mode, frontmost_process_name())
    return image_path, items, snapshot


def raw_text(items: list[OcrText]) -> str:
    return " || ".join(item.text for item in normalize_ocr_text(items))


def is_trade_page(items: list[OcrText]) -> bool:
    text = raw_text(items)
    trade_markers = ["模拟交易", "模拟炒股", "买入", "卖出", "撤单", "持仓", "添加账户", "账户管理"]
    return any(key in text for key in trade_markers) and any(key in text for key in ["A股", "模拟", "交易"])


def is_search_page(items: list[OcrText]) -> bool:
    text = raw_text(items)
    return "搜索" in text and any(key in text for key in ["搜索历史", "搜索发现", "识股", "AI问答"])


def close_known_popup(rect: tuple[int, int, int, int], items: list[OcrText]) -> bool:
    text = raw_text(items)
    modal_markers = ["确定", "取消", "我知道", "关闭", "暂不", "以后再说"]
    if not any(marker in text for marker in modal_markers):
        return False
    # Conservative generic close points: center OK first, then top-right dialog area.
    click_relative(rect, 0.50, 0.54)
    time.sleep(0.5)
    return True


def navigate_to_holdings(app_name: str, bundle_id: str | None, process_name: str, symbol: str) -> dict[str, Any]:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    activate_app(app_name, bundle_id)
    time.sleep(1.0)

    rect = get_any_window_rect(process_name, "同花顺", app_name)
    if rect is None:
        raise RuntimeError(f"cannot locate {process_name} window")

    steps: list[dict[str, Any]] = []

    def record(label: str) -> tuple[Path, list[OcrText], dict[str, Any]]:
        image_path, items, snapshot = capture_ocr(
            app_name=app_name,
            process_name=process_name,
            symbol=symbol,
            label=label,
        )
        diagnostic_path = image_path.with_suffix(".json")
        diagnostic = {
            "label": label,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "screenshot_path": str(image_path),
            "snapshot": snapshot,
            "ocr_items": [asdict(item) for item in normalize_ocr_text(items)],
        }
        diagnostic_path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
        steps.append(
            {
                "label": label,
                "screenshot_path": str(image_path),
                "diagnostic_path": str(diagnostic_path),
                "account_mode": snapshot.get("account_mode"),
                "anchors": snapshot.get("anchors", {}),
                "warnings": snapshot.get("warnings", []),
                "validation_errors": snapshot.get("validation_errors", []),
            }
        )
        return image_path, items, snapshot

    image_path, items, _ = record("opened")
    close_known_popup(rect, items)
    if is_search_page(items):
        back_x, back_y = relative_point(rect, 0.030, 0.052)
        click_at(back_x, back_y)
        click_meta = {"method": "fallback_relative", "target": ["<", "＜"], "rel_x": 0.030, "rel_y": 0.052, "x": back_x, "y": back_y}
        steps[-1]["click"] = click_meta
        time.sleep(1.0)
        image_path, items, _ = record("search_back")
        close_known_popup(rect, items)

    # Bottom navigation is tiny and often missed by OCR; use the calibrated bottom tab position.
    trade_x, trade_y = relative_point(rect, 0.728, 0.988)
    click_at(trade_x, trade_y)
    click_meta = {"method": "fallback_relative", "target": ["交易"], "rel_x": 0.728, "rel_y": 0.988, "x": trade_x, "y": trade_y}
    steps[-1]["click"] = click_meta
    time.sleep(1.2)
    image_path, items, _ = record("bottom_trade")
    close_known_popup(rect, items)
    if not is_trade_page(items):
        trade_x, trade_y = relative_point(rect, 0.728, 0.992)
        click_at(trade_x, trade_y)
        click_meta = {
            "method": "fallback_relative",
            "target": ["交易"],
            "rel_x": 0.728,
            "rel_y": 0.992,
            "x": trade_x,
            "y": trade_y,
        }
        steps[-1]["click"] = click_meta
        time.sleep(1.2)
        image_path, items, _ = record("bottom_trade_retry")
        close_known_popup(rect, items)

    if not is_trade_page(items):
        raise RuntimeError("failed to enter THS trade page; refusing to click top navigation blindly")

    # Top title area: simulated trading entry/selector. Skip if already in simulated trading.
    if "模拟交易" not in raw_text(items):
        click_meta = click_ocr_text(
            image_path,
            items,
            rect,
            ["模拟"],
            min_rel_y=0.0,
            max_rel_y=0.14,
            offset_y=16,
            fallback=(0.612, 0.042),
        )
        steps[-1]["click"] = click_meta
        time.sleep(1.5)
        image_path, items, _ = record("top_simulation")
        close_known_popup(rect, items)

    # Simulated trading landing page shortcut: holdings.
    click_meta = click_ocr_text(
        image_path,
        items,
        rect,
        ["持仓", "特仓"],
        min_rel_y=0.18,
        max_rel_y=0.28,
        offset_y=-20,
        fallback=(0.699, 0.230),
    )
    steps[-1]["click"] = click_meta
    time.sleep(1.2)
    image_path, items, snapshot = record("holdings")

    ordered_text = raw_text(items)
    position = next((item for item in snapshot.get("positions", []) if item and item.get("symbol") == symbol), None)
    quantity = position.get("quantity") if isinstance(position, dict) else None
    sellable_quantity = position.get("sellable_quantity") if isinstance(position, dict) else None
    anchors = {
        "simulation": snapshot.get("account_mode") == "simulation"
        or any(key in ordered_text for key in ["模拟交易", "模拟练习区", "梗拟练习区", "模拟"]),
        "holdings_page": "持仓" in ordered_text,
        "target_symbol": symbol in ordered_text,
        "target_position_quantity": isinstance(quantity, int),
    }
    validation_errors = [name for name, ok in anchors.items() if not ok]

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": "applescript_vision_ocr",
        "app": app_name,
        "process_name": process_name,
        "account_mode": "simulation" if anchors["simulation"] else "unknown",
        "page": "holdings" if anchors["holdings_page"] else "unknown",
        "symbol": symbol,
        "quantity": quantity,
        "sellable_quantity": sellable_quantity,
        "position": position,
        "anchors": anchors,
        "validation_errors": validation_errors,
        "steps": steps,
        "raw_ui_text": ordered_text,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Open THS simulated holdings page and verify by OCR")
    parser.add_argument("--app-name", default="同花顺至尊版")
    parser.add_argument("--bundle-id", default="cn.com.10jqka.iHexinFee")
    parser.add_argument("--process-name", default="EQHexinFee")
    parser.add_argument("--symbol", default="588330")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    try:
        result = navigate_to_holdings(args.app_name, args.bundle_id, args.process_name, args.symbol)
    except Exception as exc:
        result = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": "applescript_vision_ocr",
            "app": args.app_name,
            "process_name": args.process_name,
            "account_mode": "unknown",
            "page": "unknown",
            "symbol": args.symbol,
            "quantity": None,
            "sellable_quantity": None,
            "position": None,
            "anchors": {
                "simulation": False,
                "holdings_page": False,
                "target_symbol": False,
                "target_position_quantity": False,
            },
            "validation_errors": ["bridge_runtime_error"],
            "error": str(exc),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["validation_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
