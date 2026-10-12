"""config 模块的自动化测试。

运行方式（在项目根目录 E:\\pj2\\fx-kurumi-sim 下）::

    python -m unittest discover -s tests -t . -v

或直接::

    python kurumi_fx_sim/tests/test_config.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

# 让测试在任意工作目录下都能 import 到包（项目根 = kurumi_fx_sim 的上一级）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    ConfigError,
    load_config,
)


class ConfigTestCase(unittest.TestCase):
    """公共夹具：把包内默认 config.json 复制到临时目录再按需改坏。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.base = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))

    def write_config(self, data: object | None = None, name: str = "config.json") -> Path:
        payload = self.base if data is None else data
        path = self.tmpdir / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def load(self, data: object | None = None):
        return load_config(self.write_config(data), use_env=False)

    def assertConfigError(self, data: object, needle: str = "") -> ConfigError:  # noqa: N802
        with self.assertRaises(ConfigError) as ctx:
            self.load(data)
        if needle:
            self.assertIn(needle, str(ctx.exception))
        return ctx.exception


class TestDefaults(ConfigTestCase):
    def test_default_config_loads(self) -> None:
        cfg = self.load()
        self.assertEqual(cfg.version, 1)
        self.assertEqual(cfg.source_path, (self.tmpdir / "config.json").resolve())

    def test_default_values_match_spec(self) -> None:
        cfg = self.load()
        self.assertEqual(cfg.account.currency, "JPY")
        self.assertEqual(cfg.account.initial_capital_jpy, Decimal("50000"))
        self.assertEqual(cfg.instrument.symbol, "USD/JPY")
        self.assertEqual(cfg.instrument.base_currency, "USD")
        self.assertEqual(cfg.instrument.quote_currency, "JPY")
        self.assertEqual(cfg.instrument.min_trade_unit, Decimal("1000"))
        # 杠杆 25x 是日本金融厅对个人 FX 的法定上限，不要动
        self.assertEqual(cfg.risk.max_leverage, Decimal("25"))
        # 以下是「大幅放开」后的风险偏好（为了看到 AI 的判断力，而不是看它一直空仓）
        self.assertEqual(cfg.risk.max_margin_usage_ratio, Decimal("0.90"))
        self.assertEqual(cfg.risk.max_risk_per_trade_ratio, Decimal("0.15"))
        self.assertEqual(cfg.risk.max_total_risk_ratio, Decimal("0.60"))
        self.assertEqual(cfg.risk.daily_drawdown_stop_ratio, Decimal("0.40"))
        self.assertEqual(cfg.risk.min_reward_risk_ratio, Decimal("1.0"))
        self.assertEqual(cfg.margin.margin_call_level, Decimal("1.50"))
        self.assertEqual(cfg.margin.stop_out_level, Decimal("1.00"))
        self.assertEqual(cfg.margin.min_equity_to_open_jpy, Decimal("1000"))
        self.assertEqual(cfg.margin.bankruptcy_equity_jpy, Decimal("0"))
        self.assertEqual(cfg.market.monitor_interval_seconds, 60)
        self.assertEqual(cfg.market.spread_jpy, Decimal("0.005"))
        self.assertEqual(cfg.market.slippage_jpy, Decimal("0.003"))

    def test_all_money_fields_are_decimal(self) -> None:
        cfg = self.load()
        values = [
            cfg.account.initial_capital_jpy,
            cfg.instrument.min_trade_unit,
            cfg.risk.max_leverage,
            cfg.risk.max_margin_usage_ratio,
            cfg.risk.max_risk_per_trade_ratio,
            cfg.risk.max_total_risk_ratio,
            cfg.risk.daily_drawdown_stop_ratio,
            cfg.margin.margin_call_level,
            cfg.margin.stop_out_level,
            cfg.margin.min_equity_to_open_jpy,
            cfg.margin.bankruptcy_equity_jpy,
            cfg.market.spread_jpy,
            cfg.market.slippage_jpy,
            cfg.llm.temperature,
        ]
        for value in values:
            self.assertIsInstance(value, Decimal, msg=repr(value))
        self.assertIsInstance(cfg.market.monitor_interval_seconds, int)

    def test_timezone_is_asia_tokyo(self) -> None:
        cfg = self.load()
        self.assertEqual(cfg.timezone_name, "Asia/Tokyo")
        self.assertEqual(cfg.now().utcoffset(), timedelta(hours=9))
        self.assertEqual(cfg.today(), cfg.now().date())

    def test_spread_and_slippage_are_positive(self) -> None:
        """点差与滑点只要求为正；具体数值是风险偏好，不是硬规则。"""
        cfg = self.load()
        self.assertGreater(cfg.market.spread_jpy, Decimal("0"))
        self.assertGreater(cfg.market.slippage_jpy, Decimal("0"))
        # 滑点不应大于点差总宽度，否则成本模型就失真了
        self.assertLessEqual(cfg.market.slippage_jpy, cfg.market.spread_jpy)

    def test_warnings_when_secrets_missing(self) -> None:
        cfg = self.load()
        joined = " ".join(cfg.warnings)
        self.assertIn("llm.api_key", joined)
        self.assertIn("llm.model_id", joined)
        self.assertFalse(cfg.llm.ready)

    def test_llm_helpers(self) -> None:
        cfg = self.load()
        self.assertEqual(
            cfg.llm.chat_completions_url,
            "https://api.openai.com/v1/chat/completions",
        )
        self.assertEqual(cfg.llm.temperature, Decimal("0.2"))
        self.assertEqual(cfg.llm.timeout_seconds, 120)
        self.assertEqual(cfg.llm.max_tokens, 4096)
        self.assertEqual(cfg.llm.max_retries, 2)


