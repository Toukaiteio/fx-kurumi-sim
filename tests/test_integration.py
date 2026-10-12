"""端到端集成测试：把九个模块串起来跑完整流程。

目的不是重复各模块的单元测试，而是验证**模块之间的组合**在真实调用路径上正确，
覆盖需求列出的 12 种情形：

1. 初始本金只发放一次
2. 多仓与空仓的浮动盈亏
3. Bid/Ask 点差与滑点没有重复扣款
4. 开仓数量、保证金、止损与交易风险校验
5. 止损、止盈与跳空成交处理
6. 保证金预警、强制平仓、日内回撤限制、爆仓状态
7. 合法与非法资金操作
8. 同一指令重复提交不重复成交
9. 重启后账户、持仓与历史记录恢复
10. 数据源失效或历史不完整时不伪造交易结果
11. 账本核对、事务、快照恢复、审计日志验证
12. 08:00 / 20:00 JST 定时任务不重复执行

全部使用**独立临时数据库与模拟行情**：不访问网络、不连接券商、不做真实交易，
也不触碰 ``data/fxsim.sqlite3``。

运行方式（项目根目录 E:\\pj2\\fx-kurumi-sim）::

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import main as cli  # noqa: E402
from config import load_config  # noqa: E402
from engine import (  # noqa: E402
    CloseReason,
    EngineError,
    TradeIntent,
    TradeRejectedError,
    TradingEngine,
    calculate_equity,
    calculate_margin_level,
    calculate_realized_pnl,
    calculate_required_margin,
    calculate_unrealized_pnl,
    calculate_used_margin,
    trading_date_for,
)
from market_data import (  # noqa: E402
    Bar,
    BarSeries,
    Granularity,
    HistoryUnavailableError,
    MarketDataError,
    Quote,
    QuoteType,
)
from reporting import (  # noqa: E402
    build_report_data,
    export_snapshot,
    generate_report_template,
    verify_snapshot,
)
from risk_monitor import (  # noqa: E402
    TASK_NAME as _MONITOR_TASK,
    RiskEventKind,
    RiskMonitor,
)
from scheduler import REPORT_TASK_PREFIX, Scheduler  # noqa: E402
from storage import (  # noqa: E402
    AccountStatus,
    FlowType,
    INITIAL_CAPITAL_EVENT_KEY,
    PositionSide,
    PositionStatus,
    RunStatus,
    Storage,
    StorageError,
)

_JST = timezone(timedelta(hours=9))
_INITIAL = Decimal("50000")


# --------------------------------------------------------------------------- #
# 集成夹具：一条完整的模块链
# --------------------------------------------------------------------------- #


class Harness:
    """把 storage → engine → risk_monitor → reporting → scheduler 串起来的测试夹具。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cfg = load_config()
        self.db = root / "data" / "fxsim.sqlite3"
        self.reports = root / "reports"
        self.state = root / "state"
        self.storage = Storage(self.db, tz=_JST)
        self.engine = TradingEngine(self.cfg, self.storage)
        self.scripted_quotes: list[Quote | BaseException] = []
        self.scripted_bars: list[Bar] = []
        self.bars_error: BaseException | None = None
        self.monitor = RiskMonitor(
            self.cfg,
            self.engine,
            storage=self.storage,
            quote_source=self.next_quote,
            bars_source=self.bars_source,
            logger_=logging.getLogger("test.integration"),
        )
        self.scheduler = Scheduler(
            self.cfg,
            storage=self.storage,
            engine=self.engine,
            monitor=self.monitor,
            reports_dir=self.reports,
            state_dir=self.state,
            logger_=logging.getLogger("test.integration"),
        )

    # -- 生命周期 ---------------------------------------------------------- #

    def initialize(self, *, opened_at: datetime | None = None) -> None:
        self.storage.initialize_account(
            self.cfg.account.initial_capital_jpy,
            opened_at=opened_at or datetime(2026, 10, 10, 8, 0, tzinfo=_JST),
        )

    def reopen(self) -> Harness:
        """模拟进程重启：在同一个数据库上重建全部对象。"""
        fresh = Harness.__new__(Harness)
        fresh.root = self.root
        fresh.cfg = self.cfg
        fresh.db = self.db
        fresh.reports = self.reports
        fresh.state = self.state
        fresh.storage = Storage(self.db, tz=_JST)
        fresh.engine = TradingEngine(fresh.cfg, fresh.storage)
        fresh.scripted_quotes = []
        fresh.scripted_bars = []
        fresh.bars_error = None
        fresh.monitor = RiskMonitor(
            fresh.cfg, fresh.engine, storage=fresh.storage,
            quote_source=fresh.next_quote, bars_source=fresh.bars_source,
            logger_=logging.getLogger("test.integration"),
        )
        fresh.scheduler = Scheduler(
            fresh.cfg, storage=fresh.storage, engine=fresh.engine, monitor=fresh.monitor,
            reports_dir=fresh.reports, state_dir=fresh.state,
            logger_=logging.getLogger("test.integration"),
        )
        return fresh

    # -- 行情 -------------------------------------------------------------- #

    def quote(self, bid: str = "158.000", ask: str = "158.010", *, stale: bool = False) -> Quote:
        bid_d, ask_d = Decimal(bid), Decimal(ask)
        return Quote(
            symbol=self.cfg.instrument.symbol, bid=bid_d, ask=ask_d, mid=(bid_d + ask_d) / 2,
            timestamp=datetime.now(_JST), source="integration_test",
            source_url="https://example.invalid/", quote_type=QuoteType.BID_ASK,
            granularity=Granularity.TICK, is_stale=stale,
            age_seconds=Decimal("999") if stale else Decimal("0"),
        )

    def next_quote(self) -> Quote:
        if self.scripted_quotes:
            item = self.scripted_quotes.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return self.quote()

    def bars_source(self, start: datetime, end: datetime) -> BarSeries:
        if self.bars_error is not None:
            raise self.bars_error
        return BarSeries(
            symbol=self.cfg.instrument.symbol, interval="1m", bars=tuple(self.scripted_bars),
            source="integration_bars", source_url="", requested_start=start, requested_end=end,
        )

    def bar(self, moment: datetime, *, open_="158.000", high="158.020",
            low="157.400", close="157.500") -> Bar:
        return Bar(
            timestamp=moment, open=Decimal(open_), high=Decimal(high), low=Decimal(low),
            close=Decimal(close), source="integration_bars", interval="1m",
        )

    # -- 交易 -------------------------------------------------------------- #

    def intent(self, side=PositionSide.LONG, *, volume="1000", stop=None, target=None,
               intent_id=None) -> TradeIntent:
        if stop is None:
            stop = "157.950" if side is PositionSide.LONG else "158.060"
        if target is None:
            target = "158.100" if side is PositionSide.LONG else "157.910"
        return TradeIntent(
            symbol=self.cfg.instrument.symbol, side=side, volume_usd=Decimal(volume),
            stop_loss=Decimal(stop), take_profit=Decimal(target), intent_id=intent_id,
        )

    def open(self, side=PositionSide.LONG, *, quote=None, **kwargs):
        return self.engine.execute_virtual_trade(
            self.intent(side, **kwargs), quote or self.quote(),
            occurred_at=datetime(2026, 10, 10, 9, 0, tzinfo=_JST),
        )

    def at(self, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 10, 10, hour, minute, tzinfo=_JST)

    def cli(self, *argv: str) -> tuple[int, str, str]:
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--db", str(self.db)])
        return code, out.getvalue(), err.getvalue()


class IntegrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.h = Harness(self.root)
        self.h.initialize()


# --------------------------------------------------------------------------- #
# 1. 初始本金只发放一次
# --------------------------------------------------------------------------- #


class TestScenario01InitialCapital(IntegrationTestCase):
    def test_initial_capital_granted_once(self) -> None:
        for _ in range(5):
            self.h.storage.initialize_account(self.h.cfg.account.initial_capital_jpy)
        self.assertEqual(self.h.storage.ledger_balance(), _INITIAL)
        flows = self.h.storage.read_cash_flows()
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0].event_key, INITIAL_CAPITAL_EVENT_KEY)
        self.assertEqual(flows[0].amount, _INITIAL)

    def test_initial_capital_matches_config(self) -> None:
        self.assertEqual(self.h.storage.ledger_balance(), self.h.cfg.account.initial_capital_jpy)

    def test_reinitialization_with_other_amount_refused(self) -> None:
        from storage import AccountAlreadyInitializedError

        with self.assertRaises(AccountAlreadyInitializedError):
            self.h.storage.initialize_account(Decimal("999999"))
        self.assertEqual(self.h.storage.ledger_balance(), _INITIAL)

    def test_engine_ensure_initialized_is_idempotent(self) -> None:
        for _ in range(3):
            self.h.engine.ensure_initialized()
        self.assertEqual(self.h.storage.ledger_balance(), _INITIAL)
        self.assertEqual(len(self.h.storage.read_cash_flows()), 1)

    def test_init_cli_grants_once(self) -> None:
        for _ in range(3):
            code, _, _ = self.h.cli("init")
            self.assertEqual(code, 0)
        self.assertEqual(self.h.storage.ledger_balance(), _INITIAL)


# --------------------------------------------------------------------------- #
# 2. 多仓与空仓的浮动盈亏
# --------------------------------------------------------------------------- #


class TestScenario02UnrealizedPnl(IntegrationTestCase):
    def test_long_unrealized_pnl_formula(self) -> None:
        position = self.h.open(PositionSide.LONG).position
        self.assertEqual(position.entry_price, Decimal("158.010"))  # 开多走 Ask
        # (当前 Bid − 开仓 Ask) × 数量
        pnl = calculate_unrealized_pnl(position, self.h.quote("158.500", "158.510"))
        self.assertEqual(pnl, Decimal("490.000"))
        self.assertEqual(pnl, (Decimal("158.500") - Decimal("158.010")) * Decimal("1000"))
        # 价格不动时浮亏恰为点差成本
        self.assertEqual(
            calculate_unrealized_pnl(position, self.h.quote("158.000", "158.010")),
            Decimal("-10.000"),
        )

    def test_short_unrealized_pnl_formula(self) -> None:
        position = self.h.open(PositionSide.SHORT).position
        self.assertEqual(position.entry_price, Decimal("158.000"))  # 开空走 Bid
        # (开仓 Bid − 当前 Ask) × 数量
        pnl = calculate_unrealized_pnl(position, self.h.quote("157.500", "157.510"))
        self.assertEqual(pnl, Decimal("490.000"))
        self.assertEqual(pnl, (Decimal("158.000") - Decimal("157.510")) * Decimal("1000"))
        self.assertEqual(
            calculate_unrealized_pnl(position, self.h.quote("158.000", "158.010")),
            Decimal("-10.000"),
        )

    def test_realized_equals_unrealized_at_close(self) -> None:
        execution = self.h.open(PositionSide.LONG)
        quote = self.h.quote("158.700", "158.710")
        unrealized = calculate_unrealized_pnl(execution.position, quote)
        closed = self.h.engine.close_position(execution.position.position_id, quote)
        self.assertEqual(closed.realized_pnl_jpy, unrealized)

    def test_equity_and_margin_level_chain(self) -> None:
        self.h.open(PositionSide.LONG)
        quote = self.h.quote("158.500", "158.510")
        cash = self.h.storage.ledger_balance()
        unrealized = sum(
            (calculate_unrealized_pnl(p, quote) for p in self.h.storage.read_positions()), Decimal("0")
        )
        equity = calculate_equity(cash, unrealized)
        self.assertEqual(equity, Decimal("50490.000"))
        used = calculate_used_margin(self.h.storage.read_positions())
        self.assertEqual(used, Decimal("6320.400"))
        self.assertEqual(calculate_margin_level(equity, used), equity / used)


# --------------------------------------------------------------------------- #
# 3. 点差与滑点没有重复扣款
# --------------------------------------------------------------------------- #


class TestScenario03SpreadAndSlippage(IntegrationTestCase):
    def test_spread_is_not_deducted_twice(self) -> None:
        """开仓 Ask 与平仓 Bid 相同时盈亏为 0，余额必须回到本金。"""
        execution = self.h.open(PositionSide.LONG)
        self.h.engine.close_position(execution.position.position_id, self.h.quote("158.010", "158.020"))
        self.assertEqual(self.h.storage.ledger_balance(), _INITIAL)

    def test_slippage_not_charged_as_separate_fee(self) -> None:
        execution = self.h.open(PositionSide.LONG)
        self.h.engine.close_position(execution.position.position_id, self.h.quote("158.510", "158.520"))
        # 盈亏 (158.510-158.010)×1000 = 500，与滑点 0.005 无关
        self.assertEqual(self.h.storage.ledger_balance(), _INITIAL + Decimal("500"))
        flow_types = [f.flow_type for f in self.h.storage.read_cash_flows()]
        self.assertEqual(flow_types, ["DEPOSIT", "REALIZED_PNL"])
        self.assertNotIn("SPREAD", flow_types)
        self.assertNotIn("SLIPPAGE", flow_types)

    def test_cost_breakdown_separates_charged_and_embedded(self) -> None:
        self.h.engine.record_standalone_fee(Decimal("120"), fee_id="fee-1")
        execution = self.h.open(PositionSide.LONG)
        self.h.engine.close_position(execution.position.position_id, self.h.quote("158.010", "158.020"))
        data = build_report_data(cfg=self.h.cfg, engine=self.h.engine, storage=self.h.storage,
                                 quote=self.h.quote())
        # 实际扣款只有手续费
        self.assertEqual(data.costs.total_charged_jpy, Decimal("120.000"))
        # 隐含点差：一开一平 = 一个完整点差 × 数量 = 0.01 × 1000
        self.assertEqual(data.costs.embedded_spread_jpy, Decimal("10.000"))
        self.assertEqual(
            data.costs.total_including_embedded_jpy, Decimal("130.000")
        )
        # 余额 = 本金 − 手续费（点差已体现在 0 盈亏里，没有重复扣）
        self.assertEqual(self.h.storage.ledger_balance(), _INITIAL - Decimal("120"))

    def test_trade_rows_record_spread_and_slippage_for_audit(self) -> None:
        execution = self.h.open(PositionSide.LONG)
        self.h.engine.close_position(execution.position.position_id, self.h.quote())
        trades = self.h.storage.query_trades()
        self.assertEqual(len(trades), 2)
        for trade in trades:
            self.assertEqual(trade.spread_jpy, Decimal("0.010"))
            self.assertEqual(trade.quoted_price, Decimal("158.005"))  # mid


