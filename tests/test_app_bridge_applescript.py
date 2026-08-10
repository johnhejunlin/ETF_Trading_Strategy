import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import AppBridge_AppleScript as bridge
from AppBridge_OCRPositionCalculation import ImageSize, WindowRect, infer_capture_scale_and_offset, infer_symmetric_content_offset


class AppBridgeAppleScriptTests(unittest.TestCase):
    def test_login_page_detection_requires_login_markers(self) -> None:
        login_items = [bridge.OcrText("密码登录 记住密码 正在登录", 1.0, 0, 0, 1, 1)]
        home_items = [bridge.OcrText("自选 行情 交易", 1.0, 0, 0, 1, 1)]

        self.assertTrue(bridge.is_login_page(login_items))
        self.assertFalse(bridge.is_login_page(home_items))

    def test_home_page_news_does_not_count_as_trade_page(self) -> None:
        items = [bridge.OcrText("自选 行情 交易 大笔买入 大笔卖出", 1.0, 0, 0, 1, 1)]

        self.assertFalse(bridge.is_trade_page(items))

    def test_desktop_trade_workspace_is_detected(self) -> None:
        items = [bridge.OcrText("A股 模拟 买入 卖出 持仓 资金明细 确定买入", 1.0, 0, 0, 1, 1)]

        self.assertTrue(bridge.is_trade_page(items))

    def test_trade_ocr_label_maps_to_desktop_sidebar(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            image = Path(tempdir) / "screen.png"
            Image.new("RGB", (2526, 2484)).save(image)
            item = bridge.OcrText("交易", 1.0, 0.053779, 0.636473, 0.021802, 0.013453)

            with mock.patch.object(bridge, "click_at") as click:
                metadata = bridge.click_ocr_text(
                    image,
                    [item],
                    (0, 39, 1151, 1130),
                    ["交易"],
                    min_rel_x=0.0,
                    max_rel_x=0.08,
                    min_rel_y=0.18,
                    max_rel_y=0.40,
                    offset_y=-18,
                )

        click.assert_called_once_with(25, 408)
        self.assertEqual(metadata["target"], "交易")

    def test_press_sidebar_button_near_ocr_anchor_records_ax_geometry(self) -> None:
        with mock.patch.object(bridge, "run_osascript_output", return_value="4|390|42|42|17"):
            result = bridge.ax_press_sidebar_button_near_point("同花顺", 25, 426)

        self.assertEqual(result["method"], "accessibility_near_ocr_anchor")
        self.assertEqual(result["ocr_anchor"], {"x": 25, "y": 426})
        self.assertEqual(result["distance"], 17)

    def test_press_sidebar_button_near_ocr_anchor_rejects_missing_control(self) -> None:
        with mock.patch.object(bridge, "run_osascript_output", return_value=""):
            with self.assertRaisesRegex(bridge.AppleScriptBridgeError, "sidebar button not found"):
                bridge.ax_press_sidebar_button_near_point("同花顺", 25, 426)

    def test_accessibility_navigation_enters_trade_then_simulation_then_target(self) -> None:
        with (
            mock.patch.object(
                bridge,
                "read_accessibility_text",
                side_effect=["首页 行情 交易", "交易 买入 卖出 持仓"],
            ),
            mock.patch.object(
                bridge,
                "ax_press_named_control",
                side_effect=lambda _process, targets: {"target": targets[0]},
            ) as press,
            mock.patch.object(bridge.time, "sleep"),
        ):
            actions = bridge.navigate_accessibility_to_simulation_page("同花顺", "持仓")

        self.assertEqual([call.args[1] for call in press.call_args_list], [["交易"], ["模拟"], ["持仓"]])
        self.assertEqual([action["target"] for action in actions], ["交易", "模拟", "持仓"])

    def test_accessibility_navigation_skips_trade_and_simulation_when_already_simulated(self) -> None:
        with (
            mock.patch.object(bridge, "read_accessibility_text", return_value="模拟交易 买入 卖出 持仓"),
            mock.patch.object(
                bridge,
                "ax_press_named_control",
                return_value={"target": "持仓"},
            ) as press,
        ):
            bridge.navigate_accessibility_to_simulation_page("同花顺", "持仓")

        press.assert_called_once_with("同花顺", ["持仓"])

    def test_read_accessibility_controls_parses_semantic_controls(self) -> None:
        output = "\n".join(
            [
                "AXStaticText|代码|文本|代码|2071|175|25|23|true",
                "AXTextField|missing value|文本栏|588330|2103|176|138|20|true",
                "AXButton|确定买入|按钮|missing value|2101|325|142|28|true",
            ]
        )

        with mock.patch.object(bridge, "run_osascript_output", return_value=output):
            controls = bridge.read_accessibility_controls("同花顺")

        self.assertEqual(len(controls), 3)
        self.assertEqual(controls[1].role, "AXTextField")
        self.assertEqual(controls[1].value, "588330")
        self.assertTrue(controls[2].can_press)

    def test_read_accessibility_order_fields_maps_fields_by_nearby_labels(self) -> None:
        controls = [
            bridge.AXControl("AXStaticText", "代码", "文本", "代码", 100, 100, 30, 20, True, False),
            bridge.AXControl("AXStaticText", "价格", "文本", "价格", 100, 140, 30, 20, True, False),
            bridge.AXControl("AXStaticText", "数量", "文本", "数量", 100, 180, 30, 20, True, False),
            bridge.AXControl("AXTextField", "", "文本栏", "588330", 150, 101, 100, 20, True, False),
            bridge.AXControl("AXTextField", "", "文本栏", "1.345", 150, 141, 100, 20, True, False),
            bridge.AXControl("AXTextField", "", "文本栏", "100", 150, 181, 100, 20, True, False),
            bridge.AXControl("AXButton", "确定买入", "按钮", "", 150, 220, 100, 20, True, True),
            bridge.AXControl("AXStaticText", "模拟练习", "文本", "模拟练习", 20, 20, 80, 20, True, False),
        ]

        fields = bridge.order_fields_from_controls(controls)

        self.assertEqual(fields["symbol"], "588330")
        self.assertEqual(fields["price"], "1.345")
        self.assertEqual(fields["quantity"], "100")
        self.assertTrue(bridge.is_accessibility_order_form(fields, "BUY"))

    def test_fill_order_form_accessibility_requires_exact_readback(self) -> None:
        fields = {
            "symbol": "588330",
            "price": "1.345",
            "quantity": "100",
            "raw_text": "模拟练习 确定买入",
        }
        with (
            mock.patch.object(
                bridge,
                "ax_set_text_field_near_label",
                side_effect=[
                    {"label": "代码", "value": "588330"},
                    {"label": "价格", "value": "1.345"},
                    {"label": "数量", "value": "100"},
                ],
            ) as set_field,
            mock.patch.object(bridge, "read_accessibility_order_fields", return_value=fields),
        ):
            result = bridge.fill_order_form_accessibility("同花顺", "588330", 100, 1.345)

        self.assertEqual(set_field.call_count, 3)
        self.assertEqual(result["quantity"], "100")

    def test_ordinary_ths_accessibility_style_form_is_accepted_by_ocr_guard(self) -> None:
        items = [
            bridge.OcrText("模拟练习", 1.0, 0, 0, 0, 0),
            bridge.OcrText("代码", 1.0, 0, 0, 0, 0),
            bridge.OcrText("价格", 1.0, 0, 0, 0, 0),
            bridge.OcrText("数量", 1.0, 0, 0, 0, 0),
            bridge.OcrText("确定买入", 1.0, 0, 0, 0, 0),
        ]

        self.assertTrue(bridge.is_simulated_buy_form(items))

    def test_accessibility_success_with_failed_ocr_guard_does_not_use_visual_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            image = Path(tempdir) / "screen.png"
            invalid_items = [bridge.OcrText("普通交易", 1.0, 0, 0, 1, 1)]
            fields = {
                "symbol": "588330",
                "price": "1.345",
                "quantity": "",
                "raw_text": "模拟练习 代码 价格 数量 确定买入",
            }
            with (
                mock.patch.object(bridge, "activate_app"),
                mock.patch.object(bridge.time, "sleep"),
                mock.patch.object(bridge, "get_any_window_rect", return_value=(0, 0, 788, 1079)),
                mock.patch.object(
                    bridge,
                    "capture_ocr",
                    side_effect=[
                        (image, invalid_items, {}),
                        (image, invalid_items, {}),
                    ],
                ),
                mock.patch.object(bridge, "read_accessibility_text", return_value="模拟练习"),
                mock.patch.object(
                    bridge,
                    "ax_press_named_control",
                    return_value={"method": "accessibility"},
                ),
                mock.patch.object(
                    bridge,
                    "read_accessibility_order_fields",
                    return_value=fields,
                ),
                mock.patch.object(bridge, "click_ocr_text") as visual_click,
            ):
                with self.assertRaisesRegex(RuntimeError, "refusing visual fallback"):
                    bridge.navigate_to_order_form(
                        "同花顺",
                        None,
                        "同花顺",
                        "588330",
                        "BUY",
                    )

        visual_click.assert_not_called()

    def test_expected_order_fields_accepts_buy_intent(self) -> None:
        symbol, side, quantity, limit_price = bridge.expected_order_fields(
            {"symbol": "588330", "side": "BUY", "quantity": 100, "limit_price": 1.345}
        )

        self.assertEqual(symbol, "588330")
        self.assertEqual(side, "BUY")
        self.assertEqual(quantity, 100)
        self.assertEqual(limit_price, 1.345)

    def test_expected_order_fields_accepts_sell_intent(self) -> None:
        symbol, side, quantity, limit_price = bridge.expected_order_fields(
            {"symbol": "588330", "side": "SELL", "quantity": 100, "limit_price": 1.345}
        )

        self.assertEqual((symbol, side, quantity, limit_price), ("588330", "SELL", 100, 1.345))

    def test_read_order_intent_accepts_nested_order_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "intent.json"
            path.write_text(
                json.dumps({"submitted_at": "2026-07-07T10:00:00", "order": {"symbol": "588330", "side": "BUY"}}),
                encoding="utf-8",
            )

            order = bridge.read_order_intent(path)

        self.assertEqual(order["symbol"], "588330")
        self.assertEqual(order["side"], "BUY")

    def test_buy_confirm_fields_match_requires_dialog_symbol_quantity_and_price(self) -> None:
        text = "委托买入确认 名称 双创50ETF华宝 代码 588330 数量 100 价格 1.345 金额 134.50 确认买入"

        ok, errors = bridge.buy_confirm_fields_match(text, "588330", 100, 1.345)

        self.assertTrue(ok, errors)

    def test_buy_confirm_fields_match_reports_missing_fields(self) -> None:
        ok, errors = bridge.buy_confirm_fields_match("买入页面 588330", "588330", 100, 1.345)

        self.assertFalse(ok)
        self.assertIn("missing buy confirmation dialog", errors)
        self.assertIn("missing quantity 100", errors)
        self.assertIn("missing price 1.345", errors)

    def test_sell_confirm_fields_match_requires_exact_fields(self) -> None:
        text = "委托卖出确认 代码 588330 数量 100 价格 1.345 确认卖出"

        ok, errors = bridge.sell_confirm_fields_match(text, "588330", 100, 1.345)

        self.assertTrue(ok, errors)

    def test_extract_sellable_quantity(self) -> None:
        self.assertEqual(bridge.extract_sellable_quantity("限价 1.345 可卖 1,200 股 卖出量"), 1200)
        self.assertIsNone(bridge.extract_sellable_quantity("限价 1.345 卖出量"))

    def test_simulated_buy_form_does_not_accept_holdings_page_tabs(self) -> None:
        items = [
            bridge.OcrText("模拟交易", 1.0, 0, 0, 0, 0),
            bridge.OcrText("买入", 1.0, 0, 0, 0, 0),
            bridge.OcrText("卖出", 1.0, 0, 0, 0, 0),
            bridge.OcrText("撤单", 1.0, 0, 0, 0, 0),
            bridge.OcrText("持仓股", 1.0, 0, 0, 0, 0),
            bridge.OcrText("证券代码", 1.0, 0, 0, 0, 0),
        ]

        self.assertFalse(bridge.is_simulated_buy_form(items))

    def test_simulated_buy_form_accepts_buy_inputs(self) -> None:
        items = [
            bridge.OcrText("模拟交易", 1.0, 0, 0, 0, 0),
            bridge.OcrText("股票代码/简拼", 1.0, 0, 0, 0, 0),
            bridge.OcrText("限价", 1.0, 0, 0, 0, 0),
            bridge.OcrText("可买45600股", 1.0, 0, 0, 0, 0),
            bridge.OcrText("买入（模拟账户）", 1.0, 0, 0, 0, 0),
        ]

        self.assertTrue(bridge.is_simulated_buy_form(items))

    def test_simulated_sell_form_requires_sell_markers(self) -> None:
        sell_items = [
            bridge.OcrText("模拟交易", 1.0, 0, 0, 0, 0),
            bridge.OcrText("股票代码/简拼", 1.0, 0, 0, 0, 0),
            bridge.OcrText("限价", 1.0, 0, 0, 0, 0),
            bridge.OcrText("可卖1200股", 1.0, 0, 0, 0, 0),
            bridge.OcrText("卖出（模拟账户）", 1.0, 0, 0, 0, 0),
        ]

        self.assertTrue(bridge.is_simulated_sell_form(sell_items))
        self.assertFalse(bridge.is_simulated_buy_form(sell_items))

    def test_run_sell_order_validates_sellable_quantity_and_confirmation(self) -> None:
        ocr = lambda text: [bridge.OcrText(text, 1.0, 0, 0, 1, 1)]
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            intent = root / "intent.json"
            verification = root / "verification.json"
            image = root / "screen.png"
            intent.write_text(json.dumps({"order": {"symbol": "588330", "side": "SELL", "quantity": 100, "limit_price": 1.345}}))
            form_items = ocr("模拟交易 股票代码 限价 可卖1200股 卖出（模拟账户）")
            confirm_items = ocr("模拟交易 可卖1200股 委托卖出确认 588330 数量100 价格1.345 确认卖出")

            with (
                mock.patch.object(bridge, "activate_app"),
                mock.patch.object(bridge, "get_any_window_rect", return_value=(0, 0, 788, 1079)),
                mock.patch.object(bridge, "navigate_to_order_form", return_value=((0, 0, 788, 1079), image, form_items, [], False)),
                mock.patch.object(bridge, "fill_order_form"),
                mock.patch.object(bridge, "click_ocr_text", return_value={"method": "ocr_text"}),
                mock.patch.object(bridge, "capture_ocr", side_effect=[
                    (image, ocr("模拟交易"), {}),
                    (image, form_items, {}),
                    (image, confirm_items, {}),
                ]),
                mock.patch.object(bridge.time, "sleep"),
            ):
                result = bridge.run_sell_order(
                    app_name="同花顺", bundle_id=None, process_name="同花顺",
                    intent_path=intent, verification_path=verification, submit=False,
                    interaction_mode="visual_only",
                )

        self.assertEqual(result["side"], "SELL")
        self.assertEqual(result["sellable_quantity"], 1200)
        self.assertFalse(result["submitted"])
        self.assertEqual(result["validation_errors"], [])

    def test_run_sell_order_resumes_verified_confirmation_before_submit(self) -> None:
        ocr = lambda text: [bridge.OcrText(text, 1.0, 0, 0, 1, 1)]
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            intent = root / "intent.json"
            verification = root / "verification.json"
            image = root / "screen.png"
            intent.write_text(json.dumps({"order": {"symbol": "588330", "side": "SELL", "quantity": 100, "limit_price": 1.345}}))
            confirm = ocr("模拟交易 可卖1200股 委托卖出确认 588330 数量100 价格1.345 确认卖出")
            receipt = ocr("模拟交易 委托已提交 合同号123")

            with (
                mock.patch.object(bridge, "activate_app"),
                mock.patch.object(bridge, "get_any_window_rect", return_value=(0, 0, 788, 1079)),
                mock.patch.object(bridge, "click_ocr_text", return_value={"method": "ocr_text"}) as click,
                mock.patch.object(bridge, "click_relative"),
                mock.patch.object(bridge, "capture_ocr", side_effect=[(image, confirm, {}), (image, receipt, {})]),
                mock.patch.object(bridge.time, "sleep"),
            ):
                result = bridge.run_sell_order(
                    app_name="同花顺", bundle_id=None, process_name="同花顺",
                    intent_path=intent, verification_path=verification, submit=True,
                    interaction_mode="visual_only",
                )

        self.assertTrue(result["submitted"])
        self.assertEqual(click.call_args.args[3], ["确认卖出", "确定卖出"])

    def test_infer_content_offset_tracks_active_and_inactive_window_captures(self) -> None:
        window = WindowRect(0, 39, 788, 1079)

        active = infer_symmetric_content_offset(window, ImageSize(1800, 2382))
        inactive = infer_symmetric_content_offset(window, ImageSize(1712, 2294))

        self.assertEqual(active.x, 112)
        self.assertEqual(active.y, 112)
        self.assertEqual(inactive.x, 68)
        self.assertEqual(inactive.y, 68)

    def test_ocr_center_to_screen_uses_retina_scaled_screenshot_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            image_path = Path(tempdir) / "capture.png"
            Image.new("RGBA", (1800, 2382), (255, 255, 255, 255)).save(image_path)
            item = bridge.OcrText(
                text="持仓",
                confidence=1.0,
                x=0.657692308038523,
                y=0.892441860522794,
                width=0.026923075781928207,
                height=0.01162790671603997,
            )

            x, y = bridge.ocr_center_to_screen(image_path, item, (0, 39, 788, 1079), target="持仓")

        self.assertEqual(x, 548)
        self.assertEqual(y, 104)

    def test_infer_capture_scale_handles_one_x_window_captures(self) -> None:
        scale_x, scale_y, offset = infer_capture_scale_and_offset(WindowRect(1800, 30, 788, 1079), ImageSize(900, 1191))

        self.assertEqual(scale_x, 1.0)
        self.assertEqual(scale_y, 1.0)
        self.assertEqual(offset.x, 56)
        self.assertEqual(offset.y, 56)

    def test_infer_capture_scale_handles_one_x_inactive_window_captures(self) -> None:
        scale_x, scale_y, offset = infer_capture_scale_and_offset(WindowRect(1800, 30, 788, 1079), ImageSize(856, 1147))

        self.assertEqual(scale_x, 1.0)
        self.assertEqual(scale_y, 1.0)
        self.assertEqual(offset.x, 34)
        self.assertEqual(offset.y, 34)

    def test_infer_capture_scale_still_handles_retina_active_and_inactive_captures(self) -> None:
        window = WindowRect(0, 39, 788, 1079)

        cases = [
            (ImageSize(1800, 2382), 112),
            (ImageSize(1712, 2294), 68),
        ]
        for image_size, expected_offset in cases:
            with self.subTest(image_size=image_size):
                scale_x, scale_y, offset = infer_capture_scale_and_offset(window, image_size)

                self.assertEqual(scale_x, 2.0)
                self.assertEqual(scale_y, 2.0)
                self.assertEqual(offset.x, expected_offset)
                self.assertEqual(offset.y, expected_offset)


if __name__ == "__main__":
    unittest.main()
