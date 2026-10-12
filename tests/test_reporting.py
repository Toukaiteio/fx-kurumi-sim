"""reporting 模块的自动化测试。

覆盖：早/晚报告时段、资金口径（出入金不计入盈亏）、累计净利润与收益率公式、
交易成本拆解、持仓与止损止盈状态、本期事件、风险关注项、快照导出与不覆盖、
精度无损往返、快照校验（格式 / 自哈希 / 公式 / 与权威账本一致性）、以及
「只读」约束（报告模块不得修改账户）。

所有测试都在临时目录建库并注入模拟行情，**不访问网络、不连接券商、不做真实交易**。

运行方式（项目根目录 E:\\pj2\\fx-kurumi-sim）::

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import load_config  # noqa: E402
from engine import CloseReason, TradeIntent, TradingEngine  # noqa: E402
from market_data import (  # noqa: E402
    Granularity,
    MarketDataError,
    Quote,
    QuoteType,
)
from reporting import (  # noqa: E402
    SNAPSHOT_VERSION,
    ReportKind,
    build_report_data,
    export_snapshot,
    generate_report_template,
    latest_report_slot,
    load_snapshot,
    report_period_for,
    verify_snapshot,
)
from storage import FlowType, PositionSide, Storage  # noqa: E402

_JST = timezone(timedelta(hours=9))
_INITIAL = Decimal("50000")


def make_quote(bid: str, ask: str, *, stale: bool = False) -> Quote:
    bid_d, ask_d = Decimal(bid), Decimal(ask)
    return Quote(
        symbol="USD/JPY", bid=bid_d, ask=ask_d, mid=(bid_d + ask_d) / 2,
        timestamp=datetime.now(_JST), source="test_source",
        source_url="https://example.invalid/test", quote_type=QuoteType.BID_ASK,
        granularity=Granularity.TICK, is_stale=stale,
        age_seconds=Decimal("999") if stale else Decimal("0"),
    )


class ReportingTestCase(unittest.TestCase):
    """公共夹具：临时库 + 已初始化账户 + 可注入行情。"""

    #: 固定的账户初始化时刻（JST），让所有时点显式可比
    OPENED_AT = datetime(2026, 10, 10, 8, 0, tzinfo=_JST)
    OPENED_AT_ISO = "2026-10-10T08:00:00+09:00"
    OPEN_AT = datetime(2026, 10, 10, 9, 0, tzinfo=_JST)
    REPORT_AT = datetime(2026, 10, 10, 20, 0, tzinfo=_JST)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "data" / "fxsim.sqlite3"
        self.reports_dir = self.root / "reports"
        self.cfg = load_config()
        self.storage = Storage(self.db_path, tz=_JST)
        self.engine = TradingEngine(self.cfg, self.storage)
        # 用固定时间初始化，使所有时点显式、测试结果不依赖运行时刻
        self.storage.initialize_account(_INITIAL, opened_at=self.OPENED_AT)
        self.quote: Quote | None = make_quote("158.000", "158.010")

    # -- 工具 -------------------------------------------------------------- #

    def build(self, kind=None, **kwargs):
        kwargs.setdefault("cfg", self.cfg)
        kwargs.setdefault("engine", self.engine)
        kwargs.setdefault("quote", self.quote)
        kwargs.setdefault("report_at", self.REPORT_AT)
        return build_report_data(kind, **kwargs)

    def open_position(self, side=PositionSide.LONG, *, stop=None, target=None, volume="1000"):
        if stop is None:
            stop = "157.950" if side is PositionSide.LONG else "158.060"
        if target is None:
            target = "158.100" if side is PositionSide.LONG else "157.910"
        intent = TradeIntent(
            symbol="USD/JPY", side=side, volume_usd=Decimal(volume),
            stop_loss=Decimal(stop), take_profit=Decimal(target),
            intent_id=f"r-{side.value}-{stop}-{target}-{volume}-{id(self)}",
        )
        return self.engine.execute_virtual_trade(
            intent, make_quote("158.000", "158.010"), occurred_at=self.OPEN_AT
        )

    def close_position(self, position_id, bid="158.500", ask="158.510", reason=CloseReason.MANUAL):
        return self.engine.close_position(
            position_id, make_quote(bid, ask), reason=reason,
            occurred_at=datetime(2026, 10, 10, 10, 0, tzinfo=_JST),
        )

    def deposit(self, amount: str, key: str, *, at: datetime | None = None) -> None:
        self.storage.record_cash_flow(
            flow_type=FlowType.DEPOSIT, amount=Decimal(amount), event_key=key,
            occurred_at=at or datetime(2026, 10, 10, 12, 0, tzinfo=_JST),
        )

    def withdraw(self, amount: str, key: str, *, at: datetime | None = None) -> None:
        self.storage.record_cash_flow(
            flow_type=FlowType.WITHDRAWAL, amount=-Decimal(amount), event_key=key,
            occurred_at=at or datetime(2026, 10, 10, 12, 30, tzinfo=_JST),
        )


# --------------------------------------------------------------------------- #
# 1. 报告时段
# --------------------------------------------------------------------------- #


class TestReportPeriods(unittest.TestCase):
    def test_morning_and_evening_slot_hours(self) -> None:
        self.assertEqual(ReportKind.MORNING.slot_hour, 8)
        self.assertEqual(ReportKind.EVENING.slot_hour, 20)

    def test_latest_slot_evening(self) -> None:
        kind, slot = latest_report_slot(datetime(2026, 10, 10, 20, 30, tzinfo=_JST), _JST)
        self.assertEqual(kind, ReportKind.EVENING)
        self.assertEqual(slot, datetime(2026, 10, 10, 20, 0, tzinfo=_JST))

    def test_latest_slot_morning(self) -> None:
        kind, slot = latest_report_slot(datetime(2026, 10, 10, 12, 0, tzinfo=_JST), _JST)
        self.assertEqual(kind, ReportKind.MORNING)
        self.assertEqual(slot, datetime(2026, 10, 10, 8, 0, tzinfo=_JST))

    def test_before_eight_belongs_to_previous_evening(self) -> None:
        kind, slot = latest_report_slot(datetime(2026, 10, 11, 3, 0, tzinfo=_JST), _JST)
        self.assertEqual(kind, ReportKind.EVENING)
        self.assertEqual(slot, datetime(2026, 10, 10, 20, 0, tzinfo=_JST))

    def test_period_is_twelve_hours_ending_at_slot(self) -> None:
        slot = datetime(2026, 10, 10, 20, 0, tzinfo=_JST)
        start, end = report_period_for(ReportKind.EVENING, slot)
        self.assertEqual(end, slot)
        self.assertEqual(start, datetime(2026, 10, 10, 8, 0, tzinfo=_JST))
        self.assertEqual(int((end - start).total_seconds()), 12 * 3600)

    def test_kind_from_value_accepts_aliases(self) -> None:
        self.assertEqual(ReportKind.from_value("morning"), ReportKind.MORNING)
        self.assertEqual(ReportKind.from_value("EVENING"), ReportKind.EVENING)
        self.assertEqual(ReportKind.from_value("早间"), ReportKind.MORNING)
        with self.assertRaises(Exception):
            ReportKind.from_value("noon")

    def test_morning_report_period_covers_overnight(self) -> None:
        slot = datetime(2026, 10, 11, 8, 0, tzinfo=_JST)
        start, end = report_period_for(ReportKind.MORNING, slot)
        self.assertEqual(start, datetime(2026, 10, 10, 20, 0, tzinfo=_JST))
        self.assertEqual(end, slot)


# --------------------------------------------------------------------------- #
# 2. 资金口径：出入金不计入盈亏
# --------------------------------------------------------------------------- #


class TestFundingVsProfit(ReportingTestCase):
    def test_net_profit_is_zero_without_trading(self) -> None:
        data = self.build()
        self.assertEqual(data.account.net_profit, Decimal("0.000"))
        self.assertEqual(data.account.return_pct, Decimal("0.00"))
        self.assertEqual(data.account.total_deposits, Decimal("0.000"))
        self.assertEqual(data.account.total_withdrawals, Decimal("0.000"))

    def test_initial_capital_not_counted_as_deposit(self) -> None:
        data = self.build()
        self.assertEqual(data.account.initial_capital, _INITIAL)
        self.assertEqual(data.account.total_deposits, Decimal("0.000"))
        self.assertEqual(data.account.deposit_count, 0)

    def test_deposit_does_not_inflate_profit(self) -> None:
        self.deposit("10000", "d1")
        data = self.build()
        self.assertEqual(data.account.equity, Decimal("60000.000"))
        self.assertEqual(data.account.total_deposits, Decimal("10000.000"))
        self.assertEqual(data.account.deposit_count, 1)
        self.assertEqual(data.account.net_profit, Decimal("0.000"))  # 入金不是收益
        self.assertEqual(data.account.return_pct, Decimal("0.00"))

    def test_withdrawal_does_not_create_loss(self) -> None:
        self.deposit("10000", "d2")
        self.withdraw("4000", "w2")
        data = self.build()
        self.assertEqual(data.account.equity, Decimal("56000.000"))
        self.assertEqual(data.account.total_withdrawals, Decimal("4000.000"))
        self.assertEqual(data.account.net_profit, Decimal("0.000"))  # 出金不是亏损

    def test_realized_profit_counts(self) -> None:
        execution = self.open_position(PositionSide.LONG)
        self.close_position(execution.position.position_id, "158.500", "158.510")
        data = self.build()
        self.assertEqual(data.account.realized_pnl, Decimal("490.000"))
        self.assertEqual(data.account.equity, Decimal("50490.000"))
        self.assertEqual(data.account.net_profit, Decimal("490.000"))
        # 490 / 50000 * 100 = 0.98%
        self.assertEqual(data.account.return_pct, Decimal("0.98"))

    def test_deposit_and_profit_combine_correctly(self) -> None:
        self.deposit("10000", "d3")
        execution = self.open_position(PositionSide.LONG)
        self.close_position(execution.position.position_id, "158.500", "158.510")
        data = self.build()
        # 净值 = 50000 + 10000 + 490
        self.assertEqual(data.account.equity, Decimal("60490.000"))
        # 净利润 = 60490 + 0 - 50000 - 10000 = 490
        self.assertEqual(data.account.net_profit, Decimal("490.000"))
        # 收益率分母含入金：490 / 60000 * 100
        self.assertEqual(data.account.return_pct, Decimal("0.82"))

    def test_formula_identity_holds_with_mixed_flows(self) -> None:
        self.deposit("8000", "d4")
        self.withdraw("3000", "w4")
        execution = self.open_position(PositionSide.LONG)
        self.close_position(execution.position.position_id, "158.500", "158.510")
        data = self.build()
        a = data.account
        expected = (
            a.equity + a.total_withdrawals - a.initial_capital - a.total_deposits
        )
        self.assertEqual(a.net_profit, expected)
        contribution = a.initial_capital + a.total_deposits
        self.assertEqual(
            a.return_pct, (a.net_profit / contribution * 100).quantize(Decimal("0.01"))
        )


# --------------------------------------------------------------------------- #
# 3. 交易成本拆解
# --------------------------------------------------------------------------- #


class TestTradingCost(ReportingTestCase):
    def test_no_cost_initially(self) -> None:
        data = self.build()
        self.assertEqual(data.costs.total_charged_jpy, Decimal("0.000"))
        self.assertEqual(data.costs.embedded_spread_jpy, Decimal("0.000"))

    def test_standalone_fee_counted_once(self) -> None:
        self.engine.record_standalone_fee(Decimal("120"), fee_id="c1")
        self.engine.record_standalone_fee(Decimal("120"), fee_id="c1")  # 幂等
        data = self.build()
        self.assertEqual(data.costs.explicit_fees_jpy, Decimal("120.000"))
        self.assertEqual(data.costs.total_charged_jpy, Decimal("120.000"))
        self.assertEqual(data.account.cash_balance, _INITIAL - Decimal("120"))

    def test_swap_fee_counted_separately(self) -> None:
        self.engine.record_swap_fee(Decimal("-30"), swap_id="s1", verified=True)
        data = self.build()
        self.assertEqual(data.costs.swap_fees_jpy, Decimal("30.000"))
        self.assertEqual(data.costs.explicit_fees_jpy, Decimal("0.000"))
        self.assertEqual(data.costs.total_charged_jpy, Decimal("30.000"))

    def test_embedded_spread_is_estimated_but_not_deducted(self) -> None:
        execution = self.open_position(PositionSide.LONG)
        self.close_position(execution.position.position_id, "158.500", "158.510")
        data = self.build()
        # 成交价是单边价，每笔只付半个点差：0.01/2 × 1000 × 2 笔 = 10
        self.assertEqual(data.costs.embedded_spread_jpy, Decimal("10.000"))
        # 但现金只反映盈亏（490），点差没有被再扣一次
        self.assertEqual(data.account.cash_balance, Decimal("50490.000"))
        self.assertEqual(data.costs.total_charged_jpy, Decimal("0.000"))
        self.assertEqual(
            data.costs.total_including_embedded_jpy, Decimal("10.000")
        )

    def test_embedded_spread_matches_actual_round_trip_cost(self) -> None:
        """一开一平（中间价不变）的真实点差成本 = 一个完整点差 × 数量。"""
        execution = self.open_position(PositionSide.LONG)
        # 开仓 Ask 158.010、平仓 Bid 158.010 → 中间价不变，盈亏为 0
        self.close_position(execution.position.position_id, "158.010", "158.020")
        data = self.build()
        spread = self.cfg.market.spread_jpy
        self.assertEqual(
            data.costs.embedded_spread_jpy, spread * Decimal("1000")
        )
        self.assertEqual(data.account.cash_balance, Decimal("50000.000"))

    def test_cost_breakdown_serialization(self) -> None:
        payload = self.build().to_payload()["cumulative_trading_cost"]
        for key in (
            "explicit_fees_jpy", "swap_fees_jpy", "embedded_spread_jpy",
            "total_charged_jpy", "total_including_embedded_jpy",
        ):
            self.assertIsInstance(payload[key], str)


# --------------------------------------------------------------------------- #
# 4. 行情与不可核实字段
# --------------------------------------------------------------------------- #


class TestMarketAndUnverifiable(ReportingTestCase):
    def test_market_snapshot_fields(self) -> None:
        market = self.build().market
        self.assertTrue(market.quote_available)
        self.assertEqual(market.bid, Decimal("158.000"))
        self.assertEqual(market.ask, Decimal("158.010"))
        self.assertEqual(market.mid, Decimal("158.005"))
        self.assertEqual(market.spread_jpy, Decimal("0.010"))
        self.assertEqual(market.source, "test_source")
        self.assertEqual(market.quote_type, "bid_ask")
        self.assertTrue(market.valid_for_decisions)

    def test_missing_quote_yields_nulls_not_zeros(self) -> None:
        data = self.build(quote=None, quote_source=lambda: (_ for _ in ()).throw(
            MarketDataError("行情源故障")
        ))
        self.assertFalse(data.market.quote_available)
        self.assertIsNone(data.market.bid)
        self.assertIsNone(data.account.unrealized_pnl)
        self.assertIsNone(data.account.equity)
        self.assertIsNone(data.account.net_profit)
        self.assertIsNone(data.account.return_pct)
        self.assertIsNone(data.account.margin_level)
        self.assertIsNone(data.account.free_margin)
        self.assertTrue(data.data_quality["unverifiable_fields"])
        payload = data.to_payload()
        self.assertIsNone(payload["account"]["equity"])
        self.assertIsNone(payload["account"]["net_profit"])
        # 可核实的事实仍然给出
        self.assertEqual(payload["account"]["cash_balance"], "50000.000")

    def test_stale_quote_marks_invalid_for_decisions(self) -> None:
        data = self.build(quote=make_quote("158.000", "158.010", stale=True))
        self.assertTrue(data.market.is_stale)
        self.assertFalse(data.market.valid_for_decisions)
        self.assertTrue(any("过期" in issue for issue in data.market.issues))

    def test_position_distances_null_without_quote(self) -> None:
        self.open_position(PositionSide.LONG)
        data = self.build(quote=None, quote_source=lambda: (_ for _ in ()).throw(
            MarketDataError("x")
        ))
        item = data.positions[0]
        self.assertIsNone(item.unrealized_pnl_jpy)
        self.assertIsNone(item.stop_distance_jpy)
        self.assertIsNone(item.stop_hit)
        self.assertEqual(item.position.stop_loss, Decimal("157.950"))  # 库里的仍可核实


# --------------------------------------------------------------------------- #
# 5. 持仓与止损止盈状态
# --------------------------------------------------------------------------- #


class TestPositions(ReportingTestCase):
    def test_no_positions_is_empty_array(self) -> None:
        payload = self.build().to_payload()
        self.assertEqual(payload["positions"], [])
        self.assertEqual(payload["integrity"]["open_position_count"], 0)

    def test_position_snapshot_fields(self) -> None:
        self.open_position(PositionSide.LONG)
        data = self.build(quote=make_quote("158.500", "158.510"))
        item = data.positions[0]
        self.assertEqual(item.position.side, "LONG")
        self.assertEqual(item.position.volume, Decimal("1000"))
        self.assertEqual(item.position.entry_price, Decimal("158.010"))
        self.assertEqual(item.executable_exit_price, Decimal("158.500"))  # 平多走 Bid
        self.assertEqual(item.unrealized_pnl_jpy, Decimal("490.000"))
        self.assertEqual(item.stop_distance_jpy, Decimal("0.550"))   # 158.500 - 157.950
        self.assertEqual(item.target_distance_jpy, Decimal("0.400"))  # 158.100 - 158.500 → 绝对值
        self.assertEqual(item.reward_risk_ratio, Decimal("1.5"))
        self.assertFalse(item.stop_hit)
        self.assertTrue(item.target_hit)  # bid 158.500 已越过止盈 158.100

    def test_position_neither_stop_nor_target_hit(self) -> None:
        self.open_position(PositionSide.LONG)
        item = self.build(quote=make_quote("158.050", "158.060")).positions[0]
        self.assertFalse(item.stop_hit)
        self.assertFalse(item.target_hit)
        self.assertEqual(item.unrealized_pnl_jpy, Decimal("40.000"))

    def test_stop_and_target_hit_flags(self) -> None:
        self.open_position(PositionSide.LONG)
        hit_stop = self.build(quote=make_quote("157.900", "157.910")).positions[0]
        self.assertTrue(hit_stop.stop_hit)
        hit_target = self.build(quote=make_quote("158.200", "158.210")).positions[0]
        self.assertTrue(hit_target.target_hit)

    def test_short_position_uses_ask(self) -> None:
        self.open_position(PositionSide.SHORT)
        item = self.build(quote=make_quote("157.900", "157.910")).positions[0]
        self.assertEqual(item.executable_exit_price, Decimal("157.910"))
        self.assertEqual(item.unrealized_pnl_jpy, Decimal("90.000"))

    def test_stop_risk_includes_slippage(self) -> None:
        self.open_position(PositionSide.LONG)
        item = self.build(quote=make_quote("158.500", "158.510")).positions[0]
        # (0.550 + 0.005) * 1000
        self.assertEqual(item.stop_risk_jpy, Decimal("555.000"))

    def test_position_json_has_decimal_strings(self) -> None:
        self.open_position(PositionSide.LONG)
        payload = self.build().to_payload()
        entry = payload["positions"][0]
        for key in ("volume_usd", "entry_price", "margin_jpy", "stop_loss", "take_profit"):
            self.assertIsInstance(entry[key], str)
        self.assertIsInstance(entry["unrealized_pnl_jpy"], str)


# --------------------------------------------------------------------------- #
# 6. 本期事件与风险关注
# --------------------------------------------------------------------------- #


class TestEventsAndRiskWatch(ReportingTestCase):
    def test_events_include_open_and_close(self) -> None:
        execution = self.open_position(PositionSide.LONG)
        self.close_position(execution.position.position_id, "158.500", "158.510")
        data = self.build()
        kinds = [event.kind for event in data.events]
        self.assertIn("POSITION_OPENED", kinds)
        self.assertIn("POSITION_CLOSE_EXECUTED", kinds)
        summaries = " ".join(event.summary for event in data.events)
        self.assertIn("开仓", summaries)
        self.assertIn("平仓", summaries)

    def test_events_limited_to_period(self) -> None:
        execution = self.open_position(PositionSide.LONG)
        self.close_position(execution.position.position_id, "158.500", "158.510")
        # 报告时段设为很久之前 → 不应包含这些事件
        data = self.build(report_at=datetime(2026, 10, 1, 20, 0, tzinfo=_JST))
        self.assertEqual(data.events, ())
        self.assertEqual(data.trades_in_period, ())

    def test_trades_in_period(self) -> None:
        execution = self.open_position(PositionSide.LONG)
        self.close_position(execution.position.position_id, "158.500", "158.510")
        data = self.build()
        self.assertEqual(len(data.trades_in_period), 2)
        self.assertEqual([t.action for t in data.trades_in_period], ["OPEN", "CLOSE"])

    def test_cash_flows_in_period(self) -> None:
        self.deposit("1000", "d-evt")
        data = self.build()
        keys = [f.event_key for f in data.flows_in_period]
        self.assertIn("d-evt", keys)
        self.assertIn("INITIAL_CAPITAL", keys)  # 初始本金也落在本时段内
        self.assertTrue(all(f.flow_type == "DEPOSIT" for f in data.flows_in_period))

    def test_risk_watch_covers_configured_thresholds(self) -> None:
        data = self.build()
        codes = {item.code for item in data.risk_watch}
        for expected in (
            "MARGIN_LEVEL_MARGIN_CALL", "MARGIN_LEVEL_STOP_OUT", "DAILY_DRAWDOWN",
            "MARGIN_USAGE", "EFFECTIVE_LEVERAGE", "MIN_EQUITY_TO_OPEN",
            "BANKRUPTCY_EQUITY", "QUOTE_VALIDITY", "LEDGER_INTEGRITY",
        ):
            self.assertIn(expected, codes)

    def test_risk_watch_thresholds_match_config(self) -> None:
        data = self.build()
        by_code = {item.code: item for item in data.risk_watch}
        self.assertEqual(
            by_code["MARGIN_LEVEL_MARGIN_CALL"].threshold, self.cfg.margin.margin_call_level
        )
        self.assertEqual(
            by_code["MARGIN_LEVEL_STOP_OUT"].threshold, self.cfg.margin.stop_out_level
        )
        self.assertEqual(
            by_code["DAILY_DRAWDOWN"].threshold, self.cfg.risk.daily_drawdown_stop_ratio
        )
        self.assertEqual(
            by_code["BANKRUPTCY_EQUITY"].threshold, self.cfg.margin.bankruptcy_equity_jpy
        )

    def test_risk_watch_includes_position_levels(self) -> None:
        execution = self.open_position(PositionSide.LONG)
        data = self.build()
        codes = {item.code for item in data.risk_watch}
        self.assertIn(f"POSITION_{execution.position.position_id}_STOP", codes)
        self.assertIn(f"POSITION_{execution.position.position_id}_TARGET", codes)

    def test_risk_watch_flags_bad_quote(self) -> None:
        data = self.build(quote=make_quote("158.000", "158.010", stale=True))
        by_code = {item.code: item for item in data.risk_watch}
        self.assertTrue(by_code["QUOTE_VALIDITY"].triggered)
        self.assertEqual(by_code["QUOTE_VALIDITY"].severity, "critical")

    def test_risk_watch_unknown_when_quote_missing(self) -> None:
        data = self.build(quote=None, quote_source=lambda: (_ for _ in ()).throw(
            MarketDataError("x")
        ))
        by_code = {item.code: item for item in data.risk_watch}
        self.assertEqual(by_code["MARGIN_LEVEL_STOP_OUT"].severity, "unknown")
        self.assertIsNone(by_code["MARGIN_LEVEL_STOP_OUT"].current)
        self.assertEqual(by_code["MARGIN_LEVEL_STOP_OUT"].note, "缺行情，无法核实")


# --------------------------------------------------------------------------- #
# 7. 导出与精度往返
# --------------------------------------------------------------------------- #


class TestExportAndRoundTrip(ReportingTestCase):
    def test_export_creates_file_with_jst_timestamp(self) -> None:
        path = export_snapshot(cfg=self.cfg, engine=self.engine, quote=self.quote,
                               out_dir=self.reports_dir,
                               report_at=datetime(2026, 10, 10, 20, 0, tzinfo=_JST))
        self.assertTrue(path.is_file())
        self.assertIn("evening", path.name)
        self.assertIn("20261010T200000+0900", path.name)
        self.assertEqual(path.parent, self.reports_dir)

    def test_morning_and_evening_filenames_differ(self) -> None:
        morning = export_snapshot(cfg=self.cfg, engine=self.engine, quote=self.quote,
                                  out_dir=self.reports_dir, kind="morning",
                                  report_at=datetime(2026, 10, 10, 8, 0, tzinfo=_JST))
        evening = export_snapshot(cfg=self.cfg, engine=self.engine, quote=self.quote,
                                  out_dir=self.reports_dir, kind="evening",
                                  report_at=datetime(2026, 10, 10, 20, 0, tzinfo=_JST))
        self.assertIn("morning", morning.name)
        self.assertIn("evening", evening.name)
        self.assertNotEqual(morning, evening)

    def test_export_never_overwrites(self) -> None:
        first = export_snapshot(cfg=self.cfg, engine=self.engine, quote=self.quote,
                                out_dir=self.reports_dir,
                                report_at=datetime(2026, 10, 10, 20, 0, tzinfo=_JST))
        second = export_snapshot(cfg=self.cfg, engine=self.engine, quote=self.quote,
                                 out_dir=self.reports_dir,
                                 report_at=datetime(2026, 10, 10, 20, 0, tzinfo=_JST))
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file() and second.is_file())
        self.assertTrue(second.name.endswith("-1.json"))

    def test_round_trip_preserves_decimals_exactly(self) -> None:
        self.deposit("1234.567", "prec")
        self.open_position(PositionSide.LONG, volume="1000")
        data = self.build(quote=make_quote("158.24688447", "158.25688447"))
        path = export_snapshot(data, out_dir=self.reports_dir, cfg=self.cfg)
        raw = path.read_text(encoding="utf-8")
        payload = load_snapshot(path)

        # 文件里必须是字符串，且不存在被 JSON 转成浮点的痕迹
        self.assertIn('"total_deposits": "1234.567"', raw)
        self.assertEqual(payload["account"]["total_deposits"], "1234.567")
        self.assertEqual(Decimal(payload["account"]["total_deposits"]), Decimal("1234.567"))
        self.assertEqual(
            Decimal(payload["positions"][0]["entry_price"]), Decimal("158.010")
        )
        self.assertEqual(
            Decimal(payload["market"]["bid"]), Decimal("158.24688447")
        )
        # 再解析一次仍完全相等
        self.assertEqual(
            Decimal(payload["account"]["cash_balance"]),
            self.storage.ledger_balance(),
        )

    def test_round_trip_keeps_nulls_as_null(self) -> None:
        path = export_snapshot(cfg=self.cfg, engine=self.engine, out_dir=self.reports_dir,
                               quote=None,
                               quote_source=lambda: (_ for _ in ()).throw(MarketDataError("x")))
        payload = load_snapshot(path)
        self.assertIsNone(payload["account"]["equity"])
        self.assertIsNone(payload["account"]["net_profit"])
        self.assertIsNone(payload["market"]["bid"])
        self.assertIsNone(payload["account"]["unrealized_pnl"])

    def test_round_trip_empty_positions_is_array(self) -> None:
        path = export_snapshot(cfg=self.cfg, engine=self.engine, quote=self.quote,
                               out_dir=self.reports_dir)
        payload = load_snapshot(path)
        self.assertEqual(payload["positions"], [])
        self.assertEqual(payload["trades_in_period"], [])

    def test_snapshot_has_self_hash(self) -> None:
        path = export_snapshot(cfg=self.cfg, engine=self.engine, quote=self.quote,
                               out_dir=self.reports_dir)
        payload = load_snapshot(path)
        self.assertEqual(len(payload["snapshot_sha256"]), 64)
        self.assertEqual(payload["snapshot_version"], SNAPSHOT_VERSION)

    def test_load_snapshot_rejects_non_object(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text("[1,2,3]", encoding="utf-8")
        with self.assertRaises(Exception):
            load_snapshot(bad)


# --------------------------------------------------------------------------- #
# 8. 快照校验
# --------------------------------------------------------------------------- #


class TestVerifySnapshot(ReportingTestCase):
    def export(self, *, report_at: datetime | None = None, kind: str = "evening", **kwargs) -> Path:
        kwargs.setdefault("quote", self.quote)
        return export_snapshot(
            cfg=self.cfg, engine=self.engine, out_dir=self.reports_dir,
            kind=kind, report_at=report_at or self.REPORT_AT, **kwargs,
        )

    def test_valid_snapshot_passes(self) -> None:
        path = self.export()
        result = verify_snapshot(path, cfg=self.cfg, storage=self.storage)
        self.assertTrue(result.ok, msg=str(result.problems))
        self.assertTrue(result.format_ok)
        self.assertTrue(result.self_hash_ok)
        self.assertTrue(result.formulas_ok)
        self.assertTrue(result.ledger_ok)
        self.assertEqual(result.report_kind, "EVENING")

    def test_detects_tampered_amount(self) -> None:
        path = self.export()
        payload = load_snapshot(path)
        payload["account"]["cash_balance"] = "999999"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = verify_snapshot(path, cfg=self.cfg, storage=self.storage)
        self.assertFalse(result.ok)
        self.assertFalse(result.self_hash_ok)
        self.assertTrue(any("自哈希" in p for p in result.problems))

    def test_detects_ledger_mismatch_even_if_hash_recomputed(self) -> None:
        """即使攻击者重算了自哈希，与权威账本的比对仍能发现改动。"""
        path = self.export()
        payload = load_snapshot(path)
        payload["account"]["cash_balance"] = "999999"
        from reporting import _payload_hash

        payload["snapshot_sha256"] = _payload_hash(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = verify_snapshot(path, cfg=self.cfg, storage=self.storage)
        self.assertFalse(result.ok)
        self.assertTrue(result.self_hash_ok)  # 自哈希自洽
        self.assertFalse(result.ledger_ok)  # 但账本对不上
        self.assertTrue(any("与账本不一致" in p for p in result.problems))

    def test_detects_broken_profit_formula(self) -> None:
        path = self.export()
        payload = load_snapshot(path)
        payload["account"]["net_profit"] = "123456"
        from reporting import _payload_hash

        payload["snapshot_sha256"] = _payload_hash(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = verify_snapshot(path, cfg=self.cfg, storage=self.storage)
        self.assertFalse(result.ok)
        self.assertFalse(result.formulas_ok)
        self.assertTrue(any("累计净利润公式不成立" in p for p in result.problems))

    def test_detects_broken_return_formula(self) -> None:
        path = self.export()
        payload = load_snapshot(path)
        payload["account"]["return_pct"] = "99.99"
        from reporting import _payload_hash

        payload["snapshot_sha256"] = _payload_hash(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = verify_snapshot(path, cfg=self.cfg, storage=self.storage)
        self.assertFalse(result.ok)
        self.assertTrue(any("累计收益率公式不成立" in p for p in result.problems))

    def test_detects_fake_zero_when_quote_unavailable(self) -> None:
        path = self.export(quote=None,
                           quote_source=lambda: (_ for _ in ()).throw(MarketDataError("x")))
        payload = load_snapshot(path)
        payload["account"]["equity"] = "0"
        from reporting import _payload_hash

        payload["snapshot_sha256"] = _payload_hash(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = verify_snapshot(path, cfg=self.cfg, storage=self.storage)
        self.assertFalse(result.ok)
        self.assertTrue(any("不得伪装成 0" in p for p in result.problems))

    def test_detects_missing_field(self) -> None:
        path = self.export()
        payload = load_snapshot(path)
        del payload["risk_watch"]
        from reporting import _payload_hash

        payload["snapshot_sha256"] = _payload_hash(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = verify_snapshot(path, cfg=self.cfg, storage=self.storage)
        self.assertFalse(result.ok)
        self.assertFalse(result.format_ok)
        self.assertTrue(any("risk_watch" in p for p in result.problems))

    def test_detects_wrong_version(self) -> None:
        path = self.export()
        payload = load_snapshot(path)
        payload["snapshot_version"] = 99
        from reporting import _payload_hash

        payload["snapshot_sha256"] = _payload_hash(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = verify_snapshot(path, cfg=self.cfg, storage=self.storage)
        self.assertFalse(result.ok)
        self.assertTrue(any("版本不兼容" in p for p in result.problems))

    def test_detects_float_instead_of_decimal_string(self) -> None:
        path = self.export()
        payload = load_snapshot(path)
        payload["account"]["cash_balance"] = 50000.0
        from reporting import _payload_hash

        payload["snapshot_sha256"] = _payload_hash(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = verify_snapshot(path, cfg=self.cfg, storage=self.storage)
        self.assertFalse(result.ok)
        self.assertTrue(any("必须是十进制字符串" in p for p in result.problems))

    def test_historical_snapshot_still_verifies_after_new_activity(self) -> None:
        """历史快照按 report_at 重建账本，不会被之后的新流水误判为不一致。"""
        execution = self.open_position(PositionSide.LONG)  # 09:00
        # 报告时点取 09:30：包含开仓与初始本金，但不包含之后的平仓与入金
        path = self.export(
            kind="morning", report_at=datetime(2026, 10, 10, 9, 30, tzinfo=_JST)
        )
        self.close_position(execution.position.position_id, "158.600", "158.610")  # 10:00
        self.deposit("5000", "later-deposit")  # 12:00
        result = verify_snapshot(path, cfg=self.cfg, storage=self.storage)
        self.assertTrue(result.ok, msg=str(result.problems))

    def test_missing_file_reported(self) -> None:
        result = verify_snapshot(self.root / "nope.json", cfg=self.cfg)
        self.assertFalse(result.ok)
        self.assertFalse(result.format_ok)
        self.assertTrue(any("无法读取快照" in p for p in result.problems))

    def test_can_verify_from_dict(self) -> None:
        payload = self.build().to_payload()
        result = verify_snapshot(payload, cfg=self.cfg, storage=self.storage)
        self.assertTrue(result.ok, msg=str(result.problems))
        self.assertIsNone(result.path)

    def test_verification_is_json_serializable(self) -> None:
        path = self.export()
        result = verify_snapshot(path, cfg=self.cfg, storage=self.storage)
        json.loads(json.dumps(result.as_dict(), ensure_ascii=False))


# --------------------------------------------------------------------------- #
# 9. 报告模板
# --------------------------------------------------------------------------- #


class TestTemplate(ReportingTestCase):
    def test_template_contains_required_sections(self) -> None:
        text = generate_report_template(self.build())
        for heading in (
            "## 1. 报告与行情", "## 2. 资金口径", "## 3. 账户表现",
            "## 4. 当前持仓", "## 5. 本时段成交",
            "## 6. 自上次报告以来的账户事件",
            "## 7. 下一周期需关注的风险触发条件", "## 8. 数据质量",
        ):
            self.assertIn(heading, text)
        self.assertIn("久留美的市场分析", text)
        self.assertIn("久留美的交易日记", text)

    def test_template_marks_unverifiable_instead_of_zero(self) -> None:
        data = self.build(quote=None, quote_source=lambda: (_ for _ in ()).throw(
            MarketDataError("x")
        ))
        text = generate_report_template(data)
        self.assertIn("无法核实", text)
        self.assertIn("当前净值：无法核实", text)
        self.assertNotIn("当前净值：0", text)

    def test_template_reports_no_positions_explicitly(self) -> None:
        text = generate_report_template(self.build())
        self.assertIn("当前无持仓。", text)
        self.assertIn("本时段无成交。", text)

    def test_template_lists_positions(self) -> None:
        self.open_position(PositionSide.LONG)
        text = generate_report_template(self.build(quote=make_quote("158.500", "158.510")))
        self.assertIn("| # | 方向 |", text)
        self.assertIn("LONG", text)
        self.assertIn("158.010", text)

    def test_template_includes_report_kind(self) -> None:
        morning = generate_report_template(self.build(kind="morning"))
        evening = generate_report_template(self.build(kind="evening"))
        self.assertIn("早间报告", morning)
        self.assertIn("晚间报告", evening)


# --------------------------------------------------------------------------- #
# 10. 只读约束
# --------------------------------------------------------------------------- #


class TestReadOnly(ReportingTestCase):
    def test_building_report_does_not_change_account(self) -> None:
        execution = self.open_position(PositionSide.LONG)
        before_balance = self.storage.ledger_balance()
        before_state = self.storage.read_account_state()
        before_counts = self.storage.counts()
        before_head = self.storage.audit_head()

        for _ in range(3):
            self.build(quote=make_quote("158.500", "158.510"))
            generate_report_template(self.build(quote=make_quote("158.500", "158.510")))

        self.assertEqual(self.storage.ledger_balance(), before_balance)
        self.assertEqual(self.storage.audit_head(), before_head)
        self.assertEqual(self.storage.counts(), before_counts)
        after_state = self.storage.read_account_state()
        self.assertEqual(after_state.revision, before_state.revision)
        self.assertEqual(after_state.equity, before_state.equity)
        self.assertEqual(after_state.unrealized_pnl, before_state.unrealized_pnl)
        self.assertEqual(
            self.storage.read_position(execution.position.position_id).status, "OPEN"
        )

    def test_export_only_writes_to_reports_dir(self) -> None:
        path = export_snapshot(cfg=self.cfg, engine=self.engine, quote=self.quote,
                               out_dir=self.reports_dir)
        self.assertEqual(path.parent.resolve(), self.reports_dir.resolve())
        # 数据库目录里没有新文件
        db_files = {p.name for p in (self.root / "data").iterdir()}
        self.assertIn("fxsim.sqlite3", db_files)
        self.assertNotIn(path.name, db_files)

    def test_report_module_does_not_import_llm(self) -> None:
        source = Path(
            sys.modules["reporting"].__file__
        ).read_text(encoding="utf-8")
        for forbidden in ("openai", "chat/completions", "llm.", "requests."):
            self.assertNotIn(forbidden, source)


class TestCapitalPressureCost(unittest.TestCase):
    """督促扣款必须出现在成本拆解里，且与手续费/掉期分开。"""

    def build(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "data" / "fxsim.sqlite3"
            cfg = load_config()
            storage = Storage(db, tz=_JST)
            storage.initialize_account(cfg.account.initial_capital_jpy)
            engine = TradingEngine(cfg, storage)
            for flow_type, amount, key in kwargs.get("flows", ()):
                storage.record_cash_flow(
                    flow_type=flow_type, amount=Decimal(amount), event_key=key
                )
            # 给一个报价：没有它就拿不到净值，也就算不出累计净利润。
            return build_report_data(
                ReportKind.EVENING, cfg=cfg, engine=engine, storage=storage,
                quote=make_quote("158.000", "158.010"),
            )

    def test_zero_when_never_charged(self) -> None:
        data = self.build()
        self.assertEqual(data.costs.capital_pressure_jpy, Decimal("0.000"))

    def test_recorded_separately_from_fees(self) -> None:
        data = self.build(flows=[
            ("CAPITAL_DECAY", "-3000", "cd1"),
            ("FEE", "-120", "fee1"),
        ])
        self.assertEqual(data.costs.capital_pressure_jpy, Decimal("3000.000"))
        self.assertEqual(data.costs.explicit_fees_jpy, Decimal("120.000"))
        # 督促扣款不能混进「实际扣款的手续费+掉期」里
        self.assertEqual(data.costs.total_charged_jpy, Decimal("120.000"))

    def test_serialized(self) -> None:
        payload = self.build(flows=[("CAPITAL_DECAY", "-3000", "cd1")]).to_payload()
        self.assertEqual(payload["cumulative_trading_cost"]["capital_pressure_jpy"],
                         "3000.000")

    def test_reduces_net_profit(self) -> None:
        """扣款必须让累计净利润真的下降——否则督促机制对 AI 没有说服力。"""
        before = self.build()
        after = self.build(flows=[("CAPITAL_DECAY", "-3000", "cd1")])
        self.assertLess(after.account.net_profit, before.account.net_profit)
        self.assertEqual(
            before.account.net_profit - after.account.net_profit, Decimal("3000.000")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
