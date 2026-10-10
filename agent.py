"""AI 交易决策循环（function calling）。

职责边界
========

把**事实**（账户、行情、风控限额）交给语言模型，模型通过**工具调用**提交决策，
再由 :mod:`engine` 重新校验；通过才执行，被拒则把拒绝原因回灌给模型让它修正，
最多循环若干轮。

它**不**做这些事：

* 不解析自由文本——决策只能通过工具调用来表达（见 :mod:`llm_client` 的说明）；
* 不绕过风控——模型的输出必须通过 :meth:`engine.TradingEngine.validate_trade_intent`，
  且执行时 :meth:`engine.TradingEngine.execute_virtual_trade` 会**再校验一次**；
* 不允许模型指定成交价、保证金、盈亏或余额——工具参数只有
  ``side / volume_usd / stop_loss / take_profit / reason``，
  成交价由引擎按规则从报价推导；
* 默认**不真的开仓**：``dry_run=True`` 时只返回决策与校验结果。

上下文与缓存
============

每次决策的消息分成两段，顺序固定：

1. **稳定前缀**（tools + system 消息）：角色、规则、风控限额、工具约定。这些内容
   只由 ``config.json`` 决定，同一进程内**逐字节不变**，因此可以命中服务端的前缀
   缓存（KV cache）。稳定内容放在前缀里几乎不花钱，所以规则可以写得比较全。
2. **易变尾部**（user 消息）：时刻、行情、账户、持仓、最近成交、自己的笔记。
   每次决策都不一样，注定无法缓存，所以刻意保持精简。

**改 system 提示词或工具描述等于让缓存整体失效**，改动时要有意识。有测试
（``test_agent.TestContextCache``）断言稳定前缀在行情/时刻变化时逐字节不变。

幂等
====

``decision_id`` 决定 ``intent_id``（``ai:<decision_id>``）。同一个 ``decision_id``
重复运行不会重复开仓——第二次会被引擎判定为重复指令并返回既有持仓。

记忆（笔记）
============

模型可以用 ``remember_note`` 工具把自己的心得、决策理由写进数据库
（``storage.agent_notes``），之后每次决策都会按时间正序回灌给它。
笔记是**模型自己写的文本，不是账本事实**，回灌时会明确标注，避免模型把自己的
旧猜测当成当前行情。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

from config import Config, ConfigError, load_config
from engine import (
    CloseExecution,
    CloseReason,
    EngineError,
    PositionSide,
    TradeExecution,
    TradeIntent,
    TradeRejectedError,
    TradeValidation,
    TradingEngine,
    trading_date_for,
)
from llm_client import (
    ChatResponse,
    LLMClient,
    LLMConfigError,
    LLMEmptyCompletionError,
    LLMError,
    ToolCall,
    assistant_message,
    system_message,
    tool_result_message,
    tool_schema,
    user_message,
)
from market_data import (
    HistoryUnavailableError,
    HourlyRange,
    MarketDataError,
    Quote,
    get_current_quote,
    get_hourly_ranges,
    hour_start,
)
from reporting import build_report_data
from storage import (
    MAX_NOTE_CHARS,
    AgentNote,
    AgentTurn,
    HourSummary,
    PositionStatus,
    Storage,
    StorageError,
)

__all__ = [
    "AGENT_TOOLS",
    "CLOSE_POSITION_TOOL",
    "DEFAULT_HISTORY_HOURS",
    "DEFAULT_MAX_HOUR_SUMMARIES",
    "DEFAULT_MAX_NOTES_IN_CONTEXT",
    "DecisionResult",
    "HOLD_TOOL",
    "NOTE_KINDS",
    "REMEMBER_NOTE_TOOL",
    "SUBMIT_INTENT_TOOL",
    "SYSTEM_NOTE_KINDS",
    "ToolInvocation",
    "TradingAgent",
    "build_decision_context",
    "build_static_context",
    "build_system_prompt",
    "hour_key_for",
    "summarize_hour",
]

logger = logging.getLogger("kurumi_fx_sim.agent")

#: 默认最大循环轮数（含被拒后的修正重试）。
DEFAULT_MAX_ROUNDS: Final[int] = 3

#: 每次决策回灌给模型的笔记条数上限（取最近 N 条）。笔记是易变内容，
#: 必须封顶，否则模型可以靠不断写笔记把自己的上下文顶爆。
DEFAULT_MAX_NOTES_IN_CONTEXT: Final[int] = 8

#: 注入上下文的「价格区间」取最近多少个自然小时。
DEFAULT_HISTORY_HOURS: Final[int] = 12

#: 注入上下文的「已压缩小时摘要」条数上限。
DEFAULT_MAX_HOUR_SUMMARIES: Final[int] = 6

#: 由**系统**（不是模型）写入的笔记类别：压缩后的长期记忆。
SYSTEM_NOTE_KINDS: Final[tuple[str, ...]] = ("HOUR_SUMMARY", "DAY_SUMMARY")

#: 决策结果取值。
OUTCOME_EXECUTED: Final[str] = "EXECUTED"        # 已真实开仓/平仓
OUTCOME_VALIDATED: Final[str] = "VALIDATED"      # 校验通过但 dry-run，未落单
OUTCOME_HELD: Final[str] = "HELD"                # 模型主动决定不交易
OUTCOME_NO_DECISION: Final[str] = "NO_DECISION"  # 模型没调用任何工具
OUTCOME_DUPLICATE: Final[str] = "DUPLICATE"    # 同一 decision_id 重复运行，未重复开仓
OUTCOME_ERROR: Final[str] = "ERROR"              # 调用或解析失败

_AUDIT_TYPE: Final[str] = "AI_DECISION"


# --------------------------------------------------------------------------- #
# 工具声明
# --------------------------------------------------------------------------- #

SUBMIT_INTENT_TOOL: Final[dict[str, Any]] = tool_schema(
    "submit_trade_intent",
    "提交一笔开仓决策。数量必须是最小交易单位的正整数倍；必须同时给出止损与止盈，"
    "且止盈距离至少是止损距离的下限倍数；止损风险与保证金占用不得超过 system 消息里"
    "给出的限额。校验不通过时会把原因返回给你，请据此修正。",
    {
        "side": {"type": "string", "enum": ["LONG", "SHORT"], "description": "做多或做空"},
        "volume_usd": {"type": "number", "description": "美元数量，最小交易单位的整数倍"},
        "stop_loss": {"type": "number", "description": "止损价（多仓低于开仓价，空仓高于开仓价）"},
        "take_profit": {"type": "number", "description": "止盈价（多仓高于开仓价，空仓低于开仓价）"},
        "reason": {"type": "string", "description": "决策理由，一句话，会写入成交记录与审计"},
    },
    required=["side", "volume_usd", "stop_loss", "take_profit", "reason"],
)

CLOSE_POSITION_TOOL: Final[dict[str, Any]] = tool_schema(
    "close_position",
    "平掉当前持仓（最多只有一笔）。用于主动止盈止损或改变看法。",
    {"reason": {"type": "string", "description": "平仓理由，一句话"}},
    required=["reason"],
)

HOLD_TOOL: Final[dict[str, Any]] = tool_schema(
    "hold",
    "决定本轮不交易。行情不明、风险过大或已有持仓且不想调整时使用。",
    {"reason": {"type": "string", "description": "不交易的理由，一句话"}},
    required=["reason"],
)

#: 笔记类别。限制成枚举是为了让「心得」可检索，而不是一堆自由文本。
NOTE_KINDS: Final[tuple[str, ...]] = (
    "LESSON",          # 从一次盈亏里学到的教训
    "DECISION_REASON",  # 本次为什么这样决策（尤其是没开仓的原因）
    "MARKET_VIEW",     # 对当前行情结构的看法
    "MISTAKE",         # 事后确认的判断错误
    "PLAN",            # 打算在什么条件下行动
)

#: 单条笔记最多允许的标签数。
MAX_NOTE_TAGS: Final[int] = 5

REMEMBER_NOTE_TOOL: Final[dict[str, Any]] = tool_schema(
    "remember_note",
    "把心得、决策理由或观察写进长期记忆。写下的笔记会在**之后每一次决策**里回灌给你"
    "（按时间正序，只保留最近若干条），所以只写对将来有用的东西——例如「这次为什么"
    "没开仓」「某个价位区间的教训」。"
    "调用本工具**不会**结束本轮决策：记完笔记后仍然必须调用 submit_trade_intent、"
    "close_position 或 hold 来表达本次决策。",
    {
        "kind": {
            "type": "string",
            "enum": list(NOTE_KINDS),
            "description": "笔记类别",
        },
        "content": {
            "type": "string",
            "description": f"笔记正文，一句话到一小段（≤{MAX_NOTE_CHARS} 字符）",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": f"可选，最多 {MAX_NOTE_TAGS} 个短标签，便于以后检索",
        },
    },
    required=["kind", "content"],
)

#: 交给模型的全部工具。**顺序固定**：这段 JSON 是缓存前缀的一部分，改动顺序或
#: 文字会让前缀缓存整体失效。
AGENT_TOOLS: Final[tuple[dict[str, Any], ...]] = (
    SUBMIT_INTENT_TOOL,
    CLOSE_POSITION_TOOL,
    HOLD_TOOL,
    REMEMBER_NOTE_TOOL,
)


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """一次工具调用的记录。"""

    round_index: int
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    accepted: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_index,
            "name": self.name,
            "arguments": self.arguments,
            "result": self.result,
            "accepted": self.accepted,
        }


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """一次决策循环的完整结果。"""

    decision_id: str
    outcome: str
    message: str
    rounds: int
    dry_run: bool
    intent: TradeIntent | None = None
    validation: TradeValidation | None = None
    execution: TradeExecution | None = None
    close_execution: CloseExecution | None = None
    invocations: tuple[ToolInvocation, ...] = ()
    transcript: tuple[dict[str, Any], ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    #: 这次决策属于哪个自然小时（``2026-10-10T14``）。
    hour_key: str | None = None
    #: 这次决策复用了当前小时内多少条历史消息（0 表示本小时首次决策）。
    replayed_messages: int = 0
    #: 这次决策顺带压缩了哪些已完成的小时。
    compacted_hours: tuple[str, ...] = ()
    #: 这次决策顺带把哪一天压缩成了长期记忆笔记。
    compacted_day: str | None = None
    #: 对话是否已进小时存档。
    archived: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome in (
            OUTCOME_EXECUTED, OUTCOME_VALIDATED, OUTCOME_HELD, OUTCOME_DUPLICATE
        )

    @property
    def accepted(self) -> bool:
        """模型给出的决策是否通过了引擎校验。"""
        return bool(self.validation and self.validation.ok)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "outcome": self.outcome,
            "message": self.message,
            "rounds": self.rounds,
            "dry_run": self.dry_run,
            "accepted": self.accepted,
            "intent": self.intent.as_dict() if self.intent else None,
            "validation": self.validation.as_dict() if self.validation else None,
            "execution": self.execution.as_dict() if self.execution else None,
            "close_execution": self.close_execution.as_dict() if self.close_execution else None,
            "invocations": [item.as_dict() for item in self.invocations],
            "usage": self.usage,
            "errors": list(self.errors),
            "hour_key": self.hour_key,
            "replayed_messages": self.replayed_messages,
            "compacted_hours": list(self.compacted_hours),
            "compacted_day": self.compacted_day,
            "archived": self.archived,
        }


# --------------------------------------------------------------------------- #
# 上下文构造
# --------------------------------------------------------------------------- #


def build_static_context(
    cfg: Config,
    *,
    min_reward_risk_ratio: Decimal | int | str | None = None,
) -> dict[str, Any]:
    """构造决策上下文中**不随时间变化**的部分。

    只由 ``config.json``（与引擎的盈亏比下限）决定，因此同一进程内逐字节稳定，
    可以放进 system 消息吃前缀缓存。这里的每个数字都从配置读取，
    **不在提示词里另写一份**——否则改了配置提示词就会说谎。
    """
    ratio = Decimal(
        str(min_reward_risk_ratio if min_reward_risk_ratio is not None
            else engine_min_reward_risk_ratio())
    )
    return {
        "symbol": cfg.instrument.symbol,
        "min_trade_unit_usd": _s(cfg.instrument.min_trade_unit),
        "risk_limits": {
            "max_leverage": _s(cfg.risk.max_leverage),
            "max_margin_usage_ratio": _s(cfg.risk.max_margin_usage_ratio),
            "max_risk_per_trade_ratio": _s(cfg.risk.max_risk_per_trade_ratio),
            "max_total_risk_ratio": _s(cfg.risk.max_total_risk_ratio),
            "min_reward_risk_ratio": _s(ratio),
            "daily_drawdown_stop_ratio": _s(cfg.risk.daily_drawdown_stop_ratio),
            "min_equity_to_open_jpy": _s(cfg.margin.min_equity_to_open_jpy),
            "margin_call_level": _s(cfg.margin.margin_call_level),
            "stop_out_level": _s(cfg.margin.stop_out_level),
            "bankruptcy_equity_jpy": _s(cfg.margin.bankruptcy_equity_jpy),
        },
        "cost_model": {
            "spread_jpy": _s(cfg.market.spread_jpy),
            "slippage_jpy": _s(cfg.market.slippage_jpy),
            "note": "开多按 ask 成交、平多按 bid 成交；滑点只影响止损风险估算，不进成交价。",
        },
    }


def build_system_prompt(
    cfg: Config,
    *,
    min_reward_risk_ratio: Decimal | int | str | None = None,
) -> str:
    """由**配置**生成 system 提示词（不在源码里另写一份规则数字）。

    输出必须逐字节稳定：它属于缓存前缀，任何随时刻变化的字样都会让前缀缓存失效。
    """
    static = build_static_context(cfg, min_reward_risk_ratio=min_reward_risk_ratio)
    limits = static["risk_limits"]
    unit = static["min_trade_unit_usd"]
    return (
        f"你是「FX 战士久留美」这个虚拟交易模拟器的决策者，交易品种 {static['symbol']}。"
        "你只能通过提供的工具来表达决策，不要用自然语言描述交易计划。"
        "\n\n硬性约束（违反会被风控拒绝）："
        f"\n1. 数量必须是 {unit} 美元的正整数倍；"
        "\n2. 必须同时给出止损与止盈，且止盈距离 ≥ 止损距离的 "
        f"{limits['min_reward_risk_ratio']} 倍；"
        f"\n3. 止损风险（含滑点）不得超过账户净值的 "
        f"{_pct(limits['max_risk_per_trade_ratio'])}；"
        f"\n4. 开仓后保证金占用不得超过净值的 "
        f"{_pct(limits['max_margin_usage_ratio'])}；"
        "\n5. 同时最多持有一笔仓位，不能加仓或反向对冲；"
        "\n6. 行情不可用或已过期时不允许开仓；"
        f"\n7. 当日净值回撤达到 {_pct(limits['daily_drawdown_stop_ratio'])} 会停止当日开仓；"
        f"\n8. 净值低于 {limits['min_equity_to_open_jpy']} 日元时不允许开仓。"
        "\n\n被拒绝时工具会返回具体原因，请据此修正后重新提交；"
        "确实不该交易时请调用 hold。"
        "\n\n你可以用 remember_note 把自己的心得与决策理由写进长期记忆——"
        "它会在之后每次决策里回灌给你。记笔记不会结束本轮决策，"
        "记完仍需用 submit_trade_intent / close_position / hold 表达本次决策。"
        "\n\n注意：这是纯虚拟模拟，不涉及任何真实资金；但规则必须严格遵守。"
        "\n\n以下是机器可读的限额与成本参数（与上面文字一致，取自配置）：\n"
        + json.dumps(static, ensure_ascii=False, indent=2, sort_keys=True)
    )


def engine_min_reward_risk_ratio() -> Decimal:
    """引擎的盈亏比下限（单一来源，避免在提示词里另写一个 1.5）。"""
    from engine import MIN_REWARD_RISK_RATIO

    return MIN_REWARD_RISK_RATIO


def _pct(value: str | None) -> str:
    """把 ``0.02`` 这样的比例显示成 ``2%``。"""
    if value is None:  # pragma: no cover - 配置必填，防御性分支
        return "（未配置）"
    ratio = Decimal(value) * 100
    text = format(ratio.normalize(), "f")
    return f"{text}%"


def build_decision_context(
    cfg: Config,
    engine: TradingEngine,
    storage: Storage,
    quote: Quote | None,
    *,
    max_recent_trades: int = 5,
    max_notes: int = DEFAULT_MAX_NOTES_IN_CONTEXT,
    price_history: Mapping[str, Any] | None = None,
    max_hour_summaries: int = DEFAULT_MAX_HOUR_SUMMARIES,
) -> dict[str, Any]:
    """构造喂给模型的**易变**事实上下文（只读，取自账本与行情）。

    刻意保持精简：只给决策必需的东西，不给整份报告数据包。不随时间变化的部分
    （规则与限额）在 :func:`build_static_context` 里，属于 system 消息。

    ``price_history`` 由 :meth:`TradingAgent._prepare_price_history` 预备好传进来
    （它需要联网，所以不在这里做，方便测试注入）。
    """
    state = storage.read_account_state()
    positions = storage.read_positions(status=PositionStatus.OPEN)
    recent = storage.query_trades(order="DESC", limit=max_recent_trades)
    notes = storage.read_notes(limit=max_notes, newest_first=False)
    hours = storage.read_hour_summaries(limit=max_hour_summaries, newest_first=False)
    data = build_report_data(
        cfg=cfg, engine=engine, storage=storage, quote=quote, skip_quote=quote is None
    )

    context: dict[str, Any] = {
        "symbol": cfg.instrument.symbol,
        "now_jst": data.report_at.astimezone(cfg.tz).isoformat(timespec="seconds"),
        "trading_date": data.trading_date,
        "market": {
            "quote_available": data.market.quote_available,
            "bid": _s(data.market.bid),
            "ask": _s(data.market.ask),
            "spread_jpy": _s(data.market.spread_jpy),
            "source": data.market.source,
            "quote_type": data.market.quote_type,
            "is_stale": data.market.is_stale,
            "valid_for_decisions": data.market.valid_for_decisions,
            "issues": list(data.market.issues),
        },
        "account": {
            "currency": data.account.currency,
            "cash_balance": _s(data.account.cash_balance),
            "equity": _s(data.account.equity),
            "realized_pnl": _s(data.account.realized_pnl),
            "unrealized_pnl": _s(data.account.unrealized_pnl),
            "used_margin": _s(data.account.used_margin),
            "free_margin": _s(data.account.free_margin),
            "margin_level": _s(data.account.margin_level),
            "margin_usage_ratio": _s(data.account.margin_usage_ratio),
            "notional_jpy": _s(data.account.notional_jpy),
            "effective_leverage": _s(data.account.effective_leverage),
            "net_profit": _s(data.account.net_profit),
            "return_pct": _s(data.account.return_pct),
            "peak_equity": _s(data.account.peak_equity),
            "day_start_equity": _s(data.account.day_start_equity),
            "daily_drawdown_ratio": _s(data.account.daily_drawdown_ratio),
            "status": state.status,
            "halted": state.halted,
            "halt_reason": state.halt_reason,
        },
        "open_positions": [
            {
                "position_id": p.position_id,
                "side": p.side,
                "volume_usd": _s(p.volume),
                "entry_price": _s(p.entry_price),
                "stop_loss": _s(p.stop_loss),
                "take_profit": _s(p.take_profit),
                "margin_jpy": _s(p.margin_jpy),
                "planned_risk_jpy": _s(p.planned_risk_jpy),
                "entry_at": p.entry_at.isoformat(timespec="seconds"),
            }
            for p in positions
        ],
        "recent_trades": [
            {
                "trade_no": t.trade_no,
                "action": t.action,
                "side": t.side,
                "volume_usd": _s(t.volume),
                "price": _s(t.price),
                "realized_pnl_jpy": _s(t.realized_pnl_jpy),
                "reason": t.reason,
                "at": t.occurred_at.isoformat(timespec="seconds"),
            }
            for t in recent
        ],
        # 价格历史：按自然小时降采样成区间。拿不到就如实说「不可用」，不编。
        "price_history": dict(price_history) if price_history is not None else {
            "available": False,
            "reason": "本次决策未请求价格历史",
            "hours": [],
        },
        # 已压缩的小时记忆（原始对话在数据库里，这里只给压缩后的摘要）。
        "hour_memory": {
            "disclaimer": (
                "以下是你之前每小时决策的压缩摘要，供回忆当时的判断脉络。"
                "要判断当前行情，只依据上面的 price_history 与 market 字段。"
            ),
            "recent": [
                {
                    "hour": h.hour_key,
                    "turn_count": h.turn_count,
                    "summary": h.summary,
                }
                for h in hours
            ],
        },
        # 模型自己写下的笔记。**必须标注来源**：这是模型过去的自述，不是账本事实，
        # 如果不标注，模型很容易把自己上次的猜测当成当前行情。
        "my_notes": {
            "disclaimer": (
                "以下是你在之前决策里自己写下的笔记，按时间正序，仅供回忆参考。"
                "它们是你的旧判断，**不是当前事实**——判断行情与账户请只依据上面的字段。"
            ),
            "count_total": storage.count_notes(),
            "recent": [
                {
                    "note_no": n.note_no,
                    "kind": n.kind,
                    "content": n.content,
                    "tags": list(n.tags),
                    "decision_id": n.decision_id,
                    "at": n.created_at.isoformat(timespec="seconds"),
                }
                for n in notes
            ],
        },
        "unverifiable_fields": list(data.data_quality["unverifiable_fields"]),
    }
    return context


def _s(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


# --------------------------------------------------------------------------- #
# 小时分桶与压缩
# --------------------------------------------------------------------------- #


def hour_key_for(moment: datetime, *, tz: tzinfo) -> str:
    """``2026-10-10T14`` 形式的小时键（东京时间的自然小时）。"""
    return hour_start(moment, tz=tz).strftime("%Y-%m-%dT%H")


def summarize_hour(turns: Sequence[AgentTurn]) -> tuple[str, dict[str, Any]]:
    """把一小时内的决策**确定性地**压缩成摘要 + 统计。

    刻意不用语言模型：压缩是每小时都要做的事，用 LLM 会引入成本、延迟与不确定性，
    而「这一小时做了几次决策、每次是什么结果、理由是什么」本来就是机械可提取的。
    定性的心得留给模型自己的 ``remember_note``。
    """
    if not turns:
        return "（这一小时没有决策记录）", {"turns": 0}

    outcomes: dict[str, int] = {}
    lines: list[str] = []
    for turn in turns:
        outcomes[turn.outcome or "UNKNOWN"] = outcomes.get(turn.outcome or "UNKNOWN", 0) + 1
        stamp = turn.created_at.strftime("%H:%M")
        lines.append(f"- {stamp} {turn.summary or turn.outcome or '（无摘要）'}")

    head = "；".join(f"{name} {count} 次" for name, count in sorted(outcomes.items()))
    text = f"本小时共 {len(turns)} 次决策（{head}）：\n" + "\n".join(lines)
    stats: dict[str, Any] = {"turns": len(turns), **outcomes}
    return text, stats


def _turn_summary(result: DecisionResult) -> str:
    """把一次决策压成一行，供小时摘要使用。"""
    parts: list[str] = [result.outcome]
    intent = result.intent
    if intent is not None:
        parts.append(
            f"{intent.side.value} {_s(intent.volume_usd)} USD"
            f"（止损 {_s(intent.stop_loss)} / 止盈 {_s(intent.take_profit)}）"
        )
    if result.validation is not None and not result.validation.ok:
        parts.append("被拒：" + "；".join(result.validation.problems))
    reason = ""
    if intent is not None and intent.note:
        reason = intent.note
    elif result.message:
        reason = result.message
    if reason:
        parts.append(f"理由：{reason[:120]}")
    return " ".join(parts)


def _should_archive(result: DecisionResult) -> bool:
    """这次决策的对话要不要进小时存档。

    只有当对话**以一个完整的助手/工具回合收尾**时才存——否则重放出去会出现
    「连续两条 user 消息」这种非法序列（LLM 调用失败的那次就是这样）。
    """
    if not result.transcript:
        return False
    return str(result.transcript[-1].get("role") or "") in ("assistant", "tool")


# --------------------------------------------------------------------------- #
# 决策循环
# --------------------------------------------------------------------------- #


class TradingAgent:
    """把「事实 → 模型 → 工具调用 → 引擎校验 → 执行」串起来的决策循环。"""

    def __init__(
        self,
        cfg: Config | None = None,
        *,
        engine: TradingEngine | None = None,
        storage: Storage | None = None,
        client: LLMClient | None = None,
        quote_source: Callable[[], Quote] | None = None,
        hourly_source: Callable[..., tuple[HourlyRange, ...]] | None = None,
        memory: Any | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        max_notes: int = DEFAULT_MAX_NOTES_IN_CONTEXT,
        max_hour_summaries: int = DEFAULT_MAX_HOUR_SUMMARIES,
        history_hours: int = DEFAULT_HISTORY_HOURS,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.cfg: Config = cfg or load_config()
        self.engine: TradingEngine = engine or TradingEngine(self.cfg)
        self.storage: Storage = storage or self.engine.storage
        self.client: LLMClient = client or LLMClient(self.cfg)
        self._quote_source = quote_source
        self._hourly_source = hourly_source or (
            lambda **kwargs: get_hourly_ranges(**kwargs)
        )
        self._memory = memory
        self.max_rounds = max(1, int(max_rounds))
        self.max_notes = max(0, int(max_notes))
        self.max_hour_summaries = max(0, int(max_hour_summaries))
        self.history_hours = max(1, int(history_hours))
        self.logger = logger_ or logger

    @property
    def memory(self) -> Any:
        """文件记忆投影（惰性构造，避免无谓地创建目录）。"""
        if self._memory is None:
            from memory import MemoryStore

            self._memory = MemoryStore(self.cfg, storage=self.storage)
        return self._memory

    @classmethod
    def from_config(cls, cfg: Config | None = None, **kwargs: Any) -> TradingAgent:
        cfg = cfg or load_config()
        engine = kwargs.pop("engine", None) or TradingEngine.from_config(cfg)
        return cls(cfg, engine=engine, **kwargs)

    # -- 提示词 ------------------------------------------------------------ #

    def system_prompt(self) -> str:
        """由配置生成（缓存前缀的一部分，逐字节稳定）。"""
        return build_system_prompt(
            self.cfg, min_reward_risk_ratio=self.engine.min_reward_risk_ratio
        )

    # -- 主流程 ------------------------------------------------------------ #

    def decide(
        self,
        *,
        quote: Quote | None = None,
        decision_id: str | None = None,
        dry_run: bool = True,
        max_rounds: int | None = None,
        now: datetime | None = None,
        record_audit: bool = True,
    ) -> DecisionResult:
        """跑一次决策循环。

        Args:
            quote: 注入行情（测试用）；``None`` 时实时获取。
            decision_id: 幂等键；``None`` 时自动生成。
            dry_run: ``True``（默认）只返回决策与校验结果，不真的开仓。
            max_rounds: 最大轮数（含被拒后的修正重试）。
            record_audit: 是否把这次决策写入审计日志。

        Returns:
            :class:`DecisionResult`。**任何异常都会被捕获并记进 ``errors``**，
            调用方不需要 try/except。
        """
        decision_id = decision_id or f"dec-{uuid.uuid4().hex[:12]}"
        moment = now or datetime.now(self.cfg.tz)
        hour_key = hour_key_for(moment, tz=self.cfg.tz)
        day_key = hour_key[:10]

        # 0) 记忆维护：压缩已经走完的小时 / 天，然后取出本小时的对话历史
        compacted_hours, compacted_day = self._roll_memory(moment)
        history = self._history_messages(hour_key)

        result = self._decide_inner(
            decision_id=decision_id, quote=quote, dry_run=dry_run,
            max_rounds=max_rounds, now=moment, record_audit=record_audit,
            history=history, hour_key=hour_key,
        )
        archived = self._archive_turn(result, hour_key, day_key)
        return replace(
            result,
            replayed_messages=len(history),
            compacted_hours=compacted_hours,
            compacted_day=compacted_day,
            archived=archived,
        )

    # -- 记忆维护 ---------------------------------------------------------- #

    def _roll_memory(self, moment: datetime) -> tuple[tuple[str, ...], str | None]:
        """压缩「已经走完但还没压缩」的小时与天。**任何失败都不影响本次决策。**"""
        hour_key = hour_key_for(moment, tz=self.cfg.tz)
        day_key = hour_key[:10]
        compacted: list[str] = []
        compacted_day: str | None = None
        try:
            done = set(self.storage.list_hour_summary_keys())
            for candidate in self.storage.list_turn_hours():
                if candidate >= hour_key or candidate in done:
                    continue
                turns = self.storage.read_turns(hour_key=candidate)
                if not turns:
                    continue
                text, stats = summarize_hour(turns)
                summary, _ = self.storage.record_hour_summary(
                    candidate, text, day_key=candidate[:10],
                    turn_count=len(turns), stats=stats, occurred_at=moment,
                )
                self._memory.sync_hour(summary)
                compacted.append(candidate)
            compacted_day = self._compact_finished_day(day_key, moment)
        except (StorageError, OSError) as exc:  # noqa: BLE001 - 记忆是辅助，不能挡决策
            self.logger.warning("记忆压缩失败（不影响本次决策）：%s", exc)
        if compacted:
            self.logger.info("已压缩小时记忆：%s", "、".join(compacted))
        return tuple(compacted), compacted_day

    def _compact_finished_day(self, today: str, moment: datetime) -> str | None:
        """把「已经结束的天」的小时摘要压缩成一条 ``DAY_SUMMARY`` 笔记。

        这就是「更早的记忆压缩后存进 note」的那一步：原始对话留在
        ``agent_turns``，小时摘要留在 ``hour_summaries``，而长期记忆只保留
        每天一条可检索的笔记。
        """
        days = {key[:10] for key in self.storage.list_hour_summary_keys()}
        finished = sorted(d for d in days if d < today)
        if not finished:
            return None
        target = finished[-1]
        event_key = f"day-summary:{target}"
        existing = [
            n for n in self.storage.read_notes(kind="DAY_SUMMARY", limit=None)
            if n.event_key == event_key
        ]
        if existing:
            return None
        hours = self.storage.read_hour_summaries(day_key=target, newest_first=False)
        if not hours:
            return None
        total = sum(h.turn_count for h in hours)
        body = f"{target} 共 {len(hours)} 个小时、{total} 次决策。\n" + "\n".join(
            f"- {h.hour_key[11:13]}:00 {h.summary.splitlines()[0] if h.summary else ''}"
            for h in hours
        )
        note, _ = self.storage.record_note(
            "DAY_SUMMARY", body, tags=[target], event_key=event_key,
            trading_date=target, author="system", occurred_at=moment,
        )
        self._memory.sync_note(note)
        self.logger.info("已把 %s 压缩成长期记忆笔记 #%d", target, note.note_no)
        return target

    def _history_messages(self, hour_key: str) -> tuple[dict[str, Any], ...]:
        """取本小时**最后一条**存档的完整消息列表，作为本次请求的历史前缀。

        为什么取「最后一条的完整列表」而不是把所有回合拼起来：每条存档保存的
        本来就是「当时发出去的完整消息列表」，所以最后一条天然包含了本小时
        之前所有回合，而且**逐字节等于**上一次请求发出去的内容——这正是前缀缓存
        命中所需的条件（差一个字节就整体失效）。

        若存档里的 system 消息与当前配置生成的不一致（例如中途改了配置），
        就**放弃历史**：那种情况下缓存本来就会失效，而且规则已经变了，
        重放旧规则会让模型按过期规则决策。
        """
        turns = self.storage.read_turns(hour_key=hour_key)
        if not turns:
            return ()
        messages = tuple(dict(m) for m in turns[-1].messages)
        if not messages:
            return ()
        expected = self.system_prompt()
        first = messages[0]
        if first.get("role") != "system" or first.get("content") != expected:
            self.logger.warning(
                "本小时存档的 system 消息与当前配置不一致，放弃重放历史（缓存会失效）"
            )
            return ()
        return messages

    def _archive_turn(self, result: DecisionResult, hour_key: str, day_key: str) -> bool:
        """把这次决策的对话存档进本小时。失败只记日志，不影响决策结果。"""
        if not _should_archive(result):
            return False
        try:
            _, created = self.storage.record_turn(
                hour_key, result.decision_id, result.transcript,
                day_key=day_key, outcome=result.outcome,
                summary=_turn_summary(result),
            )
        except StorageError as exc:  # noqa: BLE001
            self.logger.warning("对话存档失败：%s", exc)
            return False
        return created

    def _prepare_price_history(
        self, moment: datetime, quote: Quote | None
    ) -> dict[str, Any]:
        """预备「按小时划分的价格区间」。

        拿不到就**如实报告不可用**，绝不编造或用当前报价凑数——模型宁可知道
        「没有历史」，也不能被假历史误导。
        """
        hours = self.history_hours
        try:
            ranges = self._hourly_source(
                hours=hours, cfg=self.cfg, now=moment,
            )
        except (MarketDataError, HistoryUnavailableError) as exc:
            return {
                "available": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "requested_hours": hours,
                "hours": [],
            }
        except Exception as exc:  # noqa: BLE001 - 历史数据缺失不该终止决策
            return {
                "available": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "requested_hours": hours,
                "hours": [],
            }

        payload = [r.as_dict() for r in ranges]
        with_data = [r for r in ranges if r.has_data]
        result: dict[str, Any] = {
            "available": bool(with_data),
            "requested_hours": hours,
            "hours": payload,
            "note": "按东京时间自然小时聚合的 1 分钟 K 线区间（已降采样，不是原始 K 线）。",
        }
        if not with_data:
            result["reason"] = "数据源没有返回任何可核验的 1 分钟 K 线（休市或源站缺数据）"
        if quote is not None and quote.is_stale:
            result["quote_is_stale"] = True
        return result

    def _decide_inner(
        self,
        *,
        decision_id: str,
        quote: Quote | None,
        dry_run: bool,
        max_rounds: int | None,
        now: datetime | None,
        record_audit: bool,
        history: tuple[dict[str, Any], ...],
        hour_key: str,
    ) -> DecisionResult:
        rounds_limit = max(1, int(max_rounds if max_rounds is not None else self.max_rounds))
        errors: list[str] = []
        invocations: list[ToolInvocation] = []
        usage_total: dict[str, int] = {}

        # 1) 行情
        if quote is None:
            try:
                quote = (self._quote_source or (lambda: get_current_quote(self.cfg)))()
            except MarketDataError as exc:
                return self._finish(
                    decision_id, OUTCOME_ERROR, f"无法获取行情：{exc}", 0, dry_run,
                    errors=[f"行情获取失败：{exc}"], hour_key=hour_key,
                )

        # 2) 上下文。稳定部分进 system（吃前缀缓存），易变部分进 user。
        context = build_decision_context(
            self.cfg, self.engine, self.storage, quote,
            max_notes=self.max_notes,
            price_history=self._prepare_price_history(now or datetime.now(self.cfg.tz), quote),
            max_hour_summaries=self.max_hour_summaries,
        )
        current = user_message(
            "当前时刻、行情与账户事实如下（JSON）。请据此决策，并通过工具提交。\n"
            + json.dumps(context, ensure_ascii=False, indent=2)
        )
        # 本小时已有历史 → 直接续在它后面（前缀逐字节不变，可命中缓存）；
        # 本小时首次决策 → 从 system 开始。
        messages: list[dict[str, Any]] = list(history) + [current] if history else [
            system_message(self.system_prompt()), current,
        ]

        # 3) 循环
        for round_index in range(1, rounds_limit + 1):
            try:
                reply = self.client.chat(messages, tools=list(AGENT_TOOLS), tool_choice="auto")
            except LLMEmptyCompletionError as exc:
                errors.append(str(exc))
                return self._finish(
                    decision_id, OUTCOME_ERROR, f"模型返回空响应：{exc}", round_index, dry_run,
                    invocations=invocations, transcript=messages, usage=usage_total,
                    errors=errors, hour_key=hour_key,
                )
            except LLMError as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                return self._finish(
                    decision_id, OUTCOME_ERROR, f"LLM 调用失败：{exc}", round_index, dry_run,
                    invocations=invocations, transcript=messages, usage=usage_total,
                    errors=errors, hour_key=hour_key,
                )
            _accumulate_usage(usage_total, reply)
            messages.append(
                assistant_message(
                    reply.content or None,
                    tool_calls=[call.to_message_part() for call in reply.tool_calls],
                )
            )

            if not reply.tool_calls:
                errors.append("模型没有调用任何工具")
                return self._finish(
                    decision_id, OUTCOME_NO_DECISION,
                    f"模型未提交决策（仅返回文本：{reply.content[:120]!r}）",
                    round_index, dry_run, invocations=invocations, transcript=messages,
                    usage=usage_total, errors=errors, hour_key=hour_key,
                )

            terminal: DecisionResult | None = None
            for call in reply.tool_calls:
                invocation, terminal = self._handle_call(
                    call, round_index, decision_id, quote, dry_run, now
                )
                invocations.append(invocation)
                messages.append(
                    tool_result_message(call.id, json.dumps(invocation.result, ensure_ascii=False))
                )
                if terminal is not None:
                    break

            if terminal is not None:
                result = self._finish(
                    decision_id, terminal.outcome, terminal.message, round_index, dry_run,
                    intent=terminal.intent, validation=terminal.validation,
                    execution=terminal.execution, close_execution=terminal.close_execution,
                    invocations=invocations, transcript=messages, usage=usage_total,
                    errors=list(terminal.errors), hour_key=hour_key,
                )
                if record_audit:
                    self._audit(result, context)
                return result

        # 4) 轮数用尽
        errors.append(f"{rounds_limit} 轮内没有得到可执行的决策")
        result = self._finish(
            decision_id, OUTCOME_ERROR,
            f"模型在 {rounds_limit} 轮内始终未给出通过风控的决策",
            rounds_limit, dry_run, invocations=invocations, transcript=messages,
            usage=usage_total, errors=errors, hour_key=hour_key,
        )
        if record_audit:
            self._audit(result, context)
        return result

    # -- 单个工具调用 ------------------------------------------------------ #

    def _handle_call(
        self,
        call: ToolCall,
        round_index: int,
        decision_id: str,
        quote: Quote,
        dry_run: bool,
        now: datetime | None,
    ) -> tuple[ToolInvocation, DecisionResult | None]:
        if call.name == "hold":
            reason = str(call.arguments.get("reason") or "未说明理由")
            return (
                ToolInvocation(round_index, call.name, call.arguments,
                               {"ok": True, "held": True}, accepted=True),
                DecisionResult(
                    decision_id=decision_id, outcome=OUTCOME_HELD, message=f"模型决定不交易：{reason}",
                    rounds=round_index, dry_run=dry_run,
                ),
            )
        if call.name == "remember_note":
            return self._handle_note(call, round_index, decision_id, now)
        if call.name == "close_position":
            return self._handle_close(call, round_index, decision_id, quote, dry_run, now)
        if call.name == "submit_trade_intent":
            return self._handle_submit(call, round_index, decision_id, quote, dry_run, now)
        result = {"ok": False, "error": f"未知工具：{call.name}"}
        return ToolInvocation(round_index, call.name, call.arguments, result, accepted=False), None

    def _handle_note(
        self,
        call: ToolCall,
        round_index: int,
        decision_id: str,
        now: datetime | None,
    ) -> tuple[ToolInvocation, DecisionResult | None]:
        """写一条长期记忆。

        **永远不终止决策循环**（返回值第二项恒为 ``None``）：记笔记是副产物，
        模型记完还得用 submit/close/hold 表达本次决策。

        dry-run **不会**阻止写笔记：笔记不是交易，不动账户；如果 dry-run 时不能记，
        那么默认 dry-run 的调度器就永远学不到东西。
        """
        args = call.arguments
        kind = str(args.get("kind") or "").strip().upper()
        content = str(args.get("content") or "").strip()
        if kind not in NOTE_KINDS:
            payload = {
                "ok": False,
                "error": f"kind 必须是 {'/'.join(NOTE_KINDS)} 之一，收到 {kind!r}",
            }
            return ToolInvocation(round_index, call.name, args, payload, accepted=False), None
        if not content:
            payload = {"ok": False, "error": "content 不能为空"}
            return ToolInvocation(round_index, call.name, args, payload, accepted=False), None

        raw_tags = args.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags]
        if not isinstance(raw_tags, (list, tuple)):
            payload = {"ok": False, "error": "tags 必须是字符串数组"}
            return ToolInvocation(round_index, call.name, args, payload, accepted=False), None
        tags = [str(t).strip()[:24] for t in raw_tags if str(t).strip()][:MAX_NOTE_TAGS]

        moment = now or datetime.now(self.cfg.tz)
        # 同一次决策内重复写同一条笔记只留一条（跨决策则算新笔记，时间不同）。
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        event_key = f"note:{decision_id}:{kind}:{digest}"
        try:
            note, created = self.storage.record_note(
                kind,
                content,
                tags=tags,
                decision_id=decision_id,
                trading_date=trading_date_for(moment, tz=self.cfg.tz).isoformat(),
                event_key=event_key,
                occurred_at=moment,
            )
        except StorageError as exc:
            self.logger.warning("AI 笔记写入失败：%s", exc)
            payload = {"ok": False, "error": f"笔记写入失败：{exc}"}
            return ToolInvocation(round_index, call.name, args, payload, accepted=False), None

        self.logger.info(
            "AI 笔记 #%d（%s，%s）：%s",
            note.note_no, note.kind, "新建" if created else "已存在", note.content[:80],
        )
        # 文件投影是辅助：写文件失败不影响笔记本身（数据库里已经写进去了）。
        try:
            self.memory.sync_note(note)
        except OSError as exc:  # noqa: BLE001
            self.logger.warning("记忆文件投影失败（笔记已入库）：%s", exc)
        payload = {
            "ok": True,
            "note_no": note.note_no,
            "created": created,
            "note": "已记住。记笔记不结束本轮决策——请继续用工具表达本次决策。",
        }
        return ToolInvocation(round_index, call.name, args, payload, accepted=True), None

    def _handle_submit(
        self,
        call: ToolCall,
        round_index: int,
        decision_id: str,
        quote: Quote,
        dry_run: bool,
        now: datetime | None,
    ) -> tuple[ToolInvocation, DecisionResult | None]:
        args = call.arguments
        try:
            intent = TradeIntent(
                symbol=self.cfg.instrument.symbol,
                side=PositionSide(str(args["side"]).upper()),
                volume_usd=Decimal(str(args["volume_usd"])),
                stop_loss=Decimal(str(args["stop_loss"])),
                take_profit=Decimal(str(args["take_profit"])),
                intent_id=f"ai:{decision_id}",
                decision_ref=decision_id,
                note=str(args.get("reason") or "")[:200] or None,
            )
        except (KeyError, EngineError, InvalidOperation, ValueError) as exc:
            payload = {"ok": False, "error": f"参数无法解析：{exc}"}
            return ToolInvocation(round_index, call.name, args, payload, accepted=False), None

        # 幂等检查必须排在风控校验**之前**：重复运行同一个 decision_id 时账户里已有
        # 这笔持仓，「同一方向最多一笔」的规则会先把它拒掉，于是「重复指令」会被误报
        # 成「被风控拒绝」。键由引擎统一提供，避免这里复制 OPEN: 前缀的约定。
        event_key = self.engine.open_event_key(intent)
        existing = self.storage.find_position_by_event_key(event_key)
        if existing is not None:
            payload = {
                "ok": True,
                "duplicate": True,
                "position_id": existing.position_id,
                "note": "该决策此前已开仓，未重复开仓。",
            }
            return (
                ToolInvocation(round_index, call.name, args, payload, accepted=True),
                DecisionResult(
                    decision_id=decision_id, outcome=OUTCOME_DUPLICATE,
                    message=(
                        f"重复指令：决策 {decision_id} 已开过仓 "
                        f"#{existing.position_id}，未重复开仓"
                    ),
                    rounds=round_index, dry_run=dry_run,
                ),
            )

        # 引擎重新校验（执行时还会再校验一次）
        validation = self.engine.validate_trade_intent(intent, quote)
        if not validation.ok:
            payload = {
                "ok": False,
                "rejected": True,
                "problems": list(validation.problems),
                "hint": "请修正后重新调用 submit_trade_intent，或改为调用 hold。",
            }
            self.logger.warning("AI 决策被风控拒绝：%s", "；".join(validation.problems))
            return (
                ToolInvocation(round_index, call.name, args, payload, accepted=False),
                None,  # 不终止循环，让模型有机会修正
            )

        if dry_run:
            payload = {
                "ok": True,
                "dry_run": True,
                "note": "已通过风控校验，但当前是 dry-run，没有真实开仓。",
                "entry_price": _s(validation.entry_price),
                "required_margin_jpy": _s(validation.required_margin_jpy),
                "planned_risk_jpy": _s(validation.planned_risk_jpy),
                "reward_risk_ratio": _s(validation.reward_risk_ratio),
            }
            return (
                ToolInvocation(round_index, call.name, args, payload, accepted=True),
                DecisionResult(
                    decision_id=decision_id, outcome=OUTCOME_VALIDATED,
                    message="决策通过风控校验（dry-run，未开仓）", rounds=round_index,
                    dry_run=True, intent=intent, validation=validation,
                ),
            )

        try:
            execution = self.engine.execute_virtual_trade(intent, quote, occurred_at=now)
        except TradeRejectedError as exc:  # 二次校验兜底
            payload = {"ok": False, "rejected": True, "problems": list(exc.validation.problems)}
            return ToolInvocation(round_index, call.name, args, payload, accepted=False), None
        except (EngineError, StorageError) as exc:
            payload = {"ok": False, "error": f"执行失败：{exc}"}
            return ToolInvocation(round_index, call.name, args, payload, accepted=False), None

        payload = {
            "ok": True,
            "duplicate": execution.is_duplicate,
            "position_id": execution.position.position_id,
            "entry_price": _s(execution.position.entry_price),
            "required_margin_jpy": _s(execution.position.margin_jpy),
            "note": "重复指令，未重复开仓。" if execution.is_duplicate else "已开仓。",
        }
        return (
            ToolInvocation(round_index, call.name, args, payload, accepted=True),
            DecisionResult(
                decision_id=decision_id,
                outcome=OUTCOME_EXECUTED,
                message=("重复指令，未重复开仓" if execution.is_duplicate else "已按决策开仓"),
                rounds=round_index, dry_run=False, intent=intent,
                validation=validation, execution=execution,
            ),
        )

    def _handle_close(
        self,
        call: ToolCall,
        round_index: int,
        decision_id: str,
        quote: Quote,
        dry_run: bool,
        now: datetime | None,
    ) -> tuple[ToolInvocation, DecisionResult | None]:
        reason = str(call.arguments.get("reason") or "AI 主动平仓")
        positions = self.storage.read_positions(status=PositionStatus.OPEN)
        if not positions:
            payload = {"ok": False, "error": "当前没有未平仓持仓"}
            return ToolInvocation(round_index, call.name, call.arguments, payload, accepted=False), None
        position = positions[0]
        if dry_run:
            payload = {
                "ok": True, "dry_run": True,
                "note": f"将平掉持仓 #{position.position_id}，但当前是 dry-run，未执行。",
            }
            return (
                ToolInvocation(round_index, call.name, call.arguments, payload, accepted=True),
                DecisionResult(
                    decision_id=decision_id, outcome=OUTCOME_VALIDATED,
                    message=f"将平掉持仓 #{position.position_id}（dry-run，未执行）",
                    rounds=round_index, dry_run=True,
                ),
            )
        try:
            execution = self.engine.close_position(
                position.position_id, quote, reason=CloseReason.MANUAL, occurred_at=now
            )
        except (EngineError, StorageError) as exc:
            payload = {"ok": False, "error": f"平仓失败：{exc}"}
            return ToolInvocation(round_index, call.name, call.arguments, payload, accepted=False), None
        payload = {
            "ok": True, "position_id": execution.position.position_id,
            "exit_price": _s(execution.trade.price),
            "realized_pnl_jpy": _s(execution.realized_pnl_jpy),
            "reason": reason,
        }
        return (
            ToolInvocation(round_index, call.name, call.arguments, payload, accepted=True),
            DecisionResult(
                decision_id=decision_id, outcome=OUTCOME_EXECUTED,
                message=f"已平仓 #{execution.position.position_id}（{reason}）",
                rounds=round_index, dry_run=False, close_execution=execution,
            ),
        )

    # -- 收尾 -------------------------------------------------------------- #

    @staticmethod
    def _finish(
        decision_id: str,
        outcome: str,
        message: str,
        rounds: int,
        dry_run: bool,
        **kwargs: Any,
    ) -> DecisionResult:
        return DecisionResult(
            decision_id=decision_id, outcome=outcome, message=message,
            rounds=rounds, dry_run=dry_run, **kwargs,
        )

    def _audit(self, result: DecisionResult, context: Mapping[str, Any]) -> None:
        try:
            self.storage.audit(
                _AUDIT_TYPE,
                {
                    "decision_id": result.decision_id,
                    "outcome": result.outcome,
                    "dry_run": result.dry_run,
                    "rounds": result.rounds,
                    "accepted": result.accepted,
                    "intent": result.intent.as_dict() if result.intent else None,
                    "problems": (
                        list(result.validation.problems)
                        if result.validation and not result.validation.ok
                        else []
                    ),
                    "position_id": (
                        result.execution.position.position_id if result.execution else None
                    ),
                    "market_bid": context.get("market", {}).get("bid"),
                    "market_ask": context.get("market", {}).get("ask"),
                    "usage": result.usage,
                },
            )
        except StorageError as exc:  # pragma: no cover - 审计失败不影响决策结果
            self.logger.warning("写入 AI 决策审计失败：%s", exc)


def _accumulate_usage(total: dict[str, int], reply: ChatResponse) -> None:
    if reply.usage is None:
        return
    for key, value in reply.usage.as_dict().items():
        if value is None:
            continue
        total[key] = total.get(key, 0) + int(value)


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent",
        description=(
            "跑一次 AI 交易决策循环（function calling + 引擎风控校验）。"
            "默认 dry-run，不真的开仓。"
        ),
    )
    parser.add_argument("--live", action="store_true", help="真的执行（默认为 dry-run，不落单）")
    parser.add_argument("--decision-id", default=None, dest="decision_id", help="幂等键")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS, dest="max_rounds")
    parser.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    parser.add_argument("--show-context", action="store_true", dest="show_context",
                        help="同时打印喂给模型的上下文")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2
    agent = TradingAgent.from_config(cfg)

    if args.show_context:
        try:
            quote = get_current_quote(cfg)
        except MarketDataError as exc:
            print(f"[行情错误] {exc}")
            return 1
        # 打印的必须是**真正发出去的两段**：稳定前缀（system）+ 易变尾部（user），
        # 而不是只打 user 那段——否则看不到模型实际读到的规则。
        print("=== 稳定前缀：system 消息（缓存前缀的一部分，逐字节不变）===")
        print(agent.system_prompt())
        print()
        print("=== 易变尾部：user 消息 ===")
        print(json.dumps(
            build_decision_context(cfg, agent.engine, agent.storage, quote),
            ensure_ascii=False, indent=2,
        ))
        print()

    result = agent.decide(decision_id=args.decision_id, dry_run=not args.live,
                          max_rounds=args.max_rounds)
    if args.as_json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1

    print(f"决策编号    : {result.decision_id}")
    print(f"结果        : {result.outcome}（{'dry-run' if result.dry_run else '已执行'}）")
    print(f"说明        : {result.message}")
    print(f"轮数        : {result.rounds}")
    for item in result.invocations:
        flag = "通过" if item.accepted else "未通过"
        print(f"  · 第{item.round_index}轮 {item.name} → {flag}")
        if item.result.get("problems"):
            for problem in item.result["problems"]:
                print(f"      - {problem}")
    if result.validation and result.validation.ok:
        v = result.validation
        print(f"  成交价 {v.entry_price} | 保证金 {v.required_margin_jpy} | "
              f"计划风险 {v.planned_risk_jpy} | 盈亏比 1:{v.reward_risk_ratio}")
    if result.usage:
        print(f"token 用量  : {result.usage}")
    for error in result.errors:
        print(f"  [错误] {error}")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
