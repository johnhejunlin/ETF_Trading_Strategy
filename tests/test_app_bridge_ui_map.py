import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import AppBridge_UIMap as ui_map
from apple_account_snapshot import OcrText


class AppBridgeUIMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "app_ui_map.sqlite3"
        self.image_path = self.root / "screen.png"
        Image.new("RGBA", (1800, 2382), (255, 255, 255, 255)).save(self.image_path)
        self.conn = ui_map.connect_db(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def test_store_page_capture_initializes_pages_elements_and_observations(self) -> None:
        items = [
            OcrText("模拟交易", 1.0, 0.10, 0.90, 0.08, 0.02),
            OcrText("持仓", 0.98, 0.65, 0.82, 0.04, 0.02),
        ]

        result = ui_map.store_page_capture(
            self.conn,
            page_id="holdings",
            page_name="holdings",
            image_path=self.image_path,
            items=items,
            source="test",
            capture_mode="unit",
            frontmost_process="同花顺",
            window_rect=(0, 39, 788, 1079),
        )

        self.assertEqual(result["page_id"], "holdings")
        self.assertEqual(result["page_state"], "holdings")
        self.assertEqual(result["overlay_state"], "none")
        self.assertEqual(result["account_mode"], "simulation")
        self.assertTrue(result["executable"])
        self.assertTrue(result["trusted_click"])
        self.assertEqual(result["ocr_items"], 2)

        page = self.conn.execute("SELECT * FROM ui_pages WHERE page_id='holdings'").fetchone()
        self.assertEqual(page["hierarchy_code"], "L1F2.L2F5.L3F4")
        self.assertEqual(page["page_state"], "holdings")
        self.assertEqual(page["overlay_state"], "none")
        self.assertEqual(page["executable"], 1)

        observation = self.conn.execute("SELECT * FROM ui_observations WHERE ocr_text='持仓'").fetchone()
        self.assertEqual(observation["trusted_click"], 1)
        self.assertIsNotNone(observation["click_point_json"])

    def test_manual_import_without_window_rect_does_not_trust_click_coordinates(self) -> None:
        items = [OcrText("买入", 0.9, 0.1, 0.8, 0.04, 0.02)]

        result = ui_map.store_page_capture(
            self.conn,
            page_id="buy_form",
            page_name="buy_form",
            image_path=self.image_path,
            items=items,
            source="manual_screenshot_import",
            capture_mode="manual_import",
        )

        self.assertFalse(result["trusted_click"])
        observation = self.conn.execute("SELECT * FROM ui_observations").fetchone()
        self.assertEqual(observation["trusted_click"], 0)
        self.assertIsNone(observation["click_point_json"])

    def test_export_map_writes_json_payload(self) -> None:
        items = [OcrText("模拟交易", 1.0, 0.10, 0.90, 0.08, 0.02)]
        ui_map.store_page_capture(
            self.conn,
            page_id="simulation",
            page_name="simulation",
            image_path=self.image_path,
            items=items,
            source="test",
        )
        output = self.root / "ui_map_export.json"

        result = ui_map.export_map(self.conn, output)

        self.assertEqual(result["pages"], 1)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["pages"][0]["page_id"], "simulation")
        self.assertGreaterEqual(len(payload["transitions"]), 1)

    def test_ad_popup_marks_trade_capture_as_overlay_not_expected_page(self) -> None:
        items = [
            OcrText("模拟交易", 1.0, 0.10, 0.90, 0.08, 0.02),
            OcrText("买入", 0.9, 0.20, 0.80, 0.04, 0.02),
            OcrText("八强赛火热开踢", 0.9, 0.40, 0.45, 0.20, 0.04),
            OcrText("立即参与", 0.9, 0.45, 0.20, 0.10, 0.03),
        ]

        result = ui_map.store_page_capture(
            self.conn,
            page_id="trade",
            page_name="trade",
            image_path=self.image_path,
            items=items,
            source="test",
            window_rect=(0, 39, 788, 1079),
        )

        self.assertEqual(result["page_state"], "trade")
        self.assertEqual(result["overlay_state"], "popup_ad")
        self.assertFalse(result["anchors"]["expected_page"])
        self.assertFalse(result["executable"])
        self.assertIn("close_overlay", result["safe_actions"])

    def test_ad_webview_is_not_trade_even_when_captured_as_trade(self) -> None:
        items = [
            OcrText("在同花顺 看世界杯", 1.0, 0.10, 0.92, 0.30, 0.02),
            OcrText("选择支持的球队", 0.9, 0.25, 0.70, 0.20, 0.04),
            OcrText("抽大奖", 0.9, 0.70, 0.20, 0.12, 0.03),
        ]

        result = ui_map.store_page_capture(
            self.conn,
            page_id="trade",
            page_name="trade",
            image_path=self.image_path,
            items=items,
            source="test",
            window_rect=(0, 39, 788, 1079),
        )

        self.assertEqual(result["page_state"], "ad_webview")
        self.assertEqual(result["overlay_state"], "none")
        self.assertFalse(result["anchors"]["expected_page"])
        self.assertIn("back", result["safe_actions"])

    def test_capture_page_uses_read_only_capture_pipeline(self) -> None:
        def fake_capture(path, _window_id, _rect):
            Image.new("RGBA", (1800, 2382), (255, 255, 255, 255)).save(path)
            return "window_id:123"

        with (
            mock.patch.object(ui_map, "activate_app") as activate,
            mock.patch.object(ui_map, "get_window_rect", return_value=(0, 39, 788, 1079)) as rect,
            mock.patch.object(ui_map, "get_coregraphics_window_id", return_value=123) as window_id,
            mock.patch.object(ui_map, "capture_screenshot", side_effect=fake_capture) as capture,
            mock.patch.object(ui_map, "run_vision_ocr", return_value=[OcrText("模拟交易", 1.0, 0.1, 0.9, 0.08, 0.02)]) as ocr,
            mock.patch.object(ui_map, "frontmost_process_name", return_value="同花顺"),
        ):
            result = ui_map.capture_page(
                self.conn,
                page_id="simulation",
                app_name="同花顺",
                bundle_id="cn.com.10jqka.macstock",
                process_name="同花顺",
            )

        activate.assert_called_once()
        rect.assert_called_once()
        window_id.assert_called_once()
        capture.assert_called_once()
        ocr.assert_called_once()
        self.assertEqual(result["account_mode"], "simulation")

    def test_verify_click_coordinate_clicks_and_records_result(self) -> None:
        items = [OcrText("交易", 1.0, 0.50, 0.08, 0.04, 0.02)]
        ui_map.store_page_capture(
            self.conn,
            page_id="home",
            page_name="home",
            image_path=self.image_path,
            items=items,
            source="test",
            window_rect=(0, 39, 788, 1079),
        )
        after_capture = {
            "page_id": "trade",
            "page_name": "trade",
            "page_state": "trade",
            "overlay_state": "none",
            "account_mode": "unknown",
            "executable": False,
            "anchors": {"expected_page": True},
            "screenshot_path": str(self.root / "after.png"),
            "ocr_items": 1,
            "trusted_click": True,
            "database_path": str(self.db_path),
        }

        with (
            mock.patch.object(ui_map, "perform_safe_click") as click,
            mock.patch.object(ui_map, "capture_page", return_value=after_capture) as capture,
        ):
            result = ui_map.verify_click_coordinate(
                self.conn,
                page_id="home",
                expected_page_id="trade",
                app_name="同花顺",
                bundle_id="cn.com.10jqka.macstock",
                process_name="同花顺",
                text="交易",
                wait_seconds=0,
            )

        click.assert_called_once()
        capture.assert_called_once()
        self.assertTrue(result["passed"])
        row = self.conn.execute("SELECT * FROM ui_click_verifications").fetchone()
        self.assertEqual(row["passed"], 1)
        self.assertEqual(row["blocked"], 0)

    def test_popup_close_verification_requires_overlay_to_disappear(self) -> None:
        self.assertTrue(
            ui_map.click_verification_passed(
                {"page_state": "trade", "overlay_state": "none", "anchors": {"expected_page": False}},
                expected_page_id="popup_close",
            )
        )
        self.assertTrue(
            ui_map.click_verification_passed(
                {"page_state": "ad_webview", "overlay_state": "none", "anchors": {"expected_page": False}},
                expected_page_id="popup_close",
            )
        )
        self.assertFalse(
            ui_map.click_verification_passed(
                {"page_state": "trade", "overlay_state": "popup_ad", "anchors": {"expected_page": False}},
                expected_page_id="popup_close",
            )
        )
        self.assertFalse(
            ui_map.click_verification_passed(
                {"page_state": "unknown", "overlay_state": "none", "anchors": {"expected_page": False}},
                expected_page_id="popup_close",
            )
        )

    def test_verify_click_coordinate_blocks_high_risk_text(self) -> None:
        items = [OcrText("确认买入", 1.0, 0.50, 0.50, 0.08, 0.02)]
        ui_map.store_page_capture(
            self.conn,
            page_id="buy_form",
            page_name="buy_form",
            image_path=self.image_path,
            items=items,
            source="test",
            window_rect=(0, 39, 788, 1079),
        )

        with (
            mock.patch.object(ui_map, "perform_safe_click") as click,
            mock.patch.object(ui_map, "capture_page") as capture,
        ):
            result = ui_map.verify_click_coordinate(
                self.conn,
                page_id="buy_form",
                expected_page_id="buy_form",
                app_name="同花顺",
                bundle_id="cn.com.10jqka.macstock",
                process_name="同花顺",
                text="确认买入",
                wait_seconds=0,
            )

        click.assert_not_called()
        capture.assert_not_called()
        self.assertFalse(result["passed"])
        self.assertTrue(result["blocked"])
        row = self.conn.execute("SELECT * FROM ui_click_verifications").fetchone()
        self.assertEqual(row["blocked"], 1)


if __name__ == "__main__":
    unittest.main()
