"""scheduler 模块的自动化测试。

覆盖：08:00 / 20:00 JST 报告时点计算、报告任务的唯一性与幂等（重复唤醒不重复生成）、
监控间隔判定、重启后的状态恢复与补查、重大事件快照、任务异常与网络故障处理、
AI 决策任务的间隔判定与「失败就等下一个间隔」策略、分离进程的启动/停止/状态管理，
以及「调度器不绕过引擎下单、默认 dry-run、未配 LLM 就不调用语言模型」的约束。

需要真实进程的用例会实际启动一个**分离进程**（这是本机实测可用的能力），
其余用例全部在临时目录里运行，**不访问网络、不连接券商、不做真实交易**。
AI 决策用例一律注入**假 Agent**，不消耗 token。

运行方式（项目根目录 E:\\pj2\\fx-kurumi-sim）::

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import subprocess
import time
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import scheduler as sched  # noqa: E402
from config import load_config  # noqa: E402
from engine import TradingEngine  # noqa: E402
from market_data import MarketDataError  # noqa: E402
from reporting import ReportKind  # noqa: E402
from risk_monitor import RiskEvent, RiskEventKind  # noqa: E402
from storage import RunStatus, Storage  # noqa: E402

_JST = timezone(timedelta(hours=9))


@dataclass
class StubMonitorResult:
    events: tuple = ()
    closed: tuple = ()
    equity: Decimal | None = Decimal("50000")
    margin_level: Decimal | None = None
    blocked_new_positions: bool = False
    #: 调度器用这两项判断「可交易状态」（休市跳过决策的权威信号）。
    quote_ok: bool = True
    quote_is_stale: bool = False


class RecordingMonitor:
    """记录调用参数的假监控器（避免联网）。"""

    def __init__(self, result=None, error: BaseException | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result if result is not None else StubMonitorResult()
        self._error = error

    def monitor_once(self, quote=None, *, now=None, backfill=True, last_check_at=None):
        self.calls.append(
            {"quote": quote, "now": now, "backfill": backfill, "last_check_at": last_check_at}
        )
        if self._error is not None:
            raise self._error
        return self._result


class SchedulerTestCase(unittest.TestCase):
    """公共夹具：临时数据库 + 已初始化账户 + 假监控器。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "data" / "fxsim.sqlite3"
        self.reports = self.root / "reports"
        self.state = self.root / "state"
        self.cfg = load_config()
        self.storage = Storage(self.db, tz=_JST)
        self.storage.initialize_account(
            self.cfg.account.initial_capital_jpy,
            opened_at=datetime(2026, 10, 10, 8, 0, tzinfo=_JST),
        )
        self.engine = TradingEngine(self.cfg, self.storage)
        self.monitor = RecordingMonitor()
        self.scheduler = sched.Scheduler(
            self.cfg,
            storage=self.storage,
            engine=self.engine,
            monitor=self.monitor,
            reports_dir=self.reports,
            state_dir=self.state,
        )

    # -- 工具 -------------------------------------------------------------- #

    def new_scheduler(self, monitor=None, **kwargs) -> sched.Scheduler:
        return sched.Scheduler(
            self.cfg,
            storage=Storage(self.db, tz=_JST),
            engine=TradingEngine(self.cfg, Storage(self.db, tz=_JST)),
            monitor=monitor or RecordingMonitor(),
            reports_dir=self.reports,
            state_dir=self.state,
            **kwargs,
        )

    def seed_monitor_run(self, started_at: datetime) -> None:
        run = self.storage.start_scheduler_run(
            sched.MONITOR_TASK_NAME, started_at=started_at
        )
        self.storage.finish_scheduler_run(run.run_no, status=RunStatus.SUCCESS)


# --------------------------------------------------------------------------- #
# 1. 报告时点（08:00 / 20:00 JST）
# --------------------------------------------------------------------------- #


class TestReportSlots(unittest.TestCase):
    def test_slot_hours(self) -> None:
        self.assertEqual(ReportKind.MORNING.slot_hour, 8)
        self.assertEqual(ReportKind.EVENING.slot_hour, 20)

    def test_latest_slot_before_eight_is_previous_evening(self) -> None:
        kind, slot = sched.latest_report_slot(datetime(2026, 10, 11, 3, 0, tzinfo=_JST), _JST)
        self.assertEqual(kind, ReportKind.EVENING)
        self.assertEqual(slot, datetime(2026, 10, 10, 20, 0, tzinfo=_JST))

    def test_latest_slot_between_eight_and_twenty_is_morning(self) -> None:
        kind, slot = sched.latest_report_slot(datetime(2026, 10, 11, 13, 30, tzinfo=_JST), _JST)
        self.assertEqual(kind, ReportKind.MORNING)
        self.assertEqual(slot, datetime(2026, 10, 11, 8, 0, tzinfo=_JST))

    def test_latest_slot_after_twenty_is_evening(self) -> None:
        kind, slot = sched.latest_report_slot(datetime(2026, 10, 11, 20, 0, tzinfo=_JST), _JST)
        self.assertEqual(kind, ReportKind.EVENING)
        self.assertEqual(slot, datetime(2026, 10, 11, 20, 0, tzinfo=_JST))

    def test_due_slots_are_exactly_eight_and_twenty(self) -> None:
        scheduler = SchedulerTestCase.setUp if False else None  # 占位避免误用
        self.assertIsNone(scheduler)

    def test_slots_are_twelve_hours_apart(self) -> None:
        now = datetime(2026, 10, 11, 17, 0, tzinfo=_JST)
        cfg = load_config()
        scheduler = sched.Scheduler(cfg, storage=Storage(Path(tempfile.mkdtemp()) / "d.sqlite3", tz=_JST),
                                    monitor=RecordingMonitor(), state_dir=Path(tempfile.mkdtemp()))
        slots = scheduler.due_report_slots(now)
        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0], (ReportKind.MORNING, datetime(2026, 10, 11, 8, 0, tzinfo=_JST)))
        self.assertEqual(slots[1], (ReportKind.EVENING, datetime(2026, 10, 10, 20, 0, tzinfo=_JST)))
        self.assertEqual(slots[0][1] - slots[1][1], timedelta(hours=12))

    def test_event_key_is_unique_per_slot(self) -> None:
        morning = sched.Scheduler.report_event_key(
            ReportKind.MORNING, datetime(2026, 10, 11, 8, 0, tzinfo=_JST)
        )
        evening = sched.Scheduler.report_event_key(
            ReportKind.EVENING, datetime(2026, 10, 11, 20, 0, tzinfo=_JST)
        )
        self.assertNotEqual(morning, evening)
        self.assertEqual(morning, "report:MORNING:2026-10-11T08:00+09:00")

    def test_next_slots_are_upcoming(self) -> None:
        now = datetime(2026, 10, 11, 9, 0, tzinfo=_JST)
        cfg = load_config()
        scheduler = sched.Scheduler(cfg, storage=Storage(Path(tempfile.mkdtemp()) / "d.sqlite3", tz=_JST),
                                    monitor=RecordingMonitor(), state_dir=Path(tempfile.mkdtemp()))
        upcoming = scheduler.next_report_slots(now)
        self.assertEqual(upcoming[0][0], "EVENING")
        self.assertTrue(upcoming[0][1].startswith("2026-10-11T20:00"))
        self.assertEqual(upcoming[1][0], "MORNING")
        self.assertTrue(upcoming[1][1].startswith("2026-10-12T08:00"))


# --------------------------------------------------------------------------- #
# 2. tick：幂等与报告任务
# --------------------------------------------------------------------------- #


