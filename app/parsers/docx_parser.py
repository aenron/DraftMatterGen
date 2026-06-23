import asyncio
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P

from app.core.errors import ServiceError
from app.parsers.base import DocumentParser


def _extract_docx(path: Path) -> str:
    document = Document(path)
    parts: list[str] = []

    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            text = Paragraph(child, document).text.strip()
            if text:
                parts.append(text)
        elif isinstance(child, CT_Tbl):
            table = Table(child, document)
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                line = "\t".join(value for value in cells if value)
                if line:
                    parts.append(line)

    return "\n".join(parts)


class DocxParser(DocumentParser):
    async def extract(self, path: Path) -> str:
        try:
            return await asyncio.to_thread(_extract_docx, path)
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(422, "DOCUMENT_PARSE_FAILED", "DOCX 文档无法解析") from exc