# --------------------------------------------------------------------------- #
# 4. 开仓数量、保证金、止损与风险校验
# --------------------------------------------------------------------------- #


class TestScenario04TradeValidation(IntegrationTestCase):
    def validate(self, **kwargs):
        return self.h.engine.validate_trade_intent(
            self.h.intent(**kwargs), self.h.quote()
        )

    def test_volume_must_be_multiple_of_min_unit(self) -> None:
        self.assertFalse(self.validate(volume="1500").ok)
        self.assertTrue(any("正整数倍" in p for p in self.validate(volume="1500").problems))
        self.assertTrue(self.validate(volume="2000", stop="157.515", target="158.760").ok)

    def test_margin_is_notional_over_leverage(self) -> None:
        result = self.validate()
        self.assertEqual(result.notional_jpy, Decimal("158010.000"))
        self.assertEqual(result.required_margin_jpy, Decimal("6320.400"))
        self.assertEqual(
            result.required_margin_jpy,
            calculate_required_margin(Decimal("1000"), Decimal("158.010"), self.h.cfg),
        )
        self.assertEqual(
            result.required_margin_jpy,
            result.notional_jpy / self.h.cfg.risk.max_leverage,
        )

    def test_stop_and_target_required(self) -> None:
        intent = TradeIntent(symbol="USD/JPY", side=PositionSide.LONG,
                             volume_usd=Decimal("1000"))
        result = self.h.engine.validate_trade_intent(intent, self.h.quote())
        self.assertFalse(result.ok)
        self.assertTrue(any("必须设置止损价" in p for p in result.problems))
        self.assertTrue(any("必须设置止盈价" in p for p in result.problems))

    def test_reward_risk_ratio_at_least_one_to_one_point_five(self) -> None:
        bad = self.validate(stop="157.500", target="158.020")  # 盈亏比 0.33
        self.assertFalse(bad.ok)
        self.assertTrue(any("计划盈亏比" in p for p in bad.problems))
        good = self.validate(stop="157.500", target="158.775")  # 恰好 1.5
        self.assertTrue(good.ok)
        self.assertEqual(good.reward_risk_ratio, Decimal("1.5"))

    def test_stop_risk_includes_adverse_slippage(self) -> None:
        result = self.validate()  # 默认止损 157.950，开仓 Ask 158.010
        self.assertEqual(result.stop_distance_jpy, Decimal("0.060"))
        self.assertEqual(result.slippage_buffer_jpy, self.h.cfg.market.slippage_jpy)
        self.assertEqual(result.risk_per_unit_jpy, Decimal("0.065"))
        self.assertEqual(result.planned_risk_jpy, Decimal("65.000"))
        # 不含滑点会是 60；含滑点后是 65
        self.assertEqual(result.planned_risk_jpy, (Decimal("0.060") + self.h.cfg.market.slippage_jpy) * Decimal("1000"))
        # 上限 = 净值的 2%
        self.assertEqual(result.risk_limit_jpy, _INITIAL * Decimal("0.02"))

    def test_per_trade_risk_limit_enforced(self) -> None:
        result = self.validate(volume="2000", stop="157.000", target="159.000")
        self.assertFalse(result.ok)
        self.assertTrue(any("单笔计划止损风险" in p for p in result.problems))

    def test_margin_usage_limit_enforced(self) -> None:
        result = self.validate(volume="5000", stop="157.950", target="158.100")
        self.assertFalse(result.ok)
        self.assertTrue(any("已用保证金" in p for p in result.problems))

    def test_only_one_position_allowed(self) -> None:
        self.h.open(PositionSide.LONG)
        for side in (PositionSide.LONG, PositionSide.SHORT):
            result = self.h.engine.validate_trade_intent(
                self.h.intent(side, intent_id=f"x-{side.value}"), self.h.quote()
            )
            self.assertFalse(result.ok)
            self.assertTrue(any("禁止加仓" in p for p in result.problems))

    def test_engine_rejects_and_writes_nothing(self) -> None:
        with self.assertRaises(TradeRejectedError):
            self.h.engine.execute_virtual_trade(self.h.intent(volume="1500"), self.h.quote())
        self.assertEqual(self.h.storage.counts()["positions"], 0)
        self.assertEqual(self.h.storage.counts()["trades"], 0)
        self.assertEqual(self.h.storage.ledger_balance(), _INITIAL)
        self.assertIn("TRADE_REJECTED", [e.event_type for e in self.h.storage.read_audit_events()])


# --------------------------------------------------------------------------- #
# 5. 止损、止盈与跳空成交
# --------------------------------------------------------------------------- #


class TestScenario05StopTargetGap(IntegrationTestCase):
    def test_stop_loss_normal_touch_fills_at_stop(self) -> None:
        self.h.open(PositionSide.LONG, stop="157.950", target="158.100")
        self.h.monitor.monitor_once(quote=self.h.quote("158.000", "158.010"))
        result = self.h.monitor.monitor_once(quote=self.h.quote("157.900", "157.910"))
        self.assertTrue(result.did_close)
        event = result.events[0]
        self.assertEqual(event.kind, RiskEventKind.STOP_LOSS.value)
        self.assertFalse(event.is_gap)
        self.assertEqual(event.fill_price, Decimal("157.950"))
        self.assertEqual(event.realized_pnl_jpy, Decimal("-60.000"))

    def test_take_profit_fills_at_target(self) -> None:
        self.h.open(PositionSide.SHORT, stop="158.060", target="157.910")
        self.h.monitor.monitor_once(quote=self.h.quote())
        result = self.h.monitor.monitor_once(quote=self.h.quote("157.900", "157.910"))
        event = result.events[0]
        self.assertEqual(event.kind, RiskEventKind.TAKE_PROFIT.value)
        self.assertEqual(event.fill_price, Decimal("157.910"))
        self.assertEqual(event.realized_pnl_jpy, Decimal("90.000"))

    def test_gap_over_stop_uses_executable_plus_slippage(self) -> None:
        self.h.open(PositionSide.LONG, stop="157.950", target="158.100")
        # 首次观测即已在止损不利侧 → 无法确认此前价位 → 按跳空处理
        result = self.h.monitor.monitor_once(quote=self.h.quote("157.000", "157.010"))
        event = result.events[0]
        self.assertTrue(event.is_gap)
        self.assertEqual(event.fill_price, Decimal("156.995"))  # 157.000 − 0.005
        self.assertEqual(event.realized_pnl_jpy, Decimal("-1015.000"))
        self.assertIn("跳空", event.reason)

    def test_bar_straddling_stop_fills_at_stop(self) -> None:
        position = self.h.open(PositionSide.LONG, stop="157.950", target="158.100").position
        trigger = self.h.monitor._bar_trigger(
            self.h.storage.read_position(position.position_id),
            self.h.bar(self.h.at(10), open_="158.000", high="158.020", low="157.400", close="157.500"),
        )
        self.assertEqual(trigger.reason, CloseReason.STOP_LOSS)
        self.assertFalse(trigger.is_gap)
        self.assertEqual(trigger.fill_price, Decimal("157.950"))

    def test_ambiguous_bar_prefers_stop_loss(self) -> None:
        position = self.h.open(PositionSide.LONG, stop="157.950", target="158.100").position
        trigger = self.h.monitor._bar_trigger(
            self.h.storage.read_position(position.position_id),
            self.h.bar(self.h.at(10), open_="158.000", high="158.200", low="157.400", close="157.900"),
        )
        self.assertEqual(trigger.reason, CloseReason.STOP_LOSS)  # 同时触及 → 止损优先

    def test_backfill_closes_position_missed_during_outage(self) -> None:
        self.h.open(PositionSide.LONG, stop="157.950", target="158.100")
        now = datetime.now(_JST)
        start = now - timedelta(minutes=10)
        self.h.scripted_bars = [self.h.bar(start + timedelta(minutes=i)) for i in range(11)]
        self.h.monitor._last_check_at = start
        result = self.h.monitor.monitor_once(quote=self.h.quote(), now=now)
        self.assertTrue(result.backfilled)
        stops = [e for e in result.events if e.kind == RiskEventKind.STOP_LOSS.value]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].fill_price, Decimal("157.950"))
        self.assertEqual(self.h.storage.read_positions(status=PositionStatus.OPEN), [])