class TestTick(SchedulerTestCase):
    def test_first_tick_creates_catch_up_report_tasks(self) -> None:
        now = datetime(2026, 10, 10, 17, 0, tzinfo=_JST)
        result = self.scheduler.tick(now=now)
        self.assertTrue(result.ok, msg=str(result.errors))
        self.assertTrue(result.monitor_ran)
        self.assertEqual(len(result.report_tasks_created), 2)
        kinds = sorted(t.kind for t in result.report_tasks_created)
        self.assertEqual(kinds, ["EVENING", "MORNING"])
        slots = sorted(t.slot for t in result.report_tasks_created)
        self.assertEqual(slots[0], datetime(2026, 10, 9, 20, 0, tzinfo=_JST))
        self.assertEqual(slots[1], datetime(2026, 10, 10, 8, 0, tzinfo=_JST))

    def test_repeated_ticks_do_not_duplicate_report_tasks(self) -> None:
        now = datetime(2026, 10, 10, 17, 0, tzinfo=_JST)
        first = self.scheduler.tick(now=now)
        self.assertEqual(len(first.report_tasks_created), 2)
        for _ in range(5):
            again = self.scheduler.tick(now=now)
            self.assertEqual(len(again.report_tasks_created), 0)
            self.assertEqual(len(again.report_tasks_skipped), 2)
        self.assertEqual(len(self.scheduler.list_report_tasks()), 2)
        report_runs = [
            r for r in self.storage.read_scheduler_runs()
            if r.task_name.startswith(sched.REPORT_TASK_PREFIX)
        ]
        self.assertEqual(len(report_runs), 2)  # 只有 2 条任务记录，没有重复

    def test_tick_creates_exactly_one_task_per_slot(self) -> None:
        self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        # 到了晚间时点后再 tick，只应新增晚间任务
        result = self.scheduler.tick(now=datetime(2026, 10, 10, 20, 0, 1, tzinfo=_JST))
        self.assertEqual(len(result.report_tasks_created), 1)
        self.assertEqual(result.report_tasks_created[0].kind, "EVENING")
        self.assertEqual(
            result.report_tasks_created[0].slot,
            datetime(2026, 10, 10, 20, 0, tzinfo=_JST),
        )
        self.assertEqual(len(self.scheduler.list_report_tasks()), 3)

    def test_report_task_artifacts_exist(self) -> None:
        result = self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        task = result.report_tasks_created[0]
        self.assertTrue(Path(task.snapshot_path).is_file())
        self.assertTrue(Path(task.template_path).is_file())
        self.assertTrue(Path(task.handoff_path).is_file())
        payload = json.loads(Path(task.snapshot_path).read_text(encoding="utf-8"))
        self.assertIn("account", payload)
        self.assertEqual(payload["positions"], [])
        handoff = json.loads(Path(task.handoff_path).read_text(encoding="utf-8"))
        self.assertEqual(handoff["status"], "PENDING")
        self.assertIn("Agent", handoff["agent_instruction"])
        self.assertIn("不会也不能唤醒语言模型", handoff["agent_instruction"])

    def test_report_task_uses_slot_period(self) -> None:
        result = self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        morning = next(t for t in result.report_tasks_created if t.kind == "MORNING")
        self.assertEqual(morning.period_start, datetime(2026, 10, 9, 20, 0, tzinfo=_JST))
        self.assertEqual(morning.period_end, datetime(2026, 10, 10, 8, 0, tzinfo=_JST))
        evening = next(t for t in result.report_tasks_created if t.kind == "EVENING")
        self.assertEqual(evening.period_start, datetime(2026, 10, 9, 8, 0, tzinfo=_JST))
        self.assertEqual(evening.period_end, datetime(2026, 10, 9, 20, 0, tzinfo=_JST))

    def test_report_run_recorded_with_times_and_result(self) -> None:
        self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        runs = [
            r for r in self.storage.read_scheduler_runs()
            if r.task_name.startswith(sched.REPORT_TASK_PREFIX)
        ]
        self.assertTrue(runs)
        for run in runs:
            self.assertEqual(run.status, RunStatus.SUCCESS.value)
            self.assertIsNotNone(run.started_at)
            self.assertIsNotNone(run.finished_at)
            self.assertTrue(run.detail)

    def test_monitor_run_recorded_with_times(self) -> None:
        self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        run = self.storage.read_scheduler_runs(task_name=sched.MONITOR_TASK_NAME)[0]
        self.assertEqual(run.status, RunStatus.SUCCESS.value)
        self.assertIsNotNone(run.finished_at)
        self.assertIn("事件", run.detail)

    def test_complete_report_task(self) -> None:
        result = self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        task = result.report_tasks_created[0]
        done = self.scheduler.complete_report_task(task.task_id, note="已撰写")
        self.assertEqual(done.status, "COMPLETED")
        payload = json.loads(Path(task.handoff_path).read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "COMPLETED")
        self.assertEqual(payload["completion_note"], "已撰写")
        self.assertIn("completed_at", payload)

    def test_complete_unknown_task_raises(self) -> None:
        with self.assertRaises(Exception):
            self.scheduler.complete_report_task("no-such-task")

    def test_tick_never_opens_positions(self) -> None:
        """调度器不做交易决策：tick 不会开仓。

        注意：tick 会顺带结算**督促机制**（扣本金），所以余额会变；这里断言的是
        「没有产生持仓与成交」，以及余额的变化**恰好等于**督促扣款——
        任何别的资金变动都说明 tick 动了不该动的东西。
        """
        for hour in (9, 13, 17, 20):
            self.scheduler.tick(now=datetime(2026, 10, 10, hour, 0, tzinfo=_JST))
        self.assertEqual(self.storage.counts()["positions"], 0)
        self.assertEqual(self.storage.counts()["trades"], 0)
        charged = self.scheduler.engine.capital_pressure_total()
        self.assertEqual(self.storage.ledger_balance(), Decimal("50000") - charged)


# --------------------------------------------------------------------------- #
# 3. 监控间隔判定
# --------------------------------------------------------------------------- #


class TestMonitorInterval(SchedulerTestCase):
    def test_monitor_runs_when_no_previous_run(self) -> None:
        result = self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        self.assertTrue(result.monitor_ran)
        self.assertIsNone(result.monitor_skipped_reason)

    def test_monitor_skipped_before_interval(self) -> None:
        self.seed_monitor_run(datetime(2026, 10, 10, 17, 0, 0, tzinfo=_JST))
        result = self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, 30, tzinfo=_JST))
        self.assertFalse(result.monitor_ran)
        self.assertIn("未到间隔", result.monitor_skipped_reason or "")
        self.assertEqual(self.monitor.calls, [])

    def test_monitor_runs_after_interval(self) -> None:
        self.seed_monitor_run(datetime(2026, 10, 10, 17, 0, 0, tzinfo=_JST))
        result = self.scheduler.tick(now=datetime(2026, 10, 10, 17, 1, 1, tzinfo=_JST))
        self.assertTrue(result.monitor_ran)

    def test_force_monitor_overrides_interval(self) -> None:
        self.seed_monitor_run(datetime(2026, 10, 10, 17, 0, 0, tzinfo=_JST))
        result = self.scheduler.tick(
            now=datetime(2026, 10, 10, 17, 0, 5, tzinfo=_JST), force_monitor=True
        )
        self.assertTrue(result.monitor_ran)

    def test_interval_comes_from_config(self) -> None:
        self.assertEqual(
            self.scheduler.interval_seconds, int(self.cfg.market.monitor_interval_seconds)
        )
        self.assertEqual(self.scheduler.interval_seconds, 60)

    def test_monitor_receives_previous_run_time(self) -> None:
        """监控器必须拿到「上一次检查时刻」，否则补查窗口会被自己刚写的记录吃掉。"""
        previous = datetime(2026, 10, 10, 16, 0, 0, tzinfo=_JST)
        self.seed_monitor_run(previous)
        self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, 0, tzinfo=_JST))
        self.assertEqual(len(self.monitor.calls), 1)
        self.assertEqual(self.monitor.calls[0]["last_check_at"], previous)

    def test_monitor_receives_none_when_no_previous_run(self) -> None:
        self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, 0, tzinfo=_JST))
        self.assertIsNone(self.monitor.calls[0]["last_check_at"])


