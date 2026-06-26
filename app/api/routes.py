import secrets
from pathlib import Path

from fastapi import APIRouter, File, Header, Query, Request, UploadFile

from app.api.schemas import (
    AsyncJobData,
    AsyncJobResponse,
    AsyncJobSubmissionData,
    AsyncJobSubmissionResponse,
    DocumentSummaryAsyncJobData,
    DocumentSummaryAsyncJobResponse,
    DocumentSummaryData,
    DocumentSummaryResponse,
    DraftReasonData,
    DraftReasonResponse,
    HealthResponse,
)
from app.core.errors import ServiceError
from app.services.async_job_manager import JobType


router = APIRouter()


def _check_api_key(request: Request, provided: str | None) -> None:
    settings = request.app.state.settings
    if not settings.api_key_enabled:
        return
    configured = settings.service_api_key
    if configured is None or provided is None or not secrets.compare_digest(
        configured.get_secret_value(), provided
    ):
        raise ServiceError(401, "UNAUTHORIZED", "接口鉴权失败")


@router.post("/api/v1/draft-reasons/extract", response_model=DraftReasonResponse)
async def extract_draft_reason(
    request: Request,
    file: UploadFile = File(...),
    include_metadata: bool = Query(False),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> DraftReasonResponse:
    _check_api_key(request, x_api_key)
    reason, filename, chars = await request.app.state.draft_reason_service.extract_from_upload(file)
    request.state.filename = filename
    request.state.source_chars = chars
    request.state.result_chars = len(reason)
    return DraftReasonResponse(
        data=DraftReasonData(
            draft_reason=reason,
            filename=filename if include_metadata else None,
            chars_processed=chars if include_metadata else None,
        ),
        request_id=request.state.request_id,
    )


@router.post("/api/v1/document-summaries/extract", response_model=DocumentSummaryResponse)
async def extract_document_summaries(
    request: Request,
    files: list[UploadFile] = File(...),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> DocumentSummaryResponse:
    _check_api_key(request, x_api_key)
    request.state.filename = ",".join(Path(file.filename or "").name or "-" for file in files)
    summaries = await request.app.state.document_summary_service.summarize_uploads(files)
    request.state.result_chars = sum(len(item.summary or "") for item in summaries)
    return DocumentSummaryResponse(
        data=DocumentSummaryData(summaries=summaries),
        request_id=request.state.request_id,
    )


@router.post(
    "/api/v1/document-summaries/extract-async",
    response_model=AsyncJobSubmissionResponse,
    status_code=202,
)
async def submit_document_summary_job(
    request: Request,
    files: list[UploadFile] = File(...),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> AsyncJobSubmissionResponse:
    _check_api_key(request, x_api_key)
    request.state.filename = ",".join(Path(file.filename or "").name or "-" for file in files)
    record = await request.app.state.async_job_manager.submit_document_summary(
        files, request.state.request_id
    )
    status_url = str(request.url_for("get_document_summary_job", job_id=record.job_id))
    return AsyncJobSubmissionResponse(
        data=AsyncJobSubmissionData(
            job_id=record.job_id,
            status=record.status,
            status_url=status_url,
        ),
        request_id=request.state.request_id,
    )


@router.get(
    "/api/v1/document-summaries/jobs/{job_id}",
    response_model=DocumentSummaryAsyncJobResponse,
)
async def get_document_summary_job(
    job_id: str,
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> DocumentSummaryAsyncJobResponse:
    _check_api_key(request, x_api_key)
    snapshot = await request.app.state.async_job_manager.get(job_id, JobType.DOCUMENT_SUMMARY)
    return DocumentSummaryAsyncJobResponse(
        data=DocumentSummaryAsyncJobData.model_validate(snapshot),
        request_id=request.state.request_id,
    )


@router.post(
    "/api/v1/draft-reasons/extract-async",
    response_model=AsyncJobSubmissionResponse,
    status_code=202,
)
async def submit_draft_reason_job(
    request: Request,
    file: UploadFile = File(...),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> AsyncJobSubmissionResponse:
    _check_api_key(request, x_api_key)
    request.state.filename = Path(file.filename or "").name or "-"
    record = await request.app.state.async_job_manager.submit(file, request.state.request_id)
    status_url = str(request.url_for("get_draft_reason_job", job_id=record.job_id))
    return AsyncJobSubmissionResponse(
        data=AsyncJobSubmissionData(
            job_id=record.job_id,
            status=record.status,
            status_url=status_url,
        ),
        request_id=request.state.request_id,
    )


@router.get(
    "/api/v1/draft-reasons/jobs/{job_id}",
    response_model=AsyncJobResponse,
)
async def get_draft_reason_job(
    job_id: str,
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> AsyncJobResponse:
    _check_api_key(request, x_api_key)
    snapshot = await request.app.state.async_job_manager.get(job_id, JobType.DRAFT_REASON)
    return AsyncJobResponse(
        data=AsyncJobData.model_validate(snapshot),
        request_id=request.state.request_id,
    )


@router.get("/health/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/health/ready", response_model=HealthResponse)
async def ready(request: Request) -> HealthResponse:
    if not request.app.state.settings.llm_ready:
        raise ServiceError(503, "LLM_NOT_CONFIGURED", "LLM 服务尚未配置")
    return HealthResponse(status="ready")
