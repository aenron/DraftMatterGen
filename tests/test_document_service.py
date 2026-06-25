import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile

from app.core.config import Settings
from app.core.errors import ServiceError
from app.services.document_service import DocumentService


def make_service(tmp_path: Path, max_mb: int = 20) -> DocumentService:
    return DocumentService(Settings(TEMP_DIR=tmp_path, UPLOAD_MAX_MB=max_mb))


def test_txt_upload_and_cleanup(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    upload = UploadFile(filename="sample.txt", file=BytesIO("第一行\n第二行".encode()))
    text, filename = asyncio.run(service.extract_upload(upload))
    assert text == "第一行\n第二行"
    assert filename == "sample.txt"
    assert list(tmp_path.iterdir()) == []


def test_reject_binary_disguised_as_txt(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    upload = UploadFile(filename="sample.txt", file=BytesIO(b"abc\x00def"))
    with pytest.raises(ServiceError) as error:
        asyncio.run(service.extract_upload(upload))
    assert error.value.code == "FILE_SIGNATURE_MISMATCH"
    assert list(tmp_path.iterdir()) == []


def test_reject_unsupported_extension(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    upload = UploadFile(filename="sample.exe", file=BytesIO(b"content"))
    with pytest.raises(ServiceError) as error:
        asyncio.run(service.extract_upload(upload))
    assert error.value.status_code == 415
