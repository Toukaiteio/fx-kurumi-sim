"""storage 模块的自动化测试。

覆盖用户明确要求的场景：重复初始化、事务回滚、重启恢复、余额核对、
审计日志完整性；以及 Decimal 精度、schema 守卫、幂等、并发、备份等。

所有测试都在临时目录里建库，**不触碰真实账户、不执行任何真实交易**，
也不访问网络。

运行方式（项目根目录 E:\\pj2\\fx-kurumi-sim）::

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import storage as st  # noqa: E402
from storage import (  # noqa: E402
    GENESIS_HASH,
    INITIAL_CAPITAL_EVENT_KEY,
    SCHEMA_VERSION,
    AccountAlreadyInitializedError,
    AccountState,
    AccountStatus,
    ConcurrencyError,
    DuplicateEventError,
    FlowType,
    LedgerMismatchError,
    PositionSide,
    PositionStatus,
    ReconciliationError,
    RunStatus,
    SchemaVersionError,
    Storage,
    StorageError,
    TradeAction,
)

_JST = timezone(timedelta(hours=9))
_INITIAL = Decimal("50000")


class StorageTestCase(unittest.TestCase):
    """公共夹具：临时目录 + 已初始化的 50000 日元账户。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "data" / "fxsim.sqlite3"
        self.storage = Storage(self.db_path, tz=_JST)
        self.storage.initialize_account(_INITIAL)

    # -- 工具 -------------------------------------------------------------- #

    def raw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def raw_execute(self, sql: str, params: tuple = ()) -> None:
        conn = self.raw()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def raw_query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = self.raw()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def count(self, table: str, where: str = "", params: tuple = ()) -> int:
        sql = f"SELECT COUNT(*) AS n FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(self.raw_query(sql, params)[0]["n"])

    def open_long(self, **overrides):
        kwargs = {
            "symbol": "USD/JPY",
            "side": PositionSide.LONG,
            "volume": Decimal("1000"),
            "entry_price": Decimal("158.000"),
            "margin_jpy": Decimal("6320"),
            "stop_loss": Decimal("157.000"),
            "take_profit": Decimal("160.000"),
            "planned_risk_jpy": Decimal("1000"),
        }
        kwargs.update(overrides)
        return self.storage.open_position(**kwargs)


# --------------------------------------------------------------------------- #
# 1. 重复初始化
# --------------------------------------------------------------------------- #


class TestInitialization(StorageTestCase):
    def test_initial_state(self) -> None:
        state = self.storage.read_account_state()
        self.assertEqual(state.cash_balance, _INITIAL)
        self.assertEqual(state.equity, _INITIAL)
        self.assertEqual(state.peak_equity, _INITIAL)
        self.assertEqual(state.day_start_equity, _INITIAL)
        self.assertEqual(state.status, AccountStatus.ACTIVE.value)
        self.assertEqual(state.revision, 2)  # 建表 1 次 + 本金投影 1 次
        self.assertEqual(state.margin_used, Decimal("0"))
        self.assertIsNone(state.margin_level)

    def test_initial_capital_granted_exactly_once(self) -> None:
        self.assertEqual(self.count("cash_flows"), 1)
        flow = self.storage.read_cash_flows()[0]
        self.assertEqual(flow.flow_type, FlowType.DEPOSIT.value)
        self.assertEqual(flow.amount, _INITIAL)
        self.assertEqual(flow.balance_after, _INITIAL)

    def test_repeated_initialization_is_idempotent(self) -> None:
        before = self.storage.read_account_state()
        for _ in range(3):
            state = self.storage.initialize_account(_INITIAL)
        after = self.storage.read_account_state()
        # 不重复发放本金，也不产生新流水
        self.assertEqual(self.count("cash_flows"), 1)
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)
        self.assertEqual(after.cash_balance, _INITIAL)
        # 幂等调用不再写库，revision 不变
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(state.cash_balance, _INITIAL)

    def test_reinitialization_with_different_capital_is_refused(self) -> None:
        with self.assertRaises(AccountAlreadyInitializedError) as err:
            self.storage.initialize_account(Decimal("999999"))
        self.assertIn("不会重复发放", str(err.exception))
        # 本金与账本保持原样
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)
        self.assertEqual(self.storage.read_account_state().cash_balance, _INITIAL)
        self.assertEqual(self.count("cash_flows"), 1)
        row = self.raw_query("SELECT initial_capital FROM account")[0]
        self.assertEqual(Decimal(row["initial_capital"]), _INITIAL)

    def test_reinitialization_with_different_currency_is_refused(self) -> None:
        with self.assertRaises(AccountAlreadyInitializedError):
            self.storage.initialize_account(_INITIAL, currency="USD")
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)

    def test_reopen_existing_db_does_not_reset(self) -> None:
        self.storage.record_cash_flow(
            flow_type=FlowType.WITHDRAWAL, amount=Decimal("-1000"), event_key="w1"
        )
        reopened = Storage(self.db_path, tz=_JST)
        self.assertTrue(reopened.is_initialized())
        self.assertEqual(reopened.ledger_balance(), Decimal("49000"))
        # 重新初始化不得再次发放本金
        reopened.initialize_account(_INITIAL)
        self.assertEqual(reopened.ledger_balance(), Decimal("49000"))
        self.assertEqual(self.count("cash_flows"), 2)

    def test_negative_capital_rejected(self) -> None:
        path = self.root / "other" / "db.sqlite3"
        fresh = Storage(path, tz=_JST)
        with self.assertRaises(StorageError):
            fresh.initialize_account(Decimal("-1"))

    def test_operations_before_initialization_are_refused(self) -> None:
        path = self.root / "uninit" / "db.sqlite3"
        fresh = Storage(path, tz=_JST)
        self.assertFalse(fresh.is_initialized())
        with self.assertRaises(StorageError) as err:
            fresh.open_position(
                symbol="USD/JPY", side=PositionSide.LONG, volume=Decimal("1000"),
                entry_price=Decimal("158"), margin_jpy=Decimal("6000"),
            )
        self.assertIn("尚未初始化", str(err.exception))
        with self.assertRaises(StorageError):
            fresh.read_account_state()


# --------------------------------------------------------------------------- #
# 2. 事务回滚
# --------------------------------------------------------------------------- #


