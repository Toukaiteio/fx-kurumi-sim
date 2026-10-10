"""真实行情接口的集成测试 —— **默认跳过**。

只有显式设置环境变量才会运行，避免单元测试意外访问网络::

    KURUMI_LIVE_TESTS=1 python -m unittest tests.test_market_data_live -v

这些用例只读取公开行情接口，不连接券商、不下单、不涉及任何真实账户。
由于外汇市场周末休市，报价会被正确判定为过期（``is_stale=True``），
这属于预期行为，因此本文件不断言「必须新鲜」。
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import load_config  # noqa: E402
from market_data import (  # noqa: E402
    BarSeries,
    FrankfurterProvider,
    MarketContext,
    MarketDataError,
    Quote,
    QuoteType,
    YahooChartProvider,
    get_current_quote,
    get_historical_bars,
)

_LIVE = os.environ.get("KURUMI_LIVE_TESTS") == "1"
_SKIP_REASON = "设置 KURUMI_LIVE_TESTS=1 才会访问真实行情接口"


@unittest.skipUnless(_LIVE, _SKIP_REASON)
class TestLiveYahoo(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = load_config()
        self.ctx = MarketContext.from_config(self.cfg)
        self.provider = YahooChartProvider()

    def test_quote_is_valid_and_synthetic(self) -> None:
        quote = self.provider.fetch_quote(self.ctx)
        self.assertIsInstance(quote, Quote)
        self.assertEqual(quote.quote_type, QuoteType.SYNTHETIC_SPREAD)
        self.assertLessEqual(quote.bid, quote.ask)
        self.assertEqual(quote.spread, self.ctx.spread_jpy)
        self.assertIsNotNone(quote.source_url)
        # 报价与历史同源：取最近一根 1 分钟 K 线的收盘价
        self.assertGreater(quote.mid, 0)

    def test_recent_bars_are_parseable(self) -> None:
        end = self.ctx.now()
        start = end - timedelta(minutes=120)
        series: BarSeries = self.provider.fetch_bars(self.ctx, start, end)
        self.assertIsInstance(series, BarSeries)
        # 休市时可能为空；只要不为空就必须几何自洽
        for bar in series:
            self.assertGreaterEqual(bar.high, bar.low)
            self.assertGreaterEqual(bar.high, max(bar.open, bar.close))
            self.assertLessEqual(bar.low, min(bar.open, bar.close))
        if series:
            self.assertEqual(series.bars[0].timestamp.utcoffset(), timedelta(hours=9))


@unittest.skipUnless(_LIVE, _SKIP_REASON)
class TestLiveFrankfurter(unittest.TestCase):
    def test_daily_reference_quote(self) -> None:
        cfg = load_config()
        ctx = MarketContext.from_config(cfg)
        quote = FrankfurterProvider().fetch_quote(ctx)
        self.assertEqual(quote.quote_type, QuoteType.SYNTHETIC_SPREAD)
        self.assertGreater(quote.mid, 0)
        self.assertLessEqual(quote.bid, quote.ask)


@unittest.skipUnless(_LIVE, _SKIP_REASON)
class TestLiveChain(unittest.TestCase):
    def test_auto_chain_returns_a_quote(self) -> None:
        cfg = load_config()
        quote = get_current_quote(cfg)
        print(
            f"\n[live] source={quote.source} mid={quote.mid} "
            f"type={quote.quote_type.value} stale={quote.is_stale} "
            f"age={quote.age_seconds}s"
        )
        self.assertGreater(quote.mid, 0)

    def test_history_window_over_seven_days_is_reported(self) -> None:
        cfg = load_config()
        ctx = MarketContext.from_config(cfg)
        with self.assertRaises(MarketDataError):
            get_historical_bars(ctx.now() - timedelta(days=30), ctx.now(), cfg=cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
