"""FX 战士久留美 —— 统一命令行入口。

调用已完成的功能模块，**不重复实现任何业务逻辑**：

* 配置 → :mod:`config`
* 行情 → :mod:`market_data`
* 持久化 → :mod:`storage`
* 交易执行与盈亏 → :mod:`engine`
* 风险监控 → :mod:`risk_monitor`
* 报告 → :mod:`reporting`

用法::

    python -m main <命令> [选项]
    python -m main <命令> [选项]

安全约束（刻意的设计，不是遗漏）
================================

1. **开仓无法绕过风险规则**：``open-long`` / ``open-short`` 只是把参数打包成
   :class:`~engine.TradeIntent` 交给 :meth:`TradingEngine.execute_virtual_trade`，
   数量、止损、止盈、账户状态、保证金、全部风险限额都由引擎重新校验。
   命令行**刻意不提供** ``--allow-stale`` / ``--force`` / ``--skip-risk`` 之类的开关，
   也没有任何参数可以直接指定成交价、保证金、盈亏或余额。
2. **资金存取需要严格确认**：``deposit`` / ``withdraw`` 必须传入与金额精确匹配的
   确认文字（如 ``--confirm "确认入金 10000"``），且要求账户**无持仓**、
   **无待执行订单**、账户状态允许。确认不通过时**不写任何数据**。
3. **没有后门命令**：``test`` 只运行自动化测试（测试全部使用临时数据库，
   不触碰真实账户）；``init`` 只能按配置发放一次初始本金，重复调用不会重复发放；
   不存在任何可以绕过资金授权或凭空增加余额的命令或开关。
4. **查询不产生副作用**：``status`` / ``quote`` / ``report-data`` / ``verify``
   只读，不会触发交易，也不会改写账户状态。

退出码：``0`` 成功；``1`` 命令失败（被拒绝/校验不通过）；``2`` 参数错误。
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import logging
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Sequence

from agent import DEFAULT_MAX_ROUNDS, TradingAgent, build_decision_context
from config import Config, ConfigError, load_config
from engine import (
    ACCEPTANCE_GATE_META_KEY,
    GATE_BLOCKED_PREFIX,
    CloseReason,
    EngineError,
    PositionNotFoundError,
    TradeIntent,
    TradeRejectedError,
    TradingEngine,
    calculate_equity,
    calculate_margin_level,
    calculate_unrealized_pnl,
    calculate_used_margin,
)
from llm_client import LLMConfigError, LLMError
from market_data import MarketDataError, Quote, get_current_quote
from reporting import (
    build_report_data,
    export_snapshot,
    generate_report_template,
)
from risk_monitor import RiskMonitor
from version import __version__
from storage import (
    AccountStatus,
    DuplicateEventError,
    FlowType,
    PositionSide,
    PositionStatus,
    Storage,
    StorageError,
)

__all__ = ["main", "build_parser", "CliError", "EXIT_OK", "EXIT_FAILED", "EXIT_USAGE"]

logger = logging.getLogger("kurumi_fx_sim.main")

EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1
EXIT_USAGE: Final[int] = 2

#: 单笔资金存取的下限与步长（日元）。
MIN_FLOW_AMOUNT: Final[Decimal] = Decimal("1000")
FLOW_STEP: Final[Decimal] = Decimal("1000")
#: 单笔入金上限（日元）。出金无此上限，但不得超过现金余额。
MAX_DEPOSIT_AMOUNT: Final[Decimal] = Decimal("100000")

DEPOSIT_CONFIRM_TEMPLATE: Final[str] = "确认入金 {amount}"
WITHDRAW_CONFIRM_TEMPLATE: Final[str] = "确认出金 {amount}"

#: 允许资金存取的账户状态（爆仓/已关闭的账户不接受资金操作）。
_FLOW_ALLOWED_STATUSES: Final[frozenset[str]] = frozenset(
    {AccountStatus.ACTIVE.value, AccountStatus.HALTED.value}
)

_CLOSE_REASON_BY_NAME: Final[dict[str, CloseReason]] = {
    "manual": CloseReason.MANUAL,
    "stop-loss": CloseReason.STOP_LOSS,
    "take-profit": CloseReason.TAKE_PROFIT,
    "margin-call": CloseReason.MARGIN_CALL,
    "daily-drawdown": CloseReason.DAILY_DRAWDOWN,
    "bankruptcy": CloseReason.BANKRUPTCY,
    "session-end": CloseReason.SESSION_END,
}


class CliError(RuntimeError):
    """命令层面的错误（参数校验、业务前置条件不满足）。"""


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #


def _resolve(cfg: Config, db: str | None) -> Storage:
    """按 ``--db`` 或配置打开数据库。"""
    if db:
        return Storage(Path(db).expanduser(), tz=cfg.tz)
    return Storage.from_config(cfg)


def _engine(cfg: Config, storage: Storage) -> TradingEngine:
    return TradingEngine(cfg, storage)


def _jst(cfg: Config) -> timezone:
    return cfg.tz  # type: ignore[return-value]


def _parse_flow_amount(raw: str, *, label: str) -> tuple[Decimal, str, str]:
    """解析并校验资金存取金额。返回 ``(金额, 原样字符串, 规范字符串)``。"""
    text = str(raw).strip()
    if not text:
        raise CliError(f"{label}金额不能为空")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise CliError(f"{label}金额不是合法数字: {raw!r}") from exc
    if not amount.is_finite():
        raise CliError(f"{label}金额必须是有限数字: {raw!r}")
    if amount <= 0:
        raise CliError(f"{label}金额必须为正数，收到 {amount}")
    if amount < MIN_FLOW_AMOUNT:
        raise CliError(f"单笔{label}最低 {MIN_FLOW_AMOUNT} 日元，收到 {amount}")
    if amount % FLOW_STEP != 0:
        raise CliError(f"单笔{label}必须是 {FLOW_STEP} 日元的整数倍，收到 {amount}")
    return amount, text, format(amount.normalize(), "f")


def _confirm_matches(confirm: str | None, template: str, raw: str, canonical: str) -> bool:
    """确认文字必须与金额**精确匹配**（接受原样与规范两种写法）。"""
    if confirm is None:
        return False
    text = confirm.strip()
    return text in {template.format(amount=canonical), template.format(amount=raw)}


def _pending_orders(storage: Storage) -> tuple[()]:
    """待执行订单。

    本系统采用**立即成交**模型（:meth:`TradingEngine.execute_virtual_trade` 一步完成
    校验与落库），数据库里没有挂单表，因此这里恒为空。

    保留该函数是为了让「资金存取前必须无待执行订单」这条规则在代码里有明确落点：
    若将来引入挂单，应在此查询真实订单表。
    """
    return ()


def _guard_flow_allowed(storage: Storage) -> None:
    """资金存取前置条件：无持仓、无待执行订单、账户状态允许。"""
    state = storage.read_account_state()
    if state.status not in _FLOW_ALLOWED_STATUSES:
        raise CliError(
            f"账户状态为 {state.status}，不允许资金存取"
            f"（仅允许 {'、'.join(sorted(_FLOW_ALLOWED_STATUSES))}）"
        )
    positions = storage.read_positions(status=PositionStatus.OPEN)
    if positions:
        held = "、".join(f"#{p.position_id}({p.side} {p.volume})" for p in positions)
        raise CliError(
            f"当前仍有未平仓持仓（{held}），为避免影响保证金与盈亏口径，"
            "请先平仓再存取资金"
        )
    pending = _pending_orders(storage)
    if pending:  # pragma: no cover - 当前模型下恒为空
        raise CliError(f"存在 {len(pending)} 笔待执行订单，请先处理后再存取资金")


def _read_only_quote(cfg: Config) -> Quote | None:
    """尽力取一次行情；失败返回 ``None``（不抛异常，由调用方决定如何处理）。"""
    try:
        return get_current_quote(cfg)
    except MarketDataError as exc:
        logger.warning("行情获取失败：%s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - 行情异常不应让只读命令失败
        logger.warning("行情获取异常：%s: %s", type(exc).__name__, exc)
        return None


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _fmt(value: Any, unit: str = "") -> str:
    """展示用格式化；``None`` 显示为「无法核实」，绝不显示为 0。"""
    if value is None:
        return "无法核实"
    return f"{value}{unit}"


def _status_payload(cfg: Config, storage: Storage, engine: TradingEngine, quote: Quote | None) -> dict[str, Any]:
    """只读的账户状态汇总（不调用任何会写库的方法）。"""
    state = storage.read_account_state()
    positions = storage.read_positions(status=PositionStatus.OPEN)
    cash = storage.ledger_balance()
    used_margin = calculate_used_margin(positions)
    if quote is not None:
        unrealized: Decimal | None = sum(
            (calculate_unrealized_pnl(p, quote) for p in positions), Decimal("0")
        )
        equity: Decimal | None = calculate_equity(cash, unrealized)
    else:
        unrealized = None
        equity = None
    margin_level = calculate_margin_level(equity, used_margin) if equity is not None else None
    trading_date = engine.trading_date().isoformat()
    baseline = storage.read_daily_baseline(trading_date)
    day_start = baseline.day_start_equity if baseline is not None else state.day_start_equity
    drawdown = None if equity is None or day_start <= 0 else (day_start - equity) / day_start
    return {
        "symbol": cfg.instrument.symbol,
        "currency": cfg.account.currency,
        "initial_capital": str(cfg.account.initial_capital_jpy),
        "cash_balance": str(cash),
        "realized_pnl": str(state.realized_pnl),
        "unrealized_pnl": None if unrealized is None else str(unrealized),
        "equity": None if equity is None else str(equity),
        "equity_last_known": str(state.equity),
        "used_margin": str(used_margin),
        "free_margin": None if equity is None else str(equity - used_margin),
        "margin_level": None if margin_level is None else str(margin_level),
        "peak_equity": str(state.peak_equity),
        "day_start_equity": str(day_start),
        "daily_drawdown_ratio": None if drawdown is None else str(drawdown),
        "trading_date": trading_date,
        "revision": state.revision,
        "status": state.status,
        "halted": state.halted,
        "halt_reason": state.halt_reason,
        "as_of": state.as_of.isoformat(timespec="seconds"),
        "quote_available": quote is not None,
        "quote_is_stale": bool(getattr(quote, "is_stale", False)) if quote else None,
        "open_positions": [
            {
                "position_id": p.position_id,
                "side": p.side,
                "volume_usd": str(p.volume),
                "entry_price": str(p.entry_price),
                "entry_at": p.entry_at.isoformat(timespec="seconds"),
                "stop_loss": None if p.stop_loss is None else str(p.stop_loss),
                "take_profit": None if p.take_profit is None else str(p.take_profit),
                "margin_jpy": str(p.margin_jpy),
                "planned_risk_jpy": None if p.planned_risk_jpy is None else str(p.planned_risk_jpy),
            }
            for p in positions
        ],
    }


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #


def _cmd_init(args: argparse.Namespace) -> int:
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    already = storage.is_initialized()
    state = storage.initialize_account(
        cfg.account.initial_capital_jpy, currency=cfg.account.currency
    )
    if already:
        print("账户已初始化（**未重复发放本金**）")
    else:
        print(
            f"账户已初始化，初始本金 "
            f"{cfg.account.initial_capital_jpy} {cfg.account.currency}"
        )
    print(f"  数据库      : {storage.db_path}")
    print(f"  现金余额    : {storage.ledger_balance()}")
    print(f"  净值        : {state.equity}")
    print(f"  状态        : {state.status}　revision={state.revision}")
    print("  提示        : 初始本金只发放一次，重复执行 init 不会增加余额。")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    if not storage.is_initialized():
        raise CliError("账户尚未初始化，请先执行：python -m main init")
    engine = _engine(cfg, storage)
    quote = None if args.no_quote else _read_only_quote(cfg)
    payload = _status_payload(cfg, storage, engine, quote)
    if args.as_json:
        _print_json(payload)
        return EXIT_OK

    print(f"品种            : {payload['symbol']}（{payload['currency']}）")
    print(f"账户状态        : {payload['status']}"
          + (f"　已停止开仓：{payload['halt_reason']}" if payload["halted"] else ""))
    print(f"交易日          : {payload['trading_date']}（08:00 JST 分界）")
    print(f"现金余额        : {payload['cash_balance']}")
    print(f"已实现盈亏      : {payload['realized_pnl']}")
    print(f"浮动盈亏        : {_fmt(payload['unrealized_pnl'])}")
    print(f"当前净值        : {_fmt(payload['equity'])}"
          + (f"（最后一次已知 {payload['equity_last_known']}）" if payload["equity"] is None else ""))
    print(f"已用/可用保证金 : {payload['used_margin']} / {_fmt(payload['free_margin'])}")
    print(f"保证金水平      : {_fmt(payload['margin_level'])}")
    print(f"峰值/当日起始净值: {payload['peak_equity']} / {payload['day_start_equity']}")
    print(f"当日回撤        : {_fmt(payload['daily_drawdown_ratio'])}")
    if quote is None:
        print("行情            : 不可用（浮动盈亏/净值/保证金水平无法核实）")
    else:
        print(f"行情            : {quote.bid}/{quote.ask}　来源 {quote.source}"
              f"　{'已过期' if quote.is_stale else '新鲜'}")
    positions = payload["open_positions"]
    if not positions:
        print("当前持仓        : 无")
    else:
        print(f"当前持仓        : {len(positions)} 笔")
        for item in positions:
            print(f"  #{item['position_id']} {item['side']} {item['volume_usd']} USD "
                  f"@ {item['entry_price']}　止损 {_fmt(item['stop_loss'])}　"
                  f"止盈 {_fmt(item['take_profit'])}　保证金 {item['margin_jpy']}")
    print(f"状态快照时间    : {payload['as_of']}")
    return EXIT_OK


def _cmd_quote(args: argparse.Namespace) -> int:
    cfg = load_config()
    try:
        quote = get_current_quote(cfg)
    except MarketDataError as exc:
        raise CliError(f"获取行情失败：{exc}") from exc
    payload = quote.as_dict()
    payload["spread_jpy"] = str(Decimal(quote.ask) - Decimal(quote.bid))
    payload["valid_for_decisions"] = None
    try:
        _engine(cfg, _resolve(cfg, args.db)).check_quote(quote)
        payload["valid_for_decisions"] = True
    except EngineError as exc:
        payload["valid_for_decisions"] = False
        payload["invalid_reason"] = str(exc)

    if args.as_json:
        _print_json(payload)
        return EXIT_OK
    print(f"品种        : {payload['symbol']}")
    print(f"Bid / Ask   : {payload['bid']} / {payload['ask']}")
    print(f"Mid         : {payload['mid']}　点差 {payload['spread_jpy']} 日元")
    print(f"报价时间戳  : {payload['timestamp']}")
    print(f"接收时间    : {payload['received_at']}")
    print(f"来源        : {payload['source']}")
    print(f"来源地址    : {payload['source_url']}")
    print(f"报价类型    : {payload['quote_type']}（{'模拟报价' if payload['quote_type'] == 'synthetic_spread' else '源站买卖价'}）")
    print(f"粒度        : {payload['granularity']}")
    print(f"是否过期    : {payload['is_stale']}　行情年龄 {payload['age_seconds']} 秒")
    print(f"可否用于决策: {payload['valid_for_decisions']}"
          + (f"（{payload.get('invalid_reason')}）" if not payload["valid_for_decisions"] else ""))
    return EXIT_OK


def _cmd_monitor_once(args: argparse.Namespace) -> int:
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    engine = _engine(cfg, storage)
    # 显式传入行情源，便于测试注入；行情失败时监控器会如实降级而不是崩溃。
    monitor = RiskMonitor(
        cfg, engine, storage=storage, quote_source=lambda: get_current_quote(cfg)
    )
    result = monitor.monitor_once(now=None)
    if args.as_json:
        _print_json(result.as_dict())
        return EXIT_OK
    print(f"交易日        : {result.trading_date}")
    print(f"检测时间      : {result.detected_at.isoformat(timespec='seconds')}")
    print(f"行情          : {'可用' if result.quote_ok else '不可用'}"
          f"（{'已过期' if result.quote_is_stale else '新鲜'}）")
    print(f"净值          : {_fmt(result.equity)}")
    print(f"保证金水平    : {_fmt(result.margin_level)}")
    print(f"当日回撤      : {_fmt(result.daily_drawdown_ratio)}")
    print(f"禁止新开仓    : {'是' if result.blocked_new_positions else '否'}"
          + (f"（{result.block_reason}）" if result.block_reason else ""))
    print(f"数据缺口      : {len(result.data_gaps)} 处")
    print(f"本次平仓      : {len(result.closed)} 笔")
    for item in result.closed:
        print(f"  #{item.position.position_id} 原因 {item.reason} "
              f"成交价 {item.trade.price} 盈亏 {item.realized_pnl_jpy}")
    print(f"风险事件      : {len(result.events)} 个")
    for event in result.events:
        print(f"  · {event.kind}：{event.reason}")
    for reason in result.degradation_reasons:
        print(f"  ⚠ {reason}")
    return EXIT_OK


def _open_position(args: argparse.Namespace, side: PositionSide) -> int:
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    engine = _engine(cfg, storage)
    if not storage.is_initialized():
        raise CliError("账户尚未初始化，请先执行：python -m main init")

    try:
        volume = Decimal(str(args.volume))
        stop = Decimal(str(args.stop))
        target = Decimal(str(args.take_profit))
    except InvalidOperation as exc:
        raise CliError(f"数量/止损/止盈必须是合法数字：{exc}") from exc

    reason = str(args.reason).strip()
    if not reason:
        raise CliError("必须通过 --reason 说明交易理由")

    intent = TradeIntent(
        symbol=cfg.instrument.symbol,
        side=side,
        volume_usd=volume,
        stop_loss=stop,
        take_profit=target,
        intent_id=args.intent_id,
        decision_ref=args.decision_ref,
        note=reason,
    )
    quote = _read_only_quote(cfg)
    if quote is None:
        raise CliError(
            "无法获取行情，拒绝开仓（不会用过期或缺失的报价撮合开仓）"
        )

    # 引擎会重新校验数量、止损止盈、盈亏比、账户状态、保证金与全部风险限额。
    execution = engine.execute_virtual_trade(intent, quote)
    if execution.is_duplicate:
        print(f"重复指令（intent_id={args.intent_id}），未重复开仓")
        print(f"  既有持仓 #{execution.position.position_id} {execution.position.side} "
              f"{execution.position.volume} @ {execution.position.entry_price}")
        return EXIT_OK

    validation = execution.validation
    position = execution.position
    print(f"已开{'多' if side is PositionSide.LONG else '空'}仓 #{position.position_id}")
    print(f"  成交价      : {position.entry_price}"
          f"（{'Ask' if side is PositionSide.LONG else 'Bid'}）")
    print(f"  数量        : {position.volume} USD")
    print(f"  止损 / 止盈 : {position.stop_loss} / {position.take_profit}")
    print(f"  理由        : {reason}")
    if validation is not None:
        print(f"  名义金额    : {validation.notional_jpy} 日元")
        print(f"  所需保证金  : {validation.required_margin_jpy} 日元")
        print(f"  计划止损风险: {validation.planned_risk_jpy} 日元"
              f"（上限 {validation.risk_limit_jpy}，含滑点 {validation.slippage_buffer_jpy}）")
        print(f"  计划盈亏比  : 1:{validation.reward_risk_ratio}")
        print(f"  开仓后净值  : {validation.equity_at_entry}（点差成本 {validation.spread_cost_jpy}）")
    print(f"  行情来源    : {quote.source}　报价类型 {getattr(quote.quote_type, 'value', quote.quote_type)}")
    return EXIT_OK


def _cmd_open_long(args: argparse.Namespace) -> int:
    return _open_position(args, PositionSide.LONG)


def _cmd_open_short(args: argparse.Namespace) -> int:
    return _open_position(args, PositionSide.SHORT)


def _cmd_close(args: argparse.Namespace) -> int:
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    engine = _engine(cfg, storage)
    positions = storage.read_positions(status=PositionStatus.OPEN)
    if not positions:
        raise CliError("当前没有未平仓持仓")
    if args.position_id is None:
        if len(positions) > 1:
            ids = "、".join(f"#{p.position_id}" for p in positions)
            raise CliError(f"存在多笔持仓（{ids}），请用 --position-id 指定")
        position = positions[0]
    else:
        position = next((p for p in positions if p.position_id == args.position_id), None)
        if position is None:
            raise CliError(f"未找到未平仓持仓 #{args.position_id}")

    reason = _CLOSE_REASON_BY_NAME[args.reason]
    quote = _read_only_quote(cfg)
    if quote is None:
        raise CliError("无法获取行情，拒绝平仓（成交价无法确定）")

    fee = None
    if args.fee is not None:
        try:
            fee = Decimal(str(args.fee))
        except InvalidOperation as exc:
            raise CliError(f"--fee 必须是合法数字：{exc}") from exc

    try:
        result = engine.close_position(
            position.position_id, quote, reason=reason, fee_jpy=fee
        )
    except PositionNotFoundError as exc:
        raise CliError(str(exc)) from exc

    if result.is_duplicate:
        print(f"持仓 #{position.position_id} 已平仓，未重复记账")
        return EXIT_OK
    print(f"已平仓 #{result.position.position_id}（原因 {result.reason}）")
    print(f"  成交价      : {result.trade.price}"
          f"（{'Bid' if position.side == 'LONG' else 'Ask'}）")
    print(f"  已实现盈亏  : {result.realized_pnl_jpy}")
    print(f"  平仓后现金  : {result.cash_balance_after}")
    print(f"  平仓后净值  : {result.equity_after}")
    return EXIT_OK


def _do_flow(args: argparse.Namespace, *, kind: str) -> int:
    """入金/出金的公共实现。任何校验失败都**不写数据**。"""
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    is_deposit = kind == FlowType.DEPOSIT.value
    label = "入金" if is_deposit else "出金"
    template = DEPOSIT_CONFIRM_TEMPLATE if is_deposit else WITHDRAW_CONFIRM_TEMPLATE

    amount, raw, canonical = _parse_flow_amount(args.amount, label=label)
    if is_deposit and amount > MAX_DEPOSIT_AMOUNT:
        raise CliError(
            f"单笔入金最高 {MAX_DEPOSIT_AMOUNT} 日元，收到 {amount}"
        )

    expected = template.format(amount=canonical)
    if not _confirm_matches(args.confirm, template, raw, canonical):
        raise CliError(
            f"{label}未确认：--confirm 必须与金额精确匹配。\n"
            f"  期望：--confirm \"{expected}\"\n"
            f"  收到：{args.confirm!r}"
        )

    if not storage.is_initialized():
        raise CliError("账户尚未初始化，请先执行：python -m main init")

    # 前置条件：无持仓、无待执行订单、账户状态允许
    _guard_flow_allowed(storage)

    cash_before = storage.ledger_balance()
    if not is_deposit:
        if amount > cash_before:
            raise CliError(
                f"出金 {amount} 超过现金余额 {cash_before}，已拒绝（未更改任何数据）"
            )

    event_key = f"CLI:{kind}:{args.request_id}" if args.request_id else None
    try:
        flow = storage.record_cash_flow(
            flow_type=kind,
            amount=amount if is_deposit else -amount,
            event_key=event_key,
            note=args.note or f"CLI {label}（已确认）",
            on_duplicate="ERROR",
        )
    except DuplicateEventError:
        print(f"重复请求（request_id={args.request_id}），未重复记账")
        print(f"  现金余额仍为 {cash_before}")
        return EXIT_OK

    cash_after = storage.ledger_balance()
    print(f"已{label} {amount} 日元（流水号 {flow.flow_no}）")
    print(f"  现金余额    : {cash_before} → {cash_after}")
    print(f"  账户净值    : {storage.read_account_state().equity}")
    print(f"  确认文字    : {expected}")
    print("  提示        : 出入金不计入投资收益，报告中会单独列示。")
    return EXIT_OK


def _cmd_deposit(args: argparse.Namespace) -> int:
    return _do_flow(args, kind=FlowType.DEPOSIT.value)


def _cmd_withdraw(args: argparse.Namespace) -> int:
    return _do_flow(args, kind=FlowType.WITHDRAWAL.value)


def _cmd_report_data(args: argparse.Namespace) -> int:
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    engine = _engine(cfg, storage)
    data = build_report_data(
        args.kind,
        cfg=cfg,
        engine=engine,
        storage=storage,
        quote=None if args.no_quote else _read_only_quote(cfg),
        skip_quote=args.no_quote,
        report_at=(datetime.fromisoformat(args.at) if args.at else None),
    )
    if args.as_json:
        _print_json(data.to_payload())
    else:
        a = data.account
        print(f"报告种类      : {data.kind.value}（{data.kind.label}）")
        print(f"报告时刻      : {data.report_at.astimezone(cfg.tz).isoformat(timespec='seconds')}")
        print(f"覆盖时段      : {data.period.start.astimezone(cfg.tz).isoformat(timespec='seconds')}"
              f" ~ {data.period.end.astimezone(cfg.tz).isoformat(timespec='seconds')}")
        print(f"行情          : {'可用' if data.market.quote_available else '不可用'}"
              f"　来源 {_fmt(data.market.source)}　过期 {_fmt(data.market.is_stale)}")
        print(f"初始本金      : {a.initial_capital}　累计入金 {a.total_deposits}"
              f"　累计出金 {a.total_withdrawals}")
        print(f"现金余额      : {a.cash_balance}　已实现盈亏 {a.realized_pnl}"
              f"　浮动盈亏 {_fmt(a.unrealized_pnl)}")
        print(f"当前净值      : {_fmt(a.equity)}　累计净利润 {_fmt(a.net_profit)}"
              f"　累计收益率 {_fmt(a.return_pct, '%')}")
        print(f"保证金        : 已用 {a.used_margin}　可用 {_fmt(a.free_margin)}"
              f"　水平 {_fmt(a.margin_level)}")
        print(f"交易成本      : 实际扣款 {data.costs.total_charged_jpy}"
              f"（含隐含点差 {data.costs.embedded_spread_jpy}）")
        print(f"持仓/成交/事件: {len(data.positions)} / {len(data.trades_in_period)} / {len(data.events)}")
        print(f"风险关注项    : {len(data.risk_watch)} 条")
        unverifiable = data.data_quality["unverifiable_fields"]
        if unverifiable:
            print(f"无法核实字段  : {len(unverifiable)} 个（快照中为 null，未伪装成 0）")

    if args.export:
        path = export_snapshot(data, out_dir=args.out_dir, cfg=cfg)
        template = path.with_suffix(".md")
        template.write_text(generate_report_template(data), encoding="utf-8")
        print(f"快照          : {path}")
        print(f"报告模板      : {template}")
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    if not storage.is_initialized():
        raise CliError("账户尚未初始化，请先执行：python -m main init")
    engine = _engine(cfg, storage)
    integrity = storage.verify_integrity()
    engine_report = engine.reconcile_account(strict=False)
    payload = {
        "integrity": integrity.as_dict(),
        "engine_reconciliation": engine_report.as_dict(),
        "audit_head": storage.audit_head(),
    }
    if args.as_json:
        _print_json(payload)
    else:
        print(f"数据库          : {storage.db_path}")
        print(f"SQLite 完整性   : {integrity.sqlite_integrity}")
        print(f"外键违规        : {len(integrity.foreign_key_violations)} 处")
        print(f"schema 版本     : {integrity.schema_version}")
        print(f"审计链          : 检查 {integrity.audit.checked} 条，"
              f"{'通过' if integrity.audit.ok else '异常'}，"
              f"头哈希 {integrity.audit.head_hash[:16]}…")
        print(f"账本            : {integrity.ledger.flow_count} 条流水，"
              f"余额 {integrity.ledger.ledger_balance}，"
              f"{'通过' if integrity.ledger.ok else '异常'}")
        print(f"账户核对        : {'通过' if integrity.reconciliation.ok else '不一致'}"
              f"（账本 {integrity.reconciliation.ledger_balance} / "
              f"缓存 {integrity.reconciliation.stored_balance}）")
        print(f"引擎级核对      : {'通过' if engine_report.ok else '不一致'}"
              f"（保证金投影差额 {engine_report.margin_difference}，"
              f"净值差额 {engine_report.equity_difference}）")
        for problem in integrity.problems:
            print(f"  [问题] {problem}")
        for problem in engine_report.problems:
            print(f"  [问题] {problem}")
        print(f"整体            : {'OK' if integrity.ok and engine_report.ok else 'FAILED'}")
    return EXIT_OK if (integrity.ok and engine_report.ok) else EXIT_FAILED


def _cmd_backup(args: argparse.Namespace) -> int:
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    target = storage.backup(args.dest)
    print(f"备份完成：{target}")
    print(f"  大小        : {target.stat().st_size} 字节")
    print("  校验        : SQLite 完整性、schema 版本与账本余额均已比对通过")
    return EXIT_OK


def _cmd_pressure(args: argparse.Namespace) -> int:
    """结算 / 查看「督促机制」（每 N 小时扣本金）。"""
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    if not storage.is_initialized():
        raise CliError("账户尚未初始化，请先执行：python -m main init")
    engine = _engine(cfg, storage)
    pressure = cfg.pressure

    if args.dry_run:
        result = engine.apply_capital_pressure(dry_run=True)
    else:
        result = engine.apply_capital_pressure(quote=None)

    if args.as_json:
        _print_json({
            "config": {
                "enabled": pressure.enabled,
                "interval_hours": pressure.interval_hours,
                "deduction_jpy": str(pressure.deduction_jpy),
                "catch_up_missed_slots": pressure.catch_up_missed_slots,
            },
            "result": result.as_dict(),
            "charged_total_jpy": str(engine.capital_pressure_total()),
            "last_charge_at": (
                engine.last_capital_pressure_at().isoformat(timespec="minutes")
                if engine.last_capital_pressure_at() is not None else None
            ),
        })
        return EXIT_OK

    print(f"督促机制        : {'开启' if pressure.enabled else '关闭'}")
    print(f"  间隔 / 金额   : 每 {pressure.interval_hours} 小时扣 "
          f"{pressure.deduction_jpy} 日元")
    print(f"  漏槽位补扣    : {'是' if pressure.catch_up_missed_slots else '否'}")
    print(f"  累计已扣      : {engine.capital_pressure_total()} 日元")
    last = engine.last_capital_pressure_at()
    print(f"  上次扣款      : {last.isoformat(timespec='minutes') if last else '（从未）'}")
    nxt = result.next_slot_at
    print(f"  下次扣款      : {nxt.isoformat(timespec='minutes') if nxt else '（机制关闭）'}")
    print(f"  现金余额      : {storage.ledger_balance()}")
    if result.applied:
        print(f"本次结算        : 已扣 {result.charged_jpy} 日元"
              f"（{'、'.join(result.slots)}）")
    else:
        print(f"本次结算        : 未扣款（{result.skipped_reason}）")
    if args.dry_run:
        print("  提示          : dry-run 只预演，没有真的扣款")
    return EXIT_OK


def _cmd_memory(args: argparse.Namespace) -> int:
    """查看 / 重建记忆文件投影（不碰账本）。"""
    from memory import MemoryStore

    cfg = load_config()
    storage = _resolve(cfg, args.db)
    if not storage.is_initialized():
        raise CliError("账户尚未初始化，请先执行：python -m main init")
    store = MemoryStore(cfg, storage=storage, root=args.root)

    if args.rebuild:
        result = store.rebuild_all()
        if args.as_json:
            _print_json({**result, "root": str(store.paths.root)})
            return EXIT_OK
        print(f"记忆目录        : {store.paths.root}")
        print(f"  笔记文件      : {result['notes']}")
        print(f"  小时文件      : {result['hours']}")
        print(f"  天索引        : {result['days']}")
        return EXIT_OK

    if args.as_json:
        _print_json({
            "root": str(store.paths.root),
            "note_files": [str(p) for p in store.list_note_files()],
            "hour_files": [str(p) for p in store.list_hour_files()],
            "notes_in_db": storage.count_notes(),
            "turns_in_db": storage.count_turns(),
            "hour_summaries_in_db": len(storage.list_hour_summary_keys()),
        })
        return EXIT_OK

    index = store.read_master_index()
    print(f"记忆目录        : {store.paths.root}")
    print(f"笔记（数据库）  : {storage.count_notes()} 条")
    print(f"小时摘要        : {len(storage.list_hour_summary_keys())} 个")
    print(f"对话存档        : {storage.count_turns()} 次决策")
    print(f"笔记文件        : {len(store.list_note_files())} 个")
    if not index:
        print()
        print("（还没有生成总索引。先跑一次决策，或执行：python -m main memory --rebuild）")
        return EXIT_OK
    print()
    print(index)
    return EXIT_OK


def _cmd_notes(args: argparse.Namespace) -> int:
    """查看 AI 自己写下的笔记（只读）。

    笔记是**模型的自述**，不是账本事实；本命令只是让人能看见模型记住了什么。
    """
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    if not storage.is_initialized():
        raise CliError("账户尚未初始化，请先执行：python -m main init")
    notes = storage.read_notes(kind=args.kind, limit=args.limit)
    if args.as_json:
        _print_json({
            "count_total": storage.count_notes(),
            "returned": len(notes),
            "notes": [n.as_dict() for n in notes],
        })
        return EXIT_OK
    total = storage.count_notes()
    print(f"数据库          : {storage.db_path}")
    print(f"笔记总数        : {total}（本次显示 {len(notes)} 条）")
    if not notes:
        print("（还没有笔记。模型在决策时调用 remember_note 就会写进来。）")
        return EXIT_OK
    for note in notes:
        tags = f"　[{' '.join(note.tags)}]" if note.tags else ""
        print(f"  #{note.note_no} {note.created_at.isoformat(timespec='minutes')} "
              f"{note.kind}{tags}")
        print(f"      {note.content}")
        if note.decision_id:
            print(f"      决策 {note.decision_id}")
    return EXIT_OK


#: 影响**账户正确性**与**强制平仓**的关键测试清单。
#: 任一项未通过，正式模拟账户必须保持禁止开仓（见 ``gate`` 命令）。
CRITICAL_TESTS: Final[tuple[str, ...]] = (
    "tests.test_integration.TestScenario01InitialCapital.test_initial_capital_granted_once",
    "tests.test_integration.TestScenario02UnrealizedPnl.test_long_unrealized_pnl_formula",
    "tests.test_integration.TestScenario02UnrealizedPnl.test_short_unrealized_pnl_formula",
    "tests.test_integration.TestScenario03SpreadAndSlippage.test_spread_is_not_deducted_twice",
    "tests.test_integration.TestScenario04TradeValidation.test_margin_is_notional_over_leverage",
    "tests.test_integration.TestScenario05StopTargetGap.test_stop_loss_normal_touch_fills_at_stop",
    "tests.test_integration.TestScenario05StopTargetGap.test_gap_over_stop_uses_executable_plus_slippage",
    "tests.test_integration.TestScenario06RiskLimits.test_margin_call_force_closes",
    "tests.test_integration.TestScenario06RiskLimits.test_daily_drawdown_closes_and_halts_for_the_day",
    "tests.test_integration.TestScenario06RiskLimits.test_bankruptcy_is_permanent_across_restart",
    "tests.test_integration.TestScenario08Idempotency.test_duplicate_monitor_event_not_executed_twice",
    "tests.test_integration.TestScenario09Restart.test_state_survives_restart",
    "tests.test_integration.TestScenario10NoFabrication.test_quote_failure_does_not_fabricate_a_close",
    "tests.test_integration.TestScenario11Integrity.test_transaction_rolls_back_completely",
    "tests.test_integration.TestScenario11Integrity.test_audit_chain_detects_tampering",
    "tests.test_integration.TestScenario12ScheduledTasks.test_repeated_wakeups_do_not_duplicate_report_tasks",
)


def _cmd_decide(args: argparse.Namespace) -> int:
    """让 AI 通过 function calling 提交一次交易决策，由引擎重新校验。

    默认 **dry-run**：只返回决策与校验结果，不真的开仓；加 ``--live`` 才执行。
    无论哪种模式，开仓都必须通过 :mod:`engine` 的完整风控校验。
    """
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    if not storage.is_initialized():
        raise CliError("账户尚未初始化，请先执行：python -m main init")
    engine = _engine(cfg, storage)
    agent = TradingAgent(cfg, engine=engine, storage=storage,
                         max_rounds=args.max_rounds)

    if not cfg.llm.ready:
        print("[LLM 未配置] 请设置 llm.base_url / llm.api_key / llm.model_id。", file=sys.stderr)
        print("  推荐用环境变量：KURUMI_LLM_BASE_URL / KURUMI_LLM_API_KEY / KURUMI_LLM_MODEL_ID",
              file=sys.stderr)
        print("  或写入已被 gitignore 的 config.local.json（见 README「LLM 接入」）",
              file=sys.stderr)
        return EXIT_USAGE

    if args.show_context:
        quote = _read_only_quote(cfg)
        if quote is None:
            raise CliError("无法获取行情，无法构造决策上下文")
        print("=== 喂给模型的上下文 ===")
        _print_json(build_decision_context(cfg, engine, storage, quote))
        print()

    result = agent.decide(
        decision_id=args.decision_id, dry_run=not args.live, max_rounds=args.max_rounds
    )
    if args.as_json:
        _print_json(result.as_dict())
        return EXIT_OK if result.ok else EXIT_FAILED

    print(f"决策编号    : {result.decision_id}")
    print(f"模式        : {'dry-run（未落单）' if result.dry_run else 'LIVE（已执行）'}")
    print(f"结果        : {result.outcome}")
    print(f"说明        : {result.message}")
    print(f"轮数        : {result.rounds}")
    for item in result.invocations:
        print(f"  · 第{item.round_index}轮 {item.name} → "
              f"{'通过' if item.accepted else '未通过'}")
        for problem in item.result.get("problems") or []:
            print(f"      - {problem}")
        if item.result.get("error"):
            print(f"      - {item.result['error']}")
    if result.validation and result.validation.ok:
        v = result.validation
        print(f"  成交价 {v.entry_price} | 保证金 {v.required_margin_jpy} | "
              f"计划风险 {v.planned_risk_jpy} | 盈亏比 1:{v.reward_risk_ratio}")
    if result.execution:
        print(f"  持仓 #{result.execution.position.position_id} "
              f"{result.execution.position.side} {result.execution.position.volume} "
              f"@ {result.execution.position.entry_price}")
    if result.usage:
        print(f"token 用量  : {result.usage}")
    for error in result.errors:
        print(f"  [错误] {error}")
    if result.dry_run and result.outcome == "VALIDATED":
        print()
        print("提示：这是 dry-run，未真实开仓。确认无误后加 --live 执行。")
    return EXIT_OK if result.ok else EXIT_FAILED


def _run_critical_tests() -> tuple[bool, list[str], list[str]]:
    """运行关键测试清单，返回 ``(是否全通过, 失败项, 加载失败项)``。"""
    suite = unittest.TestSuite()
    load_errors: list[str] = []
    for dotted in CRITICAL_TESTS:
        module_and_class, _, method = dotted.rpartition(".")
        module_name, _, class_name = module_and_class.rpartition(".")
        try:
            module = importlib.import_module(module_name)
            suite.addTest(getattr(module, class_name)(method))
        except Exception as exc:  # noqa: BLE001
            load_errors.append(f"{dotted}（{type(exc).__name__}: {exc}）")
    runner = unittest.TextTestRunner(verbosity=0, stream=io.StringIO())
    result = runner.run(suite)
    failures = [str(case) for case, _ in result.failures + result.errors]
    return (not failures and not load_errors), failures, load_errors


def _cmd_gate(args: argparse.Namespace) -> int:
    """验收闸门：关键测试全绿才允许正式模拟账户开仓。

    结果写入数据库 ``meta`` 表的 ``acceptance_gate`` 键：
    ``PASSED`` 或 ``BLOCKED:<原因>``。引擎在判定「是否停止开仓」时会读取它，
    因此**未通过时禁止开仓是真实生效的**，不会被下一次盯市按条件清掉。
    """
    cfg = load_config()
    storage = _resolve(cfg, args.db)
    print(f"验收闸门：运行 {len(CRITICAL_TESTS)} 项关键测试（账户正确性 / 强制平仓）")
    print(f"  数据库: {storage.db_path}")
    passed, failures, load_errors = _run_critical_tests()
    print(f"  结果  : {len(CRITICAL_TESTS) - len(failures) - len(load_errors)}"
          f"/{len(CRITICAL_TESTS)} 通过")
    for item in failures:
        print(f"  [失败] {item}")
    for item in load_errors:
        print(f"  [加载失败] {item}")

    if passed:
        storage.set_meta(ACCEPTANCE_GATE_META_KEY, "PASSED")
        print()
        print("闸门通过：正式模拟账户允许开仓（仍受全部风险规则约束）。")
        print("  提示：闸门只解除它自己设置的阻断；当日回撤停止、净值过低等")
        print("        由引擎按实时条件独立判定。")
        return EXIT_OK

    reason = "；".join((failures + load_errors)[:3]) or "未知原因"
    storage.set_meta(ACCEPTANCE_GATE_META_KEY, f"{GATE_BLOCKED_PREFIX}{reason}")
    print()
    print("闸门未通过：正式模拟账户**已禁止开仓**。")
    print(f"  阻断原因已写入 meta.{ACCEPTANCE_GATE_META_KEY}")
    print("  修复后重新运行本命令即可解除；查询、监控、平仓、报告不受影响。")
    return EXIT_FAILED


def _cmd_test(args: argparse.Namespace) -> int:
    """运行自动化测试。

    测试全部使用**临时数据库**，不触碰 ``data/fxsim.sqlite3``，也不会为真实账户
    发放本金或修改任何数据。
    """
    project_root = Path(__file__).resolve().parent
    top_level = project_root
    start_dir = project_root / "tests"
    if not start_dir.is_dir():  # pragma: no cover - 打包异常时
        raise CliError(f"找不到测试目录：{start_dir}")
    print(f"运行测试：{start_dir}（使用临时数据库，不影响真实账户）")
    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=str(start_dir), pattern=args.pattern, top_level_dir=str(top_level)
    )
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1, stream=sys.stdout)
    result = runner.run(suite)
    print(
        f"\n共 {result.testsRun} 个测试，"
        f"失败 {len(result.failures)}，错误 {len(result.errors)}，"
        f"跳过 {len(result.skipped)}"
    )
    return EXIT_OK if result.wasSuccessful() else EXIT_FAILED


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #


def _add_db_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db", default=None, help="数据库路径（默认取 config.json 的配置）"
    )


def _add_open_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--volume", required=True, help="数量（美元，必须是 1000 的整数倍）")
    parser.add_argument("--stop", required=True, help="止损价（必填）")
    parser.add_argument("--take-profit", required=True, dest="take_profit", help="止盈价（必填）")
    parser.add_argument("--reason", required=True, help="交易理由（必填，写入成交记录与审计）")
    parser.add_argument("--intent-id", default=None, dest="intent_id", help="幂等键（重复提交同一 id 不会重复开仓）")
    parser.add_argument("--decision-ref", default=None, dest="decision_ref", help="决策来源标识（可选）")
    _add_db_option(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m main",
        description=(
            "FX 战士久留美 —— 虚拟交易模拟器命令行入口。"
            "只做模拟交易，不连接任何真实券商、不下单、不做真实资金转账。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m main init\n"
            "  python -m main status\n"
            "  python -m main quote\n"
            "  python -m main deposit --amount 10000 --confirm \"确认入金 10000\"\n"
            "  python -m main open-long --volume 1000 --stop 157.950 \\\n"
            "      --take-profit 158.100 --reason \"15 分钟级别突破回踩\"\n"
            "  python -m main monitor-once\n"
            "  python -m main close --reason manual\n"
            "  python -m main withdraw --amount 5000 --confirm \"确认出金 5000\"\n"
            "  python -m main report-data --kind evening --export\n"
            "  python -m main verify\n"
            "  python -m main backup\n"
            "  python -m main test\n"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="输出 INFO 级日志")
    subparsers = parser.add_subparsers(dest="command", metavar="<命令>")

    p = subparsers.add_parser("init", help="初始化账户（只发放一次初始本金）")
    _add_db_option(p)
    p.set_defaults(handler=_cmd_init)

    p = subparsers.add_parser("status", help="显示账户状态（只读）")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    p.add_argument("--no-quote", action="store_true", dest="no_quote", help="不获取行情")
    _add_db_option(p)
    p.set_defaults(handler=_cmd_status)

    p = subparsers.add_parser("quote", help="显示最新行情及数据时间（只读）")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    _add_db_option(p)
    p.set_defaults(handler=_cmd_quote)

    p = subparsers.add_parser("monitor-once", help="执行一次风险监控")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    _add_db_option(p)
    p.set_defaults(handler=_cmd_monitor_once)

    p = subparsers.add_parser("open-long", help="提交模拟多仓指令（由引擎重新校验）")
    _add_open_arguments(p)
    p.set_defaults(handler=_cmd_open_long)

    p = subparsers.add_parser("open-short", help="提交模拟空仓指令（由引擎重新校验）")
    _add_open_arguments(p)
    p.set_defaults(handler=_cmd_open_short)

    p = subparsers.add_parser("close", help="请求平仓")
    p.add_argument("--position-id", type=int, default=None, dest="position_id", help="持仓编号（仅一笔时可省略）")
    p.add_argument("--reason", default="manual", choices=sorted(_CLOSE_REASON_BY_NAME), help="平仓原因")
    p.add_argument("--fee", default=None, help="独立手续费（日元，可选）")
    _add_db_option(p)
    p.set_defaults(handler=_cmd_close)

    for name, handler, label, verb in (
        ("deposit", _cmd_deposit, "申请虚拟入金（需严格确认）", "入金"),
        ("withdraw", _cmd_withdraw, "申请虚拟出金（需严格确认）", "出金"),
    ):
        p = subparsers.add_parser(name, help=label)
        p.add_argument("--amount", required=True, help="金额（日元，1000 的整数倍，最低 1000）")
        p.add_argument(
            "--confirm", required=True,
            help=f'确认文字，必须与金额精确匹配，例如 "确认{verb} 10000"',
        )
        p.add_argument("--request-id", default=None, dest="request_id", help="幂等键（重复提交同一 id 不会重复记账）")
        p.add_argument("--note", default=None, help="备注（可选）")
        _add_db_option(p)
        p.set_defaults(handler=handler)

    p = subparsers.add_parser("report-data", help="生成报告数据包（只读）")
    p.add_argument("--kind", default=None, help="morning / evening（默认按当前时刻推断）")
    p.add_argument("--at", default=None, help="报告时刻（ISO 8601，默认当前时间）")
    p.add_argument("--no-quote", action="store_true", dest="no_quote", help="不获取行情")
    p.add_argument("--export", action="store_true", help="同时导出 JSON 快照与报告模板")
    p.add_argument("--out-dir", default=None, dest="out_dir", help="快照输出目录")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出完整数据包")
    _add_db_option(p)
    p.set_defaults(handler=_cmd_report_data)

    p = subparsers.add_parser("verify", help="核对账户与审计日志（只读）")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    _add_db_option(p)
    p.set_defaults(handler=_cmd_verify)

    p = subparsers.add_parser(
        "pressure",
        help="结算 / 查看督促机制（每 N 小时扣本金）",
        description=(
            "督促机制：每隔固定小时数**无条件**从本金扣掉一笔钱，与行情、持仓、盈亏无关，"
            "目的是让「一直空仓观望」也有成本。扣款记为一笔 CAPITAL_DECAY 资金流水，"
            "因此会降低净值与累计净利润，但**不计入当日回撤**"
            "（否则一次扣款就会触发「当日停止开仓」，与督促行动的目的一致相反）。"
            "同一个槽位只扣一次，重复执行、进程重启都不会重复扣。"
        ),
    )
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="只预演，不真的扣款")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    _add_db_option(p)
    p.set_defaults(handler=_cmd_pressure)

    p = subparsers.add_parser(
        "memory",
        help="查看 / 重建模型的记忆文件投影（总索引 + 按天索引 + 每条记忆一个文件）",
        description=(
            "记忆的**权威数据**在数据库里（agent_notes / hour_summaries / agent_turns）；"
            "memory/ 目录是从它投影出来的可读文件。默认只查看总索引，"
            "加 --rebuild 才从数据库完整重建文件（幂等，删掉 memory/ 也能恢复）。"
        ),
    )
    p.add_argument("--rebuild", action="store_true", help="从数据库完整重建 memory/ 目录")
    p.add_argument("--root", default=None, help="记忆目录（默认 <项目根>/memory）")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    _add_db_option(p)
    p.set_defaults(handler=_cmd_memory)

    p = subparsers.add_parser(
        "notes",
        help="查看 AI 自己写下的笔记（只读）",
        description=(
            "列出模型通过 remember_note 工具写进长期记忆的笔记。这些内容是**模型的自述**，"
            "不是账本事实，也不会影响余额；查看它们只是为了让人类能看见模型记住了什么。"
            "查看可读的文件版本请用 `memory` 命令。"
        ),
    )
    p.add_argument("--limit", type=int, default=20, help="最多显示多少条（默认 20）")
    p.add_argument("--kind", default=None,
                   help="只看某一类：LESSON / DECISION_REASON / MARKET_VIEW / MISTAKE / PLAN")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    _add_db_option(p)
    p.set_defaults(handler=_cmd_notes)

    p = subparsers.add_parser("backup", help="备份数据库并校验")
    p.add_argument("--dest", default=None, help="备份目标路径（默认写入 data/backups/）")
    _add_db_option(p)
    p.set_defaults(handler=_cmd_backup)

    p = subparsers.add_parser(
        "decide",
        help="让 AI 通过 function calling 提交一次决策（默认 dry-run）",
        description=(
            "让语言模型通过 function calling 提交一次交易决策。默认 dry-run："
            "只输出决策与引擎风控校验结果，不真的开仓；加 --live 才执行。"
            "无论哪种模式，开仓都必须通过 engine 的完整风控校验，命令行无法绕过。"
        ),
    )
    p.add_argument("--live", action="store_true", help="真的执行（默认为 dry-run，不落单）")
    p.add_argument("--decision-id", default=None, dest="decision_id", help="幂等键")
    p.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS, dest="max_rounds",
                   help="最大决策轮数（含被拒后的修正重试）")
    p.add_argument("--show-context", action="store_true", dest="show_context",
                   help="同时打印喂给模型的上下文")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    _add_db_option(p)
    p.set_defaults(handler=_cmd_decide)

    p = subparsers.add_parser(
        "gate", help="验收闸门：关键测试全绿才允许正式账户开仓"
    )
    _add_db_option(p)
    p.set_defaults(handler=_cmd_gate)

    p = subparsers.add_parser("test", help="运行自动化测试（使用临时数据库）")
    p.add_argument("--pattern", default="test_*.py", help="测试文件匹配模式")
    p.set_defaults(handler=_cmd_test)

    return parser


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    handler = getattr(args, "handler", None)
    if handler is None:
        print(f"kurumi_fx_sim 虚拟交易模拟器 v{__version__}（仅模拟，不接真实券商）")
        print()
        parser.print_help()
        return EXIT_USAGE

    try:
        return int(handler(args))
    except CliError as exc:
        print(f"[命令错误] {exc}", file=sys.stderr)
        return EXIT_FAILED
    except TradeRejectedError as exc:
        print("[交易被拒绝] 引擎重新校验未通过：", file=sys.stderr)
        for problem in exc.validation.problems:
            print(f"  · {problem}", file=sys.stderr)
        return EXIT_FAILED
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return EXIT_USAGE
    except MarketDataError as exc:
        print(f"[行情错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except StorageError as exc:
        print(f"[存储错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except EngineError as exc:
        print(f"[引擎错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        print("\n已中断", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - CLI 不应抛出裸异常
        print(f"[未预期的错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