class TestTransactionRollback(StorageTestCase):
    def test_open_position_rolls_back_when_audit_fails(self) -> None:
        """开仓过程中审计写入失败 → 持仓与成交都不得残留。"""
        with mock.patch.object(
            Storage, "_insert_audit", side_effect=StorageError("模拟审计写入失败")
        ):
            with self.assertRaises(StorageError):
                self.open_long()
        self.assertEqual(self.count("positions"), 0)
        self.assertEqual(self.count("trades"), 0)
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)

    def test_close_position_rolls_back_completely(self) -> None:
        """平仓过程中审计失败 → 持仓仍为 OPEN，无平仓成交，余额不变。"""
        position = self.open_long()
        trades_before = self.count("trades")
        flows_before = self.count("cash_flows")
        revision_before = self.storage.read_position(position.position_id).revision

        with mock.patch.object(
            Storage, "_insert_audit", side_effect=StorageError("模拟审计写入失败")
        ):
            with self.assertRaises(StorageError):
                self.storage.close_position(
                    position.position_id,
                    exit_price=Decimal("159.000"),
                    realized_pnl_jpy=Decimal("1000"),
                )

        reloaded = self.storage.read_position(position.position_id)
        self.assertEqual(reloaded.status, PositionStatus.OPEN.value)
        self.assertIsNone(reloaded.closed_at)
        self.assertIsNone(reloaded.realized_pnl_jpy)
        self.assertEqual(reloaded.revision, revision_before)
        self.assertEqual(self.count("trades"), trades_before)
        self.assertEqual(self.count("cash_flows"), flows_before)
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)

    def test_close_position_rejects_stale_revision(self) -> None:
        position = self.open_long()
        self.storage.update_position(position.position_id, stop_loss=Decimal("157.5"))
        with self.assertRaises(ConcurrencyError):
            self.storage.close_position(
                position.position_id,
                exit_price=Decimal("159"),
                realized_pnl_jpy=Decimal("1000"),
                expected_revision=position.revision,  # 已过期
            )
        self.assertEqual(
            self.storage.read_position(position.position_id).status, PositionStatus.OPEN.value
        )
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)

    def test_duplicate_close_event_key_rolls_back(self) -> None:
        first = self.open_long()
        self.storage.close_position(
            first.position_id, exit_price=Decimal("159"), realized_pnl_jpy=Decimal("1000"),
            event_key="close-1",
        )
        second = self.open_long()
        with self.assertRaises(DuplicateEventError):
            self.storage.close_position(
                second.position_id, exit_price=Decimal("159"), realized_pnl_jpy=Decimal("1000"),
                event_key="close-1",
            )
        # 第二个持仓未被平掉，也没有多余流水
        self.assertEqual(
            self.storage.read_position(second.position_id).status, PositionStatus.OPEN.value
        )
        self.assertEqual(self.storage.ledger_balance(), _INITIAL + Decimal("1000"))

    def test_save_account_state_rejects_stale_revision(self) -> None:
        import dataclasses

        state = self.storage.read_account_state()
        bumped = self.storage.set_margin_used(Decimal("1000"))
        with self.assertRaises(ConcurrencyError):
            self.storage.save_account_state(
                dataclasses.replace(state, cash_balance=_INITIAL),
                expected_revision=state.revision,  # 已被 set_margin_used 递增，此值已过期
            )
        current = self.storage.read_account_state()
        self.assertEqual(current.revision, bumped.revision)
        self.assertEqual(current.margin_used, Decimal("1000"))

    def test_save_account_state_refuses_balance_that_contradicts_ledger(self) -> None:
        import dataclasses

        state = self.storage.read_account_state()
        with self.assertRaises(LedgerMismatchError) as err:
            self.storage.save_account_state(
                dataclasses.replace(state, cash_balance=Decimal("999999"))
            )
        self.assertIn("账本流水之和", str(err.exception))
        self.assertEqual(self.storage.read_account_state().cash_balance, _INITIAL)

    def test_save_account_state_accepts_ledger_consistent_balance(self) -> None:
        import dataclasses

        self.storage.record_cash_flow(flow_type=FlowType.DEPOSIT, amount=Decimal("5000"), event_key="d1")
        state = self.storage.read_account_state()
        saved = self.storage.save_account_state(
            dataclasses.replace(state, equity=state.cash_balance, halted=True, halt_reason="测试")
        )
        self.assertTrue(saved.halted)
        self.assertEqual(saved.cash_balance, Decimal("55000"))
        self.assertEqual(saved.revision, state.revision + 1)


# --------------------------------------------------------------------------- #
# 3. 重启恢复与幂等
# --------------------------------------------------------------------------- #


class TestRestartAndIdempotency(StorageTestCase):
    def test_data_survives_reopen(self) -> None:
        position = self.open_long()
        self.storage.close_position(
            position.position_id, exit_price=Decimal("159.000"), realized_pnl_jpy=Decimal("1000")
        )
        head_before = self.storage.audit_head()
        balance_before = self.storage.ledger_balance()

        reopened = Storage(self.db_path, tz=_JST)
        self.assertEqual(reopened.ledger_balance(), balance_before)
        self.assertEqual(reopened.audit_head(), head_before)
        self.assertTrue(reopened.verify_audit_chain().ok)
        self.assertEqual(len(reopened.read_positions(status=None)), 1)
        self.assertEqual(len(reopened.query_trades()), 2)

    def test_recover_aborted_runs_after_crash(self) -> None:
        run = self.storage.start_scheduler_run("monitor_loop")
        self.assertEqual(run.status, RunStatus.RUNNING.value)
        # 模拟进程崩溃后重启：新实例清理遗留的 RUNNING
        reopened = Storage(self.db_path, tz=_JST)
        self.assertEqual(reopened.recover_aborted_runs(), 1)
        recovered = reopened.read_scheduler_runs()[0]
        self.assertEqual(recovered.status, RunStatus.ABORTED.value)
        self.assertIsNotNone(recovered.finished_at)
        # 再次调用没有可清理的
        self.assertEqual(reopened.recover_aborted_runs(), 0)

    def test_finished_run_is_not_overwritten(self) -> None:
        run = self.storage.start_scheduler_run("t")
        self.storage.finish_scheduler_run(run.run_no, status=RunStatus.SUCCESS, detail="ok")
        again = self.storage.finish_scheduler_run(
            run.run_no, status=RunStatus.FAILED, detail="不该覆盖"
        )
        self.assertEqual(again.status, RunStatus.SUCCESS.value)
        self.assertEqual(again.detail, "ok")

    def test_scheduler_run_event_key_is_idempotent(self) -> None:
        first = self.storage.start_scheduler_run("t", event_key="2026-10-10T15:00")
        second = self.storage.start_scheduler_run("t", event_key="2026-10-10T15:00")
        self.assertEqual(first.run_no, second.run_no)
        self.assertEqual(self.count("scheduler_runs"), 1)
        with self.assertRaises(DuplicateEventError):
            self.storage.start_scheduler_run(
                "t", event_key="2026-10-10T15:00", on_duplicate="ERROR"
            )

    def test_cash_flow_event_key_is_idempotent(self) -> None:
        first = self.storage.record_cash_flow(
            flow_type=FlowType.DEPOSIT, amount=Decimal("1000"), event_key="dep-1"
        )
        second = self.storage.record_cash_flow(
            flow_type=FlowType.DEPOSIT, amount=Decimal("1000"), event_key="dep-1"
        )
        self.assertEqual(first.flow_no, second.flow_no)
        self.assertEqual(self.count("cash_flows"), 2)  # 初始本金 + 这一笔
        self.assertEqual(self.storage.ledger_balance(), Decimal("51000"))
        with self.assertRaises(DuplicateEventError):
            self.storage.record_cash_flow(
                flow_type=FlowType.DEPOSIT, amount=Decimal("1000"), event_key="dep-1",
                on_duplicate="ERROR",
            )

    def test_open_position_event_key_is_idempotent(self) -> None:
        first = self.open_long(event_key="pos-1")
        second = self.open_long(event_key="pos-1")
        self.assertEqual(first.position_id, second.position_id)
        self.assertEqual(self.count("positions"), 1)
        self.assertEqual(self.count("trades"), 1)
        with self.assertRaises(DuplicateEventError):
            self.open_long(event_key="pos-1", on_duplicate="ERROR")

    def test_record_trade_with_same_event_key_does_not_double_count_pnl(self) -> None:
        kwargs = {
            "symbol": "USD/JPY",
            "action": TradeAction.CLOSE,
            "side": PositionSide.LONG,
            "volume": Decimal("1000"),
            "price": Decimal("159.000"),
            "realized_pnl_jpy": Decimal("1000"),
            "event_key": "trade-x",
        }
        first = self.storage.record_trade(**kwargs)
        second = self.storage.record_trade(**kwargs)
        self.assertEqual(first.trade_no, second.trade_no)
        self.assertEqual(self.count("trades"), 1)
        self.assertEqual(self.count("cash_flows", "flow_type=?", (FlowType.REALIZED_PNL.value,)), 1)
        # 关键：余额只增加一次 1000，不因重复写入而翻倍
        self.assertEqual(self.storage.ledger_balance(), Decimal("51000"))
        self.assertEqual(self.storage.read_account_state().realized_pnl, Decimal("1000"))

    def test_close_position_twice_does_not_double_count(self) -> None:
        position = self.open_long()
        self.storage.close_position(
            position.position_id, exit_price=Decimal("159.000"), realized_pnl_jpy=Decimal("1000")
        )
        self.storage.close_position(
            position.position_id, exit_price=Decimal("159.000"), realized_pnl_jpy=Decimal("1000")
        )
        self.assertEqual(self.count("trades", "action=?", (TradeAction.CLOSE.value,)), 1)
        self.assertEqual(self.storage.ledger_balance(), Decimal("51000"))


