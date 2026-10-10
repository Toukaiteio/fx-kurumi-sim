"""engine 模块的自动化测试。

覆盖：规则 1–10 的价格/盈亏/保证金/风险/单仓约束、账本规则（不重复扣点差与滑点、
费用只扣一次）、幂等性、自动平仓原因、账户核对，以及「调用方不能指定余额或盈亏」。

所有测试都在临时目录里建库，**不连接券商、不下单、不做真实资金转账**，也不访问网络。

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
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import load_config  # noqa: E402
from engine import (  # noqa: E402
    MIN_REWARD_RISK_RATIO,
    CloseReason,
    EngineError,
    PositionNotFoundError,
    QuoteUnusableError,
    TradeIntent,
    TradeRejectedError,
    TradeStatus,
    TradingEngine,
    calculate_equity,
    calculate_margin_level,
    calculate_required_margin,
    calculate_unrealized_pnl,
    calculate_used_margin,
)
from market_data import Granularity, Quote, QuoteType  # noqa: E402
from storage import (  # noqa: E402
    AccountStatus,
    FlowType,
    PositionSide,
    PositionStatus,
    Storage,
)

_JST = timezone(timedelta(hours=9))
_INITIAL = Decimal("50000")


def make_quote(
    bid: str,
    ask: str,
    *,
    symbol: str = "USD/JPY",
    stale: bool = False,
    timestamp: datetime | None = None,
) -> Quote:
    bid_d, ask_d = Decimal(bid), Decimal(ask)
    return Quote(
        symbol=symbol,
        bid=bid_d,
        ask=ask_d,
        mid=(bid_d + ask_d) / 2,
        timestamp=timestamp or datetime.now(_JST),
        source="test_source",
        source_url="https://example.invalid/test",
        quote_type=QuoteType.BID_ASK,
        granularity=Granularity.TICK,
        is_stale=stale,
        age_seconds=Decimal("999") if stale else Decimal("0"),
    )


class EngineTestCase(unittest.TestCase):
    """公共夹具：临时数据库 + 已发放 50000 日元本金 + 现货点差 0.01。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "data" / "fxsim.sqlite3"
        self.cfg = load_config()
        self.storage = Storage(self.db_path, tz=_JST)
        self.engine = TradingEngine(self.cfg, self.storage)
        self.engine.ensure_initialized()

    # -- 便捷构造 ---------------------------------------------------------- #

    def intent(
        self,
        side=PositionSide.LONG,
        *,
        volume: str = "1000",
        stop: str | None = None,
        target: str | None = None,
        intent_id: str | None = "intent-1",
        symbol: str = "USD/JPY",
    ) -> TradeIntent:
        if stop is None:
            stop = "157.500" if side is PositionSide.LONG else "158.510"
        if target is None:
            target = "158.775" if side is PositionSide.LONG else "157.235"
        return TradeIntent(
            symbol=symbol, side=side, volume_usd=Decimal(volume),
            stop_loss=Decimal(stop), take_profit=Decimal(target), intent_id=intent_id,
        )

    def entry_quote(self) -> Quote:
        return make_quote("158.000", "158.010")

    def open_long(self, **kwargs):
        return self.engine.execute_virtual_trade(self.intent(PositionSide.LONG, **kwargs), self.entry_quote())

    def open_short(self, **kwargs):
        return self.engine.execute_virtual_trade(self.intent(PositionSide.SHORT, **kwargs), self.entry_quote())


# --------------------------------------------------------------------------- #
# 1. 纯计算函数
# --------------------------------------------------------------------------- #


class TestPureCalculations(EngineTestCase):
    def test_required_margin_is_notional_over_leverage(self) -> None:
        # 规则 5：1000 美元 @158.010 → 名义 158010 → 保证金 /25
        margin = calculate_required_margin(Decimal("1000"), Decimal("158.010"), self.cfg)
        self.assertEqual(margin, Decimal("6320.400"))
        self.assertEqual(
            calculate_required_margin(Decimal("2000"), Decimal("158.000"), self.cfg),
            Decimal("12640.000"),
        )

    def test_required_margin_rejects_non_positive(self) -> None:
        with self.assertRaises(EngineError):
            calculate_required_margin(Decimal("0"), Decimal("158"), self.cfg)
        with self.assertRaises(EngineError):
            calculate_required_margin(Decimal("1000"), Decimal("0"), self.cfg)

    def test_equity_and_margin_level(self) -> None:
        self.assertEqual(calculate_equity(Decimal("50000"), Decimal("490")), Decimal("50490.000"))
        self.assertEqual(calculate_margin_level(Decimal("50490"), Decimal("6320.4")),
                         Decimal("50490") / Decimal("6320.4"))
        self.assertIsNone(calculate_margin_level(Decimal("50000"), Decimal("0")))
        with self.assertRaises(EngineError):
            calculate_margin_level(Decimal("50000"), Decimal("-1"))

    def test_used_margin_sums_open_positions_only(self) -> None:
        long_exec = self.open_long()
        self.assertEqual(calculate_used_margin(self.engine.open_positions()), Decimal("6320.400"))
        self.engine.close_position(long_exec.position.position_id, make_quote("158.500", "158.510"))
        self.assertEqual(calculate_used_margin(self.engine.open_positions()), Decimal("0"))

    def test_unrealized_pnl_long_formula(self) -> None:
        """规则 3：多仓 =（当前 Bid − 开仓 Ask）× 数量。"""
        execution = self.open_long()
        position = execution.position
        self.assertEqual(position.entry_price, Decimal("158.010"))  # 开多走 Ask
        pnl = calculate_unrealized_pnl(position, make_quote("158.500", "158.510"))
        self.assertEqual(pnl, Decimal("490.000"))  # (158.500 - 158.010) * 1000
        # 价格不动时，浮亏恰好等于点差成本
        self.assertEqual(
            calculate_unrealized_pnl(position, make_quote("158.000", "158.010")),
            Decimal("-10.000"),
        )

    def test_unrealized_pnl_short_formula(self) -> None:
        """规则 4：空仓 =（开仓 Bid − 当前 Ask）× 数量。"""
        execution = self.open_short()
        position = execution.position
        self.assertEqual(position.entry_price, Decimal("158.000"))  # 开空走 Bid
        pnl = calculate_unrealized_pnl(position, make_quote("157.500", "157.510"))
        self.assertEqual(pnl, Decimal("490.000"))  # (158.000 - 157.510) * 1000
        self.assertEqual(
            calculate_unrealized_pnl(position, make_quote("158.000", "158.010")),
            Decimal("-10.000"),
        )

    def test_engine_methods_delegate_to_pure_functions(self) -> None:
        execution = self.open_long()
        quote = make_quote("158.500", "158.510")
        self.assertEqual(
            self.engine.calculate_unrealized_pnl(execution.position, quote),
            calculate_unrealized_pnl(execution.position, quote),
        )
        self.assertEqual(
            self.engine.calculate_required_margin(Decimal("1000"), Decimal("158")),
            calculate_required_margin(Decimal("1000"), Decimal("158"), self.cfg),
        )


