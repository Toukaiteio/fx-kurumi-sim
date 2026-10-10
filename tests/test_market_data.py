"""market_data 模块的自动化测试。

原则
====

* 全部使用**预置的模拟响应**，不访问真实网络。测试进程启动时会把
  ``urllib.request.urlopen`` 替换成「一旦被调用就失败」的桩，任何意外的
  真实请求都会立刻让测试报错。
* 不连接任何券商、不涉及真实账户、不下单。
* 预置响应来自对各数据源真实返回结构的裁剪复制（见各常量上方的说明）。

运行方式（项目根目录 E:\\pj2\\fx-kurumi-sim）::

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import Config, load_config  # noqa: E402
import market_data as md  # noqa: E402
from market_data import (  # noqa: E402
    AlphaVantageProvider,
    Bar,
    BarSeries,
    DataSourceError,
    FrankfurterProvider,
    Granularity,
    HistoryUnavailableError,
    HourlyRange,
    InvalidDataError,
    MarketContext,
    MarketDataError,
    MarketDataProvider,
    NoProviderAvailableError,
    Quote,
    QuoteType,
    RateLimitError,
    StaleQuoteError,
    TwelveDataProvider,
    YahooChartProvider,
    aggregate_hourly_bars,
    build_provider_chain,
    default_stale_after_seconds,
    get_current_quote,
    get_historical_bars,
    get_hourly_ranges,
    hour_start,
    validate_bars,
    validate_quote,
)

_JST = timezone(timedelta(hours=9))

# --------------------------------------------------------------------------- #
# 网络护栏：任何真实请求都视为测试失败
# --------------------------------------------------------------------------- #

_NETWORK_GUARD: mock._patch | None = None


def setUpModule() -> None:  # noqa: N802
    global _NETWORK_GUARD
    _NETWORK_GUARD = mock.patch(
        "market_data.urllib.request.urlopen",
        side_effect=AssertionError("测试不得访问真实网络（urlopen 被调用）"),
    )
    _NETWORK_GUARD.start()


def tearDownModule() -> None:  # noqa: N802
    if _NETWORK_GUARD is not None:
        _NETWORK_GUARD.stop()


# --------------------------------------------------------------------------- #
# 预置响应（按真实返回结构裁剪）
# --------------------------------------------------------------------------- #

# Yahoo v8/finance/chart 响应结构（字段名与实测一致），最后一根 K 线全为 null，
# 用于验证 null 被正确跳过。
YAHOO_CHART: dict[str, Any] = {
    "chart": {
        "result": [
            {
                "meta": {
                    "currency": "JPY",
                    "symbol": "USDJPY=X",
                    "regularMarketPrice": 158.246,
                    "regularMarketTime": 1791500580,
                    "shortName": "USD/JPY",
                    "scale": 4,
                    "priceHint": 4,
                },
                "timestamp": [1791500400, 1791500460, 1791500520, 1791500580],
                "indicators": {
                    "quote": [
                        {
                            "open": [157.873, 157.902, 157.920, None],
                            "high": [157.914, 157.926, 157.930, None],
                            "low": [157.871, 157.902, 157.910, None],
                            "close": [157.907, 157.923, 157.925, None],
                            "volume": [0, 0, 0, None],
                        }
                    ]
                },
            }
        ],
        "error": None,
    }
}

# Yahoo 对无效 symbol 的真实响应形态（HTTP 404 + chart.error）。
YAHOO_NOT_FOUND: dict[str, Any] = {
    "chart": {
        "result": None,
        "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"},
    }
}

# Alpha Vantage CURRENCY_EXCHANGE_RATE（实测字段名，含 Bid/Ask）。
AV_QUOTE: dict[str, Any] = {
    "Realtime Currency Exchange Rate": {
        "1. From_Currency Code": "USD",
        "2. From_Currency Name": "United States Dollar",
        "3. To_Currency Code": "JPY",
        "4. To_Currency Name": "Japanese Yen",
        "5. Exchange Rate": "158.24688447",
        "6. Last Refreshed": "2026-10-10 06:03:55",
        "7. Time Zone": "UTC",
        "8. Bid Price": "158.24069702",
        "9. Ask Price": "158.24915531",
    }
}

# Alpha Vantage 限流时的真实形态。
AV_RATE_LIMITED: dict[str, Any] = {
    "Note": (
        "Thank you for using Alpha Vantage! Our standard API rate limit is "
        "25 requests per day."
    )
}

# Alpha Vantage 免费档对 FX_INTRADAY 的真实拒绝形态。
AV_DEMO_INTRADAY: dict[str, Any] = {
    "Information": (
        "The **demo** API key is for demo purposes only. Please claim your free "
        "API key at (https://www.alphavantage.co/support/#api-key)"
    )
}

# Alpha Vantage FX_INTRADAY 响应。
AV_INTRADAY: dict[str, Any] = {
    "Meta Data": {"1. Information": "FX Intraday (1min)", "2. From Symbol": "USD"},
    "Time Series FX (1min)": {
        "2026-10-09 22:57:00": {
            "1. open": "158.200",
            "2. high": "158.250",
            "3. low": "158.180",
            "4. close": "158.240",
        },
        "2026-10-09 22:58:00": {
            "1. open": "158.240",
            "2. high": "158.260",
            "3. low": "158.230",
            "4. close": "158.250",
        },
    },
}

# Twelve Data /quote（demo 实测：有 close 与时间戳，但没有 bid/ask）。
TD_QUOTE: dict[str, Any] = {
    "symbol": "USD/JPY",
    "name": "US Dollar / Japanese Yen",
    "exchange": "Forex",
    "datetime": "2026-10-10",
    "timestamp": 1791576000,
    "last_quote_at": 1791612120,
    "open": "158.25135",
    "high": "158.33114",
    "low": "158.23828",
    "close": "158.32035",
    "previous_close": "158.24862",
    "is_market_open": True,
}

# Twelve Data /quote 在付费档会带 bid/ask。
TD_QUOTE_WITH_BID_ASK: dict[str, Any] = {
    "symbol": "USD/JPY",
    "close": "158.32035",
    "last_quote_at": 1791612120,
    "bid": "158.31800",
    "ask": "158.32270",
}

TD_RATE_LIMITED: dict[str, Any] = {
    "code": 429,
    "message": "API limit reached. Please try again later.",
    "status": "error",
}

TD_BAD_KEY: dict[str, Any] = {
    "code": 401,
    "message": "Invalid API key or api_key is missing.",
    "status": "error",
}

# Frankfurter（ECB）最新参考汇率。
FRANKFURTER_LATEST: dict[str, Any] = {
    "amount": 1.0,
    "base": "USD",
    "date": "2026-10-09",
    "rates": {"JPY": 158.25},
}


# --------------------------------------------------------------------------- #
# 测试工具
# --------------------------------------------------------------------------- #


def make_ctx(spread: str = "0.01", symbol: str = "USD/JPY") -> MarketContext:
    return MarketContext(symbol=symbol, tz=_JST, spread_jpy=Decimal(spread))


def make_quote(
    *,
    bid: str = "158.240",
    ask: str = "158.250",
    mid: str = "158.245",
    age_seconds: float = 0.0,
    source: str = "stub",
    quote_type: QuoteType = QuoteType.BID_ASK,
    now: datetime | None = None,
) -> Quote:
    reference = now or datetime.now(_JST)
    return Quote(
        symbol="USD/JPY",
        bid=Decimal(bid),
        ask=Decimal(ask),
        mid=Decimal(mid),
        timestamp=reference - timedelta(seconds=age_seconds),
        source=source,
        source_url="https://example.invalid/quote",
        quote_type=quote_type,
        granularity=Granularity.TICK,
    )


def make_bar(
    minutes: int, *, open_: str = "158.000", high: str = "158.100",
    low: str = "157.900", close: str = "158.050",
) -> Bar:
    base = datetime(2026, 10, 9, 9, 0, tzinfo=_JST)
    return Bar(
        timestamp=base + timedelta(minutes=minutes),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        source="stub",
    )


class FakeHttp:
    """按 URL 子串匹配返回预置响应；支持预置异常。"""

    def __init__(self, routes: Mapping[str, Any]) -> None:
        self.routes = dict(routes)
        self.calls: list[str] = []

    def __call__(self, url: str, *, timeout: float, retries: int, sleep: Any) -> Any:
        self.calls.append(url)
        for needle, payload in self.routes.items():
            if needle in url:
                if isinstance(payload, BaseException):
                    raise payload
                if callable(payload):
                    return payload(url)
                return payload
        raise AssertionError(f"测试未预置该 URL: {url}")


class StubProvider(MarketDataProvider):
    """可控的假数据源，用于测试链式回退与过期判定。"""

    name = "stub"

    def __init__(
        self,
        *,
        quote: Quote | None = None,
        quote_error: BaseException | None = None,
        bars: tuple[Bar, ...] | None = None,
        bars_error: BaseException | None = None,
        supports_1m_history: bool = True,
    ) -> None:
        super().__init__()
        self._quote = quote
        self._quote_error = quote_error
        self._bars = bars
        self._bars_error = bars_error
        self.supports_1m_history = supports_1m_history

    def fetch_quote(self, ctx: MarketContext) -> Quote:
        if self._quote_error is not None:
            raise self._quote_error
        assert self._quote is not None
        return self._quote

    def fetch_bars(self, ctx, start, end, *, interval="1m") -> BarSeries:
        if self._bars_error is not None:
            raise self._bars_error
        return BarSeries(
            symbol=ctx.symbol, interval=interval, bars=self._bars or (), source=self.name,
            source_url="https://example.invalid/bars", requested_start=start, requested_end=end,
        )


def http_error(code: int, body: bytes = b"error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid/", code, "msg", {}, io.BytesIO(body)  # type: ignore[arg-type]
    )


class FakeUrlopenResponse:
    def __init__(self, payload: Any) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self._body = raw
        self.status = 200

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount is None or amount < 0 else self._body[:amount]

    def __enter__(self) -> FakeUrlopenResponse:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #


class TestHttpLayer(unittest.TestCase):
    def setUp(self) -> None:
        self.noop_sleep = lambda _seconds: None  # noqa: E731

    def _call(self, url: str = "https://example.invalid/api", **kwargs: Any) -> Any:
        return md._http_get_json(
            url, timeout=1.0, sleep=self.noop_sleep, **kwargs
        )

    def test_parses_json(self) -> None:
        with mock.patch(
            "market_data.urllib.request.urlopen",
            return_value=FakeUrlopenResponse({"ok": True}),
        ):
            self.assertEqual(self._call(), {"ok": True})

    def test_429_raises_rate_limit(self) -> None:
        with mock.patch(
            "market_data.urllib.request.urlopen",
            side_effect=http_error(429, b"too many requests"),
        ):
            with self.assertRaises(RateLimitError):
                self._call()

    def test_404_raises_invalid_data(self) -> None:
        with mock.patch(
            "market_data.urllib.request.urlopen",
            side_effect=http_error(404, b"not found"),
        ):
            with self.assertRaises(InvalidDataError):
                self._call()

    def test_4xx_not_retried(self) -> None:
        opener = mock.Mock(side_effect=http_error(400, b"bad request"))
        with mock.patch("market_data.urllib.request.urlopen", opener):
            with self.assertRaises(DataSourceError):
                self._call(retries=3)
        self.assertEqual(opener.call_count, 1)

    def test_5xx_then_success_retries(self) -> None:
        opener = mock.Mock(
            side_effect=[http_error(503), FakeUrlopenResponse({"ok": 1})]
        )
        with mock.patch("market_data.urllib.request.urlopen", opener):
            self.assertEqual(self._call(retries=2), {"ok": 1})
        self.assertEqual(opener.call_count, 2)

    def test_5xx_exhausts_retries(self) -> None:
        opener = mock.Mock(side_effect=http_error(500))
        with mock.patch("market_data.urllib.request.urlopen", opener):
            with self.assertRaises(DataSourceError):
                self._call(retries=2)
        self.assertEqual(opener.call_count, 3)

    def test_timeout_then_success(self) -> None:
        opener = mock.Mock(
            side_effect=[
                urllib.error.URLError("timed out"),
                FakeUrlopenResponse({"ok": 2}),
            ]
        )
        with mock.patch("market_data.urllib.request.urlopen", opener):
            self.assertEqual(self._call(retries=1), {"ok": 2})

    def test_timeout_exhausted(self) -> None:
        with mock.patch(
            "market_data.urllib.request.urlopen",
            side_effect=urllib.error.URLError("timed out"),
        ):
            with self.assertRaises(DataSourceError) as ctx:
                self._call(retries=1)
        self.assertIn("网络/超时", str(ctx.exception))

    def test_invalid_json(self) -> None:
        with mock.patch(
            "market_data.urllib.request.urlopen",
            return_value=FakeUrlopenResponse(b"{ not json"),
        ):
            with self.assertRaises(InvalidDataError):
                self._call()

    def test_sleep_called_between_retries(self) -> None:
        delays: list[float] = []
        with mock.patch(
            "market_data.urllib.request.urlopen",
            side_effect=[http_error(503), FakeUrlopenResponse({"ok": 1})],
        ):
            md._http_get_json(
                "https://example.invalid/api", timeout=1.0, retries=2, sleep=delays.append
            )
        self.assertEqual(len(delays), 1)


# --------------------------------------------------------------------------- #
# Yahoo
# --------------------------------------------------------------------------- #


class TestYahooProvider(unittest.TestCase):
    def test_symbol_mapping(self) -> None:
        self.assertEqual(YahooChartProvider.yahoo_symbol("USD/JPY"), "USDJPY=X")
        self.assertEqual(YahooChartProvider.yahoo_symbol("eur/jpy"), "EURJPY=X")

    def test_quote_uses_last_valid_bar_close_and_synthesizes_spread(self) -> None:
        provider = YahooChartProvider(http_get=FakeHttp({"finance/chart": YAHOO_CHART}))
        quote = provider.fetch_quote(make_ctx())
        # 最后一根全 null 的 K 线被跳过，取 157.925 那根
        self.assertEqual(quote.mid, Decimal("157.925"))
        self.assertEqual(quote.bid, Decimal("157.920"))
        self.assertEqual(quote.ask, Decimal("157.930"))
        self.assertEqual(quote.quote_type, QuoteType.SYNTHETIC_SPREAD)
        self.assertTrue(quote.is_synthetic)
        self.assertEqual(quote.granularity, Granularity.MINUTE)
        self.assertEqual(quote.source, "yahoo_finance_chart")
        self.assertIn("finance/chart", quote.source_url)
        self.assertEqual(quote.timestamp.utcoffset(), timedelta(hours=9))

    def test_quote_falls_back_to_meta_price(self) -> None:
        payload = json.loads(json.dumps(YAHOO_CHART))
        payload["chart"]["result"][0]["indicators"]["quote"][0] = {
            "open": [None], "high": [None], "low": [None], "close": [None], "volume": [None]
        }
        payload["chart"]["result"][0]["timestamp"] = [1791500580]
        provider = YahooChartProvider(http_get=FakeHttp({"finance/chart": payload}))
        quote = provider.fetch_quote(make_ctx())
        self.assertEqual(quote.mid, Decimal("158.246"))

    def test_chart_error_payload_raises_invalid_data(self) -> None:
        # Yahoo 对无效 symbol 会在响应体里带 chart.error，本模块据此抛 InvalidDataError
        provider = YahooChartProvider(http_get=FakeHttp({"finance/chart": YAHOO_NOT_FOUND}))
        with self.assertRaises(InvalidDataError) as err:
            provider.fetch_quote(make_ctx())
        self.assertIn("Not Found", str(err.exception))

    def test_http_layer_404_maps_to_invalid_data(self) -> None:
        # 真实 HTTP 层把 404 翻译成 InvalidDataError；此处模拟该行为验证透传
        provider = YahooChartProvider(
            http_get=FakeHttp({"finance/chart": InvalidDataError("数据源返回 404")})
        )
        with self.assertRaises(InvalidDataError):
            provider.fetch_quote(make_ctx())

    def test_bars_skip_null_rows_and_sort(self) -> None:
        provider = YahooChartProvider(http_get=FakeHttp({"finance/chart": YAHOO_CHART}))
        ctx = make_ctx()
        series = provider.fetch_bars(
            ctx,
            datetime(2026, 10, 9, 8, 0, tzinfo=_JST),
            datetime(2026, 10, 9, 9, 0, tzinfo=_JST),
        )
        self.assertEqual(len(series), 3)  # 3 根有效 + 1 根 null 被跳过
        self.assertEqual(series.bars[0].open, Decimal("157.873"))
        self.assertEqual(series.bars[-1].close, Decimal("157.925"))
        self.assertEqual(series.source, "yahoo_finance_chart")

    def test_float_noise_is_quantized_to_source_scale(self) -> None:
        # Yahoo 用 float64 返回价格（实测 157.90699768066406），按 meta.scale 量化
        payload = json.loads(json.dumps(YAHOO_CHART))
        result = payload["chart"]["result"][0]
        result["meta"]["scale"] = 3
        result["timestamp"] = [1791500400]
        result["indicators"]["quote"][0] = {
            "open": [157.87299999999999],
            "high": [157.91400146484375],
            "low": [157.87100219726562],
            "close": [157.90699768066406],
            "volume": [0],
        }
        provider = YahooChartProvider(http_get=FakeHttp({"finance/chart": payload}))
        quote = provider.fetch_quote(make_ctx())
        self.assertEqual(quote.mid, Decimal("157.907"))
        # 价格必须是干净的有限小数，不带浮点尾巴
        self.assertEqual(str(quote.mid), "157.907")

    def test_bars_reject_span_over_seven_days(self) -> None:
        provider = YahooChartProvider(http_get=FakeHttp({"finance/chart": YAHOO_CHART}))
        ctx = make_ctx()
        with self.assertRaises(HistoryUnavailableError) as err:
            provider.fetch_bars(
                ctx,
                ctx.now() - timedelta(days=30),
                ctx.now(),
            )
        self.assertIn("7 天", str(err.exception))

    def test_bars_report_old_window_as_unavailable(self) -> None:
        provider = YahooChartProvider(http_get=FakeHttp({"finance/chart": YAHOO_CHART}))
        ctx = make_ctx()
        with self.assertRaises(HistoryUnavailableError) as err:
            provider.fetch_bars(
                ctx,
                ctx.now() - timedelta(days=30),
                ctx.now() - timedelta(days=29),
            )
        self.assertIn("回溯上限", str(err.exception))

    def test_bars_reject_non_1m_interval(self) -> None:
        provider = YahooChartProvider(http_get=FakeHttp({"finance/chart": YAHOO_CHART}))
        with self.assertRaises(HistoryUnavailableError):
            provider.fetch_bars(
                make_ctx(), datetime(2026, 10, 9, 8, 0, tzinfo=_JST),
                datetime(2026, 10, 9, 9, 0, tzinfo=_JST), interval="5m",
            )

    def test_invalid_bar_geometry_rejected(self) -> None:
        payload = json.loads(json.dumps(YAHOO_CHART))
        quote = payload["chart"]["result"][0]["indicators"]["quote"][0]
        quote["high"][0] = 157.000  # high < open/close
        provider = YahooChartProvider(http_get=FakeHttp({"finance/chart": payload}))
        with self.assertRaises(InvalidDataError):
            provider.fetch_bars(
                make_ctx(),
                datetime(2026, 10, 9, 8, 0, tzinfo=_JST),
                datetime(2026, 10, 9, 9, 0, tzinfo=_JST),
            )


# --------------------------------------------------------------------------- #
# Alpha Vantage
# --------------------------------------------------------------------------- #


class TestAlphaVantageProvider(unittest.TestCase):
    def _provider(self, payload: Any) -> AlphaVantageProvider:
        return AlphaVantageProvider("test-key", http_get=FakeHttp({"alphavantage.co": payload}))

    def test_quote_returns_real_bid_ask(self) -> None:
        quote = self._provider(AV_QUOTE).fetch_quote(make_ctx())
        self.assertEqual(quote.quote_type, QuoteType.BID_ASK)
        self.assertFalse(quote.is_synthetic)
        self.assertEqual(quote.bid, Decimal("158.24069702"))
        self.assertEqual(quote.ask, Decimal("158.24915531"))
        self.assertEqual(quote.mid, Decimal("158.24688447"))
        self.assertEqual(quote.granularity, Granularity.TICK)
        self.assertEqual(quote.source, "alpha_vantage")

    def test_quote_falls_back_to_synthetic_when_bid_ask_absent(self) -> None:
        payload = json.loads(json.dumps(AV_QUOTE))
        payload["Realtime Currency Exchange Rate"]["8. Bid Price"] = "0.00000"
        payload["Realtime Currency Exchange Rate"]["9. Ask Price"] = "0.00000"
        quote = self._provider(payload).fetch_quote(make_ctx())
        self.assertEqual(quote.quote_type, QuoteType.SYNTHETIC_SPREAD)
        self.assertEqual(quote.mid, Decimal("158.24688447"))
        self.assertEqual(quote.bid, Decimal("158.24188447"))
        self.assertEqual(quote.ask, Decimal("158.25188447"))

    def test_note_field_means_rate_limited(self) -> None:
        with self.assertRaises(RateLimitError):
            self._provider(AV_RATE_LIMITED).fetch_quote(make_ctx())

    def test_information_field_means_source_error(self) -> None:
        with self.assertRaises(DataSourceError) as err:
            self._provider(AV_DEMO_INTRADAY).fetch_bars(
                make_ctx(),
                datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc),
            )
        self.assertNotIsInstance(err.exception, RateLimitError)

    def test_bars_parsed_and_filtered_by_window(self) -> None:
        provider = self._provider(AV_INTRADAY)
        series = provider.fetch_bars(
            make_ctx(),
            datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(series), 2)
        self.assertEqual(series.bars[0].timestamp.utcoffset(), timedelta(hours=9))
        self.assertFalse(series.bars[0].is_synthetic)

    def test_bars_outside_window_excluded(self) -> None:
        provider = self._provider(AV_INTRADAY)
        series = provider.fetch_bars(
            make_ctx(),
            datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(series.is_empty)

    def test_empty_timeseries_raises(self) -> None:
        with self.assertRaises(InvalidDataError) as err:
            self._provider({"Meta Data": {}}).fetch_bars(
                make_ctx(),
                datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc),
            )
        self.assertIn("Time Series FX (1min)", str(err.exception))

    def test_missing_key_env_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(
                AlphaVantageProvider.from_environment(Path(tempfile.gettempdir()))
            )

    def test_key_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {"ALPHAVANTAGE_API_KEY": "sk-av"}, clear=True):
            provider = AlphaVantageProvider.from_environment(Path(tempfile.gettempdir()))
        assert provider is not None
        self.assertEqual(provider.api_key, "sk-av")
        self.assertEqual(provider.key_origin, "env:ALPHAVANTAGE_API_KEY")


# --------------------------------------------------------------------------- #
# Twelve Data / Frankfurter
# --------------------------------------------------------------------------- #


class TestTwelveDataProvider(unittest.TestCase):
    def test_quote_without_bid_ask_is_synthetic(self) -> None:
        provider = TwelveDataProvider("k", http_get=FakeHttp({"twelvedata.com": TD_QUOTE}))
        quote = provider.fetch_quote(make_ctx())
        self.assertEqual(quote.quote_type, QuoteType.SYNTHETIC_SPREAD)
        self.assertEqual(quote.mid, Decimal("158.32035"))
        self.assertEqual(quote.bid, Decimal("158.31535"))
        self.assertEqual(quote.ask, Decimal("158.32535"))

    def test_quote_with_bid_ask_is_real(self) -> None:
        provider = TwelveDataProvider(
            "k", http_get=FakeHttp({"twelvedata.com": TD_QUOTE_WITH_BID_ASK})
        )
        quote = provider.fetch_quote(make_ctx())
        self.assertEqual(quote.quote_type, QuoteType.BID_ASK)
        self.assertEqual(quote.bid, Decimal("158.31800"))
        self.assertEqual(quote.ask, Decimal("158.32270"))

    def test_rate_limit_error_payload(self) -> None:
        provider = TwelveDataProvider("k", http_get=FakeHttp({"twelvedata.com": TD_RATE_LIMITED}))
        with self.assertRaises(RateLimitError):
            provider.fetch_quote(make_ctx())

    def test_bad_key_raises_source_error(self) -> None:
        provider = TwelveDataProvider("k", http_get=FakeHttp({"twelvedata.com": TD_BAD_KEY}))
        with self.assertRaises(DataSourceError) as err:
            provider.fetch_quote(make_ctx())
        self.assertNotIsInstance(err.exception, RateLimitError)
        self.assertIn("凭据无效", str(err.exception))


class TestFrankfurterProvider(unittest.TestCase):
    def test_quote_is_daily_synthetic(self) -> None:
        provider = FrankfurterProvider(http_get=FakeHttp({"frankfurter": FRANKFURTER_LATEST}))
        quote = provider.fetch_quote(make_ctx())
        self.assertEqual(quote.quote_type, QuoteType.SYNTHETIC_SPREAD)
        self.assertEqual(quote.granularity, Granularity.DAILY)
        self.assertEqual(quote.mid, Decimal("158.25"))
        self.assertEqual(quote.bid, Decimal("158.245"))
        self.assertEqual(quote.ask, Decimal("158.255"))
        self.assertEqual(quote.source, "frankfurter_ecb")

    def test_history_is_reported_unavailable(self) -> None:
        provider = FrankfurterProvider(http_get=FakeHttp({"frankfurter": FRANKFURTER_LATEST}))
        with self.assertRaises(HistoryUnavailableError) as err:
            provider.fetch_bars(
                make_ctx(), datetime(2026, 10, 9, tzinfo=_JST), datetime(2026, 10, 10, tzinfo=_JST)
            )
        self.assertIn("不提供", str(err.exception))

    def test_missing_rate_raises(self) -> None:
        provider = FrankfurterProvider(http_get=FakeHttp({"frankfurter": {"rates": {}}}))
        with self.assertRaises(InvalidDataError):
            provider.fetch_quote(make_ctx())


# --------------------------------------------------------------------------- #
# 校验函数
# --------------------------------------------------------------------------- #


class TestValidation(unittest.TestCase):
    def test_valid_quote_passes(self) -> None:
        validate_quote(make_quote())

    def test_bid_above_ask_rejected(self) -> None:
        with self.assertRaises(InvalidDataError) as err:
            validate_quote(make_quote(bid="158.300", ask="158.200", mid="158.250"))
        self.assertIn("Bid 高于 Ask", str(err.exception))

    def test_non_positive_price_rejected(self) -> None:
        with self.assertRaises(InvalidDataError):
            validate_quote(make_quote(bid="0", ask="158.200", mid="158.100"))

    def test_mid_far_outside_spread_rejected(self) -> None:
        with self.assertRaises(InvalidDataError) as err:
            validate_quote(make_quote(bid="158.240", ask="158.250", mid="160.000"))
        self.assertIn("严重不一致", str(err.exception))

    def test_naive_timestamp_rejected(self) -> None:
        quote = make_quote()
        broken = Quote(
            symbol=quote.symbol, bid=quote.bid, ask=quote.ask, mid=quote.mid,
            timestamp=datetime(2026, 10, 9, 9, 0), source="stub",
            source_url=quote.source_url, quote_type=quote.quote_type,
            granularity=quote.granularity,
        )
        with self.assertRaises(InvalidDataError) as err:
            validate_quote(broken)
        self.assertIn("时区", str(err.exception))

    def test_future_timestamp_rejected(self) -> None:
        now = datetime(2026, 10, 9, 12, 0, tzinfo=_JST)
        with self.assertRaises(InvalidDataError) as err:
            validate_quote(
                make_quote(now=now + timedelta(hours=1)), now=now
            )
        self.assertIn("未来", str(err.exception))

    def test_small_clock_skew_allowed(self) -> None:
        now = datetime(2026, 10, 9, 12, 0, tzinfo=_JST)
        validate_quote(make_quote(now=now + timedelta(seconds=30)), now=now)

    def test_bar_high_low_inverted_rejected(self) -> None:
        bar = make_bar(0, open_="158.0", high="157.0", low="157.9", close="158.0")
        with self.assertRaises(InvalidDataError):
            validate_bars([bar])

    def test_bar_high_below_close_rejected(self) -> None:
        bar = make_bar(0, open_="158.0", high="158.0", low="157.0", close="158.5")
        with self.assertRaises(InvalidDataError):
            validate_bars([bar])

    def test_valid_bars_pass(self) -> None:
        validate_bars([make_bar(0), make_bar(1)])


# --------------------------------------------------------------------------- #
# 公开入口：过期判定与链式回退
# --------------------------------------------------------------------------- #


class TestGetCurrentQuote(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg: Config = load_config()

    def test_fresh_quote_not_stale(self) -> None:
        provider = StubProvider(quote=make_quote(age_seconds=5))
        quote = get_current_quote(self.cfg, provider=provider)
        self.assertFalse(quote.is_stale)
        self.assertLessEqual(quote.age_seconds, Decimal("60"))
        self.assertIsNotNone(quote.received_at)

    def test_old_quote_marked_stale(self) -> None:
        # 默认阈值 = 60 秒 × 3 = 180 秒
        self.assertEqual(default_stale_after_seconds(self.cfg), 180.0)
        provider = StubProvider(quote=make_quote(age_seconds=600))
        quote = get_current_quote(self.cfg, provider=provider)
        self.assertTrue(quote.is_stale)
        self.assertGreaterEqual(quote.age_seconds, Decimal("600"))

    def test_require_fresh_raises_stale_error(self) -> None:
        provider = StubProvider(quote=make_quote(age_seconds=600))
        with self.assertRaises(StaleQuoteError):
            get_current_quote(self.cfg, provider=provider, require_fresh=True)

    def test_stale_threshold_override(self) -> None:
        provider = StubProvider(quote=make_quote(age_seconds=60))
        quote = get_current_quote(self.cfg, provider=provider, stale_after_seconds=10)
        self.assertTrue(quote.is_stale)
        quote2 = get_current_quote(self.cfg, provider=provider, stale_after_seconds=3600)
        self.assertFalse(quote2.is_stale)

    def test_all_providers_failing_raises_with_reasons(self) -> None:
        provider = StubProvider(quote_error=DataSourceError("boom"))
        with self.assertRaises(NoProviderAvailableError) as err:
            get_current_quote(self.cfg, provider=provider)
        self.assertIn("boom", str(err.exception))
        self.assertEqual(err.exception.failures[0][0], "stub")

    def test_chain_falls_back_to_next_provider(self) -> None:
        failing = StubProvider(quote_error=RateLimitError("429"))
        working = StubProvider(quote=make_quote(age_seconds=1, source="working"))
        with mock.patch.object(md, "build_provider_chain", return_value=[failing, working]):
            quote = get_current_quote(self.cfg)
        self.assertEqual(quote.source, "working")

    def test_chain_skips_invalid_data_from_first_provider(self) -> None:
        bad = StubProvider(quote=make_quote(bid="158.300", ask="158.200"))
        good = StubProvider(quote=make_quote(source="good"))
        with mock.patch.object(md, "build_provider_chain", return_value=[bad, good]):
            quote = get_current_quote(self.cfg)
        self.assertEqual(quote.source, "good")

    def test_invalid_quote_from_single_provider_raises(self) -> None:
        bad = StubProvider(quote=make_quote(bid="158.300", ask="158.200"))
        with self.assertRaises(NoProviderAvailableError):
            get_current_quote(self.cfg, provider=bad)

    def test_unknown_provider_name(self) -> None:
        with self.assertRaises(MarketDataError) as err:
            get_current_quote(self.cfg, provider="no_such_source")
        self.assertIn("未知数据源", str(err.exception))

    def test_named_provider_requiring_key_without_key(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(DataSourceError) as err:
                get_current_quote(self.cfg, provider="alpha_vantage")
        self.assertIn("API 密钥", str(err.exception))

    def test_symbol_override(self) -> None:
        captured: dict[str, str] = {}

        class Recorder(StubProvider):
            def fetch_quote(self, ctx: MarketContext) -> Quote:
                captured["symbol"] = ctx.symbol
                return make_quote()

        get_current_quote(self.cfg, symbol="EUR/JPY", provider=Recorder())
        self.assertEqual(captured["symbol"], "EUR/JPY")


class TestGetHistoricalBars(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg: Config = load_config()
        self.ctx = MarketContext.from_config(self.cfg)
        self.start = self.ctx.now() - timedelta(minutes=30)
        self.end = self.ctx.now()

    def test_returns_bars_from_provider(self) -> None:
        bars = (make_bar(0), make_bar(1))
        series = get_historical_bars(
            self.start, self.end, cfg=self.cfg, provider=StubProvider(bars=bars)
        )
        self.assertEqual(len(series), 2)
        self.assertFalse(series.is_empty)

    def test_interval_other_than_1m_rejected(self) -> None:
        with self.assertRaises(HistoryUnavailableError) as err:
            get_historical_bars(
                self.start, self.end, cfg=self.cfg, interval="1h",
                provider=StubProvider(bars=()),
            )
        self.assertIn("1 分钟", str(err.exception))

    def test_start_after_end_rejected(self) -> None:
        with self.assertRaises(HistoryUnavailableError):
            get_historical_bars(
                self.end, self.start, cfg=self.cfg, provider=StubProvider(bars=())
            )

    def test_naive_datetimes_assumed_tokyo(self) -> None:
        naive_start = datetime(2026, 10, 9, 8, 0)
        naive_end = datetime(2026, 10, 9, 9, 0)
        series = get_historical_bars(
            naive_start, naive_end, cfg=self.cfg, provider=StubProvider(bars=(make_bar(0),))
        )
        self.assertEqual(series.requested_start.utcoffset(), timedelta(hours=9))

    def test_chain_falls_back_on_history_unavailable(self) -> None:
        first = StubProvider(bars_error=HistoryUnavailableError("窗口太旧"))
        second = StubProvider(bars=(make_bar(0),))
        with mock.patch.object(md, "build_provider_chain", return_value=[first, second]):
            series = get_historical_bars(self.start, self.end, cfg=self.cfg)
        self.assertEqual(len(series), 1)

    def test_all_sources_unavailable(self) -> None:
        with self.assertRaises(NoProviderAvailableError):
            get_historical_bars(
                self.start, self.end, cfg=self.cfg,
                provider=StubProvider(bars_error=HistoryUnavailableError("x")),
            )


# --------------------------------------------------------------------------- #
# BarSeries 辅助（止损/止盈/跳空检查）
# --------------------------------------------------------------------------- #


class TestBarSeriesHelpers(unittest.TestCase):
    def test_find_missing_bars(self) -> None:
        bars = (make_bar(0), make_bar(1), make_bar(6), make_bar(7))
        series = BarSeries(
            symbol="USD/JPY", interval="1m", bars=bars, source="stub",
            source_url="u", requested_start=make_bar(0).timestamp,
            requested_end=make_bar(7).timestamp,
        )
        gaps = series.find_missing_bars()
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].missing_count, 4)
        self.assertEqual(gaps[0].seconds, 300)

    def test_find_missing_bars_contiguous(self) -> None:
        series = BarSeries(
            symbol="USD/JPY", interval="1m", bars=(make_bar(0), make_bar(1)), source="stub",
            source_url="u", requested_start=make_bar(0).timestamp,
            requested_end=make_bar(1).timestamp,
        )
        self.assertEqual(series.find_missing_bars(), ())

    def test_find_price_gaps(self) -> None:
        bars = (
            make_bar(0, open_="158.0", high="158.2", low="157.9", close="158.0"),
            make_bar(1, open_="158.5", high="158.6", low="158.4", close="158.5"),
        )
        series = BarSeries(
            symbol="USD/JPY", interval="1m", bars=bars, source="stub",
            source_url="u", requested_start=bars[0].timestamp,
            requested_end=bars[1].timestamp,
        )
        gaps = series.find_price_gaps("0.3")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].gap, Decimal("0.5"))
        self.assertEqual(series.find_price_gaps("0.6"), ())

    def test_high_low_aggregates(self) -> None:
        bars = (
            make_bar(0, high="158.9", low="157.1"),
            make_bar(1, high="158.3", low="157.5"),
        )
        series = BarSeries(
            symbol="USD/JPY", interval="1m", bars=bars, source="stub",
            source_url="u", requested_start=bars[0].timestamp,
            requested_end=bars[1].timestamp,
        )
        self.assertEqual(series.high, Decimal("158.9"))
        self.assertEqual(series.low, Decimal("157.1"))

    def test_empty_series(self) -> None:
        series = BarSeries(
            symbol="USD/JPY", interval="1m", bars=(), source="stub", source_url="u",
            requested_start=make_bar(0).timestamp, requested_end=make_bar(1).timestamp,
        )
        self.assertTrue(series.is_empty)
        self.assertIsNone(series.high)
        # 空序列但有宽度的窗口 → 报告整段缺失（不能假装有覆盖）
        gaps = series.find_missing_bars()
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].missing_count, 1)
        self.assertEqual(series.find_missing_bars(include_edges=False), ())
        self.assertEqual(series.coverage(), Decimal("0"))

    def test_find_missing_bars_reports_edge_gaps(self) -> None:
        # 请求 9 分钟窗口但只拿到中间 6 根 → 首尾各缺 2 根
        bars = tuple(make_bar(i) for i in range(2, 8))
        series = BarSeries(
            symbol="USD/JPY", interval="1m", bars=bars, source="stub", source_url="u",
            requested_start=make_bar(0).timestamp, requested_end=make_bar(9).timestamp,
        )
        gaps = series.find_missing_bars()
        self.assertEqual(len(gaps), 2)
        self.assertEqual(gaps[0].missing_count, 2)  # 首部
        self.assertEqual(gaps[-1].missing_count, 2)  # 尾部
        # 关掉边缘检查后，内部连续则无缺口 —— 正是会误导调用方的行为
        self.assertEqual(series.find_missing_bars(include_edges=False), ())

    def test_coverage_ratio(self) -> None:
        full = BarSeries(
            symbol="USD/JPY", interval="1m", bars=tuple(make_bar(i) for i in range(6)),
            source="stub", source_url="u",
            requested_start=make_bar(0).timestamp, requested_end=make_bar(6).timestamp,
        )
        self.assertEqual(full.coverage(), Decimal("1"))
        partial = BarSeries(
            symbol="USD/JPY", interval="1m", bars=tuple(make_bar(i) for i in range(6)),
            source="stub", source_url="u",
            requested_start=make_bar(0).timestamp, requested_end=make_bar(12).timestamp,
        )
        self.assertEqual(partial.coverage(), Decimal("0.5"))

    def test_as_dict_is_json_serializable(self) -> None:
        series = BarSeries(
            symbol="USD/JPY", interval="1m", bars=(make_bar(0),), source="stub",
            source_url="u", requested_start=make_bar(0).timestamp,
            requested_end=make_bar(0).timestamp,
        )
        payload = json.loads(json.dumps(series.as_dict()))
        self.assertEqual(payload["bar_count"], 1)
        self.assertEqual(payload["bars"][0]["open"], "158.000")


# --------------------------------------------------------------------------- #
# 凭据解析与数据源链
# --------------------------------------------------------------------------- #


class TestSecretsAndChain(unittest.TestCase):
    def test_env_var_wins(self) -> None:
        with mock.patch.dict(os.environ, {"ALPHAVANTAGE_API_KEY": "from-env"}, clear=True):
            key, origin = md._resolve_api_key(
                ("ALPHAVANTAGE_API_KEY",), "alphavantage_api_key",
                project_root=Path(tempfile.gettempdir()), allow_demo_key=False,
            )
        self.assertEqual(key, "from-env")
        self.assertTrue(origin.startswith("env:"))

    def test_local_secrets_file_used_when_env_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / md.SECRETS_FILENAME).write_text(
                json.dumps({"market_data": {"alphavantage_api_key": "from-file"}}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                key, origin = md._resolve_api_key(
                    ("ALPHAVANTAGE_API_KEY",), "alphavantage_api_key",
                    project_root=root, allow_demo_key=False,
                )
        self.assertEqual(key, "from-file")
        self.assertIn(md.SECRETS_FILENAME, origin)

    def test_demo_key_requires_explicit_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {}, clear=True):
                key, origin = md._resolve_api_key(
                    ("ALPHAVANTAGE_API_KEY",), "alphavantage_api_key",
                    project_root=Path(tmp), allow_demo_key=True,
                )
                blocked, _ = md._resolve_api_key(
                    ("ALPHAVANTAGE_API_KEY",), "alphavantage_api_key",
                    project_root=Path(tmp), allow_demo_key=False,
                )
        self.assertEqual(key, "demo")
        self.assertIn("demo", origin)
        self.assertIsNone(blocked)

    def test_source_code_contains_no_hardcoded_key(self) -> None:
        # 源码里不应出现常见密钥字面量形态（真实密钥只能来自环境变量或 config.local.json）
        text = Path(md.__file__).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"sk-[A-Za-z0-9]{10,}", text))
        self.assertIsNone(re.search(r"apikey=[A-Za-z0-9]{16,}", text))

    def test_chain_order_without_keys(self) -> None:
        cfg = load_config()
        with mock.patch.dict(os.environ, {}, clear=True):
            chain = build_provider_chain(cfg, allow_demo_key=False)
        self.assertEqual([p.name for p in chain], ["yahoo_finance_chart", "frankfurter_ecb"])

    def test_chain_excludes_daily_source_when_history_needed(self) -> None:
        cfg = load_config()
        with mock.patch.dict(os.environ, {}, clear=True):
            chain = build_provider_chain(cfg, need_history=True, allow_demo_key=False)
        names = [p.name for p in chain]
        self.assertIn("yahoo_finance_chart", names)
        self.assertNotIn("frankfurter_ecb", names)

    def test_chain_includes_keyed_sources_when_key_present(self) -> None:
        cfg = load_config()
        env = {"ALPHAVANTAGE_API_KEY": "k1", "TWELVEDATA_API_KEY": "k2"}
        with mock.patch.dict(os.environ, env, clear=True):
            chain = build_provider_chain(cfg, allow_demo_key=False)
        self.assertEqual(
            [p.name for p in chain],
            ["alpha_vantage", "twelve_data", "yahoo_finance_chart", "frankfurter_ecb"],
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


class TestCli(unittest.TestCase):
    def test_cli_reports_error_without_crashing(self) -> None:
        with mock.patch.object(
            md,
            "get_current_quote",
            side_effect=NoProviderAvailableError(
                "all down", [("stub", DataSourceError("no network"))]
            ),
        ):
            self.assertEqual(md._main(["--history-minutes", "0"]), 1)

    def test_cli_prints_quote(self) -> None:
        provider = StubProvider(quote=make_quote(age_seconds=1))
        with mock.patch.object(md, "get_current_quote", return_value=provider.fetch_quote(make_ctx())):
            self.assertEqual(md._main([]), 0)


# --------------------------------------------------------------------------- #
# 按小时聚合（AI 上下文用的价格区间）
# --------------------------------------------------------------------------- #


def bar_at(hour: int, minute: int, *, open_: str, high: str, low: str, close: str,
           day: int = 9) -> Bar:
    return Bar(
        timestamp=datetime(2026, 10, day, hour, minute, tzinfo=_JST),
        open=Decimal(open_), high=Decimal(high), low=Decimal(low), close=Decimal(close),
        source="stub",
    )


class TestHourlyAggregation(unittest.TestCase):
    def test_hour_start_truncates(self) -> None:
        moment = datetime(2026, 10, 9, 14, 37, 59, 123456, tzinfo=_JST)
        self.assertEqual(hour_start(moment, tz=_JST),
                         datetime(2026, 10, 9, 14, 0, tzinfo=_JST))

    def test_hour_start_converts_timezone(self) -> None:
        utc = timezone.utc
        moment = datetime(2026, 10, 9, 5, 30, tzinfo=utc)  # = 14:30 JST
        self.assertEqual(hour_start(moment, tz=_JST),
                         datetime(2026, 10, 9, 14, 0, tzinfo=_JST))

    def test_buckets_by_natural_hour(self) -> None:
        bars = [
            bar_at(13, 0, open_="158.0", high="158.5", low="157.8", close="158.3"),
            bar_at(13, 30, open_="158.3", high="158.9", low="158.1", close="158.7"),
            bar_at(14, 5, open_="158.7", high="159.0", low="158.6", close="158.8"),
        ]
        ranges = aggregate_hourly_bars(bars, tz=_JST)
        self.assertEqual(len(ranges), 2)
        first, second = ranges
        self.assertEqual(first.hour.hour, 13)
        self.assertEqual(first.bar_count, 2)
        self.assertEqual(first.open, Decimal("158.0"))
        self.assertEqual(first.high, Decimal("158.9"))
        self.assertEqual(first.low, Decimal("157.8"))
        self.assertEqual(first.close, Decimal("158.7"))
        self.assertEqual(first.range_jpy, Decimal("1.1"))
        self.assertEqual(second.hour.hour, 14)
        self.assertEqual(second.bar_count, 1)

    def test_ordered_ascending(self) -> None:
        bars = [
            bar_at(15, 0, open_="1", high="1", low="1", close="1"),
            bar_at(13, 0, open_="1", high="1", low="1", close="1"),
        ]
        ranges = aggregate_hourly_bars(bars, tz=_JST)
        self.assertEqual([r.hour.hour for r in ranges], [13, 15])

    def test_missing_hours_are_not_invented(self) -> None:
        """只有 13 点和 16 点有数据时，不得凭空造出 14、15 点。"""
        bars = [
            bar_at(13, 0, open_="1", high="1", low="1", close="1"),
            bar_at(16, 0, open_="1", high="1", low="1", close="1"),
        ]
        ranges = aggregate_hourly_bars(bars, tz=_JST)
        self.assertEqual([r.hour.hour for r in ranges], [13, 16])

    def test_empty_hour_is_reported_without_data(self) -> None:
        """空小时的价格字段必须是 None，不能填 0 或沿用前值。"""
        bars = [bar_at(13, 0, open_="1", high="1", low="1", close="1")]
        ranges = aggregate_hourly_bars(
            bars, tz=_JST, now=datetime(2026, 10, 9, 15, 30, tzinfo=_JST)
        )
        # 13 点有数据；14 点没数据但不在 now 所在小时 → 不出现
        self.assertEqual([r.hour.hour for r in ranges], [13, 15])
        empty = ranges[-1]
        self.assertEqual(empty.bar_count, 0)
        self.assertFalse(empty.has_data)
        self.assertIsNone(empty.open)
        self.assertIsNone(empty.high)
        self.assertIsNone(empty.low)
        self.assertIsNone(empty.close)
        self.assertIsNone(empty.range_jpy)
        self.assertTrue(empty.is_partial)

    def test_current_hour_is_marked_partial(self) -> None:
        bars = [
            bar_at(14, 0, open_="1", high="1", low="1", close="1"),
            bar_at(14, 5, open_="1", high="1", low="1", close="1"),
        ]
        ranges = aggregate_hourly_bars(
            bars, tz=_JST, now=datetime(2026, 10, 9, 14, 30, tzinfo=_JST)
        )
        self.assertEqual(len(ranges), 1)
        self.assertTrue(ranges[0].is_partial)

    def test_past_hours_are_not_partial(self) -> None:
        bars = [bar_at(13, 0, open_="1", high="1", low="1", close="1")]
        ranges = aggregate_hourly_bars(
            bars, tz=_JST, now=datetime(2026, 10, 9, 14, 30, tzinfo=_JST)
        )
        past = next(r for r in ranges if r.hour.hour == 13)
        self.assertFalse(past.is_partial)

    def test_limit_keeps_most_recent(self) -> None:
        bars = [
            bar_at(hour, 0, open_="1", high="1", low="1", close="1")
            for hour in range(10, 18)
        ]
        ranges = aggregate_hourly_bars(bars, tz=_JST, limit=3)
        self.assertEqual([r.hour.hour for r in ranges], [15, 16, 17])

    def test_empty_input(self) -> None:
        self.assertEqual(aggregate_hourly_bars((), tz=_JST), ())
        ranges = aggregate_hourly_bars(
            (), tz=_JST, now=datetime(2026, 10, 9, 14, 30, tzinfo=_JST)
        )
        self.assertEqual(len(ranges), 1)
        self.assertFalse(ranges[0].has_data)

    def test_as_dict_shape_and_types(self) -> None:
        bars = [bar_at(13, 0, open_="158.0", high="158.5", low="157.8", close="158.3")]
        payload = aggregate_hourly_bars(bars, tz=_JST)[0].as_dict()
        self.assertEqual(
            set(payload),
            {"hour", "bar_count", "open", "high", "low", "close", "range_jpy",
             "is_partial", "has_data"},
        )
        self.assertIsInstance(payload["open"], str, "价格必须是十进制字符串，不是 float")
        self.assertEqual(payload["open"], "158.0")
        self.assertIsInstance(payload["bar_count"], int)

    def test_get_hourly_ranges_uses_1m_history(self) -> None:
        """get_hourly_ranges 只是「取 1 分钟 K 线 → 聚合」的组合，不自己造数据。"""
        seen: dict[str, Any] = {}

        def fake_history(start, end, **kwargs):
            seen["start"], seen["end"] = start, end
            return BarSeries(
                symbol="USD/JPY", interval="1m",
                bars=(bar_at(13, 0, open_="1", high="1", low="1", close="1"),),
                source="stub", source_url="", requested_start=start, requested_end=end,
            )

        with mock.patch.object(md, "get_historical_bars", side_effect=fake_history):
            ranges = get_hourly_ranges(
                hours=4, cfg=load_config(),
                now=datetime(2026, 10, 9, 14, 30, tzinfo=_JST),
            )
        self.assertEqual(seen["end"], datetime(2026, 10, 9, 14, 30, tzinfo=_JST))
        self.assertEqual(seen["start"], datetime(2026, 10, 9, 11, 0, tzinfo=_JST))
        # 只有 13 点那一根真实 K 线 + now 所在的 14 点（partial）；
        # 11/12 点没有数据就**不出现**——不凭空补小时。
        self.assertEqual([r.hour.hour for r in ranges], [13, 14])
        self.assertEqual(ranges[0].bar_count, 1)
        self.assertFalse(ranges[1].has_data)

    def test_get_hourly_ranges_propagates_unavailable(self) -> None:
        """拿不到历史必须抛，不能返回空壳。"""
        with mock.patch.object(
            md, "get_historical_bars", side_effect=HistoryUnavailableError("太久了")
        ):
            with self.assertRaises(HistoryUnavailableError):
                get_hourly_ranges(hours=4, cfg=load_config(),
                                  now=datetime(2026, 10, 9, 14, 30, tzinfo=_JST))


if __name__ == "__main__":
    unittest.main(verbosity=2)
