"""OpenAI Chat Completions 兼容客户端（含 function calling）。

职责边界
========

**只做传输与协议解析**：构造请求、发出去、把响应解析成结构化对象。
它不做任何交易决策、不碰账户、不碰数据库、不写日志里的密钥。

决策逻辑在 :mod:`agent`，风控闸门在 :mod:`engine`。

为什么用 function calling 而不是让模型吐纯 JSON
===============================================

让模型「只输出一个 JSON」时，输出是**自由文本**：可能被 markdown 代码块包住、
可能被截断、可能混入解释。实测该模型在 `max_tokens` 不足时会返回空字符串。
用 tools 声明参数 schema 后，模型返回的是结构化的 ``tool_calls``，参数的解析由
协议层保证，失败时也能拿到原始字符串并给出可诊断的错误。

推理型模型的坑
==============

有些模型（例如 LongCat-2.5-Preview）会额外返回 ``reasoning_content``，
且**推理 token 计入 ``max_tokens``**。额度被推理吃满时 ``content`` 为空、
``finish_reason == "length"``。这种情况本模块抛
:class:`LLMEmptyCompletionError` 并明确提示调大 ``llm.max_tokens``，
而不是把它当成「模型没有意见」。

安全
====

密钥只从 :class:`config.LLMConfig` 读取（来源是环境变量或已被 gitignore 的
``config.local.json``）；本模块不接收、不打印、不落盘密钥，错误信息里一律脱敏。
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Final, Mapping, Sequence

from config import Config, ConfigError, load_config

__all__ = [
    "ChatResponse",
    "LLMAuthError",
    "LLMClient",
    "LLMConfigError",
    "LLMEmptyCompletionError",
    "LLMError",
    "LLMRateLimitError",
    "LLMRequestError",
    "LLMResponseError",
    "LLMTimeoutError",
    "LLMTransportError",
    "ToolCall",
    "Usage",
    "assistant_message",
    "system_message",
    "tool_result_message",
    "tool_schema",
    "user_message",
]

logger = logging.getLogger("kurumi_fx_sim.llm_client")

_DEFAULT_TIMEOUT: Final[float] = 120.0
_RETRY_BACKOFF_SECONDS: Final[float] = 2.0
_MAX_ERROR_BODY_CHARS: Final[int] = 800

_HEADERS: Final[dict[str, str]] = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class LLMError(RuntimeError):
    """LLM 客户端所有异常的基类。"""


class LLMConfigError(LLMError):
    """配置不完整或不可用（缺 base_url / api_key / model_id）。"""


class LLMTransportError(LLMError):
    """网络或服务端问题（连接失败、超时、5xx）。"""


class LLMTimeoutError(LLMTransportError):
    """请求超时。推理型模型容易触发，必要时调大 llm.timeout_seconds。"""


class LLMRateLimitError(LLMTransportError):
    """被限流（HTTP 429）。"""


class LLMAuthError(LLMError):
    """凭据无效（HTTP 401/403）。重试无意义。"""


class LLMRequestError(LLMError):
    """请求本身不合法（4xx，鉴权与限流除外）。

    刻意**不**继承 :class:`LLMTransportError`——重试一个 400 只是白等，
    客户端据此把它排除在重试范围之外。
    """


class LLMResponseError(LLMError):
    """响应结构不可解析或不合法。"""


class LLMEmptyCompletionError(LLMResponseError):
    """既没有内容也没有工具调用。

    最常见的原因是 ``max_tokens`` 被推理 token 吃满（``finish_reason=length``）。
    """


# --------------------------------------------------------------------------- #
# 消息构造
# --------------------------------------------------------------------------- #


def system_message(content: str) -> dict[str, Any]:
    return {"role": "system", "content": content}


def user_message(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def assistant_message(
    content: str | None = None, *, tool_calls: Sequence[Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """构造 assistant 消息。回灌工具调用历史时必须带上原始 ``tool_calls``。"""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = list(tool_calls)
    return message


def tool_result_message(tool_call_id: str, content: str) -> dict[str, Any]:
    """把工具执行结果回灌给模型。``content`` 必须是字符串。"""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def tool_schema(
    name: str,
    description: str,
    properties: Mapping[str, Mapping[str, Any]],
    *,
    required: Sequence[str] = (),
    additional_properties: bool = False,
) -> dict[str, Any]:
    """构造一个 function calling 工具声明。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": dict(properties),
                "required": list(required),
                "additionalProperties": additional_properties,
            },
        },
    }