# --------------------------------------------------------------------------- #
# 2. 价格与成交（规则 1–4）
# --------------------------------------------------------------------------- #


class TestFillPrices(EngineTestCase):
    def test_long_opens_at_ask_closes_at_bid(self) -> None:
        execution = self.open_long()
        self.assertEqual(execution.position.entry_price, Decimal("158.010"))
        self.assertEqual(execution.trade.price, Decimal("158.010"))
        closed = self.engine.close_position(
            execution.position.position_id, make_quote("158.500", "158.510")
        )
        self.assertEqual(closed.trade.price, Decimal("158.500"))  # 平多走 Bid
        self.assertEqual(closed.realized_pnl_jpy, Decimal("490.000"))

    def test_short_opens_at_bid_closes_at_ask(self) -> None:
        execution = self.open_short()
        self.assertEqual(execution.position.entry_price, Decimal("158.000"))
        closed = self.engine.close_position(
            execution.position.position_id, make_quote("157.500", "157.510")
        )
        self.assertEqual(closed.trade.price, Decimal("157.510"))  # 平空走 Ask
        self.assertEqual(closed.realized_pnl_jpy, Decimal("490.000"))

    def test_realized_equals_unrealized_at_close(self) -> None:
        """已实现盈亏与平仓前的浮动盈亏必须一致（同一公式，无口径漂移）。"""
        execution = self.open_long()
        quote = make_quote("158.700", "158.710")
        before = self.engine.mark_to_market(quote)
        closed = self.engine.close_position(execution.position.position_id, quote)
        self.assertEqual(closed.realized_pnl_jpy, before.unrealized_pnl)

    def test_trigger_uses_fill_side_price_not_mid(self) -> None:
        """止损判定用平仓侧价格：多仓看 Bid，空仓看 Ask。"""
        long_exec = self.open_long(stop="157.500", target="160.000")
        # Bid 未触及止损但 Ask 已跌破 → 多仓不应触发
        self.assertIsNone(
            self.engine.evaluate_exit_triggers(long_exec.position, make_quote("157.600", "157.500"))
        )
        self.assertIsNotNone(
            self.engine.evaluate_exit_triggers(long_exec.position, make_quote("157.400", "157.410"))
        )


# --------------------------------------------------------------------------- #
# 3. 校验（规则 5–9）
# --------------------------------------------------------------------------- #