# --------------------------------------------------------------------------- #
# 4. 余额核对
# --------------------------------------------------------------------------- #


class TestReconciliation(StorageTestCase):
    def test_reconcile_ok_after_normal_activity(self) -> None:
        position = self.open_long()
        self.storage.close_position(
            position.position_id,
            exit_price=Decimal("159.000"),
            realized_pnl_jpy=Decimal("1000"),
            fee_jpy=Decimal("50"),
        )
        self.storage.record_cash_flow(flow_type=FlowType.DEPOSIT, amount=Decimal("2000"), event_key="d")
        self.storage.record_cash_flow(
            flow_type=FlowType.WITHDRAWAL, amount=Decimal("-500"), event_key="w"
        )
        report = self.storage.reconcile_account()
        self.assertTrue(report.ok, msg=str(report.problems))
        self.assertEqual(report.ledger_balance, Decimal("52450"))
        self.assertEqual(report.balance_difference, Decimal("0"))
        self.assertEqual(report.pnl_difference, Decimal("0"))
        self.assertEqual(report.realized_pnl_from_trades, Decimal("1000"))
        self.assertEqual(report.flow_count, 5)
        self.assertEqual(report.trade_count, 2)
        self.assertEqual(report.open_position_count, 0)

    def test_reconcile_reports_tampered_balance(self) -> None:
        # 直接改数据库里的缓存余额（模拟被外部篡改）
        self.raw_execute("UPDATE account_state SET cash_balance='123456' WHERE account_id=1")
        report = self.storage.reconcile_account()
        self.assertFalse(report.ok)
        self.assertEqual(report.stored_balance, Decimal("123456"))
        self.assertEqual(report.ledger_balance, _INITIAL)
        self.assertEqual(report.balance_difference, Decimal("123456") - _INITIAL)
        self.assertTrue(any("现金余额不一致" in p for p in report.problems))

    def test_reconcile_strict_raises(self) -> None:
        self.raw_execute("UPDATE account_state SET cash_balance='1' WHERE account_id=1")
        with self.assertRaises(ReconciliationError):
            self.storage.reconcile_account(strict=True)

    def test_reconcile_reports_tampered_flow_amount(self) -> None:
        self.raw_execute("UPDATE cash_flows SET amount='60000' WHERE flow_no=1")
        report = self.storage.reconcile_account()
        self.assertFalse(report.ok)
        # balance_after 链被破坏（第一条的 amount 与 balance_after 不再自洽）
        self.assertTrue(any("balance_after" in p for p in report.problems))

    def test_reconcile_reports_tampered_equity(self) -> None:
        self.raw_execute("UPDATE account_state SET equity='1' WHERE account_id=1")
        report = self.storage.reconcile_account()
        self.assertFalse(report.ok)
        self.assertTrue(any("净值不一致" in p for p in report.problems))

    def test_reconcile_reports_pnl_mismatch(self) -> None:
        position = self.open_long()
        self.storage.close_position(
            position.position_id, exit_price=Decimal("159"), realized_pnl_jpy=Decimal("1000")
        )
        # 篡改成交里的盈亏金额，使其与流水不一致
        self.raw_execute("UPDATE trades SET realized_pnl_jpy='9999' WHERE action='CLOSE'")
        report = self.storage.reconcile_account()
        self.assertFalse(report.ok)
        self.assertTrue(any("已实现盈亏不一致" in p for p in report.problems))

    def test_reconcile_reports_open_position_margin(self) -> None:
        self.open_long(margin_jpy=Decimal("6320"))
        self.open_long(margin_jpy=Decimal("6320"))
        report = self.storage.reconcile_account()
        self.assertTrue(report.ok, msg=str(report.problems))
        self.assertEqual(report.open_position_count, 2)
        self.assertEqual(report.open_position_margin, Decimal("12640"))

    def test_verify_ledger_detects_orphan_pnl_flow(self) -> None:
        self.storage.record_cash_flow(
            flow_type=FlowType.REALIZED_PNL, amount=Decimal("100"), event_key="orphan"
        )
        verification = self.storage.verify_ledger()
        self.assertFalse(verification.ok)
        self.assertTrue(any("未关联成交记录" in p for p in verification.problems))

    def test_ledger_is_the_authority_for_balance(self) -> None:
        # 每一笔流水之后，缓存余额都必须等于账本之和
        for index in range(5):
            self.storage.record_cash_flow(
                flow_type=FlowType.DEPOSIT, amount=Decimal("100"), event_key=f"e{index}"
            )
            self.assertEqual(
                self.storage.read_account_state().cash_balance,
                self.storage.ledger_balance(),
            )
        self.assertEqual(self.storage.ledger_balance(), _INITIAL + Decimal("500"))


# --------------------------------------------------------------------------- #
# 5. 审计日志完整性
# --------------------------------------------------------------------------- #


