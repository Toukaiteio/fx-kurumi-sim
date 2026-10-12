"""督促机制（每 N 小时扣本金）的自动化测试。

覆盖：槽位对齐、幂等（同槽位只扣一次）、不追溯生效、漏槽位补扣 / 不补扣、
扣款不超过现金余额、账本口径与审计链、dry-run 无副作用，
以及最关键的**与当日回撤的交互**——扣款必须不计入回撤，否则一次扣款就会
触发「当日停止开仓」，与「督促 AI 多行动」的目的正好相反。

全部在临时目录里跑，**不联网**。

运行方式（仓库根目录）::

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import load_config  # noqa: E402
from engine import (  # noqa: E402
    CAPITAL_DECAY_EVENT_PREFIX,
    CAPITAL_PRESSURE_ANCHOR_META_KEY,
    TradingEngine,
    capital_pressure_slot,
    capital_pressure_slots_between,
)
from market_data import Granularity, Quote, QuoteType  # noqa: E402
from storage import FlowType, Storage  # noqa: E402

_JST = timezone(timedelta(hours=9))
_INITIAL = Decimal("50000")


def make_quote(moment: datetime | None = None) -> Quote:
    bid, ask = Decimal("158.000"), Decimal("158.010")
    return Quote(
        symbol="USD/JPY", bid=bid, ask=ask, mid=(bid + ask) / 2,
        timestamp=moment or datetime(2026, 10, 12, 9, 0, tzinfo=_JST),
        source="pressure_test", source_url="", quote_type=QuoteType.BID_ASK,
        granularity=Granularity.TICK, is_stale=False, age_seconds=Decimal("0"),
    )


class PressureTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cfg = load_config()
        self.storage = Storage(self.root / "data" / "fxsim.sqlite3", tz=_JST)
        self.storage.initialize_account(
            _INITIAL, opened_at=datetime(2026, 10, 12, 8, 0, tzinfo=_JST)
        )
        self.engine = TradingEngine(self.cfg, self.storage)

    def at(self, hour: int, minute: int = 0, day: int = 12) -> datetime:
        return datetime(2026, 10, day, hour, minute, tzinfo=_JST)

    def baseline(self, moment: datetime | None = None) -> None:
        """建立当日基准（回撤口径的参照点）。"""
        self.engine.mark_to_market(make_quote(moment), occurred_at=moment or self.at(9))

    def pressure_flows(self) -> list:
        return self.storage.read_cash_flows(flow_type=FlowType.CAPITAL_DECAY)


# --------------------------------------------------------------------------- #
# 1. 槽位计算
# --------------------------------------------------------------------------- #


class TestSlots(unittest.TestCase):
    def test_slot_aligns_to_natural_hours(self) -> None:
        self.assertEqual(
            capital_pressure_slot(self.at_h(13, 37), tz=_JST, interval_hours=6),
            datetime(2026, 10, 12, 12, 0, tzinfo=_JST),
        )

    def at_h(self, hour: int, minute: int) -> datetime:
        return datetime(2026, 10, 12, hour, minute, tzinfo=_JST)

    def test_slot_on_boundary(self) -> None:
        self.assertEqual(
            capital_pressure_slot(self.at_h(18, 0), tz=_JST, interval_hours=6),
            datetime(2026, 10, 12, 18, 0, tzinfo=_JST),
        )

    def test_interval_one_hour(self) -> None:
        self.assertEqual(
            capital_pressure_slot(self.at_h(13, 59), tz=_JST, interval_hours=1),
            datetime(2026, 10, 12, 13, 0, tzinfo=_JST),
        )

    def test_interval_eight_hours(self) -> None:
        self.assertEqual(
            capital_pressure_slot(self.at_h(15, 0), tz=_JST, interval_hours=8),
            datetime(2026, 10, 12, 8, 0, tzinfo=_JST),
        )

    def test_slots_between_is_left_open_right_closed(self) -> None:
        slots = capital_pressure_slots_between(
            datetime(2026, 10, 12, 12, 0, tzinfo=_JST),
            datetime(2026, 10, 12, 18, 0, tzinfo=_JST),
            tz=_JST, interval_hours=6,
        )
        self.assertEqual([s.hour for s in slots], [18])

    def test_slots_between_same_slot_is_empty(self) -> None:
        slots = capital_pressure_slots_between(
            datetime(2026, 10, 12, 18, 5, tzinfo=_JST),
            datetime(2026, 10, 12, 18, 55, tzinfo=_JST),
            tz=_JST, interval_hours=6,
        )
        self.assertEqual(slots, [])

    def test_slots_between_crosses_days(self) -> None:
        slots = capital_pressure_slots_between(
            datetime(2026, 10, 12, 12, 0, tzinfo=_JST),
            datetime(2026, 10, 13, 6, 0, tzinfo=_JST),
            tz=_JST, interval_hours=6,
        )
        self.assertEqual(
            [(s.day, s.hour) for s in slots], [(12, 18), (13, 0), (13, 6)]
        )


# --------------------------------------------------------------------------- #
# 2. 扣款行为
# --------------------------------------------------------------------------- #


class TestDeduction(PressureTestCase):
    def test_first_call_does_not_charge(self) -> None:
        """机制刚启用不立刻扣款：起算点从当前槽位开始。"""
        result = self.engine.apply_capital_pressure(now=self.at(13, 37))
        self.assertFalse(result.applied)
        self.assertEqual(result.charged_jpy, Decimal("0"))
        self.assertEqual(self.engine.cash_balance(), _INITIAL)
        self.assertIn("未到下一个扣款槽位", result.skipped_reason)

    def test_charges_at_next_slot(self) -> None:
        self.engine.apply_capital_pressure(now=self.at(13, 37))     # 立起算点
        result = self.engine.apply_capital_pressure(now=self.at(18, 0))
        self.assertTrue(result.applied)
        self.assertEqual(result.charged_jpy, Decimal("3000"))
        self.assertEqual(list(result.slots), ["2026-10-12T18"])
        self.assertEqual(self.engine.cash_balance(), _INITIAL - 3000)

    def test_charge_is_idempotent_within_slot(self) -> None:
        self.engine.apply_capital_pressure(now=self.at(13, 0))
        self.engine.apply_capital_pressure(now=self.at(18, 0))
        for minute in (5, 30, 59):
            again = self.engine.apply_capital_pressure(now=self.at(18, minute))
            self.assertFalse(again.applied, msg=minute)
            self.assertEqual(again.charged_jpy, Decimal("0"))
        self.assertEqual(self.engine.cash_balance(), _INITIAL - 3000)
        self.assertEqual(len(self.pressure_flows()), 1)

    def test_charge_survives_restart(self) -> None:
        """换一个 engine 实例（模拟进程重启）也不会重复扣。"""
        self.engine.apply_capital_pressure(now=self.at(13, 0))
        self.engine.apply_capital_pressure(now=self.at(18, 0))
        fresh = TradingEngine(self.cfg, Storage(self.storage.db_path, tz=_JST))
        again = fresh.apply_capital_pressure(now=self.at(18, 30))
        self.assertFalse(again.applied)
        self.assertEqual(len(self.pressure_flows()), 1)

    def test_catch_up_charges_all_missed_slots(self) -> None:
        self.engine.apply_capital_pressure(now=self.at(13, 0))
        result = self.engine.apply_capital_pressure(now=self.at(13, 0, day=13))
        self.assertTrue(result.applied)
        # 12:00 之后的槽位：18 / 00 / 06 / 12
        self.assertEqual(len(result.slots), 4)
        self.assertEqual(result.charged_jpy, Decimal("12000"))
        self.assertEqual(self.engine.cash_balance(), _INITIAL - 12000)

    def test_catch_up_disabled_charges_only_latest(self) -> None:
        cfg = dataclasses.replace(
            self.cfg,
            pressure=dataclasses.replace(self.cfg.pressure, catch_up_missed_slots=False),
        )
        engine = TradingEngine(cfg, Storage(self.root / "n.sqlite3", tz=_JST))
        engine.storage.initialize_account(_INITIAL, opened_at=self.at(8))
        engine.apply_capital_pressure(now=self.at(13, 0))
        result = engine.apply_capital_pressure(now=self.at(13, 0, day=13))
        self.assertEqual(len(result.slots), 1)
        self.assertEqual(result.charged_jpy, Decimal("3000"))
        self.assertEqual(engine.cash_balance(), _INITIAL - 3000)

    def test_disabled_does_nothing(self) -> None:
        cfg = dataclasses.replace(
            self.cfg, pressure=dataclasses.replace(self.cfg.pressure, enabled=False)
        )
        engine = TradingEngine(cfg, Storage(self.root / "off.sqlite3", tz=_JST))
        engine.storage.initialize_account(_INITIAL, opened_at=self.at(8))
        result = engine.apply_capital_pressure(now=self.at(13, 0))
        self.assertFalse(result.applied)
        self.assertFalse(result.enabled)
        self.assertIn("已关闭", result.skipped_reason)
        result = engine.apply_capital_pressure(now=self.at(13, 0, day=14))
        self.assertEqual(engine.cash_balance(), _INITIAL)
        self.assertEqual(engine.next_capital_pressure_at(), None)

    def test_zero_deduction_does_nothing(self) -> None:
        cfg = dataclasses.replace(
            self.cfg,
            pressure=dataclasses.replace(self.cfg.pressure, deduction_jpy=Decimal("0")),
        )
        engine = TradingEngine(cfg, Storage(self.root / "zero.sqlite3", tz=_JST))
        engine.storage.initialize_account(_INITIAL, opened_at=self.at(8))
        engine.apply_capital_pressure(now=self.at(13, 0))
        result = engine.apply_capital_pressure(now=self.at(18, 0))
        self.assertFalse(result.applied)
        self.assertIn("0", result.skipped_reason)

    def test_never_drives_cash_negative(self) -> None:
        self.storage.record_cash_flow(
            flow_type=FlowType.WITHDRAWAL, amount=Decimal("-48500"), event_key="w1"
        )
        self.engine.apply_capital_pressure(now=self.at(13, 0))
        result = self.engine.apply_capital_pressure(now=self.at(18, 0))
        self.assertEqual(result.charged_jpy, Decimal("1500"))
        self.assertEqual(self.engine.cash_balance(), Decimal("0"))
        self.assertGreaterEqual(self.engine.cash_balance(), Decimal("0"))

    def test_stops_when_cash_exhausted(self) -> None:
        self.storage.record_cash_flow(
            flow_type=FlowType.WITHDRAWAL, amount=Decimal("-50000"), event_key="w2"
        )
        self.engine.apply_capital_pressure(now=self.at(13, 0))
        result = self.engine.apply_capital_pressure(now=self.at(18, 0))
        self.assertFalse(result.applied)
        self.assertIn("无可扣金额", result.skipped_reason)

    def test_dry_run_has_no_side_effects(self) -> None:
        """dry-run 预演不得改变任何状态（余额、流水、起算点）。"""
        self.engine.apply_capital_pressure(now=self.at(13, 0))   # 真实立起算点
        before = self.engine.cash_balance()
        preview = self.engine.apply_capital_pressure(now=self.at(18, 0), dry_run=True)
        self.assertFalse(preview.applied)
        self.assertIn("dry-run", preview.skipped_reason)
        self.assertEqual(preview.charged_jpy, Decimal("3000"), "预演应算出该扣多少")
        self.assertEqual(list(preview.slots), ["2026-10-12T18"])
        self.assertEqual(self.engine.cash_balance(), before)
        self.assertEqual(self.pressure_flows(), [])

    def test_dry_run_before_first_use_does_not_create_anchor(self) -> None:
        """机制从未真正运行过时，dry-run 也不能把起算点写进去。"""
        preview = self.engine.apply_capital_pressure(now=self.at(13, 0), dry_run=True)
        self.assertFalse(preview.applied)
        self.assertIsNone(self.storage.get_meta(CAPITAL_PRESSURE_ANCHOR_META_KEY))
        self.assertEqual(
            preview.next_slot_at, datetime(2026, 10, 12, 18, 0, tzinfo=_JST)
        )

    def test_next_charge_time_is_predictable(self) -> None:
        self.engine.apply_capital_pressure(now=self.at(13, 0))
        self.assertEqual(
            self.engine.next_capital_pressure_at(self.at(13, 30)),
            datetime(2026, 10, 12, 18, 0, tzinfo=_JST),
        )

    def test_event_key_prefix(self) -> None:
        self.engine.apply_capital_pressure(now=self.at(13, 0))
        self.engine.apply_capital_pressure(now=self.at(18, 0))
        flow = self.pressure_flows()[0]
        self.assertTrue(flow.event_key.startswith(CAPITAL_DECAY_EVENT_PREFIX))
        self.assertEqual(flow.amount, Decimal("-3000"))
        self.assertIn("督促机制", flow.note)


# --------------------------------------------------------------------------- #
# 3. 账本与审计
# --------------------------------------------------------------------------- #


class TestLedger(PressureTestCase):
    def drain(self) -> None:
        self.engine.apply_capital_pressure(now=self.at(13, 0))
        self.engine.apply_capital_pressure(now=self.at(13, 0, day=13))

    def test_ledger_stays_consistent(self) -> None:
        self.drain()
        self.assertEqual(self.engine.cash_balance(), _INITIAL - 12000)
        self.assertTrue(self.engine.reconcile_account(strict=False).ok)

    def test_audit_chain_intact(self) -> None:
        self.drain()
        self.assertTrue(self.storage.verify_audit_chain().ok)

    def test_flow_type_recorded(self) -> None:
        self.drain()
        types = {f.flow_type for f in self.pressure_flows()}
        self.assertEqual(types, {FlowType.CAPITAL_DECAY.value})

    def test_total_helpers(self) -> None:
        self.engine.apply_capital_pressure(now=self.at(13, 0))
        self.engine.apply_capital_pressure(now=self.at(18, 0))
        self.assertEqual(self.engine.capital_pressure_total(), Decimal("3000"))
        self.assertEqual(
            self.engine.capital_pressure_charged_since(self.at(17)), Decimal("3000")
        )
        self.assertEqual(
            self.engine.capital_pressure_charged_since(self.at(19)), Decimal("0")
        )


# --------------------------------------------------------------------------- #
# 4. 与当日回撤的交互（最关键）
# --------------------------------------------------------------------------- #


class TestDrawdownInteraction(PressureTestCase):
    def charge_once(self) -> None:
        self.engine.apply_capital_pressure(now=self.at(13, 0))
        self.engine.apply_capital_pressure(now=self.at(18, 0), quote=make_quote())

    def test_deduction_alone_does_not_halt(self) -> None:
        """3000 是本金的 6%，若计入回撤会越过 3% 阈值 → 必须不触发。"""
        self.baseline()
        self.charge_once()
        mark = self.engine.mark_to_market(make_quote(), occurred_at=self.at(18, 5))
        self.assertEqual(mark.equity, _INITIAL - 3000, "净值必须真的少了 3000")
        self.assertEqual(mark.daily_drawdown_ratio, Decimal("0"), "回撤口径应剔除扣款")
        self.assertFalse(mark.halted)
        self.assertEqual(mark.status, "ACTIVE")

    def test_real_loss_still_halts(self) -> None:
        """真实交易亏损必须照常触发 3% 回撤停止。"""
        self.baseline()
        self.storage.record_cash_flow(
            flow_type=FlowType.REALIZED_PNL, amount=Decimal("-1500"), event_key="loss"
        )
        mark = self.engine.mark_to_market(make_quote(), occurred_at=self.at(12, 30))
        self.assertEqual(mark.daily_drawdown_ratio, Decimal("0.03"))
        self.assertTrue(mark.halted)

    def test_deduction_plus_small_loss_still_allows_trading(self) -> None:
        self.baseline()
        self.storage.record_cash_flow(
            flow_type=FlowType.REALIZED_PNL, amount=Decimal("-1200"), event_key="loss"
        )
        self.charge_once()
        mark = self.engine.mark_to_market(make_quote(), occurred_at=self.at(18, 5))
        self.assertEqual(mark.daily_drawdown_ratio, Decimal("0.024"))
        self.assertFalse(mark.halted)

    def test_drawdown_basis_helper(self) -> None:
        self.charge_once()
        equity = _INITIAL - 3000
        self.assertEqual(
            self.engine.drawdown_basis_equity(equity, self.at(19)), _INITIAL
        )

    def test_bankruptcy_still_reachable_by_real_loss(self) -> None:
        """扣款不会掩盖真实爆仓：真实亏损把净值打到 0 仍必须爆仓。"""
        self.baseline()
        self.storage.record_cash_flow(
            flow_type=FlowType.REALIZED_PNL, amount=Decimal("-50000"), event_key="wipe"
        )
        mark = self.engine.mark_to_market(make_quote(), occurred_at=self.at(12, 30))
        self.assertEqual(mark.equity, Decimal("0"))
        self.assertEqual(mark.status, "BANKRUPT")
        self.assertTrue(mark.halted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