class TestPathsAndDirectories(ConfigTestCase):
    def test_paths_resolve_relative_to_config_file(self) -> None:
        cfg = self.load()
        self.assertEqual(cfg.paths.project_root, self.tmpdir.resolve())
        self.assertEqual(cfg.paths.data_dir, (self.tmpdir / "data").resolve())
        self.assertEqual(cfg.paths.reports_dir, (self.tmpdir / "reports").resolve())
        self.assertEqual(
            cfg.paths.database_path,
            (self.tmpdir / "data" / "fxsim.sqlite3").resolve(),
        )
        self.assertEqual(cfg.paths.backup_dir.name, "backups")
        self.assertEqual(cfg.paths.snapshot_dir.name, "snapshots")

    def test_ensure_directories_creates_all(self) -> None:
        cfg = self.load()
        created = cfg.ensure_directories()
        for path in created:
            self.assertTrue(path.is_dir(), msg=str(path))
        self.assertTrue(cfg.paths.backup_dir.is_dir())
        self.assertTrue(cfg.paths.snapshot_dir.is_dir())

    def test_path_traversal_rejected(self) -> None:
        data = self.base
        data["paths"]["data_dir"] = "../evil"
        self.assertConfigError(data, "不允许使用 '..'")

    def test_database_filename_must_be_plain_name(self) -> None:
        data = self.base
        data["paths"]["database_filename"] = "sub/db.sqlite3"
        self.assertConfigError(data, "必须是纯文件名")


class TestFieldCompleteness(ConfigTestCase):
    def test_missing_top_level_field(self) -> None:
        data = self.base
        del data["risk"]
        self.assertConfigError(data, "缺少必填配置段: risk")

    def test_missing_nested_field(self) -> None:
        data = self.base
        del data["account"]["initial_capital_jpy"]
        self.assertConfigError(data, "account.initial_capital_jpy")

    def test_missing_llm_field(self) -> None:
        data = self.base
        del data["llm"]["model_id"]
        self.assertConfigError(data, "llm.model_id")

    def test_unknown_top_level_field(self) -> None:
        data = self.base
        data["surprise"] = 1
        self.assertConfigError(data, "未知配置项")

    def test_unknown_nested_field(self) -> None:
        data = self.base
        data["risk"]["max_levarage"] = "10"  # 故意拼错
        self.assertConfigError(data, "未知配置项")

    def test_non_object_section(self) -> None:
        data = self.base
        data["market"] = [1, 2, 3]
        self.assertConfigError(data, "必须是 JSON 对象")

    def test_non_object_root(self) -> None:
        path = self.tmpdir / "config.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_config(path, use_env=False)
        self.assertIn("顶层必须是 JSON 对象", str(ctx.exception))


