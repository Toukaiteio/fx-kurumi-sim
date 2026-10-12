"""FX 战士久留美 —— 行情数据模块（USD/JPY）。

职责
====

* 从**可验证的公开行情接口**获取 USD/JPY 报价与 1 分钟 K 线；
* 统一为 :class:`Quote` / :class:`Bar` 数据结构，价格一律使用 ``decimal.Decimal``；
* 校验价格为正、Bid 不高于 Ask、时间戳有效，并判断报价是否过期；
* 对网络超时、接口限流、无效数据、行情过期、数据源故障分别抛出可区分的异常。

本模块**不使用随机数，也不使用模型记忆伪造行情**：所有数值都来自真实 HTTP 响应。
若某个数据源无法提供某项能力（例如只有日度中间价），一律抛出
:class:`HistoryUnavailableError` 明确报告，而不是编造数据。

已实测的数据源（2026-10-10 在本机实测）
=======================================

``yahoo_finance_chart``（无需密钥）
    报价：``v8/finance/chart/{SYM}?interval=1m&range=1d``。Yahoo 的 FX 报价是
    **指示性中间价，没有 Bid/Ask**，因此本模块按 config 的 ``market.spread_jpy``
    合成 Bid/Ask，并标记 ``quote_type=synthetic_spread``。
    报价取「最近一根 1 分钟 K 线的收盘价」，时间戳记为该 K 线的收盘时刻；
    这样报价与历史数据同源、口径一致。``meta.regularMarketPrice`` 仅作兜底。
    历史：同接口的 1 分钟 OHLC。
    **限制：1 分钟粒度只回溯约 7–9 天**（``range=1mo`` 实测返回 HTTP 422）。

``alpha_vantage``（需 ``ALPHAVANTAGE_API_KEY``）
    报价：``CURRENCY_EXCHANGE_RATE`` 直接给出 Bid/Ask，``quote_type=bid_ask``。
    历史：``FX_INTRADAY&interval=1min``，免费档仅最近 100 根（``outputsize=compact``）。
    **注意**：其 ``Last Refreshed`` 是 *API 刷新时刻*，不是市场成交时刻
    （实测两次取样时间戳推进而价格不变），因此它几乎永远不会被判为过期；
    该字段只用于记录，不能当作「行情新鲜」的证据。

``twelve_data``（需 ``TWELVEDATA_API_KEY``）
    报价与 1 分钟历史。响应含 Bid/Ask 时 ``quote_type=bid_ask``，否则按中间价合成。
    官方 ``demo`` 密钥实测返回的数据不可靠（周六仍报 ``is_market_open=true``，
    且 ``/quote`` 不含 Bid/Ask），因此仅在被显式允许时使用。

``frankfurter_ecb``（无需密钥，仅作最后兜底）
    只有 ECB **每日**参考中间价，**没有 1 分钟数据**。按 60 秒监控它必然过期，
    因此排在链尾，仅用于「其他源全部故障时至少拿到一个可解释的价格」。

安全边界
========

本项目只做虚拟交易模拟。本模块只读取公开行情，不连接任何券商、不下单、
不执行任何资金转账。数据源凭据一律从环境变量或 ``config.local.json``
（已在 .gitignore 中排除）读取，源码中不含任何真实密钥。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping, Sequence

from config import Config, ConfigError, load_config

__all__ = [
    "Bar",
    "BarGap",
    "BarSeries",
    "DataSourceError",
    "Granularity",
    "HourlyRange",
    "HistoryUnavailableError",
    "InvalidDataError",
    "MarketContext",
    "MarketDataError",
    "MarketDataProvider",
    "NoProviderAvailableError",
    "PriceGap",
    "Quote",
    "QuoteType",
    "RateLimitError",
    "StaleQuoteError",
    "AlphaVantageProvider",
    "FrankfurterProvider",
    "TwelveDataProvider",
    "YahooChartProvider",
    "aggregate_hourly_bars",
    "build_provider_chain",
    "default_stale_after_seconds",
    "get_current_quote",
    "get_historical_bars",
    "forex_session_open",
    "get_hourly_ranges",
    "hour_start",
    "validate_bars",
    "validate_quote",
]


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 数据层常量（不是交易规则，因此不放进 config.json）。
DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0
DEFAULT_RETRIES: Final[int] = 2
_RETRY_BACKOFF_SECONDS: Final[float] = 1.5
_MAX_ERROR_BODY_CHARS: Final[int] = 400

#: 判定「行情过期」的默认阈值 = 监控间隔 × 该倍数。
#: config.json 里没有独立的过期阈值字段（不擅自扩充配置），因此由
#: ``market.monitor_interval_seconds`` 推导；调用方可用 ``stale_after_seconds`` 覆盖。
STALE_MONITOR_INTERVALS: Final[int] = 3

#: 允许数据源时间戳比本地时钟快出的容差（时钟偏差 / 服务端时间戳取整）。
_FUTURE_TOLERANCE_SECONDS: Final[int] = 300

#: mid 与 [bid, ask] 的最大相对偏差；超过则视为该源数据自相矛盾。
_MID_RELATIVE_TOLERANCE: Final[Decimal] = Decimal("0.01")

#: 本地密钥文件（已被 .gitignore 排除），结构见 README 说明：
#: ``{"market_data": {"alphavantage_api_key": "...", "twelvedata_api_key": "..."}}``
SECRETS_FILENAME: Final[str] = "config.local.json"

#: 日本标准时间没有夏令时，固定 +09:00 与 Asia/Tokyo 完全等价。
_JST: Final[tzinfo] = timezone(timedelta(hours=9), "Asia/Tokyo")

#: Yahoo 1 分钟数据的回溯上限（实测 range=7d 返回约 9 天）。
_YAHOO_1M_LOOKBACK_DAYS: Final[int] = 9
#: (最大跨度天数, Yahoo range 参数)
_YAHOO_RANGES: Final[tuple[tuple[int, str], ...]] = ((1, "1d"), (5, "5d"), (7, "7d"))
#: Yahoo meta 未声明精度时使用的小数位。
_DEFAULT_PRICE_SCALE: Final[int] = 6

_DEFAULT_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json,text/csv,*/*",
}


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class MarketDataError(RuntimeError):
    """行情模块所有异常的基类。"""


class DataSourceError(MarketDataError):
    """数据源不可用：网络超时、连接失败、5xx、返回结构无法解析等。"""


class RateLimitError(DataSourceError):
    """数据源限流（HTTP 429，或接口以业务错误码/提示表示超频）。"""


class InvalidDataError(MarketDataError):
    """数据本身不合法：价格非正、Bid 高于 Ask、时间戳无效、字段缺失等。"""


class StaleQuoteError(MarketDataError):
    """报价已过期（仅在 ``require_fresh=True`` 时抛出）。"""


class HistoryUnavailableError(MarketDataError):
    """该数据源无法提供所请求的历史数据（粒度不支持、时间窗超出回溯范围等）。"""


class NoProviderAvailableError(MarketDataError):
    """链上所有数据源都失败，附每个源的失败原因。"""

    def __init__(self, message: str, failures: Sequence[tuple[str, BaseException]]) -> None:
        super().__init__(message)
        self.failures = tuple(failures)


# --------------------------------------------------------------------------- #
# 枚举与数据结构
# --------------------------------------------------------------------------- #


class QuoteType(str, Enum):
    """报价来源性质。"""

    #: 数据源直接给出买价/卖价。
    BID_ASK = "bid_ask"
    #: 只拿到中间价，按 config 的模拟点差合成买卖价 —— 属于**模拟报价**。
    SYNTHETIC_SPREAD = "synthetic_spread"


class Granularity(str, Enum):
    """报价所代表的时间粒度。"""

    TICK = "tick"
    MINUTE = "minute"
    DAILY = "daily"


@dataclass(frozen=True, slots=True)
class Quote:
    """统一的报价结构。所有价格字段均为 :class:`~decimal.Decimal`，时间戳为东京时间。"""

    symbol: str
    bid: Decimal
    ask: Decimal
    mid: Decimal
    timestamp: datetime
    source: str
    source_url: str
    quote_type: QuoteType
    granularity: Granularity
    is_stale: bool = False
    age_seconds: Decimal = Decimal("0")
    received_at: datetime | None = None

    @property
    def spread(self) -> Decimal:
        """买卖价差（绝对值，单位日元）。"""
        return self.ask - self.bid

    @property
    def is_synthetic(self) -> bool:
        """True 表示 Bid/Ask 由中间价 + 配置点差合成，属于模拟报价。"""
        return self.quote_type is QuoteType.SYNTHETIC_SPREAD

    def as_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化 / 可写库的字典。"""
        return {
            "symbol": self.symbol,
            "bid": str(self.bid),
            "ask": str(self.ask),
            "mid": str(self.mid),
            "timestamp": self.timestamp.isoformat(timespec="seconds"),
            "source": self.source,
            "source_url": self.source_url,
            "quote_type": self.quote_type.value,
            "granularity": self.granularity.value,
            "is_stale": self.is_stale,
            "age_seconds": str(self.age_seconds),
            "received_at": (
                self.received_at.isoformat(timespec="seconds") if self.received_at else None
            ),
        }


