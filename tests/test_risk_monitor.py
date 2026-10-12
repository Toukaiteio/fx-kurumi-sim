"""risk_monitor 模块的自动化测试。

覆盖：止损/止盈判定与成交模型（含跳空、模糊 K 线、多空用价）、保证金预警与强平、
当日回撤停止、净值过低与爆仓、过期行情降级、断线补查与数据缺口、事件去重
（含跨重启）、持续循环的退出与异常恢复。

所有测试都注入模拟行情，**不访问网络、不连接券商、不做真实交易**。

运行方式（项目根目录 E:\\pj2\\fx-kurumi-sim）::

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import load_config  # noqa: E402
from engine import (  # noqa: E402
    CloseReason,
    TradingEngine,
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
from risk_monitor import (  # noqa: E402
    TASK_NAME,
    DataGap,
    RiskEventKind,
    RiskEventLedger,
    RiskMonitor,
    _fake_quote,
)
from storage import (  # noqa: E402
    AccountStatus,
    FlowType,
    PositionSide,
    PositionStatus,
    RunStatus,
    Storage,
)

_JST = timezone(timedelta(hours=9))


def make_quote(bid: str, ask: str, *, stale: bool = False, symbol: str = "USD/JPY") -> Quote:
    bid_d, ask_d = Decimal(bid), Decimal(ask)
    return Quote(
        symbol=symbol,
        bid=bid_d,
        ask=ask_d,
        mid=(bid_d + ask_d) / 2,
        timestamp=datetime.now(_JST),
        source="test_source",
        source_url="https://example.invalid/test",
        quote_type=QuoteType.BID_ASK,
        granularity=Granularity.TICK,
        is_stale=stale,
        age_seconds=Decimal("999") if stale else Decimal("0"),
    )


def make_series(
    start: datetime, end: datetime, bars: list[Bar], *, symbol: str = "USD/JPY"
) -> BarSeries:
    return BarSeries(
        symbol=symbol, interval="1m", bars=tuple(bars), source="test_bars",
        source_url="https://example.invalid/bars", requested_start=start, requested_end=end,
    )


def bar_at(
    moment: datetime,
    *,
    open_: str = "158.000",
    high: str = "158.020",
    low: str = "157.400",
    close: str = "157.500",
) -> Bar:
    return Bar(
        timestamp=moment, open=Decimal(open_), high=Decimal(high), low=Decimal(low),
        close=Decimal(close), source="test_bars", interval="1m",
    )



def pinned_spec_config():
    """把风险规则固定成初始规格里的数值（单笔 2% / 保证金 60% / 日回撤 3% /
    盈亏比 1:1.5 / 点差 0.01 / 滑点 0.005）。

    这些用例验证「规则怎么算」，不是「出货配置是多少」；显式钉住参数后，
    调整 config.json 的风险偏好不会让机制测试集体失败。
    """
    import dataclasses

    cfg = load_config()
    return dataclasses.replace(
        cfg,
        risk=dataclasses.replace(
            cfg.risk,
            max_margin_usage_ratio=Decimal("0.60"),
            max_risk_per_trade_ratio=Decimal("0.02"),
            max_total_risk_ratio=Decimal("0.05"),
            daily_drawdown_stop_ratio=Decimal("0.03"),
            min_reward_risk_ratio=Decimal("1.5"),
        ),
        margin=dataclasses.replace(cfg.margin, min_equity_to_open_jpy=Decimal("5000")),
        market=dataclasses.replace(
            cfg.market, spread_jpy=Decimal("0.01"), slippage_jpy=Decimal("0.005")
        ),
    )

class MonitorTestCase(unittest.TestCase):
    """公共夹具：临时数据库 + 已初始化账户 + 可注入行情源的监控器。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "data" / "fxsim.sqlite3"
        self.cfg = pinned_spec_config()
        self.storage = Storage(self.db_path, tz=_JST)
        self.engine = TradingEngine(self.cfg, self.storage)
        self.engine.ensure_initialized()
        self._quote: Quote | None = make_quote("158.000", "158.010")
        self._bars: list[Bar] = []
        self._bars_error: BaseException | None = None
        self.monitor = self._make_monitor()

    # -- 工具 -------------------------------------------------------------- #

    def _make_monitor(self, **kwargs) -> RiskMonitor:
        monitor = RiskMonitor(
            self.cfg, self.engine,
            quote_source=lambda: self._quote,
            bars_source=self._bars_source,
            logger_=logging.getLogger("test.risk_monitor"),
            **kwargs,
        )
        return monitor

    def _bars_source(self, start: datetime, end: datetime) -> BarSeries:
        if self._bars_error is not None:
            raise self._bars_error
        return make_series(start, end, self._bars)

    def open_position(
        self,
        side=PositionSide.LONG,
        *,
        stop: str | None = None,
        target: str | None = None,
        volume: str = "1000",
        intent_id: str | None = None,
    ):
        from engine import TradeIntent

        if stop is None:
            stop = "157.950" if side is PositionSide.LONG else "158.060"
        if target is None:
            target = "158.100" if side is PositionSide.LONG else "157.910"
        intent = TradeIntent(
            symbol=self.cfg.instrument.symbol, side=side, volume_usd=Decimal(volume),
            stop_loss=Decimal(stop), take_profit=Decimal(target),
            intent_id=intent_id or f"t-{side.value}-{stop}-{target}-{volume}-{id(self)}",
        )
        return self.engine.execute_virtual_trade(intent, make_quote("158.000", "158.010"))

    def deposit(self, amount: str, key: str) -> None:
        self.storage.record_cash_flow(
            flow_type=FlowType.DEPOSIT, amount=Decimal(amount), event_key=key
        )

    def adjust_margin_level(self, target: Decimal) -> Decimal:
        """通过出入金把保证金水平调到目标值（保证金占用不变）。"""
        margin = calculate_used_margin(self.engine.open_positions())
        desired = (margin * target).quantize(Decimal("0.001"))
        delta = self.engine.account_state().equity - desired
        if delta > 0:
            self.storage.record_cash_flow(
                flow_type=FlowType.WITHDRAWAL, amount=-delta, event_key=f"adj-w-{id(self)}-{target}"
            )
        elif delta < 0:
            self.storage.record_cash_flow(
                flow_type=FlowType.DEPOSIT, amount=-delta, event_key=f"adj-d-{id(self)}-{target}"
            )
        return self.engine.account_state().equity

    def event_kinds(self, result) -> list[str]:
        return [event.kind for event in result.events]