class TestAuditChain(StorageTestCase):
    def test_chain_is_valid_after_activity(self) -> None:
        position = self.open_long()
        self.storage.close_position(
            position.position_id, exit_price=Decimal("159"), realized_pnl_jpy=Decimal("1000")
        )
        self.storage.record_cash_flow(flow_type=FlowType.DEPOSIT, amount=Decimal("1"), event_key="a")
        verification = self.storage.verify_audit_chain()
        self.assertTrue(verification.ok, msg=str(verification.problems))
        self.assertGreaterEqual(verification.checked, 4)
        self.assertEqual(verification.head_hash, self.storage.audit_head())

    def test_chain_links_to_previous_hash(self) -> None:
        events = self.storage.read_audit_events()
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0].prev_hash, GENESIS_HASH)
        for previous, current in zip(events, events[1:]):
            self.assertEqual(current.prev_hash, previous.hash)
            self.assertEqual(current.seq, previous.seq + 1)

    def test_appended_events_advance_head(self) -> None:
        head_before = self.storage.audit_head()
        self.storage.audit("CUSTOM_EVENT", {"note": "hello"})
        head_after = self.storage.audit_head()
        self.assertNotEqual(head_before, head_after)
        self.assertTrue(self.storage.verify_audit_chain().ok)

    def test_tampered_payload_is_detected(self) -> None:
        self.storage.audit("A", {"value": 1})
        self.storage.audit("B", {"value": 2})
        self.storage.audit("C", {"value": 3})
        self.raw_execute(
            "UPDATE audit_events SET payload=? WHERE seq=2", ('{"value":999}',)
        )
        verification = self.storage.verify_audit_chain()
        self.assertFalse(verification.ok)
        self.assertEqual(verification.first_bad_seq, 2)
        self.assertTrue(any("hash 校验失败" in p for p in verification.problems))

    def test_deleted_event_is_detected(self) -> None:
        self.storage.audit("A", {})
        self.storage.audit("B", {})
        self.storage.audit("C", {})
        self.raw_execute("DELETE FROM audit_events WHERE seq=2")
        verification = self.storage.verify_audit_chain()
        self.assertFalse(verification.ok)
        self.assertTrue(any("seq 不连续" in p for p in verification.problems))

    def test_broken_link_is_detected(self) -> None:
        self.storage.audit("A", {})
        self.storage.audit("B", {})
        self.raw_execute("UPDATE audit_events SET prev_hash=? WHERE seq=2", (GENESIS_HASH,))
        verification = self.storage.verify_audit_chain()
        self.assertFalse(verification.ok)
        self.assertTrue(
            any("链断裂" in p or "hash 校验失败" in p for p in verification.problems)
        )

    def test_non_canonical_payload_is_detected(self) -> None:
        self.storage.audit("A", {"b": 1, "a": 2})
        # 改成非规范形式（键未排序、含空格），语义相同但形式不同
        self.raw_execute(
            "UPDATE audit_events SET payload=? WHERE seq=1", ('{"b": 1, "a": 2}',)
        )
        verification = self.storage.verify_audit_chain()
        self.assertFalse(verification.ok)
        self.assertTrue(any("规范形式" in p for p in verification.problems))

    def test_audit_payload_contains_decimals_as_strings(self) -> None:
        self.storage.record_cash_flow(
            flow_type=FlowType.DEPOSIT, amount=Decimal("0.1"), event_key="tiny"
        )
        events = self.storage.read_audit_events(event_type="CASH_FLOW_RECORDED")
        self.assertEqual(events[-1].payload["amount"], "0.1")
        self.assertEqual(events[-1].payload["balance_after"], "50000.1")


# --------------------------------------------------------------------------- #
# 6. Decimal 精度与 schema 守卫
# --------------------------------------------------------------------------- #


