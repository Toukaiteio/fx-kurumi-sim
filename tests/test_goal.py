"""目标 + 奖励机制的自动化测试。

覆盖：目标建立、进度、达成发奖、目标逐档抬高与奖励递增、
**奖励不可连锁刷取**（新目标以「达成时净值 + 奖励」为基准）、
幂等（同一档只发一次）、dry-run 无副作用、账本口径与审计链、
以及「奖励不计入交易盈亏」这条会计口径。

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
from engine import GOAL_STATE_META_KEY, TradingEngine  # noqa: E402
from reporting import ReportKind, build_report_data  # noqa: E402
from storage import FlowType, Storage  # noqa: E402

_JST = timezone(timedelta(hours=9))
_INITIAL = Decimal("50000")


def goal_config(*, growth: str = "0.20", reward: str = "5000", reward_growth: str = "0.50"):
    """显式钉住目标参数，避免测试依赖出货配置的数值。"""
    base = load_config()
    return dataclasses.replace(
        base,
        goal=dataclasses.replace(
            base.goal,
            enabled=True,
            target_growth_ratio=Decimal(growth),
            reward_jpy=Decimal(reward),
            reward_growth_ratio=Decimal(reward_growth),
        ),
    )


class GoalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cfg = goal_config()
        self.storage = Storage(self.root / "data" / "fxsim.sqlite3", tz=_JST)
        self.storage.initialize_account(
            _INITIAL, opened_at=datetime(2026, 10, 12, 8, 0, tzinfo=_JST)
        )
        self.engine = TradingEngine(self.cfg, self.storage)

    def at(self, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 10, 12, hour, minute, tzinfo=_JST)

    def rewards(self) -> list:
        return self.storage.read_cash_flows(flow_type=FlowType.GOAL_REWARD)


# --------------------------------------------------------------------------- #
# 1. 建立与进度
# --------------------------------------------------------------------------- #


class TestGoalSetup(GoalTestCase):
    def test_first_settlement_creates_level_one(self) -> None:
        result = self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(9))
        self.assertTrue(result.enabled)
        self.assertFalse(result.achieved)
        self.assertEqual(result.level, 1)
        # 50000 × 1.2
        self.assertEqual(result.target_jpy, Decimal("60000.000"))
        self.assertEqual(self.rewards(), [])

    def test_target_is_relative_to_current_equity(self) -> None:
        """第一档目标从「现在的净值」出发，而不是一个陈旧的绝对值。"""
        result = self.engine.apply_goal_reward(equity=Decimal("80000"), now=self.at(9))
        self.assertEqual(result.target_jpy, Decimal("96000.000"))

    def test_progress_ratio(self) -> None:
        self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(9))
        result = self.engine.apply_goal_reward(equity=Decimal("55000"), now=self.at(12))
        self.assertFalse(result.achieved)
        self.assertEqual(result.target_jpy, Decimal("60000.000"))
        self.assertAlmostEqual(float(result.progress_ratio), 55000 / 60000, places=6)

    def test_elapsed_hours_is_tracked(self) -> None:
        self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(9))
        result = self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(15))
        self.assertEqual(result.elapsed_hours, Decimal("6.000"))

    def test_state_persisted_in_meta(self) -> None:
        self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(9))
        raw = self.storage.get_meta(GOAL_STATE_META_KEY)
        self.assertIsNotNone(raw)
        state = self.engine.goal_state()
        self.assertEqual(state["level"], 1)
        self.assertEqual(Decimal(state["target_jpy"]), Decimal("60000.000"))

    def test_disabled_does_nothing(self) -> None:
        cfg = dataclasses.replace(
            self.cfg, goal=dataclasses.replace(self.cfg.goal, enabled=False)
        )
        engine = TradingEngine(cfg, Storage(self.root / "off.sqlite3", tz=_JST))
        engine.storage.initialize_account(_INITIAL, opened_at=self.at(8))
        result = engine.apply_goal_reward(equity=_INITIAL, now=self.at(9))
        self.assertFalse(result.enabled)
        self.assertIn("已关闭", result.skipped_reason)
        self.assertEqual(engine.storage.get_meta(GOAL_STATE_META_KEY), None)


# --------------------------------------------------------------------------- #
# 2. 达成与奖励
# --------------------------------------------------------------------------- #


class TestGoalAchievement(GoalTestCase):
    def reach_level_one(self) -> None:
        self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(9))

    def test_achievement_pays_reward(self) -> None:
        self.reach_level_one()
        result = self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(15))
        self.assertTrue(result.achieved)
        self.assertEqual(result.reward_paid_jpy, Decimal("5000.000"))
        self.assertEqual(result.level, 1)
        self.assertEqual(len(self.rewards()), 1)
        self.assertEqual(self.rewards()[0].amount, Decimal("5000.000"))

    def test_reward_increases_cash(self) -> None:
        """奖励是**真的钱**：现金余额要增加。"""
        self.reach_level_one()
        before = self.engine.cash_balance()
        self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(15))
        self.assertEqual(self.engine.cash_balance(), before + Decimal("5000"))

    def test_next_target_is_above_post_reward_equity(self) -> None:
        """**防连锁刷奖励**：发完奖励后净值必须严格低于下一档目标。

        否则奖励金本身就能跨过下一档 → 奖励自我兑现 → 等于印钱。
        """
        self.reach_level_one()
        result = self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(15))
        equity_after = Decimal("60000") + result.reward_paid_jpy
        self.assertGreater(result.next_target_jpy, equity_after)
        # 65000 × 1.2
        self.assertEqual(result.next_target_jpy, Decimal("78000.000"))

    def test_cannot_chain_rewards_immediately(self) -> None:
        """用发奖后的净值立刻再结算，必须**不能**再次达成。"""
        self.reach_level_one()
        first = self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(15))
        again = self.engine.apply_goal_reward(
            equity=Decimal("60000") + first.reward_paid_jpy, now=self.at(15, 1)
        )
        self.assertFalse(again.achieved)
        self.assertEqual(len(self.rewards()), 1)

    def test_level_advances_and_reward_grows(self) -> None:
        self.reach_level_one()
        self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(15))
        second = self.engine.apply_goal_reward(
            equity=Decimal("78000"), now=self.at(20)
        )
        self.assertTrue(second.achieved)
        self.assertEqual(second.level, 2)
        # 5000 × 1.5
        self.assertEqual(second.reward_paid_jpy, Decimal("7500.000"))
        self.assertEqual(self.engine.goal_total_reward(), Decimal("12500.000"))
        self.assertEqual(self.engine.goal_reward_count(), 2)

    def test_same_level_is_paid_once(self) -> None:
        """同一档重复达成不会重复发奖（事件键幂等）。"""
        self.reach_level_one()
        self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(15))
        # 手动把状态回退到第 1 档，模拟重复结算同一档
        state = self.engine.goal_state()
        state["level"] = 1
        state["target_jpy"] = "60000.000"
        self.engine._write_goal_state(state)
        self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(16))
        self.assertEqual(len(self.rewards()), 1, "同一档不应重复发奖")

    def test_survives_restart(self) -> None:
        self.reach_level_one()
        self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(15))
        fresh = TradingEngine(self.cfg, Storage(self.storage.db_path, tz=_JST))
        result = fresh.apply_goal_reward(equity=Decimal("65000"), now=self.at(16))
        self.assertEqual(result.level, 2)
        self.assertFalse(result.achieved)

    def test_elapsed_recorded_on_achievement(self) -> None:
        self.reach_level_one()
        result = self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(21))
        self.assertEqual(result.elapsed_hours, Decimal("12.000"))


# --------------------------------------------------------------------------- #
# 3. dry-run 与账本
# --------------------------------------------------------------------------- #


class TestGoalLedger(GoalTestCase):
    def test_dry_run_has_no_side_effects(self) -> None:
        preview = self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(9), dry_run=True)
        self.assertFalse(preview.achieved)
        self.assertIsNone(self.storage.get_meta(GOAL_STATE_META_KEY))
        self.assertEqual(self.rewards(), [])
        self.assertEqual(self.engine.cash_balance(), _INITIAL)

    def test_dry_run_previews_achievement_without_paying(self) -> None:
        self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(9))
        preview = self.engine.apply_goal_reward(
            equity=Decimal("60000"), now=self.at(15), dry_run=True
        )
        self.assertTrue(preview.achieved)
        self.assertEqual(preview.reward_paid_jpy, Decimal("5000.000"))
        self.assertEqual(self.rewards(), [], "dry-run 不能真的发钱")
        self.assertEqual(self.engine.goal_state()["level"], 1, "dry-run 不能推进档位")

    def test_ledger_stays_consistent(self) -> None:
        self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(9))
        self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(15))
        self.assertTrue(self.engine.reconcile_account(strict=False).ok)

    def test_audit_chain_intact(self) -> None:
        self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(9))
        self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(15))
        self.assertTrue(self.storage.verify_audit_chain().ok)
        types = [e.event_type for e in self.storage.read_audit_events()]
        self.assertIn("GOAL_SET", types)
        self.assertIn("GOAL_ACHIEVED", types)

    def test_snapshot_reports_progress(self) -> None:
        self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(9))
        snap = self.engine.goal_snapshot(Decimal("55000"), now=self.at(12))
        self.assertTrue(snap["enabled"])
        self.assertEqual(snap["level"], 1)
        self.assertEqual(snap["target_jpy"], "60000.000")
        self.assertEqual(snap["gap_jpy"], "5000.000")
        self.assertFalse(snap["reached"])
        self.assertEqual(snap["elapsed_hours"], "3.0")
        self.assertEqual(snap["reward_on_success_jpy"], "5000.000")


# --------------------------------------------------------------------------- #
# 4. 会计口径：奖励不是交易盈利
# --------------------------------------------------------------------------- #


class TestGoalRewardIsNotTradingProfit(GoalTestCase):
    def build(self):
        return build_report_data(
            ReportKind.EVENING, cfg=self.cfg, engine=self.engine,
            storage=self.storage, quote=None, skip_quote=True,
        )

    def test_reward_excluded_from_net_profit(self) -> None:
        """奖励把净值推高了，但**不能**被算成「赚到的钱」。

        否则「达成目标」会被记成投资收益，交易表现被美化。
        """
        self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(9))
        before = self.build().account
        self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(15))
        after = self.build().account

        self.assertEqual(after.cash_balance - before.cash_balance, Decimal("5000.000"))
        self.assertEqual(after.goal_reward_jpy, Decimal("5000.000"))
        self.assertEqual(
            after.net_profit, before.net_profit,
            "发奖励不应改变交易净利润（奖励是外部注入）",
        )

    def test_goal_reward_serialized(self) -> None:
        self.engine.apply_goal_reward(equity=_INITIAL, now=self.at(9))
        self.engine.apply_goal_reward(equity=Decimal("60000"), now=self.at(15))
        payload = self.build().to_payload()
        self.assertEqual(payload["account"]["goal_reward_jpy"], "5000.000")

    def test_zero_when_no_reward(self) -> None:
        self.assertEqual(self.build().account.goal_reward_jpy, Decimal("0.000"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