# --------------------------------------------------------------------------- #
# 1. 交易日分界（08:00 JST）
# --------------------------------------------------------------------------- #


class TestTradingDayBoundary(unittest.TestCase):
    def test_before_boundary_belongs_to_previous_day(self) -> None:
        self.assertEqual(
            trading_date_for(datetime(2026, 10, 11, 7, 30, tzinfo=_JST), _JST),
            date(2026, 10, 10),
        )
        self.assertEqual(
            trading_date_for(datetime(2026, 10, 11, 0, 0, tzinfo=_JST), _JST),
            date(2026, 10, 10),
        )

    def test_at_and_after_boundary(self) -> None:
        self.assertEqual(
            trading_date_for(datetime(2026, 10, 11, 8, 0, tzinfo=_JST), _JST),
            date(2026, 10, 11),
        )
        self.assertEqual(
            trading_date_for(datetime(2026, 10, 11, 23, 59, tzinfo=_JST), _JST),
            date(2026, 10, 11),
        )

    def test_naive_datetime_treated_as_local(self) -> None:
        self.assertEqual(
            trading_date_for(datetime(2026, 10, 11, 7, 0), _JST), date(2026, 10, 10)
        )

    def test_other_timezone_converted(self) -> None:
        # 2026-10-10 23:30 UTC = 2026-10-11 08:30 JST → 属于 10-11
        self.assertEqual(
            trading_date_for(datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc), _JST),
            date(2026, 10, 11),
        )


# --------------------------------------------------------------------------- #
# 2. 止损/止盈判定与成交模型
# --------------------------------------------------------------------------- #


