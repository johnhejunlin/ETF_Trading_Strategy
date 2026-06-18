import unittest

from market_data import EastMoneyMarketData, LatestQuote


class MarketDataTests(unittest.TestCase):
    def test_market_symbol_for_sh_etf(self) -> None:
        self.assertEqual(EastMoneyMarketData._tencent_symbol("588330"), "sh588330")

    def test_market_symbol_for_sz_stock(self) -> None:
        self.assertEqual(EastMoneyMarketData._tencent_symbol("000001"), "sz000001")

    def test_parse_tencent_quote(self) -> None:
        raw = 'v_sh588330="1~科创综指ETF华夏~588330~1.290~1.280~1.281~1000~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~20260615103000~0";'

        quote = EastMoneyMarketData._parse_tencent_quote("588330", raw)

        self.assertIsInstance(quote, LatestQuote)
        self.assertEqual(quote.symbol, "588330")
        self.assertEqual(quote.name, "科创综指ETF华夏")
        self.assertEqual(quote.price, 1.29)
        self.assertEqual(quote.previous_close, 1.28)
        self.assertEqual(quote.open_price, 1.281)
        self.assertEqual(quote.trade_time, "20260615103000")


if __name__ == "__main__":
    unittest.main()