class TestValidation(EngineTestCase):
    def validate(self, intent, quote=None):
        return self.engine.validate_trade_intent(intent, quote or self.entry_quote())

    def test_valid_long_intent_passes(self) -> None:
        result = self.validate(self.intent())
        self.assertTrue(result.ok, msg=str(result.problems))
        self.assertEqual(result.entry_price, Decimal("158.010"))
        self.assertEqual(result.notional_jpy, Decimal("158010.000"))
        self.assertEqual(result.required_margin_jpy, Decimal("6320.400"))
        self.assertEqual(result.planned_risk_jpy, Decimal("515.000"))  # (0.51+0.005)*1000
        self.assertEqual(result.reward_risk_ratio, Decimal("1.5"))
        self.assertEqual(result.risk_limit_jpy, Decimal("1000.000"))  # 2% × 50000
        self.assertEqual(result.spread_cost_jpy, Decimal("10.000"))

    def test_valid_short_intent_passes(self) -> None:
        result = self.validate(self.intent(PositionSide.SHORT))
        self.assertTrue(result.ok, msg=str(result.problems))
        self.assertEqual(result.entry_price, Decimal("158.000"))
        self.assertEqual(result.planned_risk_jpy, Decimal("515.000"))

    def test_missing_stop_or_target_rejected(self) -> None:
        result = self.validate(
            TradeIntent(symbol="USD/JPY", side=PositionSide.LONG,
                        volume_usd=Decimal("1000"), stop_loss=None, take_profit=None)
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("必须设置止损价" in p for p in result.problems))
        self.assertTrue(any("必须设置止盈价" in p for p in result.problems))

    def test_reward_risk_below_minimum_rejected(self) -> None:
        # 止损 0.51，止盈只有 0.51 → 盈亏比 1.0 < 1.5
        result = self.validate(self.intent(stop="157.500", target="158.520"))
        self.assertFalse(result.ok)
        self.assertTrue(any("计划盈亏比" in p for p in result.problems))

    def test_reward_risk_exactly_minimum_passes(self) -> None:
        result = self.validate(self.intent(stop="157.500", target="158.775"))
        self.assertTrue(result.ok, msg=str(result.problems))
        self.assertEqual(result.reward_risk_ratio, Decimal("1.5"))

    def test_stop_and_target_on_wrong_side_rejected(self) -> None:
        long_result = self.validate(self.intent(stop="158.500", target="157.000"))
        self.assertFalse(long_result.ok)
        self.assertTrue(any("多仓止损价" in p for p in long_result.problems))
        self.assertTrue(any("多仓止盈价" in p for p in long_result.problems))
        short_result = self.validate(
            self.intent(PositionSide.SHORT, stop="157.500", target="158.500")
        )
        self.assertFalse(short_result.ok)
        self.assertTrue(any("空仓止损价" in p for p in short_result.problems))
        self.assertTrue(any("空仓止盈价" in p for p in short_result.problems))

    def test_volume_must_be_multiple_of_min_unit(self) -> None:
        result = self.validate(self.intent(volume="1500"))
        self.assertFalse(result.ok)
        self.assertTrue(any("正整数倍" in p for p in result.problems))

    def test_volume_multiple_passes(self) -> None:
        # 2000 美元：止损距离 0.495 + 滑点 0.005 = 0.5/单位 → 风险恰好 1000（上限）
        result = self.validate(self.intent(volume="2000", stop="157.515", target="158.760"))
        self.assertTrue(result.ok, msg=str(result.problems))
        self.assertEqual(result.planned_risk_jpy, Decimal("1000.000"))
        self.assertEqual(result.required_margin_jpy, Decimal("12640.800"))

    def test_wrong_symbol_rejected(self) -> None:
        result = self.validate(self.intent(symbol="EUR/JPY"))
        self.assertFalse(result.ok)
        self.assertTrue(any("品种" in p for p in result.problems))

    def test_stale_quote_rejected_by_default(self) -> None:
        result = self.validate(self.intent(), make_quote("158.000", "158.010", stale=True))
        self.assertFalse(result.ok)
        self.assertTrue(any("过期" in p for p in result.problems))

    def test_stale_quote_allowed_when_configured(self) -> None:
        engine = TradingEngine(self.cfg, self.storage, allow_stale_quote=True)
        result = engine.validate_trade_intent(
            self.intent(), make_quote("158.000", "158.010", stale=True)
        )
        self.assertTrue(result.ok, msg=str(result.problems))

    def test_invalid_quote_rejected(self) -> None:
        """报价非法/品种不符时，开仓被拒绝并说明原因。"""
        with self.assertRaises(TradeRejectedError) as err:
            self.engine.execute_virtual_trade(self.intent(), make_quote("159.000", "158.000"))
        self.assertTrue(any("报价非法" in p for p in err.exception.validation.problems))
        with self.assertRaises(TradeRejectedError) as err2:
            self.engine.execute_virtual_trade(
                self.intent(), make_quote("158.000", "158.010", symbol="EUR/JPY")
            )
        self.assertTrue(any("品种" in p for p in err2.exception.validation.problems))

    def test_close_refuses_unusable_quote_when_strict(self) -> None:
        """``allow_stale_quote=False`` 时平仓同样检查报价，抛出 QuoteUnusableError。"""
        execution = self.open_long()
        with self.assertRaises(QuoteUnusableError):
            self.engine.close_position(
                execution.position.position_id,
                make_quote("158.500", "158.510", stale=True),
                allow_stale_quote=False,
            )
        # 默认允许过期报价，止损/强平在行情停滞时仍能执行
        closed = self.engine.close_position(
            execution.position.position_id, make_quote("158.500", "158.510", stale=True)
        )
        self.assertEqual(closed.reason, CloseReason.MANUAL.value)

    def test_risk_per_trade_limit_enforced(self) -> None:
        # 放大到 2000 美元，止损距离 1.0 → 风险 2010 > 2% × 50000
        result = self.validate(self.intent(volume="2000", stop="157.000", target="159.000"))
        self.assertFalse(result.ok)
        self.assertTrue(any("单笔计划止损风险" in p for p in result.problems))

    def test_slippage_is_included_in_planned_risk(self) -> None:
        """规则 7：止损风险必须包含预期不利滑点。"""
        result = self.validate(self.intent(stop="157.500", target="158.775"))
        self.assertEqual(result.slippage_buffer_jpy, self.cfg.market.slippage_jpy)
        self.assertEqual(result.stop_distance_jpy, Decimal("0.510"))
        self.assertEqual(result.risk_per_unit_jpy, Decimal("0.515"))
        # 若不含滑点，风险会是 510 而不是 515
        self.assertEqual(result.planned_risk_jpy, Decimal("515.000"))

    def test_margin_usage_limit_enforced(self) -> None:
        # 5000 美元 @158.01 → 名义 790050 → 保证金 31602，超过净值 60%
        result = self.validate(self.intent(volume="5000", stop="157.500", target="158.775"))
        self.assertFalse(result.ok)
        self.assertTrue(any("已用保证金" in p for p in result.problems))
        self.assertEqual(result.required_margin_jpy, Decimal("31602.000"))
        self.assertGreater(result.margin_usage_ratio_after, self.cfg.risk.max_margin_usage_ratio)

    def test_rule_8_second_position_rejected(self) -> None:
        self.open_long()
        result = self.validate(self.intent(PositionSide.LONG, intent_id="second"))
        self.assertFalse(result.ok)
        self.assertTrue(any("禁止加仓" in p for p in result.problems))

    def test_rule_8_opposite_direction_rejected(self) -> None:
        self.open_long()
        result = self.validate(self.intent(PositionSide.SHORT, intent_id="hedge"))
        self.assertFalse(result.ok)
        self.assertTrue(any("禁止加仓" in p for p in result.problems))

    def test_rule_8_allows_new_position_after_close(self) -> None:
        execution = self.open_long()
        self.engine.close_position(execution.position.position_id, make_quote("158.500", "158.510"))
        result = self.validate(self.intent(PositionSide.SHORT, intent_id="next"))
        self.assertTrue(result.ok, msg=str(result.problems))

    def test_validation_collects_multiple_problems(self) -> None:
        result = self.validate(
            TradeIntent(symbol="EUR/JPY", side=PositionSide.LONG,
                        volume_usd=Decimal("1500"), stop_loss=Decimal("158.5"),
                        take_profit=Decimal("157.0"))
        )
        self.assertFalse(result.ok)
        self.assertGreaterEqual(len(result.problems), 4)


