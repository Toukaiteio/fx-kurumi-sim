"""main 模块（统一命令行入口）的自动化测试。

覆盖：全部命令的正常路径与失败路径、资金存取的严格确认与限额、开仓必须经引擎
重新校验（不可绕过）、查询命令的只读性、参数错误与退出码、重复操作，
以及「不得存在绕过资金授权的后门」这一硬约束。

所有测试都在临时目录建库并注入模拟行情，**不访问网络、不连接券商、不做真实交易**，
也不会触碰 ``data/fxsim.sqlite3``。

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
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import main as cli  # noqa: E402
from config import load_config  # noqa: E402
from engine import TradingEngine  # noqa: E402
from market_data import (  # noqa: E402
    Granularity,
    MarketDataError,
    Quote,
    QuoteType,
)
from storage import (  # noqa: E402
    AccountStatus,
    FlowType,
    PositionSide,
    PositionStatus,
    Storage,
)

_JST = timezone(timedelta(hours=9))


def make_quote(bid: str = "158.000", ask: str = "158.010", *, stale: bool = False) -> Quote:
    bid_d, ask_d = Decimal(bid), Decimal(ask)
    return Quote(
        symbol="USD/JPY", bid=bid_d, ask=ask_d, mid=(bid_d + ask_d) / 2,
        timestamp=datetime.now(_JST), source="cli_test", source_url="https://example.invalid/",
        quote_type=QuoteType.BID_ASK, granularity=Granularity.TICK, is_stale=stale,
        age_seconds=Decimal("999") if stale else Decimal("0"),
    )


def run_cli(*argv: str) -> tuple[int, str, str]:
    """执行 CLI，返回 ``(退出码, stdout, stderr)``。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTestCase(unittest.TestCase):
    """公共夹具：临时数据库 + 已初始化账户。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "data" / "fxsim.sqlite3"
        self.reports = self.root / "reports"
        self.cfg = load_config()
        storage = Storage(self.db, tz=_JST)
        storage.initialize_account(
            self.cfg.account.initial_capital_jpy,
            opened_at=datetime(2026, 10, 10, 8, 0, tzinfo=_JST),
        )

    # -- 工具 -------------------------------------------------------------- #

    def storage(self) -> Storage:
        return Storage(self.db, tz=_JST)

    def snapshot(self) -> dict:
        s = self.storage()
        state = s.read_account_state()
        return {
            "balance": s.ledger_balance(),
            "counts": s.counts(),
            "revision": state.revision,
            "equity": state.equity,
            "status": state.status,
            "audit_head": s.audit_head(),
        }

    def cli(self, *argv: str) -> tuple[int, str, str]:
        return run_cli(*argv, "--db", str(self.db))

    def cli_quote(self, quote: Quote | None = None, *argv: str):
        """注入模拟行情执行 CLI。"""
        with mock.patch.object(cli, "get_current_quote", return_value=quote or make_quote()):
            return run_cli(*argv, "--db", str(self.db))

    def open_position(self, side=PositionSide.LONG, *, stop="157.950", target="158.100"):
        engine = TradingEngine(self.cfg, self.storage())
        from engine import TradeIntent

        return engine.execute_virtual_trade(
            TradeIntent(
                symbol="USD/JPY", side=side, volume_usd=Decimal("1000"),
                stop_loss=Decimal(stop), take_profit=Decimal(target),
                intent_id=f"cli-fixture-{side.value}-{stop}",
            ),
            make_quote(), occurred_at=datetime(2026, 10, 10, 9, 0, tzinfo=_JST),
        )


# --------------------------------------------------------------------------- #
# 1. init / status / quote
# --------------------------------------------------------------------------- #


class TestInitAndStatus(CliTestCase):
    def test_init_is_idempotent(self) -> None:
        code, out, _ = self.cli("init")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("未重复发放本金", out)
        s = self.storage()
        self.assertEqual(s.ledger_balance(), Decimal("50000"))
        self.assertEqual(s.counts()["cash_flows"], 1)

    def test_init_grants_exactly_once_even_if_repeated(self) -> None:
        for _ in range(5):
            self.assertEqual(self.cli("init")[0], cli.EXIT_OK)
        self.assertEqual(self.storage().ledger_balance(), Decimal("50000"))
        self.assertEqual(self.storage().counts()["cash_flows"], 1)

    def test_status_is_read_only(self) -> None:
        before = self.snapshot()
        # 注入模拟行情：测试绝不能真的去取行情（慢，而且会被数据源限流）
        code, out, _ = self.cli_quote(make_quote(), "status")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("现金余额", out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.storage().counts()["trades"], 0)

    def test_status_without_quote_reports_unverifiable(self) -> None:
        code, out, _ = self.cli("status", "--no-quote")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("无法核实", out)

    def test_status_json(self) -> None:
        code, out, _ = self.cli("status", "--json", "--no-quote")
        payload = json.loads(out)
        self.assertEqual(payload["cash_balance"], "50000")
        self.assertIsNone(payload["equity"])
        self.assertEqual(payload["open_positions"], [])

    def test_status_before_init_is_refused(self) -> None:
        fresh = self.root / "other" / "db.sqlite3"
        code, _, err = run_cli("status", "--db", str(fresh))
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("尚未初始化", err)

    def test_quote_shows_timestamps_and_source(self) -> None:
        code, out, _ = self.cli_quote(make_quote(), "quote")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("报价时间戳", out)
        self.assertIn("cli_test", out)
        self.assertIn("点差", out)

    def test_quote_is_read_only(self) -> None:
        before = self.snapshot()
        self.cli_quote(make_quote(), "quote")
        self.assertEqual(self.snapshot(), before)

    def test_quote_failure_is_reported(self) -> None:
        with mock.patch.object(
            cli, "get_current_quote", side_effect=MarketDataError("数据源全挂")
        ):
            code, _, err = self.cli("quote")
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("数据源全挂", err)


# --------------------------------------------------------------------------- #
# 2. 开仓：必须经引擎重新校验，不可绕过
# --------------------------------------------------------------------------- #


class TestOpenCommands(CliTestCase):
    def test_open_long_success(self) -> None:
        code, out, _ = self.cli_quote(
            make_quote(), "open-long", "--volume", "1000", "--stop", "157.950",
            "--take-profit", "158.100", "--reason", "15 分钟级别突破回踩",
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("已开多仓", out)
        self.assertIn("Ask", out)
        self.assertIn("15 分钟级别突破回踩", out)
        s = self.storage()
        positions = s.read_positions(status=PositionStatus.OPEN)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].entry_price, Decimal("158.010"))
        self.assertEqual(s.counts()["trades"], 1)
        # 开仓不产生现金流
        self.assertEqual(s.ledger_balance(), Decimal("50000"))

    def test_open_short_success(self) -> None:
        code, out, _ = self.cli_quote(
            make_quote(), "open-short", "--volume", "1000", "--stop", "158.060",
            "--take-profit", "157.910", "--reason", "上方假突破",
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("已开空仓", out)
        self.assertEqual(
            self.storage().read_positions()[0].entry_price, Decimal("158.000")
        )

    def test_reason_is_required(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.cli_quote(
                make_quote(), "open-long", "--volume", "1000",
                "--stop", "157.950", "--take-profit", "158.100",
            )
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_volume_stop_target_all_required(self) -> None:
        for missing in ("--volume", "--stop", "--take-profit"):
            argv = ["open-long", "--volume", "1000", "--stop", "157.950",
                    "--take-profit", "158.100", "--reason", "x"]
            index = argv.index(missing)
            del argv[index:index + 2]
            with self.assertRaises(SystemExit) as ctx:
                self.cli_quote(make_quote(), *argv)
            self.assertEqual(ctx.exception.code, cli.EXIT_USAGE, msg=missing)

    def test_engine_revalidates_and_rejects_bad_ratio(self) -> None:
        code, _, err = self.cli_quote(
            make_quote(), "open-long", "--volume", "1000", "--stop", "157.950",
            "--take-profit", "158.020",  # 盈亏比不足 1:1.5
            "--reason", "盈亏比不足",
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("引擎重新校验未通过", err)
        self.assertIn("计划盈亏比", err)
        self.assertEqual(self.storage().counts()["positions"], 0)

    def test_engine_revalidates_and_rejects_oversized_volume(self) -> None:
        code, _, err = self.cli_quote(
            make_quote(), "open-long", "--volume", "1500", "--stop", "157.950",
            "--take-profit", "158.100", "--reason", "非整数倍",
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("正整数倍", err)
        self.assertEqual(self.storage().counts()["positions"], 0)

    def test_stale_quote_rejects_open_and_cannot_be_bypassed(self) -> None:
        code, _, err = self.cli_quote(
            make_quote(stale=True), "open-long", "--volume", "1000",
            "--stop", "157.950", "--take-profit", "158.100", "--reason", "过期行情",
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("过期", err)
        self.assertEqual(self.storage().counts()["positions"], 0)

    def test_missing_quote_rejects_open(self) -> None:
        with mock.patch.object(
            cli, "get_current_quote", side_effect=MarketDataError("无行情")
        ):
            code, _, err = self.cli(
                "open-long", "--volume", "1000", "--stop", "157.950",
                "--take-profit", "158.100", "--reason", "无行情",
            )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("无法获取行情", err)

    def test_second_position_is_rejected_by_rule(self) -> None:
        self.cli_quote(make_quote(), "open-long", "--volume", "1000", "--stop",
                       "157.950", "--take-profit", "158.100", "--reason", "第一笔")
        code, _, err = self.cli_quote(
            make_quote(), "open-long", "--volume", "1000", "--stop",
            "157.950", "--take-profit", "158.100", "--reason", "第二笔",
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("禁止加仓", err)
        self.assertEqual(len(self.storage().read_positions()), 1)

    def test_duplicate_intent_id_does_not_open_twice(self) -> None:
        argv = ["open-long", "--volume", "1000", "--stop", "157.950",
                "--take-profit", "158.100", "--reason", "重复提交", "--intent-id", "cli-1"]
        self.assertEqual(self.cli_quote(make_quote(), *argv)[0], cli.EXIT_OK)
        code, out, _ = self.cli_quote(make_quote(), *argv)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("重复指令", out)
        self.assertEqual(self.storage().counts()["positions"], 1)
        self.assertEqual(self.storage().counts()["trades"], 1)

    def test_before_init_is_refused(self) -> None:
        fresh = self.root / "fresh" / "db.sqlite3"
        with mock.patch.object(cli, "get_current_quote", return_value=make_quote()):
            code, _, err = run_cli(
                "open-long", "--db", str(fresh), "--volume", "1000",
                "--stop", "157.950", "--take-profit", "158.100", "--reason", "x",
            )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("尚未初始化", err)


# --------------------------------------------------------------------------- #
# 3. 平仓
# --------------------------------------------------------------------------- #

class TestCloseCommand(CliTestCase):
    def test_close_without_position_is_refused(self) -> None:
        code, _, err = self.cli_quote(make_quote(), "close", "--reason", "manual")
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("没有未平仓持仓", err)

    def test_close_records_realized_pnl(self) -> None:
        self.open_position(PositionSide.LONG)
        code, out, _ = self.cli_quote(
            make_quote("158.500", "158.510"), "close", "--reason", "manual"
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("已平仓", out)
        self.assertIn("490.000", out)  # (158.500 - 158.010) * 1000
        s = self.storage()
        self.assertEqual(s.ledger_balance(), Decimal("50490"))
        self.assertEqual(s.read_positions(status=PositionStatus.OPEN), [])

    def test_close_with_explicit_position_id(self) -> None:
        execution = self.open_position(PositionSide.LONG)
        code, out, _ = self.cli_quote(
            make_quote("158.500", "158.510"), "close",
            "--position-id", str(execution.position.position_id), "--reason", "take-profit",
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("TAKE_PROFIT", out)

    def test_close_unknown_position_id(self) -> None:
        self.open_position(PositionSide.LONG)
        code, _, err = self.cli_quote(
            make_quote(), "close", "--position-id", "999999"
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("未找到未平仓持仓", err)

    def test_close_with_fee(self) -> None:
        self.open_position(PositionSide.LONG)
        code, out, _ = self.cli_quote(
            make_quote("158.500", "158.510"), "close", "--fee", "50"
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.storage().ledger_balance(), Decimal("50440"))

    def test_invalid_close_reason_is_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.cli("close", "--reason", "no-such-reason")
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)


# --------------------------------------------------------------------------- #
# 4. 资金存取：严格确认与限额
# --------------------------------------------------------------------------- #


class TestDepositWithdraw(CliTestCase):
    # -- 正常路径 ---------------------------------------------------------- #

    def test_deposit_with_correct_confirmation(self) -> None:
        code, out, _ = self.cli(
            "deposit", "--amount", "10000", "--confirm", "确认入金 10000"
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("已入金 10000", out)
        self.assertEqual(self.storage().ledger_balance(), Decimal("60000"))
        flows = self.storage().read_cash_flows(flow_type=FlowType.DEPOSIT)
        self.assertEqual(len(flows), 2)  # 初始本金 + 本次

    def test_withdraw_with_correct_confirmation(self) -> None:
        code, out, _ = self.cli(
            "withdraw", "--amount", "5000", "--confirm", "确认出金 5000"
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.storage().ledger_balance(), Decimal("45000"))

    def test_deposit_accepts_decimal_rendering_of_amount(self) -> None:
        code, _, _ = self.cli(
            "deposit", "--amount", "10000.0", "--confirm", "确认入金 10000"
        )
        self.assertEqual(code, cli.EXIT_OK)
        code2, _, _ = self.cli(
            "deposit", "--amount", "2000.00", "--confirm", "确认入金 2000.00"
        )
        self.assertEqual(code2, cli.EXIT_OK)

    # -- 确认不匹配：绝不写数据 -------------------------------------------- #

    def test_wrong_confirmation_changes_nothing(self) -> None:
        before = self.snapshot()
        code, _, err = self.cli(
            "deposit", "--amount", "10000", "--confirm", "确认入金 9999"
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("未确认", err)
        self.assertIn("确认入金 10000", err)  # 错误信息里给出期望文字
        self.assertEqual(self.snapshot(), before)

    def test_missing_confirmation_is_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.cli("deposit", "--amount", "10000")
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_empty_confirmation_rejected(self) -> None:
        before = self.snapshot()
        code, _, _ = self.cli("deposit", "--amount", "10000", "--confirm", "   ")
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertEqual(self.snapshot(), before)

    def test_withdraw_requires_withdraw_confirmation_text(self) -> None:
        before = self.snapshot()
        code, _, err = self.cli(
            "withdraw", "--amount", "5000", "--confirm", "确认入金 5000"
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("确认出金 5000", err)
        self.assertEqual(self.snapshot(), before)

    def test_confirmation_with_extra_text_rejected(self) -> None:
        before = self.snapshot()
        code, _, _ = self.cli(
            "deposit", "--amount", "10000", "--confirm", "确认入金 10000 请执行"
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertEqual(self.snapshot(), before)

    # -- 金额限额 ---------------------------------------------------------- #

    def test_amount_below_minimum_rejected(self) -> None:
        before = self.snapshot()
        code, _, err = self.cli("deposit", "--amount", "500", "--confirm", "确认入金 500")
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("最低 1000", err)
        self.assertEqual(self.snapshot(), before)

    def test_amount_must_be_multiple_of_1000(self) -> None:
        before = self.snapshot()
        code, _, err = self.cli(
            "deposit", "--amount", "1500", "--confirm", "确认入金 1500"
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("整数倍", err)
        self.assertEqual(self.snapshot(), before)

    def test_deposit_above_maximum_rejected(self) -> None:
        before = self.snapshot()
        code, _, err = self.cli(
            "deposit", "--amount", "101000", "--confirm", "确认入金 101000"
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("最高 100000", err)
        self.assertEqual(self.snapshot(), before)

    def test_deposit_at_maximum_allowed(self) -> None:
        code, _, _ = self.cli(
            "deposit", "--amount", "100000", "--confirm", "确认入金 100000"
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.storage().ledger_balance(), Decimal("150000"))

    def test_withdraw_above_cash_balance_rejected(self) -> None:
        before = self.snapshot()
        code, _, err = self.cli(
            "withdraw", "--amount", "51000", "--confirm", "确认出金 51000"
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("超过现金余额", err)
        self.assertEqual(self.snapshot(), before)

    def test_withdraw_exactly_cash_balance_allowed(self) -> None:
        code, _, _ = self.cli(
            "withdraw", "--amount", "50000", "--confirm", "确认出金 50000"
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.storage().ledger_balance(), Decimal("0"))

    def test_invalid_amount_text_rejected(self) -> None:
        for bad in ("abc", "", "1e9999", "-1000", "0"):
            before = self.snapshot()
            code, _, _ = self.cli(
                "deposit", "--amount", bad, "--confirm", f"确认入金 {bad}"
            )
            self.assertEqual(code, cli.EXIT_FAILED, msg=bad)
            self.assertEqual(self.snapshot(), before, msg=bad)

    # -- 前置条件 ---------------------------------------------------------- #

    def test_deposit_with_open_position_rejected(self) -> None:
        self.open_position(PositionSide.LONG)
        before = self.snapshot()
        code, _, err = self.cli(
            "deposit", "--amount", "10000", "--confirm", "确认入金 10000"
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("未平仓持仓", err)
        self.assertEqual(self.snapshot(), before)

    def test_withdraw_with_open_position_rejected(self) -> None:
        self.open_position(PositionSide.LONG)
        before = self.snapshot()
        code, _, _ = self.cli(
            "withdraw", "--amount", "5000", "--confirm", "确认出金 5000"
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertEqual(self.snapshot(), before)

    def test_flow_allowed_after_position_closed(self) -> None:
        self.open_position(PositionSide.LONG)
        self.cli_quote(make_quote("158.500", "158.510"), "close")
        code, _, _ = self.cli(
            "deposit", "--amount", "10000", "--confirm", "确认入金 10000"
        )
        self.assertEqual(code, cli.EXIT_OK)

    def test_deposit_rejected_when_account_bankrupt(self) -> None:
        import dataclasses

        s = self.storage()
        state = s.read_account_state()
        s.save_account_state(
            dataclasses.replace(state, status=AccountStatus.BANKRUPT.value, halted=True)
        )
        before = self.snapshot()
        code, _, err = self.cli(
            "deposit", "--amount", "10000", "--confirm", "确认入金 10000"
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("账户状态", err)
        self.assertEqual(self.snapshot(), before)

    def test_flow_before_init_rejected(self) -> None:
        fresh = self.root / "fresh2" / "db.sqlite3"
        code, _, err = run_cli(
            "deposit", "--db", str(fresh), "--amount", "10000",
            "--confirm", "确认入金 10000",
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("尚未初始化", err)

    # -- 幂等 -------------------------------------------------------------- #

    def test_duplicate_request_id_does_not_double_charge(self) -> None:
        argv = ["deposit", "--amount", "10000", "--confirm", "确认入金 10000",
                "--request-id", "req-1"]
        self.assertEqual(self.cli(*argv)[0], cli.EXIT_OK)
        code, out, _ = self.cli(*argv)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("重复请求", out)
        self.assertEqual(self.storage().ledger_balance(), Decimal("60000"))
        self.assertEqual(
            len(self.storage().read_cash_flows(flow_type=FlowType.DEPOSIT)), 2
        )

    def test_six_invalid_attempts_leave_no_trace(self) -> None:
        before = self.snapshot()
        attempts = [
            ("deposit", "--amount", "10000", "--confirm", "确认入金 9999"),
            ("deposit", "--amount", "500", "--confirm", "确认入金 500"),
            ("deposit", "--amount", "1500", "--confirm", "确认入金 1500"),
            ("deposit", "--amount", "101000", "--confirm", "确认入金 101000"),
            ("withdraw", "--amount", "5000", "--confirm", "确认入金 5000"),
            ("withdraw", "--amount", "99000", "--confirm", "确认出金 99000"),
        ]
        for argv in attempts:
            code, _, _ = self.cli(*argv)
            self.assertEqual(code, cli.EXIT_FAILED, msg=str(argv))
        self.assertEqual(self.snapshot(), before)


# --------------------------------------------------------------------------- #
# 5. monitor-once / report-data / verify / backup
# --------------------------------------------------------------------------- #


class TestMonitorReportVerifyBackup(CliTestCase):
    def test_monitor_once_is_safe_without_positions(self) -> None:
        code, out, _ = self.cli_quote(make_quote(), "monitor-once")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("本次平仓      : 0 笔", out)
        self.assertEqual(self.storage().counts()["trades"], 0)

    def test_monitor_once_triggers_stop_loss(self) -> None:
        self.open_position(PositionSide.LONG, stop="157.950", target="158.100")
        # 先建立「此前价位」，再触发
        self.cli_quote(make_quote(), "monitor-once")
        code, out, _ = self.cli_quote(make_quote("157.900", "157.910"), "monitor-once")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("STOP_LOSS", out)
        self.assertEqual(self.storage().read_positions(status=PositionStatus.OPEN), [])

    def test_monitor_once_json(self) -> None:
        code, out, _ = self.cli_quote(make_quote(), "monitor-once", "--json")
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(out)
        self.assertIn("events", payload)
        self.assertIn("closed", payload)

    def test_report_data_stdout(self) -> None:
        code, out, _ = self.cli_quote(make_quote(), "report-data", "--kind", "evening")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("累计净利润", out)
        self.assertIn("累计收益率", out)

    def test_report_data_no_quote_marks_unverifiable(self) -> None:
        code, out, _ = self.cli("report-data", "--no-quote")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("行情          : 不可用", out)
        self.assertIn("无法核实", out)

    def test_report_data_json_packet(self) -> None:
        code, out, _ = self.cli_quote(make_quote(), "report-data", "--json")
        payload = json.loads(out)
        for key in (
            "report_kind", "report_at", "market", "account",
            "cumulative_trading_cost", "positions", "risk_watch", "data_quality",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["positions"], [])
        self.assertIsInstance(payload["account"]["cash_balance"], str)

    def test_report_data_export_writes_files(self) -> None:
        code, out, _ = self.cli_quote(
            make_quote(), "report-data", "--kind", "evening",
            "--export", "--out-dir", str(self.reports),
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("快照", out)
        snapshots = list(self.reports.glob("*.json"))
        templates = list(self.reports.glob("*.md"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(len(templates), 1)
        self.assertIn("evening", snapshots[0].name)
        payload = json.loads(snapshots[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["account"]["cash_balance"], "50000.000")

    def test_report_data_is_read_only(self) -> None:
        before = self.snapshot()
        self.cli_quote(make_quote(), "report-data")
        self.assertEqual(self.snapshot(), before)

    def test_verify_ok(self) -> None:
        code, out, _ = self.cli("verify")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("整体            : OK", out)

    def test_verify_detects_audit_tampering(self) -> None:
        import sqlite3

        self.cli("deposit", "--amount", "10000", "--confirm", "确认入金 10000")
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE audit_events SET payload='{\"tampered\":1}' WHERE seq=2")
        conn.commit()
        conn.close()
        code, out, _ = self.cli("verify")
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("FAILED", out)

    def test_verify_is_read_only(self) -> None:
        before = self.snapshot()
        self.cli("verify")
        self.assertEqual(self.snapshot(), before)

    def test_backup_creates_and_verifies_file(self) -> None:
        dest = self.root / "backups" / "manual.sqlite3"
        code, out, _ = self.cli("backup", "--dest", str(dest))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertTrue(dest.is_file())
        self.assertIn("校验", out)
        # 备份可用且账本一致
        backup = Storage(dest, tz=_JST)
        self.assertEqual(backup.ledger_balance(), self.storage().ledger_balance())


# --------------------------------------------------------------------------- #
# 6. test 命令
# --------------------------------------------------------------------------- #


class TestNotesCommand(CliTestCase):
    """`notes` 命令：查看 AI 自己写下的笔记（只读）。"""

    def seed(self, *notes) -> None:
        storage = self.storage()
        for kind, content in notes:
            storage.record_note(kind, content, decision_id="dec-seed")

    def test_empty_state(self) -> None:
        code, out, _ = self.cli("notes")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("笔记总数        : 0", out)
        self.assertIn("还没有笔记", out)

    def test_lists_notes(self) -> None:
        self.seed(("LESSON", "别在休市时开仓"), ("PLAN", "157.5 是支撑"))
        code, out, _ = self.cli("notes")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("笔记总数        : 2", out)
        self.assertIn("别在休市时开仓", out)
        self.assertIn("157.5 是支撑", out)
        self.assertIn("LESSON", out)
        self.assertIn("dec-seed", out)

    def test_json_output(self) -> None:
        self.seed(("LESSON", "一条"))
        code, out, _ = self.cli("notes", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["count_total"], 1)
        self.assertEqual(payload["notes"][0]["kind"], "LESSON")
        self.assertEqual(payload["notes"][0]["content"], "一条")

    def test_kind_filter(self) -> None:
        self.seed(("LESSON", "教训"), ("PLAN", "计划"))
        code, out, _ = self.cli("notes", "--kind", "PLAN")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("计划", out)
        self.assertNotIn("教训", out)

    def test_limit(self) -> None:
        self.seed(*[("LESSON", f"n{i}") for i in range(5)])
        code, out, _ = self.cli("notes", "--json", "--limit", "2")
        payload = json.loads(out)
        self.assertEqual(payload["count_total"], 5)
        self.assertEqual(payload["returned"], 2)

    def test_notes_command_is_read_only(self) -> None:
        """查询命令绝不能改动账户。"""
        self.seed(("LESSON", "只读"))
        before = self.storage().read_account_state()
        self.cli("notes")
        self.cli("notes", "--json")
        after = self.storage().read_account_state()
        self.assertEqual(before.cash_balance, after.cash_balance)
        self.assertEqual(before.revision, after.revision)
        self.assertEqual(self.storage().count_notes(), 1)

    def test_notes_command_does_not_require_quote(self) -> None:
        """查看笔记不该触发行情请求。"""
        with mock.patch.object(cli, "get_current_quote",
                               side_effect=AssertionError("不该取行情")):
            code, _, _ = self.cli("notes")
        self.assertEqual(code, cli.EXIT_OK)

    def test_uninitialized_db_is_a_clean_error(self) -> None:
        # 注意用 run_cli 而不是 self.cli：后者会把夹具的 --db 追加在后面。
        code, _, err = run_cli("notes", "--db", str(self.root / "empty" / "db.sqlite3"))
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertNotIn("Traceback", err)


class TestMemoryCommand(CliTestCase):
    """`memory` 命令：查看 / 重建记忆文件投影（不碰账本）。"""

    def test_view_without_rebuild(self) -> None:
        code, out, _ = self.cli("memory")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("笔记（数据库）", out)
        self.assertIn("对话存档", out)

    def test_rebuild_writes_files(self) -> None:
        root = self.root / "mem"
        self.storage().record_note("LESSON", "一条", decision_id="dec-1")
        code, out, _ = self.cli("memory", "--rebuild", "--root", str(root))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("笔记文件", out)
        self.assertTrue((root / "INDEX.md").is_file())

    def test_rebuild_json(self) -> None:
        root = self.root / "mem"
        code, out, _ = self.cli("memory", "--rebuild", "--json", "--root", str(root))
        payload = json.loads(out)
        self.assertEqual(payload["notes"], 0)
        self.assertIn("root", payload)

    def test_view_json(self) -> None:
        code, out, _ = self.cli("memory", "--json")
        payload = json.loads(out)
        for key in ("root", "note_files", "hour_files", "notes_in_db", "turns_in_db"):
            self.assertIn(key, payload)

    def test_memory_command_is_read_only(self) -> None:
        before = self.storage().read_account_state()
        self.cli("memory")
        self.cli("memory", "--json")
        after = self.storage().read_account_state()
        self.assertEqual(before.cash_balance, after.cash_balance)
        self.assertEqual(before.revision, after.revision)

    def test_rebuild_does_not_touch_account(self) -> None:
        root = self.root / "mem"
        self.storage().record_note("LESSON", "一条")
        before = self.storage().read_account_state()
        self.cli("memory", "--rebuild", "--root", str(root))
        after = self.storage().read_account_state()
        self.assertEqual(before.cash_balance, after.cash_balance)
        self.assertEqual(before.revision, after.revision)
        self.assertEqual(self.storage().ledger_balance(), Decimal("50000"))


class TestPressureCommand(CliTestCase):
    """`pressure` 命令：结算 / 查看督促机制。"""

    def test_view(self) -> None:
        code, out, _ = self.cli("pressure")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("督促机制", out)
        self.assertIn("每 6 小时扣 3000 日元", out)
        self.assertIn("下次扣款", out)

    def test_dry_run_does_not_charge(self) -> None:
        before = self.storage().ledger_balance()
        code, out, _ = self.cli("pressure", "--dry-run")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("dry-run", out)
        self.assertEqual(self.storage().ledger_balance(), before)
        self.assertEqual(self.storage().read_cash_flows(flow_type="CAPITAL_DECAY"), [])

    def test_json_output(self) -> None:
        code, out, _ = self.cli("pressure", "--json")
        payload = json.loads(out)
        self.assertTrue(payload["config"]["enabled"])
        self.assertEqual(payload["config"]["interval_hours"], 6)
        self.assertIn("result", payload)
        self.assertEqual(payload["charged_total_jpy"], "0")

    def test_pressure_command_does_not_require_quote(self) -> None:
        with mock.patch.object(cli, "get_current_quote",
                               side_effect=AssertionError("不该取行情")):
            code, _, _ = self.cli("pressure")
        self.assertEqual(code, cli.EXIT_OK)

    def test_uninitialized_db_is_a_clean_error(self) -> None:
        code, _, err = run_cli("pressure", "--db", str(self.root / "empty" / "db.sqlite3"))
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertNotIn("Traceback", err)


class TestTestCommand(CliTestCase):
    def test_test_command_runs_suite(self) -> None:
        with mock.patch.object(
            cli.unittest.TestLoader, "discover", return_value=unittest.TestSuite()
        ):
            code, out, _ = run_cli("test")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("共 0 个测试", out)

    def test_test_command_reports_failure(self) -> None:
        failing = unittest.TestSuite([unittest.FunctionTestCase(lambda: self.fail("boom"))])
        with mock.patch.object(cli.unittest.TestLoader, "discover", return_value=failing):
            code, _, _ = run_cli("test")
        self.assertEqual(code, cli.EXIT_FAILED)

    def test_test_command_does_not_touch_database(self) -> None:
        """test 命令不得为真实账户发放本金或修改任何数据。"""
        before = self.snapshot()
        with mock.patch.object(
            cli.unittest.TestLoader, "discover", return_value=unittest.TestSuite()
        ):
            run_cli("test")
        self.assertEqual(self.snapshot(), before)

    def test_test_command_does_not_accept_db_option(self) -> None:
        """test 命令刻意不接受 --db：它根本不打开数据库，因此与账户状态无关。"""
        self.assertNotIn("--db", _options_for("test"))
        with mock.patch.object(
            cli.unittest.TestLoader, "discover", return_value=unittest.TestSuite()
        ):
            code, _, _ = run_cli("test")
        self.assertEqual(code, cli.EXIT_OK)


# --------------------------------------------------------------------------- #
# 7. 后门与绕过检查（硬约束）
# --------------------------------------------------------------------------- #

_FORBIDDEN_OPTION_SUBSTRINGS = (
    "force", "skip", "bypass", "override", "unsafe", "ignore",
    "no-risk", "no-check", "allow-stale", "allow_stale",
    "set-balance", "set-pnl", "set-price", "set-equity", "set-margin",
    "unlock", "reset",
)


def _collect_option_strings(parser: argparse.ArgumentParser) -> set[str]:
    options: set[str] = set()
    for action in parser._actions:
        options.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                options |= _collect_option_strings(sub)
    return options


def _options_for(command: str) -> set[str]:
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(_collect_option_strings(action.choices[command]))
    raise AssertionError("找不到子命令解析器")


class TestNoBackdoor(unittest.TestCase):
    def test_no_forbidden_option_anywhere(self) -> None:
        parser = cli.build_parser()
        options = _collect_option_strings(parser)
        offenders = sorted(
            opt for opt in options
            for bad in _FORBIDDEN_OPTION_SUBSTRINGS
            if bad in opt.lower()
        )
        self.assertEqual(offenders, [], msg=f"存在可疑开关：{offenders}")

    def test_open_commands_expose_only_expected_options(self) -> None:
        expected = {
            "-h", "--help", "--volume", "--stop", "--take-profit", "--reason",
            "--intent-id", "--decision-ref", "--db",
        }
        for command in ("open-long", "open-short"):
            self.assertEqual(_options_for(command), expected, msg=command)

    def test_decide_command_options(self) -> None:
        """decide 只允许 dry-run/live 切换，不得有任何绕过风控的开关。"""
        options = _options_for("decide")
        self.assertEqual(
            options,
            {"-h", "--help", "--live", "--decision-id", "--max-rounds",
             "--show-context", "--json", "--db"},
        )
        self.assertNotIn("--force", options)
        self.assertNotIn("--skip-risk", options)

    def test_decide_help_mentions_dry_run_default(self) -> None:
        """decide 的帮助文本必须写明默认 dry-run（避免误以为会直接开仓）。"""
        parser = cli.build_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                help_text = action.choices["decide"].description or ""
                break
        else:
            self.fail("找不到 decide 子命令")
        self.assertIn("dry-run", help_text)

    def test_decide_live_is_not_a_risk_bypass(self) -> None:
        """--live 只是「真的执行」，不是「跳过风控」：引擎校验照旧。"""
        from unittest import mock as _mock

        import agent as agent_mod

        captured: dict[str, object] = {}
        original = agent_mod.TradingAgent.decide

        def spy(self, **kwargs):
            captured.update(kwargs)
            raise SystemExit(0)

        env = {"KURUMI_LLM_BASE_URL": "https://llm.example.invalid/v1",
               "KURUMI_LLM_API_KEY": "sk-test-key", "KURUMI_LLM_MODEL_ID": "Test-Model"}
        with _mock.patch.dict(os.environ, env, clear=False),              _mock.patch.object(agent_mod.TradingAgent, "decide", spy):
            with self.assertRaises(SystemExit):
                cli.main(["decide", "--live"])
        self.assertIs(captured.get("dry_run"), False)

    def test_flow_commands_cannot_bypass_confirmation(self) -> None:
        for command in ("deposit", "withdraw"):
            options = _options_for(command)
            self.assertIn("--confirm", options)
            # 没有任何可以跳过确认的开关
            self.assertFalse(
                any(
                    "confirm" not in opt
                    and any(bad in opt.lower() for bad in ("skip", "force", "no-"))
                    for opt in options
                ),
                msg=command,
            )

    def test_cli_cannot_set_balance_pnl_or_price(self) -> None:
        """所有命令的开关里都不存在可以直接指定余额/盈亏/成交价/保证金的参数。"""
        parser = cli.build_parser()
        for action in parser._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, sub in action.choices.items():
                for opt in _collect_option_strings(sub):
                    lowered = opt.lower()
                    # 注意：--take-profit 是需求要求必填的合法参数，因此不把 "profit" 列入
                    for forbidden in ("balance", "pnl", "equity", "fill", "notional", "set-"):
                        self.assertNotIn(forbidden, lowered, msg=f"{name} {opt}")

    def test_no_hidden_subcommands(self) -> None:
        """命令集合必须恰好是需求列出的那些（加上 notes），没有隐藏命令。"""
        parser = cli.build_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                self.assertEqual(
                    set(action.choices),
                    {
                        "init", "status", "quote", "monitor-once", "open-long",
                        "open-short", "close", "deposit", "withdraw",
                        "report-data", "verify", "notes", "memory", "pressure",
                        "backup", "decide", "gate", "test",
                    },
                )
                return
        self.fail("没有子命令")


# --------------------------------------------------------------------------- #
# 8. 参数错误与异常处理
# --------------------------------------------------------------------------- #


class TestArgumentErrors(CliTestCase):
    def test_no_command_prints_help_and_returns_usage(self) -> None:
        code, out, _ = run_cli()
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("usage:", out)

    def test_unknown_command_is_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            run_cli("no-such-command")
        self.assertEqual(ctx.exception.code, cli.EXIT_USAGE)

    def test_foreign_database_reported_cleanly(self) -> None:
        import sqlite3

        foreign = self.root / "foreign.sqlite3"
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()
        code, _, err = run_cli("status", "--db", str(foreign))
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("不是本模块创建的数据库", err)

    def test_corrupt_database_reported_cleanly(self) -> None:
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"this is not a sqlite database at all")
        code, _, err = run_cli("status", "--db", str(corrupt))
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertTrue(err.strip())
        self.assertNotIn("Traceback", err)

    def test_no_command_leaks_a_traceback(self) -> None:
        """任何失败路径都必须是干净的报错，不抛裸异常。"""
        cases = [
            ("status", "--db", str(self.root / "nope" / "db.sqlite3")),
            ("deposit", "--amount", "x", "--confirm", "y"),
            ("withdraw", "--amount", "1", "--confirm", "z"),
            ("close",),
            ("quote",),
        ]
        for argv in cases:
            with mock.patch.object(cli, "get_current_quote", side_effect=MarketDataError("x")):
                code, _, err = self.cli(*argv)
            self.assertNotIn("Traceback", err, msg=str(argv))
            self.assertIsInstance(code, int, msg=str(argv))


if __name__ == "__main__":
    unittest.main(verbosity=2)