class TestDecimalAndSchema(StorageTestCase):
    def test_no_real_columns_anywhere(self) -> None:
        tables = [
            row["name"]
            for row in self.raw_query(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        self.assertGreaterEqual(len(tables), 10)
        offenders: list[str] = []
        for table in tables:
            for column in self.raw_query(f"PRAGMA table_info({table})"):
                declared = str(column["type"]).upper()
                if any(token in declared for token in ("REAL", "FLOAT", "DOUBLE", "NUMERIC")):
                    offenders.append(f"{table}.{column['name']} ({declared})")
        self.assertEqual(offenders, [], msg=f"存在浮点列: {offenders}")

    def test_decimal_round_trip_is_exact(self) -> None:
        tricky = Decimal("0.1") + Decimal("0.2")  # 浮点下会是 0.30000000000000004
        self.storage.record_cash_flow(
            flow_type=FlowType.DEPOSIT, amount=tricky, event_key="exact"
        )
        self.assertEqual(self.storage.ledger_balance(), Decimal("50000.3"))
        row = self.raw_query("SELECT amount, balance_after FROM cash_flows WHERE event_key='exact'")[0]
        self.assertEqual(row["amount"], "0.3")
        self.assertEqual(row["balance_after"], "50000.3")
        self.assertEqual(Decimal(row["balance_after"]), Decimal("50000.3"))

    def test_long_decimal_price_preserved(self) -> None:
        price = Decimal("158.24688447")
        position = self.open_long(entry_price=price)
        self.assertEqual(self.storage.read_position(position.position_id).entry_price, price)
        self.assertEqual(self.storage.query_trades()[0].price, price)
        row = self.raw_query("SELECT price FROM trades")[0]
        self.assertEqual(row["price"], "158.24688447")

    def test_no_scientific_notation_in_storage(self) -> None:
        self.storage.record_cash_flow(
            flow_type=FlowType.DEPOSIT, amount=Decimal("1E+2"), event_key="sci"
        )
        row = self.raw_query("SELECT amount FROM cash_flows WHERE event_key='sci'")[0]
        self.assertEqual(row["amount"], "100")

    def test_foreign_database_is_refused(self) -> None:
        path = self.root / "foreign" / "db.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE someone_elses_table (id INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(SchemaVersionError) as err:
            Storage(path, tz=_JST)
        self.assertIn("不是本模块创建的数据库", str(err.exception))

    def test_schema_version_mismatch_is_refused(self) -> None:
        self.raw_execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        with self.assertRaises(SchemaVersionError) as err:
            Storage(self.db_path, tz=_JST)
        self.assertIn("不会就地改写", str(err.exception))

    def test_schema_version_recorded(self) -> None:
        row = self.raw_query("SELECT value FROM meta WHERE key='schema_version'")[0]
        self.assertEqual(int(row["value"]), SCHEMA_VERSION)

    def test_required_tables_exist(self) -> None:
        expected = {
            "meta", "account", "account_state", "positions", "trades", "cash_flows",
            "quotes", "audit_events", "daily_equity_baseline", "scheduler_runs",
        }
        found = {
            row["name"]
            for row in self.raw_query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertTrue(expected.issubset(found), msg=f"缺少表: {expected - found}")


# --------------------------------------------------------------------------- #
# 7. 持仓与交易
# --------------------------------------------------------------------------- #


class TestPositionsAndTrades(StorageTestCase):
    def test_open_position_writes_position_trade_and_audit(self) -> None:
        position = self.open_long()
        self.assertEqual(position.status, PositionStatus.OPEN.value)
        self.assertTrue(position.is_open)
        self.assertEqual(position.revision, 1)
        self.assertEqual(position.side, PositionSide.LONG.value)
        self.assertEqual(position.entry_price, Decimal("158.000"))
        self.assertEqual(self.count("trades"), 1)
        trade = self.storage.query_trades()[0]
        self.assertEqual(trade.action, TradeAction.OPEN.value)
        self.assertEqual(trade.position_id, position.position_id)
        self.assertIsNone(trade.realized_pnl_jpy)
        self.assertIn("POSITION_OPENED", [e.event_type for e in self.storage.read_audit_events()])

    def test_close_position_updates_everything_atomically(self) -> None:
        position = self.open_long()
        closed, trade = self.storage.close_position(
            position.position_id,
            exit_price=Decimal("159.000"),
            realized_pnl_jpy=Decimal("1000"),
            fee_jpy=Decimal("50"),
            reason="take_profit",
        )
        self.assertEqual(closed.status, PositionStatus.CLOSED.value)
        self.assertFalse(closed.is_open)
        self.assertEqual(closed.realized_pnl_jpy, Decimal("1000"))
        self.assertEqual(closed.close_reason, "take_profit")
        self.assertEqual(closed.revision, 2)
        self.assertEqual(trade.action, TradeAction.CLOSE.value)
        self.assertEqual(trade.realized_pnl_jpy, Decimal("1000"))
        # 余额 = 本金 + 盈亏 - 手续费
        self.assertEqual(self.storage.ledger_balance(), Decimal("50950"))
        self.assertEqual(self.storage.read_account_state().cash_balance, Decimal("50950"))
        self.assertEqual(self.storage.read_account_state().realized_pnl, Decimal("950"))

    def test_zero_pnl_close_writes_no_pnl_flow(self) -> None:
        position = self.open_long()
        self.storage.close_position(
            position.position_id, exit_price=Decimal("158.000"), realized_pnl_jpy=Decimal("0")
        )
        self.assertEqual(
            self.count("cash_flows", "flow_type=?", (FlowType.REALIZED_PNL.value,)), 0
        )
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)

    def test_read_positions_filters_by_status(self) -> None:
        first = self.open_long()
        second = self.open_long()
        self.storage.close_position(
            first.position_id, exit_price=Decimal("159"), realized_pnl_jpy=Decimal("1000")
        )
        open_positions = self.storage.read_positions()
        self.assertEqual([p.position_id for p in open_positions], [second.position_id])
        all_positions = self.storage.read_positions(status=None)
        self.assertEqual(len(all_positions), 2)
        closed_only = self.storage.read_positions(status=PositionStatus.CLOSED)
        self.assertEqual([p.position_id for p in closed_only], [first.position_id])

    def test_update_position_only_widens_allowed_fields(self) -> None:
        position = self.open_long()
        updated = self.storage.update_position(
            position.position_id, stop_loss=Decimal("157.500"), take_profit=Decimal("161.000")
        )
        self.assertEqual(updated.stop_loss, Decimal("157.500"))
        self.assertEqual(updated.take_profit, Decimal("161.000"))
        self.assertEqual(updated.revision, 2)
        # 未传的字段保持原值
        self.assertEqual(updated.planned_risk_jpy, Decimal("1000"))

    def test_update_closed_position_is_refused(self) -> None:
        position = self.open_long()
        self.storage.close_position(
            position.position_id, exit_price=Decimal("159"), realized_pnl_jpy=Decimal("1000")
        )
        with self.assertRaises(StorageError) as err:
            self.storage.update_position(position.position_id, stop_loss=Decimal("1"))
        self.assertIn("已平仓", str(err.exception))

    def test_query_trades_filters(self) -> None:
        position = self.open_long()
        self.storage.close_position(
            position.position_id, exit_price=Decimal("159"), realized_pnl_jpy=Decimal("1000")
        )
        self.assertEqual(len(self.storage.query_trades(action=TradeAction.OPEN)), 1)
        self.assertEqual(len(self.storage.query_trades(action=TradeAction.CLOSE)), 1)
        self.assertEqual(len(self.storage.query_trades(position_id=position.position_id)), 2)
        self.assertEqual(len(self.storage.query_trades(symbol="EUR/JPY")), 0)
        newest = self.storage.query_trades(order="DESC", limit=1)
        self.assertEqual(newest[0].action, TradeAction.CLOSE.value)

    def test_invalid_position_inputs_rejected(self) -> None:
        with self.assertRaises(StorageError):
            self.open_long(volume=Decimal("0"))
        with self.assertRaises(StorageError):
            self.open_long(entry_price=Decimal("-1"))
        with self.assertRaises(StorageError):
            self.open_long(margin_jpy=Decimal("0"))
        with self.assertRaises(StorageError):
            self.open_long(side="SIDEWAYS")
        with self.assertRaises(StorageError):
            self.storage.close_position(
                999999, exit_price=Decimal("159"), realized_pnl_jpy=Decimal("0")
            )

    def test_set_margin_used(self) -> None:
        self.open_long()
        state = self.storage.set_margin_used(Decimal("6320"))
        self.assertEqual(state.margin_used, Decimal("6320"))
        self.assertEqual(state.free_margin, Decimal("43680"))
        self.assertEqual(state.margin_level, _INITIAL / Decimal("6320"))
        with self.assertRaises(StorageError):
            self.storage.set_margin_used(Decimal("-1"))


# --------------------------------------------------------------------------- #
# 8. 报价 / 每日基准 / 备份
# --------------------------------------------------------------------------- #


class TestQuotesBaselineBackup(StorageTestCase):
    def _quote(self):
        from market_data import Granularity, Quote, QuoteType

        return Quote(
            symbol="USD/JPY",
            bid=Decimal("158.2410"),
            ask=Decimal("158.2510"),
            mid=Decimal("158.2460"),
            timestamp=datetime(2026, 10, 9, 8, 0, tzinfo=_JST),
            source="yahoo_finance_chart",
            source_url="https://example.invalid/chart",
            quote_type=QuoteType.SYNTHETIC_SPREAD,
            granularity=Granularity.MINUTE,
            is_stale=True,
        )

    def test_record_quote_preserves_decimals(self) -> None:
        record = self.storage.record_quote(self._quote())
        self.assertEqual(record.bid, Decimal("158.2410"))
        self.assertEqual(record.mid, Decimal("158.2460"))
        self.assertEqual(record.quote_type, "synthetic_spread")
        self.assertEqual(record.granularity, "minute")
        self.assertTrue(record.is_stale)
        row = self.raw_query("SELECT bid FROM quotes")[0]
        self.assertEqual(row["bid"], "158.2410")
        self.assertEqual(len(self.storage.read_quotes(symbol="USD/JPY")), 1)

    def test_record_quote_rejects_bid_above_ask(self) -> None:
        import dataclasses

        bad = dataclasses.replace(self._quote(), bid=Decimal("159"), ask=Decimal("158"))
        with self.assertRaises(StorageError):
            self.storage.record_quote(bad)

    def test_quote_event_key_is_idempotent(self) -> None:
        first = self.storage.record_quote(self._quote(), event_key="q1")
        second = self.storage.record_quote(self._quote(), event_key="q1")
        self.assertEqual(first.quote_no, second.quote_no)
        self.assertEqual(self.count("quotes"), 1)

    def test_daily_baseline_created_once(self) -> None:
        first = self.storage.ensure_daily_baseline(
            "2026-10-10", equity=_INITIAL, balance=_INITIAL
        )
        # 当日基准不会被后续调用覆盖
        second = self.storage.ensure_daily_baseline(
            "2026-10-10", equity=Decimal("1"), balance=Decimal("1")
        )
        self.assertEqual(first.created_at, second.created_at)
        self.assertEqual(second.day_start_equity, _INITIAL)
        self.assertEqual(self.count("daily_equity_baseline"), 1)

    def test_daily_baseline_extremes_only_widen(self) -> None:
        self.storage.ensure_daily_baseline("2026-10-10", equity=_INITIAL, balance=_INITIAL)
        self.storage.update_daily_baseline("2026-10-10", equity=Decimal("52000"))
        updated = self.storage.update_daily_baseline("2026-10-10", equity=Decimal("49000"))
        self.assertEqual(updated.peak_equity, Decimal("52000"))
        self.assertEqual(updated.low_equity, Decimal("49000"))
        # 更小的峰值 / 更大的低点不会覆盖极值
        again = self.storage.update_daily_baseline("2026-10-10", equity=Decimal("50000"))
        self.assertEqual(again.peak_equity, Decimal("52000"))
        self.assertEqual(again.low_equity, Decimal("49000"))

    def test_daily_baseline_trigger_flag(self) -> None:
        self.storage.ensure_daily_baseline("2026-10-10", equity=_INITIAL, balance=_INITIAL)
        triggered = self.storage.update_daily_baseline(
            "2026-10-10", drawdown_stop_triggered=True, triggered_at=datetime.now(_JST)
        )
        self.assertTrue(triggered.drawdown_stop_triggered)
        self.assertIsNotNone(triggered.triggered_at)

    def test_update_missing_baseline_is_refused(self) -> None:
        with self.assertRaises(StorageError):
            self.storage.update_daily_baseline("2020-01-01", equity=Decimal("1"))

    def test_backup_is_verified_and_consistent(self) -> None:
        self.storage.record_cash_flow(
            flow_type=FlowType.DEPOSIT, amount=Decimal("1234.56"), event_key="bk"
        )
        target = self.storage.backup(self.root / "backups" / "manual.sqlite3")
        self.assertTrue(target.is_file())
        backup_storage = Storage(target, tz=_JST)
        self.assertEqual(backup_storage.ledger_balance(), self.storage.ledger_balance())
        self.assertTrue(backup_storage.verify_audit_chain().ok)
        self.assertTrue(backup_storage.reconcile_account().ok)

    def test_default_backup_location(self) -> None:
        target = self.storage.backup()
        self.assertEqual(target.parent.name, "backups")
        self.assertTrue(target.is_file())

    def test_corrupted_backup_is_detected(self) -> None:
        target = self.storage.backup(self.root / "backups" / "b.sqlite3", verify=False)
        conn = sqlite3.connect(target)
        conn.execute("UPDATE cash_flows SET amount='1' WHERE flow_no=1")
        conn.commit()
        conn.close()
        with self.assertRaises(StorageError) as err:
            self.storage._verify_backup(target)
        self.assertIn("账本余额", str(err.exception))


# --------------------------------------------------------------------------- #
# 9. 整体完整性与并发
# --------------------------------------------------------------------------- #


class TestIntegrityAndConcurrency(StorageTestCase):
    def test_verify_integrity_ok(self) -> None:
        position = self.open_long()
        self.storage.close_position(
            position.position_id, exit_price=Decimal("159"), realized_pnl_jpy=Decimal("1000")
        )
        report = self.storage.verify_integrity()
        self.assertTrue(report.ok, msg=str(report.problems))
        self.assertEqual(report.sqlite_integrity, "ok")
        self.assertEqual(report.foreign_key_violations, ())
        self.assertEqual(report.schema_version, SCHEMA_VERSION)

    def test_verify_integrity_reports_audit_tampering(self) -> None:
        self.storage.audit("A", {"v": 1})
        self.storage.audit("B", {"v": 2})
        self.raw_execute("UPDATE audit_events SET payload='{\"v\":99}' WHERE seq=1")
        report = self.storage.verify_integrity()
        self.assertFalse(report.ok)
        self.assertTrue(any("审计链" in p for p in report.problems))

    def test_counts(self) -> None:
        self.open_long()
        counts = self.storage.counts()
        self.assertEqual(counts["positions"], 1)
        self.assertEqual(counts["positions_open"], 1)
        self.assertEqual(counts["trades"], 1)
        self.assertEqual(counts["cash_flows"], 1)
        self.assertGreaterEqual(counts["audit_events"], 2)

    def test_concurrent_cash_flows_are_all_recorded(self) -> None:
        """多线程并发写入：WAL + busy_timeout + BEGIN IMMEDIATE 下不丢不重。"""
        storage = Storage(self.db_path, tz=_JST, timeout=15.0)
        threads = 4
        per_thread = 5
        errors: list[BaseException] = []
        barrier = threading.Barrier(threads)

        def worker(worker_id: int) -> None:
            try:
                barrier.wait(timeout=10)
                for index in range(per_thread):
                    storage.record_cash_flow(
                        flow_type=FlowType.DEPOSIT,
                        amount=Decimal("10"),
                        event_key=f"w{worker_id}-{index}",
                    )
            except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append(exc)

        workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join(timeout=30)

        self.assertEqual(errors, [], msg=f"并发写入出现异常: {errors}")
        expected = threads * per_thread
        self.assertEqual(
            self.count("cash_flows", "flow_type=?", (FlowType.DEPOSIT.value,)), expected + 1
        )
        self.assertEqual(
            self.storage.ledger_balance(), _INITIAL + Decimal(10 * expected)
        )
        # 并发写入后账本与审计链仍然自洽
        self.assertTrue(self.storage.verify_ledger().ok)
        self.assertTrue(self.storage.verify_audit_chain().ok)
        self.assertTrue(self.storage.reconcile_account().ok)

    def test_concurrent_initialization_grants_capital_once(self) -> None:
        path = self.root / "race" / "db.sqlite3"
        storage = Storage(path, tz=_JST, timeout=15.0)
        errors: list[BaseException] = []
        barrier = threading.Barrier(3)

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                storage.initialize_account(_INITIAL)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=worker) for _ in range(3)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join(timeout=30)

        self.assertEqual(errors, [], msg=f"并发初始化异常: {errors}")
        self.assertEqual(self.count("cash_flows"), 1)
        self.assertEqual(storage.ledger_balance(), _INITIAL)


# --------------------------------------------------------------------------- #
# 10. 配置集成与 CLI
# --------------------------------------------------------------------------- #


class TestConfigIntegrationAndCli(unittest.TestCase):
    def test_from_config_uses_configured_path(self) -> None:
        from config import load_config

        cfg = load_config()
        storage = Storage.from_config(cfg)
        self.assertEqual(storage.db_path, cfg.paths.database_path)
        self.assertEqual(storage.db_path.name, "fxsim.sqlite3")
        self.assertEqual(storage.db_path.parent.name, "data")

    def test_cli_initializes_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "data" / "fxsim.sqlite3"
            code = st._main(["--db", str(db), "--initialize", "--backup"])
            self.assertEqual(code, 0)
            self.assertTrue(db.is_file())
            self.assertTrue(any((Path(tmp) / "data" / "backups").iterdir()))
            # 再跑一次：不应重复发放本金
            self.assertEqual(st._main(["--db", str(db), "--initialize"]), 0)
            storage = Storage(db, tz=_JST)
            self.assertEqual(storage.ledger_balance(), _INITIAL)
            self.assertEqual(storage.counts()["cash_flows"], 1)

    def test_cli_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "data" / "fxsim.sqlite3"
            self.assertEqual(st._main(["--db", str(db), "--initialize", "--json"]), 0)

    def test_cli_reports_schema_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "foreign.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.commit()
            conn.close()
            self.assertEqual(st._main(["--db", str(path)]), 1)

    def test_audit_head_is_json_serializable_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", tz=_JST)
            storage.initialize_account(_INITIAL)
            report = storage.verify_integrity()
            payload = json.loads(json.dumps(report.as_dict(), ensure_ascii=False))
            self.assertIn("audit", payload)
            self.assertEqual(payload["ledger"]["ledger_balance"], "50000")
            self.assertEqual(INITIAL_CAPITAL_EVENT_KEY, "INITIAL_CAPITAL")


# --------------------------------------------------------------------------- #
# 10. AI 笔记
# --------------------------------------------------------------------------- #


class TestAgentNotes(StorageTestCase):
    def test_table_exists_after_open(self) -> None:
        self.assertEqual(self.storage.count_notes(), 0)
        self.assertEqual(self.storage.counts()["agent_notes"], 0)

    def test_record_and_read(self) -> None:
        note, created = self.storage.record_note(
            "LESSON", "别在数据缺口时开仓",
            tags=["risk", "data-gap"], decision_id="dec-1", trading_date="2026-10-10",
        )
        self.assertTrue(created)
        self.assertEqual(note.kind, "LESSON")
        self.assertEqual(note.content, "别在数据缺口时开仓")
        self.assertEqual(note.tags, ("risk", "data-gap"))
        self.assertEqual(note.decision_id, "dec-1")
        self.assertEqual(note.author, "ai")
        read = self.storage.read_notes()
        self.assertEqual([n.note_no for n in read], [note.note_no])

    def test_tags_are_deduped_and_trimmed(self) -> None:
        note, _ = self.storage.record_note("LESSON", "x", tags=[" a ", "a", "", "b"])
        self.assertEqual(note.tags, ("a", "b"))

    def test_newest_first_is_default(self) -> None:
        for index in range(3):
            self.storage.record_note("LESSON", f"n{index}")
        self.assertEqual([n.content for n in self.storage.read_notes()],
                         ["n2", "n1", "n0"])
        self.assertEqual([n.content for n in self.storage.read_notes(newest_first=False)],
                         ["n0", "n1", "n2"])

    def test_filter_by_kind_and_limit(self) -> None:
        self.storage.record_note("LESSON", "l1")
        self.storage.record_note("PLAN", "p1")
        self.storage.record_note("LESSON", "l2")
        kinds = [n.content for n in self.storage.read_notes(kind="LESSON", newest_first=False)]
        self.assertEqual(kinds, ["l1", "l2"])
        self.assertEqual(len(self.storage.read_notes(limit=1)), 1)

    def test_event_key_is_idempotent(self) -> None:
        first, created1 = self.storage.record_note("LESSON", "同一件事", event_key="k1")
        second, created2 = self.storage.record_note("LESSON", "同一件事", event_key="k1")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.note_no, second.note_no)
        self.assertEqual(self.storage.count_notes(), 1)

    def test_event_key_can_demand_error(self) -> None:
        self.storage.record_note("LESSON", "x", event_key="k2")
        with self.assertRaises(DuplicateEventError):
            self.storage.record_note("LESSON", "x", event_key="k2", on_duplicate="ERROR")

    def test_empty_kind_or_content_rejected(self) -> None:
        with self.assertRaises(StorageError):
            self.storage.record_note("", "x")
        with self.assertRaises(StorageError):
            self.storage.record_note("LESSON", "   ")
        self.assertEqual(self.storage.count_notes(), 0)

    def test_kind_length_bounded(self) -> None:
        with self.assertRaises(StorageError):
            self.storage.record_note("K" * 41, "x")

    def test_content_length_bounded(self) -> None:
        """笔记会被回灌进上下文，必须有硬上限。"""
        with self.assertRaises(StorageError):
            self.storage.record_note("LESSON", "x" * (st.MAX_NOTE_CHARS + 1))
        self.assertEqual(self.storage.count_notes(), 0)

    def test_note_joins_audit_chain(self) -> None:
        self.storage.record_note("LESSON", "进链", decision_id="dec-audit")
        events = [e for e in self.storage.read_audit_events() if e.event_type == "AGENT_NOTE"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["content"], "进链")
        self.assertEqual(events[0].payload["decision_id"], "dec-audit")
        self.assertTrue(self.storage.verify_audit_chain().ok)

    def test_note_does_not_touch_ledger_or_pnl(self) -> None:
        """笔记不是账本事实：写笔记不得改变余额、盈亏或任何账户字段。"""
        before = self.storage.read_account_state()
        self.storage.record_note("LESSON", "不动账")
        after = self.storage.read_account_state()
        self.assertEqual(before.cash_balance, after.cash_balance)
        self.assertEqual(before.realized_pnl, after.realized_pnl)
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)
        self.assertTrue(self.storage.reconcile_account(strict=False).ok)

    def test_note_survives_reopen(self) -> None:
        self.storage.record_note("PLAN", "要活下来")
        reopened = Storage(self.db_path, tz=_JST)
        self.assertEqual([n.content for n in reopened.read_notes()], ["要活下来"])

    def test_integrity_ok_with_notes(self) -> None:
        self.storage.record_note("LESSON", "x")
        report = self.storage.verify_integrity()
        self.assertTrue(report.ok)
        self.assertTrue(report.audit.ok)

    def test_existing_db_gains_table_without_version_bump(self) -> None:
        """纯新增表不该让旧库报 schema 版本错误（用户既有正式账户不能被弄坏）。"""
        conn = self.raw()
        try:
            conn.execute("DROP TABLE agent_notes")
            conn.commit()
        finally:
            conn.close()
        reopened = Storage(self.db_path, tz=_JST)  # 不应抛 SchemaVersionError
        self.assertEqual(reopened.count_notes(), 0)
        self.assertEqual(int(reopened.get_meta("schema_version")), SCHEMA_VERSION)

    def test_notes_are_appended_not_overwritten(self) -> None:
        for index in range(5):
            self.storage.record_note("LESSON", f"n{index}")
        self.assertEqual(self.storage.count_notes(), 5)
        self.assertEqual(len(self.storage.read_notes(limit=100)), 5)


