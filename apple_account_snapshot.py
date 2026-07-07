#!/usr/bin/env python3
"""Create a read-only account snapshot from the THS window using macOS tools.

This is a diagnostic bridge only. It captures the THS window or screen, runs
Apple Vision OCR, and writes a JSON snapshot with source=apple_vision_ocr.
It does not click or submit orders.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SCREENSHOTS = ROOT / "screenshots"
DEFAULT_LATEST = SCREENSHOTS / "latest_account_snapshot.json"
DEFAULT_OCR = SCREENSHOTS / "latest_account_ocr.json"
SNAPSHOT_SOURCE = "apple_vision_ocr"


SWIFT_OCR = r'''
import Foundation
import Vision
import CoreGraphics
import ImageIO

struct Observation: Codable {
    let text: String
    let confidence: Float
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    fputs("usage: ocr.swift image_path\n", stderr)
    exit(2)
}

let url = URL(fileURLWithPath: args[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fputs("failed to load image\n", stderr)
    exit(1)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.minimumTextHeight = 0.008

let handler = VNImageRequestHandler(cgImage: image, options: [:])
try handler.perform([request])

let observations = (request.results ?? []).compactMap { item -> Observation? in
    guard let candidate = item.topCandidates(1).first else { return nil }
    let box = item.boundingBox
    return Observation(
        text: candidate.string,
        confidence: candidate.confidence,
        x: box.origin.x,
        y: box.origin.y,
        width: box.size.width,
        height: box.size.height
    )
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
let data = try encoder.encode(observations)
FileHandle.standardOutput.write(data)
'''


SWIFT_WINDOWS = r'''
import Foundation
import CoreGraphics

struct WindowInfo: Codable {
    let window_id: Int
    let owner_name: String
    let window_name: String
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let is_onscreen: Bool
}

let windows = (CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]] ?? []).compactMap { item -> WindowInfo? in
    guard let number = item[kCGWindowNumber as String] as? Int,
          let owner = item[kCGWindowOwnerName as String] as? String,
          let bounds = item[kCGWindowBounds as String] as? [String: Any] else {
        return nil
    }
    let name = item[kCGWindowName as String] as? String ?? ""
    let onscreen = (item[kCGWindowIsOnscreen as String] as? Int ?? 0) == 1
    let x = bounds["X"] as? Double ?? 0
    let y = bounds["Y"] as? Double ?? 0
    let width = bounds["Width"] as? Double ?? 0
    let height = bounds["Height"] as? Double ?? 0
    return WindowInfo(window_id: number, owner_name: owner, window_name: name, x: x, y: y, width: width, height: height, is_onscreen: onscreen)
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
let data = try encoder.encode(windows)
FileHandle.standardOutput.write(data)
'''


@dataclass
class OcrText:
    text: str
    confidence: float
    x: float
    y: float
    width: float
    height: float


def run(command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)


def activate_app(app_name: str, bundle_id: str | None) -> None:
    scripts = []
    if bundle_id:
        scripts.append(f'tell application id "{bundle_id}" to activate')
    scripts.append(f'tell application "{app_name}" to activate')
    for script in scripts:
        try:
            run(["osascript", "-e", script], timeout=5)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue


def frontmost_process_name() -> str | None:
    script = 'tell application "System Events" to get name of first process whose frontmost is true'
    try:
        return run(["osascript", "-e", script], timeout=5).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def get_window_rect(process_name: str) -> tuple[int, int, int, int] | None:
    script = (
        'tell application "System Events"\n'
        f'  tell process "{process_name}"\n'
        '    set frontmost to true\n'
        '    set p to position of window 1\n'
        '    set s to size of window 1\n'
        '    return (item 1 of p as text) & "," & (item 2 of p as text) & "," & '
        '(item 1 of s as text) & "," & (item 2 of s as text)\n'
        '  end tell\n'
        'end tell'
    )
    try:
        output = run(["osascript", "-e", script], timeout=8).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    parts = [int(float(part.strip())) for part in output.split(",") if part.strip()]
    if len(parts) != 4:
        return None
    x, y, width, height = parts
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def get_coregraphics_window_id(owner_hint: str, title_hint: str | None) -> int | None:
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
    windows = json.loads(output)
    candidates = []
    for window in windows:
        owner = str(window.get("owner_name", ""))
        title = str(window.get("window_name", ""))
        width = float(window.get("width", 0))
        height = float(window.get("height", 0))
        if width < 400 or height < 300:
            continue
        if owner_hint not in owner and (not title_hint or title_hint not in title):
            continue
        score = width * height
        if window.get("is_onscreen"):
            score *= 2
        if title_hint and title_hint in title:
            score *= 2
        candidates.append((score, int(window["window_id"])))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def capture_screenshot(path: Path, window_id: int | None, rect: tuple[int, int, int, int] | None) -> str:
    screencapture = shutil.which("screencapture")
    if not screencapture:
        raise RuntimeError("macOS screencapture not found")
    if window_id is not None:
        run([screencapture, "-x", "-l", str(window_id), str(path)], timeout=10)
        return f"window_id:{window_id}"
    if rect:
        x, y, width, height = rect
        run([screencapture, "-x", "-R", f"{x},{y},{width},{height}", str(path)], timeout=10)
        return f"window:{x},{y},{width},{height}"
    run([screencapture, "-x", str(path)], timeout=10)
    return "fullscreen"


def run_vision_ocr(image_path: Path) -> list[OcrText]:
    swift = shutil.which("swift")
    if not swift:
        raise RuntimeError("Swift runtime not found; Apple Vision OCR cannot run")
    with tempfile.TemporaryDirectory() as tempdir:
        swift_path = Path(tempdir) / "vision_ocr.swift"
        swift_path.write_text(SWIFT_OCR, encoding="utf-8")
        output = run([swift, str(swift_path), str(image_path)], timeout=40).stdout
    payload = json.loads(output)
    return [OcrText(**item) for item in payload]


def parse_money(value: str) -> float | None:
    cleaned = value.replace(",", "").replace("，", "").replace(" ", "")
    cleaned = cleaned.replace("−", "-").replace("—", "-")
    try:
        return float(cleaned)
    except ValueError:
        return None


NUMBER_RE = re.compile(r"[-−]?\d{1,3}(?:[,，]\d{3})*(?:\.\d+)?|[-−]?\d+(?:\.\d+)?")


def number_after_label(raw_text: str, labels: list[str], limit: int = 80) -> float | None:
    for label in labels:
        index = raw_text.find(label)
        if index < 0:
            continue
        chunk = raw_text[index + len(label) : index + len(label) + limit]
        match = NUMBER_RE.search(chunk)
        if match:
            value = parse_money(match.group(0))
            if value is not None:
                return value
    return None


def first_number(text: str) -> float | None:
    match = NUMBER_RE.search(text)
    if not match:
        return None
    return parse_money(match.group(0))


def text_matches_label(text: str, label: str) -> bool:
    normalized = text.replace(" ", "")
    if label == "可用":
        return normalized.startswith("可用") and "/" not in normalized
    if label == "总市值":
        return normalized.startswith("总市值")
    if label == "总资产":
        return normalized.startswith("总资产")
    if label == "总盈亏":
        return normalized.startswith("总盈亏")
    return label in normalized


def value_below_label(
    items: list[OcrText],
    labels: list[str],
    *,
    max_dx: float = 0.08,
    max_dy: float = 0.08,
    min_label_y: float | None = None,
) -> float | None:
    labels_found = [
        item
        for item in items
        if (min_label_y is None or item.y >= min_label_y) and any(text_matches_label(item.text, label) for label in labels)
    ]
    if not labels_found:
        return None
    candidates: list[tuple[float, float]] = []
    for label in labels_found:
        label_x = label.x + label.width / 2
        for item in items:
            value = first_number(item.text)
            if value is None:
                continue
            item_x = item.x + item.width / 2
            dy = label.y - item.y
            dx = abs(label_x - item_x)
            if 0 < dy <= max_dy and dx <= max_dx:
                candidates.append((dy + dx, value))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def normalize_ocr_text(items: list[OcrText]) -> list[OcrText]:
    return sorted(items, key=lambda item: (-item.y, item.x))


def column_values(items: list[OcrText], labels: list[str], *, max_dx: float = 0.06, max_dy: float = 0.06) -> list[float]:
    headers = [item for item in items if any(label in item.text for label in labels)]
    values: list[tuple[float, float]] = []
    for header in headers:
        header_x = header.x + header.width / 2
        for item in items:
            value = first_number(item.text)
            if value is None:
                continue
            item_x = item.x + item.width / 2
            dy = header.y - item.y
            if 0 < dy <= max_dy and abs(header_x - item_x) <= max_dx:
                values.append((item.y, value))
    values.sort(reverse=True)
    return [value for _, value in values]


def parse_position(raw_text: str, symbol: str, items: list[OcrText]) -> dict[str, Any] | None:
    if symbol not in raw_text:
        return None
    symbol_items = [item for item in items if symbol in item.text]
    symbol_y = symbol_items[0].y if symbol_items else None
    row_items = [
        item.text
        for item in items
        if symbol_y is not None and abs(item.y - symbol_y) <= 0.04
    ]

    market_values = column_values(items, ["市值"])
    pnl_values = column_values(items, ["盈亏"])
    quantity_values = column_values(items, ["持仓/可用"])
    cost_price_values = column_values(items, ["成本/现价"])
    position_ratio_values = column_values(items, ["个股仓位"])

    position: dict[str, Any] = {
        "symbol": symbol,
        "source": SNAPSHOT_SOURCE,
        "raw_position_text": " || ".join(row_items),
    }
    if quantity_values:
        position["quantity"] = int(quantity_values[0])
        position["sellable_quantity"] = int(quantity_values[1]) if len(quantity_values) > 1 else int(quantity_values[0])
        position["available_quantity"] = position["sellable_quantity"]
    if len(cost_price_values) >= 2:
        position["avg_cost"] = cost_price_values[0]
        position["current_price"] = cost_price_values[1]
    elif cost_price_values:
        position["current_price"] = cost_price_values[0]
    if market_values:
        position["market_value"] = market_values[0]
    if pnl_values:
        position["profit_loss"] = pnl_values[0]
    if position_ratio_values:
        position["position_ratio"] = position_ratio_values[0] / 100.0
    return position


def validate_snapshot(snapshot: dict[str, Any], symbol: str) -> list[str]:
    errors: list[str] = []
    anchors = snapshot.get("anchors", {})
    missing_anchors = [key for key in ("simulation", "total_assets", "available_cash", "position_area", "target_symbol") if not anchors.get(key)]
    if missing_anchors:
        errors.append("missing anchors: " + ", ".join(missing_anchors))
    if snapshot.get("account_mode") != "simulation":
        errors.append("account_mode is not simulation")
    if snapshot.get("source") != SNAPSHOT_SOURCE:
        errors.append("unexpected snapshot source")

    total_assets = snapshot.get("total_assets")
    available_cash = snapshot.get("available_cash")
    cash_balance = snapshot.get("cash_balance")
    market_value = snapshot.get("market_value")
    for field, value in (
        ("total_assets", total_assets),
        ("available_cash/cash_balance", available_cash if available_cash is not None else cash_balance),
        ("market_value", market_value),
    ):
        if not isinstance(value, (int, float)):
            errors.append(f"{field} is missing")
    if isinstance(total_assets, (int, float)) and isinstance(market_value, (int, float)):
        cash = available_cash if isinstance(available_cash, (int, float)) else cash_balance
        if isinstance(cash, (int, float)) and abs(total_assets - cash - market_value) > max(5.0, abs(total_assets) * 0.01):
            errors.append("total_assets is not close to cash + market_value")

    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        errors.append("positions is not a list")
        return errors
    position = next((item for item in positions if isinstance(item, dict) and item.get("symbol") == symbol), None)
    if position is None:
        errors.append(f"target position {symbol} is missing")
        return errors
    for field in ("quantity", "sellable_quantity", "avg_cost", "current_price", "market_value"):
        if not isinstance(position.get(field), (int, float)):
            errors.append(f"position.{field} is missing")
    quantity = position.get("quantity")
    current_price = position.get("current_price")
    position_market_value = position.get("market_value")
    if all(isinstance(value, (int, float)) for value in (quantity, current_price, position_market_value)):
        implied_value = float(quantity) * float(current_price)
        if abs(implied_value - float(position_market_value)) > max(3.0, abs(float(position_market_value)) * 0.03):
            errors.append("position market_value is not close to quantity * current_price")
    if isinstance(quantity, (int, float)) and (int(quantity) <= 0 or int(quantity) % 100 != 0):
        errors.append("position.quantity is not a positive 100-share lot")
    return errors


def build_snapshot(
    items: list[OcrText],
    symbol: str,
    image_path: Path,
    capture_mode: str,
    frontmost_process: str | None,
) -> dict[str, Any]:
    ordered = normalize_ocr_text(items)
    raw_text = " || ".join(item.text for item in ordered)
    total_assets = value_below_label(ordered, ["总资产"], min_label_y=0.75) or number_after_label(raw_text, ["总资产"])
    available_cash = value_below_label(ordered, ["可用资金", "可用"], min_label_y=0.72) or number_after_label(raw_text, ["可用资金", "可用"])
    cash_balance = value_below_label(ordered, ["资金余额", "余额"]) or number_after_label(raw_text, ["资金余额", "余额"])
    market_value = value_below_label(ordered, ["总市值"], min_label_y=0.72) or number_after_label(raw_text, ["总市值"])
    profit_loss = value_below_label(ordered, ["总盈亏"], min_label_y=0.75) or number_after_label(raw_text, ["总盈亏"])
    position = parse_position(raw_text, symbol, ordered)

    anchors = {
        "simulation": any(key in raw_text for key in ["模拟炒股", "模拟账户", "模拟交易", "大玩家"]),
        "total_assets": total_assets is not None,
        "available_cash": available_cash is not None or cash_balance is not None,
        "position_area": any(key in raw_text for key in ["持仓股", "持仓"]),
        "target_symbol": symbol in raw_text,
    }
    warnings: list[str] = []
    if total_assets is not None and market_value is not None and (available_cash or cash_balance):
        cash = available_cash if available_cash is not None else cash_balance
        assert cash is not None
        if abs(total_assets - cash - market_value) > max(5.0, total_assets * 0.01):
            warnings.append("total_assets is not close to available_cash + market_value")
    missing = [key for key, ok in anchors.items() if not ok]
    if missing:
        warnings.append("missing anchors: " + ", ".join(missing))

    snapshot: dict[str, Any] = {
        "account_mode": "simulation" if anchors["simulation"] else "unknown",
        "total_assets": total_assets,
        "available_cash": available_cash,
        "cash_balance": cash_balance if cash_balance is not None else available_cash,
        "market_value": market_value,
        "profit_loss": profit_loss,
        "positions": [position] if position else [],
        "source": SNAPSHOT_SOURCE,
        "submitted": False,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "frontmost_process": frontmost_process,
        "capture_mode": capture_mode,
        "screenshot_path": str(image_path),
        "anchors": anchors,
        "warnings": warnings,
        "raw_ui_text": raw_text,
    }
    validation_errors = validate_snapshot(snapshot, symbol)
    if validation_errors:
        snapshot["validation_errors"] = validation_errors
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Read THS simulated account snapshot with Apple Vision OCR")
    parser.add_argument("--app-name", default="同花顺至尊版")
    parser.add_argument("--bundle-id", default="cn.com.10jqka.iHexinFee")
    parser.add_argument("--process-name", default="EQHexinFee")
    parser.add_argument("--symbol", default="588330")
    parser.add_argument("--write-latest", action="store_true", help="also write screenshots/latest_account_snapshot.json")
    parser.add_argument("--output", type=Path, default=None, help="diagnostic JSON output path")
    parser.add_argument("--no-activate", action="store_true")
    args = parser.parse_args()

    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    image_path = SCREENSHOTS / f"apple_account_snapshot_{timestamp}.png"
    output_path = args.output or SCREENSHOTS / f"apple_account_snapshot_{timestamp}.json"

    if not args.no_activate:
        activate_app(args.app_name, args.bundle_id)
        time.sleep(1.0)
    frontmost = frontmost_process_name()
    window_id = get_coregraphics_window_id("同花顺", args.app_name)
    rect = get_window_rect(args.process_name)
    capture_mode = capture_screenshot(image_path, window_id, rect)
    ocr_items = run_vision_ocr(image_path)
    snapshot = build_snapshot(ocr_items, args.symbol, image_path, capture_mode, frontmost)

    diagnostic = {
        "snapshot": snapshot,
        "ocr_items": [asdict(item) for item in normalize_ocr_text(ocr_items)],
    }
    output_path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
    DEFAULT_OCR.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
    validation_errors = snapshot.get("validation_errors", [])
    if args.write_latest and validation_errors:
        print("Refusing to write latest snapshot because validation failed: " + "; ".join(validation_errors), file=sys.stderr)
    elif args.write_latest:
        DEFAULT_LATEST.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    if snapshot.get("warnings") or validation_errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