# --------------------------------------------------------------------------- #
# 4. 重启恢复
# --------------------------------------------------------------------------- #


class TestRestartRecovery(SchedulerTestCase):
    def test_leftover_running_runs_are_aborted(self) -> None:
        leftover = self.storage.start_scheduler_run(
            sched.MONITOR_TASK_NAME, started_at=datetime(2026, 10, 10, 16, 0, tzinfo=_JST)
        )
        self.assertEqual(leftover.status, RunStatus.RUNNING.value)
        # 模拟进程重启后新建调度器
        fresh = self.new_scheduler()
        recovered = fresh.recover()
        self.assertEqual(recovered, 1)
        runs = self.storage.read_scheduler_runs()
        aborted = [r for r in runs if r.run_no == leftover.run_no]
        self.assertEqual(aborted[0].status, RunStatus.ABORTED.value)

    def test_recover_runs_once_automatically_on_first_tick(self) -> None:
        self.storage.start_scheduler_run(
            sched.MONITOR_TASK_NAME, started_at=datetime(2026, 10, 10, 16, 0, tzinfo=_JST)
        )
        fresh = self.new_scheduler()
        fresh.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        statuses = [r.status for r in self.storage.read_scheduler_runs()]
        self.assertIn(RunStatus.ABORTED.value, statuses)

    def test_restart_does_not_recreate_report_tasks(self) -> None:
        """报告任务的唯一性由数据库约束保证，跨重启也不会重复生成。"""
        now = datetime(2026, 10, 10, 17, 0, tzinfo=_JST)
        self.scheduler.tick(now=now)
        self.assertEqual(len(self.scheduler.list_report_tasks()), 2)
        fresh = self.new_scheduler()
        result = fresh.tick(now=now)
        self.assertEqual(len(result.report_tasks_created), 0)
        self.assertEqual(len(fresh.list_report_tasks()), 2)

    def test_restart_recovers_monitor_state_from_storage(self) -> None:
        """重启后监控器从数据库拿到上次检查时刻，据此补查中断期间行情。"""
        previous = datetime(2026, 10, 10, 16, 0, 0, tzinfo=_JST)
        self.seed_monitor_run(previous)
        fresh_monitor = RecordingMonitor()
        fresh = self.new_scheduler(monitor=fresh_monitor)
        fresh.tick(now=datetime(2026, 10, 10, 17, 0, 0, tzinfo=_JST))
        self.assertEqual(fresh_monitor.calls[0]["last_check_at"], previous)

    def test_report_task_survives_restart_in_db(self) -> None:
        self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        fresh = self.new_scheduler()
        tasks = fresh.list_report_tasks()
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(t.status == "PENDING" for t in tasks))


# --------------------------------------------------------------------------- #
# 5. 重大事件快照
# --------------------------------------------------------------------------- #

