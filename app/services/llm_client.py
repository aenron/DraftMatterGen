import asyncio
import json
import re
import time
from typing import Any

import httpx
from loguru import logger

from app.core.config import Settings
from app.core.errors import ServiceError
from app.prompts.draft_reason import SYSTEM_PROMPT, build_user_prompt


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def extract_draft_reason(self, document_text: str) -> str:
        if not self.settings.llm_ready:
            raise ServiceError(503, "LLM_NOT_CONFIGURED", "LLM 服务尚未配置")

        url = (
            self.settings.llm_base_url.rstrip("/")
            + "/"
            + self.settings.llm_chat_completions_path.lstrip("/")
        )
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key.get_secret_value()}"

        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(document_text)},
            ],
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
        }
        if self.settings.llm_response_format_json:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        async with httpx.AsyncClient(
            timeout=self.settings.llm_timeout_seconds,
            transport=self.transport,
        ) as client:
            for attempt in range(self.settings.llm_max_retries + 1):
                started_at = time.perf_counter()
                try:
                    logger.info(
                        "llm_request_started model={} input_chars={} attempt={}",
                        self.settings.llm_model,
                        len(document_text),
                        attempt + 1,
                    )
                    response = await client.post(url, headers=headers, json=payload)
                    if response.status_code == 429 or response.status_code >= 500:
                        response.raise_for_status()
                    if response.status_code >= 400:
                        raise ServiceError(
                            502,
                            "LLM_REQUEST_REJECTED",
                            f"LLM 服务拒绝请求，状态码 {response.status_code}",
                        )
                    result = self._parse_response(response.json())
                    logger.info(
                        "llm_request_completed model={} status={} result_chars={} duration_ms={:.2f}",
                        self.settings.llm_model,
                        response.status_code,
                        len(result),
                        (time.perf_counter() - started_at) * 1000,
                    )
                    return result
                except ServiceError:
                    raise
                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                    last_error = exc
                    logger.warning(
                        "llm_request_retryable_error model={} attempt={} error_type={} duration_ms={:.2f}",
                        self.settings.llm_model,
                        attempt + 1,
                        type(exc).__name__,
                        (time.perf_counter() - started_at) * 1000,
                    )
                    if attempt < self.settings.llm_max_retries:
                        await asyncio.sleep(min(0.5 * (2**attempt), 2.0))
                        continue
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning(
                        "llm_invalid_response model={} error_type={}",
                        self.settings.llm_model,
                        type(exc).__name__,
                    )
                    raise ServiceError(502, "LLM_INVALID_RESPONSE", "LLM 返回内容格式错误") from exc

        if isinstance(last_error, httpx.TimeoutException):
            raise ServiceError(504, "LLM_TIMEOUT", "LLM 服务调用超时") from last_error
        raise ServiceError(502, "LLM_UNAVAILABLE", "LLM 服务暂时不可用") from last_error

    @staticmethod
    def _parse_response(payload: dict[str, Any]) -> str:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("message content is not a string")

        content = content.strip()
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
        parsed = json.loads(content)
        reason = parsed.get("draft_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("draft_reason is missing")
        return reason.strip()
