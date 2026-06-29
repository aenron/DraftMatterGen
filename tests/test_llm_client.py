import asyncio
import json

import httpx
import pytest
from loguru import logger

from app.core.config import Settings
from app.core.errors import ServiceError
from app.services.llm_client import LLMClient


def test_parse_json_response() -> None:
    payload = {
        "choices": [
            {"message": {"content": '{"draft_reason":"办理相关事项。"}'}}
        ]
    }
    assert LLMClient._parse_response(payload) == "办理相关事项。"


def test_parse_markdown_json_response() -> None:
    payload = {
        "choices": [
            {"message": {"content": '```json\n{"draft_reason":"办理相关事项。"}\n```'}}
        ]
    }
    assert LLMClient._parse_response(payload) == "办理相关事项。"


def test_parse_json_with_reasoning_and_explanation() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": '<think>分析过程</think>结果如下：\n```json\n{"draft_reason":"办理相关事项。"}\n```\n请查收。'
                }
            }
        ]
    }
    assert LLMClient._parse_response(payload) == "办理相关事项。"


def test_reject_missing_reason() -> None:
    with pytest.raises(ValueError):
        LLMClient._parse_response({"choices": [{"message": {"content": "{}"}}]})


def test_llm_http_round_trip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer secret"
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert "公文正文" in body["messages"][1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"draft_reason":"拟办理相关事项。"}'}}
                ]
            },
        )

    settings = Settings(
        LLM_BASE_URL="https://llm.test/v1",
        LLM_API_KEY="secret",
        LLM_MODEL="test-model",
    )
    client = LLMClient(settings, transport=httpx.MockTransport(handler))
    assert asyncio.run(client.extract_draft_reason("公文正文")) == "拟办理相关事项。"


def test_llm_max_concurrency_is_respected() -> None:
    active = 0
    max_active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"summary":"摘要"}'}}
                ]
            },
        )

    settings = Settings(
        LLM_BASE_URL="https://llm.test/v1",
        LLM_MODEL="test-model",
        LLM_MAX_CONCURRENCY=2,
    )
    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    async def run_many() -> None:
        await asyncio.gather(*(client.summarize_document(f"文档{i}") for i in range(5)))

    asyncio.run(run_many())
    assert max_active <= 2


def test_empty_http_response_logs_diagnostic_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-length": "0",
                "server": "test-gateway",
                "x-request-id": "upstream-123",
            },
            content=b"",
        )

    settings = Settings(
        LLM_BASE_URL="https://llm.test/v1",
        LLM_MODEL="test-model",
        LLM_MAX_RETRIES=0,
    )
    client = LLMClient(settings, transport=httpx.MockTransport(handler))
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        with pytest.raises(ServiceError) as exc_info:
            asyncio.run(client.extract_draft_reason("公文正文"))
    finally:
        logger.remove(sink_id)

    assert exc_info.value.code == "LLM_INVALID_RESPONSE"
    log_text = "".join(messages)
    assert "body_bytes=0" in log_text
    assert "响应首尾=<empty>" in log_text
    assert "content-length=0" in log_text
    assert "server=test-gateway" in log_text
    assert "x-request-id=upstream-123" in log_text


def test_non_json_http_response_logs_body_preview() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            content="upstream unavailable".encode(),
        )

    settings = Settings(
        LLM_BASE_URL="https://llm.test/v1",
        LLM_MODEL="test-model",
        LLM_MAX_RETRIES=0,
    )
    client = LLMClient(settings, transport=httpx.MockTransport(handler))
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        with pytest.raises(ServiceError):
            asyncio.run(client.extract_draft_reason("公文正文"))
    finally:
        logger.remove(sink_id)

    log_text = "".join(messages)
    assert "body_bytes=20" in log_text
    assert "Content-Type=text/plain; charset=utf-8" in log_text
    assert "响应首尾=<full>upstream unavailable" in log_text


def test_long_non_json_http_response_logs_head_and_tail() -> None:
    response_text = "HEAD-" + ("x" * 1500) + "-TAIL"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            content=response_text.encode(),
        )

    settings = Settings(
        LLM_BASE_URL="https://llm.test/v1",
        LLM_MODEL="test-model",
        LLM_MAX_RETRIES=0,
    )
    client = LLMClient(settings, transport=httpx.MockTransport(handler))
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        with pytest.raises(ServiceError):
            asyncio.run(client.extract_draft_reason("公文正文"))
    finally:
        logger.remove(sink_id)

    log_text = "".join(messages)
    assert "响应首尾=<head>HEAD-" in log_text
    assert "<truncated 1390 chars>" in log_text
    assert "<tail>" in log_text
    assert "-TAIL" in log_text
