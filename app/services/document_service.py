import asyncio
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from loguru import logger

from app.core.config import Settings
from app.core.errors import ServiceError
from app.parsers import DocParser, DocxParser, PdfParser, TxtParser


OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    filename: str
    extension: str
    pages: list[str] | None = None


class DocumentService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        docx_parser = DocxParser()
        pdf_parser = PdfParser()
        self.parsers = {
            "docx": docx_parser,
            "doc": DocParser(settings, docx_parser),
            "pdf": pdf_parser,
            "txt": TxtParser(),
        }
        self.pdf_parser = pdf_parser

    async def extract_upload(self, upload: UploadFile) -> tuple[str, str]:
        parsed = await self.extract_upload_document(upload)
        return parsed.text, parsed.filename

    async def extract_upload_document(self, upload: UploadFile) -> ParsedDocument:
        filename = Path(upload.filename or "").name
        suffix = Path(filename).suffix.lower().lstrip(".")
        logger.debug("document_received filename={} extension={}", filename or "unknown", suffix or "none")
        if not filename or not suffix:
            await upload.close()
            raise ServiceError(400, "INVALID_FILENAME", "文件名或扩展名无效")
        if suffix not in self.settings.allowed_extension_set or suffix not in self.parsers:
            await upload.close()
            raise ServiceError(415, "UNSUPPORTED_FILE_TYPE", f"不支持的文件类型: .{suffix}")

        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="job-", dir=self.settings.temp_dir))
        safe_stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(filename).stem)[:80] or "input"
        target = work_dir / f"{safe_stem}.{suffix}"
        try:
            size_bytes = await self._save_upload(upload, target)
            logger.debug(
                "document_saved filename={} extension={} size_bytes={}",
                filename,
                suffix,
                size_bytes,
            )
            await asyncio.to_thread(self._validate_signature, target, suffix)
            logger.debug("document_signature_valid filename={} extension={}", filename, suffix)
            pages: list[str] | None = None
            if suffix == "pdf":
                pages = await self.pdf_parser.extract_pages(target, self.settings.summary_max_pdf_pages)
                text = "\n".join(page for page in pages if page)
            else:
                text = await self.parsers[suffix].extract(target)
            text = self._clean_text(text)
            if pages is not None:
                pages = [self._clean_text(page) for page in pages]
            if not text:
                raise ServiceError(422, "NO_READABLE_TEXT", "文档中未提取到可读文字")
            logger.info(
                "📥 文件接收完成 | 文件名={} | 类型={} | 大小={} | 文本长度={}字符",
                filename,
                suffix,
                self._format_size(size_bytes),
                len(text),
            )
            return ParsedDocument(text=text, filename=filename, extension=suffix, pages=pages)
        finally:
            await upload.close()
            await asyncio.to_thread(shutil.rmtree, work_dir, True)
            logger.debug("document_temp_files_removed filename={}", filename)

    async def _save_upload(self, upload: UploadFile, target: Path) -> int:
        total = 0
        with target.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > self.settings.upload_max_bytes:
                    raise ServiceError(
                        413,
                        "FILE_TOO_LARGE",
                        f"文件不能超过 {self.settings.upload_max_mb} MB",
                    )
                output.write(chunk)
        if total == 0:
            raise ServiceError(400, "EMPTY_FILE", "上传文件为空")
        return total

    @staticmethod
    def _validate_signature(path: Path, suffix: str) -> None:
        header = path.read_bytes()[:16]
        if suffix == "doc" and not header.startswith(OLE_SIGNATURE):
            raise ServiceError(400, "FILE_SIGNATURE_MISMATCH", "文件内容不是有效的 DOC 格式")
        if suffix == "docx":
            if not zipfile.is_zipfile(path):
                raise ServiceError(400, "FILE_SIGNATURE_MISMATCH", "文件内容不是有效的 DOCX 格式")
            try:
                with zipfile.ZipFile(path) as archive:
                    names = set(archive.namelist())
                    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                        raise ServiceError(
                            400, "FILE_SIGNATURE_MISMATCH", "文件内容不是有效的 DOCX 格式"
                        )
            except zipfile.BadZipFile as exc:
                raise ServiceError(400, "FILE_SIGNATURE_MISMATCH", "DOCX 文件已损坏") from exc
        if suffix == "txt" and b"\x00" in header:
            raise ServiceError(400, "FILE_SIGNATURE_MISMATCH", "TXT 文件包含二进制内容")
        if suffix == "pdf" and not header.startswith(b"%PDF-"):
            raise ServiceError(400, "FILE_SIGNATURE_MISMATCH", "文件内容不是有效的 PDF 格式")

    @staticmethod
    def _clean_text(text: str) -> str:
        text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes}B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f}KB"
        return f"{size_bytes / (1024 * 1024):.1f}MB"
