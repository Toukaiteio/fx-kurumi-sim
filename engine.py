"""FX 战士久留美 —— 交易执行与盈亏计算核心。

定位
====

**本模块是整个模拟系统唯一的交易执行与盈亏计算核心。**

* 配置来自 :mod:`config`（不重写、不硬编码规则）；
* 行情来自 :mod:`market_data`（不重新实现接口）；
* 持久化一律通过 :mod:`storage`（不绕过、不直接写库）。

价格与盈亏模型（严格按规则 1–4）
================================

=============================== ==============================================
开多仓                           成交价 = **Ask**
平多仓                           成交价 = **Bid**
开空仓                           成交价 = **Bid**
平空仓                           成交价 = **Ask**
多仓浮动盈亏                     ``(当前 Bid − 开仓 Ask) × 美元持仓数量``
空仓浮动盈亏                     ``(开仓 Bid − 当前 Ask) × 美元持仓数量``
=============================== ==============================================

因为成交价直接用 Bid/Ask，**点差已经体现在盈亏里**，所以余额不再二次扣点差。
平仓时的已实现盈亏就是「用平仓报价套用同一个公式」的结果，因此浮动盈亏与
已实现盈亏之间不存在口径漂移。

滑点的处理（一处需要你确认的判断）
----------------------------------

规则 1–4 明确把成交价定义为裸 Bid/Ask，因此本模块**不把滑点加进成交价**。
``market.slippage_jpy`` 只在两处使用：

1. 规则 7 要求的「止损风险包含预期不利滑点」——计入 ``planned_risk_jpy``；
2. 作为审计信息记录在成交行上（``slippage_jpy``）。

它**不会**再从余额里扣一次（也没有可扣的：成交价里没有它）。
若你希望滑点真实恶化成交价，只需在 :meth:`TradingEngine._fill_price` 里加一项，
盈亏与余额会自动跟随，无需改动账本逻辑。

保证金模型（规则 5）
====================

``名义金额(日元) = 美元持仓数量 × 成交价``
``所需保证金 = 名义金额 / max_leverage``（默认 25）

并执行配置限额：开仓后已用保证金不得超过净值的 ``max_margin_usage_ratio``
（60%），且名义金额/净值不得超过 ``max_leverage``。

账本模型
========

``现金余额 = 初始本金 + 入金 − 出金 + 已实现盈亏 − 独立扣款费用``
``账户净值 = 现金余额 + 当前持仓浮动盈亏``

* **开仓不产生任何现金流**（保证金是冻结占用，不是支出），只更新已用保证金；
* 平仓时由 :mod:`storage` 按已实现盈亏写一条 ``REALIZED_PNL`` 流水；
* 独立手续费（佣金）与已核验的掉期费用各以唯一的 ``event_key`` 记账，只扣一次；
* 点差与滑点**不**作为独立费用出现（它们已体现在成交价/盈亏里）。

余额永远由 :meth:`storage.Storage.ledger_balance` 决定；本模块从不直接指定余额
或最终盈亏，所有数字都由上述公式推导。调用方（包括 AI）只能提交**交易意图**，
不能提交余额、盈亏或成交价。

幂等性
======

* ``TradeIntent.intent_id`` 提供时，重复提交同一指令会返回既有持仓
  （``status="DUPLICATE"``），不重复开仓；
* 平仓天然幂等：已平仓的持仓再次平仓会返回既有结果，不重复记账；
* 未提供 ``intent_id`` 时每次调用生成新的幂等键（即不做去重），此时规则 8
  （同一方向最多一笔）仍会拦住重复开仓。

安全边界
========

本项目只做虚拟交易模拟：不连接券商、不下单、不做真实资金转账。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Final, Iterable, Sequence

from config import Config, load_config
from market_data import Quote
from storage import (
    INITIAL_CAPITAL_EVENT_KEY,
    AccountState,
    AccountStatus,
    CashFlow,
    DuplicateEventError,
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
    "AccountNotTradableError",
    "CAPITAL_DECAY_EVENT_PREFIX",
    "CAPITAL_PRESSURE_ANCHOR_META_KEY",
    "GOAL_REWARD_EVENT_PREFIX",
    "GOAL_STATE_META_KEY",
    "GoalRewardResult",
    "CapitalPressureResult",
    "CloseExecution",
    "CloseReason",
    "EngineError",
    "EngineReconciliation",
    "ExitTrigger",
    "MarkToMarket",
    "MIN_REWARD_RISK_RATIO",
    "PositionNotFoundError",
    "ProcessResult",
    "QuoteUnusableError",
    "TradeExecution",
    "TradeIntent",
    "TradeRejectedError",
    "TradeStatus",
    "TradeValidation",
    "TradingEngine",
    "calculate_equity",
    "capital_pressure_slot",
    "capital_pressure_slots_between",
    "calculate_margin_level",
    "calculate_required_margin",
    "calculate_unrealized_pnl",
    "calculate_used_margin",
    "open_engine",
]

#: 计划盈亏比下限的**回退默认值**。现在它已经配置化
#: （``risk.min_reward_risk_ratio``），这个常量只在配置缺失时兜底。
MIN_REWARD_RISK_RATIO: Final[Decimal] = Decimal("1.5")

#: 报价允许比本地时钟快出的容差（与行情层一致）。
_FUTURE_TOLERANCE_SECONDS: Final[int] = 300

#: 金额输出精度：日元保留 3 位小数（与行情层价格精度一致）。
_MONEY_EXPONENT: Final[Decimal] = Decimal("0.001")

#: 交易日的分界时刻（东京时间）。当日基准取该时刻的净值，
#: 因此 [08:00 JST 今天, 08:00 JST 明天) 属于同一个交易日。
TRADING_DAY_BOUNDARY_HOUR: Final[int] = 8

#: 验收闸门在 ``meta`` 表里的键。值为 ``BLOCKED:<原因>`` 时禁止开仓。
#: 这样「关键测试未通过就禁止开仓」是**真实生效**的，而不是写个 halted 就被
#: 下一次盯市按条件清掉。
ACCEPTANCE_GATE_META_KEY: Final[str] = "acceptance_gate"
GATE_BLOCKED_PREFIX: Final[str] = "BLOCKED:"

#: 督促机制扣款流水的事件键前缀。键为 ``CAPITAL_DECAY:<槽位>``，
#: 靠 ``cash_flows.event_key`` 的 UNIQUE 约束保证**同一个槽位只扣一次**。
logger = logging.getLogger("kurumi_fx_sim.engine")

CAPITAL_DECAY_EVENT_PREFIX: Final[str] = "CAPITAL_DECAY:"

#: 督促机制起算点存在 ``meta`` 表里的键（首次调用时写入，不追溯生效）。
CAPITAL_PRESSURE_ANCHOR_META_KEY: Final[str] = "capital_pressure_anchor"

#: 目标 + 奖励的当前状态存在 ``meta`` 表里的键（JSON）。
GOAL_STATE_META_KEY: Final[str] = "goal_state"

#: 目标奖励流水的事件键前缀：``GOAL_REWARD:L<档位>:<目标值>``。
GOAL_REWARD_EVENT_PREFIX: Final[str] = "GOAL_REWARD:"


def capital_pressure_slot(
    moment: datetime,
    *,
    tz: tzinfo,
    interval_hours: int,
) -> datetime:
    """返回 ``moment`` 所属的**督促槽位**起点（东京时间的自然小时对齐）。

    槽位按「自然小时」切分，所以 6 小时一档就是 00:00 / 06:00 / 12:00 / 18:00。
    用自然槽位而不是「距上次扣款满 6 小时」，是为了让扣款时刻**可预测**：
    AI 能算出下一次扣款在几点，也就知道还剩多少时间。
    """
    hours = max(1, int(interval_hours))
    local = moment.astimezone(tz)
    slot_hour = (local.hour // hours) * hours
    return local.replace(hour=slot_hour, minute=0, second=0, microsecond=0)


def capital_pressure_slots_between(
    start: datetime,
    end: datetime,
    *,
    tz: tzinfo,
    interval_hours: int,
) -> list[datetime]:
    """枚举 ``(start, end]`` 之间已经走完的槽位起点，按时间正序。

    左开右闭：``start`` 所在的槽位已经算过（它就是上一次扣款的槽位），
    ``end`` 所在的槽位只要已经跨过起点就算「到期」。
    """
    first = capital_pressure_slot(start, tz=tz, interval_hours=interval_hours)
    last = capital_pressure_slot(end, tz=tz, interval_hours=interval_hours)
    step = timedelta(hours=max(1, int(interval_hours)))
    slots: list[datetime] = []
    cursor = first + step
    while cursor <= last:
        slots.append(cursor)
        cursor += step
    return slots


@dataclass(frozen=True, slots=True)
class GoalRewardResult:
    """一次「目标 + 奖励」结算的结果。"""

    enabled: bool
    achieved: bool
    level: int
    target_jpy: Decimal
    equity_jpy: Decimal
    progress_ratio: Decimal | None
    reward_paid_jpy: Decimal
    next_target_jpy: Decimal | None
    next_reward_jpy: Decimal | None
    elapsed_hours: Decimal | None
    total_reward_jpy: Decimal
    achieved_count: int
    skipped_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        def _t(v):
            return None if v is None else format(v, "f")
        return {
            "enabled": self.enabled,
            "achieved": self.achieved,
            "level": self.level,
            "target_jpy": _t(self.target_jpy),
            "equity_jpy": _t(self.equity_jpy),
            "progress_ratio": _t(self.progress_ratio),
            "reward_paid_jpy": _t(self.reward_paid_jpy),
            "next_target_jpy": _t(self.next_target_jpy),
            "next_reward_jpy": _t(self.next_reward_jpy),
            "elapsed_hours": _t(self.elapsed_hours),
            "total_reward_jpy": _t(self.total_reward_jpy),
            "achieved_count": self.achieved_count,
            "skipped_reason": self.skipped_reason,
        }


@dataclass(frozen=True, slots=True)
class CapitalPressureResult:
    """一次督促机制结算的结果。"""

    enabled: bool
    applied: bool
    charged_jpy: Decimal
    slots: tuple[str, ...]
    skipped_reason: str | None
    next_slot_at: datetime | None
    total_charged_jpy: Decimal
    charged_count: int
    cash_balance_after: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "applied": self.applied,
            "charged_jpy": str(self.charged_jpy),
            "slots": list(self.slots),
            "skipped_reason": self.skipped_reason,
            "next_slot_at": (
                self.next_slot_at.isoformat(timespec="minutes")
                if self.next_slot_at is not None
                else None
            ),
            "total_charged_jpy": str(self.total_charged_jpy),
            "charged_count": self.charged_count,
            "cash_balance_after": str(self.cash_balance_after),
        }


def trading_date_for(
    moment: datetime,
    tz: tzinfo,
    *,
    boundary_hour: int = TRADING_DAY_BOUNDARY_HOUR,
) -> date:
    """按 08:00 JST 分界求所属交易日。

    08:00 之前属于前一个交易日（例如 10-11 07:30 JST 属于 10-10 这个交易日）。
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    local = moment.astimezone(tz)
    if local.hour < boundary_hour:
        return (local - timedelta(days=1)).date()
    return local.date()


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class EngineError(RuntimeError):
    """引擎层所有异常的基类。"""


class TradeRejectedError(EngineError):
    """交易意图未通过校验（规则 6–9）。异常上附带完整的校验明细。"""

    def __init__(self, message: str, validation: TradeValidation) -> None:
        super().__init__(message)
        self.validation = validation


class QuoteUnusableError(EngineError):
    """报价不可用：过期、非法或与交易品种不匹配。"""