# --------------------------------------------------------------------------- #
# 6. 保证金预警、强制平仓、日内回撤、爆仓
# --------------------------------------------------------------------------- #


class TestScenario06RiskLimits(IntegrationTestCase):
    def adjust_margin_level(self, target: Decimal, key: str) -> None:
        margin = calculate_used_margin(self.h.storage.read_positions())
        desired = (margin * target).quantize(Decimal("0.001"))
        delta = self.h.storage.read_account_state().equity - desired
        self.h.storage.record_cash_flow(
            flow_type=FlowType.WITHDRAWAL if delta > 0 else FlowType.DEPOSIT,
            amount=-delta if delta > 0 else -delta,
            event_key=key,
        )

    def test_margin_warning_blocks_new_positions(self) -> None:
        self.h.monitor.monitor_once(quote=self.h.quote())  # 确立当日基准
        self.h.storage.record_cash_flow(flow_type=FlowType.DEPOSIT, amount=Decimal("40000"),
                                        event_key="dep-w")
        self.h.open(PositionSide.LONG, volume="8000", stop="157.950", target="158.100")
        self.adjust_margin_level(Decimal("1.45"), "wd-w")
        result = self.h.monitor.monitor_once(quote=self.h.quote())
        self.assertIn(RiskEventKind.MARGIN_WARNING.value, [e.kind for e in result.events])
        self.assertTrue(result.blocked_new_positions)
        self.assertFalse(result.did_close)  # 只预警，不强制平仓

    def test_margin_call_force_closes(self) -> None:
        self.h.monitor.monitor_once(quote=self.h.quote())
        self.h.storage.record_cash_flow(flow_type=FlowType.DEPOSIT, amount=Decimal("40000"),
                                        event_key="dep-mc")
        self.h.open(PositionSide.LONG, volume="8000", stop="157.950", target="158.100")
        self.adjust_margin_level(Decimal("0.95"), "wd-mc")
        result = self.h.monitor.monitor_once(quote=self.h.quote())
        self.assertTrue(result.did_close)
        self.assertEqual(result.closed[0].reason, CloseReason.MARGIN_CALL.value)
        self.assertEqual(self.h.storage.read_positions(status=PositionStatus.OPEN), [])
        self.assertEqual(result.closed[0].trade.reason, CloseReason.MARGIN_CALL.value)

    def test_daily_drawdown_closes_and_halts_for_the_day(self) -> None:
        self.h.monitor.monitor_once(quote=self.h.quote())
        self.h.open(PositionSide.LONG)
        self.h.storage.record_cash_flow(flow_type=FlowType.WITHDRAWAL, amount=Decimal("-1500"),
                                        event_key="wd-dd")
        result = self.h.monitor.monitor_once(quote=self.h.quote())
        self.assertTrue(result.did_close)
        self.assertEqual(result.closed[0].reason, CloseReason.DAILY_DRAWDOWN.value)
        baseline = self.h.storage.read_daily_baseline(self.h.monitor.trading_date())
        self.assertTrue(baseline.drawdown_stop_triggered)
        state = self.h.storage.read_account_state()
        self.assertTrue(state.halted)
        self.assertEqual(state.status, AccountStatus.HALTED.value)
        with self.assertRaises(TradeRejectedError):
            self.h.engine.execute_virtual_trade(self.h.intent(intent_id="after-dd"), self.h.quote())

    def test_drawdown_halt_survives_equity_recovery_within_the_day(self) -> None:
        self.h.monitor.monitor_once(quote=self.h.quote())
        self.h.engine.set_daily_drawdown_stop()
        self.h.storage.record_cash_flow(flow_type=FlowType.DEPOSIT, amount=Decimal("10000"),
                                        event_key="dep-rec")
        result = self.h.monitor.monitor_once(quote=self.h.quote())
        self.assertTrue(result.blocked_new_positions)
        self.assertTrue(self.h.storage.read_account_state().halted)

    def test_bankruptcy_is_permanent_across_restart(self) -> None:
        self.h.monitor.monitor_once(quote=self.h.quote())
        self.h.open(PositionSide.LONG)
        self.h.storage.record_cash_flow(flow_type=FlowType.WITHDRAWAL, amount=Decimal("-51000"),
                                        event_key="wd-bk")
        result = self.h.monitor.monitor_once(quote=self.h.quote())
        self.assertIn(RiskEventKind.BANKRUPTCY.value, [e.kind for e in result.events])
        self.assertEqual(self.h.storage.read_account_state().status, AccountStatus.BANKRUPT.value)
        fresh = self.h.reopen()
        after = fresh.monitor.monitor_once(quote=fresh.quote())
        self.assertEqual(after.mark.status, AccountStatus.BANKRUPT.value)
        self.assertTrue(after.blocked_new_positions)

    def test_min_equity_halt(self) -> None:
        self.h.monitor.monitor_once(quote=self.h.quote())
        self.h.open(PositionSide.LONG)
        self.h.storage.record_cash_flow(flow_type=FlowType.WITHDRAWAL, amount=Decimal("-46000"),
                                        event_key="wd-min")
        result = self.h.monitor.monitor_once(quote=self.h.quote())
        self.assertIn(RiskEventKind.MIN_EQUITY_HALT.value, [e.kind for e in result.events])

    def test_risk_watch_reflects_config_thresholds(self) -> None:
        data = build_report_data(cfg=self.h.cfg, engine=self.h.engine, storage=self.h.storage,
                                 quote=self.h.quote())
        by_code = {item.code: item for item in data.risk_watch}
        self.assertEqual(by_code["MARGIN_LEVEL_MARGIN_CALL"].threshold,
                         self.h.cfg.margin.margin_call_level)
        self.assertEqual(by_code["MARGIN_LEVEL_STOP_OUT"].threshold,
                         self.h.cfg.margin.stop_out_level)
        self.assertEqual(by_code["DAILY_DRAWDOWN"].threshold,
                         self.h.cfg.risk.daily_drawdown_stop_ratio)


