import asyncio
from pathlib import Path

import httpx
from fastapi import UploadFile

from app.core.config import Settings
from app.services.draft_reason_service import DraftReasonService
from app.services.document_service import DocumentService
from app.services.llm_client import LLMClient


REFERENCE_DIR = Path(__file__).parents[1] / "参考文件"


def test_reference_docx_full_pipeline(tmp_path: Path) -> None:
    expected = "为保障三院地区机房气体灭火系统的稳定运行，我办拟与原服务商续签维保服务合同。报送相关部门阅示。"

    def handler(request: httpx.Request) -> httpx.Response:
        assert "上海智伏机电科技工程中心" in request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": f'{{"draft_reason":"{expected}"}}'}}]},
        )

    settings = Settings(
        TEMP_DIR=tmp_path,
        LLM_BASE_URL="https://llm.test/v1",
        LLM_MODEL="test-model",
    )
    service = DraftReasonService(
        settings,
        document_service=DocumentService(settings),
        llm_client=LLMClient(settings, transport=httpx.MockTransport(handler)),
    )
    source = REFERENCE_DIR / "样例1.docx"
    with source.open("rb") as stream:
        upload = UploadFile(filename=source.name, file=stream)
        reason, filename, chars = asyncio.run(service.extract_from_upload(upload))

    assert reason == expected
    assert filename == "样例1.docx"
    assert chars > 100
    assert list(tmp_path.iterdir()) == []
