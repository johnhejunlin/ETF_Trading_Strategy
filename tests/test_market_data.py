import unittest
from unittest import mock

import market_data
from market_data import EastMoneyMarketData, LatestQuote


class MarketDataTests(unittest.TestCase):
    def test_market_symbol_for_sh_etf(self) -> None:
        self.assertEqual(EastMoneyMarketData._tencent_symbol("588330"), "sh588330")

    def test_market_symbol_for_sz_stock(self) -> None:
        self.assertEqual(EastMoneyMarketData._tencent_symbol("000001"), "sz000001")

    def test_market_symbol_for_bj_stock(self) -> None:
        self.assertEqual(EastMoneyMarketData._tencent_symbol("832000"), "bj832000")

    def test_normalize_symbol_variants(self) -> None:
        self.assertEqual(EastMoneyMarketData._normalize_symbol("SH588330"), "588330")
        self.assertEqual(EastMoneyMarketData._normalize_symbol("588330.SH"), "588330")
        self.assertEqual(EastMoneyMarketData._normalize_symbol(" sz000001 "), "000001")

    def test_parse_tencent_quote(self) -> None:
        raw = 'v_sh588330="1~科创综指ETF华夏~588330~1.290~1.280~1.281~1000~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~20260615103000~0";'

        quote = EastMoneyMarketData._parse_tencent_quote("SH588330", raw)

        self.assertIsInstance(quote, LatestQuote)
        self.assertEqual(quote.symbol, "588330")
        self.assertEqual(quote.name, "科创综指ETF华夏")
        self.assertEqual(quote.price, 1.29)
        self.assertEqual(quote.previous_close, 1.28)
        self.assertEqual(quote.open_price, 1.281)
        self.assertEqual(quote.trade_time, "20260615103000")

    def test_eastmoney_wait_throttles_between_calls(self) -> None:
        original_last_call = market_data._eastmoney_last_call
        original_interval = market_data.EASTMONEY_MIN_INTERVAL_SECONDS
        try:
            market_data._eastmoney_last_call = 100.0
            market_data.EASTMONEY_MIN_INTERVAL_SECONDS = 1.0
            with mock.patch("market_data.time.time", side_effect=[100.2, 100.9]), \
                    mock.patch("market_data.time.sleep") as sleep, \
                    mock.patch("market_data.random.uniform", return_value=0.3):
                market_data._wait_for_eastmoney_slot()

            sleep.assert_called_once()
            self.assertAlmostEqual(sleep.call_args.args[0], 1.1)
            self.assertEqual(market_data._eastmoney_last_call, 100.9)
        finally:
            market_data._eastmoney_last_call = original_last_call
            market_data.EASTMONEY_MIN_INTERVAL_SECONDS = original_interval


if __name__ == "__main__":
    unittest.main()