class TestTypesAndRanges(ConfigTestCase):
    def test_non_numeric_money(self) -> None:
        data = self.base
        data["account"]["initial_capital_jpy"] = "abc"
        self.assertConfigError(data, "不是合法的十进制数字")

    def test_boolean_rejected_for_number(self) -> None:
        data = self.base
        data["account"]["initial_capital_jpy"] = True
        self.assertConfigError(data, "不能是布尔值")

    def test_boolean_rejected_for_int(self) -> None:
        data = self.base
        data["market"]["monitor_interval_seconds"] = True
        self.assertConfigError(data, "不能是布尔值")

    def test_none_rejected(self) -> None:
        data = self.base
        data["account"]["initial_capital_jpy"] = None
        self.assertConfigError(data, "必须是数字或数字字符串")

    def test_non_finite_rejected(self) -> None:
        data = self.base
        data["account"]["initial_capital_jpy"] = "NaN"
        self.assertConfigError(data, "必须是有限数字")

    def test_negative_capital_rejected(self) -> None:
        data = self.base
        data["account"]["initial_capital_jpy"] = "-1"
        self.assertConfigError(data, "必须大于 0")

    def test_zero_leverage_rejected(self) -> None:
        data = self.base
        data["risk"]["max_leverage"] = "0"
        self.assertConfigError(data, "必须大于 0")

    def test_leverage_upper_bound(self) -> None:
        data = self.base
        data["risk"]["max_leverage"] = "1001"
        self.assertConfigError(data, "不能大于 1000")

    def test_ratio_above_one_rejected(self) -> None:
        data = self.base
        data["risk"]["max_margin_usage_ratio"] = "1.5"
        self.assertConfigError(data, "不能大于 1")

    def test_ratio_zero_rejected(self) -> None:
        data = self.base
        data["risk"]["max_risk_per_trade_ratio"] = "0"
        self.assertConfigError(data, "必须大于 0")

    def test_negative_spread_rejected(self) -> None:
        data = self.base
        data["market"]["spread_jpy"] = "-0.01"
        self.assertConfigError(data, "不能小于 0")

    def test_interval_must_be_positive(self) -> None:
        data = self.base
        data["market"]["monitor_interval_seconds"] = 0
        self.assertConfigError(data, "不能小于 1")

    def test_interval_must_be_integer(self) -> None:
        data = self.base
        data["market"]["monitor_interval_seconds"] = 1.5
        self.assertConfigError(data, "必须是整数")

    def test_min_trade_unit_must_be_integral(self) -> None:
        data = self.base
        data["instrument"]["min_trade_unit"] = "1000.5"
        self.assertConfigError(data, "必须是整数")

    def test_float_json_number_is_accepted_as_decimal(self) -> None:
        data = self.base
        data["account"]["initial_capital_jpy"] = 50000.0
        data["market"]["spread_jpy"] = 0.01
        cfg = self.load(data)
        self.assertEqual(cfg.account.initial_capital_jpy, Decimal("50000"))
        self.assertEqual(cfg.market.spread_jpy, Decimal("0.01"))

    def test_empty_string_rejected(self) -> None:
        data = self.base
        data["instrument"]["symbol"] = "   "
        self.assertConfigError(data, "不能为空")


class TestSymbolAndTimezone(ConfigTestCase):
    def test_symbol_without_slash(self) -> None:
        data = self.base
        data["instrument"]["symbol"] = "USDJPY"
        self.assertConfigError(data, "必须形如 'USD/JPY'")

    def test_symbol_lowercase_normalized(self) -> None:
        data = self.base
        data["instrument"]["symbol"] = "usd/jpy"
        cfg = self.load(data)
        self.assertEqual(cfg.instrument.symbol, "USD/JPY")
        self.assertEqual(cfg.instrument.base_currency, "USD")

    def test_symbol_quote_must_match_account_currency(self) -> None:
        data = self.base
        data["instrument"]["symbol"] = "EUR/USD"
        self.assertConfigError(data, "不一致")

    def test_symbol_same_currencies_rejected(self) -> None:
        data = self.base
        data["instrument"]["symbol"] = "JPY/JPY"
        self.assertConfigError(data, "不能相同")

    def test_account_currency_must_be_jpy(self) -> None:
        data = self.base
        data["account"]["currency"] = "USD"
        self.assertConfigError(data, "仅支持 'JPY'")

    def test_timezone_must_be_tokyo(self) -> None:
        data = self.base
        data["timezone"] = "UTC"
        self.assertConfigError(data, "Asia/Tokyo")

    def test_version_must_be_positive(self) -> None:
        data = self.base
        data["version"] = 0
        self.assertConfigError(data, "version 必须 >= 1")


