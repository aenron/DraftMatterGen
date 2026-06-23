import re

from loguru import logger

from app.core.config import Settings
from app.core.errors import ServiceError
from app.services.document_service import DocumentService
from app.services.llm_client import LLMClient


class DraftReasonService:
    def __init__(
        self,
        settings: Settings,
        document_service: DocumentService | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.settings = settings
        self.document_service = document_service or DocumentService(settings)
        self.llm_client = llm_client or LLMClient(settings)

    async def extract_from_upload(self, upload) -> tuple[str, str, int]:
        text, filename = await self.document_service.extract_upload(upload)
        reason = await self._extract_text(text)
        normalized = self._normalize_reason(reason)
        logger.debug(
            "draft_reason_completed filename={} source_chars={} result_chars={}",
            filename,
            len(text),
            len(normalized),
        )
        return normalized, filename, len(text)

    async def _extract_text(self, text: str) -> str:
        if len(text) <= self.settings.extract_max_chars:
            return await self.llm_client.extract_draft_reason(text)

        chunks = self._split_chunks(text)
        logger.info("long_document_split source_chars={} chunks={}", len(text), len(chunks))
        candidates = [await self.llm_client.extract_draft_reason(chunk) for chunk in chunks]
        merged_input = "以下是同一文档各部分提取出的候选拟稿事由，请结合并去重，输出最终拟稿事由：\n" + "\n".join(
            f"{index + 1}. {candidate}" for index, candidate in enumerate(candidates)
        )
        return await self.llm_client.extract_draft_reason(merged_input)

    def _split_chunks(self, text: str) -> list[str]:
        limit = self.settings.extract_max_chars
        chunks: list[str] = []
        current: list[str] = []
        current_size = 0
        for paragraph in text.splitlines():
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if len(paragraph) > limit:
                if current:
                    chunks.append("\n".join(current))
                    current, current_size = [], 0
                chunks.extend(paragraph[i : i + limit] for i in range(0, len(paragraph), limit))
                continue
            if current and current_size + len(paragraph) + 1 > limit:
                chunks.append("\n".join(current))
                current, current_size = [], 0
            current.append(paragraph)
            current_size += len(paragraph) + 1
        if current:
            chunks.append("\n".join(current))
        if len(chunks) > self.settings.max_document_chunks:
            raise ServiceError(413, "DOCUMENT_TOO_LONG", "文档内容过长，超过可处理范围")
        return chunks

    @staticmethod
    def _normalize_reason(reason: str) -> str:
        reason = reason.strip().strip('"').strip()
        reason = re.sub(r"^拟稿事由\s*[：:]\s*", "", reason)
        reason = re.sub(r"\s+", "", reason)
        if not reason:
            raise ServiceError(502, "EMPTY_LLM_RESULT", "LLM 未返回有效的拟稿事由")
        if len(reason) > 300:
            raise ServiceError(502, "LLM_RESULT_TOO_LONG", "LLM 返回的拟稿事由过长")
        return reason
