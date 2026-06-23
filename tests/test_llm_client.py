import asyncio
import json

import httpx
import pytest

from app.core.config import Settings
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