class TestExitTriggerFillModel(MonitorTestCase):
    def test_long_stop_loss_normal_touch_fills_at_stop(self) -> None:
        execution = self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        # 先建立「此前价位」（未触发）
        first = self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.assertFalse(first.did_close)
        # 再触发：Bid 越过止损
        result = self.monitor.monitor_once(make_quote("157.900", "157.910"))
        self.assertTrue(result.did_close)
        event = result.events[0]
        self.assertEqual(event.kind, RiskEventKind.STOP_LOSS.value)
        self.assertFalse(event.is_gap)
        self.assertEqual(event.fill_price, Decimal("157.950"))  # 按止损价成交
        self.assertEqual(event.realized_pnl_jpy, Decimal("-60.000"))  # (157.950-158.010)*1000

    def test_long_stop_loss_without_prior_price_is_treated_as_gap(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        result = self.monitor.monitor_once(make_quote("157.000", "157.010"))
        event = result.events[0]
        self.assertTrue(event.is_gap)
        # 首个可核验的可执行报价 157.000 加不利滑点 0.005
        self.assertEqual(event.fill_price, Decimal("156.995"))
        self.assertEqual(event.realized_pnl_jpy, Decimal("-1015.000"))
        self.assertIn("跳空", event.reason)

    def test_long_stop_loss_gap_when_previous_already_beyond_stop(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        # 直接用手工注入的「此前价位」模拟监控此前已看到不利价位
        self.monitor._last_executable[1] = Decimal("157.900")
        result = self.monitor.monitor_once(make_quote("157.800", "157.810"))
        event = result.events[0]
        self.assertTrue(event.is_gap)
        self.assertEqual(event.fill_price, Decimal("157.795"))  # 157.800 - 0.005

    def test_short_stop_loss_uses_ask_and_adverse_slippage(self) -> None:
        self.open_position(PositionSide.SHORT, stop="158.060", target="157.910")
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        result = self.monitor.monitor_once(make_quote("158.100", "158.110"))
        event = result.events[0]
        self.assertEqual(event.kind, RiskEventKind.STOP_LOSS.value)
        # 此前 Ask=158.010 < 止损 158.060 → 穿越 → 按止损价成交
        self.assertEqual(event.fill_price, Decimal("158.060"))
        self.assertFalse(event.is_gap)

    def test_short_take_profit_uses_ask(self) -> None:
        self.open_position(PositionSide.SHORT, stop="158.060", target="157.910")
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        result = self.monitor.monitor_once(make_quote("157.900", "157.910"))
        event = result.events[0]
        self.assertEqual(event.kind, RiskEventKind.TAKE_PROFIT.value)
        self.assertEqual(event.fill_price, Decimal("157.910"))  # 按止盈价成交，不计有利滑点
        self.assertEqual(event.realized_pnl_jpy, Decimal("90.000"))  # (158.000-157.910)*1000

    def test_long_take_profit_not_triggered_by_bid_below_target(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        # Bid 158.050 未达 158.100，即使 Ask 更高也不触发
        result = self.monitor.monitor_once(make_quote("158.050", "158.200"))
        self.assertFalse(result.did_close)

    def test_long_take_profit_fills_at_target_conservatively(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        result = self.monitor.monitor_once(make_quote("158.500", "158.510"))
        event = result.events[0]
        self.assertEqual(event.kind, RiskEventKind.TAKE_PROFIT.value)
        # 有利跳空不计入，仍按止盈价成交（保守）
        self.assertEqual(event.fill_price, Decimal("158.100"))
        self.assertEqual(event.realized_pnl_jpy, Decimal("90.000"))

    def test_event_records_all_required_fields(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        result = self.monitor.monitor_once(make_quote("157.900", "157.910"))
        event = result.events[0]
        # 触发时间、检测时间、参考报价、成交价、原因、最终盈亏
        self.assertIsInstance(event.triggered_at, datetime)
        self.assertIsInstance(event.detected_at, datetime)
        self.assertEqual(event.reference_bid, Decimal("157.900"))
        self.assertEqual(event.reference_ask, Decimal("157.910"))
        self.assertEqual(event.reference_source, "test_source")
        self.assertEqual(event.fill_price, Decimal("157.950"))
        self.assertIsNotNone(event.realized_pnl_jpy)
        self.assertTrue(event.reason)
        payload = json.loads(json.dumps(event.as_dict(), ensure_ascii=False))
        self.assertIn("triggered_at", payload)
        self.assertIn("detected_at", payload)
        self.assertIn("fill_price", payload)


# --------------------------------------------------------------------------- #
# 3. 历史 K 线判定（补查用）
# --------------------------------------------------------------------------- #


class TestBarTrigger(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = pinned_spec_config()
        self.storage = Storage(Path(self._tmp.name) / "db.sqlite3", tz=_JST)
        self.engine = TradingEngine(self.cfg, self.storage)
        self.engine.ensure_initialized()
        self.monitor = RiskMonitor(self.cfg, self.engine, quote_source=lambda: make_quote("158", "158.01"))
        self.execution = self.engine.execute_virtual_trade(
            self._intent(PositionSide.LONG, "157.950", "158.100"), make_quote("158.000", "158.010")
        )

    def _intent(self, side, stop, target):
        from engine import TradeIntent

        return TradeIntent(
            symbol=self.cfg.instrument.symbol, side=side, volume_usd=Decimal("1000"),
            stop_loss=Decimal(stop), take_profit=Decimal(target), intent_id=f"b-{stop}-{target}",
        )

    @property
    def position(self):
        return self.engine.storage.read_position(self.execution.position.position_id)

    def test_bar_straddling_stop_fills_at_stop(self) -> None:
        bar = bar_at(datetime(2026, 10, 10, 9, 0, tzinfo=_JST))  # low 157.400 high 158.020
        trigger = self.monitor._bar_trigger(self.position, bar)
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.reason, CloseReason.STOP_LOSS)
        self.assertFalse(trigger.is_gap)
        self.assertEqual(trigger.fill_price, Decimal("157.950"))

    def test_bar_entirely_beyond_stop_is_a_gap(self) -> None:
        bar = bar_at(
            datetime(2026, 10, 10, 9, 0, tzinfo=_JST),
            open_="157.000", high="157.500", low="156.800", close="157.200",
        )
        trigger = self.monitor._bar_trigger(self.position, bar)
        self.assertIsNotNone(trigger)
        self.assertTrue(trigger.is_gap)
        # 可执行开盘价 = 157.000 - 0.005(单边点差) = 156.995，再减不利滑点 0.005
        self.assertEqual(trigger.fill_price, Decimal("156.990"))

    def test_bar_touching_only_take_profit(self) -> None:
        bar = bar_at(
            datetime(2026, 10, 10, 9, 0, tzinfo=_JST),
            open_="158.000", high="158.200", low="157.990", close="158.150",
        )
        trigger = self.monitor._bar_trigger(self.position, bar)
        self.assertEqual(trigger.reason, CloseReason.TAKE_PROFIT)
        self.assertEqual(trigger.fill_price, Decimal("158.100"))

    def test_ambiguous_bar_prefers_stop_loss(self) -> None:
        """同一根 K 线同时触及止损与止盈、先后未知 → 按止损先发生（更不利）。"""
        bar = bar_at(
            datetime(2026, 10, 10, 9, 0, tzinfo=_JST),
            open_="158.000", high="158.200", low="157.400", close="157.900",
        )
        trigger = self.monitor._bar_trigger(self.position, bar)
        self.assertEqual(trigger.reason, CloseReason.STOP_LOSS)
        self.assertEqual(trigger.fill_price, Decimal("157.950"))

    def test_short_bar_uses_ask_side(self) -> None:
        self.engine.close_position(
            self.execution.position.position_id, make_quote("158.000", "158.010")
        )
        short = self.engine.execute_virtual_trade(
            self._intent(PositionSide.SHORT, "158.060", "157.910"), make_quote("158.000", "158.010")
        )
        position = self.engine.storage.read_position(short.position.position_id)
        # 空仓：可执行价 = bar 高价 + 单边点差；止损在 158.060 上方
        bar = bar_at(
            datetime(2026, 10, 10, 9, 0, tzinfo=_JST),
            open_="158.000", high="158.100", low="157.900", close="158.050",
        )
        trigger = self.monitor._bar_trigger(position, bar)
        self.assertEqual(trigger.reason, CloseReason.STOP_LOSS)
        # 可执行区间 [157.905, 158.105] 包含 158.060 → 按止损价
        self.assertEqual(trigger.fill_price, Decimal("158.060"))

    def test_no_trigger_when_bar_inside_range(self) -> None:
        bar = bar_at(
            datetime(2026, 10, 10, 9, 0, tzinfo=_JST),
            open_="158.000", high="158.050", low="157.990", close="158.020",
        )
        self.assertIsNone(self.monitor._bar_trigger(self.position, bar))


# --------------------------------------------------------------------------- #
# 4. 账户级风控
# --------------------------------------------------------------------------- #


class TestAccountLevelLimits(MonitorTestCase):
    def test_margin_warning_records_once_and_blocks(self) -> None:
        # 先确立当日基准（50000），再入金抬高净值以允许大仓位
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.deposit("40000", "dep-1")
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100", volume="8000")
        self.adjust_margin_level(Decimal("1.45"))
        result = self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.assertIn(RiskEventKind.MARGIN_WARNING.value, self.event_kinds(result))
        self.assertTrue(result.blocked_new_positions)
        self.assertLessEqual(result.margin_level, self.cfg.margin.margin_call_level)
        self.assertGreater(result.margin_level, self.cfg.margin.stop_out_level)
        # 同一天不重复记录
        again = self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.assertNotIn(RiskEventKind.MARGIN_WARNING.value, self.event_kinds(again))
        # 未开仓（只预警）
        self.assertFalse(result.did_close)

    def test_margin_call_force_closes_all_positions(self) -> None:
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.deposit("40000", "dep-2")
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100", volume="8000")
        self.adjust_margin_level(Decimal("0.95"))
        result = self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.assertTrue(result.did_close)
        self.assertIn(RiskEventKind.MARGIN_CALL.value, self.event_kinds(result))
        closed = result.closed[0]
        self.assertEqual(closed.reason, CloseReason.MARGIN_CALL.value)
        self.assertEqual(closed.position.close_reason, CloseReason.MARGIN_CALL.value)
        self.assertEqual(closed.trade.reason, CloseReason.MARGIN_CALL.value)
        # 强平按可执行报价 + 不利滑点成交
        self.assertEqual(closed.trade.price, Decimal("157.995"))
        self.assertEqual(self.engine.open_positions(), [])

    def test_daily_drawdown_closes_and_halts_for_the_day(self) -> None:
        self.monitor.monitor_once(make_quote("158.000", "158.010"))  # 基准 50000
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        # 出金 1500 把净值压到当日基准的 3% 以下（真实路径，不 mock 盯市）
        self.storage.record_cash_flow(
            flow_type=FlowType.WITHDRAWAL, amount=Decimal("-1500"), event_key="wd-dd"
        )
        result = self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.assertTrue(result.did_close)
        self.assertIn(RiskEventKind.DAILY_DRAWDOWN.value, self.event_kinds(result))
        self.assertEqual(result.closed[0].reason, CloseReason.DAILY_DRAWDOWN.value)
        # 当日基准被标记，暂停当日新开仓
        baseline = self.storage.read_daily_baseline(self.monitor.trading_date())
        self.assertIsNotNone(baseline)
        self.assertTrue(baseline.drawdown_stop_triggered)
        state = self.storage.read_account_state()
        self.assertTrue(state.halted)
        self.assertEqual(state.status, AccountStatus.HALTED.value)
        # 新开仓被引擎拒绝
        from engine import TradeIntent, TradeRejectedError

        with self.assertRaises(TradeRejectedError):
            self.engine.execute_virtual_trade(
                TradeIntent(
                    symbol="USD/JPY", side=PositionSide.LONG, volume_usd=Decimal("1000"),
                    stop_loss=Decimal("157.950"), take_profit=Decimal("158.100"),
                    intent_id="after-halt",
                ),
                make_quote("158.000", "158.010"),
            )

    def test_drawdown_halt_persists_even_if_equity_recovers(self) -> None:
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.monitor.engine.set_daily_drawdown_stop()
        self.deposit("10000", "dep-3")  # 净值回升
        result = self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.assertTrue(result.blocked_new_positions)
        self.assertIn("回撤", result.block_reason or "")
        self.assertTrue(self.storage.read_account_state().halted)

    def test_min_equity_halt_event(self) -> None:
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        self.storage.record_cash_flow(
            flow_type=FlowType.WITHDRAWAL, amount=Decimal("-46000"), event_key="wd-min"
        )
        result = self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.assertIn(RiskEventKind.MIN_EQUITY_HALT.value, self.event_kinds(result))
        self.assertTrue(result.blocked_new_positions)
        state = self.storage.read_account_state()
        self.assertTrue(state.halted)

    def test_bankruptcy_marks_account_permanently(self) -> None:
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        self.storage.record_cash_flow(
            flow_type=FlowType.WITHDRAWAL, amount=Decimal("-51000"), event_key="wd-bk"
        )
        result = self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.assertIn(RiskEventKind.BANKRUPTCY.value, self.event_kinds(result))
        self.assertTrue(result.did_close)
        self.assertEqual(result.closed[0].reason, CloseReason.BANKRUPTCY.value)
        state = self.storage.read_account_state()
        self.assertEqual(state.status, AccountStatus.BANKRUPT.value)
        # 爆仓状态持久化，重启后依然停止
        reopened = RiskMonitor(
            self.cfg, TradingEngine(self.cfg, Storage(self.db_path, tz=_JST)),
            quote_source=lambda: make_quote("158.000", "158.010"),
            bars_source=self._bars_source,
        )
        after = reopened.monitor_once(make_quote("158.000", "158.010"))
        self.assertEqual(after.mark.status, AccountStatus.BANKRUPT.value)
        self.assertTrue(after.blocked_new_positions)

    def test_bankruptcy_recorded_only_once(self) -> None:
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        self.storage.record_cash_flow(
            flow_type=FlowType.WITHDRAWAL, amount=Decimal("-51000"), event_key="wd-bk2"
        )
        first = self.monitor.monitor_once(make_quote("158.000", "158.010"))
        second = self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.assertIn(RiskEventKind.BANKRUPTCY.value, self.event_kinds(first))
        self.assertNotIn(RiskEventKind.BANKRUPTCY.value, self.event_kinds(second))

    def test_monitor_never_opens_positions(self) -> None:
        """监控器只平仓，不产生开仓决策。"""
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.assertEqual(self.storage.counts()["positions"], 0)


# --------------------------------------------------------------------------- #
# 5. 数据质量：过期行情与降级
# --------------------------------------------------------------------------- #


class TestDataQuality(MonitorTestCase):
    def test_stale_quote_blocks_and_degrades(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        result = self.monitor.monitor_once(make_quote("158.000", "158.010", stale=True))
        self.assertTrue(result.quote_is_stale)
        self.assertTrue(result.degraded)
        self.assertTrue(result.blocked_new_positions)
        self.assertIn("过期", result.block_reason or "")
        self.assertIn(RiskEventKind.DEGRADED_MONITORING.value, self.event_kinds(result))
        # 仍按过期报价盯市，但明确标记监控能力受限
        self.assertIsNotNone(result.mark)
        self.assertTrue(any("受限" in reason for reason in result.degradation_reasons))

    def test_quote_fetch_failure_degrades_without_crashing(self) -> None:
        monitor = RiskMonitor(
            self.cfg, self.engine,
            quote_source=lambda: (_ for _ in ()).throw(MarketDataError("数据源全部故障")),
            bars_source=self._bars_source,
        )
        result = monitor.monitor_once()
        self.assertIsNone(result.quote)
        self.assertFalse(result.quote_ok)
        self.assertTrue(result.degraded)
        self.assertTrue(result.blocked_new_positions)
        self.assertIn("数据源全部故障", " ".join(result.degradation_reasons))

    def test_unusable_quote_does_not_fabricate_close(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        result = self.monitor.monitor_once(make_quote("159.000", "158.000"))  # bid > ask
        self.assertIsNone(result.quote)
        self.assertFalse(result.did_close)
        self.assertEqual(
            self.storage.read_positions(status=PositionStatus.OPEN)[0].status, "OPEN"
        )

    def test_degraded_event_recorded_once_per_day(self) -> None:
        first = self.monitor.monitor_once(make_quote("158.000", "158.010", stale=True))
        second = self.monitor.monitor_once(make_quote("158.000", "158.010", stale=True))
        self.assertIn(RiskEventKind.DEGRADED_MONITORING.value, self.event_kinds(first))
        self.assertNotIn(RiskEventKind.DEGRADED_MONITORING.value, self.event_kinds(second))


# --------------------------------------------------------------------------- #
# 6. 断线补查与数据缺口
# --------------------------------------------------------------------------- #


class TestBackfill(MonitorTestCase):
    def test_no_backfill_without_previous_check(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        result = self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.assertFalse(result.backfilled)

    def test_backfill_replays_bars_and_closes_on_stop(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        now = datetime.now(_JST)
        start = now - timedelta(minutes=10)
        self._bars = [bar_at(start + timedelta(minutes=i)) for i in range(11)]
        self.monitor._last_check_at = start
        result = self.monitor.monitor_once(make_quote("158.000", "158.010"), now=now)
        self.assertTrue(result.backfilled)
        self.assertEqual(result.backfilled_bars, 11)
        self.assertTrue(result.did_close)
        stops = [e for e in result.events if e.kind == RiskEventKind.STOP_LOSS.value]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].fill_price, Decimal("157.950"))
        self.assertIn("K 线", stops[0].reason)

    def test_backfill_records_data_gap_for_missing_bars(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        now = datetime.now(_JST)
        start = now - timedelta(minutes=10)
        # 只提供前 5 分钟，后 5 分钟缺失
        self._bars = [bar_at(start + timedelta(minutes=i)) for i in range(5)]
        self.monitor._last_check_at = start
        result = self.monitor.monitor_once(make_quote("158.000", "158.010"), now=now)
        self.assertTrue(result.data_gaps)
        gap = result.data_gaps[0]
        self.assertGreater(gap.missing_bars or 0, 0)
        self.assertIn("无法核实", gap.reason)
        self.assertIn(RiskEventKind.DATA_GAP.value, self.event_kinds(result))

    def test_unavailable_history_records_gap_and_keeps_position(self) -> None:
        position = self.open_position(PositionSide.LONG, stop="157.950", target="158.100").position
        self._bars_error = HistoryUnavailableError("1 分钟数据只回溯约 9 天")
        self.monitor._last_check_at = datetime.now(_JST) - timedelta(days=30)
        result = self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.assertTrue(result.backfilled)
        self.assertEqual(result.backfilled_bars, 0)
        self.assertFalse(result.did_close)  # 绝不虚构成交
        self.assertTrue(result.data_gaps)
        self.assertIn("回溯", result.data_gaps[0].reason)
        self.assertEqual(
            self.storage.read_position(position.position_id).status, PositionStatus.OPEN.value
        )

    def test_backfill_does_not_reclose_a_position(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        now = datetime.now(_JST)
        start = now - timedelta(minutes=5)
        self._bars = [bar_at(start + timedelta(minutes=i)) for i in range(6)]
        self.monitor._last_check_at = start
        result = self.monitor.monitor_once(make_quote("158.000", "158.010"), now=now)
        self.assertEqual(len(result.closed), 1)
        closes = self.storage.query_trades(action="CLOSE")
        self.assertEqual(len(closes), 1)

    def test_bar_trigger_during_backfill_records_trigger_time(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        now = datetime.now(_JST)
        start = now - timedelta(minutes=5)
        self._bars = [bar_at(start + timedelta(minutes=i)) for i in range(6)]
        self.monitor._last_check_at = start
        result = self.monitor.monitor_once(make_quote("158.000", "158.010"), now=now)
        stops = [e for e in result.events if e.kind == RiskEventKind.STOP_LOSS.value]
        self.assertEqual(len(stops), 1)
        # 触发时间来自 K 线，检测时间来自本次 tick，二者不同
        self.assertLess(stops[0].triggered_at, stops[0].detected_at)


# --------------------------------------------------------------------------- #
# 7. 事件去重（含跨重启）
# --------------------------------------------------------------------------- #


class TestEventDeduplication(MonitorTestCase):
    def test_same_stop_event_not_executed_twice(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        first = self.monitor.monitor_once(make_quote("157.900", "157.910"))
        second = self.monitor.monitor_once(make_quote("157.800", "157.810"))
        self.assertEqual(len(first.closed), 1)
        self.assertEqual(len(second.closed), 0)
        self.assertEqual(len(self.storage.query_trades(action="CLOSE")), 1)

    def test_ledger_survives_restart(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        result = self.monitor.monitor_once(make_quote("157.900", "157.910"))
        self.assertTrue(result.events)
        key = result.events[0].event_key
        # 模拟进程重启：新的监控器从审计日志重建已触发集合
        fresh = RiskMonitor(
            self.cfg, TradingEngine(self.cfg, Storage(self.db_path, tz=_JST)),
            quote_source=lambda: make_quote("157.800", "157.810"),
            bars_source=self._bars_source,
        )
        self.assertTrue(fresh.ledger.already_fired(key))

    def test_ledger_reload_counts_events(self) -> None:
        self.monitor.monitor_once(make_quote("158.000", "158.010", stale=True))
        ledger = RiskEventLedger(self.storage)
        self.assertGreaterEqual(len(ledger._fired), 1)
        self.assertGreaterEqual(ledger.reload(), 1)

    def test_already_fired_returns_true(self) -> None:
        ledger = RiskEventLedger(self.storage)
        self.assertFalse(ledger.already_fired("EXIT:STOP_LOSS:1"))

    def test_events_are_appended_to_audit_chain(self) -> None:
        self.monitor.monitor_once(make_quote("158.000", "158.010", stale=True))
        audit = self.storage.read_audit_events(event_type=RiskEventLedger.AUDIT_TYPE)
        self.assertTrue(audit)
        self.assertIn("event_key", audit[-1].payload)
        self.assertTrue(self.storage.verify_audit_chain().ok)


# --------------------------------------------------------------------------- #
# 8. 持续循环
# --------------------------------------------------------------------------- #


class TestRunForever(MonitorTestCase):
    def test_iterations_and_run_records(self) -> None:
        ticks = self.monitor.run_forever(interval_seconds=0.01, iterations=3)
        self.assertEqual(ticks, 3)
        runs = self.storage.read_scheduler_runs(task_name=TASK_NAME)
        self.assertEqual(len(runs), 3)
        self.assertTrue(all(run.status == RunStatus.SUCCESS.value for run in runs))
        self.assertTrue(all(run.finished_at is not None for run in runs))

    def test_stop_event_exits_cleanly(self) -> None:
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.monitor.run_forever(interval_seconds=0.01, stop_event=stop), 0)

    def test_on_tick_callback(self) -> None:
        seen: list[str] = []
        self.monitor.run_forever(
            interval_seconds=0.01, iterations=2, on_tick=lambda r: seen.append(r.trading_date)
        )
        self.assertEqual(len(seen), 2)

    def test_exception_recovery_continues_after_a_failed_tick(self) -> None:
        calls = {"n": 0}
        original = RiskMonitor.monitor_once

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟单次失败")
            return original(self, *args, **kwargs)

        with mock.patch.object(RiskMonitor, "monitor_once", flaky):
            ticks = self.monitor.run_forever(interval_seconds=0.01, iterations=2)
        self.assertEqual(ticks, 2)  # 失败那次不计入，循环继续
        runs = self.storage.read_scheduler_runs(task_name=TASK_NAME, order="ASC")
        statuses = [run.status for run in runs]
        self.assertIn(RunStatus.FAILED.value, statuses)
        self.assertIn(RunStatus.SUCCESS.value, statuses)

    def test_gives_up_after_max_consecutive_errors(self) -> None:
        with mock.patch.object(
            RiskMonitor, "monitor_once", side_effect=RuntimeError("持续失败")
        ):
            with self.assertRaises(RuntimeError):
                self.monitor.run_forever(
                    interval_seconds=0.01, max_consecutive_errors=2
                )

    def test_recover_aborted_runs_marks_leftover(self) -> None:
        self.storage.start_scheduler_run(TASK_NAME, event_key="leftover-1")
        self.assertEqual(self.monitor.recover_aborted_runs(), 1)
        runs = self.storage.read_scheduler_runs(task_name=TASK_NAME)
        self.assertEqual(runs[0].status, RunStatus.ABORTED.value)
        self.assertEqual(self.monitor.recover_aborted_runs(), 0)

    def test_interval_must_be_positive(self) -> None:
        from engine import EngineError

        with self.assertRaises(EngineError):
            self.monitor.run_forever(interval_seconds=0, iterations=1)

    def test_run_forever_does_not_swallow_keyboard_interrupt(self) -> None:
        with mock.patch.object(
            RiskMonitor, "monitor_once", side_effect=KeyboardInterrupt
        ):
            self.assertEqual(self.monitor.run_forever(interval_seconds=0.01), 0)


# --------------------------------------------------------------------------- #
# 9. 与引擎的集成
# --------------------------------------------------------------------------- #


class TestIntegrationWithEngine(MonitorTestCase):
    def test_full_round_trip_stays_consistent(self) -> None:
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        result = self.monitor.monitor_once(make_quote("157.900", "157.910"))
        self.assertTrue(result.did_close)
        report = self.engine.reconcile_account(strict=False)
        self.assertTrue(report.ok, msg=str(report.problems))
        self.assertEqual(report.open_position_count, 0)
        self.assertEqual(report.ledger_balance, Decimal("49940.000"))

    def test_take_profit_then_new_position_allowed(self) -> None:
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        self.monitor.monitor_once(make_quote("158.000", "158.010"))
        self.monitor.monitor_once(make_quote("158.500", "158.510"))
        self.assertEqual(self.engine.open_positions(), [])
        # 平仓后可再次开仓
        self.open_position(PositionSide.SHORT, stop="158.060", target="157.910", intent_id="after-tp")

    def test_snapshot_and_result_are_json_serializable(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        result = self.monitor.monitor_once(make_quote("158.000", "158.010", stale=True))
        json.loads(json.dumps(result.as_dict(), ensure_ascii=False))

    def test_gap_and_data_gap_dataclass_serialization(self) -> None:
        gap = DataGap(
            start=datetime(2026, 10, 10, 9, 0, tzinfo=_JST),
            end=datetime(2026, 10, 10, 9, 5, tzinfo=_JST),
            seconds=300, missing_bars=5, reason="测试缺口",
        )
        json.loads(json.dumps(gap.as_dict(), ensure_ascii=False))

    def test_fake_quote_helper_is_usable(self) -> None:
        quote = _fake_quote(self.cfg, "158.000", "158.010")
        self.assertEqual(quote.symbol, self.cfg.instrument.symbol)
        self.assertFalse(quote.is_stale)


if __name__ == "__main__":
    unittest.main(verbosity=2)
