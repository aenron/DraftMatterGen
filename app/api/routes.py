import secrets

from fastapi import APIRouter, File, Header, Query, Request, UploadFile

from app.api.schemas import DraftReasonData, DraftReasonResponse, HealthResponse
from app.core.errors import ServiceError


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
    return DraftReasonResponse(
        data=DraftReasonData(
            draft_reason=reason,
            filename=filename if include_metadata else None,
            chars_processed=chars if include_metadata else None,
        ),
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

