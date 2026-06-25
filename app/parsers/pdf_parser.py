import asyncio
from pathlib import Path

from app.core.errors import ServiceError
from app.parsers.base import DocumentParser


def _extract_pdf_pages(path: Path, max_pages: int | None = None) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ServiceError(500, "PDF_PARSER_UNAVAILABLE", "PDF 解析组件不可用") from exc

    reader = PdfReader(str(path))
    pages: list[str] = []
    limit = min(len(reader.pages), max_pages) if max_pages is not None else len(reader.pages)
    for index in range(limit):
        text = reader.pages[index].extract_text() or ""
        pages.append(text.strip())
    return pages


class PdfParser(DocumentParser):
    async def extract_pages(self, path: Path, max_pages: int | None = None) -> list[str]:
        try:
            return await asyncio.to_thread(_extract_pdf_pages, path, max_pages)
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(422, "DOCUMENT_PARSE_FAILED", "PDF 文档无法解析") from exc

    async def extract(self, path: Path) -> str:
        pages = await self.extract_pages(path)
        return "\n".join(page for page in pages if page)
