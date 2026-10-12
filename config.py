"""FX 战士久留美 —— 虚拟交易模拟器：配置模块。

本模块是模拟器全部可调参数的唯一来源（single source of truth）：

* 从 ``config.json`` 读取参数，做类型、取值范围与字段完整性校验；
* 所有金额、汇率、比例、盈亏一律使用 :class:`decimal.Decimal`；
* 时间统一使用 ``Asia/Tokyo``（日本标准时间，无夏令时）；
* 其他模块必须通过 :func:`load_config` 取得参数，不得各自硬编码一份交易规则。

安全边界：本项目只做虚拟交易模拟，不连接任何真实券商，不执行任何真实资金
转账或下单操作。本模块也不会发起任何网络请求。

用法::

    from config import load_config

    cfg = load_config()              # 读取包内默认 config.json
    cfg.account.initial_capital_jpy  # Decimal('50000')
    cfg.now()                        # 带 +09:00 的当前时间

命令行自检::

    python -m config
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Mapping
from urllib.parse import urlparse

try:  # Python 3.9+ 标准库
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - 仅在极旧解释器上发生
    ZoneInfo = None  # type: ignore[assignment]

__all__ = [
    "AccountConfig",
    "Config",
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    "InstrumentConfig",
    "LLMConfig",
    "MarginConfig",
    "MarketConfig",
    "PROJECT_ROOT",
    "PathsConfig",
    "REQUIRED_TIMEZONE",
    "RiskConfig",
    "load_config",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH: Final[Path] = PROJECT_ROOT / "config.json"

#: 本项目统一时区；日本标准时间没有夏令时。
REQUIRED_TIMEZONE: Final[str] = "Asia/Tokyo"
_JST_FALLBACK_OFFSET: Final[timedelta] = timedelta(hours=9)

#: 杠杆的校验上限，仅为防止明显不合理的配置值。
_MAX_SUPPORTED_LEVERAGE: Final[Decimal] = Decimal("1000")

_ALLOWED_TOP_LEVEL: Final[frozenset[str]] = frozenset(
    {
        "version",
        "timezone",
        "account",
        "instrument",
        "risk",
        "margin",
        "market",
        "pressure",
        "llm",
        "paths",
    }
)

_ALLOWED_ACCOUNT: Final[frozenset[str]] = frozenset({"currency", "initial_capital_jpy"})
_ALLOWED_INSTRUMENT: Final[frozenset[str]] = frozenset({"symbol", "min_trade_unit"})
_ALLOWED_RISK: Final[frozenset[str]] = frozenset(
    {
        "max_leverage",
        "max_margin_usage_ratio",
        "max_risk_per_trade_ratio",
        "max_total_risk_ratio",
        "daily_drawdown_stop_ratio",
    }
)
_ALLOWED_MARGIN: Final[frozenset[str]] = frozenset(
    {
        "margin_call_level",
        "stop_out_level",
        "min_equity_to_open_jpy",
        "bankruptcy_equity_jpy",
    }
)
_ALLOWED_MARKET: Final[frozenset[str]] = frozenset(
    {"monitor_interval_seconds", "spread_jpy", "slippage_jpy"}
)
_ALLOWED_PRESSURE: Final[frozenset[str]] = frozenset(
    {
        "enabled",
        "interval_hours",
        "deduction_jpy",
        "catch_up_missed_slots",
    }
)
_ALLOWED_LLM: Final[frozenset[str]] = frozenset(
    {
        "base_url",
        "api_key",
        "model_id",
        "temperature",
        "timeout_seconds",
        "max_tokens",
        "max_retries",
    }
)
_ALLOWED_PATHS: Final[frozenset[str]] = frozenset(
    {
        "data_dir",
        "reports_dir",
        "database_filename",
        "backup_dirname",
        "snapshot_dirname",
    }
)

#: 环境变量覆盖 LLM 连接信息。``KURUMI_LLM_*`` 优先级高于 ``OPENAI_*``，
#: 后写入者生效，因此 ``KURUMI_LLM_*`` 放在后面。
_LLM_ENV_MAP: Final[tuple[tuple[str, str], ...]] = (
    ("OPENAI_BASE_URL", "base_url"),
    ("OPENAI_API_KEY", "api_key"),
    ("OPENAI_MODEL", "model_id"),
    ("KURUMI_LLM_BASE_URL", "base_url"),
    ("KURUMI_LLM_API_KEY", "api_key"),
    ("KURUMI_LLM_MODEL_ID", "model_id"),
)


class ConfigError(ValueError):
    """配置缺失、类型错误或取值越界时抛出。"""


# --------------------------------------------------------------------------- #
# 基础校验工具
# --------------------------------------------------------------------------- #


def _require(mapping: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"缺少必填配置项: {path}.{key}")
    return mapping[key]


def _reject_unknown(mapping: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise ConfigError(
            f"{path} 存在未知配置项: {', '.join(unknown)}；"
            f"允许的配置项为: {', '.join(sorted(allowed))}"
        )


def _section(raw: Mapping[str, Any], name: str, allowed: frozenset[str]) -> Mapping[str, Any]:
    if name not in raw:
        raise ConfigError(f"缺少必填配置段: {name}")
    section = raw[name]
    if not isinstance(section, dict):
        raise ConfigError(f"配置段 {name} 必须是 JSON 对象，收到 {type(section).__name__}")
    _reject_unknown(section, allowed, name)
    return section


def _to_decimal(value: Any, path: str) -> Decimal:
    """把 JSON 里的数字安全地转成 Decimal，避免二进制浮点误差。"""
    if isinstance(value, bool):  # bool 是 int 的子类，必须先拦掉
        raise ConfigError(f"{path} 必须是数字，不能是布尔值（收到 {value!r}）")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        # JSON 中的小数会先被解析为 float，用 repr 还原最接近的字面量。
        result = Decimal(repr(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ConfigError(f"{path} 不能是空字符串")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise ConfigError(f"{path} 不是合法的十进制数字: {value!r}") from exc
    else:
        raise ConfigError(
            f"{path} 必须是数字或数字字符串，收到 {type(value).__name__}: {value!r}"
        )
    if not result.is_finite():
        raise ConfigError(f"{path} 必须是有限数字，收到 {value!r}")
    return result


def _to_int(value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{path} 必须是整数，不能是布尔值（收到 {value!r}）")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ConfigError(f"{path} 必须是整数，收到 {value!r}")
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ConfigError(f"{path} 必须是整数，收到 {value!r}") from exc
    raise ConfigError(f"{path} 必须是整数，收到 {type(value).__name__}: {value!r}")


def _boolean(value: Any, path: str) -> bool:
    """严格布尔：只接受 JSON 的 true/false，不接受 0/1 或字符串。"""
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{path} 必须是 true 或 false，收到 {value!r}")


def _to_str(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{path} 必须是字符串，收到 {type(value).__name__}: {value!r}")
    text = value.strip()
    if not text and not allow_empty:
        raise ConfigError(f"{path} 不能为空")
    return text


def _decimal(
    value: Any,
    path: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    exclusive_min: bool = False,
    exclusive_max: bool = False,
) -> Decimal:
    result = _to_decimal(value, path)
    if minimum is not None:
        if exclusive_min and result <= minimum:
            raise ConfigError(f"{path} 必须大于 {minimum}，收到 {result}")
        if not exclusive_min and result < minimum:
            raise ConfigError(f"{path} 不能小于 {minimum}，收到 {result}")
    if maximum is not None:
        if exclusive_max and result >= maximum:
            raise ConfigError(f"{path} 必须小于 {maximum}，收到 {result}")
        if not exclusive_max and result > maximum:
            raise ConfigError(f"{path} 不能大于 {maximum}，收到 {result}")
    return result


def _ratio(value: Any, path: str) -> Decimal:
    """比例型参数：取值区间 (0, 1]。"""
    return _decimal(
        value,
        path,
        minimum=Decimal("0"),
        maximum=Decimal("1"),
        exclusive_min=True,
    )


def _positive_int(value: Any, path: str, *, minimum: int = 1) -> int:
    result = _to_int(value, path)
    if result < minimum:
        raise ConfigError(f"{path} 不能小于 {minimum}，收到 {result}")
    return result


def _bounded_int(value: Any, path: str, *, minimum: int, maximum: int) -> int:
    result = _to_int(value, path)
    if result < minimum:
        raise ConfigError(f"{path} 不能小于 {minimum}，收到 {result}")
    if result > maximum:
        raise ConfigError(f"{path} 不能大于 {maximum}，收到 {result}")
    return result


def _load_timezone(name: str) -> tzinfo:
    if name != REQUIRED_TIMEZONE:
        raise ConfigError(
            f"timezone 必须为 {REQUIRED_TIMEZONE!r}（本项目统一使用东京时间），收到 {name!r}"
        )
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:  # ZoneInfoNotFoundError 等
            pass
    # 兜底：日本标准时间全年固定 +09:00，与 Asia/Tokyo 完全等价。
    # Windows 上若缺少时区数据库，可 pip install tzdata 使用完整实现。
    return timezone(_JST_FALLBACK_OFFSET, REQUIRED_TIMEZONE)


def _parse_symbol(symbol: str, path: str) -> tuple[str, str]:
    parts = symbol.split("/")
    if len(parts) != 2:
        raise ConfigError(f"{path} 必须形如 'USD/JPY'，收到 {symbol!r}")
    base, quote = (part.strip().upper() for part in parts)
    for code in (base, quote):
        if len(code) != 3 or not code.isascii() or not code.isalpha():
            raise ConfigError(f"{path} 的货币代码必须是 3 位字母，收到 {symbol!r}")
    if base == quote:
        raise ConfigError(f"{path} 的基础货币与计价货币不能相同: {symbol!r}")
    return base, quote


def _resolve_path(value: Any, path: str, project_root: Path) -> Path:
    text = _to_str(value, path)
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate
    if ".." in candidate.parts:
        raise ConfigError(f"{path} 不允许使用 '..' 跳出项目目录: {text!r}")
    return (project_root / candidate).resolve()


# --------------------------------------------------------------------------- #
# 配置数据模型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AccountConfig:
    """账户基础设置。"""

    currency: str
    initial_capital_jpy: Decimal


@dataclass(frozen=True, slots=True)
class InstrumentConfig:
    """交易品种与最小交易单位。"""

    symbol: str
    base_currency: str
    quote_currency: str
    min_trade_unit: Decimal


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """风险限额。所有比例均为净值/保证金的相对比例。"""

    max_leverage: Decimal
    max_margin_usage_ratio: Decimal
    max_risk_per_trade_ratio: Decimal
    max_total_risk_ratio: Decimal
    daily_drawdown_stop_ratio: Decimal


@dataclass(frozen=True, slots=True)
class MarginConfig:
    """保证金与风控触发阈值。"""

    margin_call_level: Decimal
    stop_out_level: Decimal
    min_equity_to_open_jpy: Decimal
    bankruptcy_equity_jpy: Decimal


@dataclass(frozen=True, slots=True)
class MarketConfig:
    """行情监控与撮合成本模拟参数。"""

    monitor_interval_seconds: int
    spread_jpy: Decimal
    slippage_jpy: Decimal


@dataclass(frozen=True, slots=True)
class PressureConfig:
    """督促机制：定期从本金里扣一笔钱，逼 AI 别一直空仓观望。

    这是**游戏机制**，不是交易成本：它按固定的自然小时槽位发生，与行情、持仓、
    交易结果都无关。之所以要做成「扣本金」而不是「限制开仓」，是因为要制造的
    压力方向是**促进行动**，而不是阻止行动。

    实现上它记为一笔 :data:`storage.FlowType.CAPITAL_DECAY` 资金流水，
    因此会降低净值与累计净利润（AI 能看见代价），但**不计入当日回撤**
    ——详见 :meth:`engine.TradingEngine._evaluate_halts` 的说明。
    """

    enabled: bool
    interval_hours: int
    deduction_jpy: Decimal
    catch_up_missed_slots: bool

    @property
    def interval_hours_float(self) -> float:
        return float(self.interval_hours)


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """供 AI 决策使用的 OpenAI Chat Completions 兼容接口设置。"""

    base_url: str
    api_key: str = field(repr=False)
    model_id: str
    temperature: Decimal
    timeout_seconds: int
    #: 单次请求的最大生成 token 数。注意推理型模型（返回 reasoning_content 的）
    #: 会把推理 token 也计入该额度，给太小会只得到空 content（finish_reason=length）。
    max_tokens: int
    max_retries: int

    @property
    def chat_completions_url(self) -> str:
        """OpenAI 兼容的 Chat Completions 端点。"""
        return self.base_url.rstrip("/") + "/chat/completions"

    @property
    def ready(self) -> bool:
        """是否已具备调用所需的最小信息。"""
        return bool(self.api_key and self.model_id)


@dataclass(frozen=True, slots=True)
class PathsConfig:
    """运行期路径（已解析为绝对路径）。"""

    project_root: Path
    data_dir: Path
    reports_dir: Path
    database_filename: str
    backup_dirname: str
    snapshot_dirname: str

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_filename

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / self.backup_dirname

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / self.snapshot_dirname


@dataclass(frozen=True, slots=True)
class Config:
    """校验通过后的完整配置，供其他模块统一读取。"""

    version: int
    timezone_name: str
    tz: tzinfo = field(repr=False, compare=False)
    account: AccountConfig
    instrument: InstrumentConfig
    risk: RiskConfig
    margin: MarginConfig
    market: MarketConfig
    pressure: PressureConfig
    llm: LLMConfig
    paths: PathsConfig
    source_path: Path = field(repr=False, compare=False)
    warnings: tuple[str, ...] = ()

    # -- 时间 ------------------------------------------------------------- #
    def now(self) -> datetime:
        """当前东京时间（带时区信息）。"""
        return datetime.now(self.tz)

    def today(self) -> date:
        """当前东京日期，用于每日回撤等按日统计。"""
        return self.now().date()

    # -- 规则查询 --------------------------------------------------------- #
    def is_valid_trade_volume(self, volume: Decimal | int | str) -> bool:
        """交易量必须是「最小交易单位的正整数倍」。"""
        try:
            value = _to_decimal(volume, "trade_volume")
        except ConfigError:
            return False
        if value <= 0:
            return False
        return value % self.instrument.min_trade_unit == 0

    # -- 导出 ------------------------------------------------------------- #
    def to_dict(self, *, redact_secrets: bool = False) -> dict[str, Any]:
        """转回可 JSON 序列化的字典；Decimal 一律输出为字符串以保精度。"""
        api_key = self.llm.api_key
        if redact_secrets:
            api_key = "***" if api_key else ""
        return {
            "version": self.version,
            "timezone": self.timezone_name,
            "account": {
                "currency": self.account.currency,
                "initial_capital_jpy": str(self.account.initial_capital_jpy),
            },
            "pressure": {
                "enabled": self.pressure.enabled,
                "interval_hours": self.pressure.interval_hours,
                "deduction_jpy": str(self.pressure.deduction_jpy),
                "catch_up_missed_slots": self.pressure.catch_up_missed_slots,
            },
            "instrument": {
                "symbol": self.instrument.symbol,
                "min_trade_unit": str(self.instrument.min_trade_unit),
            },
            "risk": {
                "max_leverage": str(self.risk.max_leverage),
                "max_margin_usage_ratio": str(self.risk.max_margin_usage_ratio),
                "max_risk_per_trade_ratio": str(self.risk.max_risk_per_trade_ratio),
                "max_total_risk_ratio": str(self.risk.max_total_risk_ratio),
                "daily_drawdown_stop_ratio": str(self.risk.daily_drawdown_stop_ratio),
            },
            "margin": {
                "margin_call_level": str(self.margin.margin_call_level),
                "stop_out_level": str(self.margin.stop_out_level),
                "min_equity_to_open_jpy": str(self.margin.min_equity_to_open_jpy),
                "bankruptcy_equity_jpy": str(self.margin.bankruptcy_equity_jpy),
            },
            "market": {
                "monitor_interval_seconds": self.market.monitor_interval_seconds,
                "spread_jpy": str(self.market.spread_jpy),
                "slippage_jpy": str(self.market.slippage_jpy),
            },
            "llm": {
                "base_url": self.llm.base_url,
                "api_key": api_key,
                "model_id": self.llm.model_id,
                "temperature": str(self.llm.temperature),
                "timeout_seconds": self.llm.timeout_seconds,
                "max_tokens": self.llm.max_tokens,
                "max_retries": self.llm.max_retries,
            },
            "paths": {
                "data_dir": str(self.paths.data_dir),
                "reports_dir": str(self.paths.reports_dir),
                "database_filename": self.paths.database_filename,
                "backup_dirname": self.paths.backup_dirname,
                "snapshot_dirname": self.paths.snapshot_dirname,
            },
        }

    def redacted_dict(self) -> dict[str, Any]:
        """用于日志输出：隐去 API Key。"""
        return self.to_dict(redact_secrets=True)

    def ensure_directories(self) -> tuple[Path, ...]:
        """创建数据、报告、备份与快照目录，返回被确保存在的路径。"""
        targets = (
            self.paths.data_dir,
            self.paths.reports_dir,
            self.paths.backup_dir,
            self.paths.snapshot_dir,
        )
        for target in targets:
            target.mkdir(parents=True, exist_ok=True)
        return targets


# --------------------------------------------------------------------------- #
# 各配置段构建
# --------------------------------------------------------------------------- #


def _build_account(raw: Mapping[str, Any]) -> AccountConfig:
    section = _section(raw, "account", _ALLOWED_ACCOUNT)
    currency = _to_str(_require(section, "currency", "account"), "account.currency").upper()
    if currency != "JPY":
        raise ConfigError(
            f"account.currency 目前仅支持 'JPY'（本模拟器以日元计价），收到 {currency!r}"
        )
    capital = _decimal(
        _require(section, "initial_capital_jpy", "account"),
        "account.initial_capital_jpy",
        minimum=Decimal("0"),
        exclusive_min=True,
    )
    return AccountConfig(currency=currency, initial_capital_jpy=capital)


def _build_instrument(raw: Mapping[str, Any], account: AccountConfig) -> InstrumentConfig:
    section = _section(raw, "instrument", _ALLOWED_INSTRUMENT)
    symbol = _to_str(_require(section, "symbol", "instrument"), "instrument.symbol")
    base, quote = _parse_symbol(symbol, "instrument.symbol")
    if quote != account.currency:
        raise ConfigError(
            f"instrument.symbol 的计价货币 {quote} 与 account.currency "
            f"{account.currency} 不一致（盈亏需以日元结算）"
        )
    unit = _decimal(
        _require(section, "min_trade_unit", "instrument"),
        "instrument.min_trade_unit",
        minimum=Decimal("0"),
        exclusive_min=True,
    )
    if unit != unit.to_integral_value():
        raise ConfigError("instrument.min_trade_unit 必须是整数（最小交易单位按整份计量）")
    return InstrumentConfig(
        symbol=f"{base}/{quote}",
        base_currency=base,
        quote_currency=quote,
        min_trade_unit=unit,
    )


def _build_risk(raw: Mapping[str, Any]) -> RiskConfig:
    section = _section(raw, "risk", _ALLOWED_RISK)
    return RiskConfig(
        max_leverage=_decimal(
            _require(section, "max_leverage", "risk"),
            "risk.max_leverage",
            minimum=Decimal("0"),
            maximum=_MAX_SUPPORTED_LEVERAGE,
            exclusive_min=True,
        ),
        max_margin_usage_ratio=_ratio(
            _require(section, "max_margin_usage_ratio", "risk"), "risk.max_margin_usage_ratio"
        ),
        max_risk_per_trade_ratio=_ratio(
            _require(section, "max_risk_per_trade_ratio", "risk"), "risk.max_risk_per_trade_ratio"
        ),
        max_total_risk_ratio=_ratio(
            _require(section, "max_total_risk_ratio", "risk"), "risk.max_total_risk_ratio"
        ),
        daily_drawdown_stop_ratio=_ratio(
            _require(section, "daily_drawdown_stop_ratio", "risk"),
            "risk.daily_drawdown_stop_ratio",
        ),
    )


def _build_margin(raw: Mapping[str, Any]) -> MarginConfig:
    section = _section(raw, "margin", _ALLOWED_MARGIN)
    margin_call = _decimal(
        _require(section, "margin_call_level", "margin"),
        "margin.margin_call_level",
        minimum=Decimal("1"),
    )
    # stop_out_level <= margin_call_level、bankruptcy <= min_equity 属于跨字段关系，
    # 统一由 _cross_checks 判定，此处不重复设置上限。
    stop_out = _decimal(
        _require(section, "stop_out_level", "margin"),
        "margin.stop_out_level",
        minimum=Decimal("0"),
        exclusive_min=True,
    )
    min_equity = _decimal(
        _require(section, "min_equity_to_open_jpy", "margin"),
        "margin.min_equity_to_open_jpy",
        minimum=Decimal("0"),
    )
    bankruptcy = _decimal(
        _require(section, "bankruptcy_equity_jpy", "margin"),
        "margin.bankruptcy_equity_jpy",
        minimum=Decimal("0"),
    )
    return MarginConfig(
        margin_call_level=margin_call,
        stop_out_level=stop_out,
        min_equity_to_open_jpy=min_equity,
        bankruptcy_equity_jpy=bankruptcy,
    )


def _build_market(raw: Mapping[str, Any]) -> MarketConfig:
    section = _section(raw, "market", _ALLOWED_MARKET)
    return MarketConfig(
        monitor_interval_seconds=_positive_int(
            _require(section, "monitor_interval_seconds", "market"),
            "market.monitor_interval_seconds",
        ),
        spread_jpy=_decimal(
            _require(section, "spread_jpy", "market"),
            "market.spread_jpy",
            minimum=Decimal("0"),
        ),
        slippage_jpy=_decimal(
            _require(section, "slippage_jpy", "market"),
            "market.slippage_jpy",
            minimum=Decimal("0"),
        ),
    )


def _build_pressure(raw: Mapping[str, Any]) -> PressureConfig:
    section = _section(raw, "pressure", _ALLOWED_PRESSURE)
    enabled = _boolean(_require(section, "enabled", "pressure"), "pressure.enabled")
    return PressureConfig(
        enabled=enabled,
        interval_hours=_bounded_int(
            _require(section, "interval_hours", "pressure"),
            "pressure.interval_hours", minimum=1, maximum=24,
        ),
        deduction_jpy=_decimal(
            _require(section, "deduction_jpy", "pressure"),
            "pressure.deduction_jpy", minimum=Decimal("0"),
        ),
        catch_up_missed_slots=_boolean(
            _require(section, "catch_up_missed_slots", "pressure"),
            "pressure.catch_up_missed_slots",
        ),
    )


def _build_llm(section: Mapping[str, Any]) -> LLMConfig:
    base_url = _to_str(_require(section, "base_url", "llm"), "llm.base_url")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(
            f"llm.base_url 必须是 http(s) 开头的完整地址，收到 {base_url!r}"
        )
    return LLMConfig(
        base_url=base_url,
        api_key=_to_str(
            _require(section, "api_key", "llm"), "llm.api_key", allow_empty=True
        ),
        model_id=_to_str(
            _require(section, "model_id", "llm"), "llm.model_id", allow_empty=True
        ),
        temperature=_decimal(
            _require(section, "temperature", "llm"),
            "llm.temperature",
            minimum=Decimal("0"),
            maximum=Decimal("2"),
        ),
        timeout_seconds=_positive_int(
            _require(section, "timeout_seconds", "llm"), "llm.timeout_seconds"
        ),
        max_tokens=_bounded_int(
            _require(section, "max_tokens", "llm"), "llm.max_tokens",
            minimum=1, maximum=200_000,
        ),
        max_retries=_positive_int(
            _require(section, "max_retries", "llm"), "llm.max_retries", minimum=0
        ),
    )


def _build_paths(raw: Mapping[str, Any], project_root: Path) -> PathsConfig:
    section = _section(raw, "paths", _ALLOWED_PATHS)
    data_dir = _resolve_path(_require(section, "data_dir", "paths"), "paths.data_dir", project_root)
    reports_dir = _resolve_path(
        _require(section, "reports_dir", "paths"), "paths.reports_dir", project_root
    )
    database_filename = _to_str(
        _require(section, "database_filename", "paths"), "paths.database_filename"
    )
    if Path(database_filename).name != database_filename or ".." in Path(database_filename).parts:
        raise ConfigError(
            f"paths.database_filename 必须是纯文件名，收到 {database_filename!r}"
        )
    return PathsConfig(
        project_root=project_root,
        data_dir=data_dir,
        reports_dir=reports_dir,
        database_filename=database_filename,
        backup_dirname=_to_str(
            _require(section, "backup_dirname", "paths"), "paths.backup_dirname"
        ),
        snapshot_dirname=_to_str(
            _require(section, "snapshot_dirname", "paths"), "paths.snapshot_dirname"
        ),
    )


def _apply_llm_env(section: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(section)
    for env_name, key in _LLM_ENV_MAP:
        value = os.environ.get(env_name)
        if value is not None and value.strip():
            merged[key] = value.strip()
    return merged


def _cross_checks(
    account: AccountConfig,
    risk: RiskConfig,
    margin: MarginConfig,
    llm: LLMConfig,
    warnings: list[str],
) -> None:
    if risk.max_risk_per_trade_ratio > risk.max_total_risk_ratio:
        raise ConfigError(
            "risk.max_risk_per_trade_ratio（单笔止损风险上限）不能大于 "
            f"risk.max_total_risk_ratio（累计止损风险上限）: "
            f"{risk.max_risk_per_trade_ratio} > {risk.max_total_risk_ratio}"
        )
    if margin.stop_out_level > margin.margin_call_level:
        raise ConfigError(
            "margin.stop_out_level（强制平仓水平）不能高于 "
            f"margin.margin_call_level（预警水平）: "
            f"{margin.stop_out_level} > {margin.margin_call_level}"
        )
    if margin.bankruptcy_equity_jpy > margin.min_equity_to_open_jpy:
        raise ConfigError(
            "margin.bankruptcy_equity_jpy（爆仓净值）不能高于 "
            f"margin.min_equity_to_open_jpy（停止开仓净值）: "
            f"{margin.bankruptcy_equity_jpy} > {margin.min_equity_to_open_jpy}"
        )
    if account.initial_capital_jpy <= margin.min_equity_to_open_jpy:
        warnings.append(
            f"初始本金 {account.initial_capital_jpy} 不高于停止开仓阈值 "
            f"{margin.min_equity_to_open_jpy}，账户开局即无法开仓。"
        )
    if not llm.api_key:
        warnings.append(
            "llm.api_key 为空；请填写 config.json 或设置环境变量 "
            "KURUMI_LLM_API_KEY / OPENAI_API_KEY。"
        )
    if not llm.model_id:
        warnings.append(
            "llm.model_id 为空；请填写 config.json 或设置环境变量 "
            "KURUMI_LLM_MODEL_ID / OPENAI_MODEL。"
        )


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


def _read_json(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise ConfigError(f"找不到配置文件: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 {path} 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 {path} 的顶层必须是 JSON 对象")
    return data


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    use_env: bool = True,
) -> Config:
    """读取并校验配置，返回统一的 :class:`Config` 对象。

    Args:
        path: 配置文件路径；``None`` 表示包内默认 ``config.json``。
        use_env: 是否允许 ``KURUMI_LLM_*`` / ``OPENAI_*`` 环境变量覆盖 LLM 连接信息。

    Raises:
        ConfigError: 文件缺失、JSON 非法、字段缺失/未知、类型或取值越界。
    """
    config_path = DEFAULT_CONFIG_PATH if path is None else Path(path).expanduser()
    config_path = config_path.resolve()
    project_root = config_path.parent

    raw = _read_json(config_path)
    _reject_unknown(raw, _ALLOWED_TOP_LEVEL, "config")

    version = _to_int(_require(raw, "version", "config"), "version")
    if version < 1:
        raise ConfigError(f"version 必须 >= 1，收到 {version}")

    timezone_name = _to_str(_require(raw, "timezone", "config"), "timezone")
    tz = _load_timezone(timezone_name)

    account = _build_account(raw)
    instrument = _build_instrument(raw, account)
    risk = _build_risk(raw)
    margin = _build_margin(raw)
    market = _build_market(raw)
    pressure = _build_pressure(raw)

    llm_section = _section(raw, "llm", _ALLOWED_LLM)
    if use_env:
        llm_section = _apply_llm_env(llm_section)
    llm = _build_llm(llm_section)

    paths = _build_paths(raw, project_root)

    warnings: list[str] = []
    _cross_checks(account, risk, margin, llm, warnings)

    return Config(
        version=version,
        timezone_name=timezone_name,
        tz=tz,
        account=account,
        instrument=instrument,
        risk=risk,
        margin=margin,
        market=market,
        pressure=pressure,
        llm=llm,
        paths=paths,
        source_path=config_path,
        warnings=tuple(warnings),
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m config",
        description="校验本项目的配置文件并打印摘要。",
    )
    parser.add_argument("--config", default=None, help="配置文件路径（默认包内 config.json）")
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")
    parser.add_argument(
        "--no-env", action="store_true", help="忽略 KURUMI_LLM_* / OPENAI_* 环境变量覆盖"
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config, use_env=not args.no_env)
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 1

    if args.as_json:
        print(json.dumps(cfg.redacted_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"配置文件        : {cfg.source_path}")
    print(f"时区            : {cfg.timezone_name}（当前 {cfg.now().isoformat(timespec='seconds')}）")
    print(f"初始本金        : {cfg.account.initial_capital_jpy} {cfg.account.currency}")
    print(
        f"交易品种        : {cfg.instrument.symbol}"
        f"（最小交易单位 {cfg.instrument.min_trade_unit} {cfg.instrument.base_currency}）"
    )
    print(f"最大名义杠杆    : {cfg.risk.max_leverage} 倍")
    print(f"保证金占用上限  : {cfg.risk.max_margin_usage_ratio}")
    print(
        f"止损风险上限    : 单笔 {cfg.risk.max_risk_per_trade_ratio} / "
        f"累计 {cfg.risk.max_total_risk_ratio}"
    )
    print(f"每日回撤停止线  : {cfg.risk.daily_drawdown_stop_ratio}")
    print(
        f"保证金水平      : 预警 {cfg.margin.margin_call_level} / "
        f"强平 {cfg.margin.stop_out_level}"
    )
    print(
        f"净值阈值        : 停止开仓 {cfg.margin.min_equity_to_open_jpy} / "
        f"爆仓 {cfg.margin.bankruptcy_equity_jpy}"
    )
    print(
        f"行情监控        : 每 {cfg.market.monitor_interval_seconds} 秒，"
        f"点差 {cfg.market.spread_jpy} 日元，滑点 {cfg.market.slippage_jpy} 日元"
    )
    p_cfg = cfg.pressure
    print(
        f"督促机制        : "
        + (
            f"每 {p_cfg.interval_hours} 小时扣本金 {p_cfg.deduction_jpy} 日元"
            f"（漏掉的槽位{'会补扣' if p_cfg.catch_up_missed_slots else '不补扣'}）"
            if p_cfg.enabled
            else "已关闭"
        )
    )
    print(
        f"LLM 接口        : {cfg.llm.base_url} "
        f"model={cfg.llm.model_id or '(未设置)'} "
        f"api_key={'已设置' if cfg.llm.api_key else '未设置'}"
    )
    for warning in cfg.warnings:
        print(f"[警告] {warning}")
    print("配置校验通过。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