@dataclass(frozen=True, slots=True)
class Bar:
    """一根 K 线。价格均为 Decimal，时间戳为东京时间（K 线开盘时刻）。"""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    source: str
    interval: str = "1m"
    volume: Decimal | None = None
    is_synthetic: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(timespec="seconds"),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume) if self.volume is not None else None,
            "source": self.source,
            "interval": self.interval,
            "is_synthetic": self.is_synthetic,
        }


@dataclass(frozen=True, slots=True)
class BarGap:
    """K 线序列中的时间缺口（休市、无成交、源站缺数据）。"""

    after: datetime
    before: datetime
    missing_count: int
    seconds: int


@dataclass(frozen=True, slots=True)
class PriceGap:
    """相邻 K 线之间的价格跳空（前一根收盘 → 后一根开盘）。"""

    timestamp: datetime
    prev_close: Decimal
    next_open: Decimal

    @property
    def gap(self) -> Decimal:
        return self.next_open - self.prev_close


@dataclass(frozen=True, slots=True)
class BarSeries:
    """一段 K 线序列，附带用于检查止损/止盈/跳空的辅助方法。"""

    symbol: str
    interval: str
    bars: tuple[Bar, ...]
    source: str
    source_url: str
    requested_start: datetime
    requested_end: datetime

    def __len__(self) -> int:
        return len(self.bars)

    def __iter__(self):
        return iter(self.bars)

    def __getitem__(self, index):
        return self.bars[index]

    @property
    def is_empty(self) -> bool:
        return not self.bars

    @property
    def start(self) -> datetime | None:
        return self.bars[0].timestamp if self.bars else None

    @property
    def end(self) -> datetime | None:
        return self.bars[-1].timestamp if self.bars else None

    @property
    def high(self) -> Decimal | None:
        return max((b.high for b in self.bars), default=None)

    @property
    def low(self) -> Decimal | None:
        return min((b.low for b in self.bars), default=None)

    def find_missing_bars(
        self, interval_seconds: int = 60, *, include_edges: bool = True
    ) -> tuple[BarGap, ...]:
        """找出时间轴上缺失的 K 线。

        ``include_edges=True``（默认）时同时检查**请求窗口首尾**的覆盖缺口：
        数据源常有发布延迟，只检查 K 线之间的空隙会把「窗口尾部的缺口」漏掉，
        让调用方误以为整个报告间隔都被覆盖了。
        """
        if not self.bars:
            if not include_edges:
                return ()
            total = int((self.requested_end - self.requested_start).total_seconds())
            if total < interval_seconds:
                return ()
            return (
                BarGap(
                    after=self.requested_start,
                    before=self.requested_end,
                    missing_count=total // interval_seconds,
                    seconds=total,
                ),
            )

        gaps: list[BarGap] = []
        if include_edges:
            leading = int((self.bars[0].timestamp - self.requested_start).total_seconds())
            if leading >= interval_seconds:
                gaps.append(
                    BarGap(
                        after=self.requested_start,
                        before=self.bars[0].timestamp,
                        missing_count=leading // interval_seconds,
                        seconds=leading,
                    )
                )
        for prev, curr in zip(self.bars, self.bars[1:]):
            delta = int((curr.timestamp - prev.timestamp).total_seconds())
            if delta > interval_seconds:
                gaps.append(
                    BarGap(
                        after=prev.timestamp,
                        before=curr.timestamp,
                        missing_count=delta // interval_seconds - 1,
                        seconds=delta,
                    )
                )
        if include_edges:
            trailing = int((self.requested_end - self.bars[-1].timestamp).total_seconds())
            if trailing >= interval_seconds:
                gaps.append(
                    BarGap(
                        after=self.bars[-1].timestamp,
                        before=self.requested_end,
                        missing_count=trailing // interval_seconds,
                        seconds=trailing,
                    )
                )
        return tuple(gaps)

    def coverage(self, interval_seconds: int = 60) -> Decimal:
        """请求窗口的 K 线覆盖率（实际根数 / 按 ``interval_seconds`` 期望根数）。

        返回值区间 ``[0, 1]``；窗口内应无 K 线时记为 1。用于让调用方判断
        「报告间隔内是否真的拿到了完整数据」，避免在缺数据时静默下结论。
        """
        expected = int((self.requested_end - self.requested_start).total_seconds()) // (
            interval_seconds
        )
        if expected <= 0:
            return Decimal("1")
        return Decimal(min(len(self.bars), expected)) / Decimal(expected)

    def find_price_gaps(self, min_gap: Decimal | int | str) -> tuple[PriceGap, ...]:
        """找出价格跳空幅度不小于 ``min_gap`` 的相邻 K 线对。"""
        threshold = _to_decimal(min_gap, "min_gap")
        gaps: list[PriceGap] = []
        for prev, curr in zip(self.bars, self.bars[1:]):
            candidate = PriceGap(
                timestamp=curr.timestamp, prev_close=prev.close, next_open=curr.open
            )
            if abs(candidate.gap) >= threshold:
                gaps.append(candidate)
        return tuple(gaps)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [bar.as_dict() for bar in self.bars]

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "source": self.source,
            "source_url": self.source_url,
            "requested_start": self.requested_start.isoformat(timespec="seconds"),
            "requested_end": self.requested_end.isoformat(timespec="seconds"),
            "bar_count": len(self.bars),
            "bars": self.as_dicts(),
        }


@dataclass(frozen=True, slots=True)
class MarketContext:
    """从 :class:`~config.Config` 提取的、数据层需要的少量参数。

    这样各 Provider 不必直接依赖 Config，测试时也能轻松构造。
    """

    symbol: str
    tz: tzinfo
    spread_jpy: Decimal

    @classmethod
    def from_config(cls, cfg: Config, symbol: str | None = None) -> MarketContext:
        return cls(
            symbol=symbol or cfg.instrument.symbol,
            tz=cfg.tz,
            spread_jpy=cfg.market.spread_jpy,
        )

    @property
    def base_currency(self) -> str:
        return self.symbol.split("/")[0]

    @property
    def quote_currency(self) -> str:
        return self.symbol.split("/")[1]

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def to_local(self, moment: datetime) -> datetime:
        if moment.tzinfo is None:
            raise InvalidDataError(f"时间戳缺少时区信息: {moment!r}")
        return moment.astimezone(self.tz)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


def _to_decimal(value: Any, field_name: str) -> Decimal:
    """把接口返回值转成 Decimal；失败即视为无效数据。"""
    if isinstance(value, bool) or value is None:
        raise InvalidDataError(f"{field_name} 缺失或类型非法: {value!r}")
    if isinstance(value, Decimal):
        result = value
    else:
        text = str(value).strip()
        if not text:
            raise InvalidDataError(f"{field_name} 为空")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise InvalidDataError(f"{field_name} 不是合法数字: {value!r}") from exc
    if not result.is_finite():
        raise InvalidDataError(f"{field_name} 不是有限数字: {value!r}")
    return result


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    result = _to_decimal(value, field_name)
    if result <= 0:
        raise InvalidDataError(f"{field_name} 必须为正数，收到 {result}")
    return result