# --------------------------------------------------------------------------- #
# 4. 执行与拒绝（规则 9）
# --------------------------------------------------------------------------- #


class TestExecution(EngineTestCase):
    def test_rejected_intent_raises_with_details(self) -> None:
        with self.assertRaises(TradeRejectedError) as err:
            self.engine.execute_virtual_trade(
                self.intent(stop="157.500", target="158.520"), self.entry_quote()
            )
        self.assertFalse(err.exception.validation.ok)
        self.assertTrue(any("计划盈亏比" in p for p in err.exception.validation.problems))

    def test_rejected_intent_writes_no_position_or_trade(self) -> None:
        with self.assertRaises(TradeRejectedError):
            self.engine.execute_virtual_trade(
                self.intent(volume="1500"), self.entry_quote()
            )
        self.assertEqual(self.storage.counts()["positions"], 0)
        self.assertEqual(self.storage.counts()["trades"], 0)
        self.assertEqual(self.engine.cash_balance(), _INITIAL)
        # 拒绝事件必须留痕
        types = [e.event_type for e in self.storage.read_audit_events()]
        self.assertIn("TRADE_REJECTED", types)

    def test_open_does_not_change_cash_balance(self) -> None:
        """开仓只是冻结保证金，不产生现金流。"""
        self.open_long()
        self.assertEqual(self.engine.cash_balance(), _INITIAL)
        self.assertEqual(self.storage.read_account_state().margin_used, Decimal("6320.400"))
        self.assertEqual(
            self.storage.counts()["cash_flows"], 1  # 只有初始本金
        )

    def test_open_sets_margin_and_equity_projection(self) -> None:
        self.open_long()
        state = self.storage.read_account_state()
        self.assertEqual(state.margin_used, Decimal("6320.400"))
        self.assertEqual(state.cash_balance, _INITIAL)
        # 开仓后引擎立即按开仓报价盯市：浮亏恰好等于点差成本
        self.assertEqual(state.unrealized_pnl, Decimal("-10.000"))
        self.assertEqual(state.equity, Decimal("49990.000"))
        mark = self.engine.mark_to_market(self.entry_quote())
        self.assertEqual(mark.unrealized_pnl, Decimal("-10.000"))
        self.assertEqual(mark.equity, Decimal("49990.000"))
        self.assertEqual(mark.margin_level, Decimal("49990.000") / Decimal("6320.400"))

    def test_execution_records_audit_trail(self) -> None:
        execution = self.open_long()
        types = [e.event_type for e in self.storage.read_audit_events()]
        self.assertIn("TRADE_EXECUTED", types)
        self.assertIn("POSITION_OPENED", types)
        event = [e for e in self.storage.read_audit_events() if e.event_type == "TRADE_EXECUTED"][-1]
        self.assertEqual(event.payload["position_id"], execution.position.position_id)
        self.assertEqual(event.payload["entry_price"], "158.010")
        self.assertEqual(event.payload["margin_jpy"], "6320.400")
        self.assertEqual(event.payload["reward_risk_ratio"], "1.5")

    def test_position_row_carries_stop_target_and_risk(self) -> None:
        position = self.open_long().position
        self.assertEqual(position.stop_loss, Decimal("157.500"))
        self.assertEqual(position.take_profit, Decimal("158.775"))
        self.assertEqual(position.planned_risk_jpy, Decimal("515.000"))
        self.assertEqual(position.margin_jpy, Decimal("6320.400"))

    def test_trade_row_carries_quoted_price_and_spread(self) -> None:
        trade = self.open_long().trade
        self.assertEqual(trade.quoted_price, Decimal("158.005"))  # mid
        self.assertEqual(trade.spread_jpy, Decimal("0.010"))
        self.assertEqual(trade.slippage_jpy, Decimal("0.005"))
        self.assertEqual(trade.action, "OPEN")


