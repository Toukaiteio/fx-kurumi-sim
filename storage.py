"""FX 战士久留美 —— 虚拟账户持久化模块（SQLite）。

定位
====

**本模块是账户状态的唯一权威来源。** 其他模块不得绕过本模块直接读写数据库。

* 数据库位置：``data/fxsim.sqlite3``（由 ``config.json`` 的
  ``paths.data_dir`` + ``paths.database_filename`` 决定，见 :meth:`Storage.from_config`）。
* 金额与价格一律以**十进制字符串**（``TEXT``）持久化，绝不使用 SQLite 的
  REAL 列，也不在 SQL 里做 ``SUM()`` —— 所有算术都在 Python 侧用
  :class:`decimal.Decimal` 完成，避免二进制浮点误差。
* 时间戳统一以 ``Asia/Tokyo`` 的 ISO-8601 字符串存储（``2026-10-10T15:11:32+09:00``）。
  日本标准时间全年固定 +09:00，因此字符串排序等价于时间排序。
* 关键操作（开仓、平仓、记账、状态更新）都在**单个事务**内完成，不会只更新一部分。

数据表
======

======================== ==================================================
``meta``                 元信息（schema 版本、审计头锚点等）
``account``              账户身份与初始本金（不可变，只创建一次）
``account_state``        账户状态快照（现金、净值、保证金、修订号）
``positions``            当前持仓（含已平仓历史）
``trades``               交易历史（每次成交一条，追加式）
``cash_flows``           入金/出金/已实现盈亏/手续费流水（追加式账本）
``quotes``               报价记录（追加式）
``audit_events``         审计事件（追加式 + 哈希链）
``daily_equity_baseline``每日净值基准（按交易日，每日只创建一次）
``scheduler_runs``       定时任务运行记录
======================== ==================================================

账本模型
========

现金余额**完全由 ``cash_flows`` 账本决定**：``cash_balance == SUM(amount)``。
``account_state.cash_balance`` 只是账本的缓存投影，任何写账本的操作都会在
同一个事务里重算投影；:meth:`Storage.save_account_state` 也会先校验投影与
账本一致，否则拒绝写入。:meth:`Storage.reconcile_account` 用于独立复核。

审计哈希链的能力边界
====================

每条 ``audit_events`` 记录包含前一条的 ``hash``，因此**单条篡改、删除、
重排**都会被 :meth:`Storage.verify_audit_chain` 发现。但若攻击者有能力
重写整条链（重新计算所有后续哈希），纯数据库内的校验无法发现 —— 必须把
:meth:`Storage.audit_head` 的返回值**锚定到数据库之外**（例如单独提交到
版本库、写入只追加的外部日志）才能防住整链重写。这一点无法靠本模块单独解决。

安全边界
========

本项目只做虚拟交易模拟。本模块只读写本地 SQLite 文件，不联网、不连接券商、
不下单、不做任何真实资金转账。``cash_flows`` 中的入金/出金仅是模拟记账。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Final, Iterator, Mapping, Sequence

from config import Config, load_config

__all__ = [
    "AccountState",
    "AccountStatus",
    "AgentNote",
    "AgentTurn",
    "AuditEvent",
    "AuditVerification",
    "CashFlow",
    "ConcurrencyError",
    "DailyBaseline",
    "DuplicateEventError",
    "FlowType",
    "HourSummary",
    "IntegrityReport",
    "LedgerMismatchError",
    "LedgerVerification",
    "Position",
    "PositionSide",
    "PositionStatus",
    "QuoteRecord",
    "ReconciliationReport",
    "RunStatus",
    "SchedulerRun",
    "SchemaVersionError",
    "Storage",
    "StorageError",
    "TradeAction",
    "TradeRecord",
    "AccountAlreadyInitializedError",
    "MAX_NOTE_CHARS",
    "open_storage",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 数据库 schema 版本。**破坏性**改动（改列、改语义）时必须递增，旧库会明确报错
#: 而不是被就地改写。纯**新增**表不算破坏性：旧库下次打开时会自动建出空表，
#: 既有数据一行都不会动，因此不需要递增（`agent_notes` 就是这样加进来的）。
SCHEMA_VERSION: Final[int] = 1

#: 单条 AI 笔记的最大字符数。笔记会被回灌进模型上下文，必须有硬上限，
#: 否则模型可以靠反复写长文把自己的上下文撑爆。
MAX_NOTE_CHARS: Final[int] = 2000

#: 审计哈希链的创世前驱哈希。
GENESIS_HASH: Final[str] = "0" * 64

#: 账户表固定只有一行。
_ACCOUNT_ID: Final[int] = 1

#: 初始本金的流水事件键；UNIQUE 约束保证「只发放一次」。
INITIAL_CAPITAL_EVENT_KEY: Final[str] = "INITIAL_CAPITAL"

#: 日本标准时间没有夏令时，固定 +09:00 与 Asia/Tokyo 等价。
_JST: Final[tzinfo] = timezone(timedelta(hours=9), "Asia/Tokyo")

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class StorageError(RuntimeError):
    """持久化层所有异常的基类。"""


class SchemaVersionError(StorageError):
    """数据库 schema 版本与本模块不匹配（旧库不会被就地覆盖）。"""


class ConcurrencyError(StorageError):
    """乐观并发冲突：调用方持有的修订号已过期，拒绝写入以免覆盖他人数据。"""


class LedgerMismatchError(StorageError):
    """传入的现金余额与账本流水不一致，拒绝写入。"""


class ReconciliationError(StorageError):
    """账户核对失败（``reconcile_account(strict=True)`` 时抛出）。"""


class AccountAlreadyInitializedError(StorageError):
    """账户已初始化且参数与新请求冲突；不会重复发放本金或改写本金。"""


class DuplicateEventError(StorageError):
    """同一 ``event_key`` 的事件已存在，且调用方要求重复即报错。"""


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    HALTED = "HALTED"
    BANKRUPT = "BANKRUPT"
    CLOSED = "CLOSED"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class TradeAction(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"


class FlowType(str, Enum):
    """资金流水类型。``DEPOSIT`` / ``WITHDRAWAL`` 为外部出入金，其余为账户内部变动。"""

    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    REALIZED_PNL = "REALIZED_PNL"
    FEE = "FEE"
    SWAP = "SWAP"
    ADJUSTMENT = "ADJUSTMENT"
    #: 督促机制的本金扣减（游戏机制，不是交易成本）。见 :class:`config.PressureConfig`。
    CAPITAL_DECAY = "CAPITAL_DECAY"
    #: 达成目标发放的奖励金（外部注入，**不计入交易盈亏**）。
    #: 见 :class:`config.GoalConfig` 与 :meth:`engine.TradingEngine.apply_goal_reward`。
    GOAL_REWARD = "GOAL_REWARD"


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class OnDuplicate(str, Enum):
    """``event_key`` 重复时的处理策略。"""

    IGNORE = "IGNORE"  # 幂等：返回已存在的记录
    ERROR = "ERROR"  # 抛 DuplicateEventError


# --------------------------------------------------------------------------- #
# 值转换：Decimal / 时间 / 枚举
# --------------------------------------------------------------------------- #


def _as_decimal(value: Any, field_name: str = "value") -> Decimal:
    """把入参转成有限的 Decimal；不接受 bool 与 NaN/Inf。"""
    if isinstance(value, bool):
        raise StorageError(f"{field_name} 不能是布尔值: {value!r}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise StorageError(f"{field_name} 不能是空字符串")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise StorageError(f"{field_name} 不是合法十进制数字: {value!r}") from exc
    elif isinstance(value, float):
        result = Decimal(repr(value))
    else:
        raise StorageError(f"{field_name} 必须是 Decimal/int/str，收到 {type(value).__name__}")
    if not result.is_finite():
        raise StorageError(f"{field_name} 必须是有限数字: {value!r}")
    return result


def _dec_text(value: Any, field_name: str = "value") -> str:
    """转成用于持久化的十进制字符串（绝不用科学计数法）。"""
    return format(_as_decimal(value, field_name), "f")


def _opt_dec_text(value: Any | None, field_name: str = "value") -> str | None:
    return None if value is None else _dec_text(value, field_name)


def _opt_dec(value: Any | None, field_name: str = "value") -> Decimal | None:
    return None if value is None else _as_decimal(value, field_name)


def _row_dec(row: Mapping[str, Any], key: str) -> Decimal:
    return _as_decimal(row[key], key)


def _row_opt_dec(row: Mapping[str, Any], key: str) -> Decimal | None:
    return None if row[key] is None else _as_decimal(row[key], key)


def _iso(moment: datetime | None, tz: tzinfo, field_name: str = "时间") -> str:
    if moment is None:
        moment = datetime.now(tz)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(tz).isoformat(timespec="seconds")


def _parse_iso(text: str | None, field_name: str = "时间") -> datetime | None:
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise StorageError(f"{field_name} 不是合法 ISO 时间: {text!r}") from exc


def _enum_value(value: Any, enum_cls: type[Enum], field_name: str) -> str:
    """接受枚举成员或字符串，返回其 ``value``，非法值直接报错。"""
    if isinstance(value, enum_cls):
        return value.value
    if isinstance(value, str):
        try:
            return enum_cls(value).value
        except ValueError:
            pass
    allowed = ", ".join(member.value for member in enum_cls)
    raise StorageError(f"{field_name} 非法: {value!r}（允许: {allowed}）")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="seconds")
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"无法序列化为 JSON: {type(obj).__name__}")


def _canonical_json(payload: Any) -> str:
    """规范化 JSON：键排序、无多余空白，保证哈希可复现。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default
    )


def _note_from_row(row: sqlite3.Row, tz: tzinfo) -> AgentNote:
    raw_tags = row["tags"]
    try:
        tags = tuple(str(t) for t in json.loads(raw_tags)) if raw_tags else ()
    except (TypeError, ValueError):
        tags = ()
    return AgentNote(
        note_no=int(row["note_no"]),
        kind=str(row["kind"]),
        content=str(row["content"]),
        tags=tags,
        decision_id=row["decision_id"],
        trading_date=row["trading_date"],
        author=str(row["author"]),
        event_key=row["event_key"],
        created_at=_parse_iso(row["created_at"], "created_at"),
    )


def _turn_from_row(row: sqlite3.Row, tz: tzinfo) -> AgentTurn:
    try:
        messages = tuple(dict(m) for m in json.loads(row["messages"]))
    except (TypeError, ValueError):  # pragma: no cover - 存档损坏时不应炸掉决策
        messages = ()
    return AgentTurn(
        turn_no=int(row["turn_no"]),
        hour_key=str(row["hour_key"]),
        day_key=str(row["day_key"]),
        decision_id=str(row["decision_id"]),
        outcome=row["outcome"],
        messages=messages,
        summary=row["summary"],
        created_at=_parse_iso(row["created_at"], "created_at"),
    )


def _hour_from_row(row: sqlite3.Row, tz: tzinfo) -> HourSummary:
    try:
        stats = json.loads(row["stats"]) if row["stats"] else {}
    except (TypeError, ValueError):  # pragma: no cover
        stats = {}
    return HourSummary(
        hour_key=str(row["hour_key"]),
        day_key=str(row["day_key"]),
        turn_count=int(row["turn_count"]),
        summary=str(row["summary"]),
        stats=dict(stats),
        created_at=_parse_iso(row["created_at"], "created_at"),
    )


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AccountState:
    """账户状态快照。``revision`` 用于乐观并发控制。"""

    account_id: int
    revision: int
    status: str
    cash_balance: Decimal
    equity: Decimal
    margin_used: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    peak_equity: Decimal
    day_start_equity: Decimal
    trading_date: str | None
    halted: bool
    halt_reason: str | None
    updated_at: datetime
    as_of: datetime

    @property
    def free_margin(self) -> Decimal:
        return self.equity - self.margin_used

    @property
    def margin_level(self) -> Decimal | None:
        """保证金水平（净值 / 已用保证金）；无持仓时返回 None。"""
        if self.margin_used == 0:
            return None
        return self.equity / self.margin_used

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "revision": self.revision,
            "status": self.status,
            "cash_balance": str(self.cash_balance),
            "equity": str(self.equity),
            "margin_used": str(self.margin_used),
            "unrealized_pnl": str(self.unrealized_pnl),
            "realized_pnl": str(self.realized_pnl),
            "peak_equity": str(self.peak_equity),
            "day_start_equity": str(self.day_start_equity),
            "trading_date": self.trading_date,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
            "as_of": self.as_of.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True, slots=True)
class Position:
    position_id: int
    revision: int
    symbol: str
    side: str
    volume: Decimal
    entry_price: Decimal
    entry_at: datetime
    stop_loss: Decimal | None
    take_profit: Decimal | None
    planned_risk_jpy: Decimal | None
    margin_jpy: Decimal
    status: str
    closed_at: datetime | None
    realized_pnl_jpy: Decimal | None
    close_reason: str | None
    decision_ref: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_open(self) -> bool:
        return self.status == PositionStatus.OPEN.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "revision": self.revision,
            "symbol": self.symbol,
            "side": self.side,
            "volume": str(self.volume),
            "entry_price": str(self.entry_price),
            "entry_at": self.entry_at.isoformat(timespec="seconds"),
            "stop_loss": str(self.stop_loss) if self.stop_loss is not None else None,
            "take_profit": str(self.take_profit) if self.take_profit is not None else None,
            "planned_risk_jpy": (
                str(self.planned_risk_jpy) if self.planned_risk_jpy is not None else None
            ),
            "margin_jpy": str(self.margin_jpy),
            "status": self.status,
            "closed_at": self.closed_at.isoformat(timespec="seconds") if self.closed_at else None,
            "realized_pnl_jpy": (
                str(self.realized_pnl_jpy) if self.realized_pnl_jpy is not None else None
            ),
            "close_reason": self.close_reason,
            "decision_ref": self.decision_ref,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True, slots=True)