def _quantize(value: Decimal, places: int) -> Decimal:
    """按指定小数位量化（四舍五入），用于抹掉 JSON 浮点噪声。

    量化是单调不减的，因此不会破坏 ``high >= open/close`` 之类的大小关系。
    """
    if places <= 0:
        return value
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _parse_epoch(seconds: Any, field_name: str, ctx: MarketContext) -> datetime:
    try:
        moment = datetime.fromtimestamp(int(seconds), timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        raise InvalidDataError(f"{field_name} 不是合法的时间戳: {seconds!r}") from exc
    return ctx.to_local(moment)


def _parse_datetime_text(
    text: Any, field_name: str, ctx: MarketContext, *, tz: tzinfo | None = None
) -> datetime:
    """解析 ``YYYY-MM-DD HH:MM:SS`` 形式的时间戳。"""
    if not isinstance(text, str) or not text.strip():
        raise InvalidDataError(f"{field_name} 缺失或类型非法: {text!r}")
    raw = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return ctx.to_local(naive.replace(tzinfo=tz or timezone.utc))
    raise InvalidDataError(f"{field_name} 无法解析为时间: {text!r}")


def _http_get_json(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """带重试的 GET + JSON 解析，并把网络异常翻译成本模块的异常类型。

    * 超时 / 连接失败 / 5xx：重试 ``retries`` 次后抛 :class:`DataSourceError`；
    * HTTP 429：直接抛 :class:`RateLimitError`（重试无意义，交给上层换源）；
    * HTTP 404：抛 :class:`InvalidDataError`（通常是 symbol 不受支持）；
    * JSON 解析失败 / 结构异常：抛 :class:`InvalidDataError`。
    """
    merged_headers = dict(_DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)

    last_error: BaseException | None = None
    body: bytes | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=merged_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
            last_error = None
            break
        except urllib.error.HTTPError as exc:
            detail = _read_error_body(exc)
            if exc.code == 429:
                raise RateLimitError(f"数据源限流（HTTP 429）: {url} {detail}") from exc
            if exc.code == 404:
                raise InvalidDataError(
                    f"数据源返回 404，symbol 可能不受支持: {url} {detail}"
                ) from exc
            last_error = DataSourceError(f"数据源返回 HTTP {exc.code}: {url} {detail}")
            if exc.code < 500:
                # 4xx 重试无意义，直接失败并交给上层换源。
                raise last_error from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            last_error = DataSourceError(f"访问数据源失败（网络/超时）: {url} ({exc})")
        if attempt < retries:
            sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))

    if body is None:
        raise last_error or DataSourceError(f"访问数据源失败: {url}")
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise InvalidDataError(f"数据源返回的不是合法 JSON: {url}") from exc


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read(_MAX_ERROR_BODY_CHARS).decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - 读取错误响应体失败不影响主流程
        return ""


def _load_local_secrets(project_root: Path) -> Mapping[str, Any]:
    """读取被 .gitignore 排除的本地密钥文件；不存在或格式错误时返回空。"""
    path = project_root / SECRETS_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    section = data.get("market_data")
    return section if isinstance(section, dict) else {}


def _resolve_api_key(
    env_keys: Sequence[str],
    secret_key: str,
    *,
    project_root: Path,
    allow_demo_key: bool,
    demo_key: str = "demo",
) -> tuple[str | None, str]:
    """按「环境变量 → config.local.json → demo」的顺序解析凭据。

    返回 ``(密钥, 来源说明)``。源码中不硬编码任何真实密钥。
    """
    for env_name in env_keys:
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip(), f"env:{env_name}"
    secret = _load_local_secrets(project_root).get(secret_key)
    if isinstance(secret, str) and secret.strip():
        return secret.strip(), f"{SECRETS_FILENAME}:market_data.{secret_key}"
    if allow_demo_key:
        return demo_key, "demo（公共占位密钥，数据不可靠，仅供联调）"
    return None, "未配置"


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def _yahoo_price_scale(result: Mapping[str, Any]) -> int:
    """读取 Yahoo 自报的价格精度（``meta.scale`` / ``meta.priceHint``）。

    Yahoo 用 float64 返回价格（例如 157.90699768066406），按其自报精度量化可以
    抹掉浮点噪声，而不必自己发明一套小数位规则。
    """
    meta = result.get("meta")
    if isinstance(meta, dict):
        for key in ("scale", "priceHint"):
            raw = meta.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool) and 0 < raw <= 8:
                return raw
    return _DEFAULT_PRICE_SCALE


def validate_quote(quote: Quote, *, now: datetime | None = None) -> None:
    """校验报价的硬性约束，不合法则抛 :class:`InvalidDataError`。

    检查项：价格为正数、Bid 不高于 Ask、mid 与买卖价自洽、时间戳带时区且不在
    未来（允许少量时钟偏差）。本函数幂等，Provider 与公开入口都会调用。
    """
    for name in ("bid", "ask", "mid"):
        value = getattr(quote, name)
        if not isinstance(value, Decimal) or value <= 0:
            raise InvalidDataError(f"{quote.source}: {name} 必须为正数，收到 {value!r}")
    if quote.bid > quote.ask:
        raise InvalidDataError(
            f"{quote.source}: Bid 高于 Ask（bid={quote.bid} > ask={quote.ask}）"
        )
    centre = (quote.bid + quote.ask) / 2
    if not (quote.bid <= quote.mid <= quote.ask) and abs(quote.mid - centre) > (
        centre * _MID_RELATIVE_TOLERANCE
    ):
        raise InvalidDataError(
            f"{quote.source}: mid={quote.mid} 与买卖价 [{quote.bid}, {quote.ask}] 严重不一致"
        )
    if quote.timestamp.tzinfo is None:
        raise InvalidDataError(f"{quote.source}: 时间戳缺少时区信息")
    reference = now or datetime.now(timezone.utc)
    if quote.timestamp > reference + timedelta(seconds=_FUTURE_TOLERANCE_SECONDS):
        raise InvalidDataError(
            f"{quote.source}: 时间戳位于未来（{quote.timestamp.isoformat()} > "
            f"{reference.isoformat()}）"
        )


def validate_bars(bars: Iterable[Bar], *, source: str = "?") -> None:
    """校验 K 线的价格关系，不合法则抛 :class:`InvalidDataError`。"""
    for bar in bars:
        for name in ("open", "high", "low", "close"):
            value = getattr(bar, name)
            if not isinstance(value, Decimal) or value <= 0:
                raise InvalidDataError(
                    f"{source}: {bar.timestamp.isoformat()} 的 {name} 必须为正数，收到 {value!r}"
                )
        if bar.high < bar.low:
            raise InvalidDataError(
                f"{source}: {bar.timestamp.isoformat()} 的 high < low"
                f"（{bar.high} < {bar.low}）"
            )
        if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
            raise InvalidDataError(
                f"{source}: {bar.timestamp.isoformat()} 的 high/low 未覆盖 open/close"
            )


def default_stale_after_seconds(cfg: Config) -> float:
    """由 config 推导默认的过期阈值（监控间隔 × :data:`STALE_MONITOR_INTERVALS`）。"""
    return float(cfg.market.monitor_interval_seconds * STALE_MONITOR_INTERVALS)


def _finalize_quote(quote: Quote, *, now: datetime, stale_after_seconds: float) -> Quote:
    """补齐 ``is_stale`` / ``age_seconds`` / ``received_at``。"""
    age = Decimal(str(round((now - quote.timestamp).total_seconds(), 3)))
    return replace(
        quote,
        is_stale=age > Decimal(str(stale_after_seconds)),
        age_seconds=age,
        received_at=now,
    )


# --------------------------------------------------------------------------- #
# Provider 抽象
# --------------------------------------------------------------------------- #


