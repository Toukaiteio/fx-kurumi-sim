"""FX 战士久留美 —— 任务调度：风险监控进程与报告任务触发记录。

依赖 :mod:`risk_monitor`、:mod:`reporting`、:mod:`storage`、:mod:`config`，
不重复实现其中任何一项。

本模块做什么
============

1. 按 ``config.json`` 的 ``market.monitor_interval_seconds``（默认 60 秒）持续执行
   风险监控；
2. 全部时间以 ``Asia/Tokyo`` 计算；
3. 每天 **08:00 与 20:00 JST 各创建一次报告任务**（早间 / 晚间报告）；
4. 每个任务都有唯一编号（``scheduler_runs.run_no``），报告任务另有一个基于
   ``(种类, 时点)`` 的幂等键，靠数据库 UNIQUE 约束保证**重复唤醒不会重复生成**；
5. 进程重启后从数据库（账本 + ``scheduler_runs``）恢复状态：遗留的 ``RUNNING``
   运行被标记为 ``ABORTED``，中断期间的行情由监控器补查历史 K 线；
6. 重大风险事件发生后保存**事件 + 账户快照**；
7. 每次任务都记录开始时间、结束时间、执行结果与错误信息；
8. 任务异常、网络故障、程序退出都不会让调度器整体崩掉。

重要限制：调度器**不能凭空唤醒语言模型**
========================================

独立的 Python 脚本无法唤起 LLM。因此默认行为是：报告时点**生成报告任务与数据包**
（JSON 快照 + Markdown 模板 + 交接文件 ``reports/tasks/<task_id>.json``），
由 Agent 自己去读取并撰写角色报告。

只有在平台确实提供**可验证的 Agent 调度接口**时才应接入；此时由该接口在时点唤醒
Agent，Agent 读取上面那些文件即可。本模块**不假装**已经接入了任何 LLM。

本机环境实测结论（2026-10-10）
==============================

================================================= ==============================
能力                                               实测结果
================================================= ==============================
分离后台进程（``pythonw`` + ``DETACHED_PROCESS``）    **可用**：父进程退出后子进程继续
                                                   运行并在 8 秒后写出文件
Windows 任务计划程序 ``schtasks``                   可用（本模块**未**自动注册任何任务）
平台 Agent 调度接口（``CronCreate`` / ``CronList``）  可用，但**仅 Agent 侧**，Python
                                                   脚本无法调用
``systemd-run`` / ``nssm`` / ``winsw`` / ``cron``    均不存在
================================================= ==============================

因此本模块提供两种**已在本机验证过**的驱动方式：

* **A. 常驻进程**：``scheduler start`` 用分离进程在后台跑循环，``stop`` / ``restart`` /
  ``status`` 通过 PID 文件与停止标记文件管理（优雅退出，超时后才强制结束）；
* **B. 外部定时驱动**：由 Windows 任务计划程序（或平台的 Agent 调度、或人工）
  每分钟调用一次 ``scheduler tick``。``tick`` 是**幂等**的：到点才监控、到点才建
  报告任务，因此「每分钟唤醒」不会产生重复任务或重复交易。

两种方式可以互换，状态都在数据库里，切换不会丢状态。

不做什么
========

* **不绕过引擎下单**：AI 决策走 :mod:`agent` → :mod:`engine`，与人工下单同一条路径，
  由引擎重新校验每一条规则。调度器自己不算盈亏、不写余额。
* **默认不下单**：AI 决策默认 **dry-run**（只校验并记录），要真正下单必须显式加
  ``--live-decisions``。没有配 LLM（``llm.api_key`` / ``llm.model_id`` 为空）时
  决策自动跳过，只做监控与报告。
* **不修改账户余额**：余额只由 :mod:`engine` 依据账本变更。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Final, Sequence

from config import Config, load_config
from engine import EngineError, TradingEngine
from market_data import MarketDataError
from reporting import (
    ReportKind,
    build_report_data,
    export_snapshot,
    generate_report_template,
    latest_report_slot,
    report_period_for,
)
from market_data import forex_session_open
from risk_monitor import TASK_NAME as MONITOR_TASK_NAME, RiskEventKind, RiskMonitor
from storage import (
    DuplicateEventError,
    RunStatus,
    SchedulerRun,
    Storage,
    StorageError,
)

__all__ = [
    "CAPABILITIES",
    "MAJOR_EVENT_KINDS",
    "MONITOR_TASK_NAME",
    "REPORT_TASK_PREFIX",
    "ReportTask",
    "Scheduler",
    "SchedulerStatus",
    "TickResult",
    "capabilities_report",
]

logger = logging.getLogger("kurumi_fx_sim.scheduler")

#: 报告任务在 ``scheduler_runs`` 里的任务名前缀。
REPORT_TASK_PREFIX: Final[str] = "report"

#: AI 决策任务在 ``scheduler_runs`` 里的任务名。
DECISION_TASK_NAME: Final[str] = "ai_decision"

#: AI 决策的默认间隔（秒）。与监控同频：每分钟问模型一次。
DEFAULT_DECISION_INTERVAL_SECONDS: Final[int] = 60

#: 视为「重大风险事件」、需要额外保存事件 + 账户快照的类型。
MAJOR_EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {
        RiskEventKind.STOP_LOSS.value,
        RiskEventKind.TAKE_PROFIT.value,
        RiskEventKind.MARGIN_CALL.value,
        RiskEventKind.DAILY_DRAWDOWN.value,
        RiskEventKind.BANKRUPTCY.value,
        RiskEventKind.MARGIN_WARNING.value,
        RiskEventKind.MIN_EQUITY_HALT.value,
    }
)

#: 启动时最多回溯创建几个报告时点的任务（默认 2 个 ≈ 24 小时）。
DEFAULT_CATCH_UP_SLOTS: Final[int] = 2

#: 优雅停止的等待上限（秒），超时才强制结束进程。
STOP_TIMEOUT_SECONDS: Final[float] = 20.0

#: 本机能力实测结论（供 ``capabilities`` 命令输出，也可被文档引用）。
CAPABILITIES: Final[tuple[dict[str, str], ...]] = (
    {
        "capability": "分离后台进程（pythonw + DETACHED_PROCESS）",
        "status": "verified",
        "detail": "实测：父进程退出后子进程继续运行并写出文件；scheduler start 基于此实现",
    },
    {
        "capability": "Windows 任务计划程序 schtasks",
        "status": "available",
        "detail": "命令可用；本模块不会自动注册任务，install-task 只打印命令，加 --apply 才注册",
    },
    {
        "capability": "平台 Agent 调度接口（CronCreate / CronList）",
        "status": "agent_side_only",
        "detail": "可用，但只有 Agent 能调用，Python 脚本无法接入；需要时由平台在时点唤醒 Agent",
    },
    {
        "capability": "systemd-run / nssm / winsw / cron / crontab",
        "status": "absent",
        "detail": "本机均不存在，因此不提供基于它们的启动方式",
    },
    {
        "capability": "脚本唤醒语言模型",
        "status": "not_possible",
        "detail": "独立 Python 脚本无法唤起 LLM；本模块只生成报告任务与数据包，由 Agent 读取",
    },
)


def capabilities_report() -> list[dict[str, str]]:
    """返回本机调度能力的实测结论（只读）。"""
    return [dict(item) for item in CAPABILITIES]


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReportTask:
    """一份待 Agent 处理的报告任务。"""

    task_id: str
    kind: str
    slot: datetime
    event_key: str
    period_start: datetime
    period_end: datetime
    snapshot_path: str
    template_path: str
    handoff_path: str
    created_at: datetime
    status: str
    agent_instruction: str
    run_no: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "slot": self.slot.isoformat(timespec="seconds"),
            "event_key": self.event_key,
            "period_start": self.period_start.isoformat(timespec="seconds"),
            "period_end": self.period_end.isoformat(timespec="seconds"),
            "snapshot_path": self.snapshot_path,
            "template_path": self.template_path,
            "handoff_path": self.handoff_path,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "status": self.status,
            "agent_instruction": self.agent_instruction,
            "run_no": self.run_no,
        }


@dataclass(frozen=True, slots=True)
class TickResult:
    """一次 ``tick`` 的结果。"""

    started_at: datetime
    finished_at: datetime
    monitor_ran: bool
    monitor_skipped_reason: str | None
    monitor_run_no: int | None
    monitor_status: str | None
    equity: str | None
    margin_level: str | None
    blocked_new_positions: bool
    risk_event_count: int
    closed_count: int
    report_tasks_created: tuple[ReportTask, ...]
    report_tasks_skipped: tuple[str, ...]
    event_snapshots: tuple[str, ...]
    decision_ran: bool = False
    decision_skipped_reason: str | None = None
    decision_outcome: str | None = None
    decision_message: str | None = None
    decision_run_no: int | None = None
    decision_status: str | None = None
    pressure_applied: bool = False
    pressure_charged_jpy: str | None = None
    pressure_slots: tuple[str, ...] = ()
    pressure_skipped_reason: str | None = None
    goal_achieved: bool = False
    goal_level: int | None = None
    goal_target_jpy: str | None = None
    goal_reward_paid_jpy: str | None = None
    goal_next_target_jpy: str | None = None
    goal_progress_ratio: str | None = None
    market_open: bool | None = None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds"),
            "monitor_ran": self.monitor_ran,
            "monitor_skipped_reason": self.monitor_skipped_reason,
            "monitor_run_no": self.monitor_run_no,
            "monitor_status": self.monitor_status,
            "equity": self.equity,
            "margin_level": self.margin_level,
            "blocked_new_positions": self.blocked_new_positions,
            "risk_event_count": self.risk_event_count,
            "closed_count": self.closed_count,
            "report_tasks_created": [t.as_dict() for t in self.report_tasks_created],
            "report_tasks_skipped": list(self.report_tasks_skipped),
            "event_snapshots": list(self.event_snapshots),
            "decision_ran": self.decision_ran,
            "decision_skipped_reason": self.decision_skipped_reason,
            "decision_outcome": self.decision_outcome,
            "decision_message": self.decision_message,
            "decision_run_no": self.decision_run_no,
            "decision_status": self.decision_status,
            "pressure_applied": self.pressure_applied,
            "pressure_charged_jpy": self.pressure_charged_jpy,
            "pressure_slots": list(self.pressure_slots),
            "pressure_skipped_reason": self.pressure_skipped_reason,
            "goal_achieved": self.goal_achieved,
            "goal_level": self.goal_level,
            "goal_target_jpy": self.goal_target_jpy,
            "goal_reward_paid_jpy": self.goal_reward_paid_jpy,
            "goal_next_target_jpy": self.goal_next_target_jpy,
            "goal_progress_ratio": self.goal_progress_ratio,
            "market_open": self.market_open,
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    """调度器运行状态。"""

    running: bool
    pid: int | None
    pid_alive: bool
    stop_requested: bool
    timezone: str
    interval_seconds: int
    database: str
    state_dir: str
    last_monitor_run: SchedulerRun | None
    last_report_run: SchedulerRun | None
    recent_runs: tuple[SchedulerRun, ...]
    pending_report_tasks: tuple[ReportTask, ...]
    next_report_slots: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "pid": self.pid,
            "pid_alive": self.pid_alive,
            "stop_requested": self.stop_requested,
            "timezone": self.timezone,
            "interval_seconds": self.interval_seconds,
            "database": self.database,
            "state_dir": self.state_dir,
            "last_monitor_run": (
                self.last_monitor_run.as_dict() if self.last_monitor_run else None
            ),
            "last_report_run": (
                self.last_report_run.as_dict() if self.last_report_run else None
            ),
            "recent_runs": [run.as_dict() for run in self.recent_runs],
            "pending_report_tasks": [t.as_dict() for t in self.pending_report_tasks],
            "next_report_slots": [list(item) for item in self.next_report_slots],
        }


# --------------------------------------------------------------------------- #
# 进程存活检查（Windows 与 POSIX 分开处理）
# --------------------------------------------------------------------------- #


def _pid_alive(pid: int) -> bool:
    """判断进程是否仍在运行。

    注意：Windows 上 ``os.kill(pid, 0)`` 会**终止**目标进程，因此绝不能那样用。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return int(code.value) == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# 调度器