class TradeRecord:
    trade_no: int
    position_id: int | None
    symbol: str
    action: str
    side: str
    volume: Decimal
    price: Decimal
    quoted_price: Decimal | None
    spread_jpy: Decimal | None
    slippage_jpy: Decimal | None
    realized_pnl_jpy: Decimal | None
    fee_jpy: Decimal | None
    reason: str | None
    occurred_at: datetime
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_no": self.trade_no,
            "position_id": self.position_id,
            "symbol": self.symbol,
            "action": self.action,
            "side": self.side,
            "volume": str(self.volume),
            "price": str(self.price),
            "quoted_price": str(self.quoted_price) if self.quoted_price is not None else None,
            "spread_jpy": str(self.spread_jpy) if self.spread_jpy is not None else None,
            "slippage_jpy": str(self.slippage_jpy) if self.slippage_jpy is not None else None,
            "realized_pnl_jpy": (
                str(self.realized_pnl_jpy) if self.realized_pnl_jpy is not None else None
            ),
            "fee_jpy": str(self.fee_jpy) if self.fee_jpy is not None else None,
            "reason": self.reason,
            "occurred_at": self.occurred_at.isoformat(timespec="seconds"),
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True, slots=True)
class CashFlow:
    flow_no: int
    flow_type: str
    amount: Decimal
    balance_after: Decimal
    currency: str
    position_id: int | None
    trade_no: int | None
    note: str | None
    occurred_at: datetime
    created_at: datetime
    #: 幂等键。初始本金那条流水的 key 为 ``INITIAL_CAPITAL``，调用方据此区分
    #: 「初始本金」与「后续入金」，避免重复计入。
    event_key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "flow_no": self.flow_no,
            "flow_type": self.flow_type,
            "amount": str(self.amount),
            "balance_after": str(self.balance_after),
            "currency": self.currency,
            "position_id": self.position_id,
            "trade_no": self.trade_no,
            "note": self.note,
            "event_key": self.event_key,
            "occurred_at": self.occurred_at.isoformat(timespec="seconds"),
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True, slots=True)
class QuoteRecord:
    quote_no: int
    symbol: str
    bid: Decimal
    ask: Decimal
    mid: Decimal
    quote_timestamp: datetime
    source: str
    source_url: str | None
    quote_type: str
    granularity: str | None
    is_stale: bool
    recorded_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "quote_no": self.quote_no,
            "symbol": self.symbol,
            "bid": str(self.bid),
            "ask": str(self.ask),
            "mid": str(self.mid),
            "quote_timestamp": self.quote_timestamp.isoformat(timespec="seconds"),
            "source": self.source,
            "source_url": self.source_url,
            "quote_type": self.quote_type,
            "granularity": self.granularity,
            "is_stale": self.is_stale,
            "recorded_at": self.recorded_at.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True, slots=True)
class AuditEvent:
    seq: int
    event_type: str
    payload: Any
    prev_hash: str
    hash: str
    occurred_at: datetime
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_type": self.event_type,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "occurred_at": self.occurred_at.isoformat(timespec="seconds"),
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True, slots=True)
class DailyBaseline:
    trading_date: str
    day_start_equity: Decimal
    day_start_balance: Decimal
    peak_equity: Decimal
    low_equity: Decimal
    drawdown_stop_triggered: bool
    triggered_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "trading_date": self.trading_date,
            "day_start_equity": str(self.day_start_equity),
            "day_start_balance": str(self.day_start_balance),
            "peak_equity": str(self.peak_equity),
            "low_equity": str(self.low_equity),
            "drawdown_stop_triggered": self.drawdown_stop_triggered,
            "triggered_at": (
                self.triggered_at.isoformat(timespec="seconds") if self.triggered_at else None
            ),
            "notes": self.notes,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True, slots=True)
class SchedulerRun:
    run_no: int
    task_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    detail: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_no": self.run_no,
            "task_name": self.task_name,
            "status": self.status,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": (
                self.finished_at.isoformat(timespec="seconds") if self.finished_at else None
            ),
            "detail": self.detail,
            "error": self.error,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True, slots=True)
class AgentNote:
    """AI 自己写下的一条笔记（心得 / 决策理由 / 观察）。

    **这不是账本事实**：内容由模型生成，未经校验，只作记忆用。回灌给模型时
    必须与账本数据明确区分开，否则模型会把自己的旧猜测当成当前事实。
    """

    note_no: int
    kind: str
    content: str
    tags: tuple[str, ...]
    decision_id: str | None
    trading_date: str | None
    author: str
    event_key: str | None
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "note_no": self.note_no,
            "kind": self.kind,
            "content": self.content,
            "tags": list(self.tags),
            "decision_id": self.decision_id,
            "trading_date": self.trading_date,
            "author": self.author,
            "event_key": self.event_key,
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True, slots=True)
class AgentTurn:
    """一小时对话存档里的一次决策（模型看到的消息 + 它给出的结果）。

    ``messages`` 是**当时真正发给模型的完整消息列表**（JSON 字符串）。之所以原样
    保存而不是重新拼装，是因为要重放它来命中前缀缓存——只要有一个字节不同，
    前缀缓存就整体失效。
    """

    turn_no: int
    hour_key: str
    day_key: str
    decision_id: str
    outcome: str | None
    messages: tuple[dict[str, Any], ...]
    summary: str | None
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_no": self.turn_no,
            "hour_key": self.hour_key,
            "day_key": self.day_key,
            "decision_id": self.decision_id,
            "outcome": self.outcome,
            "messages": [dict(m) for m in self.messages],
            "summary": self.summary,
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True, slots=True)
class HourSummary:
    """一个小时结束后的压缩摘要（原始对话仍在 ``agent_turns`` 里）。"""

    hour_key: str
    day_key: str
    turn_count: int
    summary: str
    stats: dict[str, Any]
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "hour_key": self.hour_key,
            "day_key": self.day_key,
            "turn_count": self.turn_count,
            "summary": self.summary,
            "stats": self.stats,
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True, slots=True)
class AuditVerification:
    ok: bool
    checked: int
    head_hash: str
    first_bad_seq: int | None
    problems: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "head_hash": self.head_hash,
            "first_bad_seq": self.first_bad_seq,
            "problems": list(self.problems),
        }


@dataclass(frozen=True, slots=True)
class LedgerVerification:
    ok: bool
    flow_count: int
    ledger_balance: Decimal
    problems: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "flow_count": self.flow_count,
            "ledger_balance": str(self.ledger_balance),
            "problems": list(self.problems),
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """账户核对结果。本方法只报告，不自动改写数据。"""

    ok: bool
    checked_at: datetime
    ledger_balance: Decimal
    stored_balance: Decimal
    balance_difference: Decimal
    realized_pnl_from_trades: Decimal
    realized_pnl_from_flows: Decimal
    pnl_difference: Decimal
    flow_count: int
    trade_count: int
    open_position_count: int
    open_position_margin: Decimal
    problems: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at.isoformat(timespec="seconds"),
            "ledger_balance": str(self.ledger_balance),
            "stored_balance": str(self.stored_balance),
            "balance_difference": str(self.balance_difference),
            "realized_pnl_from_trades": str(self.realized_pnl_from_trades),
            "realized_pnl_from_flows": str(self.realized_pnl_from_flows),
            "pnl_difference": str(self.pnl_difference),
            "flow_count": self.flow_count,
            "trade_count": self.trade_count,
            "open_position_count": self.open_position_count,
            "open_position_margin": str(self.open_position_margin),
            "problems": list(self.problems),
        }


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    ok: bool
    schema_version: int
    sqlite_integrity: str
    foreign_key_violations: tuple[str, ...]
    audit: AuditVerification
    ledger: LedgerVerification
    reconciliation: ReconciliationReport
    problems: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "schema_version": self.schema_version,
            "sqlite_integrity": self.sqlite_integrity,
            "foreign_key_violations": list(self.foreign_key_violations),
            "audit": self.audit.as_dict(),
            "ledger": self.ledger.as_dict(),
            "reconciliation": self.reconciliation.as_dict(),
            "problems": list(self.problems),
        }


# --------------------------------------------------------------------------- #
# DDL
# --------------------------------------------------------------------------- #

