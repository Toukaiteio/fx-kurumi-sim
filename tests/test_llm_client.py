"""llm_client 模块的自动化测试。

覆盖：配置解析与脱敏、function calling 的工具调用解析、各类错误映射
（超时/限流/鉴权/5xx/不可解析/空响应）、重试与退避、以及「密钥不进错误信息」。

全部使用**假 HTTP 传输**，**不访问网络**，不消耗任何 token。

运行方式（仓库根目录）::

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import sys
import unittest
import urllib.error
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import load_config  # noqa: E402
from llm_client import (  # noqa: E402
    ChatResponse,
    LLMAuthError,
    LLMClient,
    LLMConfigError,
    LLMEmptyCompletionError,
    LLMError,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponseError,
    LLMTimeoutError,
    LLMTransportError,
    Usage,
    assistant_message,
    system_message,
    tool_result_message,
    tool_schema,
    user_message,
)

_FAKE_KEY = "sk-test-abcdefghijklmnopqrstuvwxyz012345"
_BASE = "https://llm.example.invalid/v1"
_MODEL = "Test-Model-1"


def _llm_config(**overrides: str):
    """构造一份带 LLM 设置的配置（走环境变量覆盖，与生产路径一致）。"""
    env = {
        "KURUMI_LLM_BASE_URL": _BASE,
        "KURUMI_LLM_API_KEY": _FAKE_KEY,
        "KURUMI_LLM_MODEL_ID": _MODEL,
    }
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=False):
        return load_config()


def _reply(
    *,
    content: str | None = "ok",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
    model: str = _MODEL,
    reasoning: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _tool_call(name: str = "submit_trade_intent", arguments: str = '{"side":"LONG"}',
               call_id: str = "call_1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class FakeTransport:
    """记录请求、按脚本返回响应的假 HTTP 传输。"""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, payload: Mapping[str, Any], *, headers, timeout) -> Mapping[str, Any]:
        self.calls.append({"url": url, "payload": payload, "headers": dict(headers), "timeout": timeout})
        if not self.responses:
            raise AssertionError("假传输的响应脚本已用尽")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def last_payload(self) -> Mapping[str, Any]:
        return self.calls[-1]["payload"]


def http_error(code: int, body: bytes = b'{"error":"boom"}') -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError(_BASE + "/chat/completions", code, "err", {}, io.BytesIO(body))


class TestConfigAndDescribe(unittest.TestCase):
    def test_describe_redacts_key(self) -> None:
        client = LLMClient(_llm_config())
        info = client.describe()
        self.assertEqual(info["base_url"], _BASE)
        self.assertEqual(info["chat_completions_url"], _BASE + "/chat/completions")
        self.assertEqual(info["model_id"], _MODEL)
        self.assertTrue(info["api_key_present"])
        self.assertNotEqual(info["api_key"], _FAKE_KEY)
        self.assertIn("…", info["api_key"])
        self.assertTrue(info["ready"])
        self.assertEqual(info["timeout_seconds"], 120)
        self.assertEqual(info["max_tokens"], 4096)

    def test_require_ready_without_key(self) -> None:
        cfg = load_config()
        with mock.patch.dict(os.environ, {"KURUMI_LLM_API_KEY": "", "OPENAI_API_KEY": ""},
                             clear=False):
            cfg = load_config()
        client = LLMClient(cfg)
        if client.llm.api_key:
            self.skipTest("环境中存在密钥，跳过")
        with self.assertRaises(LLMConfigError) as err:
            client.require_ready()
        self.assertIn("KURUMI_LLM_API_KEY", str(err.exception))

    def test_chat_without_key_raises_before_network(self) -> None:
        transport = FakeTransport(_reply())
        cfg = load_config()
        client = LLMClient(cfg, http_post=transport)
        if client.llm.api_key:
            self.skipTest("环境中存在密钥，跳过")
        with self.assertRaises(LLMConfigError):
            client.chat([user_message("hi")])
        self.assertEqual(transport.calls, [])  # 没有发出请求


class TestRequestBuilding(unittest.TestCase):
    def test_payload_fields(self) -> None:
        transport = FakeTransport(_reply(content="hello"))
        client = LLMClient(_llm_config(), http_post=transport)
        reply = client.chat([system_message("sys"), user_message("hi")])
        payload = transport.last_payload
        self.assertEqual(payload["model"], _MODEL)
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertNotIn("tools", payload)
        self.assertEqual(reply.content, "hello")
        self.assertFalse(reply.has_tool_calls)

    def test_tools_and_tool_choice_included(self) -> None:
        transport = FakeTransport(_reply())
        client = LLMClient(_llm_config(), http_post=transport)
        tool = tool_schema("t", "desc", {"a": {"type": "string"}}, required=["a"])
        client.chat([user_message("x")], tools=[tool])
        payload = transport.last_payload
        self.assertEqual(payload["tools"], [tool])
        self.assertEqual(payload["tool_choice"], "auto")

    def test_tool_choice_can_be_required(self) -> None:
        transport = FakeTransport(_reply())
        client = LLMClient(_llm_config(), http_post=transport)
        client.chat([user_message("x")], tools=[tool_schema("t", "d", {})], tool_choice="required")
        self.assertEqual(transport.last_payload["tool_choice"], "required")

    def test_overrides(self) -> None:
        transport = FakeTransport(_reply())
        client = LLMClient(_llm_config(), http_post=transport)
        client.chat([user_message("x")], max_tokens=99, temperature=1.5)
        self.assertEqual(transport.last_payload["max_tokens"], 99)
        self.assertEqual(transport.last_payload["temperature"], 1.5)

    def test_authorization_header_uses_key(self) -> None:
        transport = FakeTransport(_reply())
        client = LLMClient(_llm_config(), http_post=transport)
        client.chat([user_message("x")])
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], f"Bearer {_FAKE_KEY}")

    def test_timeout_comes_from_config(self) -> None:
        transport = FakeTransport(_reply())
        client = LLMClient(_llm_config(), http_post=transport)
        client.chat([user_message("x")])
        self.assertEqual(transport.calls[0]["timeout"], 120)


class TestToolCallParsing(unittest.TestCase):
    def test_parses_tool_calls(self) -> None:
        transport = FakeTransport(_reply(
            content=None, finish_reason="tool_calls",
            tool_calls=[_tool_call("submit_trade_intent", '{"side":"LONG","volume_usd":1000}')],
        ))
        client = LLMClient(_llm_config(), http_post=transport)
        reply = client.chat([user_message("x")], tools=[tool_schema("submit_trade_intent", "d", {})])
        self.assertTrue(reply.has_tool_calls)
        self.assertEqual(len(reply.tool_calls), 1)
        call = reply.tool_calls[0]
        self.assertEqual(call.name, "submit_trade_intent")
        self.assertEqual(call.arguments, {"side": "LONG", "volume_usd": 1000})
        self.assertEqual(call.id, "call_1")
        self.assertEqual(json.loads(call.raw_arguments), call.arguments)
        self.assertEqual(reply.finish_reason, "tool_calls")
        self.assertEqual(reply.content, "")

    def test_tool_call_round_trip_message(self) -> None:
        transport = FakeTransport(_reply(
            content=None, tool_calls=[_tool_call(arguments='{"a":1}')]))
        client = LLMClient(_llm_config(), http_post=transport)
        call = client.chat([user_message("x")], tools=[tool_schema("t", "d", {})]).tool_calls[0]
        part = call.to_message_part()
        self.assertEqual(part["id"], call.id)
        self.assertEqual(part["type"], "function")
        self.assertEqual(part["function"]["name"], call.name)
        self.assertEqual(part["function"]["arguments"], '{"a":1}')

    def test_multiple_tool_calls(self) -> None:
        transport = FakeTransport(_reply(content=None, tool_calls=[
            _tool_call("a", '{"x":1}', "c1"), _tool_call("b", '{"y":2}', "c2")]))
        client = LLMClient(_llm_config(), http_post=transport)
        reply = client.chat([user_message("x")], tools=[tool_schema("a", "d", {}), tool_schema("b", "d", {})])
        self.assertEqual([c.name for c in reply.tool_calls], ["a", "b"])

    def test_empty_arguments_string_becomes_empty_dict(self) -> None:
        transport = FakeTransport(_reply(content=None, tool_calls=[_tool_call("t", "")]))
        client = LLMClient(_llm_config(), http_post=transport)
        self.assertEqual(client.chat([user_message("x")], tools=[tool_schema("t", "d", {})]).tool_calls[0].arguments, {})

    def test_malformed_arguments_raise(self) -> None:
        transport = FakeTransport(_reply(content=None, tool_calls=[_tool_call("t", "{not json")]))
        client = LLMClient(_llm_config(), http_post=transport)
        with self.assertRaises(LLMResponseError) as err:
            client.chat([user_message("x")], tools=[tool_schema("t", "d", {})])
        self.assertIn("不是合法 JSON", str(err.exception))

    def test_arguments_must_be_object(self) -> None:
        transport = FakeTransport(_reply(content=None, tool_calls=[_tool_call("t", "[1,2]")]))
        client = LLMClient(_llm_config(), http_post=transport)
        with self.assertRaises(LLMResponseError) as err:
            client.chat([user_message("x")], tools=[tool_schema("t", "d", {})])
        self.assertIn("必须是 JSON 对象", str(err.exception))

    def test_missing_function_name_raises(self) -> None:
        bad = {"id": "c1", "type": "function", "function": {"arguments": "{}"}}
        transport = FakeTransport(_reply(content=None, tool_calls=[bad]))
        client = LLMClient(_llm_config(), http_post=transport)
        with self.assertRaises(LLMResponseError) as err:
            client.chat([user_message("x")], tools=[tool_schema("t", "d", {})])
        self.assertIn("缺少函数名", str(err.exception))


class TestEmptyCompletion(unittest.TestCase):
    def test_empty_content_with_length_finish_mentions_max_tokens(self) -> None:
        transport = FakeTransport(_reply(
            content="", finish_reason="length",
            usage={"prompt_tokens": 100, "completion_tokens": 4096, "total_tokens": 4196,
                   "completion_tokens_details": {"reasoning_tokens": 4090}},
        ))
        client = LLMClient(_llm_config(), http_post=transport)
        with self.assertRaises(LLMEmptyCompletionError) as err:
            client.chat([user_message("x")])
        message = str(err.exception)
        self.assertIn("max_tokens", message)
        self.assertIn("4096", message)
        self.assertIn("length", message)

    def test_empty_content_without_tool_calls_raises(self) -> None:
        transport = FakeTransport(_reply(content="", finish_reason="stop"))
        client = LLMClient(_llm_config(), http_post=transport)
        with self.assertRaises(LLMEmptyCompletionError):
            client.chat([user_message("x")])

    def test_whitespace_only_content_counts_as_empty(self) -> None:
        transport = FakeTransport(_reply(content="   \n  ", finish_reason="stop"))
        client = LLMClient(_llm_config(), http_post=transport)
        with self.assertRaises(LLMEmptyCompletionError):
            client.chat([user_message("x")])

    def test_empty_content_with_tool_calls_is_fine(self) -> None:
        transport = FakeTransport(_reply(content="", tool_calls=[_tool_call()]))
        client = LLMClient(_llm_config(), http_post=transport)
        reply = client.chat([user_message("x")], tools=[tool_schema("submit_trade_intent", "d", {})])
        self.assertTrue(reply.has_tool_calls)


class TestUsageAndReasoning(unittest.TestCase):
    def test_usage_parsed_including_reasoning(self) -> None:
        transport = FakeTransport(_reply(usage={
            "prompt_tokens": 247, "completion_tokens": 45, "total_tokens": 292,
            "completion_tokens_details": {"reasoning_tokens": 30},
        }))
        client = LLMClient(_llm_config(), http_post=transport)
        usage = client.chat([user_message("x")]).usage
        self.assertEqual(usage.prompt_tokens, 247)
        self.assertEqual(usage.completion_tokens, 45)
        self.assertEqual(usage.total_tokens, 292)
        self.assertEqual(usage.reasoning_tokens, 30)

    # -- 前缀缓存计费字段（曾经被自己的解析器静默丢掉） ------------------- #

    def test_openai_style_cached_tokens_are_kept(self) -> None:
        """``prompt_tokens_details.cached_tokens`` 必须被解析出来。

        早期版本只取 4 个字段，把缓存信息丢了，于是「看不到缓存」被误读成
        「端点不支持缓存」——这个测试就是为了不再犯同样的错。
        """
        transport = FakeTransport(_reply(usage={
            "prompt_tokens": 1418, "completion_tokens": 23, "total_tokens": 1441,
            "prompt_tokens_details": {"cached_tokens": 1408},
            "completion_tokens_details": {"reasoning_tokens": 19},
        }))
        usage = LLMClient(_llm_config(), http_post=transport).chat(
            [user_message("x")]
        ).usage
        self.assertEqual(usage.cached_tokens, 1408)
        self.assertAlmostEqual(usage.cache_hit_ratio, 1408 / 1418)
        self.assertIn("cached_tokens", usage.as_dict())

    def test_deepseek_style_cache_fields_are_kept(self) -> None:
        transport = FakeTransport(_reply(usage={
            "prompt_tokens": 100, "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
        }))
        usage = LLMClient(_llm_config(), http_post=transport).chat(
            [user_message("x")]
        ).usage
        self.assertEqual(usage.cached_tokens, 80)
        self.assertEqual(usage.cache_miss_tokens, 20)
        self.assertAlmostEqual(usage.cache_hit_ratio, 0.8)

    def test_missing_cache_fields_give_none_not_zero(self) -> None:
        """「没有这个信息」和「命中率为零」必须区分开。"""
        transport = FakeTransport(_reply(usage={"prompt_tokens": 100}))
        usage = LLMClient(_llm_config(), http_post=transport).chat(
            [user_message("x")]
        ).usage
        self.assertIsNone(usage.cached_tokens)
        self.assertIsNone(usage.cache_hit_ratio)

    def test_zero_prompt_tokens_does_not_divide_by_zero(self) -> None:
        usage = Usage(prompt_tokens=0, cached_tokens=0)
        self.assertIsNone(usage.cache_hit_ratio)

    def test_usage_defaults_have_no_cache_claim(self) -> None:
        usage = Usage()
        self.assertIsNone(usage.cached_tokens)
        self.assertIsNone(usage.cache_hit_ratio)

    def test_usage_missing_is_none(self) -> None:
        transport = FakeTransport(_reply())
        client = LLMClient(_llm_config(), http_post=transport)
        self.assertIsNone(client.chat([user_message("x")]).usage)

    def test_reasoning_content_captured(self) -> None:
        transport = FakeTransport(_reply(reasoning="让我想想……"))
        client = LLMClient(_llm_config(), http_post=transport)
        reply = client.chat([user_message("x")])
        self.assertEqual(reply.reasoning_content, "让我想想……")
        self.assertIn("reasoning_content", reply.as_dict())


class FakeResponse:
    """假的 urlopen 返回值（上下文管理器 + read）。"""

    def __init__(self, payload: Any) -> None:
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount is None or amount < 0 else self._body[:amount]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class TestErrorMapping(unittest.TestCase):
    """HTTP 状态码 / 网络异常 → 本模块异常类型。

    这一层映射在**真实传输函数**里，因此这里 patch ``urllib.request.urlopen``
    走真实传输，而不是注入假传输（假传输会绕过映射）。
    """

    def _client(self, *, retries: str = "0") -> LLMClient:
        client = LLMClient(_llm_config())
        client.llm = dataclasses.replace(client.llm, max_retries=int(retries))
        client._sleep = lambda _s: None
        return client

    def _run(self, side_effect: Any, *, retries: str = "0"):
        # side_effect 需要是可迭代的：单个异常/响应要包成列表，
        # 否则 mock 会把它当成「不可调用、不可迭代」的值而报 TypeError。
        effects = side_effect if isinstance(side_effect, list) else [side_effect]
        client = self._client(retries=retries)
        with mock.patch("llm_client.urllib.request.urlopen", side_effect=effects):
            return client.chat([user_message("x")])

    def test_401_is_auth_error(self) -> None:
        with self.assertRaises(LLMAuthError):
            self._run(http_error(401))

    def test_403_is_auth_error(self) -> None:
        with self.assertRaises(LLMAuthError):
            self._run(http_error(403))

    def test_429_is_rate_limit(self) -> None:
        with self.assertRaises(LLMRateLimitError):
            self._run(http_error(429))

    def test_500_is_transport_error(self) -> None:
        with self.assertRaises(LLMTransportError):
            self._run(http_error(500))

    def test_400_not_retried(self) -> None:
        """4xx（非鉴权/限流）重试无意义，必须只发一次请求。"""
        opener = mock.Mock(side_effect=http_error(400))
        with mock.patch("llm_client.urllib.request.urlopen", opener):
            with self.assertRaises(LLMRequestError):
                self._client(retries="3").chat([user_message("x")])
        self.assertEqual(opener.call_count, 1)

    def test_404_not_retried(self) -> None:
        opener = mock.Mock(side_effect=http_error(404))
        with mock.patch("llm_client.urllib.request.urlopen", opener):
            with self.assertRaises(LLMRequestError):
                self._client(retries="3").chat([user_message("x")])
        self.assertEqual(opener.call_count, 1)

    def test_500_then_success_retries(self) -> None:
        opener = mock.Mock(side_effect=[http_error(503), FakeResponse(_reply(content="ok"))])
        with mock.patch("llm_client.urllib.request.urlopen", opener):
            reply = self._client(retries="2").chat([user_message("x")])
        self.assertEqual(reply.content, "ok")
        self.assertEqual(opener.call_count, 2)

    def test_429_then_success_retries(self) -> None:
        opener = mock.Mock(side_effect=[http_error(429), FakeResponse(_reply(content="ok"))])
        with mock.patch("llm_client.urllib.request.urlopen", opener):
            reply = self._client(retries="2").chat([user_message("x")])
        self.assertEqual(reply.content, "ok")
        self.assertEqual(opener.call_count, 2)

    def test_timeout_maps_and_retries(self) -> None:
        opener = mock.Mock(side_effect=[socket.timeout(), FakeResponse(_reply(content="ok"))])
        with mock.patch("llm_client.urllib.request.urlopen", opener):
            reply = self._client(retries="1").chat([user_message("x")])
        self.assertEqual(reply.content, "ok")
        self.assertEqual(opener.call_count, 2)

    def test_timeout_exhausted_raises(self) -> None:
        with self.assertRaises(LLMTimeoutError):
            self._run([socket.timeout(), socket.timeout()], retries="1")

    def test_urlerror_connection_maps(self) -> None:
        with self.assertRaises(LLMTransportError) as err:
            self._run(urllib.error.URLError("connection refused"))
        self.assertIn("无法连接", str(err.exception))

    def test_sleep_called_between_retries(self) -> None:
        delays: list[float] = []
        client = self._client(retries="2")
        client._sleep = delays.append
        opener = mock.Mock(side_effect=[http_error(503), FakeResponse(_reply(content="ok"))])
        with mock.patch("llm_client.urllib.request.urlopen", opener):
            client.chat([user_message("x")])
        self.assertEqual(len(delays), 1)
        self.assertGreater(delays[0], 0)

    def test_non_json_body(self) -> None:
        with self.assertRaises(LLMResponseError):
            self._run(FakeResponse(b"<html>bad gateway</html>"))

    def test_non_object_body(self) -> None:
        with self.assertRaises(LLMResponseError):
            self._run(FakeResponse([1, 2, 3]))

    def test_missing_choices(self) -> None:
        client = LLMClient(_llm_config(), http_post=FakeTransport({"id": "x"}))
        with self.assertRaises(LLMResponseError) as err:
            client.chat([user_message("x")])
        self.assertIn("choices", str(err.exception))

    def test_error_field_in_body(self) -> None:
        client = LLMClient(_llm_config(), http_post=FakeTransport({"error": {"message": "quota exceeded"}}))
        with self.assertRaises(LLMResponseError) as err:
            client.chat([user_message("x")])
        self.assertIn("quota exceeded", str(err.exception))

    def test_transport_error_from_fake_transport_is_retried(self) -> None:
        """假传输抛已映射的异常时，客户端同样按重试策略处理。"""
        transport = FakeTransport(LLMTransportError("boom"), _reply(content="ok"))
        client = LLMClient(_llm_config(), http_post=transport)
        client.llm = dataclasses.replace(client.llm, max_retries=2)
        client._sleep = lambda _s: None
        self.assertEqual(client.chat([user_message("x")]).content, "ok")
        self.assertEqual(len(transport.calls), 2)

    def test_all_errors_are_llm_error(self) -> None:
        for exc in (LLMAuthError, LLMEmptyCompletionError, LLMRateLimitError,
                    LLMRequestError, LLMResponseError, LLMTimeoutError,
                    LLMTransportError, LLMConfigError):
            self.assertTrue(issubclass(exc, LLMError), msg=exc)


class TestRedaction(unittest.TestCase):
    def test_key_not_in_response_error(self) -> None:
        client = LLMClient(
            _llm_config(), http_post=FakeTransport({"error": {"message": f"bad key {_FAKE_KEY}"}})
        )
        with self.assertRaises(LLMResponseError) as err:
            client.chat([user_message("x")])
        self.assertNotIn(_FAKE_KEY, str(err.exception))
        self.assertIn("***", str(err.exception))

    def test_describe_never_shows_full_key(self) -> None:
        info = LLMClient(_llm_config()).describe()
        self.assertNotIn(_FAKE_KEY, json.dumps(info))


class TestMessageHelpers(unittest.TestCase):
    def test_message_shapes(self) -> None:
        self.assertEqual(system_message("s"), {"role": "system", "content": "s"})
        self.assertEqual(user_message("u"), {"role": "user", "content": "u"})
        self.assertEqual(assistant_message("a"), {"role": "assistant", "content": "a"})
        self.assertEqual(
            tool_result_message("c1", "{}"),
            {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        )

    def test_assistant_with_tool_calls(self) -> None:
        msg = assistant_message(None, tool_calls=[{"id": "c1"}])
        self.assertIsNone(msg["content"])
        self.assertEqual(msg["tool_calls"], [{"id": "c1"}])

    def test_tool_schema_shape(self) -> None:
        schema = tool_schema(
            "submit", "desc", {"a": {"type": "string"}}, required=["a"]
        )
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "submit")
        self.assertEqual(schema["function"]["parameters"]["type"], "object")
        self.assertEqual(schema["function"]["parameters"]["required"], ["a"])
        self.assertFalse(schema["function"]["parameters"]["additionalProperties"])


class TestCli(unittest.TestCase):
    def test_no_call_prints_describe(self) -> None:
        import contextlib
        import io

        import llm_client as mod

        with mock.patch.dict(os.environ, {
            "KURUMI_LLM_BASE_URL": _BASE, "KURUMI_LLM_API_KEY": _FAKE_KEY,
            "KURUMI_LLM_MODEL_ID": _MODEL,
        }, clear=False):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = mod._main(["--no-call"])
        self.assertEqual(code, 0)
        self.assertIn(_MODEL, out.getvalue())
        self.assertNotIn(_FAKE_KEY, out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
