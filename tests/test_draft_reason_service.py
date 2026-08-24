import asyncio
from io import BytesIO

from fastapi import UploadFile

from app.core.config import Settings
from app.core.draft_type import DraftType
from app.services.draft_reason_service import DraftReasonService


def test_fixed_endings_are_appended_once() -> None:
    letter_ending = "特此致函，恳请予以支持配合。"
    report_ending = "特此报告。"

    assert DraftReasonService._append_fixed_ending("办理事项。", DraftType.LETTER) == (
        f"办理事项。{letter_ending}"
    )
    assert DraftReasonService._append_fixed_ending("办理事项。", DraftType.REPORT) == (
        f"办理事项。{report_ending}"
    )
    assert DraftReasonService._append_fixed_ending(
        f"办理事项。{report_ending}", DraftType.REPORT
    ) == f"办理事项。{report_ending}"
    assert DraftReasonService._append_fixed_ending(
        "办理事项。", DraftType.REQUEST_OR_SUBMISSION
    ) == "办理事项。"


class FakeDocumentService:
    async def extract_upload(self, upload):
        await upload.read()
        await upload.close()
        return "文档内容", "sample.txt"


class FakeLLMClient:
    def __init__(self) -> None:
        self.draft_types = []

    async def extract_draft_reason(self, document_text, draft_type=None):
        self.draft_types.append(draft_type)
        return "根据工作需要，拟办理相关事项。"


def test_explicit_report_uses_type_and_appends_ending(tmp_path) -> None:
    llm_client = FakeLLMClient()
    service = DraftReasonService(
        Settings(TEMP_DIR=tmp_path),
        document_service=FakeDocumentService(),
        llm_client=llm_client,
    )
    upload = UploadFile(filename="sample.txt", file=BytesIO(b"test"))

    reason, filename, chars = asyncio.run(
        service.extract_from_upload(upload, DraftType.REPORT)
    )

    assert reason == "根据工作需要，拟办理相关事项。特此报告。"
    assert filename == "sample.txt"
    assert chars == 4
    assert llm_client.draft_types == [DraftType.REPORT]
