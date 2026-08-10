import unittest
from pathlib import Path

from apple_account_snapshot import OcrText, build_snapshot


def item(text: str, x: float, y: float, width: float = 0.03) -> OcrText:
    return OcrText(text, 1.0, x, y, width, 0.01)


class AppleAccountSnapshotTests(unittest.TestCase):
    def test_ordinary_ths_holdings_table_parses_account_and_position(self) -> None:
        items = [
            item("楧拟练习", 0.06, 0.92),
            item("总资产", 0.06, 0.90),
            item("60783.53", 0.11, 0.90),
            item("总市值", 0.06, 0.88),
            item("123.00", 0.11, 0.88),
            item("总盈亏", 0.06, 0.86),
            item("-18.42", 0.11, 0.86),
            item("资金余额", 0.06, 0.84),
            item("60660.53", 0.11, 0.84),
            item("可用金额", 0.06, 0.82),
            item("60660.53", 0.11, 0.82),
            item("持仓", 0.18, 0.73),
            item("证券代码", 0.18, 0.70),
            item("证券名称", 0.23, 0.70),
            item("市价", 0.28, 0.70),
            item("盈亏", 0.33, 0.70),
            item("实际数量", 0.48, 0.70),
            item("股票余额", 0.54, 0.70),
            item("可用余额", 0.59, 0.70),
            item("冻结数量", 0.65, 0.70),
            item("588330 双创50ETF", 0.18, 0.68, 0.07),
            item("1.230", 0.28, 0.68),
            item("-18.420", 0.33, 0.68),
            item("100", 0.49, 0.68),
            item("100", 0.55, 0.68),
            item("100", 0.60, 0.68),
            item("0", 0.66, 0.68),
            item("双创50ETF华宝 均价：1.244 最新：1.230", 0.35, 0.85, 0.15),
        ]

        snapshot = build_snapshot(items, "588330", Path("test.png"), "test", "同花顺")

        self.assertEqual(snapshot["account_mode"], "simulation")
        self.assertEqual(snapshot["total_assets"], 60783.53)
        self.assertEqual(snapshot["available_cash"], 60660.53)
        self.assertEqual(snapshot["market_value"], 123.0)
        position = snapshot["positions"][0]
        self.assertEqual(position["quantity"], 100)
        self.assertEqual(position["sellable_quantity"], 100)
        self.assertEqual(position["avg_cost"], 1.244)
        self.assertEqual(position["current_price"], 1.23)
        self.assertEqual(position["market_value"], 123.0)
        self.assertNotIn("validation_errors", snapshot)

    def test_number_parser_does_not_truncate_five_digit_account_values(self) -> None:
        items = [
            item("模拟练习", 0.06, 0.92),
            item("总资产", 0.06, 0.90),
            item("60783.53", 0.11, 0.90),
        ]

        snapshot = build_snapshot(items, "588330", Path("test.png"), "test", "同花顺")

        self.assertEqual(snapshot["total_assets"], 60783.53)


if __name__ == "__main__":
    unittest.main()