# --------------------------------------------------------------------------- #


class Scheduler:
    """风险监控与报告任务调度器。

    核心是**幂等**的 :meth:`tick`：无论被调用多少次，到点才监控、到点才建报告任务，
    因此既能常驻循环驱动，也能由外部定时器每分钟驱动。
    """

    def __init__(
        self,
        cfg: Config | None = None,
        *,
        storage: Storage | None = None,
        engine: TradingEngine | None = None,
        monitor: RiskMonitor | None = None,
        reports_dir: str | Path | None = None,
        state_dir: str | Path | None = None,
        catch_up_slots: int = DEFAULT_CATCH_UP_SLOTS,
        quote_source: Callable[[], Any] | None = None,
        bars_source: Callable[[datetime, datetime], Any] | None = None,
        logger_: logging.Logger | None = None,
        decision_interval_seconds: int | None = None,
        decision_enabled: bool = True,
        decision_dry_run: bool = True,
        decision_agent: Any | None = None,
    ) -> None:
        self.cfg: Config = cfg or load_config()
        self.storage: Storage = storage or Storage.from_config(self.cfg)
        self.engine: TradingEngine = engine or TradingEngine(self.cfg, self.storage)
        self.logger = logger_ or logger
        self.reports_dir = (
            Path(reports_dir) if reports_dir is not None else self.cfg.paths.reports_dir
        )
        self.state_dir = (
            Path(state_dir)
            if state_dir is not None
            else self.cfg.paths.data_dir / "scheduler"
        )
        self.tasks_dir = self.reports_dir / "tasks"
        self.events_dir = self.reports_dir / "events"
        self.catch_up_slots = max(1, int(catch_up_slots))
        self.decision_interval_seconds = int(
            decision_interval_seconds or DEFAULT_DECISION_INTERVAL_SECONDS
        )
        self.decision_enabled = decision_enabled
        #: 默认 dry-run：调度器自动决策**不会真的开仓**，除非显式关掉。
        self.decision_dry_run = decision_dry_run
        #: 可注入的 TradingAgent（测试用）；为空时按需惰性构造。
        self._decision_agent = decision_agent
        #: 最近一次监控看到的「报价能否用于决策」；None 表示本进程还没监控过。
        self._last_quote_state: tuple[bool, str] | None = None
        self.monitor = monitor or RiskMonitor(
            self.cfg,
            self.engine,
            storage=self.storage,
            quote_source=quote_source,
            bars_source=bars_source,
            logger_=self.logger,
        )
        self._recovered = False

    # -- 路径 -------------------------------------------------------------- #

    @property
    def pid_file(self) -> Path:
        return self.state_dir / "scheduler.pid"

    @property
    def stop_file(self) -> Path:
        return self.state_dir / "scheduler.stop"

    @property
    def log_file(self) -> Path:
        return self.state_dir / "scheduler.log"

    @property
    def interval_seconds(self) -> int:
        return int(self.cfg.market.monitor_interval_seconds)

    # -- 状态恢复 ---------------------------------------------------------- #

    def recover(self) -> int:
        """启动恢复：把上次进程遗留的 ``RUNNING`` 运行标记为 ``ABORTED``。

        监控状态本身从数据库恢复——监控器会读 ``scheduler_runs`` 里的上次运行时刻，
        据此补查中断期间的 1 分钟 K 线，因此重启不会漏掉期间的止损/止盈。
        """
        count = self.storage.recover_aborted_runs()
        self._recovered = True
        if count:
            self.logger.warning("恢复：把 %d 条遗留的 RUNNING 运行标记为 ABORTED", count)
        return count

    def last_monitor_run(self) -> SchedulerRun | None:
        runs = self.storage.read_scheduler_runs(task_name=MONITOR_TASK_NAME, limit=1)
        return runs[0] if runs else None

    # -- AI 决策任务 -------------------------------------------------------- #

    def last_decision_run(self) -> SchedulerRun | None:
        runs = self.storage.read_scheduler_runs(task_name=DECISION_TASK_NAME, limit=1)
        return runs[0] if runs else None

    def market_tradeable(self, now: datetime) -> tuple[bool, str]:
        """当前是否处于「可交易」状态。返回 ``(可交易, 说明)``。

        两级判断，从便宜到权威：

        1. **周内时段**（免费、纯计算）：休市窗口是周六 07:00 → 周一 06:00 JST，
           这是 EST/EDT 两种情况的交集，只会漏判不会误判；
        2. **报价可用性**（权威）：最近一次监控看到的报价能不能用于决策。
           它自动覆盖节假日、数据源故障等一切「拿不到可交易报价」的情况。

        两级都只是**判断能不能决策**，不参与任何资金逻辑——开仓的最终闸门永远在
        :meth:`engine.TradingEngine.validate_trade_intent`。
        """
        opened, reason = forex_session_open(now, tz=self.cfg.tz)
        if not opened:
            return False, reason
        if self._last_quote_state is not None:
            ok, why = self._last_quote_state
            if not ok:
                return False, why
        return True, "可交易"

    def decision_plan(
        self, now: datetime
    ) -> tuple[bool, str | None, bool | None]:
        """是否该跑一次 AI 决策。返回 ``(是否执行, 跳过原因, 是否开市)``。

        **休市期间按另一套更慢的节拍**（``decision.closed_interval_hours``，默认 6 小时），
        而不是平时的 60 秒。原因：休市时报价必然过期、规则 6 不允许开仓，每次调用都
        只会换来一个 HOLD，却要付完整的提示词 token（而且按小时重放会让提示词越来越大）。
        实测一个周末能省下 **99% 以上**的决策调用。

        ``closed_interval_hours = 0`` 表示休市期间完全不问模型。
        """
        # 先算「能不能交易」——它是纯计算（外加一个缓存的状态），免费，
        # 而且即使不跑决策也该报出去：运维需要知道「现在是不是休市」。
        tradeable, market_reason = self.market_tradeable(now)

        if not self.decision_enabled:
            return False, "未启用", tradeable
        if not self.cfg.llm.ready:
            return False, "LLM 未配置（需要 llm.api_key 与 llm.model_id）", tradeable

        if not tradeable and self.cfg.decision.skip_when_market_closed:
            limit_hours = self.cfg.decision.closed_interval_hours
            if limit_hours <= 0:
                return False, f"{market_reason}；配置为休市期间不调用模型", tradeable
            last = self.last_decision_run()
            if last is not None:
                elapsed = (now - last.started_at).total_seconds()
                if elapsed < limit_hours * 3600:
                    return (
                        False,
                        f"{market_reason}；距上次决策 {elapsed / 3600:.1f} 小时，"
                        f"未到休市间隔 {limit_hours} 小时",
                        tradeable,
                    )
            return True, None, tradeable

        due, reason = self.decision_due(now)
        if not due:
            return False, reason, tradeable
        return True, None, tradeable

    def decision_due(self, now: datetime) -> tuple[bool, str | None]:
        """决策是否到点。

        判定只看**上一次运行是否已启动**，不看成功与否——所以一次失败之后
        自然就是「再等一个间隔」，不需要额外的重试或退避逻辑。
        """
        last = self.last_decision_run()
        if last is None:
            return True, None
        elapsed = (now - last.started_at).total_seconds()
        if elapsed >= self.decision_interval_seconds:
            return True, None
        return False, (
            f"距上次决策仅 {elapsed:.0f} 秒，未到间隔 {self.decision_interval_seconds} 秒"
        )

    def decision_agent(self) -> Any:
        """按需构造 TradingAgent（惰性导入，避免无 LLM 时也加载这一层）。"""
        if self._decision_agent is None:
            from agent import TradingAgent

            self._decision_agent = TradingAgent(
                self.cfg, engine=self.engine, storage=self.storage
            )
        return self._decision_agent

    def run_decision_task(
        self, *, now: datetime | None = None, quote: Any | None = None
    ) -> tuple[Any | None, str | None]:
        """跑一次 AI 决策，并把结果写进 ``scheduler_runs``。

        返回 ``(DecisionResult | None, 跳过原因)``。**任何异常都不外抛**：
        记进运行记录后等下一个间隔再试。
        """
        moment = now or datetime.now(self.cfg.tz)
        # 决策键按分钟取整：同一分钟内重复 tick 会得到同一个 intent_id，
        # 引擎据此判定为重复指令，不会重复开仓。
        decision_id = f"tick-{moment.strftime('%Y%m%dT%H%M')}"
        run = self.storage.start_scheduler_run(DECISION_TASK_NAME, started_at=moment)
        try:
            result = self.decision_agent().decide(
                quote=quote,
                decision_id=decision_id,
                dry_run=self.decision_dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - 决策失败不应终止调度循环
            message = f"{type(exc).__name__}: {exc}"
            self.logger.exception("AI 决策任务失败")
            self.storage.finish_scheduler_run(
                run.run_no, status=RunStatus.FAILED, error=message
            )
            return None, message
        status = RunStatus.SUCCESS if result.ok else RunStatus.FAILED
        self.storage.finish_scheduler_run(
            run.run_no,
            status=status,
            error=None if result.ok else (result.message[:400] or None),
            detail=(
                f"{result.outcome}（{'dry-run' if result.dry_run else 'live'}）"
                f" 轮数 {result.rounds}：{result.message[:160]}"
            ),
        )
        self.logger.info("AI 决策：%s — %s", result.outcome, result.message[:120])
        return result, None

    # -- 报告时点 ---------------------------------------------------------- #

    def due_report_slots(self, now: datetime) -> list[tuple[ReportKind, datetime]]:
        """返回应当处理的报告时点（含回溯窗口内的历史时点）。

        ``latest_report_slot`` 给出「不晚于 now 的最近时点」，再往前推即可枚举
        最近若干个时点；08:00 与 20:00 各一个，全部按 ``Asia/Tokyo`` 计算。
        """
        slots: list[tuple[ReportKind, datetime]] = []
        cursor = now
        for _ in range(self.catch_up_slots):
            kind, slot = latest_report_slot(cursor, self.cfg.tz)
            slots.append((kind, slot))
            cursor = slot - timedelta(seconds=1)
        return slots

    @staticmethod
    def report_event_key(kind: ReportKind, slot: datetime) -> str:
        """报告任务的幂等键：同一时点同一类型只会有一个任务。"""
        return f"{REPORT_TASK_PREFIX}:{kind.value}:{slot.isoformat(timespec='minutes')}"

    def next_report_slots(self, now: datetime | None = None) -> list[tuple[str, str]]:
        """接下来的两个报告时点（08:00 / 20:00 JST）。"""
        moment = now or datetime.now(self.cfg.tz)
        upcoming: list[tuple[str, str]] = []
        for kind, hour in ((ReportKind.MORNING, 8), (ReportKind.EVENING, 20)):
            candidate = moment.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate <= moment:
                candidate += timedelta(days=1)
            upcoming.append((kind.value, candidate.isoformat(timespec="seconds")))
        upcoming.sort(key=lambda item: item[1])
        return upcoming

    # -- 报告任务 ---------------------------------------------------------- #

    def _agent_instruction(self, task_id: str, kind: ReportKind) -> str:
        return (
            f"报告任务 {task_id}（{kind.label}）已生成数据包，等待 Agent 处理。"
            "调度器不会也不能唤醒语言模型。请由 Agent（或平台的 Agent 调度接口）"
            "读取 snapshot_path 里的 JSON 数据包，把「久留美的市场分析」与"
            "「久留美的交易日记」两节写入 template_path 对应的 Markdown 文件，"
            "只使用数据包中出现过的事实，不得编造。"
            "完成后可执行 `python -m scheduler complete-task "
            f"{task_id}` 标记完成。"
        )

    def create_report_task(
        self,
        kind: ReportKind,
        slot: datetime,
        *,
        now: datetime | None = None,
        quote: Any | None = None,
    ) -> ReportTask | None:
        """创建一次报告任务。

        * 同一时点重复调用返回 ``None``（不重复生成）——幂等性由
          ``scheduler_runs.event_key`` 的 UNIQUE 约束在**数据库层**保证，
          因此并发或多重唤醒都不会重复；
        * 创建过程出错时，先把任务运行记录标为 ``FAILED`` 并写入错误信息，
          然后**上抛异常**，让调用方能把它与「已存在」区分开（否则失败会被
          误报成「跳过」）。
        """
        moment = now or datetime.now(self.cfg.tz)
        event_key = self.report_event_key(kind, slot)
        task_name = f"{REPORT_TASK_PREFIX}:{kind.value}"
        try:
            run = self.storage.start_scheduler_run(
                task_name, event_key=event_key, started_at=moment, on_duplicate="ERROR"
            )
        except DuplicateEventError:
            self.logger.info("报告任务已存在，跳过：%s", event_key)
            return None

        errors: list[str] = []
        try:
            data = build_report_data(
                kind,
                cfg=self.cfg,
                engine=self.engine,
                storage=self.storage,
                quote=quote,
                skip_quote=quote is None,
                report_at=slot,
                period_start=report_period_for(kind, slot)[0],
                period_end=slot,
            )
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            snapshot_path = export_snapshot(data, out_dir=self.reports_dir, cfg=self.cfg)
            template_path = snapshot_path.with_suffix(".md")
            template_path.write_text(generate_report_template(data), encoding="utf-8")

            task_id = f"{kind.value.lower()}-{slot.strftime('%Y%m%dT%H%M%S%z')}"
            self.tasks_dir.mkdir(parents=True, exist_ok=True)
            handoff_path = self.tasks_dir / f"{task_id}.json"
            task = ReportTask(
                task_id=task_id,
                kind=kind.value,
                slot=slot,
                event_key=event_key,
                period_start=data.period.start,
                period_end=data.period.end,
                snapshot_path=str(snapshot_path),
                template_path=str(template_path),
                handoff_path=str(handoff_path),
                created_at=moment,
                status="PENDING",
                agent_instruction=self._agent_instruction(task_id, kind),
                run_no=run.run_no,
            )
            handoff_path.write_text(
                json.dumps(task.as_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - 单个任务失败不应影响调度循环
            message = f"{type(exc).__name__}: {exc}"
            errors.append(message)
            self.logger.exception("创建报告任务失败：%s", event_key)
            self.storage.finish_scheduler_run(
                run.run_no, status=RunStatus.FAILED, error=message
            )
            # 上抛：调用方需要把「创建失败」与「已存在」区分开
            raise

        self.storage.finish_scheduler_run(
            run.run_no,
            status=RunStatus.SUCCESS,
            detail=f"{kind.label} 报告任务 {task_id}；快照 {snapshot_path.name}",
        )
        self.logger.info("已创建报告任务 %s（快照 %s）", task_id, snapshot_path.name)
        return task

    def list_report_tasks(self, *, limit: int | None = None) -> list[ReportTask]:
        """列出已生成的报告任务（读交接文件，按创建时间升序）。"""
        if not self.tasks_dir.is_dir():
            return []
        tasks: list[ReportTask] = []
        for path in sorted(self.tasks_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.logger.warning("跳过无法解析的交接文件：%s", path)
                continue
            tasks.append(
                ReportTask(
                    task_id=str(payload["task_id"]),
                    kind=str(payload["kind"]),
                    slot=datetime.fromisoformat(str(payload["slot"])),
                    event_key=str(payload["event_key"]),
                    period_start=datetime.fromisoformat(str(payload["period_start"])),
                    period_end=datetime.fromisoformat(str(payload["period_end"])),
                    snapshot_path=str(payload["snapshot_path"]),
                    template_path=str(payload["template_path"]),
                    handoff_path=str(path),
                    created_at=datetime.fromisoformat(str(payload["created_at"])),
                    status=str(payload.get("status", "PENDING")),
                    agent_instruction=str(payload.get("agent_instruction", "")),
                    run_no=payload.get("run_no"),
                )
            )
        tasks.sort(key=lambda item: item.created_at)
        return tasks if limit is None else tasks[-limit:]

    def complete_report_task(self, task_id: str, *, note: str | None = None) -> ReportTask:
        """把报告任务标记为已完成（Agent 写完角色报告后调用）。"""
        path = self.tasks_dir / f"{task_id}.json"
        if not path.is_file():
            raise EngineError(f"报告任务不存在: {task_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "COMPLETED"
        payload["completed_at"] = datetime.now(self.cfg.tz).isoformat(timespec="seconds")
        if note:
            payload["completion_note"] = note
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return next(t for t in self.list_report_tasks() if t.task_id == task_id)

    # -- 事件快照 ---------------------------------------------------------- #

    def save_event_snapshot(
        self, event: Any, *, now: datetime | None = None, quote: Any | None = None
    ) -> Path | None:
        """在重大风险事件后保存「事件 + 账户快照」。

        同一事件（同一个 ``event_key``）只保存一次：文件名由事件键哈希决定，
        已存在即跳过。
        """
        moment = now or datetime.now(self.cfg.tz)
        digest = hashlib.sha256(str(event.event_key).encode("utf-8")).hexdigest()[:12]
        self.events_dir.mkdir(parents=True, exist_ok=True)
        target = self.events_dir / f"event-{event.kind}-{digest}.json"
        if target.exists():
            self.logger.info("事件快照已存在，跳过：%s", target.name)
            return None
        try:
            data = build_report_data(
                None,
                cfg=self.cfg,
                engine=self.engine,
                storage=self.storage,
                quote=quote,
                skip_quote=quote is None,
                report_at=moment,
            )
            payload = {
                "saved_at": moment.isoformat(timespec="seconds"),
                "event": event.as_dict(),
                "account_snapshot": data.to_payload(),
            }
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 - 快照失败不应影响调度循环
            self.logger.exception("保存事件快照失败：%s", event.event_key)
            return None
        self.logger.warning("已保存重大事件快照：%s", target.name)
        return target

    # -- 核心：一次 tick --------------------------------------------------- #

    def monitor_due(self, now: datetime) -> tuple[bool, str | None]:
        """判断监控是否到点。返回 ``(是否执行, 跳过原因)``。"""
        last = self.last_monitor_run()
        if last is None:
            return True, None
        elapsed = (now - last.started_at).total_seconds()
        if elapsed >= self.interval_seconds:
            return True, None
        return False, (
            f"距上次监控仅 {elapsed:.1f} 秒，未到间隔 {self.interval_seconds} 秒"
        )

    def tick(
        self,
        *,
        now: datetime | None = None,
        quote: Any | None = None,
        force_monitor: bool = False,
    ) -> TickResult:
        """执行一次调度：到点则监控、到点则创建报告任务。

        **幂等**：重复调用不会产生重复的报告任务，也不会重复执行交易。
        """
        started = now or datetime.now(self.cfg.tz)
        if not self._recovered:
            self.recover()

        errors: list[str] = []
        report_tasks: list[ReportTask] = []
        skipped: list[str] = []
        event_snapshots: list[str] = []
        monitor_ran = False
        monitor_skip_reason: str | None = None
        monitor_run_no: int | None = None
        monitor_status: str | None = None
        equity: str | None = None
        margin_level: str | None = None
        blocked = False
        event_count = 0
        closed_count = 0

        # --- 0) 督促机制（在监控之前结算，好让随后的盯市立刻反映扣款后的净值）--- #
        pressure_applied = False
        pressure_charged: str | None = None
        pressure_slots: tuple[str, ...] = ()
        pressure_skipped: str | None = None
        try:
            pressure = self.engine.apply_capital_pressure(now=started)
        except Exception as exc:  # noqa: BLE001 - 扣款失败不应终止调度
            errors.append(f"督促机制结算失败：{type(exc).__name__}: {exc}")
            pressure_skipped = f"{type(exc).__name__}: {exc}"
            self.logger.exception("督促机制结算失败")
        else:
            pressure_applied = pressure.applied
            pressure_charged = str(pressure.charged_jpy)
            pressure_slots = pressure.slots
            pressure_skipped = pressure.skipped_reason

        # --- 1) 风险监控 --------------------------------------------------- #
        due, reason = self.monitor_due(started)
        if force_monitor:
            due, reason = True, None
        if not due:
            monitor_skip_reason = reason
        else:
            # 先读上次 tick 时刻，再写本次运行记录，否则补查窗口会被自己吃掉
            previous = self.last_monitor_run()
            last_check = previous.started_at if previous is not None else None
            run = self.storage.start_scheduler_run(
                MONITOR_TASK_NAME, started_at=started
            )
            monitor_run_no = run.run_no
            try:
                result = self.monitor.monitor_once(
                    quote=quote, now=started, last_check_at=last_check
                )
            except Exception as exc:  # noqa: BLE001 - 任务异常不应终止调度
                errors.append(f"风险监控失败：{type(exc).__name__}: {exc}")
                monitor_status = RunStatus.FAILED.value
                self.logger.exception("风险监控 tick 失败")
                self.storage.finish_scheduler_run(
                    run.run_no, status=RunStatus.FAILED, error=errors[-1]
                )
            else:
                monitor_ran = True
                monitor_status = RunStatus.SUCCESS.value
                event_count = len(result.events)
                closed_count = len(result.closed)
                equity = None if result.equity is None else str(result.equity)
                margin_level = (
                    None if result.margin_level is None else str(result.margin_level)
                )
                blocked = result.blocked_new_positions
                # 记下「报价能不能用于决策」——这是判断休市/不可交易的**权威信号**，
                # 由监控顺带得到，不需要额外联网，也不需要维护交易日历。
                if not result.quote_ok:
                    self._last_quote_state = (False, "本次监控没能取到报价")
                elif result.quote_is_stale:
                    self._last_quote_state = (False, "报价已过期（休市或数据源延迟）")
                else:
                    self._last_quote_state = (True, "报价可用于决策")
                self.storage.finish_scheduler_run(
                    run.run_no,
                    status=RunStatus.SUCCESS,
                    detail=(
                        f"事件 {event_count} 个，平仓 {closed_count} 笔，"
                        f"净值 {equity}，阻断新开仓 {'是' if blocked else '否'}"
                    ),
                )
                # --- 2) 重大事件快照 --------------------------------------- #
                for event in result.events:
                    if event.kind not in MAJOR_EVENT_KINDS:
                        continue
                    saved = self.save_event_snapshot(event, now=started, quote=quote)
                    if saved is not None:
                        event_snapshots.append(str(saved))

        # --- 2.5) 目标 + 奖励 ---------------------------------------------- #
        # 放在监控之后：监控会盯市刷新净值，用刷新后的净值判定目标才准。
        goal_result = None
        if self.cfg.goal.enabled:
            try:
                goal_result = self.engine.apply_goal_reward(now=started)
            except Exception as exc:  # noqa: BLE001 - 目标结算失败不应终止调度
                errors.append(f"目标结算失败：{type(exc).__name__}: {exc}")
                self.logger.exception("目标结算失败")

        # --- 3) 报告任务 --------------------------------------------------- #
        for kind, slot in self.due_report_slots(started):
            if slot > started:
                continue
            key = self.report_event_key(kind, slot)
            try:
                task = self.create_report_task(kind, slot, now=started, quote=quote)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"创建报告任务失败 {key}：{type(exc).__name__}: {exc}")
                continue
            if task is None:
                skipped.append(key)
            else:
                report_tasks.append(task)

        # --- 4) AI 决策 ----------------------------------------------------- #
        # 平时每分钟问一次；休市期间按更慢的节拍（默认 6 小时），避免拿过期报价
        # 反复换来一堆 HOLD 却照付 token。失败不重试、不加速：到点判定只看上一次
        # 是否已启动，所以一次失败之后自然就是「等下一个间隔」。
        decision_result: Any | None = None
        decision_skipped: str | None = None
        decision_run_no: int | None = None
        should_decide, decision_skipped, market_open = self.decision_plan(started)
        if should_decide:
            decision_result, decision_error = self.run_decision_task(
                now=started, quote=quote
            )
            if decision_error is not None:
                errors.append(f"AI 决策失败：{decision_error}")
            if decision_result is not None:
                latest = self.last_decision_run()
                decision_run_no = None if latest is None else latest.run_no

        finished = datetime.now(self.cfg.tz)
        result = TickResult(
            started_at=started,
            finished_at=finished,
            monitor_ran=monitor_ran,
            monitor_skipped_reason=monitor_skip_reason,
            monitor_run_no=monitor_run_no,
            monitor_status=monitor_status,
            equity=equity,
            margin_level=margin_level,
            blocked_new_positions=blocked,
            risk_event_count=event_count,
            closed_count=closed_count,
            report_tasks_created=tuple(report_tasks),
            report_tasks_skipped=tuple(skipped),
            event_snapshots=tuple(event_snapshots),
            decision_ran=decision_result is not None,
            decision_skipped_reason=decision_skipped,
            decision_outcome=(
                None if decision_result is None else decision_result.outcome
            ),
            decision_message=(
                None if decision_result is None else decision_result.message
            ),
            decision_run_no=decision_run_no,
            decision_status=(
                None
                if decision_result is None
                else ("SUCCESS" if decision_result.ok else "FAILED")
            ),
            pressure_applied=pressure_applied,
            pressure_charged_jpy=pressure_charged,
            pressure_slots=pressure_slots,
            pressure_skipped_reason=pressure_skipped,
            goal_achieved=bool(goal_result and goal_result.achieved),
            goal_level=(None if goal_result is None else goal_result.level),
            goal_target_jpy=(
                None if goal_result is None else str(goal_result.target_jpy)
            ),
            goal_reward_paid_jpy=(
                None if goal_result is None else str(goal_result.reward_paid_jpy)
            ),
            goal_next_target_jpy=(
                None
                if goal_result is None or goal_result.next_target_jpy is None
                else str(goal_result.next_target_jpy)
            ),
            market_open=market_open,
            goal_progress_ratio=(
                None
                if goal_result is None or goal_result.progress_ratio is None
                else str(goal_result.progress_ratio)
            ),
            errors=tuple(errors),
        )
        self.logger.info(
            "tick 完成：监控=%s 报告任务新建=%d 跳过=%d 决策=%s 错误=%d",
            "执行" if monitor_ran else (monitor_skip_reason or "未执行"),
            len(report_tasks), len(skipped),
            (
                f"{result.decision_outcome}"
                if decision_result is not None
                else (decision_skipped or "未执行")
            ),
            len(errors),
        )
        return result

    # -- 常驻循环 ---------------------------------------------------------- #

    def run_forever(
        self,
        *,
        interval_seconds: float | None = None,
        iterations: int | None = None,
        stop_event: threading.Event | None = None,
        max_consecutive_errors: int = 5,
        on_tick: Callable[[TickResult], None] | None = None,
    ) -> int:
        """常驻循环：每个周期执行一次 :meth:`tick`。

        * 正常退出：收到 ``stop_event``、检测到停止标记文件、或 ``KeyboardInterrupt``；
        * 异常恢复：单次 tick 抛异常时记录并按退避重试，连续失败达阈值才上抛；
        * 停止标记文件让 ``scheduler stop`` 能优雅结束进程。
        """
        period = float(interval_seconds) if interval_seconds is not None else float(
            self.interval_seconds
        )
        if period <= 0:
            raise EngineError(f"调度间隔必须为正数: {period}")
        self.recover()
        ticks = 0
        consecutive_errors = 0
        self.logger.info(
            "调度器启动：间隔 %.0f 秒，时区 %s，数据库 %s",
            period, self.cfg.tz, self.storage.db_path,
        )
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    self.logger.info("收到停止信号，正常退出")
                    break
                if self.stop_file.exists():
                    self.logger.info("检测到停止标记文件，正常退出")
                    break
                try:
                    result = self.tick()
                except Exception as exc:  # noqa: BLE001 - 单次失败不终止循环
                    consecutive_errors += 1
                    self.logger.exception(
                        "tick 失败（连续第 %d 次）：%s", consecutive_errors, exc
                    )
                    if consecutive_errors >= max_consecutive_errors:
                        self.logger.error("连续失败达到 %d 次，停止调度", consecutive_errors)
                        raise
                else:
                    consecutive_errors = 0
                    ticks += 1
                    if on_tick is not None:
                        on_tick(result)
                if iterations is not None and ticks >= iterations:
                    break
                if not self._sleep(period, stop_event):
                    self.logger.info("等待期间收到停止信号，正常退出")
                    break
        except KeyboardInterrupt:
            self.logger.info("收到中断信号（KeyboardInterrupt），正常退出")
        finally:
            self.logger.info("调度器结束，共执行 %d 次 tick", ticks)
        return ticks

    def _sleep(self, period: float, stop_event: threading.Event | None) -> bool:
        """分片睡眠，及时响应停止信号与停止标记文件。返回 False 表示应停止。"""
        deadline = time.monotonic() + period
        while True:
            if stop_event is not None and stop_event.is_set():
                return False
            if self.stop_file.exists():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.5, remaining))

    # -- 常驻进程管理 ------------------------------------------------------ #

    def read_pid(self) -> int | None:
        try:
            text = self.pid_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def status(self, *, recent: int = 10) -> SchedulerStatus:
        """当前调度器状态（只读）。"""
        pid = self.read_pid()
        runs = self.storage.read_scheduler_runs(limit=recent)
        monitor_runs = [r for r in runs if r.task_name == MONITOR_TASK_NAME]
        report_runs = [r for r in runs if r.task_name.startswith(REPORT_TASK_PREFIX)]
        pending = tuple(t for t in self.list_report_tasks() if t.status != "COMPLETED")
        return SchedulerStatus(
            running=bool(pid and _pid_alive(pid)),
            pid=pid,
            pid_alive=bool(pid and _pid_alive(pid)),
            stop_requested=self.stop_file.exists(),
            timezone=str(self.cfg.tz),
            interval_seconds=self.interval_seconds,
            database=str(self.storage.db_path),
            state_dir=str(self.state_dir),
            last_monitor_run=monitor_runs[0] if monitor_runs else None,
            last_report_run=report_runs[0] if report_runs else None,
            recent_runs=tuple(runs),
            pending_report_tasks=pending,
            next_report_slots=tuple(self.next_report_slots()),
        )

    def launch_detached(
        self,
        *,
        interval_seconds: float | None = None,
        python: str | None = None,
        extra_args: Sequence[str] | None = None,
    ) -> int:
        """以**分离进程**在后台启动常驻循环，返回子进程 pid。

        使用 ``pythonw`` + ``DETACHED_PROCESS``，父进程退出后子进程继续运行
        （本机已实测）。日志写入 ``state_dir/scheduler.log``。
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.stop_file.exists():
            self.stop_file.unlink()
        executable = Path(python) if python else Path(sys.executable).with_name(
            "pythonw.exe" if os.name == "nt" else "python3"
        )
        if not executable.is_file():
            executable = Path(sys.executable)
        argv = [
            str(executable),
            "-m",
            "scheduler",
            "run",
            "--interval",
            str(interval_seconds if interval_seconds is not None else self.interval_seconds),
            "--db",
            str(self.storage.db_path),
            "--reports-dir",
            str(self.reports_dir),
            "--state-dir",
            str(self.state_dir),
        ]
        if extra_args:
            argv.extend(extra_args)
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= getattr(subprocess, name, 0)
        log_handle = open(self.log_file, "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(Path(__file__).resolve().parent),
                creationflags=flags,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
            )
        finally:
            log_handle.close()
        self.pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
        self.logger.info("已启动分离进程 pid=%s，日志 %s", proc.pid, self.log_file)
        return proc.pid

    def request_stop(self) -> bool:
        """请求优雅停止：写停止标记文件。"""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text(
            datetime.now(self.cfg.tz).isoformat(timespec="seconds") + "\n", encoding="utf-8"
        )
        return True

    def stop(self, *, timeout: float = STOP_TIMEOUT_SECONDS) -> tuple[bool, str]:
        """优雅停止常驻进程；超时后强制结束。

        返回 ``(是否已停止, 说明)``。
        """
        pid = self.read_pid()
        if pid is None:
            return True, "没有 PID 文件，视为未在运行"
        if not _pid_alive(pid):
            self.pid_file.unlink(missing_ok=True)
            self.stop_file.unlink(missing_ok=True)
            return True, f"进程 {pid} 已不在运行，已清理 PID 文件"
        self.request_stop()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                self.pid_file.unlink(missing_ok=True)
                self.stop_file.unlink(missing_ok=True)
                return True, f"进程 {pid} 已优雅退出"
            time.sleep(0.5)
        # 超时：强制结束
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    check=False, capture_output=True,
                )
            else:  # pragma: no cover - 非 Windows
                os.kill(pid, 15)
        except Exception as exc:  # noqa: BLE001
            return False, f"强制结束进程 {pid} 失败：{type(exc).__name__}: {exc}"
        time.sleep(0.5)
        alive = _pid_alive(pid)
        if not alive:
            self.pid_file.unlink(missing_ok=True)
            self.stop_file.unlink(missing_ok=True)
        return (not alive), (
            f"进程 {pid} 超时未退出，已强制结束" if not alive else f"进程 {pid} 仍未退出"
        )


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #


def _build_scheduler(args: argparse.Namespace) -> Scheduler:
    cfg = load_config()
    storage = Storage(
        Path(args.db).expanduser() if args.db else cfg.paths.database_path, tz=cfg.tz
    )
    return Scheduler(
        cfg,
        storage=storage,
        reports_dir=args.reports_dir,
        state_dir=args.state_dir,
        decision_interval_seconds=getattr(args, "decision_interval", None),
        decision_enabled=not getattr(args, "no_decide", False),
        decision_dry_run=not getattr(args, "live_decisions", False),
    )


def _cmd_tick(args: argparse.Namespace) -> int:
    """执行一次调度（幂等）。适合由外部定时器每分钟调用。"""
    scheduler = _build_scheduler(args)
    result = scheduler.tick(force_monitor=args.force_monitor)
    if args.as_json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"tick 时间       : {result.started_at.isoformat(timespec='seconds')}"
              f"（{scheduler.cfg.tz}）")
        if result.monitor_ran:
            print(f"风险监控        : 已执行（运行号 {result.monitor_run_no}）"
                  f"　事件 {result.risk_event_count} 个　平仓 {result.closed_count} 笔")
            print(f"净值 / 保证金水平: {result.equity} / {result.margin_level}")
            print(f"禁止新开仓      : {'是' if result.blocked_new_positions else '否'}")
        else:
            print(f"风险监控        : 跳过（{result.monitor_skipped_reason}）")
        print(f"报告任务新建    : {len(result.report_tasks_created)} 个")
        for task in result.report_tasks_created:
            print(f"  · {task.task_id}（{task.kind}，时点 {task.slot.isoformat(timespec='minutes')}）")
            print(f"    数据包 {task.snapshot_path}")
            print(f"    模板   {task.template_path}")
            print(f"    交接   {task.handoff_path}")
        print(f"报告任务跳过    : {len(result.report_tasks_skipped)} 个（已存在）")
        if result.goal_achieved:
            print(f"目标奖励        : 达成第 {result.goal_level} 档"
                  f"（目标 {result.goal_target_jpy}），发放 {result.goal_reward_paid_jpy} 日元")
            print(f"  下一档目标    : {result.goal_next_target_jpy}")
        elif result.goal_level is not None:
            pct = (
                f"{float(result.goal_progress_ratio):.1%}"
                if result.goal_progress_ratio else "—"
            )
            print(f"目标进度        : 第 {result.goal_level} 档"
                  f"（目标 {result.goal_target_jpy}，进度 {pct}）")
        if result.pressure_applied:
            print(f"督促机制        : 已扣 {result.pressure_charged_jpy} 日元"
                  f"（{len(result.pressure_slots)} 个槽位：{'、'.join(result.pressure_slots)}）")
        else:
            print(f"督促机制        : 跳过（{result.pressure_skipped_reason}）")
        print(f"事件快照        : {len(result.event_snapshots)} 个")
        for path in result.event_snapshots:
            print(f"  · {path}")
        if result.decision_ran:
            print(f"AI 决策         : 已执行（运行号 {result.decision_run_no}）"
                  f"　结果 {result.decision_outcome}")
            if result.decision_message:
                print(f"  说明          : {result.decision_message}")
        else:
            print(f"AI 决策         : 跳过（{result.decision_skipped_reason}）")
        if result.market_open is not None:
            print(f"可交易状态      : {'是' if result.market_open else '否（休市/报价不可用）'}")
        for error in result.errors:
            print(f"  [错误] {error}")
    return 0 if result.ok else 1


def _cmd_run(args: argparse.Namespace) -> int:
    """常驻循环（前台或作为分离进程运行）。"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    scheduler = _build_scheduler(args)
    ticks = scheduler.run_forever(
        interval_seconds=args.interval, iterations=args.iterations
    )
    print(f"调度器已退出，共执行 {ticks} 次 tick")
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    scheduler = _build_scheduler(args)
    pid = scheduler.launch_detached(interval_seconds=args.interval)
    print(f"已在后台启动调度器（分离进程）")
    print(f"  PID        : {pid}")
    print(f"  时区       : {scheduler.cfg.tz}")
    print(f"  监控间隔   : {scheduler.interval_seconds} 秒（来自 config.json）")
    print(f"  循环节拍   : {args.interval or scheduler.interval_seconds} 秒"
          "（只影响响应停止标记与报告时点的及时性）")
    print(f"  数据库     : {scheduler.storage.db_path}")
    print(f"  日志       : {scheduler.log_file}")
    print(f"  查看状态   : python -m scheduler status")
    print(f"  停止       : python -m scheduler stop")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    scheduler = _build_scheduler(args)
    ok, message = scheduler.stop(timeout=args.timeout)
    print(("已停止：" if ok else "停止失败：") + message)
    return 0 if ok else 1


def _cmd_restart(args: argparse.Namespace) -> int:
    scheduler = _build_scheduler(args)
    ok, message = scheduler.stop(timeout=args.timeout)
    print(("已停止：" if ok else "停止失败：") + message)
    if not ok:
        return 1
    pid = scheduler.launch_detached(interval_seconds=args.interval)
    print(f"已重新启动（PID {pid}）")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    scheduler = _build_scheduler(args)
    status = scheduler.status(recent=args.recent)
    if args.as_json:
        print(json.dumps(status.as_dict(), ensure_ascii=False, indent=2))
        return 0
    print(f"是否在运行      : {'是' if status.running else '否'}"
          + (f"（PID {status.pid}）" if status.pid else "（无 PID 文件）"))
    if status.stop_requested:
        print("停止标记        : 存在（等待进程退出）")
    print(f"时区            : {status.timezone}")
    print(f"监控间隔        : {status.interval_seconds} 秒")
    print(f"数据库          : {status.database}")
    print(f"状态目录        : {status.state_dir}")
    print(f"日志文件        : {scheduler.log_file}")
    print(f"最近监控运行    : "
          + (f"#{status.last_monitor_run.run_no} {status.last_monitor_run.status} "
             f"{status.last_monitor_run.started_at.isoformat(timespec='seconds')}"
             if status.last_monitor_run else "无"))
    print(f"最近报告任务    : "
          + (f"#{status.last_report_run.run_no} {status.last_report_run.status} "
             f"{status.last_report_run.started_at.isoformat(timespec='seconds')}"
             if status.last_report_run else "无"))
    print(f"待处理报告任务  : {len(status.pending_report_tasks)} 个")
    for task in status.pending_report_tasks:
        print(f"  · {task.task_id}（{task.kind}）数据包 {task.snapshot_path}")
    print("接下来的报告时点：")
    for kind, moment in status.next_report_slots:
        print(f"  · {kind} → {moment}")
    if status.recent_runs:
        print("最近任务记录    :")
        for run in status.recent_runs[:8]:
            print(f"  #{run.run_no} {run.task_name} {run.status} "
                  f"{run.started_at.isoformat(timespec='seconds')} → "
                  f"{run.finished_at.isoformat(timespec='seconds') if run.finished_at else '进行中'}"
                  + (f"　错误 {run.error}" if run.error else ""))
    return 0


def _cmd_tasks(args: argparse.Namespace) -> int:
    scheduler = _build_scheduler(args)
    tasks = scheduler.list_report_tasks(limit=args.limit)
    if args.as_json:
        print(json.dumps([t.as_dict() for t in tasks], ensure_ascii=False, indent=2))
        return 0
    if not tasks:
        print("暂无报告任务。到 08:00 / 20:00 JST 后执行 tick 即可生成。")
        return 0
    for task in tasks:
        print(f"[{task.status}] {task.task_id}（{task.kind}）时点 {task.slot.isoformat(timespec='minutes')}")
        print(f"    覆盖时段 {task.period_start.isoformat(timespec='minutes')} ~ "
              f"{task.period_end.isoformat(timespec='minutes')}")
        print(f"    数据包   {task.snapshot_path}")
        print(f"    模板     {task.template_path}")
        print(f"    交接文件 {task.handoff_path}")
    return 0


def _cmd_complete_task(args: argparse.Namespace) -> int:
    scheduler = _build_scheduler(args)
    task = scheduler.complete_report_task(args.task_id, note=args.note)
    print(f"报告任务 {task.task_id} 已标记为 {task.status}")
    return 0


def _cmd_capabilities(args: argparse.Namespace) -> int:
    report = capabilities_report()
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print("本机调度能力实测结论：")
    for item in report:
        print(f"  [{item['status']}] {item['capability']}")
        print(f"      {item['detail']}")
    print("\n结论：常驻分离进程可用；外部定时驱动可用（schtasks 或平台 Agent 调度）；")
    print("      但独立 Python 脚本无法唤醒语言模型，因此报告以「任务 + 数据包」形式交付。")
    return 0


def _cmd_install_task(args: argparse.Namespace) -> int:
    """生成（或注册）Windows 任务计划程序条目，每分钟调用一次 tick。"""
    scheduler = _build_scheduler(args)
    executable = Path(sys.executable).with_name("pythonw.exe")
    if not executable.is_file():  # pragma: no cover - 非 Windows
        executable = Path(sys.executable)
    command = (
        f'"{executable}" -m scheduler tick '
        f'--db "{scheduler.storage.db_path}" '
        f'--reports-dir "{scheduler.reports_dir}" '
        f'--state-dir "{scheduler.state_dir}"'
    )
    create = [
        "schtasks", "/create", "/tn", args.name, "/tr", command,
        "/sc", "minute", "/mo", str(args.every_minutes), "/f",
    ]
    print("计划任务名    :", args.name)
    print("执行命令      :", command)
    # 用 list2cmdline 正确转义（/tr 的值本身含引号与空格，简单加引号会拼坏）
    print("注册命令      :", subprocess.list2cmdline(create))
    print()
    print("说明：tick 是幂等的——到点才监控、到点才建报告任务，")
    print("      因此每分钟唤醒不会产生重复任务，也不会重复执行交易。")
    if not args.apply:
        print()
        print("当前为**预演模式**，未注册任何系统任务。")
        print("确认无误后加 --apply 才会真正注册（可用 uninstall-task 撤销）。")
        return 0
    try:
        completed = subprocess.run(create, check=False, capture_output=True, text=True)
    except OSError as exc:
        print(f"注册失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(completed.stdout.strip() or completed.stderr.strip())
    return 0 if completed.returncode == 0 else 1


def _cmd_uninstall_task(args: argparse.Namespace) -> int:
    delete = ["schtasks", "/delete", "/tn", args.name, "/f"]
    print("删除命令      :", subprocess.list2cmdline(delete))
    if not args.apply:
        print("当前为**预演模式**，未删除任何系统任务。加 --apply 才会真正删除。")
        return 0
    try:
        completed = subprocess.run(delete, check=False, capture_output=True, text=True)
    except OSError as exc:
        print(f"删除失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(completed.stdout.strip() or completed.stderr.strip())
    return 0 if completed.returncode == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scheduler",
        description=(
            "风险监控与报告任务调度器。每分钟做一次风险监控；若 config.json 里"
            "配好了 LLM（llm.api_key + llm.model_id），还会顺带问一次模型做交易决策"
            "（默认 dry-run，只校验不落单）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用：\n"
            "  python -m scheduler capabilities        # 查看本机支持哪些调度能力\n"
            "  python -m scheduler tick                # 执行一次（幂等，可被外部定时器调用）\n"
            "  python -m scheduler start               # 后台常驻（分离进程）\n"
            "  python -m scheduler status              # 查看运行状态\n"
            "  python -m scheduler stop                # 优雅停止\n"
            "  python -m scheduler restart             # 重启\n"
            "  python -m scheduler tasks               # 查看报告任务\n"
            "\n"
            "AI 决策开关（默认开启，需配好 LLM；默认 dry-run 不会真开仓）：\n"
            "  --no-decide             只监控与报告，不调用语言模型\n"
            "  --live-decisions        决策真的下单（谨慎）\n"
            "  --decision-interval 60  两次决策的最小间隔秒数\n"
        ),
    )
    # 公共选项挂到每个子命令上，这样 `scheduler tick --db X` 与
    # `scheduler --db X tick` 都能用（与 main.py 的用法保持一致）。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="数据库路径（默认取 config.json）")
    common.add_argument("--reports-dir", default=None, dest="reports_dir", help="报告输出目录")
    common.add_argument(
        "--state-dir", default=None, dest="state_dir",
        help="状态目录（PID/停止标记/日志）",
    )
    common.add_argument(
        "--no-decide", action="store_true", dest="no_decide",
        help="本次不调用语言模型做交易决策（只做监控与报告）",
    )
    common.add_argument(
        "--live-decisions", action="store_true", dest="live_decisions",
        help="AI 决策真的下单（默认 dry-run：只校验并记录，不动账户）",
    )
    common.add_argument(
        "--decision-interval", type=int, default=None, dest="decision_interval",
        help=f"两次 AI 决策之间的最小间隔秒数（默认 {DEFAULT_DECISION_INTERVAL_SECONDS}）",
    )
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    p = sub.add_parser("tick", parents=[common], help="执行一次调度（幂等，适合外部定时器每分钟调用）")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    p.add_argument("--force-monitor", action="store_true", dest="force_monitor",
                   help="忽略间隔限制，强制执行一次风险监控")
    p.set_defaults(handler=_cmd_tick)

    p = sub.add_parser("run", parents=[common], help="前台运行常驻循环（分离进程内部使用）")
    p.add_argument("--interval", type=float, default=None, help="循环间隔（秒）")
    p.add_argument("--iterations", type=int, default=None, help="执行多少次后退出（调试用）")
    p.set_defaults(handler=_cmd_run)

    p = sub.add_parser("start", parents=[common], help="以分离进程在后台启动常驻循环")
    p.add_argument("--interval", type=float, default=None, help="循环间隔（秒）")
    p.set_defaults(handler=_cmd_start)

    p = sub.add_parser("stop", parents=[common], help="优雅停止常驻进程（超时后强制结束）")
    p.add_argument("--timeout", type=float, default=STOP_TIMEOUT_SECONDS, help="优雅停止等待秒数")
    p.set_defaults(handler=_cmd_stop)

    p = sub.add_parser("restart", parents=[common], help="停止后重新启动")
    p.add_argument("--interval", type=float, default=None, help="循环间隔（秒）")
    p.add_argument("--timeout", type=float, default=STOP_TIMEOUT_SECONDS, help="优雅停止等待秒数")
    p.set_defaults(handler=_cmd_restart)

    p = sub.add_parser("status", parents=[common], help="查看调度器运行状态（只读）")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    p.add_argument("--recent", type=int, default=10, help="显示最近多少条任务记录")
    p.set_defaults(handler=_cmd_status)

    p = sub.add_parser("tasks", parents=[common], help="查看报告任务（供 Agent 读取）")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    p.add_argument("--limit", type=int, default=None, help="只显示最近 N 个")
    p.set_defaults(handler=_cmd_tasks)

    p = sub.add_parser("complete-task", parents=[common], help="把报告任务标记为已完成")
    p.add_argument("task_id", help="任务编号")
    p.add_argument("--note", default=None, help="完成说明")
    p.set_defaults(handler=_cmd_complete_task)

    p = sub.add_parser("capabilities", parents=[common], help="查看本机支持的调度能力（实测结论）")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    p.set_defaults(handler=_cmd_capabilities)

    p = sub.add_parser("install-task", parents=[common], help="生成/注册「每分钟调用 tick」的系统计划任务")
    p.add_argument("--name", default="kurumi_fx_sim_scheduler", help="计划任务名")
    p.add_argument("--every-minutes", type=int, default=1, dest="every_minutes", help="间隔分钟数")
    p.add_argument("--apply", action="store_true", help="真正注册（默认只预演打印命令）")
    p.set_defaults(handler=_cmd_install_task)

    p = sub.add_parser("uninstall-task", parents=[common], help="删除该系统计划任务")
    p.add_argument("--name", default="kurumi_fx_sim_scheduler", help="计划任务名")
    p.add_argument("--apply", action="store_true", help="真正删除（默认只预演）")
    p.set_defaults(handler=_cmd_uninstall_task)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return int(handler(args))
    except (EngineError, StorageError, MarketDataError) as exc:
        print(f"[调度错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[未预期的错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