# --------------------------------------------------------------------------- #
# 11. 逐小时对话存档与小时摘要
# --------------------------------------------------------------------------- #


class TestAgentTurnsAndHours(StorageTestCase):
    MESSAGES = (
        {"role": "system", "content": "你是决策者"},
        {"role": "user", "content": "当前事实"},
        {"role": "assistant", "content": None},
        {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
    )

    def test_record_and_read_turn(self) -> None:
        turn, created = self.storage.record_turn(
            "2026-10-10T14", "dec-1", self.MESSAGES, outcome="HELD",
            summary="HELD 理由：观望",
        )
        self.assertTrue(created)
        self.assertEqual(turn.hour_key, "2026-10-10T14")
        self.assertEqual(turn.day_key, "2026-10-10")
        self.assertEqual(turn.outcome, "HELD")
        self.assertEqual(turn.messages, tuple(self.MESSAGES))
        read = self.storage.read_turns(hour_key="2026-10-10T14")
        self.assertEqual([t.turn_no for t in read], [turn.turn_no])

    def test_messages_round_trip_byte_identical(self) -> None:
        """重放要命中前缀缓存，所以消息必须原样存原样取，一个字节都不能变。"""
        turn, _ = self.storage.record_turn("2026-10-10T14", "dec-b", self.MESSAGES)
        self.assertEqual(
            json.dumps(list(turn.messages), ensure_ascii=False, sort_keys=False),
            json.dumps(list(self.MESSAGES), ensure_ascii=False, sort_keys=False),
        )

    def test_turn_is_idempotent_per_hour_and_decision(self) -> None:
        first, created1 = self.storage.record_turn("2026-10-10T14", "dec-2", self.MESSAGES)
        second, created2 = self.storage.record_turn("2026-10-10T14", "dec-2", self.MESSAGES)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.turn_no, second.turn_no)
        self.assertEqual(self.storage.count_turns(), 1)

    def test_same_decision_in_another_hour_is_a_new_turn(self) -> None:
        self.storage.record_turn("2026-10-10T14", "dec-3", self.MESSAGES)
        self.storage.record_turn("2026-10-10T15", "dec-3", self.MESSAGES)
        self.assertEqual(self.storage.count_turns(), 2)

    def test_reads_are_chronological(self) -> None:
        for hour in ("2026-10-10T15", "2026-10-10T13", "2026-10-10T14"):
            self.storage.record_turn(hour, f"dec-{hour[-2:]}", self.MESSAGES)
        hours = [t.hour_key for t in self.storage.read_turns()]
        self.assertEqual(hours, sorted(hours), "重放必须按时间正序")

    def test_list_turn_hours(self) -> None:
        self.storage.record_turn("2026-10-10T15", "a", self.MESSAGES)
        self.storage.record_turn("2026-10-10T13", "b", self.MESSAGES)
        self.assertEqual(
            self.storage.list_turn_hours(), ["2026-10-10T13", "2026-10-10T15"]
        )

    def test_empty_hour_key_rejected(self) -> None:
        with self.assertRaises(StorageError):
            self.storage.record_turn("", "dec-x", self.MESSAGES)

    def test_hour_summary_round_trip(self) -> None:
        summary, created = self.storage.record_hour_summary(
            "2026-10-10T14", "本小时 2 次决策", turn_count=2, stats={"HELD": 2},
        )
        self.assertTrue(created)
        self.assertEqual(summary.turn_count, 2)
        self.assertEqual(summary.stats, {"HELD": 2})
        self.assertEqual(self.storage.read_hour_summary("2026-10-10T14").summary,
                         "本小时 2 次决策")

    def test_hour_summary_is_overwritable(self) -> None:
        """摘要是派生数据，可以重算覆盖；原始对话不删。"""
        self.storage.record_hour_summary("2026-10-10T14", "第一版", turn_count=1)
        second, created = self.storage.record_hour_summary(
            "2026-10-10T14", "第二版", turn_count=5
        )
        self.assertFalse(created)
        self.assertEqual(second.summary, "第二版")
        self.assertEqual(self.storage.read_hour_summary("2026-10-10T14").turn_count, 5)
        self.assertEqual(len(self.storage.list_hour_summary_keys()), 1)

    def test_empty_summary_rejected(self) -> None:
        with self.assertRaises(StorageError):
            self.storage.record_hour_summary("2026-10-10T14", "   ")

    def test_hour_summaries_are_ordered(self) -> None:
        for hour in ("2026-10-10T15", "2026-10-10T13"):
            self.storage.record_hour_summary(hour, f"{hour} 摘要", turn_count=1)
        ascending = self.storage.read_hour_summaries(newest_first=False)
        self.assertEqual([h.hour_key for h in ascending],
                         ["2026-10-10T13", "2026-10-10T15"])
        newest = self.storage.read_hour_summaries(limit=1)
        self.assertEqual(newest[0].hour_key, "2026-10-10T15")

    def test_filter_by_day(self) -> None:
        self.storage.record_hour_summary("2026-10-10T13", "a", turn_count=1)
        self.storage.record_hour_summary("2026-10-11T13", "b", turn_count=1)
        self.assertEqual(
            [h.hour_key for h in self.storage.read_hour_summaries(day_key="2026-10-10")],
            ["2026-10-10T13"],
        )

    def test_hour_summary_joins_audit_chain(self) -> None:
        self.storage.record_hour_summary("2026-10-10T14", "进链", turn_count=1)
        types = [e.event_type for e in self.storage.read_audit_events()]
        self.assertIn("AGENT_HOUR_SUMMARY", types)
        self.assertTrue(self.storage.verify_audit_chain().ok)

    def test_archive_does_not_touch_ledger(self) -> None:
        self.storage.record_turn("2026-10-10T14", "dec-4", self.MESSAGES)
        self.storage.record_hour_summary("2026-10-10T14", "摘要", turn_count=1)
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)
        self.assertTrue(self.storage.reconcile_account(strict=False).ok)

    def test_counts_include_new_tables(self) -> None:
        self.storage.record_turn("2026-10-10T14", "dec-5", self.MESSAGES)
        self.storage.record_hour_summary("2026-10-10T14", "摘要", turn_count=1)
        counts = self.storage.counts()
        self.assertEqual(counts["agent_turns"], 1)
        self.assertEqual(counts["hour_summaries"], 1)

    def test_survives_reopen(self) -> None:
        self.storage.record_turn("2026-10-10T14", "dec-6", self.MESSAGES)
        reopened = Storage(self.db_path, tz=_JST)
        self.assertEqual(reopened.count_turns(), 1)
        self.assertEqual(reopened.read_turns()[0].messages, tuple(self.MESSAGES))

    def test_existing_db_gains_tables_without_version_bump(self) -> None:
        conn = self.raw()
        try:
            conn.execute("DROP TABLE agent_turns")
            conn.execute("DROP TABLE hour_summaries")
            conn.commit()
        finally:
            conn.close()
        reopened = Storage(self.db_path, tz=_JST)
        self.assertEqual(reopened.count_turns(), 0)
        self.assertEqual(reopened.list_hour_summary_keys(), [])
        self.assertEqual(int(reopened.get_meta("schema_version")), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
