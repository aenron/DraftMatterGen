import asyncio
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile

from app.core.config import Settings
from app.services.document_service import ParsedDocument
from app.services.document_summary_service import DocumentSummaryService


class FakeDocumentService:
    def __init__(self, parsed: ParsedDocument) -> None:
        self.parsed = parsed

    async def extract_upload_document(self, upload):
        await upload.close()
        return self.parsed


class FakeLLMClient:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def summarize_document(self, document_text: str) -> str:
        self.inputs.append(document_text)
        return f"摘要{len(self.inputs)}"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        APP_ENV="test",
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="test-model",
        TEMP_DIR=tmp_path,
        SUMMARY_CHUNK_DELAY_SECONDS=0,
    )


def test_ignores_xlsx(tmp_path: Path) -> None:
    service = DocumentSummaryService(make_settings(tmp_path))
    upload = UploadFile(filename="budget.xlsx", file=BytesIO(b"content"))
    result = asyncio.run(service.summarize_uploads([upload]))[0]
    assert result.status == "ignored"
    assert result.reason == "xlsx 文件已按规则忽略"


def test_rejects_xlsx_when_summary_allowed_extensions_excludes_it(tmp_path: Path) -> None:
    settings = Settings(
        APP_ENV="test",
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="test-model",
        TEMP_DIR=tmp_path,
        SUMMARY_ALLOWED_EXTENSIONS="docx,doc,pdf,txt",
    )
    service = DocumentSummaryService(settings)
    upload = UploadFile(filename="budget.xlsx", file=BytesIO(b"content"))

    result = asyncio.run(service.summarize_uploads([upload]))[0]

    assert result.status == "failed"
    assert result.reason == "不支持的文件类型: .xlsx"


def test_summary_uses_summary_allowed_extensions_instead_of_global_allowed_extensions(
    tmp_path: Path,
) -> None:
    settings = Settings(
        APP_ENV="test",
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="test-model",
        TEMP_DIR=tmp_path,
        ALLOWED_EXTENSIONS="docx,doc,txt",
        SUMMARY_ALLOWED_EXTENSIONS="docx,doc,pdf,txt",
    )
    service = DocumentSummaryService(settings)

    assert "pdf" in service.document_service.allowed_extensions
    assert "pdf" not in settings.allowed_extension_set


def test_long_document_is_chunked_and_merged(tmp_path: Path) -> None:
    text = "\n".join(
        [
            "目录\n一、项目背景\n二、研究目标\n三、建设内容",
            "项目背景：" + "甲" * 6000,
            "研究目标：" + "乙" * 6000,
            "建设内容：" + "丙" * 6000,
        ]
    )
    parsed = ParsedDocument(text=text, filename="申报书.docx", extension="docx")
    llm = FakeLLMClient()
    service = DocumentSummaryService(make_settings(tmp_path), FakeDocumentService(parsed), llm)

    upload = UploadFile(filename="申报书.docx", file=BytesIO(b"content"))
    result = asyncio.run(service.summarize_uploads([upload]))[0]

    assert result.status == "succeeded"
    assert result.summary == "摘要4"
    assert len(llm.inputs) == 4
    assert "正文切片摘要" in llm.inputs[-1]


def test_pdf_opening_uses_page_boundaries(tmp_path: Path) -> None:
    parsed = ParsedDocument(
        text="第一页\n第二页\n第三页",
        filename="材料.pdf",
        extension="pdf",
        pages=["第一页", "第二页", "第三页"],
    )
    service = DocumentSummaryService(make_settings(tmp_path))
    opening = service._extract_opening(parsed)
    assert "第1页" in opening
    assert "第2页" in opening