# --------------------------------------------------------------------------- #
# 7. 合法与非法资金操作
# --------------------------------------------------------------------------- #


class TestScenario07Funding(IntegrationTestCase):
    def test_valid_deposit_and_withdraw(self) -> None:
        code, _, _ = self.h.cli("deposit", "--amount", "10000", "--confirm", "确认入金 10000")
        self.assertEqual(code, 0)
        self.assertEqual(self.h.storage.ledger_balance(), Decimal("60000"))
        code, _, _ = self.h.cli("withdraw", "--amount", "5000", "--confirm", "确认出金 5000")
        self.assertEqual(code, 0)
        self.assertEqual(self.h.storage.ledger_balance(), Decimal("55000"))

    def test_illegal_funding_attempts_change_nothing(self) -> None:
        before_balance = self.h.storage.ledger_balance()
        before_head = self.h.storage.audit_head()
        before_counts = self.h.storage.counts()
        attempts = [
            ("deposit", "--amount", "10000", "--confirm", "确认入金 9999"),
            ("deposit", "--amount", "500", "--confirm", "确认入金 500"),
            ("deposit", "--amount", "1500", "--confirm", "确认入金 1500"),
            ("deposit", "--amount", "101000", "--confirm", "确认入金 101000"),
            ("withdraw", "--amount", "5000", "--confirm", "确认入金 5000"),
            ("withdraw", "--amount", "99000", "--confirm", "确认出金 99000"),
            ("deposit", "--amount", "abc", "--confirm", "确认入金 abc"),
            ("deposit", "--amount", "-1000", "--confirm", "确认入金 -1000"),
        ]
        for argv in attempts:
            code, _, _ = self.h.cli(*argv)
            self.assertEqual(code, 1, msg=str(argv))
        self.assertEqual(self.h.storage.ledger_balance(), before_balance)
        self.assertEqual(self.h.storage.audit_head(), before_head)
        self.assertEqual(self.h.storage.counts(), before_counts)

    def test_funding_blocked_while_position_open(self) -> None:
        self.h.open(PositionSide.LONG)
        before = self.h.storage.ledger_balance()
        code, _, err = self.h.cli("deposit", "--amount", "10000", "--confirm", "确认入金 10000")
        self.assertEqual(code, 1)
        self.assertIn("未平仓持仓", err)
        self.assertEqual(self.h.storage.ledger_balance(), before)

    def test_funding_blocked_when_bankrupt(self) -> None:
        import dataclasses

        state = self.h.storage.read_account_state()
        self.h.storage.save_account_state(
            dataclasses.replace(state, status=AccountStatus.BANKRUPT.value, halted=True)
        )
        before = self.h.storage.ledger_balance()
        code, _, err = self.h.cli("deposit", "--amount", "10000", "--confirm", "确认入金 10000")
        self.assertEqual(code, 1)
        self.assertIn("账户状态", err)
        self.assertEqual(self.h.storage.ledger_balance(), before)

    def test_deposit_is_not_counted_as_profit(self) -> None:
        self.h.cli("deposit", "--amount", "10000", "--confirm", "确认入金 10000")
        data = build_report_data(cfg=self.h.cfg, engine=self.h.engine, storage=self.h.storage,
                                 quote=self.h.quote())
        self.assertEqual(data.account.equity, Decimal("60000.000"))
        self.assertEqual(data.account.total_deposits, Decimal("10000.000"))
        self.assertEqual(data.account.net_profit, Decimal("0.000"))
        self.assertEqual(data.account.return_pct, Decimal("0.00"))

    def test_duplicate_request_id_is_idempotent(self) -> None:
        argv = ("deposit", "--amount", "10000", "--confirm", "确认入金 10000",
                "--request-id", "req-1")
        self.assertEqual(self.h.cli(*argv)[0], 0)
        code, out, _ = self.h.cli(*argv)
        self.assertEqual(code, 0)
        self.assertIn("重复请求", out)
        self.assertEqual(self.h.storage.ledger_balance(), Decimal("60000"))


# --------------------------------------------------------------------------- #
# 8. 重复指令不重复成交
# --------------------------------------------------------------------------- #


class TestScenario08Idempotency(IntegrationTestCase):
    def test_same_intent_id_opens_once(self) -> None:
        first = self.h.open(PositionSide.LONG, intent_id="dup-1")
        second = self.h.open(PositionSide.LONG, intent_id="dup-1")
        self.assertTrue(second.is_duplicate)
        self.assertEqual(first.position.position_id, second.position.position_id)
        self.assertEqual(self.h.storage.counts()["positions"], 1)
        self.assertEqual(self.h.storage.counts()["trades"], 1)

    def test_duplicate_close_records_one_trade(self) -> None:
        execution = self.h.open(PositionSide.LONG)
        quote = self.h.quote("158.500", "158.510")
        self.h.engine.close_position(execution.position.position_id, quote, intent_id="c1")
        self.h.engine.close_position(execution.position.position_id, quote, intent_id="c1")
        self.assertEqual(len(self.h.storage.query_trades(action="CLOSE")), 1)
        self.assertEqual(self.h.storage.ledger_balance(), Decimal("50490"))

    def test_duplicate_monitor_event_not_executed_twice(self) -> None:
        self.h.open(PositionSide.LONG, stop="157.950", target="158.100")
        self.h.monitor.monitor_once(quote=self.h.quote())
        first = self.h.monitor.monitor_once(quote=self.h.quote("157.900", "157.910"))
        second = self.h.monitor.monitor_once(quote=self.h.quote("157.800", "157.810"))
        self.assertEqual(len(first.closed), 1)
        self.assertEqual(len(second.closed), 0)
        self.assertEqual(len(self.h.storage.query_trades(action="CLOSE")), 1)

    def test_cli_duplicate_intent_id_reports_duplicate(self) -> None:
        import contextlib
        import io

        argv = ("open-long", "--volume", "1000", "--stop", "157.950",
                "--take-profit", "158.100", "--reason", "重复", "--intent-id", "cli-dup")
        with mock.patch.object(cli, "get_current_quote", return_value=self.h.quote()):
            self.assertEqual(self.h.cli(*argv)[0], 0)
            code, out, _ = self.h.cli(*argv)
        self.assertEqual(code, 0)
        self.assertIn("重复指令", out)
        self.assertEqual(self.h.storage.counts()["positions"], 1)


# --------------------------------------------------------------------------- #
# 9. 重启恢复
# --------------------------------------------------------------------------- #


