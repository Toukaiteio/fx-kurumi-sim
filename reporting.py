"""FX 战士久留美 —— 报告数据包与快照生成。

定位
====

**严格只读。** 本模块不修改账户、不写入资金流水、不执行交易、不调用语言模型。
它只从权威账本（:mod:`storage`）与行情层（:mod:`market_data`）汇总事实，
产出：

1. :func:`build_report_data` —— 供 Agent 分析与撰写角色报告的数据包；
2. :func:`export_snapshot` —— 完整 JSON 快照文件（带 JST 时间戳，不覆盖旧文件）；
3. :func:`generate_report_template` —— 基础事实报告模板（Markdown）；
4. :func:`verify_snapshot` —— 校验快照格式及其与权威账本的一致性。

报告种类
========

``MORNING``（08:00 JST）与 ``EVENING``（20:00 JST）。每份报告覆盖截至该时刻的
**前 12 小时**：晚间报告覆盖 ``[08:00, 20:00)``，早间报告覆盖
``[前一日 20:00, 08:00)``。这与交易日 08:00 JST 分界一致。

口径定义
========

``累计净利润 = 当前净值 + 累计出金 − 初始本金 − 累计入金``
``累计收益率 = 累计净利润 ÷ (初始本金 + 累计入金) × 100%``

**出入金不是投资盈亏**：入金会抬高净值但被同一公式减去，出金会压低净值但被加回，
所以只有真实的交易盈亏与费用才进入累计净利润。

「累计入金」**不含初始本金**（初始本金本身就是一条 ``event_key=INITIAL_CAPITAL``
的 ``DEPOSIT`` 流水，必须剔除，否则会被计两次）。

交易成本
========

拆成两类，避免把已体现在成交价里的成本重复计一次：

* ``total_charged_jpy`` —— **实际从现金扣减的**：独立手续费（FEE）+ 已核验掉期（SWAP）；
* ``embedded_spread_jpy`` —— 已体现在成交价与盈亏中的点差成本（估算值，**不**再扣一次）；
* ``total_including_embedded_jpy`` —— 两者之和，仅用于展示「总摩擦成本」量级。

不伪造数据
==========

* 拿不到行情时，``unrealized_pnl`` / ``equity`` / ``margin_level`` / ``free_margin``
  一律为 ``null``，**不会伪装成 0**；最后一次已知净值放在 ``equity_last_known`` 并标注；
* 无法计算的字段登记在 ``data_quality.unverifiable_fields``；
* 无持仓时 ``positions`` 是空数组。

金额与价格一律以**十进制字符串**落盘，不使用 JSON 浮点。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping, Sequence

from config import Config, load_config
from engine import (
    EngineError,
    TradingEngine,
    calculate_equity,
    calculate_margin_level,
    calculate_unrealized_pnl,
    calculate_used_margin,
    trading_date_for,
)
from market_data import MarketDataError, Quote, get_current_quote
from storage import (
    INITIAL_CAPITAL_EVENT_KEY,
    AccountState,
    AuditEvent,
    CashFlow,
    FlowType,
    Position,
    PositionSide,
    PositionStatus,
    ReconciliationReport,
    Storage,
    StorageError,
    TradeRecord,
)

__all__ = [
    "AccountSummary",
    "AccountEvent",
    "MarketSnapshot",
    "PositionSnapshot",
    "ReportData",
    "ReportKind",
    "ReportPeriod",
    "RiskWatchItem",
    "SNAPSHOT_VERSION",
    "SnapshotVerification",
    "TradingCostBreakdown",
    "build_report_data",
    "export_snapshot",
    "generate_report_template",
    "latest_report_slot",
    "load_snapshot",
    "report_period_for",
    "verify_snapshot",
]

logger = logging.getLogger("kurumi_fx_sim.reporting")

#: 快照格式版本。字段结构变化时递增，校验时据此判断兼容性。
SNAPSHOT_VERSION: Final[int] = 1

_MONEY_EXPONENT: Final[Decimal] = Decimal("0.001")
_RATIO_EXPONENT: Final[Decimal] = Decimal("0.0001")
_PCT_EXPONENT: Final[Decimal] = Decimal("0.01")

#: 每个报告时段的长度（12 小时）。
PERIOD_HOURS: Final[int] = 12

#: 审计日志里过于琐碎、不必进入报告的事件类型。
DEFAULT_EXCLUDED_AUDIT_TYPES: Final[frozenset[str]] = frozenset(
    {"ACCOUNT_STATE_SAVED"}
)


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


def _text(value: Decimal | None, field_name: str = "value") -> str | None:
    """Decimal → 十进制字符串（无科学计数法）；``None`` 原样返回 ``None``。

    刻意保留 ``None``：无法核实的字段在快照里必须是 ``null``，不能写成 0。
    """
    if value is None:
        return None
    return format(_dec(value, field_name), "f")


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_MONEY_EXPONENT, rounding=ROUND_HALF_UP)


def _ratio(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_RATIO_EXPONENT, rounding=ROUND_HALF_UP)


def _pct(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_PCT_EXPONENT, rounding=ROUND_HALF_UP)


def _iso(moment: datetime | None, tz: tzinfo, field_name: str = "时间") -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(tz).isoformat(timespec="seconds")


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _strip_hash(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "snapshot_sha256"}


def _payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(_strip_hash(payload)).encode("utf-8")).hexdigest()


def _safe_div(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


# --------------------------------------------------------------------------- #
# 报告种类与时段
# --------------------------------------------------------------------------- #


class ReportKind(str, Enum):
    """报告种类。"""

    MORNING = "MORNING"
    EVENING = "EVENING"

    @property
    def slot_hour(self) -> int:
        """该报告的整点时刻（JST 小时）。"""
        return 8 if self is ReportKind.MORNING else 20

    @property
    def label(self) -> str:
        return "早间报告" if self is ReportKind.MORNING else "晚间报告"

    @classmethod
    def from_value(cls, value: Any) -> ReportKind:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            key = value.strip().upper()
            for member in cls:
                if member.value == key or member.name == key:
                    return member
            if key in ("早", "早间", "morning", "am"):
                return cls.MORNING
            if key in ("晚", "晚间", "evening", "pm"):
                return cls.EVENING
        raise EngineError(f"未知报告种类: {value!r}（可用 MORNING / EVENING）")


def latest_report_slot(
    moment: datetime, tz: tzinfo
) -> tuple[ReportKind, datetime]:
    """求 ``moment`` 之前（含）最近的一个报告时点及其种类。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    local = moment.astimezone(tz)
    if local.hour >= 20:
        kind, slot = ReportKind.EVENING, local.replace(hour=20, minute=0, second=0, microsecond=0)
    elif local.hour >= 8:
        kind, slot = ReportKind.MORNING, local.replace(hour=8, minute=0, second=0, microsecond=0)
    else:
        previous = local - timedelta(days=1)
        kind = ReportKind.EVENING
        slot = previous.replace(hour=20, minute=0, second=0, microsecond=0)
    return kind, slot


def report_period_for(
    kind: ReportKind, slot: datetime
) -> tuple[datetime, datetime]:
    """报告的覆盖时段：``[slot - 12h, slot]``。"""
    return slot - timedelta(hours=PERIOD_HOURS), slot


@dataclass(frozen=True, slots=True)
class ReportPeriod:
    start: datetime
    end: datetime

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    def contains(self, moment: datetime) -> bool:
        if moment.tzinfo is None:
            return False
        return self.start <= moment <= self.end

    def as_dict(self, tz: tzinfo) -> dict[str, Any]:
        return {
            "start": _iso(self.start, tz),
            "end": _iso(self.end, tz),
            "seconds": self.seconds,
        }


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TradingCostBreakdown:
    """交易成本拆解。刻意区分「实际扣款」与「已隐含在价格里」。"""

    explicit_fees_jpy: Decimal
    swap_fees_jpy: Decimal
    embedded_spread_jpy: Decimal
    total_charged_jpy: Decimal
    total_including_embedded_jpy: Decimal
    trade_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "explicit_fees_jpy": _text(self.explicit_fees_jpy),
            "swap_fees_jpy": _text(self.swap_fees_jpy),
            "embedded_spread_jpy": _text(self.embedded_spread_jpy),
            "total_charged_jpy": _text(self.total_charged_jpy),
            "total_including_embedded_jpy": _text(self.total_including_embedded_jpy),
            "trade_count": self.trade_count,
        }


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """行情快照。行情不可用时字段为 ``None``，不伪造为 0。"""

    symbol: str
    quote_available: bool
    bid: Decimal | None
    ask: Decimal | None
    mid: Decimal | None
    timestamp: datetime | None
    source: str | None
    source_url: str | None
    quote_type: str | None
    granularity: str | None
    is_stale: bool | None
    age_seconds: Decimal | None
    spread_jpy: Decimal | None
    valid_for_decisions: bool
    issues: tuple[str, ...]

    def as_dict(self, tz: tzinfo) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quote_available": self.quote_available,
            "bid": _text(self.bid),
            "ask": _text(self.ask),
            "mid": _text(self.mid),
            "spread_jpy": _text(self.spread_jpy),
            "timestamp": _iso(self.timestamp, tz),
            "source": self.source,
            "source_url": self.source_url,
            "quote_type": self.quote_type,
            "granularity": self.granularity,
            "is_stale": self.is_stale,
            "age_seconds": _text(self.age_seconds),
            "valid_for_decisions": self.valid_for_decisions,
            "issues": list(self.issues),
        }