class AccountNotTradableError(EngineError):
    """账户当前不可交易（已停止、已爆仓、净值过低等）。"""


class PositionNotFoundError(EngineError):
    """指定的持仓不存在。"""


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


class CloseReason(str, Enum):
    """平仓原因（规则 10：手动与自动触发必须各自留痕）。"""

    MANUAL = "MANUAL"                    # 手动平仓
    STOP_LOSS = "STOP_LOSS"              # 触发止损
    TAKE_PROFIT = "TAKE_PROFIT"          # 触发止盈
    MARGIN_CALL = "MARGIN_CALL"          # 保证金水平不足，强制平仓
    DAILY_DRAWDOWN = "DAILY_DRAWDOWN"    # 当日净值回撤触发，停止当日交易
    BANKRUPTCY = "BANKRUPTCY"            # 净值归零，爆仓
    SESSION_END = "SESSION_END"          # 收尾/结算

    @property
    def is_automatic(self) -> bool:
        return self is not CloseReason.MANUAL

    @property
    def is_forced(self) -> bool:
        return self in (
            CloseReason.MARGIN_CALL,
            CloseReason.DAILY_DRAWDOWN,
            CloseReason.BANKRUPTCY,
        )


class TradeStatus(str, Enum):
    OPENED = "OPENED"
    DUPLICATE = "DUPLICATE"


# --------------------------------------------------------------------------- #
# 金额工具
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
    """把金额/盈亏量化到 3 位小数（日元），避免无限小数进入账本。"""
    return value.quantize(_MONEY_EXPONENT, rounding=ROUND_HALF_UP)


def _opt_dec(value: Any | None, field_name: str = "value") -> Decimal | None:
    return None if value is None else _dec(value, field_name)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------- #
# 交易意图与结果
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """一笔待执行的交易意图。

    **只允许描述「想做什么」**：品种、方向、数量、止损、止盈。
    成交价、保证金、盈亏、余额一律由引擎计算，调用方无权指定。
    """

    symbol: str
    side: PositionSide
    volume_usd: Decimal
    #: 止损价/止盈价。允许为空，以便校验层按规则 6 给出可读的拒绝原因；
    #: 引擎默认 ``require_stop_and_target=True``，缺失会被拒绝开仓。
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    #: 幂等键。AI/上层应传入稳定标识（例如决策 id）以获得「恰好一次」语义。
    intent_id: str | None = None
    #: 决策来源说明（写进成交记录与审计，便于回溯是哪次决策下的单）。
    decision_ref: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "volume_usd", _dec(self.volume_usd, "volume_usd"))
        if self.stop_loss is not None:
            object.__setattr__(self, "stop_loss", _dec(self.stop_loss, "stop_loss"))
        if self.take_profit is not None:
            object.__setattr__(self, "take_profit", _dec(self.take_profit, "take_profit"))
        if isinstance(self.side, str):
            try:
                object.__setattr__(self, "side", PositionSide(self.side))
            except ValueError as exc:
                raise EngineError(f"side 非法: {self.side!r}") from exc

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "volume_usd": str(self.volume_usd),
            "stop_loss": str(self.stop_loss) if self.stop_loss is not None else None,
            "take_profit": str(self.take_profit) if self.take_profit is not None else None,
            "intent_id": self.intent_id,
            "decision_ref": self.decision_ref,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class TradeValidation:
    """校验结果与全部中间量。``ok=False`` 时 ``problems`` 说明原因。"""

    ok: bool
    problems: tuple[str, ...]
    symbol: str
    side: str
    volume_usd: Decimal
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    stop_distance_jpy: Decimal | None = None
    take_distance_jpy: Decimal | None = None
    slippage_buffer_jpy: Decimal | None = None
    risk_per_unit_jpy: Decimal | None = None
    planned_risk_jpy: Decimal | None = None
    planned_reward_jpy: Decimal | None = None
    reward_risk_ratio: Decimal | None = None
    notional_jpy: Decimal | None = None
    required_margin_jpy: Decimal | None = None
    effective_leverage: Decimal | None = None
    spread_cost_jpy: Decimal | None = None
    equity_before: Decimal | None = None
    equity_at_entry: Decimal | None = None
    used_margin_before: Decimal | None = None
    used_margin_after: Decimal | None = None
    margin_usage_ratio_after: Decimal | None = None
    risk_limit_jpy: Decimal | None = None
    total_risk_limit_jpy: Decimal | None = None
    projected_total_risk_jpy: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for item in dataclasses.fields(self):
            value = getattr(self, item.name)
            if isinstance(value, Decimal):
                payload[item.name] = str(value)
            elif item.name == "problems":
                payload[item.name] = list(value)
            else:
                payload[item.name] = value
        return payload


@dataclass(frozen=True, slots=True)
class TradeExecution:
    status: str
    position: Position
    trade: TradeRecord | None
    validation: TradeValidation | None
    quote: Quote
    intent: TradeIntent

    @property
    def is_duplicate(self) -> bool:
        return self.status == TradeStatus.DUPLICATE.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "position": self.position.as_dict(),
            "trade": self.trade.as_dict() if self.trade else None,
            "validation": self.validation.as_dict() if self.validation else None,
            "intent": self.intent.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class CloseExecution:
    status: str
    position: Position
    trade: TradeRecord
    realized_pnl_jpy: Decimal
    reason: str
    quote: Quote
    cash_balance_after: Decimal
    equity_after: Decimal

    @property
    def is_duplicate(self) -> bool:
        return self.status == TradeStatus.DUPLICATE.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "position": self.position.as_dict(),
            "trade": self.trade.as_dict(),
            "realized_pnl_jpy": str(self.realized_pnl_jpy),
            "reason": self.reason,
            "cash_balance_after": str(self.cash_balance_after),
            "equity_after": str(self.equity_after),
        }


@dataclass(frozen=True, slots=True)
class ExitTrigger:
    """自动平仓触发结果。"""

    position_id: int
    reason: CloseReason
    trigger_price: Decimal
    fill_price: Decimal
    note: str
    #: 是否为「跳空越过止损」：此时成交价取首个可执行报价并叠加不利滑点，
    #: 而不是假设以止损价成交。
    is_gap: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "reason": self.reason.value,
            "trigger_price": str(self.trigger_price),
            "fill_price": str(self.fill_price),
            "note": self.note,
            "is_gap": self.is_gap,
        }


@dataclass(frozen=True, slots=True)
class MarkToMarket:
    """一次盯市的结果。"""

    as_of: datetime
    cash_balance: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    used_margin: Decimal
    margin_level: Decimal | None
    peak_equity: Decimal
    day_start_equity: Decimal
    daily_drawdown_ratio: Decimal
    status: str
    halted: bool
    halt_reason: str | None
    warnings: tuple[str, ...]
    state: AccountState

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(timespec="seconds"),
            "cash_balance": str(self.cash_balance),
            "unrealized_pnl": str(self.unrealized_pnl),
            "equity": str(self.equity),
            "used_margin": str(self.used_margin),
            "margin_level": str(self.margin_level) if self.margin_level is not None else None,
            "peak_equity": str(self.peak_equity),
            "day_start_equity": str(self.day_start_equity),
            "daily_drawdown_ratio": str(self.daily_drawdown_ratio),
            "status": self.status,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """``process_quote`` 的结果：盯市 + 本次触发的所有平仓。"""

    mark: MarkToMarket
    closed: tuple[CloseExecution, ...]
    triggers: tuple[ExitTrigger, ...]

    @property
    def did_close(self) -> bool:
        return bool(self.closed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mark": self.mark.as_dict(),
            "closed": [item.as_dict() for item in self.closed],
            "triggers": [item.as_dict() for item in self.triggers],
        }


@dataclass(frozen=True, slots=True)
class EngineReconciliation:
    """引擎级账户核对：storage 的核对 + 账本口径与保证金投影的交叉检查。"""

    ok: bool
    checked_at: datetime
    problems: tuple[str, ...]
    storage_report: ReconciliationReport
    ledger_balance: Decimal
    cash_from_ledger_rule: Decimal
    cash_difference: Decimal
    margin_used_stored: Decimal
    margin_used_from_positions: Decimal
    margin_difference: Decimal
    equity_stored: Decimal
    equity_recomputed: Decimal
    equity_difference: Decimal
    open_position_count: int
    flow_totals: dict[str, Decimal]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at.isoformat(timespec="seconds"),
            "problems": list(self.problems),
            "storage_report": self.storage_report.as_dict(),
            "ledger_balance": str(self.ledger_balance),
            "cash_from_ledger_rule": str(self.cash_from_ledger_rule),
            "cash_difference": str(self.cash_difference),
            "margin_used_stored": str(self.margin_used_stored),
            "margin_used_from_positions": str(self.margin_used_from_positions),
            "margin_difference": str(self.margin_difference),
            "equity_stored": str(self.equity_stored),
            "equity_recomputed": str(self.equity_recomputed),
            "equity_difference": str(self.equity_difference),
            "open_position_count": self.open_position_count,
            "flow_totals": {key: str(value) for key, value in self.flow_totals.items()},
        }


# --------------------------------------------------------------------------- #
# 纯计算函数（无副作用，可单独测试）
# --------------------------------------------------------------------------- #


def calculate_unrealized_pnl(position: Position, quote: Quote) -> Decimal:
    """按规则 3/4 计算持仓浮动盈亏（日元）。

    多仓：``(当前 Bid − 开仓 Ask) × 美元数量``
    空仓：``(开仓 Bid − 当前 Ask) × 美元数量``

    注意 ``position.entry_price`` 就是开仓时的成交价（多仓为 Ask、空仓为 Bid），
    因此公式可直接用当前 Bid/Ask 套用。平仓时传入平仓报价，得到的就是已实现盈亏。
    """
    side = PositionSide(position.side)
    volume = _dec(position.volume, "volume")
    entry = _dec(position.entry_price, "entry_price")
    if side is PositionSide.LONG:
        return _money((_dec(quote.bid, "bid") - entry) * volume)
    return _money((entry - _dec(quote.ask, "ask")) * volume)


def calculate_required_margin(
    volume_usd: Decimal | int | str,
    price: Decimal | int | str,
    cfg: Config | None = None,
    *,
    leverage: Decimal | int | str | None = None,
) -> Decimal:
    """规则 5：所需保证金 = 名义金额(日元) / 杠杆。

    名义金额 = 美元数量 × 价格。杠杆默认取 ``risk.max_leverage``（25）。
    """
    volume = _dec(volume_usd, "volume_usd")
    unit_price = _dec(price, "price")
    if volume <= 0 or unit_price <= 0:
        raise EngineError(f"名义金额计算需要正数：volume={volume}, price={unit_price}")
    if leverage is None:
        if cfg is None:
            cfg = load_config()
        leverage = cfg.risk.max_leverage
    divisor = _dec(leverage, "leverage")
    if divisor <= 0:
        raise EngineError(f"杠杆必须为正数: {divisor}")
    return _money(volume * unit_price / divisor)


def calculate_used_margin(positions: Iterable[Position]) -> Decimal:
    """已用保证金 = 所有未平仓持仓开仓时记录的保证金之和。

    保证金在开仓时按规则 5 确定并落库（``positions.margin_jpy``），之后不随
    行情漂移；这里只做汇总，保证引擎与账本口径一致。
    """
    total = Decimal("0")
    for position in positions:
        if PositionStatus(position.status) is PositionStatus.OPEN:
            total += _dec(position.margin_jpy, "margin_jpy")
    return _money(total)


def calculate_equity(
    cash_balance: Decimal | int | str, unrealized_pnl: Decimal | int | str
) -> Decimal:
    """账户净值 = 现金余额 + 当前持仓浮动盈亏。"""
    return _money(_dec(cash_balance, "cash_balance") + _dec(unrealized_pnl, "unrealized_pnl"))


