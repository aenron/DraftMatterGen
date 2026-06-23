import asyncio
import os
from pathlib import Path

from loguru import logger

from app.core.config import Settings
from app.core.errors import ServiceError
from app.parsers.base import DocumentParser
from app.parsers.docx_parser import DocxParser


class DocParser(DocumentParser):
    def __init__(self, settings: Settings, docx_parser: DocxParser) -> None:
        self.settings = settings
        self.docx_parser = docx_parser

    async def extract(self, path: Path) -> str:
        output_dir = path.parent / "converted"
        output_dir.mkdir(exist_ok=True)
        profile_dir = path.parent / "libreoffice-profile"
        profile_dir.mkdir(exist_ok=True)

        env = os.environ.copy()
        env["HOME"] = str(profile_dir)
        logger.debug("doc_conversion_started filename={}", path.name)
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.libreoffice_binary,
                "--headless",
                f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
                "--convert-to",
                "docx",
                "--outdir",
                str(output_dir),
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.conversion_timeout_seconds
            )
        except FileNotFoundError as exc:
            logger.error("doc_converter_unavailable binary={}", self.settings.libreoffice_binary)
            raise ServiceError(500, "CONVERTER_UNAVAILABLE", "DOC 转换组件不可用") from exc
        except TimeoutError as exc:
            if "process" in locals():
                process.kill()
                await process.communicate()
            logger.warning("doc_conversion_timeout filename={}", path.name)
            raise ServiceError(422, "DOCUMENT_CONVERSION_TIMEOUT", "DOC 文档转换超时") from exc

        converted = output_dir / f"{path.stem}.docx"
        if process.returncode != 0 or not converted.exists():
            logger.warning(
                "doc_conversion_failed filename={} return_code={}", path.name, process.returncode
            )
            raise ServiceError(
                422,
                "DOCUMENT_CONVERSION_FAILED",
                "DOC 文档转换失败",
            )
        logger.debug("doc_conversion_completed filename={}", path.name)
        return await self.docx_parser.extract(converted)