@dataclass(frozen=True, slots=True)
class AccountSummary:
    """账户口径汇总。``None`` 表示无法核实（例如缺行情时的净值）。"""

    currency: str
    initial_capital: Decimal
    total_deposits: Decimal
    total_withdrawals: Decimal
    deposit_count: int
    withdrawal_count: int
    cash_balance: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    equity: Decimal | None
    equity_last_known: Decimal
    net_profit: Decimal | None
    return_pct: Decimal | None
    used_margin: Decimal
    free_margin: Decimal | None
    margin_level: Decimal | None
    margin_usage_ratio: Decimal | None
    notional_jpy: Decimal
    effective_leverage: Decimal | None
    peak_equity: Decimal
    day_start_equity: Decimal
    daily_drawdown_ratio: Decimal | None
    trading_date: str | None
    status: str
    halted: bool
    halt_reason: str | None
    as_of: datetime

    def as_dict(self, tz: tzinfo) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "initial_capital": _text(self.initial_capital),
            "total_deposits": _text(self.total_deposits),
            "total_withdrawals": _text(self.total_withdrawals),
            "deposit_count": self.deposit_count,
            "withdrawal_count": self.withdrawal_count,
            "cash_balance": _text(self.cash_balance),
            "realized_pnl": _text(self.realized_pnl),
            "unrealized_pnl": _text(self.unrealized_pnl),
            "equity": _text(self.equity),
            "equity_last_known": _text(self.equity_last_known),
            "net_profit": _text(self.net_profit),
            "return_pct": _text(self.return_pct),
            "used_margin": _text(self.used_margin),
            "free_margin": _text(self.free_margin),
            "margin_level": _text(self.margin_level),
            "margin_usage_ratio": _text(self.margin_usage_ratio),
            "notional_jpy": _text(self.notional_jpy),
            "effective_leverage": _text(self.effective_leverage),
            "peak_equity": _text(self.peak_equity),
            "day_start_equity": _text(self.day_start_equity),
            "daily_drawdown_ratio": _text(self.daily_drawdown_ratio),
            "trading_date": self.trading_date,
            "status": self.status,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "as_of": _iso(self.as_of, tz),
        }


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """持仓明细，含止损/止盈状态。行情不可用时距离类字段为 ``None``。"""

    position: Position
    unrealized_pnl_jpy: Decimal | None
    unrealized_pct: Decimal | None
    executable_exit_price: Decimal | None
    stop_distance_jpy: Decimal | None
    stop_distance_pct: Decimal | None
    target_distance_jpy: Decimal | None
    target_distance_pct: Decimal | None
    stop_risk_jpy: Decimal | None
    reward_risk_ratio: Decimal | None
    stop_hit: bool | None
    target_hit: bool | None

    def as_dict(self, tz: tzinfo) -> dict[str, Any]:
        p = self.position
        return {
            "position_id": p.position_id,
            "symbol": p.symbol,
            "side": p.side,
            "status": p.status,
            "volume_usd": _text(p.volume),
            "entry_price": _text(p.entry_price),
            "entry_at": _iso(p.entry_at, tz),
            "margin_jpy": _text(p.margin_jpy),
            "planned_risk_jpy": _text(p.planned_risk_jpy),
            "stop_loss": _text(p.stop_loss),
            "take_profit": _text(p.take_profit),
            "unrealized_pnl_jpy": _text(self.unrealized_pnl_jpy),
            "unrealized_pct": _text(self.unrealized_pct),
            "executable_exit_price": _text(self.executable_exit_price),
            "stop_distance_jpy": _text(self.stop_distance_jpy),
            "stop_distance_pct": _text(self.stop_distance_pct),
            "target_distance_jpy": _text(self.target_distance_jpy),
            "target_distance_pct": _text(self.target_distance_pct),
            "stop_risk_jpy": _text(self.stop_risk_jpy),
            "reward_risk_ratio": _text(self.reward_risk_ratio),
            "stop_hit": self.stop_hit,
            "target_hit": self.target_hit,
            "decision_ref": p.decision_ref,
        }


@dataclass(frozen=True, slots=True)
class AccountEvent:
    """自上次报告以来的账户事件。"""

    occurred_at: datetime
    kind: str
    summary: str
    source: str
    ref: str | None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, tz: tzinfo) -> dict[str, Any]:
        return {
            "occurred_at": _iso(self.occurred_at, tz),
            "kind": self.kind,
            "summary": self.summary,
            "source": self.source,
            "ref": self.ref,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RiskWatchItem:
    """下一周期需要关注的风险触发条件。"""

    code: str
    description: str
    severity: str
    threshold: Decimal | None
    current: Decimal | None
    distance: Decimal | None
    unit: str
    triggered: bool
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "description": self.description,
            "severity": self.severity,
            "threshold": _text(self.threshold),
            "current": _text(self.current),
            "distance": _text(self.distance),
            "unit": self.unit,
            "triggered": self.triggered,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ReportData:
    """完整报告数据包。"""

    report_id: str
    kind: ReportKind
    report_at: datetime
    slot: datetime
    period: ReportPeriod
    trading_date: str
    symbol: str
    timezone_name: str
    market: MarketSnapshot
    account: AccountSummary
    costs: TradingCostBreakdown
    positions: tuple[PositionSnapshot, ...]
    trades_in_period: tuple[TradeRecord, ...]
    flows_in_period: tuple[CashFlow, ...]
    events: tuple[AccountEvent, ...]
    risk_watch: tuple[RiskWatchItem, ...]
    data_quality: dict[str, Any]
    integrity: dict[str, Any]

    # -- 导出 -------------------------------------------------------------- #

    def to_payload(self) -> dict[str, Any]:
        """转成 JSON 可序列化字典（金额/价格为十进制字符串）。"""
        tz = timezone(timedelta(hours=9), "Asia/Tokyo")
        payload: dict[str, Any] = {
            "snapshot_version": SNAPSHOT_VERSION,
            "report_id": self.report_id,
            "report_kind": self.kind.value,
            "report_kind_label": self.kind.label,
            "report_slot_hour_jst": self.kind.slot_hour,
            "report_at": _iso(self.report_at, tz),
            "slot": _iso(self.slot, tz),
            "period": self.period.as_dict(tz),
            "trading_date": self.trading_date,
            "symbol": self.symbol,
            "timezone": self.timezone_name,
            "market": self.market.as_dict(tz),
            "account": self.account.as_dict(tz),
            "cumulative_trading_cost": self.costs.as_dict(),
            "positions": [item.as_dict(tz) for item in self.positions],
            "trades_in_period": [_trade_dict(t, tz) for t in self.trades_in_period],
            "cash_flows_in_period": [_flow_dict(f, tz) for f in self.flows_in_period],
            "events": [event.as_dict(tz) for event in self.events],
            "risk_watch": [item.as_dict() for item in self.risk_watch],
            "data_quality": self.data_quality,
            "integrity": self.integrity,
        }
        payload["snapshot_sha256"] = _payload_hash(payload)
        return payload

    def as_dict(self) -> dict[str, Any]:
        return self.to_payload()


def _trade_dict(trade: TradeRecord, tz: tzinfo) -> dict[str, Any]:
    payload = trade.as_dict()
    payload["is_cost_bearing"] = trade.fee_jpy is not None
    payload["spread_jpy"] = _text(trade.spread_jpy)
    return payload


def _flow_dict(flow: CashFlow, tz: tzinfo) -> dict[str, Any]:
    return flow.as_dict()


@dataclass(frozen=True, slots=True)
class SnapshotVerification:
    """快照校验结果。"""

    ok: bool
    path: str | None
    snapshot_version: int | None
    report_id: str | None
    report_kind: str | None
    report_at: datetime | None
    format_ok: bool
    self_hash_ok: bool
    formulas_ok: bool
    ledger_ok: bool
    problems: tuple[str, ...]
    checked: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "snapshot_version": self.snapshot_version,
            "report_id": self.report_id,
            "report_kind": self.report_kind,
            "report_at": _iso(self.report_at, timezone(timedelta(hours=9), "Asia/Tokyo")),
            "format_ok": self.format_ok,
            "self_hash_ok": self.self_hash_ok,
            "formulas_ok": self.formulas_ok,
            "ledger_ok": self.ledger_ok,
            "problems": list(self.problems),
            "checked": list(self.checked),
        }


