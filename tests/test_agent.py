"""agent 模块的自动化测试。

覆盖：工具调用驱动的决策循环、被拒后回灌原因重试、dry-run 与 live 的区别、
幂等（同一 decision_id 不重复开仓）、hold / 未知工具 / 参数非法 / 轮数用尽、
平仓工具、审计记录、「模型无法绕过引擎风控」这条硬约束，
以及记忆笔记（`remember_note` 非终止、dry-run 可写、幂等、上限、进审计链、回灌闭环）
和上下文缓存（稳定前缀逐字节不变、限额由配置生成、system 里没有易变内容）。

全部使用**假 LLM 客户端**，**不访问网络**、不消耗 token、不连接券商。

运行方式（仓库根目录）::

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import os
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

import agent as agent_mod  # noqa: E402
from agent import (  # noqa: E402
    AGENT_TOOLS,
    CLOSE_POSITION_TOOL,
    DEFAULT_HISTORY_HOURS,
    DEFAULT_MAX_NOTES_IN_CONTEXT,
    HOLD_TOOL,
    NOTE_KINDS,
    OUTCOME_DUPLICATE,
    OUTCOME_ERROR,
    OUTCOME_EXECUTED,
    OUTCOME_HELD,
    OUTCOME_NO_DECISION,
    OUTCOME_VALIDATED,
    REMEMBER_NOTE_TOOL,
    SUBMIT_INTENT_TOOL,
    TradingAgent,
    build_decision_context,
    build_static_context,
    build_system_prompt,
    hour_key_for,
    summarize_hour,
)
from config import load_config  # noqa: E402
from engine import TradingEngine  # noqa: E402
from llm_client import (  # noqa: E402
    ChatResponse,
    LLMEmptyCompletionError,
    LLMTransportError,
    ToolCall,
    Usage,
)
from market_data import (  # noqa: E402
    Granularity,
    HourlyRange,
    HistoryUnavailableError,
    Quote,
    QuoteType,
)
from memory import MemoryStore  # noqa: E402
from storage import PositionStatus, Storage  # noqa: E402

_JST = timezone(timedelta(hours=9))
_INITIAL = Decimal("50000")

_VALID_INTENT = {
    "side": "LONG",
    "volume_usd": 1000,
    "stop_loss": 157.95,
    "take_profit": 158.10,
    "reason": "突破回踩",
}
_BAD_RATIO_INTENT = {
    "side": "LONG",
    "volume_usd": 1000,
    "stop_loss": 157.95,
    "take_profit": 158.02,  # 盈亏比不足
    "reason": "盈亏比不足",
}


def make_quote(bid: str = "158.000", ask: str = "158.010") -> Quote:
    bid_d, ask_d = Decimal(bid), Decimal(ask)
    return Quote(
        symbol="USD/JPY", bid=bid_d, ask=ask_d, mid=(bid_d + ask_d) / 2,
        timestamp=datetime.now(_JST), source="agent_test", source_url="",
        quote_type=QuoteType.BID_ASK, granularity=Granularity.TICK,
        is_stale=False, age_seconds=Decimal("0"),
    )


def make_hourly_ranges(now: datetime | None = None, *, count: int = 3):
    """构造一段**离线**的按小时价格区间（测试用，不联网）。"""
    moment = now or datetime(2026, 10, 10, 14, 30, tzinfo=_JST)
    base = moment.astimezone(_JST).replace(minute=0, second=0, microsecond=0)
    ranges = []
    for index in range(count):
        start = base - timedelta(hours=count - 1 - index)
        low = Decimal("157.00") + Decimal(index) / 10
        ranges.append(
            HourlyRange(
                hour=start,
                bar_count=60,
                open=low,
                high=low + Decimal("0.50"),
                low=low - Decimal("0.20"),
                close=low + Decimal("0.30"),
                is_partial=index == count - 1,
                source="test_history",
            )
        )
    return tuple(ranges)


def tool_reply(name: str, arguments: dict[str, Any], *, call_id: str = "call_1") -> ChatResponse:
    call = ToolCall(
        id=call_id, name=name, arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )
    return ChatResponse(
        content="", reasoning_content=None, tool_calls=(call,),
        finish_reason="tool_calls", model="fake-model", usage=Usage(10, 20, 30, 5), raw={},
    )


def text_reply(content: str) -> ChatResponse:
    return ChatResponse(
        content=content, reasoning_content=None, tool_calls=(),
        finish_reason="stop", model="fake-model", usage=Usage(5, 5, 10, 0), raw={},
    )


class ScriptedClient:
    """按脚本返回响应，并记录收到的消息（用于断言「回灌」内容）。"""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, **_kwargs) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        if not self.replies:
            raise AssertionError("脚本已用尽")
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def last_messages(self) -> list[dict[str, Any]]:
        return self.calls[-1]


class AgentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "data" / "fxsim.sqlite3"
        # 规则机制测试钉住规格数值，不随出货配置的风险偏好漂移
        import dataclasses

        base = load_config()
        self.cfg = dataclasses.replace(
            base,
            risk=dataclasses.replace(
                base.risk,
                max_margin_usage_ratio=Decimal("0.60"),
                max_risk_per_trade_ratio=Decimal("0.02"),
                max_total_risk_ratio=Decimal("0.05"),
                daily_drawdown_stop_ratio=Decimal("0.03"),
                min_reward_risk_ratio=Decimal("1.5"),
            ),
            margin=dataclasses.replace(
                base.margin, min_equity_to_open_jpy=Decimal("5000")
            ),
            market=dataclasses.replace(
                base.market, spread_jpy=Decimal("0.01"), slippage_jpy=Decimal("0.005")
            ),
        )
        self.storage = Storage(self.db, tz=_JST)
        self.storage.initialize_account(_INITIAL)
        self.engine = TradingEngine(self.cfg, self.storage)
        self.quote = make_quote()
        self.memory_root = self.root / "memory"
        self.hourly_calls: list[dict] = []

    # -- 价格历史：默认**离线**，测试绝不联网 ------------------------------- #

    def hourly_source(self, ranges=None):
        """返回一个假的「按小时价格区间」数据源，并记录调用参数。"""

        def _source(*, hours=12, cfg=None, now=None, **kwargs):
            self.hourly_calls.append({"hours": hours, "now": now})
            if isinstance(ranges, BaseException):
                raise ranges
            if ranges is not None:
                return tuple(ranges)
            return make_hourly_ranges(now)

        return _source

    def make_agent(self, *replies: Any, max_rounds: int = 3, **kwargs) -> TradingAgent:
        self.client = ScriptedClient(*replies)
        kwargs.setdefault("hourly_source", self.hourly_source())
        kwargs.setdefault("memory", MemoryStore(self.cfg, storage=self.storage,
                                                root=self.memory_root))
        return TradingAgent(
            self.cfg, engine=self.engine, storage=self.storage,
            client=self.client, max_rounds=max_rounds, **kwargs
        )

    def decide(self, *replies: Any, dry_run: bool = True, decision_id: str | None = None,
               max_rounds: int = 3, now: datetime | None = None):
        agent = self.make_agent(*replies, max_rounds=max_rounds)
        return agent.decide(quote=self.quote, dry_run=dry_run, decision_id=decision_id,
                            max_rounds=max_rounds, now=now)


# --------------------------------------------------------------------------- #
# 1. 工具声明与「模型不能指定钱」
# --------------------------------------------------------------------------- #


class TestToolSchemas(unittest.TestCase):
    def test_four_tools_declared_in_stable_order(self) -> None:
        """工具顺序固定：它是缓存前缀的一部分，改动顺序等于让前缀缓存失效。"""
        names = [t["function"]["name"] for t in AGENT_TOOLS]
        self.assertEqual(
            names, ["submit_trade_intent", "close_position", "hold", "remember_note"]
        )

    def test_tool_schema_is_byte_stable(self) -> None:
        """同一进程内反复取值必须逐字节一致（缓存前缀的前提）。"""
        import json as _json

        first = _json.dumps(AGENT_TOOLS, ensure_ascii=False, sort_keys=False)
        second = _json.dumps(list(AGENT_TOOLS), ensure_ascii=False, sort_keys=False)
        self.assertEqual(first, second)

    def test_submit_intent_parameters(self) -> None:
        params = SUBMIT_INTENT_TOOL["function"]["parameters"]
        self.assertEqual(
            sorted(params["properties"]),
            ["reason", "side", "stop_loss", "take_profit", "volume_usd"],
        )
        self.assertEqual(
            sorted(params["required"]),
            ["reason", "side", "stop_loss", "take_profit", "volume_usd"],
        )
        self.assertEqual(params["properties"]["side"]["enum"], ["LONG", "SHORT"])

    def test_model_cannot_set_price_margin_or_pnl(self) -> None:
        """工具参数里不得出现成交价、保证金、盈亏、余额之类的字段。"""
        for tool in AGENT_TOOLS:
            props = tool["function"]["parameters"]["properties"]
            for forbidden in ("price", "entry_price", "margin", "pnl", "equity",
                              "balance", "fill_price", "notional"):
                self.assertNotIn(forbidden, props, msg=f"{tool['function']['name']}.{forbidden}")

    def test_hold_and_close_have_reason(self) -> None:
        for tool in (HOLD_TOOL, CLOSE_POSITION_TOOL):
            self.assertIn("reason", tool["function"]["parameters"]["properties"])
            self.assertEqual(tool["function"]["parameters"]["required"], ["reason"])


# --------------------------------------------------------------------------- #
# 2. 决策循环
# --------------------------------------------------------------------------- #


class TestDecisionLoop(AgentTestCase):
    def test_valid_intent_in_dry_run_is_validated_not_executed(self) -> None:
        result = self.decide(tool_reply("submit_trade_intent", _VALID_INTENT), dry_run=True,
                             decision_id="d1")
        self.assertEqual(result.outcome, OUTCOME_VALIDATED)
        self.assertTrue(result.accepted)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.validation.entry_price, Decimal("158.010"))
        self.assertEqual(result.validation.required_margin_jpy, Decimal("6320.400"))
        self.assertEqual(result.validation.reward_risk_ratio, Decimal("1.5"))
        # dry-run 不落单
        self.assertEqual(self.storage.counts()["positions"], 0)
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)

    def test_live_intent_is_executed(self) -> None:
        result = self.decide(tool_reply("submit_trade_intent", _VALID_INTENT), dry_run=False,
                             decision_id="d2")
        self.assertEqual(result.outcome, OUTCOME_EXECUTED)
        self.assertFalse(result.dry_run)
        position = self.storage.read_positions(status=PositionStatus.OPEN)
        self.assertEqual(len(position), 1)
        self.assertEqual(position[0].entry_price, Decimal("158.010"))
        self.assertEqual(self.storage.counts()["trades"], 1)
        # 开仓不产生现金流
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)

    def test_rejected_intent_feeds_reasons_back_and_retries(self) -> None:
        result = self.decide(
            tool_reply("submit_trade_intent", _BAD_RATIO_INTENT, call_id="c1"),
            tool_reply("submit_trade_intent", _VALID_INTENT, call_id="c2"),
            dry_run=True, decision_id="d3",
        )
        self.assertEqual(result.outcome, OUTCOME_VALIDATED)
        self.assertEqual(result.rounds, 2)
        self.assertFalse(result.invocations[0].accepted)
        self.assertTrue(result.invocations[1].accepted)
        self.assertTrue(result.invocations[0].result["problems"])
        # 拒绝原因必须回灌给模型
        second_round = json.dumps(self.client.calls[1], ensure_ascii=False)
        self.assertIn("盈亏比", second_round)
        self.assertIn("submit_trade_intent", second_round)

    def test_hold_is_terminal(self) -> None:
        result = self.decide(tool_reply("hold", {"reason": "行情不明"}), dry_run=False,
                             decision_id="d4")
        self.assertEqual(result.outcome, OUTCOME_HELD)
        self.assertTrue(result.ok)
        # hold 是一次被接受的决定，不能显示成「未通过」
        self.assertTrue(result.invocations[0].accepted)
        self.assertEqual(self.storage.counts()["positions"], 0)

    def test_no_tool_calls_is_no_decision(self) -> None:
        result = self.decide(text_reply("我觉得可以再等等"), decision_id="d5")
        self.assertEqual(result.outcome, OUTCOME_NO_DECISION)
        self.assertFalse(result.ok)
        self.assertTrue(result.errors)

    def test_rounds_exhausted(self) -> None:
        result = self.decide(
            tool_reply("submit_trade_intent", _BAD_RATIO_INTENT, call_id="c1"),
            tool_reply("submit_trade_intent", _BAD_RATIO_INTENT, call_id="c2"),
            dry_run=True, decision_id="d6", max_rounds=2,
        )
        self.assertEqual(result.outcome, OUTCOME_ERROR)
        self.assertEqual(result.rounds, 2)
        self.assertTrue(any("轮内" in e for e in result.errors))
        self.assertEqual(self.storage.counts()["positions"], 0)

    def test_llm_transport_error_is_captured(self) -> None:
        result = self.decide(LLMTransportError("连接被拒绝"), decision_id="d7")
        self.assertEqual(result.outcome, OUTCOME_ERROR)
        self.assertIn("LLM 调用失败", result.message)
        self.assertTrue(any("LLMTransportError" in e for e in result.errors))

    def test_empty_completion_is_captured_with_hint(self) -> None:
        result = self.decide(LLMEmptyCompletionError("推理 token 吃满了 max_tokens"), decision_id="d8")
        self.assertEqual(result.outcome, OUTCOME_ERROR)
        self.assertIn("max_tokens", result.message)
        self.assertTrue(any("max_tokens" in e for e in result.errors))

    def test_unknown_tool_is_not_accepted(self) -> None:
        result = self.decide(
            tool_reply("set_balance", {"amount": 999999}, call_id="c1"),
            text_reply("放弃"), decision_id="d9", max_rounds=2,
        )
        self.assertFalse(result.invocations[0].accepted)
        self.assertIn("未知工具", result.invocations[0].result["error"])
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)

    def test_malformed_arguments_are_not_accepted(self) -> None:
        bad = {"side": "SIDEWAYS", "volume_usd": "abc", "stop_loss": None,
               "take_profit": 1, "reason": "x"}
        result = self.decide(tool_reply("submit_trade_intent", bad), text_reply("放弃"),
                             decision_id="d10", max_rounds=2)
        self.assertFalse(result.invocations[0].accepted)
        self.assertIn("参数无法解析", result.invocations[0].result["error"])

    def test_multiple_tool_calls_in_one_round(self) -> None:
        first = tool_reply("submit_trade_intent", _BAD_RATIO_INTENT, call_id="c1")
        second = ToolCall(id="c2", name="hold", arguments={"reason": "算了"},
                          raw_arguments='{"reason":"算了"}')
        reply = ChatResponse(
            content="", reasoning_content=None, tool_calls=(first.tool_calls[0], second),
            finish_reason="tool_calls", model="fake", usage=None, raw={},
        )
        result = self.decide(reply, decision_id="d11")
        # 先处理 submit（被拒，不终止），再处理 hold（终止）
        self.assertEqual(result.outcome, OUTCOME_HELD)
        self.assertEqual(len(result.invocations), 2)

    def test_usage_accumulated_across_rounds(self) -> None:
        result = self.decide(
            tool_reply("submit_trade_intent", _BAD_RATIO_INTENT, call_id="c1"),
            tool_reply("submit_trade_intent", _VALID_INTENT, call_id="c2"),
            decision_id="d12",
        )
        self.assertEqual(result.usage["prompt_tokens"], 20)
        self.assertEqual(result.usage["completion_tokens"], 40)
        self.assertEqual(result.usage["reasoning_tokens"], 10)


# --------------------------------------------------------------------------- #
# 3. 幂等
# --------------------------------------------------------------------------- #


class TestIdempotency(AgentTestCase):
    def test_same_decision_id_does_not_open_twice(self) -> None:
        self.decide(tool_reply("submit_trade_intent", _VALID_INTENT), dry_run=False,
                    decision_id="dup")
        self.assertEqual(self.storage.counts()["positions"], 1)
        result = self.decide(tool_reply("submit_trade_intent", _VALID_INTENT), dry_run=False,
                             decision_id="dup")
        self.assertEqual(result.outcome, OUTCOME_DUPLICATE)
        self.assertTrue(result.ok)
        self.assertEqual(self.storage.counts()["positions"], 1)
        self.assertEqual(self.storage.counts()["trades"], 1)

    def test_duplicate_is_detected_before_risk_rule_rejection(self) -> None:
        """重复指令不能被误报成「被风控拒绝（已有持仓）」。"""
        self.decide(tool_reply("submit_trade_intent", _VALID_INTENT), dry_run=False,
                    decision_id="dup2")
        result = self.decide(tool_reply("submit_trade_intent", _VALID_INTENT), dry_run=False,
                             decision_id="dup2")
        self.assertEqual(result.outcome, OUTCOME_DUPLICATE)
        self.assertEqual(result.rounds, 1)
        self.assertNotIn("禁止加仓", result.message)

    def test_different_decision_ids_second_is_rejected_by_rule_8(self) -> None:
        self.decide(tool_reply("submit_trade_intent", _VALID_INTENT), dry_run=False,
                    decision_id="a")
        result = self.decide(tool_reply("submit_trade_intent", _VALID_INTENT), dry_run=False,
                             decision_id="b", max_rounds=1)
        self.assertEqual(result.outcome, OUTCOME_ERROR)
        self.assertTrue(any("禁止加仓" in p for inv in result.invocations
                            for p in (inv.result.get("problems") or [])))
        self.assertEqual(self.storage.counts()["positions"], 1)

    def test_auto_decision_id_is_unique(self) -> None:
        first = self.decide(text_reply("x"))
        second = self.decide(text_reply("x"))
        self.assertNotEqual(first.decision_id, second.decision_id)


# --------------------------------------------------------------------------- #
# 4. 平仓工具
# --------------------------------------------------------------------------- #


class TestCloseTool(AgentTestCase):
    def open_position(self):
        from engine import PositionSide, TradeIntent

        return self.engine.execute_virtual_trade(
            TradeIntent(symbol="USD/JPY", side=PositionSide.LONG, volume_usd=Decimal("1000"),
                        stop_loss=Decimal("157.950"), take_profit=Decimal("158.100"),
                        intent_id="fixture"),
            make_quote(),
        )

    def test_close_without_position_is_not_accepted(self) -> None:
        result = self.decide(tool_reply("close_position", {"reason": "平掉"}, call_id="c1"),
                             text_reply("那就先不动"), dry_run=False, decision_id="c1",
                             max_rounds=2)
        self.assertFalse(result.invocations[0].accepted)
        self.assertIn("没有未平仓持仓", result.invocations[0].result["error"])

    def test_close_in_dry_run_does_not_close(self) -> None:
        self.open_position()
        result = self.decide(tool_reply("close_position", {"reason": "平掉"}), dry_run=True,
                             decision_id="c2")
        self.assertTrue(result.invocations[0].accepted)
        self.assertEqual(len(self.storage.read_positions(status=PositionStatus.OPEN)), 1)

    def test_close_live_closes_position(self) -> None:
        self.open_position()
        result = self.decide(tool_reply("close_position", {"reason": "主动平仓"}),
                             dry_run=False, decision_id="c3")
        self.assertEqual(result.outcome, OUTCOME_EXECUTED)
        self.assertEqual(self.storage.read_positions(status=PositionStatus.OPEN), [])
        self.assertEqual(len(self.storage.query_trades(action="CLOSE")), 1)
        self.assertIn("主动平仓", result.message)


# --------------------------------------------------------------------------- #
# 5. 上下文与审计
# --------------------------------------------------------------------------- #


class TestContextAndAudit(AgentTestCase):
    def test_context_contains_facts(self) -> None:
        context = build_decision_context(self.cfg, self.engine, self.storage, self.quote)
        self.assertEqual(context["symbol"], "USD/JPY")
        self.assertEqual(context["market"]["bid"], "158.000")
        self.assertEqual(context["market"]["ask"], "158.010")
        self.assertTrue(context["market"]["valid_for_decisions"])
        self.assertEqual(context["account"]["cash_balance"], "50000.000")
        self.assertEqual(context["account"]["status"], "ACTIVE")
        self.assertEqual(context["open_positions"], [])

    def test_limits_live_in_static_context_not_volatile(self) -> None:
        """规则与限额属于稳定前缀（system），不该混进易变的 user 那段。"""
        context = build_decision_context(self.cfg, self.engine, self.storage, self.quote)
        self.assertNotIn("risk_limits", context)
        static = build_static_context(self.cfg)
        limits = static["risk_limits"]
        self.assertEqual(static["min_trade_unit_usd"], "1000")
        self.assertEqual(limits["max_leverage"], "25")
        self.assertEqual(limits["max_margin_usage_ratio"], "0.60")
        self.assertEqual(limits["max_risk_per_trade_ratio"], "0.02")
        self.assertEqual(limits["min_reward_risk_ratio"], "1.5")

    def test_account_block_exposes_daily_risk_state(self) -> None:
        """模型必须看得到当日回撤这类会直接决定「能不能开仓」的状态。"""
        account = build_decision_context(
            self.cfg, self.engine, self.storage, self.quote
        )["account"]
        for key in (
            "daily_drawdown_ratio", "day_start_equity", "peak_equity",
            "margin_usage_ratio", "effective_leverage", "net_profit", "return_pct",
        ):
            self.assertIn(key, account, msg=key)

    def test_context_contains_no_secrets(self) -> None:
        with mock.patch.dict(os.environ, {"KURUMI_LLM_API_KEY": "sk-secret-value-1234567890"},
                             clear=False):
            cfg = load_config()
        context = build_decision_context(cfg, self.engine, self.storage, self.quote)
        self.assertNotIn("sk-secret-value", json.dumps(context, ensure_ascii=False))
        prompt = build_system_prompt(cfg)
        self.assertNotIn("sk-secret-value", prompt)

    def test_context_without_quote_marks_unverifiable(self) -> None:
        context = build_decision_context(self.cfg, self.engine, self.storage, None)
        self.assertFalse(context["market"]["quote_available"])
        self.assertIsNone(context["market"]["bid"])
        self.assertIsNone(context["account"]["equity"])
        self.assertTrue(context["unverifiable_fields"])

    def test_system_prompt_numbers_come_from_config(self) -> None:
        """提示词里的数字必须**由配置生成**，不能在源码里另写一份。"""
        import dataclasses

        prompt = self.make_agent().system_prompt()
        for needle in ("1000 美元", "1.5 倍", "2%", "60%", "3%", "5000 日元", "最多持有一笔"):
            self.assertIn(needle, prompt)

        # 改配置 → 提示词跟着变（这是「不在源码里硬编码规则」的证明）
        tighter = dataclasses.replace(
            self.cfg,
            risk=dataclasses.replace(
                self.cfg.risk,
                max_risk_per_trade_ratio=Decimal("0.005"),
                max_margin_usage_ratio=Decimal("0.25"),
                daily_drawdown_stop_ratio=Decimal("0.01"),
            ),
            instrument=dataclasses.replace(self.cfg.instrument, min_trade_unit=Decimal("5000")),
        )
        changed = build_system_prompt(tighter)
        self.assertIn("5000 美元", changed)
        self.assertIn("0.5%", changed)
        self.assertIn("25%", changed)
        self.assertIn("1%", changed)
        self.assertNotEqual(prompt, changed)

    def test_system_prompt_has_no_volatile_content(self) -> None:
        """system 提示词里不能出现时刻/行情等易变字样，否则缓存前缀永远不命中。"""
        prompt = self.make_agent().system_prompt()
        for needle in ("2026-", "158.0", "now_jst", "trading_date"):
            self.assertNotIn(needle, prompt, msg=needle)

    def test_decision_is_audited(self) -> None:
        self.decide(tool_reply("submit_trade_intent", _VALID_INTENT), dry_run=False,
                    decision_id="audit-1")
        events = [e for e in self.storage.read_audit_events() if e.event_type == "AI_DECISION"]
        self.assertEqual(len(events), 1)
        payload = events[0].payload
        self.assertEqual(payload["decision_id"], "audit-1")
        self.assertEqual(payload["outcome"], OUTCOME_EXECUTED)
        self.assertFalse(payload["dry_run"])
        self.assertEqual(payload["position_id"], 1)
        self.assertEqual(payload["market_bid"], "158.000")

    def test_rejected_decision_audit_records_problems(self) -> None:
        self.decide(
            tool_reply("submit_trade_intent", _BAD_RATIO_INTENT, call_id="c1"),
            tool_reply("submit_trade_intent", _BAD_RATIO_INTENT, call_id="c2"),
            decision_id="audit-2", max_rounds=2,
        )
        events = [e for e in self.storage.read_audit_events() if e.event_type == "AI_DECISION"]
        self.assertTrue(events)
        self.assertEqual(events[0].payload["outcome"], OUTCOME_ERROR)

    def test_audit_can_be_disabled(self) -> None:
        agent = self.make_agent(tool_reply("hold", {"reason": "等"}))
        agent.decide(quote=self.quote, decision_id="no-audit", record_audit=False)
        events = [e for e in self.storage.read_audit_events() if e.event_type == "AI_DECISION"]
        self.assertEqual(events, [])

    def test_audit_chain_stays_valid(self) -> None:
        self.decide(tool_reply("submit_trade_intent", _VALID_INTENT), dry_run=False,
                    decision_id="chain")
        self.assertTrue(self.storage.verify_audit_chain().ok)
        self.assertTrue(self.engine.reconcile_account(strict=False).ok)


# --------------------------------------------------------------------------- #
# 6. 风控不可绕过
# --------------------------------------------------------------------------- #


class TestRiskCannotBeBypassed(AgentTestCase):
    def test_margin_usage_violation_is_rejected(self) -> None:
        result = self.decide(
            tool_reply("submit_trade_intent", {**_VALID_INTENT, "volume_usd": 5000}),
            text_reply("放弃"), dry_run=False, decision_id="r1", max_rounds=2,
        )
        self.assertNotEqual(result.outcome, OUTCOME_EXECUTED)
        problems = [p for inv in result.invocations for p in (inv.result.get("problems") or [])]
        self.assertTrue(any("保证金" in p for p in problems))
        self.assertEqual(self.storage.counts()["positions"], 0)

    def test_volume_not_multiple_is_rejected(self) -> None:
        result = self.decide(
            tool_reply("submit_trade_intent", {**_VALID_INTENT, "volume_usd": 1500}),
            text_reply("放弃"), decision_id="r2", max_rounds=2,
        )
        problems = [p for inv in result.invocations for p in (inv.result.get("problems") or [])]
        self.assertTrue(any("整数倍" in p for p in problems))

    def test_halted_account_blocks_open(self) -> None:
        self.engine.set_daily_drawdown_stop()
        self.engine.mark_to_market(self.quote)
        result = self.decide(
            tool_reply("submit_trade_intent", _VALID_INTENT),
            text_reply("放弃"), dry_run=False, decision_id="r3", max_rounds=2,
        )
        problems = [p for inv in result.invocations for p in (inv.result.get("problems") or [])]
        self.assertTrue(any("停止开仓" in p or "回撤" in p for p in problems))
        self.assertEqual(self.storage.counts()["positions"], 0)

    def test_stale_quote_blocks_open(self) -> None:
        stale = Quote(
            symbol="USD/JPY", bid=Decimal("158.000"), ask=Decimal("158.010"),
            mid=Decimal("158.005"), timestamp=datetime.now(_JST), source="t", source_url="",
            quote_type=QuoteType.BID_ASK, granularity=Granularity.TICK, is_stale=True,
            age_seconds=Decimal("999"),
        )
        agent = self.make_agent(tool_reply("submit_trade_intent", _VALID_INTENT),
                                text_reply("放弃"))
        result = agent.decide(quote=stale, dry_run=False, decision_id="r4", max_rounds=2)
        problems = [p for inv in result.invocations for p in (inv.result.get("problems") or [])]
        self.assertTrue(any("过期" in p for p in problems))
        self.assertEqual(self.storage.counts()["positions"], 0)

    def test_gate_block_blocks_open(self) -> None:
        from engine import ACCEPTANCE_GATE_META_KEY, GATE_BLOCKED_PREFIX

        self.storage.set_meta(ACCEPTANCE_GATE_META_KEY, f"{GATE_BLOCKED_PREFIX}测试")
        result = self.decide(
            tool_reply("submit_trade_intent", _VALID_INTENT),
            text_reply("放弃"), dry_run=False, decision_id="r5", max_rounds=2,
        )
        problems = [p for inv in result.invocations for p in (inv.result.get("problems") or [])]
        self.assertTrue(any("验收闸门" in p for p in problems))
        self.assertEqual(self.storage.counts()["positions"], 0)

    def test_agent_never_writes_balance_directly(self) -> None:
        self.decide(tool_reply("submit_trade_intent", _VALID_INTENT), dry_run=False,
                    decision_id="r6")
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)
        self.assertTrue(self.engine.reconcile_account(strict=False).ok)


# --------------------------------------------------------------------------- #
# 7. CLI
# --------------------------------------------------------------------------- #


class TestAgentCli(AgentTestCase):
    def _run(self, *argv: str) -> tuple[int, str, str]:
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(agent_mod, "load_config", return_value=self.cfg), \
             mock.patch.object(agent_mod.TradingAgent, "from_config",
                               return_value=self.make_agent(tool_reply("hold", {"reason": "等"}))), \
             mock.patch.object(agent_mod, "get_current_quote", return_value=self.quote), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = agent_mod._main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_hold(self) -> None:
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("HELD", out)

    def test_cli_live_flag_is_explicit(self) -> None:
        code, out, _ = self._run("--live")
        self.assertEqual(code, 0)
        self.assertIn("已执行", out)

    def test_cli_show_context_prints_both_segments(self) -> None:
        """必须同时打印稳定前缀（system）与易变尾部（user），否则看不到模型读到的规则。"""
        code, out, _ = self._run("--show-context")
        self.assertEqual(code, 0)
        self.assertIn("稳定前缀", out)
        self.assertIn("易变尾部", out)
        self.assertIn("risk_limits", out)
        self.assertIn("my_notes", out)


# --------------------------------------------------------------------------- #
# 8. 记忆：remember_note
# --------------------------------------------------------------------------- #


class TestNotesTool(AgentTestCase):
    def test_note_tool_declared(self) -> None:
        params = REMEMBER_NOTE_TOOL["function"]["parameters"]
        self.assertEqual(sorted(params["properties"]), ["content", "kind", "tags"])
        self.assertEqual(sorted(params["required"]), ["content", "kind"])
        self.assertEqual(params["properties"]["kind"]["enum"], list(NOTE_KINDS))

    def test_note_tool_does_not_touch_money(self) -> None:
        """笔记工具里同样不得出现余额/成交价/盈亏字段。"""
        props = REMEMBER_NOTE_TOOL["function"]["parameters"]["properties"]
        for forbidden in ("price", "margin", "pnl", "equity", "balance", "volume_usd"):
            self.assertNotIn(forbidden, props)

    def test_note_is_recorded(self) -> None:
        result = self.decide(
            tool_reply("remember_note", {
                "kind": "LESSON", "content": "周末报价会过期，别在休市时开仓",
                "tags": ["weekend"],
            }),
            tool_reply("hold", {"reason": "休市"}),
            decision_id="note-1",
        )
        self.assertEqual(result.outcome, OUTCOME_HELD)
        notes = self.storage.read_notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].kind, "LESSON")
        self.assertEqual(notes[0].tags, ("weekend",))
        self.assertEqual(notes[0].decision_id, "note-1")

    def test_note_does_not_terminate_the_loop(self) -> None:
        """记笔记是副产物：模型必须还能在同一轮里继续表达决策。"""
        result = self.decide(
            tool_reply("remember_note", {"kind": "MARKET_VIEW", "content": "158 附近震荡"}),
            tool_reply("hold", {"reason": "观望"}),
            decision_id="note-2",
        )
        self.assertEqual(result.outcome, OUTCOME_HELD)
        self.assertEqual([i.name for i in result.invocations],
                         ["remember_note", "hold"])
        self.assertTrue(all(i.accepted for i in result.invocations))

    def test_note_in_same_round_as_intent(self) -> None:
        """同一轮里先记笔记再提交意图：笔记不该吃掉开仓。"""
        result = self.decide(
            tool_reply("remember_note", {"kind": "DECISION_REASON", "content": "突破回踩"}),
            tool_reply("submit_trade_intent", _VALID_INTENT),
            dry_run=False,
            decision_id="note-3",
        )
        self.assertEqual(result.outcome, OUTCOME_EXECUTED)
        self.assertEqual(self.storage.counts()["positions"], 1)
        self.assertEqual(self.storage.count_notes(), 1)

    def test_note_works_in_dry_run(self) -> None:
        """dry-run 不阻止写笔记（笔记不是交易），否则调度器永远学不到东西。"""
        self.decide(
            tool_reply("remember_note", {"kind": "PLAN", "content": "跌到 157 再考虑做多"}),
            tool_reply("hold", {"reason": "等价位"}),
            dry_run=True, decision_id="note-4",
        )
        self.assertEqual(self.storage.count_notes(), 1)
        self.assertEqual(self.storage.counts()["positions"], 0)

    def test_note_written_twice_in_one_decision_is_deduped(self) -> None:
        """同一次决策内重复写同一条笔记只留一条（event_key 幂等）。"""
        self.decide(
            tool_reply("remember_note", {"kind": "LESSON", "content": "重复内容"}, call_id="c1"),
            tool_reply("remember_note", {"kind": "LESSON", "content": "重复内容"}, call_id="c2"),
            tool_reply("hold", {"reason": "完事"}),
            decision_id="note-5",
        )
        self.assertEqual(self.storage.count_notes(), 1)

    def test_note_across_decisions_is_kept_separately(self) -> None:
        for did in ("note-6a", "note-6b"):
            self.decide(
                tool_reply("remember_note", {"kind": "LESSON", "content": "同一句话"}),
                tool_reply("hold", {"reason": "完事"}),
                decision_id=did,
            )
        self.assertEqual(self.storage.count_notes(), 2)

    def test_bad_kind_is_rejected_without_terminating(self) -> None:
        result = self.decide(
            tool_reply("remember_note", {"kind": "NOT_A_KIND", "content": "x"}),
            tool_reply("hold", {"reason": "算了"}),
            decision_id="note-7",
        )
        self.assertEqual(result.outcome, OUTCOME_HELD)
        self.assertEqual(self.storage.count_notes(), 0)
        self.assertFalse(result.invocations[0].accepted)
        self.assertIn("kind", result.invocations[0].result["error"])

    def test_empty_content_is_rejected(self) -> None:
        result = self.decide(
            tool_reply("remember_note", {"kind": "LESSON", "content": "   "}),
            tool_reply("hold", {"reason": "算了"}),
            decision_id="note-8",
        )
        self.assertEqual(self.storage.count_notes(), 0)
        self.assertFalse(result.invocations[0].accepted)

    def test_bad_tags_type_is_rejected(self) -> None:
        result = self.decide(
            tool_reply("remember_note", {"kind": "LESSON", "content": "x", "tags": 42}),
            tool_reply("hold", {"reason": "算了"}),
            decision_id="note-9",
        )
        self.assertEqual(self.storage.count_notes(), 0)
        self.assertFalse(result.invocations[0].accepted)

    def test_tags_are_capped(self) -> None:
        self.decide(
            tool_reply("remember_note", {
                "kind": "LESSON", "content": "标签太多",
                "tags": [f"t{i}" for i in range(12)],
            }),
            tool_reply("hold", {"reason": "完事"}),
            decision_id="note-10",
        )
        self.assertLessEqual(len(self.storage.read_notes()[0].tags), 5)

    def test_oversized_content_is_rejected(self) -> None:
        """笔记有硬上限，否则模型能靠写长文把自己的上下文撑爆。"""
        from storage import MAX_NOTE_CHARS

        result = self.decide(
            tool_reply("remember_note", {"kind": "LESSON", "content": "x" * (MAX_NOTE_CHARS + 1)}),
            tool_reply("hold", {"reason": "算了"}),
            decision_id="note-11",
        )
        self.assertEqual(self.storage.count_notes(), 0)
        self.assertFalse(result.invocations[0].accepted)
        self.assertIn("过长", result.invocations[0].result["error"])

    def test_note_is_in_audit_chain(self) -> None:
        self.decide(
            tool_reply("remember_note", {"kind": "LESSON", "content": "进审计链"}),
            tool_reply("hold", {"reason": "完事"}),
            decision_id="note-12",
        )
        types = [e.event_type for e in self.storage.read_audit_events()]
        self.assertIn("AGENT_NOTE", types)
        self.assertTrue(self.storage.verify_audit_chain().ok)

    def test_note_does_not_move_money(self) -> None:
        self.decide(
            tool_reply("remember_note", {"kind": "LESSON", "content": "不动账"}),
            tool_reply("hold", {"reason": "完事"}),
            dry_run=False, decision_id="note-13",
        )
        self.assertEqual(self.storage.ledger_balance(), _INITIAL)
        self.assertTrue(self.engine.reconcile_account(strict=False).ok)


# --------------------------------------------------------------------------- #
# 9. 笔记回灌（记忆闭环）
# --------------------------------------------------------------------------- #


class TestNotesInContext(AgentTestCase):
    def seed_notes(self, count: int) -> None:
        for index in range(count):
            self.storage.record_note(
                "LESSON", f"第 {index} 条心得",
                tags=["seed"], decision_id=f"seed-{index}",
                occurred_at=datetime(2026, 10, 10, 8, 0, tzinfo=_JST) + timedelta(minutes=index),
            )

    def test_notes_appear_in_context_oldest_first(self) -> None:
        self.seed_notes(3)
        context = build_decision_context(self.cfg, self.engine, self.storage, self.quote)
        recent = context["my_notes"]["recent"]
        self.assertEqual([n["content"] for n in recent],
                         ["第 0 条心得", "第 1 条心得", "第 2 条心得"])
        self.assertEqual(context["my_notes"]["count_total"], 3)

    def test_context_carries_a_disclaimer(self) -> None:
        """笔记是模型自述，不是账本事实——必须在上下文里说清楚。"""
        self.seed_notes(1)
        context = build_decision_context(self.cfg, self.engine, self.storage, self.quote)
        self.assertIn("不是当前事实", context["my_notes"]["disclaimer"])

    def test_notes_are_capped(self) -> None:
        self.seed_notes(20)
        context = build_decision_context(
            self.cfg, self.engine, self.storage, self.quote, max_notes=5
        )
        self.assertEqual(len(context["my_notes"]["recent"]), 5)
        self.assertEqual(context["my_notes"]["count_total"], 20)

    def test_default_cap_is_bounded(self) -> None:
        self.assertGreater(DEFAULT_MAX_NOTES_IN_CONTEXT, 0)
        self.assertLessEqual(DEFAULT_MAX_NOTES_IN_CONTEXT, 20)

    def test_note_written_last_decision_is_visible_next_decision(self) -> None:
        """闭环：上一分钟写的笔记，下一次决策能看到。"""
        self.decide(
            tool_reply("remember_note", {"kind": "PLAN", "content": "记住：157.5 是支撑"}),
            tool_reply("hold", {"reason": "记一下"}),
            decision_id="loop-1",
        )
        agent = self.make_agent(tool_reply("hold", {"reason": "再看"}))
        agent.decide(quote=self.quote, dry_run=True, decision_id="loop-2")
        sent = json.dumps(agent.client.calls[0], ensure_ascii=False)
        self.assertIn("157.5 是支撑", sent)


# --------------------------------------------------------------------------- #
# 10. 上下文缓存：稳定前缀必须逐字节不变
# --------------------------------------------------------------------------- #


class TestContextCache(AgentTestCase):
    def test_system_prompt_is_byte_stable(self) -> None:
        agent = self.make_agent()
        self.assertEqual(agent.system_prompt(), agent.system_prompt())
        self.assertEqual(agent.system_prompt(), build_system_prompt(self.cfg))

    def test_stable_prefix_unchanged_when_market_and_clock_move(self) -> None:
        """同一小时内第二次决策：前缀（含上一回合）逐字节复用，只有尾部是新的。"""
        agent = self.make_agent(
            tool_reply("hold", {"reason": "a"}), tool_reply("hold", {"reason": "b"})
        )
        base = datetime(2026, 10, 10, 14, 5, tzinfo=_JST)
        early = make_quote("157.000", "157.010")
        late = make_quote("159.500", "159.520")
        agent.decide(quote=early, dry_run=True, decision_id="cache-1", now=base)
        agent.decide(quote=late, dry_run=True, decision_id="cache-2",
                     now=base + timedelta(minutes=10))

        first, second = agent.client.calls[0], agent.client.calls[1]
        self.assertEqual(first[0], second[0], "system 消息（稳定前缀）变了")
        # 第二次请求的前 len(first) 条必须与第一次**完全一致**——这就是缓存前缀
        self.assertEqual(second[: len(first)], first, "同一小时内的历史前缀必须逐字节一致")
        # 多出来的尾部 = 上一回合的 assistant + tool 结果，加上本次新的 user 消息
        self.assertEqual(
            [m["role"] for m in second[len(first):]], ["assistant", "tool", "user"]
        )
        self.assertNotEqual(second[-1], first[-1], "新一条 user 消息应当变化")
        self.assertIn("159.500", second[-1]["content"])

    def test_new_hour_starts_a_fresh_conversation(self) -> None:
        """跨小时不重放上一小时的原始对话（它已经被压缩成摘要了）。"""
        agent = self.make_agent(
            tool_reply("hold", {"reason": "a"}), tool_reply("hold", {"reason": "b"})
        )
        base = datetime(2026, 10, 10, 14, 5, tzinfo=_JST)
        agent.decide(quote=self.quote, dry_run=True, decision_id="h1", now=base)
        agent.decide(quote=self.quote, dry_run=True, decision_id="h2",
                     now=base + timedelta(hours=1))
        second = agent.client.calls[1]
        self.assertEqual(len(second), 2, "新小时应当只有 system + 当前 user")
        self.assertEqual(second[0]["role"], "system")
        self.assertEqual(second[1]["role"], "user")

    def test_tools_are_sent_identically(self) -> None:
        """工具声明是前缀的一部分，必须每次完全一致。"""
        agent = self.make_agent(tool_reply("hold", {"reason": "a"}))
        captured: list[Any] = []
        original = agent.client.chat

        def spy(messages, **kwargs):
            captured.append(kwargs.get("tools"))
            return original(messages, **kwargs)

        agent.client.chat = spy  # type: ignore[method-assign]
        agent.decide(quote=self.quote, dry_run=True, decision_id="cache-3")
        self.assertEqual(captured[0], list(AGENT_TOOLS))

    def test_volatile_block_has_no_config_limits(self) -> None:
        """限额只在稳定前缀里出现一次，易变尾部不要重复（省 token）。"""
        context = build_decision_context(self.cfg, self.engine, self.storage, self.quote)
        self.assertNotIn("risk_limits", context)
        self.assertNotIn("min_trade_unit_usd", context)

    def test_static_context_is_byte_stable(self) -> None:
        first = json.dumps(build_static_context(self.cfg), ensure_ascii=False, sort_keys=True)
        second = json.dumps(build_static_context(self.cfg), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)


# --------------------------------------------------------------------------- #
# 11. 按小时的价格区间
# --------------------------------------------------------------------------- #


class TestPriceHistory(AgentTestCase):
    def test_hourly_ranges_appear_in_context(self) -> None:
        context = build_decision_context(
            self.cfg, self.engine, self.storage, self.quote,
            price_history={"available": True, "hours": [{"hour": "2026-10-10T13:00+09:00"}]},
        )
        self.assertTrue(context["price_history"]["available"])
        self.assertEqual(len(context["price_history"]["hours"]), 1)

    def test_history_is_actually_requested(self) -> None:
        agent = self.make_agent(tool_reply("hold", {"reason": "看"}))
        agent.decide(quote=self.quote, dry_run=True, decision_id="ph-1")
        self.assertEqual(len(self.hourly_calls), 1)
        self.assertEqual(self.hourly_calls[0]["hours"], DEFAULT_HISTORY_HOURS)

    def test_history_hours_are_configurable(self) -> None:
        agent = self.make_agent(tool_reply("hold", {"reason": "看"}), history_hours=4)
        agent.decide(quote=self.quote, dry_run=True, decision_id="ph-2")
        self.assertEqual(self.hourly_calls[0]["hours"], 4)

    def test_unavailable_history_is_reported_not_faked(self) -> None:
        """拿不到历史必须如实说不可用，不能用当前报价凑数。"""
        agent = self.make_agent(
            tool_reply("hold", {"reason": "看"}),
            hourly_source=self.hourly_source(HistoryUnavailableError("源站只回溯 9 天")),
        )
        agent.decide(quote=self.quote, dry_run=True, decision_id="ph-3")
        sent = agent.client.calls[0][-1]["content"]
        payload = json.loads(sent.split("\n", 1)[1])
        self.assertFalse(payload["price_history"]["available"])
        self.assertIn("HistoryUnavailableError", payload["price_history"]["reason"])
        self.assertEqual(payload["price_history"]["hours"], [])

    def test_history_failure_does_not_abort_decision(self) -> None:
        agent = self.make_agent(
            tool_reply("hold", {"reason": "看"}),
            hourly_source=self.hourly_source(RuntimeError("炸了")),
        )
        result = agent.decide(quote=self.quote, dry_run=True, decision_id="ph-4")
        self.assertEqual(result.outcome, OUTCOME_HELD)

    def test_ranges_are_downsamples_not_raw_bars(self) -> None:
        """注入的是聚合后的区间（有 high/low/bar_count），不是 60 根原始 K 线。"""
        agent = self.make_agent(tool_reply("hold", {"reason": "看"}))
        agent.decide(quote=self.quote, dry_run=True, decision_id="ph-5",
                     now=datetime(2026, 10, 10, 14, 30, tzinfo=_JST))
        sent = agent.client.calls[0][-1]["content"]
        payload = json.loads(sent.split("\n", 1)[1])
        hours = payload["price_history"]["hours"]
        self.assertEqual(len(hours), 3)
        self.assertEqual(set(hours[0]), {
            "hour", "bar_count", "open", "high", "low", "close",
            "range_jpy", "is_partial", "has_data",
        })
        self.assertEqual(hours[0]["bar_count"], 60)
        self.assertTrue(hours[-1]["is_partial"])


# --------------------------------------------------------------------------- #
# 12. 逐小时对话存档与压缩
# --------------------------------------------------------------------------- #


class TestHourlyArchive(AgentTestCase):
    def test_turn_is_archived(self) -> None:
        result = self.decide(
            tool_reply("hold", {"reason": "看"}),
            decision_id="arch-1", now=datetime(2026, 10, 10, 14, 5, tzinfo=_JST),
        )
        self.assertTrue(result.archived)
        self.assertEqual(result.hour_key, "2026-10-10T14")
        turns = self.storage.read_turns(hour_key="2026-10-10T14")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].decision_id, "arch-1")
        self.assertEqual(turns[0].outcome, OUTCOME_HELD)
        self.assertIn("看", turns[0].summary)

    def test_archive_is_idempotent_per_decision(self) -> None:
        agent = self.make_agent(
            tool_reply("hold", {"reason": "看"}), tool_reply("hold", {"reason": "看"}),
        )
        for _ in range(2):
            agent.decide(quote=self.quote, dry_run=True, decision_id="arch-2",
                         now=datetime(2026, 10, 10, 14, 5, tzinfo=_JST))
        self.assertEqual(self.storage.count_turns(), 1)

    def test_failed_llm_call_is_not_archived(self) -> None:
        """LLM 调用失败的那次对话以 user 结尾，重放会出现连续两条 user，必须不存。"""
        result = self.decide(
            LLMTransportError("断网"),
            decision_id="arch-3", now=datetime(2026, 10, 10, 14, 5, tzinfo=_JST),
        )
        self.assertFalse(result.archived)
        self.assertEqual(self.storage.count_turns(), 0)

    def test_history_replayed_within_the_hour(self) -> None:
        agent = self.make_agent(
            tool_reply("hold", {"reason": "第一次"}),
            tool_reply("hold", {"reason": "第二次"}),
        )
        base = datetime(2026, 10, 10, 14, 5, tzinfo=_JST)
        first = agent.decide(quote=self.quote, dry_run=True, decision_id="hr-1", now=base)
        second = agent.decide(quote=self.quote, dry_run=True, decision_id="hr-2",
                              now=base + timedelta(minutes=1))
        self.assertEqual(first.replayed_messages, 0)
        self.assertEqual(second.replayed_messages, len(first.transcript))
        self.assertIn("第一次", json.dumps(agent.client.calls[1], ensure_ascii=False))

    def test_hour_is_compacted_on_rollover(self) -> None:
        agent = self.make_agent(
            tool_reply("hold", {"reason": "甲"}),
            tool_reply("hold", {"reason": "乙"}),
        )
        base = datetime(2026, 10, 10, 14, 5, tzinfo=_JST)
        agent.decide(quote=self.quote, dry_run=True, decision_id="hc-1", now=base)
        result = agent.decide(quote=self.quote, dry_run=True, decision_id="hc-2",
                              now=base + timedelta(hours=1))
        self.assertEqual(result.compacted_hours, ("2026-10-10T14",))
        summary = self.storage.read_hour_summary("2026-10-10T14")
        self.assertIsNotNone(summary)
        self.assertEqual(summary.turn_count, 1)
        self.assertIn("HELD", summary.summary)
        self.assertIn("甲", summary.summary)

    def test_compaction_is_not_repeated(self) -> None:
        agent = self.make_agent(
            tool_reply("hold", {"reason": "甲"}),
            tool_reply("hold", {"reason": "乙"}),
            tool_reply("hold", {"reason": "丙"}),
        )
        base = datetime(2026, 10, 10, 14, 5, tzinfo=_JST)
        agent.decide(quote=self.quote, dry_run=True, decision_id="hc-3", now=base)
        first = agent.decide(quote=self.quote, dry_run=True, decision_id="hc-4",
                             now=base + timedelta(hours=1))
        second = agent.decide(quote=self.quote, dry_run=True, decision_id="hc-5",
                              now=base + timedelta(hours=2))
        self.assertEqual(first.compacted_hours, ("2026-10-10T14",))
        self.assertEqual(second.compacted_hours, ("2026-10-10T15",))

    def test_raw_turns_survive_compaction(self) -> None:
        """压缩只产生摘要，原始对话不删——摘要永远可以重算。"""
        agent = self.make_agent(
            tool_reply("hold", {"reason": "甲"}), tool_reply("hold", {"reason": "乙"}),
        )
        base = datetime(2026, 10, 10, 14, 5, tzinfo=_JST)
        agent.decide(quote=self.quote, dry_run=True, decision_id="hc-6", now=base)
        agent.decide(quote=self.quote, dry_run=True, decision_id="hc-7",
                     now=base + timedelta(hours=1))
        self.assertEqual(self.storage.count_turns(), 2)
        self.assertEqual(len(self.storage.read_turns(hour_key="2026-10-10T14")), 1)

    def test_hour_summary_is_injected_next_hour(self) -> None:
        agent = self.make_agent(
            tool_reply("hold", {"reason": "甲"}), tool_reply("hold", {"reason": "乙"}),
        )
        base = datetime(2026, 10, 10, 14, 5, tzinfo=_JST)
        agent.decide(quote=self.quote, dry_run=True, decision_id="hi-1", now=base)
        agent.decide(quote=self.quote, dry_run=True, decision_id="hi-2",
                     now=base + timedelta(hours=1))
        sent = agent.client.calls[1][-1]["content"]
        payload = json.loads(sent.split("\n", 1)[1])
        self.assertEqual(len(payload["hour_memory"]["recent"]), 1)
        self.assertIn("甲", payload["hour_memory"]["recent"][0]["summary"])
        self.assertIn("只依据上面的", payload["hour_memory"]["disclaimer"])

    def test_finished_day_is_compacted_into_a_note(self) -> None:
        """更早的记忆压缩后存进 note（长期记忆只保留每天一条）。"""
        agent = self.make_agent(
            tool_reply("hold", {"reason": "甲"}), tool_reply("hold", {"reason": "乙"}),
        )
        day1 = datetime(2026, 10, 10, 14, 5, tzinfo=_JST)
        agent.decide(quote=self.quote, dry_run=True, decision_id="dc-1", now=day1)
        result = agent.decide(quote=self.quote, dry_run=True, decision_id="dc-2",
                              now=day1 + timedelta(days=1))
        self.assertEqual(result.compacted_day, "2026-10-10")
        notes = self.storage.read_notes(kind="DAY_SUMMARY")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].author, "system")
        self.assertIn("2026-10-10", notes[0].content)

    def test_day_compaction_is_not_repeated(self) -> None:
        agent = self.make_agent(
            tool_reply("hold", {"reason": "甲"}),
            tool_reply("hold", {"reason": "乙"}),
            tool_reply("hold", {"reason": "丙"}),
        )
        day1 = datetime(2026, 10, 10, 14, 5, tzinfo=_JST)
        agent.decide(quote=self.quote, dry_run=True, decision_id="dc-3", now=day1)
        agent.decide(quote=self.quote, dry_run=True, decision_id="dc-4",
                     now=day1 + timedelta(days=1))
        agent.decide(quote=self.quote, dry_run=True, decision_id="dc-5",
                     now=day1 + timedelta(days=1, hours=1))
        self.assertEqual(len(self.storage.read_notes(kind="DAY_SUMMARY")), 1)

    def test_summarize_hour_counts_outcomes(self) -> None:
        agent = self.make_agent(
            tool_reply("hold", {"reason": "甲"}), tool_reply("hold", {"reason": "乙"}),
        )
        base = datetime(2026, 10, 10, 14, 5, tzinfo=_JST)
        agent.decide(quote=self.quote, dry_run=True, decision_id="sh-1", now=base)
        agent.decide(quote=self.quote, dry_run=True, decision_id="sh-2",
                     now=base + timedelta(minutes=1))
        turns = self.storage.read_turns(hour_key="2026-10-10T14")
        text, stats = summarize_hour(turns)
        self.assertEqual(stats["turns"], 2)
        self.assertEqual(stats[OUTCOME_HELD], 2)
        self.assertIn("HELD 2 次", text)

    def test_summarize_empty_hour(self) -> None:
        text, stats = summarize_hour(())
        self.assertEqual(stats, {"turns": 0})
        self.assertIn("没有决策记录", text)

    def test_hour_key_for_truncates(self) -> None:
        moment = datetime(2026, 10, 10, 14, 59, 59, tzinfo=_JST)
        self.assertEqual(hour_key_for(moment, tz=_JST), "2026-10-10T14")


# --------------------------------------------------------------------------- #
# 13. 文件记忆投影
# --------------------------------------------------------------------------- #


class TestMemoryProjection(AgentTestCase):
    def test_note_written_by_model_lands_in_files(self) -> None:
        self.decide(
            tool_reply("remember_note", {"kind": "LESSON", "content": "别在休市时开仓"}),
            tool_reply("hold", {"reason": "记一下"}),
            decision_id="mp-1", now=datetime(2026, 10, 10, 14, 5, tzinfo=_JST),
        )
        notes = self.memory_root / "notes" / "2026-10-10"
        self.assertTrue(notes.is_dir())
        files = list(notes.glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("别在休市时开仓", files[0].read_text(encoding="utf-8"))

    def test_master_and_day_index_exist(self) -> None:
        self.decide(
            tool_reply("remember_note", {"kind": "PLAN", "content": "157.5 是支撑"}),
            tool_reply("hold", {"reason": "记一下"}),
            decision_id="mp-2", now=datetime(2026, 10, 10, 14, 5, tzinfo=_JST),
        )
        master = self.memory_root / "INDEX.md"
        day = self.memory_root / "index" / "2026-10-10.md"
        self.assertTrue(master.is_file())
        self.assertTrue(day.is_file())
        self.assertIn("2026-10-10", master.read_text(encoding="utf-8"))
        self.assertIn("157.5 是支撑", day.read_text(encoding="utf-8"))

    def test_index_is_split_by_day(self) -> None:
        """索引按天拆开，单文件大小只跟那一天有关。"""
        agent = self.make_agent(
            tool_reply("remember_note", {"kind": "LESSON", "content": "第一天"}),
            tool_reply("hold", {"reason": "完事"}),
            tool_reply("remember_note", {"kind": "LESSON", "content": "第二天"}),
            tool_reply("hold", {"reason": "完事"}),
        )
        day1 = datetime(2026, 10, 10, 14, 5, tzinfo=_JST)
        agent.decide(quote=self.quote, dry_run=True, decision_id="mp-3", now=day1)
        agent.decide(quote=self.quote, dry_run=True, decision_id="mp-4",
                     now=day1 + timedelta(days=1))
        first = self.memory_root / "index" / "2026-10-10.md"
        second = self.memory_root / "index" / "2026-10-11.md"
        self.assertTrue(first.is_file() and second.is_file())
        self.assertIn("第一天", first.read_text(encoding="utf-8"))
        self.assertNotIn("第二天", first.read_text(encoding="utf-8"))
        self.assertIn("第二天", second.read_text(encoding="utf-8"))

    def test_hour_summary_lands_in_files(self) -> None:
        agent = self.make_agent(
            tool_reply("hold", {"reason": "甲"}), tool_reply("hold", {"reason": "乙"}),
        )
        base = datetime(2026, 10, 10, 14, 5, tzinfo=_JST)
        agent.decide(quote=self.quote, dry_run=True, decision_id="mp-5", now=base)
        agent.decide(quote=self.quote, dry_run=True, decision_id="mp-6",
                     now=base + timedelta(hours=1))
        path = self.memory_root / "hours" / "2026-10-10" / "14.md"
        self.assertTrue(path.is_file())
        self.assertIn("小时摘要", path.read_text(encoding="utf-8"))

    def test_projection_is_rebuildable_from_sqlite(self) -> None:
        """删掉 memory/ 不丢东西：权威数据在数据库，投影可完整重建。"""
        import shutil

        self.decide(
            tool_reply("remember_note", {"kind": "LESSON", "content": "可再生"}),
            tool_reply("hold", {"reason": "记一下"}),
            decision_id="mp-7", now=datetime(2026, 10, 10, 14, 5, tzinfo=_JST),
        )
        shutil.rmtree(self.memory_root)
        self.assertFalse(self.memory_root.exists())

        store = MemoryStore(self.cfg, storage=self.storage, root=self.memory_root)
        counts = store.rebuild_all()
        self.assertEqual(counts["notes"], 1)
        self.assertEqual(counts["days"], 1)
        self.assertTrue((self.memory_root / "INDEX.md").is_file())
        self.assertEqual(len(store.list_note_files()), 1)

    def test_memory_failure_does_not_break_decision(self) -> None:
        """记忆是辅助：写文件失败不能让决策失败。"""
        bad = MemoryStore(self.cfg, storage=self.storage,
                          root=self.root / "nul" / "x")  # 非法路径
        agent = self.make_agent(
            tool_reply("remember_note", {"kind": "LESSON", "content": "x"}),
            tool_reply("hold", {"reason": "完事"}),
            memory=bad,
        )
        with mock.patch.object(MemoryStore, "sync_note", side_effect=OSError("盘坏了")):
            result = agent.decide(quote=self.quote, dry_run=True, decision_id="mp-8")
        self.assertEqual(result.outcome, OUTCOME_HELD)
        self.assertEqual(self.storage.count_notes(), 1, "数据库里的笔记必须已经写进去")


# --------------------------------------------------------------------------- #
# 14. 督促机制在上下文与提示词里的可见性
# --------------------------------------------------------------------------- #


class TestCapitalPressureVisibility(AgentTestCase):
    """督促机制只有**被 AI 看见**才有意义，所以这几条断言是机制的一部分。

    出货配置里督促机制是关的，所以这里显式打开，专门验证「打开时是否可见」。
    """

    def setUp(self) -> None:
        super().setUp()
        import dataclasses

        self.cfg = dataclasses.replace(
            self.cfg,
            pressure=dataclasses.replace(
                self.cfg.pressure, enabled=True, interval_hours=6,
                deduction_jpy=Decimal("3000"),
            ),
        )
        self.engine = TradingEngine(self.cfg, self.storage)

    def test_prompt_states_the_pressure_rule(self) -> None:
        prompt = self.make_agent().system_prompt()
        self.assertIn("督促机制", prompt)
        self.assertIn("每 6 小时", prompt)
        self.assertIn("3000 日元", prompt)
        self.assertIn("空仓也照扣", prompt)

    def test_prompt_numbers_come_from_config(self) -> None:
        import dataclasses

        cfg = dataclasses.replace(
            self.cfg,
            pressure=dataclasses.replace(
                self.cfg.pressure, interval_hours=2, deduction_jpy=Decimal("777")
            ),
        )
        prompt = build_system_prompt(cfg)
        self.assertIn("每 2 小时", prompt)
        self.assertIn("777 日元", prompt)

    def test_prompt_omits_rule_when_disabled(self) -> None:
        import dataclasses

        cfg = dataclasses.replace(
            self.cfg, pressure=dataclasses.replace(self.cfg.pressure, enabled=False)
        )
        self.assertNotIn("督促机制", build_system_prompt(cfg))

    def test_static_context_carries_pressure(self) -> None:
        pressure = build_static_context(self.cfg)["capital_pressure"]
        self.assertTrue(pressure["enabled"])
        self.assertEqual(pressure["interval_hours"], 6)
        self.assertEqual(pressure["deduction_jpy"], "3000")

    def test_static_context_is_still_byte_stable(self) -> None:
        """加了督促机制之后，稳定前缀仍必须逐字节稳定（缓存前提）。"""
        first = json.dumps(build_static_context(self.cfg), ensure_ascii=False, sort_keys=True)
        second = json.dumps(build_static_context(self.cfg), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_live_pressure_state_in_context(self) -> None:
        """上下文必须给出「已扣多少 / 下次什么时候扣」。"""
        moment = datetime(2026, 10, 12, 9, 0, tzinfo=_JST)
        self.engine.mark_to_market(self.quote, occurred_at=moment)
        self.engine.apply_capital_pressure(now=datetime(2026, 10, 12, 13, 0, tzinfo=_JST))
        self.engine.apply_capital_pressure(
            now=datetime(2026, 10, 12, 18, 0, tzinfo=_JST), quote=self.quote
        )
        context = build_decision_context(
            self.cfg, self.engine, self.storage, self.quote,
            now=datetime(2026, 10, 12, 18, 5, tzinfo=_JST),
        )
        state = context["capital_pressure"]
        self.assertTrue(state["enabled"])
        self.assertEqual(state["charged_total_jpy"], "3000")
        self.assertEqual(state["next_charge_at"], "2026-10-13T00:00+09:00")
        self.assertEqual(state["next_charge_jpy"], "3000")
        self.assertEqual(state["charged_count"], 1)

    def test_context_reports_zero_when_never_charged(self) -> None:
        context = build_decision_context(
            self.cfg, self.engine, self.storage, self.quote,
            now=datetime(2026, 10, 12, 9, 0, tzinfo=_JST),
        )
        state = context["capital_pressure"]
        self.assertEqual(state["charged_total_jpy"], "0")
        self.assertEqual(state["charged_count"], 0)

    def test_decision_context_reaches_the_model(self) -> None:
        """端到端：模型真正收到的 user 消息里必须带督促状态。"""
        agent = self.make_agent(tool_reply("hold", {"reason": "看"}))
        agent.decide(quote=self.quote, dry_run=True, decision_id="pr-1",
                     now=datetime(2026, 10, 12, 9, 0, tzinfo=_JST))
        sent = json.dumps(agent.client.calls[0], ensure_ascii=False)
        self.assertIn("capital_pressure", sent)
        self.assertIn("next_charge_at", sent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