# --------------------------------------------------------------------------- #
# 5. 幂等性
# --------------------------------------------------------------------------- #

class TestIdempotency(EngineTestCase):
    def test_same_intent_id_does_not_open_twice(self) -> None:
        first = self.open_long(intent_id="dup-1")
        second = self.open_long(intent_id="dup-1")
        self.assertEqual(second.status, TradeStatus.DUPLICATE.value)
        self.assertTrue(second.is_duplicate)
        self.assertEqual(first.position.position_id, second.position.position_id)
        self.assertEqual(self.storage.counts()["positions"], 1)
        self.assertEqual(self.storage.counts()["trades"], 1)
        self.assertEqual(self.engine.cash_balance(), _INITIAL)

    def test_different_intent_ids_second_is_rejected_by_rule_8(self) -> None:
        self.open_long(intent_id="a")
        with self.assertRaises(TradeRejectedError):
            self.open_long(intent_id="b")
        self.assertEqual(self.storage.counts()["positions"], 1)

    def test_intent_without_id_is_not_deduped(self) -> None:
        self.open_long(intent_id=None)
        # 规则 8 拦住第二次，而不是静默去重
        with self.assertRaises(TradeRejectedError):
            self.open_long(intent_id=None)

    def test_duplicate_close_is_idempotent(self) -> None:
        execution = self.open_long()
        quote = make_quote("158.500", "158.510")
        first = self.engine.close_position(execution.position.position_id, quote, intent_id="c1")
        second = self.engine.close_position(execution.position.position_id, quote, intent_id="c1")
        self.assertEqual(second.status, TradeStatus.DUPLICATE.value)
        self.assertEqual(first.trade.trade_no, second.trade.trade_no)
        self.assertEqual(self.storage.counts()["trades"], 2)  # OPEN + CLOSE
        self.assertEqual(self.engine.cash_balance(), Decimal("50490.000"))
        self.assertEqual(
            self.storage.counts()["cash_flows"], 2  # 本金 + 已实现盈亏
        )

    def test_duplicate_open_does_not_duplicate_audit_or_margin(self) -> None:
        self.open_long(intent_id="dup-2")
        before_events = self.storage.counts()["audit_events"]
        before_margin = self.storage.read_account_state().margin_used
        self.open_long(intent_id="dup-2")
        self.assertEqual(self.storage.counts()["audit_events"], before_events)
        self.assertEqual(self.storage.read_account_state().margin_used, before_margin)


# --------------------------------------------------------------------------- #
# 6. 平仓原因与自动触发（规则 10）
# --------------------------------------------------------------------------- #


