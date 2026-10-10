"""FX 战士久留美 —— 独立风险监控与自动平仓。

定位
====

**本模块独立于交易决策**：它只观察账户与行情，在触发风险条件时执行模拟平仓。
不产生交易信号、不决定开仓（开仓由 :mod:`engine` 负责）。

依赖：:mod:`config`（规则）、:mod:`market_data`（行情）、:mod:`storage`（持久化）、
:mod:`engine`（执行与计算）。**不重复实现**其中任何一项。

监控条件（对应配置项）
======================

====================================== ==========================================
净值 / 保证金水平                        动作
====================================== ==========================================
保证金水平 ≤ ``margin_call_level``(150%)  记录预警事件 + 禁止新增仓位
保证金水平 ≤ ``stop_out_level``(100%)     立即强制平掉全部持仓
日内净值回撤 ≥ ``daily_drawdown_stop_ratio``(3%)  平掉全部持仓 + 暂停当日新开仓
净值 ≤ ``min_equity_to_open_jpy``(5000)  暂停新开仓
净值 ≤ ``bankruptcy_equity_jpy``(0)      标记爆仓，永久停止本局交易
====================================== ==========================================

「当日」以 **08:00 JST** 为界（:data:`engine.TRADING_DAY_BOUNDARY_HOUR`）：
``[08:00 JST 今天, 08:00 JST 明天)`` 属于同一个交易日，当日基准取该时段起点的净值。

止损/止盈判定与成交模型
========================

* 多仓用 **Bid** 判定并成交，空仓用 **Ask** 判定并成交（与 :mod:`engine` 规则 1/2 一致）；
* **正常触及**：K 线区间包含止损价 → 按止损价成交；止盈 → 按止盈价成交；
* **跳空越过止损**：可执行价格区间整体位于止损不利侧 → **按首个可核验的可执行
  报价叠加不利滑点成交**，不假设以止损价成交；
* **无法确认此前价位**（例如监控刚启动/刚重启）时，按**跳空**保守处理；
* **止盈不额外计滑点**，且不把有利跳空计入（按止盈价成交，偏保守）；
* **强制平仓**（强平/回撤/爆仓）一律按当前可执行报价叠加不利滑点成交。

同一根 K 线内同时触及止损与止盈、无法确定先后顺序时，**按止损先发生处理**
（对账户更不利）。实现上体现为「先判止损，命中即返回」。

幂等与事件留痕
==============

每个退出事件都有唯一键（``EXIT:STOP_LOSS:{position_id}`` 等），记录在审计日志
里，**启动时重建、之后内存维护**，因此同一事件跨重启也只执行一次。

每个事件都记录：触发时间、检测时间、参考报价、模拟成交价、原因、最终盈亏。

数据质量与诚实边界
==================

* 报价过期 → 禁止新开仓，并明确标记**已有持仓的风险监控能力受限**
  （仍按过期报价盯市，因为那是当前唯一可得的参考价）；
* 监控中断 → 尝试补查历史 1 分钟 K 线并回放检查；**无法核实的时段记为数据缺口**，
  **绝不虚构成交**；
* 历史行情源只回溯约 9 天（见 :mod:`market_data`），更早的中断窗口无法补查，
  会如实记为数据缺口。

安全边界
========

只做模拟交易：不连接任何真实券商接口、不下单、不做真实资金转账。
"""

from __future__ import annotations

import argparse
import itertools
import dataclasses
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Final, Iterable, Sequence

from config import Config, load_config
from engine import (
    CloseExecution,
    CloseReason,
    EngineError,
    ExitTrigger,
    MarkToMarket,
    QuoteUnusableError,
    TradingEngine,
    calculate_margin_level,
    trading_date_for,
)
from market_data import Bar, BarSeries, MarketDataError, Quote, get_current_quote, get_historical_bars
from storage import (
    AccountStatus,
    FlowType,
    Position,
    PositionSide,
    PositionStatus,
    RunStatus,
    Storage,
    StorageError,
)

__all__ = [
    "DataGap",
    "MonitorResult",
    "RiskEvent",
    "RiskEventKind",
    "RiskEventLedger",
    "RiskMonitor",
    "TASK_NAME",
    "open_monitor",
]

logger = logging.getLogger("kurumi_fx_sim.risk_monitor")

#: 定时任务运行记录里使用的任务名。
TASK_NAME: Final[str] = "risk_monitor"

#: 历史行情回放时使用的 K 线间隔（秒）。
_BAR_INTERVAL_SECONDS: Final[int] = 60

#: 监控间隔超过该倍数的监控周期时，视为「中断」并触发历史补查。
_INTERRUPT_INTERVALS: Final[Decimal] = Decimal("1.5")

#: 单次补查最多回放的 K 线数量上限，避免异常长窗口拖垮一次 tick。
_MAX_BACKFILL_BARS: Final[int] = 20000

_MONEY_EXPONENT: Final[Decimal] = Decimal("0.001")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def _dec(value: Any, field_name: str = "value") -> Decimal:
    if isinstance(value, bool):
        raise EngineError(f"{field_name} 不能是布尔值: {value!r}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise EngineError(f"{field_name} 不能为空")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise EngineError(f"{field_name} 不是合法十进制数字: {value!r}") from exc
    elif isinstance(value, float):
        result = Decimal(repr(value))
    else:
        raise EngineError(f"{field_name} 必须是 Decimal/int/str，收到 {type(value).__name__}")
    if not result.is_finite():
        raise EngineError(f"{field_name} 必须是有限数字: {value!r}")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_EXPONENT, rounding=ROUND_HALF_UP)


class RiskEventKind(str, Enum):
    """风险事件类型。"""

    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MARGIN_WARNING = "MARGIN_WARNING"
    MARGIN_CALL = "MARGIN_CALL"
    DAILY_DRAWDOWN = "DAILY_DRAWDOWN"
    MIN_EQUITY_HALT = "MIN_EQUITY_HALT"
    BANKRUPTCY = "BANKRUPTCY"
    DEGRADED_MONITORING = "DEGRADED_MONITORING"
    DATA_GAP = "DATA_GAP"
    MONITOR_RECOVERED = "MONITOR_RECOVERED"


