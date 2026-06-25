import asyncio
import re
from pathlib import Path

from fastapi import UploadFile
from loguru import logger

from app.api.schemas import DocumentSummaryItem
from app.core.config import Settings
from app.core.errors import ServiceError
from app.services.document_service import DocumentService, ParsedDocument
from app.services.llm_client import LLMClient


SUMMARY_KEYWORDS = (
    "项目背景",
    "研究背景",
    "研究目标",
    "建设目标",
    "建设内容",
    "研究内容",
    "技术路线",
    "实施方案",
    "创新点",
    "预期成果",
    "经费预算",
    "进度安排",
    "申报单位",
)


class DocumentSummaryService:
    def __init__(
        self,
        settings: Settings,
        document_service: DocumentService | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.settings = settings
        self.document_service = document_service or DocumentService(settings)
        self.llm_client = llm_client or LLMClient(settings)

    async def summarize_uploads(self, uploads: list[UploadFile]) -> list[DocumentSummaryItem]:
        if not uploads:
            raise ServiceError(400, "NO_FILES", "请至少上传一个文件")
        if len(uploads) > self.settings.summary_max_files:
            raise ServiceError(
                413,
                "TOO_MANY_FILES",
                f"一次最多上传 {self.settings.summary_max_files} 个文件",
            )

        results: list[DocumentSummaryItem] = []
        for upload in uploads:
            results.append(await self._summarize_upload(upload))
        return results

    async def _summarize_upload(self, upload: UploadFile) -> DocumentSummaryItem:
        filename = Path(upload.filename or "").name or "unknown"
        suffix = Path(filename).suffix.lower().lstrip(".")
        if suffix == "xlsx":
            await upload.close()
            return DocumentSummaryItem(
                filename=filename,
                status="ignored",
                reason="xlsx 文件已按规则忽略",
            )
        if suffix and suffix not in {"doc", "docx", "pdf", "txt"}:
            await upload.close()
            return DocumentSummaryItem(
                filename=filename,
                status="failed",
                reason=f"不支持的文件类型: .{suffix}",
            )

        try:
            parsed = await self.document_service.extract_upload_document(upload)
            summary = await self._summarize_document(parsed)
            logger.debug(
                "document_summary_completed filename={} source_chars={} summary_chars={}",
                parsed.filename,
                len(parsed.text),
                len(summary),
            )
            return DocumentSummaryItem(
                filename=parsed.filename,
                status="succeeded",
                summary=summary,
                chars_processed=len(parsed.text),
            )
        except ServiceError as exc:
            return DocumentSummaryItem(filename=filename, status="failed", reason=exc.message)
        except Exception as exc:
            logger.exception("document_summary_failed filename={}", filename)
            return DocumentSummaryItem(filename=filename, status="failed", reason=str(exc))

    async def _summarize_document(self, parsed: ParsedDocument) -> str:
        text = parsed.text
        if len(text) <= self.settings.summary_chunk_max_chars:
            return await self.llm_client.summarize_document(text)

        toc = self._extract_toc_candidate(parsed)
        opening = self._extract_opening(parsed)
        chunks = self._select_summary_chunks(text)
        chunk_summaries: list[str] = []
        for index, chunk in enumerate(chunks):
            if index > 0 and self.settings.summary_chunk_delay_seconds:
                await asyncio.sleep(self.settings.summary_chunk_delay_seconds)
            chunk_summaries.append(await self.llm_client.summarize_document(chunk))

        final_input = self._build_final_summary_input(
            filename=parsed.filename,
            toc=toc,
            opening=opening,
            chunk_summaries=chunk_summaries,
        )
        return await self.llm_client.summarize_document(final_input)

    def _extract_opening(self, parsed: ParsedDocument) -> str:
        if parsed.pages:
            page_limit = min(self.settings.summary_initial_pdf_pages, len(parsed.pages))
            pages = [
                f"第{index + 1}页：\n{page}"
                for index, page in enumerate(parsed.pages[:page_limit])
                if page
            ]
            return "\n\n".join(pages)[: self.settings.summary_initial_chars]
        return parsed.text[: self.settings.summary_initial_chars]

    def _extract_toc_candidate(self, parsed: ParsedDocument) -> str:
        if parsed.pages:
            page_limit = min(self.settings.summary_toc_pdf_pages, len(parsed.pages))
            scan_text = "\n".join(page for page in parsed.pages[:page_limit] if page)
        else:
            scan_text = parsed.text[: self.settings.summary_toc_scan_chars]

        match = re.search(r"(目\s*录|contents)", scan_text, flags=re.IGNORECASE)
        if match:
            return scan_text[match.start() : match.start() + self.settings.summary_toc_max_chars]

        chapter_match = re.search(
            r"((第一[章节部分]|一[、.．]|1[.．]\s*)[^\n]{0,80}\n(?:.+\n){1,80})",
            scan_text,
        )
        if chapter_match:
            return chapter_match.group(1)[: self.settings.summary_toc_max_chars]
        return ""

    def _select_summary_chunks(self, text: str) -> list[str]:
        chunks = self._split_chunks(text)
        if len(chunks) <= self.settings.summary_max_chunks:
            return chunks

        selected: list[str] = []
        seen: set[int] = set()
        for index, chunk in enumerate(chunks):
            if any(keyword in chunk for keyword in SUMMARY_KEYWORDS):
                selected.append(chunk)
                seen.add(index)
                if len(selected) >= self.settings.summary_max_chunks:
                    return selected

        for index, chunk in enumerate(chunks):
            if index in seen:
                continue
            selected.append(chunk)
            if len(selected) >= self.settings.summary_max_chunks:
                break
        return selected

    def _split_chunks(self, text: str) -> list[str]:
        limit = self.settings.summary_chunk_max_chars
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
        return chunks

    @staticmethod
    def _build_final_summary_input(
        *,
        filename: str,
        toc: str,
        opening: str,
        chunk_summaries: list[str],
    ) -> str:
        parts = [f"文件名：{filename}"]
        if toc:
            parts.append(f"疑似目录：\n{toc}")
        if opening:
            parts.append(f"文档前部内容：\n{opening}")
        if chunk_summaries:
            parts.append(
                "正文切片摘要：\n"
                + "\n".join(
                    f"{index + 1}. {summary}" for index, summary in enumerate(chunk_summaries)
                )
            )
        return "\n\n".join(parts)