class TestCloseReasons(EngineTestCase):
    def test_manual_close_records_manual_reason(self) -> None:
        execution = self.open_long()
        closed = self.engine.close_position(
            execution.position.position_id, make_quote("158.300", "158.310"),
            reason=CloseReason.MANUAL,
        )
        self.assertEqual(closed.reason, CloseReason.MANUAL.value)
        self.assertEqual(closed.position.close_reason, "MANUAL")
        self.assertEqual(closed.trade.reason, "MANUAL")
        self.assertFalse(CloseReason.MANUAL.is_automatic)

    def test_stop_loss_trigger_closes_with_stop_reason(self) -> None:
        execution = self.open_long(stop="157.500", target="160.000")
        result = self.engine.process_quote(make_quote("157.400", "157.410"))
        self.assertTrue(result.did_close)
        self.assertEqual(result.triggers[0].reason, CloseReason.STOP_LOSS)
        self.assertEqual(result.closed[0].reason, CloseReason.STOP_LOSS.value)
        self.assertEqual(result.closed[0].position.close_reason, "STOP_LOSS")
        self.assertEqual(result.closed[0].trade.price, Decimal("157.400"))  # 平多走 Bid
        self.assertLess(result.closed[0].realized_pnl_jpy, 0)

    def test_take_profit_trigger_closes_with_target_reason(self) -> None:
        execution = self.open_long(stop="157.500", target="158.775")
        result = self.engine.process_quote(make_quote("158.800", "158.810"))
        self.assertTrue(result.did_close)
        self.assertEqual(result.triggers[0].reason, CloseReason.TAKE_PROFIT)
        self.assertEqual(result.closed[0].position.close_reason, "TAKE_PROFIT")
        self.assertGreater(result.closed[0].realized_pnl_jpy, 0)

    def test_short_stop_loss_triggers_on_ask(self) -> None:
        execution = self.open_short(stop="158.510", target="157.000")
        result = self.engine.process_quote(make_quote("158.600", "158.610"))
        self.assertTrue(result.did_close)
        self.assertEqual(result.triggers[0].reason, CloseReason.STOP_LOSS)
        self.assertEqual(result.closed[0].trade.price, Decimal("158.610"))  # 平空走 Ask

    def test_short_take_profit_triggers_on_ask(self) -> None:
        execution = self.open_short(stop="158.510", target="157.235")
        result = self.engine.process_quote(make_quote("157.200", "157.210"))
        self.assertTrue(result.did_close)
        self.assertEqual(result.triggers[0].reason, CloseReason.TAKE_PROFIT)
        self.assertGreater(result.closed[0].realized_pnl_jpy, 0)

    def test_no_trigger_keeps_position_open(self) -> None:
        execution = self.open_long(stop="157.500", target="160.000")
        result = self.engine.process_quote(make_quote("158.200", "158.210"))
        self.assertFalse(result.did_close)
        self.assertEqual(self.storage.read_position(execution.position.position_id).status, "OPEN")

    def test_margin_call_forces_close(self) -> None:
        """保证金水平跌破强平线 → 强制平仓，原因为 MARGIN_CALL。"""
        self.open_long(stop="157.500", target="160.000")
        # 浮亏 43679.6 → 净值 6320.4 = 已用保证金 → 保证金水平 1.0（触及强平线）
        with mock.patch(
            "engine.calculate_unrealized_pnl",
            return_value=Decimal("-43679.600"),
        ):
            result = self.engine.process_quote(make_quote("158.000", "158.010"))
        self.assertTrue(result.did_close)
        self.assertEqual(result.closed[0].reason, CloseReason.MARGIN_CALL.value)
        self.assertTrue(CloseReason.MARGIN_CALL.is_forced)

    def test_bankruptcy_forces_close(self) -> None:
        self.open_long(stop="157.500", target="160.000")
        with mock.patch(
            "engine.calculate_unrealized_pnl",
            return_value=Decimal("-60000"),
        ):
            result = self.engine.process_quote(make_quote("158.000", "158.010"))
        self.assertTrue(result.did_close)
        self.assertEqual(result.closed[0].reason, CloseReason.BANKRUPTCY.value)
        self.assertEqual(
            self.storage.read_account_state().status, AccountStatus.BANKRUPT.value
        )

    def test_daily_drawdown_halts_and_closes(self) -> None:
        """当日回撤达到 3% → 平仓并停止当日新开仓。"""
        self.open_long(stop="157.500", target="160.000")
        # 回撤 3% → 净值 48500，浮亏 -1500（保证金水平仍远高于强平线）
        with mock.patch(
            "engine.calculate_unrealized_pnl",
            return_value=Decimal("-1500"),
        ):
            result = self.engine.process_quote(make_quote("158.000", "158.010"))
        self.assertTrue(result.did_close)
        self.assertEqual(result.closed[0].reason, CloseReason.DAILY_DRAWDOWN.value)
        mark = self.engine.mark_to_market(make_quote("158.000", "158.010"))
        self.assertTrue(mark.halted)
        self.assertIn("回撤", mark.halt_reason or "")
        # 停止后禁止新开仓
        result = self.engine.validate_trade_intent(self.intent(), make_quote("158.000", "158.010"))
        self.assertFalse(result.ok)
        self.assertTrue(any("已停止开仓" in p or "回撤" in p for p in result.problems))

    def test_close_records_distinct_reasons_per_path(self) -> None:
        """手动与自动触发必须各自留痕（规则 10）。"""
        manual = self.open_long(stop="157.500", target="160.000", intent_id="m")
        self.engine.close_position(manual.position.position_id, make_quote("158.300", "158.310"))
        auto = self.open_long(stop="157.500", target="158.775", intent_id="a")
        self.engine.process_quote(make_quote("158.900", "158.910"))
        reasons = [t.reason for t in self.storage.query_trades(action="CLOSE")]
        self.assertEqual(reasons, ["MANUAL", "TAKE_PROFIT"])

    def test_close_nonexistent_position(self) -> None:
        with self.assertRaises(PositionNotFoundError):
            self.engine.close_position(999999, make_quote("158.000", "158.010"))


# --------------------------------------------------------------------------- #
# 7. 账本规则
# --------------------------------------------------------------------------- #