class MarketDataProvider(ABC):
    """行情数据源接口。"""

    #: 稳定的数据源标识，会写入 ``Quote.source``。
    name: str = "abstract"
    #: 该源主页（用于文档与排查）。
    homepage: str = ""
    #: 是否提供 1 分钟 K 线。
    supports_1m_history: bool = False

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
        http_get: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self._http_get = http_get or _http_get_json
        self._sleep = sleep

    def _get_json(self, url: str) -> Any:
        return self._http_get(url, timeout=self.timeout, retries=self.retries, sleep=self._sleep)

    @abstractmethod
    def fetch_quote(self, ctx: MarketContext) -> Quote:
        """取当前报价；失败时抛 :class:`MarketDataError` 子类。"""

    def fetch_bars(
        self, ctx: MarketContext, start: datetime, end: datetime, *, interval: str = "1m"
    ) -> BarSeries:
        """取历史 K 线；不支持时抛 :class:`HistoryUnavailableError`。"""
        raise HistoryUnavailableError(
            f"{self.name} 不提供 {interval} 历史数据（该源只用于报价兜底）"
        )

    # -- 构造辅助 ---------------------------------------------------------- #

    def _synthetic_quote(
        self,
        ctx: MarketContext,
        *,
        mid: Decimal,
        timestamp: datetime,
        source_url: str,
        granularity: Granularity,
    ) -> Quote:
        """由中间价 + config 模拟点差合成 Bid/Ask，并标记为模拟报价。"""
        half = ctx.spread_jpy / 2
        quote = Quote(
            symbol=ctx.symbol,
            bid=mid - half,
            ask=mid + half,
            mid=mid,
            timestamp=timestamp,
            source=self.name,
            source_url=source_url,
            quote_type=QuoteType.SYNTHETIC_SPREAD,
            granularity=granularity,
        )
        validate_quote(quote)
        return quote

    def _bid_ask_quote(
        self,
        ctx: MarketContext,
        *,
        bid: Decimal,
        ask: Decimal,
        mid: Decimal | None,
        timestamp: datetime,
        source_url: str,
        granularity: Granularity,
    ) -> Quote:
        quote = Quote(
            symbol=ctx.symbol,
            bid=bid,
            ask=ask,
            mid=mid if mid is not None else (bid + ask) / 2,
            timestamp=timestamp,
            source=self.name,
            source_url=source_url,
            quote_type=QuoteType.BID_ASK,
            granularity=granularity,
        )
        validate_quote(quote)
        return quote

    def __repr__(self) -> str:  # pragma: no cover - 便于日志排查
        return f"<{type(self).__name__} name={self.name!r}>"


# --------------------------------------------------------------------------- #
# Yahoo Finance（无需密钥）
# --------------------------------------------------------------------------- #


class YahooChartProvider(MarketDataProvider):
    """Yahoo Finance chart 接口。免密钥，提供 1 分钟 OHLC，但只有指示性中间价。"""

    name = "yahoo_finance_chart"
    homepage = "https://finance.yahoo.com/quote/USDJPY=X"
    supports_1m_history = True
    BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

    @staticmethod
    def yahoo_symbol(symbol: str) -> str:
        """``USD/JPY`` → ``USDJPY=X``（Yahoo 对 ``JPY=X`` 亦等价）。"""
        return symbol.replace("/", "").upper() + "=X"

    def _chart(self, symbol: str, range_param: str, interval: str = "1m") -> tuple[dict, str]:
        url = (
            f"{self.BASE_URL}{self.yahoo_symbol(symbol)}"
            f"?interval={urllib.parse.quote(interval)}&range={urllib.parse.quote(range_param)}"
        )
        payload = self._get_json(url)
        if not isinstance(payload, dict):
            raise InvalidDataError(f"{self.name}: 响应不是 JSON 对象")
        chart = payload.get("chart")
        if not isinstance(chart, dict):
            raise InvalidDataError(f"{self.name}: 响应缺少 chart 字段")
        if chart.get("error"):
            raise InvalidDataError(f"{self.name}: {chart['error']}")
        results = chart.get("result")
        if not isinstance(results, list) or not results:
            raise InvalidDataError(f"{self.name}: chart.result 为空")
        result = results[0]
        if not isinstance(result, dict):
            raise InvalidDataError(f"{self.name}: chart.result[0] 不是对象")
        return result, url

    def fetch_quote(self, ctx: MarketContext) -> Quote:
        result, url = self._chart(ctx.symbol, "1d", "1m")
        bars = self._parse_bars(result, ctx, url)
        if bars:
            # 用最近一根 K 线的收盘价作为中间价：与历史数据同源，口径一致。
            last = bars[-1]
            return self._synthetic_quote(
                ctx,
                mid=last.close,
                timestamp=last.timestamp + timedelta(minutes=1),
                source_url=url,
                granularity=Granularity.MINUTE,
            )
        # 兜底：meta 里的当前价（可能是取整后的指示价）。
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        moment = meta.get("regularMarketTime")
        if price is None or moment is None:
            raise InvalidDataError(f"{self.name}: 既无 K 线也无 meta.regularMarketPrice")
        return self._synthetic_quote(
            ctx,
            mid=_quantize(
                _positive_decimal(price, f"{self.name}.meta.regularMarketPrice"),
                _yahoo_price_scale(result),
            ),
            timestamp=_parse_epoch(moment, f"{self.name}.meta.regularMarketTime", ctx),
            source_url=url,
            granularity=Granularity.MINUTE,
        )

    def fetch_bars(
        self, ctx: MarketContext, start: datetime, end: datetime, *, interval: str = "1m"
    ) -> BarSeries:
        if interval != "1m":
            raise HistoryUnavailableError(f"{self.name} 只提供 1 分钟 K 线，不支持 {interval!r}")
        start = ctx.to_local(start)
        end = ctx.to_local(end)
        span_days = (end - start).total_seconds() / 86400
        range_param = next((rng for limit, rng in _YAHOO_RANGES if span_days <= limit), None)
        if range_param is None:
            raise HistoryUnavailableError(
                f"{self.name}: 1 分钟数据只支持约 7 天窗口（实测 range=7d 返回约 9 天），"
                f"请求跨度 {span_days:.1f} 天"
            )
        result, url = self._chart(ctx.symbol, range_param, interval)
        bars = self._parse_bars(result, ctx, url)
        window = tuple(bar for bar in bars if start <= bar.timestamp <= end)
        if not window and end < ctx.now() - timedelta(days=_YAHOO_1M_LOOKBACK_DAYS):
            raise HistoryUnavailableError(
                f"{self.name}: 请求窗口（{start.isoformat()} ~ {end.isoformat()}）早于 "
                f"1 分钟数据的回溯上限（约 {_YAHOO_1M_LOOKBACK_DAYS} 天），无法提供"
            )
        return BarSeries(
            symbol=ctx.symbol,
            interval=interval,
            bars=window,
            source=self.name,
            source_url=url,
            requested_start=start,
            requested_end=end,
        )

    def _parse_bars(self, result: Mapping[str, Any], ctx: MarketContext, url: str) -> tuple[Bar, ...]:
        timestamps = result.get("timestamp")
        indicators = result.get("indicators")
        if not isinstance(timestamps, list) or not isinstance(indicators, dict):
            return ()
        quotes = indicators.get("quote")
        if not isinstance(quotes, list) or not quotes or not isinstance(quotes[0], dict):
            return ()
        series = quotes[0]
        opens = series.get("open") or []
        highs = series.get("high") or []
        lows = series.get("low") or []
        closes = series.get("close") or []
        volumes = series.get("volume") or []
        # Yahoo 用 float64 返回价格，按其自报精度量化以抹掉浮点噪声。
        scale = _yahoo_price_scale(result)

        bars: list[Bar] = []
        for index, epoch in enumerate(timestamps):
            try:
                o, h, l, c = (
                    opens[index],
                    highs[index],
                    lows[index],
                    closes[index],
                )
            except IndexError:
                continue
            if o is None or h is None or l is None or c is None:
                # 源站会在休市/无成交的分钟里填 null，直接跳过。
                continue
            volume = volumes[index] if index < len(volumes) else None
            bars.append(
                Bar(
                    timestamp=_parse_epoch(epoch, f"{self.name}.timestamp", ctx),
                    open=_quantize(_positive_decimal(o, f"{self.name}.open"), scale),
                    high=_quantize(_positive_decimal(h, f"{self.name}.high"), scale),
                    low=_quantize(_positive_decimal(l, f"{self.name}.low"), scale),
                    close=_quantize(_positive_decimal(c, f"{self.name}.close"), scale),
                    volume=None if volume in (None, 0) else _to_decimal(volume, f"{self.name}.volume"),
                    source=self.name,
                    interval="1m",
                    is_synthetic=True,  # Yahoo FX 的 OHLC 是指示性报价，不是可成交价
                )
            )
        validate_bars(bars, source=self.name)
        return tuple(bars)


