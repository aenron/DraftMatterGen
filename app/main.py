import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

from app.api.routes import router
from app.core.config import Settings, get_settings
from app.core.errors import ServiceError
from app.core.logging import configure_logging
from app.services.draft_reason_service import DraftReasonService


def create_app(
    settings: Settings | None = None, draft_reason_service: DraftReasonService | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    app = FastAPI(title=settings.app_name, version="1.0.0")
    app.state.settings = settings
    app.state.draft_reason_service = draft_reason_service or DraftReasonService(settings)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        started_at = time.perf_counter()
        with logger.contextualize(request_id=request.state.request_id):
            logger.info(
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
            logger.info(
                "request_completed method={} path={} status={} duration_ms={:.2f}",
                request.method,
                request.url.path,
                response.status_code,
                (time.perf_counter() - started_at) * 1000,
            )
            return response

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        logger.warning(
            "service_error code={} status={} message={}",
            exc.code,
            exc.status_code,
            exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "data": None,
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.warning("request_validation_failed errors={}", len(exc.errors()))
        return JSONResponse(
            status_code=422,
            content={
                "code": "INVALID_REQUEST",
                "message": "请求参数校验失败",
                "data": None,
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

    app.include_router(router)
    logger.info(
        "application_configured app={} env={} llm_model={} api_key_enabled={}",
        settings.app_name,
        settings.app_env,
        settings.llm_model or "not-configured",
        settings.api_key_enabled,
    )
    return app


app = create_app()