class TestLedgerRules(EngineTestCase):
    def test_spread_not_deducted_twice(self) -> None:
        """点差已体现在成交价里，余额不再二次扣点差。"""
        execution = self.open_long()
        self.engine.close_position(execution.position.position_id, make_quote("158.010", "158.020"))
        # 开仓 Ask 158.010，平仓 Bid 158.010 → 盈亏恰为 0，余额回到本金
        self.assertEqual(self.engine.cash_balance(), _INITIAL)

    def test_slippage_not_deducted_from_balance(self) -> None:
        """滑点只作为风险缓冲与审计信息，不从余额扣一次。"""
        execution = self.open_long()
        self.engine.close_position(execution.position.position_id, make_quote("158.510", "158.520"))
        # 盈亏 (158.510-158.010)*1000 = 500，与滑点 0.005 无关
        self.assertEqual(self.engine.cash_balance(), _INITIAL + Decimal("500"))
        flows = self.storage.read_cash_flows()
        self.assertEqual([f.flow_type for f in flows], ["DEPOSIT", "REALIZED_PNL"])
        # 没有任何独立的点差/滑点流水
        self.assertNotIn("SPREAD", {f.flow_type for f in flows})
        self.assertNotIn("SLIPPAGE", {f.flow_type for f in flows})

    def test_cash_equals_initial_plus_pnl(self) -> None:
        long_exec = self.open_long()
        self.engine.close_position(long_exec.position.position_id, make_quote("158.500", "158.510"))
        short_exec = self.open_short(intent_id="s2")
        self.engine.close_position(short_exec.position.position_id, make_quote("157.500", "157.510"))
        # 50000 + 490 + 490
        self.assertEqual(self.engine.cash_balance(), Decimal("50980.000"))
        report = self.engine.reconcile_account()
        self.assertTrue(report.ok, msg=str(report.problems))
        self.assertEqual(report.cash_from_ledger_rule, report.ledger_balance)

    def test_standalone_fee_deducted_once(self) -> None:
        self.engine.record_standalone_fee(Decimal("120"), fee_id="comm-1")
        self.engine.record_standalone_fee(Decimal("120"), fee_id="comm-1")  # 重复提交
        self.assertEqual(self.engine.cash_balance(), _INITIAL - Decimal("120"))
        self.assertEqual(len(self.storage.read_cash_flows(flow_type=FlowType.FEE)), 1)

    def test_standalone_fee_normalizes_sign(self) -> None:
        self.engine.record_standalone_fee(Decimal("-50"), fee_id="c2")
        self.assertEqual(self.engine.cash_balance(), _INITIAL - Decimal("50"))

    def test_swap_fee_requires_verification(self) -> None:
        with self.assertRaises(EngineError) as err:
            self.engine.record_swap_fee(Decimal("30"), swap_id="sw-1", verified=False)
        self.assertIn("核验", str(err.exception))
        self.assertEqual(self.engine.cash_balance(), _INITIAL)

    def test_swap_fee_deducted_once(self) -> None:
        self.engine.record_swap_fee(Decimal("-30"), swap_id="sw-2", verified=True)
        self.engine.record_swap_fee(Decimal("-30"), swap_id="sw-2", verified=True)
        self.assertEqual(self.engine.cash_balance(), _INITIAL - Decimal("30"))

    def test_close_fee_charged_once(self) -> None:
        execution = self.open_long()
        self.engine.close_position(
            execution.position.position_id, make_quote("158.500", "158.510"),
            fee_jpy=Decimal("50"),
        )
        # 50000 + 490 - 50
        self.assertEqual(self.engine.cash_balance(), Decimal("50440.000"))
        self.assertEqual(len(self.storage.read_cash_flows(flow_type=FlowType.FEE)), 1)

    def test_zero_pnl_close_adds_no_flow(self) -> None:
        execution = self.open_long()
        self.engine.close_position(execution.position.position_id, make_quote("158.010", "158.020"))
        self.assertEqual(len(self.storage.read_cash_flows(flow_type=FlowType.REALIZED_PNL)), 0)
        self.assertEqual(self.engine.cash_balance(), _INITIAL)


# --------------------------------------------------------------------------- #
# 8. 调用方不能指定余额或盈亏
# --------------------------------------------------------------------------- #


class TestNoCallerSuppliedMoney(EngineTestCase):
    def test_trade_intent_has_no_money_fields(self) -> None:
        import dataclasses

        names = {f.name for f in dataclasses.fields(TradeIntent)}
        for forbidden in ("balance", "equity", "pnl", "realized_pnl", "price", "margin"):
            self.assertNotIn(forbidden, names)

    def test_intent_rejects_unknown_kwargs(self) -> None:
        with self.assertRaises(TypeError):
            TradeIntent(
                symbol="USD/JPY", side=PositionSide.LONG, volume_usd=Decimal("1000"),
                stop_loss=Decimal("157"), take_profit=Decimal("159"),
                balance=Decimal("999999"),  # type: ignore[call-arg]
            )

    def test_executed_values_come_from_formulas(self) -> None:
        execution = self.open_long()
        v = execution.validation
        assert v is not None
        # 成交价 = 报价 Ask，不是调用方能传的东西
        self.assertEqual(execution.position.entry_price, self.entry_quote().ask)
        # 保证金 = 名义/25
        self.assertEqual(
            execution.position.margin_jpy,
            (execution.position.volume * execution.position.entry_price) / self.cfg.risk.max_leverage,
        )
        # 计划风险 = (止损距离 + 滑点) × 数量
        expected_risk = (
            abs(execution.position.entry_price - execution.position.stop_loss)
            + self.cfg.market.slippage_jpy
        ) * execution.position.volume
        self.assertEqual(execution.position.planned_risk_jpy, expected_risk)

    def test_ai_cannot_set_final_pnl(self) -> None:
        """即使传入离谱的止盈价，盈亏仍由平仓报价决定。"""
        execution = self.open_long(stop="157.500", target="999.000")
        closed = self.engine.close_position(
            execution.position.position_id, make_quote("158.010", "158.020")
        )
        self.assertEqual(closed.realized_pnl_jpy, Decimal("0.000"))
        self.assertEqual(self.engine.cash_balance(), _INITIAL)

    def test_snapshot_is_computed_not_stored(self) -> None:
        self.open_long()
        snapshot = self.engine.snapshot(make_quote("158.500", "158.510"))
        self.assertEqual(snapshot["cash_balance"], "50000")
        self.assertEqual(snapshot["unrealized_pnl"], "490.000")
        self.assertEqual(snapshot["equity"], "50490.000")
        self.assertEqual(snapshot["used_margin"], "6320.400")
        self.assertEqual(len(snapshot["open_positions"]), 1)


# --------------------------------------------------------------------------- #
# 9. 核对与投影修复
# --------------------------------------------------------------------------- #