class TestScenario09Restart(IntegrationTestCase):
    def test_state_survives_restart(self) -> None:
        execution = self.h.open(PositionSide.LONG)
        self.h.engine.record_standalone_fee(Decimal("100"), fee_id="f1")
        head_before = self.h.storage.audit_head()
        balance_before = self.h.storage.ledger_balance()

        fresh = self.h.reopen()
        self.assertEqual(fresh.storage.ledger_balance(), balance_before)
        self.assertEqual(fresh.storage.audit_head(), head_before)
        self.assertTrue(fresh.storage.verify_audit_chain().ok)
        position = fresh.storage.read_position(execution.position.position_id)
        self.assertEqual(position.status, PositionStatus.OPEN.value)
        self.assertEqual(position.entry_price, Decimal("158.010"))
        self.assertEqual(len(fresh.storage.query_trades()), 1)
        self.assertEqual(fresh.storage.counts()["cash_flows"], 2)  # 初始本金 + 独立手续费
        self.assertTrue(fresh.engine.reconcile_account(strict=False).ok)

    def test_monitor_recovers_last_check_from_storage(self) -> None:
        run = self.h.storage.start_scheduler_run(
            _MONITOR_TASK, started_at=datetime(2026, 10, 10, 16, 0, tzinfo=_JST)
        )
        self.h.storage.finish_scheduler_run(run.run_no, status=RunStatus.SUCCESS)
        fresh = self.h.reopen()
        fresh.open(PositionSide.LONG)
        now = datetime.now(_JST)
        start = now - timedelta(minutes=10)
        fresh.scripted_bars = [fresh.bar(start + timedelta(minutes=i)) for i in range(11)]
        result = fresh.monitor.monitor_once(quote=fresh.quote(), now=now)
        # 上一次检查是 16:00，远早于 now → 触发补查并回放到止损
        self.assertTrue(result.backfilled)
        self.assertTrue(result.did_close)

    def test_leftover_running_runs_aborted_on_restart(self) -> None:
        leftover = self.h.storage.start_scheduler_run(
            _MONITOR_TASK, started_at=datetime(2026, 10, 10, 16, 0, tzinfo=_JST)
        )
        fresh = self.h.reopen()
        self.assertEqual(fresh.scheduler.recover(), 1)
        runs = fresh.storage.read_scheduler_runs()
        self.assertEqual(
            next(r for r in runs if r.run_no == leftover.run_no).status,
            RunStatus.ABORTED.value,
        )

    def test_report_tasks_survive_restart_without_duplication(self) -> None:
        now = datetime(2026, 10, 10, 17, 0, tzinfo=_JST)
        self.h.scheduler.tick(now=now)
        self.assertEqual(len(self.h.scheduler.list_report_tasks()), 2)
        fresh = self.h.reopen()
        self.assertEqual(len(fresh.scheduler.list_report_tasks()), 2)
        result = fresh.scheduler.tick(now=now)
        self.assertEqual(len(result.report_tasks_created), 0)
        self.assertEqual(len(result.report_tasks_skipped), 2)


# --------------------------------------------------------------------------- #
# 10. 数据源失效 / 历史不完整：不伪造结果
# --------------------------------------------------------------------------- #


class TestScenario10NoFabrication(IntegrationTestCase):
    def test_quote_failure_does_not_fabricate_a_close(self) -> None:
        position = self.h.open(PositionSide.LONG, stop="157.950", target="158.100").position
        self.h.scripted_quotes = [MarketDataError("数据源全部故障")]
        result = self.h.monitor.monitor_once()
        self.assertIsNone(result.quote)
        self.assertFalse(result.did_close)
        self.assertTrue(result.degraded)
        self.assertTrue(result.blocked_new_positions)
        self.assertEqual(
            self.h.storage.read_position(position.position_id).status, PositionStatus.OPEN.value
        )

    def test_unusable_quote_does_not_fabricate_a_close(self) -> None:
        self.h.open(PositionSide.LONG)
        result = self.h.monitor.monitor_once(quote=self.h.quote("159.000", "158.000"))  # bid > ask
        self.assertIsNone(result.quote)
        self.assertFalse(result.did_close)

    def test_unavailable_history_records_gap_without_fill(self) -> None:
        position = self.h.open(PositionSide.LONG, stop="157.950", target="158.100").position
        self.h.bars_error = HistoryUnavailableError("1 分钟数据只回溯约 9 天")
        self.h.monitor._last_check_at = datetime.now(_JST) - timedelta(days=30)
        result = self.h.monitor.monitor_once(quote=self.h.quote())
        self.assertTrue(result.backfilled)
        self.assertFalse(result.did_close)
        self.assertTrue(result.data_gaps)
        self.assertIn(RiskEventKind.DATA_GAP.value, [e.kind for e in result.events])
        self.assertEqual(
            self.h.storage.read_position(position.position_id).status, PositionStatus.OPEN.value
        )

    def test_incomplete_bars_are_reported_as_gaps(self) -> None:
        self.h.open(PositionSide.LONG, stop="157.950", target="158.100")
        now = datetime.now(_JST)
        start = now - timedelta(minutes=10)
        # 只提供前 5 分钟 → 后 5 分钟是数据缺口
        self.h.scripted_bars = [self.h.bar(start + timedelta(minutes=i)) for i in range(5)]
        self.h.monitor._last_check_at = start
        result = self.h.monitor.monitor_once(quote=self.h.quote(), now=now)
        self.assertTrue(result.data_gaps)
        self.assertGreater(result.data_gaps[0].missing_bars or 0, 0)
        self.assertIn("无法核实", result.data_gaps[0].reason)

    def test_stale_quote_blocks_new_opens(self) -> None:
        result = self.h.engine.validate_trade_intent(
            self.h.intent(), self.h.quote(stale=True)
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("过期" in p for p in result.problems))

    def test_report_marks_unverifiable_as_null_not_zero(self) -> None:
        data = build_report_data(
            cfg=self.h.cfg, engine=self.h.engine, storage=self.h.storage, skip_quote=True
        )
        payload = data.to_payload()
        self.assertIsNone(payload["account"]["equity"])
        self.assertIsNone(payload["account"]["net_profit"])
        self.assertIsNone(payload["market"]["bid"])
        self.assertEqual(payload["account"]["cash_balance"], "50000.000")
        self.assertTrue(payload["data_quality"]["unverifiable_fields"])


# --------------------------------------------------------------------------- #
# 11. 账本核对、事务、快照、审计日志
# --------------------------------------------------------------------------- #