class TestCrossFieldRules(ConfigTestCase):
    def test_per_trade_risk_cannot_exceed_total_risk(self) -> None:
        data = self.base
        data["risk"]["max_risk_per_trade_ratio"] = "0.10"
        data["risk"]["max_total_risk_ratio"] = "0.05"
        self.assertConfigError(data, "不能大于")

    def test_stop_out_cannot_exceed_margin_call(self) -> None:
        data = self.base
        data["margin"]["stop_out_level"] = "2.0"
        data["margin"]["margin_call_level"] = "1.5"
        self.assertConfigError(data, "不能高于")

    def test_bankruptcy_cannot_exceed_min_equity(self) -> None:
        data = self.base
        data["margin"]["bankruptcy_equity_jpy"] = "9000"
        data["margin"]["min_equity_to_open_jpy"] = "5000"
        self.assertConfigError(data, "不能高于")

    def test_warning_when_initial_capital_below_open_threshold(self) -> None:
        data = self.base
        data["account"]["initial_capital_jpy"] = "5000"
        data["margin"]["min_equity_to_open_jpy"] = "5000"
        cfg = self.load(data)
        self.assertTrue(
            any("无法开仓" in w for w in cfg.warnings), msg=str(cfg.warnings)
        )


class TestLlmAndEnv(ConfigTestCase):
    def test_base_url_must_be_http(self) -> None:
        data = self.base
        data["llm"]["base_url"] = "api.openai.com/v1"
        self.assertConfigError(data, "必须是 http(s)")

    def test_base_url_trailing_slash(self) -> None:
        data = self.base
        data["llm"]["base_url"] = "https://example.com/v1/"
        cfg = self.load(data)
        self.assertEqual(cfg.llm.chat_completions_url, "https://example.com/v1/chat/completions")

    def test_temperature_bounds(self) -> None:
        data = self.base
        data["llm"]["temperature"] = "2.5"
        self.assertConfigError(data, "不能大于 2")

    def test_env_override_api_key(self) -> None:
        path = self.write_config()
        with mock.patch.dict(os.environ, {"KURUMI_LLM_API_KEY": "sk-test"}, clear=False):
            cfg = load_config(path, use_env=True)
        self.assertEqual(cfg.llm.api_key, "sk-test")
        # 默认配置里 model_id 仍为空，因此尚未 ready
        self.assertFalse(cfg.llm.ready)

    def test_env_override_model_and_url(self) -> None:
        path = self.write_config()
        env = {
            "KURUMI_LLM_MODEL_ID": "gpt-test",
            "KURUMI_LLM_BASE_URL": "https://llm.example.com/v1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(path, use_env=True)
        self.assertEqual(cfg.llm.model_id, "gpt-test")
        self.assertEqual(cfg.llm.base_url, "https://llm.example.com/v1")

    def test_kurumi_env_wins_over_openai_env(self) -> None:
        path = self.write_config()
        env = {"OPENAI_API_KEY": "sk-openai", "KURUMI_LLM_API_KEY": "sk-kurumi"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config(path, use_env=True)
        self.assertEqual(cfg.llm.api_key, "sk-kurumi")

    def test_env_ignored_when_disabled(self) -> None:
        path = self.write_config()
        with mock.patch.dict(os.environ, {"KURUMI_LLM_API_KEY": "sk-test"}, clear=False):
            cfg = load_config(path, use_env=False)
        self.assertEqual(cfg.llm.api_key, "")

    def test_redaction(self) -> None:
        data = self.base
        data["llm"]["api_key"] = "sk-secret-value"
        data["llm"]["model_id"] = "gpt-test"
        cfg = self.load(data)
        self.assertEqual(cfg.redacted_dict()["llm"]["api_key"], "***")
        self.assertEqual(cfg.to_dict()["llm"]["api_key"], "sk-secret-value")
        self.assertNotIn("sk-secret-value", repr(cfg.llm))


class TestTradeVolumeRule(ConfigTestCase):
    def test_valid_and_invalid_volumes(self) -> None:
        cfg = self.load()
        self.assertTrue(cfg.is_valid_trade_volume(1000))
        self.assertTrue(cfg.is_valid_trade_volume("3000"))
        self.assertTrue(cfg.is_valid_trade_volume(Decimal("2000")))
        self.assertFalse(cfg.is_valid_trade_volume(1500))
        self.assertFalse(cfg.is_valid_trade_volume(0))
        self.assertFalse(cfg.is_valid_trade_volume(-1000))
        self.assertFalse(cfg.is_valid_trade_volume("abc"))
        self.assertFalse(cfg.is_valid_trade_volume(True))


class TestFileErrors(ConfigTestCase):
    def test_missing_file(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.tmpdir / "nope.json", use_env=False)
        self.assertIn("找不到配置文件", str(ctx.exception))

    def test_invalid_json(self) -> None:
        path = self.tmpdir / "config.json"
        path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_config(path, use_env=False)
        self.assertIn("不是合法 JSON", str(ctx.exception))


class TestSerialization(ConfigTestCase):
    def test_to_dict_is_json_serializable(self) -> None:
        cfg = self.load()
        payload = cfg.redacted_dict()
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("USD/JPY", text)
        self.assertIsInstance(payload["account"]["initial_capital_jpy"], str)

    def test_cli_main_returns_zero(self) -> None:
        from config import _main

        path = self.write_config()
        self.assertEqual(_main(["--config", str(path), "--no-env"]), 0)

    def test_cli_main_reports_error(self) -> None:
        from config import _main

        data = self.base
        data["risk"]["max_leverage"] = "-5"
        path = self.write_config(data)
        self.assertEqual(_main(["--config", str(path), "--no-env"]), 1)


class TestPressureConfig(unittest.TestCase):
    """督促机制配置段。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))

    def base(self) -> dict:
        return json.loads(json.dumps(self.raw))

    def load(self, payload: dict):
        path = self.tmpdir / "config.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return load_config(path, use_env=False)

    def test_defaults_from_repo_config(self) -> None:
        """出货配置里督促机制是**关的**。

        原因：真实的资金时间成本量级是每年 2–3%（折到 6 小时约 0.04 日元），
        作为机制没有意义；而 3000/6h（24%/天）会盖过一切，让人分不清
        「是交易亏的」还是「是被扣的」。要当游戏旋钮用请先重新标定数值。
        """
        cfg = load_config()
        self.assertFalse(cfg.pressure.enabled)
        self.assertEqual(cfg.pressure.interval_hours, 6)
        self.assertEqual(cfg.pressure.deduction_jpy, Decimal("100"))
        self.assertTrue(cfg.pressure.catch_up_missed_slots)

    def test_parsed_values(self) -> None:
        raw = self.base()
        raw["pressure"] = {
            "enabled": False, "interval_hours": 3,
            "deduction_jpy": "1500", "catch_up_missed_slots": False,
        }
        cfg = self.load(raw)
        self.assertFalse(cfg.pressure.enabled)
        self.assertEqual(cfg.pressure.interval_hours, 3)
        self.assertEqual(cfg.pressure.deduction_jpy, Decimal("1500"))
        self.assertFalse(cfg.pressure.catch_up_missed_slots)

    def test_missing_section_is_rejected(self) -> None:
        raw = self.base()
        del raw["pressure"]
        with self.assertRaises(ConfigError):
            self.load(raw)

    def test_missing_field_is_rejected(self) -> None:
        raw = self.base()
        del raw["pressure"]["interval_hours"]
        with self.assertRaises(ConfigError):
            self.load(raw)

    def test_unknown_field_is_rejected(self) -> None:
        raw = self.base()
        raw["pressure"]["surprise"] = 1
        with self.assertRaises(ConfigError):
            self.load(raw)

    def test_interval_must_be_positive(self) -> None:
        raw = self.base()
        raw["pressure"]["interval_hours"] = 0
        with self.assertRaises(ConfigError):
            self.load(raw)

    def test_interval_upper_bound(self) -> None:
        raw = self.base()
        raw["pressure"]["interval_hours"] = 25
        with self.assertRaises(ConfigError):
            self.load(raw)

    def test_deduction_must_not_be_negative(self) -> None:
        raw = self.base()
        raw["pressure"]["deduction_jpy"] = "-1"
        with self.assertRaises(ConfigError):
            self.load(raw)

    def test_zero_deduction_is_allowed(self) -> None:
        raw = self.base()
        raw["pressure"]["deduction_jpy"] = "0"
        self.assertEqual(self.load(raw).pressure.deduction_jpy, Decimal("0"))

    def test_boolean_is_strict(self) -> None:
        raw = self.base()
        raw["pressure"]["enabled"] = "yes"
        with self.assertRaises(ConfigError):
            self.load(raw)

    def test_exported_in_to_dict(self) -> None:
        payload = load_config().to_dict()["pressure"]
        self.assertEqual(payload["deduction_jpy"], "100")
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["interval_hours"], 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
