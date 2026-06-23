import asyncio
from pathlib import Path

from charset_normalizer import from_bytes

from app.core.errors import ServiceError
from app.parsers.base import DocumentParser


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    match = from_bytes(data).best()
    if match is None:
        raise ServiceError(422, "TEXT_DECODE_FAILED", "TXT 文件编码无法识别")
    return str(match)


class TxtParser(DocumentParser):
    async def extract(self, path: Path) -> str:
        try:
            data = await asyncio.to_thread(path.read_bytes)
            return _decode_text(data)
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(422, "DOCUMENT_PARSE_FAILED", "TXT 文件无法解析") from exc

