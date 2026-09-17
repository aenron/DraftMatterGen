import asyncio
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile

from app.api.schemas import DocumentSummaryItem
from app.core.config import Settings
from app.prompts.document_summary import SYSTEM_PROMPT
from app.services.document_service import ParsedDocument
from app.services.document_summary_service import DocumentSummaryService, SUMMARY_STAMP_NOTE


class FakeDocumentService:
    def __init__(self, parsed: ParsedDocument) -> None:
        self.parsed = parsed

    async def extract_upload_document(self, upload):
        await upload.close()
        return self.parsed


class FakeLLMClient:
    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.max_chars: list[int | None] = []

    async def summarize_document(self, document_text: str, *, max_chars: int | None = None) -> str:
        self.inputs.append(document_text)
        self.max_chars.append(max_chars)
        return f"摘要{len(self.inputs)}"


class MultipleFakeDocumentService:
    def __init__(self, parsed_documents: list[ParsedDocument]) -> None:
        self.parsed_documents = iter(parsed_documents)

    async def extract_upload_document(self, upload):
        await upload.close()
        return next(self.parsed_documents)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        APP_ENV="test",
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="test-model",
        TEMP_DIR=tmp_path,
        SUMMARY_INITIAL_CHARS=4000,
        SUMMARY_CHUNK_MAX_CHARS=4000,
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


def test_long_document_uses_opening_and_toc_in_one_model_call(tmp_path: Path) -> None:
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
    assert result.summary == f"摘要1。{SUMMARY_STAMP_NOTE}"
    assert len(llm.inputs) == 1
    assert "疑似目录" in llm.inputs[0]
    assert "文档开头内容" in llm.inputs[0]
    assert llm.max_chars == [40]


def test_long_document_without_toc_uses_only_first_4000_characters(tmp_path: Path) -> None:
    parsed = ParsedDocument(text="甲" * 5000, filename="材料.txt", extension="txt")
    llm = FakeLLMClient()
    service = DocumentSummaryService(make_settings(tmp_path), FakeDocumentService(parsed), llm)
    upload = UploadFile(filename="材料.txt", file=BytesIO(b"content"))

    result = asyncio.run(service.summarize_uploads([upload]))[0]

    assert result.status == "succeeded"
    assert len(llm.inputs) == 1
    assert "疑似目录" not in llm.inputs[0]
    assert llm.inputs[0] == f"文档开头内容：\n{'甲' * 4000}"


def test_summary_preserves_overlong_model_output(tmp_path: Path) -> None:
    service = DocumentSummaryService(make_settings(tmp_path))
    results = [DocumentSummaryItem(filename="sample.txt", status="succeeded", summary="甲" * 100)]
    service._format_summary_results(results)

    assert results[0].summary == "甲" * 100 + f"。{SUMMARY_STAMP_NOTE}"
    assert len(results[0].summary) == 108


def test_summary_removes_unreadable_notice_and_terminal_punctuation(tmp_path: Path) -> None:
    service = DocumentSummaryService(make_settings(tmp_path))
    results = [
        DocumentSummaryItem(
            filename="sample.txt",
            status="succeeded",
            summary="可读取内容有限。正文内容。",
        )
    ]

    service._format_summary_results(results)

    assert results[0].summary == f"正文内容。{SUMMARY_STAMP_NOTE}"


def test_summary_prompt_does_not_direct_model_to_emit_unreadable_notice() -> None:
    assert "可读取内容有限" not in SYSTEM_PROMPT


def test_summary_evenly_limits_multiple_successful_results(tmp_path: Path) -> None:
    service = DocumentSummaryService(make_settings(tmp_path))
    results = [
        DocumentSummaryItem(filename=f"sample-{index}.txt", status="succeeded", summary="甲" * 100)
        for index in range(3)
    ]

    service._format_summary_results(results)

    assert [len(item.summary or "") for item in results] == [101, 101, 108]
    assert results[0].summary.endswith("；")
    assert results[1].summary.endswith("；")
    assert results[2].summary.endswith(f"。{SUMMARY_STAMP_NOTE}")


def test_summary_passes_precomputed_multiple_file_limits_to_llm(tmp_path: Path) -> None:
    parsed_documents = [
        ParsedDocument(text=f"文档{index}", filename=f"sample-{index}.txt", extension="txt")
        for index in range(2)
    ]
    llm = FakeLLMClient()
    service = DocumentSummaryService(
        make_settings(tmp_path), MultipleFakeDocumentService(parsed_documents), llm
    )
    uploads = [
        UploadFile(filename=f"sample-{index}.txt", file=BytesIO(b"content")) for index in range(2)
    ]

    results = asyncio.run(service.summarize_uploads(uploads))

    assert llm.max_chars == [44, 45]
    assert all(len(item.summary or "") <= 45 for item in results)
    assert results[0].summary == "摘要1；"
    assert results[1].summary == f"摘要2。{SUMMARY_STAMP_NOTE}"


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