class TestScenario11Integrity(IntegrationTestCase):
    def test_full_round_trip_stays_reconciled(self) -> None:
        long_exec = self.h.open(PositionSide.LONG)
        self.h.engine.close_position(long_exec.position.position_id, self.h.quote("158.500", "158.510"))
        short_exec = self.h.open(PositionSide.SHORT, intent_id="s2")
        self.h.engine.close_position(short_exec.position.position_id, self.h.quote("157.500", "157.510"))
        self.assertEqual(self.h.storage.ledger_balance(), Decimal("50980"))
        report = self.h.engine.reconcile_account(strict=False)
        self.assertTrue(report.ok, msg=str(report.problems))
        self.assertEqual(report.cash_from_ledger_rule, report.ledger_balance)
        integrity = self.h.storage.verify_integrity()
        self.assertTrue(integrity.ok, msg=str(integrity.problems))

    def test_transaction_rolls_back_completely(self) -> None:
        position = self.h.open(PositionSide.LONG).position
        trades_before = self.h.storage.counts()["trades"]
        flows_before = self.h.storage.counts()["cash_flows"]
        revision_before = self.h.storage.read_position(position.position_id).revision
        with mock.patch.object(
            Storage, "_insert_audit", side_effect=StorageError("模拟审计写入失败")
        ):
            with self.assertRaises(StorageError):
                self.h.engine.close_position(position.position_id, self.h.quote("158.500", "158.510"))
        reloaded = self.h.storage.read_position(position.position_id)
        self.assertEqual(reloaded.status, PositionStatus.OPEN.value)
        self.assertEqual(reloaded.revision, revision_before)
        self.assertEqual(self.h.storage.counts()["trades"], trades_before)
        self.assertEqual(self.h.storage.counts()["cash_flows"], flows_before)
        self.assertEqual(self.h.storage.ledger_balance(), _INITIAL)

    def test_snapshot_export_verify_round_trip(self) -> None:
        self.h.engine.record_standalone_fee(Decimal("123.456"), fee_id="f-snap")
        data = build_report_data(cfg=self.h.cfg, engine=self.h.engine, storage=self.h.storage,
                                 quote=self.h.quote())
        path = export_snapshot(data, out_dir=self.h.reports, cfg=self.h.cfg)
        self.assertTrue(path.is_file())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(Decimal(payload["account"]["cash_balance"]), self.h.storage.ledger_balance())
        self.assertEqual(Decimal(payload["account"]["initial_capital"]), _INITIAL)
        result = verify_snapshot(path, cfg=self.h.cfg, storage=self.h.storage)
        self.assertTrue(result.ok, msg=str(result.problems))
        self.assertTrue(result.format_ok and result.self_hash_ok and result.formulas_ok and result.ledger_ok)

    def test_snapshot_detects_ledger_mismatch(self) -> None:
        data = build_report_data(cfg=self.h.cfg, engine=self.h.engine, storage=self.h.storage,
                                 quote=self.h.quote())
        payload = data.to_payload()
        payload["account"]["cash_balance"] = "999999"
        from reporting import _payload_hash

        payload["snapshot_sha256"] = _payload_hash(payload)
        result = verify_snapshot(payload, cfg=self.h.cfg, storage=self.h.storage)
        self.assertFalse(result.ok)
        self.assertTrue(result.self_hash_ok)   # 自哈希自洽
        self.assertFalse(result.ledger_ok)     # 但账本对不上

    def test_audit_chain_detects_tampering(self) -> None:
        self.h.engine.record_standalone_fee(Decimal("50"), fee_id="f-audit")
        self.assertTrue(self.h.storage.verify_audit_chain().ok)
        conn = sqlite3.connect(self.h.db)
        conn.execute("UPDATE audit_events SET payload='{\"tampered\":1}' WHERE seq=2")
        conn.commit()
        conn.close()
        verification = self.h.storage.verify_audit_chain()
        self.assertFalse(verification.ok)
        self.assertEqual(verification.first_bad_seq, 2)

    def test_report_template_marks_unverifiable(self) -> None:
        data = build_report_data(
            cfg=self.h.cfg, engine=self.h.engine, storage=self.h.storage, skip_quote=True
        )
        text = generate_report_template(data)
        self.assertIn("无法核实", text)
        self.assertIn("久留美的市场分析", text)

    def test_backup_and_restore(self) -> None:
        self.h.engine.record_standalone_fee(Decimal("200"), fee_id="f-backup")
        target = self.h.storage.backup(self.root / "backups" / "snap.sqlite3")
        restored = Storage(target, tz=_JST)
        self.assertEqual(restored.ledger_balance(), self.h.storage.ledger_balance())
        self.assertTrue(restored.verify_integrity().ok)
        self.assertTrue(restored.verify_audit_chain().ok)


# --------------------------------------------------------------------------- #
# 12. 08:00 / 20:00 JST 定时任务不重复执行
# --------------------------------------------------------------------------- #


class TestScenario12ScheduledTasks(IntegrationTestCase):
    def test_report_slots_are_eight_and_twenty_jst(self) -> None:
        cases = {
            (3, 0): (("EVENING", 2026, 10, 10, 20), ("MORNING", 2026, 10, 10, 8)),
            (8, 0): (("MORNING", 2026, 10, 11, 8), ("EVENING", 2026, 10, 10, 20)),
            (13, 30): (("MORNING", 2026, 10, 11, 8), ("EVENING", 2026, 10, 10, 20)),
            (20, 0): (("EVENING", 2026, 10, 11, 20), ("MORNING", 2026, 10, 11, 8)),
            (23, 59): (("EVENING", 2026, 10, 11, 20), ("MORNING", 2026, 10, 11, 8)),
        }
        for (hour, minute), expected in cases.items():
            slots = self.h.scheduler.due_report_slots(
                datetime(2026, 10, 11, hour, minute, tzinfo=_JST)
            )
            actual = tuple(
                (k.value, s.year, s.month, s.day, s.hour) for k, s in slots
            )
            self.assertEqual(actual, expected, msg=f"{hour}:{minute:02d}")

    def test_slots_are_twelve_hours_apart_and_jst(self) -> None:
        slots = self.h.scheduler.due_report_slots(datetime(2026, 10, 11, 17, 0, tzinfo=_JST))
        self.assertEqual(slots[0][1] - slots[1][1], timedelta(hours=12))
        for _, slot in slots:
            # cfg.tz 是 ZoneInfo("Asia/Tokyo")，与固定 +09:00 不是同一对象，比偏移
            self.assertEqual(slot.utcoffset(), timedelta(hours=9))
            self.assertIn(slot.hour, (8, 20))
            self.assertEqual(slot.minute, 0)

    def test_repeated_wakeups_do_not_duplicate_report_tasks(self) -> None:
        base = datetime(2026, 10, 10, 17, 0, tzinfo=_JST)
        created = 0
        for i in range(12):
            result = self.h.scheduler.tick(now=base + timedelta(seconds=i * 5))
            created += len(result.report_tasks_created)
        self.assertEqual(created, 2)
        self.assertEqual(len(self.h.scheduler.list_report_tasks()), 2)
        self.assertEqual(len(list((self.h.reports / "tasks").glob("*.json"))), 2)
        self.assertEqual(len(list(self.h.reports.glob("fxsim-*.json"))), 2)
        report_runs = [
            r for r in self.h.storage.read_scheduler_runs()
            if r.task_name.startswith(REPORT_TASK_PREFIX)
        ]
        self.assertEqual(len(report_runs), 2)

    def test_scheduler_does_not_trade(self) -> None:
        """调度器不做交易决策。

        tick 会顺带结算**督促机制**（扣本金），所以余额会变；断言的是「没有持仓与
        成交」，且余额变化**恰好等于**督促扣款——别的资金变动都说明 tick 越界了。
        """
        for hour in (9, 13, 17, 20):
            self.h.scheduler.tick(now=self.h.at(hour))
        self.assertEqual(self.h.storage.counts()["positions"], 0)
        self.assertEqual(self.h.storage.counts()["trades"], 0)
        charged = self.h.scheduler.engine.capital_pressure_total()
        self.assertEqual(self.h.storage.ledger_balance(), _INITIAL - charged)

    def test_monitor_task_respects_interval(self) -> None:
        base = datetime(2026, 10, 10, 17, 0, tzinfo=_JST)
        first = self.h.scheduler.tick(now=base)
        self.assertTrue(first.monitor_ran)
        second = self.h.scheduler.tick(now=base + timedelta(seconds=30))
        self.assertFalse(second.monitor_ran)
        third = self.h.scheduler.tick(now=base + timedelta(seconds=61))
        self.assertTrue(third.monitor_ran)
        monitor_runs = [
            r for r in self.h.storage.read_scheduler_runs(task_name=_MONITOR_TASK)
        ]
        self.assertEqual(len(monitor_runs), 2)

    def test_task_records_have_start_end_result(self) -> None:
        self.h.scheduler.tick(now=self.h.at(17))
        for run in self.h.storage.read_scheduler_runs():
            self.assertIsNotNone(run.started_at)
            self.assertIsNotNone(run.finished_at)
            self.assertIn(run.status, {RunStatus.SUCCESS.value, RunStatus.FAILED.value})

    def test_major_event_snapshot_saved_once(self) -> None:
        self.h.open(PositionSide.LONG, stop="157.950", target="158.100")
        self.h.monitor.monitor_once(quote=self.h.quote())
        self.h.scheduler.tick(now=self.h.at(17), force_monitor=True)
        self.h.scheduler.tick(now=self.h.at(17, 1), force_monitor=True)
        events_dir = self.h.reports / "events"
        # 未触发重大事件时不应有快照
        self.assertEqual(len(list(events_dir.glob("*.json"))) if events_dir.is_dir() else 0, 0)