class TestReconciliation(EngineTestCase):
    def test_reconcile_ok_after_round_trip(self) -> None:
        execution = self.open_long()
        self.engine.close_position(execution.position.position_id, make_quote("158.500", "158.510"))
        report = self.engine.reconcile_account()
        self.assertTrue(report.ok, msg=str(report.problems))
        self.assertEqual(report.ledger_balance, Decimal("50490.000"))
        self.assertEqual(report.cash_from_ledger_rule, Decimal("50490.000"))
        self.assertEqual(report.cash_difference, Decimal("0"))
        self.assertEqual(report.margin_difference, Decimal("0"))
        self.assertEqual(report.equity_difference, Decimal("0"))
        self.assertEqual(report.open_position_count, 0)

    def test_reconcile_ok_with_open_position_and_quote(self) -> None:
        self.open_long()
        quote = make_quote("158.500", "158.510")
        self.engine.mark_to_market(quote)
        report = self.engine.reconcile_account(quote=quote)
        self.assertTrue(report.ok, msg=str(report.problems))
        self.assertEqual(report.open_position_count, 1)
        self.assertEqual(report.margin_used_from_positions, Decimal("6320.400"))

    def test_reconcile_detects_margin_projection_drift(self) -> None:
        """模拟「持仓已建但保证金投影未更新」的跨事务中断。"""
        self.open_long()
        self.storage.set_margin_used(Decimal("0"))
        report = self.engine.reconcile_account()
        self.assertFalse(report.ok)
        self.assertEqual(report.margin_used_stored, Decimal("0"))
        self.assertEqual(report.margin_used_from_positions, Decimal("6320.400"))
        self.assertTrue(any("已用保证金投影不一致" in p for p in report.problems))

    def test_repair_projections_fixes_drift(self) -> None:
        self.open_long()
        self.storage.set_margin_used(Decimal("0"))
        repaired = self.engine.repair_projections(make_quote("158.000", "158.010"))
        self.assertEqual(repaired.margin_used, Decimal("6320.400"))
        self.assertEqual(repaired.cash_balance, _INITIAL)  # 现金不受修复影响
        self.assertTrue(self.engine.reconcile_account().ok)
        types = [e.event_type for e in self.storage.read_audit_events()]
        self.assertIn("PROJECTION_REPAIRED", types)

    def test_reconcile_detects_duplicate_open_positions(self) -> None:
        """绕过引擎直接建两笔持仓（模拟外部写库）应被核对发现。"""
        self.open_long()
        self.storage.open_position(
            symbol="USD/JPY", side=PositionSide.SHORT, volume=Decimal("1000"),
            entry_price=Decimal("158.000"), margin_jpy=Decimal("6320"),
        )
        report = self.engine.reconcile_account()
        self.assertFalse(report.ok)
        self.assertTrue(any("未平仓持仓" in p for p in report.problems))
        self.assertTrue(any("对冲" in p for p in report.problems))

    def test_reconcile_strict_raises(self) -> None:
        self.open_long()
        self.storage.set_margin_used(Decimal("0"))
        with self.assertRaises(EngineError):
            self.engine.reconcile_account(strict=True)

    def test_reconcile_detects_ledger_sign_problem(self) -> None:
        """把一条 FEE 流水写成正值（符号写反）应被账本口径检查发现。"""
        self.engine.record_standalone_fee(Decimal("100"), fee_id="f-sign")
        flows = self.storage.read_cash_flows(flow_type=FlowType.FEE)
        self.assertEqual(flows[0].amount, Decimal("-100"))
        # 直接篡改成正值，模拟写反符号
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE cash_flows SET amount='100' WHERE flow_no=?", (flows[0].flow_no,))
        conn.commit()
        conn.close()
        report = self.engine.reconcile_account()
        self.assertFalse(report.ok)
        self.assertTrue(any("账本口径不一致" in p for p in report.problems))


# --------------------------------------------------------------------------- #
# 10. 报告序列化与 CLI
# --------------------------------------------------------------------------- #


class TestSerializationAndCli(unittest.TestCase):
    def test_reports_are_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config()
            engine = TradingEngine.from_config(cfg, db_path=str(Path(tmp) / "db.sqlite3"))
            engine.ensure_initialized()
            quote = make_quote("158.000", "158.010")
            intent = TradeIntent(
                symbol="USD/JPY", side=PositionSide.LONG, volume_usd=Decimal("1000"),
                stop_loss=Decimal("157.500"), take_profit=Decimal("158.775"),
                intent_id="json-1",
            )
            execution = engine.execute_virtual_trade(intent, quote)
            mark = engine.mark_to_market(quote)
            report = engine.reconcile_account(quote=quote)
            for payload in (execution.as_dict(), mark.as_dict(), report.as_dict()):
                json.loads(json.dumps(payload, ensure_ascii=False))
            self.assertIn("validation", execution.as_dict())
            self.assertIn("entry_price", execution.as_dict()["validation"])

    def test_cli_demo_runs(self) -> None:
        from engine import _main

        self.assertEqual(_main([]), 0)

    def test_min_reward_risk_ratio_constant(self) -> None:
        self.assertEqual(MIN_REWARD_RISK_RATIO, Decimal("1.5"))

    def test_min_reward_risk_ratio_can_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config()
            engine = TradingEngine.from_config(
                cfg, db_path=str(Path(tmp) / "db.sqlite3"), min_reward_risk_ratio=Decimal("1.0")
            )
            engine.ensure_initialized()
            intent = TradeIntent(
                symbol="USD/JPY", side=PositionSide.LONG, volume_usd=Decimal("1000"),
                stop_loss=Decimal("157.500"), take_profit=Decimal("158.520"),
            )
            result = engine.validate_trade_intent(intent, make_quote("158.000", "158.010"))
            self.assertTrue(result.ok, msg=str(result.problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