def calculate_realized_pnl(
    position: Position, fill_price: Decimal | int | str
) -> Decimal:
    """按**指定成交价**计算已实现盈亏。

    与 :func:`calculate_unrealized_pnl` 是同一个公式，只是把「当前报价」换成
    「模拟成交价」。当成交价就是平仓侧的 Bid/Ask 时，两者结果完全一致；风险监控
    在跳空/强制平仓时会传入叠加了不利滑点的成交价，此时这里才是权威口径。
    """
    side = PositionSide(position.side)
    volume = _dec(position.volume, "volume")
    entry = _dec(position.entry_price, "entry_price")
    exit_price = _dec(fill_price, "fill_price")
    if side is PositionSide.LONG:
        return _money((exit_price - entry) * volume)
    return _money((entry - exit_price) * volume)


def calculate_margin_level(
    equity: Decimal | int | str, used_margin: Decimal | int | str
) -> Decimal | None:
    """保证金水平 = 净值 / 已用保证金。无持仓（已用保证金为 0）时返回 ``None``。"""
    margin = _dec(used_margin, "used_margin")
    if margin == 0:
        return None
    if margin < 0:
        raise EngineError(f"已用保证金不能为负: {margin}")
    return _dec(equity, "equity") / margin


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #


class TradingEngine:
    """虚拟交易执行与盈亏计算核心。

    典型用法::

        engine = TradingEngine.from_config()          # 用 config.json + 默认数据库
        engine.ensure_initialized()                   # 首次发放 50000 日元本金
        quote = get_current_quote()                   # 来自 market_data
        intent = TradeIntent(symbol="USD/JPY", side=PositionSide.LONG,
                             volume_usd=Decimal("1000"),
                             stop_loss=Decimal("157.5"),
                             take_profit=Decimal("158.75"),
                             intent_id="decision-0001")
        engine.execute_virtual_trade(intent, quote)
        engine.process_quote(quote)                   # 盯市 + 自动止损/止盈
    """

    def __init__(
        self,
        cfg: Config | None = None,
        storage: Storage | None = None,
        *,
        min_reward_risk_ratio: Decimal | int | str | None = None,
        allow_stale_quote: bool = False,
        require_stop_and_target: bool = True,
    ) -> None:
        self.cfg: Config = cfg or load_config()
        self.storage: Storage = storage or Storage.from_config(self.cfg)
        # 优先用显式传参；否则读配置（``risk.min_reward_risk_ratio``）；
        # 配置缺失时回退到模块常量，保持向后兼容。
        if min_reward_risk_ratio is None:
            min_reward_risk_ratio = getattr(
                self.cfg.risk, "min_reward_risk_ratio", MIN_REWARD_RISK_RATIO
            )
        self.min_reward_risk_ratio = _dec(min_reward_risk_ratio, "min_reward_risk_ratio")
        if self.min_reward_risk_ratio <= 0:
            raise EngineError("min_reward_risk_ratio 必须为正数")
        self.allow_stale_quote = allow_stale_quote
        self.require_stop_and_target = require_stop_and_target

    # -- 构造 -------------------------------------------------------------- #

    @classmethod
    def from_config(
        cls,
        cfg: Config | None = None,
        *,
        db_path: str | None = None,
        **kwargs: Any,
    ) -> TradingEngine:
        cfg = cfg or load_config()
        storage = (
            Storage(db_path, tz=cfg.tz) if db_path is not None else Storage.from_config(cfg)
        )
        return cls(cfg, storage, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - 便于日志排查
        return f"<TradingEngine {self.cfg.instrument.symbol} db={self.storage.db_path}>"

    # -- 计算函数（委托给模块级纯函数，便于 ``engine.xxx()`` 与 ``xxx()`` 两种写法） -- #

    def calculate_unrealized_pnl(self, position: Position, quote: Quote) -> Decimal:
        return calculate_unrealized_pnl(position, quote)

    def calculate_required_margin(
        self, volume_usd: Decimal | int | str, price: Decimal | int | str
    ) -> Decimal:
        return calculate_required_margin(volume_usd, price, self.cfg)

    def calculate_used_margin(self, positions: Iterable[Position] | None = None) -> Decimal:
        return calculate_used_margin(self.open_positions() if positions is None else positions)

    def calculate_equity(
        self, cash_balance: Decimal | int | str, unrealized_pnl: Decimal | int | str
    ) -> Decimal:
        return calculate_equity(cash_balance, unrealized_pnl)

    def calculate_margin_level(
        self, equity: Decimal | int | str, used_margin: Decimal | int | str
    ) -> Decimal | None:
        return calculate_margin_level(equity, used_margin)

    # -- 读取辅助 ---------------------------------------------------------- #

    def ensure_initialized(self) -> AccountState:
        """确保账户已初始化；已初始化时不会重复发放本金。"""
        if self.storage.is_initialized():
            return self.storage.read_account_state()
        return self.storage.initialize_account(
            self.cfg.account.initial_capital_jpy, currency=self.cfg.account.currency
        )

    def open_positions(self) -> list[Position]:
        return self.storage.read_positions(status=PositionStatus.OPEN)

    def account_state(self) -> AccountState:
        return self.storage.read_account_state()

    def cash_balance(self) -> Decimal:
        """现金余额，权威值来自账本。"""
        return self.storage.ledger_balance()

    # -- 报价与成交价 ------------------------------------------------------ #

    def _check_quote(self, quote: Quote, *, allow_stale: bool | None = None) -> None:
        """规则 9：开仓前重新检查报价有效性。"""
        if quote is None:
            raise QuoteUnusableError("报价为空")
        symbol = getattr(quote, "symbol", None)
        if symbol != self.cfg.instrument.symbol:
            raise QuoteUnusableError(
                f"报价品种 {symbol!r} 与配置品种 {self.cfg.instrument.symbol!r} 不一致"
            )
        bid = _dec(quote.bid, "bid")
        ask = _dec(quote.ask, "ask")
        if bid <= 0 or ask <= 0:
            raise QuoteUnusableError(f"报价必须为正数：bid={bid}, ask={ask}")
        if bid > ask:
            raise QuoteUnusableError(f"报价非法：bid={bid} > ask={ask}")
        if quote.timestamp.tzinfo is None:
            raise QuoteUnusableError("报价时间戳缺少时区信息")
        now = datetime.now(self.cfg.tz)
        if quote.timestamp > now + timedelta(seconds=_FUTURE_TOLERANCE_SECONDS):
            raise QuoteUnusableError(f"报价时间戳位于未来: {quote.timestamp.isoformat()}")
        stale_allowed = self.allow_stale_quote if allow_stale is None else allow_stale
        if getattr(quote, "is_stale", False) and not stale_allowed:
            raise QuoteUnusableError(
                f"报价已过期（{quote.source}，age={getattr(quote, 'age_seconds', '?')} 秒），"
                "拒绝开仓；确需按过期报价撮合请显式开启 allow_stale_quote"
            )

    @staticmethod
    def _fill_price(side: PositionSide, quote: Quote, *, opening: bool) -> Decimal:
        """规则 1/2：开多/平空用 Ask，开空/平多用 Bid。"""
        if opening:
            return _dec(quote.ask if side is PositionSide.LONG else quote.bid, "fill_price")
        return _dec(quote.bid if side is PositionSide.LONG else quote.ask, "fill_price")

    def check_quote(self, quote: Quote, *, allow_stale: bool | None = None) -> None:
        """公开的报价校验入口（规则 9），不合法时抛 :class:`QuoteUnusableError`。

        风险监控用它判断「报价是否可用于决策」，而不必依赖私有方法。
        """
        self._check_quote(quote, allow_stale=allow_stale)

    # -- 校验 -------------------------------------------------------------- #

    def validate_trade_intent(
        self,
        intent: TradeIntent,
        quote: Quote,
        *,
        state: AccountState | None = None,
        positions: Sequence[Position] | None = None,
    ) -> TradeValidation:
        """规则 6–9 的完整校验。**收集全部问题**而不是遇到第一个就返回。"""
        problems: list[str] = []
        cfg = self.cfg
        state = state or self.storage.read_account_state()
        positions = list(positions if positions is not None else self.open_positions())

        # --- 品种与数量 ---------------------------------------------------- #
        if intent.symbol != cfg.instrument.symbol:
            problems.append(
                f"交易品种 {intent.symbol!r} 与配置品种 {cfg.instrument.symbol!r} 不一致"
            )
        volume = intent.volume_usd
        if volume <= 0:
            problems.append(f"交易量必须为正数：{volume}")
        elif not cfg.is_valid_trade_volume(volume):
            problems.append(
                f"交易量 {volume} 必须是最小交易单位 {cfg.instrument.min_trade_unit} "
                f"{cfg.instrument.base_currency} 的正整数倍"
            )

        # --- 报价 ---------------------------------------------------------- #
        entry_price: Decimal | None = None
        try:
            self._check_quote(quote)
            entry_price = self._fill_price(intent.side, quote, opening=True)
        except QuoteUnusableError as exc:
            problems.append(str(exc))

        # --- 账户状态（规则 9） -------------------------------------------- #
        # 验收闸门必须**直接读**，不能只看 account_state.halted：后者是盯市时才重算的
        # 投影，闸门刚被设置、还没盯市时它仍是旧值，会留下一个「闸门已阻断但仍能开仓」
        # 的窗口。
        gate = self.storage.get_meta(ACCEPTANCE_GATE_META_KEY)
        if gate and gate.startswith(GATE_BLOCKED_PREFIX):
            gate_reason = gate[len(GATE_BLOCKED_PREFIX):].strip() or "关键测试未通过"
            problems.append(f"验收闸门未通过，禁止开仓：{gate_reason}")
        if state.status != AccountStatus.ACTIVE.value:
            problems.append(f"账户状态为 {state.status}，不允许开仓")
        if state.halted:
            problems.append(f"账户已停止开仓：{state.halt_reason or '未说明原因'}")
        equity_before = state.equity
        if equity_before <= cfg.margin.min_equity_to_open_jpy:
            problems.append(
                f"净值 {equity_before} 不高于停止开仓阈值 "
                f"{cfg.margin.min_equity_to_open_jpy}"
            )
        if equity_before <= cfg.margin.bankruptcy_equity_jpy:
            problems.append(f"净值 {equity_before} 已达到爆仓阈值")

        # --- 规则 8：同一方向最多一笔，禁止加仓/对冲 ----------------------- #
        if positions:
            held = ", ".join(
                f"#{p.position_id}({p.side} {p.volume})" for p in positions
            )
            problems.append(f"已持有未平仓持仓 {held}，禁止加仓、网格或反向对冲")

        # --- 规则 6：必须带有效止损止盈且盈亏比 >= 1:1.5 -------------------- #
        stop_distance: Decimal | None = None
        take_distance: Decimal | None = None
        reward_risk: Decimal | None = None
        if self.require_stop_and_target:
            if intent.stop_loss is None:
                problems.append("必须设置止损价")
            if intent.take_profit is None:
                problems.append("必须设置止盈价")
        if intent.stop_loss is not None and intent.stop_loss <= 0:
            problems.append(f"止损价必须为正数：{intent.stop_loss}")
        if intent.take_profit is not None and intent.take_profit <= 0:
            problems.append(f"止盈价必须为正数：{intent.take_profit}")

        if entry_price is not None and intent.stop_loss is not None and intent.take_profit is not None:
            if intent.side is PositionSide.LONG:
                if not intent.stop_loss < entry_price:
                    problems.append(
                        f"多仓止损价 {intent.stop_loss} 必须低于开仓价 {entry_price}"
                    )
                if not intent.take_profit > entry_price:
                    problems.append(
                        f"多仓止盈价 {intent.take_profit} 必须高于开仓价 {entry_price}"
                    )
            else:
                if not intent.stop_loss > entry_price:
                    problems.append(
                        f"空仓止损价 {intent.stop_loss} 必须高于开仓价 {entry_price}"
                    )
                if not intent.take_profit < entry_price:
                    problems.append(
                        f"空仓止盈价 {intent.take_profit} 必须低于开仓价 {entry_price}"
                    )
            if intent.stop_loss != entry_price:
                stop_distance = abs(entry_price - intent.stop_loss)
            if intent.take_profit != entry_price:
                take_distance = abs(intent.take_profit - entry_price)
            if stop_distance and take_distance:
                reward_risk = take_distance / stop_distance
                if reward_risk < self.min_reward_risk_ratio:
                    problems.append(
                        f"计划盈亏比 {reward_risk:.4f} 低于要求的 "
                        f"1:{self.min_reward_risk_ratio}（止损 {stop_distance} / "
                        f"止盈 {take_distance}）"
                    )

        # --- 规则 5：保证金 ------------------------------------------------- #
        notional: Decimal | None = None
        required_margin: Decimal | None = None
        effective_leverage: Decimal | None = None
        used_before = calculate_used_margin(positions)
        used_after: Decimal | None = None
        usage_ratio: Decimal | None = None
        spread_cost: Decimal | None = None
        equity_at_entry: Decimal | None = None
        if entry_price is not None and volume > 0:
            notional = _money(volume * entry_price)
            required_margin = calculate_required_margin(volume, entry_price, cfg)
            used_after = _money(used_before + required_margin)
            # 开仓瞬间点差即形成浮亏，用它算开仓后的净值更保守
            spread = _dec(quote.ask, "ask") - _dec(quote.bid, "bid")
            spread_cost = _money(spread * volume)
            equity_at_entry = _money(equity_before - spread_cost)
            if equity_at_entry > 0:
                effective_leverage = notional / equity_at_entry
                usage_ratio = used_after / equity_at_entry
                if usage_ratio > cfg.risk.max_margin_usage_ratio:
                    problems.append(
                        f"开仓后已用保证金 {used_after} 占净值 {equity_at_entry} 的 "
                        f"{usage_ratio:.4f}，超过上限 {cfg.risk.max_margin_usage_ratio}"
                    )
                if effective_leverage > cfg.risk.max_leverage:
                    problems.append(
                        f"开仓后名义杠杆 {effective_leverage:.4f} 超过上限 "
                        f"{cfg.risk.max_leverage}"
                    )
            else:
                problems.append(f"开仓后净值为 {equity_at_entry}，无法承担任何保证金")

        # --- 规则 7：止损风险（含预期不利滑点） ---------------------------- #
        slippage_buffer = _dec(cfg.market.slippage_jpy, "slippage_jpy")
        risk_per_unit: Decimal | None = None
        planned_risk: Decimal | None = None
        planned_reward: Decimal | None = None
        risk_limit: Decimal | None = None
        total_risk_limit: Decimal | None = None
        projected_total_risk: Decimal | None = None
        if stop_distance is not None and volume > 0:
            risk_per_unit = stop_distance + slippage_buffer
            planned_risk = _money(risk_per_unit * volume)
            if take_distance is not None:
                planned_reward = _money(take_distance * volume)
            risk_limit = _money(equity_before * cfg.risk.max_risk_per_trade_ratio)
            total_risk_limit = _money(equity_before * cfg.risk.max_total_risk_ratio)
            existing_risk = sum(
                (
                    _dec(p.planned_risk_jpy)
                    for p in positions
                    if p.planned_risk_jpy is not None
                ),
                Decimal("0"),
            )
            projected_total_risk = _money(existing_risk + planned_risk)
            if planned_risk > risk_limit:
                problems.append(
                    f"单笔计划止损风险 {planned_risk} 超过上限 {risk_limit}"
                    f"（净值 {equity_before} 的 {cfg.risk.max_risk_per_trade_ratio}）"
                )
            if projected_total_risk > total_risk_limit:
                problems.append(
                    f"累计计划止损风险 {projected_total_risk} 超过上限 {total_risk_limit}"
                    f"（净值 {equity_before} 的 {cfg.risk.max_total_risk_ratio}）"
                )

        return TradeValidation(
            ok=not problems,
            problems=tuple(problems),
            symbol=intent.symbol,
            side=intent.side.value,
            volume_usd=volume,
            entry_price=entry_price,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            stop_distance_jpy=stop_distance,
            take_distance_jpy=take_distance,
            slippage_buffer_jpy=slippage_buffer,
            risk_per_unit_jpy=risk_per_unit,
            planned_risk_jpy=planned_risk,
            planned_reward_jpy=planned_reward,
            reward_risk_ratio=reward_risk,
            notional_jpy=notional,
            required_margin_jpy=required_margin,
            effective_leverage=effective_leverage,
            spread_cost_jpy=spread_cost,
            equity_before=equity_before,
            equity_at_entry=equity_at_entry,
            used_margin_before=used_before,
            used_margin_after=used_after,
            margin_usage_ratio_after=usage_ratio,
            risk_limit_jpy=risk_limit,
            total_risk_limit_jpy=total_risk_limit,
            projected_total_risk_jpy=projected_total_risk,
        )

    # -- 开仓 -------------------------------------------------------------- #

    def open_event_key(self, intent: TradeIntent) -> str:
        """开仓指令的幂等键（公开入口）。

        调用方（例如 AI 决策层）需要用它来判断「这条指令是不是已经执行过」，
        因此不能让它自己去复制 ``OPEN:`` 前缀这套约定。
        """
        return self._open_event_key(intent)

    def _open_event_key(self, intent: TradeIntent) -> str:
        if intent.intent_id:
            return f"OPEN:{intent.intent_id}"
        # 未提供幂等键时生成随机键：不做去重，但规则 8 仍会拦住重复开仓。
        return f"OPEN:auto:{uuid.uuid4().hex}"

    def execute_virtual_trade(
        self,
        intent: TradeIntent,
        quote: Quote,
        *,
        occurred_at: datetime | None = None,
    ) -> TradeExecution:
        """执行虚拟开仓。

        流程：幂等检查 → 规则 6–9 校验 → 落库（持仓 + 成交 + 保证金投影 + 审计）。

        调用方**无法**指定成交价、保证金、盈亏或余额 —— 全部由本方法按规则计算。
        校验不通过时抛 :class:`TradeRejectedError`，异常上带完整校验明细。
        """
        event_key = self._open_event_key(intent)

        # 1) 幂等：同一 intent_id 重复提交直接返回既有持仓，不重复开仓。
        existing = self.storage.find_position_by_event_key(event_key)
        if existing is not None:
            return TradeExecution(
                status=TradeStatus.DUPLICATE.value,
                position=existing,
                trade=self._last_trade_for(existing.position_id),
                validation=None,
                quote=quote,
                intent=intent,
            )

        # 2) 校验（规则 9：开仓前重新检查报价、账户、保证金与全部风险限制）
        validation = self.validate_trade_intent(intent, quote)
        if not validation.ok:
            self.storage.audit(
                "TRADE_REJECTED",
                {"intent": intent.as_dict(), "problems": list(validation.problems)},
                occurred_at=occurred_at,
            )
            raise TradeRejectedError(
                "交易意图被拒绝：" + "；".join(validation.problems), validation
            )
        assert validation.entry_price is not None
        assert validation.required_margin_jpy is not None
        assert validation.planned_risk_jpy is not None

        # 3) 开仓前先确立当日基准：此刻账户还是空仓，基准即当日真实起始净值
        self.ensure_daily_baseline(quote)

        # 4) 落库
        position = self.storage.open_position(
            symbol=intent.symbol,
            side=intent.side,
            volume=intent.volume_usd,
            entry_price=validation.entry_price,
            margin_jpy=validation.required_margin_jpy,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            planned_risk_jpy=validation.planned_risk_jpy,
            quoted_price=_dec(quote.mid, "mid"),
            spread_jpy=_dec(quote.ask, "ask") - _dec(quote.bid, "bid"),
            slippage_jpy=validation.slippage_buffer_jpy,
            reason=intent.note or "virtual_open",
            decision_ref=intent.decision_ref,
            occurred_at=occurred_at,
            event_key=event_key,
        )
        trade = self._last_trade_for(position.position_id)

        # 保证金占用与净值投影（各自都是 storage 层的事务）
        self.storage.set_margin_used(calculate_used_margin(self.open_positions()))
        self._refresh_state(quote, occurred_at=occurred_at)

        self.storage.audit(
            "TRADE_EXECUTED",
            {
                "position_id": position.position_id,
                "intent": intent.as_dict(),
                "entry_price": validation.entry_price,
                "volume_usd": intent.volume_usd,
                "margin_jpy": validation.required_margin_jpy,
                "planned_risk_jpy": validation.planned_risk_jpy,
                "reward_risk_ratio": validation.reward_risk_ratio,
                "notional_jpy": validation.notional_jpy,
            },
            occurred_at=occurred_at,
        )
        return TradeExecution(
            status=TradeStatus.OPENED.value,
            position=position,
            trade=trade,
            validation=validation,
            quote=quote,
            intent=intent,
        )

    def _last_trade_for(self, position_id: int) -> TradeRecord | None:
        trades = self.storage.query_trades(position_id=position_id, order="DESC", limit=1)
        return trades[0] if trades else None

    # -- 平仓 -------------------------------------------------------------- #

    def evaluate_exit_triggers(self, position: Position, quote: Quote) -> ExitTrigger | None:
        """判断报价是否触发止损/止盈。

        多仓按 **Bid** 判定（平多走 Bid），空仓按 **Ask** 判定（平空走 Ask），
        与规则 1/2 的成交价一致，避免用中间价判定却在另一个价位成交。
        """
        side = PositionSide(position.side)
        if side is PositionSide.LONG:
            market = _dec(quote.bid, "bid")
            fill = market
        else:
            market = _dec(quote.ask, "ask")
            fill = market
        if position.stop_loss is not None:
            stop = _dec(position.stop_loss, "stop_loss")
            hit = market <= stop if side is PositionSide.LONG else market >= stop
            if hit:
                return ExitTrigger(
                    position_id=position.position_id,
                    reason=CloseReason.STOP_LOSS,
                    trigger_price=stop,
                    fill_price=fill,
                    note=f"{side.value} 止损：平仓价 {market} 触及止损价 {stop}",
                )
        if position.take_profit is not None:
            target = _dec(position.take_profit, "take_profit")
            hit = market >= target if side is PositionSide.LONG else market <= target
            if hit:
                return ExitTrigger(
                    position_id=position.position_id,
                    reason=CloseReason.TAKE_PROFIT,
                    trigger_price=target,
                    fill_price=fill,
                    note=f"{side.value} 止盈：平仓价 {market} 触及止盈价 {target}",
                )
        return None

    def close_position(
        self,
        position_id: int,
        quote: Quote,
        *,
        reason: Any = CloseReason.MANUAL,
        fee_jpy: Decimal | int | str | None = None,
        intent_id: str | None = None,
        occurred_at: datetime | None = None,
        allow_stale_quote: bool = True,
        fill_price: Decimal | int | str | None = None,
        fill_note: str | None = None,
    ) -> CloseExecution:
        """平仓（手动或自动触发）。

        * 平仓价按规则 1/2 取 Bid/Ask，已实现盈亏用同一公式算出，**不再扣点差**；
        * ``fill_price`` 可由风险监控传入，用于表达「跳空越过止损」这类更不利的
          成交模型（首个可执行报价 + 不利滑点）。传入后已实现盈亏按该成交价计算，
          点差/滑点都只体现在这一个价格里，不会再单独扣一次；
        * ``reason`` 区分手动与各类自动触发原因（规则 10），写入持仓与成交记录；
        * 幂等：已平仓的持仓再次平仓返回既有结果，不重复记账。

        平仓允许使用过期报价（``allow_stale_quote=True``）—— 止损/强平在行情
        停滞时仍必须能执行，否则会失去保护意义。
        """
        close_reason = reason if isinstance(reason, CloseReason) else CloseReason(reason)
        position = self.storage.read_position(int(position_id))
        if position is None:
            raise PositionNotFoundError(f"持仓不存在: {position_id}")

        # 幂等：已平仓直接返回既有成交
        if PositionStatus(position.status) is PositionStatus.CLOSED:
            trade = self._close_trade_for(position.position_id)
            return CloseExecution(
                status=TradeStatus.DUPLICATE.value,
                position=position,
                trade=trade,
                realized_pnl_jpy=_dec(position.realized_pnl_jpy or 0),
                reason=str(position.close_reason or close_reason.value),
                quote=quote,
                cash_balance_after=self.cash_balance(),
                equity_after=self.storage.read_account_state().equity,
            )

        if not allow_stale_quote:
            self._check_quote(quote)

        side = PositionSide(position.side)
        if fill_price is None:
            # 规则 1/2 的默认成交价：平多走 Bid、平空走 Ask
            fill = self._fill_price(side, quote, opening=False)
            realized = calculate_unrealized_pnl(position, quote)
        else:
            fill = _dec(fill_price, "fill_price")
            if fill <= 0:
                raise EngineError(f"成交价必须为正数: {fill}")
            realized = calculate_realized_pnl(position, fill)
        fee = _opt_dec(fee_jpy, "fee_jpy")
        # 持仓/成交的 reason 保持机器可读的原因码（便于按原因查询）；
        # 成交模型的详细说明（例如跳空规则）只进审计事件与 RiskEvent。
        reason_text = close_reason.value

        event_key = f"CLOSE:{position.position_id}:{intent_id}" if intent_id else None
        closed, trade = self.storage.close_position(
            position.position_id,
            exit_price=fill,
            realized_pnl_jpy=realized,
            reason=reason_text,
            fee_jpy=fee,
            quoted_price=_dec(quote.mid, "mid"),
            spread_jpy=_dec(quote.ask, "ask") - _dec(quote.bid, "bid"),
            slippage_jpy=None,
            occurred_at=occurred_at,
            event_key=event_key,
        )

        # 保证金占用与净值投影
        self.storage.set_margin_used(calculate_used_margin(self.open_positions()))
        state = self._refresh_state(quote, occurred_at=occurred_at)

        self.storage.audit(
            "POSITION_CLOSE_EXECUTED",
            {
                "position_id": closed.position_id,
                "reason": close_reason.value,
                "automatic": close_reason.is_automatic,
                "forced": close_reason.is_forced,
                "exit_price": fill,
                "fill_source": "explicit" if fill_price is not None else "bid_ask",
                "fill_note": fill_note,
                "realized_pnl_jpy": realized,
                "fee_jpy": fee,
                "cash_balance_after": self.cash_balance(),
                "equity_after": state.equity,
            },
            occurred_at=occurred_at,
        )
        return CloseExecution(
            status=TradeStatus.OPENED.value,
            position=closed,
            trade=trade,
            realized_pnl_jpy=realized,
            reason=reason_text,
            quote=quote,
            cash_balance_after=self.cash_balance(),
            equity_after=state.equity,
        )

    def _close_trade_for(self, position_id: int) -> TradeRecord:
        trades = self.storage.query_trades(position_id=position_id, order="DESC", limit=1)
        if not trades:  # pragma: no cover - 理论不可达
            raise StorageError(f"持仓 {position_id} 没有成交记录")
        return trades[0]

    # -- 盯市与风控 -------------------------------------------------------- #

    def trading_date(self, moment: datetime | None = None) -> date:
        """当前所属交易日（按 08:00 JST 分界）。"""
        return trading_date_for(moment or datetime.now(self.cfg.tz), self.cfg.tz)

    def _ensure_baseline(self, equity: Decimal, cash: Decimal, *, notes: str | None = None):
        """确保当日基准存在（每个交易日只创建一次），返回基准行。"""
        day = self.trading_date()
        baseline = self.storage.read_daily_baseline(day)
        if baseline is None:
            baseline = self.storage.ensure_daily_baseline(
                day, equity=equity, balance=cash, notes=notes
            )
        self.storage.update_daily_baseline(day, equity=equity)
        return baseline

    def ensure_daily_baseline(
        self, quote: Quote | None = None, *, notes: str | None = None
    ) -> Decimal:
        """确立当日基准（每个交易日只创建一次），返回当日起始净值。

        必须在当日首次交易**之前**调用。若等到开仓之后才确立，基准里会混入新
        仓位的浮动盈亏（开仓瞬间的点差浮亏），使「当日净值回撤 3%」的判定基准
        失真 —— 回撤会被低估，停止线触发偏晚。
        """
        state = self.storage.read_account_state()
        positions = self.open_positions()
        if positions:
            unrealized = (
                sum(
                    (calculate_unrealized_pnl(p, quote) for p in positions), Decimal("0")
                )
                if quote is not None
                else state.unrealized_pnl
            )
        else:
            unrealized = Decimal("0")
        cash = self.cash_balance()
        return self._ensure_baseline(
            calculate_equity(cash, unrealized), cash, notes=notes
        ).day_start_equity

    def set_daily_drawdown_stop(
        self, *, triggered_at: datetime | None = None, notes: str | None = None
    ) -> None:
        """标记「当日回撤停止已触发」。

        该标记落在当日基准行上，因此**当天剩余时间一直有效**（即使净值回升），
        而跨到下一个交易日（08:00 JST 之后）会自动失效。
        """
        day = self.trading_date()
        if self.storage.read_daily_baseline(day) is None:
            self.ensure_daily_baseline(notes=notes)
        self.storage.update_daily_baseline(
            day,
            drawdown_stop_triggered=True,
            triggered_at=triggered_at or datetime.now(self.cfg.tz),
            notes=notes,
        )

    def _refresh_state(self, quote: Quote, *, occurred_at: datetime | None = None) -> AccountState:
        """按当前报价重算浮盈/净值/保证金水平并落库（不改动现金余额）。"""
        positions = self.open_positions()
        unrealized = sum(
            (calculate_unrealized_pnl(p, quote) for p in positions), Decimal("0")
        )
        used_margin = calculate_used_margin(positions)
        cash = self.cash_balance()
        equity = calculate_equity(cash, unrealized)
        baseline = self._ensure_baseline(equity, cash)
        margin_level = calculate_margin_level(equity, used_margin)

        state = self.storage.read_account_state()
        peak = max(state.peak_equity, equity)
        halted, halt_reason, status = self._evaluate_halts(
            equity=equity,
            day_start_equity=baseline.day_start_equity,
            margin_level=margin_level,
            day_flagged=baseline.drawdown_stop_triggered,
            drawdown_equity=self.drawdown_basis_equity(equity, occurred_at),
        )
        updated = self.storage.save_account_state(
            dataclasses.replace(
                state,
                status=status,
                cash_balance=cash,
                equity=equity,
                margin_used=used_margin,
                unrealized_pnl=unrealized,
                peak_equity=peak,
                day_start_equity=baseline.day_start_equity,
                trading_date=baseline.trading_date,
                halted=halted,
                halt_reason=halt_reason,
                as_of=occurred_at or datetime.now(self.cfg.tz),
            ),
            expected_revision=state.revision,
        )
        return updated

    def _evaluate_halts(
        self,
        *,
        equity: Decimal,
        day_start_equity: Decimal,
        margin_level: Decimal | None,
        day_flagged: bool,
        drawdown_equity: Decimal | None = None,
    ) -> tuple[bool, str | None, str]:
        """按配置判定账户状态、是否停止开仓。返回 ``(halted, halt_reason, status)``。

        停止状态完全由**当前条件 + 当日回撤标记**推导，而不是「一旦停止就永久
        保持」—— 这样保证金水平回升、净值回升或跨到新交易日时都能自动恢复。

        ``drawdown_equity`` 只用于**当日回撤**这一项：它是「剔除督促机制扣款之后」
        的净值。原因见 :meth:`apply_capital_pressure` 的说明——督促扣款是固定
        游戏机制，不是交易亏损，如果让它计入回撤，一次 3000 的扣款（本金 50000
        的 6%）会立刻越过 3% 的当日回撤线，把账户按成「当日禁止开仓」，
        结果与「督促 AI 多行动」的目的完全相反。
        爆仓与最低净值两项仍然用**真实净值** ``equity``——钱是真的少了。
        """
        cfg = self.cfg
        if equity <= cfg.margin.bankruptcy_equity_jpy:
            return (
                True,
                f"净值 {equity} 不高于爆仓阈值 {cfg.margin.bankruptcy_equity_jpy}",
                AccountStatus.BANKRUPT.value,
            )
        gate = self.storage.get_meta(ACCEPTANCE_GATE_META_KEY)
        if gate and gate.startswith(GATE_BLOCKED_PREFIX):
            reason = gate[len(GATE_BLOCKED_PREFIX):].strip() or "关键测试未通过"
            return True, f"验收闸门未通过，禁止开仓：{reason}", AccountStatus.HALTED.value
        if day_start_equity > 0:
            basis = equity if drawdown_equity is None else drawdown_equity
            drawdown = (day_start_equity - basis) / day_start_equity
            if drawdown >= cfg.risk.daily_drawdown_stop_ratio:
                return (
                    True,
                    f"当日净值回撤 {drawdown:.4f} 达到停止阈值 "
                    f"{cfg.risk.daily_drawdown_stop_ratio}",
                    AccountStatus.HALTED.value,
                )
        if day_flagged:
            return (
                True,
                "当日已触发净值回撤停止，暂停当日新开仓",
                AccountStatus.HALTED.value,
            )
        if equity <= cfg.margin.min_equity_to_open_jpy:
            return (
                True,
                f"净值 {equity} 不高于停止开仓阈值 {cfg.margin.min_equity_to_open_jpy}",
                AccountStatus.HALTED.value,
            )
        if margin_level is not None and margin_level <= cfg.margin.margin_call_level:
            return (
                True,
                f"保证金水平 {margin_level:.4f} 不高于预警线 "
                f"{cfg.margin.margin_call_level}，暂停新开仓",
                AccountStatus.HALTED.value,
            )
        return False, None, AccountStatus.ACTIVE.value

    # -- 督促机制：定期扣本金 ------------------------------------------------- #

    def trading_day_start(self, moment: datetime | None = None) -> datetime:
        """当前交易日的起点（08:00 JST）。"""
        day = self.trading_date(moment)
        return datetime(
            day.year, day.month, day.day,
            TRADING_DAY_BOUNDARY_HOUR, tzinfo=self.cfg.tz,
        )

    def capital_pressure_charged_since(self, moment: datetime) -> Decimal:
        """``moment`` 之后累计被督促机制扣掉的钱（正数）。"""
        flows = self.storage.read_cash_flows(
            flow_type=FlowType.CAPITAL_DECAY, since=moment
        )
        return sum((-flow.amount for flow in flows), Decimal("0"))

    def capital_pressure_total(self) -> Decimal:
        """开户以来累计被督促机制扣掉的钱（正数）。"""
        flows = self.storage.read_cash_flows(flow_type=FlowType.CAPITAL_DECAY)
        return sum((-flow.amount for flow in flows), Decimal("0"))

    def drawdown_basis_equity(
        self, equity: Decimal, moment: datetime | None = None
    ) -> Decimal:
        """用于计算**当日回撤**的净值：把督促扣款加回去。

        这样回撤只反映交易表现，不反映固定游戏机制。
        """
        offset = self.capital_pressure_charged_since(self.trading_day_start(moment))
        return equity + offset

    def last_capital_pressure_at(self) -> datetime | None:
        flows = self.storage.read_cash_flows(flow_type=FlowType.CAPITAL_DECAY)
        return flows[-1].occurred_at if flows else None

    def next_capital_pressure_at(self, moment: datetime | None = None) -> datetime | None:
        """下一次扣款时刻（槽位起点）；机制关闭时返回 ``None``。"""
        pressure = self.cfg.pressure
        if not pressure.enabled:
            return None
        now = moment or datetime.now(self.cfg.tz)
        slot = capital_pressure_slot(
            now, tz=self.cfg.tz, interval_hours=pressure.interval_hours
        )
        return slot + timedelta(hours=pressure.interval_hours)

    def apply_capital_pressure(
        self,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
        quote: Quote | None = None,
    ) -> CapitalPressureResult:
        """结算「每 N 小时扣一笔本金」的督促机制。

        规则
        ----
        * 槽位按**自然小时**对齐（6 小时 → 00/06/12/18 点 JST），所以扣款时刻可预测：
          AI 能算出下一次在几点扣、还剩多少时间。
        * 每个槽位**只扣一次**：幂等键 ``CAPITAL_DECAY:<槽位>``，靠
          ``cash_flows.event_key`` 的 UNIQUE 约束保证；重复调用、进程重启都不会重复扣。
        * **起算点记在 ``meta`` 里**（``capital_pressure_anchor``），第一次调用时
          才写入。因此机制不会追溯生效——对一个已经跑了很久的账户启用它，
          不会立刻补扣过去几天的账。首次扣款落在起算点之后的第一个槽位边界。
        * ``catch_up_missed_slots=True``：起算点之后漏掉的槽位**全部补扣**
          （进程停机不能免罚）。``False``：只扣最近一个到期槽位，漏掉的免掉。
        * **单次扣款不超过当前可用现金**，余额不会被扣成负数。因此实际扣款额可能
          小于配置值——返回值里的 ``charged_jpy`` 是**实际**扣掉的金额。

        为什么记成 ``CAPITAL_DECAY`` 流水而不是直接改余额
        ------------------------------------------------
        账本是余额的唯一权威来源（``cash_balance == SUM(cash_flows)``），
        直接改余额会被 ``save_account_state`` 拒绝。记成流水后：净值与累计净利润
        都会下降（**AI 看得见代价**，这才是督促）；而它**不计入当日回撤**
        ——理由见 :meth:`_evaluate_halts` 的说明。
        """
        pressure = self.cfg.pressure
        moment = now or datetime.now(self.cfg.tz)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.cfg.tz)

        def _result(applied, charged, slots, reason):
            return CapitalPressureResult(
                enabled=pressure.enabled,
                applied=applied,
                charged_jpy=_money(charged),
                slots=tuple(slots),
                skipped_reason=reason,
                next_slot_at=self.next_capital_pressure_at(moment),
                total_charged_jpy=_money(self.capital_pressure_total()),
                charged_count=len(
                    self.storage.read_cash_flows(flow_type=FlowType.CAPITAL_DECAY)
                ),
                cash_balance_after=_money(self.cash_balance()),
            )

        if not pressure.enabled:
            return _result(False, Decimal("0"), (), "督促机制已关闭")
        if pressure.deduction_jpy <= 0:
            return _result(False, Decimal("0"), (), "扣减金额为 0")

        # dry-run 不得产生任何副作用：不写起算点，只用一个「虚拟起算点」预演。
        anchor = self._capital_pressure_anchor(moment, create=not dry_run)
        due = capital_pressure_slots_between(
            anchor, moment, tz=self.cfg.tz, interval_hours=pressure.interval_hours
        )
        if not due:
            return _result(False, Decimal("0"), (), "未到下一个扣款槽位")
        if not pressure.catch_up_missed_slots and len(due) > 1:
            due = due[-1:]

        if dry_run:
            available = max(self.cash_balance(), Decimal("0"))
            planned = min(pressure.deduction_jpy * len(due), available)
            return _result(
                False, planned, [s.strftime("%Y-%m-%dT%H") for s in due],
                "dry-run：仅预演，未扣款",
            )

        charged = Decimal("0")
        charged_slots: list[str] = []
        out_of_cash = False
        for slot in due:
            available = max(self.cash_balance(), Decimal("0"))
            amount = min(pressure.deduction_jpy, available)
            if amount <= 0:
                out_of_cash = True
                break
            try:
                self.storage.record_cash_flow(
                    flow_type=FlowType.CAPITAL_DECAY,
                    amount=-amount,
                    event_key=f"{CAPITAL_DECAY_EVENT_PREFIX}{slot.strftime('%Y-%m-%dT%H')}",
                    note=(
                        f"督促机制：每 {pressure.interval_hours} 小时扣本金 "
                        f"{pressure.deduction_jpy} 日元"
                        f"（槽位 {slot.strftime('%Y-%m-%d %H:%M')} JST）"
                    ),
                    occurred_at=slot,
                    on_duplicate="ERROR",
                )
            except DuplicateEventError:
                # 该槽位此前已经扣过（重复调用 / 重启后重放）。跳过，不计入本次扣款。
                continue
            charged += amount
            charged_slots.append(slot.strftime("%Y-%m-%dT%H"))

        if not charged_slots:
            # 区分「没钱可扣」和「这些槽位此前已经扣过」——两种情况含义完全不同。
            reason = "现金余额为 0，无可扣金额" if out_of_cash else "到期槽位此前均已扣过"
            return _result(False, Decimal("0"), (), reason)

        logger.warning(
            "督促机制扣款：%d 个槽位共 %s 日元（累计 %s）",
            len(charged_slots), charged, self.capital_pressure_total(),
        )
        if quote is not None:
            # 有行情就顺手盯市一次，让状态/净值立即反映扣款结果。
            self._refresh_state(quote, occurred_at=moment)
        return _result(True, charged, charged_slots, None)

    def _capital_pressure_anchor(self, moment: datetime, *, create: bool = True) -> datetime:
        """读取（首次则写入）督促机制的起算点。

        ``create=False`` 时**不写库**，直接返回 ``moment`` 当作虚拟起算点
        —— ``dry_run`` 走这条路，保证预演不改变任何状态。

        起算点**不追溯**：对一个早已存在的账户启用本机制时，从启用那一刻起算，
        而不是从开户时刻起算——否则会立刻补扣过去几天的账，属于意外扣款。
        """
        key = CAPITAL_PRESSURE_ANCHOR_META_KEY
        raw = self.storage.get_meta(key)
        if raw:
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:  # pragma: no cover - 手工改坏了就重设
                parsed = None
            if parsed is not None:
                return parsed
        if create:
            self.storage.set_meta(key, moment.isoformat(timespec="seconds"))
        return moment

    # -- 目标 + 奖励：给「贪婪」一个正向出口 --------------------------------- #

    def _read_goal_state(self) -> dict[str, Any] | None:
        raw = self.storage.get_meta(GOAL_STATE_META_KEY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):  # pragma: no cover - 手工改坏了就重设
            return None
        return data if isinstance(data, dict) else None

    def _write_goal_state(self, state: Mapping[str, Any]) -> None:
        self.storage.set_meta(
            GOAL_STATE_META_KEY, json.dumps(dict(state), ensure_ascii=False)
        )

    def goal_total_reward(self) -> Decimal:
        """开户以来累计发放的目标奖励（正数）。"""
        flows = self.storage.read_cash_flows(flow_type=FlowType.GOAL_REWARD)
        return sum((flow.amount for flow in flows), Decimal("0"))

    def goal_reward_count(self) -> int:
        return len(self.storage.read_cash_flows(flow_type=FlowType.GOAL_REWARD))

    def goal_state(self) -> dict[str, Any] | None:
        """当前目标状态（档位 / 目标值 / 起算时刻）。"""
        return self._read_goal_state()

    def _goal_reward_for(self, level: int) -> Decimal:
        """第 ``level`` 档的奖励金额（按 ``reward_growth_ratio`` 递增）。"""
        goal = self.cfg.goal
        factor = (Decimal("1") + goal.reward_growth_ratio) ** max(0, int(level) - 1)
        return _money(goal.reward_jpy * factor)

    def goal_snapshot(
        self, equity: Decimal | int | str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """给 AI 看的当前进度（只读，不推进任何状态）。"""
        goal = self.cfg.goal
        moment = now or datetime.now(self.cfg.tz)
        current = _dec(equity, "equity")
        state = self._read_goal_state()
        if not goal.enabled or state is None:
            return {"enabled": goal.enabled}
        target = _dec(state.get("target_jpy"), "target_jpy")
        level = int(state.get("level", 1))
        try:
            started_at = (
                datetime.fromisoformat(str(state["started_at"]))
                if state.get("started_at") else None
            )
        except (KeyError, ValueError):  # pragma: no cover
            started_at = None
        elapsed = (
            None if started_at is None
            else Decimal(str(round((moment - started_at).total_seconds() / 3600.0, 1)))
        )
        return {
            "enabled": True,
            "level": level,
            "target_jpy": format(target, "f"),
            "equity_jpy": format(current, "f"),
            "gap_jpy": format(target - current, "f"),
            "progress_ratio": (
                None if target <= 0
                else format(
                    (current / target).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
                    "f",
                )
            ),
            "reached": current >= target,
            "started_at": (
                started_at.isoformat(timespec="minutes") if started_at else None
            ),
            "elapsed_hours": None if elapsed is None else format(elapsed, "f"),
            "reward_on_success_jpy": format(self._goal_reward_for(level), "f"),
            "achieved_count": self.goal_reward_count(),
            "total_reward_jpy": format(self.goal_total_reward(), "f"),
        }

    def apply_goal_reward(
        self,
        *,
        equity: Decimal | int | str | None = None,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> GoalRewardResult:
        """结算「目标 + 奖励」。

        规则
        ----
        * 目标是**净值**指标：净值 ≥ 目标即达成。
        * 首次结算时按**当时的净值**建立第一档目标（``×(1+target_growth_ratio)``），
          所以目标永远是从「现在」出发的一个可达增量，而不是一个陈旧的绝对值。
        * 达成后：发奖励 → 档位 +1 → 新目标 = **(达成时净值 + 奖励) × (1+增长比例)**。
        * 速度激励是内生的：目标只有在达成后才抬高，慢了只是**推迟**下一笔奖励，
          **没有任何惩罚**。这一点与督促机制（按时间扣钱）是相反的。

        为什么新目标要以「达成时净值」为基准，而不是旧目标
        --------------------------------------------------
        如果按旧目标递推（例如每次 ×1.2），奖励金本身会把净值推高，
        可能**一笔奖励就够跨过下一档**，于是奖励可以连锁刷取——那就是印钱。
        以「达成时净值（含奖励）」为基准，新目标永远在当前位置之上固定比例，
        奖励无法自我兑现。这是这个机制里最容易写错的地方。
        """
        goal = self.cfg.goal
        moment = now or datetime.now(self.cfg.tz)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.cfg.tz)

        if equity is None:
            current = _money(
                self.cash_balance() + self.storage.read_account_state().unrealized_pnl
            )
        else:
            current = _dec(equity, "equity")

        state = self._read_goal_state()
        if not goal.enabled:
            return GoalRewardResult(
                enabled=False, achieved=False, level=1, target_jpy=current,
                equity_jpy=current, progress_ratio=None,
                reward_paid_jpy=Decimal("0"), next_target_jpy=None,
                next_reward_jpy=None, elapsed_hours=None,
                total_reward_jpy=self.goal_total_reward(),
                achieved_count=self.goal_reward_count(),
                skipped_reason="目标机制已关闭",
            )

        if state is None:
            target = _money(current * (Decimal("1") + goal.target_growth_ratio))
            if dry_run:
                return GoalRewardResult(
                    enabled=True, achieved=False, level=1, target_jpy=target,
                    equity_jpy=current,
                    progress_ratio=(None if target <= 0 else current / target),
                    reward_paid_jpy=Decimal("0"), next_target_jpy=target,
                    next_reward_jpy=self._goal_reward_for(1),
                    elapsed_hours=Decimal("0"),
                    total_reward_jpy=self.goal_total_reward(), achieved_count=0,
                    skipped_reason="dry-run：将建立第 1 档目标",
                )
            self._write_goal_state({
                "level": 1,
                "target_jpy": format(target, "f"),
                "started_at": moment.isoformat(timespec="seconds"),
            })
            self.storage.audit(
                "GOAL_SET",
                {"level": 1, "target_jpy": format(target, "f")},
                occurred_at=moment,
            )
            return GoalRewardResult(
                enabled=True, achieved=False, level=1, target_jpy=target,
                equity_jpy=current,
                progress_ratio=(None if target <= 0 else current / target),
                reward_paid_jpy=Decimal("0"), next_target_jpy=target,
                next_reward_jpy=self._goal_reward_for(1), elapsed_hours=Decimal("0"),
                total_reward_jpy=self.goal_total_reward(), achieved_count=0,
                skipped_reason="已建立第 1 档目标",
            )

        level = int(state.get("level", 1))
        target = _dec(state.get("target_jpy"), "target_jpy")
        try:
            started_at = (
                datetime.fromisoformat(str(state["started_at"]))
                if state.get("started_at") else moment
            )
        except (KeyError, ValueError):  # pragma: no cover
            started_at = moment
        elapsed = Decimal(str(round((moment - started_at).total_seconds() / 3600.0, 3)))

        if current < target:
            return GoalRewardResult(
                enabled=True, achieved=False, level=level, target_jpy=target,
                equity_jpy=current,
                progress_ratio=(None if target <= 0 else current / target),
                reward_paid_jpy=Decimal("0"), next_target_jpy=target,
                next_reward_jpy=self._goal_reward_for(level), elapsed_hours=elapsed,
                total_reward_jpy=self.goal_total_reward(),
                achieved_count=self.goal_reward_count(), skipped_reason=None,
            )

        reward = self._goal_reward_for(level)
        next_target = _money(
            (current + reward) * (Decimal("1") + goal.target_growth_ratio)
        )
        if dry_run:
            return GoalRewardResult(
                enabled=True, achieved=True, level=level, target_jpy=target,
                equity_jpy=current,
                progress_ratio=(None if target <= 0 else current / target),
                reward_paid_jpy=reward, next_target_jpy=next_target,
                next_reward_jpy=self._goal_reward_for(level + 1),
                elapsed_hours=elapsed, total_reward_jpy=self.goal_total_reward(),
                achieved_count=self.goal_reward_count(),
                skipped_reason="dry-run：仅预演，未发奖励",
            )

        if reward > 0:
            self.storage.record_cash_flow(
                flow_type=FlowType.GOAL_REWARD,
                amount=reward,
                event_key=f"{GOAL_REWARD_EVENT_PREFIX}L{level}:{format(target, 'f')}",
                note=(
                    f"达成第 {level} 档目标（净值 ≥ {format(target, 'f')}），"
                    f"耗时 {format(elapsed, 'f')} 小时，"
                    f"发放奖励 {format(reward, 'f')} 日元"
                ),
                occurred_at=moment,
            )
        self._write_goal_state({
            "level": level + 1,
            "target_jpy": format(next_target, "f"),
            "started_at": moment.isoformat(timespec="seconds"),
            "last_achieved_level": level,
            "last_elapsed_hours": format(elapsed, "f"),
        })
        self.storage.audit(
            "GOAL_ACHIEVED",
            {
                "level": level,
                "target_jpy": format(target, "f"),
                "equity_jpy": format(current, "f"),
                "reward_jpy": format(reward, "f"),
                "elapsed_hours": format(elapsed, "f"),
                "next_target_jpy": format(next_target, "f"),
            },
            occurred_at=moment,
        )
        logger.warning(
            "达成第 %d 档目标（净值 %s ≥ %s），奖励 %s 日元，耗时 %s 小时；下一档目标 %s",
            level, current, target, reward, elapsed, next_target,
        )
        return GoalRewardResult(
            enabled=True, achieved=True, level=level, target_jpy=target,
            equity_jpy=current,
            progress_ratio=(None if target <= 0 else current / target),
            reward_paid_jpy=reward, next_target_jpy=next_target,
            next_reward_jpy=self._goal_reward_for(level + 1), elapsed_hours=elapsed,
            total_reward_jpy=self.goal_total_reward(),
            achieved_count=self.goal_reward_count(), skipped_reason=None,
        )

    def mark_to_market(
        self, quote: Quote, *, occurred_at: datetime | None = None
    ) -> MarkToMarket:
        """盯市：重算浮盈、净值、保证金水平并持久化。不产生任何现金流。"""
        positions = self.open_positions()
        state = self._refresh_state(quote, occurred_at=occurred_at)
        cash = self.cash_balance()
        day_start = state.day_start_equity
        # 与 _evaluate_halts 同口径：回撤剔除督促扣款，否则一次扣款就会被误报成回撤。
        basis = self.drawdown_basis_equity(state.equity, occurred_at)
        drawdown = (
            (day_start - basis) / day_start if day_start > 0 else Decimal("0")
        )
        margin_level = calculate_margin_level(state.equity, state.margin_used)
        warnings: list[str] = []
        if margin_level is not None:
            if margin_level <= self.cfg.margin.stop_out_level:
                warnings.append(
                    f"保证金水平 {margin_level:.4f} 不高于强平线 "
                    f"{self.cfg.margin.stop_out_level}"
                )
            elif margin_level <= self.cfg.margin.margin_call_level:
                warnings.append(
                    f"保证金水平 {margin_level:.4f} 不高于预警线 "
                    f"{self.cfg.margin.margin_call_level}"
                )
        if state.halted:
            warnings.append(f"账户已停止开仓：{state.halt_reason}")
        return MarkToMarket(
            as_of=state.as_of,
            cash_balance=cash,
            unrealized_pnl=state.unrealized_pnl,
            equity=state.equity,
            used_margin=state.margin_used,
            margin_level=margin_level,
            peak_equity=state.peak_equity,
            day_start_equity=day_start,
            daily_drawdown_ratio=drawdown,
            status=state.status,
            halted=state.halted,
            halt_reason=state.halt_reason,
            warnings=tuple(warnings),
            state=state,
        )

    def process_quote(
        self, quote: Quote, *, occurred_at: datetime | None = None
    ) -> ProcessResult:
        """处理一次行情：盯市 → 自动止损/止盈 → 强平/爆仓/回撤停止。

        自动平仓的 ``reason`` 会如实写入持仓与成交记录（规则 10）。
        """
        mark = self.mark_to_market(quote, occurred_at=occurred_at)
        triggers: list[ExitTrigger] = []
        closed: list[CloseExecution] = []

        # 1) 价格触发的止损/止盈（原因最具体，优先判定）
        for position in self.open_positions():
            trigger = self.evaluate_exit_triggers(position, quote)
            if trigger is None:
                continue
            triggers.append(trigger)
            closed.append(
                self.close_position(
                    position.position_id, quote, reason=trigger.reason, occurred_at=occurred_at
                )
            )

        # 2) 账户级强制平仓：爆仓 → 强平 → 当日回撤停止
        for position in self.open_positions():
            state = self.storage.read_account_state()
            level = calculate_margin_level(state.equity, state.margin_used)
            forced: CloseReason | None = None
            if state.equity <= self.cfg.margin.bankruptcy_equity_jpy:
                forced = CloseReason.BANKRUPTCY
            elif level is not None and level <= self.cfg.margin.stop_out_level:
                forced = CloseReason.MARGIN_CALL
            elif state.halted and "回撤" in (state.halt_reason or ""):
                forced = CloseReason.DAILY_DRAWDOWN
            if forced is None:
                continue
            trigger = ExitTrigger(
                position_id=position.position_id,
                reason=forced,
                trigger_price=state.equity,
                fill_price=self._fill_price(PositionSide(position.side), quote, opening=False),
                note=f"{forced.value}：净值 {state.equity}，保证金水平 {level}",
            )
            triggers.append(trigger)
            closed.append(
                self.close_position(
                    position.position_id, quote, reason=forced, occurred_at=occurred_at
                )
            )

        if closed:
            mark = self.mark_to_market(quote, occurred_at=occurred_at)
        return ProcessResult(mark=mark, closed=tuple(closed), triggers=tuple(triggers))

    # -- 独立费用 ---------------------------------------------------------- #

    def record_standalone_fee(
        self,
        amount_jpy: Decimal | int | str,
        *,
        fee_id: str,
        position_id: int | None = None,
        note: str | None = None,
        occurred_at: datetime | None = None,
    ) -> CashFlow:
        """记一笔独立扣款费用（佣金等）。

        ``fee_id`` 是幂等键：同一 ``fee_id`` 重复提交只会扣一次。
        费用以负值记入账本（``FEE`` 流水），从而出现在现金余额里。
        """
        amount = _dec(amount_jpy, "amount_jpy")
        if amount == 0:
            raise EngineError("费用金额不能为 0")
        if amount > 0:
            amount = -amount  # 费用一律为扣减
        flow = self.storage.record_cash_flow(
            flow_type=FlowType.FEE,
            amount=amount,
            event_key=f"FEE:{fee_id}",
            note=note or f"独立手续费 {fee_id}",
            occurred_at=occurred_at,
            position_id=position_id,
        )
        self.storage.audit(
            "STANDALONE_FEE_RECORDED",
            {"fee_id": fee_id, "amount": amount, "flow_no": flow.flow_no},
            occurred_at=occurred_at,
        )
        return flow

    def record_swap_fee(
        self,
        amount_jpy: Decimal | int | str,
        *,
        swap_id: str,
        position_id: int | None = None,
        verified: bool = False,
        note: str | None = None,
        occurred_at: datetime | None = None,
    ) -> CashFlow:
        """记一笔掉期（隔夜利息）费用。

        只接受**已核验**的掉期（``verified=True``），否则拒绝 —— 避免把未经确认
        的数字扣进账本。``swap_id`` 保证同一笔掉期只扣一次。
        """
        if not verified:
            raise EngineError("掉期费用必须显式核验（verified=True）后才能记账")
        amount = _dec(amount_jpy, "amount_jpy")
        if amount == 0:
            raise EngineError("掉期金额不能为 0")
        flow = self.storage.record_cash_flow(
            flow_type=FlowType.SWAP,
            amount=amount,
            event_key=f"SWAP:{swap_id}",
            note=note or f"已核验掉期 {swap_id}",
            occurred_at=occurred_at,
            position_id=position_id,
        )
        self.storage.audit(
            "SWAP_FEE_RECORDED",
            {"swap_id": swap_id, "amount": amount, "flow_no": flow.flow_no},
            occurred_at=occurred_at,
        )
        return flow

    # -- 核对 -------------------------------------------------------------- #

    def reconcile_account(
        self, *, quote: Quote | None = None, strict: bool = False
    ) -> EngineReconciliation:
        """引擎级账户核对。

        在 :meth:`storage.Storage.reconcile_account` 的基础上，额外交叉检查：

        * **账本口径**：按规则 ``初始本金 + 入金 − 出金 + 已实现盈亏 − 费用``
          重新推导现金余额，与账本汇总值比对（能发现流水符号写反之类的问题）；
        * **保证金投影**：``account_state.margin_used`` 与未平仓持仓保证金之和比对
          （能发现「持仓已建但保证金未更新」这类跨事务中断）；
        * **净值**：``account_state.equity`` 与 ``现金 + 浮动盈亏`` 比对；
        * **规则 8**：未平仓持仓不得超过 1 笔，且不得同时持有多空。
        """
        storage_report = self.storage.reconcile_account(include_balance_chain=True)
        problems: list[str] = list(storage_report.problems)

        positions = self.open_positions()
        flows = self.storage.read_cash_flows()
        totals: dict[str, Decimal] = {member.value: Decimal("0") for member in FlowType}
        for flow in flows:
            totals[flow.flow_type] = totals.get(flow.flow_type, Decimal("0")) + flow.amount

        # 账本口径：按规则的正负号约定重新推导。
        # 注意「初始本金」本身就是一条 DEPOSIT 流水（event_key=INITIAL_CAPITAL），
        # 必须把它从「入金」里剔除，否则本金会被计两次。
        extra_deposits = sum(
            (
                flow.amount
                for flow in flows
                if flow.flow_type == FlowType.DEPOSIT.value
                and flow.event_key != INITIAL_CAPITAL_EVENT_KEY
            ),
            Decimal("0"),
        )
        cash_from_rule = _money(
            self.cfg.account.initial_capital_jpy
            + extra_deposits
            - abs(totals.get(FlowType.WITHDRAWAL.value, Decimal("0")))
            + totals.get(FlowType.REALIZED_PNL.value, Decimal("0"))
            - abs(totals.get(FlowType.FEE.value, Decimal("0")))
            - abs(totals.get(FlowType.SWAP.value, Decimal("0")))
            - abs(totals.get(FlowType.CAPITAL_DECAY.value, Decimal("0")))
            + totals.get(FlowType.GOAL_REWARD.value, Decimal("0"))
            + totals.get(FlowType.ADJUSTMENT.value, Decimal("0"))
        )
        ledger_balance = self.cash_balance()
        cash_diff = cash_from_rule - ledger_balance
        if cash_diff != 0:
            problems.append(
                f"账本口径不一致：按规则推导 {cash_from_rule}，账本汇总 {ledger_balance}"
                f"（差额 {cash_diff}）"
            )

        state = self.storage.read_account_state()
        margin_from_positions = calculate_used_margin(positions)
        margin_diff = state.margin_used - margin_from_positions
        if margin_diff != 0:
            problems.append(
                f"已用保证金投影不一致：account_state={state.margin_used}，"
                f"未平仓持仓之和={margin_from_positions}（差额 {margin_diff}）"
            )

        if quote is not None:
            unrealized = sum(
                (calculate_unrealized_pnl(p, quote) for p in positions), Decimal("0")
            )
        else:
            unrealized = state.unrealized_pnl
        equity_recomputed = calculate_equity(ledger_balance, unrealized)
        equity_diff = state.equity - equity_recomputed
        if equity_diff != 0:
            problems.append(
                f"净值不一致：account_state={state.equity}，"
                f"现金 {ledger_balance} + 浮动盈亏 {unrealized} = {equity_recomputed}"
                f"（差额 {equity_diff}）"
            )

        if len(positions) > 1:
            problems.append(
                f"同时存在 {len(positions)} 笔未平仓持仓，违反「同一方向最多一笔」规则"
            )
        sides = {p.side for p in positions}
        if len(sides) > 1:
            problems.append(f"同时持有多空两个方向（{sorted(sides)}），属于禁止的对冲仓位")

        report = EngineReconciliation(
            ok=not problems,
            checked_at=datetime.now(self.cfg.tz),
            problems=tuple(problems),
            storage_report=storage_report,
            ledger_balance=ledger_balance,
            cash_from_ledger_rule=cash_from_rule,
            cash_difference=cash_diff,
            margin_used_stored=state.margin_used,
            margin_used_from_positions=margin_from_positions,
            margin_difference=margin_diff,
            equity_stored=state.equity,
            equity_recomputed=equity_recomputed,
            equity_difference=equity_diff,
            open_position_count=len(positions),
            flow_totals=totals,
        )
        if strict and not report.ok:
            raise EngineError("账户核对失败：" + "；".join(report.problems))
        return report

    # -- 投影修复 ---------------------------------------------------------- #

    def repair_projections(
        self, quote: Quote | None = None, *, reason: str = "manual_repair"
    ) -> AccountState:
        """在核对发现投影不一致后，按账本与持仓重算投影并落库。

        这是一次**显式且留痕**的修复：只重算缓存投影（保证金、浮盈、净值），
        绝不修改现金余额（现金只能由账本决定），并写入审计事件。
        """
        positions = self.open_positions()
        used = calculate_used_margin(positions)
        cash = self.cash_balance()
        if quote is not None:
            unrealized = sum(
                (calculate_unrealized_pnl(p, quote) for p in positions), Decimal("0")
            )
        else:
            unrealized = self.storage.read_account_state().unrealized_pnl
        state = self.storage.read_account_state()
        before = {
            "margin_used": state.margin_used,
            "unrealized_pnl": state.unrealized_pnl,
            "equity": state.equity,
        }
        repaired = self.storage.save_account_state(
            dataclasses.replace(
                state,
                cash_balance=cash,
                margin_used=used,
                unrealized_pnl=unrealized,
                equity=calculate_equity(cash, unrealized),
                as_of=datetime.now(self.cfg.tz),
            ),
            expected_revision=state.revision,
        )
        self.storage.audit(
            "PROJECTION_REPAIRED",
            {
                "reason": reason,
                "before": before,
                "after": {
                    "margin_used": repaired.margin_used,
                    "unrealized_pnl": repaired.unrealized_pnl,
                    "equity": repaired.equity,
                },
            },
        )
        return repaired

    # -- 便捷查询 ---------------------------------------------------------- #

    def snapshot(self, quote: Quote | None = None) -> dict[str, Any]:
        """账户快照，便于日志/报告（全部由计算得出，不含调用方提供的数字）。"""
        state = self.storage.read_account_state()
        positions = self.open_positions()
        used = calculate_used_margin(positions)
        unrealized = (
            sum((calculate_unrealized_pnl(p, quote) for p in positions), Decimal("0"))
            if quote is not None
            else state.unrealized_pnl
        )
        cash = self.cash_balance()
        equity = calculate_equity(cash, unrealized)
        return {
            "symbol": self.cfg.instrument.symbol,
            "cash_balance": str(cash),
            "unrealized_pnl": str(unrealized),
            "equity": str(equity),
            "used_margin": str(used),
            "margin_level": str(calculate_margin_level(equity, used)),
            "status": state.status,
            "halted": state.halted,
            "halt_reason": state.halt_reason,
            "open_positions": [p.as_dict() for p in positions],
        }


def open_engine(cfg: Config | None = None, **kwargs: Any) -> TradingEngine:
    """按配置创建引擎（使用配置指定的数据库路径）。"""
    return TradingEngine.from_config(cfg, **kwargs)


# --------------------------------------------------------------------------- #
# 命令行演示
# --------------------------------------------------------------------------- #


def _demo_quote(bid: str, ask: str, cfg: Config) -> Quote:
    from market_data import Granularity, QuoteType

    return Quote(
        symbol=cfg.instrument.symbol,
        bid=Decimal(bid),
        ask=Decimal(ask),
        mid=(Decimal(bid) + Decimal(ask)) / 2,
        timestamp=datetime.now(cfg.tz),
        source="engine_demo",
        source_url="https://example.invalid/demo",
        quote_type=QuoteType.BID_ASK,
        granularity=Granularity.TICK,
        is_stale=False,
    )


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m engine",
        description="在临时数据库上演示多仓与空仓的完整计算过程（不触碰真实账户）。",
    )
    parser.add_argument("--volume", default="1000", help="美元持仓数量（默认 1000）")
    args = parser.parse_args(argv)

    cfg = load_config()
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        engine = TradingEngine.from_config(cfg, db_path=str(Path(tmp) / "demo.sqlite3"))
        engine.ensure_initialized()
        volume = Decimal(args.volume)
        entry_bid, entry_ask = Decimal("158.000"), Decimal("158.010")

        for side in (PositionSide.LONG, PositionSide.SHORT):
            print(f"\n{'=' * 68}\n=== {side.value} 演示 ===\n{'=' * 68}")
            quote = _demo_quote(str(entry_bid), str(entry_ask), cfg)
            if side is PositionSide.LONG:
                stop, target = Decimal("157.500"), Decimal("158.775")
            else:
                stop, target = Decimal("158.510"), Decimal("157.235")
            intent = TradeIntent(
                symbol=cfg.instrument.symbol, side=side, volume_usd=volume,
                stop_loss=stop, take_profit=target, intent_id=f"demo-{side.value}",
            )
            execution = engine.execute_virtual_trade(intent, quote)
            v = execution.validation
            assert v is not None
            print(f"开仓成交价        : {v.entry_price}  "
                  f"({'Ask' if side is PositionSide.LONG else 'Bid'})")
            print(f"名义金额          : {v.notional_jpy} 日元")
            print(f"所需保证金        : {v.required_margin_jpy} 日元 "
                  f"(名义/{cfg.risk.max_leverage})")
            print(f"止损距离          : {v.stop_distance_jpy} + 滑点缓冲 "
                  f"{v.slippage_buffer_jpy} = {v.risk_per_unit_jpy} /单位")
            print(f"计划止损风险      : {v.planned_risk_jpy} 日元 "
                  f"(上限 {v.risk_limit_jpy})")
            print(f"计划盈亏比        : 1:{v.reward_risk_ratio}")
            print(f"开仓后净值        : {v.equity_at_entry} "
                  f"(点差成本 {v.spread_cost_jpy})")

            if side is PositionSide.LONG:
                exit_bid, exit_ask = Decimal("158.500"), Decimal("158.510")
            else:
                exit_bid, exit_ask = Decimal("157.500"), Decimal("157.510")
            exit_quote = _demo_quote(str(exit_bid), str(exit_ask), cfg)
            mark = engine.mark_to_market(exit_quote)
            print(f"盯市              : 浮动盈亏 {mark.unrealized_pnl}，净值 {mark.equity}")
            result = engine.close_position(
                execution.position.position_id, exit_quote,
                reason=CloseReason.MANUAL,
            )
            print(f"平仓成交价        : {result.trade.price}  "
                  f"({'Bid' if side is PositionSide.LONG else 'Ask'})")
            print(f"已实现盈亏        : {result.realized_pnl_jpy}")
            print(f"平仓后现金余额    : {result.cash_balance_after}")
            print(f"平仓后净值        : {result.equity_after}")

        report = engine.reconcile_account(strict=False)
        print(f"\n{'=' * 68}\n账户核对: {'通过' if report.ok else '不一致'}")
        print(f"  账本余额 {report.ledger_balance} | 规则推导 {report.cash_from_ledger_rule}")
        print(f"  未平仓 {report.open_position_count} 笔 | 保证金投影差额 "
              f"{report.margin_difference}")
        for problem in report.problems:
            print(f"  [问题] {problem}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
