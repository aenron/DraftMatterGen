import asyncio
import json
import re
import time
from dataclasses import dataclass
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


@dataclass(frozen=True)
class LLMEndpoint:
    role: str
    base_url: str
    api_key: str | None
    model: str
    chat_completions_path: str

    @property
    def url(self) -> str:
        return self.base_url.rstrip("/") + "/" + self.chat_completions_path.lstrip("/")


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

        primary = LLMEndpoint(
            role="主用",
            base_url=self.settings.llm_base_url,
            api_key=(
                self.settings.llm_api_key.get_secret_value()
                if self.settings.llm_api_key
                else None
            ),
            model=self.settings.llm_model,
            chat_completions_path=self.settings.llm_chat_completions_path,
        )
        backup = (
            LLMEndpoint(
                role="备用",
                base_url=self.settings.backup_llm_base_url,
                api_key=(
                    self.settings.backup_llm_api_key.get_secret_value()
                    if self.settings.backup_llm_api_key
                    else None
                ),
                model=self.settings.backup_llm_model,
                chat_completions_path=self.settings.backup_llm_chat_completions_path,
            )
            if self.settings.backup_llm_ready
            else None
        )

        last_error: Exception | None = None
        async with httpx.AsyncClient(
            timeout=self.settings.llm_timeout_seconds,
            transport=self.transport,
        ) as client:
            for attempt in range(self.settings.llm_max_retries + 1):
                try:
                    endpoints = [primary] if attempt == 0 or backup is None else [primary, backup]
                    if len(endpoints) == 1:
                        result = await self._request_endpoint(
                            client,
                            endpoints[0],
                            system_prompt,
                            user_prompt,
                            input_chars=input_chars,
                            max_tokens=max_tokens,
                            attempt=attempt + 1,
                        )
                    else:
                        logger.warning(
                            "↻ 启动主备LLM并发重试 | 第{}次 | 主用模型={} | 备用模型={}",
                            attempt + 1,
                            primary.model,
                            backup.model,
                        )
                        result = await self._race_endpoints(
                            client,
                            endpoints,
                            system_prompt,
                            user_prompt,
                            input_chars=input_chars,
                            max_tokens=max_tokens,
                            attempt=attempt + 1,
                        )
                    return result
                except (
                    ServiceError,
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    httpx.HTTPStatusError,
                    ValueError,
                    KeyError,
                    TypeError,
                    json.JSONDecodeError,
                ) as exc:
                    last_error = exc
                    logger.warning(
                        "⚠️ 本轮模型调用均失败 | 第{}次 | 异常={} | 是否继续重试={}",
                        attempt + 1,
                        type(exc).__name__,
                        attempt < self.settings.llm_max_retries,
                    )
                    if attempt < self.settings.llm_max_retries:
                        await asyncio.sleep(min(0.5 * (2**attempt), 2.0))
                        continue

        if isinstance(last_error, httpx.TimeoutException):
            raise ServiceError(504, "LLM_TIMEOUT", "LLM 服务调用超时") from last_error
        if isinstance(last_error, (ValueError, KeyError, TypeError, json.JSONDecodeError)):
            raise ServiceError(502, "LLM_INVALID_RESPONSE", "LLM 返回内容格式错误") from last_error
        if isinstance(last_error, ServiceError):
            raise last_error
        raise ServiceError(502, "LLM_UNAVAILABLE", "LLM 服务暂时不可用") from last_error

    async def _race_endpoints(
        self,
        client: httpx.AsyncClient,
        endpoints: list[LLMEndpoint],
        system_prompt: str,
        user_prompt: str,
        *,
        input_chars: int,
        max_tokens: int,
        attempt: int,
    ) -> dict[str, Any]:
        tasks = [
            asyncio.create_task(
                self._request_endpoint(
                    client,
                    endpoint,
                    system_prompt,
                    user_prompt,
                    input_chars=input_chars,
                    max_tokens=max_tokens,
                    attempt=attempt,
                )
            )
            for endpoint in endpoints
        ]
        errors: list[BaseException] = []
        try:
            for completed in asyncio.as_completed(tasks):
                try:
                    return await completed
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    errors.append(exc)
            raise errors[-1]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _request_endpoint(
        self,
        client: httpx.AsyncClient,
        endpoint: LLMEndpoint,
        system_prompt: str,
        user_prompt: str,
        *,
        input_chars: int,
        max_tokens: int,
        attempt: int,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        payload: dict[str, Any] = {
            "model": endpoint.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.settings.llm_temperature,
            "max_tokens": max_tokens,
        }
        if self.settings.llm_response_format_json:
            payload["response_format"] = {"type": "json_object"}

        started_at = time.perf_counter()
        async with self._semaphore:
            logger.debug(
                "llm_request_started role={} model={} input_chars={} attempt={}",
                endpoint.role,
                endpoint.model,
                input_chars,
                attempt,
            )
            response = await client.post(endpoint.url, headers=headers, json=payload)
        self._log_http_response(response, endpoint, attempt, started_at)
        if response.status_code == 429 or response.status_code >= 500:
            self._log_http_error_response(response, endpoint, attempt)
            response.raise_for_status()
        if response.status_code >= 400:
            self._log_http_error_response(response, endpoint, attempt)
            raise ServiceError(
                502,
                "LLM_REQUEST_REJECTED",
                f"{endpoint.role} LLM 服务拒绝请求，状态码 {response.status_code}",
            )
        try:
            response_payload = response.json()
        except json.JSONDecodeError as exc:
            self._log_non_json_response(response, endpoint, exc, attempt, started_at)
            raise
        self._log_message_content(response_payload, endpoint, attempt)
        try:
            result = self._decode_response_json(response_payload)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._log_invalid_llm_payload(
                response_payload, exc, attempt, endpoint.role, endpoint.model
            )
            raise
        logger.info(
            "✅ 模型响应解析成功 | 类型={} | 模型={} | 第{}次 | 结果长度={}",
            endpoint.role,
            endpoint.model,
            attempt,
            len(str(result)),
        )
        return result

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
        endpoint_role: str,
        model: str,
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
            "⚠️ 模型消息内容不是有效JSON | 类型={} | 模型={} | 第{}次 | 异常={} | "
            "finish_reason={} | content_type={} | content_preview={} | payload_keys={}",
            endpoint_role,
            model,
            attempt,
            type(exc).__name__,
            finish_reason,
            type(content).__name__,
            self._preview(content) if isinstance(content, str) else "",
            ",".join(payload.keys()),
        )

    def _log_non_json_response(
        self,
        response: httpx.Response,
        endpoint: LLMEndpoint,
        exc: json.JSONDecodeError,
        attempt: int,
        started_at: float,
    ) -> None:
        logger.warning(
            "⚠️ 模型HTTP响应不是JSON | 类型={} | 模型={} | 第{}次 | URL={} | 状态={} | "
            "HTTP版本={} | Content-Type={} | 编码={} | body_bytes={} | "
            "JSON错误=行{}列{}位置{}:{} | 响应头={} | 耗时={:.2f}s | 响应首尾={}",
            endpoint.role,
            endpoint.model,
            attempt,
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

    def _log_http_response(
        self,
        response: httpx.Response,
        endpoint: LLMEndpoint,
        attempt: int,
        started_at: float,
    ) -> None:
        logger.info(
            "🤖 模型HTTP响应 | 类型={} | 模型={} | 第{}次 | 状态={} | Content-Type={} | "
            "body_bytes={} | 耗时={:.2f}s",
            endpoint.role,
            endpoint.model,
            attempt,
            response.status_code,
            response.headers.get("content-type", ""),
            len(response.content),
            time.perf_counter() - started_at,
        )

    def _log_message_content(
        self,
        payload: dict[str, Any],
        endpoint: LLMEndpoint,
        attempt: int,
    ) -> None:
        content: Any = None
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            pass
        logger.info(
            "💬 模型消息内容 | 类型={} | 模型={} | 第{}次 | content_type={} | 内容首尾={}",
            endpoint.role,
            endpoint.model,
            attempt,
            type(content).__name__,
            self._text_head_tail(content) if isinstance(content, str) else "<unavailable>",
        )

    def _log_http_error_response(
        self,
        response: httpx.Response,
        endpoint: LLMEndpoint,
        attempt: int,
    ) -> None:
        logger.warning(
            "⚠️ 模型HTTP错误响应 | 类型={} | 模型={} | 第{}次 | 状态={} | 响应首尾={}",
            endpoint.role,
            endpoint.model,
            attempt,
            response.status_code,
            self._response_preview(response),
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
            return LLMClient._text_head_tail(text)
        return f"<non-text bytes: {response.content[:64].hex()}>"

    @staticmethod
    def _text_head_tail(value: str) -> str:
        escaped = value.replace("\r", "\\r").replace("\n", "\\n")
        if len(escaped) <= LOG_RESPONSE_HEAD_CHARS + LOG_RESPONSE_TAIL_CHARS:
            return f"<full>{escaped}"
        return (
            f"<head>{escaped[:LOG_RESPONSE_HEAD_CHARS]}"
            f"...<truncated {len(escaped) - LOG_RESPONSE_HEAD_CHARS - LOG_RESPONSE_TAIL_CHARS} chars>..."
            f"<tail>{escaped[-LOG_RESPONSE_TAIL_CHARS:]}"
        )

    @staticmethod
    def _diagnostic_headers(response: httpx.Response) -> str:
        values = [
            f"{name}={response.headers[name]}"
            for name in DIAGNOSTIC_RESPONSE_HEADERS
            if name in response.headers
        ]
        return ";".join(values) or "<none>"