# --------------------------------------------------------------------------- #
# 响应结构
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型请求调用的一次工具。"""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "raw_arguments": self.raw_arguments,
        }

    def to_message_part(self) -> dict[str, Any]:
        """还原成可直接回灌给 API 的 ``tool_calls`` 元素。"""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments},
        }


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    #: 命中**服务端前缀缓存**的 prompt token 数。这是「上下文组织得好不好」的
    #: 唯一硬指标：越接近 ``prompt_tokens``，说明稳定前缀越稳定。
    #: 各实现字段名不同（OpenAI 系 ``prompt_tokens_details.cached_tokens``，
    #: DeepSeek 系 ``prompt_cache_hit_tokens``），两种都认。
    cached_tokens: int | None = None
    #: 未命中、本次需要写入缓存的 prompt token 数（有的实现才给）。
    cache_miss_tokens: int | None = None

    @property
    def cache_hit_ratio(self) -> float | None:
        """缓存命中率（0–1）。**拿不到缓存字段时返回 ``None``，不是 0**——
        「没有这个信息」和「命中率为零」是两件事。"""
        if self.cached_tokens is None or not self.prompt_tokens:
            return None
        return self.cached_tokens / self.prompt_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
        }


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """一次 chat completions 的结构化结果。"""

    content: str
    reasoning_content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    model: str | None
    usage: Usage | None
    raw: Mapping[str, Any] = field(repr=False, default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "reasoning_content": self.reasoning_content,
            "tool_calls": [call.as_dict() for call in self.tool_calls],
            "finish_reason": self.finish_reason,
            "model": self.model,
            "usage": self.usage.as_dict() if self.usage else None,
        }


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #


class LLMClient:
    """OpenAI Chat Completions 兼容客户端。

    典型用法::

        client = LLMClient.from_config()
        reply = client.chat(
            [system_message("你是交易助手"), user_message("现在该开仓吗？")],
            tools=[submit_intent_tool, hold_tool],
        )
        for call in reply.tool_calls:
            print(call.name, call.arguments)
    """

    def __init__(
        self,
        cfg: Config | None = None,
        *,
        http_post: Callable[..., Mapping[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.cfg = cfg or load_config()
        self.llm = self.cfg.llm
        self.logger = logger_ or logger
        self._http_post = http_post or _http_post_json
        self._sleep = sleep

    @classmethod
    def from_config(cls, cfg: Config | None = None, **kwargs: Any) -> LLMClient:
        return cls(cfg, **kwargs)

    @property
    def url(self) -> str:
        return self.llm.chat_completions_url

    def describe(self) -> dict[str, Any]:
        """当前连接设置（密钥脱敏），用于自检输出。"""
        key = self.llm.api_key
        return {
            "base_url": self.llm.base_url,
            "chat_completions_url": self.url,
            "model_id": self.llm.model_id,
            "api_key": (key[:8] + "…" + key[-4:]) if key else "",
            "api_key_present": bool(key),
            "temperature": str(self.llm.temperature),
            "timeout_seconds": self.llm.timeout_seconds,
            "max_tokens": self.llm.max_tokens,
            "max_retries": self.llm.max_retries,
            "ready": self.llm.ready,
        }

    def _redact(self, text: str) -> str:
        key = self.llm.api_key
        if key:
            return text.replace(key, "***")
        return text

    def require_ready(self) -> None:
        """配置不完整时抛出可操作的错误。"""
        if not self.llm.api_key:
            raise LLMConfigError(
                "llm.api_key 为空：请设置环境变量 KURUMI_LLM_API_KEY（或 OPENAI_API_KEY），"
                "或写入已被 gitignore 的 config.local.json"
            )
        if not self.llm.model_id:
            raise LLMConfigError(
                "llm.model_id 为空：请设置环境变量 KURUMI_LLM_MODEL_ID（或 OPENAI_MODEL）"
            )

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | str | None = None,
    ) -> ChatResponse:
        """发一次 chat completions 请求。

        Args:
            messages: OpenAI 格式的消息列表。
            tools: function calling 工具声明；给了就会把 ``tools`` 放进请求体。
            tool_choice: ``"auto"`` / ``"none"`` / ``"required"``，或指定具体函数。
            max_tokens: 覆盖配置里的 ``llm.max_tokens``。
            temperature: 覆盖配置里的 ``llm.temperature``。

        Raises:
            LLMConfigError: 配置不完整。
            LLMAuthError: 凭据无效。
            LLMRateLimitError: 被限流（已重试仍失败）。
            LLMTimeoutError: 超时（已重试仍失败）。
            LLMTransportError: 其它网络/服务端错误。
            LLMResponseError: 响应结构不可解析。
            LLMEmptyCompletionError: 既无内容也无工具调用。
        """
        self.require_ready()
        payload: dict[str, Any] = {
            "model": self.llm.model_id,
            "messages": [dict(m) for m in messages],
            "temperature": float(
                temperature if temperature is not None else self.llm.temperature
            ),
            "max_tokens": int(max_tokens if max_tokens is not None else self.llm.max_tokens),
        }
        if tools:
            payload["tools"] = [dict(t) for t in tools]
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"

        body = self._post_with_retries(payload)
        return self._parse_response(body)

    def _post_with_retries(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        headers = dict(_HEADERS)
        headers["Authorization"] = f"Bearer {self.llm.api_key}"
        attempts = max(0, int(self.llm.max_retries)) + 1
        last_error: LLMError | None = None
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                body = self._http_post(
                    self.url, payload, headers=headers, timeout=self.llm.timeout_seconds
                )
            except LLMError as exc:
                last_error = exc
                retryable = isinstance(exc, (LLMTimeoutError, LLMRateLimitError, LLMTransportError))
            else:
                self.logger.info(
                    "LLM 调用成功：model=%s 耗时 %.2fs（第 %d 次尝试）",
                    self.llm.model_id, time.monotonic() - started, attempt + 1,
                )
                return body
            if not retryable or attempt == attempts - 1:
                raise last_error
            delay = _RETRY_BACKOFF_SECONDS * (attempt + 1)
            self.logger.warning(
                "LLM 调用失败（%s），%.1f 秒后重试（第 %d/%d 次）",
                type(last_error).__name__, delay, attempt + 1, attempts,
            )
            self._sleep(delay)
        raise last_error or LLMTransportError("LLM 调用失败：未知原因")  # pragma: no cover

    def _parse_response(self, body: Mapping[str, Any]) -> ChatResponse:
        if not isinstance(body, Mapping):
            raise LLMResponseError(f"响应不是 JSON 对象：{type(body).__name__}")
        if body.get("error"):
            raise LLMResponseError(f"接口返回错误：{self._redact(str(body['error']))}")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError(
                f"响应缺少 choices：{self._redact(json.dumps(body, ensure_ascii=False)[:300])}"
            )
        choice = choices[0] if isinstance(choices[0], Mapping) else {}
        message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
        content = message.get("content")
        content = "" if content is None else str(content)
        reasoning = message.get("reasoning_content")
        tool_calls = tuple(self._parse_tool_calls(message.get("tool_calls")))
        finish_reason = choice.get("finish_reason")
        if not content.strip() and not tool_calls:
            raise LLMEmptyCompletionError(
                f"模型既没有返回内容也没有调用工具（finish_reason={finish_reason!r}）。"
                f"若 finish_reason 为 'length'，说明推理 token 吃满了 llm.max_tokens，"
                f"请调大它（当前 {self.llm.max_tokens}）。"
            )
        return ChatResponse(
            content=content,
            reasoning_content=str(reasoning) if reasoning else None,
            tool_calls=tool_calls,
            finish_reason=str(finish_reason) if finish_reason else None,
            model=str(body.get("model")) if body.get("model") else None,
            usage=self._parse_usage(body.get("usage")),
            raw=body,
        )

    def _parse_tool_calls(self, raw_calls: Any) -> list[ToolCall]:
        if not raw_calls:
            return []
        if not isinstance(raw_calls, list):
            raise LLMResponseError(f"tool_calls 不是数组：{type(raw_calls).__name__}")
        calls: list[ToolCall] = []
        for index, item in enumerate(raw_calls):
            if not isinstance(item, Mapping):
                raise LLMResponseError(f"tool_calls[{index}] 不是对象")
            function = item.get("function")
            if not isinstance(function, Mapping):
                raise LLMResponseError(f"tool_calls[{index}] 缺少 function")
            name = function.get("name")
            if not name:
                raise LLMResponseError(f"tool_calls[{index}] 缺少函数名")
            raw_arguments = function.get("arguments")
            raw_text = "" if raw_arguments is None else str(raw_arguments)
            try:
                arguments = json.loads(raw_text) if raw_text.strip() else {}
            except json.JSONDecodeError as exc:
                raise LLMResponseError(
                    f"工具 {name} 的参数不是合法 JSON（{exc}）：{raw_text[:200]!r}"
                ) from exc
            if not isinstance(arguments, dict):
                raise LLMResponseError(
                    f"工具 {name} 的参数必须是 JSON 对象，收到 {type(arguments).__name__}"
                )
            calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{index}"),
                    name=str(name),
                    arguments=arguments,
                    raw_arguments=raw_text,
                )
            )
        return calls

    @staticmethod
    def _parse_usage(raw_usage: Any) -> Usage | None:
        if not isinstance(raw_usage, Mapping):
            return None
        details = raw_usage.get("completion_tokens_details")
        reasoning = details.get("reasoning_tokens") if isinstance(details, Mapping) else None
        # 缓存命中数放在两个不同的地方，各家不一样，都要认：
        #   OpenAI 系：usage.prompt_tokens_details.cached_tokens
        #   DeepSeek 系：usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens
        # 早期版本只解析了 4 个字段，把缓存信息**静默丢掉**了——于是「看不到缓存」
        # 被误读成「端点不支持缓存」。别再犯这个错：解析器要保留原始响应里的计费字段。
        prompt_details = raw_usage.get("prompt_tokens_details")
        cached = (
            prompt_details.get("cached_tokens")
            if isinstance(prompt_details, Mapping)
            else None
        )
        if cached is None:
            cached = raw_usage.get("prompt_cache_hit_tokens")
        miss = raw_usage.get("prompt_cache_miss_tokens")
        if miss is None and isinstance(prompt_details, Mapping):
            miss = prompt_details.get("cache_miss_tokens")
        return Usage(
            prompt_tokens=_maybe_int(raw_usage.get("prompt_tokens")),
            completion_tokens=_maybe_int(raw_usage.get("completion_tokens")),
            total_tokens=_maybe_int(raw_usage.get("total_tokens")),
            reasoning_tokens=_maybe_int(reasoning),
            cached_tokens=_maybe_int(cached),
            cache_miss_tokens=_maybe_int(miss),
        )


def _maybe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _http_post_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str],
    timeout: float,
) -> Mapping[str, Any]:
    """POST 一个 JSON 并把响应解析成对象；把网络异常翻译成本模块的异常类型。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = _read_body(exc)
        if exc.code in (401, 403):
            raise LLMAuthError(f"凭据无效或无权访问（HTTP {exc.code}）：{detail}") from exc
        if exc.code == 429:
            raise LLMRateLimitError(f"被限流（HTTP 429）：{detail}") from exc
        if exc.code >= 500:
            raise LLMTransportError(f"服务端错误（HTTP {exc.code}）：{detail}") from exc
        raise LLMRequestError(f"请求被拒绝（HTTP {exc.code}）：{detail}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise LLMTimeoutError(f"请求超时（{timeout} 秒）：{url}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise LLMTimeoutError(f"请求超时（{timeout} 秒）：{url}") from exc
        raise LLMTransportError(f"无法连接：{url}（{exc.reason}）") from exc
    except OSError as exc:
        raise LLMTransportError(f"网络错误：{url}（{exc}）") from exc

    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise LLMResponseError(f"响应不是合法 JSON：{raw[:300]!r}") from exc
    if not isinstance(parsed, dict):
        raise LLMResponseError(f"响应顶层不是对象：{type(parsed).__name__}")
    return parsed

def _read_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read(_MAX_ERROR_BODY_CHARS).decode("utf-8", errors="replace")
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# 命令行自检
# --------------------------------------------------------------------------- #


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m llm_client",
        description="检查 LLM 连接配置并发一次最小 function calling 请求（只读，不碰账户）。",
    )
    parser.add_argument("--prompt", default="回复两个字：收到", help="测试用的用户消息")
    parser.add_argument("--no-call", action="store_true", help="只打印配置，不真的发请求")
    parser.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2
    client = LLMClient(cfg)
    info = client.describe()
    if args.as_json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        for key, value in info.items():
            print(f"  {key:22s}: {value}")
    if args.no_call:
        return 0 if info["ready"] else 1
    if not info["ready"]:
        print("\n[未就绪] 请先配置 llm.api_key 与 llm.model_id（见 README「LLM 接入」）")
        return 1

    echo_tool = tool_schema(
        "echo",
        "把收到的文本原样回显，用于连通性自检。",
        {"text": {"type": "string", "description": "要回显的文本"}},
        required=["text"],
    )
    try:
        reply = client.chat(
            [system_message("你是一个自检助手。请调用 echo 工具回显用户的话。"),
             user_message(args.prompt)],
            tools=[echo_tool],
            tool_choice="auto",
        )
    except LLMError as exc:
        print(f"\n[调用失败] {type(exc).__name__}: {exc}")
        return 1
    print("\n=== 调用结果 ===")
    print(f"  model        : {reply.model}")
    print(f"  finish_reason: {reply.finish_reason}")
    print(f"  content      : {reply.content!r}")
    print(f"  reasoning    : {(reply.reasoning_content or '')[:80]!r}")
    print(f"  tool_calls   : {len(reply.tool_calls)}")
    for call in reply.tool_calls:
        print(f"    · {call.name}({call.arguments})")
    if reply.usage:
        print(f"  usage        : {reply.usage.as_dict()}")
        ratio = reply.usage.cache_hit_ratio
        if ratio is not None:
            print(f"  前缀缓存命中 : {ratio:.1%}"
                  f"（{reply.usage.cached_tokens}/{reply.usage.prompt_tokens} prompt token）")
    return 0 if reply.has_tool_calls else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