# --------------------------------------------------------------------------- #
# 汇总逻辑
# --------------------------------------------------------------------------- #


def _flow_totals(flows: Iterable[CashFlow]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {member.value: Decimal("0") for member in FlowType}
    for flow in flows:
        totals[flow.flow_type] = totals.get(flow.flow_type, Decimal("0")) + flow.amount
    return totals


def _external_funding(flows: Sequence[CashFlow]) -> tuple[Decimal, Decimal, int, int]:
    """累计入金 / 累计出金（入金不含初始本金）。"""
    deposits = Decimal("0")
    withdrawals = Decimal("0")
    deposit_count = 0
    withdrawal_count = 0
    for flow in flows:
        if flow.flow_type == FlowType.DEPOSIT.value:
            if flow.event_key == INITIAL_CAPITAL_EVENT_KEY:
                continue  # 初始本金单独列示，不计入「累计入金」
            deposits += flow.amount
            deposit_count += 1
        elif flow.flow_type == FlowType.WITHDRAWAL.value:
            withdrawals += abs(flow.amount)
            withdrawal_count += 1
    return deposits, withdrawals, deposit_count, withdrawal_count


def _embedded_spread(trades: Iterable[TradeRecord]) -> Decimal:
    """已隐含在成交价里的点差成本估算（不再从余额扣一次）。

    注意口径：成交价是**单边**价格（开多走 Ask、平多走 Bid），相对中间价每笔只付
    **半个**点差。因此每笔的成本是 ``|点差| / 2 × 数量``；一开一平正好付掉一个完整
    点差。若按「整点差 × 数量」累加会把成本高估一倍。
    """
    total = Decimal("0")
    for trade in trades:
        if trade.spread_jpy is None:
            continue
        total += abs(_dec(trade.spread_jpy)) / 2 * _dec(trade.volume)
    return total


def _position_snapshot(
    position: Position, quote: Quote | None, *, slippage_jpy: Decimal
) -> PositionSnapshot:
    """构造持仓明细。缺行情时所有依赖现价的字段为 ``None``。"""
    side = PositionSide(position.side)
    volume = _dec(position.volume)
    entry = _dec(position.entry_price)
    if quote is None:
        return PositionSnapshot(
            position=position,
            unrealized_pnl_jpy=None,
            unrealized_pct=None,
            executable_exit_price=None,
            stop_distance_jpy=None,
            stop_distance_pct=None,
            target_distance_jpy=None,
            target_distance_pct=None,
            stop_risk_jpy=None,
            reward_risk_ratio=None,
            stop_hit=None,
            target_hit=None,
        )

    unrealized = calculate_unrealized_pnl(position, quote)
    executable = _dec(quote.bid if side is PositionSide.LONG else quote.ask)
    notional = entry * volume
    unrealized_pct = _pct(_safe_div(unrealized, notional) * 100) if notional else None

    stop_distance = stop_pct = stop_risk = None
    if position.stop_loss is not None:
        stop = _dec(position.stop_loss)
        stop_distance = _money(abs(executable - stop))
        stop_pct = _pct(_safe_div(stop_distance, executable) * 100)
        # 若此刻触发止损，预计亏损 =（距止损距离 + 预期不利滑点）× 数量
        stop_risk = _money((abs(executable - stop) + slippage_jpy) * volume)
    target_distance = target_pct = None
    if position.take_profit is not None:
        target = _dec(position.take_profit)
        target_distance = _money(abs(target - executable))
        target_pct = _pct(_safe_div(target_distance, executable) * 100)

    reward_risk = None
    if position.stop_loss is not None and position.take_profit is not None:
        stop_span = abs(entry - _dec(position.stop_loss))
        target_span = abs(_dec(position.take_profit) - entry)
        reward_risk = _ratio(_safe_div(target_span, stop_span))

    stop_hit = target_hit = None
    if position.stop_loss is not None:
        stop = _dec(position.stop_loss)
        stop_hit = executable <= stop if side is PositionSide.LONG else executable >= stop
    if position.take_profit is not None:
        target = _dec(position.take_profit)
        target_hit = executable >= target if side is PositionSide.LONG else executable <= target

    return PositionSnapshot(
        position=position,
        unrealized_pnl_jpy=unrealized,
        unrealized_pct=unrealized_pct,
        executable_exit_price=executable,
        stop_distance_jpy=stop_distance,
        stop_distance_pct=stop_pct,
        target_distance_jpy=target_distance,
        target_distance_pct=target_pct,
        stop_risk_jpy=stop_risk,
        reward_risk_ratio=reward_risk,
        stop_hit=stop_hit,
        target_hit=target_hit,
    )


def _risk_watch(
    *,
    cfg: Config,
    account: AccountSummary,
    positions: Sequence[PositionSnapshot],
    market: MarketSnapshot,
    integrity_ok: bool,
) -> tuple[RiskWatchItem, ...]:
    """列出下一周期需要关注的风险触发条件（含当前值与到阈值的距离）。"""
    items: list[RiskWatchItem] = []

    def ratio_item(
        code: str, description: str, threshold: Decimal, current: Decimal | None,
        severity: str, *, below: bool = True, note: str | None = None,
    ) -> None:
        if current is None:
            items.append(
                RiskWatchItem(
                    code=code, description=description, severity="unknown",
                    threshold=threshold, current=None, distance=None, unit="ratio",
                    triggered=False, note="缺行情，无法核实",
                )
            )
            return
        distance = (current - threshold) if below else (threshold - current)
        triggered = current <= threshold if below else current >= threshold
        items.append(
            RiskWatchItem(
                code=code, description=description, severity=severity,
                threshold=threshold, current=_ratio(current), distance=_ratio(distance),
                unit="ratio", triggered=triggered, note=note,
            )
        )

    ratio_item(
        "MARGIN_LEVEL_MARGIN_CALL", "保证金水平降至预警线（禁止新增仓位）",
        _dec(cfg.margin.margin_call_level), account.margin_level, "warning",
    )
    ratio_item(
        "MARGIN_LEVEL_STOP_OUT", "保证金水平降至强平线（强制平仓）",
        _dec(cfg.margin.stop_out_level), account.margin_level, "critical",
    )
    ratio_item(
        "DAILY_DRAWDOWN", "当日净值回撤达到停止阈值（平仓并暂停当日开仓）",
        _dec(cfg.risk.daily_drawdown_stop_ratio), account.daily_drawdown_ratio, "critical",
        below=False,
    )
    ratio_item(
        "MARGIN_USAGE", "已用保证金占净值比例达到上限",
        _dec(cfg.risk.max_margin_usage_ratio), account.margin_usage_ratio, "warning",
        below=False,
    )
    ratio_item(
        "EFFECTIVE_LEVERAGE", "名义杠杆达到上限",
        _dec(cfg.risk.max_leverage), account.effective_leverage, "warning",
        below=False,
    )

    # 净值类阈值（绝对值）
    equity = account.equity
    for code, description, threshold, severity, below in (
        ("MIN_EQUITY_TO_OPEN", "净值降至停止开仓阈值", _dec(cfg.margin.min_equity_to_open_jpy), "warning", True),
        ("BANKRUPTCY_EQUITY", "净值降至爆仓阈值（永久停止）", _dec(cfg.margin.bankruptcy_equity_jpy), "critical", True),
    ):
        if equity is None:
            items.append(
                RiskWatchItem(
                    code=code, description=description, severity="unknown",
                    threshold=threshold, current=None, distance=None, unit="JPY",
                    triggered=False, note="缺行情，无法核实",
                )
            )
            continue
        items.append(
            RiskWatchItem(
                code=code, description=description, severity=severity,
                threshold=threshold, current=_money(equity),
                distance=_money(equity - threshold if below else threshold - equity),
                unit="JPY",
                triggered=equity <= threshold if below else equity >= threshold,
            )
        )

    # 每个持仓的止损/止盈距离
    for item in positions:
        position = item.position
        if item.stop_distance_jpy is not None:
            items.append(
                RiskWatchItem(
                    code=f"POSITION_{position.position_id}_STOP",
                    description=f"持仓 #{position.position_id} 距止损 {position.stop_loss}",
                    severity="info", threshold=_dec(position.stop_loss),
                    current=item.executable_exit_price,
                    distance=item.stop_distance_jpy, unit="JPY",
                    triggered=bool(item.stop_hit),
                )
            )
        if item.target_distance_jpy is not None:
            items.append(
                RiskWatchItem(
                    code=f"POSITION_{position.position_id}_TARGET",
                    description=f"持仓 #{position.position_id} 距止盈 {position.take_profit}",
                    severity="info", threshold=_dec(position.take_profit),
                    current=item.executable_exit_price,
                    distance=item.target_distance_jpy, unit="JPY",
                    triggered=bool(item.target_hit),
                )
            )

    # 行情数据有效性
    items.append(
        RiskWatchItem(
            code="QUOTE_VALIDITY", description="行情是否可用于开仓决策",
            severity="critical" if not market.valid_for_decisions else "info",
            threshold=None, current=_dec(market.age_seconds) if market.age_seconds is not None else None,
            distance=None, unit="seconds",
            triggered=not market.valid_for_decisions,
            note="；".join(market.issues) if market.issues else None,
        )
    )

    # 数据完整性
    items.append(
        RiskWatchItem(
            code="LEDGER_INTEGRITY", description="账本与审计链是否自洽",
            severity="critical" if not integrity_ok else "info",
            threshold=None, current=None, distance=None, unit="bool",
            triggered=not integrity_ok,
            note=None if integrity_ok else "账本/审计链校验未通过，报告中数字可能不可信",
        )
    )
    return tuple(items)


# --------------------------------------------------------------------------- #
# 主入口：构造数据包
# --------------------------------------------------------------------------- #


def build_report_data(
    kind: ReportKind | str | None = None,
    *,
    cfg: Config | None = None,
    engine: TradingEngine | None = None,
    storage: Storage | None = None,
    quote: Quote | None = None,
    quote_source: Callable[[], Quote] | None = None,
    skip_quote: bool = False,
    report_at: datetime | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    exclude_audit_types: Iterable[str] = DEFAULT_EXCLUDED_AUDIT_TYPES,
) -> ReportData:
    """构造报告数据包。**只读**：不修改账户、不写流水、不下单、不调用模型。

    Args:
        kind: ``MORNING`` / ``EVENING``；``None`` 时按 ``report_at`` 推断最近时点。
        quote: 直接注入行情（测试/离线用）。
        quote_source: 行情获取函数；``None`` 时用 :func:`market_data.get_current_quote`。
        skip_quote: 为 ``True`` 时**完全不取行情**，相关字段如实记为 ``null``。
            注意：``quote=None`` 的语义是「去获取行情」，不是「不要行情」，
            要表达后者必须用本参数。
        report_at: 报告时刻；``None`` 时取当前东京时间。
        period_start/period_end: 覆盖时段；默认 ``[slot-12h, slot]``。

    拿不到行情时，``unrealized_pnl`` / ``equity`` / ``margin_level`` 等字段为
    ``None``（快照里是 ``null``），**不会伪装成 0**。
    """
    cfg = cfg or load_config()
    tz = cfg.tz
    if engine is None:
        engine = TradingEngine(cfg, storage) if storage is not None else TradingEngine(cfg)
    storage = storage or engine.storage

    now = report_at or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    inferred_kind, slot = latest_report_slot(now, tz)
    resolved_kind = ReportKind.from_value(kind) if kind is not None else inferred_kind
    default_start, default_end = report_period_for(resolved_kind, slot)
    period = ReportPeriod(
        start=period_start or default_start,
        end=period_end or default_end,
    )

    # --- 行情（只读；失败不抛出，如实记录） ------------------------------ #
    market_issues: list[str] = []
    if skip_quote:
        quote = None
        market_issues.append("调用方指定不使用行情（skip_quote=True）")
    elif quote is None:
        source_fn = quote_source or (lambda: get_current_quote(cfg))
        try:
            quote = source_fn()
        except MarketDataError as exc:
            market_issues.append(f"行情获取失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 行情异常不应让报告生成失败
            market_issues.append(f"行情获取异常：{type(exc).__name__}: {exc}")

    valid_for_decisions = False
    if quote is not None:
        try:
            engine.check_quote(quote)
            valid_for_decisions = True
        except EngineError as exc:
            market_issues.append(f"报价不可用于决策：{exc}")
        if getattr(quote, "is_stale", False):
            market_issues.append(
                f"报价已过期（age={getattr(quote, 'age_seconds', '?')} 秒）"
            )
    elif not skip_quote:
        market_issues.append("无可用行情")

    market = MarketSnapshot(
        symbol=cfg.instrument.symbol,
        quote_available=quote is not None,
        bid=_dec(quote.bid) if quote is not None else None,
        ask=_dec(quote.ask) if quote is not None else None,
        mid=_dec(quote.mid) if quote is not None else None,
        timestamp=getattr(quote, "timestamp", None) if quote is not None else None,
        source=getattr(quote, "source", None) if quote is not None else None,
        source_url=getattr(quote, "source_url", None) if quote is not None else None,
        quote_type=getattr(getattr(quote, "quote_type", None), "value", None),
        granularity=getattr(getattr(quote, "granularity", None), "value", None),
        is_stale=bool(getattr(quote, "is_stale", False)) if quote is not None else None,
        age_seconds=_dec(getattr(quote, "age_seconds")) if quote is not None and getattr(quote, "age_seconds", None) is not None else None,
        spread_jpy=(
            _dec(quote.ask) - _dec(quote.bid) if quote is not None else None
        ),
        valid_for_decisions=valid_for_decisions,
        issues=tuple(market_issues),
    )

    # --- 账本汇总 --------------------------------------------------------- #
    state: AccountState = storage.read_account_state()
    all_flows = storage.read_cash_flows()
    all_trades = storage.query_trades()
    totals = _flow_totals(all_flows)
    deposits, withdrawals, deposit_count, withdrawal_count = _external_funding(all_flows)
    initial = _dec(cfg.account.initial_capital_jpy)
    cash = storage.ledger_balance()
    realized = totals.get(FlowType.REALIZED_PNL.value, Decimal("0"))
    explicit_fees = abs(totals.get(FlowType.FEE.value, Decimal("0")))
    swap_fees = abs(totals.get(FlowType.SWAP.value, Decimal("0")))
    embedded = _embedded_spread(all_trades)

    positions = storage.read_positions(status=PositionStatus.OPEN)
    used_margin = calculate_used_margin(positions)
    slippage = _dec(cfg.market.slippage_jpy)
    position_snapshots = tuple(
        _position_snapshot(p, quote, slippage_jpy=slippage) for p in positions
    )

    unrealized: Decimal | None = None
    equity: Decimal | None = None
    if quote is not None:
        unrealized = sum(
            (calculate_unrealized_pnl(p, quote) for p in positions), Decimal("0")
        )
        unrealized = _money(unrealized)
        equity = calculate_equity(cash, unrealized)

    net_profit = None if equity is None else _money(
        equity + withdrawals - initial - deposits
    )
    contribution = initial + deposits
    return_pct = (
        None
        if net_profit is None or contribution == 0
        else _pct(net_profit / contribution * 100)
    )
    margin_level = calculate_margin_level(equity, used_margin) if equity is not None else None
    free_margin = None if equity is None else _money(equity - used_margin)
    margin_usage_ratio = (
        _ratio(used_margin / equity) if equity is not None and equity > 0 else None
    )
    notional = sum((_dec(p.volume) * _dec(p.entry_price) for p in positions), Decimal("0"))
    effective_leverage = (
        _ratio(notional / equity) if equity is not None and equity > 0 else None
    )
    trading_date = trading_date_for(now, tz).isoformat()
    baseline = storage.read_daily_baseline(trading_date)
    day_start = baseline.day_start_equity if baseline is not None else state.day_start_equity
    drawdown = (
        None if equity is None or day_start <= 0 else (day_start - equity) / day_start
    )

    account = AccountSummary(
        currency=cfg.account.currency,
        initial_capital=initial,
        total_deposits=_money(deposits),
        total_withdrawals=_money(withdrawals),
        deposit_count=deposit_count,
        withdrawal_count=withdrawal_count,
        cash_balance=_money(cash),
        realized_pnl=_money(realized),
        unrealized_pnl=unrealized,
        equity=equity,
        equity_last_known=_money(state.equity),
        net_profit=net_profit,
        return_pct=return_pct,
        used_margin=_money(used_margin),
        free_margin=free_margin,
        margin_level=_ratio(margin_level),
        margin_usage_ratio=margin_usage_ratio,
        notional_jpy=_money(notional),
        effective_leverage=effective_leverage,
        peak_equity=_money(state.peak_equity),
        day_start_equity=_money(day_start),
        daily_drawdown_ratio=_ratio(drawdown),
        trading_date=trading_date,
        status=state.status,
        halted=state.halted,
        halt_reason=state.halt_reason,
        as_of=state.as_of,
    )

    costs = TradingCostBreakdown(
        explicit_fees_jpy=_money(explicit_fees),
        swap_fees_jpy=_money(swap_fees),
        embedded_spread_jpy=_money(embedded),
        total_charged_jpy=_money(explicit_fees + swap_fees),
        total_including_embedded_jpy=_money(explicit_fees + swap_fees + embedded),
        trade_count=len(all_trades),
    )

    # --- 本期成交与流水 --------------------------------------------------- #
    trades_in_period = tuple(
        t for t in all_trades if period.contains(t.occurred_at)
    )
    flows_in_period = tuple(f for f in all_flows if period.contains(f.occurred_at))

    # --- 自上次报告以来的账户事件 ----------------------------------------- #
    excluded = set(exclude_audit_types)
    events: list[AccountEvent] = []
    for audit_event in storage.read_audit_events():
        if audit_event.event_type in excluded:
            continue
        if not period.contains(audit_event.occurred_at):
            continue
        events.append(_audit_to_event(audit_event))
    events.sort(key=lambda item: item.occurred_at)

    # --- 风险关注 --------------------------------------------------------- #
    integrity = {
        "ledger_balance": _text(cash),
        "realized_pnl_from_flows": _text(_money(realized)),
        "cash_balance_stored": _text(state.cash_balance),
        "audit_head": storage.audit_head(),
        "flow_count": len(all_flows),
        "trade_count": len(all_trades),
        "open_position_count": len(positions),
        "position_count_total": storage.counts().get("positions", 0),
        "daily_drawdown_stop_triggered": bool(
            baseline.drawdown_stop_triggered if baseline is not None else False
        ),
    }
    reconciliation: ReconciliationReport = storage.reconcile_account(
        include_balance_chain=True
    )
    audit_verification = storage.verify_audit_chain()
    ledger_verification = storage.verify_ledger()

    unverifiable: list[str] = []
    if quote is None:
        unverifiable.extend(
            [
                "account.unrealized_pnl",
                "account.equity",
                "account.net_profit",
                "account.return_pct",
                "account.free_margin",
                "account.margin_level",
                "account.margin_usage_ratio",
                "account.effective_leverage",
                "account.daily_drawdown_ratio",
            ]
        )
        for item in position_snapshots:
            for field_name in (
                "unrealized_pnl_jpy", "executable_exit_price", "stop_distance_jpy",
                "target_distance_jpy", "stop_risk_jpy", "stop_hit", "target_hit",
            ):
                unverifiable.append(f"positions[{item.position.position_id}].{field_name}")

    data_quality = {
        "quote_available": market.quote_available,
        "quote_is_stale": market.is_stale,
        "quote_valid_for_decisions": market.valid_for_decisions,
        "quote_age_seconds": _text(market.age_seconds),
        "quote_issues": list(market.issues),
        "ledger_verified": ledger_verification.ok,
        "audit_chain_ok": audit_verification.ok,
        "reconciliation_ok": reconciliation.ok,
        "reconciliation_problems": list(reconciliation.problems),
        "audit_events_checked": audit_verification.checked,
        "unverifiable_fields": unverifiable,
        "notes": [
            "金额与价格均为十进制字符串，未使用浮点。",
            "出入金不计入投资收益；累计净利润已按公式剔除。",
            "点差已体现在成交价与盈亏中，未在累计交易成本里重复扣减。",
        ],
    }

    report_id = f"{resolved_kind.value.lower()}-{now.astimezone(tz).strftime('%Y%m%dT%H%M%S%z')}"
    return ReportData(
        report_id=report_id,
        kind=resolved_kind,
        report_at=now,
        slot=slot,
        period=period,
        trading_date=trading_date,
        symbol=cfg.instrument.symbol,
        timezone_name=str(tz),
        market=market,
        account=account,
        costs=costs,
        positions=position_snapshots,
        trades_in_period=trades_in_period,
        flows_in_period=flows_in_period,
        events=tuple(events),
        risk_watch=_risk_watch(
            cfg=cfg,
            account=account,
            positions=position_snapshots,
            market=market,
            integrity_ok=(
                ledger_verification.ok
                and audit_verification.ok
                and reconciliation.ok
            ),
        ),
        data_quality=data_quality,
        integrity=integrity,
    )


def _audit_to_event(audit_event: AuditEvent) -> AccountEvent:
    payload = audit_event.payload if isinstance(audit_event.payload, dict) else {}
    summary = _summarise_audit(audit_event.event_type, payload)
    return AccountEvent(
        occurred_at=audit_event.occurred_at,
        kind=audit_event.event_type,
        summary=summary,
        source="audit",
        ref=f"seq={audit_event.seq}",
        detail=payload,
    )


def _summarise_audit(event_type: str, payload: Mapping[str, Any]) -> str:
    if event_type == "POSITION_OPENED":
        return (
            f"开仓 #{payload.get('position_id')} {payload.get('side')} "
            f"{payload.get('volume')} @ {payload.get('entry_price')}"
        )
    if event_type == "POSITION_CLOSED":
        return (
            f"平仓 #{payload.get('position_id')} @ {payload.get('exit_price')}，"
            f"盈亏 {payload.get('realized_pnl_jpy')}，原因 {payload.get('reason')}"
        )
    if event_type == "POSITION_CLOSE_EXECUTED":
        return (
            f"执行平仓 #{payload.get('position_id')}，原因 {payload.get('reason')}，"
            f"成交价 {payload.get('exit_price')}，盈亏 {payload.get('realized_pnl_jpy')}"
        )
    if event_type == "TRADE_EXECUTED":
        return (
            f"执行交易 #{payload.get('position_id')}，成交价 {payload.get('entry_price')}，"
            f"保证金 {payload.get('margin_jpy')}"
        )
    if event_type == "TRADE_REJECTED":
        return f"交易被拒绝：{payload.get('problems')}"
    if event_type == "CASH_FLOW_RECORDED":
        return (
            f"资金流水 {payload.get('flow_type')} {payload.get('amount')}，"
            f"余额变为 {payload.get('balance_after')}"
        )
    if event_type == "RISK_EVENT":
        return f"风险事件 {payload.get('kind')}：{payload.get('reason')}"
    if event_type == "STANDALONE_FEE_RECORDED":
        return f"独立手续费 {payload.get('fee_id')} 扣减 {payload.get('amount')}"
    if event_type == "SWAP_FEE_RECORDED":
        return f"已核验掉期 {payload.get('swap_id')} 扣减 {payload.get('amount')}"
    if event_type == "DAILY_BASELINE_CREATED":
        return (
            f"当日基准建立：净值 {payload.get('day_start_equity')}，"
            f"余额 {payload.get('day_start_balance')}"
        )
    if event_type == "PROJECTION_REPAIRED":
        return f"投影修复：{payload.get('reason')}"
    if event_type == "ACCOUNT_INITIALIZED":
        return f"账户初始化，初始本金 {payload.get('initial_capital')}"
    return event_type


# --------------------------------------------------------------------------- #
# 导出与读取
# --------------------------------------------------------------------------- #


def _unique_path(path: Path) -> Path:
    """避免覆盖已有报告：同名时追加 ``-1``、``-2``…"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    index = 1
    while True:
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def export_snapshot(
    data: ReportData | None = None,
    *,
    out_dir: str | Path | None = None,
    cfg: Config | None = None,
    filename: str | None = None,
    **build_kwargs: Any,
) -> Path:
    """把完整 JSON 快照写入文件，返回实际路径。

    * 文件名带**明确的日本时间时间戳**（形如 ``morning-20261011T080000+0900``）；
    * 同名文件不会被覆盖，自动追加序号；
    * 快照内含 ``snapshot_sha256``，便于事后校验是否被改动。
    """
    cfg = cfg or load_config()
    if data is None:
        data = build_report_data(cfg=cfg, **build_kwargs)
    directory = Path(out_dir) if out_dir is not None else cfg.paths.reports_dir
    directory.mkdir(parents=True, exist_ok=True)

    tz = cfg.tz
    if filename is None:
        stamp = data.report_at.astimezone(tz).strftime("%Y%m%dT%H%M%S%z")
        filename = f"fxsim-{data.kind.value.lower()}-{stamp}.json"
    target = _unique_path(directory / filename)
    payload = data.to_payload()
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    logger.info("已导出快照：%s（%d 字节）", target, target.stat().st_size)
    return target


def load_snapshot(path: str | Path) -> dict[str, Any]:
    """读取快照 JSON。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise EngineError(f"快照顶层必须是 JSON 对象: {path}")
    return payload


# --------------------------------------------------------------------------- #
# 报告模板
# --------------------------------------------------------------------------- #


def _fmt(value: Any, unit: str = "") -> str:
    """模板里的数值展示：``None`` 显示为「无法核实」，绝不显示为 0。"""
    if value is None:
        return "无法核实"
    return f"{value}{unit}"


def generate_report_template(data: ReportData | None = None, **build_kwargs: Any) -> str:
    """生成基础事实报告模板（Markdown）。

    只包含可由账本核实的事实；市场分析与角色日记部分留空给 Agent 撰写。
    无法核实的字段写「无法核实」，不写 0。
    """
    if data is None:
        data = build_report_data(**build_kwargs)
    cfg = load_config()
    tz = cfg.tz
    a = data.account
    m = data.market
    c = data.costs

    lines: list[str] = []
    lines.append(f"# FX 战士久留美 · {data.kind.label}（{data.report_at.astimezone(tz).strftime('%Y-%m-%d %H:%M')} JST）")
    lines.append("")
    lines.append("> 本文件由程序按权威账本自动生成，只含可核实事实。")
    lines.append("> 市场分析与角色日记请由 Agent 依据数据撰写，不要改动事实部分。")
    lines.append("")
    lines.append("## 1. 报告与行情")
    lines.append("")
    lines.append(f"- 报告编号：`{data.report_id}`")
    lines.append(f"- 报告时刻：{data.report_at.astimezone(tz).isoformat(timespec='seconds')}")
    lines.append(f"- 覆盖时段：{data.period.start.astimezone(tz).isoformat(timespec='seconds')} ~ {data.period.end.astimezone(tz).isoformat(timespec='seconds')}")
    lines.append(f"- 交易日（08:00 JST 分界）：{data.trading_date}")
    lines.append(f"- 行情时间戳：{_fmt(m.timestamp.astimezone(tz).isoformat(timespec='seconds') if m.timestamp else None)}")
    lines.append(f"- 行情来源：{_fmt(m.source)}　报价类型：{_fmt(m.quote_type)}　粒度：{_fmt(m.granularity)}")
    lines.append(f"- Bid / Ask / Mid：{_fmt(_text(m.bid))} / {_fmt(_text(m.ask))} / {_fmt(_text(m.mid))}")
    lines.append(f"- 点差：{_fmt(_text(m.spread_jpy))} 日元　行情年龄：{_fmt(_text(m.age_seconds))} 秒　是否过期：{_fmt(m.is_stale)}")
    lines.append(f"- 数据有效性：{'可用于决策' if m.valid_for_decisions else '**不可用于决策**'}")
    for issue in m.issues:
        lines.append(f"  - ⚠ {issue}")
    lines.append("")
    lines.append("## 2. 资金口径")
    lines.append("")
    lines.append(f"- 初始本金：{_text(a.initial_capital)} {a.currency}")
    lines.append(f"- 累计入金：{_text(a.total_deposits)}（{a.deposit_count} 笔，不含初始本金）")
    lines.append(f"- 累计出金：{_text(a.total_withdrawals)}（{a.withdrawal_count} 笔）")
    lines.append(f"- 现金余额：{_text(a.cash_balance)}")
    lines.append(f"- 已实现盈亏：{_text(a.realized_pnl)}")
    lines.append(f"- 浮动盈亏：{_fmt(_text(a.unrealized_pnl))}")
    lines.append(f"- 累计交易成本：实际扣款 {_text(c.total_charged_jpy)}"
                 f"（手续费 {_text(c.explicit_fees_jpy)} + 掉期 {_text(c.swap_fees_jpy)}）"
                 f"，另含已体现在成交价中的点差约 {_text(c.embedded_spread_jpy)}"
                 f"，合计摩擦成本约 {_text(c.total_including_embedded_jpy)}")
    lines.append("")
    lines.append("## 3. 账户表现")
    lines.append("")
    lines.append(f"- 当前净值：{_fmt(_text(a.equity))}"
                 + (f"（最后一次已知：{_text(a.equity_last_known)}）" if a.equity is None else ""))
    lines.append(f"- 累计净利润：{_fmt(_text(a.net_profit))}　= 净值 + 累计出金 − 初始本金 − 累计入金")
    lines.append(f"- 累计收益率：{_fmt(_text(a.return_pct), '%')}")
    lines.append(f"- 已用保证金：{_text(a.used_margin)}　可用保证金：{_fmt(_text(a.free_margin))}")
    lines.append(f"- 保证金水平：{_fmt(_text(a.margin_level))}　保证金占用比：{_fmt(_text(a.margin_usage_ratio))}")
    lines.append(f"- 名义金额：{_text(a.notional_jpy)}　名义杠杆：{_fmt(_text(a.effective_leverage))}")
    lines.append(f"- 历史峰值净值：{_text(a.peak_equity)}　当日起始净值：{_text(a.day_start_equity)}")
    lines.append(f"- 当日回撤：{_fmt(_text(a.daily_drawdown_ratio))}")
    lines.append(f"- 账户状态：{a.status}　停止开仓：{'是' if a.halted else '否'}"
                 + (f"（{a.halt_reason}）" if a.halt_reason else ""))
    lines.append("")
    lines.append("## 4. 当前持仓")
    lines.append("")
    if not data.positions:
        lines.append("当前无持仓。")
    else:
        lines.append("| # | 方向 | 数量(USD) | 开仓价 | 止损 | 止盈 | 浮动盈亏 | 距止损 | 距止盈 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for item in data.positions:
            p = item.position
            lines.append(
                f"| {p.position_id} | {p.side} | {_text(p.volume)} | {_text(p.entry_price)} | "
                f"{_fmt(_text(p.stop_loss))} | {_fmt(_text(p.take_profit))} | "
                f"{_fmt(_text(item.unrealized_pnl_jpy))} | "
                f"{_fmt(_text(item.stop_distance_jpy))} | {_fmt(_text(item.target_distance_jpy))} |"
            )
        for item in data.positions:
            p = item.position
            lines.append("")
            lines.append(f"- 持仓 #{p.position_id}：保证金 {_text(p.margin_jpy)}，"
                         f"计划止损风险 {_fmt(_text(p.planned_risk_jpy))}，"
                         f"计划盈亏比 {_fmt(_text(item.reward_risk_ratio))}，"
                         f"止损触及 {_fmt(item.stop_hit)}，止盈触及 {_fmt(item.target_hit)}")
    lines.append("")
    lines.append("## 5. 本时段成交")
    lines.append("")
    if not data.trades_in_period:
        lines.append("本时段无成交。")
    else:
        lines.append("| 成交号 | 持仓 | 动作 | 方向 | 数量 | 成交价 | 已实现盈亏 | 原因 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for t in data.trades_in_period:
            lines.append(
                f"| {t.trade_no} | {_fmt(t.position_id)} | {t.action} | {t.side} | "
                f"{_text(t.volume)} | {_text(t.price)} | {_fmt(_text(t.realized_pnl_jpy))} | "
                f"{_fmt(t.reason)} |"
            )
    lines.append("")
    lines.append("## 6. 自上次报告以来的账户事件")
    lines.append("")
    if not data.events:
        lines.append("本时段无账户事件。")
    else:
        for event in data.events:
            lines.append(f"- `{event.occurred_at.astimezone(tz).strftime('%H:%M:%S')}` "
                         f"**{event.kind}**：{event.summary}")
    lines.append("")
    lines.append("## 7. 下一周期需关注的风险触发条件")
    lines.append("")
    lines.append("| 代码 | 说明 | 严重度 | 阈值 | 当前 | 距离 | 已触发 |")
    lines.append("|---|---|---|---|---|---|---|")
    for item in data.risk_watch:
        lines.append(
            f"| {item.code} | {item.description} | {item.severity} | "
            f"{_fmt(_text(item.threshold))} | {_fmt(_text(item.current))} | "
            f"{_fmt(_text(item.distance))} | {'是' if item.triggered else '否'} |"
        )
    lines.append("")
    lines.append("## 8. 数据质量")
    lines.append("")
    dq = data.data_quality
    lines.append(f"- 账本校验：{'通过' if dq['ledger_verified'] else '未通过'}"
                 f"　审计链：{'通过' if dq['audit_chain_ok'] else '未通过'}"
                 f"　账户核对：{'通过' if dq['reconciliation_ok'] else '未通过'}")
    lines.append(f"- 行情可用：{dq['quote_available']}　可用于决策：{dq['quote_valid_for_decisions']}")
    if dq["unverifiable_fields"]:
        lines.append(f"- **无法核实的字段**（在快照中为 null，未伪装成 0）："
                     f"{', '.join(dq['unverifiable_fields'])}")
    else:
        lines.append("- 所有字段均已核实。")
    for problem in dq["reconciliation_problems"]:
        lines.append(f"  - ⚠ {problem}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 久留美的市场分析")
    lines.append("")
    lines.append("<!-- 由 Agent 依据上述数据撰写，不得编造未在上面出现的事实 -->")
    lines.append("")
    lines.append("## 久留美的交易日记")
    lines.append("")
    lines.append("<!-- 由 Agent 依据上述数据撰写，不得编造未在上面出现的事实 -->")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 快照校验
# --------------------------------------------------------------------------- #

_REQUIRED_TOP_KEYS: Final[tuple[str, ...]] = (
    "snapshot_version",
    "report_id",
    "report_kind",
    "report_at",
    "period",
    "trading_date",
    "symbol",
    "market",
    "account",
    "cumulative_trading_cost",
    "positions",
    "trades_in_period",
    "cash_flows_in_period",
    "events",
    "risk_watch",
    "data_quality",
    "integrity",
    "snapshot_sha256",
)

_REQUIRED_ACCOUNT_KEYS: Final[tuple[str, ...]] = (
    "initial_capital",
    "total_deposits",
    "total_withdrawals",
    "cash_balance",
    "realized_pnl",
    "unrealized_pnl",
    "equity",
    "net_profit",
    "return_pct",
    "used_margin",
    "free_margin",
    "margin_level",
)

#: 必须是十进制字符串（不得为 JSON 浮点）的字段。
_DECIMAL_STRING_PATHS: Final[tuple[str, ...]] = (
    "account.initial_capital",
    "account.total_deposits",
    "account.total_withdrawals",
    "account.cash_balance",
    "account.realized_pnl",
    "account.used_margin",
    "account.peak_equity",
    "account.day_start_equity",
    "cumulative_trading_cost.total_charged_jpy",
)


def verify_snapshot(
    snapshot: str | Path | Mapping[str, Any],
    *,
    cfg: Config | None = None,
    storage: Storage | None = None,
    require_ledger_match: bool = True,
) -> SnapshotVerification:
    """校验快照的格式、内部公式一致性，以及与权威账本的一致性。

    账本比对是**按时点重建**的：只累加 ``occurred_at <= report_at`` 的流水，
    因此历史快照同样可以校验（不会被之后发生的新流水误判为不一致）。
    """
    problems: list[str] = []
    checked: list[str] = []
    path: str | None = None

    if isinstance(snapshot, (str, Path)):
        path = str(snapshot)
        try:
            payload: Mapping[str, Any] = load_snapshot(snapshot)
        except (OSError, json.JSONDecodeError, EngineError) as exc:
            return SnapshotVerification(
                ok=False, path=path, snapshot_version=None, report_id=None,
                report_kind=None, report_at=None, format_ok=False, self_hash_ok=False,
                formulas_ok=False, ledger_ok=False,
                problems=(f"无法读取快照：{exc}",), checked=(),
            )
    else:
        payload = snapshot

    # --- 1) 格式 ---------------------------------------------------------- #
    format_ok = True
    for key in _REQUIRED_TOP_KEYS:
        if key not in payload:
            problems.append(f"缺少必需字段: {key}")
            format_ok = False
    checked.append("format.keys")

    version = payload.get("snapshot_version")
    if not isinstance(version, int) or version != SNAPSHOT_VERSION:
        problems.append(
            f"快照版本不兼容：{version!r}（本模块支持 {SNAPSHOT_VERSION}）"
        )
        format_ok = False
    checked.append("format.version")

    account = payload.get("account")
    if not isinstance(account, dict):
        problems.append("account 必须是对象")
        format_ok = False
        account = {}
    else:
        for key in _REQUIRED_ACCOUNT_KEYS:
            if key not in account:
                problems.append(f"account 缺少字段: {key}")
                format_ok = False
    checked.append("format.account_keys")

    positions = payload.get("positions")
    if not isinstance(positions, list):
        problems.append("positions 必须是数组（无持仓时为空数组）")
        format_ok = False
        positions = []
    for name in ("trades_in_period", "cash_flows_in_period", "events", "risk_watch"):
        if not isinstance(payload.get(name), list):
            problems.append(f"{name} 必须是数组")
            format_ok = False
    checked.append("format.arrays")

    # 金额必须是十进制字符串，不能是 JSON 浮点
    for dotted in _DECIMAL_STRING_PATHS:
        section, _, key = dotted.partition(".")
        holder = payload.get(section)
        value = holder.get(key) if isinstance(holder, dict) else None
        if value is None:
            continue
        if not isinstance(value, str):
            problems.append(f"{dotted} 必须是十进制字符串，收到 {type(value).__name__}")
            format_ok = False
            continue
        try:
            Decimal(value)
        except InvalidOperation:
            problems.append(f"{dotted} 不是合法十进制数字: {value!r}")
            format_ok = False
    checked.append("format.decimal_strings")

    # --- 2) 自哈希 -------------------------------------------------------- #
    self_hash_ok = True
    recorded_hash = payload.get("snapshot_sha256")
    if not isinstance(recorded_hash, str):
        problems.append("snapshot_sha256 缺失或不是字符串")
        self_hash_ok = False
    else:
        recomputed = _payload_hash(payload)
        if recomputed != recorded_hash:
            problems.append(
                f"自哈希不匹配：文件记录 {recorded_hash[:16]}…，重算 {recomputed[:16]}…"
                "（内容已被改动）"
            )
            self_hash_ok = False
    checked.append("self_hash")

    # --- 3) 内部公式 ------------------------------------------------------ #
    formulas_ok = True

    def dget(section: Mapping[str, Any], key: str) -> Decimal | None:
        value = section.get(key)
        if value is None:
            return None
        try:
            return _dec(value, f"{key}")
        except EngineError as exc:
            problems.append(str(exc))
            return None

    initial = dget(account, "initial_capital")
    deposits = dget(account, "total_deposits")
    withdrawals = dget(account, "total_withdrawals")
    equity = dget(account, "equity")
    net_profit = dget(account, "net_profit")
    return_pct = dget(account, "return_pct")

    if initial is not None and initial <= 0:
        problems.append(f"initial_capital 必须为正数，收到 {initial}")
        formulas_ok = False

    if equity is not None and net_profit is not None and initial is not None:
        expected = equity + (withdrawals or Decimal("0")) - initial - (deposits or Decimal("0"))
        if expected != net_profit:
            problems.append(
                f"累计净利润公式不成立：净值 {equity} + 出金 {withdrawals or 0} − "
                f"本金 {initial} − 入金 {deposits or 0} = {expected}，快照写的是 {net_profit}"
            )
            formulas_ok = False
        contribution = initial + (deposits or Decimal("0"))
        if contribution != 0 and return_pct is not None:
            expected_pct = (net_profit / contribution * 100).quantize(_PCT_EXPONENT)
            if expected_pct != return_pct.quantize(_PCT_EXPONENT):
                problems.append(
                    f"累计收益率公式不成立：期望 {expected_pct}%，快照写的是 {return_pct}%"
                )
                formulas_ok = False
    checked.append("formulas.net_profit")
    checked.append("formulas.return_pct")

    # 缺行情时净值必须为 null，不得伪装成 0
    market = payload.get("market") if isinstance(payload.get("market"), dict) else {}
    if not market.get("quote_available", False):
        for key in ("unrealized_pnl", "equity", "net_profit", "return_pct", "margin_level"):
            if account.get(key) is not None:
                problems.append(
                    f"行情不可用，account.{key} 应为 null（不得伪装成 0），"
                    f"收到 {account.get(key)!r}"
                )
                formulas_ok = False
    checked.append("formulas.no_fake_zero")

    # 无持仓必须是空数组
    if payload.get("integrity", {}).get("open_position_count") == 0 and positions:
        problems.append("integrity.open_position_count 为 0，但 positions 非空")
        formulas_ok = False
    checked.append("formulas.empty_positions")

    # --- 4) 与权威账本比对 ------------------------------------------------ #
    ledger_ok = True
    if storage is not None or require_ledger_match:
        try:
            target_storage = storage or Storage.from_config(cfg or load_config())
            report_at_raw = payload.get("report_at")
            report_at = datetime.fromisoformat(str(report_at_raw)) if report_at_raw else None
            if report_at is None or report_at.tzinfo is None:
                problems.append("report_at 缺失或缺少时区，无法与账本比对")
                ledger_ok = False
            else:
                flows = [
                    f for f in target_storage.read_cash_flows() if f.occurred_at <= report_at
                ]
                trades = [
                    t for t in target_storage.query_trades() if t.occurred_at <= report_at
                ]
                ledger_cash = sum((f.amount for f in flows), Decimal("0"))
                totals = _flow_totals(flows)
                ledger_deposits, ledger_withdrawals, dep_count, wd_count = _external_funding(flows)
                ledger_realized = totals.get(FlowType.REALIZED_PNL.value, Decimal("0"))
                # 重建「在 report_at 时刻仍持仓」的集合：不能只看当前状态，
                # 否则「报告时点还开着、之后才平掉」的持仓会被误判为不存在。
                ledger_positions = [
                    p
                    for p in target_storage.read_positions(status=None)
                    if p.entry_at <= report_at
                    and (p.closed_at is None or p.closed_at > report_at)
                ]
                # 注意：calculate_used_margin 按「当前状态」过滤，对实时用途正确，
                # 但用在这里的历史集合上会把「报告时点还开着、之后才平掉」的持仓
                # 算成 0。集合已经重建好了，直接汇总即可。
                ledger_margin = sum(
                    (_dec(p.margin_jpy) for p in ledger_positions), Decimal("0")
                )

                def compare(label: str, snapshot_value: Decimal | None, ledger_value: Decimal) -> None:
                    nonlocal ledger_ok
                    if snapshot_value is None:
                        problems.append(f"{label} 在快照中为 null，但账本可核实为 {ledger_value}")
                        ledger_ok = False
                        return
                    if snapshot_value != ledger_value:
                        problems.append(
                            f"{label} 与账本不一致：快照 {snapshot_value}，账本 {ledger_value}"
                        )
                        ledger_ok = False

                compare("account.cash_balance", dget(account, "cash_balance"), ledger_cash)
                compare("account.total_deposits", deposits, ledger_deposits)
                compare("account.total_withdrawals", withdrawals, ledger_withdrawals)
                compare("account.realized_pnl", dget(account, "realized_pnl"), ledger_realized)
                compare("account.used_margin", dget(account, "used_margin"), ledger_margin)
                compare("integrity.ledger_balance", dget(payload.get("integrity", {}), "ledger_balance"), ledger_cash)

                if account.get("deposit_count") != dep_count:
                    problems.append(
                        f"deposit_count 不一致：快照 {account.get('deposit_count')}，账本 {dep_count}"
                    )
                    ledger_ok = False
                if account.get("withdrawal_count") != wd_count:
                    problems.append(
                        f"withdrawal_count 不一致：快照 {account.get('withdrawal_count')}，"
                        f"账本 {wd_count}"
                    )
                    ledger_ok = False
                integrity = payload.get("integrity", {})
                if integrity.get("flow_count") != len(flows):
                    problems.append(
                        f"integrity.flow_count 不一致：快照 {integrity.get('flow_count')}，"
                        f"账本 {len(flows)}"
                    )
                    ledger_ok = False
                if integrity.get("trade_count") != len(trades):
                    problems.append(
                        f"integrity.trade_count 不一致：快照 {integrity.get('trade_count')}，"
                        f"账本 {len(trades)}"
                    )
                    ledger_ok = False
                if integrity.get("open_position_count") != len(ledger_positions):
                    problems.append(
                        f"integrity.open_position_count 不一致：快照 "
                        f"{integrity.get('open_position_count')}，账本 {len(ledger_positions)}"
                    )
                    ledger_ok = False
                if len(positions) != len(ledger_positions):
                    problems.append(
                        f"positions 数量不一致：快照 {len(positions)}，账本 {len(ledger_positions)}"
                    )
                    ledger_ok = False
                checked.append("ledger.compare")
        except (StorageError, EngineError, OSError) as exc:
            problems.append(f"与账本比对失败：{type(exc).__name__}: {exc}")
            ledger_ok = False

    return SnapshotVerification(
        ok=not problems,
        path=path,
        snapshot_version=version if isinstance(version, int) else None,
        report_id=payload.get("report_id"),
        report_kind=payload.get("report_kind"),
        report_at=(
            datetime.fromisoformat(str(payload["report_at"]))
            if payload.get("report_at")
            else None
        ),
        format_ok=format_ok,
        self_hash_ok=self_hash_ok,
        formulas_ok=formulas_ok,
        ledger_ok=ledger_ok,
        problems=tuple(problems),
        checked=tuple(checked),
    )


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m reporting",
        description="生成账户报告数据包、JSON 快照与基础报告模板（只读，不改账户）。",
    )
    parser.add_argument("--kind", default=None, help="morning / evening（默认按当前时刻推断）")
    parser.add_argument("--at", default=None, help="报告时刻（ISO 8601，默认当前时间）")
    parser.add_argument("--out-dir", default=None, help="快照输出目录（默认 config 的 reports 目录）")
    parser.add_argument("--no-quote", action="store_true", help="不获取行情（净值等字段将为 null）")
    parser.add_argument("--verify", default=None, help="只校验指定快照文件")
    parser.add_argument("--print-template", action="store_true", help="打印报告模板")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()

    if args.verify:
        result = verify_snapshot(args.verify, cfg=cfg)
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1

    report_at = datetime.fromisoformat(args.at) if args.at else None
    data = build_report_data(
        args.kind,
        cfg=cfg,
        report_at=report_at,
        **({"quote_source": _no_quote} if args.no_quote else {}),
    )
    snapshot_path = export_snapshot(data, out_dir=args.out_dir, cfg=cfg)
    template_path = snapshot_path.with_suffix(".md")
    template_path.write_text(generate_report_template(data), encoding="utf-8")

    print(f"报告种类      : {data.kind.value}（{data.kind.label}）")
    print(f"报告时刻      : {data.report_at.astimezone(cfg.tz).isoformat(timespec='seconds')}")
    print(f"覆盖时段      : {data.period.start.astimezone(cfg.tz).isoformat(timespec='seconds')}"
          f" ~ {data.period.end.astimezone(cfg.tz).isoformat(timespec='seconds')}")
    print(f"行情          : {'可用' if data.market.quote_available else '不可用'}"
          f"（{'过期' if data.market.is_stale else '新鲜'}）来源 {data.market.source}")
    print(f"现金 / 净值   : {_text(data.account.cash_balance)} / {_fmt(_text(data.account.equity))}")
    print(f"累计净利润    : {_fmt(_text(data.account.net_profit))}"
          f"　累计收益率 {_fmt(_text(data.account.return_pct), '%')}")
    print(f"持仓 / 本期成交: {len(data.positions)} / {len(data.trades_in_period)}")
    print(f"快照          : {snapshot_path}")
    print(f"模板          : {template_path}")

    verification = verify_snapshot(snapshot_path, cfg=cfg)
    print(f"快照校验      : {'通过' if verification.ok else '未通过'}")
    for problem in verification.problems:
        print(f"  [问题] {problem}")
    if args.print_template:
        print("\n" + generate_report_template(data))
    return 0 if verification.ok else 1


def _no_quote() -> Quote:  # pragma: no cover - 仅 CLI 用
    raise MarketDataError("CLI 指定了 --no-quote")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
