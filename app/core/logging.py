import logging
import sys

from loguru import logger

from app.core.config import Settings


class InterceptHandler(logging.Handler):
    """Forward standard-library logs (including Uvicorn) to Loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(settings: Settings) -> None:
    def add_defaults(record: dict) -> None:
        record["extra"].setdefault("request_id", "-")

    logger.remove()
    logger.configure(patcher=add_defaults)
    logger.add(
        sys.stderr,
        level=settings.log_level.upper(),
        serialize=settings.log_json,
        backtrace=False,
        diagnose=False,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | request_id={extra[request_id]} | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
    )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        standard_logger = logging.getLogger(name)
        standard_logger.handlers = [InterceptHandler()]
        standard_logger.propagate = False

