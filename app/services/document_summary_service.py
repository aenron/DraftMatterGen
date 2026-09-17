import re
from pathlib import Path

from fastapi import UploadFile
from loguru import logger

from app.api.schemas import DocumentSummaryItem
from app.core.config import Settings
from app.core.errors import ServiceError
from app.services.document_service import DocumentService, ParsedDocument
from app.services.llm_client import LLMClient


SUMMARY_PARSEABLE_EXTENSIONS = {"doc", "docx", "pdf", "txt"}
SUMMARY_STAMP_NOTE = "文件需要盖章。"
SUMMARY_LAST_ITEM_SUFFIX = f"。{SUMMARY_STAMP_NOTE}"
SUMMARY_UNREADABLE_NOTICE = "可读取内容有限"


class DocumentSummaryService:
    def __init__(
        self,
        settings: Settings,
        document_service: DocumentService | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.settings = settings
        allowed_extensions = settings.summary_allowed_extension_set
        self.document_service = document_service or DocumentService(
            settings,
            allowed_extensions=allowed_extensions & SUMMARY_PARSEABLE_EXTENSIONS,
        )
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
        candidates: list[tuple[int, ParsedDocument]] = []
        for upload in uploads:
            result, parsed = await self._prepare_upload(upload)
            results.append(result)
            if parsed is not None:
                candidates.append((len(results) - 1, parsed))

        self._log_received_documents(candidates)

        for candidate_index, (result_index, parsed) in enumerate(candidates):
            try:
                summary = await self._summarize_document(
                    parsed,
                    max_chars=self._summary_body_char_limit(len(candidates), candidate_index),
                )
                logger.debug(
                    "document_summary_completed filename={} source_chars={} summary_chars={}",
                    parsed.filename,
                    len(parsed.text),
                    len(summary),
                )
                results[result_index] = DocumentSummaryItem(
                    filename=parsed.filename,
                    status="succeeded",
                    summary=summary,
                    chars_processed=len(parsed.text),
                )
            except ServiceError as exc:
                results[result_index] = DocumentSummaryItem(
                    filename=parsed.filename, status="failed", reason=exc.message
                )
            except Exception as exc:
                logger.exception("document_summary_failed filename={}", parsed.filename)
                results[result_index] = DocumentSummaryItem(
                    filename=parsed.filename, status="failed", reason=str(exc)
                )

        self._format_summary_results(results)
        return results

    async def _prepare_upload(
        self, upload: UploadFile
    ) -> tuple[DocumentSummaryItem, ParsedDocument | None]:
        filename = Path(upload.filename or "").name or "unknown"
        suffix = Path(filename).suffix.lower().lstrip(".")
        if suffix and suffix not in self.settings.summary_allowed_extension_set:
            await upload.close()
            return (
                DocumentSummaryItem(
                    filename=filename,
                    status="failed",
                    reason=f"不支持的文件类型: .{suffix}",
                ),
                None,
            )
        if suffix == "xlsx":
            await upload.close()
            return (
                DocumentSummaryItem(
                    filename=filename,
                    status="ignored",
                    reason="xlsx 文件已按规则忽略",
                ),
                None,
            )
        if suffix and suffix not in SUMMARY_PARSEABLE_EXTENSIONS:
            await upload.close()
            return (
                DocumentSummaryItem(
                    filename=filename,
                    status="failed",
                    reason=f"不支持的文件类型: .{suffix}",
                ),
                None,
            )

        try:
            parsed = await self.document_service.extract_upload_document(upload, log_received=False)
            return (
                DocumentSummaryItem(
                    filename=parsed.filename,
                    status="succeeded",
                    chars_processed=len(parsed.text),
                ),
                parsed,
            )
        except ServiceError as exc:
            return DocumentSummaryItem(filename=filename, status="failed", reason=exc.message), None
        except Exception as exc:
            logger.exception("document_summary_parse_failed filename={}", filename)
            return DocumentSummaryItem(filename=filename, status="failed", reason=str(exc)), None

    def _log_received_documents(self, candidates: list[tuple[int, ParsedDocument]]) -> None:
        if not candidates:
            return
        details = "；".join(
            "文件名={}，类型={}，大小={}，文本长度={}字符".format(
                parsed.filename,
                parsed.extension,
                DocumentService._format_size(parsed.size_bytes or 0),
                len(parsed.text),
            )
            for _, parsed in candidates
        )
        logger.info("📥 文件接收完成 | 文件数={} | 文件详情={}", len(candidates), details)

    async def _summarize_document(self, parsed: ParsedDocument, *, max_chars: int) -> str:
        text = parsed.text
        if len(text) <= self.settings.summary_chunk_max_chars:
            return await self.llm_client.summarize_document(text, max_chars=max_chars)

        toc = self._extract_toc_candidate(parsed)
        opening = self._extract_opening(parsed)
        source_text = f"文档开头内容：\n{opening}"
        if toc:
            source_text = f"疑似目录：\n{toc}\n\n{source_text}"
        return await self.llm_client.summarize_document(source_text, max_chars=max_chars)

    def _extract_opening(self, parsed: ParsedDocument) -> str:
        char_limit = min(
            self.settings.summary_initial_chars, self.settings.summary_chunk_max_chars
        )
        if parsed.pages:
            page_limit = min(self.settings.summary_initial_pdf_pages, len(parsed.pages))
            pages = [
                f"第{index + 1}页：\n{page}"
                for index, page in enumerate(parsed.pages[:page_limit])
                if page
            ]
            return "\n\n".join(pages)[:char_limit]
        return parsed.text[:char_limit]

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

    def _format_summary_results(self, results: list[DocumentSummaryItem]) -> None:
        successful = [
            item for item in results if item.status == "succeeded" and item.summary is not None
        ]
        if not successful:
            return

        for index, item in enumerate(successful):
            suffix = SUMMARY_LAST_ITEM_SUFFIX if index == len(successful) - 1 else "；"
            body = re.sub(
                rf"{re.escape(SUMMARY_UNREADABLE_NOTICE)}[。；;，,、]*", "", item.summary.strip()
            )
            item.summary = f"{body.rstrip('。；;')}{suffix}"

    def _summary_item_char_limit(self, successful_count: int) -> int:
        if successful_count == 1:
            return self.settings.summary_single_file_max_chars
        return self.settings.summary_multiple_total_chars // successful_count

    def _summary_body_char_limit(self, candidate_count: int, candidate_index: int) -> int:
        item_limit = self._summary_item_char_limit(candidate_count)
        if candidate_index == candidate_count - 1:
            return item_limit
        return max(0, item_limit - len("；"))