@dataclass(frozen=True, slots=True)
class RiskEvent:
    """一次风险事件。字段覆盖要求的：触发时间、检测时间、参考报价、成交价、原因、最终盈亏。"""

    event_key: str
    kind: str
    triggered_at: datetime
    detected_at: datetime
    reason: str
    position_id: int | None = None
    reference_bid: Decimal | None = None
    reference_ask: Decimal | None = None
    reference_source: str | None = None
    fill_price: Decimal | None = None
    realized_pnl_jpy: Decimal | None = None
    is_gap: bool = False
    detail: dict[str, Any] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "kind": self.kind,
            "position_id": self.position_id,
            "triggered_at": self.triggered_at.isoformat(timespec="seconds"),
            "detected_at": self.detected_at.isoformat(timespec="seconds"),
            "reference_bid": str(self.reference_bid) if self.reference_bid is not None else None,
            "reference_ask": str(self.reference_ask) if self.reference_ask is not None else None,
            "reference_source": self.reference_source,
            "fill_price": str(self.fill_price) if self.fill_price is not None else None,
            "realized_pnl_jpy": (
                str(self.realized_pnl_jpy) if self.realized_pnl_jpy is not None else None
            ),
            "is_gap": self.is_gap,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class DataGap:
    """无法核实的行情时段。**这些时段绝不虚构成交。**"""

    start: datetime
    end: datetime
    seconds: int
    missing_bars: int | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(timespec="seconds"),
            "end": self.end.isoformat(timespec="seconds"),
            "seconds": self.seconds,
            "missing_bars": self.missing_bars,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class MonitorResult:
    """一次监控的结果。"""

    trading_date: str
    detected_at: datetime
    quote: Quote | None
    quote_ok: bool
    quote_is_stale: bool
    degraded: bool
    degradation_reasons: tuple[str, ...]
    data_gaps: tuple[DataGap, ...]
    events: tuple[RiskEvent, ...]
    closed: tuple[CloseExecution, ...]
    mark: MarkToMarket | None
    equity: Decimal | None
    margin_level: Decimal | None
    daily_drawdown_ratio: Decimal | None
    blocked_new_positions: bool
    block_reason: str | None
    backfilled: bool
    backfilled_bars: int

    @property
    def did_close(self) -> bool:
        return bool(self.closed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trading_date": self.trading_date,
            "detected_at": self.detected_at.isoformat(timespec="seconds"),
            "quote": self.quote.as_dict() if self.quote is not None else None,
            "quote_ok": self.quote_ok,
            "quote_is_stale": self.quote_is_stale,
            "degraded": self.degraded,
            "degradation_reasons": list(self.degradation_reasons),
            "data_gaps": [gap.as_dict() for gap in self.data_gaps],
            "events": [event.as_dict() for event in self.events],
            "closed": [item.as_dict() for item in self.closed],
            "mark": self.mark.as_dict() if self.mark is not None else None,
            "equity": str(self.equity) if self.equity is not None else None,
            "margin_level": str(self.margin_level) if self.margin_level is not None else None,
            "daily_drawdown_ratio": (
                str(self.daily_drawdown_ratio)
                if self.daily_drawdown_ratio is not None
                else None
            ),
            "blocked_new_positions": self.blocked_new_positions,
            "block_reason": self.block_reason,
            "backfilled": self.backfilled,
            "backfilled_bars": self.backfilled_bars,
        }


# --------------------------------------------------------------------------- #
# 事件去重账本
# --------------------------------------------------------------------------- #


class RiskEventLedger:
    """已触发的风险事件键集合。

    启动时从审计日志重建（审计是追加式 + 哈希链，天然不可篡改），之后在内存
    中维护，因此**同一事件跨进程重启也只执行一次**。
    """

    AUDIT_TYPE: Final[str] = "RISK_EVENT"

    def __init__(self, storage: Storage, *, preload: bool = True) -> None:
        self.storage = storage
        self._fired: set[str] = set()
        if preload:
            self.reload()

    def reload(self) -> int:
        """从审计日志重建已触发集合，返回加载到的事件数。"""
        self._fired = set()
        for event in self.storage.read_audit_events(event_type=self.AUDIT_TYPE):
            key = (event.payload or {}).get("event_key")
            if isinstance(key, str) and key:
                self._fired.add(key)
        return len(self._fired)

    def already_fired(self, event_key: str) -> bool:
        return event_key in self._fired

    def mark_fired(self, event: RiskEvent) -> None:
        """把事件写入审计日志（追加式），并加入内存集合。"""
        self._fired.add(event.event_key)
        self.storage.audit(
            self.AUDIT_TYPE,
            event.as_dict(),
            occurred_at=event.detected_at,
        )


# --------------------------------------------------------------------------- #
# 监控器
# --------------------------------------------------------------------------- #