# --------------------------------------------------------------------------- #
# Alpha Vantage（需密钥）
# --------------------------------------------------------------------------- #


class AlphaVantageProvider(MarketDataProvider):
    """Alpha Vantage。唯一在免费档就同时给出 Bid/Ask 与 1 分钟 K 线的源。

    .. warning::
       ``CURRENCY_EXCHANGE_RATE`` 的 ``Last Refreshed`` 是 **API 刷新时刻**，
       不是市场成交时刻。实测在同一时刻连续请求，时间戳会推进而价格不变。
       因此本模块只把它记录为 ``timestamp``，**不能**据此认为行情新鲜。
    """

    name = "alpha_vantage"
    homepage = "https://www.alphavantage.co/documentation/"
    supports_1m_history = True
    BASE_URL = "https://www.alphavantage.co/query"
    ENV_KEYS: Final[tuple[str, ...]] = ("ALPHAVANTAGE_API_KEY", "ALPHA_VANTAGE_API_KEY")

    def __init__(
        self,
        api_key: str,
        *,
        key_origin: str = "explicit",
        outputsize: str = "compact",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key
        self.key_origin = key_origin
        self.outputsize = outputsize

    @classmethod
    def from_environment(
        cls,
        project_root: Path,
        *,
        allow_demo_key: bool = False,
        **kwargs: Any,
    ) -> AlphaVantageProvider | None:
        api_key, origin = _resolve_api_key(
            cls.ENV_KEYS, "alphavantage_api_key", project_root=project_root,
            allow_demo_key=allow_demo_key,
        )
        if api_key is None:
            return None
        return cls(api_key, key_origin=origin, **kwargs)

    def _request(self, params: Mapping[str, str]) -> tuple[Mapping[str, Any], str]:
        query = dict(params)
        query["apikey"] = self.api_key
        url = f"{self.BASE_URL}?{urllib.parse.urlencode(query)}"
        payload = self._get_json(url)
        if not isinstance(payload, dict):
            raise InvalidDataError(f"{self.name}: 响应不是 JSON 对象")
        self._raise_for_business_error(payload)
        return payload, url

    def _raise_for_business_error(self, payload: Mapping[str, Any]) -> None:
        if "Note" in payload:
            # 免费档 25 次/日、5 次/分钟，超限时 AV 用 Note 字段提示。
            raise RateLimitError(f"{self.name} 限流: {payload['Note']}")
        if "Information" in payload:
            info = str(payload["Information"])
            if "rate limit" in info.lower() or "premium" in info.lower():
                raise RateLimitError(f"{self.name} 拒绝请求（可能为限流或需付费档）: {info}")
            raise DataSourceError(f"{self.name}: {info}")
        if "Error Message" in payload:
            raise InvalidDataError(f"{self.name}: {payload['Error Message']}")

    def fetch_quote(self, ctx: MarketContext) -> Quote:
        payload, url = self._request(
            {
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": ctx.base_currency,
                "to_currency": ctx.quote_currency,
            }
        )
        node = payload.get("Realtime Currency Exchange Rate")
        if not isinstance(node, dict):
            raise InvalidDataError(f"{self.name}: 响应缺少 Realtime Currency Exchange Rate")
        mid = _positive_decimal(node.get("5. Exchange Rate"), f"{self.name}.Exchange Rate")
        # "6. Last Refreshed" 是 API 刷新时刻（见类文档），仅作记录。
        timestamp = _parse_datetime_text(
            node.get("6. Last Refreshed"), f"{self.name}.Last Refreshed", ctx, tz=timezone.utc
        )
        raw_bid = node.get("8. Bid Price")
        raw_ask = node.get("9. Ask Price")
        bid = _to_decimal(raw_bid, f"{self.name}.Bid Price") if raw_bid else None
        ask = _to_decimal(raw_ask, f"{self.name}.Ask Price") if raw_ask else None
        if bid is not None and ask is not None and bid > 0 and ask > 0 and bid <= ask:
            return self._bid_ask_quote(
                ctx, bid=bid, ask=ask, mid=mid, timestamp=timestamp,
                source_url=url, granularity=Granularity.TICK,
            )
        # 某些货币对不返回 Bid/Ask：退回「中间价 + 模拟点差」，并明确标记。
        return self._synthetic_quote(
            ctx, mid=mid, timestamp=timestamp, source_url=url, granularity=Granularity.TICK
        )

    def fetch_bars(
        self, ctx: MarketContext, start: datetime, end: datetime, *, interval: str = "1m"
    ) -> BarSeries:
        if interval != "1m":
            raise HistoryUnavailableError(f"{self.name} 只提供 1 分钟 K 线，不支持 {interval!r}")
        payload, url = self._request(
            {
                "function": "FX_INTRADAY",
                "from_symbol": ctx.base_currency,
                "to_symbol": ctx.quote_currency,
                "interval": "1min",
                "outputsize": self.outputsize,
            }
        )
        series = payload.get("Time Series FX (1min)")
        if not isinstance(series, dict) or not series:
            raise InvalidDataError(
                f"{self.name}: 响应缺少 Time Series FX (1min)"
                f"（免费档仅最近 100 根，如需更长区间需付费档）"
            )
        start = ctx.to_local(start)
        end = ctx.to_local(end)
        bars: list[Bar] = []
        for stamp, values in series.items():
            if not isinstance(values, dict):
                continue
            moment = _parse_datetime_text(stamp, f"{self.name}.Time Series", ctx, tz=timezone.utc)
            if not (start <= moment <= end):
                continue
            bars.append(
                Bar(
                    timestamp=moment,
                    open=_positive_decimal(values.get("1. open"), f"{self.name}.open"),
                    high=_positive_decimal(values.get("2. high"), f"{self.name}.high"),
                    low=_positive_decimal(values.get("3. low"), f"{self.name}.low"),
                    close=_positive_decimal(values.get("4. close"), f"{self.name}.close"),
                    source=self.name,
                    interval="1m",
                    is_synthetic=False,
                )
            )
        bars.sort(key=lambda bar: bar.timestamp)
        validate_bars(bars, source=self.name)
        return BarSeries(
            symbol=ctx.symbol, interval=interval, bars=tuple(bars), source=self.name,
            source_url=url, requested_start=start, requested_end=end,
        )


# --------------------------------------------------------------------------- #
# Twelve Data（需密钥）
# --------------------------------------------------------------------------- #


class TwelveDataProvider(MarketDataProvider):
    """Twelve Data。``/quote`` 与 ``/time_series`` 都可能带 Bid/Ask。

    官方 ``demo`` 密钥实测数据不可靠（周六仍报 ``is_market_open=true``，
    且 ``/quote`` 不含 Bid/Ask），因此只在显式允许时启用。
    """

    name = "twelve_data"
    homepage = "https://twelvedata.com/docs"
    supports_1m_history = True
    BASE_URL = "https://api.twelvedata.com"
    ENV_KEYS: Final[tuple[str, ...]] = ("TWELVEDATA_API_KEY", "TWELVE_DATA_API_KEY")

    def __init__(self, api_key: str, *, key_origin: str = "explicit", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key
        self.key_origin = key_origin

    @classmethod
    def from_environment(
        cls, project_root: Path, *, allow_demo_key: bool = False, **kwargs: Any
    ) -> TwelveDataProvider | None:
        api_key, origin = _resolve_api_key(
            cls.ENV_KEYS, "twelvedata_api_key", project_root=project_root,
            allow_demo_key=allow_demo_key,
        )
        if api_key is None:
            return None
        return cls(api_key, key_origin=origin, **kwargs)

    def _request(self, path: str, params: Mapping[str, str]) -> tuple[Mapping[str, Any], str]:
        query = dict(params)
        query["apikey"] = self.api_key
        url = f"{self.BASE_URL}/{path}?{urllib.parse.urlencode(query)}"
        payload = self._get_json(url)
        if not isinstance(payload, dict):
            raise InvalidDataError(f"{self.name}: 响应不是 JSON 对象")
        if payload.get("status") == "error" or "code" in payload:
            code = payload.get("code")
            message = str(payload.get("message") or payload)
            if code == 429 or "limit" in message.lower():
                raise RateLimitError(f"{self.name} 限流: {message}")
            if code in (401, 403):
                raise DataSourceError(f"{self.name} 凭据无效（HTTP {code}）: {message}")
            raise DataSourceError(f"{self.name} 返回错误: {message}")
        return payload, url

    def fetch_quote(self, ctx: MarketContext) -> Quote:
        payload, url = self._request("quote", {"symbol": ctx.symbol})
        mid = _positive_decimal(payload.get("close"), f"{self.name}.close")
        # last_quote_at 是最后一笔报价时刻；timestamp 是所属 K 线时刻。
        moment_raw = payload.get("last_quote_at") or payload.get("timestamp")
        if moment_raw is None:
            raise InvalidDataError(f"{self.name}: 响应缺少时间戳")
        timestamp = _parse_epoch(moment_raw, f"{self.name}.timestamp", ctx)
        raw_bid, raw_ask = payload.get("bid"), payload.get("ask")
        if raw_bid and raw_ask:
            bid = _to_decimal(raw_bid, f"{self.name}.bid")
            ask = _to_decimal(raw_ask, f"{self.name}.ask")
            if bid > 0 and ask > 0 and bid <= ask:
                return self._bid_ask_quote(
                    ctx, bid=bid, ask=ask, mid=mid, timestamp=timestamp,
                    source_url=url, granularity=Granularity.TICK,
                )
        return self._synthetic_quote(
            ctx, mid=mid, timestamp=timestamp, source_url=url, granularity=Granularity.MINUTE
        )

    def fetch_bars(
        self, ctx: MarketContext, start: datetime, end: datetime, *, interval: str = "1m"
    ) -> BarSeries:
        if interval != "1m":
            raise HistoryUnavailableError(f"{self.name} 只提供 1 分钟 K 线，不支持 {interval!r}")
        start = ctx.to_local(start)
        end = ctx.to_local(end)
        payload, url = self._request(
            "time_series",
            {
                "symbol": ctx.symbol,
                "interval": "1min",
                "timezone": "UTC",
                "start_date": start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "outputsize": "5000",
                "order": "ASC",
            },
        )
        values = payload.get("values")
        if not isinstance(values, list):
            raise InvalidDataError(f"{self.name}: 响应缺少 values 数组")
        bars: list[Bar] = []
        for item in values:
            if not isinstance(item, dict):
                continue
            bars.append(
                Bar(
                    timestamp=_parse_datetime_text(
                        item.get("datetime"), f"{self.name}.datetime", ctx, tz=timezone.utc
                    ),
                    open=_positive_decimal(item.get("open"), f"{self.name}.open"),
                    high=_positive_decimal(item.get("high"), f"{self.name}.high"),
                    low=_positive_decimal(item.get("low"), f"{self.name}.low"),
                    close=_positive_decimal(item.get("close"), f"{self.name}.close"),
                    volume=(
                        _to_decimal(item["volume"], f"{self.name}.volume")
                        if item.get("volume") not in (None, "")
                        else None
                    ),
                    source=self.name,
                    interval="1m",
                    is_synthetic=False,
                )
            )
        bars.sort(key=lambda bar: bar.timestamp)
        validate_bars(bars, source=self.name)
        return BarSeries(
            symbol=ctx.symbol, interval=interval, bars=tuple(bars), source=self.name,
            source_url=url, requested_start=start, requested_end=end,
        )


# --------------------------------------------------------------------------- #
# Frankfurter / ECB（无需密钥，仅兜底）
# --------------------------------------------------------------------------- #


class FrankfurterProvider(MarketDataProvider):
    """Frankfurter（ECB 参考汇率）。**只有每日中间价，没有 1 分钟数据。**

    按 60 秒监控时它必然被判为过期 —— 这是正确行为，不是 bug。仅作为
    「其他源全部故障时至少有一个可解释的价格」的最后兜底。
    """

    name = "frankfurter_ecb"
    homepage = "https://frankfurter.dev/"
    supports_1m_history = False
    BASE_URL = "https://api.frankfurter.dev/v1"

    def fetch_quote(self, ctx: MarketContext) -> Quote:
        url = (
            f"{self.BASE_URL}/latest?base={urllib.parse.quote(ctx.base_currency)}"
            f"&symbols={urllib.parse.quote(ctx.quote_currency)}"
        )
        payload = self._get_json(url)
        if not isinstance(payload, dict):
            raise InvalidDataError(f"{self.name}: 响应不是 JSON 对象")
        rates = payload.get("rates")
        if not isinstance(rates, dict) or ctx.quote_currency not in rates:
            raise InvalidDataError(f"{self.name}: 响应缺少 {ctx.quote_currency} 汇率")
        mid = _positive_decimal(rates[ctx.quote_currency], f"{self.name}.rates")
        # ECB 参考汇率是「按日」发布的，没有盘中时刻；这里取参考日期的 00:00 UTC
        # 作为占位时间戳，并如实标记 granularity=daily。
        reference_date = payload.get("date")
        if not isinstance(reference_date, str) or not reference_date.strip():
            raise InvalidDataError(f"{self.name}: 响应缺少 date 字段")
        timestamp = _parse_datetime_text(
            f"{reference_date.strip()} 00:00:00", f"{self.name}.date", ctx, tz=timezone.utc
        )
        return self._synthetic_quote(
            ctx, mid=mid, timestamp=timestamp, source_url=url, granularity=Granularity.DAILY
        )


# --------------------------------------------------------------------------- #
# Provider 链与公开入口
# --------------------------------------------------------------------------- #

PROVIDER_REGISTRY: Final[dict[str, type[MarketDataProvider]]] = {
    YahooChartProvider.name: YahooChartProvider,
    AlphaVantageProvider.name: AlphaVantageProvider,
    TwelveDataProvider.name: TwelveDataProvider,
    FrankfurterProvider.name: FrankfurterProvider,
}


def build_provider_chain(
    cfg: Config,
    *,
    need_history: bool = False,
    allow_demo_key: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    http_get: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[MarketDataProvider]:
    """按优先级构建数据源链。

    顺序：Alpha Vantage（有密钥时，可给真实 Bid/Ask）→ Twelve Data（有密钥时）
    → Yahoo（免密钥，主力历史源）→ Frankfurter（免密钥，仅报价兜底）。

    ``need_history=True`` 时会排除不提供 1 分钟 K 线的数据源。
    """
    options: dict[str, Any] = {
        "timeout": timeout,
        "retries": retries,
        "http_get": http_get,
        "sleep": sleep,
    }
    project_root = cfg.paths.project_root
    chain: list[MarketDataProvider] = []

    alphavantage = AlphaVantageProvider.from_environment(
        project_root, allow_demo_key=allow_demo_key, **options
    )
    if alphavantage is not None:
        chain.append(alphavantage)

    twelve_data = TwelveDataProvider.from_environment(
        project_root, allow_demo_key=allow_demo_key, **options
    )
    if twelve_data is not None:
        chain.append(twelve_data)

    chain.append(YahooChartProvider(**options))

    if not need_history:
        chain.append(FrankfurterProvider(**options))

    return [p for p in chain if (p.supports_1m_history or not need_history)]


def _select_providers(
    cfg: Config,
    *,
    provider: str | MarketDataProvider | None,
    need_history: bool,
    allow_demo_key: bool,
    timeout: float,
    retries: int,
    http_get: Callable[..., Any] | None,
    sleep: Callable[[float], None],
) -> list[MarketDataProvider]:
    if isinstance(provider, MarketDataProvider):
        return [provider]
    if isinstance(provider, str):
        if provider not in PROVIDER_REGISTRY:
            raise MarketDataError(
                f"未知数据源 {provider!r}；可用: {', '.join(sorted(PROVIDER_REGISTRY))}"
            )
        cls = PROVIDER_REGISTRY[provider]
        if issubclass(cls, (AlphaVantageProvider, TwelveDataProvider)):
            instance = cls.from_environment(  # type: ignore[attr-defined]
                cfg.paths.project_root, allow_demo_key=allow_demo_key,
                timeout=timeout, retries=retries, http_get=http_get, sleep=sleep,
            )
            if instance is None:
                raise DataSourceError(
                    f"数据源 {provider!r} 需要 API 密钥：请设置环境变量 "
                    f"{' 或 '.join(cls.ENV_KEYS)}，或写入 {SECRETS_FILENAME}"
                )
            return [instance]
        return [cls(timeout=timeout, retries=retries, http_get=http_get, sleep=sleep)]
    return build_provider_chain(
        cfg, need_history=need_history, allow_demo_key=allow_demo_key,
        timeout=timeout, retries=retries, http_get=http_get, sleep=sleep,
    )


def _run_chain(
    providers: Sequence[MarketDataProvider],
    action: Callable[[MarketDataProvider], Any],
    *,
    what: str,
) -> Any:
    """依次尝试链上的数据源，全部失败则抛 :class:`NoProviderAvailableError`。"""
    failures: list[tuple[str, BaseException]] = []
    for candidate in providers:
        try:
            return action(candidate)
        except MarketDataError as exc:
            failures.append((candidate.name, exc))
    detail = "；".join(f"{name}: {exc}" for name, exc in failures) or "链为空"
    raise NoProviderAvailableError(f"获取{what}失败，所有数据源均不可用 —— {detail}", failures)


def get_current_quote(
    cfg: Config | None = None,
    *,
    symbol: str | None = None,
    provider: str | MarketDataProvider | None = None,
    stale_after_seconds: float | None = None,
    require_fresh: bool = False,
    allow_demo_key: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    http_get: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Quote:
    """获取当前 USD/JPY 报价。

    Args:
        cfg: 配置；``None`` 时使用包内默认 ``config.json``。
        symbol: 覆盖交易品种（默认取 ``cfg.instrument.symbol``）。
        provider: 指定数据源（名称或实例）；``None`` 时按优先级自动选择。
        stale_after_seconds: 过期阈值；``None`` 时由 ``cfg`` 的监控间隔推导。
        require_fresh: 为 ``True`` 时，过期报价抛 :class:`StaleQuoteError`。
        allow_demo_key: 是否允许使用数据源的公共 demo 密钥（数据不可靠）。

    Raises:
        NoProviderAvailableError: 链上所有数据源都失败。
        StaleQuoteError: ``require_fresh=True`` 且报价过期。
    """
    cfg = cfg or load_config()
    ctx = MarketContext.from_config(cfg, symbol)
    threshold = (
        default_stale_after_seconds(cfg)
        if stale_after_seconds is None
        else float(stale_after_seconds)
    )
    providers = _select_providers(
        cfg, provider=provider, need_history=False, allow_demo_key=allow_demo_key,
        timeout=timeout, retries=retries, http_get=http_get, sleep=sleep,
    )

    def fetch_and_validate(candidate: MarketDataProvider) -> Quote:
        quote = candidate.fetch_quote(ctx)
        # 校验放在链内：某个源返回自相矛盾的数据时视为该源失败，自动换下一个源。
        validate_quote(quote, now=ctx.now())
        return quote

    quote = _run_chain(providers, fetch_and_validate, what=f"{ctx.symbol} 报价")
    finalized = _finalize_quote(quote, now=ctx.now(), stale_after_seconds=threshold)
    if require_fresh and finalized.is_stale:
        raise StaleQuoteError(
            f"{finalized.source} 的 {ctx.symbol} 报价已过期 "
            f"{finalized.age_seconds} 秒（阈值 {threshold:.0f} 秒）"
        )
    return finalized


def get_historical_bars(
    start: datetime,
    end: datetime,
    *,
    cfg: Config | None = None,
    symbol: str | None = None,
    interval: str = "1m",
    provider: str | MarketDataProvider | None = None,
    allow_demo_key: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    http_get: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> BarSeries:
    """获取指定时间区间的历史 K 线（默认 1 分钟）。

    用于检查报告间隔内是否发生止损、止盈或跳空。返回的 :class:`BarSeries`
    提供 :meth:`BarSeries.find_missing_bars` 与 :meth:`BarSeries.find_price_gaps`。

    ``start`` / ``end`` 可以是带时区的时间；无时区时按 ``cfg`` 的时区解释。

    Raises:
        HistoryUnavailableError: 所请求的粒度或时间窗超出所有数据源能力
            （例如 Yahoo 1 分钟数据只回溯约 9 天）。
        NoProviderAvailableError: 链上所有数据源都失败。
    """
    if interval != "1m":
        raise HistoryUnavailableError(
            f"本模块目前只支持 1 分钟 K 线，收到 interval={interval!r}"
        )
    cfg = cfg or load_config()
    ctx = MarketContext.from_config(cfg, symbol)
    if start.tzinfo is None:
        start = start.replace(tzinfo=ctx.tz)
    if end.tzinfo is None:
        end = end.replace(tzinfo=ctx.tz)
    if start >= end:
        raise HistoryUnavailableError(
            f"起始时间必须早于结束时间（start={start.isoformat()}, end={end.isoformat()}）"
        )
    providers = _select_providers(
        cfg, provider=provider, need_history=True, allow_demo_key=allow_demo_key,
        timeout=timeout, retries=retries, http_get=http_get, sleep=sleep,
    )
    return _run_chain(
        providers,
        lambda p: p.fetch_bars(ctx, start, end, interval=interval),
        what=f"{ctx.symbol} {interval} 历史数据",
    )


# --------------------------------------------------------------------------- #
# 按小时聚合（给 AI 上下文用的「价格区间」）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HourlyRange:
    """一个自然小时（东京时间）内的价格区间。

    AI 上下文里要的是「最近几小时在什么区间里走」，而不是 60 根 1 分钟 K 线——
    后者会把易变上下文撑大好几倍，而且每次决策都全额计费。所以这里做**降采样**。

    ``bar_count`` 为 0 表示该小时**没有可核验的 K 线**（休市或数据源缺数据），
    此时价格字段为 ``None``，绝不填 0 或沿用前值——「不知道」必须和「是 0」区分开。
    """

    hour: datetime
    bar_count: int
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    is_partial: bool
    source: str

    @property
    def has_data(self) -> bool:
        return self.bar_count > 0

    @property
    def range_jpy(self) -> Decimal | None:
        """该小时的高低价差；无数据时为 ``None``。"""
        if self.high is None or self.low is None:
            return None
        return self.high - self.low

    def as_dict(self) -> dict[str, Any]:
        return {
            "hour": self.hour.isoformat(timespec="minutes"),
            "bar_count": self.bar_count,
            "open": _text(self.open),
            "high": _text(self.high),
            "low": _text(self.low),
            "close": _text(self.close),
            "range_jpy": _text(self.range_jpy),
            "is_partial": self.is_partial,
            "has_data": self.has_data,
        }


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


#: USD/JPY 现货周的休市窗口（东京时间，保守取交集）。
#:
#: 真实的外汇现货周是「纽约时间周日 17:00 开 → 周五 17:00 收」，换算到 JST：
#:   EST（冬令时，UTC−5）：周一 07:00 开 → 周六 07:00 收
#:   EDT（夏令时，UTC−4）：周一 06:00 开 → 周六 06:00 收
#: 这里取两者的**交集**（周六 07:00 → 周一 06:00），宁可漏判也不误判——
#: 宁可多跑一次决策，也不能把可交易的时段当成休市。
#:
#: **这只是一个免费的预筛**，不处理美国夏令时切换的具体日期，也不认识节假日。
#: 权威信号永远是报价本身能不能用于决策（见 :attr:`Quote` 与 ``valid_for_decisions``）。
_WEEK_CLOSE_WEEKDAY: Final[int] = 5      # 周六
_WEEK_CLOSE_HOUR: Final[int] = 7         # 07:00 JST
_WEEK_OPEN_WEEKDAY: Final[int] = 0       # 周一
_WEEK_OPEN_HOUR: Final[int] = 6          # 06:00 JST


def forex_session_open(moment: datetime, *, tz: tzinfo) -> tuple[bool, str]:
    """按**周内时段**粗判外汇现货是否开市。返回 ``(是否开市, 说明)``。

    只用星期与小时做判断，不联网、不查日历。休市窗口取「周六 07:00 → 周一 06:00 JST」
    ——这是 EST 与 EDT 两种情况的交集，所以只会**漏判休市**，不会把开市误判成休市。
    """
    local = moment.astimezone(tz)
    weekday = local.weekday()   # 周一=0 … 周日=6
    hour = local.hour
    stamp = local.strftime("%Y-%m-%d %H:%M")
    if weekday == _WEEK_CLOSE_WEEKDAY and hour >= _WEEK_CLOSE_HOUR:
        return False, f"{stamp} JST 周六 {_WEEK_CLOSE_HOUR}:00 之后，外汇现货周已收市"
    if weekday == 6:            # 周日全天休市
        return False, f"{stamp} JST 周日，外汇现货休市"
    if weekday == _WEEK_OPEN_WEEKDAY and hour < _WEEK_OPEN_HOUR:
        return False, f"{stamp} JST 周一 {_WEEK_OPEN_HOUR}:00 之前，外汇现货周尚未开市"
    return True, f"{stamp} JST 处于外汇现货周内"


def hour_start(moment: datetime, *, tz: tzinfo) -> datetime:
    """把时刻截断到所在自然小时的起点（保留时区）。"""
    local = moment.astimezone(tz)
    return local.replace(minute=0, second=0, microsecond=0)


def aggregate_hourly_bars(
    bars: Sequence[Bar],
    *,
    tz: tzinfo,
    now: datetime | None = None,
    limit: int | None = None,
) -> tuple[HourlyRange, ...]:
    """把 1 分钟 K 线按**自然小时**聚合成区间，按时间正序返回。

    * 只统计**真实存在**的 K 线；缺数据的小时不会凭空造出来。
    * 给了 ``now`` 时会为「当前这个还没走完的小时」补一条 ``is_partial=True``
      的区间（哪怕它暂时没有 K 线），这样模型能区分「这一小时还没走完」和「这一小时没有数据」。
    * ``limit`` 只保留**最近 N 个**小时（在补完当前小时后截断）。
    """
    buckets: dict[datetime, list[Bar]] = {}
    for bar in bars:
        key = hour_start(bar.timestamp, tz=tz)
        buckets.setdefault(key, []).append(bar)

    current_hour = hour_start(now, tz=tz) if now is not None else None
    if current_hour is not None:
        buckets.setdefault(current_hour, [])

    ordered = sorted(buckets)
    if limit is not None and limit > 0:
        ordered = ordered[-int(limit):]

    result: list[HourlyRange] = []
    for key in ordered:
        group = sorted(buckets[key], key=lambda b: b.timestamp)
        if not group:
            result.append(
                HourlyRange(
                    hour=key, bar_count=0, open=None, high=None, low=None,
                    close=None, is_partial=key == current_hour, source="",
                )
            )
            continue
        result.append(
            HourlyRange(
                hour=key,
                bar_count=len(group),
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                is_partial=key == current_hour,
                source=group[0].source,
            )
        )
    return tuple(result)


def get_hourly_ranges(
    *,
    hours: int = 12,
    cfg: Config | None = None,
    symbol: str | None = None,
    now: datetime | None = None,
    http_get: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[HourlyRange, ...]:
    """取最近 ``hours`` 个自然小时的 1 分钟 K 线并聚合。

    拿不到历史数据时**抛** :class:`HistoryUnavailableError`，由调用方决定是
    「如实报告不可用」还是「继续决策但不带价格历史」——本函数不返回空壳糊弄过去。
    """
    cfg = cfg or load_config()
    ctx = MarketContext.from_config(cfg, symbol)
    moment = (now or datetime.now(ctx.tz)).astimezone(ctx.tz)
    end = moment
    start = hour_start(moment, tz=ctx.tz) - timedelta(hours=max(1, int(hours)) - 1)
    series = get_historical_bars(
        start, end, cfg=cfg, symbol=symbol, interval="1m",
        http_get=http_get, sleep=sleep,
    )
    return aggregate_hourly_bars(series.bars, tz=ctx.tz, now=moment, limit=hours)


# --------------------------------------------------------------------------- #
# 命令行自检
# --------------------------------------------------------------------------- #


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m market_data",
        description="实际请求行情接口，验证 USD/JPY 报价与 1 分钟 K 线是否可用。",
    )
    parser.add_argument("--symbol", default=None, help="交易品种（默认取 config.json）")
    parser.add_argument("--provider", default=None, help="指定数据源名称（默认自动选择）")
    parser.add_argument("--allow-demo-key", action="store_true", help="允许使用公共 demo 密钥")
    parser.add_argument("--history-minutes", type=int, default=0, help="额外拉取最近 N 分钟 K 线")
    parser.add_argument(
        "--history-end",
        default=None,
        help="历史窗口的结束时刻（ISO 8601，如 2026-10-09T21:00:00+00:00；默认当前时间）",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 1

    ctx = MarketContext.from_config(cfg, args.symbol)
    print(f"品种 {ctx.symbol} | 模拟点差 {ctx.spread_jpy} 日元 | 时区 {ctx.tz}")

    try:
        quote = get_current_quote(
            cfg, symbol=args.symbol, provider=args.provider,
            allow_demo_key=args.allow_demo_key,
        )
    except MarketDataError as exc:
        print(f"[行情错误] {type(exc).__name__}: {exc}")
        return 1

    print("\n=== 当前报价 ===")
    for key, value in quote.as_dict().items():
        print(f"  {key:14s}: {value}")
    if quote.is_synthetic:
        print("  [提示] Bid/Ask 由中间价 + 配置点差合成，属模拟报价。")

    if args.history_minutes > 0 or args.history_end:
        try:
            end = (
                datetime.fromisoformat(args.history_end)
                if args.history_end
                else ctx.now()
            )
        except ValueError as exc:
            print(f"[参数错误] --history-end 不是合法 ISO 时间: {exc}")
            return 1
        if end.tzinfo is None:
            end = end.replace(tzinfo=ctx.tz)
        minutes = args.history_minutes or 60
        start = end - timedelta(minutes=minutes)
        print(f"\n=== {start.isoformat()} ~ {end.isoformat()} 的 1 分钟 K 线 ===")
        try:
            series = get_historical_bars(
                start, end, cfg=cfg, symbol=args.symbol, provider=args.provider,
                allow_demo_key=args.allow_demo_key,
            )
        except MarketDataError as exc:
            print(f"[历史数据错误] {type(exc).__name__}: {exc}")
            return 1
        print(f"  数据源 {series.source} | 实际取得 {len(series)} 根")
        for bar in series[-5:]:
            print(
                f"  {bar.timestamp.isoformat(timespec='seconds')} "
                f"O={bar.open} H={bar.high} L={bar.low} C={bar.close}"
            )
        missing = series.find_missing_bars()
        print(f"  覆盖率 {series.coverage()} | 时间缺口 {len(missing)} 处")
        for gap in missing[:3]:
            print(
                f"    缺 {gap.missing_count} 根（{gap.seconds} 秒）: "
                f"{gap.after.isoformat(timespec='seconds')} → "
                f"{gap.before.isoformat(timespec='seconds')}"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