# --------------------------------------------------------------------------- #
# 验收闸门：关键项全绿才允许正式账户开仓
# --------------------------------------------------------------------------- #


class TestAcceptanceGateMechanism(IntegrationTestCase):
    """闸门本身必须**真实生效**，而不是写个 halted 就被下一次盯市清掉。"""

    def test_blocked_gate_rejects_open_immediately(self) -> None:
        from engine import ACCEPTANCE_GATE_META_KEY, GATE_BLOCKED_PREFIX

        self.h.storage.set_meta(
            ACCEPTANCE_GATE_META_KEY, f"{GATE_BLOCKED_PREFIX}关键测试未通过"
        )
        # 关键：此时还没盯市过，account_state.halted 仍是旧值 —— 也必须被拒绝
        with self.assertRaises(TradeRejectedError) as err:
            self.h.engine.execute_virtual_trade(self.h.intent(intent_id="g1"), self.h.quote())
        self.assertTrue(any("验收闸门" in p for p in err.exception.validation.problems))
        self.assertEqual(self.h.storage.counts()["positions"], 0)

    def test_blocked_gate_survives_mark_to_market(self) -> None:
        from engine import ACCEPTANCE_GATE_META_KEY, GATE_BLOCKED_PREFIX

        self.h.storage.set_meta(ACCEPTANCE_GATE_META_KEY, f"{GATE_BLOCKED_PREFIX}测试")
        mark = self.h.engine.mark_to_market(self.h.quote())
        self.assertTrue(mark.halted)
        self.assertIn("验收闸门", mark.halt_reason or "")
        state = self.h.storage.read_account_state()
        self.assertTrue(state.halted)
        self.assertEqual(state.status, AccountStatus.HALTED.value)

    def test_passing_gate_allows_open(self) -> None:
        from engine import ACCEPTANCE_GATE_META_KEY

        self.h.storage.set_meta(ACCEPTANCE_GATE_META_KEY, "PASSED")
        execution = self.h.engine.execute_virtual_trade(
            self.h.intent(intent_id="g2"), self.h.quote()
        )
        self.assertFalse(execution.is_duplicate)
        self.assertEqual(self.h.storage.counts()["positions"], 1)

    def test_gate_does_not_block_close_or_query(self) -> None:
        """闸门只禁止开仓；平仓、监控、查询、报告不受影响。"""
        from engine import ACCEPTANCE_GATE_META_KEY, GATE_BLOCKED_PREFIX

        execution = self.h.engine.execute_virtual_trade(
            self.h.intent(intent_id="g3"), self.h.quote()
        )
        self.h.storage.set_meta(ACCEPTANCE_GATE_META_KEY, f"{GATE_BLOCKED_PREFIX}测试")
        closed = self.h.engine.close_position(
            execution.position.position_id, self.h.quote("158.500", "158.510")
        )
        self.assertEqual(closed.realized_pnl_jpy, Decimal("490.000"))
        result = self.h.monitor.monitor_once(quote=self.h.quote())
        self.assertIsNotNone(result.mark)
        data = build_report_data(cfg=self.h.cfg, engine=self.h.engine,
                                 storage=self.h.storage, quote=self.h.quote())
        self.assertEqual(data.account.equity, Decimal("50490.000"))

    def test_gate_cli_runs_critical_tests(self) -> None:
        code, out, _ = self.h.cli("gate")
        self.assertEqual(code, 0, msg=out)
        self.assertIn("验收闸门", out)
        self.assertIn("允许开仓", out)
        from engine import ACCEPTANCE_GATE_META_KEY

        self.assertEqual(self.h.storage.get_meta(ACCEPTANCE_GATE_META_KEY), "PASSED")

    def test_gate_cli_blocks_when_critical_test_fails(self) -> None:
        from engine import ACCEPTANCE_GATE_META_KEY, GATE_BLOCKED_PREFIX

        with mock.patch.object(
            cli, "_run_critical_tests", return_value=(False, ["假失败"], [])
        ):
            code, out, _ = self.h.cli("gate")
        self.assertEqual(code, 1)
        self.assertIn("已禁止开仓", out)
        self.assertTrue(
            (self.h.storage.get_meta(ACCEPTANCE_GATE_META_KEY) or "").startswith(
                GATE_BLOCKED_PREFIX
            )
        )
        with self.assertRaises(TradeRejectedError):
            self.h.engine.execute_virtual_trade(self.h.intent(intent_id="g4"), self.h.quote())





#: 关键测试清单的单一来源在 main.py（gate 命令也用它）。
CRITICAL_TESTS = cli.CRITICAL_TESTS




class TestAcceptanceGate(unittest.TestCase):
    """确认「关键测试清单」引用的用例都真实存在（防止清单写错而误判通过）。"""

    def test_critical_test_manifest_resolves(self) -> None:
        import importlib

        missing: list[str] = []
        for dotted in CRITICAL_TESTS:
            module_and_class, _, method_name = dotted.rpartition(".")
            module_name, _, class_name = module_and_class.rpartition(".")
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                missing.append(f"{dotted}（模块不存在）")
                continue
            cls = getattr(module, class_name, None)
            if cls is None or not hasattr(cls, method_name):
                missing.append(dotted)
        self.assertEqual(missing, [], msg=f"清单里的用例不存在: {missing}")

    def test_critical_tests_all_pass(self) -> None:
        """运行关键清单；任一项失败则整体失败（用于验收闸门）。"""
        import unittest as ut

        loader = ut.TestLoader()
        suite = ut.TestSuite()
        for dotted in CRITICAL_TESTS:
            module_and_class, _, method_name = dotted.rpartition(".")
            module_name, _, class_name = module_and_class.rpartition(".")
            module = __import__(module_name, fromlist=[class_name])
            suite.addTest(getattr(module, class_name)(method_name))
        result = ut.TextTestRunner(verbosity=0, stream=open(__import__("os").devnull, "w")).run(suite)
        failures = [str(t) for t, _ in result.failures + result.errors]
        self.assertEqual(failures, [], msg=f"关键测试未通过: {failures}")
        self.assertEqual(result.testsRun, len(CRITICAL_TESTS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
