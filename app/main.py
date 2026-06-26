import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

from app.api.routes import router
from app.core.config import Settings, get_settings
from app.core.errors import ServiceError
from app.core.logging import configure_logging
from app.services.draft_reason_service import DraftReasonService
from app.services.async_job_manager import AsyncJobManager
from app.services.document_summary_service import DocumentSummaryService


def create_app(
    settings: Settings | None = None,
    draft_reason_service: DraftReasonService | None = None,
    document_summary_service: DocumentSummaryService | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    service = draft_reason_service or DraftReasonService(settings)
    summary_service = document_summary_service
    if summary_service is None:
        llm_client = getattr(service, "llm_client", None)
        summary_service = DocumentSummaryService(settings, llm_client=llm_client)
    async_job_manager = AsyncJobManager(settings, service, summary_service)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await async_job_manager.start()
        try:
            yield
        finally:
            await async_job_manager.stop()

    app = FastAPI(title=settings.app_name, version="1.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.draft_reason_service = service
    app.state.document_summary_service = summary_service
    app.state.async_job_manager = async_job_manager

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        started_at = time.perf_counter()
        with logger.contextualize(request_id=request.state.request_id):
            logger.debug(
                "request_started method={} path={} client={}",
                request.method,
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            try:
                response = await call_next(request)
            except Exception:
                logger.exception(
                    "request_failed method={} path={} duration_ms={:.2f}",
                    request.method,
                    request.url.path,
                    (time.perf_counter() - started_at) * 1000,
                )
                raise
            response.headers["X-Request-ID"] = request.state.request_id
            if request.url.path.startswith("/health/"):
                completion_logger = logger.debug
            elif response.status_code >= 400:
                completion_logger = logger.warning
            else:
                completion_logger = logger.info
            completion_logger(
                "{} 请求完成 | 状态={}{} | 耗时={:.2f}s | 结果长度={}字符 | 文件名={}",
                "✅" if response.status_code < 400 else "❌",
                response.status_code,
                (
                    " | 错误码=" + request.state.error_code
                    if hasattr(request.state, "error_code")
                    else ""
                ),
                time.perf_counter() - started_at,
                getattr(request.state, "result_chars", "-"),
                getattr(request.state, "filename", "-"),
            )
            return response

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        request.state.error_code = exc.code
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.status_code,
                "error_code": exc.code,
                "message": exc.message,
                "data": None,
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request.state.error_code = "INVALID_REQUEST"
        logger.debug("request_validation_failed errors={}", len(exc.errors()))
        return JSONResponse(
            status_code=422,
            content={
                "code": 422,
                "error_code": "INVALID_REQUEST",
                "message": "请求参数校验失败",
                "data": None,
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

    app.include_router(router)
    logger.info(
        "🚀 应用配置完成 | 服务={} | 环境={} | 模型={} | 接口鉴权={}",
        settings.app_name,
        settings.app_env,
        settings.llm_model or "not-configured",
        "开启" if settings.api_key_enabled else "关闭",
    )
    return app


app = create_app()
