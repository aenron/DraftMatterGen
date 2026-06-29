import re

from loguru import logger

from app.core.config import Settings
from app.core.logging import configure_logging
from app.main import _request_log_context


def test_plain_log_uses_tenths_and_context_fields(capsys) -> None:
    configure_logging(Settings(LOG_LEVEL="INFO", LOG_JSON=False))

    with logger.contextualize(
        request_id="request-1",
        business="draft_reason",
        mode="async_worker",
        job_id="job-1",
        filename="sample.docx",
    ):
        logger.info("test message")

    output = capsys.readouterr().err
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d \|", output)
    assert "request-1" in output
    assert "业务=draft_reason" in output
    assert "模式=async_worker" in output
    assert "任务=job-1" in output
    assert "文件=sample.docx" in output


def test_request_log_context_classifies_api_routes() -> None:
    assert _request_log_context("/api/v1/draft-reasons/extract") == {
        "business": "draft_reason",
        "mode": "sync",
        "job_id": "-",
        "filename": "-",
    }
    assert _request_log_context("/api/v1/document-summaries/extract-async")["mode"] == (
        "async_submit"
    )
    assert _request_log_context("/api/v1/document-summaries/jobs/job-123") == {
        "business": "document_summary",
        "mode": "async_status",
        "job_id": "job-123",
        "filename": "-",
    }