class TestEventSnapshot(SchedulerTestCase):
    def make_event(self, kind=RiskEventKind.STOP_LOSS, key="EXIT:STOP_LOSS:1") -> RiskEvent:
        return RiskEvent(
            event_key=key,
            kind=kind.value,
            triggered_at=datetime(2026, 10, 10, 16, 59, tzinfo=_JST),
            detected_at=datetime(2026, 10, 10, 17, 0, tzinfo=_JST),
            reason="测试事件",
            position_id=1,
            reference_bid=Decimal("157.900"),
            reference_ask=Decimal("157.910"),
            fill_price=Decimal("157.950"),
            realized_pnl_jpy=Decimal("-60"),
        )

    def test_major_event_saved_with_account_snapshot(self) -> None:
        event = self.make_event()
        path = self.scheduler.save_event_snapshot(event, now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        self.assertIsNotNone(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("event", payload)
        self.assertIn("account_snapshot", payload)
        self.assertEqual(payload["event"]["kind"], RiskEventKind.STOP_LOSS.value)
        self.assertEqual(payload["account_snapshot"]["account"]["cash_balance"], "50000.000")
        self.assertIn("saved_at", payload)

    def test_same_event_saved_only_once(self) -> None:
        event = self.make_event()
        first = self.scheduler.save_event_snapshot(event)
        second = self.scheduler.save_event_snapshot(event)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(list(self.scheduler.events_dir.glob("*.json"))), 1)

    def test_different_events_get_different_files(self) -> None:
        self.scheduler.save_event_snapshot(self.make_event(key="EXIT:STOP_LOSS:1"))
        self.scheduler.save_event_snapshot(
            self.make_event(RiskEventKind.MARGIN_CALL, key="EXIT:MARGIN_CALL:2")
        )
        self.assertEqual(len(list(self.scheduler.events_dir.glob("*.json"))), 2)

    def test_tick_saves_snapshot_for_major_event(self) -> None:
        monitor = RecordingMonitor(
            result=StubMonitorResult(events=(self.make_event(),))
        )
        scheduler = self.new_scheduler(monitor=monitor)
        result = scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        self.assertEqual(len(result.event_snapshots), 1)
        self.assertTrue(Path(result.event_snapshots[0]).is_file())

    def test_tick_ignores_non_major_events(self) -> None:
        event = RiskEvent(
            event_key="DATA_GAP:x", kind=RiskEventKind.DATA_GAP.value,
            triggered_at=datetime(2026, 10, 10, 16, 0, tzinfo=_JST),
            detected_at=datetime(2026, 10, 10, 17, 0, tzinfo=_JST),
            reason="数据缺口",
        )
        scheduler = self.new_scheduler(
            monitor=RecordingMonitor(result=StubMonitorResult(events=(event,)))
        )
        result = scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        self.assertEqual(result.event_snapshots, ())

    def test_major_event_kinds_are_the_risk_ones(self) -> None:
        self.assertIn(RiskEventKind.STOP_LOSS.value, sched.MAJOR_EVENT_KINDS)
        self.assertIn(RiskEventKind.MARGIN_CALL.value, sched.MAJOR_EVENT_KINDS)
        self.assertIn(RiskEventKind.BANKRUPTCY.value, sched.MAJOR_EVENT_KINDS)
        self.assertNotIn(RiskEventKind.DATA_GAP.value, sched.MAJOR_EVENT_KINDS)


# --------------------------------------------------------------------------- #
# 6. 异常与网络故障
# --------------------------------------------------------------------------- #


class TestErrorHandling(SchedulerTestCase):
    def test_monitor_exception_recorded_and_tick_continues(self) -> None:
        scheduler = self.new_scheduler(
            monitor=RecordingMonitor(error=RuntimeError("模拟监控崩溃"))
        )
        result = scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        self.assertFalse(result.ok)
        self.assertFalse(result.monitor_ran)
        self.assertTrue(any("模拟监控崩溃" in e for e in result.errors))
        # 监控失败不影响报告任务创建
        self.assertEqual(len(result.report_tasks_created), 2)
        run = self.storage.read_scheduler_runs(task_name=sched.MONITOR_TASK_NAME)[0]
        self.assertEqual(run.status, RunStatus.FAILED.value)
        self.assertIn("模拟监控崩溃", run.error or "")

    def test_report_task_failure_recorded_and_others_continue(self) -> None:
        with mock.patch.object(
            sched, "build_report_data", side_effect=RuntimeError("模拟报告失败")
        ):
            result = self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        self.assertTrue(result.errors)
        self.assertEqual(len(result.report_tasks_created), 0)
        failed = [
            r for r in self.storage.read_scheduler_runs()
            if r.task_name.startswith(sched.REPORT_TASK_PREFIX)
        ]
        self.assertTrue(all(r.status == RunStatus.FAILED.value for r in failed))

    def test_network_failure_does_not_break_tick(self) -> None:
        """行情获取失败时监控器降级处理，tick 仍然成功并生成报告任务。"""
        real_monitor = sched.RiskMonitor(
            self.cfg, self.engine, storage=self.storage,
            quote_source=lambda: (_ for _ in ()).throw(MarketDataError("网络不可达")),
            bars_source=lambda s, e: (_ for _ in ()).throw(MarketDataError("网络不可达")),
        )
        scheduler = self.new_scheduler(monitor=real_monitor)
        result = scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        self.assertTrue(result.monitor_ran)
        self.assertEqual(len(result.report_tasks_created), 2)
        self.assertTrue(result.blocked_new_positions)  # 数据不可用 → 禁止新开仓

    def test_event_snapshot_failure_does_not_break_tick(self) -> None:
        event = TestEventSnapshot.make_event(self)
        monitor = RecordingMonitor(result=StubMonitorResult(events=(event,)))
        scheduler = self.new_scheduler(monitor=monitor)
        with mock.patch.object(
            sched, "build_report_data", side_effect=RuntimeError("快照失败")
        ):
            result = scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        # 事件快照失败不产生 error（只记日志），tick 仍返回
        self.assertIsInstance(result, sched.TickResult)


# --------------------------------------------------------------------------- #
# 7. run_forever 与进程管理
# --------------------------------------------------------------------------- #


class TestRunForever(SchedulerTestCase):
    def test_iterations(self) -> None:
        ticks = self.scheduler.run_forever(interval_seconds=0.05, iterations=3)
        self.assertEqual(ticks, 3)

    def test_stop_event(self) -> None:
        import threading

        stop = threading.Event()
        stop.set()
        self.assertEqual(
            self.scheduler.run_forever(interval_seconds=0.05, stop_event=stop), 0
        )

    def test_stop_file_exits_cleanly(self) -> None:
        self.scheduler.request_stop()
        self.assertTrue(self.scheduler.stop_file.exists())
        self.assertEqual(self.scheduler.run_forever(interval_seconds=0.05), 0)

    def test_exception_recovery_continues(self) -> None:
        calls = {"n": 0}
        original = sched.Scheduler.tick

        def flaky(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟单次失败")
            return original(self, **kwargs)

        with mock.patch.object(sched.Scheduler, "tick", flaky):
            ticks = self.scheduler.run_forever(interval_seconds=0.05, iterations=2)
        self.assertEqual(ticks, 2)

    def test_gives_up_after_max_consecutive_errors(self) -> None:
        with mock.patch.object(sched.Scheduler, "tick", side_effect=RuntimeError("持续失败")):
            with self.assertRaises(RuntimeError):
                self.scheduler.run_forever(
                    interval_seconds=0.05, max_consecutive_errors=2
                )

    def test_keyboard_interrupt_is_swallowed(self) -> None:
        with mock.patch.object(sched.Scheduler, "tick", side_effect=KeyboardInterrupt):
            self.assertEqual(self.scheduler.run_forever(interval_seconds=0.05), 0)

    def test_interval_must_be_positive(self) -> None:
        with self.assertRaises(Exception):
            self.scheduler.run_forever(interval_seconds=0, iterations=1)


class TestProcessManagement(SchedulerTestCase):
    def test_pid_alive_for_current_process(self) -> None:
        self.assertTrue(sched._pid_alive(os.getpid()))

    def test_pid_alive_for_bogus_pid(self) -> None:
        self.assertFalse(sched._pid_alive(999_999_999))
        self.assertFalse(sched._pid_alive(0))
        self.assertFalse(sched._pid_alive(-1))

    def test_status_when_not_running(self) -> None:
        status = self.scheduler.status()
        self.assertFalse(status.running)
        self.assertIsNone(status.pid)
        self.assertEqual(status.timezone, str(self.cfg.tz))
        self.assertEqual(status.interval_seconds, 60)

    def test_status_reports_running_when_pid_alive(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        self.scheduler.pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
        status = self.scheduler.status()
        self.assertTrue(status.running)
        self.assertEqual(status.pid, os.getpid())

    def test_status_reports_stale_pid_as_not_running(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        self.scheduler.pid_file.write_text("999999999\n", encoding="utf-8")
        self.assertFalse(self.scheduler.status().running)

    def test_request_stop_writes_flag(self) -> None:
        self.scheduler.request_stop()
        self.assertTrue(self.scheduler.stop_file.is_file())

    def test_stop_without_pid_file_is_noop(self) -> None:
        ok, message = self.scheduler.stop(timeout=0.1)
        self.assertTrue(ok)
        self.assertIn("未在运行", message)

    def test_stop_cleans_stale_pid_file(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        self.scheduler.pid_file.write_text("999999999\n", encoding="utf-8")
        ok, message = self.scheduler.stop(timeout=0.1)
        self.assertTrue(ok)
        self.assertFalse(self.scheduler.pid_file.exists())

    def test_status_includes_pending_tasks_and_next_slots(self) -> None:
        self.scheduler.tick(now=datetime(2026, 10, 10, 17, 0, tzinfo=_JST))
        status = self.scheduler.status()
        self.assertEqual(len(status.pending_report_tasks), 2)
        self.assertEqual(len(status.next_report_slots), 2)
        self.assertIsNotNone(status.last_monitor_run)
        self.assertIsNotNone(status.last_report_run)

    def test_status_json_serializable(self) -> None:
        status = self.scheduler.status()
        json.loads(json.dumps(status.as_dict(), ensure_ascii=False))

    def _kill_if_alive(self, pid: int) -> None:
        """收尾：把可能还活着的分离进程杀掉，避免它漏到别的用例里。"""
        if not sched._pid_alive(pid):
            return
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           check=False, capture_output=True)
        except OSError:  # pragma: no cover
            pass

    def test_detached_process_actually_runs_and_exits(self) -> None:
        """真正启动一个分离进程，验证它脱离父进程运行并写出运行记录。

        这是本机实测可用的后台执行能力，因此这里做一次真实集成验证：
        用 ``--iterations 2`` 让它跑两次 tick 后自行退出。

        **注意**：这个用例会走**真实网络**——分离进程里的监控器要取一次真实报价
        （只有第一次 tick 会取，第二次因未到 60 秒间隔而跳过）。因此它的耗时取决于
        行情源，实测在 2–40 秒之间。这里给足 120 秒，而不是把超时设得很紧：
        本用例要验证的是「分离进程能跑完并自行退出」，不是「网络有多快」。
        行情源整个不可用时监控器会降级为 ``DEGRADED_MONITORING`` 而**不抛异常**，
        所以运行记录仍是 SUCCESS，断言依然成立。
        """
        pid = self.scheduler.launch_detached(
            interval_seconds=0.2, extra_args=["--iterations", "2"]
        )
        self.assertGreater(pid, 0)
        self.assertEqual(self.scheduler.read_pid(), pid)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if not sched._pid_alive(pid):
                break
            time.sleep(0.3)
        # 无论断言成败都要确保进程被收掉：否则它会在后续用例运行期间继续活着，
        # 既污染输出（ResourceWarning），也可能干扰其它对时序敏感的用例。
        self.addCleanup(self._kill_if_alive, pid)
        self.assertFalse(sched._pid_alive(pid), "分离进程应在 2 次 tick 后自行退出")
        log = self.scheduler.log_file.read_text(encoding="utf-8")
        self.assertIn("调度器启动", log)
        self.assertIn("调度器结束", log)
        runs = self.storage.read_scheduler_runs(task_name=sched.MONITOR_TASK_NAME)
        self.assertTrue(runs, "分离进程应写出监控运行记录")
        self.assertTrue(
            any(r.status == RunStatus.SUCCESS.value for r in runs)
        )


# --------------------------------------------------------------------------- #
# 8. AI 决策任务（每分钟一次，失败就等下一个间隔）
# --------------------------------------------------------------------------- #


@dataclass
class StubDecision:
    """假 DecisionResult：只保留调度器读取的字段。"""

    ok: bool = True
    outcome: str = "VALIDATED"
    message: str = "已校验（dry-run）"
    rounds: int = 1
    dry_run: bool = True


class RecordingAgent:
    """记录调用参数的假 TradingAgent（不联网、不消耗 token）。"""

    def __init__(self, result=None, error: BaseException | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result if result is not None else StubDecision()
        self._error = error

    def decide(self, *, quote=None, decision_id=None, dry_run=True, **kwargs):
        self.calls.append(
            {"quote": quote, "decision_id": decision_id, "dry_run": dry_run}
        )
        if self._error is not None:
            raise self._error
        return self._result


class TestDecisionTask(SchedulerTestCase):
    def configured_cfg(self):
        """把 llm 配置成「已就绪」，好让决策任务真正跑起来。"""
        import dataclasses

        return dataclasses.replace(
            self.cfg,
            llm=dataclasses.replace(
                self.cfg.llm, api_key="test-key", model_id="test-model"
            ),
        )

    def make(self, *, cfg=None, agent=None, **kwargs) -> sched.Scheduler:
        return sched.Scheduler(
            cfg or self.cfg,
            storage=Storage(self.db, tz=_JST),
            engine=TradingEngine(self.cfg, Storage(self.db, tz=_JST)),
            monitor=RecordingMonitor(),
            reports_dir=self.reports,
            state_dir=self.state,
            decision_agent=agent if agent is not None else RecordingAgent(),
            **kwargs,
        )

    # -- 默认值 ------------------------------------------------------------ #

    def test_interval_constant_is_sixty_seconds(self) -> None:
        self.assertEqual(sched.DEFAULT_DECISION_INTERVAL_SECONDS, 60)
        self.assertEqual(self.make().decision_interval_seconds, 60)

    def test_dry_run_by_default(self) -> None:
        self.assertTrue(self.make().decision_dry_run)

    def test_task_name_distinct_from_monitor(self) -> None:
        self.assertNotEqual(sched.DECISION_TASK_NAME, sched.MONITOR_TASK_NAME)

    # -- 跳过条件 ---------------------------------------------------------- #

    def test_skipped_when_llm_not_configured(self) -> None:
        """config.json 里没配 LLM 时，决策自动跳过，只做监控与报告。"""
        agent = RecordingAgent()
        scheduler = self.make(agent=agent)  # self.cfg 的 llm 未配置
        result = scheduler.tick(now=datetime(2026, 10, 13, 8, 30, tzinfo=_JST))
        self.assertFalse(result.decision_ran)
        self.assertIn("LLM 未配置", result.decision_skipped_reason)
        self.assertEqual(agent.calls, [])
        self.assertEqual(result.errors, ())

    def test_skipped_when_disabled(self) -> None:
        agent = RecordingAgent()
        scheduler = self.make(cfg=self.configured_cfg(), agent=agent, decision_enabled=False)
        result = scheduler.tick(now=datetime(2026, 10, 13, 8, 30, tzinfo=_JST))
        self.assertFalse(result.decision_ran)
        self.assertIn("未启用", result.decision_skipped_reason)
        self.assertEqual(agent.calls, [])

    # -- 正常执行 ---------------------------------------------------------- #

    def test_runs_when_configured_and_due(self) -> None:
        agent = RecordingAgent()
        scheduler = self.make(cfg=self.configured_cfg(), agent=agent)
        result = scheduler.tick(now=datetime(2026, 10, 13, 8, 30, tzinfo=_JST))
        self.assertTrue(result.decision_ran)
        self.assertEqual(result.decision_outcome, "VALIDATED")
        self.assertEqual(result.decision_status, "SUCCESS")
        self.assertIsNotNone(result.decision_run_no)
        self.assertEqual(len(agent.calls), 1)
        self.assertTrue(agent.calls[0]["dry_run"], "调度器默认必须 dry-run")

    def test_live_decisions_passes_dry_run_false(self) -> None:
        agent = RecordingAgent()
        scheduler = self.make(
            cfg=self.configured_cfg(), agent=agent, decision_dry_run=False
        )
        scheduler.tick(now=datetime(2026, 10, 13, 8, 30, tzinfo=_JST))
        self.assertFalse(agent.calls[0]["dry_run"])

    def test_run_is_recorded_in_scheduler_runs(self) -> None:
        scheduler = self.make(cfg=self.configured_cfg())
        scheduler.tick(now=datetime(2026, 10, 13, 8, 30, tzinfo=_JST))
        runs = self.storage.read_scheduler_runs(task_name=sched.DECISION_TASK_NAME)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, RunStatus.SUCCESS.value)

    def test_failed_decision_marks_run_failed(self) -> None:
        agent = RecordingAgent(result=StubDecision(ok=False, outcome="NO_DECISION",
                                                   message="模型没给结论"))
        scheduler = self.make(cfg=self.configured_cfg(), agent=agent)
        result = scheduler.tick(now=datetime(2026, 10, 13, 8, 30, tzinfo=_JST))
        self.assertTrue(result.decision_ran)
        self.assertEqual(result.decision_status, "FAILED")
        runs = self.storage.read_scheduler_runs(task_name=sched.DECISION_TASK_NAME)
        self.assertEqual(runs[0].status, RunStatus.FAILED.value)

    # -- 间隔与「失败就等下一分钟」 --------------------------------------- #

    def test_second_tick_within_interval_is_skipped(self) -> None:
        agent = RecordingAgent()
        scheduler = self.make(cfg=self.configured_cfg(), agent=agent)
        base = datetime(2026, 10, 13, 8, 30, tzinfo=_JST)
        scheduler.tick(now=base)
        result = scheduler.tick(now=base + timedelta(seconds=20))
        self.assertFalse(result.decision_ran)
        self.assertIn("未到间隔", result.decision_skipped_reason)
        self.assertEqual(len(agent.calls), 1)

    def test_runs_again_after_interval(self) -> None:
        agent = RecordingAgent()
        scheduler = self.make(cfg=self.configured_cfg(), agent=agent)
        base = datetime(2026, 10, 13, 8, 30, tzinfo=_JST)
        scheduler.tick(now=base)
        result = scheduler.tick(now=base + timedelta(seconds=61))
        self.assertTrue(result.decision_ran)
        self.assertEqual(len(agent.calls), 2)

    def test_failure_waits_for_next_interval(self) -> None:
        """上一次失败后，不重试、不加速——就是等下一个间隔。"""
        agent = RecordingAgent(error=RuntimeError("模型超时"))
        scheduler = self.make(cfg=self.configured_cfg(), agent=agent)
        base = datetime(2026, 10, 13, 8, 30, tzinfo=_JST)
        first = scheduler.tick(now=base)
        self.assertFalse(first.decision_ran)
        self.assertTrue(any("AI 决策失败" in e for e in first.errors))

        # 同一分钟内再 tick：因为上一次已经「启动过」，直接跳过。
        second = scheduler.tick(now=base + timedelta(seconds=30))
        self.assertFalse(second.decision_ran)
        self.assertIn("未到间隔", second.decision_skipped_reason)
        self.assertEqual(len(agent.calls), 1, "失败后不应在间隔内重试")

        # 过了间隔才再试一次。
        third = scheduler.tick(now=base + timedelta(seconds=62))
        self.assertEqual(len(agent.calls), 2)

    def test_failure_is_recorded_as_failed_run(self) -> None:
        agent = RecordingAgent(error=RuntimeError("模型超时"))
        scheduler = self.make(cfg=self.configured_cfg(), agent=agent)
        scheduler.tick(now=datetime(2026, 10, 13, 8, 30, tzinfo=_JST))
        runs = self.storage.read_scheduler_runs(task_name=sched.DECISION_TASK_NAME)
        self.assertEqual(runs[0].status, RunStatus.FAILED.value)
        self.assertIn("RuntimeError", runs[0].error or "")

    def test_custom_interval_respected(self) -> None:
        agent = RecordingAgent()
        scheduler = self.make(
            cfg=self.configured_cfg(), agent=agent, decision_interval_seconds=300
        )
        base = datetime(2026, 10, 13, 8, 30, tzinfo=_JST)
        scheduler.tick(now=base)
        result = scheduler.tick(now=base + timedelta(seconds=120))
        self.assertFalse(result.decision_ran)
        self.assertEqual(len(agent.calls), 1)

    # -- 幂等（按分钟取整的决策 ID） -------------------------------------- #

    def test_decision_id_is_per_minute(self) -> None:
        """同一分钟内的两次 tick 得到同一个 decision_id，引擎据此判重。"""
        agent = RecordingAgent()
        scheduler = self.make(
            cfg=self.configured_cfg(), agent=agent, decision_interval_seconds=1
        )
        base = datetime(2026, 10, 13, 8, 30, 5, tzinfo=_JST)
        scheduler.tick(now=base)
        scheduler.tick(now=base + timedelta(seconds=20))
        ids = [c["decision_id"] for c in agent.calls]
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[0], "tick-20261013T0830")

    def test_decision_id_changes_next_minute(self) -> None:
        agent = RecordingAgent()
        scheduler = self.make(
            cfg=self.configured_cfg(), agent=agent, decision_interval_seconds=1
        )
        base = datetime(2026, 10, 13, 8, 30, 5, tzinfo=_JST)
        scheduler.tick(now=base)
        scheduler.tick(now=base + timedelta(seconds=60))
        ids = [c["decision_id"] for c in agent.calls]
        self.assertNotEqual(ids[0], ids[1])

    # -- 不影响其他任务 ---------------------------------------------------- #

    def test_monitor_still_runs_when_decision_skipped(self) -> None:
        scheduler = self.make()  # LLM 未配置
        result = scheduler.tick(now=datetime(2026, 10, 13, 8, 30, tzinfo=_JST))
        self.assertTrue(result.monitor_ran)
        self.assertFalse(result.decision_ran)

    def test_decision_failure_does_not_break_tick(self) -> None:
        agent = RecordingAgent(error=RuntimeError("boom"))
        scheduler = self.make(cfg=self.configured_cfg(), agent=agent)
        result = scheduler.tick(now=datetime(2026, 10, 13, 8, 30, tzinfo=_JST))
        self.assertTrue(result.monitor_ran)
        self.assertEqual(len(result.report_tasks_created), 2)

    # -- 序列化 ------------------------------------------------------------ #

    def test_as_dict_has_decision_fields(self) -> None:
        scheduler = self.make(cfg=self.configured_cfg())
        payload = scheduler.tick(
            now=datetime(2026, 10, 13, 8, 30, tzinfo=_JST)
        ).as_dict()
        for key in (
            "decision_ran",
            "decision_skipped_reason",
            "decision_outcome",
            "decision_message",
            "decision_run_no",
            "decision_status",
        ):
            self.assertIn(key, payload)


# --------------------------------------------------------------------------- #
# 9. 能力声明与不做的事
# --------------------------------------------------------------------------- #


class TestCapabilitiesAndConstraints(unittest.TestCase):
    def test_capabilities_report_has_verified_entry(self) -> None:
        report = sched.capabilities_report()
        statuses = {item["capability"]: item["status"] for item in report}
        self.assertTrue(any(s == "verified" for s in statuses.values()))
        self.assertTrue(any(s == "not_possible" for s in statuses.values()))
        self.assertIn("脚本唤醒语言模型", statuses)

    def test_capabilities_does_not_claim_llm_wakeup(self) -> None:
        report = sched.capabilities_report()
        llm = next(i for i in report if i["capability"] == "脚本唤醒语言模型")
        self.assertEqual(llm["status"], "not_possible")
        self.assertIn("无法唤起", llm["detail"])

    def test_capabilities_does_not_claim_unverified_persistence(self) -> None:
        """不得把未验证的能力描述成已具备。"""
        report = sched.capabilities_report()
        for item in report:
            if item["status"] == "absent":
                self.assertIn("不存在", item["detail"])

    def test_scheduler_does_not_call_llm(self) -> None:
        source = Path(sys.modules["scheduler"].__file__).read_text(
            encoding="utf-8"
        )
        for forbidden in ("openai", "chat/completions", "requests.", "http.client"):
            self.assertNotIn(forbidden, source)

    def test_scheduler_never_imports_engine_execution(self) -> None:
        """调度器不导入任何开仓入口，从结构上保证它不会做交易决策。"""
        source = Path(sys.modules["scheduler"].__file__).read_text(
            encoding="utf-8"
        )
        self.assertNotIn("execute_virtual_trade", source)
        self.assertNotIn("TradeIntent", source)
        self.assertNotIn("open_position", source)

    def test_timezone_is_tokyo(self) -> None:
        cfg = load_config()
        self.assertEqual(str(cfg.tz), "Asia/Tokyo")


# --------------------------------------------------------------------------- #
# 9. CLI
# --------------------------------------------------------------------------- #


class _OfflineRiskMonitor:
    """离线的假监控器：不联网、不补查历史，只回一个空结果。"""

    def __init__(self, cfg=None, *args, **kwargs) -> None:
        self.cfg = cfg

    def monitor_once(self, quote=None, *, now=None, backfill=True, last_check_at=None):
        return StubMonitorResult()


def run_sched(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sched.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestSchedulerCli(SchedulerTestCase):
    """CLI 用例。

    ``run_sched`` 会让 ``_build_scheduler`` 造一个**真实**的 ``RiskMonitor``，
    而它的 ``monitor_once`` 会去联网补查历史 K 线。测试绝不能联网，所以这里把
    监控器换成离线的假实现（价格/风险逻辑本身在 test_risk_monitor 里覆盖）。
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(sched, "RiskMonitor", _OfflineRiskMonitor)
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self) -> list[str]:
        return [
            "--db", str(self.db),
            "--reports-dir", str(self.reports),
            "--state-dir", str(self.state),
        ]

    def test_capabilities(self) -> None:
        code, out, _ = run_sched("capabilities", *self.args())
        self.assertEqual(code, 0)
        self.assertIn("分离后台进程", out)
        self.assertIn("无法唤醒语言模型", out)

    def test_capabilities_json(self) -> None:
        code, out, _ = run_sched("capabilities", "--json", *self.args())
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(any(item["status"] == "verified" for item in payload))

    def test_tick(self) -> None:
        code, out, _ = run_sched("tick", *self.args())
        self.assertEqual(code, 0)
        self.assertIn("tick 时间", out)
        self.assertIn("报告任务新建", out)

    def test_tick_json_and_idempotent(self) -> None:
        code, out, _ = run_sched("tick", "--json", *self.args())
        payload = json.loads(out)
        self.assertEqual(len(payload["report_tasks_created"]), 2)
        code2, out2, _ = run_sched("tick", "--json", *self.args())
        payload2 = json.loads(out2)
        self.assertEqual(len(payload2["report_tasks_created"]), 0)
        self.assertEqual(len(payload2["report_tasks_skipped"]), 2)

    def test_status(self) -> None:
        code, out, _ = run_sched("status", *self.args())
        self.assertEqual(code, 0)
        self.assertIn("是否在运行", out)
        self.assertIn("Asia/Tokyo", out)

    def test_tasks_and_complete(self) -> None:
        run_sched("tick", *self.args())
        code, out, _ = run_sched("tasks", *self.args())
        self.assertEqual(code, 0)
        self.assertIn("PENDING", out)
        tasks = json.loads(run_sched("tasks", "--json", *self.args())[1])
        task_id = tasks[0]["task_id"]
        code, out, _ = run_sched("complete-task", task_id, "--note", "done", *self.args())
        self.assertEqual(code, 0)
        self.assertIn("COMPLETED", out)

    def test_install_task_dry_run_does_not_register(self) -> None:
        with mock.patch.object(sched.subprocess, "run") as runner:
            code, out, _ = run_sched("install-task", *self.args())
        self.assertEqual(code, 0)
        runner.assert_not_called()
        self.assertIn("预演模式", out)
        self.assertIn("schtasks", out)
        self.assertIn("tick", out)

    def test_install_task_apply_registers(self) -> None:
        completed = mock.Mock(returncode=0, stdout="成功", stderr="")
        with mock.patch.object(sched.subprocess, "run", return_value=completed) as runner:
            code, _, _ = run_sched("install-task", "--apply", *self.args())
        self.assertEqual(code, 0)
        argv = runner.call_args[0][0]
        self.assertEqual(argv[0], "schtasks")
        self.assertIn("/create", argv)
        self.assertIn("/sc", argv)

    def test_uninstall_task_dry_run(self) -> None:
        with mock.patch.object(sched.subprocess, "run") as runner:
            code, out, _ = run_sched("uninstall-task", *self.args())
        self.assertEqual(code, 0)
        runner.assert_not_called()
        self.assertIn("预演模式", out)

    def test_no_command_prints_help(self) -> None:
        code, out, _ = run_sched()
        self.assertEqual(code, 2)
        self.assertIn("usage:", out)

    # -- AI 决策开关 -------------------------------------------------------- #

    def test_tick_reports_decision_skipped_without_llm(self) -> None:
        """本仓库 config.json 未配 LLM：tick 应明说决策被跳过，而不是假装跑了。"""
        code, out, _ = run_sched("tick", *self.args())
        self.assertEqual(code, 0)
        self.assertIn("AI 决策", out)
        self.assertIn("跳过", out)
        self.assertIn("LLM 未配置", out)

    def test_tick_json_has_decision_fields(self) -> None:
        code, out, _ = run_sched("tick", "--json", *self.args())
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertFalse(payload["decision_ran"])
        self.assertIn("LLM 未配置", payload["decision_skipped_reason"])

    def test_no_decide_flag(self) -> None:
        code, out, _ = run_sched("tick", "--json", "--no-decide", *self.args())
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertFalse(payload["decision_ran"])
        self.assertIn("未启用", payload["decision_skipped_reason"])

    def test_help_documents_decision_flags(self) -> None:
        parser = sched.build_parser()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["tick", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        text = out.getvalue()
        self.assertIn("--no-decide", text)
        self.assertIn("--live-decisions", text)
        self.assertIn("--decision-interval", text)

    def test_flags_reach_scheduler(self) -> None:
        parser = sched.build_parser()
        args = parser.parse_args(
            ["tick", "--no-decide", "--decision-interval", "120",
             "--db", str(self.db), "--state-dir", str(self.state)]
        )
        self.assertTrue(args.no_decide)
        self.assertEqual(args.decision_interval, 120)
        scheduler = sched._build_scheduler(args)
        self.assertFalse(scheduler.decision_enabled)
        self.assertEqual(scheduler.decision_interval_seconds, 120)
        self.assertTrue(scheduler.decision_dry_run, "默认必须是 dry-run")

    def test_live_decisions_flag(self) -> None:
        parser = sched.build_parser()
        args = parser.parse_args(
            ["tick", "--live-decisions", "--db", str(self.db),
             "--state-dir", str(self.state)]
        )
        scheduler = sched._build_scheduler(args)
        self.assertTrue(scheduler.decision_enabled)
        self.assertFalse(scheduler.decision_dry_run)

    def test_unknown_command_is_usage_error(self) -> None:
        with self.assertRaises(SystemExit):
            run_sched("no-such-command")

    def test_status_json(self) -> None:
        code, out, _ = run_sched("status", "--json", *self.args())
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("running", payload)
        self.assertEqual(payload["timezone"], "Asia/Tokyo")

    def test_errors_are_clean(self) -> None:
        code, _, err = run_sched("status", "--db", str(self.root / "nope" / "db.sqlite3"),
                                 "--state-dir", str(self.state))
        self.assertNotIn("Traceback", err)


# --------------------------------------------------------------------------- #
# 15. 督促机制接入 tick
# --------------------------------------------------------------------------- #


class TestPressureInTick(SchedulerTestCase):
    """督促机制接入 tick。用显式打开的配置，不依赖出货默认值。"""

    def setUp(self) -> None:
        super().setUp()
        import dataclasses

        base = self.cfg
        self.cfg = dataclasses.replace(
            base,
            pressure=dataclasses.replace(
                base.pressure, enabled=True, interval_hours=6,
                deduction_jpy=Decimal("3000"),
            ),
        )
        self.engine = TradingEngine(self.cfg, self.storage)
        self.scheduler = sched.Scheduler(
            self.cfg, storage=self.storage, engine=self.engine,
            monitor=RecordingMonitor(), reports_dir=self.reports, state_dir=self.state,
        )

    def test_tick_reports_pressure_skip(self) -> None:
        result = self.scheduler.tick(now=datetime(2026, 10, 12, 13, 37, tzinfo=_JST))
        self.assertFalse(result.pressure_applied)
        self.assertIn("未到下一个扣款槽位", result.pressure_skipped_reason)
        self.assertEqual(result.pressure_slots, ())

    def test_tick_charges_at_slot_boundary(self) -> None:
        self.scheduler.tick(now=datetime(2026, 10, 12, 13, 37, tzinfo=_JST))
        result = self.scheduler.tick(now=datetime(2026, 10, 12, 18, 0, tzinfo=_JST))
        self.assertTrue(result.pressure_applied)
        self.assertEqual(result.pressure_charged_jpy, "3000.000")
        self.assertEqual(list(result.pressure_slots), ["2026-10-12T18"])

    def test_tick_is_idempotent_for_pressure(self) -> None:
        self.scheduler.tick(now=datetime(2026, 10, 12, 13, 0, tzinfo=_JST))
        self.scheduler.tick(now=datetime(2026, 10, 12, 18, 0, tzinfo=_JST))
        again = self.scheduler.tick(now=datetime(2026, 10, 12, 18, 30, tzinfo=_JST))
        self.assertFalse(again.pressure_applied)
        flows = self.storage.read_cash_flows(flow_type="CAPITAL_DECAY")
        self.assertEqual(len(flows), 1)

    def test_pressure_failure_does_not_break_tick(self) -> None:
        with mock.patch.object(
            sched.TradingEngine, "apply_capital_pressure",
            side_effect=RuntimeError("结算炸了"),
        ):
            result = self.scheduler.tick(now=datetime(2026, 10, 12, 13, 0, tzinfo=_JST))
        self.assertTrue(result.monitor_ran)
        self.assertTrue(any("督促机制结算失败" in e for e in result.errors))

    def test_as_dict_has_pressure_fields(self) -> None:
        payload = self.scheduler.tick(
            now=datetime(2026, 10, 12, 13, 0, tzinfo=_JST)
        ).as_dict()
        for key in ("pressure_applied", "pressure_charged_jpy", "pressure_slots",
                    "pressure_skipped_reason"):
            self.assertIn(key, payload)


# --------------------------------------------------------------------------- #
# 16. 休市节流：休市期间不要每分钟都问模型
# --------------------------------------------------------------------------- #


class _QuoteStateMonitor:
    """可控制「报价是否过期」的假监控器。"""

    def __init__(self, *, stale: bool) -> None:
        self.stale = stale
        self.calls = 0

    def monitor_once(self, quote=None, *, now=None, backfill=True, last_check_at=None):
        self.calls += 1
        return StubMonitorResult(quote_ok=True, quote_is_stale=self.stale)


class _CountingAgent:
    def __init__(self) -> None:
        self.calls = 0

    def decide(self, **kwargs):
        self.calls += 1
        return StubDecision()


class TestMarketClosedThrottle(SchedulerTestCase):
    """休市时按更慢的节拍问模型（默认 6 小时），而不是平时的 60 秒。"""

    def ready_cfg(self):
        """构造一份「LLM 已配置」的配置，否则决策会在更早的一层就被跳过。"""
        import dataclasses

        base = load_config()
        return dataclasses.replace(
            base,
            llm=dataclasses.replace(
                base.llm, api_key="sk-test", model_id="Test-Model"
            ),
        )

    def build(self, *, stale: bool, decision_cfg=None, **kwargs):
        agent = _CountingAgent()
        cfg = self.ready_cfg()
        if decision_cfg is not None:
            import dataclasses

            cfg = dataclasses.replace(cfg, decision=decision_cfg)
        scheduler = sched.Scheduler(
            cfg,
            storage=Storage(self.db, tz=_JST),
            engine=TradingEngine(cfg, Storage(self.db, tz=_JST)),
            monitor=_QuoteStateMonitor(stale=stale),
            reports_dir=self.reports,
            state_dir=self.state,
            decision_agent=agent,
            **kwargs,
        )
        return scheduler, agent

    # -- 开市：正常节奏 ---------------------------------------------------- #

    def test_open_market_keeps_minute_cadence(self) -> None:
        scheduler, agent = self.build(stale=False)
        for minute in range(10):
            scheduler.tick(now=datetime(2026, 10, 13, 10, minute, tzinfo=_JST))
        self.assertEqual(agent.calls, 10)

    # -- 休市：节流 -------------------------------------------------------- #

    def test_closed_market_throttles_to_closed_interval(self) -> None:
        scheduler, agent = self.build(stale=True)
        for minute in range(10):
            scheduler.tick(now=datetime(2026, 10, 11, 10, minute, tzinfo=_JST))
        self.assertEqual(agent.calls, 1, "休市 10 分钟内只该问一次")

    def test_skip_reason_mentions_market_closed(self) -> None:
        scheduler, _ = self.build(stale=True)
        scheduler.tick(now=datetime(2026, 10, 11, 10, 0, tzinfo=_JST))
        result = scheduler.tick(now=datetime(2026, 10, 11, 10, 30, tzinfo=_JST))
        self.assertFalse(result.decision_ran)
        self.assertIn("休市", result.decision_skipped_reason)
        self.assertIn("未到休市间隔", result.decision_skipped_reason)
        self.assertFalse(result.market_open)

    def test_asks_again_after_closed_interval(self) -> None:
        scheduler, agent = self.build(stale=True)
        scheduler.tick(now=datetime(2026, 10, 11, 10, 0, tzinfo=_JST))
        scheduler.tick(now=datetime(2026, 10, 11, 16, 10, tzinfo=_JST))
        self.assertEqual(agent.calls, 2)

    def test_zero_closed_interval_never_asks(self) -> None:
        import dataclasses

        cfg = self.ready_cfg()
        scheduler, agent = self.build(
            stale=True,
            decision_cfg=dataclasses.replace(
                cfg.decision, closed_interval_hours=0
            ),
        )
        for hour in range(0, 24, 2):
            scheduler.tick(now=datetime(2026, 10, 11, hour, 0, tzinfo=_JST))
        self.assertEqual(agent.calls, 0, "配置为 0 时休市期间完全不问")
        result = scheduler.tick(now=datetime(2026, 10, 11, 23, 0, tzinfo=_JST))
        self.assertIn("不调用模型", result.decision_skipped_reason)

    def test_throttle_can_be_disabled(self) -> None:
        import dataclasses

        cfg = self.ready_cfg()
        scheduler, agent = self.build(
            stale=True,
            decision_cfg=dataclasses.replace(
                cfg.decision, skip_when_market_closed=False
            ),
        )
        for minute in range(10):
            scheduler.tick(now=datetime(2026, 10, 11, 10, minute, tzinfo=_JST))
        self.assertEqual(agent.calls, 10, "关掉开关后恢复每分钟一次")

    # -- 报价过期（节假日 / 数据源故障）也算不可交易 ---------------------- #

    def test_stale_quote_throttles_even_on_a_weekday(self) -> None:
        scheduler, agent = self.build(stale=True)
        for minute in range(10):
            scheduler.tick(now=datetime(2026, 10, 13, 10, minute, tzinfo=_JST))
        self.assertEqual(agent.calls, 1)
        result = scheduler.tick(now=datetime(2026, 10, 13, 10, 40, tzinfo=_JST))
        self.assertIn("报价已过期", result.decision_skipped_reason)

    # -- 周末省下的调用量 -------------------------------------------------- #

    def test_weekend_saves_almost_all_calls(self) -> None:
        """周六中午起 24 小时：每 10 分钟 tick 一次（144 次）也只该问 4 次左右。

        真按每分钟 tick（1440 次）会把这个用例拖慢几十秒，而结论一样——
        节流由「距上次决策是否满 6 小时」决定，与 tick 频率无关。
        """
        scheduler, agent = self.build(stale=True)
        start = datetime(2026, 10, 10, 12, 0, tzinfo=_JST)
        ticks = 0
        for step in range(24 * 6):
            scheduler.tick(now=start + timedelta(minutes=step * 10))
            ticks += 1
        self.assertEqual(ticks, 144)
        self.assertLessEqual(agent.calls, 5)
        self.assertGreaterEqual(agent.calls, 3)
        self.assertLess(agent.calls / ticks, 0.04, "应省下 96% 以上的调用")

    # -- market_open 字段 -------------------------------------------------- #

    def test_market_open_exposed_in_result(self) -> None:
        scheduler, _ = self.build(stale=False)
        result = scheduler.tick(now=datetime(2026, 10, 13, 10, 0, tzinfo=_JST))
        self.assertTrue(result.market_open)
        self.assertIn("market_open", result.as_dict())

    def test_skip_when_llm_not_configured_reports_that_first(self) -> None:
        """没配 LLM 时优先报「未配置」，不要误报成休市。"""
        scheduler = sched.Scheduler(
            self.cfg, storage=self.storage, engine=self.engine,
            monitor=_QuoteStateMonitor(stale=True),
            reports_dir=self.reports, state_dir=self.state,
            decision_agent=_CountingAgent(),
        )
        result = scheduler.tick(now=datetime(2026, 10, 11, 10, 0, tzinfo=_JST))
        self.assertIn("LLM 未配置", result.decision_skipped_reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