class RiskMonitor:
    """独立风险监控器。

    典型用法::

        monitor = RiskMonitor.from_config()          # 用 config.json + 默认数据库
        result = monitor.monitor_once()              # 单次检查
        monitor.run_forever()                        # 持续监控（默认 60 秒一次）

    测试或离线回放时可注入行情源，避免访问网络::

        monitor = RiskMonitor(cfg, engine, quote_source=lambda: fake_quote,
                              bars_source=lambda s, e: fake_series)
    """

    def __init__(
        self,
        cfg: Config | None = None,
        engine: TradingEngine | None = None,
        *,
        storage: Storage | None = None,
        quote_source: Callable[[], Quote] | None = None,
        bars_source: Callable[[datetime, datetime], BarSeries] | None = None,
        logger_: logging.Logger | None = None,
        stale_tolerance_intervals: Decimal | int | str = Decimal("3"),
    ) -> None:
        self.cfg: Config = cfg or load_config()
        self.engine: TradingEngine = engine or TradingEngine(self.cfg)
        self.storage: Storage = storage or self.engine.storage
        self.logger = logger_ or logger
        self._quote_source = quote_source or (lambda: get_current_quote(self.cfg))
        self._bars_source = bars_source or (
            lambda start, end: get_historical_bars(start, end, cfg=self.cfg)
        )
        self.ledger = RiskEventLedger(self.storage)
        self.stale_tolerance_intervals = _dec(
            stale_tolerance_intervals, "stale_tolerance_intervals"
        )
        #: 上一次检查时刻（进程内）。为空时回退到定时任务运行记录。
        self._last_check_at: datetime | None = None
        #: 上一次看到的可执行价格（按持仓），用于判断「跳空越过止损」。
        self._last_executable: dict[int, Decimal] = {}

    # -- 构造 -------------------------------------------------------------- #

    @classmethod
    def from_config(
        cls, cfg: Config | None = None, *, db_path: str | None = None, **kwargs: Any
    ) -> RiskMonitor:
        cfg = cfg or load_config()
        engine = TradingEngine.from_config(cfg, db_path=db_path)
        return cls(cfg, engine, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - 便于日志排查
        return f"<RiskMonitor {self.cfg.instrument.symbol} db={self.storage.db_path}>"

    # -- 辅助 -------------------------------------------------------------- #

    def trading_date(self, moment: datetime | None = None) -> str:
        return trading_date_for(
            moment or datetime.now(self.cfg.tz), self.cfg.tz
        ).isoformat()

    def _half_spread(self) -> Decimal:
        """单边点差（用于把 K 线的指示性价格换算成可执行价格）。"""
        return _dec(self.cfg.market.spread_jpy) / 2

    def _slippage(self) -> Decimal:
        return _dec(self.cfg.market.slippage_jpy)

    @staticmethod
    def _executable(quote: Quote, side: PositionSide) -> Decimal:
        """可执行平仓价：平多走 Bid，平空走 Ask（规则 1/2）。"""
        return _dec(quote.bid if side is PositionSide.LONG else quote.ask, "executable")

    def _adverse(self, price: Decimal, side: PositionSide) -> Decimal:
        """叠加不利滑点：平多更低、平空更高。"""
        slip = self._slippage()
        return price - slip if side is PositionSide.LONG else price + slip

    def open_positions(self) -> list[Position]:
        return self.storage.read_positions(status=PositionStatus.OPEN)

    def _resolve_last_check_at(self) -> datetime | None:
        if self._last_check_at is not None:
            return self._last_check_at
        runs = self.storage.read_scheduler_runs(task_name=TASK_NAME, limit=1)
        if not runs:
            return None
        return runs[0].started_at

    # -- 事件构造 ---------------------------------------------------------- #

    def _make_event(
        self,
        kind: RiskEventKind,
        *,
        event_key: str,
        detected_at: datetime,
        reason: str,
        triggered_at: datetime | None = None,
        position_id: int | None = None,
        quote: Quote | None = None,
        fill_price: Decimal | None = None,
        realized_pnl_jpy: Decimal | None = None,
        is_gap: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> RiskEvent:
        return RiskEvent(
            event_key=event_key,
            kind=kind.value,
            triggered_at=triggered_at or detected_at,
            detected_at=detected_at,
            reason=reason,
            position_id=position_id,
            reference_bid=_dec(quote.bid) if quote is not None else None,
            reference_ask=_dec(quote.ask) if quote is not None else None,
            reference_source=getattr(quote, "source", None) if quote is not None else None,
            fill_price=fill_price,
            realized_pnl_jpy=realized_pnl_jpy,
            is_gap=is_gap,
            detail=detail or {},
        )

    # -- 止损/止盈的成交价模型 --------------------------------------------- #

    def _stop_fill(
        self,
        *,
        side: PositionSide,
        stop: Decimal,
        executable: Decimal,
        previous_executable: Decimal | None,
    ) -> tuple[Decimal, bool, str]:
        """决定止损的模拟成交价。返回 ``(成交价, 是否跳空, 说明)``。

        * 此前可执行价在止损**近侧**、现在越过 → 止损价在两次观测之间被穿越，
          按止损价成交；
        * 此前已在止损不利侧、或**无法确认此前价位** → 视为跳空，按当前可执行价
          叠加不利滑点成交（保守）。
        """
        crossed: bool
        if previous_executable is None:
            crossed = False
            note_prefix = "无法确认此前价位"
        else:
            crossed = (
                previous_executable > stop
                if side is PositionSide.LONG
                else previous_executable < stop
            )
            note_prefix = "此前价位在止损近侧"
        if crossed:
            return (
                stop,
                False,
                f"{note_prefix}，止损价 {stop} 在两次观测之间被穿越，按止损价成交",
            )
        fill = self._adverse(executable, side)
        return (
            fill,
            True,
            f"{note_prefix}，价格跳空越过止损价 {stop}，"
            f"按首个可核验的可执行报价 {executable} 加不利滑点 "
            f"{self._slippage()} 成交 → {fill}",
        )

    def _quote_trigger(
        self, position: Position, quote: Quote
    ) -> ExitTrigger | None:
        """用当前报价判定止损/止盈，并给出符合本模块成交模型的成交价。"""
        trigger = self.engine.evaluate_exit_triggers(position, quote)
        if trigger is None:
            return None
        side = PositionSide(position.side)
        executable = self._executable(quote, side)
        if trigger.reason is CloseReason.STOP_LOSS:
            fill, is_gap, note = self._stop_fill(
                side=side,
                stop=_dec(trigger.trigger_price),
                executable=executable,
                previous_executable=self._last_executable.get(position.position_id),
            )
            return dataclasses.replace(
                trigger, fill_price=fill, is_gap=is_gap, note=note
            )
        # 止盈：按止盈价成交，不额外计滑点，也不把有利跳空计入（保守）
        return dataclasses.replace(
            trigger,
            fill_price=_dec(trigger.trigger_price),
            is_gap=False,
            note=(
                f"止盈价 {trigger.trigger_price} 被触及，按止盈价成交"
                "（不计有利滑点，偏保守）"
            ),
        )

    def _bar_trigger(self, position: Position, bar: Bar) -> ExitTrigger | None:
        """用一根历史 K 线判定止损/止盈。

        K 线只有指示性 OHLC，因此先按单边点差换算成**可执行价格区间**再用其
        极值判定，避免用中间价判定却在另一个价位成交。

        同一根 K 线内同时触及止损与止盈时**先判止损**（对账户更不利）。
        """
        side = PositionSide(position.side)
        half = self._half_spread()
        if side is PositionSide.LONG:
            low = _dec(bar.low) - half
            high = _dec(bar.high) - half
            open_price = _dec(bar.open) - half
        else:
            low = _dec(bar.low) + half
            high = _dec(bar.high) + half
            open_price = _dec(bar.open) + half

        if position.stop_loss is not None:
            stop = _dec(position.stop_loss)
            hit = low <= stop if side is PositionSide.LONG else high >= stop
            if hit:
                if low <= stop <= high:
                    return ExitTrigger(
                        position_id=position.position_id,
                        reason=CloseReason.STOP_LOSS,
                        trigger_price=stop,
                        fill_price=stop,
                        is_gap=False,
                        note=(
                            f"K 线 {bar.timestamp.isoformat(timespec='seconds')} 的可执行区间"
                            f" [{low}, {high}] 包含止损价 {stop}，按止损价成交"
                        ),
                    )
                fill = self._adverse(open_price, side)
                return ExitTrigger(
                    position_id=position.position_id,
                    reason=CloseReason.STOP_LOSS,
                    trigger_price=stop,
                    fill_price=fill,
                    is_gap=True,
                    note=(
                        f"K 线 {bar.timestamp.isoformat(timespec='seconds')} 的可执行区间"
                        f" [{low}, {high}] 整体位于止损不利侧（跳空），"
                        f"按该 K 线首个可执行报价 {open_price} 加不利滑点成交 → {fill}"
                    ),
                )
        if position.take_profit is not None:
            target = _dec(position.take_profit)
            hit = high >= target if side is PositionSide.LONG else low <= target
            if hit:
                return ExitTrigger(
                    position_id=position.position_id,
                    reason=CloseReason.TAKE_PROFIT,
                    trigger_price=target,
                    fill_price=target,
                    is_gap=False,
                    note=(
                        f"K 线 {bar.timestamp.isoformat(timespec='seconds')} 触及止盈价 "
                        f"{target}，按止盈价成交（不计有利滑点，偏保守）"
                    ),
                )
        return None

    # -- 单次监控 ---------------------------------------------------------- #

    def monitor_once(
        self,
        quote: Quote | None = None,
        *,
        now: datetime | None = None,
        backfill: bool = True,
        last_check_at: datetime | None = None,
    ) -> MonitorResult:
        """执行一次完整监控：取价 → 补查 → 触发检查 → 盯市 → 账户级风控。

        ``last_check_at`` 可显式指定「上一次检查时刻」，用于判断监控是否中断。
        调度器需要它：调度器会先写自己的任务运行记录，若让监控器自己去
        ``scheduler_runs`` 里取「最近一次运行」，取到的会是刚写下的这条，
        补查窗口就被自己吃掉了。
        """
        detected_at = now or datetime.now(self.cfg.tz)
        trading_day = self.trading_date(detected_at)
        events: list[RiskEvent] = []
        gaps: list[DataGap] = []
        closed: list[CloseExecution] = []
        degradations: list[str] = []

        # 1) 取得最新报价（可注入，测试时无需联网）
        quote_error: str | None = None
        if quote is None:
            try:
                quote = self._quote_source()
            except MarketDataError as exc:
                quote_error = f"行情获取失败：{exc}"
            except Exception as exc:  # noqa: BLE001 - 行情源任何异常都不应中断监控
                quote_error = f"行情获取异常：{type(exc).__name__}: {exc}"

        quote_ok = False
        quote_is_stale = False
        if quote is not None:
            try:
                self.engine.check_quote(quote, allow_stale=True)
                quote_ok = True
            except QuoteUnusableError as exc:
                quote_error = f"报价不可用：{exc}"
                quote = None
        if quote is not None:
            quote_is_stale = bool(getattr(quote, "is_stale", False))
            if quote_is_stale:
                degradations.append(
                    f"报价已过期（{quote.source}，age={getattr(quote, 'age_seconds', '?')} 秒）："
                    "已禁止新开仓，且已有持仓的风险监控能力受限（仍按该报价盯市）"
                )
        if quote is None:
            degradations.append(quote_error or "无可用报价")

        # 2) 确保当日基准存在（08:00 JST 分界），并回放中断期间的历史行情
        try:
            self.engine.ensure_daily_baseline(quote)
        except (EngineError, StorageError) as exc:  # pragma: no cover - 防御性
            self.logger.warning("确立当日基准失败：%s", exc)

        backfilled_bars = 0
        backfilled = False
        if backfill and quote is not None:
            last_check = (
                last_check_at if last_check_at is not None else self._resolve_last_check_at()
            )
            if last_check is not None:
                elapsed = detected_at - last_check
                threshold = timedelta(
                    seconds=float(
                        self.cfg.market.monitor_interval_seconds
                        * self.stale_tolerance_intervals
                    )
                )
                if elapsed > threshold:
                    backfilled = True
                    backfilled_bars = self._backfill_and_check(
                        last_check, detected_at, detected_at, events, gaps, closed
                    )

        # 3) 用当前报价检查止损/止盈（历史回放之后，保证时间顺序）
        if quote is not None:
            for position in self.open_positions():
                trigger = self._quote_trigger(position, quote)
                self._last_executable[position.position_id] = self._executable(
                    quote, PositionSide(position.side)
                )
                if trigger is None:
                    continue
                self._execute_exit(
                    position, trigger, quote, detected_at, detected_at, events, closed
                )

        # 4) 盯市（更新浮盈、净值、保证金水平）
        mark: MarkToMarket | None = None
        if quote is not None:
            mark = self.engine.mark_to_market(quote, occurred_at=detected_at)

        # 5) 账户级风控
        account_events = self._check_account_limits(
            detected_at=detected_at,
            trading_day=trading_day,
            quote=quote,
            events=events,
            closed=closed,
            degradations=degradations,
        )
        if account_events and quote is not None:
            mark = self.engine.mark_to_market(quote, occurred_at=detected_at)

        # 6) 汇总阻断状态
        state = self.storage.read_account_state()
        blocked, block_reason = self._blocking_state(
            state=state, degraded=bool(degradations), mark=mark
        )

        self._last_check_at = detected_at
        equity = mark.equity if mark is not None else None
        margin_level = mark.margin_level if mark is not None else None
        drawdown = mark.daily_drawdown_ratio if mark is not None else None
        self.logger.info(
            "监控完成 trading_day=%s 净值=%s 保证金水平=%s 回撤=%s 事件=%d 平仓=%d 阻断=%s",
            trading_day, equity, margin_level, drawdown, len(events), len(closed),
            block_reason or "无",
        )
        return MonitorResult(
            trading_date=trading_day,
            detected_at=detected_at,
            quote=quote,
            quote_ok=quote_ok,
            quote_is_stale=quote_is_stale,
            degraded=bool(degradations),
            degradation_reasons=tuple(degradations),
            data_gaps=tuple(gaps),
            events=tuple(events),
            closed=tuple(closed),
            mark=mark,
            equity=equity,
            margin_level=margin_level,
            daily_drawdown_ratio=drawdown,
            blocked_new_positions=blocked,
            block_reason=block_reason,
            backfilled=backfilled,
            backfilled_bars=backfilled_bars,
        )

    # -- 历史补查 ---------------------------------------------------------- #

    def _backfill_and_check(
        self,
        start: datetime,
        end: datetime,
        detected_at: datetime,
        events: list[RiskEvent],
        gaps: list[DataGap],
        closed: list[CloseExecution],
    ) -> int:
        """补查 ``[start, end]`` 的历史 1 分钟 K 线并回放止损/止盈。

        无法取得或无法核实的时段记为数据缺口，**不虚构成交**。
        """
        seconds = int((end - start).total_seconds())
        self.logger.info(
            "检测到监控中断：补查 %s ~ %s（%d 秒）", start.isoformat(), end.isoformat(), seconds
        )
        try:
            series = self._bars_source(start, end)
        except MarketDataError as exc:
            self._record_gap(
                DataGap(
                    start=start, end=end, seconds=seconds, missing_bars=None,
                    reason=f"历史行情不可用，该时段无法核实：{exc}",
                ),
                detected_at, events, gaps,
            )
            return 0
        except Exception as exc:  # noqa: BLE001 - 行情源异常不应中断监控
            self._record_gap(
                DataGap(
                    start=start, end=end, seconds=seconds, missing_bars=None,
                    reason=f"历史行情异常，该时段无法核实：{type(exc).__name__}: {exc}",
                ),
                detected_at, events, gaps,
            )
            return 0

        # 覆盖率缺口（含首尾），逐段记为数据缺口
        for gap in series.find_missing_bars(_BAR_INTERVAL_SECONDS):
            self._record_gap(
                DataGap(
                    start=gap.after,
                    end=gap.before,
                    seconds=gap.seconds,
                    missing_bars=gap.missing_count,
                    reason="历史 K 线存在缺口，该时段无法核实是否有止损/止盈成交",
                ),
                detected_at, events, gaps,
            )

        # 按时间顺序回放；同一根 K 线内止损优先（对账户更不利）
        bars: Iterable[Bar] = series
        count = 0
        for bar in bars:
            if count >= _MAX_BACKFILL_BARS:
                self._record_gap(
                    DataGap(
                        start=bar.timestamp, end=end,
                        seconds=int((end - bar.timestamp).total_seconds()),
                        missing_bars=None,
                        reason=f"回放超过 {_MAX_BACKFILL_BARS} 根 K 线上限，剩余时段未核实",
                    ),
                    detected_at, events, gaps,
                )
                break
            count += 1
            for position in self.open_positions():
                trigger = self._bar_trigger(position, bar)
                if trigger is None:
                    continue
                self._execute_exit(
                    position, trigger, None, bar.timestamp, detected_at, events, closed
                )
        return count

    def _record_gap(
        self,
        gap: DataGap,
        detected_at: datetime,
        events: list[RiskEvent],
        gaps: list[DataGap],
    ) -> None:
        gaps.append(gap)
        event = self._make_event(
            RiskEventKind.DATA_GAP,
            event_key=f"DATA_GAP:{gap.start.isoformat()}:{gap.end.isoformat()}",
            detected_at=detected_at,
            triggered_at=gap.start,
            reason=gap.reason,
            detail=gap.as_dict(),
        )
        events.append(event)
        self.ledger.mark_fired(event)
        self.logger.warning("数据缺口 %s ~ %s：%s", gap.start, gap.end, gap.reason)

    # -- 执行平仓 ---------------------------------------------------------- #

    def _execute_exit(
        self,
        position: Position,
        trigger: ExitTrigger,
        quote: Quote | None,
        triggered_at: datetime,
        detected_at: datetime,
        events: list[RiskEvent],
        closed: list[CloseExecution],
    ) -> CloseExecution | None:
        """执行一次退出（止损/止盈/强平）。同一事件只执行一次。"""
        kind = RiskEventKind(trigger.reason.value)
        event_key = f"EXIT:{trigger.reason.value}:{position.position_id}"
        if self.ledger.already_fired(event_key):
            self.logger.info("退出事件 %s 已执行过，跳过", event_key)
            return None

        # 平仓需要一个报价对象：历史回放时用「参考报价」构造一个可执行价位的合成报价
        close_quote = quote or self._synthetic_quote(position, trigger, triggered_at)
        try:
            execution = self.engine.close_position(
                position.position_id,
                close_quote,
                reason=trigger.reason,
                occurred_at=triggered_at,
                fill_price=trigger.fill_price,
                fill_note=trigger.note,
            )
        except (EngineError, StorageError) as exc:
            self.logger.error("平仓失败 position=%s: %s", position.position_id, exc)
            return None

        self._last_executable.pop(position.position_id, None)
        closed.append(execution)
        event = self._make_event(
            kind,
            event_key=event_key,
            detected_at=detected_at,
            triggered_at=triggered_at,
            reason=trigger.note,
            position_id=position.position_id,
            quote=close_quote,
            fill_price=trigger.fill_price,
            realized_pnl_jpy=execution.realized_pnl_jpy,
            is_gap=trigger.is_gap,
            detail={
                "trigger_price": str(trigger.trigger_price),
                "trigger_reason": trigger.reason.value,
                "entry_price": str(position.entry_price),
                "side": position.side,
                "volume_usd": str(position.volume),
                "is_gap": trigger.is_gap,
            },
        )
        events.append(event)
        self.ledger.mark_fired(event)
        self.logger.warning(
            "%s position=%s 成交价=%s 盈亏=%s（%s）",
            trigger.reason.value, position.position_id, trigger.fill_price,
            execution.realized_pnl_jpy, trigger.note,
        )
        return execution

    def _synthetic_quote(
        self, position: Position, trigger: ExitTrigger, moment: datetime
    ) -> Quote:
        """历史回放时的参考报价。

        K 线没有 Bid/Ask，因此以「成交价反推」的中间价构造一个只用于记录的报价
        对象；真正的成交价由 ``trigger.fill_price`` 显式传给引擎，不会用这里的
        数字二次计算。
        """
        from market_data import Granularity, QuoteType

        half = self._half_spread()
        side = PositionSide(position.side)
        if side is PositionSide.LONG:
            bid = trigger.fill_price
            ask = trigger.fill_price + half * 2
        else:
            ask = trigger.fill_price
            bid = trigger.fill_price - half * 2
        return Quote(
            symbol=self.cfg.instrument.symbol,
            bid=bid,
            ask=ask,
            mid=(bid + ask) / 2,
            timestamp=moment,
            source="risk_monitor_backfill",
            source_url="",
            quote_type=QuoteType.SYNTHETIC_SPREAD,
            granularity=Granularity.MINUTE,
            is_stale=True,
        )

    # -- 账户级风控 -------------------------------------------------------- #

    def _check_account_limits(
        self,
        *,
        detected_at: datetime,
        trading_day: str,
        quote: Quote | None,
        events: list[RiskEvent],
        closed: list[CloseExecution],
        degradations: list[str],
    ) -> bool:
        """按严重程度依次检查：爆仓 → 强平 → 当日回撤 → 预警 → 净值过低。

        返回是否执行过强制平仓。
        """
        state = self.storage.read_account_state()
        equity = state.equity
        margin_level = calculate_margin_level(equity, state.margin_used)
        baseline = self.storage.read_daily_baseline(trading_day)
        day_start = baseline.day_start_equity if baseline is not None else state.day_start_equity
        drawdown = (day_start - equity) / day_start if day_start > 0 else Decimal("0")
        did_close = False

        # (a) 净值 ≤ 0：标记爆仓，永久停止本局交易
        if equity <= self.cfg.margin.bankruptcy_equity_jpy:
            event_key = "BANKRUPTCY_ACCOUNT"
            if not self.ledger.already_fired(event_key):
                self._force_close_all(
                    detected_at, CloseReason.BANKRUPTCY, quote, events, closed
                )
                did_close = True
                event = self._make_event(
                    RiskEventKind.BANKRUPTCY,
                    event_key=event_key,
                    detected_at=detected_at,
                    reason=(
                        f"净值 {equity} 不高于爆仓阈值 "
                        f"{self.cfg.margin.bankruptcy_equity_jpy}，标记爆仓并永久停止本局交易"
                    ),
                    quote=quote,
                    detail={"equity": str(equity), "permanent": True},
                )
                events.append(event)
                self.ledger.mark_fired(event)
                self.logger.error("爆仓：净值 %s，已强制平掉全部持仓并永久停止", equity)
            return did_close

        # (b) 保证金水平 ≤ 强平线：立即强制平掉全部持仓
        if margin_level is not None and margin_level <= self.cfg.margin.stop_out_level:
            for position in self.open_positions():
                trigger = self._forced_trigger(
                    position, CloseReason.MARGIN_CALL, quote, detected_at,
                    f"保证金水平 {margin_level:.4f} 不高于强平线 {self.cfg.margin.stop_out_level}",
                )
                if self._execute_exit(
                    position, trigger, quote, detected_at, detected_at, events, closed
                ):
                    did_close = True

        # (c) 当日回撤达到停止阈值：平掉全部持仓并暂停当日新开仓
        if drawdown >= self.cfg.risk.daily_drawdown_stop_ratio:
            already_flagged = baseline is not None and baseline.drawdown_stop_triggered
            if not already_flagged:
                self.engine.set_daily_drawdown_stop(
                    triggered_at=detected_at,
                    notes=f"回撤 {drawdown:.4f} 达到 {self.cfg.risk.daily_drawdown_stop_ratio}",
                )
            for position in self.open_positions():
                trigger = self._forced_trigger(
                    position, CloseReason.DAILY_DRAWDOWN, quote, detected_at,
                    f"当日净值回撤 {drawdown:.4f} 达到停止阈值 "
                    f"{self.cfg.risk.daily_drawdown_stop_ratio}",
                )
                if self._execute_exit(
                    position, trigger, quote, detected_at, detected_at, events, closed
                ):
                    did_close = True

        # (d) 保证金水平 ≤ 预警线：记录预警并禁止新增仓位
        if margin_level is not None and margin_level <= self.cfg.margin.margin_call_level:
            event_key = f"MARGIN_WARNING:{trading_day}"
            if not self.ledger.already_fired(event_key):
                event = self._make_event(
                    RiskEventKind.MARGIN_WARNING,
                    event_key=event_key,
                    detected_at=detected_at,
                    reason=(
                        f"保证金水平 {margin_level:.4f} 不高于预警线 "
                        f"{self.cfg.margin.margin_call_level}，记录预警并禁止新增仓位"
                    ),
                    quote=quote,
                    detail={
                        "equity": str(equity),
                        "margin_used": str(state.margin_used),
                        "margin_level": str(margin_level),
                        "blocked_new_positions": True,
                    },
                )
                events.append(event)
                self.ledger.mark_fired(event)
                self.logger.warning(
                    "保证金预警：水平 %s ≤ %s，禁止新增仓位",
                    margin_level, self.cfg.margin.margin_call_level,
                )

        # (e) 净值 ≤ 停止开仓阈值：暂停新开仓
        if equity <= self.cfg.margin.min_equity_to_open_jpy:
            event_key = f"MIN_EQUITY_HALT:{trading_day}"
            if not self.ledger.already_fired(event_key):
                event = self._make_event(
                    RiskEventKind.MIN_EQUITY_HALT,
                    event_key=event_key,
                    detected_at=detected_at,
                    reason=(
                        f"净值 {equity} 不高于停止开仓阈值 "
                        f"{self.cfg.margin.min_equity_to_open_jpy}，暂停新开仓"
                    ),
                    quote=quote,
                    detail={"equity": str(equity), "blocked_new_positions": True},
                )
                events.append(event)
                self.ledger.mark_fired(event)
                self.logger.warning("净值 %s 过低，暂停新开仓", equity)

        # (f) 数据质量降级：禁止新开仓
        if degradations:
            event_key = f"DEGRADED:{trading_day}"
            if not self.ledger.already_fired(event_key):
                event = self._make_event(
                    RiskEventKind.DEGRADED_MONITORING,
                    event_key=event_key,
                    detected_at=detected_at,
                    reason=(
                        "行情数据不可用或已过期：禁止新开仓；已有持仓的风险监控能力受限。"
                        + "；".join(degradations)
                    ),
                    quote=quote,
                    detail={
                        "degradation_reasons": list(degradations),
                        "blocked_new_positions": True,
                    },
                )
                events.append(event)
                self.ledger.mark_fired(event)
        return did_close

    def _forced_trigger(
        self,
        position: Position,
        reason: CloseReason,
        quote: Quote | None,
        detected_at: datetime,
        why: str,
    ) -> ExitTrigger:
        """构造强制平仓的退出事件：按可执行报价叠加不利滑点成交。"""
        side = PositionSide(position.side)
        if quote is not None:
            executable = self._executable(quote, side)
        else:
            # 没有报价时用止损价或开仓价作为可执行价的下界估计（并如实说明）
            fallback = position.stop_loss or position.entry_price
            executable = _dec(fallback)
        fill = self._adverse(executable, side)
        return ExitTrigger(
            position_id=position.position_id,
            reason=reason,
            trigger_price=executable,
            fill_price=fill,
            is_gap=quote is None,
            note=(
                f"{why}；按{'当前' if quote is not None else '最近可得'}可执行报价 "
                f"{executable} 加不利滑点 {self._slippage()} 成交 → {fill}"
            ),
        )

    def _force_close_all(
        self,
        detected_at: datetime,
        reason: CloseReason,
        quote: Quote | None,
        events: list[RiskEvent],
        closed: list[CloseExecution],
    ) -> None:
        for position in self.open_positions():
            trigger = self._forced_trigger(
                position, reason, quote, detected_at, f"强制平仓：{reason.value}"
            )
            self._execute_exit(
                position, trigger, quote, detected_at, detected_at, events, closed
            )

    def _blocking_state(
        self, *, state: Any, degraded: bool, mark: MarkToMarket | None
    ) -> tuple[bool, str | None]:
        """汇总当前是否禁止新开仓及原因。"""
        reasons: list[str] = []
        if degraded:
            reasons.append("行情数据不可用或已过期")
        if state.status == AccountStatus.BANKRUPT.value:
            reasons.append("账户已爆仓，永久停止")
        elif state.halted:
            reasons.append(state.halt_reason or "账户已停止开仓")
        if mark is not None and mark.margin_level is not None:
            if mark.margin_level <= self.cfg.margin.margin_call_level:
                reasons.append(
                    f"保证金水平 {mark.margin_level:.4f} 不高于预警线 "
                    f"{self.cfg.margin.margin_call_level}"
                )
        return (bool(reasons), "；".join(reasons) if reasons else None)

    # -- 运行记录与持续循环 ------------------------------------------------ #

    def recover_aborted_runs(self) -> int:
        """异常恢复：把上次进程遗留的 RUNNING 运行标记为 ABORTED。"""
        count = self.storage.recover_aborted_runs(task_name=TASK_NAME)
        if count:
            self.logger.warning("恢复：把 %d 条遗留的 RUNNING 监控运行标记为 ABORTED", count)
        return count

    def run_forever(
        self,
        *,
        interval_seconds: float | None = None,
        iterations: int | None = None,
        stop_event: threading.Event | None = None,
        max_consecutive_errors: int = 5,
        on_tick: Callable[[MonitorResult], None] | None = None,
    ) -> int:
        """持续监控循环，默认每 60 秒一次。

        * 正常退出：``KeyboardInterrupt`` 或 ``stop_event`` 被置位；
        * 异常恢复：单次 tick 抛异常时记录日志并按退避重试，连续失败达到
          ``max_consecutive_errors`` 才向上抛出；
        * 每次 tick 都写入 ``scheduler_runs``（含开始/结束状态），既是日志也是
          下次启动时判断「监控中断」的依据。

        返回实际执行的 tick 次数。
        """
        interval = (
            float(interval_seconds)
            if interval_seconds is not None
            else float(self.cfg.market.monitor_interval_seconds)
        )
        if interval <= 0:
            raise EngineError(f"监控间隔必须为正数: {interval}")
        self.recover_aborted_runs()
        self.logger.info(
            "风险监控启动：间隔 %.1f 秒，品种 %s，数据库 %s",
            interval, self.cfg.instrument.symbol, self.storage.db_path,
        )
        ticks = 0
        consecutive_errors = 0
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    self.logger.info("收到停止信号，正常退出")
                    break
                started = datetime.now(self.cfg.tz)
                # 每次 tick 都是一次独立运行，因此不传 event_key 去重：
                # 否则同一秒内的多次 tick 会被合并成同一条运行记录。
                run = self.storage.start_scheduler_run(TASK_NAME, started_at=started)
                try:
                    result = self.monitor_once(now=started)
                except Exception as exc:  # noqa: BLE001 - 单次失败不应终止循环
                    consecutive_errors += 1
                    self.storage.finish_scheduler_run(
                        run.run_no, status=RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}"
                    )
                    self.logger.exception(
                        "监控 tick 失败（连续第 %d 次）：%s", consecutive_errors, exc
                    )
                    if consecutive_errors >= max_consecutive_errors:
                        self.logger.error("连续失败达到 %d 次，停止监控", consecutive_errors)
                        raise
                else:
                    consecutive_errors = 0
                    ticks += 1
                    self.storage.finish_scheduler_run(
                        run.run_no,
                        status=RunStatus.SUCCESS,
                        detail=(
                            f"事件 {len(result.events)} 个，平仓 {len(result.closed)} 笔，"
                            f"净值 {result.equity}"
                        ),
                    )
                    if on_tick is not None:
                        on_tick(result)
                if iterations is not None and ticks >= iterations:
                    break
                if not self._sleep(interval, stop_event):
                    self.logger.info("等待期间收到停止信号，正常退出")
                    break
        except KeyboardInterrupt:
            self.logger.info("收到中断信号（KeyboardInterrupt），正常退出")
        finally:
            self.logger.info("风险监控结束，共执行 %d 次检查", ticks)
        return ticks

    @staticmethod
    def _sleep(interval: float, stop_event: threading.Event | None) -> bool:
        """分片睡眠，便于及时响应停止信号。返回 False 表示被要求停止。"""
        deadline = time.monotonic() + interval
        while True:
            if stop_event is not None and stop_event.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.5, remaining))