_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account (
        id                     INTEGER PRIMARY KEY CHECK (id = 1),
        currency               TEXT NOT NULL,
        initial_capital        TEXT NOT NULL,
        initial_capital_flow_no INTEGER,
        opened_at              TEXT NOT NULL,
        created_at             TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_state (
        account_id       INTEGER PRIMARY KEY REFERENCES account(id),
        revision         INTEGER NOT NULL DEFAULT 1,
        status           TEXT NOT NULL,
        cash_balance     TEXT NOT NULL,
        equity           TEXT NOT NULL,
        margin_used      TEXT NOT NULL,
        unrealized_pnl   TEXT NOT NULL,
        realized_pnl     TEXT NOT NULL,
        peak_equity      TEXT NOT NULL,
        day_start_equity TEXT NOT NULL,
        trading_date     TEXT,
        halted           INTEGER NOT NULL DEFAULT 0,
        halt_reason      TEXT,
        updated_at       TEXT NOT NULL,
        as_of            TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS positions (
        position_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id      INTEGER NOT NULL REFERENCES account(id),
        revision        INTEGER NOT NULL DEFAULT 1,
        symbol          TEXT NOT NULL,
        side            TEXT NOT NULL,
        volume          TEXT NOT NULL,
        entry_price     TEXT NOT NULL,
        entry_at        TEXT NOT NULL,
        stop_loss       TEXT,
        take_profit     TEXT,
        planned_risk_jpy TEXT,
        margin_jpy      TEXT NOT NULL,
        status          TEXT NOT NULL,
        closed_at       TEXT,
        realized_pnl_jpy TEXT,
        close_reason    TEXT,
        decision_ref    TEXT,
        event_key       TEXT UNIQUE,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)",
    "CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol, status)",
    """
    CREATE TABLE IF NOT EXISTS trades (
        trade_no        INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id      INTEGER NOT NULL REFERENCES account(id),
        event_key       TEXT UNIQUE,
        position_id     INTEGER REFERENCES positions(position_id),
        symbol          TEXT NOT NULL,
        action          TEXT NOT NULL,
        side            TEXT NOT NULL,
        volume          TEXT NOT NULL,
        price           TEXT NOT NULL,
        quoted_price    TEXT,
        spread_jpy      TEXT,
        slippage_jpy    TEXT,
        realized_pnl_jpy TEXT,
        fee_jpy         TEXT,
        reason          TEXT,
        occurred_at     TEXT NOT NULL,
        created_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_trades_occurred ON trades(occurred_at)",
    """
    CREATE TABLE IF NOT EXISTS cash_flows (
        flow_no       INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id    INTEGER NOT NULL REFERENCES account(id),
        event_key     TEXT UNIQUE,
        flow_type     TEXT NOT NULL,
        amount        TEXT NOT NULL,
        balance_after TEXT NOT NULL,
        currency      TEXT NOT NULL,
        position_id   INTEGER REFERENCES positions(position_id),
        trade_no      INTEGER REFERENCES trades(trade_no),
        note          TEXT,
        occurred_at   TEXT NOT NULL,
        created_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cash_flows_occurred ON cash_flows(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_cash_flows_type ON cash_flows(flow_type)",
    """
    CREATE TABLE IF NOT EXISTS quotes (
        quote_no        INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key       TEXT UNIQUE,
        symbol          TEXT NOT NULL,
        bid             TEXT NOT NULL,
        ask             TEXT NOT NULL,
        mid             TEXT NOT NULL,
        quote_timestamp TEXT NOT NULL,
        source          TEXT NOT NULL,
        source_url      TEXT,
        quote_type      TEXT NOT NULL,
        granularity     TEXT,
        is_stale        INTEGER NOT NULL,
        recorded_at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_quotes_timestamp ON quotes(quote_timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_quotes_symbol ON quotes(symbol, quote_timestamp)",
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        seq         INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type  TEXT NOT NULL,
        payload     TEXT NOT NULL,
        prev_hash   TEXT NOT NULL,
        hash        TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_equity_baseline (
        trading_date           TEXT PRIMARY KEY,
        account_id             INTEGER NOT NULL REFERENCES account(id),
        day_start_equity       TEXT NOT NULL,
        day_start_balance      TEXT NOT NULL,
        peak_equity            TEXT NOT NULL,
        low_equity             TEXT NOT NULL,
        drawdown_stop_triggered INTEGER NOT NULL DEFAULT 0,
        triggered_at           TEXT,
        notes                  TEXT,
        created_at             TEXT NOT NULL,
        updated_at             TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduler_runs (
        run_no      INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key   TEXT UNIQUE,
        task_name   TEXT NOT NULL,
        status      TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        detail      TEXT,
        error       TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scheduler_task ON scheduler_runs(task_name, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_scheduler_status ON scheduler_runs(status)",
    """
    CREATE TABLE IF NOT EXISTS agent_notes (
        note_no      INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key    TEXT UNIQUE,
        kind         TEXT NOT NULL,
        content      TEXT NOT NULL,
        tags         TEXT,
        decision_id  TEXT,
        trading_date TEXT,
        author       TEXT NOT NULL DEFAULT 'ai',
        created_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notes_created ON agent_notes(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_notes_kind ON agent_notes(kind, created_at)",
    """
    CREATE TABLE IF NOT EXISTS agent_turns (
        turn_no     INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key   TEXT UNIQUE,
        hour_key    TEXT NOT NULL,
        day_key     TEXT NOT NULL,
        decision_id TEXT NOT NULL,
        outcome     TEXT,
        messages    TEXT NOT NULL,
        summary     TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_turns_hour ON agent_turns(hour_key, turn_no)",
    """
    CREATE TABLE IF NOT EXISTS hour_summaries (
        hour_key   TEXT PRIMARY KEY,
        day_key    TEXT NOT NULL,
        turn_count INTEGER NOT NULL,
        summary    TEXT NOT NULL,
        stats      TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hours_day ON hour_summaries(day_key, hour_key)",
)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


class Storage:
    """虚拟账户的 SQLite 持久化层。

    每个公开方法都使用独立的连接与事务；写操作使用 ``BEGIN IMMEDIATE``，
    配合 WAL 日志模式与 ``busy_timeout`` 处理并发访问。SQLite 本身保证事务
    原子性，因此不存在「只提交了一半」的状态。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        tz: tzinfo | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.db_path = Path(db_path)
        self.tz: tzinfo = tz or _JST
        self.timeout = timeout
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # -- 构造 -------------------------------------------------------------- #

    @classmethod
    def from_config(cls, cfg: Config | None = None, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> Storage:
        """按 ``config.json`` 的 ``paths.data_dir`` / ``paths.database_filename`` 打开数据库。"""
        cfg = cfg or load_config()
        return cls(cfg.paths.database_path, tz=cfg.tz, timeout=timeout)

    def __repr__(self) -> str:  # pragma: no cover - 便于日志排查
        return f"<Storage {self.db_path}>"

    # -- 连接与事务 -------------------------------------------------------- #

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=self.timeout, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
            conn.execute("PRAGMA synchronous=NORMAL")
        except BaseException:
            # PRAGMA 失败（例如文件不是合法 SQLite 库）时必须关掉连接，
            # 否则文件句柄会泄漏，Windows 上表现为文件无法删除/重命名。
            conn.close()
            raise
        return conn

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """打开一个事务；异常时回滚，正常时提交。

        ``write=True`` 使用 ``BEGIN IMMEDIATE``，一进入就取得写锁，避免
        「先读后写」时的锁升级死锁（SQLITE_BUSY）。不支持嵌套事务。
        """
        conn = self._connect()
        try:
            try:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            except sqlite3.OperationalError as exc:
                raise StorageError(f"无法开始事务（数据库可能被占用）: {exc}") from exc
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - 回滚失败时保留原始异常
                    pass
                raise
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise StorageError(f"数据库操作失败: {exc}") from exc
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """创建表结构；若数据库由别的程序创建或 schema 版本不符，明确报错。

        整个 DDL 放在一个事务里，避免中途失败留下半套表（SQLite 的 DDL 是
        事务性的）。
        """
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise StorageError(f"无法打开数据库 {self.db_path}: {exc}") from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._create_schema(conn)
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - 回滚失败时保留原始异常
                    pass
                raise
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise StorageError(f"初始化数据库结构失败: {exc}") from exc
        finally:
            conn.close()

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        existing_tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if existing_tables and "meta" not in existing_tables:
            raise SchemaVersionError(
                f"{self.db_path} 已存在且不是本模块创建的数据库"
                f"（缺少 meta 表，现有表: {', '.join(sorted(existing_tables))}）；"
                "拒绝就地覆盖，请改用其它路径或先人工确认"
            )
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)

        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            return
        found = int(row["value"])
        if found != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"{self.db_path} 的 schema 版本为 {found}，本模块需要 "
                f"{SCHEMA_VERSION}；不会就地改写既有数据"
            )

    # -- 账户初始化 -------------------------------------------------------- #

    def initialize_account(
        self,
        initial_capital: Decimal | int | str,
        *,
        currency: str = "JPY",
        opened_at: datetime | None = None,
    ) -> AccountState:
        """初始化账户并发放初始本金。**重复调用是幂等的，不会重复发放本金。**

        * 账户已存在且本金一致 → 原样返回，不产生任何新流水；
        * 账户已存在但本金不同 → 抛 :class:`AccountAlreadyInitializedError`，
          绝不改写既有本金。

        幂等性由 ``cash_flows.event_key = 'INITIAL_CAPITAL'`` 的 UNIQUE 约束
        以及账户表主键共同保证。
        """
        capital = _as_decimal(initial_capital, "initial_capital")
        if capital <= 0:
            raise StorageError(f"初始本金必须为正数，收到 {capital}")
        now = _iso(opened_at, self.tz)
        capital_text = _dec_text(capital, "initial_capital")

        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM account WHERE id=?", (_ACCOUNT_ID,)).fetchone()
            if existing is not None:
                if existing["currency"] != currency:
                    raise AccountAlreadyInitializedError(
                        f"账户已用货币 {existing['currency']} 初始化，"
                        f"不能改为 {currency}（不会覆盖既有账户）"
                    )
                if _as_decimal(existing["initial_capital"]) != capital:
                    raise AccountAlreadyInitializedError(
                        f"账户已用初始本金 {existing['initial_capital']} 初始化，"
                        f"不能改为 {capital_text}（不会重复发放或改写本金）"
                    )
                return self._read_state(conn)

            conn.execute(
                "INSERT INTO account(id, currency, initial_capital, opened_at, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (_ACCOUNT_ID, currency, capital_text, now, now),
            )
            conn.execute(
                "INSERT INTO account_state("
                " account_id, revision, status, cash_balance, equity, margin_used,"
                " unrealized_pnl, realized_pnl, peak_equity, day_start_equity,"
                " trading_date, halted, updated_at, as_of"
                ") VALUES(?, 1, ?, '0', '0', '0', '0', '0', '0', '0', ?, 0, ?, ?)",
                (_ACCOUNT_ID, AccountStatus.ACTIVE.value, now[:10], now, now),
            )
            flow, _ = self._insert_cash_flow(
                conn,
                flow_type=FlowType.DEPOSIT,
                amount=capital,
                event_key=INITIAL_CAPITAL_EVENT_KEY,
                note="初始本金（仅发放一次）",
                occurred_at=now,
                on_duplicate=OnDuplicate.ERROR,
            )
            conn.execute(
                "UPDATE account SET initial_capital_flow_no=? WHERE id=?",
                (flow.flow_no, _ACCOUNT_ID),
            )
            # 初始净值/峰值基准 = 初始本金
            conn.execute(
                "UPDATE account_state SET peak_equity=?, day_start_equity=? WHERE account_id=?",
                (capital_text, capital_text, _ACCOUNT_ID),
            )
            self._sync_balance_projection(conn, occurred_at=now)
            self._insert_audit(
                conn,
                "ACCOUNT_INITIALIZED",
                {
                    "initial_capital": capital,
                    "currency": currency,
                    "initial_capital_flow_no": flow.flow_no,
                },
                occurred_at=now,
            )
            return self._read_state(conn)

    # -- meta 键值（供跨模块共享的小状态，例如验收闸门） -------------------- #

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """读取 ``meta`` 表的键；不存在时返回 ``default``。"""
        with self._transaction(write=False) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    def set_meta(
        self, key: str, value: str | None, *, occurred_at: datetime | None = None
    ) -> None:
        """写入 ``meta`` 键（``value=None`` 表示删除），并记一条审计事件。"""
        now = _iso(occurred_at, self.tz)
        with self._transaction() as conn:
            if value is None:
                conn.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )
            self._insert_audit(
                conn, "META_SET", {"key": key, "value": value}, occurred_at=now
            )

    def is_initialized(self) -> bool:
        with self._transaction(write=False) as conn:
            return conn.execute("SELECT 1 FROM account WHERE id=?", (_ACCOUNT_ID,)).fetchone() is not None

    # -- 账本读取（内部） -------------------------------------------------- #

    @staticmethod
    def _ledger_sum(conn: sqlite3.Connection) -> Decimal:
        """账本余额 = 所有流水之和。

        故意在 Python 侧用 Decimal 累加，而不是 SQL 的 ``SUM()``：SQLite 会把
        TEXT 转成浮点再求和，那正是本项目要避免的精度损失。
        """
        total = Decimal("0")
        for row in conn.execute("SELECT amount FROM cash_flows ORDER BY flow_no"):
            total += _as_decimal(row["amount"], "cash_flows.amount")
        return total

    def ledger_balance(self) -> Decimal:
        """当前账本余额（由流水推导，权威值）。"""
        with self._transaction(write=False) as conn:
            return self._ledger_sum(conn)

    @staticmethod
    def _account_currency(conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT currency FROM account WHERE id=?", (_ACCOUNT_ID,)).fetchone()
        if row is None:
            raise StorageError("账户尚未初始化，请先调用 initialize_account()")
        return str(row["currency"])

    def _sync_balance_projection(self, conn: sqlite3.Connection, *, occurred_at: str) -> None:
        """把账本余额同步进 ``account_state`` 的缓存投影（同事务内）。"""
        row = conn.execute(
            "SELECT * FROM account_state WHERE account_id=?", (_ACCOUNT_ID,)
        ).fetchone()
        if row is None:
            raise StorageError("账户状态不存在，请先调用 initialize_account()")
        balance = self._ledger_sum(conn)
        unrealized = _as_decimal(row["unrealized_pnl"], "unrealized_pnl")
        equity = balance + unrealized
        cursor = conn.execute(
            "UPDATE account_state SET cash_balance=?, equity=?, revision=revision+1,"
            " updated_at=?, as_of=? WHERE account_id=? AND revision=?",
            (_dec_text(balance), _dec_text(equity), occurred_at, occurred_at,
             _ACCOUNT_ID, row["revision"]),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyError(
                "账户状态在本次事务中被其它写入修改（revision 冲突），已回滚"
            )

    # -- 账户状态 ---------------------------------------------------------- #

    def _read_state(self, conn: sqlite3.Connection) -> AccountState:
        row = conn.execute(
            "SELECT * FROM account_state WHERE account_id=?", (_ACCOUNT_ID,)
        ).fetchone()
        if row is None:
            raise StorageError("账户尚未初始化，请先调用 initialize_account()")
        return AccountState(
            account_id=int(row["account_id"]),
            revision=int(row["revision"]),
            status=str(row["status"]),
            cash_balance=_row_dec(row, "cash_balance"),
            equity=_row_dec(row, "equity"),
            margin_used=_row_dec(row, "margin_used"),
            unrealized_pnl=_row_dec(row, "unrealized_pnl"),
            realized_pnl=_row_dec(row, "realized_pnl"),
            peak_equity=_row_dec(row, "peak_equity"),
            day_start_equity=_row_dec(row, "day_start_equity"),
            trading_date=row["trading_date"],
            halted=bool(row["halted"]),
            halt_reason=row["halt_reason"],
            updated_at=_parse_iso(row["updated_at"], "updated_at"),
            as_of=_parse_iso(row["as_of"], "as_of"),
        )

    def read_account_state(self) -> AccountState:
        """读取账户状态快照。"""
        with self._transaction(write=False) as conn:
            return self._read_state(conn)

    def save_account_state(
        self,
        state: AccountState,
        *,
        expected_revision: int | None = None,
        verify_balance: bool = True,
    ) -> AccountState:
        """保存账户状态快照。

        * **不做 INSERT OR REPLACE**：必须基于当前行做条件更新并递增 revision。
        * ``expected_revision`` 不为 ``None`` 时，与库中 revision 不符即抛
          :class:`ConcurrencyError`（防止覆盖他人写入）。
        * ``verify_balance=True``（默认）时要求 ``state.cash_balance`` 与账本
          流水之和一致，否则抛 :class:`LedgerMismatchError` —— 现金余额只能由
          账本决定，不能凭空写入。
        """
        now = _iso(None, self.tz)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM account_state WHERE account_id=?", (_ACCOUNT_ID,)
            ).fetchone()
            if row is None:
                raise StorageError("账户尚未初始化，请先调用 initialize_account()")
            current_revision = int(row["revision"])
            if expected_revision is not None and expected_revision != current_revision:
                raise ConcurrencyError(
                    f"账户状态已过期：期望 revision={expected_revision}，"
                    f"实际 revision={current_revision}"
                )
            if verify_balance:
                ledger = self._ledger_sum(conn)
                claimed = _as_decimal(state.cash_balance, "cash_balance")
                if claimed != ledger:
                    raise LedgerMismatchError(
                        f"拒绝写入：cash_balance={claimed} 与账本流水之和 {ledger} 不一致"
                        "（现金余额必须由 cash_flows 决定）"
                    )
            cursor = conn.execute(
                "UPDATE account_state SET status=?, cash_balance=?, equity=?, margin_used=?,"
                " unrealized_pnl=?, realized_pnl=?, peak_equity=?, day_start_equity=?,"
                " trading_date=?, halted=?, halt_reason=?, revision=revision+1,"
                " updated_at=?, as_of=? WHERE account_id=? AND revision=?",
                (
                    _enum_value(state.status, AccountStatus, "status"),
                    _dec_text(state.cash_balance, "cash_balance"),
                    _dec_text(state.equity, "equity"),
                    _dec_text(state.margin_used, "margin_used"),
                    _dec_text(state.unrealized_pnl, "unrealized_pnl"),
                    _dec_text(state.realized_pnl, "realized_pnl"),
                    _dec_text(state.peak_equity, "peak_equity"),
                    _dec_text(state.day_start_equity, "day_start_equity"),
                    state.trading_date,
                    1 if state.halted else 0,
                    state.halt_reason,
                    now,
                    _iso(state.as_of, self.tz),
                    _ACCOUNT_ID,
                    current_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyError("账户状态更新未生效（revision 冲突）")
            self._insert_audit(
                conn,
                "ACCOUNT_STATE_SAVED",
                {
                    "revision": current_revision + 1,
                    "cash_balance": state.cash_balance,
                    "equity": state.equity,
                    "margin_used": state.margin_used,
                    "status": state.status,
                },
                occurred_at=now,
            )
            return self._read_state(conn)

    def set_margin_used(self, margin_jpy: Decimal | int | str) -> AccountState:
        """更新已用保证金（由引擎在持仓变动后调用）。"""
        now = _iso(None, self.tz)
        with self._transaction() as conn:
            margin = _as_decimal(margin_jpy, "margin_jpy")
            if margin < 0:
                raise StorageError(f"已用保证金不能为负: {margin}")
            conn.execute(
                "UPDATE account_state SET margin_used=?, revision=revision+1, updated_at=?,"
                " as_of=? WHERE account_id=?",
                (_dec_text(margin), now, now, _ACCOUNT_ID),
            )
            return self._read_state(conn)

    # -- 资金流水 ---------------------------------------------------------- #

    def _insert_cash_flow(
        self,
        conn: sqlite3.Connection,
        *,
        flow_type: Any,
        amount: Any,
        event_key: str | None,
        note: str | None,
        occurred_at: str,
        position_id: int | None = None,
        trade_no: int | None = None,
        on_duplicate: OnDuplicate = OnDuplicate.IGNORE,
    ) -> tuple[CashFlow, bool]:
        """写入一条流水并计算 ``balance_after``（同一事务内）。

        返回 ``(流水, 是否新建)``。幂等命中时第二项为 ``False``，调用方据此
        跳过余额投影与审计，避免重复记账。
        """
        amount_dec = _as_decimal(amount, "amount")
        if event_key is not None:
            existing = conn.execute(
                "SELECT * FROM cash_flows WHERE event_key=?", (event_key,)
            ).fetchone()
            if existing is not None:
                if OnDuplicate(on_duplicate) is OnDuplicate.ERROR:
                    raise DuplicateEventError(f"流水事件 {event_key!r} 已存在")
                return self._row_to_cash_flow(existing), False
        balance_after = self._ledger_sum(conn) + amount_dec
        cursor = conn.execute(
            "INSERT INTO cash_flows("
            " account_id, event_key, flow_type, amount, balance_after, currency,"
            " position_id, trade_no, note, occurred_at, created_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _ACCOUNT_ID,
                event_key,
                _enum_value(flow_type, FlowType, "flow_type"),
                _dec_text(amount_dec, "amount"),
                _dec_text(balance_after, "balance_after"),
                self._account_currency(conn),
                position_id,
                trade_no,
                note,
                occurred_at,
                occurred_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM cash_flows WHERE flow_no=?", (cursor.lastrowid,)
        ).fetchone()
        return self._row_to_cash_flow(row), True

    @staticmethod
    def _row_to_cash_flow(row: Mapping[str, Any]) -> CashFlow:
        return CashFlow(
            flow_no=int(row["flow_no"]),
            flow_type=str(row["flow_type"]),
            amount=_row_dec(row, "amount"),
            balance_after=_row_dec(row, "balance_after"),
            currency=str(row["currency"]),
            position_id=row["position_id"],
            trade_no=row["trade_no"],
            note=row["note"],
            occurred_at=_parse_iso(row["occurred_at"], "occurred_at"),
            created_at=_parse_iso(row["created_at"], "created_at"),
            event_key=row["event_key"],
        )

    def record_cash_flow(
        self,
        *,
        flow_type: Any,
        amount: Decimal | int | str,
        event_key: str | None = None,
        note: str | None = None,
        occurred_at: datetime | None = None,
        position_id: int | None = None,
        trade_no: int | None = None,
        on_duplicate: Any = OnDuplicate.IGNORE,
    ) -> CashFlow:
        """记一笔资金流水（入金/出金/已实现盈亏/手续费/隔夜利息/调整）。

        传入 ``event_key`` 时具备幂等性：同一 key 重复写入默认返回既有记录
        （``on_duplicate='ERROR'`` 则抛 :class:`DuplicateEventError`）。
        """
        now = _iso(occurred_at, self.tz)
        with self._transaction() as conn:
            flow, created = self._insert_cash_flow(
                conn,
                flow_type=flow_type,
                amount=amount,
                event_key=event_key,
                note=note,
                occurred_at=now,
                position_id=position_id,
                trade_no=trade_no,
                on_duplicate=OnDuplicate(on_duplicate),
            )
            if not created:
                # 幂等命中：账本与余额都没有变化，不重复投影、不重复记审计。
                return flow
            self._sync_balance_projection(conn, occurred_at=now)
            self._insert_audit(
                conn,
                "CASH_FLOW_RECORDED",
                {
                    "flow_no": flow.flow_no,
                    "flow_type": flow.flow_type,
                    "amount": flow.amount,
                    "balance_after": flow.balance_after,
                    "event_key": event_key,
                },
                occurred_at=now,
            )
            return flow

    def read_cash_flows(
        self,
        *,
        flow_type: Any | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[CashFlow]:
        """按条件读取资金流水（按 ``flow_no`` 升序）。"""
        sql = "SELECT * FROM cash_flows WHERE 1=1"
        params: list[Any] = []
        if flow_type is not None:
            sql += " AND flow_type=?"
            params.append(_enum_value(flow_type, FlowType, "flow_type"))
        if since is not None:
            sql += " AND occurred_at>=?"
            params.append(_iso(since, self.tz))
        if until is not None:
            sql += " AND occurred_at<=?"
            params.append(_iso(until, self.tz))
        sql += " ORDER BY flow_no ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._transaction(write=False) as conn:
            return [self._row_to_cash_flow(row) for row in conn.execute(sql, params)]

    # -- 持仓 -------------------------------------------------------------- #

    def open_position(
        self,
        *,
        symbol: str,
        side: Any,
        volume: Decimal | int | str,
        entry_price: Decimal | int | str,
        margin_jpy: Decimal | int | str,
        stop_loss: Decimal | int | str | None = None,
        take_profit: Decimal | int | str | None = None,
        planned_risk_jpy: Decimal | int | str | None = None,
        quoted_price: Decimal | int | str | None = None,
        spread_jpy: Decimal | int | str | None = None,
        slippage_jpy: Decimal | int | str | None = None,
        reason: str | None = None,
        decision_ref: str | None = None,
        occurred_at: datetime | None = None,
        event_key: str | None = None,
        on_duplicate: Any = OnDuplicate.IGNORE,
    ) -> Position:
        """开仓：在同一事务内写入持仓、成交记录与审计事件。"""
        now = _iso(occurred_at, self.tz)
        side_value = _enum_value(side, PositionSide, "side")
        volume_dec = _as_decimal(volume, "volume")
        if volume_dec <= 0:
            raise StorageError(f"交易量必须为正数: {volume_dec}")
        entry_dec = _as_decimal(entry_price, "entry_price")
        if entry_dec <= 0:
            raise StorageError(f"开仓价必须为正数: {entry_dec}")
        margin_dec = _as_decimal(margin_jpy, "margin_jpy")
        if margin_dec <= 0:
            raise StorageError(f"保证金必须为正数: {margin_dec}")

        with self._transaction() as conn:
            self._account_currency(conn)  # 未初始化时明确报错
            if event_key is not None:
                existing = conn.execute(
                    "SELECT * FROM positions WHERE event_key=?", (event_key,)
                ).fetchone()
                if existing is not None:
                    if OnDuplicate(on_duplicate) is OnDuplicate.ERROR:
                        raise DuplicateEventError(f"持仓事件 {event_key!r} 已存在")
                    return self._row_to_position(existing)

            cursor = conn.execute(
                "INSERT INTO positions("
                " account_id, revision, symbol, side, volume, entry_price, entry_at,"
                " stop_loss, take_profit, planned_risk_jpy, margin_jpy, status,"
                " decision_ref, event_key, created_at, updated_at"
                ") VALUES(?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _ACCOUNT_ID,
                    symbol,
                    side_value,
                    _dec_text(volume_dec, "volume"),
                    _dec_text(entry_dec, "entry_price"),
                    now,
                    _opt_dec_text(stop_loss, "stop_loss"),
                    _opt_dec_text(take_profit, "take_profit"),
                    _opt_dec_text(planned_risk_jpy, "planned_risk_jpy"),
                    _dec_text(margin_dec, "margin_jpy"),
                    PositionStatus.OPEN.value,
                    decision_ref,
                    event_key,
                    now,
                    now,
                ),
            )
            position_id = int(cursor.lastrowid)
            trade, _ = self._insert_trade(
                conn,
                position_id=position_id,
                symbol=symbol,
                action=TradeAction.OPEN,
                side=side_value,
                volume=volume_dec,
                price=entry_dec,
                quoted_price=_opt_dec(quoted_price, "quoted_price"),
                spread_jpy=_opt_dec(spread_jpy, "spread_jpy"),
                slippage_jpy=_opt_dec(slippage_jpy, "slippage_jpy"),
                realized_pnl_jpy=None,
                fee_jpy=None,
                reason=reason,
                occurred_at=now,
                event_key=None,
            )
            self._insert_audit(
                conn,
                "POSITION_OPENED",
                {
                    "position_id": position_id,
                    "trade_no": trade.trade_no,
                    "symbol": symbol,
                    "side": side_value,
                    "volume": volume_dec,
                    "entry_price": entry_dec,
                    "margin_jpy": margin_dec,
                    "event_key": event_key,
                },
                occurred_at=now,
            )
            row = conn.execute(
                "SELECT * FROM positions WHERE position_id=?", (position_id,)
            ).fetchone()
            return self._row_to_position(row)

    def _insert_trade(
        self,
        conn: sqlite3.Connection,
        *,
        position_id: int | None,
        symbol: str,
        action: Any,
        side: Any,
        volume: Decimal,
        price: Decimal,
        quoted_price: Decimal | None,
        spread_jpy: Decimal | None,
        slippage_jpy: Decimal | None,
        realized_pnl_jpy: Decimal | None,
        fee_jpy: Decimal | None,
        reason: str | None,
        occurred_at: str,
        event_key: str | None,
        on_duplicate: OnDuplicate = OnDuplicate.IGNORE,
    ) -> tuple[TradeRecord, bool]:
        """写入一条成交记录，返回 ``(成交, 是否新建)``。"""
        if event_key is not None:
            existing = conn.execute(
                "SELECT * FROM trades WHERE event_key=?", (event_key,)
            ).fetchone()
            if existing is not None:
                if on_duplicate is OnDuplicate.ERROR:
                    raise DuplicateEventError(f"成交事件 {event_key!r} 已存在")
                return self._row_to_trade(existing), False
        cursor = conn.execute(
            "INSERT INTO trades("
            " account_id, event_key, position_id, symbol, action, side, volume, price,"
            " quoted_price, spread_jpy, slippage_jpy, realized_pnl_jpy, fee_jpy, reason,"
            " occurred_at, created_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _ACCOUNT_ID,
                event_key,
                position_id,
                symbol,
                _enum_value(action, TradeAction, "action"),
                _enum_value(side, PositionSide, "side"),
                _dec_text(volume, "volume"),
                _dec_text(price, "price"),
                _opt_dec_text(quoted_price, "quoted_price"),
                _opt_dec_text(spread_jpy, "spread_jpy"),
                _opt_dec_text(slippage_jpy, "slippage_jpy"),
                _opt_dec_text(realized_pnl_jpy, "realized_pnl_jpy"),
                _opt_dec_text(fee_jpy, "fee_jpy"),
                reason,
                occurred_at,
                occurred_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM trades WHERE trade_no=?", (cursor.lastrowid,)
        ).fetchone()
        return self._row_to_trade(row), True

    @staticmethod
    def _row_to_trade(row: Mapping[str, Any]) -> TradeRecord:
        return TradeRecord(
            trade_no=int(row["trade_no"]),
            position_id=row["position_id"],
            symbol=str(row["symbol"]),
            action=str(row["action"]),
            side=str(row["side"]),
            volume=_row_dec(row, "volume"),
            price=_row_dec(row, "price"),
            quoted_price=_row_opt_dec(row, "quoted_price"),
            spread_jpy=_row_opt_dec(row, "spread_jpy"),
            slippage_jpy=_row_opt_dec(row, "slippage_jpy"),
            realized_pnl_jpy=_row_opt_dec(row, "realized_pnl_jpy"),
            fee_jpy=_row_opt_dec(row, "fee_jpy"),
            reason=row["reason"],
            occurred_at=_parse_iso(row["occurred_at"], "occurred_at"),
            created_at=_parse_iso(row["created_at"], "created_at"),
        )

    @staticmethod
    def _row_to_position(row: Mapping[str, Any]) -> Position:
        return Position(
            position_id=int(row["position_id"]),
            revision=int(row["revision"]),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            volume=_row_dec(row, "volume"),
            entry_price=_row_dec(row, "entry_price"),
            entry_at=_parse_iso(row["entry_at"], "entry_at"),
            stop_loss=_row_opt_dec(row, "stop_loss"),
            take_profit=_row_opt_dec(row, "take_profit"),
            planned_risk_jpy=_row_opt_dec(row, "planned_risk_jpy"),
            margin_jpy=_row_dec(row, "margin_jpy"),
            status=str(row["status"]),
            closed_at=_parse_iso(row["closed_at"], "closed_at"),
            realized_pnl_jpy=_row_opt_dec(row, "realized_pnl_jpy"),
            close_reason=row["close_reason"],
            decision_ref=row["decision_ref"],
            created_at=_parse_iso(row["created_at"], "created_at"),
            updated_at=_parse_iso(row["updated_at"], "updated_at"),
        )

    def read_position(self, position_id: int) -> Position | None:
        with self._transaction(write=False) as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE position_id=?", (int(position_id),)
            ).fetchone()
            return None if row is None else self._row_to_position(row)

    def find_position_by_event_key(self, event_key: str) -> Position | None:
        """按幂等键查找持仓（含已平仓），供引擎判断重复指令。"""
        if not event_key:
            raise StorageError("event_key 不能为空")
        with self._transaction(write=False) as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE event_key=?", (event_key,)
            ).fetchone()
            return None if row is None else self._row_to_position(row)

    def read_positions(
        self,
        *,
        status: Any | None = PositionStatus.OPEN,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> list[Position]:
        """读取持仓；默认只返回当前未平仓持仓。``status=None`` 表示不过滤。"""
        sql = "SELECT * FROM positions WHERE 1=1"
        params: list[Any] = []
        if status is not None:
            sql += " AND status=?"
            params.append(_enum_value(status, PositionStatus, "status"))
        if symbol is not None:
            sql += " AND symbol=?"
            params.append(symbol)
        sql += " ORDER BY position_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._transaction(write=False) as conn:
            return [self._row_to_position(row) for row in conn.execute(sql, params)]

    def update_position(
        self,
        position_id: int,
        *,
        stop_loss: Decimal | int | str | None = None,
        take_profit: Decimal | int | str | None = None,
        planned_risk_jpy: Decimal | int | str | None = None,
        expected_revision: int | None = None,
    ) -> Position:
        """更新持仓的止损/止盈/计划风险（条件更新，不做整行覆盖）。"""
        now = _iso(None, self.tz)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE position_id=?", (int(position_id),)
            ).fetchone()
            if row is None:
                raise StorageError(f"持仓不存在: {position_id}")
            if row["status"] != PositionStatus.OPEN.value:
                raise StorageError(f"持仓 {position_id} 已平仓，不能再修改")
            current_revision = int(row["revision"])
            if expected_revision is not None and expected_revision != current_revision:
                raise ConcurrencyError(
                    f"持仓 {position_id} 已过期：期望 revision={expected_revision}，"
                    f"实际 revision={current_revision}"
                )
            cursor = conn.execute(
                "UPDATE positions SET stop_loss=COALESCE(?, stop_loss),"
                " take_profit=COALESCE(?, take_profit),"
                " planned_risk_jpy=COALESCE(?, planned_risk_jpy),"
                " revision=revision+1, updated_at=?"
                " WHERE position_id=? AND revision=?",
                (
                    _opt_dec_text(stop_loss, "stop_loss"),
                    _opt_dec_text(take_profit, "take_profit"),
                    _opt_dec_text(planned_risk_jpy, "planned_risk_jpy"),
                    now,
                    int(position_id),
                    current_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyError(f"持仓 {position_id} 更新未生效（revision 冲突）")
            self._insert_audit(
                conn,
                "POSITION_UPDATED",
                {
                    "position_id": int(position_id),
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "revision": current_revision + 1,
                },
                occurred_at=now,
            )
            updated = conn.execute(
                "SELECT * FROM positions WHERE position_id=?", (int(position_id),)
            ).fetchone()
            return self._row_to_position(updated)

    def close_position(
        self,
        position_id: int,
        *,
        exit_price: Decimal | int | str,
        realized_pnl_jpy: Decimal | int | str,
        reason: str | None = None,
        fee_jpy: Decimal | int | str | None = None,
        quoted_price: Decimal | int | str | None = None,
        spread_jpy: Decimal | int | str | None = None,
        slippage_jpy: Decimal | int | str | None = None,
        occurred_at: datetime | None = None,
        event_key: str | None = None,
        expected_revision: int | None = None,
    ) -> tuple[Position, TradeRecord]:
        """平仓：同一事务内更新持仓、写成交、记已实现盈亏流水、同步余额、写审计。

        这是「不会只更新一部分」的关键路径：持仓状态、交易历史、资金流水、
        账户余额与审计日志要么全部生效，要么全部回滚。
        """
        now = _iso(occurred_at, self.tz)
        exit_dec = _as_decimal(exit_price, "exit_price")
        if exit_dec <= 0:
            raise StorageError(f"平仓价必须为正数: {exit_dec}")
        pnl_dec = _as_decimal(realized_pnl_jpy, "realized_pnl_jpy")
        fee_dec = _opt_dec(fee_jpy, "fee_jpy")

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE position_id=?", (int(position_id),)
            ).fetchone()
            if row is None:
                raise StorageError(f"持仓不存在: {position_id}")
            if row["status"] == PositionStatus.CLOSED.value:
                # 幂等：已平仓则返回既有结果，不重复记账
                trade_row = conn.execute(
                    "SELECT * FROM trades WHERE position_id=? AND action=?"
                    " ORDER BY trade_no DESC LIMIT 1",
                    (int(position_id), TradeAction.CLOSE.value),
                ).fetchone()
                if trade_row is None:  # pragma: no cover - 理论不可达
                    raise StorageError(f"持仓 {position_id} 已平仓但找不到平仓成交记录")
                return self._row_to_position(row), self._row_to_trade(trade_row)

            current_revision = int(row["revision"])
            if expected_revision is not None and expected_revision != current_revision:
                raise ConcurrencyError(
                    f"持仓 {position_id} 已过期：期望 revision={expected_revision}，"
                    f"实际 revision={current_revision}"
                )

            cursor = conn.execute(
                "UPDATE positions SET status=?, closed_at=?, realized_pnl_jpy=?,"
                " close_reason=?, revision=revision+1, updated_at=?"
                " WHERE position_id=? AND revision=? AND status=?",
                (
                    PositionStatus.CLOSED.value,
                    now,
                    _dec_text(pnl_dec, "realized_pnl_jpy"),
                    reason,
                    now,
                    int(position_id),
                    current_revision,
                    PositionStatus.OPEN.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyError(f"持仓 {position_id} 平仓未生效（状态或 revision 冲突）")

            trade, _ = self._insert_trade(
                conn,
                position_id=int(position_id),
                symbol=str(row["symbol"]),
                action=TradeAction.CLOSE,
                side=str(row["side"]),
                volume=_as_decimal(row["volume"], "volume"),
                price=exit_dec,
                quoted_price=_opt_dec(quoted_price, "quoted_price"),
                spread_jpy=_opt_dec(spread_jpy, "spread_jpy"),
                slippage_jpy=_opt_dec(slippage_jpy, "slippage_jpy"),
                realized_pnl_jpy=pnl_dec,
                fee_jpy=fee_dec,
                reason=reason,
                occurred_at=now,
                event_key=event_key,
                # 平仓路径下重复 event_key 属于不一致输入：直接报错并整体回滚，
                # 而不是静默复用一条属于别的持仓的成交记录。
                on_duplicate=OnDuplicate.ERROR,
            )

            if pnl_dec != 0:
                self._insert_cash_flow(
                    conn,
                    flow_type=FlowType.REALIZED_PNL,
                    amount=pnl_dec,
                    event_key=f"PNL:{trade.trade_no}" if event_key is None else f"PNL:{event_key}",
                    note=f"持仓 {position_id} 平仓已实现盈亏",
                    occurred_at=now,
                    position_id=int(position_id),
                    trade_no=trade.trade_no,
                )
            if fee_dec:
                self._insert_cash_flow(
                    conn,
                    flow_type=FlowType.FEE,
                    amount=-abs(fee_dec),
                    event_key=f"FEE:{trade.trade_no}",
                    note=f"持仓 {position_id} 平仓手续费",
                    occurred_at=now,
                    position_id=int(position_id),
                    trade_no=trade.trade_no,
                )

            # 已实现盈亏累计 + 余额投影
            conn.execute(
                "UPDATE account_state SET realized_pnl = realized_pnl + ?"
                " WHERE account_id=?",
                (_dec_text(pnl_dec - abs(fee_dec or Decimal("0")), "realized_pnl"), _ACCOUNT_ID),
            )
            self._sync_balance_projection(conn, occurred_at=now)
            self._insert_audit(
                conn,
                "POSITION_CLOSED",
                {
                    "position_id": int(position_id),
                    "trade_no": trade.trade_no,
                    "exit_price": exit_dec,
                    "realized_pnl_jpy": pnl_dec,
                    "fee_jpy": fee_dec,
                    "reason": reason,
                },
                occurred_at=now,
            )
            updated = conn.execute(
                "SELECT * FROM positions WHERE position_id=?", (int(position_id),)
            ).fetchone()
            return self._row_to_position(updated), trade

    def record_trade(
        self,
        *,
        symbol: str,
        action: Any,
        side: Any,
        volume: Decimal | int | str,
        price: Decimal | int | str,
        position_id: int | None = None,
        quoted_price: Decimal | int | str | None = None,
        spread_jpy: Decimal | int | str | None = None,
        slippage_jpy: Decimal | int | str | None = None,
        realized_pnl_jpy: Decimal | int | str | None = None,
        fee_jpy: Decimal | int | str | None = None,
        reason: str | None = None,
        occurred_at: datetime | None = None,
        event_key: str | None = None,
        on_duplicate: Any = OnDuplicate.IGNORE,
    ) -> TradeRecord:
        """写入一条成交记录（用于部分平仓等引擎自行处理的场景）。

        ``realized_pnl_jpy`` 不为 ``None`` 时会自动生成对应的已实现盈亏流水。
        """
        now = _iso(occurred_at, self.tz)
        volume_dec = _as_decimal(volume, "volume")
        price_dec = _as_decimal(price, "price")
        pnl_dec = _opt_dec(realized_pnl_jpy, "realized_pnl_jpy")
        fee_dec = _opt_dec(fee_jpy, "fee_jpy")
        with self._transaction() as conn:
            self._account_currency(conn)
            trade, created = self._insert_trade(
                conn,
                position_id=position_id,
                symbol=symbol,
                action=action,
                side=side,
                volume=volume_dec,
                price=price_dec,
                quoted_price=_opt_dec(quoted_price, "quoted_price"),
                spread_jpy=_opt_dec(spread_jpy, "spread_jpy"),
                slippage_jpy=_opt_dec(slippage_jpy, "slippage_jpy"),
                realized_pnl_jpy=pnl_dec,
                fee_jpy=fee_dec,
                reason=reason,
                occurred_at=now,
                event_key=event_key,
                on_duplicate=OnDuplicate(on_duplicate),
            )
            if not created:
                # 幂等命中：不重复计入盈亏、不重复记账。
                return trade
            if pnl_dec is not None and pnl_dec != 0:
                self._insert_cash_flow(
                    conn,
                    flow_type=FlowType.REALIZED_PNL,
                    amount=pnl_dec,
                    event_key=f"PNL:{trade.trade_no}",
                    note=f"成交 {trade.trade_no} 已实现盈亏",
                    occurred_at=now,
                    position_id=position_id,
                    trade_no=trade.trade_no,
                )
                conn.execute(
                    "UPDATE account_state SET realized_pnl = realized_pnl + ?"
                    " WHERE account_id=?",
                    (_dec_text(pnl_dec), _ACCOUNT_ID),
                )
                self._sync_balance_projection(conn, occurred_at=now)
            self._insert_audit(
                conn,
                "TRADE_RECORDED",
                {
                    "trade_no": trade.trade_no,
                    "position_id": position_id,
                    "action": trade.action,
                    "volume": volume_dec,
                    "price": price_dec,
                    "realized_pnl_jpy": pnl_dec,
                    "event_key": event_key,
                },
                occurred_at=now,
            )
            return trade

    def query_trades(
        self,
        *,
        position_id: int | None = None,
        symbol: str | None = None,
        action: Any | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        order: str = "ASC",
    ) -> list[TradeRecord]:
        """查询交易历史。"""
        direction = "DESC" if str(order).upper() == "DESC" else "ASC"
        sql = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []
        if position_id is not None:
            sql += " AND position_id=?"
            params.append(int(position_id))
        if symbol is not None:
            sql += " AND symbol=?"
            params.append(symbol)
        if action is not None:
            sql += " AND action=?"
            params.append(_enum_value(action, TradeAction, "action"))
        if since is not None:
            sql += " AND occurred_at>=?"
            params.append(_iso(since, self.tz))
        if until is not None:
            sql += " AND occurred_at<=?"
            params.append(_iso(until, self.tz))
        sql += f" ORDER BY trade_no {direction}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._transaction(write=False) as conn:
            return [self._row_to_trade(row) for row in conn.execute(sql, params)]

    # -- 报价记录 ---------------------------------------------------------- #

    def record_quote(
        self,
        quote: Any,
        *,
        event_key: str | None = None,
        recorded_at: datetime | None = None,
        on_duplicate: Any = OnDuplicate.IGNORE,
    ) -> QuoteRecord:
        """记录一条报价。

        接受 :class:`market_data.Quote`，或任何具备
        ``symbol/bid/ask/mid/timestamp/source`` 等属性的对象。价格以十进制
        字符串落库，绝不走浮点列。
        """
        now = _iso(recorded_at, self.tz)
        bid = _as_decimal(getattr(quote, "bid"), "bid")
        ask = _as_decimal(getattr(quote, "ask"), "ask")
        mid = _as_decimal(getattr(quote, "mid"), "mid")
        if bid > ask:
            raise StorageError(f"拒绝记录非法报价：bid={bid} > ask={ask}")
        quote_type = getattr(quote, "quote_type", None)
        granularity = getattr(quote, "granularity", None)
        with self._transaction() as conn:
            if event_key is not None:
                existing = conn.execute(
                    "SELECT * FROM quotes WHERE event_key=?", (event_key,)
                ).fetchone()
                if existing is not None:
                    if OnDuplicate(on_duplicate) is OnDuplicate.ERROR:
                        raise DuplicateEventError(f"报价事件 {event_key!r} 已存在")
                    return self._row_to_quote(existing)
            cursor = conn.execute(
                "INSERT INTO quotes("
                " event_key, symbol, bid, ask, mid, quote_timestamp, source, source_url,"
                " quote_type, granularity, is_stale, recorded_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_key,
                    str(getattr(quote, "symbol")),
                    _dec_text(bid, "bid"),
                    _dec_text(ask, "ask"),
                    _dec_text(mid, "mid"),
                    _iso(getattr(quote, "timestamp"), self.tz, "quote.timestamp"),
                    str(getattr(quote, "source")),
                    getattr(quote, "source_url", None),
                    getattr(quote_type, "value", quote_type) or "unknown",
                    getattr(granularity, "value", granularity),
                    1 if getattr(quote, "is_stale", False) else 0,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM quotes WHERE quote_no=?", (cursor.lastrowid,)
            ).fetchone()
            return self._row_to_quote(row)

    @staticmethod
    def _row_to_quote(row: Mapping[str, Any]) -> QuoteRecord:
        return QuoteRecord(
            quote_no=int(row["quote_no"]),
            symbol=str(row["symbol"]),
            bid=_row_dec(row, "bid"),
            ask=_row_dec(row, "ask"),
            mid=_row_dec(row, "mid"),
            quote_timestamp=_parse_iso(row["quote_timestamp"], "quote_timestamp"),
            source=str(row["source"]),
            source_url=row["source_url"],
            quote_type=str(row["quote_type"]),
            granularity=row["granularity"],
            is_stale=bool(row["is_stale"]),
            recorded_at=_parse_iso(row["recorded_at"], "recorded_at"),
        )

    def read_quotes(
        self,
        *,
        symbol: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[QuoteRecord]:
        sql = "SELECT * FROM quotes WHERE 1=1"
        params: list[Any] = []
        if symbol is not None:
            sql += " AND symbol=?"
            params.append(symbol)
        if since is not None:
            sql += " AND quote_timestamp>=?"
            params.append(_iso(since, self.tz))
        if until is not None:
            sql += " AND quote_timestamp<=?"
            params.append(_iso(until, self.tz))
        sql += " ORDER BY quote_no ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._transaction(write=False) as conn:
            return [self._row_to_quote(row) for row in conn.execute(sql, params)]

    # -- 审计日志 ---------------------------------------------------------- #

    @staticmethod
    def _audit_digest(
        prev_hash: str, seq: int, event_type: str, occurred_at: str, payload_json: str
    ) -> str:
        material = f"{prev_hash}|{seq}|{event_type}|{occurred_at}|{payload_json}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _insert_audit(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        payload: Any,
        *,
        occurred_at: str,
    ) -> AuditEvent:
        """追加一条审计事件并接上哈希链（必须在写事务内调用）。"""
        last = conn.execute(
            "SELECT seq, hash FROM audit_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_seq = int(last["seq"]) if last is not None else 0
        prev_hash = str(last["hash"]) if last is not None else GENESIS_HASH
        seq = prev_seq + 1
        payload_json = _canonical_json(payload)
        digest = self._audit_digest(prev_hash, seq, event_type, occurred_at, payload_json)
        conn.execute(
            "INSERT INTO audit_events(seq, event_type, payload, prev_hash, hash,"
            " occurred_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (seq, event_type, payload_json, prev_hash, digest, occurred_at, occurred_at),
        )
        return AuditEvent(
            seq=seq,
            event_type=event_type,
            payload=json.loads(payload_json),
            prev_hash=prev_hash,
            hash=digest,
            occurred_at=_parse_iso(occurred_at, "occurred_at"),
            created_at=_parse_iso(occurred_at, "created_at"),
        )

    def audit(
        self, event_type: str, payload: Any | None = None, *, occurred_at: datetime | None = None
    ) -> AuditEvent:
        """追加一条自定义审计事件（只追加，不修改历史）。"""
        now = _iso(occurred_at, self.tz)
        with self._transaction() as conn:
            return self._insert_audit(
                conn, event_type, payload if payload is not None else {}, occurred_at=now
            )

    def read_audit_events(
        self,
        *,
        event_type: str | None = None,
        since_seq: int | None = None,
        limit: int | None = None,
    ) -> list[AuditEvent]:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        params: list[Any] = []
        if event_type is not None:
            sql += " AND event_type=?"
            params.append(event_type)
        if since_seq is not None:
            sql += " AND seq>?"
            params.append(int(since_seq))
        sql += " ORDER BY seq ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._transaction(write=False) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            AuditEvent(
                seq=int(row["seq"]),
                event_type=str(row["event_type"]),
                payload=json.loads(row["payload"]),
                prev_hash=str(row["prev_hash"]),
                hash=str(row["hash"]),
                occurred_at=_parse_iso(row["occurred_at"], "occurred_at"),
                created_at=_parse_iso(row["created_at"], "created_at"),
            )
            for row in rows
        ]

    def verify_audit_chain(self) -> AuditVerification:
        """重算整条审计哈希链，检查是否被篡改、删除或重排。"""
        with self._transaction(write=False) as conn:
            rows = conn.execute(
                "SELECT seq, event_type, payload, prev_hash, hash, occurred_at"
                " FROM audit_events ORDER BY seq ASC"
            ).fetchall()
        problems: list[str] = []
        prev_hash = GENESIS_HASH
        expected_seq = 1
        first_bad: int | None = None
        for row in rows:
            seq = int(row["seq"])
            if seq != expected_seq:
                problems.append(
                    f"seq 不连续：期望 {expected_seq}，实际 {seq}（存在删除或重排）"
                )
                first_bad = first_bad if first_bad is not None else seq
            payload_json = row["payload"]
            try:
                parsed = json.loads(payload_json)
            except json.JSONDecodeError:
                problems.append(f"seq={seq} 的 payload 不是合法 JSON")
                first_bad = first_bad if first_bad is not None else seq
                parsed = None
            if parsed is not None and _canonical_json(parsed) != payload_json:
                problems.append(f"seq={seq} 的 payload 不是规范形式（可能被改写）")
                first_bad = first_bad if first_bad is not None else seq
            if row["prev_hash"] != prev_hash:
                problems.append(
                    f"seq={seq} 的 prev_hash 与上一条记录的 hash 不匹配（链断裂）"
                )
                first_bad = first_bad if first_bad is not None else seq
            recomputed = self._audit_digest(
                row["prev_hash"], seq, row["event_type"], row["occurred_at"], payload_json
            )
            if recomputed != row["hash"]:
                problems.append(f"seq={seq} 的 hash 校验失败（记录被篡改）")
                first_bad = first_bad if first_bad is not None else seq
            prev_hash = str(row["hash"])
            expected_seq = seq + 1
        return AuditVerification(
            ok=not problems,
            checked=len(rows),
            head_hash=prev_hash,
            first_bad_seq=first_bad,
            problems=tuple(problems),
        )

    def audit_head(self) -> str:
        """当前审计链的头哈希。

        请把它**锚定到数据库之外**（版本库、外部只追加日志等）；只把哈希留在
        同一个数据库里无法防住「整链重写」。
        """
        with self._transaction(write=False) as conn:
            row = conn.execute(
                "SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return str(row["hash"]) if row is not None else GENESIS_HASH

    # -- 每日净值基准 ------------------------------------------------------ #

    @staticmethod
    def _row_to_baseline(row: Mapping[str, Any]) -> DailyBaseline:
        return DailyBaseline(
            trading_date=str(row["trading_date"]),
            day_start_equity=_row_dec(row, "day_start_equity"),
            day_start_balance=_row_dec(row, "day_start_balance"),
            peak_equity=_row_dec(row, "peak_equity"),
            low_equity=_row_dec(row, "low_equity"),
            drawdown_stop_triggered=bool(row["drawdown_stop_triggered"]),
            triggered_at=_parse_iso(row["triggered_at"], "triggered_at"),
            notes=row["notes"],
            created_at=_parse_iso(row["created_at"], "created_at"),
            updated_at=_parse_iso(row["updated_at"], "updated_at"),
        )

    def ensure_daily_baseline(
        self,
        trading_date: str | date,
        *,
        equity: Decimal | int | str,
        balance: Decimal | int | str,
        notes: str | None = None,
    ) -> DailyBaseline:
        """确保当日基准存在。**同一天只创建一次**，重复调用不会覆盖当日基准。"""
        day = trading_date.isoformat() if isinstance(trading_date, date) else str(trading_date)
        now = _iso(None, self.tz)
        equity_dec = _as_decimal(equity, "equity")
        balance_dec = _as_decimal(balance, "balance")
        with self._transaction() as conn:
            self._account_currency(conn)
            existing = conn.execute(
                "SELECT * FROM daily_equity_baseline WHERE trading_date=?", (day,)
            ).fetchone()
            if existing is not None:
                return self._row_to_baseline(existing)
            conn.execute(
                "INSERT INTO daily_equity_baseline("
                " trading_date, account_id, day_start_equity, day_start_balance,"
                " peak_equity, low_equity, drawdown_stop_triggered, notes,"
                " created_at, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    day,
                    _ACCOUNT_ID,
                    _dec_text(equity_dec, "day_start_equity"),
                    _dec_text(balance_dec, "day_start_balance"),
                    _dec_text(equity_dec, "peak_equity"),
                    _dec_text(equity_dec, "low_equity"),
                    notes,
                    now,
                    now,
                ),
            )
            self._insert_audit(
                conn,
                "DAILY_BASELINE_CREATED",
                {
                    "trading_date": day,
                    "day_start_equity": equity_dec,
                    "day_start_balance": balance_dec,
                },
                occurred_at=now,
            )
            row = conn.execute(
                "SELECT * FROM daily_equity_baseline WHERE trading_date=?", (day,)
            ).fetchone()
            return self._row_to_baseline(row)

    def update_daily_baseline(
        self,
        trading_date: str | date,
        *,
        equity: Decimal | int | str | None = None,
        drawdown_stop_triggered: bool | None = None,
        triggered_at: datetime | None = None,
        notes: str | None = None,
    ) -> DailyBaseline:
        """更新当日基准的极值与触发标记。

        ``peak_equity`` / ``low_equity`` 只会被**扩大**（取更大/更小值），
        不会被较小的值覆盖，符合极值语义。
        """
        day = trading_date.isoformat() if isinstance(trading_date, date) else str(trading_date)
        now = _iso(None, self.tz)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM daily_equity_baseline WHERE trading_date=?", (day,)
            ).fetchone()
            if row is None:
                raise StorageError(f"当日基准不存在: {day}（请先调用 ensure_daily_baseline）")
            peak = _row_dec(row, "peak_equity")
            low = _row_dec(row, "low_equity")
            if equity is not None:
                current = _as_decimal(equity, "equity")
                peak = max(peak, current)
                low = min(low, current)
            triggered = (
                bool(row["drawdown_stop_triggered"])
                if drawdown_stop_triggered is None
                else bool(drawdown_stop_triggered)
            )
            triggered_text = (
                _iso(triggered_at, self.tz)
                if triggered_at is not None
                else row["triggered_at"]
            )
            conn.execute(
                "UPDATE daily_equity_baseline SET peak_equity=?, low_equity=?,"
                " drawdown_stop_triggered=?, triggered_at=?, notes=COALESCE(?, notes),"
                " updated_at=? WHERE trading_date=?",
                (
                    _dec_text(peak, "peak_equity"),
                    _dec_text(low, "low_equity"),
                    1 if triggered else 0,
                    triggered_text,
                    notes,
                    now,
                    day,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM daily_equity_baseline WHERE trading_date=?", (day,)
            ).fetchone()
            return self._row_to_baseline(updated)

    def read_daily_baseline(self, trading_date: str | date) -> DailyBaseline | None:
        day = trading_date.isoformat() if isinstance(trading_date, date) else str(trading_date)
        with self._transaction(write=False) as conn:
            row = conn.execute(
                "SELECT * FROM daily_equity_baseline WHERE trading_date=?", (day,)
            ).fetchone()
            return None if row is None else self._row_to_baseline(row)

    # -- 定时任务运行记录 -------------------------------------------------- #

    @staticmethod
    def _row_to_run(row: Mapping[str, Any]) -> SchedulerRun:
        return SchedulerRun(
            run_no=int(row["run_no"]),
            task_name=str(row["task_name"]),
            status=str(row["status"]),
            started_at=_parse_iso(row["started_at"], "started_at"),
            finished_at=_parse_iso(row["finished_at"], "finished_at"),
            detail=row["detail"],
            error=row["error"],
            created_at=_parse_iso(row["created_at"], "created_at"),
            updated_at=_parse_iso(row["updated_at"], "updated_at"),
        )

    def start_scheduler_run(
        self,
        task_name: str,
        *,
        detail: str | None = None,
        event_key: str | None = None,
        started_at: datetime | None = None,
        on_duplicate: Any = OnDuplicate.IGNORE,
    ) -> SchedulerRun:
        """开始一次定时任务运行（状态 ``RUNNING``）。"""
        now = _iso(started_at, self.tz)
        with self._transaction() as conn:
            if event_key is not None:
                existing = conn.execute(
                    "SELECT * FROM scheduler_runs WHERE event_key=?", (event_key,)
                ).fetchone()
                if existing is not None:
                    if OnDuplicate(on_duplicate) is OnDuplicate.ERROR:
                        raise DuplicateEventError(f"任务运行事件 {event_key!r} 已存在")
                    return self._row_to_run(existing)
            cursor = conn.execute(
                "INSERT INTO scheduler_runs("
                " event_key, task_name, status, started_at, detail, created_at, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                (event_key, task_name, RunStatus.RUNNING.value, now, detail, now, now),
            )
            row = conn.execute(
                "SELECT * FROM scheduler_runs WHERE run_no=?", (cursor.lastrowid,)
            ).fetchone()
            return self._row_to_run(row)

    def finish_scheduler_run(
        self,
        run_no: int,
        *,
        status: Any = RunStatus.SUCCESS,
        detail: str | None = None,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> SchedulerRun:
        """结束一次任务运行。已经是终态的运行不会被改写。"""
        now = _iso(finished_at, self.tz)
        status_value = _enum_value(status, RunStatus, "status")
        if status_value == RunStatus.RUNNING.value:
            raise StorageError("结束任务运行时 status 不能是 RUNNING")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM scheduler_runs WHERE run_no=?", (int(run_no),)
            ).fetchone()
            if row is None:
                raise StorageError(f"任务运行记录不存在: {run_no}")
            if row["status"] != RunStatus.RUNNING.value:
                # 终态不可改写：重复结束是幂等的
                return self._row_to_run(row)
            conn.execute(
                "UPDATE scheduler_runs SET status=?, finished_at=?, detail=COALESCE(?, detail),"
                " error=?, updated_at=? WHERE run_no=? AND status=?",
                (status_value, now, detail, error, now, int(run_no), RunStatus.RUNNING.value),
            )
            updated = conn.execute(
                "SELECT * FROM scheduler_runs WHERE run_no=?", (int(run_no),)
            ).fetchone()
            return self._row_to_run(updated)

    def read_scheduler_runs(
        self,
        *,
        task_name: str | None = None,
        status: Any | None = None,
        limit: int | None = None,
        order: str = "DESC",
    ) -> list[SchedulerRun]:
        direction = "ASC" if str(order).upper() == "ASC" else "DESC"
        sql = "SELECT * FROM scheduler_runs WHERE 1=1"
        params: list[Any] = []
        if task_name is not None:
            sql += " AND task_name=?"
            params.append(task_name)
        if status is not None:
            sql += " AND status=?"
            params.append(_enum_value(status, RunStatus, "status"))
        sql += f" ORDER BY run_no {direction}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._transaction(write=False) as conn:
            return [self._row_to_run(row) for row in conn.execute(sql, params)]

    def recover_aborted_runs(self, *, task_name: str | None = None) -> int:
        """程序重启恢复：把遗留的 ``RUNNING`` 运行标记为 ``ABORTED``。

        进程崩溃后不可能再有 RUNNING 的运行真正在执行，因此在启动时调用本方法
        可以清理「不完整」的任务记录。返回被标记的条数。
        """
        now = _iso(None, self.tz)
        with self._transaction() as conn:
            sql = "UPDATE scheduler_runs SET status=?, finished_at=?, error=?, updated_at=?" \
                  " WHERE status=?"
            params: list[Any] = [
                RunStatus.ABORTED.value,
                now,
                "进程重启后清理：该运行未正常结束",
                now,
                RunStatus.RUNNING.value,
            ]
            if task_name is not None:
                sql += " AND task_name=?"
                params.append(task_name)
            cursor = conn.execute(sql, params)
            count = int(cursor.rowcount)
            if count:
                self._insert_audit(
                    conn,
                    "SCHEDULER_RUNS_RECOVERED",
                    {"aborted_count": count, "task_name": task_name},
                    occurred_at=now,
                )
            return count

    # -- AI 笔记 ------------------------------------------------------------ #

    def record_note(
        self,
        kind: str,
        content: str,
        *,
        tags: Sequence[str] | None = None,
        decision_id: str | None = None,
        trading_date: str | None = None,
        author: str = "ai",
        event_key: str | None = None,
        occurred_at: datetime | None = None,
        on_duplicate: str = "RETURN_EXISTING",
    ) -> tuple[AgentNote, bool]:
        """追加一条 AI 笔记。返回 ``(记录, 是否新建)``。

        ``event_key`` 给定时具备**幂等性**：同一个键重复写入返回已有记录且
        ``created=False``，不会产生第二条。笔记同时接进审计哈希链（事件类型
        ``AGENT_NOTE``），因此「模型说过什么」和账本一样是可核验的。

        ``kind`` 由调用方约定语义（本层只做长度与空白校验），这样存储层不必
        耦合到具体的工具定义。
        """
        kind_text = str(kind or "").strip()
        if not kind_text:
            raise StorageError("笔记 kind 不能为空")
        if len(kind_text) > 40:
            raise StorageError(f"笔记 kind 过长（≤40 字符）：{kind_text!r}")
        body = str(content or "").strip()
        if not body:
            raise StorageError("笔记内容不能为空")
        if len(body) > MAX_NOTE_CHARS:
            raise StorageError(f"笔记内容过长（≤{MAX_NOTE_CHARS} 字符）：{len(body)}")
        tag_list = tuple(
            dict.fromkeys(
                str(t).strip() for t in (tags or ()) if str(t).strip()
            )
        )
        now = _iso(occurred_at, self.tz)
        with self._transaction() as conn:
            if event_key is not None:
                row = conn.execute(
                    "SELECT * FROM agent_notes WHERE event_key=?", (event_key,)
                ).fetchone()
                if row is not None:
                    if on_duplicate == "ERROR":
                        raise DuplicateEventError(f"笔记事件键已存在：{event_key}")
                    return _note_from_row(row, self.tz), False
            try:
                cursor = conn.execute(
                    "INSERT INTO agent_notes(event_key, kind, content, tags, decision_id,"
                    " trading_date, author, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_key,
                        kind_text,
                        body,
                        json.dumps(list(tag_list), ensure_ascii=False) if tag_list else None,
                        decision_id,
                        trading_date,
                        author,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateEventError(f"笔记事件键冲突：{event_key}") from exc
            note_no = int(cursor.lastrowid)
            self._insert_audit(
                conn,
                "AGENT_NOTE",
                {
                    "note_no": note_no,
                    "kind": kind_text,
                    "content": body,
                    "tags": list(tag_list),
                    "decision_id": decision_id,
                    "trading_date": trading_date,
                    "author": author,
                },
                occurred_at=now,
            )
            row = conn.execute(
                "SELECT * FROM agent_notes WHERE note_no=?", (note_no,)
            ).fetchone()
        return _note_from_row(row, self.tz), True

    def read_notes(
        self,
        *,
        kind: str | None = None,
        limit: int | None = None,
        newest_first: bool = True,
    ) -> list[AgentNote]:
        """按时间读笔记。``newest_first=False`` 时按时间正序（喂给模型用）。"""
        sql = "SELECT * FROM agent_notes WHERE 1=1"
        params: list[Any] = []
        if kind is not None:
            sql += " AND kind=?"
            params.append(str(kind))
        sql += " ORDER BY note_no " + ("DESC" if newest_first else "ASC")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._transaction(write=False) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_note_from_row(row, self.tz) for row in rows]

    def count_notes(self) -> int:
        with self._transaction(write=False) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM agent_notes").fetchone()
        return int(row["n"])

    # -- 逐小时对话存档 ---------------------------------------------------- #

    def record_turn(
        self,
        hour_key: str,
        decision_id: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        day_key: str | None = None,
        outcome: str | None = None,
        summary: str | None = None,
        event_key: str | None = None,
        occurred_at: datetime | None = None,
    ) -> tuple[AgentTurn, bool]:
        """把一次决策的完整消息存档进 ``agent_turns``（按小时分桶）。

        同一个 ``decision_id`` 重复写入是幂等的（默认键为
        ``turn:<hour_key>:<decision_id>``），因此重跑同一次决策不会让历史翻倍。
        """
        hour_text = str(hour_key or "").strip()
        if not hour_text:
            raise StorageError("hour_key 不能为空")
        day_text = str(day_key or hour_text[:10]).strip()
        payload = json.dumps([dict(m) for m in messages], ensure_ascii=False)
        key = event_key or f"turn:{hour_text}:{decision_id}"
        now = _iso(occurred_at, self.tz)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM agent_turns WHERE event_key=?", (key,)
            ).fetchone()
            if row is not None:
                return _turn_from_row(row, self.tz), False
            try:
                cursor = conn.execute(
                    "INSERT INTO agent_turns(event_key, hour_key, day_key, decision_id,"
                    " outcome, messages, summary, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, hour_text, day_text, decision_id, outcome, payload, summary, now),
                )
            except sqlite3.IntegrityError as exc:  # pragma: no cover - 并发下的兜底
                raise DuplicateEventError(f"对话存档键冲突：{key}") from exc
            turn_no = int(cursor.lastrowid)
            row = conn.execute(
                "SELECT * FROM agent_turns WHERE turn_no=?", (turn_no,)
            ).fetchone()
        return _turn_from_row(row, self.tz), True

    def read_turns(
        self, *, hour_key: str | None = None, day_key: str | None = None,
        limit: int | None = None,
    ) -> list[AgentTurn]:
        """读对话存档，按**小时 → 写入顺序**正序（重放给模型用，顺序不能反）。

        第一关键字用 ``hour_key`` 而不是 ``turn_no``：补录过去某个小时时，
        ``turn_no`` 是「写入顺序」，而重放要的是「时间顺序」。
        """
        sql = "SELECT * FROM agent_turns WHERE 1=1"
        params: list[Any] = []
        if hour_key is not None:
            sql += " AND hour_key=?"
            params.append(str(hour_key))
        if day_key is not None:
            sql += " AND day_key=?"
            params.append(str(day_key))
        sql += " ORDER BY hour_key ASC, turn_no ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._transaction(write=False) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_turn_from_row(row, self.tz) for row in rows]

    def list_turn_hours(self) -> list[str]:
        """所有有存档的小时键，按时间正序。"""
        with self._transaction(write=False) as conn:
            rows = conn.execute(
                "SELECT DISTINCT hour_key FROM agent_turns ORDER BY hour_key ASC"
            ).fetchall()
        return [str(row["hour_key"]) for row in rows]

    def count_turns(self) -> int:
        with self._transaction(write=False) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM agent_turns").fetchone()
        return int(row["n"])

    def record_hour_summary(
        self,
        hour_key: str,
        summary: str,
        *,
        day_key: str | None = None,
        turn_count: int = 0,
        stats: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> tuple[HourSummary, bool]:
        """写入（或覆盖）某个小时的压缩摘要。

        摘要是**派生数据**，允许覆盖：同一个小时被重复压缩时以最后一次为准，
        但 ``agent_turns`` 里的原始对话不会被删——摘要永远可以重算。
        """
        hour_text = str(hour_key or "").strip()
        if not hour_text:
            raise StorageError("hour_key 不能为空")
        day_text = str(day_key or hour_text[:10]).strip()
        body = str(summary or "").strip()
        if not body:
            raise StorageError("小时摘要不能为空")
        stats_json = _canonical_json(dict(stats or {}))
        now = _iso(occurred_at, self.tz)
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT created_at FROM hour_summaries WHERE hour_key=?", (hour_text,)
            ).fetchone()
            created = existing is None
            conn.execute(
                "INSERT INTO hour_summaries(hour_key, day_key, turn_count, summary,"
                " stats, created_at) VALUES(?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(hour_key) DO UPDATE SET"
                " day_key=excluded.day_key, turn_count=excluded.turn_count,"
                " summary=excluded.summary, stats=excluded.stats,"
                " created_at=excluded.created_at",
                (hour_text, day_text, int(turn_count), body, stats_json, now),
            )
            self._insert_audit(
                conn,
                "AGENT_HOUR_SUMMARY",
                {
                    "hour_key": hour_text,
                    "day_key": day_text,
                    "turn_count": int(turn_count),
                    "summary": body,
                    "stats": dict(stats or {}),
                },
                occurred_at=now,
            )
            row = conn.execute(
                "SELECT * FROM hour_summaries WHERE hour_key=?", (hour_text,)
            ).fetchone()
        return _hour_from_row(row, self.tz), created

    def read_hour_summary(self, hour_key: str) -> HourSummary | None:
        with self._transaction(write=False) as conn:
            row = conn.execute(
                "SELECT * FROM hour_summaries WHERE hour_key=?", (str(hour_key),)
            ).fetchone()
        return None if row is None else _hour_from_row(row, self.tz)

    def read_hour_summaries(
        self, *, day_key: str | None = None, limit: int | None = None,
        newest_first: bool = True,
    ) -> list[HourSummary]:
        sql = "SELECT * FROM hour_summaries WHERE 1=1"
        params: list[Any] = []
        if day_key is not None:
            sql += " AND day_key=?"
            params.append(str(day_key))
        sql += " ORDER BY hour_key " + ("DESC" if newest_first else "ASC")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._transaction(write=False) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_hour_from_row(row, self.tz) for row in rows]

    def list_hour_summary_keys(self) -> list[str]:
        with self._transaction(write=False) as conn:
            rows = conn.execute(
                "SELECT hour_key FROM hour_summaries ORDER BY hour_key ASC"
            ).fetchall()
        return [str(row["hour_key"]) for row in rows]

    # -- 核对与校验 -------------------------------------------------------- #

    def verify_ledger(self) -> LedgerVerification:
        """校验账本自身的一致性：

        * 逐条重算 ``balance_after`` 是否等于到该条为止的流水累计；
        * 每笔 ``REALIZED_PNL`` 流水是否都能对应到一条已平仓的成交记录。
        """
        problems: list[str] = []
        with self._transaction(write=False) as conn:
            rows = conn.execute("SELECT * FROM cash_flows ORDER BY flow_no ASC").fetchall()
            trade_pnls = {
                int(row["trade_no"]): _as_decimal(row["realized_pnl_jpy"])
                for row in conn.execute(
                    "SELECT trade_no, realized_pnl_jpy FROM trades"
                    " WHERE realized_pnl_jpy IS NOT NULL"
                )
            }
        running = Decimal("0")
        for row in rows:
            amount = _as_decimal(row["amount"], "amount")
            running += amount
            recorded = _as_decimal(row["balance_after"], "balance_after")
            if recorded != running:
                problems.append(
                    f"flow_no={row['flow_no']} 的 balance_after={recorded} 与累计值 "
                    f"{running} 不一致"
                )
            if row["flow_type"] == FlowType.REALIZED_PNL.value:
                trade_no = row["trade_no"]
                if trade_no is None:
                    problems.append(f"flow_no={row['flow_no']} 是已实现盈亏但未关联成交记录")
                elif int(trade_no) not in trade_pnls:
                    problems.append(
                        f"flow_no={row['flow_no']} 关联的成交 {trade_no} 不存在或无盈亏金额"
                    )
                elif trade_pnls[int(trade_no)] != amount:
                    problems.append(
                        f"flow_no={row['flow_no']} 的盈亏 {amount} 与成交 {trade_no} 的 "
                        f"{trade_pnls[int(trade_no)]} 不一致"
                    )
        return LedgerVerification(
            ok=not problems,
            flow_count=len(rows),
            ledger_balance=running,
            problems=tuple(problems),
        )

    def reconcile_account(
        self, *, strict: bool = False, include_balance_chain: bool = True
    ) -> ReconciliationReport:
        """根据交易与资金流水重新核对现金余额与净值。

        只报告、不自动改写数据（避免把真实不一致悄悄抹平）。
        ``strict=True`` 时不一致会抛 :class:`ReconciliationError`。
        """
        problems: list[str] = []
        with self._transaction(write=False) as conn:
            state_row = conn.execute(
                "SELECT * FROM account_state WHERE account_id=?", (_ACCOUNT_ID,)
            ).fetchone()
            if state_row is None:
                raise StorageError("账户尚未初始化，请先调用 initialize_account()")
            ledger_balance = self._ledger_sum(conn)
            flow_count = int(
                conn.execute("SELECT COUNT(*) AS n FROM cash_flows").fetchone()["n"]
            )
            trade_count = int(conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"])
            pnl_from_trades = Decimal("0")
            for row in conn.execute(
                "SELECT realized_pnl_jpy FROM trades WHERE realized_pnl_jpy IS NOT NULL"
            ):
                pnl_from_trades += _as_decimal(row["realized_pnl_jpy"], "realized_pnl_jpy")
            pnl_from_flows = Decimal("0")
            for row in conn.execute(
                "SELECT amount FROM cash_flows WHERE flow_type=?",
                (FlowType.REALIZED_PNL.value,),
            ):
                pnl_from_flows += _as_decimal(row["amount"], "amount")
            open_rows = conn.execute(
                "SELECT margin_jpy FROM positions WHERE status=?", (PositionStatus.OPEN.value,)
            ).fetchall()
            open_margin = Decimal("0")
            for row in open_rows:
                open_margin += _as_decimal(row["margin_jpy"], "margin_jpy")
            stored_balance = _as_decimal(state_row["cash_balance"], "cash_balance")
            stored_equity = _as_decimal(state_row["equity"], "equity")
            unrealized = _as_decimal(state_row["unrealized_pnl"], "unrealized_pnl")

        balance_diff = stored_balance - ledger_balance
        pnl_diff = pnl_from_flows - pnl_from_trades
        if balance_diff != 0:
            problems.append(
                f"现金余额不一致：account_state={stored_balance}，账本流水之和={ledger_balance}"
                f"（差额 {balance_diff}）"
            )
        if pnl_diff != 0:
            problems.append(
                f"已实现盈亏不一致：流水合计={pnl_from_flows}，成交记录合计={pnl_from_trades}"
                f"（差额 {pnl_diff}）"
            )
        expected_equity = ledger_balance + unrealized
        if stored_equity != expected_equity:
            problems.append(
                f"净值不一致：account_state={stored_equity}，"
                f"账本余额 {ledger_balance} + 浮动盈亏 {unrealized} = {expected_equity}"
            )
        if include_balance_chain:
            ledger_check = self.verify_ledger()
            problems.extend(ledger_check.problems)

        report = ReconciliationReport(
            ok=not problems,
            checked_at=datetime.now(self.tz),
            ledger_balance=ledger_balance,
            stored_balance=stored_balance,
            balance_difference=balance_diff,
            realized_pnl_from_trades=pnl_from_trades,
            realized_pnl_from_flows=pnl_from_flows,
            pnl_difference=pnl_diff,
            flow_count=flow_count,
            trade_count=trade_count,
            open_position_count=len(open_rows),
            open_position_margin=open_margin,
            problems=tuple(problems),
        )
        if strict and not report.ok:
            raise ReconciliationError("账户核对失败：" + "；".join(report.problems))
        return report

    def integrity_check(self) -> tuple[str, tuple[str, ...]]:
        """运行 SQLite 自带的 ``integrity_check`` 与外键检查。"""
        with self._transaction(write=False) as conn:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            integrity = "; ".join(str(row[0]) for row in rows)
            fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        violations = tuple(
            f"表 {row[0]} 行 {row[1]} 违反外键 {row[2]} → {row[3]}" for row in fk_rows
        )
        return integrity, violations

    def verify_integrity(self) -> IntegrityReport:
        """整体完整性检查：SQLite 完整性 + 外键 + 审计链 + 账本 + 账户核对。"""
        integrity, fk_violations = self.integrity_check()
        audit = self.verify_audit_chain()
        ledger = self.verify_ledger()
        reconciliation = self.reconcile_account(include_balance_chain=False)
        problems: list[str] = []
        if integrity != "ok":
            problems.append(f"SQLite integrity_check: {integrity}")
        problems.extend(fk_violations)
        problems.extend(f"审计链: {p}" for p in audit.problems)
        problems.extend(f"账本: {p}" for p in ledger.problems)
        problems.extend(f"核对: {p}" for p in reconciliation.problems)
        with self._transaction(write=False) as conn:
            version_row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        return IntegrityReport(
            ok=not problems,
            schema_version=int(version_row["value"]) if version_row else SCHEMA_VERSION,
            sqlite_integrity=integrity,
            foreign_key_violations=fk_violations,
            audit=audit,
            ledger=ledger,
            reconciliation=reconciliation,
            problems=tuple(problems),
        )

    # -- 备份 -------------------------------------------------------------- #

    def backup(self, dest: str | Path | None = None, *, verify: bool = True) -> Path:
        """用 SQLite 在线备份 API 生成一致性备份，并可校验备份文件。

        在线备份在 WAL 模式下同样安全：它会取得读锁并复制一致快照。
        """
        if dest is None:
            stamp = datetime.now(self.tz).strftime("%Y%m%dT%H%M%S")
            target = self.db_path.parent / "backups" / f"{self.db_path.stem}-{stamp}.sqlite3"
        else:
            target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = self._connect()
        try:
            dest_conn = sqlite3.connect(target)
            try:
                source.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            source.close()
        if verify:
            self._verify_backup(target)
        return target

    def _verify_backup(self, target: Path) -> None:
        """校验备份：SQLite 完整性、schema 版本、账本余额与源库一致。"""
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
        try:
            integrity = "; ".join(str(row[0]) for row in conn.execute("PRAGMA integrity_check"))
            if integrity != "ok":
                raise StorageError(f"备份文件完整性检查失败: {target} → {integrity}")
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None or int(row["value"]) != SCHEMA_VERSION:
                raise StorageError(f"备份文件 schema 版本异常: {target}")
            total = Decimal("0")
            for item in conn.execute("SELECT amount FROM cash_flows"):
                total += _as_decimal(item["amount"], "amount")
            expected = self.ledger_balance()
            if total != expected:
                raise StorageError(
                    f"备份文件账本余额 {total} 与源库 {expected} 不一致: {target}"
                )
        finally:
            conn.close()

    # -- 便捷统计 ---------------------------------------------------------- #

    def counts(self) -> dict[str, int]:
        """各表行数，便于快速排查。"""
        tables = (
            "positions",
            "trades",
            "cash_flows",
            "quotes",
            "audit_events",
            "daily_equity_baseline",
            "scheduler_runs",
            "agent_notes",
            "agent_turns",
            "hour_summaries",
        )
        with self._transaction(write=False) as conn:
            result = {name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
                      for name in tables}
            open_count = conn.execute(
                "SELECT COUNT(*) AS n FROM positions WHERE status=?",
                (PositionStatus.OPEN.value,),
            ).fetchone()["n"]
        result["positions_open"] = int(open_count)
        return result


def open_storage(cfg: Config | None = None, **kwargs: Any) -> Storage:
    """按配置打开（必要时创建）数据库。"""
    return Storage.from_config(cfg, **kwargs)


# --------------------------------------------------------------------------- #
# 命令行自检
# --------------------------------------------------------------------------- #


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m storage",
        description="初始化/检查虚拟账户数据库（不会执行任何真实交易）。",
    )
    parser.add_argument("--db", default=None, help="数据库路径（默认取 config.json）")
    parser.add_argument("--initialize", action="store_true", help="若未初始化则发放初始本金")
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")
    parser.add_argument("--backup", action="store_true", help="生成并校验一份备份")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001 - CLI 需要把所有配置错误友好呈现
        print(f"[配置错误] {exc}")
        return 1

    db_path = Path(args.db) if args.db else cfg.paths.database_path
    try:
        storage = Storage(db_path, tz=cfg.tz)
        if args.initialize and not storage.is_initialized():
            state = storage.initialize_account(cfg.account.initial_capital_jpy, currency=cfg.account.currency)
            print(f"已初始化账户，初始本金 {cfg.account.initial_capital_jpy} {cfg.account.currency}")
            print(f"账户状态 revision={state.revision} cash_balance={state.cash_balance}")
        elif args.initialize:
            state = storage.read_account_state()
            print("账户已初始化（未重复发放本金）")
            print(f"账户状态 revision={state.revision} cash_balance={state.cash_balance}")
        aborted = storage.recover_aborted_runs()
        report = storage.verify_integrity()
    except StorageError as exc:
        print(f"[存储错误] {type(exc).__name__}: {exc}")
        return 1

    if args.as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"数据库          : {db_path}")
        print(f"表行数          : {storage.counts()}")
        print(f"审计链          : 检查 {report.audit.checked} 条，"
              f"{'通过' if report.audit.ok else '异常'}，头哈希 {report.audit.head_hash[:16]}…")
        print(f"账本            : {report.ledger.flow_count} 条流水，"
              f"余额 {report.ledger.ledger_balance}")
        print(f"账户核对        : {'通过' if report.reconciliation.ok else '不一致'}")
        for problem in report.problems:
            print(f"  [问题] {problem}")
        if aborted:
            print(f"重启恢复        : 已把 {aborted} 条遗留 RUNNING 运行标记为 ABORTED")
        print(f"整体完整性      : {'OK' if report.ok else 'FAILED'}")

    if args.backup:
        target = storage.backup()
        print(f"备份            : {target}（已校验）")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
