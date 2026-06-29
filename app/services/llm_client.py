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
from app.prompts.document_summary import (
    SYSTEM_PROMPT as SUMMARY_SYSTEM_PROMPT,
    build_user_prompt as build_summary_user_prompt,
)


LOG_PREVIEW_CHARS = 600
LOG_RESPONSE_HEAD_CHARS = 60
LOG_RESPONSE_TAIL_CHARS = 60
DIAGNOSTIC_RESPONSE_HEADERS = (
    "content-length",
    "transfer-encoding",
    "server",
    "date",
    "x-request-id",
    "x-correlation-id",
    "traceparent",
    "x-envoy-upstream-service-time",
    "x-openai-request-id",
)


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._semaphore = asyncio.Semaphore(settings.llm_max_concurrency)

    async def extract_draft_reason(self, document_text: str) -> str:
        payload = await self._chat_json(
            SYSTEM_PROMPT,
            build_user_prompt(document_text),
            input_chars=len(document_text),
            max_tokens=self.settings.llm_max_tokens,
        )
        reason = payload.get("draft_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ServiceError(502, "LLM_INVALID_RESPONSE", "LLM 返回内容格式错误")
        return reason.strip()

    async def summarize_document(self, document_text: str) -> str:
        payload = await self._chat_json(
            SUMMARY_SYSTEM_PROMPT,
            build_summary_user_prompt(document_text),
            input_chars=len(document_text),
            max_tokens=max(self.settings.llm_max_tokens, 800),
        )
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ServiceError(502, "LLM_INVALID_RESPONSE", "LLM 返回内容格式错误")
        return summary.strip()

    async def _chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        input_chars: int,
        max_tokens: int,
    ) -> dict[str, Any]:
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
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.settings.llm_temperature,
            "max_tokens": max_tokens,
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
                    async with self._semaphore:
                        logger.debug(
                            "llm_request_started model={} input_chars={} attempt={}",
                            self.settings.llm_model,
                            input_chars,
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
                    try:
                        response_payload = response.json()
                    except json.JSONDecodeError as exc:
                        last_error = exc
                        logger.warning(
                            "⚠️ 模型HTTP响应不是JSON | 模型={} | 第{}次 | URL={} | 状态={} | "
                            "HTTP版本={} | Content-Type={} | 编码={} | body_bytes={} | "
                            "JSON错误=行{}列{}位置{}:{} | 响应头={} | 耗时={:.2f}s | 响应首尾={}",
                            self.settings.llm_model,
                            attempt + 1,
                            response.request.url.copy_with(query=None),
                            response.status_code,
                            response.http_version or "unknown",
                            response.headers.get("content-type", ""),
                            response.encoding or "unknown",
                            len(response.content),
                            exc.lineno,
                            exc.colno,
                            exc.pos,
                            exc.msg,
                            self._diagnostic_headers(response),
                            time.perf_counter() - started_at,
                            self._response_preview(response),
                        )
                        if attempt < self.settings.llm_max_retries:
                            await asyncio.sleep(min(0.5 * (2**attempt), 2.0))
                            continue
                        raise

                    try:
                        result = self._decode_response_json(response_payload)
                    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                        last_error = exc
                        self._log_invalid_llm_payload(response_payload, exc, attempt + 1)
                        if attempt < self.settings.llm_max_retries:
                            await asyncio.sleep(min(0.5 * (2**attempt), 2.0))
                            continue
                        raise

                    logger.debug(
                        "llm_request_completed model={} status={} result_chars={} duration_ms={:.2f}",
                        self.settings.llm_model,
                        response.status_code,
                        len(str(result)),
                        (time.perf_counter() - started_at) * 1000,
                    )
                    return result
                except ServiceError:
                    raise
                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                    last_error = exc
                    logger.warning(
                        "⚠️ 模型调用异常，准备重试 | 模型={} | 第{}次 | 异常={} | 耗时={:.2f}s",
                        self.settings.llm_model,
                        attempt + 1,
                        type(exc).__name__,
                        time.perf_counter() - started_at,
                    )
                    if attempt < self.settings.llm_max_retries:
                        await asyncio.sleep(min(0.5 * (2**attempt), 2.0))
                        continue
                except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    last_error = exc
                    logger.debug(
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
        parsed = LLMClient._decode_response_json(payload)
        reason = parsed.get("draft_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("draft_reason is missing")
        return reason.strip()

    @staticmethod
    def _decode_response_json(payload: dict[str, Any]) -> dict[str, Any]:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("message content is not a string")

        content = re.sub(r"<think>.*?</think>", "", content, flags=re.IGNORECASE | re.DOTALL).strip()
        parsed = LLMClient._decode_json_object(content)
        return parsed

    @staticmethod
    def _decode_json_object(content: str) -> dict[str, Any]:
        candidates = [content]
        fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.IGNORECASE | re.DOTALL)
        candidates.extend(fenced)

        decoder = json.JSONDecoder()
        for candidate in candidates:
            candidate = candidate.strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

            for index, char in enumerate(candidate):
                if char != "{":
                    continue
                try:
                    parsed, _ = decoder.raw_decode(candidate[index:])
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed

        raise json.JSONDecodeError("No JSON object found", content, 0)

    def _log_invalid_llm_payload(
        self,
        payload: dict[str, Any],
        exc: Exception,
        attempt: int,
    ) -> None:
        content: Any = None
        finish_reason: Any = None
        try:
            choice = payload["choices"][0]
            finish_reason = choice.get("finish_reason")
            content = choice["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            pass

        logger.warning(
            "⚠️ 模型消息内容不是有效JSON | 模型={} | 第{}次 | 异常={} | finish_reason={} | content_type={} | content_preview={} | payload_keys={}",
            self.settings.llm_model,
            attempt,
            type(exc).__name__,
            finish_reason,
            type(content).__name__,
            self._preview(content) if isinstance(content, str) else "",
            ",".join(payload.keys()),
        )

    @staticmethod
    def _preview(value: str, limit: int = LOG_PREVIEW_CHARS) -> str:
        value = value.replace("\r", "\\r").replace("\n", "\\n")
        if len(value) <= limit:
            return value
        return value[:limit] + "...<truncated>"

    @staticmethod
    def _response_preview(response: httpx.Response) -> str:
        if not response.content:
            return "<empty>"
        text = response.text
        if text:
            escaped = text.replace("\r", "\\r").replace("\n", "\\n")
            if len(escaped) <= LOG_RESPONSE_HEAD_CHARS + LOG_RESPONSE_TAIL_CHARS:
                return f"<full>{escaped}"
            return (
                f"<head>{escaped[:LOG_RESPONSE_HEAD_CHARS]}"
                f"...<truncated {len(escaped) - LOG_RESPONSE_HEAD_CHARS - LOG_RESPONSE_TAIL_CHARS} chars>..."
                f"<tail>{escaped[-LOG_RESPONSE_TAIL_CHARS:]}"
            )
        return f"<non-text bytes: {response.content[:64].hex()}>"

    @staticmethod
    def _diagnostic_headers(response: httpx.Response) -> str:
        values = [
            f"{name}={response.headers[name]}"
            for name in DIAGNOSTIC_RESPONSE_HEADERS
            if name in response.headers
        ]
        return ";".join(values) or "<none>"