def open_monitor(cfg: Config | None = None, **kwargs: Any) -> RiskMonitor:
    """按配置创建风险监控器。"""
    return RiskMonitor.from_config(cfg, **kwargs)


# --------------------------------------------------------------------------- #
# 命令行演示（全部使用模拟行情，不访问网络）
# --------------------------------------------------------------------------- #


def _fake_quote(cfg: Config, bid: str, ask: str, *, stale: bool = False) -> Quote:
    from market_data import Granularity, QuoteType

    bid_d, ask_d = Decimal(bid), Decimal(ask)
    return Quote(
        symbol=cfg.instrument.symbol,
        bid=bid_d,
        ask=ask_d,
        mid=(bid_d + ask_d) / 2,
        timestamp=datetime.now(cfg.tz),
        source="risk_monitor_demo",
        source_url="https://example.invalid/demo",
        quote_type=QuoteType.BID_ASK,
        granularity=Granularity.TICK,
        is_stale=stale,
        age_seconds=Decimal("999") if stale else Decimal("0"),
    )




_DEMO_INTENT_SEQ = itertools.count(1)


def _intent(cfg: Config, side: PositionSide, stop: str, target: str, volume: str = "1000"):
    """构造演示用交易意图。

    ``intent_id`` 每次必须唯一：引擎的幂等是按 ``intent_id`` 去重的，若演示里重复
    使用同一个 id，第二次会被正确判定为重复指令而不再开仓。
    """
    from engine import TradeIntent

    seq = next(_DEMO_INTENT_SEQ)
    return TradeIntent(
        symbol=cfg.instrument.symbol, side=side, volume_usd=Decimal(volume),
        stop_loss=Decimal(stop), take_profit=Decimal(target),
        intent_id=f"demo-{seq}-{side.value}-{stop}-{target}-{volume}",
    )


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m risk_monitor",
        description="用模拟行情演示止损、止盈、保证金预警、强制平仓与断线恢复（不联网）。",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出事件")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as tmp:
        engine = TradingEngine.from_config(cfg, db_path=str(_Path(tmp) / "demo.sqlite3"))
        engine.ensure_initialized()
        monitor = RiskMonitor(cfg, engine)
        collected: list[dict[str, Any]] = []

        def tick(label: str, quote: Quote) -> MonitorResult:
            result = monitor.monitor_once(quote)
            collected.extend(event.as_dict() for event in result.events)
            print(
                f"  [{label}] 净值={result.equity} 保证金水平={result.margin_level} "
                f"回撤={result.daily_drawdown_ratio} 平仓={len(result.closed)} 事件={len(result.events)}"
            )
            for event in result.events:
                print(f"    · {event.kind}: {event.reason}")
                if event.fill_price is not None:
                    print(
                        f"      参考 Bid/Ask={event.reference_bid}/{event.reference_ask} "
                        f"成交价={event.fill_price} 盈亏={event.realized_pnl_jpy} 跳空={event.is_gap}"
                    )
            return result

        print("=== 场景 0：启动检查（确立 08:00 JST 当日基准） ===")
        first = monitor.monitor_once(_fake_quote(cfg, "158.000", "158.010"))
        assert first.mark is not None
        print(f"  当日基准净值={first.mark.day_start_equity} 交易日={first.trading_date}")

        print("\n=== 场景 1：多仓触发止损（正常触及止损价） ===")
        engine.execute_virtual_trade(
            _intent(cfg, PositionSide.LONG, "157.950", "158.100"),
            _fake_quote(cfg, "158.000", "158.010"),
        )
        tick("建立观测", _fake_quote(cfg, "158.000", "158.010"))
        tick("触发止损", _fake_quote(cfg, "157.900", "157.910"))

        print("\n=== 场景 2：空仓触发止盈（平空走 Ask） ===")
        engine.execute_virtual_trade(
            _intent(cfg, PositionSide.SHORT, "158.060", "157.910"),
            _fake_quote(cfg, "158.000", "158.010"),
        )
        tick("建立观测", _fake_quote(cfg, "158.000", "158.010"))
        tick("触发止盈", _fake_quote(cfg, "157.900", "157.910"))

        print("\n=== 场景 3：跳空越过止损（按首个可执行报价 + 不利滑点成交） ===")
        engine.execute_virtual_trade(
            _intent(cfg, PositionSide.LONG, "157.950", "158.100"),
            _fake_quote(cfg, "158.000", "158.010"),
        )
        gap_result = tick("跳空触发", _fake_quote(cfg, "157.000", "157.010"))
        assert gap_result.closed
        print(
            f"  → 若错误地按止损价成交，盈亏会是 "
            f"{(Decimal('157.950') - Decimal('158.010')) * 1000}；"
            f"按跳空规则实际为 {gap_result.closed[0].realized_pnl_jpy}"
        )

        print("\n=== 场景 4：同一事件只执行一次 ===")
        engine.execute_virtual_trade(
            _intent(cfg, PositionSide.LONG, "157.950", "158.100"),
            _fake_quote(cfg, "158.000", "158.010"),
        )
        tick("建立观测", _fake_quote(cfg, "158.000", "158.010"))
        tick("触发止损", _fake_quote(cfg, "157.900", "157.910"))
        before = len(engine.storage.query_trades(action="CLOSE"))
        repeat = tick("重复检查", _fake_quote(cfg, "157.800", "157.810"))
        after = len(engine.storage.query_trades(action="CLOSE"))
        print(f"  重复检查平仓 {len(repeat.closed)} 笔，平仓成交总数 {before} → {after}")

        print("\n=== 场景 5：监控中断后补查历史 1 分钟 K 线 ===")
        from market_data import Bar as _Bar, BarSeries as _BarSeries

        def straddling_bars(start: datetime, end: datetime) -> _BarSeries:
            bars = [
                _Bar(
                    timestamp=start + timedelta(minutes=index),
                    open=Decimal("158.000"), high=Decimal("158.020"),
                    low=Decimal("157.400"), close=Decimal("157.500"),
                    source="demo_bars", interval="1m",
                )
                for index in range(6)
            ]
            return _BarSeries(
                symbol=cfg.instrument.symbol, interval="1m", bars=tuple(bars),
                source="demo_bars", source_url="", requested_start=start, requested_end=end,
            )

        engine.execute_virtual_trade(
            _intent(cfg, PositionSide.LONG, "157.950", "158.100"),
            _fake_quote(cfg, "158.000", "158.010"),
        )
        offline = RiskMonitor(
            cfg, engine,
            quote_source=lambda: _fake_quote(cfg, "158.000", "158.010"),
            bars_source=straddling_bars,
        )
        offline._last_check_at = datetime.now(cfg.tz) - timedelta(minutes=10)
        result = offline.monitor_once()
        collected.extend(event.as_dict() for event in result.events)
        print(f"  补查={result.backfilled} 回放 {result.backfilled_bars} 根 K 线 "
              f"| 数据缺口 {len(result.data_gaps)} 处")
        for event in result.events:
            print(f"    · {event.kind}: {event.reason}")
        for gap in result.data_gaps:
            print(f"    · 数据缺口 {gap.start.isoformat(timespec='seconds')} ~ "
                  f"{gap.end.isoformat(timespec='seconds')}（缺 {gap.missing_bars} 根）")

        print("\n=== 场景 6：历史行情不可得 → 记数据缺口，不虚构成交 ===")
        from market_data import HistoryUnavailableError

        def unavailable_bars(start: datetime, end: datetime) -> _BarSeries:
            raise HistoryUnavailableError("1 分钟数据只回溯约 9 天，该窗口无法核实")

        open_pos = engine.execute_virtual_trade(
            _intent(cfg, PositionSide.LONG, "157.950", "158.100"),
            _fake_quote(cfg, "158.000", "158.010"),
        ).position
        blind = RiskMonitor(
            cfg, engine,
            quote_source=lambda: _fake_quote(cfg, "158.000", "158.010"),
            bars_source=unavailable_bars,
        )
        blind._last_check_at = datetime.now(cfg.tz) - timedelta(days=30)
        result = blind.monitor_once()
        collected.extend(event.as_dict() for event in result.events)
        print(f"  补查尝试={result.backfilled} 平仓={len(result.closed)} 笔（不得虚构）")
        for gap in result.data_gaps:
            print(f"    · 数据缺口：{gap.reason}")
        print(f"  持仓状态仍为 {engine.storage.read_position(open_pos.position_id).status}")
        engine.close_position(open_pos.position_id, _fake_quote(cfg, "158.000", "158.010"))

        print("\n=== 场景 7：行情过期 → 禁止新开仓 + 已持仓监控能力受限 ===")
        result = monitor.monitor_once(_fake_quote(cfg, "158.000", "158.010", stale=True))
        collected.extend(event.as_dict() for event in result.events)
        print(f"  降级={result.degraded} 阻断新开仓={result.blocked_new_positions}")
        print(f"  原因：{result.block_reason}")

        print("\n=== 场景 8：保证金水平 ≤150% 预警并禁止新增仓位 ===")
        print("  说明：当前配置下无法靠价格波动达到该水平（算术见文末），")
        print("        故用「入金抬高净值 → 开最大仓位 → 出金压低净值」构造：")
        print("        保证金占用不变、净值下降，且净值仍高于当日基准，不会先触发回撤停止。")
        engine.storage.record_cash_flow(
            flow_type=FlowType.DEPOSIT,
            amount=Decimal("20000"),
            event_key="demo-deposit-1",
            note="演示：入金抬高净值，使 60% 保证金上限允许更大仓位",
        )
        big = engine.execute_virtual_trade(
            _intent(cfg, PositionSide.LONG, "157.950", "158.100", volume="6000"),
            _fake_quote(cfg, "158.000", "158.010"),
        ).position
        leverage_used = (big.volume * big.entry_price / big.margin_jpy).quantize(Decimal("0.01"))
        print(f"  开仓：数量={big.volume} 保证金={big.margin_jpy} 实际杠杆={leverage_used}x")
        target_equity = (big.margin_jpy * Decimal("1.45")).quantize(Decimal("0.001"))
        withdraw = engine.account_state().equity - target_equity
        engine.storage.record_cash_flow(
            flow_type=FlowType.WITHDRAWAL,
            amount=-withdraw,
            event_key="demo-withdraw-1",
            note="演示：出金以降低净值（构造保证金预警）",
        )
        print(f"  出金 {withdraw} → 目标净值 {target_equity}（保证金水平约 1.45）")
        result = tick("预警", _fake_quote(cfg, "158.000", "158.010"))
        print(f"  阻断新开仓={result.blocked_new_positions} 原因：{result.block_reason}")

        print("\n=== 场景 9：保证金水平 ≤100% → 强制平仓全部持仓 ===")
        target_equity = (big.margin_jpy * Decimal("0.95")).quantize(Decimal("0.001"))
        engine.storage.record_cash_flow(
            flow_type=FlowType.WITHDRAWAL,
            amount=-(engine.account_state().equity - target_equity),
            event_key="demo-withdraw-2",
            note="演示：继续出金以触发强平",
        )
        result = tick("强制平仓", _fake_quote(cfg, "158.000", "158.010"))
        for item in result.closed:
            print(f"    position={item.position.position_id} reason={item.reason} "
                  f"成交价={item.trade.price} 盈亏={item.realized_pnl_jpy}")
        state = engine.account_state()
        print(f"  账户状态={state.status} 已停止开仓={state.halted} 原因={state.halt_reason}")

        print("\n=== 汇总 ===")
        report = engine.reconcile_account(strict=False)
        print(f"  账本余额={report.ledger_balance} 规则推导={report.cash_from_ledger_rule} "
              f"未平仓={report.open_position_count} 笔")
        print(f"  账户核对：{'通过' if report.ok else '不一致'}")
        for problem in report.problems:
            print(f"    [问题] {problem}")
        counts = engine.storage.counts()
        print(f"  成交 {counts['trades']} 笔 | 审计事件 {counts['audit_events']} 条 | "
              f"持仓 {counts['positions']} 笔")
        print(f"  已触发风险事件键 {len(monitor.ledger._fired)} 个：")
        for key in sorted(monitor.ledger._fired):
            print(f"    · {key}")
        print(
            "\n注意：「保证金水平 ≤150% 预警」在本演示中需要出金才能达到。\n"
            "  原因：开仓后保证金占用 ≤ 净值的 60%，所以保证金水平最低也有 1/0.6 ≈ 1.667；\n"
            "  而单笔止损风险上限 2% 把止损距离限制在约 0.2 日元内，价格远不足以让净值下跌\n"
            "  10%（达到 1.5 所需跌幅），会先触发止损或 3% 的当日回撤停止。该分支已实现，\n"
            "  可用构造状态验证，但在当前配置下无法由行情自然触发。"
        )
        if args.as_json:
            print(json.dumps(collected, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
